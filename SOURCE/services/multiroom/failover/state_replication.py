"""
Hub Failover System - State Replicator

Handles state replication between primary and secondary hubs using CRDT
(Conflict-free Replicated Data Type) for conflict resolution.

Wraps the existing CRDT implementation to provide state synchronization
for playback state, queue, and room memberships.

Usage:
    from services.multiroom.failover.state_replication import StateReplicator

    replicator = StateReplicator(hub_id="hub-1")
    replicator.replicate_state(local_state)
    merged = replicator.merge_state(local_state, remote_state)
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from experimental.phase2_multiroom.crdt_sync import VectorClock

logger = get_logger(__name__)


@dataclass
class PlaybackStateSnapshot:
    """
    Snapshot of playback state for replication.

    Attributes:
        is_playing: Whether audio is currently playing
        position_ms: Current playback position in milliseconds
        volume: Current volume (0-100)
        current_track_id: ID of current track (None if idle)
        timestamp: Unix timestamp when snapshot was taken
    """

    is_playing: bool = False
    position_ms: int = 0
    volume: int = 80
    current_track_id: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class QueueSnapshot:
    """
    Snapshot of playback queue for replication.

    Attributes:
        track_ids: Ordered list of track IDs in queue
        version: Queue version for conflict detection
        timestamp: Unix timestamp when snapshot was taken
    """

    track_ids: list[str] = field(default_factory=list)
    version: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RoomMembershipSnapshot:
    """
    Snapshot of room memberships for replication.

    Attributes:
        room_id: Room identifier
        member_ids: Set of device IDs in this room
        primary_hub_id: ID of the primary hub for this room
        timestamp: Unix timestamp when snapshot was taken
    """

    room_id: str = ""
    member_ids: set[str] = field(default_factory=set)
    primary_hub_id: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class StateSnapshot:
    """
    Complete state snapshot for hub-to-hub replication.

    Attributes:
        hub_id: ID of the hub that created this snapshot
        version: Monotonic version number
        fencing_token: Current fencing token
        playback: Playback state
        queue: Queue state
        room_memberships: Dict of room_id -> membership
        vector_clock: CRDT vector clock for ordering
        timestamp: Unix timestamp when snapshot was created
    """

    hub_id: str
    version: int = 0
    fencing_token: int = 0
    playback: PlaybackStateSnapshot = field(default_factory=PlaybackStateSnapshot)
    queue: QueueSnapshot = field(default_factory=QueueSnapshot)
    room_memberships: dict[str, RoomMembershipSnapshot] = field(default_factory=dict)
    vector_clock: dict[str, int] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "hub_id": self.hub_id,
            "version": self.version,
            "fencing_token": self.fencing_token,
            "playback": {
                "is_playing": self.playback.is_playing,
                "position_ms": self.playback.position_ms,
                "volume": self.playback.volume,
                "current_track_id": self.playback.current_track_id,
                "timestamp": self.playback.timestamp,
            },
            "queue": {
                "track_ids": self.queue.track_ids,
                "version": self.queue.version,
                "timestamp": self.queue.timestamp,
            },
            "room_memberships": {
                room_id: {
                    "room_id": membership.room_id,
                    "member_ids": list(membership.member_ids),
                    "primary_hub_id": membership.primary_hub_id,
                    "timestamp": membership.timestamp,
                }
                for room_id, membership in self.room_memberships.items()
            },
            "vector_clock": self.vector_clock,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateSnapshot:
        """Create from dictionary."""
        playback_data = data.get("playback", {})
        queue_data = data.get("queue", {})
        memberships_data = data.get("room_memberships", {})

        playback = PlaybackStateSnapshot(
            is_playing=playback_data.get("is_playing", False),
            position_ms=playback_data.get("position_ms", 0),
            volume=playback_data.get("volume", 80),
            current_track_id=playback_data.get("current_track_id"),
            timestamp=playback_data.get("timestamp", time.time()),
        )

        queue = QueueSnapshot(
            track_ids=queue_data.get("track_ids", []),
            version=queue_data.get("version", 0),
            timestamp=queue_data.get("timestamp", time.time()),
        )

        room_memberships = {}
        for room_id, membership_data in memberships_data.items():
            room_memberships[room_id] = RoomMembershipSnapshot(
                room_id=membership_data.get("room_id", room_id),
                member_ids=set(membership_data.get("member_ids", [])),
                primary_hub_id=membership_data.get("primary_hub_id"),
                timestamp=membership_data.get("timestamp", time.time()),
            )

        return cls(
            hub_id=data.get("hub_id", ""),
            version=data.get("version", 0),
            fencing_token=data.get("fencing_token", 0),
            playback=playback,
            queue=queue,
            room_memberships=room_memberships,
            vector_clock=data.get("vector_clock", {}),
            timestamp=data.get("timestamp", time.time()),
        )


class StateReplicator:
    """
    Handles state replication between hubs using CRDT.

    Uses vector clocks for conflict-free merging of concurrent updates.
    The primary hub's state takes precedence when fencing tokens differ.
    """

    def __init__(self, hub_id: str) -> None:
        """
        Initialize the state replicator.

        Args:
            hub_id: This hub's unique identifier
        """
        self.hub_id = hub_id
        self._vector_clock = VectorClock(device_id=hub_id)
        self._current_version = 0
        self._last_snapshot: StateSnapshot | None = None

        logger.debug("StateReplicator initialized for hub %s", hub_id)

    def create_snapshot(
        self,
        playback: PlaybackStateSnapshot | None = None,
        queue: QueueSnapshot | None = None,
        room_memberships: dict[str, RoomMembershipSnapshot] | None = None,
        fencing_token: int = 0,
    ) -> StateSnapshot:
        """
        Create a state snapshot for replication.

        Args:
            playback: Current playback state
            queue: Current queue state
            room_memberships: Current room memberships
            fencing_token: Current fencing token

        Returns:
            StateSnapshot ready for transmission
        """
        self._vector_clock.increment()
        self._current_version += 1

        snapshot = StateSnapshot(
            hub_id=self.hub_id,
            version=self._current_version,
            fencing_token=fencing_token,
            playback=playback or PlaybackStateSnapshot(),
            queue=queue or QueueSnapshot(),
            room_memberships=room_memberships or {},
            vector_clock=dict(self._vector_clock.clock),
            timestamp=time.time(),
        )

        self._last_snapshot = snapshot
        logger.debug(
            "Created state snapshot: hub=%s version=%d",
            self.hub_id,
            self._current_version,
        )

        return snapshot

    def replicate_state(self, state_snapshot: StateSnapshot) -> dict[str, Any]:
        """
        Prepare state for replication to partner hub.

        Args:
            state_snapshot: Snapshot to replicate

        Returns:
            Dictionary suitable for network transmission
        """
        logger.debug(
            "Replicating state: hub=%s version=%d",
            state_snapshot.hub_id,
            state_snapshot.version,
        )
        return state_snapshot.to_dict()

    def receive_state(self, state_data: dict[str, Any]) -> StateSnapshot:
        """
        Receive state from partner hub.

        Updates the local vector clock based on received state.

        Args:
            state_data: Received state dictionary

        Returns:
            Parsed StateSnapshot
        """
        snapshot = StateSnapshot.from_dict(state_data)

        # Update vector clock with received values
        self._vector_clock.update(snapshot.vector_clock)

        logger.debug(
            "Received state: hub=%s version=%d",
            snapshot.hub_id,
            snapshot.version,
        )

        return snapshot

    def merge_state(
        self,
        local_state: StateSnapshot,
        remote_state: StateSnapshot,
    ) -> StateSnapshot:
        """
        Merge local and remote states using CRDT rules.

        Resolution rules:
        1. Higher fencing token wins
        2. If fencing tokens equal, higher version wins
        3. If versions equal, use vector clock comparison
        4. For concurrent updates, merge field-by-field

        Args:
            local_state: Local state snapshot
            remote_state: Remote state snapshot

        Returns:
            Merged state snapshot
        """
        # Compare fencing tokens first
        if remote_state.fencing_token > local_state.fencing_token:
            logger.info(
                "Remote state wins by fencing token: %d > %d",
                remote_state.fencing_token,
                local_state.fencing_token,
            )
            return self._adopt_remote(remote_state)

        if local_state.fencing_token > remote_state.fencing_token:
            logger.debug(
                "Local state wins by fencing token: %d > %d",
                local_state.fencing_token,
                remote_state.fencing_token,
            )
            return local_state

        # Fencing tokens equal, compare versions
        if remote_state.version > local_state.version:
            logger.debug(
                "Remote state wins by version: %d > %d",
                remote_state.version,
                local_state.version,
            )
            return self._adopt_remote(remote_state)

        if local_state.version > remote_state.version:
            logger.debug(
                "Local state wins by version: %d > %d",
                local_state.version,
                remote_state.version,
            )
            return local_state

        # Versions equal, use vector clock
        local_clock = VectorClock(
            device_id=local_state.hub_id,
            clock=dict(local_state.vector_clock),
        )
        remote_clock = VectorClock(
            device_id=remote_state.hub_id,
            clock=dict(remote_state.vector_clock),
        )

        comparison = local_clock.compare(remote_clock)

        if comparison == "before":
            logger.debug("Remote state wins by vector clock (local before remote)")
            return self._adopt_remote(remote_state)

        if comparison == "after":
            logger.debug("Local state wins by vector clock (local after remote)")
            return local_state

        # Concurrent - merge field by field
        logger.debug("Concurrent states detected, performing field merge")
        return self._merge_concurrent(local_state, remote_state)

    def _adopt_remote(self, remote_state: StateSnapshot) -> StateSnapshot:
        """
        Adopt remote state as the new local state.

        Updates vector clock and version tracking.

        Args:
            remote_state: Remote state to adopt

        Returns:
            The remote state
        """
        self._vector_clock.update(remote_state.vector_clock)
        self._current_version = max(self._current_version, remote_state.version)
        self._last_snapshot = remote_state
        return remote_state

    def _merge_concurrent(
        self,
        local_state: StateSnapshot,
        remote_state: StateSnapshot,
    ) -> StateSnapshot:
        """
        Merge concurrent states field by field.

        For concurrent updates:
        - Playback: most recent timestamp wins
        - Queue: most recent version wins
        - Room memberships: union of all members

        Args:
            local_state: Local state
            remote_state: Remote state

        Returns:
            Merged state
        """
        # Playback: most recent timestamp wins
        if remote_state.playback.timestamp > local_state.playback.timestamp:
            merged_playback = copy.deepcopy(remote_state.playback)
        else:
            merged_playback = copy.deepcopy(local_state.playback)

        # Queue: most recent version wins
        if remote_state.queue.version > local_state.queue.version:
            merged_queue = copy.deepcopy(remote_state.queue)
        else:
            merged_queue = copy.deepcopy(local_state.queue)

        # Room memberships: union members, use most recent primary_hub_id
        merged_memberships: dict[str, RoomMembershipSnapshot] = {}
        all_room_ids = set(local_state.room_memberships.keys()) | set(remote_state.room_memberships.keys())

        for room_id in all_room_ids:
            local_membership = local_state.room_memberships.get(room_id)
            remote_membership = remote_state.room_memberships.get(room_id)

            if local_membership and remote_membership:
                # Merge memberships
                merged_members = local_membership.member_ids | remote_membership.member_ids
                # Use most recent primary_hub_id
                if remote_membership.timestamp > local_membership.timestamp:
                    primary_hub = remote_membership.primary_hub_id
                else:
                    primary_hub = local_membership.primary_hub_id

                merged_memberships[room_id] = RoomMembershipSnapshot(
                    room_id=room_id,
                    member_ids=merged_members,
                    primary_hub_id=primary_hub,
                    timestamp=max(local_membership.timestamp, remote_membership.timestamp),
                )
            elif local_membership:
                merged_memberships[room_id] = copy.deepcopy(local_membership)
            elif remote_membership:
                merged_memberships[room_id] = copy.deepcopy(remote_membership)

        # Merge vector clocks
        merged_clock = dict(local_state.vector_clock)
        for device_id, time_value in remote_state.vector_clock.items():
            merged_clock[device_id] = max(merged_clock.get(device_id, 0), time_value)

        # Increment our clock component
        self._vector_clock.update(merged_clock)
        self._vector_clock.increment()
        self._current_version += 1

        merged = StateSnapshot(
            hub_id=self.hub_id,
            version=self._current_version,
            fencing_token=max(local_state.fencing_token, remote_state.fencing_token),
            playback=merged_playback,
            queue=merged_queue,
            room_memberships=merged_memberships,
            vector_clock=dict(self._vector_clock.clock),
            timestamp=time.time(),
        )

        self._last_snapshot = merged
        logger.info(
            "Merged concurrent states: new version=%d",
            self._current_version,
        )

        return merged


__all__ = [
    "PlaybackStateSnapshot",
    "QueueSnapshot",
    "RoomMembershipSnapshot",
    "StateReplicator",
    "StateSnapshot",
]
