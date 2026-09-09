"""
Audio Pipeline Health Diagnostics
=================================

Comprehensive health monitoring for the audio input pipeline used by wake word detection.

Tracks:
- Clipping Detection: Samples exceeding safe range
- Noise Floor Tracking: Ambient noise levels and SNR
- Buffer Health: Underruns, overruns, latency
- Sample Rate Verification: Frame timing consistency

Usage:
    from diagnostics.audio_pipeline_health import get_audio_health_monitor

    monitor = get_audio_health_monitor()

    # Feed audio frames continuously
    monitor.process_frame(audio_int16, timestamp=time.time())

    # Get current health snapshot
    health = monitor.get_health_snapshot()

    # Check for issues
    if health.has_issues:
        for issue in health.issues:
            logger.warning("Audio issue: %s", issue)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from core.constants import AUDIO_INT16_MAX, SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


class AudioHealthSeverity(Enum):
    """Severity levels for audio health issues."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class ClippingMetrics:
    """Metrics for audio clipping detection."""

    samples_clipped: int = 0  # Total samples with abs(sample) > threshold
    clipping_rate: float = 0.0  # Ratio of clipped samples in window
    last_clip_timestamp: float | None = None
    consecutive_clips: int = 0  # Detect sustained clipping
    peak_value: int = 0  # Highest absolute sample value seen

    # Rolling window for rate calculation
    window_samples_total: int = 0
    window_samples_clipped: int = 0

    # Threshold for clipping detection (int16 scale)
    CLIP_THRESHOLD: int = field(default=32000, repr=False)

    def is_clipping(self) -> bool:
        """Check if clipping rate exceeds acceptable threshold (1%)."""
        return self.clipping_rate > 0.01

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "samples_clipped": self.samples_clipped,
            "clipping_rate": round(self.clipping_rate, 4),
            "last_clip_timestamp": self.last_clip_timestamp,
            "consecutive_clips": self.consecutive_clips,
            "peak_value": self.peak_value,
            "is_clipping": self.is_clipping(),
        }


@dataclass
class NoiseFloorMetrics:
    """Metrics for noise floor tracking and SNR estimation."""

    current_rms: float = 0.0  # Current frame RMS
    noise_floor_rms: float = 0.0  # Rolling minimum RMS (ambient noise estimate)
    signal_rms: float = 0.0  # Rolling maximum RMS (signal estimate)
    snr_db: float = 0.0  # Signal-to-noise ratio in dB
    noise_trend: str = "stable"  # "stable", "rising", "falling"

    # Rolling windows for estimation
    _rms_history: deque = field(default_factory=lambda: deque(maxlen=500))  # ~5s at 100fps
    _noise_floor_window: deque = field(default_factory=lambda: deque(maxlen=100))

    # Timestamps for trend detection
    _last_trend_update: float = 0.0
    _previous_noise_floor: float = 0.0

    def is_low_snr(self, threshold_db: float = 10.0) -> bool:
        """Check if SNR is below acceptable threshold."""
        return self.snr_db < threshold_db

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "current_rms": round(self.current_rms, 1),
            "noise_floor_rms": round(self.noise_floor_rms, 1),
            "signal_rms": round(self.signal_rms, 1),
            "snr_db": round(self.snr_db, 1),
            "noise_trend": self.noise_trend,
            "is_low_snr": self.is_low_snr(),
        }


@dataclass
class BufferHealth:
    """Metrics for audio buffer health."""

    underrun_count: int = 0  # Frames arrived late
    overrun_count: int = 0  # Buffer overflow events
    current_latency_ms: float = 0.0  # Current buffer latency
    buffer_fill_percent: float = 0.0  # Buffer utilization
    avg_frame_interval_ms: float = 0.0  # Average time between frames
    jitter_ms: float = 0.0  # Frame timing variance

    # Frame timing tracking
    _frame_timestamps: deque = field(default_factory=lambda: deque(maxlen=100))
    _expected_interval_ms: float = 10.0  # Expected interval (80ms chunk at 16kHz = 1280 samples)

    def has_timing_issues(self) -> bool:
        """Check if frame timing is inconsistent."""
        return self.jitter_ms > 5.0 or self.underrun_count > 10

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "underrun_count": self.underrun_count,
            "overrun_count": self.overrun_count,
            "current_latency_ms": round(self.current_latency_ms, 2),
            "buffer_fill_percent": round(self.buffer_fill_percent, 1),
            "avg_frame_interval_ms": round(self.avg_frame_interval_ms, 2),
            "jitter_ms": round(self.jitter_ms, 2),
            "has_timing_issues": self.has_timing_issues(),
        }


