"""
Defense Metrics Collector
=========================

Collects and reports metrics on wake word defense layer effectiveness.

This module tracks:
- Per-layer block/pass counts (VAD, echo, score, confirmation)
- Aggregate acceptance rates
- Health indicators for defense wiring

Usage:
    from diagnostics.defense_metrics import get_defense_metrics
    from core.logging_config import get_logger

    metrics = get_defense_metrics()
    metrics.record_raw_trigger()
    metrics.record_layer_result("vad_gate", passed=False, reason="Low confidence")

    snapshot = metrics.get_snapshot()
    logger = get_logger(__name__)
    logger.info("VAD blocked: %s", snapshot.vad_gate.blocked_count)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DefenseLayerStats:
    """Statistics for a single defense layer."""

    name: str
    blocked_count: int = 0
    passed_count: int = 0
    last_blocked_time: float | None = None
    last_blocked_reason: str | None = None

    @property
    def total_evaluations(self) -> int:
        return self.blocked_count + self.passed_count

    @property
    def block_rate(self) -> float:
        if self.total_evaluations == 0:
            return 0.0
        return self.blocked_count / self.total_evaluations


@dataclass
class DefenseMetricsSnapshot:
    """Point-in-time snapshot of all defense metrics."""

    timestamp: float

    # Per-layer stats
    vad_gate: DefenseLayerStats
    score_check: DefenseLayerStats
    echo_veto: DefenseLayerStats
    confirmation: DefenseLayerStats

    # Aggregate stats
    total_raw_triggers: int = 0
    total_accepted: int = 0
    total_blocked: int = 0

    # Health indicators
    aec_reference_available: bool = False
    playback_state_synced: bool = False
    vad_backend: str = "heuristic"

    @property
    def acceptance_rate(self) -> float:
        if self.total_raw_triggers == 0:
            return 0.0
        return self.total_accepted / self.total_raw_triggers

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "layers": {
                "vad_gate": {
                    "blocked": self.vad_gate.blocked_count,
                    "passed": self.vad_gate.passed_count,
                    "block_rate": round(self.vad_gate.block_rate, 3),
                    "last_blocked_reason": self.vad_gate.last_blocked_reason,
                },
                "score_check": {
                    "blocked": self.score_check.blocked_count,
                    "passed": self.score_check.passed_count,
                    "block_rate": round(self.score_check.block_rate, 3),
                    "last_blocked_reason": self.score_check.last_blocked_reason,
                },
                "echo_veto": {
                    "blocked": self.echo_veto.blocked_count,
                    "passed": self.echo_veto.passed_count,
                    "block_rate": round(self.echo_veto.block_rate, 3),
                    "last_blocked_reason": self.echo_veto.last_blocked_reason,
                },
                "confirmation": {
                    "blocked": self.confirmation.blocked_count,
                    "passed": self.confirmation.passed_count,
                    "block_rate": round(self.confirmation.block_rate, 3),
                    "last_blocked_reason": self.confirmation.last_blocked_reason,
                },
            },
            "aggregate": {
                "raw_triggers": self.total_raw_triggers,
                "accepted": self.total_accepted,
                "blocked": self.total_blocked,
                "acceptance_rate": round(self.acceptance_rate, 3),
            },
            "health": {
                "aec_reference_available": self.aec_reference_available,
                "playback_state_synced": self.playback_state_synced,
                "vad_backend": self.vad_backend,
            },
        }


class DefenseMetricsCollector:
    """
    Collects and reports defense layer effectiveness metrics.

    Thread-safe collector that tracks:
    - Per-layer block/pass counts
    - Aggregate acceptance rates
    - Health indicators for defense wiring

    USAGE PATTERN:
    ==============
    The policy calls these methods at each decision point:

    1. record_raw_trigger() - called when wake model fires
    2. record_layer_result() - called for each layer evaluation
    3. record_accepted() - called when all layers pass
    """

    SUMMARY_INTERVAL_SECONDS = 300  # 5 minutes

    def __init__(self) -> None:
        self._lock = threading.RLock()

        # Per-layer tracking
        self._vad_gate = DefenseLayerStats(name="vad_gate")
        self._score_check = DefenseLayerStats(name="score_check")
        self._echo_veto = DefenseLayerStats(name="echo_veto")
        self._confirmation = DefenseLayerStats(name="confirmation")
        self._baseline_vad = DefenseLayerStats(name="baseline_vad")
        self._listening_gate = DefenseLayerStats(name="listening_gate")

        # Aggregate tracking
        self._total_raw_triggers = 0
        self._total_accepted = 0

        # Health tracking
        self._aec_reference_available = False
        self._playback_state_synced = False
        self._vad_backend = "heuristic"

        # Periodic summary
        self._last_summary_time = time.time()
        self._summary_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def record_layer_result(
        self,
        layer: str,
        passed: bool,
        reason: str | None = None,
    ) -> None:
        """
        Record result for a defense layer evaluation.

        Args:
            layer: Layer name ("vad_gate", "score_check", "echo_veto", "confirmation")
            passed: Whether the layer passed (True) or blocked (False)
            reason: Optional blocking reason (for diagnostics)
        """
        with self._lock:
            stats = self._get_layer_stats(layer)
            if stats is None:
                logger.warning("Unknown defense layer: %s", layer)
                return

            if passed:
                stats.passed_count += 1
            else:
                stats.blocked_count += 1
                stats.last_blocked_time = time.time()
                stats.last_blocked_reason = reason

    def record_raw_trigger(self) -> None:
        """Record a raw wake word trigger (model fired)."""
        with self._lock:
            self._total_raw_triggers += 1

    def record_accepted(self) -> None:
        """Record an accepted wake (all layers passed)."""
        with self._lock:
            self._total_accepted += 1

    def update_health(
        self,
        aec_available: bool | None = None,
        playback_synced: bool | None = None,
        vad_backend: str | None = None,
    ) -> None:
        """
        Update health indicators.

        Args:
            aec_available: Whether AEC reference is available
            playback_synced: Whether playback state is synchronized
            vad_backend: Name of VAD backend in use
        """
        with self._lock:
            if aec_available is not None:
                self._aec_reference_available = aec_available
            if playback_synced is not None:
                self._playback_state_synced = playback_synced
            if vad_backend is not None:
                self._vad_backend = vad_backend

    def get_snapshot(self) -> DefenseMetricsSnapshot:
        """Get current metrics snapshot."""
        with self._lock:
            return DefenseMetricsSnapshot(
                timestamp=time.time(),
                vad_gate=DefenseLayerStats(
                    name="vad_gate",
                    blocked_count=self._vad_gate.blocked_count,
                    passed_count=self._vad_gate.passed_count,
                    last_blocked_time=self._vad_gate.last_blocked_time,
                    last_blocked_reason=self._vad_gate.last_blocked_reason,
                ),
                score_check=DefenseLayerStats(
                    name="score_check",
                    blocked_count=self._score_check.blocked_count,
                    passed_count=self._score_check.passed_count,
                    last_blocked_time=self._score_check.last_blocked_time,
                    last_blocked_reason=self._score_check.last_blocked_reason,
                ),
                echo_veto=DefenseLayerStats(
                    name="echo_veto",
                    blocked_count=self._echo_veto.blocked_count,
                    passed_count=self._echo_veto.passed_count,
                    last_blocked_time=self._echo_veto.last_blocked_time,
                    last_blocked_reason=self._echo_veto.last_blocked_reason,
                ),
                confirmation=DefenseLayerStats(
                    name="confirmation",
                    blocked_count=self._confirmation.blocked_count,
                    passed_count=self._confirmation.passed_count,
                    last_blocked_time=self._confirmation.last_blocked_time,
                    last_blocked_reason=self._confirmation.last_blocked_reason,
                ),
                total_raw_triggers=self._total_raw_triggers,
                total_accepted=self._total_accepted,
                total_blocked=self._total_raw_triggers - self._total_accepted,
                aec_reference_available=self._aec_reference_available,
                playback_state_synced=self._playback_state_synced,
                vad_backend=self._vad_backend,
            )

    def log_summary(self) -> None:
        """Log periodic summary of defense effectiveness."""
        snapshot = self.get_snapshot()

        logger.info(
            "[Wake Defense Summary] "
            "Raw triggers: %d | Accepted: %d (%.1f%%) | "
            "Blocked by: VAD=%d, Score=%d, Echo=%d, Confirm=%d",
            snapshot.total_raw_triggers,
            snapshot.total_accepted,
            snapshot.acceptance_rate * 100,
            snapshot.vad_gate.blocked_count,
            snapshot.score_check.blocked_count,
            snapshot.echo_veto.blocked_count,
            snapshot.confirmation.blocked_count,
        )

        # Log health warnings
        if not snapshot.aec_reference_available:
            logger.warning("[Wake Defense] AEC reference UNAVAILABLE - echo cancellation disabled")
        if not snapshot.playback_state_synced:
            logger.warning("[Wake Defense] Playback state NOT SYNCED - using RMS-only detection")

    def start_periodic_summary(self) -> None:
        """Start background thread for periodic summaries."""
        if self._summary_thread is not None:
            return

        self._stop_event.clear()
        self._summary_thread = threading.Thread(
            target=self._summary_loop,
            name="defense-metrics-summary",
            daemon=True,
        )
        self._summary_thread.start()
        logger.info("Defense metrics periodic summary started")

    def stop_periodic_summary(self) -> None:
        """Stop periodic summary thread."""
        self._stop_event.set()
        if self._summary_thread:
            self._summary_thread.join(timeout=TIMEOUT_SHUTDOWN)
            self._summary_thread = None

    def _summary_loop(self) -> None:
        """Background loop for periodic summaries."""
        while not self._stop_event.wait(self.SUMMARY_INTERVAL_SECONDS):
            self.log_summary()

    def _get_layer_stats(self, layer: str) -> DefenseLayerStats | None:
        """Get stats object for a layer name."""
        return {
            "vad_gate": self._vad_gate,
            "score_check": self._score_check,
            "primary_score": self._score_check,  # Alias
            "echo_veto": self._echo_veto,
            "confirmation": self._confirmation,
            "baseline_vad": self._baseline_vad,
            "listening_gate": self._listening_gate,
        }.get(layer)

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        with self._lock:
            for stats in [
                self._vad_gate,
                self._score_check,
                self._echo_veto,
                self._confirmation,
                self._baseline_vad,
                self._listening_gate,
            ]:
                stats.blocked_count = 0
                stats.passed_count = 0
                stats.last_blocked_time = None
                stats.last_blocked_reason = None
            self._total_raw_triggers = 0
            self._total_accepted = 0


# Singleton
_collector: DefenseMetricsCollector | None = None
_collector_lock = threading.Lock()


def get_defense_metrics() -> DefenseMetricsCollector:
    """Get global defense metrics collector."""
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = DefenseMetricsCollector()
        return _collector


def reset_defense_metrics() -> None:
    """Reset global defense metrics collector (for testing)."""
    global _collector
    with _collector_lock:
        if _collector is not None:
            _collector.reset()


__all__ = [
    "DefenseLayerStats",
    "DefenseMetricsCollector",
    "DefenseMetricsSnapshot",
    "get_defense_metrics",
    "reset_defense_metrics",
]
