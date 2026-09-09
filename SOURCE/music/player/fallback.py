"""
Music Player Fallback Handler.

Handles autoplay fallback and queue refill logic for the music player.

This module provides the MusicPlayerFallback class which manages:
- Queue refill when empty
- Autoplay trigger logic

Usage:
    from music.player.fallback import MusicPlayerFallback

    fallback = MusicPlayerFallback(player)
    fallback.trigger_empty_queue_fallback(current_item)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from models.player import QueueItem

logger = get_logger(__name__)


class MusicPlayerFallback:
    """Handles autoplay fallback and queue refill logic."""

    def __init__(self, player) -> None:
        """Initialize the fallback handler with a reference to the player."""
        self.player = player

    def trigger_empty_queue_fallback(self, current: QueueItem | None) -> None:
        """
        Trigger autoplay fallback to refill queue.

        CRITICAL: This must not block - autoplay operations should be async/non-blocking.
        `AutoplayController.check_and_run_async()` is itself the non-blocking
        entry point (it schedules its own asyncio task or background thread
        internally), so this is a direct, synchronous call - not something
        this handler needs to wrap in its own task.

        This method must never raise exceptions - all errors are caught and logged.
        """
        autoplay = self.player.autoplay
        if autoplay is None:
            return

        try:
            set_anchor = getattr(autoplay, "set_anchor", None)
            if callable(set_anchor):
                # Anchor the track fallback should resume from (mirrors the
                # TRACK_ENDED autoplay trigger in
                # ui/websocket/command_handlers.py). `current` may be None;
                # check_and_run_async()'s current-track resolution falls
                # back to now_playing/_last_played/this anchor in order.
                set_anchor(current)

            trigger = getattr(autoplay, "check_and_run_async", None)
            if not callable(trigger):
                self.player._logger.warning("Autoplay has no check_and_run_async - cannot refill empty queue")
                return
            trigger()
        except Exception as exc:
            # Broad catch for any issues with autoplay system
            self.player._logger.warning("Autoplay fallback system error: %r", exc)


__all__ = ["MusicPlayerFallback"]
