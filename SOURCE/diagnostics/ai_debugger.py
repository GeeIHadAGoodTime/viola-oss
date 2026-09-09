"""
diagnostics/ai_debugger.py
===========================

Structured diagnostic payloads for debugging tools.

This module provides a structured diagnostic snapshot optimized for AI analysis:
- Clear subsystem health status with severity levels
- Recent command history with execution results
- Actionable recommendations based on current state
- Human-readable summaries alongside structured data

MCP tools and API clients can inspect these snapshots without parsing raw logs.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum

from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

logger = get_logger(__name__)

_TRACE_PER_USER_LIMIT = 50
_TRACE_USER_LIMIT = 100
_TRACE_TEXT_PREVIEW_CHARS = 160
_TRACE_ERROR_PREVIEW_CHARS = 240
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passcode|secret|token|api[_\s-]?key|session[_\s-]?token)\b\s*(?:is|=|:)\s*([^\s,;]+)"
)
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def _get_dict(snapshot: dict[str, object], key: str) -> dict[str, object]:
    value = snapshot.get(key)
    if isinstance(value, dict):
        return value
    return {}


def _get_dict_list(snapshot: dict[str, object], key: str) -> list[dict[str, object]]:
    value = snapshot.get(key)
    if not isinstance(value, list):
        return []
    out: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            out.append(item)
    return out


def _get_str(mapping: dict[str, object], key: str, default: str = "") -> str:
    value = mapping.get(key)
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _get_float(mapping: dict[str, object], key: str, default: float = 0.0) -> float:
    value = mapping.get(key)
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _get_bool(mapping: dict[str, object], key: str, default: bool = False) -> bool:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off", ""}:
            return False
    return default


class DiagnosticSeverity(str, Enum):
    """Severity levels for diagnostic issues."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class SubsystemStatus:
    """Health status of a single subsystem."""

    name: str
    status: DiagnosticSeverity
    message: str
    details: dict[str, object] = field(default_factory=dict)
    last_heartbeat: float | None = None
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "last_heartbeat": self.last_heartbeat,
            "recommendations": self.recommendations,
        }


@dataclass
class CommandTrace:
    """Record of an executed command."""

    timestamp: float
    command_text: str
    intent_type: str | None
    success: bool
    response_summary: str
    duration_ms: float | None = None
    error: str | None = None
    command_hash: str | None = None
    response_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "command_text": self.command_text,
            "intent_type": self.intent_type,
            "success": self.success,
            "response_summary": self.response_summary,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "command_hash": self.command_hash,
            "response_hash": self.response_hash,
        }


@dataclass
class AIDebugPayload:
    """Complete diagnostic payload for AI consumption."""

    generated_at: float
    overall_status: DiagnosticSeverity
    summary: str
    subsystems: list[SubsystemStatus]
    recent_commands: list[CommandTrace]
    recent_operations: list[dict[str, object]]
    recent_failures: list[dict[str, object]]
    playback_state: dict[str, object]
    recommendations: list[str]
    raw_metrics: dict[str, object] | None = None
    # New fields for enhanced AI debugging
    error_patterns: list[dict[str, object]] = field(default_factory=list)
    health_score: int = 100  # 0-100, higher is better
    debugging_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "overall_status": self.overall_status.value,
            "summary": self.summary,
            "health_score": self.health_score,
            "subsystems": [s.to_dict() for s in self.subsystems],
            "recent_commands": [c.to_dict() for c in self.recent_commands],
            "recent_operations": self.recent_operations,
            "recent_failures": self.recent_failures,
            "error_patterns": self.error_patterns,
            "debugging_hints": self.debugging_hints,
            "playback_state": self.playback_state,
            "recommendations": self.recommendations,
            "raw_metrics": self.raw_metrics,
        }


# Command trace buffers - stores recent command executions scoped per user.
_PER_USER_TRACE_BUFFER: OrderedDict[str, deque[CommandTrace]] = OrderedDict()
_COMMAND_TRACE_LOCK = None  # Lazy init to avoid import-time threading issues


def _get_trace_lock():
    """Get or create the command trace lock."""
    global _COMMAND_TRACE_LOCK
    if _COMMAND_TRACE_LOCK is None:
        import threading

        _COMMAND_TRACE_LOCK = threading.Lock()
    return _COMMAND_TRACE_LOCK


def _require_trace_user_id(user_id: str | None) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Command trace access requires a non-empty user_id")
    normalized = user_id.strip()
    if normalized.lower() == "default":
        raise ValueError('Command trace access cannot use user_id="default"')
    return normalized


def _resolve_optional_trace_user_id(user_id: str | None) -> str | None:
    if user_id is not None:
        return _require_trace_user_id(user_id)
    try:
        from core.user_context import get_current_user_id

        return _require_trace_user_id(get_current_user_id())
    except LookupError:
        return None


def _hash_trace_text(user_id: str, value: str | None) -> str | None:
    if not value:
        return None
    payload = f"{user_id}\0{value}".encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()[:16]


def _redact_secret_assignments(text: str) -> str:
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED:SECRET]", text)


def _redact_trace_text(value: object, *, max_chars: int) -> str:
    if value is None:
        return ""
    text = _redact_secret_assignments(str(value))
    try:
        from intent.log_redaction import redact_card_data, redact_pii

        redacted = redact_pii(redact_card_data(text))
        text = redacted if isinstance(redacted, str) else str(redacted)
    except Exception:
        logger.debug("Trace text redaction failed", exc_info=True)

    text = _redact_secret_assignments(text)
    text = _UUID_RE.sub("[REDACTED:UUID]", text)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 16].rstrip()}... [truncated]"


