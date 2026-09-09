"""
music/caching/memory.py

In-memory LRU cache implementation.
Fast but non-persistent - resets on restart.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from core.logging_config import get_logger

from .base import CacheEntry, ResolutionCache

logger = get_logger(__name__)


class MemoryCache(ResolutionCache):
    """
    Thread-safe in-memory LRU cache.

    Features:
    - Configurable size (default 100)
    - LRU eviction policy
    - Automatic expiration checking
    - Thread-safe operations
    """

    def __init__(self, max_size: int = 100, max_age_seconds: float = 6 * 3600):
        """
        Initialize memory cache.

        Args:
            max_size: Maximum number of entries (default 100)
            max_age_seconds: Maximum age before expiration (default 6 hours)
        """
        self._cache: dict[str, CacheEntry] = {}
        self._access_order: deque[str] = deque()
        self._lock = threading.RLock()
        self._max_size = max_size
        self._max_age = max_age_seconds

        # Stats
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> CacheEntry | None:
        """Get entry from cache, checking expiration and updating LRU order"""
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return None

            # Check expiration
            if entry.is_expired(self._max_age):
                # Expired - remove and return miss
                self._remove(key)
                self._misses += 1
                return None

            # Valid hit - update LRU order
            self._hits += 1
            self._move_to_end(key)
            return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        """Store entry in cache, evicting oldest if necessary"""
        with self._lock:
            # If key exists, remove from old position
            if key in self._cache:
                self._access_order.remove(key)

            # Add entry
            self._cache[key] = entry
            self._access_order.append(key)

            # Evict oldest if over capacity
            while len(self._access_order) > self._max_size:
                oldest_key = self._access_order.popleft()
                self._cache.pop(oldest_key, None)
                self._evictions += 1

    def clear(self) -> None:
        """Clear all entries"""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def size(self) -> int:
        """Get current cache size"""
        with self._lock:
            return len(self._cache)

    def invalidate(self, key: str) -> bool:
        """Invalidate specific entry"""
        with self._lock:
            if key in self._cache:
                self._remove(key)
                return True
            return False

    def stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0

            return {
                "type": "MemoryCache",
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "evictions": self._evictions,
                "max_age_hours": self._max_age / 3600,
            }

    def invalidate_provider(self, provider: str) -> int:
        """
        Invalidate all entries for provider.

        Returns:
            Number of entries removed.
        """
        with self._lock:
            normalized = provider.lower()
            keys = [
                key for key, entry in self._cache.items() if entry.provider and entry.provider.lower() == normalized
            ]
            for key in keys:
                self._remove(key)
            return len(keys)

    def _remove(self, key: str) -> None:
        """Remove entry (must be called with lock held)"""
        self._cache.pop(key, None)
        try:
            self._access_order.remove(key)
        except ValueError as e:
            logger.exception("Failed to remove key from access order: %s", e)

    def _move_to_end(self, key: str) -> None:
        """Move key to end of access order (must be called with lock held)"""
        try:
            self._access_order.remove(key)
            self._access_order.append(key)
        except ValueError as e:
            logger.exception("Failed to move key to end of access order: %s", e)
