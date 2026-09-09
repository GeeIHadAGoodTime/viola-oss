"""
Wake Word Analytics and Correlations
=====================================

Provides time-bucketed statistics and trigger correlation analysis
for wake word detection patterns.

Features:
- Time-bucketed statistics (1min, 5min, 15min, 1h, 8h windows)
- Trigger correlations (playback, volume, time of day)
- Layer effectiveness analysis
- False positive estimation

Usage:
    from diagnostics.wake_analytics import get_wake_analytics

    analytics = get_wake_analytics()

    # Record a trigger event
    analytics.record_trigger(
        outcome="approved",
        score=0.85,
        playback_active=True,
        volume=80,
        blocking_layer=None,
    )

    # Get time-bucketed stats
    stats = analytics.get_bucketed_stats("5min")

    # Get trigger correlations
    correlations = analytics.get_correlations()

    # Get layer effectiveness
    layer_stats = analytics.get_layer_statistics()
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


class TriggerOutcome(Enum):
    """Outcome of a wake word trigger."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    BELOW_THRESHOLD = "below_threshold"


@dataclass
class TriggerRecord:
    """Record of a single trigger event."""

    timestamp: float
    outcome: TriggerOutcome
    score: float
    threshold: float

    # Context
    playback_active: bool = False
    volume: int = 0
    tts_active: bool = False

    # Layer info
    blocking_layer: str | None = None
    layers_passed: list[str] = field(default_factory=list)

    # Audio metrics
    loopback_rms: float = 0.0
    reduction_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "outcome": self.outcome.value,
            "score": round(self.score, 4),
            "threshold": round(self.threshold, 4),
            "playback_active": self.playback_active,
            "volume": self.volume,
            "tts_active": self.tts_active,
            "blocking_layer": self.blocking_layer,
            "layers_passed": self.layers_passed,
            "loopback_rms": round(self.loopback_rms, 1),
            "reduction_ratio": round(self.reduction_ratio, 3),
        }


@dataclass
class TimeBucketedStats:
    """Statistics aggregated over a time bucket."""

    bucket_name: str  # "1min", "5min", "15min", "1h", "8h"
    bucket_duration_seconds: float
    start_timestamp: float = 0.0
    end_timestamp: float = 0.0

    # Counts
    triggers_total: int = 0
    triggers_approved: int = 0
    triggers_blocked: int = 0
    triggers_below_threshold: int = 0

    # Rates
    triggers_per_minute: float = 0.0
    approval_rate: float = 0.0

    # Score statistics
    avg_score: float = 0.0
    max_score: float = 0.0
    min_score: float = 0.0

    # Context breakdown
    playback_active_percent: float = 0.0
    avg_reduction_ratio: float = 0.0

    # Layer block counts
    layer_block_counts: dict[str, int] = field(default_factory=dict)

    # False positive estimation
    false_positive_estimate: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "bucket_name": self.bucket_name,
            "bucket_duration_seconds": self.bucket_duration_seconds,
            "time_window": {
                "start": self.start_timestamp,
                "end": self.end_timestamp,
            },
            "counts": {
                "total": self.triggers_total,
                "approved": self.triggers_approved,
                "blocked": self.triggers_blocked,
                "below_threshold": self.triggers_below_threshold,
            },
            "rates": {
                "triggers_per_minute": round(self.triggers_per_minute, 2),
                "approval_rate": round(self.approval_rate, 3),
            },
            "score_stats": {
                "avg": round(self.avg_score, 4),
                "max": round(self.max_score, 4),
                "min": round(self.min_score, 4),
            },
            "context": {
                "playback_active_percent": round(self.playback_active_percent, 1),
                "avg_reduction_ratio": round(self.avg_reduction_ratio, 3),
            },
            "layer_block_counts": self.layer_block_counts,
            "false_positive_estimate": self.false_positive_estimate,
        }


