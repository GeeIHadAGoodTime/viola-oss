"""Post-tool hook middleware chain for the agent executor (LA-4).

This package extracts cross-cutting post-tool logic from the monolithic
agent executor loop into independently testable hook classes.

Hook Execution Order
--------------------
Hooks run in this exact order after every tool call.  The order matches
the original inline logic in ``agent_executor.py``:

1. **RuntimeStateHook** — Track browser URL, API registry, browser escalation
   tiers.  Runs first because later hooks may need the updated URL/tier
   state.

2. **StepLoggerHook** — Record structured step to task log, emit JSONL,
   broadcast WebSocket event, telemetry.  Runs early so that all tool
   calls are logged even if later hooks raise.

3. **SpinDetectorHook** — Record tool success/failure with the spin
   detector.  Updates ``context.consecutive_failures``.

4. **ApprovalBridgeHook** — Check for approval rejections, blacklist tools,
   inject correction messages (Fix 2, 3, 4, 5).

5. **TokenTrackerHook** — Update the token budget tracker with the tool
   result size.  Runs last because it's purely informational.

Usage::

    from intent.hooks import HookChain, build_default_chain

    chain = build_default_chain(...)
    await chain.run(tool_name, tool_input, tool_result, tool_error, context)
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook

logger = get_logger(__name__)


class HookChain:
    """Runs a sequence of :class:`PostToolHook` instances in order.

    Each hook receives the same arguments and shared ``HookContext``.
    If a hook raises an exception, it is logged and the chain continues
    (hooks are non-critical — a failure in logging must not break the
    agent loop).
    """

    def __init__(self, hooks: list[PostToolHook]) -> None:
        self._hooks = hooks

    async def run(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Execute all hooks in order, catching and logging errors."""
        for hook in self._hooks:
            try:
                await hook.on_tool_call(
                    tool_name,
                    tool_input,
                    tool_result,
                    tool_error,
                    context,
                )
            except Exception as exc:
                logger.warning(
                    "Post-tool hook %s failed for %s: %s",
                    hook.name,
                    tool_name,
                    exc,
                )

    @property
    def hooks(self) -> list[PostToolHook]:
        """Read-only access to the hook list."""
        return list(self._hooks)


from intent.hooks.dispatcher import (
    clear_dispatcher_state,
    dispatch_hook,
    dispatch_lifecycle,
    dispatch_tool_hook,
    has_registered_hooks,
    hook_result_frames,
    take_initial_user_message,
    watch_paths,
)
from intent.hooks.lifecycle import (
    HookRegistry,
    clear_hooks,
    create_hook_registry,
    dispatch,
    get_default_hook_registry,
    register_hook,
)
from intent.hooks.schema import HookEvent, HookEventName, HookResult
import intent.hooks.safety as safety

__all__ = [
    "HookChain",
    "HookContext",
    "HookEvent",
    "HookEventName",
    "HookRegistry",
    "HookResult",
    "PostToolHook",
    "clear_dispatcher_state",
    "clear_hooks",
    "create_hook_registry",
    "dispatch",
    "dispatch_hook",
    "dispatch_lifecycle",
    "dispatch_tool_hook",
    "get_default_hook_registry",
    "has_registered_hooks",
    "hook_result_frames",
    "register_hook",
    "safety",
    "take_initial_user_message",
    "watch_paths",
]
