"""
WASAPI Loopback capture provider for Windows.

Captures system audio output via WASAPI loopback, delivering 48 kHz
stereo 16-bit PCM chunks.  Uses ``pyaudiowpatch`` (preferred) which
exposes real WASAPI loopback devices for every output endpoint.  Falls
back to ``sounddevice`` Stereo-Mix detection if pyaudiowpatch is not
installed.
"""

from __future__ import annotations

import math
import platform
import threading
from collections.abc import Callable

import numpy as np
import soxr

from audio_core.portaudio_guard import PORTAUDIO_LOCK, open_stream, terminate_portaudio
from core.constants import AUDIO_CHANNELS_STEREO, AUDIO_INT16_MAX, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .base import AudioCaptureProvider

logger = get_logger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# Chunk parameters: 20 ms at 48 kHz, stereo, 16-bit = 3840 bytes
_BYTES_PER_SAMPLE = 2
_CHUNK_DURATION_MS = 20
_SAMPLES_PER_CHUNK = (SAMPLE_RATE_48K * _CHUNK_DURATION_MS) // 1000  # 960
_CHUNK_SIZE_BYTES = _SAMPLES_PER_CHUNK * AUDIO_CHANNELS_STEREO * _BYTES_PER_SAMPLE  # 3840

__all__ = ["WasapiLoopbackProvider"]


