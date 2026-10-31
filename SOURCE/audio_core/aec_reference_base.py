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
import weakref
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

# A device loopback frame quieter than this (int16 RMS) is treated as "the
# loopback cannot hear Viola's own playback". Real playback captured at a usable
# level sits in the hundreds or thousands; a broken, offloaded, or heavily
# attenuated loopback reads exactly 0 or single digits.
LOOPBACK_SILENT_RMS = 50.0

# Viola's own playback this loud (int16 RMS) is evidence about the loopback: if
# the loopback can hear the speakers at all, it hears this.
LOCAL_LOUD_RMS = 300.0
# 10 ms frames of loud own playback with a silent loopback before the loopback
# is judged unable to hear Viola (0.5 s; the mixer lag at an onset is far less).
LOOPBACK_DEAF_FRAMES = 50
# 10 ms frames of loud own playback that the loopback also hears before it is
# judged to hear Viola (0.2 s).
LOOPBACK_HEARS_FRAMES = 20

# Most of Viola's own playback the reference keeps queued (250ms at 16kHz). The
# wake listener drains it in real time; the cap only bounds the backlog when the
# listener pauses (for example while a command is being recorded).
LOCAL_PLAYBACK_BUFFER_SAMPLES = 4000

__all__ = [
    "LOCAL_LOUD_RMS",
    "LOCAL_PLAYBACK_BUFFER_SAMPLES",
    "LOOPBACK_DEAF_FRAMES",
    "LOOPBACK_HEARS_FRAMES",
    "LOOPBACK_SILENT_RMS",
    "MIN_READY_SAMPLES",
    "LocalPlaybackAECReferenceAdapter",
    "RingBufferAECReferenceAdapter",
    "push_local_playback_reference",
    "resample_mono_int16",
]

# Every live reference adapter, so Viola's own output path (TTS) can feed the
# PCM it is playing straight into the echo canceller's reference. Weak, so a
# dropped adapter is never kept alive by this registry.
_LOCAL_PLAYBACK_SINKS: weakref.WeakSet[RingBufferAECReferenceAdapter] = weakref.WeakSet()
_LOCAL_PLAYBACK_SINKS_LOCK = threading.Lock()


