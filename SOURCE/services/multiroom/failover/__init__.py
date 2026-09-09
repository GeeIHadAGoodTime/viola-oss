"""
Hub Failover System for Multi-Room Audio Sync

This module provides hub failover capabilities for multi-room audio synchronization.
It implements:

- Fencing tokens for split-brain prevention
- Heartbeat protocol for health monitoring
- CRDT-based state replication
- Leader election with deterministic tie-breaking
- Split-brain detection and resolution
- Spoke reconnection after failover

Architecture:
    Primary Hub <--heartbeat--> Secondary Hub
         |                           |
         |                           |
    [Spokes...]               [Spokes...]

Usage:
    from services.multiroom.failover import (
        FencingTokenManager,
        HubHeartbeatProtocol,
        LeaderElection,
        SplitBrainResolver,
        SpokeReconnectionHandler,
        StateReplicator,
    )

    # Initialize components
    fencing = FencingTokenManager()
    heartbeat = HubHeartbeatProtocol(hub_id="hub-1", ...)
    election = LeaderElection(hub_id="hub-1")
    replicator = StateReplicator(hub_id="hub-1")
    split_brain = SplitBrainResolver(hub_id="hub-1")
    spoke_handler = SpokeReconnectionHandler(hub_id="hub-1", fencing)

    # Start heartbeat protocol
    await heartbeat.start()

    # On partner timeout, consider becoming primary
    if election.should_become_primary(...):
        election.promote_to_primary(fencing.generate_token())
        await spoke_handler.broadcast_hub_announcement()
"""

from __future__ import annotations

from .fencing import FencingTokenManager
from .heartbeat_protocol import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_MULTIPLIER,
    HEARTBEAT_TIMEOUT_SECONDS,
    HubHeartbeatProtocol,
)
from .leader_election import LeaderElection
from .split_brain import SplitBrainResolution, SplitBrainResolver
from .spoke_reconnection import (
    ReconnectionResult,
    SpokeInfo,
    SpokeReconnectionHandler,
)
from .state_replication import (
    PlaybackStateSnapshot,
    QueueSnapshot,
    RoomMembershipSnapshot,
    StateReplicator,
    StateSnapshot,
)
from .types import FailoverState, HubAnnouncement, HubHeartbeat, HubRole

__all__ = [
    # Heartbeat
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TIMEOUT_MULTIPLIER",
    "HEARTBEAT_TIMEOUT_SECONDS",
    # Types
    "FailoverState",
    "FencingTokenManager",
    "HubAnnouncement",
    "HubHeartbeat",
    "HubHeartbeatProtocol",
    "HubRole",
    # Leader Election
    "LeaderElection",
    # State Replication
    "PlaybackStateSnapshot",
    "QueueSnapshot",
    "ReconnectionResult",
    "RoomMembershipSnapshot",
    # Split Brain
    "SplitBrainResolution",
    "SplitBrainResolver",
    # Spoke Reconnection
    "SpokeInfo",
    "SpokeReconnectionHandler",
    "StateReplicator",
    "StateSnapshot",
]
