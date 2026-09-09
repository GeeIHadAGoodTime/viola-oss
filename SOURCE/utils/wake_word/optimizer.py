"""
Wake word threshold optimization.

Optimizes wake word threshold based on runtime performance.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DetectionStats:
    """Wake word detection statistics"""

    total_detections: int = 0
    false_accepts: int = 0
    true_accepts: int = 0
    missed_commands: int = 0
    confidence_scores: deque[float] = field(default_factory=lambda: deque(maxlen=1000))  # Keep last 1000 scores


class WakeWordOptimizer:
    """
    Optimizes wake word threshold based on runtime performance.

    Features:
    - Adaptive threshold adjustment
    - Background noise adaptation
    - Confidence scoring
    """

    def __init__(
        self,
        initial_threshold: float = 0.5,
        min_threshold: float = 0.3,
        max_threshold: float = 0.9,
        adaptation_rate: float = 0.1,
    ):
        """
        Initialize optimizer.

        Args:
            initial_threshold: Initial threshold value
            min_threshold: Minimum allowed threshold
            max_threshold: Maximum allowed threshold
            adaptation_rate: Rate of adaptation (0.0-1.0)
        """
        self.current_threshold = initial_threshold
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.adaptation_rate = adaptation_rate

        self.stats = DetectionStats()
        self.last_optimization = time.time()
        self.optimization_interval = 60.0  # Optimize every 60 seconds
        self._history: deque[tuple[float, float]] = deque(maxlen=24)

    def record_detection(
        self,
        confidence: float,
        is_true_positive: bool = True,
        is_false_positive: bool = False,
    ):
        """
        Record a wake word detection.

        Args:
            confidence: Confidence score (0.0-1.0)
            is_true_positive: True if this was a valid detection
            is_false_positive: True if this was a false positive
        """
        self.stats.total_detections += 1
        self.stats.confidence_scores.append(confidence)

        if is_false_positive:
            self.stats.false_accepts += 1
        elif is_true_positive:
            self.stats.true_accepts += 1

        # Periodic optimization
        current_time = time.time()
        if current_time - self.last_optimization > self.optimization_interval:
            self._optimize()
            self.last_optimization = current_time

    def record_missed_command(self):
        """Record a missed wake word command"""
        self.stats.missed_commands += 1

    def _optimize(self):
        """Optimize threshold based on current statistics"""
        if self.stats.total_detections < 10:
            # Not enough data
            return

        # Calculate false accept rate
        hours = (time.time() - (self.last_optimization - self.optimization_interval)) / 3600.0
        false_accept_rate = self.stats.false_accepts / hours if hours > 0 else 0.0

        target_far = 0.2  # Target: 0.2 false accepts per hour

        # Adjust threshold based on false accept rate
        if false_accept_rate > target_far * 1.5:
            # Too many false accepts, raise threshold
            adjustment = self.adaptation_rate * 0.1
            self.current_threshold = min(self.max_threshold, self.current_threshold + adjustment)
            logger.debug(
                "Raising threshold to %.3f (FAR: %.2f/hr)",
                self.current_threshold,
                false_accept_rate,
            )
        elif false_accept_rate < target_far * 0.5 and self.stats.missed_commands > 0:
            # Too few detections, might be missing commands, lower threshold slightly
            adjustment = self.adaptation_rate * 0.05
            self.current_threshold = max(self.min_threshold, self.current_threshold - adjustment)
            logger.debug(
                "Lowering threshold to %.3f (missed: %s)",
                self.current_threshold,
                self.stats.missed_commands,
            )

    def get_optimal_threshold(self) -> float:
        """
        Get current optimal threshold.

        Returns:
            Optimal threshold value
        """
        return self.current_threshold

    def get_stats(self) -> dict[str, int | float]:
        """
        Get optimization statistics.

        Returns:
            Statistics dictionary
        """
        hours = (time.time() - (self.last_optimization - self.optimization_interval)) / 3600.0
        false_accept_rate = self.stats.false_accepts / hours if hours > 0 else 0.0

        average_confidence = (
            sum(self.stats.confidence_scores) / len(self.stats.confidence_scores)
            if self.stats.confidence_scores
            else 0.0
        )

        stats: dict[str, int | float] = {
            "threshold": self.current_threshold,
            "total_detections": self.stats.total_detections,
            "false_accepts": self.stats.false_accepts,
            "true_accepts": self.stats.true_accepts,
            "missed_commands": self.stats.missed_commands,
            "false_accept_rate_per_hour": false_accept_rate,
            "average_confidence": average_confidence,
        }
        return stats

    def reset_stats(self):
        """Reset detection statistics"""
        self.stats = DetectionStats()

    def update_threshold(self, new_threshold: float) -> float:
        """
        Update the current threshold with clamping.

        Args:
            new_threshold: Proposed new threshold value.

        Returns:
            The clamped threshold value that was applied.
        """
        clamped = max(self.min_threshold, min(self.max_threshold, float(new_threshold)))
        if not math.isclose(clamped, self.current_threshold, rel_tol=1e-6, abs_tol=1e-6):
            logger.debug(
                "WakeWordOptimizer threshold adjusted: %.3f → %.3f",
                self.current_threshold,
                clamped,
            )
        self.current_threshold = clamped
        self._history.append((time.time(), clamped))
        return clamped

    def get_recent_thresholds(self) -> deque[tuple[float, float]]:
        """Expose recent threshold history (timestamp, value) for diagnostics."""
        return self._history.copy()
