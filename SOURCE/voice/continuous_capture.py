"""
Continuous Microphone Capture for Desktop Voice Pipeline.

Routes audio from the wake detector's existing PyAudio stream to multiple
consumers depending on the current mode:

    - WAKE: feeds the wake detector scoring loop
    - RECORD: accumulates audio into a recording buffer (command capture)
    - VAD_MONITOR: runs SileroVAD for interrupt detection during TTS playback
    - PAUSED: audio is discarded

Audio is received via :meth:`feed_audio`, called by the wake detector's
frame tap. This avoids opening a second PyAudio stream, which causes a
native ACCESS_VIOLATION (0xC0000005) segfault on Windows PortAudio.

Thread Safety:
    ``feed_audio`` is called from the wake detector's read thread.
    Mode switches arrive from the async event loop thread.
    A threading.Lock protects mode transitions and buffer access.

Memory:
    The recording buffer is capped at MAX_RECORDING_SECONDS to prevent OOM.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

if TYPE_CHECKING:
    from voice.wake_detector.aec_processor import AECProcessor

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default chunk size matches ViolaWakeListener (80ms at 16kHz)
DEFAULT_CHUNK_SIZE: int = 1280

# Maximum recording buffer: 30 seconds at 16kHz, 16-bit mono = ~960KB
MAX_RECORDING_SECONDS: float = 30.0

# VAD interrupt detection: consecutive speech frames required before interrupt
# 300ms at 512 samples/window (32ms per window) = ~10 windows
VAD_INTERRUPT_WINDOWS: int = 10

# SileroVAD window size (fixed by the model)
SILERO_WINDOW_SAMPLES: int = 512

# Speech probability threshold for interrupt detection
VAD_SPEECH_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Mode Enum (string-based for simplicity and logging)
# ---------------------------------------------------------------------------

MODE_WAKE = "wake"
MODE_RECORD = "record"
MODE_VAD_MONITOR = "vad_monitor"
MODE_PAUSED = "paused"

_VALID_MODES = frozenset({MODE_WAKE, MODE_RECORD, MODE_VAD_MONITOR, MODE_PAUSED})


class CrossUserCaptureError(RuntimeError):
    """Raised when a capture buffer bound to user A is accessed by user B."""


def _current_user_id() -> str | None:
    """Resolve the ambient user_id, or None if not set.

    Reads ``core.user_context.get_current_user_id`` (populated by
    AuthMiddleware). Returns None when called outside a request
    context — for instance, from the PyAudio read thread. Callers
    treat None as "don't enforce cross-user guard" so the audio thread
    does not spuriously raise.
    """
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except (LookupError, Exception):
        return None


# ---------------------------------------------------------------------------
# ContinuousMicCapture
# ---------------------------------------------------------------------------


class ContinuousMicCapture:
    """Routes audio from the wake detector stream to multiple consumers.

    Does NOT open its own PyAudio stream. Audio arrives via :meth:`feed_audio`,
    called by the wake detector's frame tap callback.

    Args:
        sample_rate: Audio sample rate (default 16000 Hz).
        chunk_size: Samples per read chunk (default 1280 = 80ms).
        aec_processor: Optional AEC processor for echo cancellation.
        aec_reference_source: Callback that provides playback reference frames
            for AEC. Signature: (frame_samples, target_rate) -> np.ndarray.
        device_index: PyAudio input device index (unused, kept for API compat).
        on_wake_audio: Callback for wake mode audio routing.
            Receives raw int16 numpy chunks for wake detection.
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE_16K,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        aec_processor: AECProcessor | None = None,
        aec_reference_source: Callable[[int, int | None], np.ndarray] | None = None,
        device_index: int | None = None,
        on_wake_audio: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size
        self._aec_processor = aec_processor
        self._aec_reference_source = aec_reference_source
        self._device_index = device_index
        self._on_wake_audio = on_wake_audio

        # State (no PyAudio — audio fed externally via feed_audio)
        self._running = False
        self._stop_event = threading.Event()

        # Mode and lock
        self._mode: str = MODE_PAUSED
        self._mode_lock = threading.Lock()

        # Recording buffer — bound to the user_id that owns the active
        # capture session. Cross-user access raises to prevent one user's
        # recorded audio from leaking into another user's command flow.
        max_samples = int(MAX_RECORDING_SECONDS * sample_rate)
        self._recording_buffer = bytearray()
        self._max_buffer_bytes = max_samples * 2  # 16-bit = 2 bytes per sample
        self._recording_user_id: str | None = None

        # VAD interrupt detection
        self._vad_model = None  # Lazy-loaded SileroVAD
        self._vad_load_attempted = False
        self._vad_consecutive_speech: int = 0
        self._on_interrupt: Callable[[], None] | None = None
        self._interrupt_fired = False

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self) -> bool:
        """Mark the capture as running.

        Audio frames are supplied externally via :meth:`feed_audio` (called
        by the wake detector's frame tap). No hardware resources acquired.

        Returns:
            True (always succeeds).
        """
        if self._running:
            logger.debug("ContinuousMicCapture already running")
            return True

        self._running = True
        self._stop_event.clear()
        logger.info(
            "ContinuousMicCapture started (rate=%d, chunk=%d, fed via frame tap)",
            self._sample_rate,
            self._chunk_size,
        )
        return True

    def stop(self) -> None:
        """Stop the capture."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        logger.info("ContinuousMicCapture stopped")

    # -------------------------------------------------------------------
    # External audio feed (replaces the old internal PyAudio read loop)
    # -------------------------------------------------------------------

    def feed_audio(self, audio_bytes: bytes) -> None:
        """Receive a raw audio chunk from the wake detector's frame tap.

        Called on the wake detector's read thread for every frame it reads.
        Routes audio based on the current mode.
        """
        if not self._running:
            return

        audio_int16 = np.frombuffer(audio_bytes, dtype=np.int16)

        # Apply AEC if available and in VAD monitor mode
        with self._mode_lock:
            current_mode = self._mode
            aec_proc = self._aec_processor
            aec_ref = self._aec_reference_source

        if aec_proc is not None and aec_ref is not None and current_mode == MODE_VAD_MONITOR:
            audio_int16 = self._apply_aec(audio_int16, aec_proc, aec_ref)

        # Route audio based on mode
        if current_mode == MODE_WAKE:
            self._handle_wake_audio(audio_int16)
        elif current_mode == MODE_RECORD:
            self._handle_record_audio(audio_int16)
        elif current_mode == MODE_VAD_MONITOR:
            self._handle_vad_monitor_audio(audio_int16)
        # MODE_PAUSED: discard

    @property
    def is_running(self) -> bool:
        """Whether the capture is active."""
        return self._running

    @property
    def mode(self) -> str:
        """Current capture mode."""
        with self._mode_lock:
            return self._mode

    # -------------------------------------------------------------------
    # Mode Switching
    # -------------------------------------------------------------------

    def set_mode(self, mode: str, **kwargs) -> None:
        """Switch the capture routing mode.

        Args:
            mode: One of "wake", "record", "vad_monitor", "paused".
            **kwargs:
                on_interrupt: Callback for vad_monitor mode (fired on speech detection).
                on_wake_audio: Callback for wake mode audio routing.
        """
        if mode not in _VALID_MODES:
            logger.error("Invalid capture mode: %s", mode)
            return

        with self._mode_lock:
            old_mode = self._mode
            self._mode = mode

            if mode == MODE_RECORD:
                self._recording_buffer.clear()
                self._recording_user_id = _current_user_id()

            elif mode == MODE_VAD_MONITOR:
                # Fresh buffer per TTS turn. vad_monitor accumulates every
                # frame into _recording_buffer so the barge-in speech can be
                # transcribed; without clearing here, each TTS turn's audio
                # piled onto the previous turns' (only MODE_RECORD cleared,
                # and it is never used in production). The result was a
                # get_recording() that returned turn-1 + turn-2 + ... audio —
                # so from the 2nd TTS turn on, an interrupt was transcribed
                # with a stale prefix, and once the 30s cap was hit the real
                # barge-in audio was dropped entirely.
                self._recording_buffer.clear()
                self._recording_user_id = _current_user_id()
                self._vad_consecutive_speech = 0
                self._interrupt_fired = False
                self._on_interrupt = kwargs.get("on_interrupt")
                if self._vad_model is not None:
                    try:
                        self._vad_model.reset_states()
                    except (AttributeError, RuntimeError):
                        logger.exception(
                            "VAD reset_states failed; interrupt detection may use stale state",
                        )

            elif mode == MODE_WAKE:
                if "on_wake_audio" in kwargs:
                    self._on_wake_audio = kwargs["on_wake_audio"]

        if old_mode != mode:
            logger.debug("ContinuousMicCapture mode: %s -> %s", old_mode, mode)

    # -------------------------------------------------------------------
    # Recording Buffer Access
    # -------------------------------------------------------------------

    def get_recording(self) -> bytes:
        """Return accumulated recording buffer as bytes.

        Raises:
            CrossUserCaptureError: If the caller's RequestContext user_id
                differs from the user that started the recording.
        """
        with self._mode_lock:
            self._enforce_recording_owner_locked()
            return bytes(self._recording_buffer)

    def get_recording_duration(self) -> float:
        """Return duration of accumulated recording in seconds."""
        with self._mode_lock:
            self._enforce_recording_owner_locked()
            num_bytes = len(self._recording_buffer)
        return num_bytes / (self._sample_rate * 2)

    def clear_recording(self) -> None:
        """Clear the recording buffer."""
        with self._mode_lock:
            self._recording_buffer.clear()
            self._recording_user_id = None

    def _enforce_recording_owner_locked(self) -> None:
        """Fail loudly if the current user doesn't own the recording buffer."""
        owner = self._recording_user_id
        caller = _current_user_id()
        if owner is None or caller is None:
            return
        if owner != caller:
            raise CrossUserCaptureError(
                "ContinuousMicCapture recording buffer owned by user %r; " "accessed by user %r" % (owner, caller)
            )

    # -------------------------------------------------------------------
    # AEC Configuration
    # -------------------------------------------------------------------

    def set_aec_processor(self, processor: AECProcessor | None) -> None:
        """Update the AEC processor (thread-safe)."""
        with self._mode_lock:
            self._aec_processor = processor

    def set_aec_reference_source(self, source: Callable[[int, int | None], np.ndarray] | None) -> None:
        """Update the AEC reference source callback."""
        with self._mode_lock:
            self._aec_reference_source = source

    # -------------------------------------------------------------------
    # Internal: AEC
    # -------------------------------------------------------------------

    def _apply_aec(
        self,
        audio_int16: np.ndarray,
        aec_proc: AECProcessor,
        aec_ref: Callable[[int, int | None], np.ndarray],
    ) -> np.ndarray:
        """Apply AEC frame-by-frame to the audio chunk."""
        from voice.wake_detector.aec_processor import AEC_FRAME_SAMPLES, AEC_SAMPLE_RATE

        try:
            processed_frames = []
            chunk_len = len(audio_int16)

            for i in range(0, chunk_len, AEC_FRAME_SAMPLES):
                frame_end = min(i + AEC_FRAME_SAMPLES, chunk_len)
                mic_frame = audio_int16[i:frame_end]

                if len(mic_frame) < AEC_FRAME_SAMPLES:
                    mic_frame = np.pad(mic_frame, (0, AEC_FRAME_SAMPLES - len(mic_frame)))

                ref_frame = aec_ref(AEC_FRAME_SAMPLES, AEC_SAMPLE_RATE)
                processed = aec_proc.process_frame(mic_frame, ref_frame)
                processed_frames.append(processed[: frame_end - i])

            return np.concatenate(processed_frames)
        except Exception as exc:
            logger.debug("AEC processing error in continuous capture: %s", exc)
            return audio_int16

    # -------------------------------------------------------------------
    # Mode Handlers
    # -------------------------------------------------------------------

    def _handle_wake_audio(self, audio_int16: np.ndarray) -> None:
        """Route audio to the wake detector callback."""
        if self._on_wake_audio is not None:
            try:
                self._on_wake_audio(audio_int16)
            except Exception as exc:
                logger.debug("Wake audio callback error: %s", exc)

    def _handle_record_audio(self, audio_int16: np.ndarray) -> None:
        """Accumulate audio in the recording buffer.

        Drops frames silently when the active RequestContext user differs
        from the owner — the audio thread must never raise into PortAudio.
        """
        raw_bytes = audio_int16.tobytes()
        caller = _current_user_id()
        with self._mode_lock:
            owner = self._recording_user_id
            if owner is not None and caller is not None and owner != caller:
                logger.warning(
                    "Dropping record-mode frame: owner=%r caller=%r",
                    owner,
                    caller,
                )
                return
            remaining = self._max_buffer_bytes - len(self._recording_buffer)
            if remaining > 0:
                self._recording_buffer.extend(raw_bytes[:remaining])

    def _handle_vad_monitor_audio(self, audio_int16: np.ndarray) -> None:
        """Run VAD on AEC'd audio and fire interrupt on sustained speech."""
        raw_bytes = audio_int16.tobytes()
        with self._mode_lock:
            remaining = self._max_buffer_bytes - len(self._recording_buffer)
            if remaining > 0:
                self._recording_buffer.extend(raw_bytes[:remaining])

            if self._interrupt_fired:
                return

        speech_detected_in_chunk = self._run_vad_on_chunk(audio_int16)

        callback = None
        with self._mode_lock:
            if self._interrupt_fired:
                return

            if speech_detected_in_chunk:
                self._vad_consecutive_speech += 1
            else:
                self._vad_consecutive_speech = 0

            if self._vad_consecutive_speech >= VAD_INTERRUPT_WINDOWS:
                self._interrupt_fired = True
                callback = self._on_interrupt

        if callback is not None:
            logger.info(
                "TTS interrupt detected: %d consecutive speech windows",
                VAD_INTERRUPT_WINDOWS,
            )
            try:
                callback()
            except Exception as exc:
                logger.warning("Interrupt callback error: %s", exc)

    def _run_vad_on_chunk(self, audio_int16: np.ndarray) -> bool:
        """Run SileroVAD on the audio chunk. Returns True if speech detected."""
        model = self._ensure_vad_model()
        if model is None:
            rms = float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))
            return rms > 500.0

        try:
            audio_f32 = audio_int16.astype(np.float32) / 32768.0

            max_prob = 0.0
            for i in range(0, len(audio_f32) - SILERO_WINDOW_SAMPLES + 1, SILERO_WINDOW_SAMPLES):
                window = audio_f32[i : i + SILERO_WINDOW_SAMPLES]
                prob = model(window, self._sample_rate)
                if prob > max_prob:
                    max_prob = prob

            return max_prob > VAD_SPEECH_THRESHOLD

        except Exception as exc:
            logger.debug("VAD inference error: %s", exc)
            rms = float(np.sqrt(np.mean(audio_int16.astype(np.float32) ** 2)))
            return rms > 500.0

    def _ensure_vad_model(self) -> object | None:
        """Lazy-load SileroVAD model. Returns None if unavailable."""
        if self._vad_model is not None:
            return self._vad_model
        if self._vad_load_attempted:
            return None

        self._vad_load_attempted = True
        try:
            from voice.vad.silero_onnx import create_silero_vad

            model = create_silero_vad()
            if model is None:
                raise RuntimeError("Silero VAD ONNX not available")
            self._vad_model = model
            logger.info("SileroVAD ONNX loaded for TTS interrupt detection")
            return model
        except Exception as exc:
            logger.warning(
                "SileroVAD unavailable for interrupt detection, using RMS fallback: %s",
                exc,
            )
            return None

    # -------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            "ContinuousMicCapture("
            "running=%s, mode=%s, buffer_bytes=%d"
            ")" % (self._running, self._mode, len(self._recording_buffer))
        )
