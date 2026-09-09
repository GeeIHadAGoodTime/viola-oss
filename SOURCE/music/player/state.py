"""
Music Player State Management.

This module contains state and queue management operations including:
- Player state tracking and reporting
- Queue operations (add, remove, reorder)
- Artwork handling

Consolidated from:
- music_player_state_management.py
- music_player_queue.py
- music_player_artwork.py
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Mapping

from core.json_types import to_json_value
from core.logging_config import get_logger
from models.player import QueueItem

logger = get_logger(__name__)


# ============================================================================
# State Manager
# ============================================================================


class MusicPlayerStateManager:
    """Handles state management and monitoring for the music player."""

    def __init__(self, player_instance):
        """
        Initialize state manager.

        Args:
            player_instance: The MusicPlayer instance
        """
        self.player = player_instance

    def state(self) -> object:
        """
        Get current player state.

        Returns:
            PlayerState object
        """
        with self.player._lock:
            try:
                from models.player import PlayerState

                backend_capabilities: dict[str, object] = {}
                backend = getattr(self.player, "_backend", None)
                if backend is not None:
                    caps_func = getattr(backend, "capabilities", None)
                    if callable(caps_func):
                        try:
                            caps = caps_func()
                            if dataclasses.is_dataclass(caps) and not isinstance(caps, type):
                                backend_capabilities = dataclasses.asdict(caps)
                            elif isinstance(caps, Mapping):
                                backend_capabilities = dict(caps)
                        except Exception as exc:
                            logger.exception("Failed to read backend capabilities: %s", exc)

                playback_capabilities: dict[str, object] = dict(backend_capabilities)
                current_item = self.player._state.now_playing
                if isinstance(current_item, QueueItem):
                    for key, value in current_item.capabilities.items():
                        playback_capabilities[key] = value

                backend_capabilities_value = to_json_value(backend_capabilities)
                backend_capabilities_json = (
                    backend_capabilities_value if isinstance(backend_capabilities_value, dict) else {}
                )
                playback_capabilities_value = to_json_value(playback_capabilities)
                playback_capabilities_json = (
                    playback_capabilities_value if isinstance(playback_capabilities_value, dict) else {}
                )

                # Build current state
                # Note: PlayerState uses 'now_playing' field, not 'current_item'
                # QUEUE ARCHITECTURE: Read from canonical source (PlaylistQueueEngine)
                # instead of the deprecated _queue attribute
                has_playlist = hasattr(self.player, "_playlist")
                playlist_not_none = self.player._playlist is not None if has_playlist else False
                if has_playlist and playlist_not_none:
                    canonical_queue = self.player._playlist.upcoming()
                else:
                    canonical_queue = list(self.player._queue)  # Fallback for test mode
                # State machine is the single source of truth for playback phase.
                # Fallback to legacy formula only if _playback_sm is missing.
                _sm = getattr(self.player, "_playback_sm", None)
                if _sm is not None:
                    _is_playing = _sm.is_playing
                else:
                    _is_playing = self.player._is_playing and not self.player._paused

                _pos_ms = self.player._position_ms or 0
                _dur_ms = self._get_current_duration() or 0
                state = PlayerState(
                    is_playing=_is_playing,
                    now_playing=self.player._state.now_playing,
                    queue=canonical_queue,
                    volume=self.player._volume,
                    position=int(_pos_ms / 1000),
                    position_ms=int(_pos_ms),
                    duration=int(_dur_ms / 1000) if _dur_ms else 0,
                    position_percentage=(_pos_ms / _dur_ms) if _dur_ms > 0 else 0.0,
                    backend=self._get_backend_type(),
                    backend_display_name=self._get_backend_type(),
                    backend_capabilities=backend_capabilities_json,
                    playback_capabilities=playback_capabilities_json,
                    playback_mode=(getattr(current_item, "playback_mode", None) or self.player._state.playback_mode),
                )

                return state

            except Exception as e:
                logger.exception("Failed to build player state: %s", e)
                # Return minimal state on error
                from models.player import PlayerState

                return PlayerState(
                    is_playing=False,
                    now_playing=None,
                    queue=[],
                    volume=50,
                    position=0,
                    duration=0,
                    backend="unknown",
                    backend_display_name="unknown",
                    backend_capabilities={},
                    playback_capabilities={},
                )

    def status(self) -> dict[str, object]:
        """
        Get detailed player status.

        Returns:
            Status dictionary
        """
        with self.player._lock:
            try:
                _sm = getattr(self.player, "_playback_sm", None)
                status = {
                    "is_playing": (
                        _sm.is_playing if _sm is not None else (self.player._is_playing and not self.player._paused)
                    ),
                    "is_paused": (_sm.is_paused if _sm is not None else self.player._paused),
                    "current_item": self.player._state.now_playing,
                    "current_url": self.player._current_url,
                    "queue_size": len(self.player._queue),
                    "volume": self.player._volume,
                    "position_ms": self.player._position_ms,
                    "duration_ms": self._get_current_duration(),
                    "backend_type": self._get_backend_type(),
                    "worker_heartbeat": (
                        self.player._worker_heartbeat if hasattr(self.player, "_worker_heartbeat") else 0
                    ),
                    "last_resolution_error": self._get_last_resolution_error(),
                    "resolution_failure_count": len(self.player._resolution_failures),
                }

                # Add queue summary (first few items)
                if self.player._queue:
                    status["queue_items"] = [
                        {
                            "id": getattr(item, "id", "unknown"),
                            "title": getattr(item, "title", "Unknown"),
                            "artist": getattr(item, "artist", "Unknown"),
                        }
                        for item in self.player._queue[:5]
                    ]

                return status

            except Exception as e:
                logger.exception("Failed to build player status: %s", e)
                return {"is_playing": False, "error": str(e), "timestamp": time.time()}

    def wait_for_condition(
        self,
        condition: Callable[[], bool],
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> bool:
        """
        Wait for a condition to become true.

        Args:
            condition: Function that returns True when condition is met
            timeout: Maximum time to wait
            poll_interval: How often to check condition

        Returns:
            True if condition met, False if timeout
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            with self.player._lock:
                if condition():
                    return True

            time.sleep(poll_interval)

        return False

    def worker_heartbeat(self) -> int:
        """
        Get worker heartbeat counter.

        Returns:
            Heartbeat counter value
        """
        return getattr(self.player, "_worker_heartbeat", 0)

    def wait_for_worker_heartbeat(self, last_seen: int, timeout: float = 0.5) -> bool:
        """
        Wait for worker heartbeat to change.

        Args:
            last_seen: Last heartbeat value seen
            timeout: Maximum time to wait

        Returns:
            True if heartbeat changed, False if timeout
        """

        def heartbeat_changed():
            return self.player._worker_heartbeat != last_seen

        return self.wait_for_condition(heartbeat_changed, timeout=timeout, poll_interval=0.05)

    def last_resolution_error(self) -> dict[str, object] | None:
        """
        Get the last resolution error.

        Returns:
            Error details or None
        """
        with self.player._lock:
            if self.player._resolution_failures:
                return self.player._resolution_failures[-1]
            return None

    def resolution_failure_history(self) -> list[dict[str, object]]:
        """
        Get resolution failure history.

        Returns:
            List of failure records
        """
        with self.player._lock:
            return list(self.player._resolution_failures)

    def record_resolution_failure(self, error: Exception, context: dict[str, object] | None = None) -> None:
        """
        Record a resolution failure.

        Args:
            error: The exception that occurred
            context: Additional context information
        """
        failure_record = {
            "timestamp": time.time(),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context or {},
        }

        with self.player._lock:
            self.player._resolution_failures.append(failure_record)

            # Keep only last 50 failures
            if len(self.player._resolution_failures) > 50:
                self.player._resolution_failures = self.player._resolution_failures[-50:]

    def _get_current_duration(self) -> int | None:
        """Get current track duration.

        Priority order:
        1. _duration_ms attribute (set by WebSocket handler for iframe backends)
        2. Backend's current_duration_ms() method (for backends that track duration internally)
        """
        try:
            # First check _duration_ms (set by WebSocket handler for iframe playback)
            duration_ms = getattr(self.player, "_duration_ms", 0)
            if duration_ms and duration_ms > 0:
                return duration_ms

            # Fallback to backend's duration method
            if self.player._backend:
                return self.player._backend.current_duration_ms()
            return None
        except Exception as e:
            logger.exception("Failed to get current duration: %s", e)
            return None

    def _get_backend_type(self) -> str:
        """Get current backend type."""
        try:
            backend_name = getattr(self.player, "_backend_name", None)
            if isinstance(backend_name, str) and backend_name:
                return backend_name
            if self.player._backend:
                return type(self.player._backend).__name__
            return "none"
        except Exception as e:
            logger.exception("Failed to get backend type: %s", e)
            return "unknown"

    def _get_last_error(self) -> str | None:
        """Get last error message."""
        try:
            # Check for recent resolution failures
            if self.player._resolution_failures:
                latest = self.player._resolution_failures[-1]
                return latest.get("error_message")

            return None
        except Exception as e:
            logger.exception("Failed to get last error: %s", e)
            return None

    def _get_last_resolution_error(self) -> dict[str, object] | None:
        """Get last resolution error with full details."""
        try:
            if self.player._resolution_failures:
                return self.player._resolution_failures[-1]
            return None
        except Exception as e:
            logger.exception("Failed to get last resolution error: %s", e)
            return None

    def handle_backend_progress(self, progress: object) -> None:
        """
        Handle progress updates from the backend.

        Args:
            progress: Progress information from backend
        """
        try:
            # Extract progress information
            position_ms = getattr(progress, "position_ms", 0)
            duration_ms = getattr(progress, "duration_ms", 0)
            is_playing = getattr(progress, "is_playing", True)

            # Update player state
            with self.player._lock:
                self.player._position_ms = position_ms
                if duration_ms > 0:
                    self.player._duration_ms = duration_ms

                # Update state object if it exists
                if hasattr(self.player, "_state"):
                    self.player._state.position = position_ms
                    self.player._state.duration = duration_ms
                    self.player._state.is_playing = is_playing

            # Emit progress event
            if hasattr(self.player, "_emit_progress"):
                self.player._emit_progress(progress)

            # Handle position cache for state() method optimization
            current_time = time.time()
            if hasattr(self.player, "_position_cache_time"):
                cache_age = current_time - self.player._position_cache_time
                if cache_age > 1.0:  # Update cache every second
                    if hasattr(self.player, "_position_cache"):
                        self.player._position_cache = (position_ms, duration_ms, 0.0)
                        self.player._position_cache_time = current_time

            # Handle track completion detection
            if duration_ms > 0 and position_ms >= duration_ms - 1000:  # Within 1 second of end
                try:
                    if hasattr(self.player, "_handle_track_completion"):
                        self.player._handle_track_completion()
                except Exception as e:
                    logger.debug("Error handling track completion: %s", e)

            # Update metrics
            try:
                if hasattr(self.player, "_metrics_recorder"):
                    self.player._metrics_recorder.record_progress(position_ms=position_ms, duration_ms=duration_ms)
            except Exception as e:
                logger.debug("Error updating progress metrics: %s", e)

        except Exception as e:
            logger.exception("Error handling backend progress: %s", e)

    def ensure_playlist_coherent_locked(self) -> None:
        """
        Ensure playlist coherence in test mode.
        This method maintains consistency between the playlist state and the player state.
        """
        if not self.player._test_mode:
            return

        current = self.player._state.now_playing if isinstance(self.player._state.now_playing, QueueItem) else None

        upcoming = list(self.player._queue)

        # Update history reference if needed
        if hasattr(self.player, "_history") and hasattr(self.player._state_pipeline, "replace_history"):
            if self.player._history is not self.player._playlist.history_ref:
                self.player._history = self.player._state_pipeline.replace_history(self.player._history)

        # Force current item and replace upcoming
        upcoming_list = list(upcoming)
        self.player._playlist.force_current(current, pending=current is not None)
        self.player._playlist.replace_upcoming(upcoming_list)


