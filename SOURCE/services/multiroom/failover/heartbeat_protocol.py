"""
Hub Failover System - Heartbeat Protocol

Implements the heartbeat protocol for hub health monitoring and failover detection.

Heartbeats are sent at 1Hz frequency. After 3 missed heartbeats (3 seconds),
the hub is considered timed out and failover procedures begin.

Usage:
    from services.multiroom.failover.heartbeat_protocol import HubHeartbeatProtocol

    protocol = HubHeartbeatProtocol(
        hub_id="hub-1",
        on_heartbeat=handle_heartbeat,
        on_timeout=handle_timeout,
    )
    await protocol.start()
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger

from .types import HubHeartbeat, HubRole

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


# Heartbeat frequency and timeout constants
HEARTBEAT_INTERVAL_SECONDS = 1.0  # 1Hz heartbeat frequency
HEARTBEAT_TIMEOUT_MULTIPLIER = 3  # Number of missed heartbeats before timeout
HEARTBEAT_TIMEOUT_SECONDS = HEARTBEAT_INTERVAL_SECONDS * HEARTBEAT_TIMEOUT_MULTIPLIER  # 3 seconds


class HubHeartbeatProtocol:
    """
    Manages heartbeat sending and receiving for hub failover.

    The protocol sends heartbeats at 1Hz and monitors for missed heartbeats
    from the partner hub. After 3 missed heartbeats (3 seconds), the
    on_timeout callback is invoked to initiate failover.

    Attributes:
        hub_id: This hub's identifier
        heartbeat_interval: Interval between heartbeats (default 1.0s)
        timeout_seconds: Timeout threshold (default 3.0s)
    """

    def __init__(
        self,
        hub_id: str,
        on_heartbeat: Callable[[HubHeartbeat], Awaitable[None]] | None = None,
        on_timeout: Callable[[], Awaitable[None]] | None = None,
        fencing_token: int = 0,
        state_version: int = 0,
        role: HubRole = HubRole.SECONDARY,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Initialize the heartbeat protocol.

        Args:
            hub_id: This hub's unique identifier
            on_heartbeat: Async callback when heartbeat is received
            on_timeout: Async callback when partner times out
            fencing_token: Initial fencing token
            state_version: Initial state version
            role: Initial hub role
            heartbeat_interval: Interval between heartbeats
            timeout_seconds: Timeout threshold for missed heartbeats
        """
        self.hub_id = hub_id
        self._on_heartbeat = on_heartbeat
        self._on_timeout = on_timeout
        self._fencing_token = fencing_token
        self._state_version = state_version
        self._role = role
        self.heartbeat_interval = heartbeat_interval
        self.timeout_seconds = timeout_seconds

        self._running = False
        self._send_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._last_received_time: float = 0.0
        self._send_callback: Callable[[HubHeartbeat], Awaitable[None]] | None = None

        logger.debug(
            "HubHeartbeatProtocol initialized for hub %s (interval=%ss, timeout=%ss)",
            hub_id,
            heartbeat_interval,
            timeout_seconds,
        )

    def set_send_callback(
        self,
        callback: Callable[[HubHeartbeat], Awaitable[None]],
    ) -> None:
        """
        Set the callback for sending heartbeats to the partner hub.

        Args:
            callback: Async function to send heartbeat to partner
        """
        self._send_callback = callback

    def update_state(
        self,
        fencing_token: int | None = None,
        state_version: int | None = None,
        role: HubRole | None = None,
    ) -> None:
        """
        Update the state included in heartbeats.

        Args:
            fencing_token: New fencing token (optional)
            state_version: New state version (optional)
            role: New hub role (optional)
        """
        if fencing_token is not None:
            self._fencing_token = fencing_token
        if state_version is not None:
            self._state_version = state_version
        if role is not None:
            self._role = role

    async def start(self) -> None:
        """
        Start the heartbeat protocol.

        Begins sending heartbeats and monitoring for partner timeouts.
        """
        if self._running:
            logger.warning("Heartbeat protocol already running for hub %s", self.hub_id)
            return

        self._running = True
        self._last_received_time = time.time()

        # Start heartbeat sender
        self._send_task = asyncio.create_task(
            self._send_loop(),
            name=f"heartbeat-send-{self.hub_id}",
        )
        self._send_task.add_done_callback(_log_task_exception)

        # Start timeout monitor
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name=f"heartbeat-monitor-{self.hub_id}",
        )
        self._monitor_task.add_done_callback(_log_task_exception)

        logger.info("Heartbeat protocol started for hub %s", self.hub_id)

    async def stop(self) -> None:
        """
        Stop the heartbeat protocol.

        Cancels all running tasks and cleans up.
        """
        if not self._running:
            return

        self._running = False

        # Cancel tasks
        if self._send_task:
            self._send_task.cancel()
            try:
                await asyncio.wait_for(self._send_task, timeout=TIMEOUT_DEFAULT)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._send_task = None

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await asyncio.wait_for(self._monitor_task, timeout=TIMEOUT_DEFAULT)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._monitor_task = None

        logger.info("Heartbeat protocol stopped for hub %s", self.hub_id)

    async def send_heartbeat(self) -> None:
        """
        Send a single heartbeat to the partner hub.

        Called automatically by the send loop, but can also be called
        manually for immediate heartbeat.
        """
        if not self._send_callback:
            logger.debug("No send callback configured, skipping heartbeat")
            return

        heartbeat = HubHeartbeat(
            hub_id=self.hub_id,
            fencing_token=self._fencing_token,
            state_version=self._state_version,
            hub_time=time.time(),
            role="primary" if self._role == HubRole.PRIMARY else "secondary",
        )

        try:
            await self._send_callback(heartbeat)
            logger.debug(
                "Sent heartbeat: hub=%s token=%d version=%d",
                self.hub_id,
                self._fencing_token,
                self._state_version,
            )
        except Exception:
            logger.exception("Failed to send heartbeat")

    async def on_heartbeat_received(self, heartbeat: HubHeartbeat) -> None:
        """
        Handle a received heartbeat from the partner hub.

        Updates the last received time and invokes the callback if configured.

        Args:
            heartbeat: Received heartbeat message
        """
        self._last_received_time = time.time()

        logger.debug(
            "Received heartbeat: hub=%s token=%d version=%d role=%s",
            heartbeat.hub_id,
            heartbeat.fencing_token,
            heartbeat.state_version,
            heartbeat.role,
        )

        if self._on_heartbeat:
            try:
                await self._on_heartbeat(heartbeat)
            except Exception:
                logger.exception("Error in heartbeat callback")

    def get_time_since_last_heartbeat(self) -> float:
        """
        Get time elapsed since last heartbeat was received.

        Returns:
            Seconds since last heartbeat
        """
        if self._last_received_time == 0.0:
            return float("inf")
        return time.time() - self._last_received_time

    def is_partner_healthy(self) -> bool:
        """
        Check if partner hub is considered healthy.

        Returns:
            True if last heartbeat was within timeout threshold
        """
        return self.get_time_since_last_heartbeat() < self.timeout_seconds

    async def _send_loop(self) -> None:
        """Internal loop for sending heartbeats at regular intervals."""
        while self._running:
            try:
                await self.send_heartbeat()
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat send loop")
                await asyncio.sleep(self.heartbeat_interval)

    async def _monitor_loop(self) -> None:
        """Internal loop for monitoring partner heartbeat timeout."""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)

                time_since = self.get_time_since_last_heartbeat()
                if time_since > self.timeout_seconds:
                    logger.warning(
                        "Partner hub timeout: %ss since last heartbeat (threshold: %ss)",
                        format(time_since, ".1f"),
                        self.timeout_seconds,
                    )
                    await self._on_timeout_triggered()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in heartbeat monitor loop")

    async def _on_timeout_triggered(self) -> None:
        """Handle partner timeout event."""
        if self._on_timeout:
            try:
                await self._on_timeout()
            except Exception:
                logger.exception("Error in timeout callback")


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TIMEOUT_MULTIPLIER",
    "HEARTBEAT_TIMEOUT_SECONDS",
    "HubHeartbeatProtocol",
]
