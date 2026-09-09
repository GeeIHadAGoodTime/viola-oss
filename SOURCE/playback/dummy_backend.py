"""
Dummy Backend Module
Dummy backend for testing (no actual audio output).
"""

from __future__ import annotations

import logging
import threading
import time

from core.logging_config import get_logger


class DummyBackend:
    """Dummy backend for testing (no actual audio output)"""

    def __init__(self, logger: logging.Logger | None = None):
        """
        Initialize dummy backend.

        Args:
            logger: Optional logger instance
        """
        self._logger = logger or get_logger("viola.backend.dummy")
        self._lock = threading.RLock()
        self._playing = False
        self._paused = False
        self._volume = 50
        self._position = 0
        self._duration = 0
        self._current_url: str | None = None
        self._start_time: float | None = None

        self._logger.info("DummyBackend initialized (test mode)")

    def play_url(self, url: str) -> None:
        """
        Simulate playback.

        Args:
            url: URL to "play"
        """
        with self._lock:
            self._current_url = url
            self._playing = True
            self._paused = False
            self._position = 0
            self._duration = 300  # Default 5 minutes
            self._start_time = time.time()
            self._logger.debug("DummyBackend: playing %s", url)

    def pause(self) -> None:
        """Simulate pause"""
        with self._lock:
            if self._playing and not self._paused:
                self._paused = True
                self._update_position()
                self._logger.debug("DummyBackend: paused")

    def resume(self) -> None:
        """Simulate resume"""
        with self._lock:
            if self._playing and self._paused:
                self._paused = False
                if self._start_time:
                    # Adjust start time to account for paused duration
                    self._start_time = time.time() - (
                        self._position / self._duration * (time.time() - self._start_time)
                    )
                else:
                    self._start_time = time.time()
                self._logger.debug("DummyBackend: resumed")

    def stop(self) -> None:
        """Simulate stop"""
        with self._lock:
            self._playing = False
            self._paused = False
            self._position = 0
            self._duration = 0
            self._start_time = None
            self._current_url = None
            self._logger.debug("DummyBackend: stopped")

    def is_playing(self) -> bool:
        """
        Check if currently playing.

        Returns:
            True if playing
        """
        with self._lock:
            return self._playing and not self._paused

    def set_volume(self, level: int) -> int:
        """
        Set volume level (0-100).

        Args:
            level: Volume level (0-100)

        Returns:
            Actual volume level set
        """
        with self._lock:
            self._volume = max(0, min(100, int(level)))
            self._logger.debug("DummyBackend: volume set to %s", self._volume)
            return self._volume

    def get_volume(self) -> int:
        """
        Get current volume level.

        Returns:
            Volume level (0-100)
        """
        with self._lock:
            return self._volume

    def get_position(self) -> int:
        """
        Get current playback position in seconds.

        Returns:
            Position in seconds
        """
        with self._lock:
            self._update_position()
            return int(self._position)

    def get_duration(self) -> int:
        """
        Get track duration in seconds.

        Returns:
            Duration in seconds
        """
        with self._lock:
            return int(self._duration)

    def get_position_percentage(self) -> float:
        """
        Get playback position as percentage.

        Returns:
            Position percentage (0.0 to 1.0)
        """
        with self._lock:
            if self._duration > 0:
                return float(self._position / self._duration)
            return 0.0

    def seek(self, position_seconds: int) -> None:
        """
        Seek to position in seconds.

        Args:
            position_seconds: Position to seek to
        """
        with self._lock:
            position_seconds = max(0, min(position_seconds, self._duration))
            self._position = position_seconds
            if self._start_time:
                self._start_time = time.time() - position_seconds
            self._logger.debug("DummyBackend: seeked to %ss", position_seconds)

    def _update_position(self) -> None:
        """Update current playback position"""
        if self._playing and not self._paused and self._start_time:
            elapsed = time.time() - self._start_time
            self._position = int(min(elapsed, self._duration))

    def cleanup(self) -> None:
        """Cleanup backend resources"""
        with self._lock:
            self.stop()
            self._logger.debug("DummyBackend: cleaned up")
