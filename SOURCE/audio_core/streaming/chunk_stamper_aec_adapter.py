"""
ChunkStamper AEC Reference Adapter.

Provides AEC reference audio by tapping the ChunkStamper output stream
instead of running a separate WASAPI loopback capture.  This gives the
wake word detector a reference signal that is exactly time-aligned with
what the speakers are playing (delayed by buffer_target_sec), with a
fixed, known delay — eliminating the variable 153-224ms WASAPI delay
that made auto-calibration impossible.

Implements the same duck-typed ``AECReferenceSource`` protocol as
``WasapiAECReferenceAdapter``.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
from scipy.signal import resample_poly

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

from .chunk_protocol import (
    CHANNELS,
    CHUNK_SIZE_BYTES,
    CHUNK_SIZE_BYTES_24,
    deserialize,
    unpack_int24_to_float32,
)
from .exceptions import ChunkDeserializationError

logger = get_logger(__name__)

# Minimum samples before serving real frames (10ms at 16kHz = 1 AEC frame)
# Must be small to avoid adding delay that exceeds the acoustic echo path.
# A large buffer (e.g. 800=50ms) delays the reference signal beyond the
# ~30ms acoustic echo delay, making FDAF convergence impossible (causal
# filter cannot model negative delays).
MIN_READY_SAMPLES = 160

# Adaptive buffer_target bounds (milliseconds)
MIN_BUFFER_TARGET_MS = 5.0
MAX_BUFFER_TARGET_MS = 300.0
DEFAULT_BUFFER_TARGET_MS = 100.0

# Exponential smoothing factor for adaptive delay updates
SMOOTHING_ALPHA = 0.2


class ChunkStamperAECAdapter:
    """AEC reference adapter backed by the ChunkStamper output stream.

    Subscribes to ChunkStamper frames, extracts PCM, downsamples
    48kHz stereo to 16kHz mono, and serves 10ms frames (160 samples)
    to the FDAF echo canceller.

    The fixed delay from ChunkStamper → speakers equals buffer_target_sec,
    so this adapter buffers that same delay before serving frames.  The
    FDAF receives reference audio at the exact same delay as the acoustic
    echo path, making delay calibration unnecessary.

    Implements:
        - start() -> bool
        - stop() -> None
        - get_aec_reference_frame(frame_samples, target_rate) -> np.ndarray
        - has_aec_reference() -> bool
    """

    # Ring buffer: 500ms at 16kHz
    BUFFER_SIZE_SAMPLES = 8000

    def __init__(
        self,
        stamper: object,
        buffer_target_sec: float = DEFAULT_BUFFER_TARGET_MS / 1000.0,
    ) -> None:
        self._stamper = stamper
        self._buffer_target_sec = buffer_target_sec
        self._target_rate = SAMPLE_RATE_16K

        self._buffer: deque[np.int16] = deque(maxlen=self.BUFFER_SIZE_SAMPLES)
        self._lock = threading.Lock()
        self._running = False

        # Startup gate: don't serve real frames until buffer reaches threshold
        self._startup_samples = int(buffer_target_sec * SAMPLE_RATE_16K)
        self._primed = False

        # Underrun protection: replay last good frame
        self._last_frame = np.zeros(160, dtype=np.int16)

    def start(self) -> bool:
        """Subscribe to ChunkStamper output and start buffering."""
        if self._running:
            return True

        try:
            add_output = getattr(self._stamper, "add_output", None)
            if not callable(add_output):
                logger.error("ChunkStamperAECAdapter: stamper has no add_output method")
                return False

            add_output(self._on_stamper_frame)
            self._running = True
            logger.info(
                "ChunkStamperAECAdapter started (buffer_target=%.3fs, startup_samples=%d)",
                self._buffer_target_sec,
                self._startup_samples,
            )
            return True
        except Exception:
            logger.exception("ChunkStamperAECAdapter failed to start")
            return False

    def stop(self) -> None:
        """Unsubscribe from ChunkStamper and stop."""
        if not self._running:
            return

        try:
            remove_output = getattr(self._stamper, "remove_output", None)
            if callable(remove_output):
                remove_output(self._on_stamper_frame)
        except Exception:
            logger.warning("Error removing ChunkStamperAECAdapter output")

        self._running = False
        logger.info("ChunkStamperAECAdapter stopped")

    def _on_stamper_frame(self, frame_bytes: bytes) -> None:
        """Process a serialized frame from ChunkStamper.

        Extracts PCM payload, converts stereo 48kHz PCM to mono 16kHz int16,
        and appends to the ring buffer. Production ChunkStamper output is
        currently 24-bit; legacy 16-bit frames remain supported for older
        callers and tests.
        """
        try:
            chunk = deserialize(frame_bytes)
            mono_48k = self._decode_stereo_payload(chunk.pcm_data)
            mono_16k = self._resample_48k_to_16k(mono_48k)

            with self._lock:
                self._buffer.extend(mono_16k)

        except ChunkDeserializationError as exc:
            logger.debug("ChunkStamperAECAdapter dropped malformed frame: %s", exc)
        except Exception:
            logger.debug("ChunkStamperAECAdapter frame processing error")

    def _decode_stereo_payload(self, pcm: bytes) -> np.ndarray:
        """Decode 16-bit or 24-bit stereo PCM to mono 48kHz float samples."""
        if len(pcm) == CHUNK_SIZE_BYTES_24:
            samples = unpack_int24_to_float32(pcm)
            if len(samples) % CHANNELS:
                raise ValueError("24-bit PCM payload is not channel-aligned")
            stereo = samples.reshape(-1, CHANNELS)
            return stereo.mean(axis=1) * 32768.0

        if len(pcm) == CHUNK_SIZE_BYTES:
            samples = np.frombuffer(pcm, dtype=np.int16)
            if len(samples) % CHANNELS:
                raise ValueError("16-bit PCM payload is not channel-aligned")
            stereo = samples.reshape(-1, CHANNELS).astype(np.float32)
            return stereo.mean(axis=1)

        raise ValueError("unsupported ChunkStamper PCM payload size: %d" % len(pcm))

    @staticmethod
    def _resample_48k_to_16k(mono_48k: np.ndarray) -> np.ndarray:
        """Resample mono 48kHz samples to 16kHz int16 AEC frames."""
        n_in = len(mono_48k)
        if n_in == 0:
            return np.zeros(0, dtype=np.int16)
        n_out = round(n_in * SAMPLE_RATE_16K / SAMPLE_RATE_48K)
        mono_16k = resample_poly(
            mono_48k.astype(np.float32, copy=False),
            1,
            3,
            padtype="line",
        )
        if len(mono_16k) < n_out:
            mono_16k = np.pad(mono_16k, (0, n_out - len(mono_16k)))
        elif len(mono_16k) > n_out:
            mono_16k = mono_16k[:n_out]
        return np.clip(np.rint(mono_16k), -32768, 32767).astype(np.int16)

    # ------------------------------------------------------------------ #
    # AECReferenceSource protocol                                         #
    # ------------------------------------------------------------------ #

    def get_aec_reference_frame(
        self,
        frame_samples: int,
        target_rate: int | None = None,
    ) -> np.ndarray:
        """Get a frame of reference audio for AEC.

        Implements startup gate (waits until buffer_target_sec accumulated)
        and underrun protection (replays last good frame).

        Args:
            frame_samples: Number of samples to return (typically 160 = 10ms at 16kHz).
            target_rate: Target sample rate (default 16kHz, ignored if already matching).

        Returns:
            int16 numpy array of frame_samples length.
        """
        with self._lock:
            # Startup gate: wait until enough samples buffered for delay alignment
            if not self._primed:
                if len(self._buffer) < max(self._startup_samples, MIN_READY_SAMPLES):
                    return np.zeros(frame_samples, dtype=np.int16)
                self._primed = True
                logger.info(
                    "ChunkStamperAECAdapter primed: buffer=%d samples",
                    len(self._buffer),
                )

            # Cap buffer delay: if production outpaces consumption, the
            # buffer grows and ref becomes stale.  Drain excess so the
            # FDAF always gets ref within ~30ms of real-time.  Without
            # this, buffer_len grows to 4000-6000 (250-375ms) because
            # the mic callback rate doesn't match the adapter's 50 FPS.
            max_buf = frame_samples * 3  # 480 samples = 30ms at 16kHz
            if len(self._buffer) > max_buf:
                drain = len(self._buffer) - max_buf
                for _ in range(drain):
                    self._buffer.popleft()

            if len(self._buffer) < frame_samples:
                # Underrun: replay last good frame, padded to correct size
                result = np.zeros(frame_samples, dtype=np.int16)
                n = min(len(self._last_frame), frame_samples)
                result[:n] = self._last_frame[:n]
                return result

            # FIFO: consume samples from left side
            consumed = []
            for _ in range(min(frame_samples, len(self._buffer))):
                consumed.append(self._buffer.popleft())
            frame_data = np.array(consumed, dtype=np.int16)

            # Save as last good frame for underrun protection
            self._last_frame = frame_data.copy()

        return frame_data

    def has_aec_reference(self) -> bool:
        """Return True if the adapter is running."""
        return self._running

    def has_sufficient_reference(self, min_samples: int = 160) -> bool:
        """Check if buffer has enough samples for AEC processing."""
        with self._lock:
            return self._running and len(self._buffer) >= min_samples

    def get_buffer_fill_level(self) -> tuple[int, int]:
        """Get current buffer fill level for diagnostics."""
        with self._lock:
            return len(self._buffer), self.BUFFER_SIZE_SAMPLES

    # ------------------------------------------------------------------ #
    # Adaptive buffer_target                                               #
    # ------------------------------------------------------------------ #

    def set_buffer_target(self, delay_ms: float) -> None:
        """Update buffer_target from a measured speaker-to-mic delay.

        Applies exponential smoothing to avoid jitter from noisy measurements.
        Clamps to [MIN_BUFFER_TARGET_MS, MAX_BUFFER_TARGET_MS].

        Args:
            delay_ms: Measured acoustic delay in milliseconds.
        """
        clamped = max(MIN_BUFFER_TARGET_MS, min(MAX_BUFFER_TARGET_MS, delay_ms))
        old_ms = self._buffer_target_sec * 1000.0

        # Exponential smoothing: target = (1 - alpha) * old + alpha * new
        new_sec = (1.0 - SMOOTHING_ALPHA) * self._buffer_target_sec + SMOOTHING_ALPHA * (clamped / 1000.0)

        with self._lock:
            self._buffer_target_sec = new_sec
            self._startup_samples = int(new_sec * SAMPLE_RATE_16K)

        logger.info(
            "[AEC_DELAY] updated buffer_target: %.0fms -> %.0fms (measured=%.0fms, clamped=%.0fms)",
            old_ms,
            new_sec * 1000.0,
            delay_ms,
            clamped,
        )

    def get_buffer_target_ms(self) -> float:
        """Return current buffer_target in milliseconds."""
        return self._buffer_target_sec * 1000.0

    def get_recent_reference(self, num_samples: int) -> np.ndarray:
        """Return a copy of the most recent N samples from the ring buffer.

        Used by the passive calibrator to cross-correlate against mic audio.
        Does NOT consume samples — this is a read-only snapshot.

        Args:
            num_samples: Number of samples to return (capped to buffer size).

        Returns:
            int16 numpy array (may be shorter than requested if buffer is small).
        """
        with self._lock:
            available = len(self._buffer)
            n = min(num_samples, available)
            if n == 0:
                return np.zeros(0, dtype=np.int16)
            # Snapshot the tail (most recent samples) without consuming
            buf_list = list(self._buffer)
            return np.array(buf_list[-n:], dtype=np.int16)


__all__ = ["ChunkStamperAECAdapter"]
