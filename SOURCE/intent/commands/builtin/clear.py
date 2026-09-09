"""``/clear`` — wipe conversation history while preserving session identity.

Parity reference: ``src/commands/clear/``. Claude's ``/clear``:

1. Fires ``SessionEnd`` hooks (reason=``clear``) so plugins/hooks can
   persist or react to the boundary.
2. Resets session-scoped state: conversation history, file state, MCP
   clients/tools/resources, transcript pointer, queued initial-user
   messages, dispatcher side-channel state.
3. Fires ``SessionStart`` hooks with source=``clear`` so the next turn
   gets a fresh hook-injected initial-user message (matching the
   ``commands/clear/conversation.ts:244-250`` boundary).

The handler is best-effort — when a sub-component isn't wired (e.g. unit
tests with a stub pipeline) the corresponding step records ``False`` in
the returned data dict and we continue with the rest of the reset. This
is the F-008 fix: the previous implementation only invoked one of
``clear_history|reset|reset_conversation|clear`` on the state manager
and skipped every lifecycle hook.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from intent.commands.registry import CommandInvocation, CommandSpec
from intent.hooks.dispatcher import (
    HookDispatchState,
    clear_dispatcher_state,
    dispatch_lifecycle,
)
from intent.hooks.schema import HookEventName

logger = get_logger(__name__)


def command_spec(*, pipeline: Any) -> CommandSpec:
    """Build the ``/clear`` spec bound to ``pipeline``."""

    def _handler(invocation: CommandInvocation) -> dict[str, object]:
        return _handle(invocation, pipeline)

    return CommandSpec(
        name="clear",
        aliases=("/clear", "/reset", "/new"),
        source="builtin",
        handler=_handler,
        remote_safe=False,  # state mutation — never accept remotely.
        description="Clear the current conversation history.",
        command_type="local",
        progress_message="Clearing conversation...",
        is_session_control=True,
        extra={"session_lifecycle": ("SessionEnd", "SessionStart")},
    )


def _handle(invocation: CommandInvocation, pipeline: Any) -> dict[str, object]:
    previous_session_id = invocation.session_id
    session_id = _fresh_session_id()
    manager = _resolve_state_manager(pipeline, previous_session_id)
    registry = getattr(pipeline, "hook_registry", None)
    dispatch_state = getattr(registry, "dispatch_state", None)
    if not isinstance(dispatch_state, HookDispatchState):
        dispatch_state = None

    # 1. SessionEnd — let hooks persist transcript / react to boundary.
    session_end_fired = _fire_lifecycle(
        registry,
        HookEventName.SESSION_END,
        context={"reason": "clear", "session_id": previous_session_id},
        session_id=previous_session_id,
    )

    # 2. Reset session-scoped state.
    cleared = _clear_manager(manager)
    file_state_cleared = _reset_file_state(pipeline)
    mcp_cleared = _reset_mcp_state(pipeline)
    session_identity_updated = _apply_new_session_identity(
        manager,
        pipeline,
        session_id=session_id,
        parent_session_id=previous_session_id,
    )
    if dispatch_state is not None:
        # Clear queued initial-user messages and dynamic watch paths so the
        # post-clear SessionStart starts from a clean slate, matching
        # ``commands/clear/conversation.ts:124-208`` (the dispatcher state
        # is the Python analogue of the Claude ``hooks/state`` module).
        try:
            clear_dispatcher_state(dispatch_state=dispatch_state)
        except (RuntimeError, AttributeError, ValueError) as exc:
            logger.debug("clear_dispatcher_state failed: %s", exc)

    # 3. SessionStart — source=clear, mirrors Claude's restart boundary.
    session_start_fired = _fire_lifecycle(
        registry,
        HookEventName.SESSION_START,
        context={"source": "clear", "session_id": session_id, "parent_session_id": previous_session_id},
        session_id=session_id,
    )

    message = "Conversation cleared." if cleared else "Conversation already empty."
    return {
        "message": message,
        "data": {
            "command": "clear",
            "session_id": session_id,
            "previous_session_id": previous_session_id,
            "parent_session_id": previous_session_id,
            "cleared": cleared,
            "session_identity_updated": session_identity_updated,
            "file_state_cleared": file_state_cleared,
            "mcp_state_cleared": mcp_cleared,
            "session_end_fired": session_end_fired,
            "session_start_fired": session_start_fired,
        },
    }


def _fresh_session_id() -> str:
    try:
        from services.conversation.session_identity import new_session_id

        return new_session_id()
    except (ImportError, RuntimeError, AttributeError, ValueError):
        import uuid

        return "session_%s" % uuid.uuid4().hex


def _apply_new_session_identity(
    manager: Any,
    pipeline: Any,
    *,
    session_id: str,
    parent_session_id: str | None,
) -> bool:
    updated = False
    for owner in (manager, pipeline):
        if owner is None:
            continue
        for attr in ("_session_id", "session_id"):
            try:
                current = getattr(owner, attr, None)
            except (RuntimeError, AttributeError, ValueError):
                continue
            if callable(current):
                continue
            try:
                setattr(owner, attr, session_id)
                updated = True
            except (RuntimeError, AttributeError, ValueError):
                continue
        for attr in ("_parent_session_id", "parent_session_id"):
            try:
                setattr(owner, attr, parent_session_id)
                updated = True
            except (RuntimeError, AttributeError, ValueError):
                continue
    try:
        from bootstrap.session_state import get_session_state

        get_session_state().start_new_session(session_id, parent_session_id=parent_session_id)
        updated = True
    except (ImportError, RuntimeError, AttributeError, ValueError) as exc:
        logger.debug("SessionState /clear identity update failed: %s", exc)
    return updated


def _resolve_state_manager(pipeline: Any, session_id: str | None) -> Any:
    resolver: Callable[..., Any] | None = getattr(pipeline, "get_state_manager", None)
    if callable(resolver):
        try:
            return resolver(session_id=session_id) if session_id is not None else resolver()
        except TypeError:
            return resolver()
    return getattr(pipeline, "conversation_state_manager", None)


def _clear_manager(manager: Any) -> bool:
    if manager is None:
        return False
    for method_name in ("clear_history", "reset_conversation", "clear", "reset"):
        method = getattr(manager, method_name, None)
        if callable(method):
            try:
                method()
                return True
            except Exception:
                continue
    return False


def _fire_lifecycle(
    registry: Any,
    event: HookEventName,
    *,
    context: dict[str, Any],
    session_id: str | None,
) -> bool:
    """Fire a lifecycle hook through the pipeline's registry.

    Returns True if the dispatch call completed (even when no handlers
    were registered), False only if dispatching raised. ``/clear`` reports
    this in its result so callers can verify the boundary fired.
    """

    try:
        if registry is not None and hasattr(registry, "dispatch_lifecycle"):
            registry.dispatch_lifecycle(event, context, session_id=session_id)
        else:
            # Fallback to the module-level dispatcher (legacy registry).
            dispatch_lifecycle(event, context, session_id=session_id)
        return True
    except (RuntimeError, AttributeError, ValueError) as exc:
        logger.debug("Lifecycle %s dispatch failed: %s", event, exc)
        return False


def _reset_file_state(pipeline: Any) -> bool:
    """Drop the read-file cache the agent uses for context tracking, if any."""

    cleared = False
    for attr in ("file_state", "_file_state_cache", "_recent_files"):
        value = getattr(pipeline, attr, None)
        if value is None:
            continue
        for method in ("clear", "reset"):
            method_fn = getattr(value, method, None)
            if callable(method_fn):
                try:
                    method_fn()
                    cleared = True
                except (RuntimeError, AttributeError, ValueError) as exc:
                    logger.debug("file-state reset %s.%s failed: %s", attr, method, exc)
                break
    return cleared


def _reset_mcp_state(pipeline: Any) -> bool:
    """Drop cached MCP tools/resources so the next session start re-discovers them."""

    cleared = False
    ai = getattr(pipeline, "ai_controller", None)
    for owner in (pipeline, ai):
        if owner is None:
            continue
        for attr in ("mcp_hub", "_mcp_tool_cache", "_mcp_resource_cache"):
            cache = getattr(owner, attr, None)
            if cache is None:
                continue
            for method in ("reset_session", "clear_cache", "clear"):
                method_fn = getattr(cache, method, None)
                if callable(method_fn):
                    try:
                        method_fn()
                        cleared = True
                    except (RuntimeError, AttributeError, ValueError) as exc:
                        logger.debug("mcp-state reset %s.%s failed: %s", attr, method, exc)
                    break
    return cleared


__all__ = ["command_spec"]
