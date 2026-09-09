"""
Playback control client for music operations.
Handles play/pause, track navigation, volume, and seeking.
"""

from __future__ import annotations

from contracts.api_response import ensure_envelope
from core.constants import TIMEOUT_EXTENDED, TIMEOUT_LONG, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

logger = get_logger(__name__)


class PlaybackClient:
    """Client for music playback control operations."""

    base_url: str | None = None

    def __init__(self, session):
        self.session = session
        self.base_url = None  # Set by parent client

    def _post_simple_command(self, path: str, *, timeout: float = 2.0) -> bool:
        """Execute simple POST command with standard error handling."""
        try:
            response = self.session.post(f"{self.base_url}{path}", timeout=timeout)
            response.raise_for_status()

            # Parse ResponseEnvelope
            envelope = ensure_envelope(response.json())
            return envelope["ok"]
        except Exception as e:
            logger.error("%s failed: %s", path, e)
            return False

    def play_pause(self) -> bool:
        """Toggle play/pause state."""
        return self._post_simple_command("/v1/playback/toggle")

    def pause(self) -> bool:
        """Pause playback."""
        return self._post_simple_command("/v1/pause")

    def resume(self) -> bool:
        """Resume playback."""
        return self._post_simple_command("/v1/resume")

    def next_track(self) -> bool:
        """Skip to next track."""
        return self._post_simple_command("/v1/skip", timeout=TIMEOUT_EXTENDED)

    def previous_track(self) -> bool:
        """Go to previous track."""
        return self._post_simple_command("/v1/previous", timeout=TIMEOUT_LONG)

    def set_volume(self, level: int) -> bool:
        """Set volume level (0-100)."""
        try:
            response = self.session.post(
                f"{self.base_url}/v1/volume",
                json={"level": max(0, min(100, level))},
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()

            # Parse ResponseEnvelope
            envelope = ensure_envelope(response.json())
            return envelope["ok"]
        except Exception as e:
            logger.error("Set volume failed: %s", e)
            return False

    def seek(self, position: int) -> bool:
        """Seek to position (seconds)."""
        try:
            response = self.session.post(
                f"{self.base_url}/v1/seek",
                json={"position": position},
                timeout=TIMEOUT_SHUTDOWN,
            )
            response.raise_for_status()

            # Parse ResponseEnvelope
            envelope = ensure_envelope(response.json())
            return envelope["ok"]
        except Exception as e:
            logger.error("Seek failed: %s", e)
            return False


__all__ = ["PlaybackClient"]