class WasapiLoopbackProvider(AudioCaptureProvider):
    """Capture system audio on Windows via WASAPI loopback."""

    def __init__(self) -> None:
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._stream = None
        self._pyaudio_instance = None
        self._running = False
        self._lock = threading.Lock()
        self._buffer = bytearray()
        # Diagnostics
        self._chunks_produced: int = 0
        self._last_rms: float = 0.0
        self._metrics_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # AudioCaptureProvider interface                                      #
    # ------------------------------------------------------------------ #

    def set_callback(self, fn: Callable[[bytes, int, int, int], None]) -> None:
        self._callback = fn

    def start(self) -> None:
        if not self.is_available():
            raise RuntimeError("WASAPI loopback is not available on this platform")

        with self._lock:
            if self._running:
                return

            # Try pyaudiowpatch first (preferred — real WASAPI loopback)
            if _has_pyaudiowpatch():
                self._start_pyaudiowpatch()
            else:
                self._start_sounddevice()

            self._running = True

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    if hasattr(self._stream, "stop_stream"):
                        # pyaudiowpatch stream
                        self._stream.stop_stream()
                        self._stream.close()
                    elif hasattr(self._stream, "close"):
                        # sounddevice stream
                        self._stream.close()
                except Exception:
                    logger.debug("Error closing WASAPI loopback stream")
                self._stream = None

            if self._pyaudio_instance is not None:
                try:
                    # terminate_portaudio (not a raw .terminate()) so any guarded
                    # stream still open on this instance is quiesced first —
                    # pyaudio's own terminate() closes them from THIS thread (#4650).
                    terminate_portaudio(self._pyaudio_instance)
                except Exception:
                    logger.debug("Error terminating PyAudio instance")
                self._pyaudio_instance = None

            self._running = False
            self._buffer.clear()
            logger.debug("WASAPI loopback capture stopped")

    @classmethod
    def is_available(cls) -> bool:
        if not IS_WINDOWS:
            return False
        if _has_pyaudiowpatch():
            return _find_pyaudiowpatch_loopback() is not None
        return _find_sounddevice_loopback() is not None

    def get_metrics(self) -> dict:
        """Return capture metrics for pipeline diagnostics."""
        with self._metrics_lock:
            return {
                "provider": "wasapi_loopback",
                "rms": self._last_rms,
                "chunks_produced": self._chunks_produced,
            }

    def _track_chunk(self, chunk: bytes) -> None:
        """Update RMS metric every 50th chunk (~1/sec)."""
        with self._metrics_lock:
            self._chunks_produced += 1
            if self._chunks_produced % 50 == 0:
                self._last_rms = _compute_rms_int16(chunk)

    # ------------------------------------------------------------------ #
    # pyaudiowpatch backend (preferred)                                   #
    # ------------------------------------------------------------------ #

    def _start_pyaudiowpatch(self) -> None:
        """Start capture using pyaudiowpatch WASAPI loopback."""
        import pyaudiowpatch as pyaudio

        with PORTAUDIO_LOCK:
            p = pyaudio.PyAudio()
        self._pyaudio_instance = p

        loopback_info = _find_pyaudiowpatch_loopback(p)
        if loopback_info is None:
            terminate_portaudio(p)
            self._pyaudio_instance = None
            raise RuntimeError("No WASAPI loopback device found via pyaudiowpatch")

        dev_name = loopback_info["name"]
        dev_rate = int(loopback_info["defaultSampleRate"])
        dev_channels = min(int(loopback_info["maxInputChannels"]), AUDIO_CHANNELS_STEREO)
        dev_index = int(loopback_info["index"])

        logger.info(
            "Starting WASAPI loopback (pyaudiowpatch): device='%s' rate=%d ch=%d",
            dev_name,
            dev_rate,
            dev_channels,
        )

        # Calculate frames per buffer for ~20ms at device rate
        frames_per_buffer = int(dev_rate * _CHUNK_DURATION_MS / 1000)
        need_resample = dev_rate != SAMPLE_RATE_48K

        if need_resample:
            logger.info(
                "Device rate %d != target %d; resampling via soxr",
                dev_rate,
                SAMPLE_RATE_48K,
            )

        def _pa_callback(in_data, frame_count, time_info, status):
            if self._callback is None or in_data is None:
                return (None, pyaudio.paContinue)

            # in_data is bytes in the format we requested (int16)
            if need_resample:
                resampled = _resample_int16(
                    in_data,
                    dev_rate,
                    SAMPLE_RATE_48K,
                    dev_channels,
                )
                self._buffer.extend(resampled)
            else:
                self._buffer.extend(in_data)

            # Deliver complete chunks
            while len(self._buffer) >= _CHUNK_SIZE_BYTES:
                chunk = bytes(self._buffer[:_CHUNK_SIZE_BYTES])
                del self._buffer[:_CHUNK_SIZE_BYTES]
                self._track_chunk(chunk)
                self._callback(
                    chunk,
                    SAMPLE_RATE_48K,
                    AUDIO_CHANNELS_STEREO,
                    _BYTES_PER_SAMPLE,
                )

            return (None, pyaudio.paContinue)

        self._stream = open_stream(
            p,
            format=pyaudio.paInt16,
            channels=dev_channels,
            rate=dev_rate,
            input=True,
            input_device_index=dev_index,
            frames_per_buffer=frames_per_buffer,
            stream_callback=_pa_callback,
        )
        self._stream.start_stream()
        logger.info("WASAPI loopback capture started (pyaudiowpatch)")

    # ------------------------------------------------------------------ #
    # sounddevice fallback                                                #
    # ------------------------------------------------------------------ #

    def _start_sounddevice(self) -> None:
        """Start capture using sounddevice (Stereo Mix / named loopback)."""
        import numpy as np
        import sounddevice as sd

        from core.constants import AUDIO_INT16_MAX

        device_idx = _find_sounddevice_loopback()
        if device_idx is None:
            raise RuntimeError("No WASAPI loopback device found. " "Enable Stereo Mix or install pyaudiowpatch.")

        dev_info = sd.query_devices(device_idx)
        dev_rate = int(dev_info.get("default_samplerate", SAMPLE_RATE_48K))
        dev_channels = min(
            int(dev_info.get("max_input_channels", AUDIO_CHANNELS_STEREO)),
            AUDIO_CHANNELS_STEREO,
        )

        logger.info(
            "Starting WASAPI loopback (sounddevice): device='%s' rate=%d ch=%d",
            dev_info.get("name", "unknown"),
            dev_rate,
            dev_channels,
        )

        def _sd_callback(indata, frames, time_info, status):
            if status:
                logger.debug("WASAPI loopback stream status: %s", status)
            if self._callback is None or indata is None:
                return
            int16_data = (indata * AUDIO_INT16_MAX).astype(np.int16)
            raw = int16_data.tobytes()
            self._buffer.extend(raw)
            while len(self._buffer) >= _CHUNK_SIZE_BYTES:
                chunk = bytes(self._buffer[:_CHUNK_SIZE_BYTES])
                del self._buffer[:_CHUNK_SIZE_BYTES]
                self._track_chunk(chunk)
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
        logger.info("WASAPI loopback capture started (sounddevice)")


