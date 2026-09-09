"""Approval bridge hook - records deterministic approval rejections (LA-4).

The hook preserves explicit approval-policy denials. It does not apply
counter-based correction messages or model-choice filters.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook

logger = get_logger(__name__)


def _is_approval_block_error(error_category: str | None = None) -> bool:
    """Return True when a tool result is an approval-policy denial."""
    return str(error_category or "").upper() == "APPROVAL_BLOCKED"


class ApprovalBridgeHook(PostToolHook):
    """Post-tool hook for approval-related rejection context.

    Handles:
    - Fix 3: Blacklist tools after approval rejection
    """

    def __init__(self) -> None:
        pass

    def reset(self) -> None:
        """Reset per-task state."""
        pass

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Check for approval rejections and inject correction messages."""
        # Fix 3: remember deterministic approval rejection context.
        if (
            _is_approval_block_error(getattr(tool_result, "error_category", None))
            and tool_name not in context.rejected_tools
        ):
            context.rejected_tools.add(tool_name)
            logger.warning(
                "Fix3: recording approval rejection for %s (task=%s)",
                tool_name,
                context.task_id,
            )
            context.correction_messages.append("Tool %s is not available for this task." % tool_name)
