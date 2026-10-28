"""
Room Discovery & CRUD API Routes.

FastAPI router for listing, creating, and deleting rooms/devices in the network.

Endpoints:
    GET    /api/v1/rooms          - List all discovered rooms (including local device)
    POST   /api/v1/rooms          - Create a new room
    PATCH  /api/v1/rooms/{room_id} - Rename a room
    PUT    /api/v1/rooms/{room_id}/volume - Set room volume
    PUT    /api/v1/rooms/{room_id}/mute - Set room mute state
    DELETE /api/v1/rooms/{room_id} - Delete a room
"""

from __future__ import annotations

import platform
import uuid
from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, Request
from ui.api.routes.auth_dependencies import (
    get_current_user_id,
    require_auth,
    require_operator_auth,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/rooms", tags=["rooms"])

# Module-level cache for local device info (generated once per process)
_local_device_id: str | None = None
_local_device_name: str | None = None


def _get_local_device_id() -> str:
    """Get or generate a stable local device ID."""
    global _local_device_id
    if _local_device_id is None:
        # Try to get from settings first
        try:
            from config.settings import settings

            if hasattr(settings, "device_id") and settings.device_id:
                _local_device_id = str(settings.device_id)
            else:
                # Generate a new one based on hostname for stability
                hostname = platform.node()
                _local_device_id = f"local-{hostname}-{uuid.uuid4().hex[:8]}"
        except Exception as exc:
            logger.debug("Failed to get device_id from settings: %s", exc)
            _local_device_id = f"local-{uuid.uuid4().hex[:12]}"
    return _local_device_id


def _get_local_device_name() -> str:
    """Get or generate a human-readable local device name."""
    global _local_device_name
    if _local_device_name is None:
        try:
            hostname = platform.node()
            _local_device_name = f"Viola ({hostname})"
        except Exception as exc:
            logger.debug("Failed to get hostname: %s", exc)
            _local_device_name = "Viola (Local)"
    return _local_device_name


# Pydantic Models for Request/Response


class RoomResponse(BaseModel):
    """Response model for a single room/device."""

    id: str = Field(..., description="Unique identifier for the room")
    name: str = Field(..., description="Human-readable room name")
    room_name: str | None = Field(None, description="Legacy display-name alias for browser-spoke controls")
    device_id: str = Field(..., description="Device identifier")
    is_local: bool = Field(..., description="Whether this is the local device")
    is_hub: bool = Field(False, description="Whether this is the hub-local room")
    status: Literal["online", "offline", "unknown"] = Field(..., description="Connection status of the room")
    host: str | None = Field(None, description="IP address or hostname (None for local)")
    port: int | None = Field(None, description="API port (None for local)")
    capabilities: list[str] = Field(default_factory=list, description="List of supported capabilities")
    last_sync: float | None = Field(None, description="Unix timestamp of last successful state sync")
    volume: int = Field(80, ge=0, le=100, description="Room output volume (0-100)")
    muted: bool = Field(False, description="Whether the room output is muted")


class RoomListData(BaseModel):
    """Data payload for list rooms response."""

    rooms: list[RoomResponse]
    data: list[RoomResponse] | None = Field(None, description="Legacy array alias for older speaker controls")
    count: int


class ErrorData(BaseModel):
    """Error detail structure."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class RoomListSuccessResponse(BaseModel):
    """Success response for listing rooms."""

    ok: Literal[True] = True
    error: None = None
    data: RoomListData


class RoomListErrorResponse(BaseModel):
    """Error response for room endpoints."""

    ok: Literal[False] = False
    error: ErrorData
    data: None = None


def _get_local_room(user_id: str) -> RoomResponse | None:
    """Get the local device room from the RoomRegistry (source of truth).

    Falls back to on-the-fly synthesis only if the registry has no local room
    (e.g. multiroom not enabled).
    """
    try:
        from services.multiroom.room_registry import get_room_registry

        registry = get_room_registry(user_id=user_id)
        local = registry.get_local_room()
        if local is not None:
            return RoomResponse(
                id=local.id,
                name=local.name,
                room_name=local.name,
                device_id=_get_local_device_id(),
                is_local=True,
                is_hub=True,
                status="online",
                host=local.ip_address,
                port=None,
                capabilities=["playback", "wake_word", "voice_control"],
                volume=local.volume,
                muted=local.muted,
            )
    except Exception as exc:
        logger.debug("Failed to get local room from registry: %s", exc)

    # Fallback: synthesize (keeps single-room mode working)
    device_id = _get_local_device_id()
    return RoomResponse(
        id="local",
        name=_get_local_device_name(),
        room_name=_get_local_device_name(),
        device_id=device_id,
        is_local=True,
        is_hub=True,
        status="online",
        host=None,
        port=None,
        capabilities=["playback", "wake_word", "voice_control"],
    )


def _get_discovered_rooms() -> list[RoomResponse]:
    """Get rooms from the production DiscoveryService singleton."""
    rooms: list[RoomResponse] = []

    try:
        from services.multiroom.discovery import get_discovery_service

        discovery = get_discovery_service()
        if not discovery.is_running():
            logger.debug("DiscoveryService not running, no discovered rooms")
            return rooms

        discovered = discovery.get_discovered_devices()

        for device in discovered:
            rooms.append(
                RoomResponse(
                    id=f"room-{device.device_id}",
                    name=device.room_name,
                    room_name=device.room_name,
                    device_id=device.device_id,
                    is_local=False,
                    status="online",
                    host=device.host,
                    port=device.port,
                    capabilities=device.capabilities or ["playback"],
                )
            )
    except ImportError:
        logger.debug("DiscoveryService not available")
    except Exception as exc:
        logger.debug("Failed to get discovered devices: %s", exc)

    return rooms


def _get_registered_multiroom_rooms(user_id: str) -> list[RoomResponse]:
    """Get durable rooms registered by in-house spoke pairing."""
    rooms: list[RoomResponse] = []
    try:
        from services.multiroom.room_registry import get_room_registry

        registry = get_room_registry(user_id=user_id)
        for room in registry.get_remote_rooms():
            rooms.append(
                RoomResponse(
                    id=room.id,
                    name=room.name,
                    room_name=room.name,
                    device_id=room.device_id,
                    is_local=False,
                    status=room.status,
                    host=room.ip_address,
                    port=None,
                    capabilities=["playback", "speaker"],
                    last_sync=room.last_seen,
                    volume=room.volume,
                    muted=room.muted,
                )
            )
    except Exception as exc:
        logger.debug("Failed to get registered multi-room rooms: %s", exc)

    return rooms


@router.get(
    "",
    dependencies=[Depends(require_auth)],
    response_model=RoomListSuccessResponse | RoomListErrorResponse,
    responses={
        200: {
            "model": RoomListSuccessResponse,
            "description": "List of available rooms",
        },
        500: {"model": RoomListErrorResponse, "description": "Internal error"},
    },
)
async def list_rooms(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> RoomListSuccessResponse | RoomListErrorResponse:
    """
    List all available rooms/devices.

    Returns the local device as a room plus any discovered network devices.
    The local device is always included to ensure at least one room is available.
    """
    try:
        rooms: list[RoomResponse] = []

        # Always include local device
        local_room = _get_local_room(user_id)
        rooms.append(local_room)

        # Add this user's registered rooms only. Raw LAN discovery is global
        # process state and must not be exposed as another user's room list.
        registered_rooms = _get_registered_multiroom_rooms(user_id)
        discovered_rooms: list[RoomResponse] = []
        seen_room_ids = {room.id for room in rooms if room is not None}
        for room in registered_rooms + discovered_rooms:
            if room.id in seen_room_ids:
                continue
            rooms.append(room)
            seen_room_ids.add(room.id)

        # Enrich rooms with last_sync timestamps from StateHub.
        # StateHub stores rooms by device_id (from connect endpoint), while
        # the rooms API may prefix with "room-". Check both keys.
        try:
            from core.state_hub import get_state_hub

            hub = get_state_hub(user_id=user_id)
            app_state = hub.get_state()
            for room in rooms:
                room_state = app_state.rooms.get(room.id)
                if room_state is None and room.device_id:
                    room_state = app_state.rooms.get(room.device_id)
                if room_state is not None and room_state.last_sync > 0:
                    room.last_sync = room_state.last_sync
        except Exception as exc:
            logger.debug("Could not enrich rooms with sync data: %s", exc)

        logger.debug(
            "Found %d rooms (1 local, %d registered, %d discovered)",
            len(rooms),
            len(registered_rooms),
            len(discovered_rooms),
        )

        room_payloads = [room.model_dump() for room in rooms]
        return success_response(
            {
                "rooms": room_payloads,
                "data": room_payloads,
                "count": len(rooms),
            }
        )
    except Exception:
        logger.exception("Failed to list rooms")
        return JSONResponse(
            status_code=500,
            content=failure_response("list_rooms_failed", "Couldn't load your rooms. Please try again."),
        )


class CreateRoomRequest(BaseModel):
    """Request body for creating a room."""

    name: str = Field(..., min_length=1, max_length=100, description="Human-readable room name")
    config: dict[str, Any] | None = Field(default_factory=dict, description="Optional room configuration")


class RenameRoomRequest(BaseModel):
    """Request body for renaming a room."""

    name: str = Field(..., min_length=1, max_length=100, description="New display name for the room")


class RoomVolumeRequest(BaseModel):
    """Request body for setting a room volume."""

    volume: int = Field(..., ge=0, le=100, description="Room volume (0-100)")


class RoomMuteRequest(BaseModel):
    """Request body for setting a room mute state."""

    muted: bool = Field(..., description="Whether the room should be muted")


def _get_active_spoke_match(room_id: str) -> tuple[Any | None, int | None, str | None]:
    """Resolve a durable room ID or legacy numeric spoke ID to an active spoke."""
    try:
        from ui.api.routes.audio_stream import get_active_audio_stream_manager

        manager = get_active_audio_stream_manager()
    except Exception as exc:
        logger.debug("Could not inspect active audio-stream spokes: %s", exc)
        return None, None, None

    if manager is None:
        return None, None, None

    try:
        snapshot = manager.get_spoke_info_snapshot()
    except Exception as exc:
        logger.debug("Could not read active spoke snapshot: %s", exc)
        return manager, None, None

    for info in snapshot:
        if not isinstance(info, dict):
            continue
        registry_room_id = info.get("registry_room_id")
        spoke_id = info.get("id")
        if str(registry_room_id or "") != room_id and str(spoke_id or "") != room_id:
            continue
        try:
            numeric_spoke_id = int(spoke_id)
        except (TypeError, ValueError):
            numeric_spoke_id = None
        durable_room_id = registry_room_id if isinstance(registry_room_id, str) and registry_room_id else None
        return manager, numeric_spoke_id, durable_room_id

    return manager, None, None


async def _mirror_live_spoke_volume(room_id: str, volume: int) -> tuple[str | None, bool | None]:
    """Best-effort mirror of a room volume update to an active browser spoke."""
    manager, spoke_id, durable_room_id = _get_active_spoke_match(room_id)
    if manager is None or spoke_id is None:
        return durable_room_id, None
    try:
        return durable_room_id, bool(await manager.set_spoke_volume(spoke_id, volume))
    except Exception:
        logger.exception("Failed to mirror volume to live spoke room %s", room_id)
        return durable_room_id, False


async def _mirror_live_spoke_mute(room_id: str, muted: bool) -> tuple[str | None, bool | None]:
    """Best-effort mirror of a room mute update to an active browser spoke."""
    manager, spoke_id, durable_room_id = _get_active_spoke_match(room_id)
    if manager is None or spoke_id is None:
        return durable_room_id, None
    try:
        return durable_room_id, bool(await manager.set_spoke_mute(spoke_id, muted))
    except Exception:
        logger.exception("Failed to mirror mute to live spoke room %s", room_id)
        return durable_room_id, False


@router.patch(
    "/{room_id}",
    dependencies=[Depends(require_operator_auth)],
)
async def rename_room(
    room_id: str,
    body: RenameRoomRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> JSONResponse:
    """
    Rename an existing room.

    Updates the display name of a room via StateHub's RenameRoom command.
    """
    try:
        from services.multiroom.room_registry import get_room_registry

        registry = get_room_registry(user_id=user_id)
        room = registry.get_room(room_id)
        if room is None:
            return JSONResponse(
                status_code=404,
                content=failure_response("room_not_found", f"Room '{room_id}' not found"),
            )

        renamed = registry.rename_room(room_id, body.name)
        if not renamed:
            return JSONResponse(
                status_code=400,
                content=failure_response("rename_failed", "Failed to rename room"),
            )

        return success_response({"room_id": room_id, "name": body.name})
    except Exception:
        logger.exception("Failed to rename room")
        return JSONResponse(
            status_code=500,
            content=failure_response("rename_room_failed", "Couldn't rename the room. Please try again."),
        )


@router.put(
    "/{room_id}/volume",
    dependencies=[Depends(require_auth)],
)
async def set_room_volume(
    room_id: str,
    body: RoomVolumeRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> JSONResponse:
    """Set per-room volume through the durable registry and live spoke path."""
    try:
        from services.multiroom.room_registry import get_room_registry

        live_room_id, live_control = await _mirror_live_spoke_volume(room_id, body.volume)
        registry_room_id = live_room_id or room_id
        registry = get_room_registry(user_id=user_id)
        updated = registry.set_room_volume(registry_room_id, body.volume)
        if updated is None and live_control is not True:
            return JSONResponse(
                status_code=404,
                content=failure_response("room_not_found", f"Room '{room_id}' not found"),
            )

        return success_response(
            {
                "room_id": registry_room_id,
                "volume": updated.volume if updated is not None else body.volume,
                "muted": updated.muted if updated is not None else None,
                "registry_persisted": updated is not None,
                "live_control": live_control is True,
            }
        )
    except Exception:
        logger.exception("Failed to set room volume")
        return JSONResponse(
            status_code=500,
            content=failure_response("set_room_volume_failed", "Couldn't adjust the room volume. Please try again."),
        )


@router.put(
    "/{room_id}/mute",
    dependencies=[Depends(require_auth)],
)
async def set_room_mute(
    room_id: str,
    body: RoomMuteRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> JSONResponse:
    """Set per-room mute through the durable registry and live spoke path."""
    try:
        from services.multiroom.room_registry import get_room_registry

        live_room_id, live_control = await _mirror_live_spoke_mute(room_id, body.muted)
        registry_room_id = live_room_id or room_id
        registry = get_room_registry(user_id=user_id)
        updated = registry.set_room_mute(registry_room_id, body.muted)
        if updated is None and live_control is not True:
            return JSONResponse(
                status_code=404,
                content=failure_response("room_not_found", f"Room '{room_id}' not found"),
            )

        return success_response(
            {
                "room_id": registry_room_id,
                "volume": updated.volume if updated is not None else None,
                "muted": updated.muted if updated is not None else body.muted,
                "registry_persisted": updated is not None,
                "live_control": live_control is True,
            }
        )
    except Exception:
        logger.exception("Failed to set room mute")
        return JSONResponse(
            status_code=500,
            content=failure_response("set_room_mute_failed", "Couldn't mute the room. Please try again."),
        )


@router.post(
    "",
    dependencies=[Depends(require_operator_auth)],
)
async def create_room(
    body: CreateRoomRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> JSONResponse:
    """
    Create a new room.

    Registers a remote room via RoomRegistry with an auto-generated ID.
    """
    try:
        from services.multiroom.room_registry import get_room_registry

        room_id = f"room-{uuid.uuid4().hex[:12]}"
        registry = get_room_registry(user_id=user_id)
        registry.register_remote_room(room_id, body.name)

        room_obj = {
            "id": room_id,
            "name": body.name,
            "is_local": False,
            "status": "online",
            "config": body.config or {},
        }
        logger.info("Room created: id=%s name=%s", room_id, body.name)
        return success_response({"room": room_obj})
    except Exception:
        logger.exception("Failed to create room")
        return JSONResponse(
            status_code=500,
            content=failure_response("create_room_failed", "Couldn't create the room. Please try again."),
        )


@router.delete(
    "/{room_id}",
    dependencies=[Depends(require_operator_auth)],
)
async def delete_room(
    room_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> JSONResponse:
    """
    Delete an existing room.

    Unregisters a non-local room via RoomRegistry. Cannot delete the local room.
    """
    try:
        from services.multiroom.room_registry import get_room_registry

        registry = get_room_registry(user_id=user_id)
        room = registry.get_room(room_id)
        if room is None:
            return JSONResponse(
                status_code=404,
                content=failure_response("room_not_found", "Room not found"),
            )

        removed = registry.unregister_room(room_id)
        if not removed:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "delete_failed",
                    "Cannot delete this room (it may be the local/primary room)",
                ),
            )

        # Removing a room must actually unpair the speaker that served it,
        # otherwise its credential stays valid for months after the user
        # thought they had removed the device (#4434). Revocation is
        # per-device, so the rest of the house stays paired.
        revoked_devices: list[str] = []
        try:
            import asyncio

            from ui.security.spoke_device_registry import device_ids_for_room, revoke_device

            def _revoke_room_devices() -> list[str]:
                # Worker-thread hop: the registry resolves the secret
                # directory, which can harden it (icacls subprocess) on first
                # use — never on the event loop.
                return [device_id for device_id in device_ids_for_room(room_id) if revoke_device(device_id)]

            revoked_devices = await asyncio.to_thread(_revoke_room_devices)
        except Exception:
            logger.exception("Failed to revoke spoke credentials for deleted room %s", room_id)

        logger.info("Room deleted: id=%s (revoked %d paired device(s))", room_id, len(revoked_devices))
        return success_response({"deleted": room_id, "revoked_devices": len(revoked_devices)})
    except Exception:
        logger.exception("Failed to delete room")
        return JSONResponse(
            status_code=500,
            content=failure_response("delete_room_failed", "Couldn't delete the room. Please try again."),
        )


def create_rooms_router() -> APIRouter:
    """Factory function for the rooms router."""
    return router


__all__ = [
    "CreateRoomRequest",
    "ErrorData",
    "RenameRoomRequest",
    "RoomListData",
    "RoomListErrorResponse",
    "RoomListSuccessResponse",
    "RoomMuteRequest",
    "RoomResponse",
    "RoomVolumeRequest",
    "create_rooms_router",
    "router",
]
