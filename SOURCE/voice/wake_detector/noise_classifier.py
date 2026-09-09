"""
Lightweight background noise classifier for wake word listeners.

Provides rolling RMS analysis to categorize the environment into coarse
profiles (quiet, normal, noisy). Used by adaptive gain scheduling to
auto-adjust thresholds and telemetry.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class NoiseSnapshot:
    """Rolling snapshot of noise level statistics."""

    profile: str
    average_rms: float
    peak_rms: float


class NoiseProfileClassifier:
    """
    Classify audio RMS levels into noise profiles.

    The classifier maintains a rolling window of RMS samples and
    emits profile transitions only when the average crosses the
    configured thresholds to avoid jitter.
    """

    def __init__(
        self,
        quiet_threshold: float = 350.0,
        noisy_threshold: float = 1400.0,
        window_size: int = 50,
    ) -> None:
        if quiet_threshold >= noisy_threshold:
            noisy_threshold = quiet_threshold * 1.5

        self._quiet_threshold = float(quiet_threshold)
        self._noisy_threshold = float(noisy_threshold)
        self._samples: deque[float] = deque(maxlen=max(5, window_size))
        self._peak = 0.0

    def classify(self, rms_value: float) -> NoiseSnapshot:
        """
        Update the classifier with the latest RMS value.

        Args:
            rms_value: Root-mean-square amplitude of current frame.

        Returns:
            NoiseSnapshot describing the current profile.
        """
        value = abs(float(rms_value))
        self._samples.append(value)
        self._peak = max(self._peak * 0.95, value)

        avg = sum(self._samples) / len(self._samples) if self._samples else value
        profile = self._profile_for(avg)

        return NoiseSnapshot(profile=profile, average_rms=avg, peak_rms=self._peak)

    def _profile_for(self, avg_rms: float) -> str:
        if avg_rms <= self._quiet_threshold:
            return "quiet"
        if avg_rms >= self._noisy_threshold:
            return "noisy"
        return "normal"

    def bounds(self) -> tuple[float, float]:
        """Return (quiet_threshold, noisy_threshold)."""
        return self._quiet_threshold, self._noisy_threshold
