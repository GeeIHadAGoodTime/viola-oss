"""Queue operation history tracking for observability and debugging.

This module provides structured event logging for all queue/playback operations
to enable reconstruction of "why did we play X again?" without guessing.

Hardening features (P0/P1):
- Monotonic sequence numbers for total ordering
- correlation_id for playback attempt tracking (distinct from command_id)
- Active playback tracking with start validation
- Completion guard keyed by (track_id, correlation_id)
- Skip dedup mechanism for UI+backend safety
- "No repeated start" invariant detection

Usage:
    from diagnostics.queue_history import get_queue_history, log_queue_event, QueueEventType

    # Log an event with correlation_id
    log_queue_event(
        QueueEventType.ADD,
        track_id="abc123",
        title="Song Name",
        queue_pos=0,
        command_id="cmd-uuid",
        correlation_id="corr-xyz",
    )

    # Get history for debugging
    history = get_queue_history().get_recent(limit=50)
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class QueueEventType(str, Enum):
    """Types of queue/playback events for structured logging."""

    # Queue operations
    ADD = "QUEUE_ADD"
    REMOVE = "QUEUE_REMOVE"
    POP = "QUEUE_POP"
    ADVANCE = "QUEUE_ADVANCE"
    COMPLETE = "QUEUE_COMPLETE"
    CLEAR = "QUEUE_CLEAR"
    REORDER = "QUEUE_REORDER"
    RESTORE = "QUEUE_RESTORE"

    # Playback operations
    PLAY_START_REQUEST = "PLAY_START_REQUEST"
    PLAY_STARTED = "PLAY_STARTED"
    PLAY_ERROR = "PLAY_ERROR"
    PLAY_SKIP_CALLED = "PLAY_SKIP_CALLED"
    PLAY_SKIP_DEDUP = "PLAY_SKIP_DEDUP"
    PLAY_PAUSE = "PLAY_PAUSE"
    PLAY_RESUME = "PLAY_RESUME"
    PLAY_STOP = "PLAY_STOP"

    # Intent bridge operations
    INTENT_DISPATCH = "INTENT_DISPATCH"
    INTENT_RECEIVE = "INTENT_RECEIVE"
    INTENT_DUPLICATE_IGNORED = "INTENT_DUPLICATE_IGNORED"

    # Invariant violations
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    INVARIANT_DOUBLE_COMPLETE = "INVARIANT_DOUBLE_COMPLETE"
    INVARIANT_STATE_DESYNC = "INVARIANT_STATE_DESYNC"
    INVARIANT_NO_CURRENT = "INVARIANT_NO_CURRENT"
    INVARIANT_REPEATED_START = "INVARIANT_REPEATED_START"
    INVARIANT_COMPLETION_GUARD = "INVARIANT_COMPLETION_GUARD"
    INVARIANT_MISSING_CORRELATION_ID = "INVARIANT_MISSING_CORRELATION_ID"
    INVARIANT_RAPID_REPEATED_START = "INVARIANT_RAPID_REPEATED_START"


@dataclass
class QueueEvent:
    """A single queue/playback event for history tracking.

    Fields:
        seq: Monotonic sequence number for total ordering (assigned by manager)
        ts: Wall-clock timestamp
        correlation_id: Playback attempt ID (unique per start request, propagates to completion)
        command_id: User-action idempotency key (for intent dedup)
    """

    event: QueueEventType
    seq: int = 0  # Monotonic sequence, assigned by QueueHistoryManager
    ts: float = field(default_factory=time.time)
    track_id: str | None = None
    title: str | None = None
    queue_pos: int | None = None
    current_id: str | None = None
    next_id: str | None = None
    from_id: str | None = None
    to_id: str | None = None
    reason: str | None = None
    outcome: str | None = None  # success, skip, fail, timeout, cancel
    command_id: str | None = None
    correlation_id: str | None = None  # Playback attempt ID
    attempt: int | None = None
    queue_length: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d["event"] = self.event.value
        # Remove None values and zero seq for cleaner output
        return {k: v for k, v in d.items() if v is not None and not (k == "seq" and v == 0)}


@dataclass
class ActivePlaybackAttempt:
    """Tracks an active playback attempt for invariant checking."""

    track_id: str
    correlation_id: str
    start_seq: int
    start_ts: float
    start_count: int = 1  # Number of starts without completion (for rapid-start detection)


class QueueHistoryManager:
    """Thread-safe manager for queue operation history.

    Maintains a ring buffer of recent queue operations for debugging
    and observability purposes.

    Hardening features:
    - Monotonic sequence numbers for total ordering
    - Active playback tracking for repeated-start detection
    - Rapid repeated-start detection (>=3 starts without completion)
    - Completion guard keyed by (track_id, correlation_id)
    - Skip dedup to prevent UI+backend double-advance
    - Missing correlation_id detection
    """

    # Grace period before flagging repeated start (avoids duplicate logs from rapid events)
    REPEATED_START_GRACE_MS = 250
    # Threshold for rapid repeated start detection (regardless of grace period)
    RAPID_START_THRESHOLD = 3
    # Window for rapid start detection (ms)
    RAPID_START_WINDOW_MS = 5000

    def __init__(self, maxlen: int = 200) -> None:
        self._history: deque[QueueEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0  # Monotonic sequence counter

        # Legacy double-complete tracking (by track_id only)
        self._last_completed_id: str | None = None
        self._completion_count: dict[str, int] = {}  # track_id -> completion count

        # P0-1: Completion guard keyed by (track_id, correlation_id)
        self._completed_attempts: dict[str, int] = {}  # "track_id:correlation_id" -> completion_seq

        # P0-2: Skip dedup - tracks skips by (track_id, correlation_id)
        self._skip_attempts: dict[str, int] = {}  # "track_id:correlation_id" -> skip_seq

        # P0-3: Active playback tracking for repeated-start detection
        self._active_playback: ActivePlaybackAttempt | None = None
        self._last_completion_seq_by_track: dict[str, int] = {}  # track_id -> last completion seq
        self._last_start_seq_by_track: dict[str, int] = {}  # track_id -> last start seq

    def _next_seq(self) -> int:
        """Get next monotonic sequence number. Must hold lock."""
        self._seq += 1
        return self._seq

    def record(self, event: QueueEvent) -> int:
        """Record a queue event to history. Returns assigned sequence number."""
        with self._lock:
            event.seq = self._next_seq()
            self._history.append(event)
            seq = event.seq

        # Emit structured log
        log_data = event.to_dict()
        logger.info(
            "[%s] seq=%d track_id=%s correlation_id=%s command_id=%s reason=%s",
            event.event.value,
            event.seq,
            event.track_id,
            event.correlation_id,
            event.command_id,
            event.reason,
            **{
                k: v
                for k, v in log_data.items()
                if k
                not in (
                    "event",
                    "seq",
                    "track_id",
                    "correlation_id",
                    "command_id",
                    "reason",
                )
            },
        )
        return seq

    def get_recent(
        self,
        limit: int = 50,
        *,
        since_seq: int | None = None,
        track_id: str | None = None,
        command_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get most recent events as dictionaries with optional filtering."""
        with self._lock:
            events = list(self._history)

        # Apply filters
        if since_seq is not None:
            events = [e for e in events if e.seq > since_seq]
        if track_id is not None:
            events = [e for e in events if e.track_id == track_id]
        if command_id is not None:
            events = [e for e in events if e.command_id == command_id]
        if correlation_id is not None:
            events = [e for e in events if e.correlation_id == correlation_id]

        # Return last N after filtering
        return [e.to_dict() for e in events[-limit:]]

    def get_all(self) -> list[dict[str, Any]]:
        """Get all events as dictionaries."""
        with self._lock:
            return [e.to_dict() for e in self._history]

    def clear(self) -> None:
        """Clear all history and reset state."""
        with self._lock:
            self._history.clear()
            self._seq = 0
            self._last_completed_id = None
            self._completion_count.clear()
            self._completed_attempts.clear()
            self._skip_attempts.clear()
            self._active_playback = None
            self._last_completion_seq_by_track.clear()
            self._last_start_seq_by_track.clear()

    # ------------------------------------------------------------------ #
    # P0-3: Repeated Start Detection                                     #
    # ------------------------------------------------------------------ #

    def register_playback_start(self, track_id: str, correlation_id: str) -> bool:
        """Register a playback start attempt. Returns True if this is a repeated start.

        A repeated start is detected when:
        - PLAY_START_REQUEST arrives for the same track_id
        - Last completion for that track_id is before active.start_seq
        - More than REPEATED_START_GRACE_MS has passed since active start
        OR
        - >=RAPID_START_THRESHOLD starts for same track without completion within window
          (triggers regardless of grace period)

        Args:
            track_id: The track being started
            correlation_id: Unique ID for this playback attempt

        Returns:
            True if this is a repeated start (invariant violation logged)
        """
        now = time.time()
        with self._lock:
            active = self._active_playback
            is_repeated = False
            start_count = 1

            last_completion_seq = self._last_completion_seq_by_track.get(track_id, 0)
            last_start_seq = self._last_start_seq_by_track.get(track_id, 0)

            if active is not None and active.track_id == track_id:
                # Check if track was completed after the active start
                # If so, this is a legitimate new attempt - reset count
                if last_completion_seq >= active.start_seq:
                    # Completion happened after active start - this is a NEW attempt
                    start_count = 1
                else:
                    # No completion since active start - increment count
                    start_count = active.start_count + 1
                elapsed_ms = (now - active.start_ts) * 1000
                grace_passed = elapsed_ms > self.REPEATED_START_GRACE_MS
                within_rapid_window = elapsed_ms <= self.RAPID_START_WINDOW_MS

                # Check for rapid repeated starts (>=3 starts without completion)
                if start_count >= self.RAPID_START_THRESHOLD and within_rapid_window:
                    if last_completion_seq < active.start_seq:
                        is_repeated = True
                        event = QueueEvent(
                            event=QueueEventType.INVARIANT_RAPID_REPEATED_START,
                            track_id=track_id,
                            correlation_id=correlation_id,
                            reason="rapid_repeated_start_without_complete",
                            extra={
                                "active_correlation_id": active.correlation_id,
                                "active_start_seq": active.start_seq,
                                "last_start_seq": last_start_seq,
                                "last_completion_seq": last_completion_seq,
                                "start_count": start_count,
                                "elapsed_ms": elapsed_ms,
                            },
                        )
                        event.seq = self._next_seq()
                        self._history.append(event)
                        logger.warning(
                            "[INVARIANT_RAPID_REPEATED_START] track_id=%s start_count=%d corr=%s",
                            track_id,
                            start_count,
                            correlation_id,
                        )

                # Check for standard repeated start (grace period passed)
                elif last_completion_seq < active.start_seq and grace_passed:
                    is_repeated = True
                    event = QueueEvent(
                        event=QueueEventType.INVARIANT_REPEATED_START,
                        track_id=track_id,
                        correlation_id=correlation_id,
                        reason="repeated_start_without_complete",
                        extra={
                            "active_correlation_id": active.correlation_id,
                            "active_start_seq": active.start_seq,
                            "last_start_seq": last_start_seq,
                            "last_completion_seq": last_completion_seq,
                            "start_count": start_count,
                            "elapsed_ms": elapsed_ms,
                        },
                    )
                    event.seq = self._next_seq()
                    self._history.append(event)
                    logger.warning(
                        "[INVARIANT_REPEATED_START] track_id=%s new_corr=%s active_corr=%s",
                        track_id,
                        correlation_id,
                        active.correlation_id,
                    )

            # Always update active playback to the new attempt
            new_seq = self._seq + 1  # Predict next seq
            self._active_playback = ActivePlaybackAttempt(
                track_id=track_id,
                correlation_id=correlation_id,
                start_seq=new_seq,
                start_ts=now,
                start_count=start_count,
            )
            self._last_start_seq_by_track[track_id] = new_seq

        return is_repeated

    def clear_active_playback(self, track_id: str, correlation_id: str, completion_seq: int) -> None:
        """Clear active playback when completion occurs.

        Args:
            track_id: The completed track
            correlation_id: The playback attempt that completed
            completion_seq: Sequence number of the completion event
        """
        with self._lock:
            active = self._active_playback
            if active is not None:
                if active.track_id == track_id and active.correlation_id == correlation_id:
                    self._active_playback = None
            # Always update last completion seq for this track
            self._last_completion_seq_by_track[track_id] = completion_seq

    # ------------------------------------------------------------------ #
    # P0-1: Completion Guard (by track_id + correlation_id)              #
    # ------------------------------------------------------------------ #

    def log_missing_correlation_id(self, track_id: str, context: str) -> None:
        """Log an invariant event when correlation_id is missing.

        This helps identify code paths that bypass the hardened completion flow.
        """
        with self._lock:
            event = QueueEvent(
                event=QueueEventType.INVARIANT_MISSING_CORRELATION_ID,
                track_id=track_id,
                reason=f"missing_correlation_id_{context}",
                extra={"context": context},
            )
            event.seq = self._next_seq()
            self._history.append(event)
        logger.warning(
            "[INVARIANT_MISSING_CORRELATION_ID] track_id=%s context=%s",
            track_id,
            context,
        )

    def check_completion_guard(self, track_id: str, correlation_id: str | None) -> bool:
        """Check if this (track_id, correlation_id) has already been completed.

        Returns True if already completed (guard triggered, should not advance).
        If correlation_id is None, logs INVARIANT_MISSING_CORRELATION_ID and uses
        a None-keyed entry in _completed_attempts (does NOT fall back to
        check_double_complete, which would poison _last_completed_id before
        complete_current() gets a chance to check it).
        """
        effective_corr = correlation_id
        if effective_corr is None:
            # Log missing correlation_id for observability
            self.log_missing_correlation_id(track_id, "completion")
            effective_corr = "__no_corr__"

        key = f"{track_id}:{effective_corr}"
        with self._lock:
            if key in self._completed_attempts:
                prev_seq = self._completed_attempts[key]
                event = QueueEvent(
                    event=QueueEventType.INVARIANT_COMPLETION_GUARD,
                    track_id=track_id,
                    correlation_id=correlation_id,
                    reason="completion_guard_triggered",
                    extra={"previous_completion_seq": prev_seq},
                )
                event.seq = self._next_seq()
                self._history.append(event)
                logger.warning(
                    "[INVARIANT_COMPLETION_GUARD] track_id=%s correlation_id=%s prev_seq=%d",
                    track_id,
                    correlation_id,
                    prev_seq,
                )
                return True

            # Mark as completed in _completed_attempts only.
            # DO NOT update _last_completed_id here — that is owned exclusively
            # by check_double_complete() inside complete_current().  Setting it
            # here would poison the double-complete check and prevent queue
            # advancement on every single track completion.
            self._completed_attempts[key] = self._seq + 1
            return False

    def check_double_complete(self, track_id: str) -> bool:
        """Check if we're trying to complete the same track twice (legacy).

        Returns True if this would be a double completion (invariant violation).
        """
        with self._lock:
            if self._last_completed_id == track_id:
                count = self._completion_count.get(track_id, 0) + 1
                self._completion_count[track_id] = count

                event = QueueEvent(
                    event=QueueEventType.INVARIANT_DOUBLE_COMPLETE,
                    track_id=track_id,
                    reason=f"Double completion attempt #{count}",
                    extra={"completion_count": count},
                )
                event.seq = self._next_seq()
                self._history.append(event)
                return True

            # Track this completion
            self._last_completed_id = track_id
            self._completion_count[track_id] = self._completion_count.get(track_id, 0) + 1
            return False

    # ------------------------------------------------------------------ #
    # P0-2: Skip Dedup (UI + Backend Safety)                             #
    # ------------------------------------------------------------------ #

    def check_skip_dedup(self, track_id: str, correlation_id: str | None) -> bool:
        """Check if skip for this (track_id, correlation_id) was already processed.

        Returns True if skip was already processed (duplicate, should be no-op).
        """
        if correlation_id is None:
            return False  # Cannot dedup without correlation_id

        key = f"{track_id}:{correlation_id}"
        with self._lock:
            if key in self._skip_attempts:
                prev_seq = self._skip_attempts[key]
                event = QueueEvent(
                    event=QueueEventType.PLAY_SKIP_DEDUP,
                    track_id=track_id,
                    correlation_id=correlation_id,
                    reason="skip_dedup_triggered",
                    extra={"previous_skip_seq": prev_seq},
                )
                event.seq = self._next_seq()
                self._history.append(event)
                logger.debug(
                    "[PLAY_SKIP_DEDUP] track_id=%s correlation_id=%s prev_seq=%d",
                    track_id,
                    correlation_id,
                    prev_seq,
                )
                return True

            # Mark skip as processed
            self._skip_attempts[key] = self._seq + 1
            return False

    # ------------------------------------------------------------------ #
    # Invariant Recording                                                #
    # ------------------------------------------------------------------ #

    def record_invariant_violation(
        self,
        violation_type: str,
        *,
        track_id: str | None = None,
        correlation_id: str | None = None,
        current_id: str | None = None,
        expected: Any = None,
        actual: Any = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Record an invariant violation for debugging."""
        event = QueueEvent(
            event=QueueEventType.INVARIANT_VIOLATION,
            track_id=track_id,
            correlation_id=correlation_id,
            current_id=current_id,
            reason=violation_type,
            extra={
                "expected": expected,
                "actual": actual,
                **(context or {}),
            },
        )
        self.record(event)
        logger.warning(
            "[INVARIANT] %s track_id=%s expected=%s actual=%s",
            violation_type,
            track_id,
            expected,
            actual,
        )

    def get_state_snapshot(self) -> dict[str, Any]:
        """Get current state for debugging."""
        with self._lock:
            active = self._active_playback
            return {
                "history_length": len(self._history),
                "current_seq": self._seq,
                "last_completed_id": self._last_completed_id,
                "recent_completions": dict(self._completion_count),
                "completed_attempts_count": len(self._completed_attempts),
                "skip_attempts_count": len(self._skip_attempts),
                "last_start_seq_by_track": dict(self._last_start_seq_by_track),
                "last_completion_seq_by_track": dict(self._last_completion_seq_by_track),
                "active_playback": (
                    {
                        "track_id": active.track_id,
                        "correlation_id": active.correlation_id,
                        "start_seq": active.start_seq,
                        "start_count": active.start_count,
                    }
                    if active
                    else None
                ),
            }

    def get_current_seq(self) -> int:
        """Get current sequence number."""
        with self._lock:
            return self._seq


# Global singleton instance
_queue_history: QueueHistoryManager | None = None
_history_lock = threading.Lock()


def get_queue_history() -> QueueHistoryManager:
    """Get the global queue history manager instance."""
    global _queue_history
    if _queue_history is None:
        with _history_lock:
            if _queue_history is None:
                _queue_history = QueueHistoryManager(maxlen=200)
    return _queue_history


def new_command_id() -> str:
    """Generate a new command ID for user-action idempotency."""
    return f"cmd-{uuid.uuid4().hex[:12]}"


def new_correlation_id() -> str:
    """Generate a new correlation ID for playback attempt tracking.

    Unlike command_id (which is stable per user action for dedup),
    correlation_id is unique per playback attempt and propagates
    from PLAY_START_REQUEST through to QUEUE_COMPLETE.
    """
    return f"corr-{uuid.uuid4().hex[:12]}"


def log_queue_event(
    event_type: QueueEventType,
    *,
    track_id: str | None = None,
    title: str | None = None,
    queue_pos: int | None = None,
    current_id: str | None = None,
    next_id: str | None = None,
    from_id: str | None = None,
    to_id: str | None = None,
    reason: str | None = None,
    outcome: str | None = None,
    command_id: str | None = None,
    correlation_id: str | None = None,
    attempt: int | None = None,
    queue_length: int | None = None,
    **extra: Any,
) -> int:
    """Log a queue event with structured fields.

    This is the primary interface for recording queue/playback events.
    All fields are optional except event_type.

    Args:
        event_type: Type of event (from QueueEventType enum)
        track_id: ID of the track involved
        title: Track title for readability
        queue_pos: Position in queue
        current_id: Current track ID
        next_id: Next track ID
        from_id: Source track for transitions
        to_id: Destination track for transitions
        reason: Human-readable reason
        outcome: success/fail/skip/timeout/cancel
        command_id: User-action idempotency key (stable per user action)
        correlation_id: Playback attempt ID (unique per start, propagates to completion)
        attempt: Retry attempt number
        queue_length: Current queue length

    Returns:
        Assigned sequence number for the event
    """
    event = QueueEvent(
        event=event_type,
        track_id=track_id,
        title=title,
        queue_pos=queue_pos,
        current_id=current_id,
        next_id=next_id,
        from_id=from_id,
        to_id=to_id,
        reason=reason,
        outcome=outcome,
        command_id=command_id,
        correlation_id=correlation_id,
        attempt=attempt,
        queue_length=queue_length,
        extra=extra if extra else {},
    )
    return get_queue_history().record(event)


def check_invariant(
    condition: bool,
    violation_type: str,
    *,
    track_id: str | None = None,
    correlation_id: str | None = None,
    current_id: str | None = None,
    expected: Any = None,
    actual: Any = None,
    context: dict[str, Any] | None = None,
) -> bool:
    """Check an invariant and record violation if false.

    Returns the condition value (True if invariant holds, False if violated).
    """
    if not condition:
        get_queue_history().record_invariant_violation(
            violation_type,
            track_id=track_id,
            correlation_id=correlation_id,
            current_id=current_id,
            expected=expected,
            actual=actual,
            context=context,
        )
    return condition


__all__ = [
    "ActivePlaybackAttempt",
    "QueueEvent",
    "QueueEventType",
    "QueueHistoryManager",
    "check_invariant",
    "get_queue_history",
    "log_queue_event",
    "new_command_id",
    "new_correlation_id",
]
