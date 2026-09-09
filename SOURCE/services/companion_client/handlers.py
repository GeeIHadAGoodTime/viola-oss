"""Capability handlers -- the desktop's answers to companion commands.

Each handler implements one ``<scope>.<action>`` from
``services/companion/protocol.py``. Handlers DELEGATE to existing desktop
code (``LocalMusicProvider``, ``HomeAssistantClient``, ...) rather than
reimplementing it -- the desktop already knows how to read the local music
library and reach the LAN smart home; the companion client only re-exposes
those capabilities to the user's own cloud account.

Design rules (fail safe):

* A handler returns a JSON-serializable dict on success.
* A handler returns ``{"error": "<reason>"}`` for an expected failure
  (not configured, not found, bad input). The client turns that into an
  ``error`` frame; it never crashes the connection.
* Handlers do not trust the payload -- every field is validated/clamped.
* Handlers serve the user's *own* data only. There is no cross-user path:
  the desktop has exactly one local library / one HA config.

Scopes implemented here:

* ``files``      -- the on-disk music library (``file_list`` search,
                    ``directory_browse`` playlists, ``file_read`` track
                    metadata).
* ``smart_home`` -- relays to the LAN Home Assistant instance.
* ``system``     -- health / version introspection.
* ``agent``      -- the whole-turn relay (``agent.dispatch``). Its handler
                    lives in :mod:`agent_handler` because it runs the full
                    desktop intent pipeline rather than one bounded action.
"""

from __future__ import annotations

import time
from typing import Any

from core.constants import VIOLA_VERSION
from core.logging_config import get_logger

logger = get_logger(__name__)

# Desktop's device user id -- the local music library is single-tenant on the
# desktop, so every read is scoped to this one principal.
from core.user_context import get_current_or_device_user_id

# Bound result sizes so a malicious/buggy cloud request cannot make the
# desktop serialize an unbounded payload.
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 25


def _clamp_limit(value: Any, default: int = _DEFAULT_LIMIT) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(_MAX_LIMIT, limit))


def _error(reason: str) -> dict[str, Any]:
    return {"error": reason}


# ---------------------------------------------------------------------------
# files.* -- local music library
# ---------------------------------------------------------------------------


def _local_music_provider() -> Any | None:
    """Return an initialized :class:`LocalMusicProvider`, or ``None``.

    Mirrors how ``bootstrap/factory.py`` wires the local provider: the music
    folder comes from the ``local_music_folder`` setting (the sole source of
    truth per PATH-1). Returns ``None`` when no folder is configured.
    """
    try:
        from music.providers.local.provider import LocalMusicProvider
        from ui.settings_manager import get_settings_manager

        folder = str(get_settings_manager().get("local_music_folder", "") or "").strip()
        if not folder:
            return None
        provider = LocalMusicProvider()
        provider.initialize(folder)
        return provider
    except Exception:
        logger.exception("Failed to construct local music provider for companion request")
        return None


def _track_to_dict(track: Any) -> dict[str, Any]:
    """Project a ``TrackSummary`` into a JSON-safe dict (no embedded art)."""
    extras = getattr(track, "extras", {}) or {}
    return {
        "id": getattr(track, "id", None),
        "title": getattr(track, "title", None),
        "artist": getattr(track, "artist_name", None),
        "album": getattr(track, "album_name", None),
        "duration_ms": getattr(track, "duration_ms", None),
        "file_path": extras.get("file_path"),
        "format": extras.get("format"),
        "media_type": extras.get("media_type", "audio"),
        "library_id": extras.get("library_id"),
    }


async def handle_files_file_list(payload: dict[str, Any]) -> dict[str, Any]:
    """Search / list the user's local music library.

    Payload: ``{"query": str, "limit": int, "cursor": str}``. An empty
    query lists the library; a non-empty query fuzzy-searches it.
    """
    provider = _local_music_provider()
    if provider is None:
        return _error("No local music folder is configured on this desktop.")

    query = str(payload.get("query") or payload.get("path") or "").strip()
    limit = _clamp_limit(payload.get("limit"))
    cursor = payload.get("cursor")
    cursor = str(cursor) if cursor is not None else None

    try:
        if query:
            results = provider.search_tracks(get_current_or_device_user_id(), query, limit=limit, cursor=cursor)
            items = list(getattr(results, "items", []) or [])
            next_cursor = getattr(results, "next_cursor", None)
            total = getattr(results, "total", len(items))
        else:
            # No query -> page through the whole library.
            repo = provider._get_repo()  # canonical library repo
            all_rows = repo.get_all_tracks()
            start = 0
            if cursor is not None:
                try:
                    start = max(0, int(cursor))
                except ValueError:
                    start = 0
            from music.providers.local.provider import _track_summary_from_row

            page_rows = all_rows[start : start + limit]
            items = [_track_summary_from_row(row) for row in page_rows]
            total = len(all_rows)
            next_cursor = str(start + limit) if start + limit < total else None
    except Exception:
        logger.exception("Companion files.file_list failed")
        return _error("Could not read the local music library.")

    return {
        "tracks": [_track_to_dict(track) for track in items],
        "count": len(items),
        "total": total,
        "next_cursor": next_cursor,
        "query": query,
    }


