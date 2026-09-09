"""Capture-source-agnostic half of an AEC reference adapter.

An AEC reference adapter answers one question for the wake detector: "what is
coming out of the speakers right now?", so the echo canceller can subtract it
from what the microphone hears. That job splits cleanly in two:

* **Where the playback audio comes from** -- WASAPI loopback on Windows, a Core
  Audio process tap on macOS, a PulseAudio monitor source on Linux. This part is
  genuinely per-platform and lives in each concrete adapter's ``start``/``stop``.
* **What happens to the bytes afterwards** -- downmix to mono, resample to the
  detector's rate, hold a ring buffer, serve frames under the
  ``AECReferenceSource`` protocol, and report diagnostics. None of that depends
  on the platform at all.

Only the first half was ever platform-aware. The second half lived inside
``audio_core/wasapi/aec_reference_adapter.py``, so the only way to obtain it was
to construct the Windows adapter -- which is why macOS had no AEC reference at
all even after a working Core Audio system-capture provider landed
(``audio_core/capture/coreaudio_capture.py``). Hoisting the shared half here is
what lets a second platform reuse it without touching the Windows path.

The buffer semantics are preserved exactly as the Windows adapter had them, and
the existing WASAPI adapter test suite is the regression net for that claim.
"""

from __future__ import annotations

import threading
from collections import deque
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

logger = get_logger(__name__)

# Minimum samples before the adapter starts serving real data (50ms at 16kHz).
# Prevents the FDAF from receiving near-zero reference frames during the first
# few callbacks when the loopback buffer has barely filled.
MIN_READY_SAMPLES = 800

__all__ = ["MIN_READY_SAMPLES", "RingBufferAECReferenceAdapter", "resample_mono_int16"]


