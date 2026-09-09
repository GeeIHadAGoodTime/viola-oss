from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger
from core.validation import (
    sanitize_error_message,
    validate_query,
    validate_seek_seconds,
    validate_volume,
)
from diagnostics.queue_history import QueueEventType, log_queue_event

from .models import DispatchResult, IntentPayload

logger = get_logger(__name__)


# Deduplication window in seconds - ignore duplicate commands within this window
_DEDUP_WINDOW_SECONDS = 5.0
# Maximum cached command results to prevent memory growth
_DEDUP_CACHE_MAX_SIZE = 100


class IntentBridgeError(RuntimeError):
    """Base error for intent bridge dispatch issues.

    Base error for all intent bridge dispatch issues (HTTP bridge layer).
    """


class UnknownIntentError(IntentBridgeError):
    """Raised when the dispatcher lacks a handler for a given intent."""


class ValidationFailedError(IntentBridgeError):
    """Raised when user-provided parameters fail validation."""

    def __init__(self, message: str, intent: str) -> None:
        super().__init__(message)
        self.intent = intent


class PolicyViolation(IntentBridgeError):
    """Raised when an intent violates system policy."""

    def __init__(self, message: str, intent: str) -> None:
        super().__init__(message)
        self.intent = intent


# Backward compatibility alias - deprecated, use IntentBridgeError
DispatchError = IntentBridgeError


class _MusicAdapter(Protocol):
    def status(self) -> ResponseEnvelope: ...

    def play(self, query: str, *, source: str | None = None, interrupt: bool = ...) -> JsonValue: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def skip(self) -> None: ...

    def previous(self) -> None: ...

    def set_volume(self, volume: int) -> int: ...

    def change_volume(self, delta: int) -> int: ...

    def seek(self, position: int) -> JsonValue: ...

    def transfer(self, device: str) -> JsonValue: ...

    def transfer_device(self, device: str) -> JsonValue: ...


def _as_str(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: JsonValue | None) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class CommandContext:
    music: _MusicAdapter | None

    def status(self) -> ResponseEnvelope:
        if self.music:
            try:
                return self.music.status()
            except Exception as exc:
                logger.debug("Failed to fetch music status: %s", exc)
        return success_response({})

    def emit_state_change(self) -> None:
        """Emit state change notification if music adapter supports it."""
        if self.music:
            try:
                # Try MusicControllerAdapter's emit_state_change method first
                emit_state_change = getattr(self.music, "emit_state_change", None)
                if callable(emit_state_change):
                    emit_state_change()
                    return
                emit = getattr(self.music, "_emit", None)
                if callable(emit):
                    emit()
            except Exception as exc:
                logger.debug("Failed to emit state change: %s", exc)