# ---------------------------------------------------------------------- #
# Module-level helpers                                                    #
# ---------------------------------------------------------------------- #


def _compute_rms_int16(pcm: bytes) -> float:
    """Compute RMS of int16 PCM data, normalized to [0, 1]."""
    n_samples = len(pcm) // 2
    if n_samples == 0:
        return 0.0
    samples = np.frombuffer(pcm[: n_samples * 2], dtype=np.int16)
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / n_samples) / AUDIO_INT16_MAX


def _has_pyaudiowpatch() -> bool:
    """Return True if pyaudiowpatch is importable."""
    try:
        from importlib import import_module

        import_module("pyaudiowpatch")

        return True
    except ImportError:
        return False


def _find_pyaudiowpatch_loopback(p=None) -> dict | None:
    """Find the WASAPI loopback device for the default output endpoint.

    Override the target output device by setting ``VIOLA_WASAPI_DEVICE_NAME``
    to a substring of the desired output device name.  This is useful for
    testing or when the default output device (e.g. USB headphones) doesn't
    support WASAPI loopback capture.
    """
    import os

    try:
        import pyaudiowpatch as pyaudio

        own_instance = p is None
        if own_instance:
            with PORTAUDIO_LOCK:
                p = pyaudio.PyAudio()

        try:
            wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)

            # Check for device name override
            override_name = os.environ.get("VIOLA_WASAPI_DEVICE_NAME")
            if override_name:
                # Find loopback matching the override name
                for loopback in p.get_loopback_device_info_generator():
                    if override_name in loopback["name"]:
                        logger.info(
                            "Using WASAPI loopback override: %s",
                            loopback["name"],
                        )
                        return loopback
                logger.warning(
                    "VIOLA_WASAPI_DEVICE_NAME='%s' not found in loopback devices",
                    override_name,
                )

            default_output_idx = wasapi["defaultOutputDevice"]
            default_output = p.get_device_info_by_index(default_output_idx)
            default_name = default_output["name"]

            # Find the loopback device matching the default output
            for loopback in p.get_loopback_device_info_generator():
                if default_name in loopback["name"]:
                    return loopback

            # If no match for default, return the first available loopback
            for loopback in p.get_loopback_device_info_generator():
                return loopback

            return None
        finally:
            if own_instance:
                with PORTAUDIO_LOCK:
                    p.terminate()
    except Exception:
        return None


def _find_sounddevice_loopback() -> int | None:
    """Find a WASAPI loopback or Stereo Mix device via sounddevice."""
    try:
        import sounddevice as sd

        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            devices = sd.query_devices()
        if not isinstance(devices, list):
            devices = [devices]

        for i, dev in enumerate(devices):
            name = str(dev.get("name", "")).lower()
            if "loopback" in name and int(dev.get("max_input_channels", 0)) > 0:
                return i

        for i, dev in enumerate(devices):
            name = str(dev.get("name", "")).lower()
            if any(term in name for term in ("stereo mix", "what you hear", "wave out mix")):
                if int(dev.get("max_input_channels", 0)) > 0:
                    return i

        return None
    except Exception:
        return None


def _resample_int16(
    data: bytes,
    src_rate: int,
    dst_rate: int,
    channels: int,
) -> bytes:
    """
    Resample interleaved int16 PCM data with soxr.

    Uses band-limited resampling for device-rate conversion (for example, 44100 -> 48000).
    """
    sample_size = 2  # int16
    frame_size = channels * sample_size
    n_frames = len(data) // frame_size

    if n_frames == 0 or src_rate == dst_rate:
        return data

    samples = np.frombuffer(data[: n_frames * frame_size], dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(n_frames, channels)
    resampled = soxr.resample(samples, src_rate, dst_rate, quality="HQ")
    return np.clip(np.rint(resampled), -32768, AUDIO_INT16_MAX).astype(np.int16).tobytes()
