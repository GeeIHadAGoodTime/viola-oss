"""
Room Groups Module - Phase 5 Multi-room Feature

Provides room grouping and per-room volume control for multi-room audio.

Usage:
    from services.multiroom.room_groups import (
        RoomGroup,
        RoomGroupMember,
        RoomGroupManager,
        calculate_effective_volume,
    )
"""

from .manager import (
    GroupNotFoundError,
    InvalidVolumeError,
    MissingRoomGroupUserError,
    RoomGroupError,
    RoomGroupManager,
    RoomNotInGroupError,
)
from .types import RoomGroup, RoomGroupMember, calculate_effective_volume

__all__ = [
    "GroupNotFoundError",
    "InvalidVolumeError",
    "MissingRoomGroupUserError",
    "RoomGroup",
    "RoomGroupError",
    "RoomGroupManager",
    "RoomGroupMember",
    "RoomNotInGroupError",
    "calculate_effective_volume",
]
