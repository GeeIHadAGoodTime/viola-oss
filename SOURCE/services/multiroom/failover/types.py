"""
Hub Failover System - Type Definitions

Dataclasses and enums for hub failover coordination in multi-room audio sync.

Usage:
    from services.multiroom.failover.types import (
        HubRole,
        HubHeartbeat,
        HubAnnouncement,
        FailoverState,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class HubRole(Enum):
    """Role of a hub in the failover system."""

    PRIMARY = "primary"  # Active hub handling all operations
    SECONDARY = "secondary"  # Standby hub ready to take over
    SPOKE = "spoke"  # Client node, not a hub


@dataclass(frozen=True, slots=True)
class HubHeartbeat:
    """
    Heartbeat message sent between hubs.

    Sent at 1Hz frequency to monitor hub health and coordinate failover.

    Attributes:
        hub_id: Unique identifier for the hub
        fencing_token: Monotonic token for leader election
        state_version: Current state version for consistency tracking
        hub_time: Unix timestamp from the hub's clock
        role: Current role of the hub
    """

    hub_id: str
    fencing_token: int
    state_version: int
    hub_time: float
    role: Literal["primary", "secondary"]


@dataclass(frozen=True, slots=True)
class HubAnnouncement:
    """
    Hub announcement for spoke reconnection.

    Broadcast when a hub becomes primary to notify spokes of the new primary.

    Attributes:
        hub_id: Unique identifier for the hub
        role: Current role of the hub
        fencing_token: Current fencing token
        hub_time: Unix timestamp from the hub's clock
        endpoint: Network endpoint (host:port) for spoke connections
    """

    hub_id: str
    role: Literal["primary", "secondary"]
    fencing_token: int
    hub_time: float
    endpoint: str


@dataclass(slots=True)
class FailoverState:
    """
    Current failover state for a hub.

    Mutable state tracking the hub's role and partner coordination.

    Attributes:
        current_role: Current role in the failover system
        partner_hub_id: ID of the partner hub (None if no partner)
        last_heartbeat_time: Unix timestamp of last received heartbeat
        fencing_token: Current fencing token for this hub
        state_version: Current state version
    """

    current_role: HubRole = HubRole.SECONDARY
    partner_hub_id: str | None = None
    last_heartbeat_time: float = 0.0
    fencing_token: int = 0
    state_version: int = 0


__all__ = [
    "FailoverState",
    "HubAnnouncement",
    "HubHeartbeat",
    "HubRole",
]
