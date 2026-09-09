"""
AEC Health Diagnostics - Single Source of Truth
================================================

Implements the AEC_HEALTH state machine and comprehensive loopback diagnostics.

This module provides:
1. AECHealth enum (GOOD, BAD, UNKNOWN) with hysteresis
2. Periodic 1Hz health logging with [PLAYBACK_LOOPBACK_HEALTH] tag
3. Warning detection for loopback issues
4. Calibration/delay stability tracking
5. Diagnosis summary output

Environment Variables:
    VIOLA_AEC_DIAG=1 - Enable verbose AEC diagnostics (periodic logs)
    VIOLA_WAKE_AUDIT=1 - Also enables diagnostics

Usage:
    from diagnostics.aec_health import get_aec_health_tracker

    tracker = get_aec_health_tracker()

    # Update with each frame
    tracker.update_frame(
        playback_active=True,
        loopback_rms=2500.0,
        mic_rms=1200.0,
        post_aec_rms=800.0,
        correlation=0.65,
    )

    # Get current health state
    health = tracker.get_health()  # AECHealth.GOOD, AECHealth.BAD, or AECHealth.UNKNOWN

    # Print diagnosis summary at shutdown
    tracker.print_diagnosis_summary()
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Environment Variable Check                                                   #
# --------------------------------------------------------------------------- #


def is_aec_diag_enabled() -> bool:
    """Check if AEC diagnostics are enabled via environment variable."""
    return os.environ.get("VIOLA_AEC_DIAG", "").lower() in (
        "1",
        "true",
        "yes",
    ) or os.environ.get(
        "VIOLA_WAKE_AUDIT", ""
    ).lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# AEC Health Enum                                                              #
# --------------------------------------------------------------------------- #


class AECHealth(Enum):
    """
    AEC health status - single source of truth.

    GOOD: AEC is functioning correctly
        - playback_active AND loopback_rms > threshold
        - callbacks arriving at expected rate
        - no underruns
        - calibration confidence good OR peak_corr good
        - delay stable (low variance and slope)

    BAD: AEC has issues that may cause false wakes
        - playback_active AND loopback_rms <= eps for >0.5s
        - callbacks missing or underruns
        - calibration repeatedly fails
        - delay unstable/drifting

    UNKNOWN: Cannot determine health (no playback, initializing)
    """

    GOOD = "GOOD"
    BAD = "BAD"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Configuration Constants (Tunable)                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AECHealthConfig:
    """Configuration for AEC health detection."""

    # RMS thresholds
    loopback_rms_min: float = 50.0  # Minimum RMS to consider loopback active
    loopback_zero_duration_sec: float = 0.5  # Duration of zero RMS to consider BAD

    # Callback health
    expected_callbacks_per_sec: float = 100.0  # Expected callbacks at 10ms
    callback_rate_tolerance: float = 0.8  # Accept 80% of expected
    max_underruns_per_sec: int = 0  # Any underruns = problem

    # Delay stability
    max_delay_variance: float = 100.0  # Max acceptable delay variance (samples^2)
    max_delay_slope: float = 0.5  # Max acceptable drift (samples/sec)

    # Calibration
    min_calibration_confidence: float = 0.5
    min_peak_corr: float = 0.4

    # Callback jitter
    expected_callback_period_ms: float = 10.0
    max_jitter_ratio: float = 2.0  # p95 jitter can be 2x expected period

    # Hysteresis for state transitions
    windows_to_change_state: int = 2  # Require N consecutive windows to change state

    # Logging interval
    health_log_interval_sec: float = 1.0  # Log health every 1 second


# Default config
DEFAULT_AEC_HEALTH_CONFIG = AECHealthConfig()


# --------------------------------------------------------------------------- #
# Rolling Statistics Buffer                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class RollingStats:
    """Rolling window statistics for RMS and other metrics."""

    _values: deque = field(default_factory=lambda: deque(maxlen=100))  # 1 second at 100fps

    def add(self, value: float) -> None:
        """Add a value to the rolling window."""
        self._values.append(value)

    def get_min(self) -> float:
        """Get minimum value in window."""
        return min(self._values) if self._values else 0.0

    def get_max(self) -> float:
        """Get maximum value in window."""
        return max(self._values) if self._values else 0.0

    def get_avg(self) -> float:
        """Get average value in window."""
        return sum(self._values) / len(self._values) if self._values else 0.0

    def get_std(self) -> float:
        """Get standard deviation in window."""
        if len(self._values) < 2:
            return 0.0
        return statistics.stdev(self._values)

    def get_p95(self) -> float:
        """Get 95th percentile in window."""
        if not self._values:
            return 0.0
        sorted_vals = sorted(self._values)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def count(self) -> int:
        """Get count of values in window."""
        return len(self._values)

    def clear(self) -> None:
        """Clear all values."""
        self._values.clear()


# --------------------------------------------------------------------------- #
# Callback Timing Tracker                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class CallbackTimingTracker:
    """Track callback timing for jitter analysis."""

    _timestamps: deque = field(default_factory=lambda: deque(maxlen=200))
    _periods: deque = field(default_factory=lambda: deque(maxlen=100))
    _last_timestamp: float = 0.0
    _callback_count: int = 0
    _underrun_count: int = 0
    _overrun_count: int = 0

    # Per-second counters (reset each window)
    _callbacks_this_window: int = 0
    _underruns_this_window: int = 0
    _overruns_this_window: int = 0
    _window_start_time: float = field(default_factory=time.time)

    def record_callback(self, timestamp: float | None = None) -> None:
        """Record a callback timestamp."""
        ts = timestamp if timestamp is not None else time.time()
        self._timestamps.append(ts)
        self._callback_count += 1
        self._callbacks_this_window += 1

        if self._last_timestamp > 0:
            period_ms = (ts - self._last_timestamp) * 1000.0
            self._periods.append(period_ms)

        self._last_timestamp = ts

    def record_underrun(self) -> None:
        """Record a buffer underrun."""
        self._underrun_count += 1
        self._underruns_this_window += 1

    def record_overrun(self) -> None:
        """Record a buffer overrun."""
        self._overrun_count += 1
        self._overruns_this_window += 1

    def get_period_avg_ms(self) -> float:
        """Get average callback period in ms."""
        if not self._periods:
            return 0.0
        return sum(self._periods) / len(self._periods)

    def get_period_std_ms(self) -> float:
        """Get callback period standard deviation in ms."""
        if len(self._periods) < 2:
            return 0.0
        return statistics.stdev(self._periods)

    def get_period_p95_ms(self) -> float:
        """Get 95th percentile callback period in ms."""
        if not self._periods:
            return 0.0
        sorted_periods = sorted(self._periods)
        idx = int(len(sorted_periods) * 0.95)
        return sorted_periods[min(idx, len(sorted_periods) - 1)]

    def get_callbacks_last_1s(self) -> int:
        """Get callbacks in the last 1 second."""
        return self._callbacks_this_window

    def get_underruns_last_1s(self) -> int:
        """Get underruns in the last 1 second."""
        return self._underruns_this_window

    def reset_window(self) -> tuple[int, int, int]:
        """Reset per-window counters and return (callbacks, underruns, overruns)."""
        callbacks = self._callbacks_this_window
        underruns = self._underruns_this_window
        overruns = self._overruns_this_window
        self._callbacks_this_window = 0
        self._underruns_this_window = 0
        self._overruns_this_window = 0
        self._window_start_time = time.time()
        return callbacks, underruns, overruns

    @property
    def total_callbacks(self) -> int:
        return self._callback_count

    @property
    def total_underruns(self) -> int:
        return self._underrun_count


# --------------------------------------------------------------------------- #
# Delay Stability Tracker                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class DelayStabilityTracker:
    """Track delay measurements for stability analysis."""

    _delays: deque = field(default_factory=lambda: deque(maxlen=100))  # samples
    _timestamps: deque = field(default_factory=lambda: deque(maxlen=100))
    _confidences: deque = field(default_factory=lambda: deque(maxlen=100))
    _fail_reasons: deque = field(default_factory=lambda: deque(maxlen=20))
    _ok_count: int = 0
    _fail_count: int = 0

    def record_calibration(
        self,
        delay_samples: int,
        confidence: float,
        success: bool,
        fail_reason: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Record a calibration result."""
        ts = timestamp if timestamp is not None else time.time()

        if success:
            self._delays.append(delay_samples)
            self._timestamps.append(ts)
            self._confidences.append(confidence)
            self._ok_count += 1
        else:
            self._fail_count += 1
            if fail_reason:
                self._fail_reasons.append(fail_reason)

    def get_delay_mean(self) -> float:
        """Get mean delay in samples."""
        if not self._delays:
            return 0.0
        return sum(self._delays) / len(self._delays)

    def get_delay_std(self) -> float:
        """Get delay standard deviation in samples."""
        if len(self._delays) < 2:
            return 0.0
        return statistics.stdev(self._delays)

    def get_delay_p95(self) -> float:
        """Get 95th percentile delay in samples."""
        if not self._delays:
            return 0.0
        sorted_delays = sorted(self._delays)
        idx = int(len(sorted_delays) * 0.95)
        return sorted_delays[min(idx, len(sorted_delays) - 1)]

    def get_delay_slope(self) -> float:
        """
        Get delay drift slope (samples/sec) via linear regression.

        Positive = delay increasing, Negative = delay decreasing.
        """
        if len(self._delays) < 3:
            return 0.0

        delays = list(self._delays)
        timestamps = list(self._timestamps)

        # Normalize timestamps to start from 0
        t0 = timestamps[0]
        times = [t - t0 for t in timestamps]

        if times[-1] - times[0] < 0.5:  # Need at least 0.5s of data
            return 0.0

        # Simple linear regression: slope = sum((x-mx)(y-my)) / sum((x-mx)^2)
        n = len(times)
        mean_t = sum(times) / n
        mean_d = sum(delays) / n

        numerator = sum((t - mean_t) * (d - mean_d) for t, d in zip(times, delays))
        denominator = sum((t - mean_t) ** 2 for t in times)

        if denominator < 1e-9:
            return 0.0

        return numerator / denominator

    def get_confidence_avg(self) -> float:
        """Get average calibration confidence."""
        if not self._confidences:
            return 0.0
        return sum(self._confidences) / len(self._confidences)

    def get_top_fail_reasons(self, n: int = 3) -> list[tuple[str, int]]:
        """Get top N failure reasons with counts."""
        if not self._fail_reasons:
            return []

        from collections import Counter

        counts = Counter(self._fail_reasons)
        return counts.most_common(n)

    @property
    def ok_count(self) -> int:
        return self._ok_count

    @property
    def fail_count(self) -> int:
        return self._fail_count


