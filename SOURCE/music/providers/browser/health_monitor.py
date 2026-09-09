"""
music.providers.browser.health_monitor
---------------------------------------

Background health monitor for browser playback sessions.

Periodically checks whether the browser playback is healthy by verifying:

- Audio is still playing (position is advancing)
- The page is responsive (JS injection returns results)
- No ads are currently playing
- The session has not expired

Reports health via logger.  Does NOT use the event bus -- keeping it
simple and self-contained.

Usage::

    monitor = BrowserHealthMonitor(controller, error_handler)
    await monitor.start()
    # ... later ...
    await monitor.stop()
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger

if TYPE_CHECKING:
    from music.providers.browser.error_handler import BrowserErrorHandler
    from music.providers.browser.provider import BrowserPlaybackController

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Health status
# ---------------------------------------------------------------------------


class HealthStatus(StrEnum):
    """Health status for the browser playback session."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthSnapshot:
    """A point-in-time health reading.

    Attributes:
        status: Overall health status.
        is_playing: Whether the controller reports playing state.
        position_seconds: Current playback position (from last poll).
        position_advancing: ``True`` if position changed since last check.
        page_responsive: ``True`` if JS injection returned a result.
        ad_detected: ``True`` if an ad is currently playing.
        consecutive_unresponsive: Number of consecutive checks where the
            page did not respond to JS.
        timestamp: Monotonic timestamp of this reading.
    """

    status: HealthStatus = HealthStatus.UNKNOWN
    is_playing: bool = False
    position_seconds: float = 0.0
    position_advancing: bool = True
    page_responsive: bool = True
    ad_detected: bool = False
    consecutive_unresponsive: int = 0
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default health check interval in milliseconds
_DEFAULT_CHECK_INTERVAL_MS = 5000

# If position has not changed for this many seconds while playing,
# it is considered a stall.
_STALL_THRESHOLD_SECONDS = 10.0

# Number of consecutive unresponsive checks before declaring unhealthy
_UNRESPONSIVE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------


