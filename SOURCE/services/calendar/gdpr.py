"""GDPR export/purge for the local calendar provider's per-user stores.

The always-on local calendar provider persists per-user event SQLite files
under ``data_dir/calendar/local/<sha256(user_id)>/events.db`` and offline
fallback records under ``data_dir/calendar_fallback/<user_id>/events.json``.
On desktop these are the user's own machine; with the cloud calendar route
group enabled (backend/cloud_route_manifest.py) the SAME stores live on the
cloud data volume — server-held user content that must round-trip through
Article 15/20 export and Article 17 deletion (auth/gdpr.py wires both).

Helpers are synchronous (GDPR service call sites are sync blocks inside async
methods) and read the SQLite store directly; the per-user path computation is
shared with the providers via their public ``user_storage_dir`` methods, so
the partitioning scheme has a single source of truth.
"""

from __future__ import annotations

import shutil
import sqlite3
from typing import Any

from core.logging_config import get_logger
from services.calendar.fallback import CalendarFallbackStorage
from services.calendar.providers.local import LocalCalendarProvider

logger = get_logger(__name__)


def _require_user_id(user_id: str) -> str:
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


def _read_local_events(user_id: str) -> list[dict[str, Any]]:
    db_path = LocalCalendarProvider().user_storage_dir(user_id) / "events.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def export_local_calendar_for_user(user_id: str) -> dict[str, Any]:
    """Export every local-provider event and pending fallback record."""
    uid = _require_user_id(user_id)
    return {
        "events": _read_local_events(uid),
        "pending_fallback_events": CalendarFallbackStorage().load_fallback_events(user_id=uid),
    }


def delete_local_calendar_for_user(user_id: str) -> dict[str, int]:
    """Delete the user's local calendar + fallback stores; returns counts."""
    uid = _require_user_id(user_id)
    events = _read_local_events(uid)
    fallback_storage = CalendarFallbackStorage()
    fallback_events = fallback_storage.load_fallback_events(user_id=uid)

    local_dir = LocalCalendarProvider().user_storage_dir(uid)
    if local_dir.exists():
        shutil.rmtree(local_dir)
    fallback_dir = fallback_storage.user_storage_dir(uid)
    if fallback_dir.exists():
        shutil.rmtree(fallback_dir)

    logger.info(
        "GDPR purge removed %d local calendar event(s) and %d fallback record(s) for user %s",
        len(events),
        len(fallback_events),
        uid,
    )
    return {
        "events": len(events),
        "pending_fallback_events": len(fallback_events),
    }


def _json_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in event.items():
        if hasattr(value, "isoformat"):
            safe[key] = value.isoformat()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = str(value)
    return safe


async def export_caldav_calendar_for_user(user_id: str) -> dict[str, Any]:
    """Export the user's Viola-hosted CalDAV events (cloud self-host backend).

    Read-only: uses ``provision=False`` so exporting a user who never used the
    calendar can NEVER create a CalDAV account as a side effect. Returns an
    empty, honest payload on deployments without a hosted backend (desktop).
    """
    uid = _require_user_id(user_id)
    from services.calendar.providers.caldav import CalDAVCalendarProvider, CalDAVProviderError

    provider = CalDAVCalendarProvider()
    try:
        credentials = await provider.load_credentials(uid, provision=False)
    except CalDAVProviderError:
        # No cloud credential store on this surface (desktop without caldav
        # setup) — nothing hosted for this user.
        return {"provisioned": False, "events": []}
    if credentials is None:
        return {"provisioned": False, "events": []}
    events = await provider.list_events(uid, max_results=10000)
    return {"provisioned": True, "events": [_json_safe_event(e) for e in events]}


async def delete_caldav_calendar_for_user(user_id: str) -> dict[str, Any]:
    """Purge the user's Viola-hosted CalDAV account: collections, htpasswd, credential row.

    The provisioning shim's DELETE removes both the htpasswd entry and the
    user's whole collection tree (deploy/calendar/provision.py), so no orphaned
    iCalendar data survives on the calendar volume. The credential row is also
    FK-cascade-covered on account deletion; deleting it here keeps the purge
    honest when called outside a full account cascade.
    """
    uid = _require_user_id(user_id)
    from services.calendar.cloud_caldav import CloudCalDAVProvisioner, cloud_caldav_configured
    from services.calendar.providers.caldav import CalDAVCalendarProvider

    if not cloud_caldav_configured():
        return {"provisioned": False, "events": 0, "deprovisioned": False}

    provider = CalDAVCalendarProvider()
    credentials = await provider.load_credentials(uid, provision=False)
    if credentials is None:
        return {"provisioned": False, "events": 0, "deprovisioned": False}

    events = await provider.list_events(uid, max_results=10000)
    deprovisioned = await CloudCalDAVProvisioner().deprovision(uid)
    await provider.delete_credentials(uid)
    logger.info(
        "GDPR purge removed hosted CalDAV account (%d event(s)) for user %s",
        len(events),
        uid,
    )
    return {"provisioned": True, "events": len(events), "deprovisioned": deprovisioned}


__all__ = [
    "delete_caldav_calendar_for_user",
    "delete_local_calendar_for_user",
    "export_caldav_calendar_for_user",
    "export_local_calendar_for_user",
]
