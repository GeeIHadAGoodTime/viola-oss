"""
Defense Health Monitor (Phase 7 Enhanced)
==========================================

Validates that all wake word defense components are properly wired
and functioning. Runs checks on startup and periodically during operation.

PHASE 7 ENHANCEMENTS:
- Tiered check intervals (critical/normal/low priority)
- Anomaly detection for abnormal behavior patterns
- Latency tracking and alerting
- Component health scoring
- Historical trend analysis

This module detects silent failures such as:
- AEC reference not wired
- Playback state callbacks not firing
- Config values that don't make sense
- State disagreements between RMS and player state
- Abnormal detection patterns (too many/few triggers)
- Latency spikes
- Component degradation

Usage:
    from voice.wake_detector.defense_health_monitor import DefenseHealthMonitor

    monitor = DefenseHealthMonitor(policy)
    monitor.set_listener(listener)
    monitor.set_music_controller(music)
    monitor.start_monitoring()

    # Check health manually
    report = monitor.run_checks()
    report.log()

    # Get anomaly status
    anomalies = monitor.get_active_anomalies()
"""

from __future__ import annotations

import atexit
import importlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

import numpy as np

from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

if TYPE_CHECKING:
    from voice.wake_detector.wake_decision_policy import WakeDecisionPolicy

logger = get_logger(__name__)


class CheckPriority(Enum):
    """Priority levels for health checks."""

    CRITICAL = 1  # Check every 10 seconds
    NORMAL = 2  # Check every 60 seconds
    LOW = 3  # Check every 5 minutes


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    name: str
    passed: bool
    message: str
    severity: str = "warning"  # "info", "warning", "critical"

    def __str__(self) -> str:
        if self.passed:
            status = "PASS"
        elif self.severity == "critical":
            status = "CRIT"
        else:
            status = "WARN"
        return f"[{status}] {self.name}: {self.message}"


@dataclass
class HealthReport:
    """Complete health report for defense system."""

    timestamp: float
    checks: list[HealthCheckResult]
    overall_healthy: bool

    def to_dict(self) -> HealthReportDict:
        return {
            "timestamp": self.timestamp,
            "overall_healthy": self.overall_healthy,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "message": c.message,
                    "severity": c.severity,
                }
                for c in self.checks
            ],
        }

    def log(self) -> None:
        """Log the health report."""
        if self.overall_healthy:
            logger.info("[Defense Health] All checks passed")
        else:
            logger.warning("[Defense Health] Issues detected:")
            for check in self.checks:
                if not check.passed:
                    logger.warning("  %s", check)


@dataclass
class AnomalyEvent:
    """Detected anomaly in system behavior."""

    anomaly_type: str
    severity: str  # "warning", "critical"
    message: str
    timestamp: float
    value: float = 0.0
    threshold: float = 0.0

    def __str__(self) -> str:
        return f"[{self.severity}] {self.anomaly_type}: {self.message}"


