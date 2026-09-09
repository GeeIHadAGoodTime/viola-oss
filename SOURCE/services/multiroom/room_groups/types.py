"""
Room Groups Type Definitions - Phase 5 Multi-room Feature

Dataclasses for room grouping and per-room volume control.

Volume Formula:
    effective_volume = clamp(0, 100, master_volume + room_offset)

Usage:
    from services.multiroom.room_groups.types import (
        RoomGroupMember,
        RoomGroup,
        calculate_effective_volume,
    )
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RoomGroupMember:
    """
    A member room within a group.

    Attributes:
        room_id: Unique identifier for the room
        volume_offset: Volume offset from master (-100 to +100)
        is_muted: Whether this room is muted within the group
    """

    room_id: str
    volume_offset: int = 0  # -100 to +100
    is_muted: bool = False

    def __post_init__(self) -> None:
        """Validate volume_offset is within bounds."""
        if not -100 <= self.volume_offset <= 100:
            # Use object.__setattr__ because dataclass is frozen
            object.__setattr__(
                self,
                "volume_offset",
                max(-100, min(100, self.volume_offset)),
            )


@dataclass(frozen=True, slots=True)
class RoomGroup:
    """
    A group of rooms with synchronized playback and individual volume control.

    Attributes:
        group_id: Unique identifier for this group (UUID)
        group_name: Human-readable display name
        room_ids: Tuple of room IDs in this group (immutable)
        master_volume: Master volume level (0-100)
        members: Tuple of RoomGroupMember with per-room settings
        created_at: Unix timestamp when group was created
        updated_at: Unix timestamp of last update
    """

    group_id: str
    group_name: str
    room_ids: tuple[str, ...] = field(default_factory=tuple)
    master_volume: int = 80  # 0-100
    members: tuple[RoomGroupMember, ...] = field(default_factory=tuple)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Validate master_volume is within bounds."""
        if not 0 <= self.master_volume <= 100:
            object.__setattr__(
                self,
                "master_volume",
                max(0, min(100, self.master_volume)),
            )


def calculate_effective_volume(master_volume: int, room_offset: int) -> int:
    """
    Calculate effective volume for a room.

    Formula: effective_volume = clamp(0, 100, master_volume + room_offset)

    Args:
        master_volume: Group master volume (0-100)
        room_offset: Room-specific offset (-100 to +100)

    Returns:
        Effective volume clamped to 0-100
    """
    return max(0, min(100, master_volume + room_offset))


__all__ = [
    "RoomGroup",
    "RoomGroupMember",
    "calculate_effective_volume",
]
