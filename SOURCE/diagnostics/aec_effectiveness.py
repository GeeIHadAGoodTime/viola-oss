"""
AEC Effectiveness Diagnostics
=============================

Monitors the effectiveness of Acoustic Echo Cancellation (AEC) for wake word detection.

Tracks:
- Reference Buffer Health: Is AEC receiving playback data?
- AEC Reduction Metrics: How much echo is being cancelled?
- Delay Calibration Status: Is the delay properly configured?
- Convergence Status: Has the adaptive filter converged?

Usage:
    from diagnostics.aec_effectiveness import get_aec_diagnostics

    diag = get_aec_diagnostics()

    # Update with each AEC frame
    diag.record_frame(
        mic_rms=1200.0,
        reference_rms=3500.0,
        post_aec_rms=800.0,
        correlation=0.65,
    )

    # Get effectiveness snapshot
    effectiveness = diag.get_effectiveness_snapshot()

    # Check if AEC is working
    if not effectiveness.is_effective:
        logger.warning("AEC degraded: %s", effectiveness.issues)
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


class AECBackend(Enum):
    """AEC backend types."""

    PYAEC = "PyAEC"
    SPEEX = "Speex"
    WEBRTC = "WebRTC"
    NOOP = "NoOp"
    UNKNOWN = "Unknown"


class AECStatus(Enum):
    """AEC operational status."""

    ACTIVE = "active"  # AEC is processing and effective
    DEGRADED = "degraded"  # AEC is running but not effective
    INACTIVE = "inactive"  # AEC is not processing (no reference)
    ERROR = "error"  # AEC has encountered an error


@dataclass
class ReferenceBufferHealth:
    """Health metrics for the AEC reference buffer."""

    is_receiving_data: bool = False  # Data written in last 500ms
    buffer_fill_percent: float = 0.0  # Buffer utilization
    last_write_timestamp: float = 0.0  # When data was last written
    average_rms: float = 0.0  # Is reference audio loud enough?
    samples_written: int = 0  # Total samples written
    sample_rate: int = 0  # Reference sample rate
    sample_rate_match: bool = True  # Reference rate matches mic rate

    # Rolling window for health tracking
    _rms_history: deque = field(default_factory=lambda: deque(maxlen=100))
    _write_timestamps: deque = field(default_factory=lambda: deque(maxlen=50))

    # Threshold for "receiving data" (500ms)
    DATA_STALE_THRESHOLD_MS: float = 500.0

    def is_healthy(self) -> bool:
        """Check if reference buffer is healthy."""
        return self.is_receiving_data and self.sample_rate_match

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "is_receiving_data": self.is_receiving_data,
            "buffer_fill_percent": round(self.buffer_fill_percent, 1),
            "last_write_timestamp": self.last_write_timestamp,
            "average_rms": round(self.average_rms, 1),
            "samples_written": self.samples_written,
            "sample_rate": self.sample_rate,
            "sample_rate_match": self.sample_rate_match,
            "is_healthy": self.is_healthy(),
        }


@dataclass
class AECReductionMetrics:
    """Metrics for AEC echo reduction effectiveness."""

    backend: str = "Unknown"  # "PyAEC", "Speex", "NoOp"
    delay_ms: float = 0.0  # Current delay setting
    reference_rms: float = 0.0  # Current playback RMS
    post_aec_rms: float = 0.0  # RMS after AEC processing
    reduction_ratio: float = 0.0  # 1 - (post_aec / reference), higher = better
    reduction_db: float = 0.0  # Reduction in decibels
    converged: bool = False  # If backend supports convergence detection

    # Rolling statistics (1 minute window)
    avg_reduction_ratio_1min: float = 0.0
    min_reduction_ratio_1min: float = 0.0
    max_reduction_ratio_1min: float = 0.0

    # Rolling window for statistics
    _reduction_history: deque = field(default_factory=lambda: deque(maxlen=600))  # ~60s at 10fps

    # Thresholds for effectiveness
    EFFECTIVE_REDUCTION_RATIO: float = 0.5  # 50% reduction = working
    MINIMAL_REDUCTION_RATIO: float = 0.2  # <20% = failing

    def is_effective(self) -> bool:
        """Check if AEC is effectively reducing echo."""
        return self.reduction_ratio >= self.EFFECTIVE_REDUCTION_RATIO

    def is_failing(self) -> bool:
        """Check if AEC is failing to reduce echo."""
        return self.reduction_ratio < self.MINIMAL_REDUCTION_RATIO

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "backend": self.backend,
            "delay_ms": round(self.delay_ms, 1),
            "reference_rms": round(self.reference_rms, 1),
            "post_aec_rms": round(self.post_aec_rms, 1),
            "reduction_ratio": round(self.reduction_ratio, 3),
            "reduction_db": round(self.reduction_db, 1),
            "converged": self.converged,
            "avg_reduction_ratio_1min": round(self.avg_reduction_ratio_1min, 3),
            "min_reduction_ratio_1min": round(self.min_reduction_ratio_1min, 3),
            "max_reduction_ratio_1min": round(self.max_reduction_ratio_1min, 3),
            "is_effective": self.is_effective(),
            "is_failing": self.is_failing(),
        }


@dataclass
class DelayCalibration:
    """Status of AEC delay calibration."""

    calibrated: bool = False
    calibration_timestamp: float | None = None
    measured_delay_ms: float = 0.0
    configured_delay_ms: float = 0.0
    confidence: float = 0.0
    method: str = "default"  # "auto", "manual", "default"
    device_id: str = ""  # Device the calibration was done for
    failure_reason: str | None = None

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
        """Check if calibration is stale and should be redone."""
        if not self.calibrated or self.calibration_timestamp is None:
            return True
        age_hours = (time.time() - self.calibration_timestamp) / 3600
        return age_hours > max_age_hours

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "calibrated": self.calibrated,
            "calibration_timestamp": self.calibration_timestamp,
            "measured_delay_ms": round(self.measured_delay_ms, 1),
            "configured_delay_ms": round(self.configured_delay_ms, 1),
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "device_id": self.device_id,
            "failure_reason": self.failure_reason,
            "is_stale": self.is_stale(),
        }


@dataclass
class AECEffectivenessSnapshot:
    """Complete AEC effectiveness snapshot."""

    timestamp: float
    status: AECStatus
    reference_buffer: ReferenceBufferHealth
    reduction: AECReductionMetrics
    calibration: DelayCalibration

    # Overall assessment
    is_effective: bool = True
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "status": self.status.value,
            "is_effective": self.is_effective,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "reference_buffer": self.reference_buffer.to_dict(),
            "reduction": self.reduction.to_dict(),
            "calibration": self.calibration.to_dict(),
        }


# --------------------------------------------------------------------------- #
# Frame Metrics for Per-Frame Tracking                                         #
# --------------------------------------------------------------------------- #


@dataclass
class AECFrameRecord:
    """Record of a single AEC frame for analysis."""

    timestamp: float
    mic_rms: float
    reference_rms: float
    post_aec_rms: float
    correlation: float
    reduction_ratio: float
    gating_active: bool


# --------------------------------------------------------------------------- #
# AEC Diagnostics Monitor                                                       #
# --------------------------------------------------------------------------- #


class AECDiagnostics:
    """
    Comprehensive AEC effectiveness diagnostics.

    Tracks reference buffer health, echo reduction effectiveness,
    and calibration status to provide real-time AEC assessment.

    Thread-safe for concurrent access.
    """

    # Configuration
    REFERENCE_STALE_MS = 500.0  # Consider reference stale after 500ms
    MIN_REFERENCE_RMS = 100.0  # Minimum RMS to consider reference valid
    FRAME_HISTORY_SIZE = 1000  # Keep ~10s of frame history

    def __init__(self) -> None:
        """Initialize AEC diagnostics."""
        self._lock = threading.RLock()

        # Metrics
        self._reference_buffer = ReferenceBufferHealth()
        self._reduction = AECReductionMetrics()
        self._calibration = DelayCalibration()

        # Frame history for analysis
        self._frame_history: deque[AECFrameRecord] = deque(maxlen=self.FRAME_HISTORY_SIZE)

        # Backend info
        self._backend_name = "Unknown"
        self._backend_version = ""

        # Timestamps
        self._start_time = time.time()
        self._frame_count = 0

        logger.info("AECDiagnostics initialized")

    def set_backend_info(
        self,
        backend_name: str,
        version: str = "",
        delay_ms: float = 0.0,
    ) -> None:
        """
        Set AEC backend information.

        Args:
            backend_name: Name of the AEC backend
            version: Backend version string
            delay_ms: Current delay setting
        """
        with self._lock:
            self._backend_name = backend_name
            self._backend_version = version
            self._reduction.backend = backend_name
            self._reduction.delay_ms = delay_ms
            self._calibration.configured_delay_ms = delay_ms

        logger.info(
            "AEC backend configured: %s v%s (delay=%dms)",
            backend_name,
            version,
            delay_ms,
        )

    def set_calibration_result(
        self,
        success: bool,
        delay_ms: float,
        confidence: float,
        method: str = "auto",
        device_id: str = "",
        failure_reason: str | None = None,
    ) -> None:
        """
        Record calibration result.

        Args:
            success: Whether calibration succeeded
            delay_ms: Measured delay
            confidence: Confidence in the measurement
            method: Calibration method used
            device_id: Device ID this calibration is for
            failure_reason: Reason for failure (if failed)
        """
        with self._lock:
            self._calibration.calibrated = success
            self._calibration.calibration_timestamp = time.time()
            self._calibration.measured_delay_ms = delay_ms
            self._calibration.confidence = confidence
            self._calibration.method = method
            self._calibration.device_id = device_id
            self._calibration.failure_reason = failure_reason

            if success:
                self._calibration.configured_delay_ms = delay_ms
                self._reduction.delay_ms = delay_ms

        logger.info(
            "AEC calibration result: success=%s, delay=%sms, confidence=%s, method=%s",
            success,
            format(delay_ms, ".1f"),
            format(confidence, ".3f"),
            method,
        )

    def record_reference_write(
        self,
        samples: int,
        rms: float,
        sample_rate: int,
        buffer_fill_percent: float = 0.0,
    ) -> None:
        """
        Record a reference buffer write event.

        Called by the AEC reference buffer when playback audio is written.

        Args:
            samples: Number of samples written
            rms: RMS of the written audio
            sample_rate: Sample rate of the reference
            buffer_fill_percent: Buffer utilization percentage
        """
        now = time.time()

        with self._lock:
            self._reference_buffer.last_write_timestamp = now
            self._reference_buffer.samples_written += samples
            self._reference_buffer.sample_rate = sample_rate
            self._reference_buffer.buffer_fill_percent = buffer_fill_percent

            # Update RMS history
            self._reference_buffer._rms_history.append(rms)
            self._reference_buffer._write_timestamps.append(now)

            # Calculate average RMS
            if len(self._reference_buffer._rms_history) > 0:
                self._reference_buffer.average_rms = sum(self._reference_buffer._rms_history) / len(
                    self._reference_buffer._rms_history
                )

            # Check if receiving data
            self._reference_buffer.is_receiving_data = True

    def record_frame(
        self,
        mic_rms: float,
        reference_rms: float,
        post_aec_rms: float,
        correlation: float = 0.0,
        gating_active: bool = False,
    ) -> None:
        """
        Record AEC frame metrics.

        Called for each AEC processing frame to track effectiveness.

        Args:
            mic_rms: Raw microphone RMS
            reference_rms: Playback reference RMS
            post_aec_rms: RMS after AEC processing
            correlation: Mic-to-reference correlation
            gating_active: Whether echo gating is active
        """
        now = time.time()
        self._frame_count += 1

        with self._lock:
            # Update reference buffer health
            stale_threshold = self.REFERENCE_STALE_MS / 1000.0
            if now - self._reference_buffer.last_write_timestamp > stale_threshold:
                self._reference_buffer.is_receiving_data = False

            # Calculate reduction metrics
            self._reduction.reference_rms = reference_rms
            self._reduction.post_aec_rms = post_aec_rms

            # Only calculate reduction when there's meaningful reference audio
            if reference_rms > self.MIN_REFERENCE_RMS:
                # Reduction ratio: how much echo was removed
                # reduction = 1 - (post_aec / reference) when post_aec < reference
                # If post_aec > reference, speech is louder than echo (good)
                if post_aec_rms < reference_rms:
                    reduction_ratio = 1.0 - (post_aec_rms / reference_rms)
                else:
                    # Post-AEC is louder = speech present, AEC working
                    reduction_ratio = 1.0

                self._reduction.reduction_ratio = max(0.0, min(1.0, reduction_ratio))

                # Calculate reduction in dB
                if post_aec_rms > 0:
                    self._reduction.reduction_db = 20 * np.log10(reference_rms / max(post_aec_rms, 1.0))
                else:
                    self._reduction.reduction_db = 60.0  # Complete cancellation

                # Update rolling statistics
                self._reduction._reduction_history.append((now, reduction_ratio))
                self._update_rolling_stats()
            else:
                # No meaningful reference - can't assess effectiveness
                self._reduction.reduction_ratio = 0.0
                self._reduction.reduction_db = 0.0

            # Record frame for analysis
            frame = AECFrameRecord(
                timestamp=now,
                mic_rms=mic_rms,
                reference_rms=reference_rms,
                post_aec_rms=post_aec_rms,
                correlation=correlation,
                reduction_ratio=self._reduction.reduction_ratio,
                gating_active=gating_active,
            )
            self._frame_history.append(frame)

    def _update_rolling_stats(self) -> None:
        """Update rolling statistics from reduction history."""
        now = time.time()
        cutoff = now - 60.0  # 1 minute window

        recent = [r for t, r in self._reduction._reduction_history if t > cutoff]

        if len(recent) > 0:
            self._reduction.avg_reduction_ratio_1min = sum(recent) / len(recent)
            self._reduction.min_reduction_ratio_1min = min(recent)
            self._reduction.max_reduction_ratio_1min = max(recent)
        else:
            self._reduction.avg_reduction_ratio_1min = 0.0
            self._reduction.min_reduction_ratio_1min = 0.0
            self._reduction.max_reduction_ratio_1min = 0.0

    def get_effectiveness_snapshot(self) -> AECEffectivenessSnapshot:
        """
        Get current AEC effectiveness snapshot.

        Returns:
            Complete effectiveness snapshot with assessment.
        """
        with self._lock:
            # Determine status
            status = self._assess_status()

            # Create snapshot
            snapshot = AECEffectivenessSnapshot(
                timestamp=time.time(),
                status=status,
                reference_buffer=ReferenceBufferHealth(
                    is_receiving_data=self._reference_buffer.is_receiving_data,
                    buffer_fill_percent=self._reference_buffer.buffer_fill_percent,
                    last_write_timestamp=self._reference_buffer.last_write_timestamp,
                    average_rms=self._reference_buffer.average_rms,
                    samples_written=self._reference_buffer.samples_written,
                    sample_rate=self._reference_buffer.sample_rate,
                    sample_rate_match=self._reference_buffer.sample_rate_match,
                ),
                reduction=AECReductionMetrics(
                    backend=self._reduction.backend,
                    delay_ms=self._reduction.delay_ms,
                    reference_rms=self._reduction.reference_rms,
                    post_aec_rms=self._reduction.post_aec_rms,
                    reduction_ratio=self._reduction.reduction_ratio,
                    reduction_db=self._reduction.reduction_db,
                    converged=self._reduction.converged,
                    avg_reduction_ratio_1min=self._reduction.avg_reduction_ratio_1min,
                    min_reduction_ratio_1min=self._reduction.min_reduction_ratio_1min,
                    max_reduction_ratio_1min=self._reduction.max_reduction_ratio_1min,
                ),
                calibration=DelayCalibration(
                    calibrated=self._calibration.calibrated,
                    calibration_timestamp=self._calibration.calibration_timestamp,
                    measured_delay_ms=self._calibration.measured_delay_ms,
                    configured_delay_ms=self._calibration.configured_delay_ms,
                    confidence=self._calibration.confidence,
                    method=self._calibration.method,
                    device_id=self._calibration.device_id,
                    failure_reason=self._calibration.failure_reason,
                ),
            )

            # Assess overall effectiveness
            self._assess_effectiveness(snapshot)

            return snapshot

    def _assess_status(self) -> AECStatus:
        """Determine current AEC status."""
        if not self._reference_buffer.is_receiving_data:
            return AECStatus.INACTIVE

        if self._reduction.backend == "NoOp":
            return AECStatus.INACTIVE

        if self._reduction.is_failing():
            return AECStatus.DEGRADED

        if self._reduction.is_effective():
            return AECStatus.ACTIVE

        return AECStatus.DEGRADED

    def _assess_effectiveness(self, snapshot: AECEffectivenessSnapshot) -> None:
        """Assess overall AEC effectiveness and generate recommendations."""
        issues = []
        recommendations = []
        is_effective = True

        # Check reference buffer
        if not snapshot.reference_buffer.is_receiving_data:
            issues.append("AEC reference buffer not receiving playback data")
            recommendations.append("Check audio pipeline wiring - ensure playback is routed to AEC buffer")
            is_effective = False

        if snapshot.reference_buffer.average_rms < self.MIN_REFERENCE_RMS:
            issues.append(f"Reference audio too quiet (RMS={snapshot.reference_buffer.average_rms:.0f})")
            recommendations.append("No active playback or playback volume too low")

        # Check reduction effectiveness
        if snapshot.reduction.backend == "NoOp":
            issues.append("AEC backend is NoOp - no echo cancellation active")
            recommendations.append("Install PyAEC or Speex for echo cancellation")
            is_effective = False

        if snapshot.reduction.is_failing() and snapshot.reference_buffer.is_receiving_data:
            issues.append(f"AEC reduction ineffective ({snapshot.reduction.reduction_ratio:.1%})")
            recommendations.append("Check AEC delay calibration - may need recalibration")
            is_effective = False

        # Check calibration
        if not snapshot.calibration.calibrated:
            issues.append("AEC delay not calibrated - using default value")
            recommendations.append("Run AEC delay calibration for optimal performance")

        if snapshot.calibration.is_stale():
            issues.append("AEC calibration is stale (>24 hours old)")
            recommendations.append("Consider recalibrating AEC delay")

        if snapshot.calibration.confidence < 0.5:
            issues.append(f"Low calibration confidence ({snapshot.calibration.confidence:.2f})")
            recommendations.append("Recalibrate in quieter environment")

        snapshot.issues = issues
        snapshot.recommendations = recommendations
        snapshot.is_effective = is_effective

    def get_frame_history(
        self,
        last_n: int = 100,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get frame history for analysis.

        Args:
            last_n: Maximum number of frames to return
            since: Only return frames after this timestamp

        Returns:
            List of frame records as dictionaries
        """
        with self._lock:
            frames = list(self._frame_history)

            if since is not None:
                frames = [f for f in frames if f.timestamp > since]

            frames = frames[-last_n:]

            return [
                {
                    "timestamp": f.timestamp,
                    "mic_rms": round(f.mic_rms, 1),
                    "reference_rms": round(f.reference_rms, 1),
                    "post_aec_rms": round(f.post_aec_rms, 1),
                    "correlation": round(f.correlation, 3),
                    "reduction_ratio": round(f.reduction_ratio, 3),
                    "gating_active": f.gating_active,
                }
                for f in frames
            ]

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics as dictionary for API exposure."""
        return self.get_effectiveness_snapshot().to_dict()

    def reset(self) -> None:
        """Reset all metrics to initial state."""
        with self._lock:
            self._reference_buffer = ReferenceBufferHealth()
            self._reduction = AECReductionMetrics()
            self._reduction.backend = self._backend_name
            self._reduction.delay_ms = self._calibration.configured_delay_ms
            self._frame_history.clear()
            self._frame_count = 0

        logger.info("AECDiagnostics reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_diagnostics: AECDiagnostics | None = None
_diagnostics_lock = threading.Lock()


def get_aec_diagnostics() -> AECDiagnostics:
    """
    Get the global AEC diagnostics instance.

    Returns:
        Global AECDiagnostics instance
    """
    global _diagnostics
    with _diagnostics_lock:
        if _diagnostics is None:
            _diagnostics = AECDiagnostics()
        return _diagnostics


def reset_aec_diagnostics() -> None:
    """Reset the global AEC diagnostics (for testing)."""
    global _diagnostics
    with _diagnostics_lock:
        if _diagnostics is not None:
            _diagnostics.reset()


__all__ = [
    "AECBackend",
    "AECDiagnostics",
    "AECEffectivenessSnapshot",
    "AECReductionMetrics",
    "AECStatus",
    "DelayCalibration",
    "ReferenceBufferHealth",
    "get_aec_diagnostics",
    "reset_aec_diagnostics",
]