@dataclass
class TriggerCorrelations:
    """Correlation analysis between triggers and various conditions."""

    # Playback correlation
    triggers_during_playback: int = 0
    triggers_during_silence: int = 0

    # Volume correlation
    triggers_by_volume_category: dict[str, int] = field(default_factory=dict)

    # Time of day correlation
    triggers_by_hour: dict[int, int] = field(default_factory=dict)

    # TTS correlation
    triggers_during_tts: int = 0
    triggers_shortly_after_tts: int = 0

    # Content type correlation (if available)
    triggers_by_content_type: dict[str, int] = field(default_factory=dict)

    # Score margin correlation
    near_miss_count: int = 0  # Score within 0.05 of threshold
    clear_pass_count: int = 0  # Score >0.2 above threshold

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        total = self.triggers_during_playback + self.triggers_during_silence
        playback_rate = self.triggers_during_playback / total if total > 0 else 0

        return {
            "playback": {
                "during_playback": self.triggers_during_playback,
                "during_silence": self.triggers_during_silence,
                "playback_trigger_rate": round(playback_rate, 3),
            },
            "volume": self.triggers_by_volume_category,
            "time_of_day": self.triggers_by_hour,
            "tts": {
                "during_tts": self.triggers_during_tts,
                "shortly_after_tts": self.triggers_shortly_after_tts,
            },
            "content_type": self.triggers_by_content_type,
            "score_margin": {
                "near_miss_count": self.near_miss_count,
                "clear_pass_count": self.clear_pass_count,
            },
        }


@dataclass
class LayerStatistics:
    """Per-layer effectiveness statistics."""

    layer_name: str

    # Counts
    evaluations: int = 0
    blocks: int = 0
    passes: int = 0

    # Rates
    block_rate: float = 0.0

    # Margin analysis
    avg_margin_when_blocked: float = 0.0
    avg_margin_when_passed: float = 0.0

    # Missed blocks estimate (triggers blocked by later layer that this could have blocked)
    missed_blocks: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "layer_name": self.layer_name,
            "evaluations": self.evaluations,
            "blocks": self.blocks,
            "passes": self.passes,
            "block_rate": round(self.block_rate, 3),
            "avg_margin_when_blocked": round(self.avg_margin_when_blocked, 4),
            "avg_margin_when_passed": round(self.avg_margin_when_passed, 4),
            "missed_blocks": self.missed_blocks,
        }


# --------------------------------------------------------------------------- #
# Time Bucket Definitions                                                       #
# --------------------------------------------------------------------------- #


TIME_BUCKETS = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "1h": 3600,
    "8h": 28800,
}


# --------------------------------------------------------------------------- #
# Wake Analytics                                                                #
# --------------------------------------------------------------------------- #


