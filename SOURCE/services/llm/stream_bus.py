"""In-process SSE stream registry for command response tokens."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import threading
import time
from collections.abc import Generator
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

STREAM_TTL_SECONDS = 300
_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

StreamEvent = dict[str, Any]
StreamQueue = asyncio.Queue[StreamEvent | None]

_current_command_stream_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_command_stream_id",
    default=None,
)
_current_command_stream_metadata: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "current_command_stream_metadata",
    default=None,
)

# In-flight streaming tasks keyed by stream_id/task_id.
_active_streams: dict[str, StreamQueue] = {}
_stream_created_at: dict[str, float] = {}
_stream_owners: dict[str, str] = {}
_stream_producers: set[str] = set()
_stream_viewer_counts: dict[str, int] = {}
_stream_token_counts: dict[str, int] = {}
_stream_tool_events: dict[str, list[StreamEvent]] = {}
_stream_event_history: dict[str, list[StreamEvent | None]] = {}
_streams_lock = threading.RLock()


def normalize_stream_id(raw: Any) -> str | None:
    """Return a safe stream id, or ``None`` when the client did not provide one."""
    stream_id = str(raw or "").strip()
    if not stream_id:
        return None
    if not _STREAM_ID_RE.fullmatch(stream_id):
        raise ValueError("Invalid stream_id")
    return stream_id


def expire_stale_streams() -> None:
    """Remove streams older than ``STREAM_TTL_SECONDS`` to prevent unbounded growth."""
    now = time.monotonic()
    with _streams_lock:
        expired = [sid for sid, created in _stream_created_at.items() if now - created > STREAM_TTL_SECONDS]
        for sid in expired:
            remove_stream(sid)
    if expired:
        logger.info("Expired %d stale streams", len(expired))


def register_stream(stream_id: str, *, owner_id: str) -> StreamQueue:
    """Create or reuse a stream queue owned by ``owner_id``."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        raise ValueError("stream_id is required")
    owner = str(owner_id or "").strip()
    if not owner:
        raise ValueError("owner_id is required")

    expire_stale_streams()
    with _streams_lock:
        existing_owner = _stream_owners.get(normalized)
        if existing_owner and existing_owner != owner:
            raise PermissionError("stream owner mismatch")

        queue = _active_streams.get(normalized)
        if queue is None:
            queue = asyncio.Queue()
            _active_streams[normalized] = queue
            _stream_created_at[normalized] = time.monotonic()
            _stream_token_counts[normalized] = 0
            _stream_tool_events[normalized] = []
            _stream_event_history[normalized] = []
            _stream_viewer_counts[normalized] = 0
        else:
            _stream_event_history.setdefault(normalized, [])
            _stream_viewer_counts.setdefault(normalized, 0)
        _stream_owners[normalized] = owner
        return queue


def bind_stream_producer(stream_id: str, *, owner_id: str) -> StreamQueue:
    """Bind the single command/producer allowed to publish to ``stream_id``."""
    queue = register_stream(stream_id, owner_id=owner_id)
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        raise ValueError("stream_id is required")
    with _streams_lock:
        if normalized in _stream_producers:
            raise FileExistsError("stream already has a producer")
        _stream_producers.add(normalized)
    return queue


def get_stream_queue(stream_id: str) -> StreamQueue | None:
    """Return a stream queue if it is currently registered."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return None
    expire_stale_streams()
    with _streams_lock:
        return _active_streams.get(normalized)


def get_stream_owner(stream_id: str) -> str | None:
    """Return the user_id that owns a registered stream."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return None
    with _streams_lock:
        return _stream_owners.get(normalized)


def remove_stream(stream_id: str) -> None:
    """Forget a stream queue and ownership metadata."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return
    with _streams_lock:
        _active_streams.pop(normalized, None)
        _stream_created_at.pop(normalized, None)
        _stream_owners.pop(normalized, None)
        _stream_producers.discard(normalized)
        _stream_viewer_counts.pop(normalized, None)
        _stream_token_counts.pop(normalized, None)
        _stream_tool_events.pop(normalized, None)
        _stream_event_history.pop(normalized, None)


def stream_has_producer(stream_id: str) -> bool:
    """Return whether a command/producer has claimed this stream id."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return False
    with _streams_lock:
        return normalized in _stream_producers


def attach_stream_viewer(stream_id: str) -> None:
    """Track an active SSE reader for orphan cleanup."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return
    with _streams_lock:
        if normalized in _active_streams:
            _stream_viewer_counts[normalized] = _stream_viewer_counts.get(normalized, 0) + 1


def detach_stream_viewer(stream_id: str) -> None:
    """Release an active SSE reader count."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return
    with _streams_lock:
        current = _stream_viewer_counts.get(normalized, 0)
        if current <= 1:
            _stream_viewer_counts[normalized] = 0
        else:
            _stream_viewer_counts[normalized] = current - 1


def remove_unbound_orphan_stream(stream_id: str) -> bool:
    """Remove a stream that was created by a reader but never claimed."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return False
    with _streams_lock:
        if normalized not in _active_streams:
            return False
        if normalized in _stream_producers:
            return False
        if _stream_viewer_counts.get(normalized, 0) > 0:
            return False
        if _stream_event_history.get(normalized):
            return False
        remove_stream(normalized)
        return True


def get_stream_event_history(stream_id: str) -> list[StreamEvent | None]:
    """Return a copy of buffered events for same-owner SSE reconnects."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return []
    expire_stale_streams()
    with _streams_lock:
        events = _stream_event_history.get(normalized, [])
        return [dict(event) if isinstance(event, dict) else None for event in events]


