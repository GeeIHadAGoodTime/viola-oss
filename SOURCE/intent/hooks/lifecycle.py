"""Lifecycle hook registry for the agent executor."""

from __future__ import annotations

from typing import Any, Callable

from core.logging_config import get_logger
from intent.hooks.schema import (
    CANONICAL_HOOK_EVENTS,
    HookEventName,
    HookResult,
    coerce_hook_event_name,
    hook_result_from_exception,
    hook_result_from_value,
)

logger = get_logger(__name__)

HookHandler = Callable[..., Any]
_GLOBAL_SCOPE = "__global__"
_LEGACY_SCOPE = "__legacy__"

_LEGACY_EVENTS: tuple[str, ...] = (
    "pre_tool_use",
    "post_tool_use",
    "task_completed",
    "task_failed",
    "TaskFailed",
)
_VALID_EVENTS: tuple[str | HookEventName, ...] = (*_LEGACY_EVENTS, *CANONICAL_HOOK_EVENTS)
_VALID_EVENT_KEYS: tuple[str, ...] = tuple(str(event) for event in _VALID_EVENTS)

# F-006 (R3-A): events declared in ``HookEventName`` that production code
# actually dispatches. Events listed in the canonical enum but absent here
# can be registered (legacy compatibility) but will warn so users don't
# silently subscribe to events that will never fire. The warning includes
# the closest canonical event that *is* dispatched.
#
# When a new dispatch site is added (e.g. R9-B wires F-004 settings-source
# loading), add the event to this set.
_DISPATCHED_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        # Tool lifecycle
        HookEventName.PRE_TOOL_USE.value,
        HookEventName.POST_TOOL_USE.value,
        HookEventName.POST_TOOL_USE_FAILURE.value,
        HookEventName.USER_PROMPT_SUBMIT.value,
        HookEventName.NOTIFICATION.value if hasattr(HookEventName, "NOTIFICATION") else "Notification",
        # Session / agent lifecycle
        HookEventName.SESSION_START.value,
        HookEventName.SESSION_END.value,
        HookEventName.STOP.value,
        HookEventName.STOP_FAILURE.value if hasattr(HookEventName, "STOP_FAILURE") else "StopFailure",
        HookEventName.SUBAGENT_START.value if hasattr(HookEventName, "SUBAGENT_START") else "SubagentStart",
        HookEventName.SUBAGENT_STOP.value,
        HookEventName.PRE_COMPACT.value,
        HookEventName.POST_COMPACT.value,
        HookEventName.PERMISSION_REQUEST.value,
        HookEventName.PERMISSION_DENIED.value if hasattr(HookEventName, "PERMISSION_DENIED") else "PermissionDenied",
        # Watcher / context lifecycle
        HookEventName.CWD_CHANGED.value,
        HookEventName.FILE_CHANGED.value,
        # Task / teammate lifecycle (dispatched at agent_loop.py:4081-4113)
        HookEventName.TASK_COMPLETED.value,
        HookEventName.TEAMMATE_IDLE.value,
        # Elicitation request flow
        HookEventName.ELICITATION.value if hasattr(HookEventName, "ELICITATION") else "Elicitation",
        # Setup-time only
        HookEventName.SETUP.value if hasattr(HookEventName, "SETUP") else "Setup",
        # Legacy registry keys
        *_LEGACY_EVENTS,
    }
)

_ALIASES: dict[str, tuple[str, ...]] = {
    "PreToolUse": ("pre_tool_use",),
    "pre_tool_use": ("PreToolUse",),
    "PostToolUse": ("post_tool_use",),
    "post_tool_use": ("PostToolUse",),
    "TaskCompleted": ("task_completed",),
    "task_completed": ("TaskCompleted",),
    "TaskFailed": ("task_failed",),
    "task_failed": ("TaskFailed",),
}


_UNDISPATCHED_WARNED: set[str] = set()


def _warn_undispatched_hook_event(event_key: str, handler_name: str) -> None:
    """Log a one-time warning when a hook is registered for an undispatched event.

    F-006: ``HookEventName`` carries Claude-canon events that Viola has not
    wired (TaskCreated, WorktreeCreate/Remove, ConfigChange, InstructionsLoaded,
    ElicitationResult). The schema accepts them so settings files written
    against Claude don't crash at load, but users should know their hook will
    never fire. R9-B / R9-D / R9-E may wire the missing dispatch later.
    """

    if event_key in _UNDISPATCHED_WARNED:
        return
    _UNDISPATCHED_WARNED.add(event_key)
    logger.warning(
        "Hook event %r is declared in HookEventName but is not yet "
        "dispatched from production code; handler %s will never fire. "
        "Track the dispatch site in the R9 wave fixes (F-004/F-005/F-006).",
        event_key,
        handler_name,
    )


