"""
WASAPI Loopback Capture
=======================

Low-level WASAPI loopback capture for Windows.
Uses PyAudioWPatch for native WASAPI loopback support.
"""

from __future__ import annotations

import platform
import threading
from collections.abc import Callable
from typing import Protocol

from audio_core.portaudio_guard import PORTAUDIO_LOCK, open_stream, terminate_portaudio
from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

logger = get_logger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# Try to import PyAudioWPatch for native WASAPI loopback
_PYAUDIOWPATCH_AVAILABLE = False
try:
    if IS_WINDOWS:
        import pyaudiowpatch as pyaudio

        _PYAUDIOWPATCH_AVAILABLE = True
except ImportError:
    pyaudio = None


def _safe_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return int(default)
    return int(default)


class _PyAudioStream(Protocol):
    def start_stream(self) -> None: ...

    def stop_stream(self) -> None: ...

    def close(self) -> None: ...


class _SounddeviceStream(Protocol):
    def start(self) -> None: ...

    def close(self) -> None: ...


class NativeWasapiLoopback:
    """
    Native WASAPI loopback capture.

    Captures system audio output (speakers/headphones) for AEC reference.
    Uses PyAudioWPatch which provides native WASAPI loopback support on Windows.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE_16K, channels: int = 1):
        """
        Initialize loopback capture.

        Args:
            sample_rate: Target sample rate for captured audio
            channels: Number of output channels (1=mono, 2=stereo)
        """
        self._target_sample_rate = sample_rate
        self._target_channels = channels
        self._pyaudio_stream: _PyAudioStream | None = None
        self._sounddevice_stream: _SounddeviceStream | None = None
        self._pyaudio: pyaudio.PyAudio | None = None
        self._running = False
        self._callback: Callable[[bytes], None] | None = None
        self._lock = threading.Lock()
        self._loopback_device_info: dict[str, object] | None = None

    def start(self, callback: Callable[[bytes], None]) -> bool:
        """
        Start loopback capture.

        Args:
            callback: Function called with PCM audio bytes (int16)

        Returns:
            True if started successfully
        """
        if not IS_WINDOWS:
            logger.warning("WASAPI loopback only available on Windows")
            return False

        if not _PYAUDIOWPATCH_AVAILABLE:
            logger.warning("PyAudioWPatch not available - install with: pip install pyaudiowpatch")
            return self._fallback_sounddevice_start(callback)

        with self._lock:
            if self._running:
                return True

            self._callback = callback

            try:
                with PORTAUDIO_LOCK:
                    self._pyaudio = pyaudio.PyAudio()

                # Find loopback device for default output
                loopback_device = self._find_loopback_for_default_output()
                if loopback_device is None:
                    logger.warning("No WASAPI loopback device found")
                    terminate_portaudio(self._pyaudio)
                    self._pyaudio = None
                    return False

                self._loopback_device_info = loopback_device
                device_rate = _safe_int(
                    loopback_device.get("defaultSampleRate", SAMPLE_RATE_48K),
                    SAMPLE_RATE_48K,
                )
                device_channels = min(_safe_int(loopback_device.get("maxInputChannels", 2), 2), 2)

                logger.info(
                    "Starting WASAPI loopback: device='%s', rate=%d, ch=%d",
                    loopback_device.get("name", "unknown"),
                    device_rate,
                    device_channels,
                )

                def stream_callback(in_data, frame_count, time_info, status):
                    if self._callback and in_data:
                        self._callback(in_data)
                    return (None, pyaudio.paContinue)

                stream = open_stream(
                    self._pyaudio,
                    format=pyaudio.paInt16,
                    channels=device_channels,
                    rate=device_rate,
                    input=True,
                    input_device_index=_safe_int(loopback_device["index"], 0),
                    frames_per_buffer=int(device_rate * 0.01),  # 10ms blocks
                    stream_callback=stream_callback,
                )
                self._pyaudio_stream = stream
                stream.start_stream()
                self._running = True

                logger.info("WASAPI loopback started (native)")
                return True

            except Exception as e:
                logger.error("Failed to start loopback: %s", e)
                if self._pyaudio:
                    terminate_portaudio(self._pyaudio)
                    self._pyaudio = None
                self._running = False
                return False

    def stop(self) -> None:
        """Stop loopback capture."""
        with self._lock:
            if self._pyaudio_stream is not None:
                try:
                    self._pyaudio_stream.stop_stream()
                    self._pyaudio_stream.close()
                except Exception as e:
                    logger.debug("Error stopping stream: %s", e)
                self._pyaudio_stream = None

            if self._sounddevice_stream is not None:
                try:
                    self._sounddevice_stream.close()
                except Exception as e:
                    logger.debug("Error stopping fallback stream: %s", e)
                self._sounddevice_stream = None

            if self._pyaudio:
                try:
                    # Drains any guarded stream on this instance before
                    # Pa_Terminate, which would otherwise free it from here (#4650).
                    terminate_portaudio(self._pyaudio)
                except Exception as e:
                    logger.debug("Error terminating PyAudio: %s", e)
                self._pyaudio = None

            self._running = False
            self._callback = None
            self._loopback_device_info = None
            logger.debug("WASAPI loopback stopped")

    def _find_loopback_for_default_output(self) -> dict[str, object] | None:
        """
        Find the WASAPI loopback device for the default output.

        Uses PyAudioWPatch's loopback device generator to find the
        loopback endpoint matching the system's default audio output.
        """
        if not self._pyaudio:
            return None

        try:
            # Get WASAPI host API info
            wasapi_info = self._pyaudio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output_idx = wasapi_info.get("defaultOutputDevice")

            if default_output_idx is None or default_output_idx < 0:
                logger.warning("No default WASAPI output device found")
                return None

            # Get default output device info
            default_output = self._pyaudio.get_device_info_by_index(default_output_idx)
            default_name = str(default_output.get("name", ""))

            logger.debug("Looking for loopback matching: %s", default_name)

            # Extract base name (before the parenthetical driver info)
            default_name_prefix = default_name.split(" (")[0]

            # Find loopback device matching default output
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                loopback_name = str(loopback.get("name", ""))
                if loopback_name.startswith(default_name_prefix):
                    logger.debug(
                        "Found loopback device: %s (index %d)",
                        loopback_name,
                        loopback["index"],
                    )
                    return dict(loopback)

            # Fallback: return first loopback device if no match
            logger.warning("No loopback matching '%s', using first available", default_name)
            for loopback in self._pyaudio.get_loopback_device_info_generator():
                logger.debug("Using fallback loopback: %s", loopback.get("name"))
                return dict(loopback)

            return None

        except Exception as e:
            logger.error("Error finding loopback device: %s", e)
            return None

    def _fallback_sounddevice_start(self, callback: Callable[[bytes], None]) -> bool:
        """
        Fallback to sounddevice if PyAudioWPatch is not available.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            logger.warning("sounddevice not available for fallback loopback capture")
            return False

        with self._lock:
            if self._running:
                return True

            self._callback = callback

            loopback_idx = self._find_loopback_device_legacy()
            if loopback_idx is None:
                logger.warning("No WASAPI loopback device found")
                return False

            try:
                device_info = sd.query_devices(loopback_idx)
                device_rate = int(device_info.get("default_samplerate", SAMPLE_RATE_48K))
                device_channels = min(int(device_info.get("max_input_channels", 2)), 2)

                logger.info(
                    "Starting WASAPI loopback (fallback): device='%s', rate=%d, ch=%d",
                    device_info.get("name", "unknown"),
                    device_rate,
                    device_channels,
                )

                def stream_callback(indata, frames, time_info, status):
                    if status:
                        logger.debug("Loopback stream status: %s", status)
                    if self._callback and indata is not None:
                        int16_data = (indata * AUDIO_INT16_MAX).astype(np.int16)
                        self._callback(int16_data.tobytes())

                stream = sd.InputStream(
                    device=loopback_idx,
                    samplerate=device_rate,
                    channels=device_channels,
                    dtype="float32",
                    callback=stream_callback,
                    blocksize=int(device_rate * 0.01),
                )
                self._sounddevice_stream = stream
                stream.start()
                self._running = True

                logger.info("WASAPI loopback started (fallback)")
                return True

            except Exception as e:
                logger.error("Failed to start fallback loopback: %s", e)
                self._running = False
                return False

    def _find_loopback_device_legacy(self) -> int | None:
        """Find a WASAPI loopback device index using legacy pattern matching."""
        try:
            import sounddevice as sd

            from audio_core.portaudio_guard import sounddevice_guard

            with sounddevice_guard():
                devices = sd.query_devices()

            # Priority 1: Explicit loopback devices
            for i, dev in enumerate(devices):
                name = dev.get("name", "").lower()
                if "loopback" in name and dev.get("max_input_channels", 0) > 0:
                    return i

            # Priority 2: Stereo Mix / What You Hear
            for i, dev in enumerate(devices):
                name = dev.get("name", "").lower()
                if any(term in name for term in ["stereo mix", "what you hear", "wave out mix"]):
                    if dev.get("max_input_channels", 0) > 0:
                        return i

            return None

        except Exception as e:
            logger.debug("Error finding loopback device: %s", e)
            return None

    @property
    def is_running(self) -> bool:
        """Check if capture is running."""
        return self._running

    @property
    def sample_rate(self) -> int:
        """Get configured sample rate."""
        return self._target_sample_rate

    @property
    def loopback_device_name(self) -> str | None:
        """Get the name of the active loopback device."""
        if self._loopback_device_info:
            name = self._loopback_device_info.get("name")
            return name if isinstance(name, str) else None
        return None


