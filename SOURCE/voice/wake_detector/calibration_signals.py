"""
Calibration Signal Generation for AEC Delay Measurement
========================================================

Generates calibration signals for measuring speaker-to-microphone delay.

DESIGN:
- Uses marker + chirp signal for robust detection
- Marker (click) provides precise timing reference
- Chirp provides frequency diversity for correlation

WHY MARKER + CHIRP:
1. Click is easy to detect even in noise (impulse)
2. Chirp correlation has sharp peak (low ambiguity)
3. Combined: robust timing + accurate delay measurement

SIGNAL STRUCTURE:
[silence: 50ms][marker: 10ms][silence: 20ms][chirp: 200ms][silence: 50ms]

Total duration: ~330ms
"""

from __future__ import annotations

import numpy as np

from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_48K
from core.logging_config import get_logger

logger = get_logger(__name__)


class CalibrationSignalGenerator:
    """
    Generate calibration signals for delay measurement.

    SIGNAL STRUCTURE:
    [silence: 50ms][marker: 10ms][silence: 20ms][chirp: 200ms][silence: 50ms]

    Total duration: ~330ms
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE_48K):
        """
        Initialize generator.

        Args:
            sample_rate: Output sample rate for playback
        """
        self._sample_rate = sample_rate

        # Timing (in samples)
        self._pre_silence = int(0.050 * sample_rate)  # 50ms
        self._marker_duration = int(0.010 * sample_rate)  # 10ms
        self._gap = int(0.020 * sample_rate)  # 20ms
        self._chirp_duration = int(0.200 * sample_rate)  # 200ms
        self._post_silence = int(0.050 * sample_rate)  # 50ms

        # Chirp parameters
        self._chirp_f0 = 500  # Start frequency
        self._chirp_f1 = 6000  # End frequency

        # Pre-generate signals
        self._marker = self._generate_marker()
        self._chirp = self._generate_chirp()
        self._full_signal: np.ndarray | None = None

    def _generate_marker(self) -> np.ndarray:
        """
        Generate timing marker (shaped impulse).

        Uses Gaussian-windowed click for clean spectrum.
        """
        t = np.arange(self._marker_duration) / self._sample_rate

        # Gaussian envelope
        center = self._marker_duration / 2 / self._sample_rate
        sigma = 0.002  # 2ms width
        envelope = np.exp(-((t - center) ** 2) / (2 * sigma**2))

        # High-frequency carrier for detectability
        carrier = np.sin(2 * np.pi * 4000 * t)

        return (envelope * carrier * 0.8).astype(np.float32)

    def _generate_chirp(self) -> np.ndarray:
        """
        Generate frequency sweep (chirp) signal.

        Linear chirp from f0 to f1 with Tukey window.
        """
        try:
            from scipy import signal as scipy_signal

            t = np.arange(self._chirp_duration) / self._sample_rate

            # Linear chirp
            chirp = scipy_signal.chirp(
                t,
                self._chirp_f0,
                t[-1],
                self._chirp_f1,
                method="linear",
            )

            # Apply Tukey window (tapered edges)
            window = scipy_signal.windows.tukey(len(chirp), alpha=0.1)

            return (chirp * window * 0.7).astype(np.float32)

        except ImportError:
            # Fallback: simple sine sweep without scipy
            logger.warning("scipy not available, using simple sine sweep")
            return self._generate_simple_chirp()

    def _generate_simple_chirp(self) -> np.ndarray:
        """Generate chirp without scipy (fallback)."""
        t = np.arange(self._chirp_duration) / self._sample_rate
        duration = t[-1]

        # Instantaneous frequency: linear from f0 to f1
        # Phase is integral of frequency
        k = (self._chirp_f1 - self._chirp_f0) / duration
        phase = 2 * np.pi * (self._chirp_f0 * t + 0.5 * k * t**2)

        chirp = np.sin(phase)

        # Simple fade in/out
        fade_samples = int(0.01 * self._sample_rate)  # 10ms fade
        fade_in = np.linspace(0, 1, fade_samples)
        fade_out = np.linspace(1, 0, fade_samples)

        chirp[:fade_samples] *= fade_in
        chirp[-fade_samples:] *= fade_out

        return (chirp * 0.7).astype(np.float32)

    def generate(self) -> np.ndarray:
        """
        Generate complete calibration signal.

        Returns:
            Calibration signal as float32 array
        """
        if self._full_signal is not None:
            return self._full_signal.copy()

        total_length = self._pre_silence + self._marker_duration + self._gap + self._chirp_duration + self._post_silence

        signal: np.ndarray = np.zeros(total_length, dtype=np.float32)

        # Insert marker
        pos = self._pre_silence
        signal[pos : pos + self._marker_duration] = self._marker

        # Insert chirp
        pos = self._pre_silence + self._marker_duration + self._gap
        signal[pos : pos + self._chirp_duration] = self._chirp

        self._full_signal = signal

        logger.debug(
            "Generated calibration signal: %s samples (%sms)",
            len(signal),
            format(len(signal) / self._sample_rate * 1000, ".0f"),
        )

        return signal.copy()

    def get_marker_template(self) -> np.ndarray:
        """Get marker template for detection."""
        return self._marker.copy()

    def get_chirp_template(self) -> np.ndarray:
        """Get chirp template for correlation."""
        return self._chirp.copy()

    @property
    def marker_offset_samples(self) -> int:
        """Offset of marker start from signal start."""
        return self._pre_silence

    @property
    def chirp_offset_samples(self) -> int:
        """Offset of chirp start from signal start."""
        return self._pre_silence + self._marker_duration + self._gap

    @property
    def total_duration_ms(self) -> float:
        """Total signal duration in milliseconds."""
        total = self._pre_silence + self._marker_duration + self._gap + self._chirp_duration + self._post_silence
        return total / self._sample_rate * 1000

    @property
    def sample_rate(self) -> int:
        """Signal sample rate."""
        return self._sample_rate

    def to_int16(self) -> np.ndarray:
        """Get signal as int16 for playback."""
        return (self.generate() * AUDIO_INT16_MAX).astype(np.int16)

    def to_stereo_int16(self) -> np.ndarray:
        """Get signal as stereo int16 for playback (duplicated channels)."""
        mono = self.to_int16()
        return np.column_stack((mono, mono)).flatten()


__all__ = ["CalibrationSignalGenerator"]
