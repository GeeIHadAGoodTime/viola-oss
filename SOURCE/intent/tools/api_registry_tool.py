"""Agent tool: return neutral facts about direct API integration availability."""

from __future__ import annotations

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)


async def check_api_registry_handler(service_or_url: str) -> ToolResult:
    """Check whether a service or URL has a direct API integration entry.

    Args:
        service_or_url: Service name (e.g., 'gmail') or URL to check.
    """
    from services.api_registry.registry import get_api_registry

    if not service_or_url or not service_or_url.strip():
        return ToolResult(
            ok=False,
            data=None,
            error="service_or_url is required",
        )

    registry = get_api_registry()
    entry = None
    query = service_or_url.strip()
    # Try URL lookup
    if query.startswith("http"):
        entry = registry.lookup_by_domain(query)
    else:
        entry = registry.lookup_by_service(query)

    if entry and entry.status == "active":
        return ToolResult(
            ok=True,
            data={
                "api_available": True,
                "service": entry.service_name,
                "display_name": entry.display_name,
                "status": entry.status,
                "source": entry.source,
                "tools": entry.tools,
                "description": entry.description,
                "domains": entry.domains,
                "auth_type": entry.auth_type or "",
                "notes": entry.notes,
            },
        )

    if entry and entry.status == "needs_auth":
        return ToolResult(
            ok=True,
            data={
                "api_available": False,
                "service": entry.service_name,
                "display_name": entry.display_name,
                "status": entry.status,
                "source": entry.source,
                "tools": entry.tools,
                "description": entry.description,
                "domains": entry.domains,
                "auth_type": entry.auth_type or "",
                "notes": entry.notes,
            },
        )

    return ToolResult(
        ok=True,
        data={
            "api_available": False,
            "service": query,
            "status": "not_found",
            "tools": [],
            "description": "",
            "domains": [],
            "auth_type": "",
            "notes": "",
        },
    )
