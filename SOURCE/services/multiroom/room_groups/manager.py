"""
Room Group Manager - Phase 5 Multi-room Feature

Manages room groups with per-room volume control and persistence.

Usage:
    from services.multiroom.room_groups.manager import RoomGroupManager
    from services.persistence.state_store import get_state_store

    store = get_state_store()
    manager = RoomGroupManager(store)
    group = manager.create_group("Living Room", ["room_1", "room_2"])
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from core.logging_config import get_logger

from .types import RoomGroup, RoomGroupMember, calculate_effective_volume

if TYPE_CHECKING:
    from services.multiroom.room_registry import RoomRegistry
    from services.persistence.state_store import PersistentStateStore

logger = get_logger(__name__)


class RoomGroupError(Exception):
    """Base exception for room group operations."""


class MissingRoomGroupUserError(RoomGroupError):
    """Raised when room group state is accessed without a tenant."""


class GroupNotFoundError(RoomGroupError):
    """Raised when a group is not found."""


class RoomNotInGroupError(RoomGroupError):
    """Raised when a room is not in the specified group."""


class InvalidVolumeError(RoomGroupError):
    """Raised when volume value is invalid."""


class RoomGroupManager:
    """
    Manages room groups with per-room volume control.

    Thread-safe operations backed by PersistentStateStore.
    """

    STORAGE_KEY = "room_groups"

    def __init__(self, persistence: PersistentStateStore) -> None:
        """
        Initialize the RoomGroupManager.

        Args:
            persistence: PersistentStateStore for durable storage
        """
        self._persistence = persistence
        self._groups_by_user: dict[str, dict[str, RoomGroup]] = {}

    @staticmethod
    def _require_user_id(user_id: str | None) -> str:
        """Return a normalized tenant id or fail before touching user state."""
        if not isinstance(user_id, str) or not user_id.strip():
            raise MissingRoomGroupUserError("Room group operations require user_id")
        return user_id.strip()

    def _groups_for_user(self, user_id: str | None) -> dict[str, RoomGroup]:
        """Load and return the in-memory group map for one tenant."""
        normalized_user_id = self._require_user_id(user_id)
        if normalized_user_id not in self._groups_by_user:
            self._groups_by_user[normalized_user_id] = self._load_groups(normalized_user_id)
        return self._groups_by_user[normalized_user_id]

    def _load_groups(self, user_id: str) -> dict[str, RoomGroup]:
        """Load groups from persistence."""
        groups: dict[str, RoomGroup] = {}
        try:
            stored = self._persistence.get_setting(user_id, self.STORAGE_KEY, default={})
            if isinstance(stored, dict):
                for group_id, data in stored.items():
                    if isinstance(data, dict):
                        members = tuple(
                            RoomGroupMember(
                                room_id=m.get("room_id", ""),
                                volume_offset=m.get("volume_offset", 0),
                                is_muted=m.get("is_muted", False),
                            )
                            for m in data.get("members", [])
                        )
                        groups[group_id] = RoomGroup(
                            group_id=group_id,
                            group_name=data.get("group_name", ""),
                            room_ids=tuple(data.get("room_ids", [])),
                            master_volume=data.get("master_volume", 80),
                            members=members,
                            created_at=data.get("created_at", time.time()),
                            updated_at=data.get("updated_at", time.time()),
                        )
                logger.info(
                    "Loaded %d room groups from persistence for user=%s",
                    len(groups),
                    user_id,
                )
        except Exception as exc:
            logger.warning("Failed to load room groups for user=%s: %s", user_id, exc)
        return groups

    def _save_groups(self, user_id: str) -> None:
        """Save groups to persistence."""
        try:
            data = {}
            for group_id, group in self._groups_for_user(user_id).items():
                data[group_id] = {
                    "group_name": group.group_name,
                    "room_ids": list(group.room_ids),
                    "master_volume": group.master_volume,
                    "members": [
                        {
                            "room_id": m.room_id,
                            "volume_offset": m.volume_offset,
                            "is_muted": m.is_muted,
                        }
                        for m in group.members
                    ],
                    "created_at": group.created_at,
                    "updated_at": group.updated_at,
                }
            self._persistence.set_setting(user_id, self.STORAGE_KEY, data)
        except Exception as exc:
            logger.exception("Failed to save room groups for user=%s: %s", user_id, exc)

    def create_group(self, name: str, room_ids: list[str], *, user_id: str) -> RoomGroup:
        """
        Create a new room group.

        Args:
            name: Display name for the group
            room_ids: List of room IDs to include

        Returns:
            The created RoomGroup
        """
        normalized_user_id = self._require_user_id(user_id)
        groups = self._groups_for_user(normalized_user_id)
        group_id = str(uuid.uuid4())
        now = time.time()

        # Create members for each room with default settings
        members = tuple(RoomGroupMember(room_id=rid) for rid in room_ids)

        group = RoomGroup(
            group_id=group_id,
            group_name=name,
            room_ids=tuple(room_ids),
            master_volume=80,
            members=members,
            created_at=now,
            updated_at=now,
        )

        groups[group_id] = group
        self._save_groups(normalized_user_id)
        logger.info(
            "Created room group: %s with %d rooms for user=%s",
            name,
            len(room_ids),
            normalized_user_id,
        )
        return group

    def update_group(self, group_id: str, *, user_id: str, **kwargs) -> RoomGroup:
        """
        Update a room group.

        Args:
            group_id: ID of the group to update
            **kwargs: Fields to update (group_name, room_ids, master_volume)

        Returns:
            The updated RoomGroup

        Raises:
            GroupNotFoundError: If group does not exist
        """
        normalized_user_id = self._require_user_id(user_id)
        groups = self._groups_for_user(normalized_user_id)
        if group_id not in groups:
            raise GroupNotFoundError(f"Group not found: {group_id}")

        group = groups[group_id]
        updates = {"updated_at": time.time()}

        if "group_name" in kwargs:
            updates["group_name"] = str(kwargs["group_name"])

        if "room_ids" in kwargs:
            new_room_ids = tuple(kwargs["room_ids"])
            updates["room_ids"] = new_room_ids
            # Update members to match new room_ids
            existing_members = {m.room_id: m for m in group.members}
            new_members = []
            for rid in new_room_ids:
                if rid in existing_members:
                    new_members.append(existing_members[rid])
                else:
                    new_members.append(RoomGroupMember(room_id=rid))
            updates["members"] = tuple(new_members)

        if "master_volume" in kwargs:
            vol = int(kwargs["master_volume"])
            if not 0 <= vol <= 100:
                raise InvalidVolumeError(f"Master volume must be 0-100, got {vol}")
            updates["master_volume"] = vol

        group = replace(group, **updates)
        groups[group_id] = group
        self._save_groups(normalized_user_id)
        logger.info("Updated room group: %s for user=%s", group_id, normalized_user_id)
        return group

    def delete_group(self, group_id: str, *, user_id: str) -> bool:
        """
        Delete a room group.

        Args:
            group_id: ID of the group to delete

        Returns:
            True if deleted, False if not found
        """
        normalized_user_id = self._require_user_id(user_id)
        groups = self._groups_for_user(normalized_user_id)
        if group_id not in groups:
            return False

        del groups[group_id]
        self._save_groups(normalized_user_id)
        logger.info("Deleted room group: %s for user=%s", group_id, normalized_user_id)
        return True

    def get_group(self, group_id: str, *, user_id: str) -> RoomGroup | None:
        """
        Get a room group by ID.

        Args:
            group_id: ID of the group

        Returns:
            RoomGroup or None if not found
        """
        return self._groups_for_user(user_id).get(group_id)

    def list_groups(self, *, user_id: str) -> list[RoomGroup]:
        """
        List all room groups.

        Returns:
            List of RoomGroup objects
        """
        return list(self._groups_for_user(user_id).values())

    def set_master_volume(self, group_id: str, volume: int, *, user_id: str) -> None:
        """
        Set the master volume for a group.

        Args:
            group_id: ID of the group
            volume: Master volume level (0-100)

        Raises:
            GroupNotFoundError: If group does not exist
            InvalidVolumeError: If volume is out of range
        """
        if not 0 <= volume <= 100:
            raise InvalidVolumeError(f"Volume must be 0-100, got {volume}")

        self.update_group(group_id, user_id=user_id, master_volume=volume)

    def set_room_volume(self, group_id: str, room_id: str, offset: int, *, user_id: str) -> None:
        """
        Set the volume offset for a room within a group.

        Args:
            group_id: ID of the group
            room_id: ID of the room
            offset: Volume offset (-100 to +100)

        Raises:
            GroupNotFoundError: If group does not exist
            RoomNotInGroupError: If room is not in the group
            InvalidVolumeError: If offset is out of range
        """
        normalized_user_id = self._require_user_id(user_id)
        groups = self._groups_for_user(normalized_user_id)
        if group_id not in groups:
            raise GroupNotFoundError(f"Group not found: {group_id}")

        if not -100 <= offset <= 100:
            raise InvalidVolumeError(f"Volume offset must be -100 to +100, got {offset}")

        group = groups[group_id]
        if room_id not in group.room_ids:
            raise RoomNotInGroupError(f"Room {room_id} not in group {group_id}")

        # Update the member's volume offset
        new_members = []
        for member in group.members:
            if member.room_id == room_id:
                new_members.append(replace(member, volume_offset=offset))
            else:
                new_members.append(member)

        group = replace(group, members=tuple(new_members), updated_at=time.time())
        groups[group_id] = group
        self._save_groups(normalized_user_id)
        logger.debug(
            "Set room %s volume offset to %d in group %s for user=%s",
            room_id,
            offset,
            group_id,
            normalized_user_id,
        )

    def set_room_mute(self, group_id: str, room_id: str, muted: bool, *, user_id: str) -> None:
        """
        Set the mute state for a room within a group.

        Args:
            group_id: ID of the group
            room_id: ID of the room
            muted: Whether the room should be muted

        Raises:
            GroupNotFoundError: If group does not exist
            RoomNotInGroupError: If room is not in the group
        """
        normalized_user_id = self._require_user_id(user_id)
        groups = self._groups_for_user(normalized_user_id)
        if group_id not in groups:
            raise GroupNotFoundError(f"Group not found: {group_id}")

        group = groups[group_id]
        if room_id not in group.room_ids:
            raise RoomNotInGroupError(f"Room {room_id} not in group {group_id}")

        # Update the member's mute state
        new_members = []
        for member in group.members:
            if member.room_id == room_id:
                new_members.append(replace(member, is_muted=muted))
            else:
                new_members.append(member)

        group = replace(group, members=tuple(new_members), updated_at=time.time())
        groups[group_id] = group
        self._save_groups(normalized_user_id)
        logger.debug(
            "Set room %s mute=%s in group %s for user=%s",
            room_id,
            muted,
            group_id,
            normalized_user_id,
        )

    def calculate_effective_volume(self, group_id: str, room_id: str, *, user_id: str) -> int:
        """
        Calculate the effective volume for a room in a group.

        Args:
            group_id: ID of the group
            room_id: ID of the room

        Returns:
            Effective volume (0-100), or 0 if room is muted

        Raises:
            GroupNotFoundError: If group does not exist
            RoomNotInGroupError: If room is not in the group
        """
        groups = self._groups_for_user(user_id)
        if group_id not in groups:
            raise GroupNotFoundError(f"Group not found: {group_id}")

        group = groups[group_id]
        if room_id not in group.room_ids:
            raise RoomNotInGroupError(f"Room {room_id} not in group {group_id}")

        # Find the member
        for member in group.members:
            if member.room_id == room_id:
                if member.is_muted:
                    return 0
                return calculate_effective_volume(group.master_volume, member.volume_offset)

        # Room ID in room_ids but no member entry (shouldn't happen)
        return calculate_effective_volume(group.master_volume, 0)

    def evict_dead_members(
        self,
        *,
        user_id: str,
        registry: RoomRegistry | None = None,
        ttl_seconds: float = 300.0,
    ) -> int:
        """
        Evict dead spokes from all room groups based on registry health and TTL.

        A spoke is evicted if:
        - It is not the local room
        - It is not in the online registry (offline or missing entirely)
        - Its last_seen timestamp is older than ttl_seconds

        Args:
            registry: RoomRegistry instance. If None, fetches the global singleton.
            ttl_seconds: Age threshold in seconds. Spokes unseen longer than this
                         are removed from all groups.

        Returns:
            Total count of evicted group members across all groups.
        """
        _reg: RoomRegistry
        if registry is None:
            from services.multiroom.room_registry import get_room_registry

            _reg = get_room_registry()
        else:
            _reg = registry

        try:
            local_id = _reg.get_local_room().id
        except Exception:
            local_id = None

        online_ids = {r.id for r in _reg.get_online_rooms()}
        now = time.time()
        evicted_total = 0

        normalized_user_id = self._require_user_id(user_id)
        for group in self.list_groups(user_id=normalized_user_id):
            to_evict: list[str] = []
            for room_id in group.room_ids:
                if room_id == local_id:
                    continue  # Never evict local room
                if room_id in online_ids:
                    continue  # Room is online, keep it
                # Check TTL via last_seen
                room_info = _reg.get_room(room_id)
                if room_info is None:
                    # Room not in registry at all — treat as dead (no TTL check)
                    age = ttl_seconds + 1  # force eviction
                else:
                    age = now - room_info.last_seen
                if age >= ttl_seconds:
                    to_evict.append(room_id)

            if to_evict:
                new_room_ids = tuple(r for r in group.room_ids if r not in to_evict)
                new_members = tuple(m for m in group.members if m.room_id not in to_evict)
                try:
                    self.update_group(
                        group.group_id,
                        user_id=normalized_user_id,
                        room_ids=new_room_ids,
                        members=new_members,
                    )
                    for room_id in to_evict:
                        room_info = _reg.get_room(room_id)
                        age_str = f"{now - room_info.last_seen:.0f}" if room_info else "unknown"
                        logger.info(
                            "Evicted dead spoke %s from group '%s' (offline for %ss)",
                            room_id,
                            group.group_name,
                            age_str,
                        )
                    evicted_total += len(to_evict)
                except Exception as exc:
                    logger.warning(
                        "Failed to evict dead spokes %s from group %s: %s",
                        to_evict,
                        group.group_id,
                        exc,
                    )

        return evicted_total


__all__ = [
    "GroupNotFoundError",
    "InvalidVolumeError",
    "MissingRoomGroupUserError",
    "RoomGroupError",
    "RoomGroupManager",
    "RoomNotInGroupError",
]