class AnomalyDetector:
    """
    Detects abnormal behavior patterns in wake word detection.

    Monitors:
    - Detection rate (too many = false positives, too few = missed detections)
    - Latency spikes
    - Score distribution anomalies
    - Component failure patterns
    """

    def __init__(self, window_seconds: float = 300.0):
        """
        Initialize anomaly detector.

        Args:
            window_seconds: Analysis window in seconds
        """
        self._window_seconds = window_seconds

        # Tracking buffers
        self._detections: deque[tuple[float, float]] = deque(maxlen=1000)  # (timestamp, score)
        self._latencies: deque[tuple[float, float]] = deque(maxlen=1000)  # (timestamp, latency_ms)
        self._rejections: deque[tuple[float, str]] = deque(maxlen=500)  # (timestamp, reason)

        # Active anomalies
        self._active_anomalies: list[AnomalyEvent] = []

        # Thresholds
        self._max_detections_per_minute = 5.0  # More than this is suspicious
        self._max_rejections_per_minute = 10.0  # Sustained rejection spikes are suspicious
        self._max_latency_ms = 200.0  # Latency above this is concerning

    def record_detection(self, score: float, timestamp: float | None = None) -> None:
        """Record a wake word detection."""
        ts = timestamp or time.time()
        self._detections.append((ts, score))

    def record_latency(self, latency_ms: float, timestamp: float | None = None) -> None:
        """Record processing latency."""
        ts = timestamp or time.time()
        self._latencies.append((ts, latency_ms))

    def record_rejection(self, reason: str, timestamp: float | None = None) -> None:
        """Record a rejected detection."""
        ts = timestamp or time.time()
        self._rejections.append((ts, reason))

    def analyze(self) -> list[AnomalyEvent]:
        """
        Analyze recent data for anomalies.

        Returns:
            List of detected anomalies
        """
        now = time.time()
        anomalies = []

        # Get recent data
        recent_detections = [(ts, score) for ts, score in self._detections if now - ts <= self._window_seconds]

        recent_latencies = [(ts, lat) for ts, lat in self._latencies if now - ts <= self._window_seconds]
        recent_rejections = [(ts, reason) for ts, reason in self._rejections if now - ts <= self._window_seconds]

        # Check detection rate
        if recent_detections:
            rate_per_minute = len(recent_detections) / (self._window_seconds / 60)

            if rate_per_minute > self._max_detections_per_minute:
                anomalies.append(
                    AnomalyEvent(
                        anomaly_type="high_detection_rate",
                        severity="warning",
                        message=f"Detection rate {rate_per_minute:.1f}/min exceeds threshold",
                        timestamp=now,
                        value=rate_per_minute,
                        threshold=self._max_detections_per_minute,
                    )
                )

        # Check rejection rate
        if recent_rejections:
            rejection_rate_per_minute = len(recent_rejections) / (self._window_seconds / 60)

            if rejection_rate_per_minute > self._max_rejections_per_minute:
                anomalies.append(
                    AnomalyEvent(
                        anomaly_type="high_rejection_rate",
                        severity="warning",
                        message=(f"Rejection rate {rejection_rate_per_minute:.1f}/min " "exceeds threshold"),
                        timestamp=now,
                        value=rejection_rate_per_minute,
                        threshold=self._max_rejections_per_minute,
                    )
                )

        # Check latency
        if recent_latencies:
            latency_values = [lat for _, lat in recent_latencies]
            _avg_latency = float(np.mean(latency_values))
            max_latency = float(max(latency_values))
            p95_latency = float(np.percentile(latency_values, 95))

            if p95_latency > self._max_latency_ms:
                anomalies.append(
                    AnomalyEvent(
                        anomaly_type="high_latency",
                        severity="warning",
                        message=f"P95 latency {p95_latency:.0f}ms exceeds {self._max_latency_ms:.0f}ms",
                        timestamp=now,
                        value=p95_latency,
                        threshold=self._max_latency_ms,
                    )
                )

            if max_latency > self._max_latency_ms * 2:
                anomalies.append(
                    AnomalyEvent(
                        anomaly_type="latency_spike",
                        severity="critical",
                        message=f"Latency spike detected: {max_latency:.0f}ms",
                        timestamp=now,
                        value=max_latency,
                        threshold=self._max_latency_ms * 2,
                    )
                )

        # Check score distribution
        if len(recent_detections) >= 10:
            scores = [score for _, score in recent_detections]
            avg_score = float(np.mean(scores))

            # Unusually low scores might indicate model degradation
            if avg_score < 0.6:
                anomalies.append(
                    AnomalyEvent(
                        anomaly_type="low_score_average",
                        severity="warning",
                        message=f"Average detection score {avg_score:.2f} is low",
                        timestamp=now,
                        value=avg_score,
                        threshold=0.6,
                    )
                )

        self._active_anomalies = anomalies
        return anomalies

    def get_statistics(self) -> AnomalyStatistics:
        """Get current statistics."""
        now = time.time()

        recent_detections = [(ts, score) for ts, score in self._detections if now - ts <= 3600]  # Last hour

        recent_latencies = [lat for ts, lat in self._latencies if now - ts <= 300]  # Last 5 minutes

        return {
            "detections_last_hour": len(recent_detections),
            "avg_latency_ms": (float(np.mean(recent_latencies)) if recent_latencies else 0.0),
            "p95_latency_ms": (float(np.percentile(recent_latencies, 95)) if len(recent_latencies) >= 5 else 0.0),
            "active_anomalies": len(self._active_anomalies),
        }