# --------------------------------------------------------------------------- #
# Diagnosis Summary Accumulator                                                #
# --------------------------------------------------------------------------- #


@dataclass
class DiagnosisSummary:
    """Accumulates metrics for the final diagnosis summary."""

    # Session counters
    start_time: float = field(default_factory=time.time)
    total_frames: int = 0
    total_windows: int = 0

    # Playback observations
    playback_seen: bool = False
    playback_windows: int = 0

    # Device matching
    device_match_windows: int = 0
    device_mismatch_windows: int = 0
    device_unknown_windows: int = 0

    # Loopback health
    loopback_zero_windows: int = 0
    loopback_nonzero_windows: int = 0

    # Callback health
    total_callbacks: int = 0
    total_underruns: int = 0
    total_overruns: int = 0
    avg_callbacks_per_sec: float = 0.0

    # Format
    format_mismatch_detected: bool = False
    format_mismatch_details: str = ""

    # Calibration
    calibration_ok_count: int = 0
    calibration_fail_count: int = 0
    calibration_top_fail_reasons: list[tuple[str, int]] = field(default_factory=list)

    # Delay
    delay_mean: float = 0.0
    delay_std: float = 0.0
    delay_p95: float = 0.0
    delay_slope: float = 0.0
    delay_confidence_avg: float = 0.0

    # AEC Health time distribution
    time_in_good: float = 0.0
    time_in_bad: float = 0.0
    time_in_unknown: float = 0.0
    last_health: AECHealth = AECHealth.UNKNOWN
    last_reason: str = ""

    # Warnings issued
    warnings: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# AEC Health Tracker (Main Class)                                              #
