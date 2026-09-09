"""
Background Resolver Service
===========================
Async, non-blocking YouTube provider resolution service that prevents UI freezing.

Key Features:
- Background thread pool for slow resolution tasks
- Caching with TTL to prevent repeated lookups
- Timeout management (configurable, defaults to 15s)
- Progress callbacks for streaming feedback
- Graceful fallback on errors

Part of the unified service architecture for NOVVIOLA.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from core.logging_config import get_logger
from music.exceptions import ConfigurationError, ResolutionError
from music.log_utils import log_kv
from music.resolution.helpers import ResolutionMetadata

logger = get_logger(__name__)

# ---------- Protocol for resolution results ----------


@dataclass(frozen=True)
class ResolveResult:
    """Result of a YouTube resolution attempt."""

    url: str
    title: str
    video_id: str | None = None
    artist: str | None = None
    duration: int | None = None
    thumbnail: str | None = None
    error: str | None = None
    resolver_path: str = "builtin"
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.error is None

    def to_metadata(
        self,
        *,
        default_source: str,
        resolver_label: str = "background",
    ) -> ResolutionMetadata:
        if not self.is_success or not self.url:
            raise ResolutionError(self.error or "background_resolver_failure")

        combined_path = f"{resolver_label}.{self.resolver_path}"
        metadata = ResolutionMetadata(
            url=self.url,
            title=self.title or default_source,
            source=default_source,
            video_id=self.video_id,
            artist=self.artist,
            duration=self.duration,
            thumbnail_url=self.thumbnail,
            artwork_url=self.thumbnail,
            provider="youtube_music",
            resolver_path=combined_path,
            extras=self.extras.copy(),
            resolved_at=time.time(),
        )
        return metadata


@dataclass
class ResolveProgress:
    """Progress update during resolution."""

    stage: str  # "searching", "extracting", "complete", "error"
    message: str
    query: str
    progress: float = 0.0  # 0.0 to 1.0


# ---------- Cache with TTL ----------


K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """Simple LRU cache with time-to-live."""

    def __init__(self, max_size: int = 100, ttl_seconds: int = 3600):
        self._cache: OrderedDict[K, tuple[V, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds

    def get(self, key: K) -> V | None:
        """Get cached value if still valid."""
        if key not in self._cache:
            return None

        value, timestamp = self._cache[key]
        if time.time() - timestamp > self._ttl:
            # Expired
            del self._cache[key]
            return None

        # Move to end (LRU)
        self._cache.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        """Store value in cache."""
        self._cache[key] = (value, time.time())
        self._cache.move_to_end(key)

        # Evict oldest if over size
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()


# ---------- Background Resolver Service ----------


class BackgroundResolver:
    """
    Non-blocking resolver for music queries (legacy support, YouTube scraping removed).

    Features:
    - Async API that doesn't block the event loop
    - Progress callbacks for UI feedback
    - Intelligent caching to avoid repeated slow lookups
    - Configurable timeouts
    - Graceful degradation on failures

    Note: YouTube resolution has been removed. This resolver now blocks YouTube queries
    and requires a linked YouTube Music provider for any YouTube-related operations.
    """

    def __init__(
        self,
        max_workers: int | None = None,
        timeout: float = 15.0,
        cache_ttl: int = 3600,
        progress_callback: Callable[[ResolveProgress], None] | None = None,
        config: Any | None = None,
    ):
        """
        Initialize background resolver.

        Args:
            max_workers: Max concurrent resolution threads (None = auto-detect)
            timeout: Timeout per resolution (seconds)
            cache_ttl: Cache time-to-live (seconds)
            progress_callback: Optional callback for progress updates
            config: AppConfig instance for lightweight mode optimization
        """
        self._timeout = timeout
        self._progress_callback = progress_callback
        self._cache: TTLCache[str, ResolveResult] = TTLCache(max_size=100, ttl_seconds=cache_ttl)

        # Scraping-based resolver removed. Keep attribute for API compatibility.
        self._builtin_resolver = None

        # IMPROVEMENT #2: Dynamic thread pool sizing
        default_resolver_workers = 4
        if max_workers is None:
            if config:
                try:
                    from performance.pi_optimizations import (
                        get_optimal_thread_pool_size,
                    )

                    max_workers = get_optimal_thread_pool_size(config, default_resolver_workers)
                except ImportError:
                    max_workers = default_resolver_workers
            else:
                max_workers = 4

        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="music-resolver")
        self._active_tasks: dict[str, asyncio.Task[ResolveResult]] = {}

        log_kv(
            logger,
            "info",
            "background_resolver_initialized",
            workers=max_workers,
            timeout_seconds=timeout,
            cache_ttl_seconds=cache_ttl,
        )

    async def resolve(self, query: str, source: str = "ytsearch1") -> ResolveResult:
        """
        Resolve a query to a playable URL asynchronously.

        Args:
            query: Search query or URL
            source: Source type ("ytsearch1", "url", "local")

        Returns:
            ResolveResult with URL and metadata
        """
        # SAFETY: Block YouTube/YouTube Music resolution if provider not linked
        if source != "local":
            try:
                from music.providers.checker import (
                    is_youtube_url,
                    require_provider_linked,
                )

                if is_youtube_url(query) or source == "ytsearch1":
                    require_provider_linked(
                        "youtube_music",
                        error_message=(
                            "YouTube Music provider is not linked. "
                            "Cannot use background resolver to stream from YouTube/YouTube Music without a linked provider."
                        ),
                    )
            except ConfigurationError as exc:
                log_kv(
                    logger,
                    "warning",
                    "background_resolver_blocked",
                    query=query[:50],
                    reason="provider_not_linked",
                )
                self._emit_progress(query, "error", "Provider not linked", 1.0)
                return ResolveResult(
                    url="",
                    title=query,
                    error=f"Provider not linked: {exc}",
                )
            except Exception as exc:
                # If checker import fails, fail closed
                log_kv(
                    logger,
                    "warning",
                    "background_resolver_check_failed",
                    query=query[:50],
                    error=str(exc),
                )
                self._emit_progress(query, "error", "Provider check failed", 1.0)
                return ResolveResult(
                    url="",
                    title=query,
                    error="Provider check failed",
                )

        # Check cache first
        cache_key = f"{source}:{query}"
        cached = self._cache.get(cache_key)
        if cached:
            log_kv(
                logger,
                "debug",
                "background_resolver_cache_hit",
                query=query[:50],
            )
            self._emit_progress(query, "complete", "Loaded from cache", 1.0)
            return cached

        # Emit searching progress
        self._emit_progress(query, "searching", f"Searching for: {query[:50]}", 0.1)

        # Resolve in background
        try:
            # Run resolution in thread pool with timeout
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self._resolve_sync, query, source),
                timeout=self._timeout,
            )

            # Cache successful results
            if result.is_success:
                self._cache.put(cache_key, result)
                self._emit_progress(query, "complete", f"Found: {result.title}", 1.0)
            else:
                self._emit_progress(query, "error", result.error or "Resolution failed", 1.0)

            return result

        except TimeoutError:
            log_kv(
                logger,
                "warning",
                "background_resolver_timeout",
                query=query[:50],
                timeout_seconds=self._timeout,
            )
            self._emit_progress(query, "error", "Search timed out", 1.0)
            return ResolveResult(url="", title=query, error=f"Timeout after {self._timeout}s")
        except Exception as e:
            log_kv(
                logger,
                "error",
                "background_resolver_exception",
                query=query[:50],
                error=str(e),
            )
            self._emit_progress(query, "error", f"Error: {str(e)[:50]}", 1.0)
            return ResolveResult(url="", title=query, error=str(e))

    def _resolve_sync(self, query: str, source: str) -> ResolveResult:
        """
        Synchronous resolution logic (runs in thread pool).

        This is the blocking part, isolated to worker threads.
        """
        # YouTube scraping removed: background resolver no longer performs extraction.
        # Directly return a structured failure so callers can fall back or surface UI.
        return ResolveResult(
            url="",
            title=query,
            error="scraping_removed",
            resolver_path="background.unsupported",
        )

    def _emit_progress(self, query: str, stage: str, message: str, progress: float) -> None:
        """Emit progress update if callback is set."""
        if self._progress_callback:
            try:
                self._progress_callback(ResolveProgress(stage=stage, message=message, query=query, progress=progress))
            except Exception as e:
                log_kv(
                    logger,
                    "warning",
                    "background_resolver_progress_callback_error",
                    error=str(e),
                )

    def shutdown(self) -> None:
        """Shutdown the executor gracefully."""
        log_kv(logger, "info", "background_resolver_shutdown_begin")
        self._executor.shutdown(wait=True, cancel_futures=True)
        log_kv(logger, "info", "background_resolver_shutdown_complete")

    def __del__(self):
        """Cleanup on deletion."""
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.debug("Executor shutdown error: %s", e, exc_info=True)
            pass


# ---------- Singleton instance (optional, for backward compatibility) ----------

_resolver_instance: BackgroundResolver | None = None


def get_background_resolver(
    timeout: float = 15.0,
    config: Any | None = None,
    **kwargs,
) -> BackgroundResolver:
    """
    Get or create the global background resolver instance.

    This is a convenience function for backward compatibility.
    For better testability, inject BackgroundResolver directly.
    """
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = BackgroundResolver(
            timeout=timeout,
            config=config,
            **kwargs,
        )
    return _resolver_instance


def shutdown_background_resolver() -> None:
    """Shutdown the global resolver instance."""
    global _resolver_instance
    if _resolver_instance:
        _resolver_instance.shutdown()
        _resolver_instance = None
