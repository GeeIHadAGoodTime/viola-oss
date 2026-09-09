"""Token budget tracking hook — updates token estimates after each tool call (LA-4).

Feeds tool result sizes into the :class:`TokenBudgetTracker` so it maintains
an accurate running estimate of conversation token usage.  The actual
compaction decision is made in ``_get_next_response()`` — this hook just
ensures the tracker stays up to date.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook
from intent.token_budget import TokenBudgetTracker

logger = get_logger(__name__)


class TokenTrackerHook(PostToolHook):
    """Updates the token budget tracker with each tool result."""

    def __init__(self, tracker: TokenBudgetTracker) -> None:
        self._tracker = tracker

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Estimate tokens for this tool result and update the tracker."""
        result_text = tool_result.to_llm_text() if hasattr(tool_result, "to_llm_text") else str(tool_result)
        # Simulate the message that will be appended to conversation history
        self._tracker.add_message({"role": "user", "content": "[TOOL_RESULT: %s]\n%s" % (tool_name, result_text)})
