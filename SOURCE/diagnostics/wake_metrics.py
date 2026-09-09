"""
Wake Word Diagnostics Metrics

Tracks wake word detection behavior:
- Raw triggers (each time detector fires)
- Accepted wakes (each time orchestrator enters LISTENING)
- Rejected wakes (cooldown, already listening, etc.)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from diagnostics.bus import get_diagnostics_bus

logger = get_logger(__name__)


@dataclass
class WakeMetricsSnapshot:
    """Snapshot of wake word metrics at a point in time."""

    raw_wake_triggers: int = 0
    accepted_wakes: int = 0
    rejected_wakes_cooldown: int = 0
    rejected_wakes_already_listening: int = 0
    rejected_wakes_other: int = 0

    # Per-minute rates (calculated from recent history)
    raw_triggers_per_minute: float = 0.0
    accepted_wakes_per_minute: float = 0.0

    # Timestamps for rate calculation
    first_trigger_time: float | None = None
    last_trigger_time: float | None = None
    last_accepted_time: float | None = None

    # Recent history for rate calculation (last 60 seconds)
    recent_triggers: deque = field(default_factory=lambda: deque(maxlen=100))
    recent_accepted: deque = field(default_factory=lambda: deque(maxlen=100))


class WakeWordMetrics:
    """
    Thread-safe wake word metrics collector.

    Tracks:
    - Raw wake triggers (detector fires)
    - Accepted wakes (orchestrator enters LISTENING)
    - Rejected wakes by reason (cooldown, already listening, etc.)
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._raw_wake_triggers = 0
        self._accepted_wakes = 0
        self._rejected_wakes_cooldown = 0
        self._rejected_wakes_already_listening = 0
        self._rejected_wakes_other = 0
        self._false_positives_marked = 0  # User-marked false positives (hard negatives)

        # Timestamps for rate calculation
        self._first_trigger_time: float | None = None
        self._last_trigger_time: float | None = None
        self._last_accepted_time: float | None = None

        # Recent history (last 60 seconds) for per-minute rate calculation
        self._recent_triggers: deque[float] = deque(maxlen=100)
        self._recent_accepted: deque[float] = deque(maxlen=100)

        # Confidence score tracking (rolling window of last 100 accepted wakes)
        self._confidence_scores: deque[float] = deque(maxlen=100)

        self._diagnostics_bus = get_diagnostics_bus()

    def record_raw_trigger(self) -> None:
        """Record a raw wake word trigger (detector fired)."""
        now = time.time()
        with self._lock:
            self._raw_wake_triggers += 1
            if self._first_trigger_time is None:
                self._first_trigger_time = now
            self._last_trigger_time = now
            self._recent_triggers.append(now)

        self._diagnostics_bus.emit(
            "wake.raw_trigger",
            severity="DEBUG",
            message="Raw wake word trigger detected",
            timestamp=now,
        )
        logger.debug("Raw wake trigger recorded (total: %d)", self._raw_wake_triggers)

    def record_accepted_wake(self, confidence: float | None = None) -> None:
        """Record an accepted wake (orchestrator entered LISTENING).

        Args:
            confidence: Optional confidence score from wake detection (0.0 to 1.0)
        """
        now = time.time()
        with self._lock:
            self._accepted_wakes += 1
            self._last_accepted_time = now
            self._recent_accepted.append(now)
            if confidence is not None:
                self._confidence_scores.append(confidence)

        self._diagnostics_bus.emit(
            "wake.accepted",
            severity="INFO",
            message="Wake word accepted, entering LISTENING",
            timestamp=now,
            confidence=confidence,
        )
        logger.info(
            "Accepted wake recorded (total: %d, confidence: %s)",
            self._accepted_wakes,
            f"{confidence:.2f}" if confidence is not None else "N/A",
        )

    def record_rejected_wake_cooldown(self) -> None:
        """Record a rejected wake due to cooldown."""
        with self._lock:
            self._rejected_wakes_cooldown += 1

        self._diagnostics_bus.emit(
            "wake.rejected",
            severity="DEBUG",
            message="Wake word rejected: cooldown",
            reason="cooldown",
        )
        logger.debug(
            "Rejected wake (cooldown) recorded (total: %d)",
            self._rejected_wakes_cooldown,
        )

    def record_rejected_wake_already_listening(self) -> None:
        """Record a rejected wake because already listening."""
        with self._lock:
            self._rejected_wakes_already_listening += 1

        self._diagnostics_bus.emit(
            "wake.rejected",
            severity="DEBUG",
            message="Wake word rejected: already listening",
            reason="already_listening",
        )
        logger.debug(
            "Rejected wake (already listening) recorded (total: %d)",
            self._rejected_wakes_already_listening,
        )

    def record_rejected_wake_other(self, reason: str = "unknown") -> None:
        """Record a rejected wake for other reasons."""
        with self._lock:
            self._rejected_wakes_other += 1

        self._diagnostics_bus.emit(
            "wake.rejected",
            severity="DEBUG",
            message=f"Wake word rejected: {reason}",
            reason=reason,
        )
        logger.debug(
            "Rejected wake (%s) recorded (total: %d)",
            reason,
            self._rejected_wakes_other,
        )

    def record_false_positive_marked(self) -> None:
        """Record when user marks a detection as false positive."""
        with self._lock:
            self._false_positives_marked += 1

        self._diagnostics_bus.emit(
            "wake.false_positive_marked",
            severity="INFO",
            message="User marked wake detection as false positive",
        )
        logger.info("False positive marked (total: %d)", self._false_positives_marked)

    def _calculate_per_minute_rate(self, timestamps: deque[float], window_seconds: float = 60.0) -> float:
        """Calculate events per minute from recent timestamps."""
        if not timestamps:
            return 0.0

        now = time.time()
        cutoff = now - window_seconds

        # Count events in the window
        count = sum(1 for ts in timestamps if ts >= cutoff)

        # Convert to per-minute rate
        return (count / window_seconds) * 60.0

    def get_avg_confidence(self) -> float | None:
        """Get average confidence score from recent accepted wakes.

        Returns:
            Average confidence (0.0-1.0) or None if no confidence data available.
        """
        with self._lock:
            if not self._confidence_scores:
                return None
            return sum(self._confidence_scores) / len(self._confidence_scores)

    def get_snapshot(self) -> WakeMetricsSnapshot:
        """Get a snapshot of current metrics."""
        with self._lock:
            # Calculate per-minute rates
            raw_rate = self._calculate_per_minute_rate(self._recent_triggers)
            accepted_rate = self._calculate_per_minute_rate(self._recent_accepted)

            return WakeMetricsSnapshot(
                raw_wake_triggers=self._raw_wake_triggers,
                accepted_wakes=self._accepted_wakes,
                rejected_wakes_cooldown=self._rejected_wakes_cooldown,
                rejected_wakes_already_listening=self._rejected_wakes_already_listening,
                rejected_wakes_other=self._rejected_wakes_other,
                raw_triggers_per_minute=raw_rate,
                accepted_wakes_per_minute=accepted_rate,
                first_trigger_time=self._first_trigger_time,
                last_trigger_time=self._last_trigger_time,
                last_accepted_time=self._last_accepted_time,
                recent_triggers=deque(self._recent_triggers),
                recent_accepted=deque(self._recent_accepted),
            )

    def get_false_positive_rate_per_8h(self) -> float:
        """
        Calculate false positive rate extrapolated to 8-hour window.

        A false positive is defined as a raw trigger that was NOT followed
        by an accepted wake within 5 seconds.

        PRD target: <1 false positive per 8 hours of ambient audio.

        Returns:
            Estimated FPs per 8 hours based on recent hour's data.
        """
        now = time.time()
        hour_ago = now - 3600

        with self._lock:
            # Get triggers from the last hour
            recent_triggers = [t for t in self._recent_triggers if t > hour_ago]
            recent_accepted = list(self._recent_accepted)

        if not recent_triggers:
            return 0.0

        # Count false positives: triggers not followed by acceptance within 5s
        false_positives = 0
        for trigger_time in recent_triggers:
            if self._is_false_positive(trigger_time, recent_accepted):
                false_positives += 1

        # Extrapolate to 8 hours
        return false_positives * 8.0

    def _is_false_positive(self, trigger_time: float, accepted_times: list[float]) -> bool:
        """
        Determine if a trigger was a false positive.

        A trigger is a false positive if no accepted wake occurred
        within 5 seconds after it.

        Args:
            trigger_time: Timestamp of the raw trigger
            accepted_times: List of accepted wake timestamps

        Returns:
            True if the trigger was a false positive
        """
        for accepted_time in accepted_times:
            # Check if acceptance occurred within 5 seconds after trigger
            if 0 < (accepted_time - trigger_time) < 5.0:
                return False
        return True

    def is_sla_compliant(self) -> bool:
        """
        Check if current false positive rate meets SLA target.

        PRD target: <1 FP per 8 hours.

        Returns:
            True if SLA is met
        """
        return self.get_false_positive_rate_per_8h() < 1.0

    def get_metrics_dict(self) -> dict[str, Any]:
        """Get metrics as a dictionary for JSON serialization."""
        snapshot = self.get_snapshot()
        fp_rate_8h = self.get_false_positive_rate_per_8h()
        with self._lock:
            false_positives_marked = self._false_positives_marked
        return {
            "raw_wake_triggers": snapshot.raw_wake_triggers,
            "accepted_wakes": snapshot.accepted_wakes,
            "rejected_wakes": {
                "cooldown": snapshot.rejected_wakes_cooldown,
                "already_listening": snapshot.rejected_wakes_already_listening,
                "other": snapshot.rejected_wakes_other,
                "total": (
                    snapshot.rejected_wakes_cooldown
                    + snapshot.rejected_wakes_already_listening
                    + snapshot.rejected_wakes_other
                ),
            },
            "false_positives_marked": false_positives_marked,
            "false_positive_rate_per_8h": round(fp_rate_8h, 2),
            "sla_compliant": fp_rate_8h < 1.0,
            "rates": {
                "raw_triggers_per_minute": round(snapshot.raw_triggers_per_minute, 2),
                "accepted_wakes_per_minute": round(snapshot.accepted_wakes_per_minute, 2),
            },
            "timestamps": {
                "first_trigger": snapshot.first_trigger_time,
                "last_trigger": snapshot.last_trigger_time,
                "last_accepted": snapshot.last_accepted_time,
            },
            "acceptance_ratio": (
                round(snapshot.accepted_wakes / snapshot.raw_wake_triggers, 3)
                if snapshot.raw_wake_triggers > 0
                else 0.0
            ),
        }

    def reset(self) -> None:
        """Reset all counters (for testing or manual reset)."""
        with self._lock:
            self._raw_wake_triggers = 0
            self._accepted_wakes = 0
            self._rejected_wakes_cooldown = 0
            self._rejected_wakes_already_listening = 0
            self._rejected_wakes_other = 0
            self._false_positives_marked = 0
            self._first_trigger_time = None
            self._last_trigger_time = None
            self._last_accepted_time = None
            self._recent_triggers.clear()
            self._recent_accepted.clear()
        logger.info("Wake word metrics reset")


