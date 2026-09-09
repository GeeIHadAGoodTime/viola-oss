"""Runtime state hook for post-tool agent bookkeeping (LA-4).

Tracks browser URL, API registry usage, and browser escalation tiers. It does
not filter, scope, or select the tool surface.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.hooks.base import HookContext, PostToolHook

logger = get_logger(__name__)

_BROWSER_TIER_MAP: dict[str, str] = {
    "browser_navigate": "ref",
    "browser_interact": "ref",
    "browser_fill_form": "ref",
    "browser_run_script": "script",
    "browser_screenshot": "visual",
}


class RuntimeStateHook(PostToolHook):
    """Tracks runtime state after each tool call."""

    def __init__(
        self,
        track_page_url_fn: Any,
    ) -> None:
        self._track_page_url = track_page_url_fn
        self._api_registry_checked: bool = False

    @property
    def api_registry_checked(self) -> bool:
        """Whether check_api_registry has been called."""

        return self._api_registry_checked

    async def on_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        tool_error: str | None,
        context: HookContext,
    ) -> None:
        """Update browser URL, API registry flag, and escalation tiers."""

        self._track_page_url(tool_name, tool_result)

        if tool_name == "check_api_registry":
            self._api_registry_checked = True

        tier = _BROWSER_TIER_MAP.get(tool_name)
        if tier:
            context.browser_tiers_used.add(tier)
