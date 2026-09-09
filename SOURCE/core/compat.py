"""
Compatibility Layer - Bridge between old and new state management.

AI Instructions
===============
This module provides compatibility adapters for gradual migration from
ConsolidatedState to the new StateHub architecture.

Usage:
    # Instead of:
    # from models.state_manager import ConsolidatedState
    # state = ConsolidatedState()

    # Use:
    >>> from core.compat import StateCompat
    >>> state = StateCompat()
    >>> state.is_playing()
    >>> state.queue_length()
    >>> state.snapshot()

Migration Strategy:
    1. Replace ConsolidatedState imports with StateCompat
    2. Old code continues to work with familiar API
    3. StateCompat reads from StateHub under the hood
    4. Gradually migrate to using selectors directly
    5. Remove StateCompat when migration complete

Related Modules:
    - core/state_hub.py: New state coordinator
    - core/state_selectors.py: New read accessors
    - models/state_manager.py: Old ConsolidatedState (deprecated)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from core.state_hub import (
    SetAutoplay,
    SetDucked,
    SetRepeatMode,
    SetShuffle,
    SetVolume,
    UpdatePlayerState,
    UpdateVoiceMode,
    get_state_hub,
)
from core.unified_state import VoiceMode

if TYPE_CHECKING:
    from models.player import PlayerState, QueueItem

logger = get_logger(__name__)


class StateCompat:
    """
    Compatibility wrapper providing ConsolidatedState-like interface.

    Use this during migration - old code can use familiar API
    while actually reading from StateHub.

    Methods mirror ConsolidatedState but read from StateHub.

    Note:
        Write operations dispatch commands to StateHub.
        Some operations may behave slightly differently due to
        the new architecture's event-driven nature.
    """

    def __init__(self, *, user_id: str | None = None) -> None:
        self._hub = get_state_hub(user_id=user_id)
        self.user_id = self._hub.user_id

    def _dispatch_mutation(self, command: Any) -> None:
        accepted = self._hub.dispatch(command)
        if accepted is not True:
            raise RuntimeError("StateHub rejected %s" % type(command).__name__)

    # =========================================================================
    # Playback State (Read)
    # =========================================================================

    def is_playing(self) -> bool:
        """Check if actively playing."""
        room = self._hub.get_state().active_room
        return room is not None and room.player.is_playing

    def is_paused(self) -> bool:
        """Check if paused (not playing but has now_playing)."""
        room = self._hub.get_state().active_room
        if room is None:
            return False
        return not room.player.is_playing and room.player.now_playing is not None

    def now_playing(self) -> QueueItem | None:
        """Get currently playing item."""
        room = self._hub.get_state().active_room
        return room.player.now_playing if room else None

    def get_position(self) -> tuple[int, int, float]:
        """Get (position, duration, percentage)."""
        room = self._hub.get_state().active_room
        if room is None:
            return (0, 0, 0.0)
        p = room.player
        return (p.position, p.duration, p.position_percentage)

    # =========================================================================
    # Queue State (Read)
    # =========================================================================

    def queue_length(self) -> int:
        """Get current queue length."""
        room = self._hub.get_state().active_room
        return len(room.player.queue) if room else 0

    def queue_is_empty(self) -> bool:
        """Check if queue is empty."""
        return self.queue_length() == 0

    def queue_is_full(self, max_size: int = 10) -> bool:
        """Check if queue is at max capacity."""
        return self.queue_length() >= max_size

    def get_queue(self) -> list[QueueItem]:
        """Get queue items."""
        room = self._hub.get_state().active_room
        return list(room.player.queue) if room else []

    # =========================================================================
    # Preferences (Read)
    # =========================================================================

    def get_volume(self) -> int:
        """Get current volume."""
        state = self._hub.get_state()
        room = state.active_room
        return room.player.volume if room else state.preferences.volume

    def get_shuffle(self) -> bool:
        """Get shuffle state."""
        return self._hub.get_state().preferences.shuffle

    def get_repeat_mode(self) -> str:
        """Get repeat mode (off, all, one)."""
        return self._hub.get_state().preferences.repeat_mode

    def get_autoplay(self) -> bool:
        """Get autoplay enabled state."""
        return self._hub.get_state().preferences.autoplay_enabled

    # =========================================================================
    # Preferences (Write)
    # =========================================================================

    def set_volume(self, level: int) -> int:
        """Set volume (clamped 0-100)."""
        clamped = max(0, min(100, level))
        self._dispatch_mutation(SetVolume(volume=clamped))
        return clamped

    def set_shuffle(self, enabled: bool) -> None:
        """Set shuffle state."""
        self._dispatch_mutation(SetShuffle(shuffle=enabled))

    def set_repeat_mode(self, mode: str) -> None:
        """Set repeat mode (off, all, one)."""
        self._dispatch_mutation(SetRepeatMode(mode=mode))

    def cycle_repeat_mode(self) -> str:
        """Cycle through repeat modes: off -> all -> one -> off."""
        current = self.get_repeat_mode()
        if current == "off":
            new_mode = "all"
        elif current == "all":
            new_mode = "one"
        else:
            new_mode = "off"
        # Use dispatch_sync to ensure state is updated before returning
        self._hub.dispatch_sync(SetRepeatMode(mode=new_mode))
        return new_mode

    def set_autoplay(self, enabled: bool) -> None:
        """Set autoplay enabled state."""
        self._dispatch_mutation(SetAutoplay(enabled=enabled))

    # =========================================================================
    # Player State Update
    # =========================================================================

    def update_player_state(self, player_state: PlayerState, room_id: str = "local") -> None:
        """
        Update player state for a room.

        Args:
            player_state: New player state
            room_id: Room to update
        """
        self._dispatch_mutation(UpdatePlayerState(room_id=room_id, player_state=player_state))

    # =========================================================================
    # Voice State
    # =========================================================================

    def is_ducked(self) -> bool:
        """Check if audio is ducked."""
        return self._hub.get_state().voice.is_ducked

    def set_ducked(self, ducked: bool) -> None:
        """Set ducking state."""
        self._dispatch_mutation(SetDucked(is_ducked=ducked))

    def get_voice_mode(self) -> str:
        """Get voice mode as string."""
        return self._hub.get_state().voice.mode.value

    def set_voice_mode(self, mode: str) -> None:
        """Set voice mode from string."""
        mode_map = {
            "idle": VoiceMode.IDLE,
            "listening": VoiceMode.LISTENING,
            "processing": VoiceMode.PROCESSING,
            "speaking": VoiceMode.SPEAKING,
        }
        vm = mode_map.get(mode, VoiceMode.IDLE)
        self._dispatch_mutation(UpdateVoiceMode(mode=vm))

    # =========================================================================
    # Snapshot (Compatibility)
    # =========================================================================

    def snapshot(self) -> dict[str, Any]:
        """
        Get snapshot in old ConsolidatedState format.

        Returns:
            Dict matching old snapshot() format for backward compatibility
        """
        state = self._hub.get_state()
        room = state.active_room

        # Build queue items
        queue_items = []
        if room:
            queue_items = [item.model_dump() for item in room.player.queue]

        # Build now_playing
        now_playing = None
        if room and room.player.now_playing:
            now_playing = room.player.now_playing.model_dump()

        # Determine status
        status = "idle"
        if room:
            if room.player.is_playing:
                status = "playing"
            elif room.player.now_playing:
                status = "paused"

        return {
            "playback": {
                "status": status,
                "now_playing": now_playing,
                "position": room.player.position if room else 0,
                "duration": room.player.duration if room else 0,
                "position_percentage": room.player.position_percentage if room else 0.0,
            },
            "queue": {
                "items": queue_items,
                "count": len(queue_items),
                "history_count": 0,  # Not tracked in new system
                "max_size": 10,
            },
            "preferences": {
                "volume": room.player.volume if room else state.preferences.volume,
                "shuffle": state.preferences.shuffle,
                "repeat_mode": state.preferences.repeat_mode,
                "autoplay_enabled": state.preferences.autoplay_enabled,
                "playback_mode": state.preferences.playback_mode.value,
            },
            "control": {
                "user_paused": False,  # Not tracked in new system
                "stop_worker": False,
                "autoplay_running": False,
            },
            "voice": {
                "mode": state.voice.mode.value,
                "is_ducked": state.voice.is_ducked,
                "wake_enabled": state.voice.wake_enabled,
                "wake_active": state.voice.wake_active,
            },
            "timestamp": time.time(),
            "version": state.version,
        }

    def diff(self, old_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Get diff between current state and old snapshot.

        Note: This is a simplified implementation.
        For full diff support, compare snapshots directly.
        """
        current = self.snapshot()
        if old_snapshot is None:
            return {}

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


__all__ = [
    "StateCompat",
]