# Singleton instance
_wake_metrics: WakeWordMetrics | None = None
_wake_metrics_lock = threading.Lock()


def get_wake_metrics() -> WakeWordMetrics:
    """Get the singleton wake word metrics instance."""
    global _wake_metrics
    if _wake_metrics is None:
        with _wake_metrics_lock:
            if _wake_metrics is None:
                _wake_metrics = WakeWordMetrics()
    return _wake_metrics


@dataclass
class WakeMetricsResult:
    """Result object returned by WakeMetricsLogger.get_metrics()."""

    total_wakes: int = 0
    false_positives: int = 0
    avg_confidence: float | None = None  # None indicates confidence tracking not implemented


class WakeMetricsLogger:
    """
    Logger wrapper for wake word metrics.

    Provides a simple interface for the voice orchestrator metrics system.
    """

    def __init__(self) -> None:
        self._wake_metrics = get_wake_metrics()

    def get_metrics(self) -> WakeMetricsResult:
        """Get metrics in the format expected by voice orchestrator."""
        snapshot = self._wake_metrics.get_snapshot()

        # Calculate false positives as rejected wakes
        false_positives = (
            snapshot.rejected_wakes_cooldown + snapshot.rejected_wakes_already_listening + snapshot.rejected_wakes_other
        )

        # Get average confidence from tracked confidence scores
        avg_confidence = self._wake_metrics.get_avg_confidence()

        return WakeMetricsResult(
            total_wakes=snapshot.accepted_wakes,
            false_positives=false_positives,
            avg_confidence=avg_confidence,
        )


__all__ = [
    "WakeMetricsLogger",
    "WakeMetricsResult",
    "WakeMetricsSnapshot",
    "WakeWordMetrics",
    "get_wake_metrics",
]
