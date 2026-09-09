"""
PulseAudio/PipeWire monitor capture provider for Linux.

Captures system audio output via a PulseAudio monitor source using
sounddevice, delivering 48 kHz stereo 16-bit PCM chunks.
"""

from __future__ import annotations

import platform
import threading
from collections.abc import Callable

from core.constants import AUDIO_CHANNELS_STEREO, AUDIO_INT16_MAX, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .base import AudioCaptureProvider

logger = get_logger(__name__)

IS_LINUX = platform.system() == "Linux"

# Chunk size: 20 ms at 48 kHz, stereo, 16-bit = 3840 bytes
_BYTES_PER_SAMPLE = 2
_CHUNK_DURATION_MS = 20
_CHUNK_SIZE_BYTES = (_CHUNK_DURATION_MS * SAMPLE_RATE_48K * AUDIO_CHANNELS_STEREO * _BYTES_PER_SAMPLE) // 1000

__all__ = ["PulseMonitorProvider"]


class PulseMonitorProvider(AudioCaptureProvider):
    """Capture system audio on Linux via PulseAudio/PipeWire monitor source."""

    def __init__(self) -> None:
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._stream = None
        self._running = False
        self._lock = threading.Lock()
        self._buffer = bytearray()

    # ------------------------------------------------------------------ #
    # AudioCaptureProvider interface                                      #
    # ------------------------------------------------------------------ #

    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        self._callback = fn

    def start(self) -> None:
        if not self.is_available():
            raise RuntimeError("PulseAudio monitor is not available on this platform")

        with self._lock:
            if self._running:
                return

            import numpy as np
            import sounddevice as sd
            from scipy.signal import resample_poly as _resample_poly

            device_idx = self._find_monitor_source()
            if device_idx is None:
                raise RuntimeError("No PulseAudio monitor source found")

            dev_info = sd.query_devices(device_idx)
            dev_rate = int(dev_info.get("default_samplerate", SAMPLE_RATE_48K))
            dev_channels = min(
                int(dev_info.get("max_input_channels", AUDIO_CHANNELS_STEREO)),
                AUDIO_CHANNELS_STEREO,
            )

            logger.info(
                "Starting PulseAudio monitor capture: device='%s' rate=%d ch=%d",
                dev_info.get("name", "unknown"),
                dev_rate,
                dev_channels,
            )

            def _sd_callback(indata, frames, time_info, status):
                if status:
                    logger.debug("Pulse monitor stream status: %s", status)
                if self._callback is None or indata is None:
                    return

                # indata is float32 (frames, dev_channels). Normalize to the
                # contract every downstream consumer (ChunkStamper / AudioTee)
                # assumes: 48 kHz, stereo, int16. The monitor source's native
                # rate/channel layout is NOT guaranteed to be 48 kHz stereo
                # (mono monitors and 44.1 kHz sinks are common on Linux), so
                # we resample + (up/down)mix here rather than mislabel the PCM
                # — emitting a chunk tagged 48 kHz/stereo that was actually a
                # different rate/layout plays back at the wrong speed/pitch on
                # spoke devices.
                int16_data = self._conform_to_48k_stereo(indata, dev_rate, _resample_poly)
                self._buffer.extend(int16_data.tobytes())
                while len(self._buffer) >= _CHUNK_SIZE_BYTES:
                    chunk = bytes(self._buffer[:_CHUNK_SIZE_BYTES])
                    del self._buffer[:_CHUNK_SIZE_BYTES]
                    self._callback(
                        chunk,
                        SAMPLE_RATE_48K,
                        AUDIO_CHANNELS_STEREO,
                        _BYTES_PER_SAMPLE,
                    )

            self._stream = sd.InputStream(
                device=device_idx,
                samplerate=dev_rate,
                channels=dev_channels,
                dtype="float32",
                callback=_sd_callback,
                blocksize=int(dev_rate * _CHUNK_DURATION_MS / 1000),
            )
            self._stream.start()
            self._running = True
            logger.info("PulseAudio monitor capture started")

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    logger.debug("Error closing Pulse monitor stream")
                self._stream = None
            self._running = False
            self._buffer.clear()
            logger.debug("PulseAudio monitor capture stopped")

    @classmethod
    def is_available(cls) -> bool:
        if not IS_LINUX:
            return False
        try:
            from importlib import import_module

            import_module("sounddevice")

            return cls._find_monitor_source() is not None
        except ImportError:
            return False

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_monitor_source() -> int | None:
        """Find a PulseAudio monitor source device index."""
        try:
            import sounddevice as sd

            from audio_core.portaudio_guard import sounddevice_guard

            with sounddevice_guard():
                devices = sd.query_devices()
            if not isinstance(devices, list):
                devices = [devices]

            for i, dev in enumerate(devices):
                name = str(dev.get("name", "")).lower()
                if "monitor" in name and int(dev.get("max_input_channels", 0)) > 0:
                    return i

            return None
        except Exception:
            return None

    @staticmethod
    def _conform_to_48k_stereo(indata, dev_rate, resample_poly):
        """Conform a float32 capture block to 48 kHz stereo int16.

        ``indata`` is the sounddevice float32 block, shape ``(frames,)`` or
        ``(frames, dev_channels)``. Returns an int16 numpy array of shape
        ``(frames_out * 2,)`` interleaved L/R, so ``tobytes()`` yields the
        exact byte layout ChunkStamper / AudioTee expect for a 48 kHz stereo
        16-bit stream.

        Channel handling: >= 2 channels are truncated to the first two; a
        single channel is duplicated to both L and R. Sample-rate handling:
        polyphase FIR resampling (anti-aliased) when ``dev_rate`` differs
        from 48 kHz, otherwise pass-through. Without this conform, a monitor
        running at e.g. 44.1 kHz or mono would be emitted tagged as 48 kHz
        stereo, playing back at the wrong speed/pitch on spoke devices.
        """
        import numpy as np

        block = np.asarray(indata, dtype=np.float32)
        if block.ndim == 1:
            block = block.reshape(-1, 1)

        # Channel conform -> stereo.
        if block.shape[1] >= AUDIO_CHANNELS_STEREO:
            stereo = block[:, :AUDIO_CHANNELS_STEREO]
        else:
            stereo = np.repeat(block[:, :1], AUDIO_CHANNELS_STEREO, axis=1)

        # Sample-rate conform -> 48 kHz via polyphase FIR (anti-aliased).
        if dev_rate != SAMPLE_RATE_48K and stereo.shape[0] > 0:
            from math import gcd

            g = gcd(SAMPLE_RATE_48K, int(dev_rate))
            up = SAMPLE_RATE_48K // g
            down = int(dev_rate) // g
            left = resample_poly(stereo[:, 0].astype(np.float64), up, down)
            right = resample_poly(stereo[:, 1].astype(np.float64), up, down)
            n = min(len(left), len(right))
            conformed = np.empty((n, AUDIO_CHANNELS_STEREO), dtype=np.float32)
            conformed[:, 0] = left[:n]
            conformed[:, 1] = right[:n]
            stereo = conformed

        return np.clip(
            stereo * AUDIO_INT16_MAX,
            -AUDIO_INT16_MAX - 1,
            AUDIO_INT16_MAX,
        ).astype(np.int16)
