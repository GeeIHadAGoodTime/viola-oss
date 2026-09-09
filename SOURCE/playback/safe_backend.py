"""
Safe Backend Module
Safe software fallback when devices vanish.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from types import ModuleType

from audio_core.portaudio_guard import open_portaudio, terminate_portaudio
from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger
from core.subprocess_utils import popen_silent

logger = get_logger(__name__)

# Declare fallback before conditional import
pyaudio: ModuleType | None = None

try:
    import pyaudio as _pyaudio_module

    pyaudio = _pyaudio_module
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    logger.warning("pyaudio not available - safe backend will use fallback")


class SafeBackend:
    """Safe software fallback when devices vanish"""

    def __init__(self, logger: logging.Logger | None = None):
        """
        Initialize safe backend.

        Uses software audio device (always available).

        Args:
            logger: Optional logger instance
        """
        self._logger = logger or get_logger("viola.backend.safe")
        self._lock = threading.Lock()
        self._volume = 50
        self._playing = False
        self._paused = False
        self._current_url: str | None = None
        self._proc: subprocess.Popen | None = None

        # Try to find software audio device
        self._audio_device = self._find_safe_device()

        if not self._audio_device:
            self._logger.warning("No safe audio device found - playback may not work")

    def _find_safe_device(self) -> int | None:
        """
        Find safe software audio device.

        Returns:
            Device index or None
        """
        if not PYAUDIO_AVAILABLE or pyaudio is None:
            return None

        try:
            # open_portaudio()/terminate_portaudio() serialize Pa_Initialize and
            # Pa_Terminate under the process-wide lock.
            p = open_portaudio()

            # Look for default device or software device
            try:
                default_device = p.get_default_output_device_info()
                if default_device:
                    device_index = default_device.get("index")
                    if device_index is not None:
                        device_index = int(device_index)
                        self._logger.info(
                            "Found safe device: %s",
                            default_device.get("name", "Unknown"),
                        )
                        terminate_portaudio(p)
                        return device_index
            except Exception as e:
                self._logger.debug("Failed to get default device: %s", e)

            # Look for any available output device
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                    if int(info.get("maxOutputChannels", 0)) > 0:
                        self._logger.info("Found safe device: %s", info.get("name", "Unknown"))
                        terminate_portaudio(p)
                        return i
                except Exception as e:
                    self._logger.debug("Failed to query device %d (non-critical): %s", i, e)
                    continue

            terminate_portaudio(p)
            return None

        except Exception as e:
            self._logger.error("Error finding safe device: %s", e)
            return None

    def play_url(self, url: str) -> None:
        """
        Play audio from URL using safe software device.

        Args:
            url: URL to play
        """
        with self._lock:
            # Avoid deadlock: do not call public stop() while holding lock
            self._stop_unlocked()

            self._current_url = url
            self._playing = True
            self._paused = False

            # Try ffplay first (software fallback)
            ffplay = shutil.which("ffplay")
            if ffplay:
                try:
                    vol = max(0, min(100, int(self._volume)))
                    args = [
                        "ffplay",
                        "-nodisp",
                        "-autoexit",
                        "-vn",
                        "-loglevel",
                        "quiet",
                        "-volume",
                        str(vol),
                        url,
                    ]
                    self._logger.info("Safe backend: using ffplay")
                    self._proc = popen_silent(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as e:
                    self._logger.error("Error playing with ffplay: %s", e)
                    self._playing = False
            else:
                self._logger.warning("ffplay not available - audio may not play")
                self._playing = False

    def pause(self) -> None:
        """Pause playback"""
        with self._lock:
            if self._playing and not self._paused:
                self._paused = True
                # ffplay doesn't support pause via CLI, so we stop
                if self._proc is not None:
                    try:
                        self._proc.terminate()
                    except Exception as e:
                        self._logger.debug("Process termination failed: %s", e)

    def resume(self) -> None:
        """Resume playback"""
        with self._lock:
            if self._playing and self._paused:
                self._paused = False
                # Replay from URL
                if self._current_url:
                    # Avoid re-entrant locking via play_url since it acquires lock; do minimal sequence
                    url = self._current_url
                    self._stop_unlocked()
                    self._playing = True
                    self._paused = False
                    ffplay = shutil.which("ffplay")
                    if ffplay:
                        try:
                            vol = max(0, min(100, int(self._volume)))
                            args = [
                                "ffplay",
                                "-nodisp",
                                "-autoexit",
                                "-vn",
                                "-loglevel",
                                "quiet",
                                "-volume",
                                str(vol),
                                url,
                            ]
                            self._logger.info("Safe backend: using ffplay")
                            self._proc = popen_silent(
                                args,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        except Exception as e:
                            self._logger.error("Error playing with ffplay: %s", e)
                            self._playing = False
                    else:
                        self._logger.warning("ffplay not available - audio may not play")
                        self._playing = False

    def _stop_unlocked(self) -> None:
        """Internal stop that assumes caller holds _lock."""
        self._playing = False
        self._paused = False
        self._current_url = None

        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=TIMEOUT_DEFAULT)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=TIMEOUT_DEFAULT)
            except Exception as e:
                self._logger.warning("Error stopping process: %s", e)
            self._proc = None

    def stop(self) -> None:
        """Stop playback"""
        with self._lock:
            self._stop_unlocked()

    def is_playing(self) -> bool:
        """
        Check if currently playing.

        Returns:
            True if playing
        """
        with self._lock:
            if not self._playing or self._paused:
                return False

            if self._proc is not None:
                return self._proc.poll() is None

            return False

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
            # Volume applied on next play
            return self._volume

    def get_position(self) -> int:
        """Get current playback position in seconds (not supported)"""
        return 0

    def get_duration(self) -> int:
        """Get track duration in seconds (not supported)"""
        return 0

    def get_position_percentage(self) -> float:
        """Get playback position as percentage (not supported)"""
        return 0.0

    def seek(self, position_seconds: int) -> None:
        """Seek to position (not supported in safe backend)"""
        self._logger.warning("Seek not supported in safe backend")

    def cleanup(self) -> None:
        """Cleanup backend resources"""
        with self._lock:
            self._stop_unlocked()
            self._logger.debug("Safe backend: cleaned up")