def _new_event_registry() -> dict[str, list[HookHandler]]:
    registry: dict[str, list[HookHandler]] = {
        "pre_tool_use": [],
        "post_tool_use": [],
        "task_completed": [],
        "task_failed": [],
    }
    for event in _VALID_EVENTS:
        registry.setdefault(_event_key(event), [])
    return registry


def _event_key(event: HookEventName | str) -> str:
    if isinstance(event, HookEventName):
        return event.value
    return str(event).strip()


def _canonical_or_legacy_event_key(event: HookEventName | str) -> str:
    key = _event_key(event)
    if key in _LEGACY_EVENTS:
        return key
    try:
        return coerce_hook_event_name(key).value
    except ValueError as exc:
        raise ValueError("Unknown hook event: %s. Valid: %s" % (event, list(_VALID_EVENT_KEYS))) from exc


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _handler_name(handler: HookHandler) -> str:
    return str(getattr(handler, "__name__", handler.__class__.__name__))


class HookRegistry:
    """Owned hook registry with explicit global and per-user partitions."""

    def __init__(self, *, default_user_id: str | None = None) -> None:
        from intent.hooks.dispatcher import HookDispatchState

        self._handlers_by_scope: dict[str, dict[str, list[HookHandler]]] = {}
        self._default_user_id = _string_or_none(default_user_id)
        self.dispatch_state = HookDispatchState()

    def register_hook(
        self,
        event: HookEventName | str,
        handler: HookHandler,
        *,
        user_id: str | None = None,
        global_hook: bool = False,
    ) -> None:
        """Register a handler for a lifecycle event in an explicit scope."""

        event_key = _canonical_or_legacy_event_key(event)
        if event_key not in _VALID_EVENT_KEYS:
            raise ValueError("Unknown hook event: %s. Valid: %s" % (event, list(_VALID_EVENT_KEYS)))
        # F-006 (R3-A): events listed in ``HookEventName`` but never
        # dispatched from production code accept registration (legacy
        # compatibility) and warn once per process so users don't
        # silently subscribe to events that will never fire.
        if event_key not in _DISPATCHED_HOOK_EVENTS:
            _warn_undispatched_hook_event(event_key, _handler_name(handler))
        scope = self._registration_scope(user_id=user_id, global_hook=global_hook)
        self._scope_registry(scope)[event_key].append(handler)
        try:
            from bootstrap.session_state import get_session_state

            state_user_id = _string_or_none(user_id) or self._default_user_id
            if state_user_id:
                get_session_state(user_id=state_user_id).register_hook(event_key)
        except (ImportError, RuntimeError, AttributeError, ValueError) as exc:
            logger.debug("SessionState hook registration update failed: %s", exc)
        logger.debug("Registered %s hook in %s scope: %s", event_key, scope, _handler_name(handler))

    def dispatch(self, event_name: HookEventName | str, **kwargs: Any) -> HookResult:
        """Dispatch a lifecycle event to handlers visible to this request."""

        event_key = _canonical_or_legacy_event_key(event_name)
        if event_key not in _VALID_EVENT_KEYS:
            raise ValueError("Unknown hook event: %s. Valid: %s" % (event_name, list(_VALID_EVENT_KEYS)))
        user_id = _string_or_none(kwargs.get("user_id"))
        handlers = self.handlers_for_event(event_key, user_id=user_id)
        result = HookResult()
        for handler in handlers:
            try:
                result = result.merge(hook_result_from_value(handler(**kwargs), expected_event=event_key))
            except Exception as exc:
                if exc.__class__.__name__ == "SafetyBlockError":
                    raise
                logger.exception("Hook %s handler %s failed", event_key, _handler_name(handler))
                result = result.merge(hook_result_from_exception(event_key, _handler_name(handler), exc))
        return result

    def clear(self) -> None:
        """Remove all registered handlers and dispatcher side-channel state."""

        for registry in self._handlers_by_scope.values():
            for handlers in registry.values():
                handlers.clear()
        self.dispatch_state.clear()
        try:
            from bootstrap.session_state import clear_all_session_hook_state

            clear_all_session_hook_state()
        except (ImportError, RuntimeError, AttributeError, ValueError) as exc:
            logger.debug("SessionState hook clear update failed: %s", exc)

    def has_hooks(self, event: HookEventName | str, *, user_id: str | None = None) -> bool:
        """Return True if a scoped handler is registered for an event or alias."""

        return bool(self.handlers_for_event(event, user_id=user_id))

    def handlers_for_event(self, event: HookEventName | str, *, user_id: str | None = None) -> list[HookHandler]:
        handlers: list[HookHandler] = []
        seen: set[int] = set()
        event_key = _canonical_or_legacy_event_key(event)
        for scope in self._dispatch_scopes(user_id):
            registry = self._handlers_by_scope.get(scope, {})
            for registry_key in (event_key, *_ALIASES.get(event_key, ())):
                for handler in registry.get(registry_key, []):
                    marker = id(handler)
                    if marker in seen:
                        continue
                    handlers.append(handler)
                    seen.add(marker)
        return handlers

    def dispatch_lifecycle(
        self,
        name: HookEventName | str,
        context: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        frame_uuid: str | None = None,
    ) -> HookResult:
        """Dispatch a lifecycle hook through this registry."""

        from intent.hooks.dispatcher import dispatch_lifecycle

        return dispatch_lifecycle(
            name,
            context,
            dispatch_fn=self.dispatch,
            dispatch_state=self.dispatch_state,
            session_id=session_id,
            frame_uuid=frame_uuid,
        )

    def dispatch_tool_hook(
        self,
        name: HookEventName | str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        frame_uuid: str | None = None,
    ) -> HookResult:
        """Dispatch a tool hook through this registry."""

        from intent.hooks.dispatcher import dispatch_tool_hook

        return dispatch_tool_hook(
            name,
            tool_name,
            tool_input,
            context,
            dispatch_fn=self.dispatch,
            dispatch_state=self.dispatch_state,
            session_id=session_id,
            frame_uuid=frame_uuid,
        )

    def take_initial_user_message(self, session_id: str | None) -> str | None:
        """Consume one queued initial user message from this registry state."""

        return self.dispatch_state.take_initial_user_message(session_id)

    def watch_paths(self, session_id: str | None) -> tuple[str, ...]:
        """Return dynamic watch paths from this registry state."""

        return self.dispatch_state.watch_paths(session_id)

    def seed_watch_paths(self, session_id: str | None, paths: tuple[str, ...]) -> None:
        """Seed static watch paths from settings before SessionStart completes."""

        self.dispatch_state.seed_watch_paths(session_id, paths)

    def _registration_scope(self, *, user_id: str | None, global_hook: bool) -> str:
        if global_hook:
            return _GLOBAL_SCOPE
        resolved_user_id = _string_or_none(user_id) or self._default_user_id
        if resolved_user_id:
            return "user:%s" % resolved_user_id
        return _LEGACY_SCOPE

    def _dispatch_scopes(self, user_id: str | None) -> tuple[str, ...]:
        resolved_user_id = _string_or_none(user_id) or self._default_user_id
        if resolved_user_id:
            return (_GLOBAL_SCOPE, "user:%s" % resolved_user_id)
        return (_GLOBAL_SCOPE, _LEGACY_SCOPE)

    def _scope_registry(self, scope: str) -> dict[str, list[HookHandler]]:
        registry = self._handlers_by_scope.get(scope)
        if registry is None:
            registry = _new_event_registry()
            self._handlers_by_scope[scope] = registry
        return registry