def push_local_playback_reference(samples: np.ndarray, sample_rate: int) -> int:
    """Hand PCM that Viola itself is playing to every live AEC reference adapter.

    The device loopback is the general reference: it hears whatever any process
    plays. It is not reliable for Viola's own voice, though. On some machines it
    reads digital silence while Viola is audibly speaking (1.0.5: ``ref_rms=0``
    for the whole onboarding narration while the microphone heard it clearly),
    and the wake word then fired on Viola's own words. Viola always knows exactly
    what it is playing, so the TTS output path pushes those samples here as it
    plays them.

    Args:
        samples: Mono PCM, either float in [-1, 1] or int16.
        sample_rate: Sample rate of ``samples`` in Hz.

    Returns:
        Number of adapters that received the samples.
    """
    if samples is None or len(samples) == 0 or sample_rate <= 0:
        return 0
    with _LOCAL_PLAYBACK_SINKS_LOCK:
        sinks = list(_LOCAL_PLAYBACK_SINKS)
    delivered = 0
    for sink in sinks:
        try:
            sink.push_local_playback(samples, sample_rate)
            delivered += 1
        except Exception as exc:  # noqa: BLE001, RUF100 - playback must never fail because of the reference tap
            logger.debug("Local playback AEC reference push failed: %s", exc)
    return delivered


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

        # Viola's own playback (TTS), pushed by the output path. Served in place
        # of the device loopback whenever the loopback cannot hear it.
        self._local_buffer: deque[np.int16] = deque(maxlen=LOCAL_PLAYBACK_BUFFER_SAMPLES)
        # Does the device loopback hear Viola's own playback? None until there
        # is evidence (see _learn_loopback_hearing); then sticky.
        self._loopback_hears_viola: bool | None = None
        self._loopback_heard_run = 0
        self._loopback_silent_run = 0
        with _LOCAL_PLAYBACK_SINKS_LOCK:
            _LOCAL_PLAYBACK_SINKS.add(self)

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
            frame_data = self._next_loopback_frame_locked(frame_samples)
            local_frame = self._next_local_frame_locked(frame_samples)

        if local_frame is not None:
            loopback_rms = float(np.sqrt(np.mean(frame_data.astype(np.float32) ** 2))) if len(frame_data) else 0.0
            local_rms = float(np.sqrt(np.mean(local_frame.astype(np.float32) ** 2)))
            hears = self._learn_loopback_hearing(loopback_rms, local_rms)
            if hears is False or (hears is None and loopback_rms < LOOPBACK_SILENT_RMS):
                # The loopback is not hearing Viola's own playback (silent,
                # offloaded, or attenuated to nothing). Use what Viola knows it
                # is playing instead, so the echo canceller still has a
                # reference while Viola speaks.
                frame_data = local_frame

        # Resample if rate doesn't match
        if target_rate != self._target_rate:
            frame_data = resample_mono_int16(frame_data, self._target_rate, target_rate)

        return frame_data

    def _next_loopback_frame_locked(self, frame_samples: int) -> np.ndarray:
        """Next device-loopback frame. Caller holds ``self._lock``."""
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
        return frame_data

    def _learn_loopback_hearing(self, loopback_rms: float, local_rms: float) -> bool | None:
        """Learn, from frames where Viola is audibly playing, whether the loopback hears her.

        This used to be decided frame by frame: any 10 ms loopback frame below
        LOOPBACK_SILENT_RMS was swapped for Viola's own playback. A working
        loopback lags Viola's own playback by the mixer latency and is quiet in
        every gap between words, so the reference became a splice of two copies
        of Viola's voice at two different delays, switching at every word onset
        and gap; the echo canceller cannot converge on that. And the one-shot
        "AEC_LOCAL_REFERENCE ... rms=0.0" line (1.0.5 build 7, logged once at
        sign-in) read like a sign-in decision that was never revisited, while
        it only described the first frame of the first sound Viola played.

        Now: only frames where Viola's own playback is loud count as evidence.
        0.5 s of them with a silent loopback means the loopback cannot hear
        Viola (use her own playback from then on); 0.2 s with the loopback
        hearing them means it can (keep the loopback, it also carries music).
        The answer is kept and re-learned whenever the evidence changes, and
        each change is logged with the levels that decided it.
        """
        if local_rms < LOCAL_LOUD_RMS:
            return self._loopback_hears_viola
        if loopback_rms >= LOOPBACK_SILENT_RMS:
            self._loopback_heard_run += 1
            self._loopback_silent_run = 0
        else:
            self._loopback_silent_run += 1
            self._loopback_heard_run = 0
        if self._loopback_silent_run >= LOOPBACK_DEAF_FRAMES and self._loopback_hears_viola is not False:
            self._loopback_hears_viola = False
            logger.info(
                "AEC_LOCAL_REFERENCE: the device loopback stayed silent (rms=%.1f, below %.0f) for %.1f s "
                "while Viola played audio at rms=%.0f; serving Viola's own playback as the echo reference "
                "while she plays (re-checked continuously)",
                loopback_rms,
                LOOPBACK_SILENT_RMS,
                LOOPBACK_DEAF_FRAMES * 0.01,
                local_rms,
            )
        elif self._loopback_heard_run >= LOOPBACK_HEARS_FRAMES and self._loopback_hears_viola is not True:
            self._loopback_hears_viola = True
            logger.info(
                "AEC_LOOPBACK_REFERENCE: the device loopback hears Viola's own playback (rms=%.0f while she "
                "played at rms=%.0f); keeping it as the echo reference (it also carries music)",
                loopback_rms,
                local_rms,
            )
        return self._loopback_hears_viola

    def _next_local_frame_locked(self, frame_samples: int) -> np.ndarray | None:
        """Next frame of Viola's own playback, or None when nothing is queued.

        Caller holds ``self._lock``. A partial frame is zero-padded: the tail of
        an utterance is real audio followed by real silence.
        """
        if not self._local_buffer:
            return None
        count = min(frame_samples, len(self._local_buffer))
        frame = np.zeros(frame_samples, dtype=np.int16)
        frame[:count] = [self._local_buffer.popleft() for _ in range(count)]
        return frame

    def push_local_playback(self, samples: np.ndarray, sample_rate: int) -> None:
        """Queue PCM that Viola itself is playing as AEC reference.

        Args:
            samples: Mono PCM, float in [-1, 1] or int16.
            sample_rate: Sample rate of ``samples`` in Hz.
        """
        data = np.asarray(samples)
        if data.size == 0:
            return
        if data.ndim > 1:
            data = data.mean(axis=1)
        if np.issubdtype(data.dtype, np.floating):
            data = np.clip(np.rint(data * 32767.0), -32768, 32767).astype(np.int16)
        else:
            data = data.astype(np.int16, copy=False)
        if sample_rate != self._target_rate:
            data = resample_mono_int16(data, int(sample_rate), self._target_rate)
        with self._lock:
            self._local_buffer.extend(data)

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


class LocalPlaybackAECReferenceAdapter(RingBufferAECReferenceAdapter):
    """Reference adapter fed only by Viola's own playback.

    Used when the platform has no device-level capture (or it failed to start),
    so the echo canceller still gets a reference whenever Viola itself speaks.
    It cannot cancel audio from other processes; that needs the device loopback.
    """

    REFERENCE_LABEL = "Local playback AEC reference"

    def start(self) -> bool:
        self._running = True
        return True

    def stop(self) -> None:
        self._running = False