async def handle_files_directory_browse(payload: dict[str, Any]) -> dict[str, Any]:
    """List local playlists (the library's "directories").

    Payload: ``{"limit": int, "cursor": str}``.
    """
    provider = _local_music_provider()
    if provider is None:
        return _error("No local music folder is configured on this desktop.")

    limit = _clamp_limit(payload.get("limit"))
    cursor = payload.get("cursor")
    cursor = str(cursor) if cursor is not None else None

    try:
        result = provider.list_playlists(get_current_or_device_user_id(), limit=limit, cursor=cursor)
        items = list(getattr(result, "items", []) or [])
        next_cursor = getattr(result, "next_cursor", None)
        total = getattr(result, "total", len(items))
    except Exception:
        logger.exception("Companion files.directory_browse failed")
        return _error("Could not list local playlists.")

    return {
        "playlists": [
            {
                "id": getattr(p, "id", None),
                "name": getattr(p, "name", None),
                "track_count": getattr(p, "track_count", 0),
            }
            for p in items
        ],
        "count": len(items),
        "total": total,
        "next_cursor": next_cursor,
    }


async def handle_files_file_read(payload: dict[str, Any]) -> dict[str, Any]:
    """Return metadata for one local track by its library id.

    Payload: ``{"track_id": str}`` (or ``{"library_id": ...}`` / ``id``).
    This intentionally returns *metadata*, not raw file bytes -- raw audio
    delivery would go through the protocol's binary ``file_stream`` path,
    which is left for a follow-up (see module docstring of the package).
    """
    provider = _local_music_provider()
    if provider is None:
        return _error("No local music folder is configured on this desktop.")

    raw_id = payload.get("track_id") or payload.get("library_id") or payload.get("id")
    try:
        library_id = int(str(raw_id).strip())
    except (TypeError, ValueError):
        return _error("file_read requires a numeric track_id.")

    try:
        repo = provider._get_repo()
        row = repo.get_track_by_id(library_id)
    except Exception:
        logger.exception("Companion files.file_read failed")
        return _error("Could not read track metadata.")

    if row is None:
        return _error("Track %s was not found in the local library." % library_id)

    from music.providers.local.provider import _track_summary_from_row

    return {"track": _track_to_dict(_track_summary_from_row(row))}


# ---------------------------------------------------------------------------
# smart_home.* -- LAN Home Assistant relay
# ---------------------------------------------------------------------------


def _home_assistant_client() -> Any:
    """Return the desktop's HomeAssistantClient singleton."""
    from services.smart_home.home_assistant import get_home_assistant

    return get_home_assistant()


def _entity_to_dict(entity: Any) -> dict[str, Any]:
    return {
        "entity_id": getattr(entity, "entity_id", None),
        "state": getattr(entity, "state", None),
        "domain": getattr(entity, "domain", None),
        "friendly_name": getattr(entity, "friendly_name", None),
        "attributes": getattr(entity, "attributes", {}) or {},
    }


async def handle_smart_home_ha_entity_list(payload: dict[str, Any]) -> dict[str, Any]:
    """List LAN Home Assistant entities, optionally filtered by domain.

    Payload: ``{"domain": str}``.
    """
    client = _home_assistant_client()
    if not getattr(client, "is_configured", False):
        return _error("Home Assistant is not configured on this desktop.")

    domain = str(payload.get("domain") or "").strip() or None
    try:
        entities = await client.list_entities(domain=domain)
    except Exception:
        logger.exception("Companion smart_home.ha_entity_list failed")
        return _error("Could not reach Home Assistant.")

    return {
        "entities": [_entity_to_dict(e) for e in entities],
        "count": len(entities),
        "domain": domain,
    }