class CommandDispatcher:
    """Maps legacy intent types to executable adapters.

    Features:
    - Command deduplication via command_id (prevents duplicate execution on retry)
    - Structured logging of all dispatched intents
    """

    def __init__(self, music: _MusicAdapter | None) -> None:
        self._ctx = CommandContext(music=music)
        # Deduplication cache: command_id -> (timestamp, ResponseEnvelope)
        self._dedup_cache: dict[str, tuple[float, ResponseEnvelope]] = {}
        self._dedup_lock = threading.Lock()
        self._handlers: dict[str, Callable[[JsonDict], ResponseEnvelope]] = {
            # Play intents
            "play": self._handle_play,
            "play_music": self._handle_play,
            "queue": self._handle_play,  # "add to queue" maps to play
            # Pause/Resume
            "pause": self._handle_pause,
            "resume": self._handle_resume,
            "unpause": self._handle_resume,  # Alias
            # Stop
            "stop": self._handle_stop,
            "halt": self._handle_stop,  # Alias
            # Skip
            "next": self._handle_next,
            "skip": self._handle_next,  # Alias
            "next_track": self._handle_next,  # Alias
            # Previous
            "previous": self._handle_previous,
            "back": self._handle_previous,  # Alias
            "prev": self._handle_previous,  # Alias
            # Volume
            "set_volume": self._handle_set_volume,
            "volume": self._handle_set_volume,  # Alias
            "change_volume": self._handle_change_volume,
            "volume_up": self._handle_change_volume,  # Maps to +10
            "volume_down": self._handle_change_volume,  # Maps to -10
            "louder": self._handle_change_volume,  # Alias
            "quieter": self._handle_change_volume,  # Alias
            # Seek
            "seek": self._handle_seek,
            "jump": self._handle_seek,  # Alias
            # Transfer
            "transfer": self._handle_transfer,
            "transfer_device": self._handle_transfer,
        }

    # ------------------------------------------------------------------ public
    def can_handle(self, intent_type: str) -> bool:
        return intent_type in self._handlers

    def _check_dedup_cache(self, command_id: str) -> ResponseEnvelope | None:
        """Check if command was recently executed. Returns cached result or None."""
        now = time.time()
        with self._dedup_lock:
            # Clean up expired entries
            expired_keys = [k for k, (ts, _) in self._dedup_cache.items() if now - ts > _DEDUP_WINDOW_SECONDS]
            for k in expired_keys:
                del self._dedup_cache[k]

            # Check for cached result
            if command_id in self._dedup_cache:
                ts, result = self._dedup_cache[command_id]
                if now - ts <= _DEDUP_WINDOW_SECONDS:
                    return result
        return None

    def _store_dedup_result(self, command_id: str, result: ResponseEnvelope) -> None:
        """Store command result in dedup cache."""
        with self._dedup_lock:
            # Evict oldest entries if cache is full
            while len(self._dedup_cache) >= _DEDUP_CACHE_MAX_SIZE:
                oldest_key = min(self._dedup_cache.keys(), key=lambda k: self._dedup_cache[k][0])
                del self._dedup_cache[oldest_key]
            self._dedup_cache[command_id] = (time.time(), result)

    def dispatch(self, intent: IntentPayload) -> ResponseEnvelope:
        # Log intent receive event
        log_queue_event(
            QueueEventType.INTENT_RECEIVE,
            command_id=intent.command_id,
            extra={
                "intent_type": intent.type,
                "params": intent.params,
            },
        )

        # Check deduplication cache if command_id is provided
        if intent.command_id:
            cached_result = self._check_dedup_cache(intent.command_id)
            if cached_result is not None:
                log_queue_event(
                    QueueEventType.INTENT_DUPLICATE_IGNORED,
                    command_id=intent.command_id,
                    reason="duplicate_within_window",
                    extra={"intent_type": intent.type},
                )
                logger.debug(
                    "Duplicate command_id=%s ignored (already executed within %ss)",
                    intent.command_id,
                    _DEDUP_WINDOW_SECONDS,
                )
                return cached_result

        # Normalize intent type: handle delegated intents and aliases
        intent_type = intent.type

        # Handle delegated intents from GPT/interpreter
        if intent_type == "delegated":
            # Extract the actual command from params
            command_obj = intent.params.get("command") or intent.params.get("original_intent")
            command = _as_str(command_obj)
            if command:
                intent_type = command
            else:
                # If no command found, try to infer from params
                if "query" in intent.params or "play" in str(intent.params).lower():
                    intent_type = "play"
                else:
                    raise UnknownIntentError(f"delegated intent missing command: {intent.params}")

        handler = self._handlers.get(intent_type)
        if handler is None:
            raise UnknownIntentError(intent_type)

        # Log dispatch event
        log_queue_event(
            QueueEventType.INTENT_DISPATCH,
            command_id=intent.command_id,
            extra={
                "intent_type": intent_type,
                "handler": handler.__name__ if handler else None,
            },
        )

        result = handler(intent.params)

        # Store result in dedup cache
        if intent.command_id:
            self._store_dedup_result(intent.command_id, result)

        return result

    # --------------------------------------------------------------- handlers
    def _handle_play(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "play"},
            )
        query_obj = params.get("query")
        query = query_obj if isinstance(query_obj, str) else ""
        is_valid, error_msg = validate_query(query)
        if not is_valid:
            raise ValidationFailedError(error_msg, "play")
        source = _as_str(params.get("source"))
        try:
            result = self._ctx.music.play(query, source=source, interrupt=True)

            # Check if the music adapter returned a failure response
            if isinstance(result, Mapping) and result.get("ok") is False:
                error_obj = result.get("error", {})
                if isinstance(error_obj, Mapping):
                    error_code = _as_str(to_json_value(error_obj.get("code"))) or "play_failed"
                    error_msg = _as_str(to_json_value(error_obj.get("message"))) or f"Failed to play: {query}"
                else:
                    error_code = _as_str(to_json_value(error_obj)) or "play_failed"
                    error_msg = f"Failed to play: {query}"
                logger.error("Play command returned failure: %s - %s", error_code, error_msg)
                return failure_response(
                    error_code,
                    error_msg,
                    data={"intent": "play", "query": query},
                )

            self._ctx.emit_state_change()
            payload: JsonDict = {
                "result": result,
                "status": to_json_value(self._ctx.status()),
            }
            return DispatchResult(ok=True, intent="play", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Play command failed")
            return failure_response(
                "play_failed",
                f"Failed to play: {exc!s}",
                data={"intent": "play", "query": query},
            )

    def _handle_pause(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "pause"},
            )
        try:
            self._ctx.music.pause()
            self._ctx.emit_state_change()
            payload: JsonDict = {"status": to_json_value(self._ctx.status())}
            return DispatchResult(ok=True, intent="pause", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Pause command failed")
            return failure_response(
                "pause_failed",
                f"Failed to pause: {exc!s}",
                data={"intent": "pause"},
            )

    def _handle_resume(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "resume"},
            )
        try:
            self._ctx.music.resume()
            self._ctx.emit_state_change()
            payload: JsonDict = {"status": to_json_value(self._ctx.status())}
            return DispatchResult(ok=True, intent="resume", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Resume command failed")
            return failure_response(
                "resume_failed",
                f"Failed to resume: {exc!s}",
                data={"intent": "resume"},
            )

    def _handle_stop(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "stop"},
            )
        try:
            self._ctx.music.stop()
            self._ctx.emit_state_change()
            payload: JsonDict = {"status": to_json_value(self._ctx.status())}
            return DispatchResult(ok=True, intent="stop", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Stop command failed")
            return failure_response(
                "stop_failed",
                f"Failed to stop: {exc!s}",
                data={"intent": "stop"},
            )

    def _handle_next(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "next"},
            )
        try:
            self._ctx.music.skip()
            self._ctx.emit_state_change()
            payload: JsonDict = {"status": to_json_value(self._ctx.status())}
            return DispatchResult(ok=True, intent="next", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Next command failed")
            return failure_response(
                "next_failed",
                f"Failed to skip to next track: {exc!s}",
                data={"intent": "next"},
            )

    def _handle_previous(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "previous"},
            )
        try:
            self._ctx.music.previous()
            self._ctx.emit_state_change()
            payload: JsonDict = {"status": to_json_value(self._ctx.status())}
            return DispatchResult(ok=True, intent="previous", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Previous command failed")
            return failure_response(
                "previous_failed",
                f"Failed to go to previous track: {exc!s}",
                data={"intent": "previous"},
            )

    def _handle_set_volume(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "set_volume"},
            )
        value = params.get("value")
        is_valid, error_msg = validate_volume(value)
        if not is_valid:
            raise ValidationFailedError(error_msg, "set_volume")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationFailedError("Volume must be an integer.", "set_volume")
        try:
            result = self._ctx.music.set_volume(value)
            self._ctx.emit_state_change()
            payload: JsonDict = {
                "volume": result,
                "status": to_json_value(self._ctx.status()),
            }
            return DispatchResult(ok=True, intent="set_volume", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Set volume command failed")
            return failure_response(
                "set_volume_failed",
                f"Failed to set volume: {exc!s}",
                data={"intent": "set_volume", "value": value},
            )

    def _handle_change_volume(self, params: JsonDict) -> ResponseEnvelope:
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "change_volume"},
            )
        delta = _as_int(params.get("delta")) or 0
        is_valid, error_msg = validate_volume(50 + delta)
        if not is_valid:
            raise ValidationFailedError(error_msg, "change_volume")
        try:
            result = self._ctx.music.change_volume(delta)
            self._ctx.emit_state_change()
            payload: JsonDict = {
                "volume": result,
                "status": to_json_value(self._ctx.status()),
            }
            return DispatchResult(ok=True, intent="change_volume", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Change volume command failed")
            return failure_response(
                "change_volume_failed",
                f"Failed to change volume: {exc!s}",
                data={"intent": "change_volume", "delta": delta},
            )

    def _handle_seek(self, params: JsonDict) -> ResponseEnvelope:
        """Handle seek command with support for both 'seconds' and 'value' parameters."""
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "seek"},
            )

        # Support both "seconds" (from dispatcher) and "value" (from interpreter)
        seconds = params.get("seconds")
        if seconds is None:
            value = params.get("value")
            if value is None:
                raise ValidationFailedError("Missing 'seconds' or 'value' parameter", "seek")
            # Convert value to seconds (handles both numeric strings and HMS format)
            seconds = self._parse_seek_value(value)

        is_valid, error_msg = validate_seek_seconds(seconds)
        if not is_valid:
            raise ValidationFailedError(error_msg, "seek")

        try:
            seconds_int = _as_int(seconds)
            if seconds_int is None:
                raise ValidationFailedError("Seek seconds must be an integer.", "seek")
            result = self._ctx.music.seek(seconds_int)
            self._ctx.emit_state_change()
            payload: JsonDict = {
                "intent": "seek",
                "position": seconds_int,
                "result": result,
                "status": to_json_value(self._ctx.status()),
            }
            return success_response(payload)
        except Exception as exc:
            logger.exception("Seek command failed")
            return failure_response(
                "seek_failed",
                f"Failed to seek: {exc!s}",
                data={"intent": "seek", "position": to_json_value(seconds)},
            )

    def _parse_seek_value(self, value: JsonValue) -> int:
        """
        Parse seek value to seconds.
        Supports:
        - Numeric strings: "30", "+10", "-5"
        - HMS format: "1:23:45", "2:30"
        """
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)

        value_str = str(value).strip()

        # Try signed integer first
        if re.fullmatch(r"[+-]?\d+", value_str):
            return int(value_str)

        # Try HMS format (h:mm:ss or mm:ss)
        hms_match = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$", value_str)
        if hms_match:
            hours = int(hms_match.group(1) or 0)
            minutes = int(hms_match.group(2))
            seconds = int(hms_match.group(3))
            return hours * 3600 + minutes * 60 + seconds

        # If we can't parse it, try converting to int directly
        try:
            return int(value_str)
        except (ValueError, TypeError):
            raise ValidationFailedError(f"Invalid seek value format: {value_str}", "seek")

    def _handle_transfer(self, params: JsonDict) -> ResponseEnvelope:
        """Handle transfer command to move playback to another device."""
        if not self._ctx.music:
            return failure_response(
                "player_unavailable",
                "Music player unavailable.",
                data={"intent": "transfer"},
            )

        device_obj = params.get("device") or params.get("target")
        device = _as_str(device_obj)
        if not device:
            raise ValidationFailedError("Missing 'device' or 'target' parameter", "transfer")

        try:
            # Try transfer method on music adapter/player
            transfer = getattr(self._ctx.music, "transfer", None)
            transfer_device = getattr(self._ctx.music, "transfer_device", None)
            if callable(transfer):
                result = transfer(device)
            elif callable(transfer_device):
                result = transfer_device(device)
            else:
                return failure_response(
                    "transfer_not_supported",
                    "Transfer not supported by music player.",
                    data={"intent": "transfer", "device": device},
                )

            self._ctx.emit_state_change()
            payload: JsonDict = {
                "device": device,
                "result": result,
                "status": to_json_value(self._ctx.status()),
            }
            return DispatchResult(ok=True, intent="transfer", payload=payload).to_envelope()
        except Exception as exc:
            logger.exception("Transfer command failed")
            return failure_response(
                "transfer_failed",
                "Failed to transfer to device. Please try again.",
                data={"intent": "transfer", "device": device},
            )