def resample_mono_int16(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample mono int16 AEC reference samples with a polyphase anti-aliasing filter."""
    if len(samples) == 0 or source_rate == target_rate:
        return samples.astype(np.int16, copy=False)

    target_len = round(len(samples) * target_rate / source_rate)
    divisor = gcd(source_rate, target_rate)
    resampled = resample_poly(
        samples.astype(np.float32, copy=False),
        target_rate // divisor,
        source_rate // divisor,
        padtype="line",
    )

    if len(resampled) < target_len:
        pad_width = target_len - len(resampled)
        if len(resampled):
            resampled = np.pad(resampled, (0, pad_width), mode="edge")
        else:
            resampled = np.zeros(target_len, dtype=np.float32)
    elif len(resampled) > target_len:
        resampled = resampled[:target_len]

    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


class RingBufferAECReferenceAdapter:
    """Ring-buffer + ``AECReferenceSource`` protocol, independent of the capture source.

    Subclasses supply only ``start()`` and ``stop()``: acquire a platform
    capture source, hand its PCM to :meth:`_on_pcm_callback`, and set
    ``self._running``. Everything below that line is shared.
    """

    # Ring buffer size in samples (at 16kHz) - 500ms of audio
    BUFFER_SIZE_SAMPLES = 8000

    #: Operator-facing name for this reference source, used in the callback-error
    #: log. Subclasses override it so a log line names the capture source that
    #: actually failed ("WASAPI AEC reference" vs "CoreAudio AEC reference")
    #: rather than a generic string an operator cannot act on.
    REFERENCE_LABEL = "AEC reference"

    #: True channel count of the incoming PCM, when the capture source reports
    #: it. ``None`` keeps the even-length heuristic the WASAPI path has always
    #: used (it hands over raw bytes with no channel count attached), so leaving
    #: this unset preserves Windows behaviour exactly.
    _channels_hint: int | None = None

    def __init__(self, target_sample_rate: int = SAMPLE_RATE_16K):
        self._target_rate = target_sample_rate
        self._buffer: deque[np.int16] = deque(maxlen=self.BUFFER_SIZE_SAMPLES)
        self._lock = threading.Lock()
        self._running = False
        self._device_sample_rate: int = SAMPLE_RATE_48K  # Will be updated from capture

        # Startup gate: don't serve real frames until buffer reaches MIN_READY_SAMPLES
        self._primed = False

        # Underrun protection: replay last good frame instead of returning zeros
        self._last_frame = np.zeros(160, dtype=np.int16)

        # Adaptive buffer target (milliseconds) - used by passive calibration
        self._buffer_target_ms = 100.0
        self._diagnostics = None
        self._diagnostics_disabled = False
        self._callback_error_count = 0
        self._callback_error_logged = False

    # ------------------------------------------------------------------ #
    # Subclass contract                                                    #
    # ------------------------------------------------------------------ #

    def start(self) -> bool:
        """Acquire the platform capture source. Subclasses must implement."""
        raise NotImplementedError

    def stop(self) -> None:
        """Release the platform capture source. Subclasses must implement."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Shared machinery                                                     #
    # ------------------------------------------------------------------ #

    def _load_diagnostics(self) -> None:
        """Resolve optional diagnostics before the native audio callback starts."""
        try:
            from diagnostics.aec_effectiveness import get_aec_diagnostics

            self._diagnostics = get_aec_diagnostics()
            self._diagnostics_disabled = False
        except (AttributeError, ImportError, RuntimeError, ValueError) as exc:
            self._diagnostics = None
            self._diagnostics_disabled = True
            logger.debug("AEC diagnostics unavailable: %s", exc)

    def _on_pcm_callback(self, pcm_bytes: bytes) -> None:
        """Callback from the capture source - stores audio in the ring buffer."""
        try:
            # Convert bytes to int16 array
            # Assume 16-bit PCM, potentially stereo
            pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16)

            # Downmix to mono. When the capture source reports its channel
            # count, believe it; otherwise fall back to "even length means
            # stereo", which is all the raw WASAPI byte stream ever offered.
            channels = self._channels_hint
            if channels is None:
                channels = 2 if len(pcm_array) % 2 == 0 else 1

            if channels > 1 and len(pcm_array) % channels == 0:
                # Reshape to (n_samples, n_channels) and take mean
                mono_data = pcm_array.reshape(-1, channels).mean(axis=1).astype(np.int16)
            else:
                mono_data = pcm_array

            # Resample if needed (device rate might be 48kHz, we want 16kHz)
            if self._device_sample_rate != self._target_rate:
                mono_data = resample_mono_int16(mono_data, self._device_sample_rate, self._target_rate)

            # Store in ring buffer
            with self._lock:
                self._buffer.extend(mono_data)
                buffer_len = len(self._buffer)

            diagnostics = self._diagnostics
            if diagnostics is not None and not self._diagnostics_disabled:
                try:
                    rms = float(np.sqrt(np.mean(mono_data.astype(np.float32) ** 2)))
                    fill_pct = buffer_len / self.BUFFER_SIZE_SAMPLES * 100
                    diagnostics.record_reference_write(
                        samples=len(mono_data),
                        rms=rms,
                        sample_rate=self._target_rate,
                        buffer_fill_percent=fill_pct,
                    )
                except (AttributeError, RuntimeError, TypeError, ValueError, FloatingPointError):
                    self._diagnostics_disabled = True

        except Exception as exc:  # noqa: BLE001, RUF100 - PortAudio callback safety boundary
            self._record_callback_error(exc)

    def _record_callback_error(self, exc: BaseException) -> None:
        self._callback_error_count += 1
        if self._callback_error_logged and self._callback_error_count % 100 != 0:
            return
        self._callback_error_logged = True
        logger.warning(
            "%s callback failed; callback_error_count=%d: %s",
            self.REFERENCE_LABEL,
            self._callback_error_count,
            exc,
        )

    # AECReferenceSource protocol implementation
    def get_aec_reference_frame(
        self,
        frame_samples: int,
        target_rate: int | None = None,
    ) -> np.ndarray:
        """Get a frame of playback audio for AEC reference."""
        if target_rate is None:
            target_rate = self._target_rate

        with self._lock:
            # Startup gate: wait until buffer has accumulated enough samples
            # to give the FDAF a meaningful reference signal.
            if not self._primed:
                if len(self._buffer) < MIN_READY_SAMPLES:
                    return np.zeros(frame_samples, dtype=np.int16)
                self._primed = True
                logger.info(
                    "AEC_ADAPTER_PRIMED: buffer reached %s samples, serving real frames",
                    len(self._buffer),
                )

            if len(self._buffer) < frame_samples:
                # Underrun: not enough fresh data -- replay last good frame
                return self._last_frame.copy()

            # FIFO: consume samples from the left side of the deque
            consumed = []
            for _ in range(min(frame_samples, len(self._buffer))):
                consumed.append(self._buffer.popleft())
            frame_data = np.array(consumed, dtype=np.int16)

            # Save as last good frame for underrun protection
            self._last_frame = frame_data.copy()

        # Resample if rate doesn't match
        if target_rate != self._target_rate:
            frame_data = resample_mono_int16(frame_data, self._target_rate, target_rate)

        return frame_data

    def has_aec_reference(self) -> bool:
        """Check if AEC reference buffer has actual data.

        Returns True if the adapter is running.

        The AECReferenceSource contract allows returning silence (zeros) when no
        playback audio is buffered yet, so "running" is the correct readiness
        signal here.
        """
        return self._running

    @property
    def callback_error_count(self) -> int:
        """Return callback processing errors swallowed at the audio boundary."""
        return self._callback_error_count

    def has_sufficient_reference(self, min_samples: int = 160) -> bool:
        """Check if buffer has enough samples for AEC processing.

        Args:
            min_samples: Minimum samples needed (default 160 = 10ms at 16kHz)

        Returns:
            True if buffer has at least min_samples of data
        """
        with self._lock:
            return self._running and len(self._buffer) >= min_samples

    def get_buffer_fill_level(self) -> tuple[int, int]:
        """Get current buffer fill level for diagnostics.

        Returns:
            Tuple of (current_samples, max_samples)
        """
        with self._lock:
            return len(self._buffer), self.BUFFER_SIZE_SAMPLES

    # ------------------------------------------------------------------ #
    # Adaptive buffer_target (shared interface with ChunkStamperAECAdapter) #
    # ------------------------------------------------------------------ #

    def set_buffer_target(self, delay_ms: float) -> None:
        """Update buffer_target from a measured speaker-to-mic delay.

        Applies exponential smoothing to avoid jitter from noisy measurements.
        Clamps to [5.0, 300.0] ms.

        For a device-level loopback reference the buffer_target is informational
        (the adapter does not insert an artificial delay), but passive
        calibration still calls this to record the measured acoustic delay.

        Args:
            delay_ms: Measured acoustic delay in milliseconds.
        """
        clamped = max(5.0, min(300.0, delay_ms))
        old_ms = self._buffer_target_ms

        # Exponential smoothing: target = (1 - alpha) * old + alpha * new
        alpha = 0.2
        self._buffer_target_ms = (1.0 - alpha) * old_ms + alpha * clamped

        logger.info(
            "[AEC_DELAY] updated buffer_target: %.0fms -> %.0fms (measured=%.0fms, clamped=%.0fms)",
            old_ms,
            self._buffer_target_ms,
            delay_ms,
            clamped,
        )

    def get_buffer_target_ms(self) -> float:
        """Return current buffer_target in milliseconds."""
        return self._buffer_target_ms

    def get_recent_reference(self, num_samples: int) -> np.ndarray:
        """Return a copy of the most recent N samples from the ring buffer.

        Used by the passive calibrator to cross-correlate against mic audio.
        Does NOT consume samples -- this is a read-only snapshot.

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
