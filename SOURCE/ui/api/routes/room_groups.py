"""
Room Groups API Routes - Phase 5 Multi-room Feature

FastAPI router for room group management and per-room volume control.

Endpoints:
    GET    /v1/rooms/groups                           - List all groups
    POST   /v1/rooms/groups                           - Create group
    GET    /v1/rooms/groups/{group_id}                - Get group
    PUT    /v1/rooms/groups/{group_id}                - Update group
    DELETE /v1/rooms/groups/{group_id}                - Delete group
    POST   /v1/rooms/groups/{group_id}/volume         - Set master volume
    POST   /v1/rooms/groups/{group_id}/rooms/{room_id}/volume - Per-room volume
    POST   /v1/rooms/groups/{group_id}/rooms/{room_id}/mute   - Mute room
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Body, Depends, Path
from services.multiroom.room_groups import (
    GroupNotFoundError,
    InvalidVolumeError,
    RoomGroupManager,
    RoomNotInGroupError,
)
from services.persistence.state_store import get_state_store
from ui.api.routes.auth_dependencies import (
    get_current_user_id,
    require_auth,
    require_operator_auth,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/rooms/groups", tags=["room_groups"])

# Singleton manager instance
_manager: RoomGroupManager | None = None


def get_room_group_manager() -> RoomGroupManager:
    """Get or create the RoomGroupManager singleton."""
    global _manager
    if _manager is None:
        _manager = RoomGroupManager(get_state_store())
    return _manager


# Pydantic models for request/response


class CreateGroupRequest(BaseModel):
    """Request body for creating a room group."""

    name: str = Field(..., min_length=1, max_length=100, description="Display name for the group")
    room_ids: list[str] = Field(..., min_length=1, description="List of room IDs to include")


class UpdateGroupRequest(BaseModel):
    """Request body for updating a room group."""

    group_name: str | None = Field(None, min_length=1, max_length=100, description="New display name")
    room_ids: list[str] | None = Field(None, description="New list of room IDs")
    master_volume: int | None = Field(None, ge=0, le=100, description="Master volume (0-100)")


class SetVolumeRequest(BaseModel):
    """Request body for setting volume."""

    volume: int = Field(..., ge=0, le=100, description="Volume level (0-100)")


class SetRoomVolumeRequest(BaseModel):
    """Request body for setting room volume offset."""

    offset: int = Field(..., ge=-100, le=100, description="Volume offset (-100 to +100)")


class SetRoomMuteRequest(BaseModel):
    """Request body for setting room mute state."""

    muted: bool = Field(..., description="Whether the room should be muted")


class RoomGroupMemberResponse(BaseModel):
    """Response model for a room group member."""

    room_id: str
    volume_offset: int
    is_muted: bool
    effective_volume: int


class RoomGroupResponse(BaseModel):
    """Response model for a room group."""

    group_id: str
    group_name: str
    room_ids: list[str]
    master_volume: int
    members: list[RoomGroupMemberResponse]
    created_at: float
    updated_at: float


# Response Envelope Models


class ErrorData(BaseModel):
    """Error detail structure."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class GroupListData(BaseModel):
    """Data payload for list groups response."""

    groups: list[RoomGroupResponse]
    count: int


class SingleGroupData(BaseModel):
    """Data payload for single group response."""

    group: RoomGroupResponse


class DeleteGroupData(BaseModel):
    """Data payload for delete group response."""

    deleted: bool
    group_id: str


class MasterVolumeData(BaseModel):
    """Data payload for master volume response."""

    group: RoomGroupResponse | None
    master_volume: int


class RoomVolumeData(BaseModel):
    """Data payload for room volume response."""

    room_id: str
    offset: int
    effective_volume: int


class RoomMuteData(BaseModel):
    """Data payload for room mute response."""

    room_id: str
    is_muted: bool
    effective_volume: int


# Success Response Envelopes


class GroupListSuccessResponse(BaseModel):
    """Success response for listing groups."""

    ok: Literal[True] = True
    error: None = None
    data: GroupListData


