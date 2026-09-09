"""
diagnostics/error_registry.py
=============================

Centralized error tracking with pattern detection for AI debugging.

This module aggregates errors across all subsystems, detects patterns
(repeated similar errors), and generates actionable recommendations.

Features:
- Error deduplication by (component, code, context_hash)
- Pattern detection: 3+ similar errors within 1 hour = pattern
- Auto-generated recommendations for known error patterns
- Integration with AIDebugPayload for diagnostic clients

Usage:
    from diagnostics.error_registry import get_error_registry

    # Record an error
    registry = get_error_registry()
    registry.record_error(
        component="playback.vlc",
        code="stream_failed",
        category=ErrorCategory.EXPECTED_NETWORK,
        message="Connection reset by peer",
    )

    # Get active patterns for diagnostics
    patterns = registry.get_active_patterns()
    summary = registry.get_summary()
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from diagnostics.error_classification import ErrorCategory, compute_error_context_hash

logger = get_logger(__name__)


@dataclass
class ErrorRecord:
    """Single occurrence of an error."""

    timestamp: float
    component: str
    code: str
    category: ErrorCategory
    message: str
    exception_type: str | None
    context_hash: str
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "component": self.component,
            "code": self.code,
            "category": self.category.name,
            "message": self.message[:200],
            "exception_type": self.exception_type,
            "context_hash": self.context_hash,
            "correlation_id": self.correlation_id,
        }


@dataclass
class ErrorPattern:
    """Detected pattern of repeated errors."""

    pattern_id: str
    component: str
    code: str
    first_seen: float
    last_seen: float
    count: int
    sample_messages: list[str] = field(default_factory=list)
    category: ErrorCategory = ErrorCategory.UNEXPECTED_BUG
    recommendation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        age_seconds = time.time() - self.last_seen
        return {
            "pattern_id": self.pattern_id,
            "component": self.component,
            "code": self.code,
            "count": self.count,
            "category": self.category.name,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_seen_ago_s": round(age_seconds, 1),
            "sample_messages": self.sample_messages[:3],
            "recommendation": self.recommendation,
        }


# Known error patterns and their recommendations
_PATTERN_RECOMMENDATIONS: dict[tuple[str, str], str] = {
    # Playback
    (
        "playback",
        "stream_failed",
    ): "Check network connectivity and YouTube availability",
    (
        "playback",
        "resolution_failed",
    ): "Verify YouTube API key and quota. Check embedded player status.",
    (
        "playback",
        "backend_crashed",
    ): "Check VLC installation and audio device configuration",
    ("playback", "audio_device_error"): "Verify audio output device and driver",
    # LLM
    ("llm", "api_error"): "Check LLM API key configuration and quota",
    ("llm", "rate_limited"): "Reduce request frequency or upgrade API tier",
    ("llm", "timeout"): "Check network latency; consider increasing timeout",
    # Voice
    ("voice", "wake_timeout"): "Check microphone permissions and audio device",
    ("voice", "stt_failed"): "Verify microphone input and reduce background noise",
    (
        "voice",
        "transcription_empty",
    ): "Speak more clearly or check microphone sensitivity",
    # Queue
    ("queue", "add_failed"): "Check track resolution and backend health",
    ("queue", "overflow"): "Queue limit reached; consider clearing old entries",
    # YouTube
    ("youtube", "auth_failed"): "Say 'connect youtube' to re-link YouTube Music",
    ("youtube", "quota_exceeded"): "YouTube API quota exceeded; wait for reset",
    # WebSocket
    ("websocket", "send_failed"): "Client disconnection; usually harmless if transient",
    # Config
    ("config", "load_failed"): "Check settings file format and permissions",
}


class ErrorRegistry:
    """
    Centralized error tracking with pattern detection.

    Thread-safe registry that:
    - Buffers recent errors (default: 500)
    - Detects patterns when 3+ similar errors occur within time window
    - Generates recommendations for known patterns
    - Provides summary for AIDebugPayload integration
    """

    def __init__(
        self,
        max_records: int = 500,
        pattern_threshold: int = 3,
        pattern_window_hours: float = 1.0,
    ):
        self._records: deque[ErrorRecord] = deque(maxlen=max_records)
        self._patterns: dict[str, ErrorPattern] = {}
        self._pattern_threshold = pattern_threshold
        self._pattern_window_seconds = pattern_window_hours * 3600
        self._lock = threading.RLock()
        self._max_sample_messages = 3

    def record_error(
        self,
        component: str,
        code: str,
        category: ErrorCategory,
        message: str,
        *,
        exception_type: str | None = None,
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> ErrorRecord:
        """
        Record an error and update pattern detection.

        Args:
            component: Component name (e.g., "playback.vlc").
            code: Error code (e.g., "stream_failed").
            category: Error category for classification.
            message: Human-readable error message.
            exception_type: Optional exception class name.
            context: Optional context for deduplication.
            correlation_id: Optional request correlation ID.

        Returns:
            The recorded ErrorRecord.
        """
        context_hash = compute_error_context_hash(component, code, context)

        record = ErrorRecord(
            timestamp=time.time(),
            component=component,
            code=code,
            category=category,
            message=message[:500] if message else "",
            exception_type=exception_type,
            context_hash=context_hash,
            correlation_id=correlation_id,
        )

        with self._lock:
            self._records.append(record)
            self._update_patterns(record)

        return record

    def _update_patterns(self, record: ErrorRecord) -> None:
        """Update pattern detection based on new record."""
        pattern_key = f"{record.component}:{record.code}:{record.context_hash}"
        now = time.time()
        cutoff = now - self._pattern_window_seconds

        if pattern_key in self._patterns:
            # Update existing pattern
            pattern = self._patterns[pattern_key]
            pattern.count += 1
            pattern.last_seen = record.timestamp

            # Add sample message if unique and under limit
            if (
                len(pattern.sample_messages) < self._max_sample_messages
                and record.message not in pattern.sample_messages
            ):
                pattern.sample_messages.append(record.message)
        else:
            # Count recent occurrences with same pattern key
            recent_count = sum(
                1
                for r in self._records
                if (
                    r.component == record.component
                    and r.code == record.code
                    and r.context_hash == record.context_hash
                    and r.timestamp >= cutoff
                )
            )

            if recent_count >= self._pattern_threshold:
                # Create new pattern
                self._patterns[pattern_key] = ErrorPattern(
                    pattern_id=pattern_key,
                    component=record.component,
                    code=record.code,
                    first_seen=record.timestamp,
                    last_seen=record.timestamp,
                    count=recent_count,
                    sample_messages=[record.message] if record.message else [],
                    category=record.category,
                    recommendation=self._generate_recommendation(record),
                )
                logger.info(
                    "Error pattern detected: %s[%s] (%s occurrences)",
                    record.component,
                    record.code,
                    recent_count,
                    pattern_key=pattern_key,
                    count=recent_count,
                )

    def _generate_recommendation(self, record: ErrorRecord) -> str | None:
        """Generate actionable recommendation based on error type."""
        # Extract base component (e.g., "playback" from "playback.vlc")
        base_component = record.component.split(".")[0]

        # Check for exact match first
        if (base_component, record.code) in _PATTERN_RECOMMENDATIONS:
            return _PATTERN_RECOMMENDATIONS[(base_component, record.code)]

        # Check for partial code match
        for (comp, code), rec in _PATTERN_RECOMMENDATIONS.items():
            if comp == base_component and code in record.code:
                return rec

        # Default recommendations based on category
        category_defaults = {
            ErrorCategory.EXPECTED_TIMEOUT: "Check network connectivity and service availability",
            ErrorCategory.EXPECTED_RATE_LIMIT: "Reduce request frequency or wait for quota reset",
            ErrorCategory.UNEXPECTED_CONFIG: "Review configuration settings and environment variables",
            ErrorCategory.UNEXPECTED_DEPENDENCY: "Check module imports and package versions",
            ErrorCategory.UNEXPECTED_SYSTEM: "Check system resources and permissions",
        }

        return category_defaults.get(record.category)

    def get_active_patterns(
        self,
        max_age_hours: float = 1.0,
        min_count: int = 1,
    ) -> list[ErrorPattern]:
        """
        Get patterns active within the time window.

        Args:
            max_age_hours: Maximum age of patterns to return.
            min_count: Minimum occurrence count to include.

        Returns:
            List of active ErrorPatterns, sorted by count descending.
        """
        cutoff = time.time() - (max_age_hours * 3600)

        with self._lock:
            active = [p for p in self._patterns.values() if p.last_seen >= cutoff and p.count >= min_count]

        # Sort by count (most frequent first)
        return sorted(active, key=lambda p: -p.count)

    def get_recent_errors(self, limit: int = 20) -> list[ErrorRecord]:
        """Get most recent error records."""
        with self._lock:
            records = list(self._records)
        return records[-limit:]

    def get_summary(self) -> dict[str, Any]:
        """
        Get summary for AIDebugPayload integration.

        Returns dict with:
        - total_errors_tracked: Total errors in buffer
        - active_patterns: Count of active patterns
        - patterns: List of pattern dicts (top 10)
        - by_category: Count breakdown by category
        - by_component: Count breakdown by component
        """
        patterns = self.get_active_patterns()

        with self._lock:
            records = list(self._records)

        # Count by category
        by_category: dict[str, int] = {}
        for r in records:
            cat_name = r.category.name
            by_category[cat_name] = by_category.get(cat_name, 0) + 1

        # Count by component
        by_component: dict[str, int] = {}
        for r in records:
            base_comp = r.component.split(".")[0]
            by_component[base_comp] = by_component.get(base_comp, 0) + 1

        # Count unexpected vs expected
        unexpected_count = sum(count for cat, count in by_category.items() if cat.startswith("UNEXPECTED_"))
        expected_count = sum(count for cat, count in by_category.items() if cat.startswith("EXPECTED_"))

        return {
            "total_errors_tracked": len(records),
            "active_patterns": len(patterns),
            "unexpected_errors": unexpected_count,
            "expected_errors": expected_count,
            "patterns": [p.to_dict() for p in patterns[:10]],
            "by_category": by_category,
            "by_component": by_component,
        }

    def clear_old_patterns(self, max_age_hours: float = 24.0) -> int:
        """
        Clear patterns older than the specified age.

        Returns the number of patterns cleared.
        """
        cutoff = time.time() - (max_age_hours * 3600)
        cleared = 0

        with self._lock:
            old_keys = [key for key, p in self._patterns.items() if p.last_seen < cutoff]
            for key in old_keys:
                del self._patterns[key]
                cleared += 1

        if cleared:
            logger.debug("Cleared %d old error patterns", cleared)

        return cleared


# Global singleton instance
_GLOBAL_REGISTRY: ErrorRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_error_registry() -> ErrorRegistry:
    """
    Get the global ErrorRegistry singleton.

    Thread-safe lazy initialization.
    """
    global _GLOBAL_REGISTRY

    if _GLOBAL_REGISTRY is None:
        with _REGISTRY_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = ErrorRegistry()

    return _GLOBAL_REGISTRY


__all__ = [
    "ErrorPattern",
    "ErrorRecord",
    "ErrorRegistry",
    "get_error_registry",
]
