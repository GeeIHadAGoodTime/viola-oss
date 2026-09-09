"""
Ring Buffer for Debug Trace

Low-overhead in-memory event logging for debugging.
Last 1000 events kept in memory, exportable on demand.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """Debug trace event"""

    timestamp: float
    level: str
    category: str
    message: str
    data: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary"""
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "data": dict(self.data),
        }


class DebugRingBuffer:
    """
    Ring buffer for debug trace (last N events)

    Low-overhead: no disk I/O unless explicitly exported
    Thread-safe operations
    """

    def __init__(self, max_size: int = 1000) -> None:
        """
        Initialize ring buffer

        Args:
            max_size: Maximum number of events to keep
        """
        self.max_size = max_size
        self.buffer: deque[TraceEvent] = deque(maxlen=max_size)
        self._event_count = 0
        self._lock = threading.Lock()

    def add(self, level: str, category: str, message: str, **data: object) -> None:
        """
        Add event to buffer

        Args:
            level: Event level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            category: Event category (e.g., "api", "music", "ui")
            message: Event message
            **data: Additional event data
        """
        event = TraceEvent(
            timestamp=time.time(),
            level=level,
            category=category,
            message=message,
            data=dict(data),
        )
        with self._lock:
            self.buffer.append(event)
            self._event_count += 1

    def get_recent(self, count: int = 100) -> list[TraceEvent]:
        """
        Get recent events

        Args:
            count: Number of events to return

        Returns:
            List of TraceEvent objects
        """
        with self._lock:
            events = list(self.buffer)
        return events[-count:]

    def get_by_category(self, category: str, limit: int = 100) -> list[TraceEvent]:
        """
        Get events filtered by category

        Args:
            category: Category to filter by
            limit: Maximum events to return

        Returns:
            List of TraceEvent objects
        """
        with self._lock:
            filtered = [e for e in self.buffer if e.category == category]
        return filtered[-limit:] if limit else filtered

    def get_by_level(self, level: str, limit: int = 100) -> list[TraceEvent]:
        """
        Get events filtered by level

        Args:
            level: Level to filter by
            limit: Maximum events to return

        Returns:
            List of TraceEvent objects
        """
        with self._lock:
            filtered = [e for e in self.buffer if e.level == level]
        return filtered[-limit:] if limit else filtered

    def export(self) -> list[dict[str, object]]:
        """
        Export buffer as JSON-serializable list

        Returns:
            List of event dictionaries
        """
        with self._lock:
            return [event.to_dict() for event in self.buffer]

    def clear(self) -> None:
        """Clear the buffer"""
        with self._lock:
            self.buffer.clear()
            self._event_count = 0

    @property
    def size(self) -> int:
        """Current number of events in buffer"""
        with self._lock:
            return len(self.buffer)

    @property
    def total_count(self) -> int:
        """Total number of events ever added"""
        with self._lock:
            return self._event_count

    def stats(self) -> dict[str, object]:
        """Get buffer statistics"""
        with self._lock:
            events = list(self.buffer)
            total = self._event_count

        if not events:
            return {
                "size": 0,
                "total_count": total,
                "levels": {},
                "categories": {},
            }

        levels: dict[str, int] = {}
        categories: dict[str, int] = {}
        for event in events:
            levels[event.level] = levels.get(event.level, 0) + 1
            categories[event.category] = categories.get(event.category, 0) + 1

        return {
            "size": len(events),
            "total_count": total,
            "max_size": self.max_size,
            "levels": levels,
            "categories": categories,
            "oldest_timestamp": events[0].timestamp,
            "newest_timestamp": events[-1].timestamp,
        }


# Global instance
_global_ring_buffer: DebugRingBuffer | None = None
_global_lock = threading.Lock()


def get_debug_ring_buffer() -> DebugRingBuffer:
    """Get global debug ring buffer instance"""
    global _global_ring_buffer
    with _global_lock:
        if _global_ring_buffer is None:
            _global_ring_buffer = DebugRingBuffer()
        return _global_ring_buffer
