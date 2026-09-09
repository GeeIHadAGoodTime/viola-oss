"""
Audio Core Integration Adapter
==============================

Bridges the audio service (`AudioServiceAPI`) with the legacy music
player implementation to enable a flag-driven migration path.

Goals
-----
* Provide a drop-in replacement for the legacy `MusicPlayer` surface so the
  broader application can toggle between implementations without code changes.
* Keep the authoritative playback queue/state in the new audio core while
  delegating actual media playback to the legacy backend until the Batch C/E
  transports are fully integrated.
* Support hot feature flag switches (no process restart) and canary rollouts.
* Run shadow comparisons to detect regressions before flipping traffic.

The adapter intentionally avoids reaching into the legacy player's private
attributes—only public methods (`play`, `pause`, `state`, …) are used—while
invoking the audio core through its asynchronous API.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from audio_core import AudioServiceAPI, AudioServiceError
from audio_core.service_api import AudioServiceError as ServiceError
from audio_core.state_machine import PlaybackState
from core.constants import TIMEOUT_DEFAULT
from core.logging_config import StructuredLogger, get_logger
from models.player import PlayerState, QueueItem

_LOGGER = get_logger("audio_core.integration.adapter")

_T = TypeVar("_T")


class _ShadowExecutor(Protocol):
    def submit_sync(self, coro: Coroutine[object, object, _T]) -> _T: ...

    def submit_shadow(self, coro: Coroutine[object, object, object]) -> None: ...


def _load_yaml_mapping(path: Path, logger: logging.Logger | StructuredLogger) -> dict[str, object]:
    try:
        yaml_module = importlib.import_module("yaml")
    except ImportError:
        logger.warning("PyYAML not installed; audio-core feature flags default to disabled")
        return {}

    safe_load_obj: object = getattr(yaml_module, "safe_load", None)
    if safe_load_obj is None or not callable(safe_load_obj):
        logger.warning("yaml.safe_load not available; audio-core feature flags default to disabled")
        return {}

    safe_load = cast(Callable[[str], object], safe_load_obj)
    raw = path.read_text(encoding="utf-8")
    loaded = safe_load(raw)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        logger.warning("Audio flag file did not parse to a mapping; defaulting to disabled")
        return {}
    return cast(dict[str, object], loaded)


# --------------------------------------------------------------------------- #
# Feature flag loading                                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class AudioCoreFlagSnapshot:
    """
    Immutable snapshot of the audio flag configuration.

    Attributes:
        enabled: When True the new audio core handles production traffic.
        canary_enabled: Enable cohort based rollout using a deterministic hash.
        canary_sample: Fraction (0.0-1.0) of cohorts receiving new core traffic.
        comparison_enabled: When True run the new core in shadow mode even when
            not serving traffic.
        comparison_sample: Fraction (0.0-1.0) of requests to shadow test.
        timestamp: UNIX timestamp when the snapshot was produced.
    """

    enabled: bool = False
    canary_enabled: bool = False
    canary_sample: float = 0.0
    comparison_enabled: bool = False
    comparison_sample: float = 0.0
    timestamp: float = 0.0


class AudioCoreFeatureFlags:
    """
    Lightweight YAML-backed feature flag reader with hot-reload semantics.

    The loader watches the configured file's mtime and reloads on demand. This
    keeps the adapter responsive to manual toggles, rollout automation, and
    incident-driven rollbacks without requiring application restart.
    """

    def __init__(
        self,
        path: Path,
        *,
        logger: logging.Logger | None = None,
        refresh_interval: float = 2.0,
    ) -> None:
        self._path = Path(path)
        self._logger = logger or get_logger("audio_core.integration.flags")
        self._refresh_interval = max(0.5, refresh_interval)
        self._lock = threading.RLock()
        self._snapshot: AudioCoreFlagSnapshot = AudioCoreFlagSnapshot()
        self._last_refresh = 0.0
        self._mtime = 0.0
        self._reload(initial=True)

    @property
    def path(self) -> Path:
        return self._path

    def _reload(self, *, initial: bool = False) -> None:
        now = time.time()
        if not initial and now - self._last_refresh < self._refresh_interval:
            return

        with self._lock:
            try:
                stat = self._path.stat()
                if not initial and stat.st_mtime <= self._mtime:
                    self._last_refresh = now
                    return
                data = _load_yaml_mapping(self._path, self._logger)
            except FileNotFoundError:
                if initial:
                    self._logger.info(
                        "Audio flag file %s not found; defaulting to legacy mode",
                        self._path,
                    )
                self._snapshot = AudioCoreFlagSnapshot(timestamp=now)
                self._mtime = 0.0
                self._last_refresh = now
                return

            def _clamp(value: object) -> float:
                if isinstance(value, (int, float)):
                    numeric = float(value)
                elif isinstance(value, str):
                    try:
                        numeric = float(value)
                    except ValueError as exc:
                        self._logger.warning(
                            "Failed to clamp value %r, defaulting to 0.0: %s",
                            value,
                            exc,
                        )
                        return 0.0
                else:
                    self._logger.warning(
                        "Failed to clamp value %r, defaulting to 0.0: unsupported type",
                        value,
                    )
                    return 0.0

                return max(0.0, min(numeric, 1.0))

            active_core_raw = data.get("active_core", "")
            active_core = active_core_raw.lower() if isinstance(active_core_raw, str) else ""
            enabled = active_core in {"new", "audio_core"}

            rollout_raw = data.get("rollout", {}) or {}
            rollout = rollout_raw if isinstance(rollout_raw, dict) else {}

            comparison_raw = data.get("comparison", {}) or {}
            comparison = comparison_raw if isinstance(comparison_raw, dict) else {}

            self._snapshot = AudioCoreFlagSnapshot(
                enabled=enabled,
                canary_enabled=bool(
                    (rollout.get("canary") if isinstance(rollout.get("canary"), dict) else {}).get("enabled", False)
                ),
                canary_sample=_clamp(
                    (rollout.get("canary") if isinstance(rollout.get("canary"), dict) else {}).get("sample", 0.0)
                ),
                comparison_enabled=bool(comparison.get("enabled", data.get("shadow_compare", False))),
                comparison_sample=_clamp(comparison.get("sample", data.get("shadow_sample", 0.0))),
                timestamp=now,
            )
            self._mtime = stat.st_mtime
            self._last_refresh = now
            self._logger.debug("Audio flags refreshed: %s", self._snapshot)

    def snapshot(self) -> AudioCoreFlagSnapshot:
        self._reload()
        with self._lock:
            return self._snapshot

    def is_new_core_enabled(self, *, cohort_key: str | None = None) -> bool:
        snap = self.snapshot()
        if snap.enabled:
            return True
        if not snap.canary_enabled or cohort_key is None:
            return False
        return _deterministic_sample(cohort_key, snap.canary_sample)

    def should_run_shadow(self, *, cohort_key: str | None = None) -> bool:
        snap = self.snapshot()
        if not snap.comparison_enabled:
            return False
        if snap.enabled:
            # Already live; keep comparison for regression detection if sample>0
            if snap.comparison_sample <= 0.0:
                return False
            if cohort_key is None:
                return True
        if cohort_key is None:
            return snap.comparison_sample >= 1.0
        return _deterministic_sample(cohort_key, snap.comparison_sample)


def _deterministic_sample(key: str, threshold: float) -> bool:
    if threshold <= 0.0:
        return False
    if threshold >= 1.0:
        return True
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < threshold


# --------------------------------------------------------------------------- #
# Legacy Player Protocol (duck typing interface)                              #
# --------------------------------------------------------------------------- #


class LegacyPlayerProtocol(Protocol):
    """Protocol for legacy music player implementations.

    This defines the expected interface for duck typing. The adapter uses
    getattr() for safe access, so not all methods need to be implemented.
    Methods use flexible signatures to accommodate various implementations.
    """

    def state(self) -> PlayerState:
        """Get current player state."""
        ...

    def play(self, query: str, **kwargs: object) -> QueueItem:
        """Play a track."""
        ...

    def pause(self) -> None:
        """Pause playback."""
        ...

    def resume(self) -> None:
        """Resume playback."""
        ...

    def stop(self) -> None:
        """Stop playback."""
        ...

    def play_next(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
    ) -> QueueItem | None:
        """Skip to next track. Returns next item or None."""
        ...

    def seek(self, position: float) -> None:
        """Seek to position."""
        ...

    def set_volume(self, volume: int) -> int | None:
        """Set volume level. May return new volume."""
        ...

    def enqueue(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
    ) -> QueueItem | None:
        """Add to queue."""
        ...

    def clear_queue(self) -> None:
        """Clear the queue."""
        ...

    def remove_from_queue(self, index: int | str) -> None:
        """Remove item from queue by index or id."""
        ...

    def reorder_queue(self, from_idx: int, to_idx: int) -> None:
        """Reorder queue items."""
        ...


# --------------------------------------------------------------------------- #
# Integration Adapter                                                         #
# --------------------------------------------------------------------------- #


class AudioCoreServiceAdapter:
    """
    Drop-in wrapper around the legacy `MusicPlayer` that keeps the new audio
    service authoritative for queue/state while delegating actual playback.
    """

    def __init__(
        self,
        *,
        audio_service: AudioServiceAPI,
        legacy_player: LegacyPlayerProtocol,
        feature_flags: AudioCoreFeatureFlags,
        cohort_key_provider: Callable[[], str | None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service = audio_service
        self._legacy: LegacyPlayerProtocol = legacy_player
        self.audio_service = audio_service
        self.legacy_player = legacy_player
        self._flags = feature_flags
        self._cohort_key_provider = cohort_key_provider or (lambda: "global")
        self._logger = logger or _LOGGER
        self._lock = threading.RLock()
        # In test/pytest mode, avoid starting a background event loop thread.
        from config.settings import settings

        test_mode = settings.test_mode
        under_pytest = settings.pytest_in_progress
        if test_mode or under_pytest:
            # In test mode, set loop/thread to None to avoid background thread overhead.
            # Runtime checks via getattr() handle None case gracefully.
            self._loop: asyncio.AbstractEventLoop | None = None
            self._loop_thread: threading.Thread | None = None
            self._shadow_executor: _ShadowExecutor = _NoopShadowExecutor()
            self._logger.debug("AudioCoreServiceAdapter running in no-op test mode")
        else:
            self._loop = asyncio.new_event_loop()
            self._loop_thread = threading.Thread(
                target=self._loop.run_forever,
                name="audio-core-adapter-loop",
                daemon=True,
            )
            self._loop_thread.start()
            self._shadow_executor = ThreadedShadowExecutor(self._loop)
        self._logger.debug("AudioCoreServiceAdapter initialised")

    # ----- public compatibility surface ---------------------------------- #
    #
    # These methods mirror the legacy MusicPlayer interface so existing
    # adapters/controllers continue to operate unchanged.

    def play(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        with self._lock:
            # Pass interrupt to legacy player if it supports it
            legacy_play = getattr(self._legacy, "play", None)
            if legacy_play is not None:
                try:
                    item = legacy_play(
                        query,
                        source=source,
                        emit=emit,
                        metadata=metadata,
                        interrupt=interrupt,
                    )
                except TypeError:
                    # Legacy player doesn't support interrupt parameter
                    item = legacy_play(query, source=source, emit=emit, metadata=metadata)
            else:
                raise RuntimeError("Legacy player has no play method")
            self._after_play(item)
            return item

    def play_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
        interrupt: bool = True,
    ):
        # Delegate to legacy async method if available.
        play_async = getattr(self._legacy, "play_async", None)
        if callable(play_async):
            try:
                return play_async(
                    query,
                    source=source,
                    emit=emit,
                    metadata=metadata,
                    interrupt=interrupt,
                )
            except TypeError:
                # Legacy player doesn't support interrupt parameter
                return play_async(query, source=source, emit=emit, metadata=metadata)
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, self.play, query, source)  # pragma: no cover - legacy fallback

    def enqueue(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
    ) -> QueueItem | None:
        with self._lock:
            item = self._legacy.enqueue(query, source, emit=emit, metadata=metadata)
            if item is not None:
                self._sync_from_legacy()
            return item

    def play_next(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: dict | None = None,
    ) -> QueueItem | None:
        with self._lock:
            item = self._legacy.play_next(query, source, emit=emit, metadata=metadata)
            if item is not None:
                self._sync_from_legacy()
            return item

    def pause(self) -> None:
        with self._lock:
            self._legacy.pause()
            self._maybe_call_service(lambda: self._service.pause())
            self._compare_states(reason="pause")

    def resume(self) -> None:
        with self._lock:
            self._legacy.resume()
            self._maybe_call_service(lambda: self._service.resume())
            self._compare_states(reason="resume")

    def stop(self) -> None:
        with self._lock:
            self._legacy.stop()
            self._maybe_call_service(lambda: self._service.stop())
            self._compare_states(reason="stop")

    def skip(self) -> None:
        with self._lock:
            # Try control_surface.skip() first (handles embedded mode correctly)
            control_surface = getattr(self._legacy, "control_surface", None)
            if control_surface and hasattr(control_surface, "skip"):
                control_surface.skip()
            else:
                # Fallback to legacy skip/next
                skip = getattr(self._legacy, "skip", None) or getattr(self._legacy, "next", None)
                if callable(skip):
                    skip()
            self._maybe_call_service(lambda: self._service.skip())
            self._sync_from_legacy()
            self._compare_states(reason="skip")

    def seek(self, seconds: int) -> None:
        with self._lock:
            self._legacy.seek(seconds)
            self._maybe_call_service(lambda: self._service.seek(seconds * 1000))
            self._compare_states(reason="seek")

    def set_volume(self, level: int) -> int:
        with self._lock:
            clamped = self._legacy.set_volume(level)
            self._maybe_call_service(lambda: self._service.set_volume(clamped))
            return clamped

    def change_volume(self, delta: int) -> dict[str, Any]:
        change_volume = getattr(self._legacy, "change_volume", None)
        if callable(change_volume):
            result = change_volume(delta)
            self._maybe_call_service(lambda: self._service.set_volume(result["value"]))
            return result
        current = getattr(self._legacy, "volume", 50)
        new_vol = self.set_volume(int(current) + int(delta))
        return {"value": new_vol, "delta": delta}  # Match dict return type

    def clear_queue(self) -> None:
        with self._lock:
            self._legacy.clear_queue()
            self._maybe_call_service(lambda: self._service.clear_queue(reason="user"))
            self._compare_states(reason="clear_queue")

    def remove_from_queue(self, item_id: str) -> None:
        with self._lock:
            self._legacy.remove_from_queue(item_id)
            self._maybe_call_service(lambda: self._service.remove_from_queue(item_id))
            self._compare_states(reason="remove_from_queue")

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        with self._lock:
            self._legacy.reorder_queue(from_index, to_index)
            self._sync_from_legacy()

    def play_item_now(self, item_id: str) -> None:
        with self._lock:
            play_item_now = getattr(self._legacy, "play_item_now", None)
            if callable(play_item_now):
                play_item_now(item_id)
            self._sync_from_legacy()

    def state(self) -> dict[str, Any]:
        player_state = self._legacy.state()
        # PlayerState is a Pydantic model; convert to dict
        return player_state.model_dump() if hasattr(player_state, "model_dump") else dict(player_state)

    def status(self) -> dict[str, Any]:
        status = getattr(self._legacy, "status", None)
        if callable(status):
            return status()
        return self._build_status_from_state(self._legacy.state())

    def queue(self) -> list[Any]:
        queue = getattr(self._legacy, "queue", None)
        return list(queue() if callable(queue) else getattr(self._legacy, "_queue", []))

    def queue_size(self) -> int:
        size = getattr(self._legacy, "queue_size", None)
        if callable(size):
            return size()
        return len(self.queue())

    @property
    def volume(self) -> int:
        return getattr(self._legacy, "volume", 0)

    def emit_state_change(self) -> None:
        emit = getattr(self._legacy, "emit_state_change", None)
        if callable(emit):
            emit()

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            stop = getattr(self._legacy, "shutdown", None)
            if callable(stop):
                stop()
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as exc:
                self._logger.debug(
                    "Event loop stop failed during shutdown, continuing: %s",
                    exc,
                    exc_info=True,
                )
        if self._loop_thread is not None:
            if self._loop_thread.is_alive():
                self._loop_thread.join(timeout=TIMEOUT_DEFAULT)

    # ----- internals ----------------------------------------------------- #

    def _after_play(self, item: QueueItem) -> None:
        self._sync_from_legacy()
        self._maybe_call_service(lambda: self._service.play(track_id=item.id))
        self._maybe_call_service(lambda: self._service._notify_loaded())
        self._compare_states(reason="play")

    def _sync_from_legacy(self) -> None:
        cohort_key = self._cohort_key_provider()
        if not (
            self._flags.is_new_core_enabled(cohort_key=cohort_key)
            or self._flags.should_run_shadow(cohort_key=cohort_key)
        ):
            return

        state = self._ensure_player_state(self._legacy.state())
        submit = self._shadow_executor.submit_sync
        submit(self._service.clear_queue(reason="sync"))
        for idx, queue_item in enumerate(state.queue):
            allow_duplicates = True if idx > 0 else False
            submit(
                self._service.add_to_queue(
                    queue_item,
                    position=None,
                    allow_duplicates=allow_duplicates,
                )
            )
        submit(self._service.set_volume(state.volume))
        submit(
            self._service._update_position(
                int(state.position * 1000),
                int(state.duration * 1000),
            )
        )
        submit(self._set_now_playing(state.now_playing))

    def _compare_states(self, *, reason: str) -> None:
        cohort_key = self._cohort_key_provider()
        if not self._flags.should_run_shadow(cohort_key=cohort_key):
            return
        try:
            service_status = self._run(self._service.get_status())
        except Exception as exc:
            self._logger.warning("Failed to fetch service status during %s: %s", reason, exc)
            return

        if service_status is None:
            # In test mode, _run returns None when _loop is None
            return

        legacy_state = self._ensure_player_state(self._legacy.state())
        mismatches = []
        if service_status.state == PlaybackState.PLAYING and not legacy_state.is_playing:
            mismatches.append("service reports PLAYING but legacy is paused/idle")
        if service_status.state in {PlaybackState.PAUSED, PlaybackState.IDLE} and legacy_state.is_playing:
            mismatches.append("legacy is playing while service reports non-playing state")
        if service_status.volume != legacy_state.volume:
            mismatches.append(f"volume mismatch (service={service_status.volume} legacy={legacy_state.volume})")
        if service_status.queue_length != len(legacy_state.queue):
            mismatches.append(
                f"queue length mismatch (service={service_status.queue_length} legacy={len(legacy_state.queue)})"
            )
        if mismatches:
            self._logger.warning(
                "Audio core shadow mismatch (%s): %s",
                reason,
                "; ".join(mismatches),
            )

    def _maybe_call_service(self, factory: Callable[[], Coroutine[object, object, object]]) -> None:
        cohort_key = self._cohort_key_provider()
        if not (
            self._flags.is_new_core_enabled(cohort_key=cohort_key)
            or self._flags.should_run_shadow(cohort_key=cohort_key)
        ):
            return
        coro = factory()
        if self._flags.is_new_core_enabled(cohort_key=cohort_key):
            self._shadow_executor.submit_sync(coro)
        elif self._flags.should_run_shadow(cohort_key=cohort_key):
            self._shadow_executor.submit_shadow(coro)

    def _run(self, coro: Coroutine[object, object, _T]) -> _T | None:
        if self._loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _set_now_playing(self, item: QueueItem | None) -> None:
        async with self._service._lock:
            self._service._now_playing = item

    @staticmethod
    def _ensure_player_state(state: PlayerState | dict | object) -> PlayerState:
        if isinstance(state, PlayerState):
            return state
        if isinstance(state, dict):
            return PlayerState.model_validate(state)
        if hasattr(state, "model_dump"):
            return PlayerState.model_validate(state.model_dump())
        if hasattr(state, "__dict__"):
            return PlayerState.model_validate(dict(state.__dict__))
        raise ValueError("Unsupported player state payload")

    @staticmethod
    def _build_status_from_state(state: PlayerState) -> dict[str, Any]:
        return {
            "queue": [item.model_dump() for item in state.queue],
            "current": state.now_playing.model_dump() if state.now_playing else None,
            "is_playing": state.is_playing,
            "position": state.position,
            "volume": state.volume,
        }

    def __getattr__(self, item: str):
        return getattr(self._legacy, item)


class ThreadedShadowExecutor:
    """
    Submit helper that executes audio service coroutines on the integration loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def submit_sync(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def submit_shadow(self, coro):
        asyncio.run_coroutine_threadsafe(self._shadow(coro), self._loop)

    async def _shadow(self, coro):
        with contextlib.suppress(ServiceError, AudioServiceError, Exception):
            await coro


class _NoopShadowExecutor:
    def submit_sync(self, coro):
        # In test mode, execute coroutines synchronously using asyncio.run
        # This allows tests to verify service state changes
        try:
            return asyncio.run(coro)
        except RuntimeError:
            # If there's already an event loop running, create a new one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def submit_shadow(self, coro):
        # Shadow mode doesn't need to execute in test mode
        return None


__all__ = [
    "AudioCoreFeatureFlags",
    "AudioCoreFlagSnapshot",
    "AudioCoreServiceAdapter",
]
