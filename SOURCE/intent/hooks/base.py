"""Base hook interface for the agent executor post-tool middleware chain (LA-4).

All post-tool hooks inherit from :class:`PostToolHook` and implement
:meth:`on_tool_call`.  Hooks receive a shared :class:`HookContext` that
carries mutable state across the chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookContext:
    """Shared state passed through the hook chain.

    Mutable — hooks may read and modify fields to communicate state
    to downstream hooks or back to the agent executor.
    """

    iteration: int
    task_id: str
    messages: list[dict[str, Any]]
    """Current conversation messages (text or native format)."""
    visible_tool_allowlist: set[str] | None
    """Optional runtime allowlist; production uses None for all-tools mode."""
    tools_called: list[str]
    """Running list of all tool names called so far."""
    use_native: bool
    """True if using Anthropic native tool calling path."""
    browser_tiers_used: set[str]
    """Browser escalation tiers seen (ref, script, visual)."""
    rejected_tools: set[str]
    """Tools blacklisted during this task."""
    allowed_tools: set[str] | None
    """Historical allowlist slot; production uses None."""
    correction_messages: list[str] = field(default_factory=list)
    """Deferred factual correction messages to append after tool results."""
    consecutive_failures: int = 0
    """Count of consecutive tool failures."""
    llm_reasoning: str = ""
    """LLM reasoning text from the provider response (for step logs)."""


class PostToolHook:
    """Base class for post-tool-call hooks.

    Subclasses implement :meth:`on_tool_call` which is called after every
    tool execution.  Hooks may:
    - Log / record the tool call
    - Update shared state in ``context``
    - Append factual correction messages to ``context.correction_messages``
    - Raise ``StopIteration`` to signal the agent should halt (force-terminate)
    """

    @property
    def name(self) -> str:
        """Human-readable hook name for logging."""
        return self.__class__.__name__

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Called after every tool call completes.

        Args:
            tool_name: Name of the tool that was called.
            tool_input: Arguments passed to the tool.
            tool_result: The ToolResult object returned by the tool.
            tool_error: Error string if the tool failed, None otherwise.
            context: Shared mutable state for the hook chain.
        """
        ...
