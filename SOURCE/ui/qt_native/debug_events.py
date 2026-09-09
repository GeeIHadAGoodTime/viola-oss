from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal, SignalInstance

from core.logging_config import get_logger

# ---------------------------------------------------------------------------
# Sanitised event storage (privacy preserving, in-memory only)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DebugEvent:
    name: str
    payload: dict[str, object]
    source: str
    timestamp_ms: float


class _DebugEventRecorder:
    """
    Lightweight, privacy-preserving recorder for emitted debug events.

    Payloads are sanitised (e.g. lengths instead of raw text) and stored
    in-memory only so that tests can assert on sequencing without exposing
    user content. Thread-safe for emissions coming from background workers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[DebugEvent] = []
        self._subscribers: list[Callable[[DebugEvent], None]] = []

    def record(self, event: DebugEvent) -> None:
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                # Subscribers are best-effort; never block instrumentation
                get_logger(__name__).exception("Debug event subscriber failed: %s", e)
                continue

    def subscribe(self, callback: Callable[[DebugEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def clear(self, source: str | None = None) -> None:
        with self._lock:
            if source is None:
                self._events.clear()
            else:
                self._events = [evt for evt in self._events if evt.source != source]

    def snapshot(self, source: str | None = None) -> list[DebugEvent]:
        with self._lock:
            if source is None:
                return list(self._events)
            return [evt for evt in self._events if evt.source == source]


_recorder = _DebugEventRecorder()
_debug_events_enabled = True


def _as_str_keyed_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            out[key] = item
    return out


def _sanitise_payload(name: str, args: Iterable[object]) -> dict[str, object]:
    args_list = list(args) if args else []

    if not args_list:
        return {}

    first = args_list[0]

    if name in {"command_submitted", "api_command_sent"}:
        text = str(first)
        return {"char_len": len(text)}

    if name == "chat_response_set":
        text = str(first)
        return {"char_len": len(text)}

    if name in {"playback_started", "playback_stopped"}:
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            video_hint = first_dict.get("video_id_hint")
            if isinstance(video_hint, str) and len(video_hint) > 8:
                video_hint = video_hint[-8:]
            sanitised = {
                "video_id_hint": video_hint,
                "title_len": first_dict.get("title_len"),
                "artist_len": first_dict.get("artist_len"),
                "duration": first_dict.get("duration"),
                "position": first_dict.get("position"),
                "queue_len": first_dict.get("queue_len"),
                "transition": first_dict.get("transition"),
                "source": first_dict.get("source"),
            }
            return {k: v for k, v in sanitised.items() if v is not None}
        return {"repr": repr(first)}

    if name in {
        "stt_transcript_ready",
        "voice_error",
        "api_request_failed",
        "api_request_finished",
        "api_request_started",
        "backend_connection_attempt",
        "backend_connection_result",
        "app_starting",
        "settings_modal_open",
        "settings_modal_loaded",
        "settings_modal_error",
        "settings_modal_saved",
        "settings_payload_received",
        "settings_devices_loaded",
        "settings_devices_error",
        "settings_gpt_models_loaded",
    }:
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            return first_dict
        if isinstance(first, str):
            return {"message": first}

    if name == "resolver_summary":
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            return {
                "path": first_dict.get("path") or first_dict.get("route") or first_dict.get("strategy"),
                "provider": first_dict.get("provider"),
                "fallback": first_dict.get("fallback"),
                "latency_ms": first_dict.get("latency_ms") or first_dict.get("latency"),
                "source": first_dict.get("source"),
            }
    if name == "resolver_failure":
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            message = str(first_dict.get("message") or first_dict.get("detail") or "")
            return {
                "code": first_dict.get("code"),
                "severity": first_dict.get("severity") or first_dict.get("level"),
                "message_len": len(message),
            }
    if name == "playlist_context":
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            return {
                "name": first_dict.get("name") or first_dict.get("title"),
                "count": first_dict.get("count") or first_dict.get("length"),
                "position": first_dict.get("position"),
                "shuffle": first_dict.get("shuffle"),
            }
    if name == "autoplay_event":
        first_dict = _as_str_keyed_dict(first)
        if first_dict is not None:
            return {
                "status": first_dict.get("status"),
                "added": first_dict.get("added") or first_dict.get("added_count"),
                "reason": first_dict.get("reason"),
            }

    if len(args_list) == 1:
        value = args_list[0]
        if isinstance(value, (str, int, float, bool)):
            return {"value": value}
        if isinstance(value, dict):
            out = _as_str_keyed_dict(value)
            return out or {}
        return {"repr": repr(value)}

    return {"values": [repr(arg) for arg in args_list]}


def emit_debug_event(name: str, payload: dict[str, object] | None = None, *, source: str = "qt") -> None:
    """
    Emit a debug event to the recorder, ensuring payloads are sanitised.

    `source` distinguishes emitters (e.g. "qt", "web", "backend").
    """
    if not _debug_events_enabled:
        return

    payload = payload or {}
    if payload:
        payload = _sanitise_payload(name, [payload])
    event = DebugEvent(
        name=name,
        payload=payload,
        source=source,
        timestamp_ms=time.monotonic_ns() / 1_000_000.0,
    )
    _recorder.record(event)


def subscribe_debug_events(
    callback: Callable[[DebugEvent], None],
) -> Callable[[], None]:
    """
    Subscribe to debug events. Returns an unsubscribe callback.
    """

    return _recorder.subscribe(callback)


def get_recorded_debug_events(source: str | None = None) -> list[DebugEvent]:
    """
    Snapshot recorded debug events. Optionally filter by source.
    """

    return _recorder.snapshot(source=source)


def reset_debug_events(source: str | None = None) -> None:
    """
    Clear recorded debug events. Optionally only for a given source.
    """

    _recorder.clear(source=source)


class DebugEventBus(QObject):
    """
    Centralized Qt signal bus for UI/debug instrumentation.

    This enables plugin-friendly subscriptions from tests and optional tools
    without coupling to specific widgets.
    """

    # Lifecycle events
    app_starting = Signal(object)
    backend_connection_attempt = Signal(object)
    backend_connection_result = Signal(object)

    # Chat/command events
    command_submitted = Signal(str)
    api_command_sent = Signal(str)
    chat_response_set = Signal(str)

    # API lifecycle events (Qt client)
    api_request_started = Signal(object)
    api_request_finished = Signal(object)
    api_request_failed = Signal(object)

    # Settings modal instrumentation
    settings_modal_open = Signal(object)
    settings_modal_loaded = Signal(object)
    settings_modal_error = Signal(object)
    settings_payload_received = Signal(object)
    settings_devices_loaded = Signal(object)
    settings_devices_error = Signal(object)
    settings_gpt_models_loaded = Signal(object)
    settings_modal_saved = Signal(object)

    # Consent wizard instrumentation
    consent_status_requested = Signal(object)
    consent_status_rendered = Signal(object)
    consent_backend_unavailable = Signal(object)
    consent_session_started = Signal(object)
    consent_step_submitted = Signal(object)
    consent_step_updated = Signal(object)
    consent_provider_revoked = Signal(object)
    consent_error = Signal(object)

    # Voice/PTT events
    ptt_started = Signal()
    ptt_stopped = Signal()
    ptt_no_audio = Signal()
    stt_transcript_ready = Signal(str)
    voice_error = Signal(str)
    wake_ready = Signal(object)
    wake_detected = Signal(object)
    wake_edge_transition = Signal(object)
    stt_started = Signal(object)
    stt_finished = Signal(object)
    voice_listening_started = Signal(object)  # Wake word accepted, STT active
    voice_listening_stopped = Signal(object)  # Listening ended
    music_engine_heartbeat = Signal(object)
    self_healing_action = Signal(object)

    # Transport events
    transport_play_pause = Signal(str)  # "play" | "pause"
    transport_next = Signal()
    transport_previous = Signal()

    # Playback events
    playback_started = Signal(object)
    playback_stopped = Signal(object)

    # Resolver & playlist instrumentation
    resolver_summary = Signal(object)
    resolver_failure = Signal(object)
    playlist_context = Signal(object)
    autoplay_event = Signal(object)

    # Queue events
    queue_updated = Signal()
    queue_dialog_opened = Signal()
    history_dialog_opened = Signal()
    timer_dialog_opened = Signal()

    SIGNAL_DEFINITIONS = {
        "app_starting": app_starting,
        "backend_connection_attempt": backend_connection_attempt,
        "backend_connection_result": backend_connection_result,
        "command_submitted": command_submitted,
        "api_command_sent": api_command_sent,
        "chat_response_set": chat_response_set,
        "api_request_started": api_request_started,
        "api_request_finished": api_request_finished,
        "api_request_failed": api_request_failed,
        "settings_modal_open": settings_modal_open,
        "settings_modal_loaded": settings_modal_loaded,
        "settings_modal_error": settings_modal_error,
        "settings_payload_received": settings_payload_received,
        "settings_devices_loaded": settings_devices_loaded,
        "settings_devices_error": settings_devices_error,
        "settings_gpt_models_loaded": settings_gpt_models_loaded,
        "settings_modal_saved": settings_modal_saved,
        "consent_status_requested": consent_status_requested,
        "consent_status_rendered": consent_status_rendered,
        "consent_backend_unavailable": consent_backend_unavailable,
        "consent_session_started": consent_session_started,
        "consent_step_submitted": consent_step_submitted,
        "consent_step_updated": consent_step_updated,
        "consent_provider_revoked": consent_provider_revoked,
        "consent_error": consent_error,
        "ptt_started": ptt_started,
        "ptt_stopped": ptt_stopped,
        "ptt_no_audio": ptt_no_audio,
        "stt_transcript_ready": stt_transcript_ready,
        "voice_error": voice_error,
        "wake_ready": wake_ready,
        "wake_detected": wake_detected,
        "wake_edge_transition": wake_edge_transition,
        "stt_started": stt_started,
        "stt_finished": stt_finished,
        "voice_listening_started": voice_listening_started,
        "voice_listening_stopped": voice_listening_stopped,
        "music_engine_heartbeat": music_engine_heartbeat,
        "self_healing_action": self_healing_action,
        "transport_play_pause": transport_play_pause,
        "transport_next": transport_next,
        "transport_previous": transport_previous,
        "playback_started": playback_started,
        "playback_stopped": playback_stopped,
        "resolver_summary": resolver_summary,
        "resolver_failure": resolver_failure,
        "playlist_context": playlist_context,
        "autoplay_event": autoplay_event,
        "queue_updated": queue_updated,
        "queue_dialog_opened": queue_dialog_opened,
        "history_dialog_opened": history_dialog_opened,
        "timer_dialog_opened": timer_dialog_opened,
    }

    def __init__(self) -> None:
        super().__init__()
        self._recorder_wired = False

    def wire_recorder(self) -> None:
        if self._recorder_wired:
            return

        def _wrap(name: str) -> Callable[..., None]:
            def _handler(*args: object) -> None:
                payload = _sanitise_payload(name, args)
                emit_debug_event(name, payload, source="qt")

            return _handler

        for name in self.SIGNAL_DEFINITIONS:
            signal = getattr(self, name, None)
            if not isinstance(signal, SignalInstance):
                continue
            try:
                signal.connect(_wrap(name))
            except TypeError as e:
                get_logger(__name__).debug("Signal connection failed for %s (non-critical): %s", name, e)
        self._recorder_wired = True

    def wire_backend_events(self) -> None:
        """Wire backend debug events to Qt signals"""
        # Map backend event names to Qt signals
        event_to_signal = {
            "voice_listening_started": self.voice_listening_started,
            "voice_listening_stopped": self.voice_listening_stopped,
            "stt_started": self.stt_started,
            "stt_finished": self.stt_finished,
            "wake_edge_transition": self.wake_edge_transition,
            "wake_ready": self.wake_ready,
        }

        def _on_backend_event(event: DebugEvent) -> None:
            """Handle backend debug events and emit corresponding Qt signals"""
            if event.source == "qt":
                return  # Skip Qt-originated events to avoid loops

            signal = event_to_signal.get(event.name)
            if signal is not None:
                try:
                    signal.emit(event.payload)
                except Exception as e:
                    # Signal emission failures should not crash the app

                    get_logger(__name__).exception("Debug signal emit failed: %s", e)
                    pass

        # Subscribe to debug events from backend
        subscribe_debug_events(_on_backend_event)


_instance: DebugEventBus | None = None


def get_debug_bus() -> DebugEventBus:
    global _instance
    if _instance is None:
        _instance = DebugEventBus()
        _instance.wire_recorder()
        _instance.wire_backend_events()
    return _instance
