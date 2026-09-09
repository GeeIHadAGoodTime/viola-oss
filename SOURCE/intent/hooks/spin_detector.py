"""Spin detection hook — tracks tool success/failure patterns (LA-4).

Extracts spin detection recording from the inline agent executor code.
This hook records each tool call result (success or failure) with the
SpinDetector instance.  Spin *checking* (is_spinning()) remains in the
main loop since it generates intervention messages that need to be
injected into the conversation at a specific point.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook

logger = get_logger(__name__)


class SpinDetectorHook(PostToolHook):
    """Records tool outcomes with the SpinDetector for pattern analysis.

    Also tracks consecutive failure counts on the HookContext.
    """

    def __init__(self, spin_detector: Any) -> None:
        self._detector = spin_detector

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Record success or failure with the spin detector."""
        # Soft-failure detection: browser tools catch Playwright exceptions
        # and return ok=True with data={"error": "..."}.  The spin detector
        # must see these as failures.
        _soft_fail = (
            tool_result.ok
            and isinstance(getattr(tool_result, "data", None), dict)
            and "error" in tool_result.data
            and not any(k in tool_result.data for k in ("filled", "clicked", "selected", "output"))
        )

        if tool_error or not tool_result.ok or _soft_fail:
            _fail_msg = str(
                tool_error or tool_result.error or (tool_result.data.get("error", "") if _soft_fail else "")
            )[:200]
            self._detector.record_failure(
                tool_name,
                str(tool_input)[:200],
                _fail_msg,
            )
            context.consecutive_failures += 1
        else:
            self._detector.record_success(
                tool_name,
                input_sig=str(tool_input)[:200],
            )
            context.consecutive_failures = 0