@dataclass
class SampleRateMetrics:
    """Metrics for sample rate verification."""

    expected_rate: int = SAMPLE_RATE_16K
    measured_rate: float = 0.0  # Calculated from frame timing
    rate_deviation_percent: float = 0.0  # Deviation from expected
    is_rate_match: bool = True  # Within acceptable tolerance (2%)

    # Tracking
    _samples_processed: int = 0
    _start_time: float = 0.0
    _last_check_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "expected_rate": self.expected_rate,
            "measured_rate": round(self.measured_rate, 1),
            "rate_deviation_percent": round(self.rate_deviation_percent, 2),
            "is_rate_match": self.is_rate_match,
        }


@dataclass
class AudioHealthSnapshot:
    """Complete audio pipeline health snapshot."""

    timestamp: float
    clipping: ClippingMetrics
    noise_floor: NoiseFloorMetrics
    buffer: BufferHealth
    sample_rate: SampleRateMetrics

    # Overall status
    overall_status: AudioHealthSeverity = AudioHealthSeverity.OK
    issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        """Check if any issues detected."""
        return len(self.issues) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "overall_status": self.overall_status.value,
            "has_issues": self.has_issues,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "clipping": self.clipping.to_dict(),
            "noise_floor": self.noise_floor.to_dict(),
            "buffer": self.buffer.to_dict(),
            "sample_rate": self.sample_rate.to_dict(),
        }


# --------------------------------------------------------------------------- #
# Audio Health Monitor                                                          #
# --------------------------------------------------------------------------- #