def stream_event_is_terminal(event: StreamEvent | None) -> bool:
    """Return True when an event should close an SSE reader."""
    if event is None:
        return True
    return bool(event.get("done") or event.get("error"))


def get_stream_token_count(stream_id: str | None) -> int:
    """Return how many visible token events have been published for a stream."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return 0
    with _streams_lock:
        return _stream_token_counts.get(normalized, 0)


def _tool_event_key(event: StreamEvent) -> str:
    tool_name = str(event.get("tool_name") or event.get("name") or "tool")
    step_number = str(event.get("step_number") or event.get("step") or len(event))
    return "%s:%s" % (tool_name, step_number)


def record_tool_event(stream_id: str | None, event: StreamEvent | None) -> bool:
    """Persist one stream-scoped tool event for later chat-message metadata."""
    if event is None:
        return False
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return False
    payload = dict(event)
    payload["stream_id"] = normalized
    with _streams_lock:
        events = _stream_tool_events.setdefault(normalized, [])
        key = _tool_event_key(payload)
        for index, existing in enumerate(events):
            if _tool_event_key(existing) == key:
                events[index] = {**existing, **payload}
                break
        else:
            events.append(payload)
        if len(events) > 100:
            del events[:-100]
    return True


def get_stream_tool_events(stream_id: str | None) -> list[StreamEvent]:
    """Return a copy of tool events recorded for a live stream."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return []
    with _streams_lock:
        return [dict(event) for event in _stream_tool_events.get(normalized, [])]


def publish_stream_event(stream_id: str | None, event: StreamEvent | None) -> bool:
    """Publish one event to a registered stream without blocking the provider."""
    if event is None:
        return False
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return False
    with _streams_lock:
        queue = _active_streams.get(normalized)
        if queue is None:
            return False
        payload = dict(event)
        if payload.get("token"):
            _stream_token_counts[normalized] = _stream_token_counts.get(normalized, 0) + 1
        _stream_event_history.setdefault(normalized, []).append(payload)
    queue.put_nowait(payload)
    return True


def publish_tool_event(event: StreamEvent | None, stream_id: str | None = None) -> bool:
    """Publish and record a tool event for the active command stream."""
    normalized = normalize_stream_id(stream_id or get_current_command_stream_id())
    if normalized is None or event is None:
        return False
    metadata = get_current_command_stream_metadata()
    payload = {**metadata, **dict(event), "stream_id": normalized}
    record_tool_event(normalized, payload)
    return publish_stream_event(normalized, {"tool": payload})


def close_stream(stream_id: str | None) -> bool:
    """Publish the ``None`` sentinel used by existing SSE consumers."""
    normalized = normalize_stream_id(stream_id)
    if normalized is None:
        return False
    with _streams_lock:
        queue = _active_streams.get(normalized)
        if queue is None:
            return False
        history = _stream_event_history.setdefault(normalized, [])
        if not history or history[-1] is not None:
            history.append(None)
    queue.put_nowait(None)
    return True


def publish_text_delta(text: str, stream_id: str | None = None) -> bool:
    """Publish visible answer text for the active command stream."""
    token = str(text or "")
    if not token:
        return False
    return publish_stream_event(stream_id or get_current_command_stream_id(), {"token": token})


def publish_thinking_delta(text: str, stream_id: str | None = None) -> bool:
    """Publish reasoning-summary text for the active command stream."""
    thinking = str(text or "")
    if not thinking:
        return False
    return publish_stream_event(stream_id or get_current_command_stream_id(), {"thinking": thinking})


def finalize_stream(
    stream_id: str | None,
    *,
    content: str | None = None,
    error: bool = False,
    message: str | None = None,
    streaming_mode: str | None = None,
    token_count: int | None = None,
    fallback: bool | None = None,
) -> bool:
    """Publish the terminal SSE event for a command stream."""
    event: StreamEvent = {"done": True}
    if content is not None:
        event["content"] = content
    if error:
        event["error"] = True
    if message:
        event["message"] = message
    if streaming_mode:
        event["streaming_mode"] = streaming_mode
    if token_count is not None:
        event["token_count"] = token_count
    if fallback is not None:
        event["fallback"] = fallback
    return publish_stream_event(stream_id, event)


def get_current_command_stream_id() -> str | None:
    """Return the stream id for the current command execution, if any."""
    return _current_command_stream_id.get()


def get_current_command_stream_metadata() -> dict[str, Any]:
    """Return stream-scoped metadata for the current command execution."""
    value = _current_command_stream_metadata.get()
    return dict(value) if isinstance(value, dict) else {}


@contextlib.contextmanager
def command_stream_context(
    stream_id: str | None,
    *,
    metadata: dict[str, Any] | None = None,
) -> Generator[None]:
    """Scope provider token publishing to ``stream_id`` across awaits."""
    normalized = normalize_stream_id(stream_id)
    stream_token = _current_command_stream_id.set(normalized)
    metadata_payload = dict(metadata or {})
    if normalized is not None:
        metadata_payload["stream_id"] = normalized
    metadata_token = _current_command_stream_metadata.set(metadata_payload)
    try:
        yield
    finally:
        _current_command_stream_metadata.reset(metadata_token)
        _current_command_stream_id.reset(stream_token)
