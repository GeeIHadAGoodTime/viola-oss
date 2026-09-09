"""
Wake Word Health Validator
==========================

Validates that all wake word components are working correctly.

This module provides:
- Automatic health checks for AEC, VAD, playback sync, defense layers
- Quick diagnosis with actionable recommendations
- Metric interpretation for debugging

Usage:
    from diagnostics.wake_health_validator import WakeHealthValidator
    from core.logging_config import get_logger

    validator = WakeHealthValidator()
    report = validator.get_full_health_report()

    logger = get_logger(__name__)
    logger.info("overall_status=%s", report.overall_status)
    logger.info("quick_diagnosis=%s", report.quick_diagnosis)
    logger.info("recommendations=%s", report.recommendations)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class HealthStatus(str, Enum):
    """Health status levels."""

    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass
class HealthCheck:
    """Result of a single health check."""

    name: str
    status: HealthStatus
    reason: str
    details: dict[str, Any] = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "details": self.details,
            "recommendations": self.recommendations,
        }


@dataclass
class MetricInterpretation:
    """Interpretation of a metric value."""

    name: str
    value: Any
    interpretation: str
    severity: HealthStatus
    normal_range: str


@dataclass
class WakeHealthReport:
    """Complete wake word health report."""

    overall_status: HealthStatus
    quick_diagnosis: str
    recommendations: list[str]
    checks: dict[str, HealthCheck]
    metrics_summary: dict[str, MetricInterpretation]
    recent_events: list[dict[str, Any]]
    policy_state: dict[str, Any]
    audio_context: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status.value,
            "quick_diagnosis": self.quick_diagnosis,
            "recommendations": self.recommendations,
            "health": {name: check.to_dict() for name, check in self.checks.items()},
            "metrics_summary": {
                name: {
                    "value": m.value,
                    "interpretation": m.interpretation,
                    "severity": m.severity.value,
                    "normal_range": m.normal_range,
                }
                for name, m in self.metrics_summary.items()
            },
            "recent_events": self.recent_events,
            "policy_state": self.policy_state,
            "audio_context": self.audio_context,
            "timestamp": self.timestamp,
        }


class WakeHealthValidator:
    """
    Validates wake word system health and provides actionable diagnostics.

    This validator checks:
    - AEC reference wiring and signal quality
    - Playback state synchronization
    - VAD backend availability
    - Defense layer effectiveness
    """

    # Thresholds for health checks
    AEC_MIN_RMS_DURING_PLAYBACK = 100.0
    AEC_BROKEN_THRESHOLD_SECONDS = 5.0
    PLAYBACK_SYNC_RMS_THRESHOLD = 100.0
    VAD_BLOCK_RATE_WARNING = 0.7
    VAD_BLOCK_RATE_ERROR = 0.9
    ACCEPTANCE_RATE_WARNING = 0.3
    ACCEPTANCE_RATE_ERROR = 0.1

    def __init__(self) -> None:
        self._last_playback_rms_time: float | None = None
        self._playback_rms_zero_since: float | None = None

    def validate_aec_reference(self) -> HealthCheck:
        """
        Check if AEC reference is wired and providing data.

        Error conditions:
        - loopback_rms=0 during playback for >5 seconds → BROKEN
        - loopback_rms consistently <100 during playback → DEGRADED
        """
        try:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            policy = get_wake_policy()
            diag = policy.get_diagnostics()

            is_playback = diag.get("is_playback_active", False)
            loopback_rms = diag.get("latest_loopback_rms", 0.0)

            if not is_playback:
                return HealthCheck(
                    name="aec_reference",
                    status=HealthStatus.OK,
                    reason="No playback active - AEC not needed",
                    details={"loopback_rms": loopback_rms, "playback_active": False},
                )

            # Playback active - check loopback_rms
            if loopback_rms == 0:
                return HealthCheck(
                    name="aec_reference",
                    status=HealthStatus.ERROR,
                    reason="loopback_rms=0 during playback - AEC reference not wired",
                    details={"loopback_rms": 0, "playback_active": True},
                    recommendations=[
                        "Check WASAPI loopback adapter is running",
                        "Verify Stereo Mix is enabled in Windows Sound settings",
                        "See docs/WAKE_WORD_MASTER_ARCHITECTURE.md 'Known Issues'",
                    ],
                )

            if loopback_rms < self.AEC_MIN_RMS_DURING_PLAYBACK:
                return HealthCheck(
                    name="aec_reference",
                    status=HealthStatus.DEGRADED,
                    reason=f"loopback_rms={loopback_rms:.0f} is low during playback",
                    details={
                        "loopback_rms": loopback_rms,
                        "threshold": self.AEC_MIN_RMS_DURING_PLAYBACK,
                    },
                    recommendations=[
                        "Check playback volume is not too low",
                        "Verify audio output device matches loopback source",
                    ],
                )

            return HealthCheck(
                name="aec_reference",
                status=HealthStatus.OK,
                reason=f"AEC reference active (loopback_rms={loopback_rms:.0f})",
                details={"loopback_rms": loopback_rms, "playback_active": True},
            )

        except Exception as e:
            return HealthCheck(
                name="aec_reference",
                status=HealthStatus.ERROR,
                reason=f"Failed to check AEC reference: {e}",
                recommendations=["Ensure wake_decision_policy is initialized"],
            )

    def validate_playback_sync(self) -> HealthCheck:
        """
        Check if playback state callback matches RMS detection.

        Error conditions:
        - is_playback_active=True but loopback_rms<100 → MISMATCH
        - is_playback_active=False but loopback_rms>1000 → MISMATCH
        """
        try:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            policy = get_wake_policy()
            diag = policy.get_diagnostics()

            is_playback = diag.get("is_playback_active", False)
            loopback_rms = diag.get("latest_loopback_rms", 0.0)

            # Check for mismatch
            if is_playback and loopback_rms < self.PLAYBACK_SYNC_RMS_THRESHOLD:
                return HealthCheck(
                    name="playback_sync",
                    status=HealthStatus.DEGRADED,
                    reason=f"Playback state=active but loopback_rms={loopback_rms:.0f} (expected >100)",
                    details={
                        "is_playback_active": is_playback,
                        "loopback_rms": loopback_rms,
                    },
                    recommendations=[
                        "Playback callback may be out of sync with actual audio",
                        "Check MusicPlayer.add_playback_listener is wired correctly",
                    ],
                )

            if not is_playback and loopback_rms > 1000:
                return HealthCheck(
                    name="playback_sync",
                    status=HealthStatus.DEGRADED,
                    reason=f"Playback state=inactive but loopback_rms={loopback_rms:.0f} (expected <100)",
                    details={
                        "is_playback_active": is_playback,
                        "loopback_rms": loopback_rms,
                    },
                    recommendations=[
                        "Playback stop callback may not have fired",
                        "Check if another app is producing audio",
                    ],
                )

            return HealthCheck(
                name="playback_sync",
                status=HealthStatus.OK,
                reason="Playback state matches audio detection",
                details={
                    "is_playback_active": is_playback,
                    "loopback_rms": loopback_rms,
                },
            )

        except Exception as e:
            return HealthCheck(
                name="playback_sync",
                status=HealthStatus.ERROR,
                reason=f"Failed to check playback sync: {e}",
            )

    def validate_vad_backend(self) -> HealthCheck:
        """
        Check if VAD is using real backend vs heuristic fallback.

        Degraded condition:
        - Using heuristic VAD instead of real VAD model
        """
        try:
            from diagnostics.defense_metrics import get_defense_metrics

            metrics = get_defense_metrics()
            snapshot = metrics.get_snapshot()
            vad_backend = snapshot.vad_backend

            if vad_backend == "heuristic":
                return HealthCheck(
                    name="vad_backend",
                    status=HealthStatus.DEGRADED,
                    reason="Using heuristic VAD fallback (less accurate)",
                    details={"vad_backend": vad_backend},
                    recommendations=[
                        "Install silero-vad for better accuracy",
                        "Heuristic VAD may cause more false positives/negatives",
                    ],
                )

            return HealthCheck(
                name="vad_backend",
                status=HealthStatus.OK,
                reason=f"VAD backend: {vad_backend}",
                details={"vad_backend": vad_backend},
            )

        except Exception as e:
            return HealthCheck(
                name="vad_backend",
                status=HealthStatus.DEGRADED,
                reason=f"Could not determine VAD backend: {e}",
            )

    def validate_defense_wiring(self) -> HealthCheck:
        """
        Check that all defense layers are being evaluated.

        Error condition:
        - A layer has 0 evaluations after >100 raw triggers → NOT WIRED
        """
        try:
            from diagnostics.defense_metrics import get_defense_metrics

            metrics = get_defense_metrics()
            snapshot = metrics.get_snapshot()

            total_triggers = snapshot.total_raw_triggers

            if total_triggers < 10:
                return HealthCheck(
                    name="defense_layers",
                    status=HealthStatus.OK,
                    reason=f"Not enough data ({total_triggers} triggers) - check later",
                    details={"total_triggers": total_triggers},
                )

            # Check each layer
            issues = []
            layers = [
                ("vad_gate", snapshot.vad_gate),
                ("score_check", snapshot.score_check),
                ("echo_veto", snapshot.echo_veto),
            ]

            for name, layer in layers:
                if layer.total_evaluations == 0:
                    issues.append(f"{name}: no evaluations (not wired?)")

            if issues:
                return HealthCheck(
                    name="defense_layers",
                    status=HealthStatus.ERROR,
                    reason=f"Defense layers may not be wired: {', '.join(issues)}",
                    details={
                        "total_triggers": total_triggers,
                        "vad_evaluations": snapshot.vad_gate.total_evaluations,
                        "score_evaluations": snapshot.score_check.total_evaluations,
                        "echo_evaluations": snapshot.echo_veto.total_evaluations,
                    },
                    recommendations=[
                        "Check WakeDecisionPolicy.final_trigger_decision is called",
                        "Ensure DefenseMetricsCollector is recording layer results",
                    ],
                )

            return HealthCheck(
                name="defense_layers",
                status=HealthStatus.OK,
                reason="All defense layers are being evaluated",
                details={
                    "total_triggers": total_triggers,
                    "vad_evaluations": snapshot.vad_gate.total_evaluations,
                    "score_evaluations": snapshot.score_check.total_evaluations,
                    "echo_evaluations": snapshot.echo_veto.total_evaluations,
                },
            )

        except Exception as e:
            return HealthCheck(
                name="defense_layers",
                status=HealthStatus.ERROR,
                reason=f"Failed to check defense layers: {e}",
            )

    def interpret_metrics(self) -> dict[str, MetricInterpretation]:
        """
        Interpret current metrics with guidance on what they mean.
        """
        interpretations = {}

        try:
            from diagnostics.defense_metrics import get_defense_metrics

            metrics = get_defense_metrics()
            snapshot = metrics.get_snapshot()

            # Acceptance rate interpretation
            acceptance_rate = snapshot.acceptance_rate
            if acceptance_rate < self.ACCEPTANCE_RATE_ERROR:
                interp = "VERY LOW - almost all triggers blocked (check if VAD/threshold too strict)"
                severity = HealthStatus.ERROR
            elif acceptance_rate < self.ACCEPTANCE_RATE_WARNING:
                interp = "LOW - most triggers blocked (may miss quiet wake words)"
                severity = HealthStatus.DEGRADED
            elif acceptance_rate > 0.9:
                interp = "HIGH - threshold may be too low (false positives likely)"
                severity = HealthStatus.DEGRADED
            else:
                interp = "NORMAL - balanced detection"
                severity = HealthStatus.OK

            interpretations["acceptance_rate"] = MetricInterpretation(
                name="acceptance_rate",
                value=round(acceptance_rate, 3),
                interpretation=interp,
                severity=severity,
                normal_range="0.3 - 0.8 (idle), 0.1 - 0.5 (during playback)",
            )

            # VAD block rate interpretation
            vad_block_rate = snapshot.vad_gate.block_rate
            if vad_block_rate > self.VAD_BLOCK_RATE_ERROR:
                interp = "VERY HIGH - VAD blocking almost everything (may miss real wake words)"
                severity = HealthStatus.ERROR
            elif vad_block_rate > self.VAD_BLOCK_RATE_WARNING:
                interp = "HIGH - VAD blocking aggressively (check vad_min_confidence settings)"
                severity = HealthStatus.DEGRADED
            elif vad_block_rate < 0.05:
                interp = "VERY LOW - VAD not blocking much (may allow false positives)"
                severity = HealthStatus.DEGRADED
            else:
                interp = "NORMAL - VAD providing reasonable filtering"
                severity = HealthStatus.OK

            interpretations["vad_block_rate"] = MetricInterpretation(
                name="vad_block_rate",
                value=round(vad_block_rate, 3),
                interpretation=interp,
                severity=severity,
                normal_range="0.1 - 0.5 (idle), 0.3 - 0.7 (during playback)",
            )

            # Echo veto rate interpretation
            echo_block_rate = snapshot.echo_veto.block_rate
            if snapshot.echo_veto.total_evaluations > 50:
                if echo_block_rate > 0.8:
                    interp = "HIGH - echo veto blocking most triggers (AEC may be miscalibrated)"
                    severity = HealthStatus.DEGRADED
                elif echo_block_rate < 0.01:
                    interp = "VERY LOW - echo veto not active (check if AEC reference is working)"
                    severity = HealthStatus.DEGRADED
                else:
                    interp = "NORMAL"
                    severity = HealthStatus.OK
            else:
                interp = "Not enough data"
                severity = HealthStatus.OK

            interpretations["echo_veto_rate"] = MetricInterpretation(
                name="echo_veto_rate",
                value=round(echo_block_rate, 3),
                interpretation=interp,
                severity=severity,
                normal_range="0.1 - 0.5 (during playback)",
            )

            # Top blocking layer
            layers = [
                ("vad_gate", snapshot.vad_gate.blocked_count),
                ("score_check", snapshot.score_check.blocked_count),
                ("echo_veto", snapshot.echo_veto.blocked_count),
                ("confirmation", snapshot.confirmation.blocked_count),
            ]
            top_layer = max(layers, key=lambda x: x[1])

            interpretations["top_blocking_layer"] = MetricInterpretation(
                name="top_blocking_layer",
                value=top_layer[0],
                interpretation=f"Most blocks from {top_layer[0]} ({top_layer[1]} blocks)",
                severity=HealthStatus.OK,
                normal_range="vad_gate or score_check typically top",
            )

        except Exception as e:
            logger.warning("Failed to interpret metrics: %s", e)

        return interpretations

    def get_recent_events(self, count: int = 10) -> list[dict[str, Any]]:
        """Get recent wake events for debugging."""
        try:
            from diagnostics.wake_event_logger import get_wake_event_logger

            logger_inst = get_wake_event_logger()
            events = logger_inst.get_recent_events(count=count)
            return [e.to_dict() for e in events]
        except Exception as e:
            logger.debug("Could not get recent events: %s", e)
            return []

    def get_policy_state(self) -> dict[str, Any]:
        """Get current policy state."""
        try:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            policy = get_wake_policy()
            return policy.get_diagnostics()
        except Exception as e:
            return {"error": str(e)}

    def get_audio_context(self) -> dict[str, Any]:
        """Get current audio context."""
        try:
            from voice.wake_detector.wake_decision_policy import get_wake_policy

            policy = get_wake_policy()
            diag = policy.get_diagnostics()
            return {
                "loopback_rms": diag.get("latest_loopback_rms", 0),
                "mic_rms": diag.get("latest_mic_rms", 0),
                "correlation": diag.get("latest_correlation", 0),
                "echo_gating_active": diag.get("echo_gating_active", False),
            }
        except Exception as e:
            return {"error": str(e)}

    def _generate_quick_diagnosis(self, checks: dict[str, HealthCheck]) -> str:
        """Generate a single-sentence diagnosis based on health checks."""
        errors = [c for c in checks.values() if c.status == HealthStatus.ERROR]
        degraded = [c for c in checks.values() if c.status == HealthStatus.DEGRADED]

        if not errors and not degraded:
            return "Wake word system is healthy"

        # Priority order for diagnosis
        if any(c.name == "aec_reference" and c.status == HealthStatus.ERROR for c in errors):
            return "AEC reference not wired - false positives likely during playback"

        if any(c.name == "defense_layers" and c.status == HealthStatus.ERROR for c in errors):
            return "Defense layers not wired - wake word protection disabled"

        if any(c.name == "vad_backend" and c.status == HealthStatus.DEGRADED for c in degraded):
            return "Using heuristic VAD fallback - detection accuracy reduced"

        if any(c.name == "playback_sync" and c.status == HealthStatus.DEGRADED for c in degraded):
            return "Playback state sync mismatch - defense timing may be wrong"

        if any(c.name == "aec_reference" and c.status == HealthStatus.DEGRADED for c in degraded):
            return "AEC reference signal weak - echo cancellation may be impaired"

        return f"{len(errors)} errors, {len(degraded)} warnings detected"

    def _collect_recommendations(self, checks: dict[str, HealthCheck]) -> list[str]:
        """Collect all recommendations from checks."""
        recommendations = []
        for check in checks.values():
            if check.status != HealthStatus.OK:
                recommendations.extend(check.recommendations)
        return list(dict.fromkeys(recommendations))  # Dedupe preserving order

    def get_full_health_report(self) -> WakeHealthReport:
        """
        Run all validators and return a consolidated health report.

        This is the main entry point for debugging - provides:
        - Overall health status
        - Quick diagnosis (single sentence)
        - Prioritized recommendations
        - Detailed check results
        - Metric interpretations
        - Recent events and current state
        """
        checks = {
            "aec_reference": self.validate_aec_reference(),
            "playback_sync": self.validate_playback_sync(),
            "vad_backend": self.validate_vad_backend(),
            "defense_layers": self.validate_defense_wiring(),
        }

        # Determine overall status
        statuses = [c.status for c in checks.values()]
        if HealthStatus.ERROR in statuses:
            overall = HealthStatus.ERROR
        elif HealthStatus.DEGRADED in statuses:
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.OK

        return WakeHealthReport(
            overall_status=overall,
            quick_diagnosis=self._generate_quick_diagnosis(checks),
            recommendations=self._collect_recommendations(checks),
            checks=checks,
            metrics_summary=self.interpret_metrics(),
            recent_events=self.get_recent_events(10),
            policy_state=self.get_policy_state(),
            audio_context=self.get_audio_context(),
        )


# Singleton
_validator: WakeHealthValidator | None = None


def get_wake_health_validator() -> WakeHealthValidator:
    """Get the global wake health validator instance."""
    global _validator
    if _validator is None:
        _validator = WakeHealthValidator()
    return _validator


__all__ = [
    "HealthCheck",
    "HealthStatus",
    "MetricInterpretation",
    "WakeHealthReport",
    "WakeHealthValidator",
    "get_wake_health_validator",
]
