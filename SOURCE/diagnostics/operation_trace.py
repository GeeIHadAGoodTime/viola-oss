"""
diagnostics/operation_trace.py
==============================

Operation-level tracing for music playback and voice pipeline state transitions.
Captures key state changes for AI debugging without excessive logging.

Use sparingly - only for key state transitions:
- Playback: play, pause, resume, skip, stop
- Queue: enqueue, remove, clear, reorder
- Backend: start, stop, restart, crash
- Voice: wake_detected, listening_start, transcription_complete
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class OperationType(str, Enum):
    """Categories of traced operations."""

    PLAYBACK = "playback"
    QUEUE = "queue"
    BACKEND = "backend"
    VOICE = "voice"
    SKILL = "skill"


@dataclass
class OperationTrace:
    """Record of a significant operation."""

    timestamp: float
    operation_type: OperationType
    operation: str  # e.g., "play", "pause", "skip", "backend_start"
    success: bool
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    error: str | None = None
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp,
            "type": self.operation_type.value,
            "operation": self.operation,
            "success": self.success,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


# Operation trace buffer - stores recent operations
_OPERATION_TRACE_BUFFER: deque[OperationTrace] = deque(maxlen=100)
_TRACE_LOCK = threading.Lock()


def _normalize_user_id(user_id: str | None) -> str | None:
    if user_id is None:
        return None
    normalized = user_id.strip()
    if not normalized or normalized.lower() == "default":
        raise ValueError("Operation trace user_id must be non-empty and not default")
    return normalized


def _resolve_operation_user_id(user_id: str | None) -> str | None:
    if user_id is not None:
        return _normalize_user_id(user_id)
    try:
        from core.user_context import get_current_user_id

        return _normalize_user_id(get_current_user_id())
    except LookupError:
        return None


def record_operation(
    operation_type: OperationType,
    operation: str,
    success: bool,
    *,
    details: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
    user_id: str | None = None,
) -> None:
    """
    Record a significant operation for debugging.

    Use sparingly - only for key state transitions that would be
    helpful when debugging why something isn't working.

    Args:
        operation_type: Category of operation (PLAYBACK, QUEUE, BACKEND, VOICE, SKILL)
        operation: Specific operation name (e.g., "play", "skip", "backend_start")
        success: Whether the operation succeeded
        details: Additional context (track_id, confidence, etc.)
        duration_ms: How long the operation took
        error: Error message if operation failed
    """
    trace_user_id = _resolve_operation_user_id(user_id)
    trace = OperationTrace(
        timestamp=time.time(),
        operation_type=operation_type,
        operation=operation,
        success=success,
        details=details or {},
        duration_ms=duration_ms,
        error=error[:500] if error else None,
        user_id=trace_user_id,
    )

    with _TRACE_LOCK:
        _OPERATION_TRACE_BUFFER.append(trace)

    # Also log at appropriate level for structured logs
    if success:
        logger.debug(
            "Operation: %s.%s",
            operation_type.value,
            operation,
            extra={"operation_trace": trace.to_dict()},
        )
    else:
        logger.warning(
            "Operation failed: %s.%s - %s",
            operation_type.value,
            operation,
            error,
            extra={"operation_trace": trace.to_dict()},
        )


def get_recent_operations(
    limit: int = 50,
    operation_type: OperationType | None = None,
    success_only: bool = False,
    failures_only: bool = False,
    user_id: str | None = None,
) -> list[OperationTrace]:
    """
    Get recent operation traces.

    Args:
        limit: Maximum number of traces to return
        operation_type: Filter by operation type
        success_only: Only return successful operations
        failures_only: Only return failed operations
        user_id: Only return traces captured for this authenticated user

    Returns:
        List of OperationTrace objects
    """
    with _TRACE_LOCK:
        traces = list(_OPERATION_TRACE_BUFFER)

    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is not None:
        traces = [t for t in traces if t.user_id == normalized_user_id]

    if operation_type:
        traces = [t for t in traces if t.operation_type == operation_type]

    if success_only:
        traces = [t for t in traces if t.success]

    if failures_only:
        traces = [t for t in traces if not t.success]

    return traces[-limit:]


def get_operation_summary(user_id: str | None = None) -> dict[str, Any]:
    """
    Get a summary of recent operations for quick diagnostics.

    Returns:
        Dictionary with operation counts and recent failures
    """
    with _TRACE_LOCK:
        traces = list(_OPERATION_TRACE_BUFFER)

    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is not None:
        traces = [t for t in traces if t.user_id == normalized_user_id]

    if not traces:
        return {"total": 0, "by_type": {}, "recent_failures": []}

    # Count by type and success
    by_type: dict[str, dict[str, int]] = {}
    for trace in traces:
        type_name = trace.operation_type.value
        if type_name not in by_type:
            by_type[type_name] = {"success": 0, "failure": 0}
        if trace.success:
            by_type[type_name]["success"] += 1
        else:
            by_type[type_name]["failure"] += 1

    # Get recent failures
    recent_failures = [
        {
            "type": t.operation_type.value,
            "operation": t.operation,
            "error": t.error,
            "timestamp": t.timestamp,
        }
        for t in traces[-20:]
        if not t.success
    ]

    return {
        "total": len(traces),
        "by_type": by_type,
        "recent_failures": recent_failures[-5:],
    }


def clear_traces(user_id: str | None = None) -> None:
    """Clear all operation traces. Useful for testing."""
    normalized_user_id = _normalize_user_id(user_id)
    with _TRACE_LOCK:
        if normalized_user_id is None:
            _OPERATION_TRACE_BUFFER.clear()
            return
        kept = [trace for trace in _OPERATION_TRACE_BUFFER if trace.user_id != normalized_user_id]
        _OPERATION_TRACE_BUFFER.clear()
        _OPERATION_TRACE_BUFFER.extend(kept)


__all__ = [
    "OperationTrace",
    "OperationType",
    "clear_traces",
    "get_operation_summary",
    "get_recent_operations",
    "record_operation",
]
