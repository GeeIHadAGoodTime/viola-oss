"""
Startup AEC Calibration
=======================

Automatically calibrates AEC delay using a startup sound.
Runs once per device, saving the calibration to a per-device profile.

DESIGN:
- On first startup for a device, plays a pleasant orchestral/chime sound
- Records from microphone simultaneously
- Cross-correlates to measure speaker-to-mic delay
- Saves to device profile for future startups

This provides "set and forget" calibration without requiring manual intervention.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import numpy as np

from core.constants import SAMPLE_RATE_16K, SAMPLE_RATE_48K
from core.logging_config import get_logger

if TYPE_CHECKING:
    from voice.wake_detector.device_profile_manager import DeviceProfile

logger = get_logger(__name__)

# Default startup sound duration
STARTUP_SOUND_DURATION_SECONDS = 2.0

# Minimum correlation for valid calibration
MIN_CORRELATION_THRESHOLD = 0.15

# Maximum reasonable delay (200ms at 16kHz = 3200 samples)
MAX_DELAY_SAMPLES = 3200


class StartupCalibrator:
    """
    Calibrates AEC delay using startup audio.

    Usage:
        calibrator = StartupCalibrator()

        # Check if calibration needed
        if calibrator.needs_calibration(device_profile):
            result = calibrator.calibrate_with_audio(
                played_audio=startup_sound,
                recorded_audio=mic_recording,
            )
            if result.success:
                device_profile.aec_delay_ms = result.delay_ms
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE_16K,
        on_progress: Callable[[str, float], None] | None = None,
    ):
        self._sample_rate = sample_rate
        self._on_progress = on_progress
        self._lock = threading.Lock()
        self._calibrating = False

    def needs_calibration(self, profile: DeviceProfile | None) -> bool:
        """Check if device needs AEC calibration."""
        if profile is None:
            return True
        if profile.aec_delay_ms is None:
            return True
        if profile.aec_calibration_confidence < MIN_CORRELATION_THRESHOLD:
            return True
        return False

    def calibrate_with_audio(
        self,
        played_audio: np.ndarray,
        recorded_audio: np.ndarray,
        playback_sample_rate: int = SAMPLE_RATE_48K,
        recording_sample_rate: int = SAMPLE_RATE_16K,
    ) -> CalibrationResult:
        """
        Calibrate using provided audio samples.

        Args:
            played_audio: The audio that was played through speakers
            recorded_audio: The audio recorded from microphone
            playback_sample_rate: Sample rate of played audio
            recording_sample_rate: Sample rate of recorded audio

        Returns:
            CalibrationResult with delay and confidence
        """
        with self._lock:
            if self._calibrating:
                return CalibrationResult(success=False, failure_reason="calibration_in_progress")
            self._calibrating = True

        try:
            return self._calibrate_impl(
                played_audio,
                recorded_audio,
                playback_sample_rate,
                recording_sample_rate,
            )
        finally:
            with self._lock:
                self._calibrating = False

    def _calibrate_impl(
        self,
        played: np.ndarray,
        recorded: np.ndarray,
        playback_rate: int,
        recording_rate: int,
    ) -> CalibrationResult:
        """Internal calibration implementation."""
        self._report_progress("resampling", 0.1)

        # Resample played audio to recording sample rate for correlation
        if playback_rate != recording_rate:
            played = self._resample(played, playback_rate, recording_rate)

        self._report_progress("correlating", 0.4)

        # Convert to float for correlation
        played_f = played.astype(np.float32)
        recorded_f = recorded.astype(np.float32)

        # Normalize
        played_f = played_f / (np.max(np.abs(played_f)) + 1e-10)
        recorded_f = recorded_f / (np.max(np.abs(recorded_f)) + 1e-10)

        # Cross-correlate to find delay
        try:
            from scipy import signal as scipy_signal

            correlation = scipy_signal.correlate(recorded_f, played_f, mode="full")
        except ImportError:
            # Fallback without scipy
            correlation = np.correlate(recorded_f, played_f, mode="full")

        self._report_progress("analyzing", 0.7)

        # Find peak
        center = len(played_f) - 1
        peak_idx = np.argmax(np.abs(correlation))
        delay_samples = peak_idx - center

        # Normalize correlation to 0-1 range
        peak_value = np.abs(correlation[peak_idx])
        correlation_strength = peak_value / (np.sqrt(np.sum(played_f**2) * np.sum(recorded_f**2)) + 1e-10)

        self._report_progress("validating", 0.9)

        # Validate delay is reasonable
        if delay_samples < 0:
            # Negative delay means recording started before playback (shouldn't happen)
            logger.warning(
                "Negative delay detected (%d samples), using absolute value",
                delay_samples,
            )
            delay_samples = abs(delay_samples)

        if delay_samples > MAX_DELAY_SAMPLES:
            return CalibrationResult(
                success=False,
                failure_reason=f"delay_too_large: {delay_samples} samples > {MAX_DELAY_SAMPLES}",
            )

        # Check correlation strength
        if correlation_strength < MIN_CORRELATION_THRESHOLD:
            return CalibrationResult(
                success=False,
                failure_reason=f"weak_correlation: {correlation_strength:.3f} < {MIN_CORRELATION_THRESHOLD}",
            )

        self._report_progress("complete", 1.0)

        delay_ms = delay_samples / recording_rate * 1000

        logger.info(
            "Startup calibration successful: delay=%d samples (%.1fms), correlation=%.3f",
            delay_samples,
            delay_ms,
            correlation_strength,
        )

        return CalibrationResult(
            success=True,
            delay_samples=delay_samples,
            delay_ms=delay_ms,
            confidence=correlation_strength,
        )

    def _resample(self, audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        """Resample audio to target sample rate."""
        if from_rate == to_rate:
            return audio

        try:
            from scipy import signal as scipy_signal

            num_samples = int(len(audio) * to_rate / from_rate)
            return scipy_signal.resample(audio, num_samples).astype(audio.dtype)
        except ImportError:
            # Simple decimation/interpolation fallback
            ratio = to_rate / from_rate
            indices = np.arange(0, len(audio), 1 / ratio).astype(int)
            indices = np.clip(indices, 0, len(audio) - 1)
            return audio[indices]

    def _report_progress(self, stage: str, progress: float) -> None:
        """Report calibration progress."""
        if self._on_progress:
            try:
                self._on_progress(stage, progress)
            except Exception as e:
                logger.warning("Progress callback failed: %s", e)


class CalibrationResult:
    """Result of startup calibration."""

    def __init__(
        self,
        success: bool,
        delay_samples: int = 0,
        delay_ms: float = 0.0,
        confidence: float = 0.0,
        failure_reason: str = "",
    ):
        self.success = success
        self.delay_samples = delay_samples
        self.delay_ms = delay_ms
        self.confidence = confidence
        self.failure_reason = failure_reason

    def __repr__(self) -> str:
        if self.success:
            return f"CalibrationResult(success=True, delay_ms={self.delay_ms:.1f}, confidence={self.confidence:.3f})"
        return f"CalibrationResult(success=False, reason={self.failure_reason!r})"


def generate_startup_chime(
    duration_seconds: float = 2.0,
    sample_rate: int = SAMPLE_RATE_48K,
) -> np.ndarray:
    """
    Generate a pleasant startup chime for calibration.

    Creates a rich harmonic sound suitable for correlation-based delay detection.
    The chime uses multiple frequencies to ensure good correlation even in
    rooms with frequency-selective acoustics.

    Args:
        duration_seconds: Length of chime
        sample_rate: Output sample rate

    Returns:
        Audio samples as int16 array
    """
    t = np.linspace(0, duration_seconds, int(sample_rate * duration_seconds), dtype=np.float32)

    # Major chord frequencies (C4, E4, G4, C5) for pleasant sound
    frequencies = [261.63, 329.63, 392.00, 523.25]

    # Generate harmonically rich signal
    signal = np.zeros_like(t)
    for i, freq in enumerate(frequencies):
        # Fade in/out envelope
        envelope = np.sin(np.pi * t / duration_seconds) ** 2

        # Add fundamental and harmonics
        signal += envelope * np.sin(2 * np.pi * freq * t) * (0.5**i)
        signal += envelope * np.sin(2 * np.pi * freq * 2 * t) * (0.25**i)  # 2nd harmonic
        signal += envelope * np.sin(2 * np.pi * freq * 3 * t) * (0.125**i)  # 3rd harmonic

    # Add some attack transient for better correlation
    attack = np.exp(-t * 10) * np.sin(2 * np.pi * 1000 * t)
    signal = signal * 0.8 + attack * 0.2

    # Normalize to 80% of int16 range
    signal = signal / np.max(np.abs(signal)) * 0.8 * 32767

    return signal.astype(np.int16)


def load_startup_sound(
    path: Path | str | None = None,
    sample_rate: int = SAMPLE_RATE_48K,
) -> np.ndarray:
    """
    Load startup sound from file, or generate default chime.

    Args:
        path: Path to audio file (WAV format). If None, generates chime.
        sample_rate: Expected sample rate

    Returns:
        Audio samples as int16 array
    """
    if path is None:
        logger.debug("No startup sound configured, using generated chime")
        return generate_startup_chime(sample_rate=sample_rate)

    path = Path(path)
    if not path.exists():
        logger.warning("Startup sound not found at %s, using generated chime", path)
        return generate_startup_chime(sample_rate=sample_rate)

    try:
        import wave

        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() > 1:
                # Mix to mono
                frames = wav.readframes(wav.getnframes())
                audio = np.frombuffer(frames, dtype=np.int16)
                audio = audio.reshape(-1, wav.getnchannels()).mean(axis=1).astype(np.int16)
            else:
                audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)

            # Resample if needed
            if wav.getframerate() != sample_rate:
                try:
                    from scipy import signal as scipy_signal

                    num_samples = int(len(audio) * sample_rate / wav.getframerate())
                    audio = scipy_signal.resample(audio, num_samples).astype(np.int16)
                except ImportError:
                    logger.warning("scipy not available for resampling, using file sample rate")

            return audio

    except Exception as e:
        logger.warning("Failed to load startup sound from %s: %s", path, e)
        return generate_startup_chime(sample_rate=sample_rate)