_DEFAULT_REGISTRY = HookRegistry()


def create_hook_registry(
    *,
    default_user_id: str | None = None,
    include_builtin_safety: bool = True,
) -> HookRegistry:
    """Create an owned hook registry for a pipeline or executor."""

    registry = HookRegistry(default_user_id=default_user_id)
    if include_builtin_safety:
        from intent.hooks.safety import register_safety_hook

        register_safety_hook(registry.register_hook)
    return registry


def get_default_hook_registry() -> HookRegistry:
    """Return the legacy module-level registry used by compatibility wrappers."""

    return _DEFAULT_REGISTRY


def register_hook(
    event: HookEventName | str,
    handler: HookHandler,
    *,
    user_id: str | None = None,
    global_hook: bool = False,
) -> None:
    """Register a handler for a lifecycle event."""

    _DEFAULT_REGISTRY.register_hook(event, handler, user_id=user_id, global_hook=global_hook)


def dispatch(event_name: HookEventName | str, **kwargs: Any) -> HookResult:
    """Dispatch a lifecycle event to all registered handlers."""

    return _DEFAULT_REGISTRY.dispatch(event_name, **kwargs)


def clear_hooks() -> None:
    """Remove all registered hooks."""

    _DEFAULT_REGISTRY.clear()
    try:
        from intent.hooks.dispatcher import clear_dispatcher_state

        clear_dispatcher_state()
    except ImportError:
        logger.debug("Hook dispatcher state clear skipped during import")


def has_hooks(event: HookEventName | str, *, user_id: str | None = None) -> bool:
    """Return True if any handler is registered for an event or its aliases."""

    return _DEFAULT_REGISTRY.has_hooks(event, user_id=user_id)
