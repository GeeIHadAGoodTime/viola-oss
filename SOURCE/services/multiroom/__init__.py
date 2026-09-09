"""
Multi-Room Audio Synchronization Services

This module provides services for multi-room audio synchronization,
including hub failover, state replication, and spoke coordination.

Submodules:
    - failover: Hub failover system with leader election and split-brain resolution
    - room_registry: Room/device tracking for multi-room coordination
"""

from __future__ import annotations

from .room_registry import RoomInfo, RoomRegistry, get_room_registry

__all__: list[str] = [
    "RoomInfo",
    "RoomRegistry",
    "get_room_registry",
]
