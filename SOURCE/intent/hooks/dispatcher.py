"""Central hook dispatcher.

The lifecycle registry remains the compatibility layer for old in-process
handlers. This dispatcher gives the agent loop a Claude Code-shaped contract:
tool input updates, allow/ask/deny decisions, lifecycle context, and hook meta
frames all use the same schema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from intent.hooks.schema import (
    HookEvent,
    HookEventName,
    HookResult,
    coerce_hook_event_name,
    hook_result_from_exception,
    hook_result_from_value,
)
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
)

logger = get_logger(__name__)

DispatchFn = Callable[..., Any]


class HookDispatchState:
    """Per-registry hook side-channel state."""

    def __init__(self) -> None:
        self._initial_user_messages: dict[str, list[str]] = {}
        self._watch_paths: dict[str, tuple[str, ...]] = {}

    def take_initial_user_message(self, session_id: str | None) -> str | None:
        """Consume one queued SessionStart initial user message for a session."""

        key = _session_key(session_id)
        queue = self._initial_user_messages.get(key)
        if not queue:
            return None
        message = queue.pop(0)
        if not queue:
            self._initial_user_messages.pop(key, None)
        return message

    def watch_paths(self, session_id: str | None) -> tuple[str, ...]:
        """Return accumulated dynamic file-watch paths for a session."""

        global_paths = self._watch_paths.get(_session_key(None), ())
        if session_id is None:
            return global_paths
        session_paths = self._watch_paths.get(_session_key(session_id), ())
        return _unique_strings((*global_paths, *session_paths))

    def seed_watch_paths(self, session_id: str | None, paths: tuple[str, ...]) -> None:
        """Seed static watch paths before any hook result emits watchPaths."""

        self._extend_watch_paths(session_id, paths)

    def clear(self) -> None:
        """Clear queued lifecycle hook state between sessions/tests."""

        self._initial_user_messages.clear()
        self._watch_paths.clear()

    def record_result_state(self, event: HookEvent, result: HookResult) -> None:
        """Record side-channel values emitted by a hook result."""

        session_id = _event_session_id(event)
        event_name = coerce_hook_event_name(event.name)
        if event_name is HookEventName.SESSION_START and result.initial_user_message:
            self._queue_initial_user_message(session_id, result.initial_user_message)
        if (
            event_name in {HookEventName.SESSION_START, HookEventName.CWD_CHANGED, HookEventName.FILE_CHANGED}
            and result.watch_paths
        ):
            self._extend_watch_paths(session_id, result.watch_paths)

    def _queue_initial_user_message(self, session_id: str | None, message: str) -> None:
        text = message.strip()
        if not text:
            return
        self._initial_user_messages.setdefault(_session_key(session_id), []).append(text)

    def _extend_watch_paths(self, session_id: str | None, paths: tuple[str, ...]) -> None:
        key = _session_key(session_id)
        existing = self._watch_paths.get(key, ())
        self._watch_paths[key] = _unique_strings((*existing, *paths))


_DEFAULT_DISPATCH_STATE = HookDispatchState()


def dispatch_hook(
    event: HookEvent,
    *,
    dispatch_fn: DispatchFn | None = None,
    dispatch_state: HookDispatchState | None = None,
) -> HookResult:
    """Dispatch a normalized hook event and return a structured result."""

    from intent.hooks import lifecycle

    dispatcher = dispatch_fn or lifecycle.dispatch
    state = _resolve_dispatch_state(dispatch_fn=dispatcher, dispatch_state=dispatch_state)
    event_name = coerce_hook_event_name(event.name)
    kwargs = dict(event.payload)
    kwargs.setdefault("hook_event", event_name.value)
    if event.tool_name is not None:
        kwargs.setdefault("tool_name", event.tool_name)
    if event.tool_input is not None:
        kwargs.setdefault("tool_input", event.tool_input)
        kwargs.setdefault("args", event.tool_input)
    if event.session_id is not None:
        kwargs.setdefault("session_id", event.session_id)
    if event.frame_uuid is not None:
        kwargs.setdefault("frame_uuid", event.frame_uuid)

    try:
        result = dispatcher(event_name, **kwargs)
    except Exception as exc:
        logger.warning("Hook dispatcher caught %s failure: %s", event_name.value, exc)
        result = hook_result_from_exception(event_name, getattr(dispatcher, "__name__", "dispatch"), exc)
    try:
        normalized = hook_result_from_value(result, expected_event=event_name)
    except Exception as exc:
        logger.warning("Hook dispatcher rejected %s result: %s", event_name.value, exc)
        normalized = hook_result_from_exception(event_name, getattr(dispatcher, "__name__", "dispatch"), exc)
    state.record_result_state(event, normalized)
    return normalized


def dispatch_lifecycle(
    name: HookEventName | str,
    context: dict[str, Any] | None = None,
    *,
    dispatch_fn: DispatchFn | None = None,
    dispatch_state: HookDispatchState | None = None,
    session_id: str | None = None,
    frame_uuid: str | None = None,
) -> HookResult:
    """Dispatch a lifecycle hook such as SessionStart, Stop, or SubagentStop."""

    payload = dict(context or {})
    resolved_session_id = session_id or _string_or_none(payload.get("session_id"))
    resolved_frame_uuid = frame_uuid or _string_or_none(payload.get("frame_uuid"))
    return dispatch_hook(
        HookEvent(
            name=name,
            payload=payload,
            session_id=resolved_session_id,
            frame_uuid=resolved_frame_uuid,
        ),
        dispatch_fn=dispatch_fn,
        dispatch_state=dispatch_state,
    )


def dispatch_tool_hook(
    name: HookEventName | str,
    tool_name: str,
    tool_input: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    dispatch_fn: DispatchFn | None = None,
    dispatch_state: HookDispatchState | None = None,
    session_id: str | None = None,
    frame_uuid: str | None = None,
) -> HookResult:
    """Dispatch a tool-scoped hook event."""

    return dispatch_hook(
        HookEvent(
            name=name,
            payload=dict(context or {}),
            session_id=session_id,
            frame_uuid=frame_uuid,
            tool_name=tool_name,
            tool_input=tool_input,
        ),
        dispatch_fn=dispatch_fn,
        dispatch_state=dispatch_state,
    )


def has_registered_hooks(name: HookEventName | str, *, registry: Any | None = None, user_id: str | None = None) -> bool:
    """Return True when a lifecycle hook has registered handlers."""

    if registry is not None and hasattr(registry, "has_hooks"):
        return bool(registry.has_hooks(name, user_id=user_id))
    from intent.hooks import lifecycle

    return lifecycle.has_hooks(name, user_id=user_id)


def take_initial_user_message(
    session_id: str | None,
    *,
    dispatch_state: HookDispatchState | None = None,
) -> str | None:
    """Consume one queued SessionStart initial user message for a session."""

    return (dispatch_state or _DEFAULT_DISPATCH_STATE).take_initial_user_message(session_id)


def watch_paths(
    session_id: str | None,
    *,
    dispatch_state: HookDispatchState | None = None,
) -> tuple[str, ...]:
    """Return accumulated dynamic file-watch paths for a session."""

    return (dispatch_state or _DEFAULT_DISPATCH_STATE).watch_paths(session_id)


def clear_dispatcher_state(*, dispatch_state: HookDispatchState | None = None) -> None:
    """Clear queued lifecycle hook state between sessions/tests."""

    (dispatch_state or _DEFAULT_DISPATCH_STATE).clear()


def hook_result_frames(
    event: HookEvent,
    result: HookResult,
    *,
    task_id: str | None = None,
) -> list[Frame]:
    """Convert model-visible hook output into typed attachment meta frames.

    F-036: Claude emits typed attachment messages preserving hook name,
    event, tool_use_id, and attachment type (``utils/hooks.ts:2769-2778``,
    ``utils/sessionStart.ts:162-170``). The pre-fix Viola implementation
    wrapped every hook output — system message, additional context,
    decision — in the same generic SystemReminder envelope, dropping
    the attachment-type signal that distinguishes a system-message
    attachment from an additional-context attachment from a decision
    block. Downstream handlers/telemetry can no longer tell what kind
    of hook output they're looking at.

    Now every emitted frame carries:

    * ``extra["hook_event"]`` — canonical event name
    * ``extra["tool_name"]`` — tool that fired the hook (if any)
    * ``extra["tool_use_id"]`` — Claude's tool-use-id from the event
    * ``extra["attachment_type"]`` — one of ``hook_system_message``,
      ``hook_additional_context``, ``hook_decision`` (matches Claude's
      ``HookAttachmentType`` enum)
    * ``extra["hook_source"]`` — source label (settings|plugin|skill|
      ...) when known from the hook command
    """

    frames: list[Frame] = []
    event_name = coerce_hook_event_name(event.name)
    if result.system_message:
        frames.append(
            _hook_frame(
                event,
                text="%s hook system message%s:\n%s"
                % (event_name.value, _tool_suffix(event.tool_name), result.system_message),
                task_id=task_id,
                attachment_type="hook_system_message",
            )
        )
    for context in result.additional_contexts:
        frames.append(
            _hook_frame(
                event,
                text="%s hook output%s:\n%s" % (event_name.value, _tool_suffix(event.tool_name), context),
                task_id=task_id,
                attachment_type="hook_additional_context",
            )
        )

    if result.decision in {"ask", "deny"} or result.prevent_continuation:
        reason = result.reason or "No reason provided."
        frames.append(
            _hook_frame(
                event,
                text="%s hook decision%s: %s. %s"
                % (
                    event_name.value,
                    _tool_suffix(event.tool_name),
                    "prevent_continuation" if result.prevent_continuation else result.decision,
                    reason,
                ),
                task_id=task_id,
                attachment_type="hook_decision",
            )
        )
    return frames


def _hook_frame(
    event: HookEvent,
    *,
    text: str,
    task_id: str | None,
    attachment_type: str,
) -> Frame:
    event_name = coerce_hook_event_name(event.name)
    payload = event.payload or {}
    tool_use_id = payload.get("tool_use_id") or payload.get("toolUseId")
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag="hook"),),
        is_meta=True,
        origin="hook",
        task_id=task_id,
        session_id=event.session_id,
        uuid=event.frame_uuid,
        extra={
            "hook_event": event_name.value,
            "tool_name": event.tool_name,
            "tool_use_id": str(tool_use_id) if tool_use_id else None,
            "attachment_type": attachment_type,
        },
    )


def _tool_suffix(tool_name: str | None) -> str:
    return " for %s" % tool_name if tool_name else ""


def _event_session_id(event: HookEvent) -> str | None:
    return event.session_id or _string_or_none(event.payload.get("session_id"))


def _session_key(session_id: str | None) -> str:
    return str(session_id or "__default__")


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


def _resolve_dispatch_state(
    *,
    dispatch_fn: DispatchFn | None,
    dispatch_state: HookDispatchState | None,
) -> HookDispatchState:
    if dispatch_state is not None:
        return dispatch_state
    owner = getattr(dispatch_fn, "__self__", None)
    owner_state = getattr(owner, "dispatch_state", None)
    if isinstance(owner_state, HookDispatchState):
        return owner_state
    owner_registry = getattr(owner, "_hook_registry", None)
    owner_registry_state = getattr(owner_registry, "dispatch_state", None)
    if isinstance(owner_registry_state, HookDispatchState):
        return owner_registry_state
    return _DEFAULT_DISPATCH_STATE


__all__ = [
    "HookDispatchState",
    "clear_dispatcher_state",
    "dispatch_hook",
    "dispatch_lifecycle",
    "dispatch_tool_hook",
    "has_registered_hooks",
    "hook_result_frames",
    "take_initial_user_message",
    "watch_paths",
]
