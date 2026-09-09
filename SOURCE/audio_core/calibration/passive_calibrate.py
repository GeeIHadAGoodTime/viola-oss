"""
Passive AEC Delay Calibrator
=============================

Measures actual speaker-to-mic acoustic delay by cross-correlating
ChunkStamper reference PCM against microphone recordings during normal
music playback.  No calibration tone needed — uses the music itself.

Designed to run in a background thread without adding latency to the
audio path.

Usage (from ViolaWakeListener)::

    calibrator = PassiveCalibrator()

    # Every ~30s during playback:
    result = calibrator.measure(reference_pcm, mic_pcm, sample_rate=16000)
    if result.confident:
        aec_adapter.set_buffer_target(result.delay_ms)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)

# Cross-correlation search range
MAX_LAG_MS = 350.0  # Never search beyond 350ms
MIN_CONFIDENCE = 0.3  # Minimum peak correlation to accept


@dataclass
class PassiveMeasurement:
    """Result of a passive delay measurement."""

    delay_ms: float = 0.0
    delay_samples: int = 0
    peak_correlation: float = 0.0
    confident: bool = False
    failure_reason: str = ""


class PassiveCalibrator:
    """Cross-correlates reference PCM against mic PCM to measure acoustic delay.

    The reference signal (from ChunkStamper ring buffer) is what the speakers
    are playing.  The mic signal is what the microphone records.  The peak
    of the cross-correlation gives the acoustic propagation delay.
    """

    def __init__(
        self,
        min_confidence: float = MIN_CONFIDENCE,
        max_lag_ms: float = MAX_LAG_MS,
    ) -> None:
        self._min_confidence = min_confidence
        self._max_lag_ms = max_lag_ms

    def measure(
        self,
        reference: np.ndarray,
        mic: np.ndarray,
        sample_rate: int = SAMPLE_RATE_16K,
    ) -> PassiveMeasurement:
        """Measure speaker-to-mic delay via cross-correlation.

        Args:
            reference: PCM from ChunkStamper (what speakers play), int16.
            mic: PCM from microphone (what mic records), int16.
            sample_rate: Sample rate of both signals (must match).

        Returns:
            PassiveMeasurement with delay and confidence.
        """
        if len(reference) < 100 or len(mic) < 100:
            return PassiveMeasurement(failure_reason="insufficient_audio")

        max_lag_samples = int(self._max_lag_ms * sample_rate / 1000.0)

        # Convert to float and normalize
        ref_f = reference.astype(np.float32)
        mic_f = mic.astype(np.float32)

        ref_norm = np.linalg.norm(ref_f)
        mic_norm = np.linalg.norm(mic_f)

        if ref_norm < 1e-6 or mic_norm < 1e-6:
            return PassiveMeasurement(failure_reason="silent_signal")

        ref_f = ref_f / ref_norm
        mic_f = mic_f / mic_norm

        # Use FFT-based cross-correlation for speed
        try:
            n = len(ref_f) + len(mic_f) - 1
            fft_size = 1
            while fft_size < n:
                fft_size *= 2

            ref_fft = np.fft.rfft(ref_f, fft_size)
            mic_fft = np.fft.rfft(mic_f, fft_size)

            # Cross-correlation: IFFT of conj(Ref) * Mic
            xcorr = np.fft.irfft(np.conj(ref_fft) * mic_fft, fft_size)

            # Only look at positive lags (mic delayed relative to ref)
            # up to max_lag_samples
            search_len = min(max_lag_samples, len(xcorr) // 2)
            if search_len < 1:
                return PassiveMeasurement(failure_reason="search_window_too_small")

            positive_lags = xcorr[:search_len]

        except Exception as e:
            logger.warning("[AEC_CALIBRATE] FFT correlation failed: %s", e)
            return PassiveMeasurement(failure_reason="correlation_error")

        # Find peak
        peak_idx = int(np.argmax(np.abs(positive_lags)))
        peak_value = float(np.abs(positive_lags[peak_idx]))

        delay_ms = peak_idx / sample_rate * 1000.0

        confident = peak_value >= self._min_confidence

        if confident:
            logger.info(
                "[AEC_CALIBRATE] measured_delay=%.0fms, confidence=%.3f, peak_corr=%.3f",
                delay_ms,
                peak_value,
                peak_value,
            )
        else:
            logger.debug(
                "[AEC_CALIBRATE] low_confidence: delay=%.0fms, corr=%.3f < %.3f",
                delay_ms,
                peak_value,
                self._min_confidence,
            )

        return PassiveMeasurement(
            delay_ms=delay_ms,
            delay_samples=peak_idx,
            peak_correlation=peak_value,
            confident=confident,
            failure_reason="" if confident else "weak_correlation",
        )


__all__ = ["PassiveCalibrator", "PassiveMeasurement"]
