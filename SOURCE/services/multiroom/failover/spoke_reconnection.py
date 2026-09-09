"""
Hub Failover System - Spoke Reconnection Handler

Handles spoke (client) reconnection after hub failover.

When a hub becomes primary, it broadcasts an announcement to all spokes.
Spokes validate the fencing token before accepting the new primary to
prevent stale leaders from taking control.

Usage:
    from services.multiroom.failover.spoke_reconnection import SpokeReconnectionHandler

    handler = SpokeReconnectionHandler(hub_id="hub-1", fencing_token_manager=manager)
    await handler.broadcast_hub_announcement(announcement)
    handler.handle_spoke_reconnect(spoke_id, spoke_fencing_token)
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from core.logging_config import get_logger

from .fencing import FencingTokenManager
from .types import HubAnnouncement, HubRole

logger = get_logger(__name__)


@dataclass
class SpokeInfo:
    """
    Information about a connected spoke.

    Attributes:
        spoke_id: Unique identifier for the spoke
        last_seen: Unix timestamp of last activity
        accepted_fencing_token: Fencing token the spoke has accepted
        is_connected: Whether spoke is currently connected
    """

    spoke_id: str
    last_seen: float = field(default_factory=time.time)
    accepted_fencing_token: int = 0
    is_connected: bool = False


@dataclass
class ReconnectionResult:
    """
    Result of a spoke reconnection attempt.

    Attributes:
        spoke_id: Spoke that attempted reconnection
        accepted: Whether reconnection was accepted
        reason: Reason for acceptance/rejection
        new_fencing_token: Current fencing token (for spoke to update)
    """

    spoke_id: str
    accepted: bool
    reason: str
    new_fencing_token: int


class SpokeReconnectionHandler:
    """
    Manages spoke reconnection after hub failover.

    Spokes must validate fencing tokens before accepting a new primary.
    This prevents stale leaders from controlling spokes after failover.
    """

    def __init__(
        self,
        hub_id: str,
        fencing_token_manager: FencingTokenManager,
        endpoint: str = "",
    ) -> None:
        """
        Initialize the spoke reconnection handler.

        Args:
            hub_id: This hub's unique identifier
            fencing_token_manager: Manager for fencing tokens
            endpoint: Network endpoint for this hub (host:port)
        """
        self.hub_id = hub_id
        self._fencing_manager = fencing_token_manager
        self._endpoint = endpoint
        self._connected_spokes: dict[str, SpokeInfo] = {}
        self._broadcast_callback: Callable[[HubAnnouncement], Awaitable[None]] | None = None
        self._on_spoke_connect_callback: Callable[[str], Awaitable[None]] | None = None
        self._on_spoke_disconnect_callback: Callable[[str], Awaitable[None]] | None = None

        logger.debug(
            "SpokeReconnectionHandler initialized: hub=%s endpoint=%s",
            hub_id,
            endpoint,
        )

    def set_endpoint(self, endpoint: str) -> None:
        """
        Set the network endpoint for hub announcements.

        Args:
            endpoint: Network endpoint (host:port)
        """
        self._endpoint = endpoint

    def set_broadcast_callback(
        self,
        callback: Callable[[HubAnnouncement], Awaitable[None]],
    ) -> None:
        """
        Set the callback for broadcasting hub announcements.

        Args:
            callback: Async function to broadcast announcement to all spokes
        """
        self._broadcast_callback = callback

    def set_spoke_callbacks(
        self,
        on_connect: Callable[[str], Awaitable[None]] | None = None,
        on_disconnect: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """
        Set callbacks for spoke connection events.

        Args:
            on_connect: Called when spoke connects
            on_disconnect: Called when spoke disconnects
        """
        self._on_spoke_connect_callback = on_connect
        self._on_spoke_disconnect_callback = on_disconnect

    async def broadcast_hub_announcement(
        self,
        role: HubRole = HubRole.PRIMARY,
    ) -> HubAnnouncement:
        """
        Broadcast a hub announcement to all spokes.

        Called when this hub becomes primary to notify all spokes.

        Args:
            role: Current role of this hub

        Returns:
            The announcement that was broadcast
        """
        announcement = HubAnnouncement(
            hub_id=self.hub_id,
            role="primary" if role == HubRole.PRIMARY else "secondary",
            fencing_token=self._fencing_manager.get_current_token(),
            hub_time=time.time(),
            endpoint=self._endpoint,
        )

        logger.info(
            "Broadcasting hub announcement: hub=%s role=%s token=%d endpoint=%s",
            announcement.hub_id,
            announcement.role,
            announcement.fencing_token,
            announcement.endpoint,
        )

        if self._broadcast_callback:
            try:
                await self._broadcast_callback(announcement)
            except Exception:
                logger.exception("Failed to broadcast hub announcement")

        return announcement

    def handle_spoke_reconnect(
        self,
        spoke_id: str,
        spoke_fencing_token: int = 0,
    ) -> ReconnectionResult:
        """
        Handle a spoke attempting to reconnect.

        Validates the spoke's fencing token before accepting the connection.
        The spoke must have a fencing token <= current token to be accepted.

        Args:
            spoke_id: ID of the spoke attempting to reconnect
            spoke_fencing_token: Fencing token the spoke last knew

        Returns:
            ReconnectionResult indicating success/failure
        """
        current_token = self._fencing_manager.get_current_token()

        # Validate fencing token
        # Spoke should accept tokens >= what they knew (we send them our current)
        # But spokes with future tokens (shouldn't happen) are suspicious
        if spoke_fencing_token > current_token:
            logger.warning(
                "Rejecting spoke %s: fencing token %d > current %d",
                spoke_id,
                spoke_fencing_token,
                current_token,
            )
            return ReconnectionResult(
                spoke_id=spoke_id,
                accepted=False,
                reason="spoke_token_from_future",
                new_fencing_token=current_token,
            )

        # Accept the spoke
        if spoke_id in self._connected_spokes:
            spoke_info = self._connected_spokes[spoke_id]
            spoke_info.last_seen = time.time()
            spoke_info.accepted_fencing_token = current_token
            spoke_info.is_connected = True
            logger.info(
                "Spoke %s reconnected (token: %d -> %d)",
                spoke_id,
                spoke_fencing_token,
                current_token,
            )
        else:
            self._connected_spokes[spoke_id] = SpokeInfo(
                spoke_id=spoke_id,
                last_seen=time.time(),
                accepted_fencing_token=current_token,
                is_connected=True,
            )
            logger.info(
                "New spoke %s connected (token: %d)",
                spoke_id,
                current_token,
            )

        return ReconnectionResult(
            spoke_id=spoke_id,
            accepted=True,
            reason="accepted",
            new_fencing_token=current_token,
        )

    async def handle_spoke_reconnect_async(
        self,
        spoke_id: str,
        spoke_fencing_token: int = 0,
    ) -> ReconnectionResult:
        """
        Async version of handle_spoke_reconnect with callbacks.

        Args:
            spoke_id: ID of the spoke attempting to reconnect
            spoke_fencing_token: Fencing token the spoke last knew

        Returns:
            ReconnectionResult indicating success/failure
        """
        result = self.handle_spoke_reconnect(spoke_id, spoke_fencing_token)

        if result.accepted and self._on_spoke_connect_callback:
            try:
                await self._on_spoke_connect_callback(spoke_id)
            except Exception:
                logger.exception("Error in spoke connect callback")

        return result

    def handle_spoke_disconnect(self, spoke_id: str) -> None:
        """
        Handle a spoke disconnecting.

        Args:
            spoke_id: ID of the disconnected spoke
        """
        if spoke_id in self._connected_spokes:
            self._connected_spokes[spoke_id].is_connected = False
            logger.info("Spoke %s disconnected", spoke_id)

    async def handle_spoke_disconnect_async(self, spoke_id: str) -> None:
        """
        Async version of handle_spoke_disconnect with callbacks.

        Args:
            spoke_id: ID of the disconnected spoke
        """
        self.handle_spoke_disconnect(spoke_id)

        if self._on_spoke_disconnect_callback:
            try:
                await self._on_spoke_disconnect_callback(spoke_id)
            except Exception:
                logger.exception("Error in spoke disconnect callback")

    def get_connected_spokes(self) -> list[str]:
        """
        Get list of currently connected spoke IDs.

        Returns:
            List of connected spoke IDs
        """
        return [spoke_id for spoke_id, info in self._connected_spokes.items() if info.is_connected]

    def get_spoke_info(self, spoke_id: str) -> SpokeInfo | None:
        """
        Get information about a specific spoke.

        Args:
            spoke_id: Spoke ID to look up

        Returns:
            SpokeInfo or None if not found
        """
        return self._connected_spokes.get(spoke_id)

    def clear_stale_spokes(self, max_age_seconds: float = 300.0) -> list[str]:
        """
        Remove spokes that haven't been seen recently.

        Args:
            max_age_seconds: Maximum age before considering spoke stale

        Returns:
            List of removed spoke IDs
        """
        now = time.time()
        stale_spokes: list[str] = []

        for spoke_id, info in list(self._connected_spokes.items()):
            if now - info.last_seen > max_age_seconds:
                stale_spokes.append(spoke_id)
                del self._connected_spokes[spoke_id]
                logger.debug(
                    "Removed stale spoke %s (last seen %ss ago)",
                    spoke_id,
                    format(now - info.last_seen, ".1f"),
                )

        if stale_spokes:
            logger.info("Cleared %d stale spokes", len(stale_spokes))

        return stale_spokes


__all__ = [
    "ReconnectionResult",
    "SpokeInfo",
    "SpokeReconnectionHandler",
]
