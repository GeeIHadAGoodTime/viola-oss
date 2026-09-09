"""``multiroom.*`` capability handlers -- the desktop hub's LAN room control.

These are the desktop half of the cloud's room/group surface. The cloud
cannot execute any of this itself: the durable room list is Tier-3
desktop-install state (``services/multiroom/room_registry.py``, whose
``resolve_room_registry_owner`` docstring records that the module "is not
reachable from ``backend/cloud_app.py`` at all"), and setting a speaker's
level means reaching a spoke on the user's LAN. So the cloud relays a
structured command here and the desktop -- the machine that owns the rooms --
performs it, exactly as ``smart_home.*`` relays to the LAN Home Assistant.

Like every other handler in this package these DELEGATE: the room registry
and the room-group manager already implement rooms and groups for the desktop
UI, so nothing here re-implements them. Each handler returns a
JSON-serializable dict, or ``{"error": ...}`` for an expected failure.

PRINCIPAL NOTE (C-668 / #4432). Every read and write below goes through
``get_room_registry`` / ``get_room_group_manager`` with the desktop's own
principal, and the registry factory -- not this module -- decides which owner
key that maps to. That is the single-source rule the C-668 fix installed:
before it, the browser-spoke writer (device principal) and the HTTP readers
(account principal) addressed two different files on disk and a user's own
speakers were invisible to their own agent. Re-deciding the principal here
would reopen exactly that split, so this module never computes an owner key.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_MAX_GROUP_ROOMS = 64


def _error(reason: str) -> dict[str, Any]:
    return {"error": reason}


def _desktop_user_id() -> str:
    """The desktop's own principal, resolved once per call.

    The registry factory maps this to the owner key that holds the paired
    rooms (see the module docstring); this function must never anticipate
    that mapping.
    """
    from core.user_context import get_current_or_device_user_id

    return get_current_or_device_user_id()


def _room_registry():
    from services.multiroom.room_registry import get_room_registry

    return get_room_registry(user_id=_desktop_user_id())


def _group_manager():
    """Return THE desktop's room-group manager, never a second instance.

    ``RoomGroupManager`` keeps an in-memory ``_groups_by_user`` cache over the
    persistent store (``services/multiroom/room_groups/manager.py:70``), so a
    second manager built over the same store would serve stale groups after
    the other one writes. ``ui.api.routes.room_groups`` owns the singleton the
    desktop UI already uses, so relayed commands and the local UI stay on one
    view of the data. The import is lazy: the cloud never loads this module.
    """
    from ui.api.routes.room_groups import get_room_group_manager

    return get_room_group_manager()


def _clamp_volume(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        volume = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, volume))


def _clamp_offset(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return None
    return max(-100, min(100, offset))


def _room_to_dict(room: Any) -> dict[str, Any]:
    return {
        "id": room.id,
        "name": room.name,
        "device_id": getattr(room, "device_id", None),
        "is_local": bool(getattr(room, "is_local", False)),
        "status": getattr(room, "status", "unknown"),
        "volume": getattr(room, "volume", 80),
        "muted": bool(getattr(room, "muted", False)),
        "last_seen": getattr(room, "last_seen", None),
    }


def _group_to_dict(group: Any, manager: Any, user_id: str) -> dict[str, Any]:
    members = []
    for member in group.members:
        try:
            effective = manager.calculate_effective_volume(group.group_id, member.room_id, user_id=user_id)
        except Exception:
            logger.exception("Failed to compute effective volume for room %s", member.room_id)
            effective = None
        members.append(
            {
                "room_id": member.room_id,
                "volume_offset": member.volume_offset,
                "is_muted": member.is_muted,
                "effective_volume": effective,
            }
        )
    return {
        "group_id": group.group_id,
        "group_name": group.group_name,
        "room_ids": list(group.room_ids),
        "master_volume": group.master_volume,
        "members": members,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


# ---------------------------------------------------------------------------
# multiroom.* -- rooms
# ---------------------------------------------------------------------------


async def handle_multiroom_room_list(payload: dict[str, Any]) -> dict[str, Any]:
    """List every room this desktop has paired, with volume + mute state."""
    del payload
    try:
        registry = _room_registry()
        rooms = registry.list_rooms()
    except Exception:
        logger.exception("Companion multiroom.room_list failed")
        return _error("Could not read the room list on this desktop.")
    return {"rooms": [_room_to_dict(room) for room in rooms], "count": len(rooms)}


async def handle_multiroom_room_set_volume(payload: dict[str, Any]) -> dict[str, Any]:
    """Set one room's output volume. Payload: ``{"room_id": str, "volume": int}``."""
    room_id = str(payload.get("room_id") or "").strip()
    volume = _clamp_volume(payload.get("volume"))
    if not room_id:
        return _error("room_set_volume requires a room_id.")
    if volume is None:
        return _error("room_set_volume requires a volume between 0 and 100.")

    try:
        registry = _room_registry()
        updated = registry.set_room_volume(room_id, volume)
    except Exception:
        logger.exception("Companion multiroom.room_set_volume failed")
        return _error("Could not set that room's volume.")

    if updated is None:
        return _error("Room %s was not found on this desktop." % room_id)
    return {"ok": True, "room": _room_to_dict(updated)}


