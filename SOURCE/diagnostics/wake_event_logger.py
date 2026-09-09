"""
Wake Event Logger
=================

Structured logging for wake word events with correlation IDs,
timing information, and full diagnostic context.

Usage:
    from diagnostics.wake_event_logger import get_wake_event_logger, WakeEventType

    logger = get_wake_event_logger()

    # Start a detection flow
    correlation_id = logger.start_detection_flow()

    # Log events in the flow
    logger.log_raw_trigger(score=0.85, mic_rms=1200, loopback_rms=3000)
    logger.log_layer_result("vad_gate", passed=True)
    logger.log_accepted(layers_passed=["vad_gate", "score_check", "echo_veto"])

    # Get recent events for debugging
    events = logger.get_recent_events(count=50)

    # Export for analysis
    logger.export_events(Path("analysis/wake_events.json"))
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)


class WakeEventType(Enum):
    """Types of wake word events."""

    # Detection events
    RAW_TRIGGER = "raw_trigger"  # Model score exceeded threshold
    LAYER_PASSED = "layer_passed"  # Defense layer passed
    LAYER_BLOCKED = "layer_blocked"  # Defense layer blocked
    ACCEPTED = "accepted"  # All layers passed, wake triggered

    # Error events
    AEC_REFERENCE_LOST = "aec_reference_lost"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"


@dataclass
class WakeEvent:
    """Structured wake word event."""

    event_id: str
    timestamp: float
    event_type: WakeEventType

    # Correlation ID for tracking detection flows
    correlation_id: str | None = None

    # Audio context (when applicable)
    wake_score: float | None = None
    effective_threshold: float | None = None
    base_threshold: float | None = None
    mic_rms: float | None = None
    loopback_rms: float | None = None
    post_aec_rms: float | None = None
    correlation: float | None = None
    vad_confidence: float | None = None
    snr_db: float | None = None

    # State context
    is_playback_active: bool | None = None
    playback_volume: int | None = None
    echo_gating_active: bool | None = None
    is_listening_active: bool | None = None

    # Layer info
    layer_name: str | None = None
    blocking_reason: str | None = None
    layers_passed: list[str] = field(default_factory=list)
    layers_failed: list[str] = field(default_factory=list)

    # Timing
    processing_time_ms: float | None = None

    # Additional metadata
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        """Convert to dictionary for JSON serialization."""
        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        json_value = to_json_value(payload)
        if not isinstance(json_value, dict):
            raise TypeError("WakeEvent serialization must produce an object")
        return json_value

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


@dataclass
class WakeEventSummary:
    """Summary of wake events over a time period."""

    start_time: float
    end_time: float

    # Counts
    raw_triggers: int = 0
    accepted: int = 0

    # Per-layer blocks
    blocked_by_vad: int = 0
    blocked_by_score: int = 0
    blocked_by_echo: int = 0
    blocked_by_confirmation: int = 0

    # Errors
    aec_reference_losses: int = 0

    @property
    def acceptance_rate(self) -> float:
        """Calculate acceptance rate."""
        if self.raw_triggers == 0:
            return 0.0
        return self.accepted / self.raw_triggers

    @property
    def duration_seconds(self) -> float:
        """Calculate duration."""
        return self.end_time - self.start_time


class WakeEventLogger:
    """
    Logger for wake word events with forensic capabilities.

    Features:
    - Structured JSON event logging
    - Correlation IDs for tracking detection flows
    - Ring buffer for recent events (for debugging)
    - Periodic summary generation
    - Export for analysis
    """

    RING_BUFFER_SIZE = 1000
    DEFAULT_LOG_PATH = get_logs_dir() / "wake_events.jsonl"

    def __init__(
        self,
        log_path: Path | None = None,
        enable_file_logging: bool = True,
        enable_summary: bool = True,
        summary_interval_seconds: float = 300.0,
    ):
        """
        Initialize wake event logger.

        Args:
            log_path: Path for JSON Lines log file
            enable_file_logging: Whether to write to file
            enable_summary: Whether to log periodic summaries
            summary_interval_seconds: Interval between summaries
        """
        self._log_path = log_path or self.DEFAULT_LOG_PATH
        self._enable_file = enable_file_logging
        self._enable_summary = enable_summary
        self._summary_interval = summary_interval_seconds

        self._lock = threading.RLock()
        self._ring_buffer: deque[WakeEvent] = deque(maxlen=self.RING_BUFFER_SIZE)
        self._current_correlation_id: str | None = None

        # Counters for summary
        self._raw_triggers = 0
        self._accepted = 0
        self._blocked_by_layer: dict[str, int] = {}
        self._errors: dict[str, int] = {}
        self._last_summary_time = time.time()

        # Initialize file logging
        if self._enable_file:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Wake event logging to: %s", self._log_path)

    def start_detection_flow(self) -> str:
        """
        Start a new detection flow and return correlation ID.

        Call this when wake model first fires to start tracking
        a detection through all defense layers.

        Returns:
            Correlation ID for this detection flow
        """
        with self._lock:
            self._current_correlation_id = str(uuid.uuid4())[:8]
            return self._current_correlation_id

    def end_detection_flow(self) -> None:
        """End the current detection flow."""
        with self._lock:
            self._current_correlation_id = None

    def _generate_event_id(self) -> str:
        """Generate unique event ID."""
        return f"{int(time.time() * 1000)}-{str(uuid.uuid4())[:4]}"

    def log_event(self, event: WakeEvent) -> None:
        """
        Log a wake event.

        Args:
            event: Wake event to log
        """
        with self._lock:
            # Add correlation ID if in a flow
            if event.correlation_id is None:
                event.correlation_id = self._current_correlation_id

            # Add to ring buffer
            self._ring_buffer.append(event)

            # Update counters
            self._update_counters(event)

            # Write to file
            if self._enable_file:
                try:
                    with open(self._log_path, "a") as f:
                        f.write(event.to_json() + "\n")
                except OSError as e:
                    logger.debug("Failed to write wake event: %s", e)

            # Check for summary
            if self._enable_summary:
                self._maybe_log_summary()

    def _update_counters(self, event: WakeEvent) -> None:
        """Update counters based on event type."""
        if event.event_type == WakeEventType.RAW_TRIGGER:
            self._raw_triggers += 1
        elif event.event_type == WakeEventType.ACCEPTED:
            self._accepted += 1
        elif event.event_type == WakeEventType.LAYER_BLOCKED:
            layer = event.layer_name or "unknown"
            self._blocked_by_layer[layer] = self._blocked_by_layer.get(layer, 0) + 1
        elif event.event_type in (
            WakeEventType.AEC_REFERENCE_LOST,
            WakeEventType.CIRCUIT_BREAKER_OPEN,
        ):
            error_name = event.event_type.value
            self._errors[error_name] = self._errors.get(error_name, 0) + 1

    # Convenience methods for common events

    def log_raw_trigger(
        self,
        score: float,
        threshold: float,
        mic_rms: float = 0.0,
        loopback_rms: float = 0.0,
        post_aec_rms: float = 0.0,
        correlation: float = 0.0,
        vad_confidence: float = 0.0,
        **metadata: JsonValue,
    ) -> str:
        """
        Log a raw wake trigger event.

        Returns the correlation ID for this detection flow.
        """
        correlation_id = self.start_detection_flow()

        event = WakeEvent(
            event_id=self._generate_event_id(),
            timestamp=time.time(),
            event_type=WakeEventType.RAW_TRIGGER,
            correlation_id=correlation_id,
            wake_score=score,
            base_threshold=threshold,
            mic_rms=mic_rms,
            loopback_rms=loopback_rms,
            post_aec_rms=post_aec_rms,
            correlation=correlation,
            vad_confidence=vad_confidence,
            metadata=metadata,
        )
        self.log_event(event)
        return correlation_id

    def log_layer_result(
        self,
        layer_name: str,
        passed: bool,
        reason: str | None = None,
        **context: JsonValue,
    ) -> None:
        """Log a defense layer evaluation result."""
        event = WakeEvent(
            event_id=self._generate_event_id(),
            timestamp=time.time(),
            event_type=(WakeEventType.LAYER_PASSED if passed else WakeEventType.LAYER_BLOCKED),
            layer_name=layer_name,
            blocking_reason=reason if not passed else None,
            metadata=context,
        )
        self.log_event(event)

    def log_accepted(
        self,
        score: float,
        effective_threshold: float,
        layers_passed: list[str],
        processing_time_ms: float | None = None,
        **context: JsonValue,
    ) -> None:
        """Log an accepted wake trigger."""
        event = WakeEvent(
            event_id=self._generate_event_id(),
            timestamp=time.time(),
            event_type=WakeEventType.ACCEPTED,
            wake_score=score,
            effective_threshold=effective_threshold,
            layers_passed=layers_passed,
            processing_time_ms=processing_time_ms,
            metadata=context,
        )
        self.log_event(event)
        self.end_detection_flow()

    def log_error(
        self,
        event_type: WakeEventType,
        message: str,
        **context: JsonValue,
    ) -> None:
        """Log an error event."""
        event = WakeEvent(
            event_id=self._generate_event_id(),
            timestamp=time.time(),
            event_type=event_type,
            blocking_reason=message,
            metadata=context,
        )
        self.log_event(event)

    # Query methods

    def get_recent_events(self, count: int = 100) -> list[WakeEvent]:
        """Get recent events from ring buffer."""
        with self._lock:
            return list(self._ring_buffer)[-count:]

    def get_events_by_correlation(self, correlation_id: str) -> list[WakeEvent]:
        """Get all events for a correlation ID."""
        with self._lock:
            return [e for e in self._ring_buffer if e.correlation_id == correlation_id]

    def get_events_since(self, since_timestamp: float) -> list[WakeEvent]:
        """Get events since a timestamp."""
        with self._lock:
            return [e for e in self._ring_buffer if e.timestamp >= since_timestamp]

    def get_summary(self, since: float | None = None) -> WakeEventSummary:
        """Get summary of wake events."""
        with self._lock:
            now = time.time()
            start_time = since or (now - self._summary_interval)

            summary = WakeEventSummary(
                start_time=start_time,
                end_time=now,
                raw_triggers=self._raw_triggers,
                accepted=self._accepted,
            )

            # Calculate per-layer blocks
            for layer, count in self._blocked_by_layer.items():
                if layer == "vad_gate" or layer == "baseline_vad":
                    summary.blocked_by_vad += count
                elif layer == "score_check" or layer == "primary_score":
                    summary.blocked_by_score += count
                elif layer == "echo_veto":
                    summary.blocked_by_echo += count
                elif layer == "confirmation":
                    summary.blocked_by_confirmation += count

            # Count errors
            summary.aec_reference_losses = self._errors.get("aec_reference_lost", 0)

            return summary

    # Export methods

    def export_events(
        self,
        path: Path,
        since: float | None = None,
        format: str = "json",
    ) -> int:
        """
        Export events to file.

        Args:
            path: Output path
            since: Only export events since this timestamp
            format: "json" or "jsonl"

        Returns:
            Number of events exported
        """
        with self._lock:
            events = list(self._ring_buffer)
            if since:
                events = [e for e in events if e.timestamp >= since]

            path.parent.mkdir(parents=True, exist_ok=True)

            if format == "jsonl":
                with open(path, "w") as f:
                    for event in events:
                        f.write(event.to_json() + "\n")
            else:
                with open(path, "w") as f:
                    json.dump([e.to_dict() for e in events], f, indent=2)

            return len(events)

    # Summary logging

    def _maybe_log_summary(self) -> None:
        """Log summary if interval elapsed."""
        now = time.time()
        if now - self._last_summary_time >= self._summary_interval:
            self._log_summary()
            self._last_summary_time = now

    def _log_summary(self) -> None:
        """Log periodic summary."""
        summary = self.get_summary()

        logger.info(
            "[Wake Events Summary] Raw triggers: %s | Accepted: %s (%s%%) | Blocked: VAD=%s, Score=%s, Echo=%s, Confirm=%s",
            summary.raw_triggers,
            summary.accepted,
            format(summary.acceptance_rate * 100, ".1f"),
            summary.blocked_by_vad,
            summary.blocked_by_score,
            summary.blocked_by_echo,
            summary.blocked_by_confirmation,
        )

        if summary.aec_reference_losses > 0:
            logger.warning("[Wake Events] AEC reference losses: %d", summary.aec_reference_losses)

    def reset(self) -> None:
        """Reset all counters (for testing)."""
        with self._lock:
            self._ring_buffer.clear()
            self._raw_triggers = 0
            self._accepted = 0
            self._blocked_by_layer.clear()
            self._errors.clear()


# Singleton
_logger: WakeEventLogger | None = None
_logger_lock = threading.Lock()


def get_wake_event_logger() -> WakeEventLogger:
    """Get global wake event logger."""
    global _logger
    with _logger_lock:
        if _logger is None:
            _logger = WakeEventLogger()
        return _logger


def reset_wake_event_logger() -> None:
    """Reset global wake event logger (for testing)."""
    global _logger
    with _logger_lock:
        if _logger is not None:
            _logger.reset()


__all__ = [
    "WakeEvent",
    "WakeEventLogger",
    "WakeEventSummary",
    "WakeEventType",
    "get_wake_event_logger",
    "reset_wake_event_logger",
]
