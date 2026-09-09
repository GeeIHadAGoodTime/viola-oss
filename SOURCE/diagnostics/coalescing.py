"""
diagnostics/coalescing.py
=========================

Time-window coalescing mechanism for failure envelopes. Groups repeated identical
failures within a configurable time window to prevent error spam while preserving
all metrics and diagnostics data.

Design:
- Failures are grouped by (component, failure_code, key_context) tuple
- First failure in a window is fully emitted
- Subsequent failures in the same window update aggregation counters and last-seen timestamp
- User-facing logs are suppressed for coalesced failures (but metrics still updated)
- Thread-safe for concurrent emission
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class CoalescedFailure:
    """Tracks aggregated failure information within a coalescing window."""

    component: str
    code: str
    key_context: str
    first_seen: float
    last_seen: float
    count: int = 1
    first_envelope: dict[str, Any] | None = None


class FailureCoalescer:
    """
    Thread-safe coalescing cache for failure envelopes.

    Groups failures by (component, code, key_context) within a configurable time window.
    The first failure in a window triggers full emission; subsequent ones are aggregated.
    """

    # Maximum number of distinct failure keys tracked simultaneously.
    # Prevents unbounded memory growth during failure storms with many
    # unique (component, code, context) combinations.
    _MAX_CACHE_SIZE: int = 500

    def __init__(self, window_seconds: float = 30.0):
        """
        Initialize coalescer with a time window.

        Args:
            window_seconds: Time window in seconds for coalescing. Default 30s.
                          Can be tuned based on requirements (PRD suggests 500ms minimum
                          for user-facing messages, but 30s is reasonable for diagnostics).
        """
        self._window_seconds = window_seconds
        self._cache: dict[str, CoalescedFailure] = {}
        self._lock = threading.Lock()
        self._cleanup_interval = window_seconds * 2  # Clean up stale entries periodically
        self._last_cleanup = time.time()

    def _make_key(self, component: str, code: str, key_context: dict[str, Any] | None = None) -> str:
        """
        Generate a stable cache key from component, code, and context.

        The key_context should contain only stable identifiers that define
        the "same" error (e.g., provider name, error type), not transient
        data like timestamps or correlation IDs.
        """
        # Normalize component and code
        parts = [component, code]

        # Include relevant context fields if provided
        if key_context:
            # Sort context keys for stable hashing
            sorted_items = sorted(key_context.items())
            # Convert to a stable string representation
            context_str = "|".join(f"{k}={v}" for k, v in sorted_items if v is not None)
            if context_str:
                parts.append(context_str)

        key_str = "||".join(parts)
        # Use hash for shorter keys (collision risk is acceptable here)
        return hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()  # nosec B324

    def _extract_key_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """
        Extract stable context fields that identify the error type.

        Filters out transient fields like correlation_id, timestamps, etc.
        Keeps stable identifiers like provider_name, error_type, etc.
        """
        # Fields to include in key context (stable identifiers)
        stable_fields = {
            "provider_name",
            "error_type",
            "exception_type",
            "tier",
            # Add other stable identifiers as needed
        }

        key_context = {}
        for field in stable_fields:
            if field in context:
                key_context[field] = context[field]

        # Also include any context keys that start with "key_" as a convention
        for k, v in context.items():
            if k.startswith("key_") or k.startswith("stable_"):
                key_context[k] = v

        return key_context

    def _cleanup_stale_entries(self, now: float) -> None:
        """Remove entries older than the cleanup interval and enforce max size."""
        if now - self._last_cleanup < self._cleanup_interval:
            # Still enforce hard size limit even between cleanup intervals
            if len(self._cache) > self._MAX_CACHE_SIZE:
                self._evict_oldest()
            return

        cutoff = now - self._cleanup_interval
        stale_keys = [key for key, entry in self._cache.items() if entry.last_seen < cutoff]
        for key in stale_keys:
            self._cache.pop(key, None)

        # Enforce max size after TTL cleanup
        if len(self._cache) > self._MAX_CACHE_SIZE:
            self._evict_oldest()

        self._last_cleanup = now

    def _evict_oldest(self) -> None:
        """Evict oldest entries to stay within _MAX_CACHE_SIZE (lock must be held)."""
        overage = len(self._cache) - self._MAX_CACHE_SIZE
        if overage <= 0:
            return
        # Sort by last_seen ascending, evict the oldest
        sorted_keys = sorted(
            self._cache.keys(),
            key=lambda k: self._cache[k].last_seen,
        )
        for key in sorted_keys[:overage]:
            self._cache.pop(key, None)

    def should_coalesce(
        self,
        component: str,
        code: str,
        context: dict[str, Any],
        now: float | None = None,
    ) -> tuple[bool, CoalescedFailure | None]:
        """
        Check if a failure should be coalesced.

        Args:
            component: Component name
            code: Failure code
            context: Full context dict
            now: Current timestamp (for testing). Defaults to time.time()

        Returns:
            Tuple of (should_coalesce, existing_entry)
            - If should_coalesce is True, the entry should be aggregated
            - If False, it's outside the window and should be emitted normally
        """
        if now is None:
            now = time.time()

        key_context = self._extract_key_context(context)
        cache_key = self._make_key(component, code, key_context)

        with self._lock:
            self._cleanup_stale_entries(now)

            existing = self._cache.get(cache_key)
            if existing is None:
                # First occurrence - create entry but don't coalesce (emit fully)
                return False, None

            # Check if within window
            if now - existing.first_seen <= self._window_seconds:
                # Within window - coalesce
                return True, existing
            else:
                # Outside window - remove stale entry and emit normally
                self._cache.pop(cache_key, None)
                return False, None

    def record_coalesced(
        self,
        component: str,
        code: str,
        context: dict[str, Any],
        envelope: dict[str, Any],
        now: float | None = None,
    ) -> CoalescedFailure:
        """
        Record a coalesced failure (updates counter and timestamp).

        Args:
            component: Component name
            code: Failure code
            context: Full context dict
            envelope: The failure envelope dict
            now: Current timestamp (for testing)

        Returns:
            Updated CoalescedFailure entry
        """
        if now is None:
            now = time.time()

        key_context = self._extract_key_context(context)
        cache_key = self._make_key(component, code, key_context)

        with self._lock:
            existing = self._cache.get(cache_key)
            if existing is None:
                # First occurrence - create entry
                existing = CoalescedFailure(
                    component=component,
                    code=code,
                    key_context=str(key_context),
                    first_seen=now,
                    last_seen=now,
                    count=1,
                    first_envelope=envelope.copy(),
                )
                self._cache[cache_key] = existing
            else:
                # Update existing entry
                existing.last_seen = now
                existing.count += 1

            return existing

    def reset(self) -> None:
        """Clear all cached entries (useful for testing)."""
        with self._lock:
            self._cache.clear()
            self._last_cleanup = time.time()


# Global singleton coalescer instance
_GLOBAL_COALESCER: FailureCoalescer | None = None
_COALESCER_LOCK = threading.Lock()


def get_coalescer(window_seconds: float | None = None) -> FailureCoalescer:
    """
    Get the global coalescer instance.

    Args:
        window_seconds: Optional window size. Only used on first call.

    Returns:
        Global FailureCoalescer instance
    """
    global _GLOBAL_COALESCER

    with _COALESCER_LOCK:
        if _GLOBAL_COALESCER is None:
            # Allow override via settings
            from config.settings import settings

            if settings.coalesce_window_seconds != 30.0:
                window_seconds = settings.coalesce_window_seconds

            _GLOBAL_COALESCER = FailureCoalescer(window_seconds=window_seconds or 30.0)
        return _GLOBAL_COALESCER


__all__ = ["CoalescedFailure", "FailureCoalescer", "get_coalescer"]
