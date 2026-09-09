"""
music/performance/integration.py

Integration layer that wires together caching, parallel resolution, and freshness management.
Provides a unified interface for injecting performance improvements into MusicPlayer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

# Import the new performance modules
from music.caching import ResolutionCache
from music.freshness import FreshnessConfig, FreshnessPolicy, URLFreshnessManager
from music.resolution import ResolutionOrchestrator

QueueItem: type | None = None

try:
    from models.player import QueueItem as _QueueItemClass

    QueueItem = _QueueItemClass
except ImportError:
    pass


@dataclass
class PerformanceConfig:
    """
    Configuration for performance enhancements.

    All settings are optional with sensible defaults.
    """

    # Caching
    enable_cache: bool = True
    cache_type: str = "hybrid"  # "memory", "persistent", or "hybrid"
    memory_cache_size: int = 50
    disk_cache_size: int = 200
    cache_max_age_hours: float = 6.0
    cache_path: Path | None = None

    # Parallel resolution
    enable_parallel: bool = True
    max_concurrent: int = 5
    resolution_timeout: float = 30.0
    retry_count: int = 1

    # Provider metadata (for compliance/telemetry)
    provider: str = "ytmusic"

    # URL freshness
    enable_freshness: bool = True
    freshness_policy: FreshnessPolicy = FreshnessPolicy.BALANCED
    enable_background_monitor: bool = False  # Disabled by default (can use manual refresh)

    # Overall toggle
    enabled: bool = True

    @classmethod
    def from_profile(cls, profile: str) -> PerformanceConfig:
        """
        Create a PerformanceConfig from a named profile.

        Args:
            profile: Profile name - "minimal", "balanced", "aggressive"

        Returns:
            PerformanceConfig with appropriate settings
        """
        profiles = {
            "minimal": cls(
                enable_cache=True,
                cache_type="memory",
                memory_cache_size=20,
                enable_parallel=False,
                enable_freshness=False,
            ),
            "balanced": cls(
                enable_cache=True,
                cache_type="hybrid",
                memory_cache_size=50,
                disk_cache_size=200,
                enable_parallel=True,
                max_concurrent=5,
                enable_freshness=True,
                freshness_policy=FreshnessPolicy.BALANCED,
            ),
            "aggressive": cls(
                enable_cache=True,
                cache_type="hybrid",
                memory_cache_size=100,
                disk_cache_size=500,
                cache_max_age_hours=12.0,
                enable_parallel=True,
                max_concurrent=10,
                enable_freshness=True,
                freshness_policy=FreshnessPolicy.STRICT,  # Most aggressive refresh policy
                enable_background_monitor=True,
            ),
        }
        if profile not in profiles:
            logger.warning("Unknown profile '%s', using 'balanced'", profile)
            profile = "balanced"
        return profiles[profile]


class PerformanceEnhancedPlayer:
    """
    Decorator/wrapper that adds performance enhancements to a MusicPlayer.

    Features:
    - Intelligent caching (hybrid memory + disk)
    - Parallel batch resolution
    - Proactive URL refresh
    - Backward compatible

    Usage:
        # Option 1: Wrap existing player
        enhanced = PerformanceEnhancedPlayer(music_player, config)

        # Option 2: Use as mixin (player inherits from this)
        class MyPlayer(PerformanceEnhancedPlayer, MusicPlayer):
            pass
    """

    def __init__(self, base_player, config: PerformanceConfig | None = None):
        """
        Initialize performance enhancements.

        Args:
            base_player: The MusicPlayer instance to enhance
            config: Optional configuration (uses defaults if None)
        """
        self._base = base_player
        self._config = config or PerformanceConfig()

        # Initialize services (lazy - only if enabled)
        self._cache: ResolutionCache | None = None
        self._orchestrator: ResolutionOrchestrator | None = None
        self._freshness: URLFreshnessManager | None = None

        if self._config.enabled:
            self._initialize_services()

        logger.info(
            "🚀 Performance enhancements initialized (cache: %s, parallel: %s, freshness: %s)",
            self._config.enable_cache,
            self._config.enable_parallel,
            self._config.enable_freshness,
        )

    def _initialize_services(self) -> None:
        """Initialize performance services based on config"""
        # 1. Cache
        if self._config.enable_cache:
            self._cache = self._create_cache()
            logger.info("✅ Cache enabled: %s", self._cache.__class__.__name__)

        # 2. Parallel orchestrator
        if self._config.enable_parallel:
            # Create resolver that uses cache
            async def cached_resolver(query: str, source: str | None = None, emit: bool = True):
                """Resolver with cache integration.

                Multi-tenant: the resolution cache key includes the
                ambient ``user_id`` so two tenants searching for the
                same query never share an entry — even if the resolved
                track is public media, the *query text* and access
                pattern are user input.
                """
                # Multi-tenant: scope cache by tenant.  Without an
                # ambient user we deliberately skip the cache rather
                # than place the entry in a shared "global" bucket.
                try:
                    from core.user_context import get_current_user_id

                    owner = get_current_user_id()
                except Exception:
                    owner = ""

                # Check cache first
                if self._cache and owner:
                    cache_key = f"{owner}:{source or 'ytsearch1'}:{query}"
                    cached = self._cache.get(cache_key)
                    if cached:
                        logger.debug("💨 Cache hit: %s", query[:50])
                        # Create QueueItem from cached data
                        return self._create_queue_item_from_cache(cached.to_tuple(), query, source)

                # Cache miss - resolve normally
                result = await self._base.play_async(query, source, emit)
                return result

            self._orchestrator = ResolutionOrchestrator(
                resolver=cached_resolver,
                max_concurrent=self._config.max_concurrent,
                retry_count=self._config.retry_count,
                timeout_per_item=self._config.resolution_timeout,
                provider=self._config.provider,
            )
            logger.info(
                "✅ Parallel orchestrator enabled (max concurrent: %s)",
                self._config.max_concurrent,
            )

        # 3. URL freshness
        if self._config.enable_freshness:

            async def refresh_func(item: Any) -> Any:
                """Refresh function that re-resolves URL"""
                try:
                    query = item.video_id or item.title
                    source = "url" if item.video_id else "ytsearch1"

                    # Re-resolve
                    new_item = await self._base.play_async(query, source, emit=False)

                    # Update timestamp
                    if hasattr(new_item, "resolved_at"):
                        new_item.resolved_at = time.time()

                    return new_item
                except Exception as e:
                    logger.error("Failed to refresh %s: %s", item.title, e)
                    return None

            freshness_config = FreshnessConfig.from_policy(self._config.freshness_policy)
            self._freshness = URLFreshnessManager(refresh_func, freshness_config)
            logger.info(
                "✅ URL freshness enabled (policy: %s)",
                self._config.freshness_policy.value,
            )

    def _create_cache(self) -> ResolutionCache:
        """Create cache instance based on config"""
        cache_path = self._config.cache_path or (get_data_dir() / "resolution_cache.json")

        if self._config.cache_type == "hybrid":
            from music.caching import HybridCache

            return HybridCache(
                memory_size=self._config.memory_cache_size,
                disk_size=self._config.disk_cache_size,
                max_age_seconds=self._config.cache_max_age_hours * 3600,
            )
        elif self._config.cache_type == "persistent":
            from music.caching import PersistentCache

            return PersistentCache(
                cache_path=cache_path,
                max_size=self._config.disk_cache_size,
                max_age_seconds=self._config.cache_max_age_hours * 3600,
            )
        else:  # memory
            from music.caching import MemoryCache

            return MemoryCache(
                max_size=self._config.memory_cache_size,
                max_age_seconds=self._config.cache_max_age_hours * 3600,
            )

    def _create_queue_item_from_cache(self, cached_tuple: Any, query: str, source: str | None) -> Any:
        """Create QueueItem from cached resolution"""
        import uuid

        url, title, video_id, artist = cached_tuple

        if QueueItem is None:
            raise RuntimeError("QueueItem class not available")
        return QueueItem(
            id=str(uuid.uuid4()),
            title=title,
            url=url,
            source=source or "ytsearch1",
            video_id=video_id,
            artist=artist,
            resolved_at=time.time(),
        )

    # ============= Enhanced Public API =============

    async def resolve_batch_parallel(
        self, queries: list[str], source: str | None = None, emit: bool = False
    ) -> list[Any]:
        """
        Resolve multiple queries in parallel (HIGH ROI feature #2).

        Args:
            queries: List of queries to resolve
            source: Source type
            emit: Whether to emit state changes

        Returns:
            List of successfully resolved QueueItems
        """
        if not self._config.enable_parallel or not self._orchestrator:
            # Fallback to sequential
            logger.warning("Parallel resolution not enabled, falling back to sequential")
            results = []
            for query in queries:
                try:
                    item = await self._base.play_async(query, source, emit=False)
                    results.append(item)
                except Exception as e:
                    logger.warning("Failed to resolve %s: %s", query, e)
            return results

        # Use orchestrator for parallel resolution
        logger.info("🔄 Resolving %s queries in parallel...", len(queries))
        start_time = time.time()

        results = await self._orchestrator.resolve_batch(
            queries=queries,
            source=source,
            emit=emit,
            provider=self._config.provider,
        )

        elapsed = time.time() - start_time
        successes = [r.data for r in results if r.success and r.data is not None]

        logger.info(
            "✅ Parallel resolution complete: %s/%s succeeded in %ss (%ss per item)",
            len(successes),
            len(queries),
            format(elapsed, ".2f"),
            format(elapsed / len(queries), ".2f"),
        )

        return successes

    async def ensure_fresh_url(self, item: Any, force: bool = False) -> Any:
        """
        Ensure item has a fresh URL (HIGH ROI feature #3).

        Args:
            item: QueueItem to check
            force: Force refresh even if not stale

        Returns:
            Updated QueueItem with fresh URL
        """
        if not self._config.enable_freshness or not self._freshness:
            return item

        return await self._freshness.ensure_fresh(item, force)

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics"""
        if self._cache:
            return self._cache.stats()
        return {"enabled": False}

    def get_orchestrator_stats(self) -> dict[str, Any]:
        """Get parallel orchestrator statistics"""
        if self._orchestrator:
            return self._orchestrator.stats()
        return {"enabled": False}

    def get_freshness_stats(self) -> dict[str, Any]:
        """Get freshness manager statistics"""
        if self._freshness:
            return self._freshness.stats()
        return {"enabled": False}

    def get_performance_stats(self) -> dict[str, Any]:
        """Get combined performance statistics"""
        return {
            "enabled": self._config.enabled,
            "cache": self.get_cache_stats(),
            "orchestrator": self.get_orchestrator_stats(),
            "freshness": self.get_freshness_stats(),
        }

    def flush_cache(self) -> None:
        """Flush cache to disk (if persistent cache is enabled)"""
        if self._cache and hasattr(self._cache, "flush"):
            self._cache.flush()

    # ============= Proxy methods to base player =============

    def __getattr__(self, name):
        """Proxy all other methods to base player"""
        return getattr(self._base, name)
