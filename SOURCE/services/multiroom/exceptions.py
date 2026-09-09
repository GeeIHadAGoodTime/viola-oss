"""
Multi-Room Sync Exception Hierarchy

All multi-room synchronization exceptions inherit from MultiRoomError for:
- Consistent error handling across the sync pipeline
- Easy distinction from other service errors
- Structured error context for debugging and user feedback

Usage:
    from services.multiroom.exceptions import NodeSyncError, HeartbeatTimeoutError

    try:
        await sync_node(node_id)
    except NodeSyncError as e:
        logger.error("Node sync failed: %s", e)
        show_error(e.user_friendly_message())
"""

from __future__ import annotations

from core.exceptions import ErrorContext, ErrorSeverity, ServiceError


class MultiRoomError(ServiceError):
    """
    Base exception for all multi-room synchronization errors.

    Inherits from ServiceError to integrate with the standard
    error handling and monitoring pipeline.
    """

    severity = ErrorSeverity.MEDIUM


class NodeSyncError(MultiRoomError):
    """Node synchronization failed."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, node_id: str, reason: str = "", context: ErrorContext | None = None):
        msg = f"Node sync failed for '{node_id}'"
        if reason:
            msg += f": {reason}"
        if context is None:
            context = ErrorContext(
                component="multiroom.sync",
                operation="sync_node",
                params={"node_id": node_id},
                user_message="Multi-room sync is experiencing issues. Retrying...",
                recovery_hint="Check network connectivity between devices.",
            )
        super().__init__(msg, context)
        self.node_id = node_id


class DeviceDiscoveryError(MultiRoomError):
    """Device discovery failed."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, reason: str = "", context: ErrorContext | None = None):
        msg = "Device discovery failed"
        if reason:
            msg += f": {reason}"
        if context is None:
            context = ErrorContext(
                component="multiroom.discovery",
                operation="discover_devices",
                user_message="Could not find other devices on your network.",
                recovery_hint="Ensure devices are on the same network and discovery is enabled.",
            )
        super().__init__(msg, context)


class HeartbeatTimeoutError(MultiRoomError):
    """Hub heartbeat timeout - hub may be unreachable."""

    severity = ErrorSeverity.HIGH
    retryable = True

    def __init__(self, hub_id: str, timeout_seconds: float, context: ErrorContext | None = None):
        msg = f"Hub '{hub_id}' heartbeat timeout after {timeout_seconds}s"
        if context is None:
            context = ErrorContext(
                component="multiroom.heartbeat",
                operation="check_heartbeat",
                params={"hub_id": hub_id, "timeout_seconds": timeout_seconds},
                user_message="Connection to the main hub was lost.",
                recovery_hint="The system will attempt to reconnect automatically.",
            )
        super().__init__(msg, context)
        self.hub_id = hub_id
        self.timeout_seconds = timeout_seconds


class CloudRelayError(MultiRoomError):
    """Cloud relay connection failed."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, reason: str = "", context: ErrorContext | None = None):
        msg = "Cloud relay connection failed"
        if reason:
            msg += f": {reason}"
        if context is None:
            context = ErrorContext(
                component="multiroom.cloud_relay",
                operation="connect",
                user_message="Could not connect to cloud sync service.",
                recovery_hint="Check internet connection. Local sync will continue working.",
            )
        super().__init__(msg, context)


class RoomLimitExceededError(MultiRoomError):
    """Room limit for subscription tier exceeded."""

    severity = ErrorSeverity.LOW
    retryable = False

    def __init__(
        self,
        current_rooms: int,
        room_limit: int,
        tier: str = "free",
        context: ErrorContext | None = None,
    ):
        msg = f"Room limit exceeded: {current_rooms}/{room_limit} for {tier} tier"
        if context is None:
            context = ErrorContext(
                component="multiroom.limits",
                operation="add_room",
                params={
                    "current_rooms": current_rooms,
                    "room_limit": room_limit,
                    "tier": tier,
                },
                user_message=f"You've reached the {room_limit}-room limit for your plan.",
                recovery_hint="Upgrade to Premium for unlimited rooms.",
            )
        super().__init__(msg, context)
        self.current_rooms = current_rooms
        self.room_limit = room_limit
        self.tier = tier


__all__ = [
    "CloudRelayError",
    "DeviceDiscoveryError",
    "HeartbeatTimeoutError",
    "MultiRoomError",
    "NodeSyncError",
    "RoomLimitExceededError",
]
