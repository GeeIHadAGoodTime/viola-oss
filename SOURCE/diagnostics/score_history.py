"""
Wake Word Score History and Distribution Analysis
=================================================

Tracks wake word detection scores over time for pattern analysis
and forensic debugging.

Provides:
- Ring buffer of recent scores with full context
- Distribution analysis (percentiles, histogram)
- Scores around specific timestamps for post-mortem
- Decision outcome tracking

Usage:
    from diagnostics.score_history import get_score_history

    history = get_score_history()

    # Record each score
    history.record_score(
        score=0.85,
        threshold=0.85,
        playback_active=True,
        decision="approved",
    )

    # Get distribution analysis
    dist = history.get_distribution()
    print(f"P95 score: {dist.p95}")

    # Get scores around a timestamp
    scores = history.get_scores_around(timestamp, window_ms=500)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


class ScoreDecision(Enum):
    """Outcome of score evaluation."""

    BELOW_THRESHOLD = "below_threshold"  # Score didn't meet threshold
    BLOCKED = "blocked"  # Score met threshold but blocked by policy
    APPROVED = "approved"  # Score met threshold and approved


@dataclass
class ScoreEntry:
    """Record of a single wake word score evaluation."""

    timestamp: float
    raw_score: float
    effective_threshold: float
    base_threshold: float

    # Context
    playback_active: bool = False
    playback_volume: int = 0
    echo_gating_active: bool = False
    vad_confidence: float = 0.0

    # Audio metrics at score time
    mic_rms: float = 0.0
    loopback_rms: float = 0.0
    post_aec_rms: float = 0.0
    correlation: float = 0.0

    # Decision
    decision: ScoreDecision = ScoreDecision.BELOW_THRESHOLD
    blocking_layer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "raw_score": round(self.raw_score, 4),
            "effective_threshold": round(self.effective_threshold, 4),
            "base_threshold": round(self.base_threshold, 4),
            "playback_active": self.playback_active,
            "playback_volume": self.playback_volume,
            "echo_gating_active": self.echo_gating_active,
            "vad_confidence": round(self.vad_confidence, 3),
            "mic_rms": round(self.mic_rms, 1),
            "loopback_rms": round(self.loopback_rms, 1),
            "post_aec_rms": round(self.post_aec_rms, 1),
            "correlation": round(self.correlation, 3),
            "decision": self.decision.value,
            "blocking_layer": self.blocking_layer,
            "score_margin": round(self.raw_score - self.effective_threshold, 4),
        }


@dataclass
class ScoreDistribution:
    """Statistical distribution of wake word scores."""

    # Basic statistics
    count: int = 0
    min: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    median: float = 0.0
    std: float = 0.0

    # Percentiles
    p10: float = 0.0
    p25: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0

    # Histogram buckets (0.0-0.1, 0.1-0.2, ..., 0.9-1.0)
    histogram: dict[str, int] = field(default_factory=dict)

    # Decision breakdown
    below_threshold_count: int = 0
    blocked_count: int = 0
    approved_count: int = 0

    # Time window
    start_timestamp: float = 0.0
    end_timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "count": self.count,
            "statistics": {
                "min": round(self.min, 4),
                "max": round(self.max, 4),
                "mean": round(self.mean, 4),
                "median": round(self.median, 4),
                "std": round(self.std, 4),
            },
            "percentiles": {
                "p10": round(self.p10, 4),
                "p25": round(self.p25, 4),
                "p50": round(self.p50, 4),
                "p75": round(self.p75, 4),
                "p90": round(self.p90, 4),
                "p95": round(self.p95, 4),
                "p99": round(self.p99, 4),
            },
            "histogram": self.histogram,
            "decisions": {
                "below_threshold": self.below_threshold_count,
                "blocked": self.blocked_count,
                "approved": self.approved_count,
            },
            "time_window": {
                "start": self.start_timestamp,
                "end": self.end_timestamp,
                "duration_seconds": (self.end_timestamp - self.start_timestamp if self.end_timestamp > 0 else 0),
            },
        }


@dataclass
class ScorePattern:
    """Detected pattern in score history."""

    pattern_type: str  # "spike", "sustained_high", "oscillation", "gradual_rise"
    start_timestamp: float
    end_timestamp: float
    peak_score: float
    avg_score: float
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pattern_type": self.pattern_type,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "duration_ms": (self.end_timestamp - self.start_timestamp) * 1000,
            "peak_score": round(self.peak_score, 4),
            "avg_score": round(self.avg_score, 4),
            "context": self.context,
        }


# --------------------------------------------------------------------------- #
# Score History Buffer                                                          #
# --------------------------------------------------------------------------- #


class ScoreHistory:
    """
    Ring buffer of wake word detection scores with analysis capabilities.

    Thread-safe for concurrent access from detection and diagnostic threads.
    """

    # Configuration
    DEFAULT_MAX_SIZE = 500  # ~50s at 10 scores/second
    HISTOGRAM_BUCKETS = [
        "0.0-0.1",
        "0.1-0.2",
        "0.2-0.3",
        "0.3-0.4",
        "0.4-0.5",
        "0.5-0.6",
        "0.6-0.7",
        "0.7-0.8",
        "0.8-0.9",
        "0.9-1.0",
    ]

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        """
        Initialize score history buffer.

        Args:
            max_size: Maximum number of scores to retain
        """
        self._lock = threading.RLock()
        self._buffer: deque[ScoreEntry] = deque(maxlen=max_size)
        self._max_size = max_size

        # Trigger history (only approved triggers)
        self._triggers: deque[ScoreEntry] = deque(maxlen=100)

        # Statistics tracking
        self._total_scores = 0
        self._total_below_threshold = 0
        self._total_blocked = 0
        self._total_approved = 0

        logger.info("ScoreHistory initialized: max_size=%d", max_size)

    def record_score(
        self,
        score: float,
        effective_threshold: float,
        base_threshold: float = 0.85,
        playback_active: bool = False,
        playback_volume: int = 0,
        echo_gating_active: bool = False,
        vad_confidence: float = 0.0,
        mic_rms: float = 0.0,
        loopback_rms: float = 0.0,
        post_aec_rms: float = 0.0,
        correlation: float = 0.0,
        decision: str = "below_threshold",
        blocking_layer: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """
        Record a wake word score evaluation.

        Args:
            score: Raw detection score (0.0-1.0)
            effective_threshold: Threshold used for this evaluation
            base_threshold: Base threshold before adjustments
            playback_active: Whether playback is active
            playback_volume: Current playback volume
            echo_gating_active: Whether echo gating is active
            vad_confidence: VAD confidence for this frame
            mic_rms: Microphone RMS
            loopback_rms: Playback loopback RMS
            post_aec_rms: Post-AEC RMS
            correlation: Mic-to-loopback correlation
            decision: Decision outcome ("below_threshold", "blocked", "approved")
            blocking_layer: Layer that blocked (if blocked)
            timestamp: Timestamp (uses current time if None)
        """
        ts = timestamp or time.time()

        # Parse decision
        try:
            decision_enum = ScoreDecision(decision)
        except ValueError:
            decision_enum = ScoreDecision.BELOW_THRESHOLD

        entry = ScoreEntry(
            timestamp=ts,
            raw_score=score,
            effective_threshold=effective_threshold,
            base_threshold=base_threshold,
            playback_active=playback_active,
            playback_volume=playback_volume,
            echo_gating_active=echo_gating_active,
            vad_confidence=vad_confidence,
            mic_rms=mic_rms,
            loopback_rms=loopback_rms,
            post_aec_rms=post_aec_rms,
            correlation=correlation,
            decision=decision_enum,
            blocking_layer=blocking_layer,
        )

        with self._lock:
            self._buffer.append(entry)
            self._total_scores += 1

            # Update decision counters
            if decision_enum == ScoreDecision.BELOW_THRESHOLD:
                self._total_below_threshold += 1
            elif decision_enum == ScoreDecision.BLOCKED:
                self._total_blocked += 1
            elif decision_enum == ScoreDecision.APPROVED:
                self._total_approved += 1
                self._triggers.append(entry)

    def get_recent(self, n: int = 50) -> list[ScoreEntry]:
        """
        Get the n most recent scores.

        Args:
            n: Number of scores to return

        Returns:
            List of ScoreEntry objects (newest last)
        """
        with self._lock:
            return list(self._buffer)[-n:]

    def get_recent_dicts(self, n: int = 50) -> list[dict[str, Any]]:
        """
        Get the n most recent scores as dictionaries.

        Args:
            n: Number of scores to return

        Returns:
            List of score dictionaries
        """
        return [entry.to_dict() for entry in self.get_recent(n)]

    def get_scores_around(
        self,
        timestamp: float,
        window_ms: float = 500.0,
    ) -> list[ScoreEntry]:
        """
        Get scores within a time window around a timestamp.

        Args:
            timestamp: Center timestamp
            window_ms: Window size in milliseconds (total, not half)

        Returns:
            List of ScoreEntry objects within the window
        """
        half_window = window_ms / 2000.0  # Convert to seconds

        with self._lock:
            return [
                entry for entry in self._buffer if timestamp - half_window <= entry.timestamp <= timestamp + half_window
            ]

    def get_scores_since(self, since: float) -> list[ScoreEntry]:
        """
        Get all scores since a timestamp.

        Args:
            since: Start timestamp

        Returns:
            List of ScoreEntry objects after the timestamp
        """
        with self._lock:
            return [entry for entry in self._buffer if entry.timestamp >= since]

    def get_distribution(self, window_seconds: float | None = None) -> ScoreDistribution:
        """
        Calculate score distribution statistics.

        Args:
            window_seconds: Time window to analyze (None = all data)

        Returns:
            ScoreDistribution with statistics
        """
        with self._lock:
            entries = list(self._buffer)

            if window_seconds is not None:
                cutoff = time.time() - window_seconds
                entries = [e for e in entries if e.timestamp > cutoff]

            if not entries:
                return ScoreDistribution()

            scores = np.array([e.raw_score for e in entries])

            # Calculate basic statistics
            dist = ScoreDistribution(
                count=len(scores),
                min=float(np.min(scores)),
                max=float(np.max(scores)),
                mean=float(np.mean(scores)),
                median=float(np.median(scores)),
                std=float(np.std(scores)),
                start_timestamp=entries[0].timestamp,
                end_timestamp=entries[-1].timestamp,
            )

            # Calculate percentiles
            dist.p10 = float(np.percentile(scores, 10))
            dist.p25 = float(np.percentile(scores, 25))
            dist.p50 = float(np.percentile(scores, 50))
            dist.p75 = float(np.percentile(scores, 75))
            dist.p90 = float(np.percentile(scores, 90))
            dist.p95 = float(np.percentile(scores, 95))
            dist.p99 = float(np.percentile(scores, 99))

            # Build histogram
            dist.histogram = {bucket: 0 for bucket in self.HISTOGRAM_BUCKETS}
            for score in scores:
                bucket_idx = min(int(score * 10), 9)
                bucket = self.HISTOGRAM_BUCKETS[bucket_idx]
                dist.histogram[bucket] += 1

            # Decision breakdown
            for entry in entries:
                if entry.decision == ScoreDecision.BELOW_THRESHOLD:
                    dist.below_threshold_count += 1
                elif entry.decision == ScoreDecision.BLOCKED:
                    dist.blocked_count += 1
                elif entry.decision == ScoreDecision.APPROVED:
                    dist.approved_count += 1

            return dist

    def get_triggers(self, last_n: int = 20) -> list[ScoreEntry]:
        """
        Get recent approved triggers.

        Args:
            last_n: Number of triggers to return

        Returns:
            List of approved trigger entries
        """
        with self._lock:
            return list(self._triggers)[-last_n:]

    def find_patterns(self, window_seconds: float = 60.0) -> list[ScorePattern]:
        """
        Find patterns in recent score history.

        Detects:
        - Spikes: Sudden high score
        - Sustained high: Multiple high scores in sequence
        - Oscillation: Scores bouncing around threshold

        Args:
            window_seconds: Time window to analyze

        Returns:
            List of detected patterns
        """
        patterns = []
        entries = self.get_scores_since(time.time() - window_seconds)

        if len(entries) < 5:
            return patterns

        scores = [e.raw_score for e in entries]
        thresholds = [e.effective_threshold for e in entries]

        # Detect spikes (score jumps >0.3 from baseline)
        baseline = np.percentile(scores, 25)
        for i, (entry, score) in enumerate(zip(entries, scores)):
            if score - baseline > 0.3:
                # Check if isolated spike or start of sustained high
                is_sustained = i + 3 < len(scores) and np.mean(scores[i : i + 3]) > baseline + 0.2

                pattern = ScorePattern(
                    pattern_type="sustained_high" if is_sustained else "spike",
                    start_timestamp=entry.timestamp,
                    end_timestamp=entry.timestamp + 0.1,
                    peak_score=score,
                    avg_score=score,
                    context={
                        "baseline": round(baseline, 4),
                        "playback_active": entry.playback_active,
                        "threshold": entry.effective_threshold,
                    },
                )
                patterns.append(pattern)

        # Detect oscillation (multiple threshold crossings)
        threshold_crossings = 0
        for i in range(1, len(scores)):
            above_curr = scores[i] >= thresholds[i]
            above_prev = scores[i - 1] >= thresholds[i - 1]
            if above_curr != above_prev:
                threshold_crossings += 1

        if threshold_crossings > 5:
            pattern = ScorePattern(
                pattern_type="oscillation",
                start_timestamp=entries[0].timestamp,
                end_timestamp=entries[-1].timestamp,
                peak_score=max(scores),
                avg_score=np.mean(scores),
                context={
                    "crossings": threshold_crossings,
                    "avg_threshold": round(np.mean(thresholds), 4),
                },
            )
            patterns.append(pattern)

        return patterns

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics summary for API exposure."""
        dist = self.get_distribution()
        patterns = self.find_patterns()

        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "max_size": self._max_size,
                "total_scores": self._total_scores,
                "total_triggers": len(self._triggers),
                "totals": {
                    "below_threshold": self._total_below_threshold,
                    "blocked": self._total_blocked,
                    "approved": self._total_approved,
                },
                "distribution": dist.to_dict(),
                "patterns": [p.to_dict() for p in patterns],
                "recent_scores": self.get_recent_dicts(10),
            }

    def reset(self) -> None:
        """Reset all score history."""
        with self._lock:
            self._buffer.clear()
            self._triggers.clear()
            self._total_scores = 0
            self._total_below_threshold = 0
            self._total_blocked = 0
            self._total_approved = 0

        logger.info("ScoreHistory reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_history: ScoreHistory | None = None
_history_lock = threading.Lock()


def get_score_history(max_size: int = ScoreHistory.DEFAULT_MAX_SIZE) -> ScoreHistory:
    """
    Get the global score history instance.

    Args:
        max_size: Maximum buffer size (only used on first call)

    Returns:
        Global ScoreHistory instance
    """
    global _history
    with _history_lock:
        if _history is None:
            _history = ScoreHistory(max_size=max_size)
        return _history


def reset_score_history() -> None:
    """Reset the global score history (for testing)."""
    global _history
    with _history_lock:
        if _history is not None:
            _history.reset()


__all__ = [
    "ScoreDecision",
    "ScoreDistribution",
    "ScoreEntry",
    "ScoreHistory",
    "ScorePattern",
    "get_score_history",
    "reset_score_history",
]