class WakeAnalytics:
    """
    Analytics engine for wake word detection patterns.

    Aggregates trigger data into time buckets and computes
    correlations between triggers and various conditions.

    Thread-safe for concurrent access.
    """

    # Configuration
    MAX_RECORDS = 5000  # ~8 hours at 10 triggers/minute
    VOLUME_CATEGORIES = ["silent", "quiet", "normal", "loud"]

    def __init__(self) -> None:
        """Initialize wake analytics."""
        self._lock = threading.RLock()

        # Trigger records
        self._records: deque[TriggerRecord] = deque(maxlen=self.MAX_RECORDS)

        # Layer tracking
        self._layer_margins: dict[str, list[float]] = {}  # layer -> list of margins
        self._layer_outcomes: dict[str, list[bool]] = {}  # layer -> list of (passed)

        # Startup time for rate calculations
        self._start_time = time.time()

        logger.info("WakeAnalytics initialized")

    def record_trigger(
        self,
        outcome: str,
        score: float,
        threshold: float,
        playback_active: bool = False,
        volume: int = 0,
        tts_active: bool = False,
        blocking_layer: str | None = None,
        layers_passed: list[str] | None = None,
        loopback_rms: float = 0.0,
        reduction_ratio: float = 0.0,
        timestamp: float | None = None,
    ) -> None:
        """
        Record a trigger event for analytics.

        Args:
            outcome: Trigger outcome ("approved", "blocked", "below_threshold")
            score: Detection score
            threshold: Threshold used
            playback_active: Whether playback was active
            volume: Playback volume (0-100)
            tts_active: Whether TTS was active
            blocking_layer: Layer that blocked (if blocked)
            layers_passed: Layers that passed before blocking
            loopback_rms: Loopback RMS at trigger time
            reduction_ratio: AEC reduction ratio at trigger time
            timestamp: Trigger timestamp
        """
        ts = timestamp or time.time()

        try:
            outcome_enum = TriggerOutcome(outcome)
        except ValueError:
            outcome_enum = TriggerOutcome.BELOW_THRESHOLD

        record = TriggerRecord(
            timestamp=ts,
            outcome=outcome_enum,
            score=score,
            threshold=threshold,
            playback_active=playback_active,
            volume=volume,
            tts_active=tts_active,
            blocking_layer=blocking_layer,
            layers_passed=layers_passed or [],
            loopback_rms=loopback_rms,
            reduction_ratio=reduction_ratio,
        )

        with self._lock:
            self._records.append(record)

    def record_layer_evaluation(
        self,
        layer_name: str,
        passed: bool,
        margin: float | None = None,
    ) -> None:
        """
        Record a layer evaluation for statistics.

        Args:
            layer_name: Name of the layer
            passed: Whether the layer passed
            margin: Decision margin (score - threshold, or confidence - threshold)
        """
        with self._lock:
            if layer_name not in self._layer_outcomes:
                self._layer_outcomes[layer_name] = []
                self._layer_margins[layer_name] = []

            self._layer_outcomes[layer_name].append(passed)
            if margin is not None:
                self._layer_margins[layer_name].append(margin)

    def get_bucketed_stats(self, bucket: str = "5min") -> TimeBucketedStats:
        """
        Get time-bucketed statistics.

        Args:
            bucket: Time bucket ("1min", "5min", "15min", "1h", "8h")

        Returns:
            Statistics for the specified time bucket
        """
        duration = TIME_BUCKETS.get(bucket, 300)
        now = time.time()
        cutoff = now - duration

        with self._lock:
            records = [r for r in self._records if r.timestamp > cutoff]

        if not records:
            return TimeBucketedStats(bucket_name=bucket, bucket_duration_seconds=duration)

        # Calculate statistics
        stats = TimeBucketedStats(
            bucket_name=bucket,
            bucket_duration_seconds=duration,
            start_timestamp=records[0].timestamp,
            end_timestamp=records[-1].timestamp,
            triggers_total=len(records),
        )

        # Outcome counts
        for r in records:
            if r.outcome == TriggerOutcome.APPROVED:
                stats.triggers_approved += 1
            elif r.outcome == TriggerOutcome.BLOCKED:
                stats.triggers_blocked += 1
            else:
                stats.triggers_below_threshold += 1

        # Rates
        actual_duration = min(duration, now - self._start_time)
        if actual_duration > 0:
            stats.triggers_per_minute = stats.triggers_total / (actual_duration / 60)
        if stats.triggers_total > 0:
            stats.approval_rate = stats.triggers_approved / stats.triggers_total

        # Score statistics
        scores = [r.score for r in records]
        stats.avg_score = sum(scores) / len(scores) if scores else 0
        stats.max_score = max(scores) if scores else 0
        stats.min_score = min(scores) if scores else 0

        # Context breakdown
        playback_count = sum(1 for r in records if r.playback_active)
        stats.playback_active_percent = (playback_count / len(records) * 100) if records else 0

        reduction_ratios = [r.reduction_ratio for r in records if r.reduction_ratio > 0]
        stats.avg_reduction_ratio = sum(reduction_ratios) / len(reduction_ratios) if reduction_ratios else 0

        # Layer block counts
        for r in records:
            if r.blocking_layer:
                stats.layer_block_counts[r.blocking_layer] = stats.layer_block_counts.get(r.blocking_layer, 0) + 1

        # False positive estimation (approved triggers during playback that might be echo)
        stats.false_positive_estimate = sum(
            1 for r in records if r.outcome == TriggerOutcome.APPROVED and r.playback_active and r.reduction_ratio < 0.3
        )

        return stats

    def get_all_bucketed_stats(self) -> dict[str, TimeBucketedStats]:
        """Get statistics for all time buckets."""
        return {bucket: self.get_bucketed_stats(bucket) for bucket in TIME_BUCKETS.keys()}

    def get_correlations(self, window_seconds: float = 3600.0) -> TriggerCorrelations:
        """
        Get trigger correlation analysis.

        Args:
            window_seconds: Time window to analyze

        Returns:
            Correlation analysis
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            records = [r for r in self._records if r.timestamp > cutoff and r.outcome == TriggerOutcome.APPROVED]

        correlations = TriggerCorrelations()

        # Initialize volume categories
        for cat in self.VOLUME_CATEGORIES:
            correlations.triggers_by_volume_category[cat] = 0

        # Initialize hours
        for hour in range(24):
            correlations.triggers_by_hour[hour] = 0

        for r in records:
            # Playback correlation
            if r.playback_active:
                correlations.triggers_during_playback += 1
            else:
                correlations.triggers_during_silence += 1

            # Volume correlation
            if r.volume == 0:
                correlations.triggers_by_volume_category["silent"] += 1
            elif r.volume < 30:
                correlations.triggers_by_volume_category["quiet"] += 1
            elif r.volume < 70:
                correlations.triggers_by_volume_category["normal"] += 1
            else:
                correlations.triggers_by_volume_category["loud"] += 1

            # Time of day correlation
            dt = datetime.fromtimestamp(r.timestamp)
            correlations.triggers_by_hour[dt.hour] += 1

            # TTS correlation
            if r.tts_active:
                correlations.triggers_during_tts += 1

            # Score margin analysis
            margin = r.score - r.threshold
            if 0 <= margin < 0.05:
                correlations.near_miss_count += 1
            elif margin >= 0.2:
                correlations.clear_pass_count += 1

        return correlations

    def get_layer_statistics(self) -> dict[str, LayerStatistics]:
        """Get per-layer effectiveness statistics."""
        with self._lock:
            stats = {}

            for layer_name in self._layer_outcomes.keys():
                outcomes = self._layer_outcomes.get(layer_name, [])
                margins = self._layer_margins.get(layer_name, [])

                if not outcomes:
                    continue

                layer_stats = LayerStatistics(
                    layer_name=layer_name,
                    evaluations=len(outcomes),
                    passes=sum(1 for o in outcomes if o),
                    blocks=sum(1 for o in outcomes if not o),
                )

                if layer_stats.evaluations > 0:
                    layer_stats.block_rate = layer_stats.blocks / layer_stats.evaluations

                # Margin analysis
                if margins:
                    blocked_margins = [m for m, o in zip(margins, outcomes) if not o]
                    passed_margins = [m for m, o in zip(margins, outcomes) if o]

                    if blocked_margins:
                        layer_stats.avg_margin_when_blocked = sum(blocked_margins) / len(blocked_margins)
                    if passed_margins:
                        layer_stats.avg_margin_when_passed = sum(passed_margins) / len(passed_margins)

                stats[layer_name] = layer_stats

            return stats

    def get_false_positive_rate_estimate(self, window_hours: float = 8.0) -> float:
        """
        Estimate false positive rate per 8 hours.

        Uses heuristics to estimate FP rate:
        - Approved triggers during playback with low VAD confidence
        - Approved triggers shortly after TTS

        Args:
            window_hours: Window to analyze

        Returns:
            Estimated false positives per 8 hours
        """
        window_seconds = window_hours * 3600
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            records = [r for r in self._records if r.timestamp > cutoff and r.outcome == TriggerOutcome.APPROVED]

        if not records:
            return 0.0

        # Count likely false positives
        fp_count = 0
        for r in records:
            is_fp = False

            # High playback, low reduction = likely echo
            if r.playback_active and r.reduction_ratio < 0.2:
                is_fp = True

            # During TTS = definitely echo
            if r.tts_active:
                is_fp = True

            if is_fp:
                fp_count += 1

        # Extrapolate to 8 hours
        actual_hours = min(window_hours, (now - self._start_time) / 3600)
        if actual_hours > 0:
            fp_per_hour = fp_count / actual_hours
            return fp_per_hour * 8.0

        return 0.0

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics summary for API exposure."""
        return {
            "total_records": len(self._records),
            "max_records": self.MAX_RECORDS,
            "uptime_hours": (time.time() - self._start_time) / 3600,
            "bucketed_stats": {name: stats.to_dict() for name, stats in self.get_all_bucketed_stats().items()},
            "correlations": self.get_correlations().to_dict(),
            "layer_statistics": {name: stats.to_dict() for name, stats in self.get_layer_statistics().items()},
            "false_positive_rate_8h": round(self.get_false_positive_rate_estimate(), 2),
        }

    def reset(self) -> None:
        """Reset all analytics data."""
        with self._lock:
            self._records.clear()
            self._layer_margins.clear()
            self._layer_outcomes.clear()
            self._start_time = time.time()

        logger.info("WakeAnalytics reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_analytics: WakeAnalytics | None = None
_analytics_lock = threading.Lock()


def get_wake_analytics() -> WakeAnalytics:
    """
    Get the global wake analytics instance.

    Returns:
        Global WakeAnalytics instance
    """
    global _analytics
    with _analytics_lock:
        if _analytics is None:
            _analytics = WakeAnalytics()
        return _analytics


def reset_wake_analytics() -> None:
    """Reset the global wake analytics (for testing)."""
    global _analytics
    with _analytics_lock:
        if _analytics is not None:
            _analytics.reset()


__all__ = [
    "LayerStatistics",
    "TimeBucketedStats",
    "TriggerCorrelations",
    "TriggerOutcome",
    "TriggerRecord",
    "WakeAnalytics",
    "get_wake_analytics",
    "reset_wake_analytics",
]