class SingleGroupSuccessResponse(BaseModel):
    """Success response for single group operations."""

    ok: Literal[True] = True
    error: None = None
    data: SingleGroupData


class DeleteGroupSuccessResponse(BaseModel):
    """Success response for delete group."""

    ok: Literal[True] = True
    error: None = None
    data: DeleteGroupData


class MasterVolumeSuccessResponse(BaseModel):
    """Success response for master volume."""

    ok: Literal[True] = True
    error: None = None
    data: MasterVolumeData


class RoomVolumeSuccessResponse(BaseModel):
    """Success response for room volume."""

    ok: Literal[True] = True
    error: None = None
    data: RoomVolumeData


class RoomMuteSuccessResponse(BaseModel):
    """Success response for room mute."""

    ok: Literal[True] = True
    error: None = None
    data: RoomMuteData


class RoomGroupErrorResponse(BaseModel):
    """Error response for room group endpoints."""

    ok: Literal[False] = False
    error: ErrorData
    data: None = None


def _group_to_response(group: Any, manager: RoomGroupManager, *, user_id: str) -> dict[str, Any]:
    """Convert a RoomGroup to response dict with effective volumes."""
    members = []
    for member in group.members:
        effective_vol = manager.calculate_effective_volume(group.group_id, member.room_id, user_id=user_id)
        members.append(
            {
                "room_id": member.room_id,
                "volume_offset": member.volume_offset,
                "is_muted": member.is_muted,
                "effective_volume": effective_vol,
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


@router.get(
    "",
    dependencies=[Depends(require_auth)],
    response_model=GroupListSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {"model": GroupListSuccessResponse, "description": "List of room groups"},
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def list_groups(
    user_id: str = Depends(get_current_user_id),
) -> GroupListSuccessResponse | RoomGroupErrorResponse:
    """List all room groups."""
    try:
        manager = get_room_group_manager()
        groups = manager.list_groups(user_id=user_id)
        return success_response(
            {
                "groups": [_group_to_response(g, manager, user_id=user_id) for g in groups],
                "count": len(groups),
            }
        )
    except Exception:
        logger.exception("Failed to list room groups")
        return JSONResponse(
            status_code=500,
            content=failure_response("list_failed", "Couldn't load your room groups. Please try again."),
        )


@router.post(
    "",
    dependencies=[Depends(require_operator_auth)],
    response_model=SingleGroupSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": SingleGroupSuccessResponse,
            "description": "Group created successfully",
        },
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def create_group(
    body: CreateGroupRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> SingleGroupSuccessResponse | RoomGroupErrorResponse:
    """Create a new room group."""
    try:
        manager = get_room_group_manager()
        group = manager.create_group(body.name, body.room_ids, user_id=user_id)
        return success_response(
            {
                "group": _group_to_response(group, manager, user_id=user_id),
            }
        )
    except Exception:
        logger.exception("Failed to create room group")
        return JSONResponse(
            status_code=500,
            content=failure_response("create_failed", "Couldn't create the room group. Please try again."),
        )


@router.get(
    "/{group_id}",
    dependencies=[Depends(require_auth)],
    response_model=SingleGroupSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {"model": SingleGroupSuccessResponse, "description": "Group details"},
        404: {"model": RoomGroupErrorResponse, "description": "Group not found"},
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def get_group(
    group_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
) -> SingleGroupSuccessResponse | RoomGroupErrorResponse:
    """Get a room group by ID."""
    try:
        manager = get_room_group_manager()
        group = manager.get_group(group_id, user_id=user_id)
        if group is None:
            return JSONResponse(
                status_code=404,
                content=failure_response("not_found", f"Group not found: {group_id}"),
            )
        return success_response(
            {
                "group": _group_to_response(group, manager, user_id=user_id),
            }
        )
    except Exception:
        logger.exception("Failed to get room group")
        return JSONResponse(
            status_code=500,
            content=failure_response("get_failed", "Couldn't load the room group. Please try again."),
        )


@router.put(
    "/{group_id}",
    dependencies=[Depends(require_operator_auth)],
    response_model=SingleGroupSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": SingleGroupSuccessResponse,
            "description": "Group updated successfully",
        },
        400: {
            "model": RoomGroupErrorResponse,
            "description": "Invalid request or volume",
        },
        404: {"model": RoomGroupErrorResponse, "description": "Group not found"},
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def update_group(
    group_id: str = Path(...),
    body: UpdateGroupRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> SingleGroupSuccessResponse | RoomGroupErrorResponse:
    """Update a room group."""
    try:
        manager = get_room_group_manager()
        kwargs = {}
        if body.group_name is not None:
            kwargs["group_name"] = body.group_name
        if body.room_ids is not None:
            kwargs["room_ids"] = body.room_ids
        if body.master_volume is not None:
            kwargs["master_volume"] = body.master_volume

        if not kwargs:
            return JSONResponse(
                status_code=400,
                content=failure_response("no_updates", "No valid fields to update"),
            )

        group = manager.update_group(group_id, user_id=user_id, **kwargs)
        return success_response(
            {
                "group": _group_to_response(group, manager, user_id=user_id),
            }
        )
    except GroupNotFoundError as exc:
        logger.warning("Room group not found during update: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("not_found", "Group not found"),
        )
    except InvalidVolumeError as exc:
        logger.warning("Invalid volume in room group update: %s", exc)
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_volume", "Invalid volume value"),
        )
    except Exception:
        logger.exception("Failed to update room group")
        return JSONResponse(
            status_code=500,
            content=failure_response("update_failed", "Couldn't update the room group. Please try again."),
        )


@router.delete(
    "/{group_id}",
    dependencies=[Depends(require_operator_auth)],
    response_model=DeleteGroupSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": DeleteGroupSuccessResponse,
            "description": "Group deleted successfully",
        },
        404: {"model": RoomGroupErrorResponse, "description": "Group not found"},
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def delete_group(
    group_id: str = Path(...),
    user_id: str = Depends(get_current_user_id),
) -> DeleteGroupSuccessResponse | RoomGroupErrorResponse:
    """Delete a room group."""
    try:
        manager = get_room_group_manager()
        deleted = manager.delete_group(group_id, user_id=user_id)
        if not deleted:
            return JSONResponse(
                status_code=404,
                content=failure_response("not_found", f"Group not found: {group_id}"),
            )
        return success_response({"deleted": True, "group_id": group_id})
    except Exception:
        logger.exception("Failed to delete room group")
        return JSONResponse(
            status_code=500,
            content=failure_response("delete_failed", "Couldn't delete the room group. Please try again."),
        )


@router.post(
    "/{group_id}/volume",
    dependencies=[Depends(require_operator_auth)],
    response_model=MasterVolumeSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": MasterVolumeSuccessResponse,
            "description": "Master volume set successfully",
        },
        400: {"model": RoomGroupErrorResponse, "description": "Invalid volume"},
        404: {"model": RoomGroupErrorResponse, "description": "Group not found"},
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def set_master_volume(
    group_id: str = Path(...),
    body: SetVolumeRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> MasterVolumeSuccessResponse | RoomGroupErrorResponse:
    """Set the master volume for a room group."""
    try:
        manager = get_room_group_manager()
        manager.set_master_volume(group_id, body.volume, user_id=user_id)
        group = manager.get_group(group_id, user_id=user_id)
        return success_response(
            {
                "group": (_group_to_response(group, manager, user_id=user_id) if group else None),
                "master_volume": body.volume,
            }
        )
    except GroupNotFoundError as exc:
        logger.warning("Room group not found for master volume: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("not_found", "Group not found"),
        )
    except InvalidVolumeError as exc:
        logger.warning("Invalid master volume value: %s", exc)
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_volume", "Invalid volume value"),
        )
    except Exception:
        logger.exception("Failed to set master volume")
        return JSONResponse(
            status_code=500,
            content=failure_response("volume_failed", "Couldn't adjust the volume. Please try again."),
        )


@router.post(
    "/{group_id}/rooms/{room_id}/volume",
    dependencies=[Depends(require_operator_auth)],
    response_model=RoomVolumeSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": RoomVolumeSuccessResponse,
            "description": "Room volume set successfully",
        },
        400: {"model": RoomGroupErrorResponse, "description": "Invalid volume"},
        404: {
            "model": RoomGroupErrorResponse,
            "description": "Group or room not found",
        },
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def set_room_volume(
    group_id: str = Path(...),
    room_id: str = Path(...),
    body: SetRoomVolumeRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> RoomVolumeSuccessResponse | RoomGroupErrorResponse:
    """Set the volume offset for a room within a group."""
    try:
        manager = get_room_group_manager()
        manager.set_room_volume(group_id, room_id, body.offset, user_id=user_id)
        effective = manager.calculate_effective_volume(group_id, room_id, user_id=user_id)
        return success_response(
            {
                "room_id": room_id,
                "offset": body.offset,
                "effective_volume": effective,
            }
        )
    except GroupNotFoundError as exc:
        logger.warning("Room group not found for room volume: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("group_not_found", "Group not found"),
        )
    except RoomNotInGroupError as exc:
        logger.warning("Room not in group for volume set: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("room_not_in_group", "Room not found in group"),
        )
    except InvalidVolumeError as exc:
        logger.warning("Invalid room volume value: %s", exc)
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_volume", "Invalid volume value"),
        )
    except Exception:
        logger.exception("Failed to set room volume")
        return JSONResponse(
            status_code=500,
            content=failure_response("volume_failed", "Couldn't adjust the volume. Please try again."),
        )


@router.post(
    "/{group_id}/rooms/{room_id}/mute",
    dependencies=[Depends(require_operator_auth)],
    response_model=RoomMuteSuccessResponse | RoomGroupErrorResponse,
    responses={
        200: {
            "model": RoomMuteSuccessResponse,
            "description": "Room mute state set successfully",
        },
        404: {
            "model": RoomGroupErrorResponse,
            "description": "Group or room not found",
        },
        500: {"model": RoomGroupErrorResponse, "description": "Internal error"},
    },
)
async def set_room_mute(
    group_id: str = Path(...),
    room_id: str = Path(...),
    body: SetRoomMuteRequest = Body(...),
    user_id: str = Depends(get_current_user_id),
) -> RoomMuteSuccessResponse | RoomGroupErrorResponse:
    """Set the mute state for a room within a group."""
    try:
        manager = get_room_group_manager()
        manager.set_room_mute(group_id, room_id, body.muted, user_id=user_id)
        effective = manager.calculate_effective_volume(group_id, room_id, user_id=user_id)
        return success_response(
            {
                "room_id": room_id,
                "is_muted": body.muted,
                "effective_volume": effective,
            }
        )
    except GroupNotFoundError as exc:
        logger.warning("Room group not found for mute: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("group_not_found", "Group not found"),
        )
    except RoomNotInGroupError as exc:
        logger.warning("Room not in group for mute: %s", exc)
        return JSONResponse(
            status_code=404,
            content=failure_response("room_not_in_group", "Room not found in group"),
        )
    except Exception:
        logger.exception("Failed to set room mute")
        return JSONResponse(
            status_code=500,
            content=failure_response("mute_failed", "Couldn't change the mute state. Please try again."),
        )


def create_room_groups_router() -> APIRouter:
    """Factory function for the room groups router."""
    return router


__all__ = [
    "CreateGroupRequest",
    "DeleteGroupData",
    "DeleteGroupSuccessResponse",
    "ErrorData",
    "GroupListData",
    "GroupListSuccessResponse",
    "MasterVolumeData",
    "MasterVolumeSuccessResponse",
    "RoomGroupErrorResponse",
    "RoomGroupMemberResponse",
    "RoomGroupResponse",
    "RoomMuteData",
    "RoomMuteSuccessResponse",
    "RoomVolumeData",
    "RoomVolumeSuccessResponse",
    "SetRoomMuteRequest",
    "SetRoomVolumeRequest",
    "SetVolumeRequest",
    "SingleGroupData",
    "SingleGroupSuccessResponse",
    "UpdateGroupRequest",
    "create_room_groups_router",
    "router",
]