async def handle_smart_home_ha_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the current state of one Home Assistant entity.

    Payload: ``{"entity_id": str}``.
    """
    client = _home_assistant_client()
    if not getattr(client, "is_configured", False):
        return _error("Home Assistant is not configured on this desktop.")

    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return _error("ha_state requires an entity_id.")

    try:
        state = await client.get_state(entity_id)
    except Exception:
        logger.exception("Companion smart_home.ha_state failed")
        return _error("Could not reach Home Assistant.")

    if state is None:
        return _error("Entity %s was not found." % entity_id)
    return {"entity": _entity_to_dict(state)}


async def handle_smart_home_ha_control(payload: dict[str, Any]) -> dict[str, Any]:
    """Control a Home Assistant entity (turn on/off, set brightness, ...).

    Payload: ``{"entity_id": str, "action": str, "params": {...}}``.
    """
    client = _home_assistant_client()
    if not getattr(client, "is_configured", False):
        return _error("Home Assistant is not configured on this desktop.")

    entity_id = str(payload.get("entity_id") or "").strip()
    action = str(payload.get("action") or "").strip()
    if not entity_id or not action:
        return _error("ha_control requires entity_id and action.")

    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}

    try:
        result = await client.call_service(entity_id, action, **params)
    except Exception:
        logger.exception("Companion smart_home.ha_control failed")
        return _error("Could not reach Home Assistant.")

    if not isinstance(result, dict):
        return _error("Home Assistant returned an unexpected response.")
    if not result.get("ok", False):
        return _error(str(result.get("error") or "Home Assistant rejected the command."))
    return {
        "ok": True,
        "entity_id": entity_id,
        "action": action,
    }


# ---------------------------------------------------------------------------
# system.* -- introspection
# ---------------------------------------------------------------------------


async def handle_system_health_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Answer a cloud-initiated health probe."""
    del payload
    return {"ok": True, "status": "online", "timestamp": time.time()}


async def handle_system_version_info(payload: dict[str, Any]) -> dict[str, Any]:
    """Report the desktop app version + platform."""
    del payload
    try:
        from services.companion_client.config import _default_platform

        platform = _default_platform()
    except Exception:
        platform = "desktop"
    return {
        "app": "viola-desktop",
        "version": VIOLA_VERSION,
        "platform": platform,
    }


async def handle_system_capabilities_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Echo back the live capability set (filled in by the client).

    The client owns the authoritative capability map (it knows which
    handlers are registered), so it injects ``capabilities`` into the
    payload before calling this. Here we just surface whatever it passed.
    """
    capabilities = payload.get("capabilities")
    return {"capabilities": capabilities if isinstance(capabilities, dict) else {}}


def build_default_dispatcher() -> Any:
    """Construct a :class:`CapabilityDispatcher` with all desktop handlers.

    This is the single place that decides what the desktop advertises. The
    advertised capability map is derived from exactly these registrations,
    so the desktop can never be asked to do something it has no handler for.
    """
    from services.companion_client.agent_handler import handle_agent_dispatch
    from services.companion_client.capabilities import CapabilityDispatcher
    from services.companion_client.multiroom_handlers import MULTIROOM_HANDLERS

    dispatcher = CapabilityDispatcher()
    dispatcher.register("files.file_list", handle_files_file_list)
    dispatcher.register("files.directory_browse", handle_files_directory_browse)
    dispatcher.register("files.file_read", handle_files_file_read)
    dispatcher.register("smart_home.ha_entity_list", handle_smart_home_ha_entity_list)
    dispatcher.register("smart_home.ha_state", handle_smart_home_ha_state)
    dispatcher.register("smart_home.ha_control", handle_smart_home_ha_control)
    dispatcher.register("system.health_check", handle_system_health_check)
    dispatcher.register("system.version_info", handle_system_version_info)
    dispatcher.register("system.capabilities_report", handle_system_capabilities_report)
    # multiroom.*: LAN room + group control. Advertising the ``multiroom``
    # scope is what tells the cloud this desktop can act as the hub for its
    # user's speakers -- per-room volume, mute, and full group CRUD cannot be
    # executed by the cloud (the room registry is Tier-3 desktop-install
    # state and the speakers are on the user's LAN), so the cloud relays
    # structured commands here. See services/companion_client/multiroom_handlers.py.
    for message_type, handler in sorted(MULTIROOM_HANDLERS.items()):
        dispatcher.register(message_type, handler)
    # agent.dispatch: the whole-turn relay. Advertising the ``agent`` scope is
    # what tells the cloud this desktop will run a cloud user's commands end
    # to end -- the cloud->desktop auto-link.
    dispatcher.register("agent.dispatch", handle_agent_dispatch)
    return dispatcher
