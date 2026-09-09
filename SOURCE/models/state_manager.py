"""
Consolidated State Management - Thread-safe state container.

AI Instructions
===============
This is the CANONICAL module for state management. Use ConsolidatedState for all
queue and playback state operations. This replaces the removed music/queue_state.py
and music/queue_manager.py modules.

Usage:
    >>> from models.state_manager import ConsolidatedState, PlaybackStatus
    >>> from models.player import QueueItem
    >>>
    >>> state = ConsolidatedState()
    >>> item = QueueItem(id="1", title="Song", url="...")
    >>> state.add_to_queue(item)
    >>> state.set_playing(item, PlaybackStatus.PLAYING)
    >>> snapshot = state.snapshot()

Thread Safety:
    All methods are thread-safe. Internal _lock protects state mutations.
    Use snapshot() for consistent reads across multiple fields.

Related Modules:
    - models/player.py: PlayerState, QueueItem definitions
    - backend/music_adapter.py: High-level music control interface
    - services/persistence/state_store.py: Persistent storage integration

Deprecated Alternatives (DO NOT USE):
    - music/queue_state.py (REMOVED - Ruff TID251 blocks import)
    - music/queue_manager.py (REMOVED - Ruff TID251 blocks import)

See Also:
    - docs/STATE_MANAGEMENT_GUIDE.md
    - docs/architecture/canonical_surfaces.md
    - CHANGELOG_RECENT.md
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from models.player import QueueItem


class PlaybackStatus(Enum):
    """Playback status enum"""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class RepeatMode(Enum):
    """
    Repeat mode for playback - matches real music player behavior.

    OFF: Play through queue/playlist, then stop or transition to autoplay
    ALL: Loop the entire queue/playlist forever
    ONE: Loop the current song forever
    """

    OFF = "off"
    ALL = "all"
    ONE = "one"


class PlaybackMode(Enum):
    """
    Playback mode for queue population.

    FREEFORM: AI-driven autoplay fills queue with recommendations
    PLAYLIST: Drawing from a cached playlist, transitions to FREEFORM when exhausted
    """

    FREEFORM = "freeform"
    PLAYLIST = "playlist"


@dataclass
class PlaybackState:
    """Playback-specific state"""

    status: PlaybackStatus = PlaybackStatus.IDLE
    now_playing: QueueItem | None = None
    position: int = 0  # seconds
    duration: int = 0  # seconds
    position_percentage: float = 0.0


@dataclass
class QueueState:
    """Queue-specific state"""

    items: list[QueueItem] = field(default_factory=list)
    history: list[QueueItem] = field(default_factory=list)
    max_size: int = 10
    history_max_size: int = 10


@dataclass
class PreferencesState:
    """User preferences state"""

    volume: int = 50
    shuffle: bool = False
    repeat_mode: RepeatMode = RepeatMode.OFF
    autoplay_enabled: bool = True
    playback_mode: PlaybackMode = PlaybackMode.FREEFORM


@dataclass
class CacheState:
    """Cache-related state"""

    position_cache: tuple[int, int, float] = (
        0,
        0,
        0.0,
    )  # (position, duration, percentage)
    position_cache_time: float = 0.0
    position_cache_ttl: float = 0.1  # 100ms


@dataclass
class ControlState:
    """Control flags"""

    user_paused: bool = False
    stop_worker: bool = False
    autoplay_running: bool = False


class ConsolidatedState:
    """
    Single source of truth for all music player state.

    Features:
    - Structured state with clear categories
    - Thread-safe access
    - Change notifications
    - Snapshot support
    - State diff tracking
    """

    def __init__(self, on_change: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._lock = threading.RLock()  # Reentrant lock for nested access

        # State categories
        self.playback = PlaybackState()
        self.queue = QueueState()
        self.preferences = PreferencesState()
        self.cache = CacheState()
        self.control = ControlState()

        # Change notification
        self._on_change = on_change
        self._last_snapshot: dict[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        """
        Get immutable snapshot of current state.

        SECURITY: Uses deep copy to prevent race conditions when state is accessed
        concurrently. This ensures that modifications to the returned snapshot don't
        affect the original state, and vice versa.

        Returns:
            Deep copy of state as dict representation
        """
        import copy

        with self._lock:
            # Helper to convert QueueItem (Pydantic) or dataclass to dict
            def to_dict(obj):
                if obj is None:
                    return None
                # QueueItem is a Pydantic model, use model_dump()
                if hasattr(obj, "model_dump"):
                    return obj.model_dump()
                # Otherwise assume dataclass
                return asdict(obj)

            # Create snapshot dict
            snapshot_dict = {
                "playback": {
                    "status": self.playback.status.value,
                    "now_playing": to_dict(self.playback.now_playing),
                    "position": self.playback.position,
                    "duration": self.playback.duration,
                    "position_percentage": self.playback.position_percentage,
                },
                "queue": {
                    "items": [to_dict(item) for item in self.queue.items],
                    "count": len(self.queue.items),
                    "history_count": len(self.queue.history),
                    "max_size": self.queue.max_size,
                },
                "preferences": {
                    "volume": self.preferences.volume,
                    "shuffle": self.preferences.shuffle,
                    "repeat_mode": self.preferences.repeat_mode.value,
                    "autoplay_enabled": self.preferences.autoplay_enabled,
                    "playback_mode": self.preferences.playback_mode.value,
                },
                "control": {
                    "user_paused": self.control.user_paused,
                    "stop_worker": self.control.stop_worker,
                    "autoplay_running": self.control.autoplay_running,
                },
                "timestamp": time.time(),
            }

            # SECURITY: Deep copy to prevent race conditions
            # This ensures that nested objects (like QueueItem dicts) are fully copied
            # and modifications to the snapshot don't affect the original state
            return copy.deepcopy(snapshot_dict)

    def diff(self, old_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Get diff between current state and old snapshot.

        Args:
            old_snapshot: Previous snapshot (None = last snapshot)

        Returns:
            Dict of changed fields
        """
        if old_snapshot is None:
            old_snapshot = self._last_snapshot

        current = self.snapshot()
        changes = {}

        def _diff_dict(old: dict, new: dict, prefix: str = "") -> None:
            for key, new_value in new.items():
                full_key = f"{prefix}.{key}" if prefix else key
                old_value = old.get(key)

                if isinstance(new_value, dict) and isinstance(old_value, dict):
                    _diff_dict(old_value, new_value, full_key)
                elif new_value != old_value:
                    changes[full_key] = {"old": old_value, "new": new_value}

        _diff_dict(old_snapshot, current)
        return changes

    def notify_change(self, force: bool = False) -> None:
        """
        Notify listeners of state change.

        Args:
            force: Force notification even if no changes detected
        """
        if not self._on_change:
            return

        current = self.snapshot()

        # Only notify if state actually changed (unless forced)
        if not force and current == self._last_snapshot:
            return

        self._on_change(current)
        self._last_snapshot = current

    # Convenience accessors with atomic operations

    def set_playing(self, item: QueueItem | None, status: PlaybackStatus = PlaybackStatus.PLAYING) -> None:
        """Atomically set now playing and status"""
        with self._lock:
            self.playback.now_playing = item
            self.playback.status = status

    def set_volume(self, level: int) -> int:
        """Set volume (clamped 0-100)"""
        with self._lock:
            self.preferences.volume = max(0, min(100, level))
            return self.preferences.volume

    def add_to_queue(self, item: QueueItem) -> bool:
        """Add item to queue (checks max size)"""
        with self._lock:
            if len(self.queue.items) >= self.queue.max_size:
                return False
            self.queue.items.append(item)
            return True

    def remove_from_queue(self, item_id: str) -> bool:
        """Remove item from queue by ID"""
        with self._lock:
            original_len = len(self.queue.items)
            self.queue.items = [item for item in self.queue.items if item.id != item_id]
            return len(self.queue.items) < original_len

    def clear_queue(self) -> None:
        """Clear all queue items"""
        with self._lock:
            self.queue.items.clear()

    def add_to_history(self, item: QueueItem) -> None:
        """Add item to history (maintains max size)"""
        with self._lock:
            self.queue.history.append(item)
            # Maintain max size
            while len(self.queue.history) > self.queue.history_max_size:
                self.queue.history.pop(0)

    def pop_from_history(self) -> QueueItem | None:
        """Pop most recent history item"""
        with self._lock:
            if not self.queue.history:
                return None
            return self.queue.history.pop()

    def update_position(self, position: int, duration: int) -> None:
        """Update playback position (uses cache)"""
        with self._lock:
            current_time = time.time()

            # Check cache TTL
            if current_time - self.cache.position_cache_time < self.cache.position_cache_ttl:
                return  # Use cached values

            # Update
            self.playback.position = position
            self.playback.duration = duration
            self.playback.position_percentage = position / duration if duration > 0 else 0.0

            # Update cache
            self.cache.position_cache = (
                position,
                duration,
                self.playback.position_percentage,
            )
            self.cache.position_cache_time = current_time

    def get_cached_position(self) -> tuple[int, int, float]:
        """Get cached position (position, duration, percentage)"""
        with self._lock:
            current_time = time.time()

            if current_time - self.cache.position_cache_time < self.cache.position_cache_ttl:
                return self.cache.position_cache

            # Cache expired, return current values
            return (
                self.playback.position,
                self.playback.duration,
                self.playback.position_percentage,
            )

    def is_playing(self) -> bool:
        """Check if actively playing"""
        with self._lock:
            return self.playback.status == PlaybackStatus.PLAYING

    def is_paused(self) -> bool:
        """Check if paused"""
        with self._lock:
            return self.playback.status == PlaybackStatus.PAUSED

    def queue_length(self) -> int:
        """Get current queue length"""
        with self._lock:
            return len(self.queue.items)

    def queue_is_empty(self) -> bool:
        """Check if queue is empty"""
        with self._lock:
            return len(self.queue.items) == 0

    def queue_is_full(self) -> bool:
        """Check if queue is at max capacity"""
        with self._lock:
            return len(self.queue.items) >= self.queue.max_size

    # Repeat mode operations

    def get_repeat_mode(self) -> RepeatMode:
        """Get current repeat mode"""
        with self._lock:
            return self.preferences.repeat_mode

    def set_repeat_mode(self, mode: RepeatMode) -> RepeatMode:
        """Set repeat mode"""
        with self._lock:
            self.preferences.repeat_mode = mode
            return mode

    # Shuffle operations
    #
    # #4214: these exist so callers stop doing `with state.
    # _lock: state.preferences.shuffle = x` -- reaching through the object for
    # its private lock to poke a field. Setting the preference is only ever
    # half of turning shuffle on; the other half is the play order, and the
    # authority that does both is `music.shuffle_control.apply_shuffle`.

    def get_shuffle(self) -> bool:
        """Get current shuffle preference"""
        with self._lock:
            return self.preferences.shuffle

    def set_shuffle(self, enabled: bool) -> bool:
        """Set shuffle preference"""
        with self._lock:
            self.preferences.shuffle = bool(enabled)
            return self.preferences.shuffle

    def cycle_repeat_mode(self) -> RepeatMode:
        """Cycle through repeat modes: OFF → ALL → ONE → OFF"""
        with self._lock:
            if self.preferences.repeat_mode == RepeatMode.OFF:
                self.preferences.repeat_mode = RepeatMode.ALL
            elif self.preferences.repeat_mode == RepeatMode.ALL:
                self.preferences.repeat_mode = RepeatMode.ONE
            else:
                self.preferences.repeat_mode = RepeatMode.OFF
            return self.preferences.repeat_mode

    def should_repeat_current(self) -> bool:
        """Check if current track should repeat (Repeat One mode)"""
        with self._lock:
            return self.preferences.repeat_mode == RepeatMode.ONE

    def should_loop_queue(self) -> bool:
        """Check if queue should loop (Repeat All mode)"""
        with self._lock:
            return self.preferences.repeat_mode == RepeatMode.ALL

    # Playback mode operations

    def get_playback_mode(self) -> PlaybackMode:
        """Get current playback mode (FREEFORM or PLAYLIST)"""
        with self._lock:
            return self.preferences.playback_mode

    def set_playback_mode(self, mode: PlaybackMode) -> PlaybackMode:
        """Set playback mode"""
        with self._lock:
            self.preferences.playback_mode = mode
            return mode

    def is_playlist_mode(self) -> bool:
        """Check if in playlist mode"""
        with self._lock:
            return self.preferences.playback_mode == PlaybackMode.PLAYLIST

    # Context managers for atomic multi-step operations

    def atomic_update(self):
        """
        Context manager for atomic multi-step state updates.

        Usage:
            with state.atomic_update():
                state.set_playing(item)
                state.add_to_queue(next_item)
                # Changes notified only once, at end of block
        """
        return _AtomicUpdateContext(self)


class _AtomicUpdateContext:
    """Context manager for atomic state updates"""

    def __init__(self, state: ConsolidatedState):
        self._state = state
        self._lock_acquired = False

    def __enter__(self):
        self._state._lock.acquire()
        self._lock_acquired = True
        return self._state

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._lock_acquired:
            # Notify change on exit (even if exception)
            if exc_type is None:  # Only notify if no exception
                self._state.notify_change()
            self._state._lock.release()
        return False  # Don't suppress exceptions