async def handle_multiroom_room_set_mute(payload: dict[str, Any]) -> dict[str, Any]:
    """Mute/unmute one room. Payload: ``{"room_id": str, "muted": bool}``."""
    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        return _error("room_set_mute requires a room_id.")
    if "muted" not in payload:
        return _error("room_set_mute requires a muted flag.")
    muted = bool(payload.get("muted"))

    try:
        registry = _room_registry()
        updated = registry.set_room_mute(room_id, muted)
    except Exception:
        logger.exception("Companion multiroom.room_set_mute failed")
        return _error("Could not change that room's mute state.")

    if updated is None:
        return _error("Room %s was not found on this desktop." % room_id)
    return {"ok": True, "room": _room_to_dict(updated)}


# ---------------------------------------------------------------------------
# multiroom.* -- groups
# ---------------------------------------------------------------------------


async def handle_multiroom_room_group_list(payload: dict[str, Any]) -> dict[str, Any]:
    """List this desktop's room groups with their per-room trims."""
    del payload
    try:
        user_id = _desktop_user_id()
        manager = _group_manager()
        groups = manager.list_groups(user_id=user_id)
    except Exception:
        logger.exception("Companion multiroom.room_group_list failed")
        return _error("Could not read the room groups on this desktop.")
    return {
        "groups": [_group_to_dict(group, manager, user_id) for group in groups],
        "count": len(groups),
    }


