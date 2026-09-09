"""
Multi-Room Sync Engine

Implements Hub monotonic clock synchronization for multi-room audio playback.
Provides drift measurement, correction, and soft DEGRADE mode when correction fails.

Design Principles:
- Hub is monotonic clock authority
- Spokes adjust playback timing to Hub time, not system wall clock
- Drift correction: pause → rebuffer → align
- Soft DEGRADE mode if drift cannot be corrected after M attempts
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.constants import TIMEOUT_MEDIUM
from core.events.bus import EventBus, LocalEventBus
from core.logging_config import StructuredLogger, get_logger

from .events import SyncPulse

_module_logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        _module_logger.error("Background task failed: %s", exc)


class SyncMode(Enum):
    """Sync engine operating mode."""

    ACTIVE = "active"  # Normal sync operation
    DEGRADE = "degrade"  # Soft degradation mode (drift correction failed)
    DISABLED = "disabled"  # Sync disabled


class DriftCorrectionResult(Enum):
    """Result of drift correction attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # Drift too small to correct


@dataclass(frozen=True, slots=True)
class DriftMeasurement:
    """Drift measurement between Hub time and local time."""

    hub_time: float  # Hub monotonic clock time
    local_time: float  # Local monotonic clock time
    drift_ms: float  # Drift in milliseconds (positive = local ahead, negative = local behind)
    measured_at: float  # Wall clock time when measurement was taken
    round_trip_ms: float | None = None  # Network round-trip time if available


@dataclass(frozen=True, slots=True)
class SyncState:
    """Current sync engine state."""

    mode: SyncMode
    hub_time_offset: float  # Offset from local monotonic to Hub monotonic
    last_drift_ms: float  # Last measured drift
    correction_count: int  # Number of corrections attempted
    consecutive_drift_count: int  # PRD §3.1: Consecutive pulses exceeding drift threshold
    last_correction_at: float | None = None
    degrade_entered_at: float | None = None


