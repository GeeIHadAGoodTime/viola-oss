"""
Acoustic Echo Cancellation (AEC) processor for wake word detection.

This module provides frame-by-frame echo cancellation to improve wake word
detection during music/TTS playback. The key insight is that AEC must be
applied continuously in the mic capture loop, NOT on concatenated buffers
before detection.

Architecture:
    Mic Input → [AEC Frame Processing] → Wake Word Buffer
                      ↑
    Playback Reference (from AECReferenceSource implementation)

Frame-by-frame processing preserves the adaptive filter's ability to track
and cancel echo over time.

STATUS:
    - Infrastructure wired and NoOp/PyAEC/ViolaAEC verified
    - Real echo cancellation via ViolaAEC backend by default
    - Delay calibrated automatically: startup auto-calibration if no saved
      device profile (violawake_listener.py:_run_auto_calibration), plus
      passive cross-correlation every ~30s during playback
      (audio_core/calibration/passive_calibrate.py).
    - ERLE (echo return loss enhancement) tracked in rolling 60s window.
    - Reference gate has a warm-up watchdog that logs WARN if the gate has
      not opened within _WARMUP_WATCHDOG_FRAMES (~3s of frames) — useful
      for catching broken loopback feeds that silently return zeros.

KNOWN LIMITATIONS:
    - Clock drift between capture/playback devices not explicitly handled
      (passive calibration absorbs small drift, but large skew is not
      corrected at the AEC layer).
"""

from __future__ import annotations

import abc
from collections import deque
from typing import Protocol

import numpy as np
import numpy.typing as npt

from config.constants import AUDIO_SAMPLE_RATE
from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# AEC Reference Source Protocol                                                #
# --------------------------------------------------------------------------- #


class AECReferenceSource(Protocol):
    """
    Protocol for audio sources that can provide AEC reference frames.

    Any audio output (WASAPI sink, CoreAudio sink, etc.) can implement
    this protocol to provide playback audio for echo cancellation.
    This decouples the wake detection subsystem from concrete sink types.

    Design note: Implementations should always return an array of the
    requested size. If insufficient data is available, return zeros.
    This is preferable to None because:
    - AEC algorithms expect continuous frame streams
    - Silence (zeros) is a valid reference signal
    - Caller doesn't need None-handling logic
    """

    def get_aec_reference_frame(
        self,
        frame_samples: int,
        target_rate: int | None = None,
    ) -> npt.NDArray[np.int16]:
        """
        Get a frame of playback audio for AEC reference.

        ALWAYS returns exactly `frame_samples` samples. If insufficient
        playback data is available (e.g., at startup), returns zeros.

        Args:
            frame_samples: Number of samples to return (at target_rate).
                          Must be positive.
            target_rate: Target sample rate for output.
                        - None: Return in buffer's native sample rate.
                        - int: Resample to this rate before returning.
                        Common value: 16000 (wake word detection rate).

        Returns:
            Audio frame as int16 numpy array of exactly `frame_samples` length.
            Returns zeros if playback buffer has insufficient data.
        """
        ...

    def has_aec_reference(self) -> bool:
        """
        Check if AEC reference buffer is available and configured.

        Returns:
            True if get_aec_reference_frame() can be called meaningfully.
            False if AEC reference was not enabled or buffer not initialized.
        """
        ...


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

# Standard AEC frame size: 10ms at 16kHz = 160 samples
# This matches WebRTC/Speex expectations
AEC_FRAME_SAMPLES = 160
AEC_SAMPLE_RATE = AUDIO_SAMPLE_RATE

# Fallback speaker-to-mic delay (samples) when neither SettingsManager nor
# the per-device calibrator has a value. 50ms at 16kHz = 800 samples.
# Prefer :func:`_resolve_default_delay_samples` over this constant.
DEFAULT_DELAY_SAMPLES = 800


def _resolve_default_delay_samples() -> int:
    """Resolve the default AEC delay in samples from SettingsManager.

    Route handlers and runtime code should read live settings, not a
    compile-time constant. Falls back to ``DEFAULT_DELAY_SAMPLES`` when
    the setting is absent or the SettingsManager itself is unavailable.
    """
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        delay_ms = sm.get("wake_aec_delay_ms", None)
        if delay_ms is None:
            return DEFAULT_DELAY_SAMPLES
        return max(0, int(float(delay_ms) * AEC_SAMPLE_RATE / 1000))
    except Exception:
        return DEFAULT_DELAY_SAMPLES


# --------------------------------------------------------------------------- #
# Backend availability detection                                               #
# --------------------------------------------------------------------------- #


# Pyaec is an optional dependency; define a minimal protocol so we can type it without ignores.
class _PyAECNativeProtocol(Protocol):
    def __init__(
        self,
        frame_size: int,
        filter_length: int,
        sample_rate: int,
        enable_preprocess: bool = True,
    ) -> None: ...

    def cancel_echo(
        self,
        rec_buffer: npt.NDArray[np.int16],
        echo_buffer: npt.NDArray[np.int16],
    ) -> npt.NDArray[np.int16]: ...