HealthCheckResultDict: TypeAlias = dict[str, str | bool]
HealthReportDict: TypeAlias = dict[str, float | bool | list[HealthCheckResultDict]]
AnomalyStatistics: TypeAlias = dict[str, int | float]
DefenseStatistics: TypeAlias = dict[str, int | float | dict[str, float]]


class DefenseHealthMonitor:
    """
    Monitors health of wake word defense system.

    PHASE 7 ENHANCED CHECKS:
    1. AEC reference wiring - is echo cancellation active?
    2. Playback state wiring - is policy receiving player state?
    3. VAD availability - is real VAD available or falling back to heuristic?
    4. Policy consistency - are RMS and player state in agreement?
    5. Config validity - are threshold values sensible?
    6. Latency monitoring - are we within budget?
    7. Anomaly detection - unusual patterns?
    8. Component health - all components functioning?

    TIERED CHECK INTERVALS:
    - Critical: Every 10 seconds (AEC, latency)
    - Normal: Every 60 seconds (config, VAD)
    - Low: Every 5 minutes (trends, statistics)
    """

    # Tiered check intervals
    CRITICAL_INTERVAL_SECONDS = 10
    NORMAL_INTERVAL_SECONDS = 60
    LOW_INTERVAL_SECONDS = 300
    CHECK_INTERVAL_SECONDS = 60  # Default interval for basic monitoring loop

    def __init__(
        self,
        policy: WakeDecisionPolicy | None = None,
        listener: object | None = None,
        music: object | None = None,
    ) -> None:
        if policy is None:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            self._policy = get_wake_policy()
        else:
            self._policy = policy
        self._listener: object | None = None
        self._music: object | None = None
        self.set_listener(listener)
        self.set_music_controller(music)

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_report: HealthReport | None = None

        # Track playback state updates for sync check
        self._last_playback_update_time: float | None = None
        self._playback_update_count = 0

        # Phase 7: Anomaly detector
        self._anomaly_detector = AnomalyDetector()

        # Tiered check timing
        self._last_critical_check = 0.0
        self._last_normal_check = 0.0
        self._last_low_check = 0.0

        # Component health scores (0.0 - 1.0)
        self._component_health: dict[str, float] = {
            "aec": 1.0,
            "vad": 1.0,
            "policy": 1.0,
            "latency": 1.0,
        }

    def set_listener(self, listener: object | None) -> None:
        """Set reference to wake listener for AEC checks."""
        self._listener = listener

    def set_music_controller(self, music: object | None) -> None:
        """Set reference to music controller for state checks."""
        self._music = music

    def record_playback_update(self) -> None:
        """Record that a playback state update was received."""
        self._last_playback_update_time = time.time()
        self._playback_update_count += 1

    def run_checks(self) -> HealthReport:
        """Run all health checks and return report."""
        checks = [
            self._check_aec_reference(),
            self._check_playback_state_wiring(),
            self._check_playback_state_sync(),
            self._check_vad_backend(),
            self._check_config_validity(),
        ]

        # Overall health: no critical failures
        critical_failures = [c for c in checks if not c.passed and c.severity == "critical"]
        overall_healthy = len(critical_failures) == 0

        report = HealthReport(
            timestamp=time.time(),
            checks=checks,
            overall_healthy=overall_healthy,
        )

        self._last_report = report
        return report

    def _check_aec_reference(self) -> HealthCheckResult:
        """Check if AEC reference is available."""
        if self._listener is None:
            return HealthCheckResult(
                name="AEC Reference",
                passed=False,
                message="Listener not configured - cannot verify AEC",
                severity="warning",
            )

        has_ref = getattr(self._listener, "has_aec_reference", lambda: False)()
        aec_proc = getattr(self._listener, "_aec_processor", None)

        if not has_ref:
            return HealthCheckResult(
                name="AEC Reference",
                passed=False,
                message="No AEC reference source - echo cancellation disabled",
                severity="critical",
            )

        if aec_proc is None:
            return HealthCheckResult(
                name="AEC Reference",
                passed=False,
                message="AEC processor not initialized",
                severity="critical",
            )

        aec_name = getattr(aec_proc, "name", "unknown")
        if aec_name == "noop":
            # Check if AEC was intended to be enabled (config vs reality mismatch)
            # Try to get wake_aec_enabled from config
            aec_intended_enabled = getattr(self._listener, "_aec_enabled", True)  # Default assume enabled
            if aec_intended_enabled:
                # AEC was enabled in config but we're using NoOp - real library missing
                return HealthCheckResult(
                    name="AEC Reference",
                    passed=False,
                    message=(
                        "AEC is ENABLED in config but using NoOp processor. "
                        "Echo cancellation is NOT active. Install pyaec: pip install pyaec"
                    ),
                    severity="warning",
                )
            else:
                return HealthCheckResult(
                    name="AEC Reference",
                    passed=True,
                    message="AEC intentionally disabled, using NoOp processor",
                    severity="info",
                )

        return HealthCheckResult(
            name="AEC Reference",
            passed=True,
            message=f"AEC active with {aec_name} backend",
        )

    def _check_playback_state_wiring(self) -> HealthCheckResult:
        """Check if playback state callback is wired."""
        if self._music is None:
            return HealthCheckResult(
                name="Playback Wiring",
                passed=False,
                message="Music controller not configured",
                severity="warning",
            )

        callback = getattr(self._music, "on_state_change", None)
        if callback is None:
            return HealthCheckResult(
                name="Playback Wiring",
                passed=False,
                message="on_state_change callback not wired",
                severity="critical",
            )

        return HealthCheckResult(
            name="Playback Wiring",
            passed=True,
            message="Playback state callback wired",
        )

    def _check_playback_state_sync(self) -> HealthCheckResult:
        """Check if playback state is in sync with RMS."""
        diag = self._policy.get_diagnostics()

        # Get policy state
        policy_playing = diag.get("is_playback_active", False)

        # Get latest RMS (if available)
        latest_rms = diag.get("latest_loopback_rms", 0.0)
        rms_threshold = diag.get("config", {}).get("playback_presence_rms_threshold", 150.0)
        rms_indicates_playing = latest_rms > rms_threshold

        # Check for disagreement
        if rms_indicates_playing and not policy_playing:
            return HealthCheckResult(
                name="Playback Sync",
                passed=False,
                message=f"RMS indicates playback ({latest_rms:.0f}) but policy state is paused",
                severity="warning",
            )

        if policy_playing and not rms_indicates_playing and latest_rms > 0:
            # This is OK - could be muted
            return HealthCheckResult(
                name="Playback Sync",
                passed=True,
                message="Policy says playing but RMS low (muted?)",
            )

        # Check if we've received any updates recently
        if self._playback_update_count == 0:
            return HealthCheckResult(
                name="Playback Sync",
                passed=False,
                message="No playback state updates received yet",
                severity="warning",
            )

        now = time.time()
        last_update = self._last_playback_update_time
        if last_update is not None:
            stale_threshold_s = max(self.CHECK_INTERVAL_SECONDS * 2, 30)
            age_s = now - last_update
            if age_s > stale_threshold_s:
                return HealthCheckResult(
                    name="Playback Sync",
                    passed=False,
                    message=f"Playback state updates stale ({age_s:.0f}s since last update)",
                    severity="warning",
                )

        return HealthCheckResult(
            name="Playback Sync",
            passed=True,
            message=f"Playback state synced ({self._playback_update_count} updates received)",
        )

    def _check_vad_backend(self) -> HealthCheckResult:
        """Check which VAD backend is in use."""
        # Check for WebRTC VAD
        try:
            importlib.import_module("webrtcvad")

            return HealthCheckResult(
                name="VAD Backend",
                passed=True,
                message="WebRTC VAD available",
            )
        except ImportError:
            pass

        # Check for torch (Silero VAD potential)
        try:
            importlib.import_module("torch")

            return HealthCheckResult(
                name="VAD Backend",
                passed=True,
                message="Silero VAD available (torch found)",
            )
        except ImportError:
            pass

        return HealthCheckResult(
            name="VAD Backend",
            passed=True,
            message="Using heuristic VAD (RMS-based) - consider installing webrtcvad",
            severity="info",
        )

    def _check_config_validity(self) -> HealthCheckResult:
        """Check if 4-gate config values are sensible."""
        config = self._policy._config
        issues = []

        if config.base_threshold <= 0 or config.base_threshold > 1.0:
            issues.append("base_threshold should be in (0, 1.0]")

        if config.cooldown_ms < 0:
            issues.append("cooldown_ms should be >= 0")

        if config.zero_input_rms_threshold <= 0:
            issues.append("zero_input_rms_threshold should be > 0")

        if issues:
            return HealthCheckResult(
                name="Config Validity",
                passed=False,
                message=f"Config issues: {'; '.join(issues)}",
                severity="warning",
            )

        return HealthCheckResult(
            name="Config Validity",
            passed=True,
            message="Config values valid",
        )

    def start_monitoring(self) -> None:
        """Start background health monitoring."""
        if self._thread is not None:
            return

        # Run initial check
        report = self.run_checks()
        report.log()

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="defense-health-monitor",
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.stop_monitoring)
        logger.info("Defense health monitor started")

    def stop_monitoring(self) -> None:
        """Stop background health monitoring."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=TIMEOUT_SHUTDOWN)
            self._thread = None

    def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        while not self._stop_event.wait(self.CHECK_INTERVAL_SECONDS):
            report = self.run_tiered_checks()
            if not report.overall_healthy:
                report.log()

    def get_last_report(self) -> HealthReport | None:
        """Get the most recent health report."""
        return self._last_report

    # --- Phase 7: Anomaly Detection Interface ---

    def record_detection(self, score: float, latency_ms: float = 0.0, *, activated: bool | None = None) -> None:
        """
        Record a wake word detection for anomaly tracking.

        Args:
            score: Detection score
            latency_ms: Processing latency in milliseconds
            activated: Whether the wake was ultimately accepted (optional; used for richer diagnostics)
        """
        self._anomaly_detector.record_detection(score)
        if latency_ms > 0:
            self._anomaly_detector.record_latency(latency_ms)
        if activated is False:
            self._anomaly_detector.record_rejection("not_activated")

    def record_rejection(self, reason: str) -> None:
        """Record a rejected detection."""
        self._anomaly_detector.record_rejection(reason)

    def get_active_anomalies(self) -> list[AnomalyEvent]:
        """Get currently active anomalies."""
        return self._anomaly_detector.analyze()

    def get_component_health(self) -> dict[str, float]:
        """Get health scores for each component."""
        return self._component_health.copy()

    def get_overall_health_score(self) -> float:
        """Get overall system health score (0.0 - 1.0)."""
        if not self._component_health:
            return 1.0
        return sum(self._component_health.values()) / len(self._component_health)

    def get_statistics(self) -> DefenseStatistics:
        """Get comprehensive statistics."""
        stats = self._anomaly_detector.get_statistics()
        return {
            "detections_last_hour": stats["detections_last_hour"],
            "avg_latency_ms": stats["avg_latency_ms"],
            "p95_latency_ms": stats["p95_latency_ms"],
            "active_anomalies": stats["active_anomalies"],
            "component_health": self.get_component_health(),
            "overall_health": self.get_overall_health_score(),
            "playback_updates": self._playback_update_count,
        }

    def get_current_metrics(self) -> DefenseStatistics:
        return self.get_statistics()

    def get_diagnostics(
        self,
    ) -> dict[str, int | float | str | bool | dict[str, float] | list[str]]:
        stats = self.get_statistics()
        active_anomalies = [str(a) for a in self.get_active_anomalies()]
        return {
            "overall_health": stats["overall_health"],
            "playback_updates": stats["playback_updates"],
            "component_health": stats["component_health"],
            "active_anomalies": active_anomalies,
        }

    # --- Phase 7: Enhanced Checks ---

    def _check_latency(self) -> HealthCheckResult:
        """Check processing latency."""
        stats = self._anomaly_detector.get_statistics()
        avg_latency = stats["avg_latency_ms"]
        p95_latency = stats["p95_latency_ms"]

        if p95_latency > 200:
            self._component_health["latency"] = 0.5
            return HealthCheckResult(
                name="Latency",
                passed=False,
                message=f"P95 latency {p95_latency:.0f}ms exceeds 200ms budget",
                severity="warning",
            )

        if avg_latency > 100:
            self._component_health["latency"] = 0.7
            return HealthCheckResult(
                name="Latency",
                passed=True,
                message=f"Average latency {avg_latency:.0f}ms is elevated",
                severity="info",
            )

        self._component_health["latency"] = 1.0
        return HealthCheckResult(
            name="Latency",
            passed=True,
            message=f"Latency OK (avg={avg_latency:.0f}ms, p95={p95_latency:.0f}ms)",
        )

    def _check_anomalies(self) -> HealthCheckResult:
        """Check for behavioral anomalies."""
        anomalies = self._anomaly_detector.analyze()

        critical = [a for a in anomalies if a.severity == "critical"]
        warnings = [a for a in anomalies if a.severity == "warning"]

        if critical:
            return HealthCheckResult(
                name="Anomaly Detection",
                passed=False,
                message=f"{len(critical)} critical anomalies: {critical[0].message}",
                severity="critical",
            )

        if warnings:
            return HealthCheckResult(
                name="Anomaly Detection",
                passed=True,
                message=f"{len(warnings)} warnings detected",
                severity="info",
            )

        return HealthCheckResult(
            name="Anomaly Detection",
            passed=True,
            message="No anomalies detected",
        )

    def run_tiered_checks(self) -> HealthReport:
        """
        Run checks based on priority tiers.

        Returns only checks that are due based on their interval.
        """
        now = time.time()
        checks = []

        # Critical checks (every 10 seconds)
        if now - self._last_critical_check >= self.CRITICAL_INTERVAL_SECONDS:
            checks.append(self._check_aec_reference())
            checks.append(self._check_latency())
            self._last_critical_check = now

        # Normal checks (every 60 seconds)
        if now - self._last_normal_check >= self.NORMAL_INTERVAL_SECONDS:
            checks.append(self._check_playback_state_wiring())
            checks.append(self._check_playback_state_sync())
            checks.append(self._check_vad_backend())
            checks.append(self._check_config_validity())
            self._last_normal_check = now

        # Low priority checks (every 5 minutes)
        if now - self._last_low_check >= self.LOW_INTERVAL_SECONDS:
            checks.append(self._check_anomalies())
            self._last_low_check = now

        if not checks:
            # No checks due, return last report
            last_report = self.get_last_report()
            if last_report is not None:
                return last_report
            return HealthReport(timestamp=now, checks=[], overall_healthy=True)

        # Evaluate overall health
        critical_failures = [c for c in checks if not c.passed and c.severity == "critical"]
        overall_healthy = len(critical_failures) == 0

        report = HealthReport(
            timestamp=now,
            checks=checks,
            overall_healthy=overall_healthy,
        )

        self._last_report = report
        return report


__all__ = [
    "AnomalyDetector",
    "AnomalyEvent",
    "CheckPriority",
    "DefenseHealthMonitor",
    "HealthCheckResult",
    "HealthReport",
]