class SyncEngine:
    """
    Multi-room sync engine with Hub monotonic clock.

    Features:
    - Hub monotonic clock tracking
    - Periodic sync_pulse emission
    - Drift measurement (HubTime vs LocalTime)
    - Drift correction (pause → rebuffer → align)
    - Soft DEGRADE mode if drift cannot be corrected

    Threading Model:
    - Thread-safe for concurrent access
    - Async-safe for async operations
    - Background task for sync pulse emission
    """

    # PRD §3.1 Drift Hard-Resync Rule constants
    PRD_DRIFT_THRESHOLD_MS: float = 30.0  # PRD: < 30ms drift tolerance
    PRD_JITTER_TOLERANCE_MS: float = 15.0  # PRD: < 15ms jitter tolerance
    PRD_CONSECUTIVE_PULSES_K: int = 5  # PRD: K=5 consecutive pulses before resync

    def __init__(
        self,
        hub_clock_url: str | None = None,
        event_bus: EventBus | None = None,
        logger: logging.Logger | StructuredLogger | None = None,
        sync_pulse_interval: float = 1.0,  # seconds
        drift_threshold_ms: float = 30.0,  # PRD §3.1: < 30ms drift tolerance
        jitter_tolerance_ms: float = 15.0,  # PRD §3.1: < 15ms jitter tolerance
        consecutive_drift_pulses_k: int = 5,  # PRD §3.1: K=5 consecutive pulses before hard resync
        max_correction_attempts: int = 3,  # PRD §3.1: M attempts before DEGRADE
        correction_cooldown: float = 5.0,  # seconds between corrections
    ) -> None:
        """
        Initialize sync engine.

        Args:
            hub_clock_url: URL for Hub clock endpoint (None = Hub mode, use local as authority)
            event_bus: Event bus for emitting events (default: LocalEventBus)
            logger: Logger instance (default: creates new logger)
            sync_pulse_interval: Interval between sync pulses (default: 1.0s)
            drift_threshold_ms: Drift threshold for correction (PRD §3.1: 30ms)
            jitter_tolerance_ms: Jitter tolerance (PRD §3.1: 15ms)
            consecutive_drift_pulses_k: Consecutive pulses exceeding drift before hard resync (PRD §3.1: K=5)
            max_correction_attempts: Max correction attempts before DEGRADE (PRD §3.1: M attempts)
            correction_cooldown: Cooldown between corrections (default: 5.0s)
        """
        self._hub_clock_url = hub_clock_url
        self._sync_pulse_interval = sync_pulse_interval
        self._drift_threshold_ms = drift_threshold_ms
        self._jitter_tolerance_ms = jitter_tolerance_ms
        self._consecutive_drift_pulses_k = consecutive_drift_pulses_k
        self._max_correction_attempts = max_correction_attempts
        self._correction_cooldown = correction_cooldown

        # State
        self._mode = SyncMode.ACTIVE
        self._hub_time_offset: float = 0.0  # Offset: hub_time = local_time + offset
        self._last_drift_ms: float = 0.0
        self._correction_count = 0
        self._last_correction_at: float | None = None
        self._degrade_entered_at: float | None = None

        # PRD §3.1: Track consecutive drift pulses for hard-resync rule
        self._consecutive_drift_count: int = 0

        # Threading
        self._lock = threading.RLock()
        self._sync_task: asyncio.Task | None = None
        self._running = False

        # Event bus and logging
        self._event_bus = event_bus or LocalEventBus()
        self._logger = logger or get_logger("audio_core.sync_engine")

        # Subscription for reading sync pulses (Spoke mode)
        self._pulse_subscription: Any | None = None

        # Callbacks for drift correction
        self._pause_callback: Callable[[], None] | None = None
        self._rebuffer_callback: Callable[[], None] | None = None
        self._align_callback: Callable[[float], None] | None = None  # Takes drift_ms
        self._resume_callback: Callable[[], None] | None = None

    def set_correction_callbacks(
        self,
        pause: Callable[[], None] | None = None,
        rebuffer: Callable[[], None] | None = None,
        align: Callable[[float], None] | None = None,
        resume: Callable[[], None] | None = None,
    ) -> None:
        """
        Set callbacks for drift correction operations.

        Args:
            pause: Callback to pause playback
            rebuffer: Callback to rebuffer audio
            align: Callback to align playback (takes drift_ms as argument)
            resume: Callback to resume playback
        """
        with self._lock:
            self._pause_callback = pause
            self._rebuffer_callback = rebuffer
            self._align_callback = align
            self._resume_callback = resume

    def start(self) -> None:
        """Start sync engine (starts background sync pulse task and subscribes to pulses)."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._logger.info("Starting sync engine")

        # Subscribe to sync pulses (for reading pulses from Hub in Spoke mode)
        from .events import SyncPulse

        self._pulse_subscription = self._event_bus.subscribe(SyncPulse, self._on_sync_pulse)

        # Start background task
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            # mt-ok: hub-spoke audio sync pulse is desktop multiroom timing,
            # not per-user state.
            self._sync_task = asyncio.create_task(self._sync_pulse_loop())
            self._sync_task.add_done_callback(_log_task_exception)
        else:
            # If no event loop, we'll start sync pulses on next async operation
            self._logger.warning("No running event loop; sync pulses will start on next async op")

    def stop(self) -> None:
        """Stop sync engine (stops background sync pulse task and unsubscribes)."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._logger.info("Stopping sync engine")

        # Unsubscribe from sync pulses
        if self._pulse_subscription:
            try:
                self._pulse_subscription.cancel()
            except Exception as e:
                self._logger.warning("Error unsubscribing from sync pulses: %s", e)
            self._pulse_subscription = None

        # Cancel background task
        if self._sync_task:
            self._sync_task.cancel()
            self._sync_task = None

    def get_hub_time(self) -> float:
        """
        Get current Hub monotonic clock time.

        Returns:
            Hub monotonic time (seconds)
        """
        with self._lock:
            local_time = time.monotonic()
            return local_time + self._hub_time_offset

    def measure_drift(self, hub_time: float | None = None) -> DriftMeasurement:
        """
        Measure drift between Hub time and local time.

        Args:
            hub_time: Optional Hub time (if None, uses current Hub time)

        Returns:
            DriftMeasurement with drift information
        """
        local_time = time.monotonic()
        wall_time = time.time()

        with self._lock:
            if hub_time is None:
                hub_time = local_time + self._hub_time_offset

            drift_ms = (local_time - hub_time) * 1000.0
            self._last_drift_ms = drift_ms

        # Telemetry: record sync drift measurement
        try:
            from admin.instrumentation import record_sync_drift

            record_sync_drift(drift_ms)
        except Exception:
            self._logger.debug("Sync drift telemetry recording failed (non-critical)")

        return DriftMeasurement(
            hub_time=hub_time,
            local_time=local_time,
            drift_ms=drift_ms,
            measured_at=wall_time,
        )

    async def sync_to_hub_clock(self, hub_time: float, round_trip_ms: float | None = None) -> None:
        """
        Synchronize local clock to Hub clock.

        Args:
            hub_time: Hub monotonic clock time
            round_trip_ms: Optional network round-trip time for adjustment
        """
        local_time = time.monotonic()

        # Adjust for half round-trip time if available
        if round_trip_ms is not None:
            adjustment = (round_trip_ms / 2.0) / 1000.0
            hub_time += adjustment

        with self._lock:
            self._hub_time_offset = hub_time - local_time
            self._logger.debug(
                "Synced to Hub clock: offset=%ss, round_trip=%sms",
                f"{self._hub_time_offset:.6f}",
                round_trip_ms,
            )

    def apply_small_drift_adjustment(self, drift_ms: float, max_adjustment_ms: float = 10.0) -> None:
        """
        Apply small drift adjustment without pause/rebuffer.

        Adjusts the hub_time_offset slightly to compensate for drift.
        This is a gradual correction that doesn't interrupt playback.

        Args:
            drift_ms: Measured drift in milliseconds (positive = local ahead, negative = local behind)
            max_adjustment_ms: Maximum adjustment per call (default: 10ms)
        """
        # Limit adjustment to avoid large jumps
        adjustment_ms = max(-max_adjustment_ms, min(max_adjustment_ms, drift_ms))

        with self._lock:
            # Adjust offset: if local is ahead (positive drift), we need to slow down
            # by increasing the offset (making hub_time appear later)
            adjustment_sec = adjustment_ms / 1000.0
            self._hub_time_offset += adjustment_sec
            self._last_drift_ms = drift_ms - adjustment_ms  # Track remaining drift

            self._logger.debug(
                "Applied small drift adjustment: %sms (remaining: %sms)",
                f"{adjustment_ms:.2f}",
                f"{self._last_drift_ms:.2f}",
            )

    def _on_sync_pulse(self, event: SyncPulse) -> None:
        """
        Handle incoming sync pulse event (for reading pulses in Spoke mode).

        Measures drift and applies small adjustments.

        Args:
            event: SyncPulse event from event bus
        """
        # Only process pulses from other sources (not our own)
        if event.source == "audio_core.sync_engine":
            return  # Ignore our own pulses

        try:
            from admin.instrumentation import record_feature_used

            record_feature_used("multiroom")
        except Exception:
            self._logger.debug("Feature usage telemetry recording failed (non-critical)")

        # Measure drift using the pulse data
        measurement = self.measure_drift(event.hub_time)

        # Apply small drift adjustments (basic sync mode)
        if self._mode == SyncMode.ACTIVE:
            drift_ms = abs(measurement.drift_ms)
            if drift_ms > 1.0:  # Only adjust if drift > 1ms
                self.apply_small_drift_adjustment(measurement.drift_ms)

    def check_drift_and_update_counter(self, measurement: DriftMeasurement) -> bool:
        """
        Check drift against threshold and update consecutive drift counter per PRD §3.1.

        PRD §3.1 Drift Hard-Resync Rule:
        If drift > 30ms for more than K consecutive sync pulses (K=5),
        the Spoke MUST pause, re-align buffer, and resume aligned to Hub.

        Args:
            measurement: Drift measurement to check

        Returns:
            True if hard resync is required (K consecutive pulses exceeded)
        """
        drift_ms = abs(measurement.drift_ms)

        with self._lock:
            if drift_ms > self._drift_threshold_ms:
                self._consecutive_drift_count += 1
                self._logger.debug(
                    "Drift %.2fms > %sms threshold, consecutive count: %s/%s",
                    drift_ms,
                    self._drift_threshold_ms,
                    self._consecutive_drift_count,
                    self._consecutive_drift_pulses_k,
                )
                return self._consecutive_drift_count >= self._consecutive_drift_pulses_k
            else:
                # Reset counter when drift is within tolerance
                if self._consecutive_drift_count > 0:
                    self._logger.debug(
                        "Drift %.2fms within tolerance, resetting consecutive count",
                        drift_ms,
                    )
                self._consecutive_drift_count = 0
                return False

    def correct_drift(self, measurement: DriftMeasurement) -> DriftCorrectionResult:
        """
        Attempt to correct drift using pause → rebuffer → align strategy per PRD §3.1.

        PRD §3.1 specifies:
        - Drift tolerance: < 30ms
        - If drift > 30ms for K=5 consecutive pulses: hard resync
        - Hard resync: pause → re-align buffer to Hub position → resume
        - If cannot recover after M attempts: mark node DEGRADED

        Args:
            measurement: Drift measurement to correct

        Returns:
            DriftCorrectionResult indicating success/failure
        """
        drift_ms = abs(measurement.drift_ms)

        # PRD §3.1: Check consecutive drift pulses before triggering hard resync
        needs_hard_resync = self.check_drift_and_update_counter(measurement)

        # Skip if drift is below threshold and not enough consecutive pulses
        if not needs_hard_resync and drift_ms < self._drift_threshold_ms:
            return DriftCorrectionResult.SKIPPED

        # Check cooldown
        with self._lock:
            now = time.time()
            if self._last_correction_at is not None and (now - self._last_correction_at) < self._correction_cooldown:
                self._logger.debug("Drift correction on cooldown: %.2fms drift", drift_ms)
                return DriftCorrectionResult.SKIPPED

            # PRD §3.1: Check if we've exceeded max attempts (M)
            if self._correction_count >= self._max_correction_attempts:
                if self._mode != SyncMode.DEGRADE:
                    self._enter_degrade_mode()
                return DriftCorrectionResult.FAILED

            self._correction_count += 1
            self._last_correction_at = now

        # PRD §3.1: Perform hard resync: pause → re-align buffer → resume
        try:
            self._logger.info(
                "PRD §3.1 hard resync triggered: %.2fms drift, %s consecutive pulses",
                drift_ms,
                self._consecutive_drift_count,
            )

            # Step 1: Temporarily pause playback
            if self._pause_callback:
                self._pause_callback()

            # Step 2: Rebuffer (re-align buffer to Hub's current position)
            if self._rebuffer_callback:
                self._rebuffer_callback()

            # Step 3: Align (adjust playback position to Hub time)
            if self._align_callback:
                self._align_callback(measurement.drift_ms)

            # Step 4: Resume playback aligned to Hub
            if self._resume_callback:
                self._resume_callback()

            # Update offset to reflect correction
            with self._lock:
                # Adjust offset to account for correction
                correction_offset = measurement.drift_ms / 1000.0
                self._hub_time_offset += correction_offset
                # Reset consecutive drift count after successful correction
                self._consecutive_drift_count = 0

            self._logger.info("PRD §3.1 hard resync successful: corrected %.2fms drift", drift_ms)

            # Telemetry: record sync correction
            try:
                from admin.instrumentation import record_sync_correction

                record_sync_correction("seek")
            except Exception:
                self._logger.debug("Sync correction telemetry recording failed (non-critical)")

            return DriftCorrectionResult.SUCCESS

        except Exception as e:
            self._logger.error("PRD §3.1 hard resync failed: %s", e)
            return DriftCorrectionResult.FAILED

    def _enter_degrade_mode(self) -> None:
        """Enter soft DEGRADE mode (drift correction failed)."""
        with self._lock:
            if self._mode == SyncMode.DEGRADE:
                return

            self._mode = SyncMode.DEGRADE
            self._degrade_entered_at = time.time()
            self._logger.warning(
                "Entering DEGRADE mode: %s correction attempts failed",
                self._correction_count,
            )

            # Telemetry: record degrade mode entry
            try:
                from admin.instrumentation import record_sync_correction

                record_sync_correction("degrade")
            except Exception:
                self._logger.debug("Sync degrade telemetry recording failed (non-critical)")

    def reset_degrade_mode(self) -> None:
        """Reset from DEGRADE mode (e.g., after manual intervention)."""
        with self._lock:
            if self._mode != SyncMode.DEGRADE:
                return

            self._mode = SyncMode.ACTIVE
            self._correction_count = 0
            self._last_correction_at = None
            self._degrade_entered_at = None
            self._logger.info("Exiting DEGRADE mode")

    async def _fetch_hub_clock(self) -> tuple[float, float] | None:
        """
        Fetch Hub clock time from Hub endpoint (Spoke mode only).

        Returns:
            Tuple of (hub_time, round_trip_ms) or None if fetch failed
        """
        if not self._hub_clock_url:
            return None  # Hub mode, no fetch needed

        try:
            # Try to import httpx (optional dependency)
            try:
                import httpx
            except ImportError:
                self._logger.warning("httpx not available, cannot fetch Hub clock")
                return None

            # Fetch Hub clock with timing
            request_start = time.monotonic()
            async with httpx.AsyncClient(timeout=TIMEOUT_MEDIUM) as client:
                response = await client.get(self._hub_clock_url)
                response.raise_for_status()
                data = response.json()

            request_end = time.monotonic()
            round_trip_ms = (request_end - request_start) * 1000.0

            # Extract hub_time from response
            if isinstance(data, dict) and "data" in data:
                hub_data = data["data"]
            else:
                hub_data = data

            hub_time = hub_data.get("hub_time")
            if hub_time is None:
                self._logger.warning("Hub clock response missing hub_time")
                return None

            return (float(hub_time), round_trip_ms)

        except Exception as e:
            self._logger.warning("Failed to fetch Hub clock: %s", e)
            return None

    async def _sync_pulse_loop(self) -> None:
        """Background task that emits sync pulses periodically."""
        while self._running:
            try:
                await asyncio.sleep(self._sync_pulse_interval)

                if not self._running:
                    break

                # If Spoke mode, fetch Hub clock and sync
                if self._hub_clock_url:
                    hub_result = await self._fetch_hub_clock()
                    if hub_result:
                        hub_time, round_trip_ms = hub_result
                        await self.sync_to_hub_clock(hub_time, round_trip_ms)

                # Emit sync pulse
                hub_time = self.get_hub_time()
                measurement = self.measure_drift(hub_time)

                # Emit event
                try:
                    event = SyncPulse(
                        hub_time=hub_time,
                        local_time=measurement.local_time,
                        drift_ms=measurement.drift_ms,
                        mode=self._mode.value,
                        source="audio_core.sync_engine",
                    )
                    self._event_bus.publish(event)
                except Exception as e:
                    self._logger.warning("Failed to emit sync pulse: %s", e)

                # Check drift and apply adjustments
                if self._mode == SyncMode.ACTIVE:
                    # Apply small drift adjustments (basic sync mode)
                    drift_ms = abs(measurement.drift_ms)
                    if drift_ms > 1.0:  # Only adjust if drift > 1ms
                        self.apply_small_drift_adjustment(measurement.drift_ms)

                    # Full correction (pause/rebuffer) only if drift exceeds threshold
                    if drift_ms >= self._drift_threshold_ms:
                        result = self.correct_drift(measurement)
                        if result == DriftCorrectionResult.FAILED:
                            self._logger.warning("Drift correction failed, may enter DEGRADE mode")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error("Error in sync pulse loop: %s", e)
                await asyncio.sleep(self._sync_pulse_interval)  # Back off on error

    def get_state(self) -> SyncState:
        """
        Get current sync engine state.

        Returns:
            SyncState with current state information
        """
        with self._lock:
            return SyncState(
                mode=self._mode,
                hub_time_offset=self._hub_time_offset,
                last_drift_ms=self._last_drift_ms,
                correction_count=self._correction_count,
                consecutive_drift_count=self._consecutive_drift_count,
                last_correction_at=self._last_correction_at,
                degrade_entered_at=self._degrade_entered_at,
            )

    def get_metrics(self) -> dict[str, Any]:
        """
        Get sync engine metrics (for monitoring).

        Returns:
            Dictionary with metrics including PRD §3.1 compliance values
        """
        state = self.get_state()
        return {
            "mode": state.mode.value,
            "hub_time_offset": state.hub_time_offset,
            "last_drift_ms": state.last_drift_ms,
            "correction_count": state.correction_count,
            "consecutive_drift_count": state.consecutive_drift_count,
            "last_correction_at": state.last_correction_at,
            "degrade_entered_at": state.degrade_entered_at,
            # PRD §3.1 configuration values
            "drift_threshold_ms": self._drift_threshold_ms,
            "jitter_tolerance_ms": self._jitter_tolerance_ms,
            "consecutive_drift_pulses_k": self._consecutive_drift_pulses_k,
            "max_correction_attempts": self._max_correction_attempts,
            # PRD §3.1 compliance reference values
            "prd_drift_threshold_ms": self.PRD_DRIFT_THRESHOLD_MS,
            "prd_jitter_tolerance_ms": self.PRD_JITTER_TOLERANCE_MS,
            "prd_consecutive_pulses_k": self.PRD_CONSECUTIVE_PULSES_K,
        }


__all__ = [
    "DriftCorrectionResult",
    "DriftMeasurement",
    "SyncEngine",
    "SyncMode",
    "SyncState",
]
