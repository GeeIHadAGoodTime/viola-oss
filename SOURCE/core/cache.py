"""
core/cache.py

Unified caching protocols and base implementations for NOVVIOLA.

This module provides:
1. Cache[K, V] Protocol - Generic cache interface for type-safe caching
2. CacheStats TypedDict - Standard statistics format
3. BaseTTLCache - Reference implementation with TTL and LRU eviction

Domain-specific caches (weather, music, GPT, etc.) should implement the
Cache protocol to ensure consistent behavior across the codebase.

Usage:
    from core.cache import Cache, CacheStats, BaseTTLCache

    # Type-safe cache usage
    def process_with_cache(cache: Cache[str, dict[str, Any]]) -> None:
        if (result := cache.get("key")) is not None:
            return result
        result = expensive_operation()
        cache.put("key", result)
        return result

    # Use the base implementation
    cache: Cache[str, str] = BaseTTLCache(max_size=100, ttl_seconds=3600)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Generic, Protocol, TypedDict, TypeVar, runtime_checkable

from core.logging_config import get_logger

logger = get_logger(__name__)

# Type variables for generic cache
K = TypeVar("K")  # Key type
V = TypeVar("V")  # Value type


class CacheStats(TypedDict, total=False):
    """
    Standard cache statistics format.

    All caches should return stats in this format for consistency.
    """

    type: str  # Cache implementation type (e.g., "MemoryCache", "HybridCache")
    size: int  # Current number of entries
    max_size: int  # Maximum capacity
    hits: int  # Cache hit count
    misses: int  # Cache miss count
    hit_rate_percent: float  # Hit rate as percentage (0-100)
    evictions: int  # Number of entries evicted
    ttl_seconds: float  # Time-to-live in seconds
    # Optional fields for specific implementations
    persistent: bool  # Whether cache survives restarts
    encryption: str  # Encryption status ("enabled"/"disabled")


@runtime_checkable
class Cache(Protocol[K, V]):
    """
    Generic cache protocol for type-safe caching.

    All cache implementations should conform to this protocol.
    This enables dependency injection and consistent testing patterns.

    Type Parameters:
        K: Key type (typically str)
        V: Value type (domain-specific, e.g., CacheEntry, dict, SearchResults)

    Example implementations:
        - music.caching.ResolutionCache (V = CacheEntry)
        - cache.weather_cache.WeatherCache (K = location, V = dict)
        - music.providers.youtube_music_cache.PersistentSearchCache (V = SearchResults)
    """

    def get(self, key: K) -> V | None:
        """
        Retrieve a value from the cache.

        Args:
            key: Cache key

        Returns:
            Cached value if found and not expired, None otherwise
        """
        ...

    def put(self, key: K, value: V) -> None:
        """
        Store a value in the cache.

        Args:
            key: Cache key
            value: Value to store
        """
        ...

    def clear(self) -> None:
        """Clear all entries from the cache."""
        ...

    def size(self) -> int:
        """Get the current number of entries in the cache."""
        ...


class BaseTTLCache(Generic[K, V]):
    """
    Thread-safe in-memory cache with TTL and LRU eviction.

    This is a reference implementation that domain-specific caches
    can use or extend. It implements the Cache protocol.

    Features:
        - Configurable max size
        - Time-to-live expiration
        - LRU eviction policy
        - Thread-safe operations
        - Statistics tracking

    Example:
        cache: Cache[str, dict] = BaseTTLCache(max_size=100, ttl_seconds=3600)
        cache.put("weather:seattle", {"temp": 65, "condition": "Cloudy"})
        data = cache.get("weather:seattle")
    """

    def __init__(self, max_size: int = 100, ttl_seconds: float = 3600.0):
        """
        Initialize the cache.

        Args:
            max_size: Maximum number of entries (default 100)
            ttl_seconds: Time-to-live in seconds (default 1 hour)
        """
        self._cache: dict[K, tuple[V, float]] = {}  # key -> (value, timestamp)
        self._access_order: deque[K] = deque()
        self._lock = threading.RLock()
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds

        # Statistics
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: K) -> V | None:
        """Get value from cache, checking TTL and updating LRU order."""
        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return None

            value, timestamp = entry

            # Check TTL expiration
            if time.time() - timestamp > self._ttl_seconds:
                self._remove(key)
                self._misses += 1
                return None

            # Valid hit - update LRU order
            self._hits += 1
            self._move_to_end(key)
            return value

    def put(self, key: K, value: V) -> None:
        """Store value in cache, evicting oldest if necessary."""
        with self._lock:
            # If key exists, remove from old position
            if key in self._cache:
                try:
                    self._access_order.remove(key)
                except ValueError:
                    pass  # Key not in access order, ignore

            # Add entry with current timestamp
            self._cache[key] = (value, time.time())
            self._access_order.append(key)

            # Evict oldest entries if over capacity
            while len(self._access_order) > self._max_size:
                oldest_key = self._access_order.popleft()
                self._cache.pop(oldest_key, None)
                self._evictions += 1

    def clear(self) -> None:
        """Clear all entries from the cache."""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def size(self) -> int:
        """Get current number of entries."""
        with self._lock:
            return len(self._cache)

    def invalidate(self, key: K) -> bool:
        """Invalidate a specific entry."""
        with self._lock:
            if key in self._cache:
                self._remove(key)
                return True
            return False

    def stats(self) -> CacheStats:
        """Get cache statistics."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0

            return CacheStats(
                type="BaseTTLCache",
                size=len(self._cache),
                max_size=self._max_size,
                hits=self._hits,
                misses=self._misses,
                hit_rate_percent=round(hit_rate, 2),
                evictions=self._evictions,
                ttl_seconds=self._ttl_seconds,
            )

    def flush(self) -> None:
        """No-op for in-memory cache."""
        pass

    def _remove(self, key: K) -> None:
        """Remove entry (must be called with lock held)."""
        self._cache.pop(key, None)
        try:
            self._access_order.remove(key)
        except ValueError:
            pass  # Key not in access order

    def _move_to_end(self, key: K) -> None:
        """Move key to end of access order (must be called with lock held)."""
        try:
            self._access_order.remove(key)
            self._access_order.append(key)
        except ValueError:
            pass  # Key not in access order


__all__ = [
    "BaseTTLCache",
    "Cache",
    "CacheStats",
]
