"""
Simple Backend Stream Management.

This module contains FFmpeg stream management logic
extracted from the main SimpleBackend class to comply with code constraints.
"""

from __future__ import annotations

import subprocess
import threading
import time

from core.constants import TIMEOUT_DEFAULT, TIMEOUT_LONG, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger
from core.subprocess_utils import popen_silent

logger = get_logger(__name__)


class SimpleBackendStreamManager:
    """Handles FFmpeg stream management for the simple backend."""

    def __init__(self, backend_instance):
        """
        Initialize stream manager.

        Args:
            backend_instance: The SimpleBackend instance
        """
        self.backend = backend_instance

    def start_stream(self, source: str, *, offset: float) -> None:
        """
        Start FFmpeg stream for audio playback.

        Args:
            source: Audio source URL or file path
            offset: Start position offset in seconds
        """
        try:
            # Stop any existing stream
            self.stop_stream()

            # Configure FFmpeg command
            ffmpeg_cmd = self._build_ffmpeg_command(source, offset)

            logger.info("Starting FFmpeg stream: %s", " ".join(ffmpeg_cmd))

            # Start FFmpeg process
            self.backend._proc = popen_silent(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=10 * 1024 * 1024,  # 10MB buffer
            )

            # Increment generation before starting thread so stale threads
            # from a previous session cannot clobber _is_playing.
            self.backend._play_generation += 1
            generation = self.backend._play_generation

            # Start playback thread
            self.backend._play_thread = threading.Thread(
                target=self.backend._playback_loop,
                args=(self.backend._proc.stdout, generation),
                name="simple-backend-playback",
                daemon=True,
            )
            self.backend._play_thread.start()

            self.backend._is_playing = True
            self.backend._stream_start_time = time.time() - offset
            self.backend._current_source = source

            logger.info("Stream started for source: %s", source)

        except Exception as e:
            logger.exception("Failed to start stream for %s: %s", source, e)
            self.stop_stream()
            raise

    def stop_stream(self) -> None:
        """Stop the current FFmpeg stream."""
        try:
            # Stop playback thread
            if self.backend._play_thread and self.backend._play_thread.is_alive():
                self.backend._stop_event.set()
                self.backend._play_thread.join(timeout=TIMEOUT_SHUTDOWN)
                if self.backend._play_thread.is_alive():
                    logger.warning("Playback thread did not stop gracefully")

            # Stop stderr thread if exists
            if (
                hasattr(self.backend, "_stderr_thread")
                and self.backend._stderr_thread
                and self.backend._stderr_thread.is_alive()
            ):
                self.backend._stderr_thread.join(timeout=TIMEOUT_DEFAULT)

            # Terminate FFmpeg process
            if self.backend._proc:
                try:
                    self.backend._proc.terminate()
                    self.backend._proc.wait(timeout=TIMEOUT_SHUTDOWN)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg process did not terminate gracefully, killing")
                    self.backend._proc.kill()
                    self.backend._proc.wait(timeout=TIMEOUT_LONG)
                except Exception as e:
                    logger.debug("Error stopping FFmpeg process: %s", e)

            # Reset state
            self.backend._proc = None
            self.backend._play_thread = None
            self.backend._stderr_thread = None
            self.backend._is_playing = False
            self.backend._paused = False
            self.backend._stream_start_time = None
            self.backend._current_source = None

        except Exception as e:
            logger.exception("Error stopping stream: %s", e)

    def _build_ffmpeg_command(self, source: str, offset: float) -> list[str]:
        """
        Build FFmpeg command for audio streaming.

        Args:
            source: Audio source
            offset: Start offset in seconds

        Returns:
            FFmpeg command as list of strings
        """
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        # Add offset if specified
        if offset > 0:
            cmd.extend(["-ss", str(offset)])

        # Input
        cmd.extend(["-i", source])

        # Audio filters and processing
        audio_filters = []

        # Volume adjustment
        if hasattr(self.backend, "_volume") and self.backend._volume != 100:
            volume_factor = self.backend._volume / 100.0
            audio_filters.append(f"volume={volume_factor}")

        # Apply filters if any
        if audio_filters:
            cmd.extend(["-af", ",".join(audio_filters)])

        # Output format: 16-bit PCM, 44.1kHz, stereo
        cmd.extend(
            [
                "-f",
                "s16le",  # PCM signed 16-bit little-endian
                "-ar",
                "44100",  # Sample rate
                "-ac",
                "2",  # Stereo
                "-acodec",
                "pcm_s16le",  # Ensure PCM output
                "pipe:1",  # Output to stdout
            ]
        )

        return cmd

    def get_stream_position(self) -> float | None:
        """
        Get current stream position in seconds.

        Returns:
            Position in seconds or None if not streaming
        """
        if not self.backend._is_playing or self.backend._stream_start_time is None:
            return None

        return time.time() - self.backend._stream_start_time

    def seek_in_stream(self, position_seconds: float) -> None:
        """
        Seek to a position in the current stream.

        Args:
            position_seconds: Position to seek to
        """
        if not self.backend._current_source:
            logger.warning("Cannot seek: no current source")
            return

        logger.info("Seeking to position: %ss", position_seconds)
        self.start_stream(self.backend._current_source, offset=position_seconds)
