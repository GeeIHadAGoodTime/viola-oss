"""
Playback Controller

Extracted from embedded_backend.py to reduce file size.
Handles playback state management and control operations.
"""

from __future__ import annotations

from typing import Any


class PlaybackController:
    """Handles playback state management and control operations."""

    def __init__(self, backend: Any):
        """
        Initialize playback controller.

        Args:
            backend: The parent EmbeddedPlayerBackend instance
        """
        self._backend = backend
        self._logger = backend._logger
        self._test_mode = backend._test_mode
        self._qt_available = backend._qt_available

    def start_playback(self, embed_url: str) -> None:
        """
        Start playback for the loaded media.

        Args:
            embed_url: The embed URL that was loaded
        """
        # Mark start as pending until playback is verified
        self._backend._pending_start = True
        self._backend._is_playing = False
        self._backend._is_paused = False

        # Start progress tracking
        self._backend._start_progress_pump()

        # Emit signal if available
        if self._backend._signals:
            self._backend._signals.playback_started.emit()

        self._logger.info("PLAYBACK_CONTROLLER: Playback started")

    def pause_playback(self) -> None:
        """Pause current playback."""
        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info("PLAYBACK_CONTROLLER: (test-mode simulation) pausing")
            self._backend._is_playing = False
            self._backend._is_paused = True
            if self._backend._signals:
                self._backend._signals.playback_paused.emit()
            return

        if not self._qt_available or not self._backend._webview:
            self._logger.warning("PLAYBACK_CONTROLLER: Cannot pause - no webview available")
            return

        try:
            # Use JS communicator to pause
            self._backend._js_communicator.execute_command("pause")
            self._backend._is_playing = False
            self._backend._is_paused = True

            if self._backend._signals:
                self._backend._signals.playback_paused.emit()

            self._logger.info("PLAYBACK_CONTROLLER: Playback paused")
        except Exception as exc:
            self._logger.error("PLAYBACK_CONTROLLER: Pause failed: %s", exc)
            if self._backend._signals:
                self._backend._signals.playback_error.emit(str(exc))

    def resume_playback(self) -> None:
        """Resume paused playback."""
        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info("PLAYBACK_CONTROLLER: (test-mode simulation) resuming")
            self._backend._is_playing = True
            self._backend._is_paused = False
            if self._backend._signals:
                self._backend._signals.playback_resumed.emit()
            return

        if not self._qt_available or not self._backend._webview:
            self._logger.warning("PLAYBACK_CONTROLLER: Cannot resume - no webview available")
            return

        try:
            # Use JS communicator to resume
            self._backend._js_communicator.execute_command("play")
            self._backend._is_playing = True
            self._backend._is_paused = False

            if self._backend._signals:
                self._backend._signals.playback_resumed.emit()

            self._logger.info("PLAYBACK_CONTROLLER: Playback resumed")
        except Exception as exc:
            self._logger.error("PLAYBACK_CONTROLLER: Resume failed: %s", exc)
            if self._backend._signals:
                self._backend._signals.playback_error.emit(str(exc))

    def stop_playback(self) -> None:
        """Stop current playback."""
        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info("PLAYBACK_CONTROLLER: (test-mode simulation) stopping")
            self._backend._is_playing = False
            self._backend._is_paused = False
            self._backend._pending_start = False
            self._backend._stop_progress_pump()
            if self._backend._signals:
                self._backend._signals.playback_stopped.emit()
            return

        if not self._qt_available or not self._backend._webview:
            self._logger.warning("PLAYBACK_CONTROLLER: Cannot stop - no webview available")
            return

        try:
            # Use JS communicator to stop
            self._backend._js_communicator.execute_command("stop")
            self._backend._is_playing = False
            self._backend._is_paused = False
            self._backend._pending_start = False
            self._backend._stop_progress_pump()

            if self._backend._signals:
                self._backend._signals.playback_stopped.emit()

            self._logger.info("PLAYBACK_CONTROLLER: Playback stopped")
        except Exception as exc:
            self._logger.error("PLAYBACK_CONTROLLER: Stop failed: %s", exc)
            if self._backend._signals:
                self._backend._signals.playback_error.emit(str(exc))

    def seek_to_position(self, position_seconds: float) -> None:
        """
        Seek to a specific position.

        Args:
            position_seconds: Position to seek to in seconds
        """
        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info(
                "PLAYBACK_CONTROLLER: (test-mode simulation) seeking to %.1f",
                position_seconds,
            )
            return

        if not self._qt_available or not self._backend._webview:
            self._logger.warning("PLAYBACK_CONTROLLER: Cannot seek - no webview available")
            return

        try:
            # Use JS communicator to seek
            self._backend._js_communicator.execute_command("seekTo", position_seconds)
            self._logger.info("PLAYBACK_CONTROLLER: Seeked to %.1f seconds", position_seconds)
        except Exception as exc:
            self._logger.error("PLAYBACK_CONTROLLER: Seek failed: %s", exc)
            if self._backend._signals:
                self._backend._signals.playback_error.emit(str(exc))

    def set_volume_level(self, level: int) -> int:
        """
        Set volume level.

        Args:
            level: Volume level (0-100)

        Returns:
            The actual volume level set
        """
        # Clamp to valid range
        level = max(0, min(100, level))
        self._backend._volume = level

        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info("PLAYBACK_CONTROLLER: (test-mode simulation) volume set to %d", level)
            return level

        if not self._qt_available or not self._backend._webview:
            self._logger.warning("PLAYBACK_CONTROLLER: Cannot set volume - no webview available")
            return level

        try:
            # Use JS communicator to set volume
            self._backend._js_communicator.execute_command("setVolume", level / 100.0)
            self._logger.debug("PLAYBACK_CONTROLLER: Volume set to %d", level)
        except Exception as exc:
            self._logger.error("PLAYBACK_CONTROLLER: Volume setting failed: %s", exc)
            if self._backend._signals:
                self._backend._signals.playback_error.emit(str(exc))

        return level

    def get_playback_state(self) -> dict[str, Any]:
        """
        Get current playback state.

        Returns:
            Dictionary with playback state information
        """
        return {
            "is_playing": self._backend._is_playing,
            "is_paused": self._backend._is_paused,
            "pending_start": self._backend._pending_start,
            "current_url": self._backend._current_url,
            "video_id": self._backend._video_id,
            "volume": self._backend._volume,
            "position_ms": self._backend._position_ms,
            "duration_ms": self._backend._duration_ms,
        }