# ============================================================================
# Queue Manager
# ============================================================================


class MusicPlayerQueueManager:
    """Handles queue operations for the music player.

    ARCHITECTURE NOTE (2026-01-12):
    Queue reads now go directly to PlaylistCursor._upcoming via the
    player._queue property. This eliminates the need for sync calls
    and ensures a single source of truth.

    The _locked mutation methods below are DEPRECATED - they were never
    called from the new code paths. All queue mutations go through
    MusicPlaybackController → PlaylistCursor.
    """

    def __init__(self, player_instance):
        """
        Initialize queue manager.

        Args:
            player_instance: The MusicPlayer instance
        """
        self.player = player_instance

    def get_queue(self) -> list[QueueItem]:
        """Get current queue items from canonical source."""
        # _queue is a computed property reading from PlaylistCursor._upcoming
        return list(self.player._queue)

    def get_queue_size(self) -> int:
        """Get queue size from canonical source."""
        # _queue is a computed property reading from PlaylistCursor._upcoming
        return len(self.player._queue)

    # ------------------------------------------------------------------ #
    # DEPRECATED METHODS (2026-01-12)                                     #
    # These methods are never called from the active code paths.          #
    # All queue mutations go through control_surface.py → _controller     #
    # → MusicPlaybackController → PlaylistCursor                          #
    # ------------------------------------------------------------------ #

    def clear_queue_locked(self) -> None:
        """DEPRECATED: Use control_surface.clear_queue() instead."""
        logger.warning("DEPRECATED: clear_queue_locked() called - use control_surface.clear_queue()")
        self.player._queue.clear()
        self.player._queue_index = -1
        # Queue mutations auto-invalidate - no sync needed
        self.player._emit()

    def remove_from_queue_locked(self, item_id: str) -> bool:
        """
        Remove item from queue by ID (must be called with state lock held).

        Args:
            item_id: Item ID to remove

        Returns:
            True if item was removed, False if not found
        """
        for i, item in enumerate(self.player._queue):
            if item.id == item_id:
                removed_item = self.player._queue.pop(i)

                # Adjust current index if needed
                if self.player._queue_index > i:
                    self.player._queue_index -= 1
                elif self.player._queue_index == i:
                    # Currently playing item was removed
                    self.player._queue_index = -1
                    self.player._set_now_playing_locked(None)

                # Queue mutations auto-invalidate - no sync needed
                self.player._emit()
                logger.info("Removed from queue: %s", removed_item.title)
                return True

        return False

    def reorder_queue_locked(self, from_index: int, to_index: int) -> bool:
        """
        Reorder queue item (must be called with state lock held).

        Args:
            from_index: Source index
            to_index: Destination index

        Returns:
            True if reorder succeeded, False otherwise
        """
        if not (0 <= from_index < len(self.player._queue)):
            return False
        if not (0 <= to_index < len(self.player._queue)):
            return False

        item = self.player._queue.pop(from_index)
        self.player._queue.insert(to_index, item)

        # Adjust current index if affected
        if self.player._queue_index == from_index:
            self.player._queue_index = to_index
        elif from_index < self.player._queue_index <= to_index:
            self.player._queue_index -= 1
        elif to_index <= self.player._queue_index < from_index:
            self.player._queue_index += 1

        # Queue mutations auto-invalidate - no sync needed
        self.player._emit()
        logger.info("Reordered queue: %s moved to position %s", item.title, to_index)
        return True

    def play_item_now_locked(self, item_id: str) -> bool:
        """
        Play specific item immediately (must be called with state lock held).

        Args:
            item_id: Item ID to play now

        Returns:
            True if item was found and queued to play next, False otherwise
        """
        for i, item in enumerate(self.player._queue):
            if item.id == item_id:
                if i == self.player._queue_index:
                    # Already playing this item
                    return True

                # Move to front of queue
                self.player._queue.pop(i)
                self.player._queue.insert(self.player._queue_index + 1, item)

                # Queue mutations auto-invalidate - no sync needed
                self.player._emit()
                logger.info("Moved to play next: %s", item.title)

                # Skip to it
                self.player.skip()
                return True

        return False

    def enqueue_resolved_item_locked(self, item: QueueItem, position: str = "end") -> None:
        """
        Enqueue a resolved item (must be called with state lock held).

        Args:
            item: Resolved queue item
            position: Where to insert ("end", "next", or "now")
        """
        if position == "now":
            # Insert at current position + 1
            insert_pos = self.player._queue_index + 1
            self.player._queue.insert(insert_pos, item)
            # Queue mutations auto-invalidate - no sync needed
            self.player._emit()
            logger.info("Enqueued now: %s", item.title)

        elif position == "next":
            # Insert after current track
            insert_pos = self.player._queue_index + 1
            self.player._queue.insert(insert_pos, item)
            # Queue mutations auto-invalidate - no sync needed
            self.player._emit()
            logger.info("Enqueued next: %s", item.title)

        else:  # "end"
            # Add to end
            self.player._queue.append(item)
            # Queue mutations auto-invalidate - no sync needed
            self.player._emit()
            logger.info("Enqueued: %s", item.title)

    def get_last_queue_error(self) -> dict[str, object] | None:
        """Get the last queue operation error."""
        return getattr(self.player, "_last_queue_error", None)

    def get_queue_backpressure_notice(self) -> dict[str, object] | None:
        """Get queue backpressure notice if any."""
        return getattr(self.player, "_queue_backpressure_notice", None)


# ============================================================================
# Artwork Handler
# ============================================================================


class MusicPlayerArtworkHandler:
    """Handles artwork updates from music providers."""

    def __init__(self, player):
        """Initialize the artwork handler with a reference to the player."""
        self.player = player

    def on_provider_artwork(self, *args, **_kwargs) -> None:
        """
        Callback for provider/engine artwork updates (optional test hook).

        Args:
            *args: Variable arguments containing artwork URL
            **_kwargs: Additional keyword arguments (ignored)
        """
        artwork_url: str | None

        if len(args) == 1:
            artwork_url = args[0]
        elif len(args) >= 2:
            artwork_url = args[1]
        else:
            artwork_url = None

        if not artwork_url:
            return

        with self.player._cv:
            current = self.player._state.now_playing
            try:
                if current is not None:
                    current.artwork_url = artwork_url
                    # Keep thumbnail alias in capabilities for legacy UI components
                    if isinstance(current.capabilities, dict):
                        current.capabilities.setdefault("thumbnail_url", artwork_url)

                    # Test-mode: opportunistically record history on first provider signal
                    if self.player._test_mode and not self.player._history:
                        self.player._history = [current]

            except Exception as e:
                logger.exception("Failed to update artwork: %s", e)