def get_loopback_device() -> dict | None:
    """
    Get information about the default loopback device.

    Returns:
        Device info dict or None if not available.
    """
    if not IS_WINDOWS or not _PYAUDIOWPATCH_AVAILABLE:
        return None

    try:
        # Hold the process-wide PortAudio lock across the whole create -> enumerate
        # -> terminate sequence (pyaudiowpatch can't use portaudio_instance(), which
        # imports plain pyaudio). The lock also prevents another thread from tearing
        # the host-API table down mid-enumeration.
        with PORTAUDIO_LOCK:
            p = pyaudio.PyAudio()
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_output_idx = wasapi_info.get("defaultOutputDevice")

                if default_output_idx is None or default_output_idx < 0:
                    return None

                default_output = p.get_device_info_by_index(default_output_idx)
                default_name_prefix = default_output.get("name", "").split(" (")[0]

                for loopback in p.get_loopback_device_info_generator():
                    if loopback.get("name", "").startswith(default_name_prefix):
                        return {
                            "index": loopback["index"],
                            "name": loopback["name"],
                            "defaultSampleRate": loopback["defaultSampleRate"],
                            "maxInputChannels": loopback["maxInputChannels"],
                        }

                # Return first loopback if no match
                for loopback in p.get_loopback_device_info_generator():
                    return {
                        "index": loopback["index"],
                        "name": loopback["name"],
                        "defaultSampleRate": loopback["defaultSampleRate"],
                        "maxInputChannels": loopback["maxInputChannels"],
                    }

                return None
            finally:
                p.terminate()

    except Exception as e:
        logger.debug("Error getting loopback device: %s", e)
        return None


__all__ = [
    "IS_WINDOWS",
    "_PYAUDIOWPATCH_AVAILABLE",
    "NativeWasapiLoopback",
    "get_loopback_device",
]