class BrowserHealthMonitor:
    """Monitors browser playback health and triggers recovery.

    Runs an asyncio background task that periodically checks the
    playback controller state.  When problems are detected, it
    delegates to the :class:`BrowserErrorHandler` for recovery.

    The monitor is designed to be started and stopped alongside the
    playback session.  It is safe to call :meth:`start` multiple times
    (subsequent calls are no-ops while running).
    """

    def __init__(
        self,
        controller: BrowserPlaybackController | None = None,
        error_handler: BrowserErrorHandler | None = None,
        check_interval_ms: int = _DEFAULT_CHECK_INTERVAL_MS,
    ) -> None:
        self._controller = controller
        self._error_handler = error_handler
        self._check_interval_ms = max(1000, check_interval_ms)

        # Internal state
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._last_position: float = 0.0
        self._last_position_time: float = 0.0
        self._consecutive_unresponsive: int = 0
        self._last_health: HealthSnapshot = HealthSnapshot()

        logger.info(
            "BrowserHealthMonitor initialized (interval=%d ms)",
            self._check_interval_ms,
        )

    def set_controller(self, controller: BrowserPlaybackController) -> None:
        """Attach or replace the playback controller.

        Args:
            controller: The BrowserPlaybackController instance.
        """
        self._controller = controller

    def set_error_handler(self, error_handler: BrowserErrorHandler) -> None:
        """Attach or replace the error handler.

        Args:
            error_handler: The BrowserErrorHandler instance.
        """
        self._error_handler = error_handler

    @property
    def last_health(self) -> HealthSnapshot:
        """Return the most recent health snapshot."""
        return self._last_health

    @property
    def is_running(self) -> bool:
        """Whether the health monitor background task is active."""
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background health monitoring task.

        If already running, this is a no-op.
        """
        if self._running:
            logger.debug("BrowserHealthMonitor already running")
            return

        self._running = True
        self._last_position = 0.0
        self._last_position_time = time.monotonic()
        self._consecutive_unresponsive = 0

        self._task = asyncio.create_task(
            self._monitor_loop(),
            name="BrowserHealthMonitor",
        )
        logger.info("BrowserHealthMonitor started")

    async def stop(self) -> None:
        """Stop the background health monitoring task.

        Waits for the current check cycle to finish (up to
        ``TIMEOUT_EXTENDED`` seconds) before returning.
        """
        if not self._running:
            return

        self._running = False

        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=TIMEOUT_EXTENDED)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._task = None

        logger.info("BrowserHealthMonitor stopped")

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Main monitoring loop.  Runs until :meth:`stop` is called."""
        interval_s = self._check_interval_ms / 1000.0

        while self._running:
            try:
                snapshot = self._check_health()
                self._last_health = snapshot
                self._log_health(snapshot)
                self._react_to_health(snapshot)
            except Exception:
                logger.exception("BrowserHealthMonitor check failed")

            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                break

        logger.debug("BrowserHealthMonitor loop exited")

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _check_health(self) -> HealthSnapshot:
        """Perform a single health check and return a snapshot.

        Reads the controller's cached state (does not inject JS itself --
        that is handled by the metadata polling in the controller).
        """
        now = time.monotonic()
        snapshot = HealthSnapshot(timestamp=now)

        if self._controller is None:
            snapshot.status = HealthStatus.UNKNOWN
            return snapshot

        # --- Playback state ---
        playback_state = self._controller.get_playback_state()
        snapshot.is_playing = playback_state == "playing"

        # --- Position tracking for stall detection ---
        metadata = self._controller.get_metadata()
        if metadata is not None:
            snapshot.page_responsive = True
            self._consecutive_unresponsive = 0
            snapshot.consecutive_unresponsive = 0

            position = metadata.get("position_seconds", 0.0) or 0.0
            snapshot.position_seconds = position

            if snapshot.is_playing:
                if abs(position - self._last_position) > 0.1:
                    # Position is advancing -- healthy
                    snapshot.position_advancing = True
                    self._last_position = position
                    self._last_position_time = now
                else:
                    # Position has not changed
                    stall_duration = now - self._last_position_time
                    if stall_duration > _STALL_THRESHOLD_SECONDS:
                        snapshot.position_advancing = False
                    else:
                        snapshot.position_advancing = True
            else:
                # Not playing -- position stagnation is expected
                snapshot.position_advancing = True
                self._last_position = position
                self._last_position_time = now
        else:
            # No metadata returned -- page may be unresponsive
            self._consecutive_unresponsive += 1
            snapshot.page_responsive = self._consecutive_unresponsive < _UNRESPONSIVE_THRESHOLD
            snapshot.consecutive_unresponsive = self._consecutive_unresponsive

        # --- Determine overall status ---
        if not snapshot.page_responsive:
            snapshot.status = HealthStatus.UNHEALTHY
        elif (snapshot.is_playing and not snapshot.position_advancing) or snapshot.ad_detected:
            snapshot.status = HealthStatus.DEGRADED
        else:
            snapshot.status = HealthStatus.HEALTHY

        return snapshot

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def _react_to_health(self, snapshot: HealthSnapshot) -> None:
        """Take corrective action based on health snapshot.

        Delegates to the error handler when issues are detected.
        """
        if self._error_handler is None:
            return

        if snapshot.status == HealthStatus.UNHEALTHY:
            if snapshot.consecutive_unresponsive >= _UNRESPONSIVE_THRESHOLD:
                logger.warning(
                    "BrowserHealthMonitor page unresponsive for %d consecutive checks",
                    snapshot.consecutive_unresponsive,
                )
                # Page may have crashed -- trigger navigation error handling
                self._error_handler.handle_error(
                    self._error_handler.NAVIGATION_ERROR,
                    {"reason": "page_unresponsive"},
                )
            return

        if snapshot.status == HealthStatus.DEGRADED:
            if snapshot.is_playing and not snapshot.position_advancing:
                logger.warning(
                    "BrowserHealthMonitor playback stall detected " "(position=%.1f, stalled for %.1f s)",
                    snapshot.position_seconds,
                    time.monotonic() - self._last_position_time,
                )
                action = self._error_handler.handle_error(
                    self._error_handler.PLAYBACK_STALL,
                    {"url": getattr(self._controller, "_current_url", "")},
                )
                if action.recovered:
                    # Give the recovery a chance -- reset stall timer
                    self._last_position_time = time.monotonic()
            return

        # Healthy -- reset error handler stall counter
        if snapshot.status == HealthStatus.HEALTHY:
            self._error_handler.reset_stall_counter()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_health(self, snapshot: HealthSnapshot) -> None:
        """Log the health snapshot at an appropriate level."""
        if snapshot.status == HealthStatus.HEALTHY:
            logger.debug(
                "BrowserHealthMonitor health=%s playing=%s pos=%.1f",
                snapshot.status,
                snapshot.is_playing,
                snapshot.position_seconds,
            )
        elif snapshot.status == HealthStatus.DEGRADED:
            logger.warning(
                "BrowserHealthMonitor health=%s playing=%s pos=%.1f advancing=%s ad=%s",
                snapshot.status,
                snapshot.is_playing,
                snapshot.position_seconds,
                snapshot.position_advancing,
                snapshot.ad_detected,
            )
        else:
            logger.error(
                "BrowserHealthMonitor health=%s responsive=%s consecutive_unresponsive=%d",
                snapshot.status,
                snapshot.page_responsive,
                snapshot.consecutive_unresponsive,
            )


__all__ = [
    "BrowserHealthMonitor",
    "HealthSnapshot",
    "HealthStatus",
]
