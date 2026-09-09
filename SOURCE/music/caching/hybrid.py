"""
music/caching/hybrid.py

Hybrid cache combining memory (fast) and disk (persistent).
Best of both worlds - speed and persistence.
"""

from __future__ import annotations

from typing import Any

from .base import CacheEntry, ResolutionCache
from .memory import MemoryCache
from .persistent import PersistentCache


class HybridCache(ResolutionCache):
    """
    Two-tier hybrid cache: Memory L1 + Persistent L2.

    Strategy:
    - Check L1 (memory) first - fast
    - On L1 miss, check L2 (disk) - persistent
    - On L2 hit, promote to L1
    - Writes go to both L1 and L2

    Benefits:
    - Fast access for frequently used entries (L1)
    - Persistence across restarts (L2)
    - Automatic promotion of popular entries
    """

    def __init__(
        self,
        memory_size: int = 50,
        disk_size: int = 200,
        max_age_seconds: float = 6 * 3600,
    ):
        """
        Initialize hybrid cache.

        Args:
            memory_size: Size of L1 memory cache (default 50)
            disk_size: Size of L2 disk cache (default 200)
            max_age_seconds: Maximum age before expiration
        """
        self._l1 = MemoryCache(max_size=memory_size, max_age_seconds=max_age_seconds)
        self._l2 = PersistentCache(max_size=disk_size, max_age_seconds=max_age_seconds)

        # Combined stats
        self._l2_promotions = 0

    def get(self, key: str) -> CacheEntry | None:
        """Get from L1, fallback to L2, promote on L2 hit"""
        # Try L1 first (fast path)
        entry = self._l1.get(key)
        if entry is not None:
            return entry

        # Try L2 (persistent)
        entry = self._l2.get(key)
        if entry is not None:
            # L2 hit - promote to L1 for fast future access
            self._l1.put(key, entry)
            self._l2_promotions += 1
            return entry

        return None

    def put(self, key: str, entry: CacheEntry) -> None:
        """Store in both L1 and L2"""
        self._l1.put(key, entry)
        self._l2.put(key, entry)

    def set_provider_lease(self, provider: str, lease_id: str) -> None:
        """Propagate provider lease to persistent tier."""
        self._l2.set_provider_lease(provider, lease_id)

    def get_provider_lease(self, provider: str) -> str | None:
        """Return current lease token tracked by persistent tier."""
        return self._l2.get_provider_lease(provider)

    def invalidate_provider(self, provider: str) -> int:
        """Invalidate all entries for provider across tiers."""
        removed = self._l2.invalidate_provider(provider)
        removed += self._l1.invalidate_provider(provider)
        return removed

    def clear(self) -> None:
        """Clear both caches"""
        self._l1.clear()
        self._l2.clear()

    def size(self) -> int:
        """Get total unique entries across both caches"""
        # Note: Some overlap expected, but we report L2 size as it's the superset
        return self._l2.size()

    def invalidate(self, key: str) -> bool:
        """Invalidate from both caches"""
        l1_removed = self._l1.invalidate(key)
        l2_removed = self._l2.invalidate(key)
        return l1_removed or l2_removed

    def stats(self) -> dict[str, Any]:
        """Get combined statistics"""
        l1_stats = self._l1.stats()
        l2_stats = self._l2.stats()

        # Calculate combined hit rate
        total_hits = l1_stats["hits"] + l2_stats["hits"]
        total_misses = l2_stats["misses"]  # Only count final misses
        total_requests = total_hits + total_misses
        combined_hit_rate = (total_hits / total_requests * 100) if total_requests > 0 else 0

        return {
            "type": "HybridCache",
            "total_size": self._l2.size(),
            "l1_size": l1_stats["size"],
            "l2_size": l2_stats["size"],
            "l1_hits": l1_stats["hits"],
            "l2_hits": l2_stats["hits"],
            "l2_promotions": self._l2_promotions,
            "misses": total_misses,
            "combined_hit_rate_percent": round(combined_hit_rate, 2),
            "l1": l1_stats,
            "l2": l2_stats,
        }

    def flush(self) -> None:
        """Force L2 to write to disk"""
        self._l2.flush()
