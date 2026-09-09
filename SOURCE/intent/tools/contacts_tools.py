"""Contacts tools for the agent executor (#3282, read-only CardDAV bridge).

Resolves a name to a phone number/email against a user's connected CardDAV
account (iCloud, or any other RFC 6352 server) so the agent can act on
requests like "call Jay" or "email my mom" -- see
services/contacts/providers/carddav.py for the client itself. Read-only:
there is no write/create/update/delete handler here, per the wave-1 scope.
"""

from __future__ import annotations

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)


def _require_user_id(user_id: str | None) -> str:
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


async def contacts_find_handler(user_id: str, name: str) -> ToolResult:
    """Resolve a name to matching contact(s) with phone/email, if connected."""
    if not name or not name.strip():
        return ToolResult(ok=False, data=None, error="A contact name is required.")

    from services.contacts import get_contacts_provider

    provider = get_contacts_provider()
    uid = _require_user_id(user_id)

    configured = await provider.is_configured(uid)
    if not configured:
        return ToolResult(
            ok=True,
            data={
                "connected": False,
                "matches": [],
                "message": (
                    "No iCloud (or other CardDAV) contacts account is connected. Connecting iCloud "
                    "Calendar in Settings > Calendar with an Apple ID and app-specific password also "
                    "connects contacts, since they share the same account."
                ),
            },
        )

    matches = await provider.find_contact(uid, name.strip())
    if not matches:
        return ToolResult(
            ok=True,
            data={
                "connected": True,
                "matches": [],
                "message": "No contact matching '%s' was found." % name.strip(),
            },
        )

    return ToolResult(
        ok=True,
        data={
            "connected": True,
            "matches": matches,
            "message": "Found %d matching contact(s)." % len(matches),
        },
    )


async def contacts_list_handler(user_id: str, max_results: int = 50) -> ToolResult:
    """List connected contacts (read-only), for browsing rather than name lookup."""
    from services.contacts import get_contacts_provider

    provider = get_contacts_provider()
    uid = _require_user_id(user_id)

    configured = await provider.is_configured(uid)
    if not configured:
        return ToolResult(
            ok=True,
            data={
                "connected": False,
                "contacts": [],
                "message": (
                    "No iCloud (or other CardDAV) contacts account is connected. Connecting iCloud "
                    "Calendar in Settings > Calendar with an Apple ID and app-specific password also "
                    "connects contacts, since they share the same account."
                ),
            },
        )

    contacts = await provider.list_contacts(uid, max_results=max(1, min(max_results, 200)))
    return ToolResult(
        ok=True,
        data={
            "connected": True,
            "contacts": contacts,
            "message": "%d contact(s) found." % len(contacts),
        },
    )