PyAEC_Native: type[_PyAECNativeProtocol] | None

# PyAEC - AEC with pre-built Windows wheels
#
# NOTE: this must catch more than ImportError. pyaec loads its native library
# (aec.dll) via ctypes at module import; when the DLL is missing (e.g. a
# frozen bundle that shipped the .py module but not the DLL), `import pyaec`
# itself raises AttributeError ("'NoneType' object has no attribute 'AecNew'")
# from pyaec's module-level `lib.AecNew.argtypes = ...` (the load_library
# failure leaves `lib = None`); a denied/corrupt DLL surfaces as OSError. A
# bare `except ImportError` lets those propagate, which kills the import of
# this whole module and with it the entire wake engine — the 1.0.1 lane-4 P0
# (L4-3). A broken OPTIONAL AEC backend must degrade to the noop backend,
# never take down wake word detection.
try:
    import pyaec

    PyAEC_Native = pyaec.Aec
    PYAEC_AVAILABLE = True
except ImportError:
    PYAEC_AVAILABLE = False
    PyAEC_Native = None
except (AttributeError, OSError, RuntimeError) as _pyaec_exc:
    logger.error(
        "pyaec is installed but unusable (native aec library failed to load): %s. "
        "AEC will degrade to the next available backend (noop in the worst case); "
        "wake word detection stays alive WITHOUT pyaec echo cancellation.",
        _pyaec_exc,
    )
    PYAEC_AVAILABLE = False
    PyAEC_Native = None

# Speex - Requires SWIG to build on Windows (optional)
# Same rationale as pyaec above: a broken optional native backend must mark
# itself unavailable, never propagate out of this module's import.
try:
    import importlib

    speexdsp = importlib.import_module("speexdsp")

    SPEEX_AVAILABLE = True
except ImportError:
    SPEEX_AVAILABLE = False
    speexdsp = None
except (AttributeError, OSError, RuntimeError) as _speex_exc:
    logger.error(
        "speexdsp is installed but unusable: %s. " "AEC will degrade to the next available backend; wake stays alive.",
        _speex_exc,
    )
    SPEEX_AVAILABLE = False
    speexdsp = None

# WebRTC bindings are notoriously hard to install on Windows
# Keep as optional future enhancement
WEBRTC_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Abstract Base Class                                                          #
# --------------------------------------------------------------------------- #


# RMS threshold (int16 scale) below which a reference frame is silence.
_REF_SILENCE_RMS = 50.0

# Min cumulative active ref frames before the inner AEC sees a real reference.
# 50 frames at 10 ms/frame = 500 ms warm-up.
_MIN_ACTIVE_FRAMES = 50

# Max frames the activity counter accumulates. Caps how long the gate takes
# to decay back closed after playback stops.
_MAX_ACTIVE_FRAMES = 300

# Warm-up watchdog thresholds.  Telemetry-only: the gate does NOT get
# forced open — a genuinely-silent session is a valid state and forcing
# adaptation on silence degrades AEC output.  These thresholds fire a
# one-shot WARN log so operators can see a stuck-closed gate, which
# typically means the reference feed is silently returning zeros (broken
# WASAPI loopback, muted output device, or unhooked AECReferenceSource
# callback).
# 300 frames × 10 ms/frame = 3 s.
_WARMUP_WATCHDOG_FRAMES = 300
# 1500 frames × 10 ms/frame = 15 s — second-tier escalation so the first
# warning isn't lost in log rotation.
_WARMUP_WATCHDOG_FRAMES_ESCALATE = 1500


