"""
Test tone capture provider.

Generates synthetic audio signals as PCM (48 kHz, stereo, 16-bit) for testing
and CI environments. Always available.

Supported signal types (set via ``signal_type`` constructor arg):
    tone           440 Hz sine wave (default)
    multitone      sum of 440 + 880 + 1760 + 3520 Hz harmonics
    sweep          linear frequency sweep 100 Hz → 20 kHz over 2 seconds
    white          white noise from PRNG with fixed seed
    tone_resampled 440 Hz sine generated at 44100 Hz, then resampled
                   to 48000 Hz via the same linear interpolation used
                   by wasapi_loopback.py
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Callable

from core.constants import AUDIO_CHANNELS_STEREO, AUDIO_INT16_MAX, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .base import AudioCaptureProvider

logger = get_logger(__name__)

# Chunk parameters: 20 ms at 48 kHz stereo 16-bit = 3840 bytes
_BYTES_PER_SAMPLE = 2
_CHUNK_DURATION_MS = 20
_SAMPLES_PER_CHUNK = (SAMPLE_RATE_48K * _CHUNK_DURATION_MS) // 1000  # 960
_CHUNK_SIZE_BYTES = _SAMPLES_PER_CHUNK * AUDIO_CHANNELS_STEREO * _BYTES_PER_SAMPLE

# Resampling source rate
_RESAMPLE_SRC_RATE = 44100
_RESAMPLE_SAMPLES_PER_CHUNK = (_RESAMPLE_SRC_RATE * _CHUNK_DURATION_MS) // 1000  # 882

# Valid signal types
SIGNAL_TYPES = ("tone", "multitone", "sweep", "white", "tone_resampled")

__all__ = ["SIGNAL_TYPES", "TestToneProvider"]


class TestToneProvider(AudioCaptureProvider):
    """
    Generate synthetic audio signals as a PCM capture source.

    Useful for integration tests and CI where no audio hardware is available.
    """

    def __init__(
        self,
        frequency: float = 440.0,
        amplitude: float = 0.5,
        signal_type: str = "tone",
    ) -> None:
        if signal_type not in SIGNAL_TYPES:
            raise ValueError("signal_type must be one of %s, got %r" % (SIGNAL_TYPES, signal_type))
        self._frequency = frequency
        self._amplitude = min(max(amplitude, 0.0), 1.0)
        self._signal_type = signal_type
        self._callback: Callable[[bytes, int, int, int], None] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
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
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._generate_loop,
            name="test-tone-capture",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        logger.info(
            "Test tone capture started: signal=%s freq=%d Hz amplitude=%.2f",
            self._signal_type,
            int(self._frequency),
            self._amplitude,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._running = False
        logger.debug("Test tone capture stopped")

    @classmethod
    def is_available(cls) -> bool:
        return True

    def get_metrics(self) -> dict:
        """Return capture metrics for pipeline diagnostics."""
        with self._metrics_lock:
            return {
                "provider": "test_tone",
                "signal_type": self._signal_type,
                "rms": self._last_rms,
                "chunks_produced": self._chunks_produced,
            }

    # ------------------------------------------------------------------ #
    # Main generation loop                                                #
    # ------------------------------------------------------------------ #

    def _generate_loop(self) -> None:
        """Background loop that emits PCM chunks at real-time rate."""
        import numpy as np

        interval = _CHUNK_DURATION_MS / 1000.0
        phase = 0.0
        chunk_index = 0

        # Set up per-signal-type state
        if self._signal_type == "white":
            rng = np.random.RandomState(seed=42)  # deterministic
        else:
            rng = None

        while not self._stop_event.is_set():
            t_start = time.monotonic()

            if self._signal_type == "tone":
                chunk, phase = self._gen_tone(np, phase)
            elif self._signal_type == "multitone":
                chunk, phase = self._gen_multitone(np, phase)
            elif self._signal_type == "sweep":
                chunk, phase = self._gen_sweep(np, phase, chunk_index)
            elif self._signal_type == "white":
                chunk = self._gen_white(np, rng)
            elif self._signal_type == "tone_resampled":
                chunk, phase = self._gen_tone_resampled(np, phase)
            else:
                chunk, phase = self._gen_tone(np, phase)

            chunk_index += 1

            if self._callback is not None:
                with self._metrics_lock:
                    self._chunks_produced += 1
                    if self._chunks_produced % 50 == 0:
                        self._last_rms = _compute_rms_bytes(chunk)
                self._callback(
                    chunk,
                    SAMPLE_RATE_48K,
                    AUDIO_CHANNELS_STEREO,
                    _BYTES_PER_SAMPLE,
                )

            elapsed = time.monotonic() - t_start
            sleep_time = interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    # ------------------------------------------------------------------ #
    # Signal generators (all return 48 kHz stereo int16 PCM bytes)        #
    # ------------------------------------------------------------------ #

    def _gen_tone(self, np, phase: float) -> tuple[bytes, float]:
        """440 Hz sine wave at 48 kHz."""
        phase_inc = 2.0 * math.pi * self._frequency / SAMPLE_RATE_48K
        t = np.arange(_SAMPLES_PER_CHUNK) * phase_inc + phase
        mono = (np.sin(t) * self._amplitude * AUDIO_INT16_MAX).astype(np.int16)
        stereo = np.column_stack((mono, mono)).flatten()
        new_phase = (phase + _SAMPLES_PER_CHUNK * phase_inc) % (2.0 * math.pi)
        return stereo.tobytes(), new_phase

    def _gen_multitone(self, np, phase: float) -> tuple[bytes, float]:
        """Sum of 440 + 880 + 1760 + 3520 Hz at 48 kHz."""
        freqs = [440.0, 880.0, 1760.0, 3520.0]
        t_base = np.arange(_SAMPLES_PER_CHUNK, dtype=np.float64)
        signal = np.zeros(_SAMPLES_PER_CHUNK, dtype=np.float64)
        for f in freqs:
            phase_inc = 2.0 * math.pi * f / SAMPLE_RATE_48K
            signal += np.sin(t_base * phase_inc + phase * (f / 440.0))
        # Normalize so peak amplitude matches self._amplitude
        signal *= self._amplitude / len(freqs)
        mono = (signal * AUDIO_INT16_MAX).astype(np.int16)
        stereo = np.column_stack((mono, mono)).flatten()
        # Phase tracks the fundamental
        fund_inc = 2.0 * math.pi * 440.0 / SAMPLE_RATE_48K
        new_phase = (phase + _SAMPLES_PER_CHUNK * fund_inc) % (2.0 * math.pi)
        return stereo.tobytes(), new_phase

    def _gen_sweep(self, np, phase: float, chunk_index: int) -> tuple[bytes, float]:
        """Linear frequency sweep 100 Hz → 20 kHz over 2 seconds, repeating."""
        sweep_duration = 2.0  # seconds
        f_start = 100.0
        f_end = 20000.0
        samples_per_sweep = int(SAMPLE_RATE_48K * sweep_duration)  # 96000

        mono = np.empty(_SAMPLES_PER_CHUNK, dtype=np.float64)
        sample_offset = (chunk_index * _SAMPLES_PER_CHUNK) % samples_per_sweep

        for i in range(_SAMPLES_PER_CHUNK):
            t_in_sweep = ((sample_offset + i) % samples_per_sweep) / SAMPLE_RATE_48K
            f_inst = f_start + (f_end - f_start) * t_in_sweep / sweep_duration
            phase += 2.0 * math.pi * f_inst / SAMPLE_RATE_48K
            mono[i] = math.sin(phase)

        phase = phase % (2.0 * math.pi)
        int_mono = (mono * self._amplitude * AUDIO_INT16_MAX).astype(np.int16)
        stereo = np.column_stack((int_mono, int_mono)).flatten()
        return stereo.tobytes(), phase

    def _gen_white(self, np, rng) -> bytes:
        """White noise from deterministic PRNG (seed=42)."""
        # Uniform [-1, 1] then scale
        noise = rng.uniform(-1.0, 1.0, _SAMPLES_PER_CHUNK)
        mono = (noise * self._amplitude * AUDIO_INT16_MAX).astype(np.int16)
        stereo = np.column_stack((mono, mono)).flatten()
        return stereo.tobytes()

    def _gen_tone_resampled(self, np, phase: float) -> tuple[bytes, float]:
        """440 Hz sine at 44100 Hz, resampled to 48000 Hz via linear interp.

        Uses the exact same ``_resample_int16`` function from
        wasapi_loopback.py to test the resampler in isolation.
        """
        from audio_core.capture.wasapi_loopback import _resample_int16

        # Generate at 44100 Hz
        phase_inc = 2.0 * math.pi * self._frequency / _RESAMPLE_SRC_RATE
        t = np.arange(_RESAMPLE_SAMPLES_PER_CHUNK) * phase_inc + phase
        mono = (np.sin(t) * self._amplitude * AUDIO_INT16_MAX).astype(np.int16)
        stereo = np.column_stack((mono, mono)).flatten()
        raw_bytes = stereo.tobytes()

        # Resample 44100 → 48000 using production linear interpolation
        resampled = _resample_int16(
            raw_bytes,
            _RESAMPLE_SRC_RATE,
            SAMPLE_RATE_48K,
            AUDIO_CHANNELS_STEREO,
        )

        new_phase = (phase + _RESAMPLE_SAMPLES_PER_CHUNK * phase_inc) % (2.0 * math.pi)
        return resampled, new_phase


def _compute_rms_bytes(pcm: bytes) -> float:
    """Compute RMS of int16 PCM data, normalized to [0, 1]."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    fmt = "<%dh" % n
    samples = struct.unpack(fmt, pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n) / AUDIO_INT16_MAX
