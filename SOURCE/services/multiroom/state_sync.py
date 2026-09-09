"""
State Synchronization Manager for Multi-Room Playback.

Polls remote room state periodically and feeds updates into the local StateHub
so the UI and other components can display up-to-date information for all rooms.

Each room has its own async polling loop that runs as an independent asyncio.Task.
If a room becomes unreachable for longer than ``offline_timeout`` seconds, the
polling loop logs a warning and stops itself.

Usage:
    >>> from services.multiroom.state_sync import get_state_sync_manager
    >>>
    >>> sync_mgr = get_state_sync_manager()
    >>> sync_mgr.start_sync("living-room")
    >>> sync_mgr.is_syncing("living-room")  # True
    >>> sync_mgr.stop_sync("living-room")

Thread Safety:
    The singleton accessor ``get_state_sync_manager()`` is protected by a
    threading.Lock. The ``start_sync`` / ``stop_sync`` / ``stop_all`` methods
    are safe to call from any thread because they schedule work on the running
    asyncio event loop via ``asyncio.run_coroutine_threadsafe`` when necessary.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time

from core.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "StateSyncManager",
    "get_state_sync_manager",
    "reset_state_sync_manager",
]


class StateSyncManager:
    """
    Periodically polls remote rooms and dispatches state updates to StateHub.

    Args:
        poll_interval: Seconds between consecutive polls for each room.
        offline_timeout: Seconds of consecutive failures before a room is
            considered offline and its polling loop is stopped.
    """

    def __init__(
        self,
        poll_interval: float = 5.0,
        offline_timeout: float = 30.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0, got %r" % (poll_interval,))
        if offline_timeout <= 0:
            raise ValueError("offline_timeout must be > 0, got %r" % (offline_timeout,))
        self._poll_interval = poll_interval
        self._offline_timeout = offline_timeout
        self._max_consecutive_failures = max(1, math.ceil(offline_timeout / poll_interval))

        self._sync_tasks: dict[str, asyncio.Task[None]] = {}
        self._failure_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_sync(self, room_id: str) -> None:
        """
        Start an async polling loop for *room_id*.

        If a loop is already running for the room, the call is a no-op.
        The polling task is scheduled on the running asyncio event loop.
        The check-then-insert on ``_sync_tasks`` is atomic under ``_lock``:
        50 concurrent callers for the same room_id produce exactly one task.

        Args:
            room_id: Identifier of the remote room to sync.
        """
        with self._lock:
            existing = self._sync_tasks.get(room_id)
            if existing is not None and not existing.done():
                logger.debug("Sync already running for room %s", room_id)
                return

            # Drop a leftover done task before replacing so the dict never
            # carries a completed handle.
            if existing is not None:
                self._sync_tasks.pop(room_id, None)

            self._failure_counts[room_id] = 0
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                task = loop.create_task(
                    self._poll_room(room_id),
                    name=f"state-sync-{room_id}",
                )
            else:
                # No running loop — wrap in a future so it starts once a loop
                # is available.  This path is uncommon but keeps the API
                # resilient when called from a non-async context.
                logger.debug(
                    "No running event loop; sync for room %s will start when a loop is available",
                    room_id,
                )
                task = asyncio.ensure_future(self._poll_room(room_id))

            self._sync_tasks[room_id] = task
            logger.info("State sync started for room %s", room_id)

    def stop_sync(self, room_id: str) -> None:
        """
        Stop the polling loop for *room_id*.

        If no loop is running for the room the call is a no-op.

        Args:
            room_id: Identifier of the remote room to stop syncing.
        """
        with self._lock:
            task = self._sync_tasks.pop(room_id, None)
            self._failure_counts.pop(room_id, None)

        if task is not None and not task.done():
            task.cancel()
            logger.info("State sync stopped for room %s", room_id)

    def stop_all(self) -> None:
        """Stop all active polling loops."""
        with self._lock:
            tasks = dict(self._sync_tasks)
            self._sync_tasks.clear()
            self._failure_counts.clear()

        for room_id, task in tasks.items():
            if not task.done():
                task.cancel()
                logger.debug("Cancelled sync task for room %s", room_id)

        if tasks:
            logger.info("All state sync tasks stopped (%d rooms)", len(tasks))

    def is_syncing(self, room_id: str) -> bool:
        """
        Check whether a polling loop is active for *room_id*.

        Args:
            room_id: Room identifier to check.

        Returns:
            True if the room has a running sync task.
        """
        with self._lock:
            task = self._sync_tasks.get(room_id)
            return task is not None and not task.done()

    def get_syncing_rooms(self) -> list[str]:
        """
        Return a list of room IDs that currently have active sync loops.

        Returns:
            List of room ID strings.
        """
        with self._lock:
            return [room_id for room_id, task in self._sync_tasks.items() if not task.done()]

    # ------------------------------------------------------------------
    # Internal polling loop
    # ------------------------------------------------------------------

    async def _poll_room(self, room_id: str) -> None:
        """
        Async polling loop for a single room.

        Runs until cancelled, the room is stopped, or the room exceeds the
        maximum number of consecutive failures (offline timeout).
        """
        logger.info(
            "Sync polling loop started for room %s (interval=%.1fs, max_failures=%d)",
            room_id,
            self._poll_interval,
            self._max_consecutive_failures,
        )

        try:
            while True:
                await self._poll_once(room_id)

                # Check if we exceeded the failure threshold
                with self._lock:
                    failures = self._failure_counts.get(room_id, 0)

                if failures >= self._max_consecutive_failures:
                    logger.warning(
                        "Room %s appears offline after %d consecutive failures " "(%.0fs); stopping sync",
                        room_id,
                        failures,
                        failures * self._poll_interval,
                    )
                    # Clean up our own entry
                    with self._lock:
                        self._sync_tasks.pop(room_id, None)
                        self._failure_counts.pop(room_id, None)
                    return

                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("Sync polling loop cancelled for room %s", room_id)
        except Exception:
            logger.exception("Unexpected error in sync polling loop for room %s", room_id)
            with self._lock:
                self._sync_tasks.pop(room_id, None)
                self._failure_counts.pop(room_id, None)

    async def _poll_once(self, room_id: str) -> None:
        """Execute a single poll cycle for *room_id*."""
        try:
            from services.multiroom.room_connections import get_room_connection_manager

            conn_mgr = get_room_connection_manager()
            client = conn_mgr.get_client(room_id)

            if client is None:
                logger.debug("No connection registered for room %s; skipping poll", room_id)
                self._record_failure(room_id)
                return

            response = await client.get_state()

            if response is None:
                logger.debug("Poll returned None for room %s", room_id)
                self._record_failure(room_id)
                return

            # Extract state data — handle both raw dict and ResponseEnvelope
            state_data = response
            if isinstance(response.get("data"), dict):
                state_data = response["data"]

            # Convert dict to PlayerState model
            from models.player import PlayerState

            try:
                player_state = PlayerState(**state_data)
            except Exception:
                logger.warning(
                    "Failed to parse PlayerState for room %s; skipping update",
                    room_id,
                )
                # Still counts as a reachable response, so reset failures
                self._reset_failures(room_id)
                return

            # Dispatch to StateHub
            from core.state_hub import SyncRoomState, get_state_hub

            hub = get_state_hub()
            hub.dispatch(
                SyncRoomState(
                    room_id=room_id,
                    player_state=player_state,
                    sync_time=time.time(),
                )
            )

            self._reset_failures(room_id)
            logger.info(
                "State synced for room %s (sync_time=%.1f)",
                room_id,
                time.time(),
            )

        except Exception:
            logger.exception("Poll failure for room %s", room_id)
            self._record_failure(room_id)

    # ------------------------------------------------------------------
    # Failure tracking helpers
    # ------------------------------------------------------------------

    def _record_failure(self, room_id: str) -> None:
        """Increment the consecutive failure counter for *room_id*."""
        with self._lock:
            self._failure_counts[room_id] = self._failure_counts.get(room_id, 0) + 1

    def _reset_failures(self, room_id: str) -> None:
        """Reset the consecutive failure counter for *room_id*."""
        with self._lock:
            self._failure_counts[room_id] = 0


# =============================================================================
# Module-level singleton
# =============================================================================

_sync_manager: StateSyncManager | None = None
_sync_lock = threading.Lock()


def get_state_sync_manager() -> StateSyncManager:
    """
    Get or create the global StateSyncManager singleton.

    Returns:
        The shared StateSyncManager instance.
    """
    global _sync_manager

    with _sync_lock:
        if _sync_manager is None:
            _sync_manager = StateSyncManager()
        return _sync_manager


def reset_state_sync_manager() -> None:
    """
    Reset the global StateSyncManager singleton.

    Stops all active sync loops and clears the singleton. Intended for
    use in tests or during application shutdown.
    """
    global _sync_manager

    with _sync_lock:
        if _sync_manager is not None:
            _sync_manager.stop_all()
            _sync_manager = None