def _get_user_trace_buffer(user_id: str) -> deque[CommandTrace]:
    buffer = _PER_USER_TRACE_BUFFER.get(user_id)
    if buffer is not None:
        _PER_USER_TRACE_BUFFER.move_to_end(user_id)
        return buffer
    while len(_PER_USER_TRACE_BUFFER) >= _TRACE_USER_LIMIT:
        _PER_USER_TRACE_BUFFER.popitem(last=False)
    buffer = deque(maxlen=_TRACE_PER_USER_LIMIT)
    _PER_USER_TRACE_BUFFER[user_id] = buffer
    return buffer


def record_command_trace(
    *,
    user_id: str,
    command_text: str,
    intent_type: str | None,
    success: bool,
    response_summary: str,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """
    Record a command execution for debugging purposes.

    Call this after processing any voice/text command to maintain
    a trace buffer for AI debugging sessions.
    """
    normalized_user_id = _require_trace_user_id(user_id)
    trace = CommandTrace(
        timestamp=time.time(),
        command_text=_redact_trace_text(command_text, max_chars=_TRACE_TEXT_PREVIEW_CHARS),
        intent_type=intent_type,
        success=success,
        response_summary=_redact_trace_text(response_summary, max_chars=_TRACE_TEXT_PREVIEW_CHARS),
        duration_ms=duration_ms,
        error=_redact_trace_text(error, max_chars=_TRACE_ERROR_PREVIEW_CHARS) if error else None,
        command_hash=_hash_trace_text(normalized_user_id, command_text),
        response_hash=_hash_trace_text(normalized_user_id, response_summary),
    )

    with _get_trace_lock():
        _get_user_trace_buffer(normalized_user_id).append(trace)


def get_recent_commands(user_id: str, limit: int = 20) -> list[CommandTrace]:
    """Get recent command traces."""
    normalized_user_id = _require_trace_user_id(user_id)
    with _get_trace_lock():
        traces = list(_PER_USER_TRACE_BUFFER.get(normalized_user_id, ()))
    return traces[-limit:]


def _clear_command_traces_for_tests() -> None:
    with _get_trace_lock():
        _PER_USER_TRACE_BUFFER.clear()


def _redact_diagnostics_value(value: object) -> object:
    if isinstance(value, str):
        return _redact_trace_text(value, max_chars=_TRACE_ERROR_PREVIEW_CHARS)
    if isinstance(value, dict):
        return {str(key): _redact_diagnostics_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [_redact_diagnostics_value(item) for item in value]
    return value


def _failure_belongs_to_user(event: object, user_id: str | None) -> bool:
    if user_id is None or not isinstance(event, dict):
        return False
    for key in ("user_id", "owner_id"):
        if event.get(key) == user_id:
            return True
    context = event.get("context")
    return isinstance(context, dict) and context.get("user_id") == user_id


def _filter_failures_for_user(failures_raw: object, user_id: str | None) -> list[dict[str, object]]:
    if not isinstance(failures_raw, list):
        return []
    filtered: list[dict[str, object]] = []
    for event in failures_raw:
        if _failure_belongs_to_user(event, user_id):
            redacted = _redact_diagnostics_value(event)
            if isinstance(redacted, dict):
                filtered.append(redacted)
    return filtered


def _tenant_safe_raw_metrics(
    metrics_snapshot: dict[str, object],
    filtered_failures: list[dict[str, object]],
) -> dict[str, object]:
    safe_snapshot = dict(metrics_snapshot)
    safe_snapshot["failures"] = filtered_failures
    redacted = _redact_diagnostics_value(safe_snapshot)
    return redacted if isinstance(redacted, dict) else {}


def _evaluate_llm_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate LLM router health with auto-fix for import errors."""
    import_error = None

    try:
        from services.llm.provider_router import create_router

        # Create a temporary router to check status
        # Note: In production, we'd want to access the singleton router
        router = create_router()

        if router is None:
            return SubsystemStatus(
                name="llm_router",
                status=DiagnosticSeverity.ERROR,
                message="LLM router not initialized",
                recommendations=[
                    "Check if LLM providers are configured in settings",
                    "Verify API keys are set (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)",
                    "Review logs for LLM initialization errors",
                ],
            )

        # Use get_status() method which exists on ProviderAgnosticRouter
        status = router.get_status() if hasattr(router, "get_status") else {}
        any_available = status.get("any_available", False)
        init_error = status.get("init_error")

        if any_available:
            degradation = status.get("degradation", {})
            if isinstance(degradation, dict) and degradation.get("degraded"):
                fallback_status = degradation.get("fallback_status") or {}
                active = fallback_status.get("active_provider") if isinstance(fallback_status, dict) else {}
                active_name = active.get("name", "backup provider") if isinstance(active, dict) else "backup provider"
                active_model = active.get("model", "unknown") if isinstance(active, dict) else "unknown"
                return SubsystemStatus(
                    name="llm_router",
                    status=DiagnosticSeverity.DEGRADED,
                    message=f"LLM degraded: running on backup provider {active_name} ({active_model})",
                    details=status,
                    recommendations=[
                        "Investigate the primary LLM provider failure recorded in fallback_status.last_event",
                    ],
                )
            primary = status.get("primary", {})
            provider_name = primary.get("name", "unknown") if primary else "unknown"
            model = primary.get("model", "unknown") if primary else "unknown"
            return SubsystemStatus(
                name="llm_router",
                status=DiagnosticSeverity.OK,
                message=f"LLM available: {provider_name} ({model})",
                details=status,
            )
        else:
            return SubsystemStatus(
                name="llm_router",
                status=DiagnosticSeverity.ERROR,
                message=init_error or "No LLM providers available",
                details=status,
                recommendations=[
                    "Configure at least one LLM provider in settings",
                    "Set OPENAI_API_KEY or ANTHROPIC_API_KEY environment variable",
                    "Or start Ollama for local LLM: ollama serve",
                    "AI features (questions, autoplay suggestions) require LLM",
                ],
            )
    except ImportError as e:
        import_error = e
        # Attempt auto-fix for stale bytecode cache
        try:
            from bootstrap.cache_cleaner import auto_fix_import_error

            if auto_fix_import_error("services.llm.provider_router", e):
                # Retry the import after fix
                from services.llm.provider_router import create_router

                router = create_router()
                if router:
                    status = router.get_status() if hasattr(router, "get_status") else {}
                    return SubsystemStatus(
                        name="llm_router",
                        status=DiagnosticSeverity.OK,
                        message="LLM router recovered after cache fix",
                        details={"auto_fixed": True, **status},
                    )
        except Exception as fix_error:
            logger.warning("Auto-fix attempt failed: %s", fix_error)

        return SubsystemStatus(
            name="llm_router",
            status=DiagnosticSeverity.ERROR,
            message=f"Import error (stale cache?): {import_error}",
            recommendations=[
                "STALE BYTECODE DETECTED - auto-fix attempted",
                "If issue persists, restart the application",
                "The system will auto-clear caches on restart",
            ],
        )
    except Exception as e:
        return SubsystemStatus(
            name="llm_router",
            status=DiagnosticSeverity.ERROR,
            message=f"Failed to check LLM status: {e}",
            recommendations=["Check LLM module imports and configuration"],
        )


def _evaluate_stt_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate speech-to-text health from heartbeats."""
    heartbeats = _get_dict_list(metrics_snapshot, "heartbeats")
    # Check for direct registration or HeartbeatSupervisor-prefixed name
    stt_heartbeat = next(
        (h for h in heartbeats if _get_str(h, "subsystem") in ("stt_transcriber", "supervisor.stt_transcriber")),
        None,
    )

    if stt_heartbeat is None:
        return SubsystemStatus(
            name="stt_transcriber",
            status=DiagnosticSeverity.UNKNOWN,
            message="No STT heartbeat data",
            recommendations=[
                "Voice pipeline may not be started",
                "Check wake word detector is enabled",
            ],
        )

    status = _get_str(stt_heartbeat, "status", "unknown")
    stale = _get_bool(stt_heartbeat, "stale", True)
    age = _get_float(stt_heartbeat, "age_seconds", 0.0)

    if status == "ok" and not stale:
        return SubsystemStatus(
            name="stt_transcriber",
            status=DiagnosticSeverity.OK,
            message="STT transcriber healthy",
            last_heartbeat=_get_float(stt_heartbeat, "last_beat", 0.0),
            details=_get_dict(stt_heartbeat, "details"),
        )

    if stale:
        return SubsystemStatus(
            name="stt_transcriber",
            status=DiagnosticSeverity.DEGRADED,
            message=f"STT heartbeat stale (age: {age:.1f}s)",
            last_heartbeat=_get_float(stt_heartbeat, "last_beat", 0.0),
            recommendations=[
                "STT transcriber may be stuck or crashed",
                "Check microphone permissions and device availability",
                "Try restarting the voice pipeline",
            ],
        )

    return SubsystemStatus(
        name="stt_transcriber",
        status=DiagnosticSeverity.DEGRADED,
        message=f"STT status: {status}",
        last_heartbeat=_get_float(stt_heartbeat, "last_beat", 0.0),
        details=_get_dict(stt_heartbeat, "details"),
    )


def _evaluate_wake_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate wake word detector health."""
    heartbeats = _get_dict_list(metrics_snapshot, "heartbeats")
    # Check for various names the wake detector might be registered under:
    # - "wake_word", "wake_detector": direct registration
    # - "supervisor.wake_detector": registered via HeartbeatSupervisor
    wake_heartbeat = next(
        (
            h
            for h in heartbeats
            if _get_str(h, "subsystem") in ("wake_word", "wake_detector", "supervisor.wake_detector")
        ),
        None,
    )

    if wake_heartbeat is None:
        return SubsystemStatus(
            name="wake_detector",
            status=DiagnosticSeverity.UNKNOWN,
            message="No wake detector heartbeat",
            recommendations=[
                "Wake word detection may be disabled",
                "Say 'enable wake word' to turn it on, or check the Voice section",
            ],
        )

    stale = _get_bool(wake_heartbeat, "stale", True)
    if not stale:
        return SubsystemStatus(
            name="wake_detector",
            status=DiagnosticSeverity.OK,
            message="Wake detector operational",
            last_heartbeat=_get_float(wake_heartbeat, "last_beat", 0.0),
            details=_get_dict(wake_heartbeat, "details"),
        )
    else:
        return SubsystemStatus(
            name="wake_detector",
            status=DiagnosticSeverity.DEGRADED,
            message="Wake detector heartbeat stale",
            last_heartbeat=_get_float(wake_heartbeat, "last_beat", 0.0),
            recommendations=[
                "Wake detector may be stuck",
                "Check microphone input device",
                "Try toggling wake word detection off and on",
            ],
        )


def _evaluate_aec_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate AEC (Acoustic Echo Cancellation) reference status."""
    # Check for WASAPI loopback availability
    try:
        from audio_core.wasapi.loopback import get_loopback_device

        device = get_loopback_device()
        if device is not None:
            return SubsystemStatus(
                name="aec_reference",
                status=DiagnosticSeverity.OK,
                message="AEC reference (WASAPI loopback) available",
                details={"device": str(device)},
            )
    except ImportError:
        pass  # WASAPI loopback module not available
    except Exception as e:
        logger.debug("AEC reference check failed: %s", e)

    # Check if PyAudioWPatch is available
    try:
        from audio_core.wasapi.loopback import _PYAUDIOWPATCH_AVAILABLE

        if not _PYAUDIOWPATCH_AVAILABLE:
            return SubsystemStatus(
                name="aec_reference",
                status=DiagnosticSeverity.DEGRADED,
                message="PyAudioWPatch not installed - native WASAPI loopback unavailable",
                recommendations=[
                    "Install pyaudiowpatch: pip install pyaudiowpatch",
                    "This enables native WASAPI loopback for AEC",
                ],
            )
    except ImportError as e:
        logger.warning("Failed to check PyAudioWPatch availability: %s", e)

    return SubsystemStatus(
        name="aec_reference",
        status=DiagnosticSeverity.DEGRADED,
        message="No WASAPI loopback device found - AEC unavailable",
        recommendations=[
            "Verify audio output device is working",
            "Check Windows audio settings",
            "AEC prevents wake word from triggering during playback",
        ],
    )


def _active_embedded_playback_details(app_state: object | None) -> dict[str, object] | None:
    if app_state is None:
        return None

    music = getattr(app_state, "music", None)
    if music is None:
        return None

    state_getter = getattr(music, "state", None)
    if not callable(state_getter):
        return None

    try:
        state = state_getter()
    except Exception as exc:
        logger.debug("Playback state lookup failed during backend diagnostics: %s", exc)
        return None

    if hasattr(state, "model_dump"):
        state = state.model_dump()
    if not isinstance(state, dict) or not bool(state.get("is_playing", False)):
        return None

    now_playing = state.get("now_playing")
    if hasattr(now_playing, "model_dump"):
        now_playing = now_playing.model_dump()
    if not isinstance(now_playing, dict):
        now_playing = {}

    capabilities = now_playing.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}

    values = [
        now_playing.get("provider"),
        now_playing.get("source"),
        now_playing.get("playback_mode"),
        now_playing.get("resolver_path"),
        capabilities.get("resolver_path"),
        capabilities.get("playback_mode"),
    ]
    marker_text = " ".join(str(value).lower() for value in values if value is not None)
    embedded = bool(capabilities.get("requires_embedded_player") or capabilities.get("embedded"))
    embedded = embedded or any(marker in marker_text for marker in ("youtube_iframe", "embedded", "iframe"))
    if not embedded:
        return None

    return {
        "track": now_playing.get("title"),
        "provider": now_playing.get("provider"),
        "playback_mode": now_playing.get("playback_mode"),
        "resolver_path": now_playing.get("resolver_path") or capabilities.get("resolver_path"),
    }


def _evaluate_vlc_status(metrics_snapshot: dict[str, object], app_state: object | None = None) -> SubsystemStatus:
    """Evaluate VLC/playback backend health."""
    vlc = _get_dict(metrics_snapshot, "vlc")

    if not vlc:
        embedded_details = _active_embedded_playback_details(app_state)
        if embedded_details is not None:
            return SubsystemStatus(
                name="playback_backend",
                status=DiagnosticSeverity.OK,
                message="Embedded playback backend active",
                details=embedded_details,
            )
        return SubsystemStatus(
            name="playback_backend",
            status=DiagnosticSeverity.UNKNOWN,
            message="No playback backend status available",
        )

    healthy = _get_bool(vlc, "healthy", False)
    if healthy:
        return SubsystemStatus(
            name="playback_backend",
            status=DiagnosticSeverity.OK,
            message="Playback backend healthy",
            details={
                "track": vlc.get("track"),
                "position": vlc.get("position_seconds"),
            },
        )
    else:
        embedded_details = _active_embedded_playback_details(app_state)
        if embedded_details is not None:
            return SubsystemStatus(
                name="playback_backend",
                status=DiagnosticSeverity.OK,
                message="Embedded playback backend active",
                details=embedded_details,
            )
        return SubsystemStatus(
            name="playback_backend",
            status=DiagnosticSeverity.ERROR,
            message="Playback backend unhealthy",
            recommendations=[
                "Check if VLC is installed and accessible",
                "Verify audio output device is working",
            ],
        )


def _evaluate_youtube_auth_status(
    metrics_snapshot: dict[str, object],
) -> SubsystemStatus:
    """Evaluate YouTube authentication status."""
    try:
        from music.providers.checker import (
            get_youtube_unavailable_reason,
            is_provider_linked,
        )

        if is_provider_linked("youtube_music"):
            return SubsystemStatus(
                name="youtube_auth",
                status=DiagnosticSeverity.OK,
                message="YouTube Music linked",
            )
        else:
            reason = get_youtube_unavailable_reason()
            return SubsystemStatus(
                name="youtube_auth",
                status=DiagnosticSeverity.DEGRADED,
                message=reason or "YouTube Music not linked",
                recommendations=[
                    "Say 'connect youtube' to link YouTube Music",
                    "YouTube playback may require authentication",
                ],
            )
    except ImportError:
        return SubsystemStatus(
            name="youtube_auth",
            status=DiagnosticSeverity.UNKNOWN,
            message="YouTube provider module not available",
        )
    except Exception as e:
        return SubsystemStatus(
            name="youtube_auth",
            status=DiagnosticSeverity.UNKNOWN,
            message=f"Could not check YouTube auth: {e}",
        )


def _evaluate_tts_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate TTS engine health.

    Checks backends in priority order: Kokoro-82M (default) then pyttsx3 (fallback).
    """
    try:
        # 1. Check Kokoro (default backend)
        kokoro_available = False
        kokoro_model_present = False
        try:
            from voice.synthesis.kokoro_engine import KokoroTTSEngine

            engine = KokoroTTSEngine()
            kokoro_model_present = engine.is_available()
            kokoro_available = True
        except ImportError:
            pass
        except Exception:
            kokoro_available = True  # Module importable, but engine creation failed

        if kokoro_available and kokoro_model_present:
            return SubsystemStatus(
                name="tts_engine",
                status=DiagnosticSeverity.OK,
                message="TTS engine available (Kokoro-82M neural)",
                details={"backend": "kokoro", "model_present": True},
            )

        # 2. Check pyttsx3 fallback
        pyttsx3_available = False
        try:
            from importlib import import_module

            import_module("pyttsx3")

            pyttsx3_available = True
        except ImportError:
            pass

        if kokoro_available and not kokoro_model_present and pyttsx3_available:
            return SubsystemStatus(
                name="tts_engine",
                status=DiagnosticSeverity.DEGRADED,
                message="Kokoro model files not found, using pyttsx3 fallback",
                details={"backend": "pyttsx3", "kokoro_model_missing": True},
                recommendations=[
                    "Download Kokoro model files to models/tts/ for neural TTS",
                ],
            )

        if pyttsx3_available:
            return SubsystemStatus(
                name="tts_engine",
                status=DiagnosticSeverity.OK,
                message="TTS engine available (pyttsx3 fallback)",
                details={"backend": "pyttsx3"},
            )

        # Neither backend available
        recommendations = []
        if not kokoro_model_present:
            recommendations.append("Download Kokoro model files to models/tts/")
        recommendations.append("Install pyttsx3 as fallback: pip install pyttsx3")
        return SubsystemStatus(
            name="tts_engine",
            status=DiagnosticSeverity.DEGRADED,
            message="No TTS backend available",
            recommendations=recommendations,
        )
    except ImportError:
        return SubsystemStatus(
            name="tts_engine",
            status=DiagnosticSeverity.UNKNOWN,
            message="TTS module not available",
        )
    except Exception as e:
        return SubsystemStatus(
            name="tts_engine",
            status=DiagnosticSeverity.ERROR,
            message="TTS check failed: %s" % e,
        )


def _evaluate_network_status(metrics_snapshot: dict[str, object]) -> SubsystemStatus:
    """Evaluate network connectivity for API access."""
    import socket

    try:
        # Quick check - try to resolve and connect to a known host
        socket.create_connection(("www.googleapis.com", 443), timeout=TIMEOUT_SHUTDOWN)
        return SubsystemStatus(
            name="network",
            status=DiagnosticSeverity.OK,
            message="Network connectivity OK",
        )
    except TimeoutError:
        return SubsystemStatus(
            name="network",
            status=DiagnosticSeverity.DEGRADED,
            message="Network connection slow",
            recommendations=["Check internet connection"],
        )
    except OSError:
        return SubsystemStatus(
            name="network",
            status=DiagnosticSeverity.ERROR,
            message="No network connectivity",
            recommendations=[
                "Check internet connection",
                "Verify firewall settings",
                "YouTube and LLM features require network access",
            ],
        )
    except Exception as e:
        return SubsystemStatus(
            name="network",
            status=DiagnosticSeverity.UNKNOWN,
            message=f"Network check failed: {e}",
        )


def _evaluate_skills_status(metrics_snapshot: dict[str, object], user_id: str | None) -> SubsystemStatus:
    """Evaluate skills execution health from recent command traces."""
    if user_id is None:
        return SubsystemStatus(
            name="skills",
            status=DiagnosticSeverity.UNKNOWN,
            message="No authenticated user context for command diagnostics",
        )

    recent_commands = get_recent_commands(user_id=user_id, limit=20)

    if not recent_commands:
        return SubsystemStatus(
            name="skills",
            status=DiagnosticSeverity.UNKNOWN,
            message="No recent command executions",
        )

    # Analyze recent command success rate
    total = len(recent_commands)
    failures = sum(1 for cmd in recent_commands if not cmd.success)
    failure_rate = failures / total if total > 0 else 0

    # Get unique error types
    error_types = list(set(cmd.error for cmd in recent_commands if cmd.error))[:3]

    if failure_rate == 0:
        return SubsystemStatus(
            name="skills",
            status=DiagnosticSeverity.OK,
            message=f"Skills healthy ({total} commands, 0 failures)",
        )
    elif failure_rate < 0.3:
        return SubsystemStatus(
            name="skills",
            status=DiagnosticSeverity.DEGRADED,
            message=f"Some skill failures ({failures}/{total})",
            details={
                "error_types": error_types,
                "failure_rate": round(failure_rate, 2),
            },
        )
    else:
        return SubsystemStatus(
            name="skills",
            status=DiagnosticSeverity.ERROR,
            message=f"High skill failure rate ({failures}/{total})",
            details={
                "error_types": error_types,
                "failure_rate": round(failure_rate, 2),
            },
            recommendations=[
                "Check recent command errors for patterns",
                "Review music player and LLM provider status",
            ],
        )


def _evaluate_music_player_status(app_state: object) -> SubsystemStatus:
    """Evaluate music player connection status."""
    try:
        # Check if music player is available
        music = getattr(app_state, "music", None)
        if music is None:
            return SubsystemStatus(
                name="music_player",
                status=DiagnosticSeverity.ERROR,
                message="Music player not initialized",
                recommendations=[
                    "Check music backend configuration",
                    "Verify VLC or embedded backend is available",
                ],
            )

        # Try to get player state
        if hasattr(music, "state"):
            state = music.state()
            if hasattr(state, "model_dump"):
                state = state.model_dump()

            is_playing = state.get("is_playing", False)
            now_playing = state.get("now_playing")

            return SubsystemStatus(
                name="music_player",
                status=DiagnosticSeverity.OK,
                message=f"Music player ready (playing: {is_playing})",
                details={
                    "is_playing": is_playing,
                    "now_playing": (now_playing.get("title") if isinstance(now_playing, dict) else None),
                },
            )

        return SubsystemStatus(
            name="music_player",
            status=DiagnosticSeverity.OK,
            message="Music player available",
        )
    except Exception as e:
        return SubsystemStatus(
            name="music_player",
            status=DiagnosticSeverity.ERROR,
            message=f"Failed to check music player: {e}",
        )


def _evaluate_playback_stack_status(app_state: object) -> SubsystemStatus:
    """Evaluate playback stack consistency between backend and frontend.

    This checks for mismatches between Python backend state and frontend
    (React/YouTube iframe) state, which is critical for debugging playback issues.
    """
    try:
        from diagnostics.playback_stack import (
            collect_full_playback_state,
            get_last_frontend_diagnostics,
        )

        music = getattr(app_state, "music", None)
        full_state = collect_full_playback_state(music)
        frontend = get_last_frontend_diagnostics()

        # Check if we have frontend data
        if frontend is None:
            return SubsystemStatus(
                name="playback_stack",
                status=DiagnosticSeverity.UNKNOWN,
                message="No frontend diagnostics available (call /v1/diagnostics/request-frontend-state first)",
                recommendations=[
                    "POST to /v1/diagnostics/request-frontend-state to request frontend state",
                    "Then GET /v1/diagnostics/full-playback-state to see combined state",
                ],
            )

        # Check frontend data freshness
        age = time.time() - frontend.received_at
        if age > 30.0:
            return SubsystemStatus(
                name="playback_stack",
                status=DiagnosticSeverity.DEGRADED,
                message=f"Frontend diagnostics stale ({age:.0f}s old)",
                details=full_state.consistency.to_dict(),
                recommendations=[
                    "POST to /v1/diagnostics/request-frontend-state to get fresh data",
                ],
            )

        # Check consistency
        consistency = full_state.consistency
        if consistency.all_layers_agree:
            return SubsystemStatus(
                name="playback_stack",
                status=DiagnosticSeverity.OK,
                message="Backend and frontend playback state consistent",
                details={
                    "backend_is_playing": full_state.backend.is_playing,
                    "frontend_state": frontend.player_state_name,
                    "warnings": consistency.warnings,
                },
            )
        else:
            return SubsystemStatus(
                name="playback_stack",
                status=DiagnosticSeverity.ERROR,
                message=f"Playback state mismatch: {len(consistency.issues)} issue(s)",
                details={
                    "issues": consistency.issues,
                    "warnings": consistency.warnings,
                    "backend_is_playing": full_state.backend.is_playing,
                    "frontend_state": frontend.player_state_name,
                },
                recommendations=[
                    "Use /v1/diagnostics/full-playback-state for detailed comparison",
                    "Check YouTube iframe is loaded and visible",
                    "Verify backend is receiving state updates via WebSocket",
                ],
            )
    except ImportError:
        return SubsystemStatus(
            name="playback_stack",
            status=DiagnosticSeverity.UNKNOWN,
            message="Playback stack module not available",
        )
    except Exception as e:
        return SubsystemStatus(
            name="playback_stack",
            status=DiagnosticSeverity.ERROR,
            message=f"Playback stack check failed: {e}",
        )


def _get_playback_state(app_state: object) -> dict[str, object]:
    """Get current playback state including full stack diagnostics."""
    result: dict[str, object] = {}

    try:
        music = getattr(app_state, "music", None)
        if music is None:
            return {"error": "no_music_player"}

        if hasattr(music, "state"):
            state = music.state()
            if hasattr(state, "model_dump"):
                result = state.model_dump()
            elif isinstance(state, dict):
                result = state
            else:
                result = {"available": True}

        # Also include full playback stack state for comprehensive debugging
        try:
            from diagnostics.playback_stack import collect_full_playback_state

            full_state = collect_full_playback_state(music)
            result["_playback_stack"] = full_state.to_dict()
        except ImportError:
            pass
        except Exception as stack_err:
            result["_playback_stack_error"] = str(stack_err)

        return result
    except Exception as e:
        return {"error": str(e)}


def _generate_recommendations(
    subsystems: list[SubsystemStatus],
    recent_failures: list[dict[str, object]] | None = None,
    recent_operations: list[dict[str, object]] | None = None,
) -> list[str]:
    """
    Generate prioritized, actionable recommendations based on subsystem status,
    recent failures, and operation patterns.
    """
    recommendations = []
    priority_recommendations = []
    recent_failures = recent_failures or []
    recent_operations = recent_operations or []

    # Collect all subsystem recommendations
    for sub in subsystems:
        if sub.status in (DiagnosticSeverity.ERROR, DiagnosticSeverity.DEGRADED):
            for rec in sub.recommendations:
                if rec not in recommendations:
                    recommendations.append(rec)

    # === PATTERN DETECTION: Analyze failure patterns ===
    failure_codes: list[str] = []
    for failure in recent_failures[-15:]:
        code_value = failure.get("code", "")
        if isinstance(code_value, str):
            failure_codes.append(code_value)
        elif code_value is not None:
            failure_codes.append(str(code_value))

    # Pattern: Repeated queue failures
    queue_failures = sum(1 for c in failure_codes if "queue" in c.lower())
    if queue_failures >= 3:
        priority_recommendations.append(
            "PATTERN: Multiple queue failures detected. Check backend health and network connectivity."
        )

    # Pattern: Resolution failures
    resolution_failures = sum(1 for c in failure_codes if "resolution" in c.lower() or "resolve" in c.lower())
    if resolution_failures >= 2:
        priority_recommendations.append("PATTERN: Track resolution failing. Verify YouTube API key and quota status.")

    # Pattern: Authentication failures
    auth_failures = sum(1 for c in failure_codes if "auth" in c.lower())
    if auth_failures >= 2:
        priority_recommendations.append(
            "PATTERN: Authentication failures detected. Check API keys and provider linking."
        )

    # === PATTERN DETECTION: Analyze operation traces ===
    backend_ops = [op for op in recent_operations if op.get("type") == "backend"]
    crash_count = sum(1 for op in backend_ops if op.get("operation") == "crash" and not op.get("success"))
    if crash_count >= 2:
        priority_recommendations.append(
            "PATTERN: Backend instability detected. Check VLC installation and audio device."
        )

    playback_ops = [op for op in recent_operations if op.get("type") == "playback"]
    failed_plays = sum(1 for op in playback_ops if op.get("operation") == "play" and not op.get("success"))
    if failed_plays >= 3:
        priority_recommendations.append("PATTERN: Playback failures detected. Check media sources and backend health.")

    voice_ops = [op for op in recent_operations if op.get("type") == "voice"]
    failed_wakes = sum(1 for op in voice_ops if op.get("operation") == "wake_detected" and not op.get("success"))
    if failed_wakes >= 2:
        priority_recommendations.append("PATTERN: Wake word callback failures. Check voice pipeline configuration.")

    # === PRIORITY ORDERING: Critical issues first ===

    # Network issues block everything
    network_sub = next((s for s in subsystems if s.name == "network"), None)
    if network_sub and network_sub.status == DiagnosticSeverity.ERROR:
        priority_recommendations.insert(0, "CRITICAL: No network connectivity. Most features will not work.")

    # LLM issues are critical for AI features
    llm_sub = next((s for s in subsystems if s.name == "llm_router"), None)
    if llm_sub and llm_sub.status == DiagnosticSeverity.ERROR:
        priority_recommendations.append(
            "PRIORITY: Configure LLM provider to enable AI features (questions, smart autoplay)"
        )

    # STT issues affect voice commands
    stt_sub = next((s for s in subsystems if s.name == "stt_transcriber"), None)
    if stt_sub and stt_sub.status != DiagnosticSeverity.OK:
        priority_recommendations.append("PRIORITY: Fix STT transcriber to enable voice commands")

    # YouTube auth issues
    yt_sub = next((s for s in subsystems if s.name == "youtube_auth"), None)
    if yt_sub and yt_sub.status == DiagnosticSeverity.DEGRADED:
        priority_recommendations.append("NOTE: YouTube Music not linked. Some playback features may be limited.")

    return priority_recommendations + recommendations


def _generate_summary(overall_status: DiagnosticSeverity, subsystems: list[SubsystemStatus]) -> str:
    """Generate a human-readable summary."""
    error_count = sum(1 for s in subsystems if s.status == DiagnosticSeverity.ERROR)
    degraded_count = sum(1 for s in subsystems if s.status == DiagnosticSeverity.DEGRADED)

    if overall_status == DiagnosticSeverity.OK:
        return "All systems operational"
    elif overall_status == DiagnosticSeverity.DEGRADED:
        return f"System degraded: {degraded_count} subsystem(s) need attention"
    else:
        issues = []
        for s in subsystems:
            if s.status == DiagnosticSeverity.ERROR:
                issues.append(f"{s.name}: {s.message}")
        return f"System errors ({error_count}): " + "; ".join(issues[:3])


def _calculate_health_score(
    subsystems: list[SubsystemStatus],
    error_patterns: list[dict[str, object]],
    recent_failures: list[dict[str, object]],
) -> int:
    """
    Calculate system health score 0-100.

    Deductions:
    - 20 points per ERROR subsystem
    - 10 points per DEGRADED subsystem
    - 5 points per active error pattern
    - 2 points per recent failure (max 20)

    Returns:
        Health score from 0 (critical) to 100 (healthy).
    """
    score = 100

    # Subsystem penalties
    for sub in subsystems:
        if sub.status == DiagnosticSeverity.ERROR:
            score -= 20
        elif sub.status == DiagnosticSeverity.DEGRADED:
            score -= 10

    # Error pattern penalties
    score -= len(error_patterns) * 5

    # Recent failure penalties (capped at 20)
    failure_penalty = min(len(recent_failures) * 2, 20)
    score -= failure_penalty

    return max(0, score)


def _generate_debugging_hints(
    error_patterns: list[dict[str, object]],
    subsystems: list[SubsystemStatus],
    recent_failures: list[dict[str, object]],
) -> list[str]:
    """
    Generate actionable debugging hints based on error patterns and subsystem states.

    These hints provide specific, actionable guidance for AI debugging sessions.
    """
    hints = []

    # === Pattern-based hints ===
    for pattern in error_patterns:
        code = _get_str(pattern, "code", "")
        count_value = pattern.get("count", 0)
        count = int(count_value) if isinstance(count_value, int) else 0
        component = _get_str(pattern, "component", "")
        code_lower = code.lower()

        if "resolution" in code_lower or "resolve" in code_lower:
            hints.append(
                f"HINT: Stream resolution failing ({count}x in {component}). "
                f"Check: 1) YouTube API key quota 2) Network connectivity 3) Embedded player status"
            )
        elif "stream" in code_lower and "fail" in code_lower:
            hints.append(
                f"HINT: Stream failures detected ({count}x). "
                f"Verify network stability and media source availability."
            )
        elif "auth" in code_lower:
            hints.append(
                f"HINT: Authentication failures ({count}x in {component}). "
                f"Check API keys. Say 'connect spotify' or 'connect youtube' to re-link."
            )
        elif "timeout" in code_lower:
            hints.append(
                f"HINT: Timeouts detected ({count}x in {component}). "
                f"Check network latency; consider increasing timeout values."
            )
        elif "backend" in code_lower and ("crash" in code_lower or "fail" in code_lower):
            hints.append(
                f"HINT: Backend instability ({count}x). " f"Check VLC installation, audio device, and system resources."
            )

    # === Cross-subsystem correlation hints ===
    llm_sub = next((s for s in subsystems if s.name == "llm_router"), None)
    stt_sub = next((s for s in subsystems if s.name == "stt_transcriber"), None)
    network_sub = next((s for s in subsystems if s.name == "network"), None)
    playback_sub = next((s for s in subsystems if s.name == "playback_backend"), None)

    # LLM down but STT working
    if llm_sub and llm_sub.status == DiagnosticSeverity.ERROR and stt_sub and stt_sub.status == DiagnosticSeverity.OK:
        hints.append(
            "HINT: STT works but LLM is down. Voice commands will be transcribed "
            "but AI features (questions, smart responses) won't work. "
            "Fix: Add API key or start Ollama."
        )

    # Network down affects multiple subsystems
    if network_sub and network_sub.status == DiagnosticSeverity.ERROR:
        hints.append(
            "HINT: Network is down. This will affect: YouTube playback, LLM responses, "
            "weather updates, and any cloud-dependent features. Fix network first."
        )

    # Playback backend unhealthy
    if playback_sub and playback_sub.status == DiagnosticSeverity.ERROR:
        hints.append(
            "HINT: Playback backend unhealthy. Check: 1) VLC installed and accessible "
            "2) Audio output device working 3) No conflicting audio applications."
        )

    # === Failure pattern hints ===
    if len(recent_failures) >= 5:
        # Analyze failure codes
        failure_codes = [f.get("code", "") for f in recent_failures[-10:]]
        unique_codes = set(failure_codes)

        if len(unique_codes) <= 2 and len(recent_failures) >= 5:
            hints.append(
                f"HINT: Repeated failures with same error code ({list(unique_codes)[0]}). "
                f"This suggests a systematic issue, not transient errors. "
                f"Focus debugging on root cause rather than retrying."
            )

    # Limit hints to avoid overwhelming
    return hints[:5]


def collect_diagnostics(
    app_state: object = None,
    include_raw_metrics: bool = False,
    user_id: str | None = None,
) -> AIDebugPayload:
    """
    Collect comprehensive diagnostics for AI debugging.

    Args:
        app_state: Optional application state object with music/voice references.
        include_raw_metrics: Whether to include raw metrics snapshot (verbose).

    Returns:
        AIDebugPayload with structured diagnostic information.
    """
    metrics_snapshot: dict[str, object]
    try:
        from diagnostics.runtime_metrics import get_runtime_metrics

        metrics = get_runtime_metrics()
        metrics_snapshot = metrics.snapshot()
    except Exception as e:
        logger.warning("Failed to get runtime metrics: %s", e)
        metrics_snapshot = {}

    trace_user_id = _resolve_optional_trace_user_id(user_id)

    # Evaluate each subsystem
    subsystems = [
        _evaluate_llm_status(metrics_snapshot),
        _evaluate_stt_status(metrics_snapshot),
        _evaluate_wake_status(metrics_snapshot),
        _evaluate_aec_status(metrics_snapshot),
        _evaluate_vlc_status(metrics_snapshot, app_state),
        _evaluate_youtube_auth_status(metrics_snapshot),
        _evaluate_tts_status(metrics_snapshot),
        _evaluate_network_status(metrics_snapshot),
        _evaluate_skills_status(metrics_snapshot, trace_user_id),
    ]

    if app_state is not None:
        subsystems.append(_evaluate_music_player_status(app_state))
        subsystems.append(_evaluate_playback_stack_status(app_state))

    # Determine overall status
    if any(s.status == DiagnosticSeverity.ERROR for s in subsystems):
        overall_status = DiagnosticSeverity.ERROR
    elif any(s.status == DiagnosticSeverity.DEGRADED for s in subsystems):
        overall_status = DiagnosticSeverity.DEGRADED
    elif all(s.status == DiagnosticSeverity.OK for s in subsystems):
        overall_status = DiagnosticSeverity.OK
    else:
        overall_status = DiagnosticSeverity.UNKNOWN

    # Get recent failures
    failures_raw = metrics_snapshot.get("failures", [])
    filtered_failures = _filter_failures_for_user(failures_raw, trace_user_id)
    recent_failures = filtered_failures[-10:]

    # Get recent operations
    try:
        if trace_user_id is None:
            recent_operations = []
        else:
            from diagnostics.operation_trace import get_recent_operations

            recent_operations = [op.to_dict() for op in get_recent_operations(limit=30, user_id=trace_user_id)]
    except ImportError:
        recent_operations = []

    # Get playback state
    playback_state = _get_playback_state(app_state) if app_state else {}

    # Get error patterns from registry
    try:
        from diagnostics.error_registry import get_error_registry

        registry = get_error_registry()
        error_patterns = [p.to_dict() for p in registry.get_active_patterns(max_age_hours=1.0)]
    except ImportError:
        error_patterns = []

    # Generate recommendations (with pattern detection from failures and operations)
    recommendations = _generate_recommendations(
        subsystems,
        recent_failures=recent_failures,
        recent_operations=recent_operations,
    )

    # Generate summary
    summary = _generate_summary(overall_status, subsystems)

    # Calculate health score
    health_score = _calculate_health_score(subsystems, error_patterns, recent_failures)

    # Generate debugging hints
    debugging_hints = _generate_debugging_hints(error_patterns, subsystems, recent_failures)

    return AIDebugPayload(
        generated_at=time.time(),
        overall_status=overall_status,
        summary=summary,
        subsystems=subsystems,
        recent_commands=(get_recent_commands(user_id=trace_user_id) if trace_user_id is not None else []),
        recent_operations=recent_operations,
        recent_failures=recent_failures,
        playback_state=playback_state,
        recommendations=recommendations,
        raw_metrics=_tenant_safe_raw_metrics(metrics_snapshot, filtered_failures) if include_raw_metrics else None,
        error_patterns=error_patterns,
        health_score=health_score,
        debugging_hints=debugging_hints,
    )


__all__ = [
    "AIDebugPayload",
    "CommandTrace",
    "DiagnosticSeverity",
    "SubsystemStatus",
    "collect_diagnostics",
    "get_recent_commands",
    "record_command_trace",
]