def handle_dispatch_error(intent_type: str, exc: Exception) -> ResponseEnvelope:
    if isinstance(exc, PolicyViolation):
        return failure_response(
            "policy_violation",
            str(exc),
            data={"intent": exc.intent},
        )
    if isinstance(exc, ValidationFailedError):
        return failure_response(
            "validation_failed",
            str(exc),
            data={"intent": exc.intent},
        )
    if isinstance(exc, UnknownIntentError):
        logger.warning("Unknown intent type received: %s", intent_type)
        return failure_response(
            "unknown_intent",
            "No handler registered for '%s'. Try rephrasing your command." % intent_type,
            data={"intent": intent_type},
        )
    if isinstance(exc, KeyError):
        message = sanitize_error_message(exc)
        logger.error("Missing required parameter: %s", message)
        return failure_response(
            "missing_parameter",
            "A required parameter is missing.",
            data={"intent": intent_type, "parameter": message},
        )

    message = sanitize_error_message(exc)
    logger.exception("dispatch failed")
    return failure_response(
        "dispatch_failed",
        message or "Dispatch failed.",
        data={"intent": intent_type},
    )


__all__ = [
    "CommandDispatcher",
    "DispatchError",
    "PolicyViolation",
    "UnknownIntentError",
    "ValidationFailedError",
    "handle_dispatch_error",
]