# --------------------------------------------------------------------------- #


class AECHealthTracker:
    """
    Central AEC health tracking with state machine and diagnostics.

    Provides:
    - AEC_HEALTH state (GOOD, BAD, UNKNOWN) with hysteresis
    - Periodic 1Hz health logging
    - Warning detection
    - Diagnosis summary output
    """

    def __init__(self, config: AECHealthConfig | None = None) -> None:
        """Initialize the AEC health tracker."""
        self._config = config or DEFAULT_AEC_HEALTH_CONFIG
        self._lock = threading.RLock()

        # Current state
        self._health = AECHealth.UNKNOWN
        self._health_reason = "initializing"
        self._consecutive_good_windows = 0
        self._consecutive_bad_windows = 0

        # Rolling statistics
        self._loopback_rms_stats = RollingStats()
        self._mic_rms_stats = RollingStats()
        self._post_aec_rms_stats = RollingStats()
        self._correlation_stats = RollingStats()

        # Callback timing
        self._callback_timing = CallbackTimingTracker()

        # Delay stability
        self._delay_tracker = DelayStabilityTracker()

        # Diagnosis summary
        self._summary = DiagnosisSummary()

        # Logging state
        self._last_health_log_time = 0.0
        self._last_warning_times: dict[str, float] = {}
        self._diag_enabled = is_aec_diag_enabled()

        # Loopback zero duration tracking
        self._loopback_zero_start: float | None = None

        # Device info
        self._output_device_id: str | None = None
        self._output_device_name: str | None = None
        self._loopback_device_id: str | None = None
        self._loopback_device_name: str | None = None
        self._device_match: bool | None = None

        # Buffer info
        self._ref_buffer_fill: int = 0
        self._ref_buffer_max: int = 8000

        # Frame counter
        self._frame_count = 0

        # Health state timing
        self._health_state_start = time.time()

        logger.info(
            "[AEC_HEALTH] AECHealthTracker initialized (diag_enabled=%s)",
            self._diag_enabled,
        )

    def set_device_info(
        self,
        output_device_id: str | None = None,
        output_device_name: str | None = None,
        loopback_device_id: str | None = None,
        loopback_device_name: str | None = None,
    ) -> None:
        """Set device information for mismatch detection."""
        with self._lock:
            self._output_device_id = output_device_id
            self._output_device_name = output_device_name
            self._loopback_device_id = loopback_device_id
            self._loopback_device_name = loopback_device_name

            # Determine device match
            if output_device_name and loopback_device_name:
                # Expected loopback name: "{output_name} [Loopback]"
                expected_loopback = f"{output_device_name} [Loopback]"
                self._device_match = (
                    loopback_device_name == expected_loopback or output_device_name in loopback_device_name
                )
            else:
                self._device_match = None

    def record_callback(self, timestamp: float | None = None) -> None:
        """Record a reference buffer callback."""
        with self._lock:
            self._callback_timing.record_callback(timestamp)

    def record_underrun(self) -> None:
        """Record a reference buffer underrun."""
        with self._lock:
            self._callback_timing.record_underrun()
            self._summary.total_underruns += 1

    def record_buffer_fill(self, current: int, maximum: int) -> None:
        """Record buffer fill level."""
        with self._lock:
            self._ref_buffer_fill = current
            self._ref_buffer_max = maximum

    def record_calibration(
        self,
        delay_samples: int,
        confidence: float,
        success: bool,
        fail_reason: str | None = None,
        peak_corr: float = 0.0,
        search_window_samples: tuple[int, int] = (0, 0),
        playback_active: bool = False,
        loopback_rms: float = 0.0,
        mic_rms: float = 0.0,
        post_aec_rms: float = 0.0,
        corr_peak_margin: float = 0.0,
    ) -> None:
        """
        Record a calibration attempt.

        Logs with [AEC_CALIBRATION] tag.
        """
        with self._lock:
            self._delay_tracker.record_calibration(
                delay_samples=delay_samples,
                confidence=confidence,
                success=success,
                fail_reason=fail_reason,
            )

            if success:
                self._summary.calibration_ok_count += 1
            else:
                self._summary.calibration_fail_count += 1

            # Log calibration attempt
            status = "OK" if success else "FAIL"
            logger.info(
                "[AEC_CALIBRATION] attempt_id=%d playback_active=%s loopback_rms=%.1f "
                "mic_rms=%.1f post_aec_rms=%.1f search_window_samples=%d-%d "
                "best_delay_samples=%d best_peak_corr=%.3f corr_peak_margin=%.3f "
                "confidence=%.3f status=%s fail_reason=%s",
                self._summary.calibration_ok_count + self._summary.calibration_fail_count,
                playback_active,
                loopback_rms,
                mic_rms,
                post_aec_rms,
                search_window_samples[0],
                search_window_samples[1],
                delay_samples,
                peak_corr,
                corr_peak_margin,
                confidence,
                status,
                fail_reason or "none",
            )

    def update_frame(
        self,
        playback_active: bool,
        loopback_rms: float,
        mic_rms: float = 0.0,
        post_aec_rms: float = 0.0,
        correlation: float = 0.0,
        volume: int = 100,
    ) -> AECHealth:
        """
        Update health state with new frame data.

        Called every frame (~10ms). Returns current health state.
        """
        with self._lock:
            self._frame_count += 1
            self._summary.total_frames += 1
            now = time.time()

            # Record frame statistics
            self._loopback_rms_stats.add(loopback_rms)
            self._mic_rms_stats.add(mic_rms)
            self._post_aec_rms_stats.add(post_aec_rms)
            self._correlation_stats.add(correlation)

            # Track playback
            if playback_active:
                self._summary.playback_seen = True

            # Track loopback zero duration during playback
            if playback_active and loopback_rms < self._config.loopback_rms_min:
                if self._loopback_zero_start is None:
                    self._loopback_zero_start = now
            else:
                self._loopback_zero_start = None

            # Periodic health check and logging (1 Hz)
            if now - self._last_health_log_time >= self._config.health_log_interval_sec:
                self._periodic_health_update(playback_active, volume, now)
                self._last_health_log_time = now

            return self._health

    def _periodic_health_update(self, playback_active: bool, volume: int, now: float) -> None:
        """Perform periodic health check and logging."""
        self._summary.total_windows += 1

        # Get window statistics
        callbacks, underruns, overruns = self._callback_timing.reset_window()
        self._summary.total_callbacks += callbacks
        self._summary.total_overruns += overruns

        loopback_rms_min = self._loopback_rms_stats.get_min()
        loopback_rms_avg = self._loopback_rms_stats.get_avg()
        loopback_rms_max = self._loopback_rms_stats.get_max()

        mic_rms_min = self._mic_rms_stats.get_min()
        mic_rms_avg = self._mic_rms_stats.get_avg()
        mic_rms_max = self._mic_rms_stats.get_max()

        post_aec_rms_min = self._post_aec_rms_stats.get_min()
        post_aec_rms_avg = self._post_aec_rms_stats.get_avg()
        post_aec_rms_max = self._post_aec_rms_stats.get_max()

        corr_min = self._correlation_stats.get_min()
        corr_avg = self._correlation_stats.get_avg()
        corr_max = self._correlation_stats.get_max()

        callback_period_avg = self._callback_timing.get_period_avg_ms()
        callback_period_std = self._callback_timing.get_period_std_ms()
        callback_period_p95 = self._callback_timing.get_period_p95_ms()

        # Track playback windows
        if playback_active:
            self._summary.playback_windows += 1

            if loopback_rms_avg < self._config.loopback_rms_min:
                self._summary.loopback_zero_windows += 1
            else:
                self._summary.loopback_nonzero_windows += 1

        # Track device matching
        if self._device_match is True:
            self._summary.device_match_windows += 1
        elif self._device_match is False:
            self._summary.device_mismatch_windows += 1
        else:
            self._summary.device_unknown_windows += 1

        # Compute health state
        new_health, reason = self._compute_health(
            playback_active=playback_active,
            loopback_rms_avg=loopback_rms_avg,
            callbacks=callbacks,
            underruns=underruns,
            now=now,
        )

        # Apply hysteresis
        if new_health == AECHealth.GOOD and self._health != AECHealth.GOOD:
            self._consecutive_good_windows += 1
            self._consecutive_bad_windows = 0
            if self._consecutive_good_windows >= self._config.windows_to_change_state:
                self._transition_health(new_health, reason, now)
        elif new_health == AECHealth.BAD and self._health != AECHealth.BAD:
            self._consecutive_bad_windows += 1
            self._consecutive_good_windows = 0
            if self._consecutive_bad_windows >= self._config.windows_to_change_state:
                self._transition_health(new_health, reason, now)
        elif new_health == self._health:
            # Staying in same state - reset counters
            if new_health == AECHealth.GOOD:
                self._consecutive_good_windows = self._config.windows_to_change_state
                self._consecutive_bad_windows = 0
            elif new_health == AECHealth.BAD:
                self._consecutive_bad_windows = self._config.windows_to_change_state
                self._consecutive_good_windows = 0
        else:
            # Transitioning to UNKNOWN
            self._transition_health(new_health, reason, now)
            self._consecutive_good_windows = 0
            self._consecutive_bad_windows = 0

        # Issue warnings
        self._check_and_issue_warnings(
            playback_active=playback_active,
            loopback_rms_avg=loopback_rms_avg,
            callbacks=callbacks,
            underruns=underruns,
            callback_period_p95=callback_period_p95,
            now=now,
        )

        # Log health status (if diag enabled)
        if self._diag_enabled:
            device_match_str = "true" if self._device_match else ("false" if self._device_match is False else "unknown")
            logger.info(
                "[PLAYBACK_LOOPBACK_HEALTH] ts=%.3f frame=%d playback_active=%s volume=%d "
                "output_device=%s loopback_device=%s device_match=%s "
                "loopback_rms_min=%.1f loopback_rms_avg=%.1f loopback_rms_max=%.1f "
                "mic_rms_min=%.1f mic_rms_avg=%.1f mic_rms_max=%.1f "
                "post_aec_rms_min=%.1f post_aec_rms_avg=%.1f post_aec_rms_max=%.1f "
                "corr_min=%.3f corr_avg=%.3f corr_max=%.3f "
                "ref_buffer_fill=%d/%d ref_callbacks_last_1s=%d ref_underruns_last_1s=%d ref_overruns_last_1s=%d "
                "callback_period_ms_avg=%.2f callback_period_ms_std=%.2f callback_period_ms_p95=%.2f "
                "health=%s",
                now,
                self._frame_count,
                playback_active,
                volume,
                self._output_device_name or "unknown",
                self._loopback_device_name or "unknown",
                device_match_str,
                loopback_rms_min,
                loopback_rms_avg,
                loopback_rms_max,
                mic_rms_min,
                mic_rms_avg,
                mic_rms_max,
                post_aec_rms_min,
                post_aec_rms_avg,
                post_aec_rms_max,
                corr_min,
                corr_avg,
                corr_max,
                self._ref_buffer_fill,
                self._ref_buffer_max,
                callbacks,
                underruns,
                overruns,
                callback_period_avg,
                callback_period_std,
                callback_period_p95,
                self._health.value,
            )

        # Log delay stability periodically
        if self._diag_enabled and self._delay_tracker.ok_count > 0:
            logger.info(
                "[AEC_DELAY_STABILITY] delay_mean=%.1f delay_std=%.1f delay_p95=%.1f "
                "delay_slope=%.3f confidence_avg=%.3f last_fail_reason=%s",
                self._delay_tracker.get_delay_mean(),
                self._delay_tracker.get_delay_std(),
                self._delay_tracker.get_delay_p95(),
                self._delay_tracker.get_delay_slope(),
                self._delay_tracker.get_confidence_avg(),
                (
                    self._delay_tracker.get_top_fail_reasons(1)[0][0]
                    if self._delay_tracker.get_top_fail_reasons(1)
                    else "none"
                ),
            )

    def _compute_health(
        self,
        playback_active: bool,
        loopback_rms_avg: float,
        callbacks: int,
        underruns: int,
        now: float,
    ) -> tuple[AECHealth, str]:
        """Compute health state based on current metrics."""
        if not playback_active:
            return AECHealth.UNKNOWN, "no_playback"

        # Check for loopback issues
        loopback_ok = loopback_rms_avg >= self._config.loopback_rms_min

        # Check for prolonged zero RMS
        loopback_zero_too_long = False
        if self._loopback_zero_start is not None:
            zero_duration = now - self._loopback_zero_start
            if zero_duration >= self._config.loopback_zero_duration_sec:
                loopback_zero_too_long = True

        # Check callback health
        expected_callbacks = self._config.expected_callbacks_per_sec
        callbacks_ok = callbacks >= expected_callbacks * self._config.callback_rate_tolerance

        # Check underruns
        underruns_ok = underruns <= self._config.max_underruns_per_sec

        # Check delay stability
        delay_std = self._delay_tracker.get_delay_std()
        delay_slope = abs(self._delay_tracker.get_delay_slope())
        delay_ok = (
            delay_std <= self._config.max_delay_variance and delay_slope <= self._config.max_delay_slope
        ) or self._delay_tracker.ok_count < 3  # Not enough data yet

        # Check calibration confidence
        confidence_avg = self._delay_tracker.get_confidence_avg()
        calibration_ok = (
            confidence_avg >= self._config.min_calibration_confidence
            or self._delay_tracker.ok_count == 0  # No calibration yet
        )

        # Determine health
        if loopback_zero_too_long:
            return AECHealth.BAD, "loopback_zero_during_playback"
        if not loopback_ok:
            return AECHealth.BAD, "loopback_rms_low"
        if not callbacks_ok:
            return AECHealth.BAD, "callbacks_missing"
        if not underruns_ok:
            return AECHealth.BAD, "ref_underruns"
        if not delay_ok:
            return AECHealth.BAD, "delay_unstable"
        if not calibration_ok:
            return AECHealth.BAD, "calibration_low_confidence"

        return AECHealth.GOOD, "all_checks_passed"

    def _transition_health(self, new_health: AECHealth, reason: str, now: float) -> None:
        """Transition to new health state."""
        old_health = self._health

        # Update time tracking
        elapsed = now - self._health_state_start
        if old_health == AECHealth.GOOD:
            self._summary.time_in_good += elapsed
        elif old_health == AECHealth.BAD:
            self._summary.time_in_bad += elapsed
        else:
            self._summary.time_in_unknown += elapsed

        self._health = new_health
        self._health_reason = reason
        self._health_state_start = now
        self._summary.last_health = new_health
        self._summary.last_reason = reason

        # Log transition
        logger.info(
            "[AEC_HEALTH] from=%s to=%s reason=%s " "loopback_rms_avg=%.1f callbacks=%d underruns=%d",
            old_health.value,
            new_health.value,
            reason,
            self._loopback_rms_stats.get_avg(),
            self._callback_timing.get_callbacks_last_1s(),
            self._callback_timing.get_underruns_last_1s(),
        )

    def _check_and_issue_warnings(
        self,
        playback_active: bool,
        loopback_rms_avg: float,
        callbacks: int,
        underruns: int,
        callback_period_p95: float,
        now: float,
    ) -> None:
        """Check conditions and issue rate-limited warnings."""
        warning_cooldown = 10.0  # Don't repeat same warning within 10 seconds

        # LOOPBACK_ZERO_DURING_PLAYBACK
        if playback_active and loopback_rms_avg < self._config.loopback_rms_min:
            if (
                self._loopback_zero_start
                and (now - self._loopback_zero_start) >= self._config.loopback_zero_duration_sec
            ):
                if now - self._last_warning_times.get("LOOPBACK_ZERO", 0) > warning_cooldown:
                    logger.warning(
                        "[PLAYBACK_LOOPBACK_WARN] warning=LOOPBACK_ZERO_DURING_PLAYBACK "
                        "playback_active=%s loopback_rms_avg=%.1f duration=%.1fs",
                        playback_active,
                        loopback_rms_avg,
                        now - self._loopback_zero_start,
                    )
                    self._last_warning_times["LOOPBACK_ZERO"] = now
                    self._summary.warnings["LOOPBACK_ZERO_DURING_PLAYBACK"] = (
                        self._summary.warnings.get("LOOPBACK_ZERO_DURING_PLAYBACK", 0) + 1
                    )

        # CALLBACK_JITTER_HIGH
        expected_p95 = self._config.expected_callback_period_ms * self._config.max_jitter_ratio
        if callback_period_p95 > expected_p95 and callbacks > 10:
            if now - self._last_warning_times.get("JITTER", 0) > warning_cooldown:
                logger.warning(
                    "[PLAYBACK_LOOPBACK_WARN] warning=CALLBACK_JITTER_HIGH "
                    "callback_period_p95=%.2fms expected_max=%.2fms",
                    callback_period_p95,
                    expected_p95,
                )
                self._last_warning_times["JITTER"] = now
                self._summary.warnings["CALLBACK_JITTER_HIGH"] = (
                    self._summary.warnings.get("CALLBACK_JITTER_HIGH", 0) + 1
                )

        # REF_UNDERRUN
        if underruns > 0:
            if now - self._last_warning_times.get("UNDERRUN", 0) > warning_cooldown:
                logger.warning(
                    "[PLAYBACK_LOOPBACK_WARN] warning=REF_UNDERRUN underruns=%d",
                    underruns,
                )
                self._last_warning_times["UNDERRUN"] = now
                self._summary.warnings["REF_UNDERRUN"] = self._summary.warnings.get("REF_UNDERRUN", 0) + 1

        # DEVICE_MISMATCH
        if self._device_match is False:
            if now - self._last_warning_times.get("DEVICE_MISMATCH", 0) > warning_cooldown:
                logger.warning(
                    "[PLAYBACK_LOOPBACK_WARN] warning=DEVICE_MISMATCH " "output_device=%s loopback_device=%s",
                    self._output_device_name or "unknown",
                    self._loopback_device_name or "unknown",
                )
                self._last_warning_times["DEVICE_MISMATCH"] = now
                self._summary.warnings["DEVICE_MISMATCH"] = self._summary.warnings.get("DEVICE_MISMATCH", 0) + 1

    def get_health(self) -> AECHealth:
        """Get current AEC health state."""
        with self._lock:
            return self._health

    def get_health_reason(self) -> str:
        """Get reason for current health state."""
        with self._lock:
            return self._health_reason

    def get_diagnostics(self) -> dict[str, Any]:
        """Get current diagnostics as a dictionary."""
        with self._lock:
            return {
                "health": self._health.value,
                "health_reason": self._health_reason,
                "frame_count": self._frame_count,
                "loopback_rms_avg": self._loopback_rms_stats.get_avg(),
                "mic_rms_avg": self._mic_rms_stats.get_avg(),
                "post_aec_rms_avg": self._post_aec_rms_stats.get_avg(),
                "correlation_avg": self._correlation_stats.get_avg(),
                "callbacks_total": self._callback_timing.total_callbacks,
                "underruns_total": self._callback_timing.total_underruns,
                "device_match": self._device_match,
                "delay_mean": self._delay_tracker.get_delay_mean(),
                "delay_std": self._delay_tracker.get_delay_std(),
                "delay_slope": self._delay_tracker.get_delay_slope(),
            }

    def print_diagnosis_summary(self) -> None:
        """Print the diagnosis summary to logs."""
        with self._lock:
            # Finalize timing for current state
            now = time.time()
            elapsed = now - self._health_state_start
            if self._health == AECHealth.GOOD:
                time_good = self._summary.time_in_good + elapsed
                time_bad = self._summary.time_in_bad
                time_unknown = self._summary.time_in_unknown
            elif self._health == AECHealth.BAD:
                time_good = self._summary.time_in_good
                time_bad = self._summary.time_in_bad + elapsed
                time_unknown = self._summary.time_in_unknown
            else:
                time_good = self._summary.time_in_good
                time_bad = self._summary.time_in_bad
                time_unknown = self._summary.time_in_unknown + elapsed

            total_time = time_good + time_bad + time_unknown
            if total_time > 0:
                pct_good = time_good / total_time * 100
                pct_bad = time_bad / total_time * 100
                pct_unknown = time_unknown / total_time * 100
            else:
                pct_good = pct_bad = pct_unknown = 0.0

            # Device match rate
            total_match_windows = self._summary.device_match_windows + self._summary.device_mismatch_windows
            device_match_rate = (
                self._summary.device_match_windows / total_match_windows * 100 if total_match_windows > 0 else 0.0
            )

            # Avg callbacks per sec
            session_duration = now - self._summary.start_time
            avg_callbacks_per_sec = self._summary.total_callbacks / session_duration if session_duration > 0 else 0.0

            # Update delay stats
            self._summary.delay_mean = self._delay_tracker.get_delay_mean()
            self._summary.delay_std = self._delay_tracker.get_delay_std()
            self._summary.delay_p95 = self._delay_tracker.get_delay_p95()
            self._summary.delay_slope = self._delay_tracker.get_delay_slope()
            self._summary.delay_confidence_avg = self._delay_tracker.get_confidence_avg()
            self._summary.calibration_top_fail_reasons = self._delay_tracker.get_top_fail_reasons(3)

            # Determine likely root cause
            root_cause = self._determine_root_cause()

            # Format warnings
            warnings_str = (
                ", ".join(f"{k}={v}" for k, v in self._summary.warnings.items()) if self._summary.warnings else "none"
            )

            # Print summary
            logger.info("==================== AEC DIAGNOSIS SUMMARY ====================")
            logger.info("- playback_active_seen: %s", self._summary.playback_seen)
            logger.info(
                "- device_match_rate: %.1f%% (%d/%d windows)",
                device_match_rate,
                self._summary.device_match_windows,
                total_match_windows,
            )
            logger.info(
                "- loopback_zero_windows: %d / %d playback windows",
                self._summary.loopback_zero_windows,
                self._summary.playback_windows,
            )
            logger.info(
                "- ref_callback_health: avg=%.1f callbacks/sec, underruns_total=%d",
                avg_callbacks_per_sec,
                self._summary.total_underruns,
            )
            logger.info(
                "- format_mismatch: %s %s",
                "yes" if self._summary.format_mismatch_detected else "no",
                self._summary.format_mismatch_details,
            )
            logger.info(
                "- calibration: ok=%d fail=%d top_fail_reasons=%s",
                self._summary.calibration_ok_count,
                self._summary.calibration_fail_count,
                self._summary.calibration_top_fail_reasons,
            )
            logger.info(
                "- delay: mean=%.1f std=%.1f p95=%.1f slope=%.3f confidence_avg=%.3f",
                self._summary.delay_mean,
                self._summary.delay_std,
                self._summary.delay_p95,
                self._summary.delay_slope,
                self._summary.delay_confidence_avg,
            )
            logger.info(
                "- AEC_HEALTH: GOOD=%.1fs (%.1f%%) BAD=%.1fs (%.1f%%) UNKNOWN=%.1fs (%.1f%%)",
                time_good,
                pct_good,
                time_bad,
                pct_bad,
                time_unknown,
                pct_unknown,
            )
            logger.info(
                "- last_state: %s reason=%s",
                self._summary.last_health.value,
                self._summary.last_reason,
            )
            logger.info("- warnings_issued: %s", warnings_str)
            logger.info("- LIKELY ROOT CAUSE: %s", root_cause)
            logger.info("===============================================================")

    def _determine_root_cause(self) -> str:
        """Determine the likely root cause category."""
        s = self._summary

        # Check for device mismatch
        if s.device_mismatch_windows > s.device_match_windows and s.device_mismatch_windows > 0:
            return "WRONG_ENDPOINT - Loopback device does not match output device"

        # Check for loopback not working
        if s.playback_windows > 0 and s.loopback_zero_windows > s.loopback_nonzero_windows:
            if s.total_callbacks < 10:
                return "LOOPBACK_NOT_WASAPI_OR_BLOCKED - No callbacks received, WASAPI loopback may not be initialized"
            return "LOOPBACK_NOT_WASAPI_OR_BLOCKED - Callbacks received but RMS is zero during playback"

        # Check for format mismatch
        if s.format_mismatch_detected:
            return f"FORMAT_MISMATCH_OR_RESAMPLE - {s.format_mismatch_details}"

        # Check for buffer issues
        if s.total_underruns > 5 or "CALLBACK_JITTER_HIGH" in s.warnings:
            return "BUFFER_STARVATION_OR_JITTER - Reference buffer underruns or callback timing issues"

        # Check for drift
        if abs(s.delay_slope) > 0.5:
            return f"DRIFT_OR_UNSTABLE_DELAY - Delay slope={s.delay_slope:.3f} samples/sec indicates clock drift"

        # Check for calibration issues
        if s.calibration_fail_count > s.calibration_ok_count and s.calibration_fail_count > 0:
            reasons = (
                ", ".join(r[0] for r in s.calibration_top_fail_reasons) if s.calibration_top_fail_reasons else "unknown"
            )
            return f"DRIFT_OR_UNSTABLE_DELAY - Calibration failures ({reasons})"

        # Check for player state lying
        if s.playback_seen and s.loopback_zero_windows > 0 and s.loopback_nonzero_windows == 0:
            return "PLAYER_STATE_LYING_OR_ROUTING_CHANGED - Player reports playing but no audio detected"

        # No clear issue found
        if not s.playback_seen:
            return "INCONCLUSIVE - No playback detected during session"

        return "INCONCLUSIVE - No clear root cause identified, AEC appears functional"

    def record_format_mismatch(
        self,
        mic_rate: int,
        loopback_rate: int,
        mic_channels: int,
        loopback_channels: int,
        action: str = "none",
    ) -> None:
        """Record a format mismatch detection."""
        with self._lock:
            self._summary.format_mismatch_detected = True
            self._summary.format_mismatch_details = (
                f"mic_rate={mic_rate} loopback_rate={loopback_rate} "
                f"mic_ch={mic_channels} loopback_ch={loopback_channels} action={action}"
            )


# --------------------------------------------------------------------------- #
# Singleton Instance                                                           #
# --------------------------------------------------------------------------- #

_tracker: AECHealthTracker | None = None
_tracker_lock = threading.Lock()


def get_aec_health_tracker() -> AECHealthTracker:
    """Get the global AEC health tracker instance."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = AECHealthTracker()
        return _tracker


def reset_aec_health_tracker() -> None:
    """Reset the global AEC health tracker (for testing)."""
    global _tracker
    with _tracker_lock:
        _tracker = None


__all__ = [
    "AECHealth",
    "AECHealthConfig",
    "AECHealthTracker",
    "get_aec_health_tracker",
    "is_aec_diag_enabled",
    "reset_aec_health_tracker",
]