class AudioHealthMonitor:
    """
    Comprehensive audio pipeline health monitor.

    Tracks clipping, noise floor, buffer health, and sample rate
    to provide real-time health assessment for wake word detection.

    Thread-safe for concurrent access from multiple threads.
    """

    # Configuration
    CLIP_THRESHOLD = 32000  # int16 max is 32767
    NOISE_FLOOR_WINDOW_SECONDS = 5.0
    TREND_UPDATE_INTERVAL = 1.0
    RATE_CHECK_INTERVAL = 5.0

    def __init__(
        self,
        expected_sample_rate: int = SAMPLE_RATE_16K,
        expected_frame_ms: float = 80.0,  # 1280 samples at 16kHz
    ) -> None:
        """
        Initialize audio health monitor.

        Args:
            expected_sample_rate: Expected audio sample rate
            expected_frame_ms: Expected interval between frames in ms
        """
        self._expected_sample_rate = expected_sample_rate
        self._expected_frame_ms = expected_frame_ms

        self._lock = threading.RLock()

        # Initialize metrics
        self._clipping = ClippingMetrics()
        self._noise_floor = NoiseFloorMetrics()
        self._buffer = BufferHealth()
        self._sample_rate = SampleRateMetrics(expected_rate=expected_sample_rate)

        # Frame counter
        self._frame_count = 0
        self._start_time = time.time()
        self._last_frame_time = 0.0

        # Clipping window (rolling)
        self._clip_window_size = 16000  # 1 second at 16kHz
        self._clip_window_samples = 0
        self._clip_window_clipped = 0

        logger.info(
            "AudioHealthMonitor initialized: rate=%dHz, frame=%dms",
            expected_sample_rate,
            expected_frame_ms,
        )

    def process_frame(
        self,
        audio: np.ndarray,
        timestamp: float | None = None,
    ) -> None:
        """
        Process an audio frame and update health metrics.

        Should be called for every audio frame from the microphone.
        Designed for minimal overhead (<1ms per frame).

        Args:
            audio: Audio samples (int16 or float32)
            timestamp: Frame timestamp (uses current time if None)
        """
        ts = timestamp or time.time()

        with self._lock:
            self._frame_count += 1

            # Convert to int16 if needed
            if audio.dtype == np.float32:
                audio_int16 = (audio * AUDIO_INT16_MAX).astype(np.int16)
            else:
                audio_int16 = audio.astype(np.int16)

            # Update all metrics
            self._update_clipping(audio_int16, ts)
            self._update_noise_floor(audio_int16, ts)
            self._update_buffer_timing(len(audio_int16), ts)
            self._update_sample_rate(len(audio_int16), ts)

    def _update_clipping(self, audio: np.ndarray, timestamp: float) -> None:
        """Update clipping metrics."""
        # Count clipped samples
        abs_audio = np.abs(audio)
        clipped_mask = abs_audio >= self.CLIP_THRESHOLD
        num_clipped = int(np.sum(clipped_mask))
        peak = int(np.max(abs_audio)) if len(abs_audio) > 0 else 0

        # Update totals
        self._clipping.samples_clipped += num_clipped
        self._clipping.peak_value = max(self._clipping.peak_value, peak)

        # Update rolling window
        self._clip_window_samples += len(audio)
        self._clip_window_clipped += num_clipped

        # Calculate rate when window is full
        if self._clip_window_samples >= self._clip_window_size:
            self._clipping.clipping_rate = self._clip_window_clipped / self._clip_window_samples
            # Reset window
            self._clip_window_samples = 0
            self._clip_window_clipped = 0

        # Track consecutive clips
        if num_clipped > 0:
            self._clipping.last_clip_timestamp = timestamp
            self._clipping.consecutive_clips += 1
        else:
            self._clipping.consecutive_clips = 0

    def _update_noise_floor(self, audio: np.ndarray, timestamp: float) -> None:
        """Update noise floor and SNR metrics."""
        # Calculate RMS
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        self._noise_floor.current_rms = rms

        # Add to history
        self._noise_floor._rms_history.append((timestamp, rms))
        self._noise_floor._noise_floor_window.append(rms)

        # Calculate noise floor (rolling minimum over quiet frames)
        if len(self._noise_floor._noise_floor_window) >= 10:
            # Use 10th percentile as noise floor estimate
            sorted_rms = sorted(self._noise_floor._noise_floor_window)
            percentile_idx = len(sorted_rms) // 10
            self._noise_floor.noise_floor_rms = sorted_rms[max(0, percentile_idx)]

            # Use 90th percentile as signal estimate
            signal_idx = int(len(sorted_rms) * 0.9)
            self._noise_floor.signal_rms = sorted_rms[min(signal_idx, len(sorted_rms) - 1)]

            # Calculate SNR
            if self._noise_floor.noise_floor_rms > 1:
                snr_ratio = self._noise_floor.signal_rms / self._noise_floor.noise_floor_rms
                self._noise_floor.snr_db = 20 * np.log10(max(snr_ratio, 1.0))
            else:
                self._noise_floor.snr_db = 60.0  # Very quiet environment

        # Update trend
        if timestamp - self._noise_floor._last_trend_update >= self.TREND_UPDATE_INTERVAL:
            prev = self._noise_floor._previous_noise_floor
            curr = self._noise_floor.noise_floor_rms

            if prev > 0:
                change_ratio = curr / prev
                if change_ratio > 1.2:
                    self._noise_floor.noise_trend = "rising"
                elif change_ratio < 0.8:
                    self._noise_floor.noise_trend = "falling"
                else:
                    self._noise_floor.noise_trend = "stable"

            self._noise_floor._previous_noise_floor = curr
            self._noise_floor._last_trend_update = timestamp

    def _update_buffer_timing(self, samples: int, timestamp: float) -> None:
        """Update buffer health and timing metrics."""
        # Track frame timestamps
        self._buffer._frame_timestamps.append(timestamp)

        if len(self._buffer._frame_timestamps) >= 2:
            # Calculate intervals
            timestamps = list(self._buffer._frame_timestamps)
            intervals = [(timestamps[i] - timestamps[i - 1]) * 1000 for i in range(1, len(timestamps))]  # Convert to ms

            self._buffer.avg_frame_interval_ms = sum(intervals) / len(intervals)

            # Calculate jitter (standard deviation)
            if len(intervals) >= 3:
                mean_interval = self._buffer.avg_frame_interval_ms
                variance = sum((i - mean_interval) ** 2 for i in intervals) / len(intervals)
                self._buffer.jitter_ms = variance**0.5

            # Detect underruns (frame arrived late)
            expected_ms = self._expected_frame_ms
            late_threshold = expected_ms * 1.5  # 50% late is an underrun

            if intervals and intervals[-1] > late_threshold:
                self._buffer.underrun_count += 1

        # Update latency estimate
        if self._last_frame_time > 0:
            self._buffer.current_latency_ms = (timestamp - self._last_frame_time) * 1000

        self._last_frame_time = timestamp

    def _update_sample_rate(self, samples: int, timestamp: float) -> None:
        """Update sample rate verification metrics."""
        self._sample_rate._samples_processed += samples

        # Initialize start time
        if self._sample_rate._start_time == 0:
            self._sample_rate._start_time = timestamp

        # Check rate periodically
        elapsed = timestamp - self._sample_rate._start_time
        if elapsed >= self.RATE_CHECK_INTERVAL:
            measured_rate = self._sample_rate._samples_processed / elapsed
            self._sample_rate.measured_rate = measured_rate

            # Calculate deviation
            expected = self._expected_sample_rate
            deviation = abs(measured_rate - expected) / expected * 100
            self._sample_rate.rate_deviation_percent = deviation
            self._sample_rate.is_rate_match = deviation < 2.0  # 2% tolerance

            # Reset for next measurement window
            if timestamp - self._sample_rate._last_check_time >= self.RATE_CHECK_INTERVAL * 2:
                self._sample_rate._samples_processed = 0
                self._sample_rate._start_time = timestamp
                self._sample_rate._last_check_time = timestamp

    def get_health_snapshot(self) -> AudioHealthSnapshot:
        """
        Get current audio pipeline health snapshot.

        Returns:
            Complete health snapshot with all metrics and recommendations.
        """
        with self._lock:
            # Create snapshot with copies of metrics
            snapshot = AudioHealthSnapshot(
                timestamp=time.time(),
                clipping=ClippingMetrics(
                    samples_clipped=self._clipping.samples_clipped,
                    clipping_rate=self._clipping.clipping_rate,
                    last_clip_timestamp=self._clipping.last_clip_timestamp,
                    consecutive_clips=self._clipping.consecutive_clips,
                    peak_value=self._clipping.peak_value,
                ),
                noise_floor=NoiseFloorMetrics(
                    current_rms=self._noise_floor.current_rms,
                    noise_floor_rms=self._noise_floor.noise_floor_rms,
                    signal_rms=self._noise_floor.signal_rms,
                    snr_db=self._noise_floor.snr_db,
                    noise_trend=self._noise_floor.noise_trend,
                ),
                buffer=BufferHealth(
                    underrun_count=self._buffer.underrun_count,
                    overrun_count=self._buffer.overrun_count,
                    current_latency_ms=self._buffer.current_latency_ms,
                    avg_frame_interval_ms=self._buffer.avg_frame_interval_ms,
                    jitter_ms=self._buffer.jitter_ms,
                ),
                sample_rate=SampleRateMetrics(
                    expected_rate=self._sample_rate.expected_rate,
                    measured_rate=self._sample_rate.measured_rate,
                    rate_deviation_percent=self._sample_rate.rate_deviation_percent,
                    is_rate_match=self._sample_rate.is_rate_match,
                ),
            )

            # Assess overall health
            self._assess_health(snapshot)

            return snapshot

    def _assess_health(self, snapshot: AudioHealthSnapshot) -> None:
        """Assess overall health and generate recommendations."""
        issues = []
        recommendations = []
        severity = AudioHealthSeverity.OK

        # Check clipping
        if snapshot.clipping.is_clipping():
            issues.append(f"Audio clipping detected: {snapshot.clipping.clipping_rate:.1%} of samples")
            recommendations.append("Reduce microphone gain or input volume")
            severity = AudioHealthSeverity.WARNING

        if snapshot.clipping.consecutive_clips > 100:
            issues.append(f"Sustained clipping: {snapshot.clipping.consecutive_clips} consecutive clips")
            severity = AudioHealthSeverity.ERROR

        # Check noise floor
        if snapshot.noise_floor.is_low_snr():
            issues.append(f"Low SNR: {snapshot.noise_floor.snr_db:.1f}dB")
            recommendations.append("Reduce background noise or move closer to microphone")
            if snapshot.noise_floor.snr_db < 5:
                severity = AudioHealthSeverity.ERROR
            elif severity == AudioHealthSeverity.OK:
                severity = AudioHealthSeverity.WARNING

        if snapshot.noise_floor.noise_trend == "rising":
            issues.append("Ambient noise level rising")
            recommendations.append("Check for new noise sources in environment")

        # Check buffer health
        if snapshot.buffer.has_timing_issues():
            issues.append(
                f"Buffer timing issues: {snapshot.buffer.underrun_count} underruns, {snapshot.buffer.jitter_ms:.1f}ms jitter"
            )
            recommendations.append("Check CPU load and audio driver stability")
            if snapshot.buffer.underrun_count > 50:
                severity = AudioHealthSeverity.ERROR
            elif severity == AudioHealthSeverity.OK:
                severity = AudioHealthSeverity.WARNING

        # Check sample rate
        if not snapshot.sample_rate.is_rate_match:
            issues.append(
                f"Sample rate mismatch: {snapshot.sample_rate.measured_rate:.0f}Hz vs expected {snapshot.sample_rate.expected_rate}Hz"
            )
            recommendations.append("Check audio device configuration and driver settings")
            severity = AudioHealthSeverity.ERROR

        snapshot.issues = issues
        snapshot.recommendations = recommendations
        snapshot.overall_status = severity

    def get_diagnostics(self) -> dict[str, Any]:
        """Get diagnostics as dictionary for API exposure."""
        return self.get_health_snapshot().to_dict()

    def reset(self) -> None:
        """Reset all metrics to initial state."""
        with self._lock:
            self._clipping = ClippingMetrics()
            self._noise_floor = NoiseFloorMetrics()
            self._buffer = BufferHealth()
            self._sample_rate = SampleRateMetrics(expected_rate=self._expected_sample_rate)
            self._frame_count = 0
            self._start_time = time.time()
            self._last_frame_time = 0.0
            self._clip_window_samples = 0
            self._clip_window_clipped = 0

        logger.info("AudioHealthMonitor reset")


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_monitor: AudioHealthMonitor | None = None
_monitor_lock = threading.Lock()


def get_audio_health_monitor(
    expected_sample_rate: int = SAMPLE_RATE_16K,
) -> AudioHealthMonitor:
    """
    Get the global audio health monitor instance.

    Args:
        expected_sample_rate: Expected sample rate (only used on first call)

    Returns:
        Global AudioHealthMonitor instance
    """
    global _monitor
    with _monitor_lock:
        if _monitor is None:
            _monitor = AudioHealthMonitor(expected_sample_rate=expected_sample_rate)
        return _monitor


def reset_audio_health_monitor() -> None:
    """Reset the global audio health monitor (for testing)."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None:
            _monitor.reset()


__all__ = [
    "AudioHealthMonitor",
    "AudioHealthSeverity",
    "AudioHealthSnapshot",
    "BufferHealth",
    "ClippingMetrics",
    "NoiseFloorMetrics",
    "SampleRateMetrics",
    "get_audio_health_monitor",
    "reset_audio_health_monitor",
]