class _RefGate:
    """Reference-signal delay alignment + activity gate.

    Bundles two concerns that both operate on the reference before it
    reaches the inner AEC backend:

    1. **Delay**: shift ref by the measured speaker-to-mic lag so the
       filter sees reference samples that were actually played back at
       the time the mic frame's echo was captured.
    2. **Activity gate**: suppress adaptation until we've seen enough
       real playback energy. Blocking zero / near-zero ref from the
       filter prevents divergence at boot when the ring buffer is cold
       and any transient noise dominates the error signal.
    """

    def __init__(self, delay_samples: int, frame_samples: int) -> None:
        self._frame = int(frame_samples)
        self._delay = max(0, int(delay_samples))
        self._buf = np.zeros(self._delay + self._frame, dtype=np.int16)
        self._active_frames = 0
        # Watchdog state: count frames since last warm-up reset so we can
        # detect gates that never open (silent loopback / broken feed).
        self._frames_since_warmup_reset = 0
        self._watchdog_warn_fired = False
        self._watchdog_escalate_fired = False
        # Count one-shot warn events across the gate's lifetime (survives
        # resets) so diagnostics can expose "we tripped this N times".
        self._watchdog_warn_count = 0

    @property
    def delay_samples(self) -> int:
        return self._delay

    @property
    def is_gate_open(self) -> bool:
        return self._active_frames >= _MIN_ACTIVE_FRAMES

    @property
    def frames_since_warmup_reset(self) -> int:
        """Expose warm-up age for diagnostics."""
        return self._frames_since_warmup_reset

    @property
    def watchdog_warn_fired(self) -> bool:
        """True once the first-tier warm-up watchdog has fired."""
        return self._watchdog_warn_fired

    @property
    def watchdog_warn_count(self) -> int:
        """Total number of watchdog-warn one-shots across gate lifetime."""
        return self._watchdog_warn_count

    def set_delay_samples(self, delay_samples: int) -> None:
        """Resize the delay buffer without discarding recent history."""
        new_delay = max(0, int(delay_samples))
        if new_delay == self._delay:
            return
        new_buf = np.zeros(new_delay + self._frame, dtype=np.int16)
        copy_len = min(len(self._buf), len(new_buf))
        if copy_len > 0:
            new_buf[-copy_len:] = self._buf[-copy_len:]
        self._buf = new_buf
        self._delay = new_delay
        self._active_frames = 0  # Force warm-up again after retuning
        # Reset the watchdog so the post-retune warm-up isn't immediately
        # flagged as stuck.  The lifetime warn-count is preserved so a
        # calibration does not erase the "we have tripped before" signal.
        self._frames_since_warmup_reset = 0
        self._watchdog_warn_fired = False
        self._watchdog_escalate_fired = False

    def apply(
        self,
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """Return the delayed ref frame, or zeros if the gate is closed.

        Always consumes the ref even when the gate is closed so the delay
        buffer keeps tracking the playback clock.
        """
        if ref_frame is None or len(ref_frame) != self._frame:
            incoming = np.zeros(self._frame, dtype=np.int16)
        else:
            incoming = ref_frame.astype(np.int16, copy=False)

        ref_rms = float(np.sqrt(np.mean(incoming.astype(np.float64) ** 2)))
        if ref_rms > _REF_SILENCE_RMS:
            self._active_frames = min(_MAX_ACTIVE_FRAMES, self._active_frames + 1)
        else:
            self._active_frames = max(0, self._active_frames - 1)

        self._frames_since_warmup_reset += 1

        # Watchdog: surface a stuck-closed gate to operators.  Log-only —
        # we do NOT force the gate open because silent reference is a
        # valid state (muted output device, paused playback).  Operators
        # can triage using ``get_diagnostics()`` output.
        if (
            not self.is_gate_open
            and not self._watchdog_warn_fired
            and self._frames_since_warmup_reset >= _WARMUP_WATCHDOG_FRAMES
        ):
            self._watchdog_warn_fired = True
            self._watchdog_warn_count += 1
            logger.warning(
                "AEC RefGate has not opened after %d frames (~%.1fs); "
                "reference signal is silent or broken.  Echo cancellation "
                "is effectively pass-through until playback energy resumes.",
                self._frames_since_warmup_reset,
                self._frames_since_warmup_reset * 0.01,
            )
        if (
            not self.is_gate_open
            and not self._watchdog_escalate_fired
            and self._frames_since_warmup_reset >= _WARMUP_WATCHDOG_FRAMES_ESCALATE
        ):
            self._watchdog_escalate_fired = True
            logger.warning(
                "AEC RefGate still closed after %d frames (~%.0fs).  "
                "Check WASAPI loopback feed, output device mute state, "
                "or AECReferenceSource wiring.",
                self._frames_since_warmup_reset,
                self._frames_since_warmup_reset * 0.01,
            )

        if self._delay > 0:
            self._buf[: -self._frame] = self._buf[self._frame :]
            self._buf[-self._frame :] = incoming
            delayed = self._buf[: self._frame].copy()
        else:
            delayed = incoming

        if not self.is_gate_open:
            return np.zeros(self._frame, dtype=np.int16)

        return delayed


class AECProcessor(abc.ABC):
    """
    Abstract base class for Acoustic Echo Cancellation.

    Implementations must process audio frame-by-frame with fixed sizes.
    The adaptive filter state is maintained internally between calls.
    """

    # Frame size in samples (10ms at 16kHz)
    FRAME_SAMPLES: int = AEC_FRAME_SAMPLES
    SAMPLE_RATE: int = AEC_SAMPLE_RATE

    def set_delay_samples(self, delay_samples: int) -> None:
        """Update the reference-alignment delay in samples.

        No-op for backends that do not use the reference gate. Backends
        that do will override and propagate to their ``_RefGate``.
        """
        return None

    def set_local_playback_active(self, active: bool) -> None:
        """Tell the backend whether Viola's OWN local playback is active.

        When True, the reference signal is Viola's own output audio (music
        or TTS played through the local embedded player). Backends whose
        per-frame cost is heavy (e.g. ViolaAEC's pure-Python FDAF FFT, which
        holds the GIL ~0.9ms/frame and starves the shared-interpreter UI/video
        loop) may use this to downshift to a cheaper native echo path for the
        duration of playback. No-op for backends that have nothing to gain.

        This must NOT be conflated with "needs real AEC" cases like a live mic
        during a phone call or external room audio — those keep the heavy
        backend. Only Viola's own known output sets this True.
        """
        return None

    @abc.abstractmethod
    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """
        Process a single audio frame to remove echo.

        Args:
            mic_frame: Microphone input frame (int16, mono, FRAME_SAMPLES length)
            ref_frame: Speaker output reference frame (same format), or None if unavailable

        Returns:
            Processed frame with echo cancelled (same format as input)

        Note:
            - Both frames MUST be exactly FRAME_SAMPLES in length
            - If ref_frame is None, the mic_frame is returned unchanged
            - The adaptive filter updates its state with each call
        """
        pass

    @abc.abstractmethod
    def reset(self) -> None:
        """Reset the adaptive filter state."""
        pass

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this AEC implementation is functional."""
        pass

    @property
    def name(self) -> str:
        """Return the name of this AEC backend."""
        return self.__class__.__name__

    def get_diagnostics(self) -> dict:
        """Return AEC diagnostics. Override in subclasses that support it."""
        return {}


# --------------------------------------------------------------------------- #
# NoOp Implementation (Always Available)                                       #
# --------------------------------------------------------------------------- #


class NoOpAEC(AECProcessor):
    """
    Pass-through AEC that does nothing.

    Used when:
    - No AEC library is installed
    - AEC is disabled in configuration
    - As a baseline for A/B testing
    """

    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """Return mic_frame unchanged."""
        return mic_frame

    def reset(self) -> None:
        """Nothing to reset."""
        pass

    @property
    def is_available(self) -> bool:
        """Always available as fallback."""
        return True

    @property
    def name(self) -> str:
        return "noop"


# --------------------------------------------------------------------------- #
# PyAEC Implementation (Recommended - has pre-built Windows wheels)            #
# --------------------------------------------------------------------------- #


class PyAEC(AECProcessor):
    """
    PyAEC-based Acoustic Echo Cancellation.

    Uses the pyaec library which provides:
    - Adaptive echo cancellation
    - Pre-built Windows wheels (no SWIG required)

    Install: pip install pyaec

    API: Aec(frame_size, filter_length, sample_rate, enable_preprocess=True)
         aec.cancel_echo(rec_buffer, echo_buffer) -> processed_buffer

    Note: PyAEC expects int16 audio
    """

    def __init__(
        self,
        frame_samples: int = AEC_FRAME_SAMPLES,
        filter_length_ms: int = 200,
        delay_samples: int = DEFAULT_DELAY_SAMPLES,
    ) -> None:
        """
        Initialize PyAEC.

        Args:
            frame_samples: Frame size in samples (must match mic capture)
            filter_length_ms: Echo tail length in milliseconds (50-500ms typical)
            delay_samples: Initial speaker-to-mic delay in samples. Updated
                at runtime via :meth:`set_delay_samples` after calibration.
        """
        self._frame_samples = frame_samples
        self._filter_length_samples = int(self.SAMPLE_RATE * filter_length_ms / 1000)

        self._aec: _PyAECNativeProtocol | None = None
        self._initialized = False
        self._ref_gate = _RefGate(delay_samples, frame_samples)

        # Diagnostics
        self._mic_power_ema = 0.0
        self._out_power_ema = 0.0
        self._ref_power_ema = 0.0
        self._power_alpha = 0.1  # EMA smoothing
        self._frames_processed = 0
        self._erle_history: deque[float] = deque(maxlen=6000)  # 60s rolling at 100Hz

        if PYAEC_AVAILABLE:
            self._init_processor()

    def _init_processor(self) -> None:
        """Initialize PyAEC processor."""
        if PyAEC_Native is None:
            return

        try:
            # Aec(frame_size, filter_length, sample_rate, enable_preprocess=True)
            self._aec = PyAEC_Native(
                self._frame_samples,
                self._filter_length_samples,
                self.SAMPLE_RATE,
                True,  # enable_preprocess
            )

            self._initialized = True
            logger.info(
                "PyAEC initialized (frame=%d, filter=%dms, rate=%d)",
                self._frame_samples,
                self._filter_length_samples * 1000 // self.SAMPLE_RATE,
                self.SAMPLE_RATE,
            )

        except Exception as e:
            logger.error("Failed to initialize PyAEC: %s", e)
            self._initialized = False

    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """Process frame with PyAEC echo cancellation."""
        if not self._initialized or self._aec is None:
            return mic_frame

        # Validate frame size
        if len(mic_frame) != self._frame_samples:
            logger.warning(
                "AEC frame size mismatch: got %d, expected %d",
                len(mic_frame),
                self._frame_samples,
            )
            return mic_frame

        try:
            # Ensure int16
            mic_int16: npt.NDArray[np.int16] = mic_frame.astype(np.int16)

            # Align ref to mic via the measured delay and gate adaptation
            # until playback is sustained (AUD-01, AUD-08).
            ref_int16 = self._ref_gate.apply(ref_frame)

            # cancel_echo(rec_buffer, echo_buffer) -> processed_buffer
            processed = np.asarray(self._aec.cancel_echo(mic_int16, ref_int16), dtype=np.int16)

            # Track power levels for ERLE
            mic_power = float(np.mean(mic_int16.astype(np.float64) ** 2))
            out_power = float(np.mean(processed.astype(np.float64) ** 2))
            ref_power = float(np.mean(ref_int16.astype(np.float64) ** 2))

            self._mic_power_ema = self._power_alpha * mic_power + (1 - self._power_alpha) * self._mic_power_ema
            self._out_power_ema = self._power_alpha * out_power + (1 - self._power_alpha) * self._out_power_ema
            self._ref_power_ema = self._power_alpha * ref_power + (1 - self._power_alpha) * self._ref_power_ema

            if self._mic_power_ema > 1e-10:
                erle_db = 10 * np.log10(self._mic_power_ema / max(self._out_power_ema, 1e-10))
            else:
                erle_db = 0.0
            self._erle_history.append(erle_db)
            self._frames_processed += 1

            return processed

        except Exception as e:
            logger.error("PyAEC processing error: %s", e)
            return mic_frame

    def reset(self) -> None:
        """Reset AEC state."""
        if PYAEC_AVAILABLE and self._initialized:
            # Re-initialize to reset state
            self._init_processor()

    def set_delay_samples(self, delay_samples: int) -> None:
        """Retune the speaker-to-mic delay used for reference alignment."""
        self._ref_gate.set_delay_samples(delay_samples)

    @property
    def is_available(self) -> bool:
        """Check if PyAEC is functional."""
        return PYAEC_AVAILABLE and self._initialized

    @property
    def name(self) -> str:
        return "pyaec"

    def get_diagnostics(self) -> dict:
        """Return ERLE, power, and reference-gate diagnostics for monitoring."""
        erle_list = list(self._erle_history)
        return {
            "backend": "pyaec_speexdsp",
            "mic_rms": np.sqrt(max(self._mic_power_ema, 0)),
            "ref_rms": np.sqrt(max(self._ref_power_ema, 0)),
            "output_rms": np.sqrt(max(self._out_power_ema, 0)),
            "erle_db": erle_list[-1] if erle_list else 0.0,
            "erle_60s_avg": float(np.mean(erle_list)) if erle_list else 0.0,
            "erle_60s_min": float(np.min(erle_list)) if erle_list else 0.0,
            "erle_60s_max": float(np.max(erle_list)) if erle_list else 0.0,
            "frames_processed": self._frames_processed,
            "filter_length_ms": self._filter_length_samples * 1000 // self.SAMPLE_RATE,
            # Reference-gate state — surfaces stuck warm-up before it
            # silently degrades echo cancellation.
            "ref_gate_open": self._ref_gate.is_gate_open,
            "ref_gate_frames_since_reset": self._ref_gate.frames_since_warmup_reset,
            "ref_gate_watchdog_warn_fired": self._ref_gate.watchdog_warn_fired,
            "ref_gate_watchdog_warn_count": self._ref_gate.watchdog_warn_count,
            "ref_gate_delay_samples": self._ref_gate.delay_samples,
        }


# --------------------------------------------------------------------------- #
# Speex Implementation (Requires SWIG on Windows)                              #
# --------------------------------------------------------------------------- #


class SpeexAEC(AECProcessor):
    """
    Speex-based Acoustic Echo Cancellation.

    Uses the speexdsp library which provides:
    - Adaptive echo cancellation
    - Noise suppression (optional)

    Install: pip install speexdsp

    Note: The actual speexdsp API uses:
    - speexdsp.EchoCanceller(frame_size, filter_length)
    - canceller.process(rec_frame, play_frame) -> processed_frame
    """

    def __init__(
        self,
        frame_samples: int = AEC_FRAME_SAMPLES,
        filter_length_ms: int = 200,
        enable_denoise: bool = True,
    ) -> None:
        """
        Initialize Speex AEC.

        Args:
            frame_samples: Frame size in samples (must match mic capture)
            filter_length_ms: Echo tail length in milliseconds (50-500ms typical)
            enable_denoise: Whether to also apply noise suppression
        """
        self._frame_samples = frame_samples
        self._filter_length_samples = int(self.SAMPLE_RATE * filter_length_ms / 1000)
        self._enable_denoise = enable_denoise

        self._echo_canceller = None
        self._denoiser = None
        self._initialized = False

        if SPEEX_AVAILABLE:
            self._init_processor()

    def _init_processor(self) -> None:
        """Initialize Speex processors."""
        if speexdsp is None:
            return

        try:
            # EchoCanceller(frame_size, filter_length)
            self._echo_canceller = speexdsp.EchoCanceller(
                self._frame_samples,
                self._filter_length_samples,
            )

            if self._enable_denoise:
                # Denoiser(frame_size, sample_rate)
                self._denoiser = speexdsp.Denoiser(
                    self._frame_samples,
                    self.SAMPLE_RATE,
                )

            self._initialized = True
            logger.info(
                "Speex AEC initialized (frame=%d, filter=%dms, denoise=%s)",
                self._frame_samples,
                self._filter_length_samples * 1000 // self.SAMPLE_RATE,
                self._enable_denoise,
            )

        except Exception as e:
            logger.error("Failed to initialize Speex AEC: %s", e)
            self._initialized = False

    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """Process frame with Speex echo cancellation."""
        if not self._initialized or self._echo_canceller is None:
            return mic_frame

        # Validate frame size
        if len(mic_frame) != self._frame_samples:
            logger.warning(
                "AEC frame size mismatch: got %d, expected %d",
                len(mic_frame),
                self._frame_samples,
            )
            return mic_frame

        try:
            # Convert to bytes (speexdsp expects bytes for int16)
            mic_int16: npt.NDArray[np.int16] = mic_frame.astype(np.int16)
            mic_bytes = mic_int16.tobytes()

            # If no reference, use silence
            if ref_frame is None or len(ref_frame) != self._frame_samples:
                ref_bytes = np.zeros(self._frame_samples, dtype=np.int16).tobytes()
            else:
                ref_int16: npt.NDArray[np.int16] = ref_frame.astype(np.int16)
                ref_bytes = ref_int16.tobytes()

            # Echo cancellation: process(rec_frame, play_frame)
            processed_bytes = self._echo_canceller.process(mic_bytes, ref_bytes)

            # Optional noise suppression
            if self._denoiser is not None:
                processed_bytes = self._denoiser.process(processed_bytes)

            # Convert back to numpy
            result: npt.NDArray[np.int16] = np.frombuffer(processed_bytes, dtype=np.int16)
            return result

        except Exception as e:
            logger.error("Speex AEC processing error: %s", e)
            return mic_frame

    def reset(self) -> None:
        """Reset echo canceller state."""
        if SPEEX_AVAILABLE and self._initialized:
            # Re-initialize to reset state
            self._init_processor()

    @property
    def is_available(self) -> bool:
        """Check if Speex AEC is functional."""
        return SPEEX_AVAILABLE and self._initialized


# --------------------------------------------------------------------------- #
# ViolaAEC Adapter (FDAF with Double-Talk Detection)                           #
# --------------------------------------------------------------------------- #


# Try importing ViolaAEC
VIOLA_AEC_AVAILABLE = False
try:
    from voice.wake_detector.aec_viola import ViolaAEC as _ViolaAECImpl

    VIOLA_AEC_AVAILABLE = True
except ImportError:
    _ViolaAECImpl = None  # type: ignore[assignment, misc]
except (AttributeError, OSError, RuntimeError) as _viola_aec_exc:
    logger.error(
        "ViolaAEC backend failed to import: %s. " "AEC will degrade to the next available backend; wake stays alive.",
        _viola_aec_exc,
    )
    _ViolaAECImpl = None  # type: ignore[assignment, misc]


class ViolaAECAdapter(AECProcessor):
    """
    Adapter wrapping ViolaAEC (FDAF) to conform to the AECProcessor interface.

    ViolaAEC provides frequency-domain adaptive filtering with frame-level
    double-talk detection, converging 10-100x faster than time-domain NLMS.
    """

    def __init__(
        self,
        frame_samples: int = AEC_FRAME_SAMPLES,
        filter_length_ms: int = 150,
        echo_scale: float = 1.0,
        delay_samples: int = DEFAULT_DELAY_SAMPLES,
    ) -> None:
        self._frame_samples = frame_samples
        self._initialized = False
        self._ref_gate = _RefGate(delay_samples, frame_samples)

        # --- Local-playback downshift ---
        # ViolaAEC's FDAF runs a pure-Python numpy FFT/iFFT per frame (~0.9ms
        # at 160 samples). Backend + Qt GUI share ONE Python interpreter lock,
        # so that per-frame FFT holds the GIL ~60x/sec exactly when Viola is
        # playing its own music — starving the UI paint loop and video decode.
        # When Viola's OWN local playback is active (the echo path is Viola's
        # known output, not an external room source), downshift to a cheaper
        # NATIVE echo canceller (PyAEC / SpeexDSP MDF) whose work runs in C with
        # the GIL released, freeing the interpreter for the UI. This still
        # cancels echo (so wake-word detection during music is preserved) — it
        # is NOT NoOp. The heavy FDAF stays on for everything else (mic during a
        # phone call, external room audio), where the reference is unknown.
        self._local_playback_active = False
        self._downshift_backend: AECProcessor | None = None

        if not VIOLA_AEC_AVAILABLE or _ViolaAECImpl is None:
            return

        try:
            self._inner = _ViolaAECImpl(
                frame_size=frame_samples,
                filter_length_ms=filter_length_ms,
                echo_scale=echo_scale,
            )
            self._initialized = True
            logger.info(
                "ViolaAEC (FDAF) initialized (frame=%d, filter=%dms, echo_scale=%.1f)",
                frame_samples,
                filter_length_ms,
                echo_scale,
            )
        except Exception as e:
            logger.error("Failed to initialize ViolaAEC: %s", e)
            self._initialized = False

    def _ensure_downshift_backend(self) -> AECProcessor:
        """Lazily build the cheap native fallback used during local playback.

        Prefers PyAEC (SpeexDSP MDF — native C, releases the GIL, still cancels
        echo). Falls back to NoOp only when no native canceller is available, so
        the interpreter is freed even on a machine without pyaec.
        """
        if self._downshift_backend is not None:
            return self._downshift_backend

        backend: AECProcessor | None = None
        if PYAEC_AVAILABLE:
            try:
                candidate = PyAEC(
                    frame_samples=self._frame_samples,
                    delay_samples=self._ref_gate.delay_samples,
                )
                if candidate.is_available:
                    backend = candidate
                    logger.info("ViolaAEC downshift backend ready: pyaec (native, GIL-releasing)")
            except (ImportError, AttributeError, OSError, RuntimeError, ValueError, MemoryError) as exc:
                logger.warning("ViolaAEC downshift PyAEC unavailable (%s); using NoOp during playback", exc)

        if backend is None:
            backend = NoOpAEC()
            logger.info("ViolaAEC downshift backend ready: noop (no native canceller installed)")

        self._downshift_backend = backend
        return backend

    def set_local_playback_active(self, active: bool) -> None:
        """Route processing to the cheap native backend while Viola plays."""
        active = bool(active)
        if active == self._local_playback_active:
            return
        self._local_playback_active = active
        if active:
            # Build the fallback up front so the first playback frame doesn't
            # pay construction cost on the hot path.
            self._ensure_downshift_backend()
        logger.debug("ViolaAEC local-playback downshift %s", "ON" if active else "OFF")

    def process_frame(
        self,
        mic_frame: npt.NDArray[np.int16],
        ref_frame: npt.NDArray[np.int16] | None,
    ) -> npt.NDArray[np.int16]:
        """Process frame with ViolaAEC FDAF echo cancellation.

        While Viola's own local playback is active, route to the cheap native
        backend instead of the FDAF FFT, freeing the shared interpreter's GIL
        for the UI/video loop.
        """
        if not self._initialized:
            return mic_frame

        if len(mic_frame) != self._frame_samples:
            logger.warning(
                "AEC frame size mismatch: got %d, expected %d",
                len(mic_frame),
                self._frame_samples,
            )
            return mic_frame

        # Downshift: Viola is playing its own output — skip the heavy per-frame
        # FDAF FFT and use the native canceller (its _RefGate aligns the ref).
        if self._local_playback_active:
            return self._ensure_downshift_backend().process_frame(mic_frame, ref_frame)

        # Align ref via the measured speaker-to-mic delay and gate adaptation
        # until sustained playback energy is observed (AUD-01, AUD-08).
        aligned_ref = self._ref_gate.apply(ref_frame)

        return self._inner.process_frame(mic_frame, aligned_ref)

    def reset(self) -> None:
        """Reset ViolaAEC state."""
        if self._initialized:
            self._inner.reset()

    def set_delay_samples(self, delay_samples: int) -> None:
        """Retune the speaker-to-mic delay used for reference alignment."""
        self._ref_gate.set_delay_samples(delay_samples)
        # Keep the native downshift backend's own ref alignment in sync.
        if self._downshift_backend is not None:
            self._downshift_backend.set_delay_samples(delay_samples)

    @property
    def is_available(self) -> bool:
        """Check if ViolaAEC is functional."""
        return VIOLA_AEC_AVAILABLE and self._initialized

    @property
    def name(self) -> str:
        return "viola"

    def get_diagnostics(self) -> dict:
        """Expose ViolaAEC ERLE + reference-gate diagnostics."""
        base: dict = {}
        if self._initialized and hasattr(self._inner, "get_diagnostics"):
            inner = self._inner.get_diagnostics()
            if isinstance(inner, dict):
                base = dict(inner)
        base.update(
            {
                "ref_gate_open": self._ref_gate.is_gate_open,
                "ref_gate_frames_since_reset": self._ref_gate.frames_since_warmup_reset,
                "ref_gate_watchdog_warn_fired": self._ref_gate.watchdog_warn_fired,
                "ref_gate_watchdog_warn_count": self._ref_gate.watchdog_warn_count,
                "ref_gate_delay_samples": self._ref_gate.delay_samples,
            }
        )
        return base


# --------------------------------------------------------------------------- #
# Factory Function                                                             #
# --------------------------------------------------------------------------- #


def create_aec_processor(
    enabled: bool = True,
    backend: str = "auto",
    frame_samples: int = AEC_FRAME_SAMPLES,
    filter_length_ms: int = 200,
    enable_denoise: bool = True,
    delay_samples: int | None = None,
) -> AECProcessor:
    """
    Factory function to create the best available AEC processor.

    Args:
        enabled: Whether AEC is enabled (False returns NoOpAEC)
        backend: Backend preference ("auto", "viola", "pyaec", "speex", "noop")
        frame_samples: Frame size in samples
        filter_length_ms: Echo filter length in milliseconds
        enable_denoise: Whether to enable noise suppression (Speex only)
        delay_samples: Override initial reference delay. When ``None``, read
            the current value from SettingsManager (falls back to
            ``DEFAULT_DELAY_SAMPLES``).

    Returns:
        AECProcessor instance (NoOpAEC if nothing better is available)
    """
    if not enabled:
        logger.info("AEC disabled by configuration")
        return NoOpAEC()

    if backend == "noop":
        return NoOpAEC()

    resolved_delay = _resolve_default_delay_samples() if delay_samples is None else int(delay_samples)

    # Try ViolaAEC first (custom FDAF with double-talk detection).
    # ViolaAEC freezes filter adaptation during speech, preventing the
    # adaptive filter from cancelling the user's voice along with echo.
    # PyAEC (SpeexDSP MDF) lacks DTD and over-cancels during double-talk,
    # crushing mic audio to near-zero (post_aec_rms=2-8 from mic_rms=700).
    # Backend construction is wrapped so that a *broken* optional backend
    # (native library present at import but failing at construction) degrades
    # to the next backend and ultimately to NoOpAEC — never propagates and
    # kills the wake engine (1.0.1 lane-4 P0, L4-3 layer b).
    if backend in ("auto", "viola") and VIOLA_AEC_AVAILABLE:
        try:
            viola_processor = ViolaAECAdapter(
                frame_samples=frame_samples,
                filter_length_ms=150,  # FDAF optimal: 150ms (2400 taps)
                echo_scale=1.0,
                delay_samples=resolved_delay,
            )
            if viola_processor.is_available:
                logger.info("Using ViolaAEC (FDAF+DTD) backend (delay=%d samples)", resolved_delay)
                return viola_processor
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError, MemoryError) as exc:
            logger.error(
                "ViolaAEC backend construction failed: %s. Degrading to next AEC backend; wake stays alive.",
                exc,
            )

    # Try PyAEC as fallback (SpeexDSP MDF — good ERLE but no DTD,
    # so it over-cancels during double-talk / speech)
    if backend in ("auto", "pyaec") and PYAEC_AVAILABLE:
        try:
            pyaec_processor = PyAEC(
                frame_samples=frame_samples,
                filter_length_ms=filter_length_ms,
                delay_samples=resolved_delay,
            )
            if pyaec_processor.is_available:
                logger.info("Using PyAEC (SpeexDSP MDF) backend (delay=%d samples, no DTD)", resolved_delay)
                return pyaec_processor
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError, MemoryError) as exc:
            logger.error(
                "PyAEC backend construction failed: %s. Degrading to next AEC backend; wake stays alive.",
                exc,
            )

    # Try Speex (requires SWIG on Windows)
    if backend in ("auto", "speex") and SPEEX_AVAILABLE:
        try:
            speex_processor = SpeexAEC(
                frame_samples=frame_samples,
                filter_length_ms=filter_length_ms,
                enable_denoise=enable_denoise,
            )
            if speex_processor.is_available:
                logger.info("Using Speex AEC backend")
                return speex_processor
        except (ImportError, AttributeError, OSError, RuntimeError, ValueError, MemoryError) as exc:
            logger.error(
                "Speex AEC backend construction failed: %s. Degrading to noop AEC; wake stays alive.",
                exc,
            )

    # Fallback to NoOp
    if backend == "auto":
        logger.warning("No AEC library available. Install pyaec for echo cancellation: pip install pyaec")
    else:
        logger.warning("Requested AEC backend '%s' not available", backend)

    return NoOpAEC()


# --------------------------------------------------------------------------- #
# Exports                                                                      #
# --------------------------------------------------------------------------- #

__all__ = [
    "AEC_FRAME_SAMPLES",
    "AEC_SAMPLE_RATE",
    "DEFAULT_DELAY_SAMPLES",
    "PYAEC_AVAILABLE",
    "SPEEX_AVAILABLE",
    "VIOLA_AEC_AVAILABLE",
    "AECProcessor",
    "AECReferenceSource",
    "NoOpAEC",
    "PyAEC",
    "SpeexAEC",
    "ViolaAECAdapter",
    "create_aec_processor",
]