async def handle_multiroom_room_group_create(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a group. Payload: ``{"group_name": str, "room_ids": [str]}``."""
    group_name = str(payload.get("group_name") or payload.get("name") or "").strip()
    if not group_name:
        return _error("room_group_create requires a group_name.")
    raw_rooms = payload.get("room_ids")
    room_ids = (
        [str(item).strip() for item in raw_rooms if isinstance(item, str) and str(item).strip()]
        if (isinstance(raw_rooms, list))
        else []
    )
    if len(room_ids) > _MAX_GROUP_ROOMS:
        return _error("A group cannot hold more than %d rooms." % _MAX_GROUP_ROOMS)

    try:
        user_id = _desktop_user_id()
        manager = _group_manager()
        group = manager.create_group(group_name, room_ids, user_id=user_id)
    except Exception:
        logger.exception("Companion multiroom.room_group_create failed")
        return _error("Could not create that room group.")
    return {"ok": True, "group": _group_to_dict(group, manager, user_id)}


async def handle_multiroom_room_group_update(payload: dict[str, Any]) -> dict[str, Any]:
    """Rename / re-member a group.

    Payload: ``{"group_id": str, "group_name": str?, "room_ids": [str]?}``.
    """
    from services.multiroom.room_groups import GroupNotFoundError

    group_id = str(payload.get("group_id") or "").strip()
    if not group_id:
        return _error("room_group_update requires a group_id.")

    updates: dict[str, Any] = {}
    group_name = payload.get("group_name")
    if isinstance(group_name, str) and group_name.strip():
        updates["group_name"] = group_name.strip()
    raw_rooms = payload.get("room_ids")
    if isinstance(raw_rooms, list):
        room_ids = [str(item).strip() for item in raw_rooms if isinstance(item, str) and str(item).strip()]
        if len(room_ids) > _MAX_GROUP_ROOMS:
            return _error("A group cannot hold more than %d rooms." % _MAX_GROUP_ROOMS)
        updates["room_ids"] = room_ids
    if not updates:
        return _error("room_group_update needs a group_name or room_ids to change.")

    try:
        user_id = _desktop_user_id()
        manager = _group_manager()
        group = manager.update_group(group_id, user_id=user_id, **updates)
    except GroupNotFoundError:
        return _error("Group %s was not found on this desktop." % group_id)
    except Exception:
        logger.exception("Companion multiroom.room_group_update failed")
        return _error("Could not update that room group.")
    return {"ok": True, "group": _group_to_dict(group, manager, user_id)}


async def handle_multiroom_room_group_delete(payload: dict[str, Any]) -> dict[str, Any]:
    """Delete a group. Payload: ``{"group_id": str}``."""
    group_id = str(payload.get("group_id") or "").strip()
    if not group_id:
        return _error("room_group_delete requires a group_id.")
    try:
        manager = _group_manager()
        deleted = manager.delete_group(group_id, user_id=_desktop_user_id())
    except Exception:
        logger.exception("Companion multiroom.room_group_delete failed")
        return _error("Could not delete that room group.")
    if not deleted:
        return _error("Group %s was not found on this desktop." % group_id)
    return {"ok": True, "group_id": group_id, "deleted": True}


async def handle_multiroom_room_group_set_master_volume(payload: dict[str, Any]) -> dict[str, Any]:
    """Set a group's master volume. Payload: ``{"group_id": str, "volume": int}``."""
    from services.multiroom.room_groups import GroupNotFoundError, InvalidVolumeError

    group_id = str(payload.get("group_id") or "").strip()
    volume = _clamp_volume(payload.get("volume"))
    if not group_id:
        return _error("room_group_set_master_volume requires a group_id.")
    if volume is None:
        return _error("room_group_set_master_volume requires a volume between 0 and 100.")

    try:
        user_id = _desktop_user_id()
        manager = _group_manager()
        manager.set_master_volume(group_id, volume, user_id=user_id)
        group = manager.get_group(group_id, user_id=user_id)
    except GroupNotFoundError:
        return _error("Group %s was not found on this desktop." % group_id)
    except InvalidVolumeError:
        return _error("That volume is out of range.")
    except Exception:
        logger.exception("Companion multiroom.room_group_set_master_volume failed")
        return _error("Could not set that group's volume.")
    if group is None:
        return _error("Group %s was not found on this desktop." % group_id)
    return {"ok": True, "group": _group_to_dict(group, manager, user_id)}


async def handle_multiroom_room_group_set_room_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Set one room's trim inside a group.

    Payload: ``{"group_id": str, "room_id": str, "volume_offset": int?,
    "is_muted": bool?}``.
    """
    from services.multiroom.room_groups import (
        GroupNotFoundError,
        InvalidVolumeError,
        RoomNotInGroupError,
    )

    group_id = str(payload.get("group_id") or "").strip()
    room_id = str(payload.get("room_id") or "").strip()
    if not group_id or not room_id:
        return _error("room_group_set_room_settings requires a group_id and a room_id.")

    offset = _clamp_offset(payload.get("volume_offset")) if "volume_offset" in payload else None
    has_mute = "is_muted" in payload
    if offset is None and not has_mute:
        return _error("room_group_set_room_settings needs a volume_offset or an is_muted flag.")

    try:
        user_id = _desktop_user_id()
        manager = _group_manager()
        if offset is not None:
            manager.set_room_volume(group_id, room_id, offset, user_id=user_id)
        if has_mute:
            manager.set_room_mute(group_id, room_id, bool(payload.get("is_muted")), user_id=user_id)
        group = manager.get_group(group_id, user_id=user_id)
    except GroupNotFoundError:
        return _error("Group %s was not found on this desktop." % group_id)
    except RoomNotInGroupError:
        return _error("Room %s is not in group %s." % (room_id, group_id))
    except InvalidVolumeError:
        return _error("That volume offset is out of range.")
    except Exception:
        logger.exception("Companion multiroom.room_group_set_room_settings failed")
        return _error("Could not update that room inside the group.")
    if group is None:
        return _error("Group %s was not found on this desktop." % group_id)
    return {"ok": True, "group": _group_to_dict(group, manager, user_id)}


MULTIROOM_HANDLERS = {
    "multiroom.room_list": handle_multiroom_room_list,
    "multiroom.room_set_volume": handle_multiroom_room_set_volume,
    "multiroom.room_set_mute": handle_multiroom_room_set_mute,
    "multiroom.room_group_list": handle_multiroom_room_group_list,
    "multiroom.room_group_create": handle_multiroom_room_group_create,
    "multiroom.room_group_update": handle_multiroom_room_group_update,
    "multiroom.room_group_delete": handle_multiroom_room_group_delete,
    "multiroom.room_group_set_master_volume": handle_multiroom_room_group_set_master_volume,
    "multiroom.room_group_set_room_settings": handle_multiroom_room_group_set_room_settings,
}


__all__ = [
    "MULTIROOM_HANDLERS",
    "handle_multiroom_room_group_create",
    "handle_multiroom_room_group_delete",
    "handle_multiroom_room_group_list",
    "handle_multiroom_room_group_set_master_volume",
    "handle_multiroom_room_group_set_room_settings",
    "handle_multiroom_room_group_update",
    "handle_multiroom_room_list",
    "handle_multiroom_room_set_mute",
    "handle_multiroom_room_set_volume",
]
