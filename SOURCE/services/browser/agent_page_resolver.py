"""Resolve the pooled cloud agent browser page for streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger
from services.browser.cloud_session_pool import get_cloud_browser_session_pool

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = get_logger(__name__)


async def resolve_agent_page(user_id: str) -> Page | None:
    """Return the active pooled page for ``user_id``, or ``None`` fail-closed."""
    resolved = (user_id or "").strip()
    if not resolved:
        return None
    session = await get_cloud_browser_session_pool().lookup_for_user(resolved)
    if session is None or session.closed:
        logger.debug("No pooled cloud browser page for user_id=%s", resolved)
        return None
    page = session.page
    try:
        if page.is_closed():
            return None
    except Exception:
        return None
    return page


def register_user_cdp_endpoint(user_id: str, cdp_endpoint: str) -> None:
    """Compatibility no-op; cloud stream resolution is pool-only now."""
    del user_id, cdp_endpoint


def unregister_user_cdp_endpoint(user_id: str) -> None:
    """Compatibility no-op; cloud stream resolution is pool-only now."""
    del user_id


__all__ = [
    "register_user_cdp_endpoint",
    "resolve_agent_page",
    "unregister_user_cdp_endpoint",
]
