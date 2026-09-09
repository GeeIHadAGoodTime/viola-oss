"""
Async YouTube Resolution Enhancement

Converts blocking YouTube resolution to non-blocking async operations.

Features:
- Background thread resolution
- Immediate user feedback
- Progress events
- Parallel resolution support
- Graceful degradation

ROI: 95/100 - Eliminates UI freezing, high user impact
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

from core.logging_config import get_logger

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


class AsyncResolutionEnhancer:
    """
    Wraps synchronous YouTube resolution with async interface.

    Usage:
        enhancer = AsyncResolutionEnhancer()
        player = enhancer.enhance(player)

        # Now resolution is async!
        await player.play_async("bohemian rhapsody")
    """

    def __init__(
        self,
        max_workers: int | None = None,
        emit_progress: bool = True,
        config: Any | None = None,
    ):
        """
        Initialize async resolution enhancer.

        Args:
            max_workers: Max parallel resolution threads (None = auto-detect)
            emit_progress: Emit progress events during resolution
            config: AppConfig instance for lightweight mode optimization
        """
        # IMPROVEMENT #2: Dynamic thread pool sizing
        default_resolution_workers = 4
        if max_workers is None:
            if config:
                try:
                    from performance.pi_optimizations import (
                        get_optimal_thread_pool_size,
                    )

                    default_resolution_workers = 4  # Reset after import
                    max_workers = get_optimal_thread_pool_size(config, default_resolution_workers)
                except ImportError:
                    default_resolution_workers = 4
                    max_workers = default_resolution_workers
            else:
                max_workers = 4

        self.max_workers = max_workers
        self.emit_progress = emit_progress
        self._executor: ThreadPoolExecutor | None = None
        self._active_resolutions: dict[str, asyncio.Task[Any]] = {}

    def _get_executor(self) -> ThreadPoolExecutor:
        """Get or create thread pool executor."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="async_resolution_")
            logger.info("🎵 Created resolution thread pool: %s workers", self.max_workers)
        return self._executor

    def enhance(self, player: Any) -> Any:
        """
        Enhance music player with async resolution.

        Args:
            player: Music player to enhance

        Returns:
            Enhanced player with async methods
        """
        if hasattr(player, "_async_resolution_enhanced"):
            logger.debug("Player already enhanced with async resolution")
            return player

        # Store original methods
        original_play = player.play if hasattr(player, "play") else None
        original_resolve = player._resolve if hasattr(player, "_resolve") else None

        if not original_play or not original_resolve:
            logger.warning("Player missing play() or _resolve(), cannot enhance")
            return player

        # Create async wrapper for resolution
        async def async_resolve(query: str, source: str = "ytsearch1") -> str | None:
            """Async wrapper for YouTube resolution."""
            # Emit searching event
            if self.emit_progress and hasattr(player, "_emit_event"):
                try:
                    player._emit_event(
                        "searching",
                        {"query": query, "source": source, "status": "started"},
                    )
                except Exception as e:
                    logger.debug("Failed to emit searching event: %s", e)

            # Run resolution in thread pool
            loop = asyncio.get_running_loop()
            executor = self._get_executor()

            try:
                url = cast(
                    str | None,
                    await loop.run_in_executor(executor, original_resolve, query, source),
                )

                # Emit found event
                if self.emit_progress and hasattr(player, "_emit_event"):
                    try:
                        player._emit_event(
                            "found",
                            {"query": query, "url": url, "status": "completed"},
                        )
                    except Exception as e:
                        logger.debug("Failed to emit found event: %s", e)

                return url

            except Exception as e:
                # Emit error event
                if self.emit_progress and hasattr(player, "_emit_event"):
                    try:
                        player._emit_event(
                            "resolution_error",
                            {"query": query, "error": str(e), "status": "failed"},
                        )
                    except Exception as ex:
                        logger.debug("Failed to emit error event: %s", ex, exc_info=True)
                        pass

                logger.error("Async resolution failed for '%s': %s", query, e)
                raise

        # Create async play method
        async def play_async(query: str, source: str = "ytsearch1") -> dict[str, Any]:
            """
            Async play method that doesn't block.

            Args:
                query: Search query or URL
                source: Source type (ytsearch1, url, local)

            Returns:
                Result dict with status and info
            """
            logger.info("🎵 Async play: '%s' (source=%s)", query, source)

            # Track active resolution
            resolution_id = f"{query}_{source}"
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_resolutions[resolution_id] = current_task

            try:
                # Resolve in background
                url = await async_resolve(query, source)

                if not url:
                    return {
                        "ok": False,
                        "error": "Resolution failed - no URL found",
                        "query": query,
                    }

                # Call original play method (but skip resolution)
                # This part needs to be synchronous as VLC operations are not async
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._get_executor(),
                    lambda: play_resolved(player, query, url),
                )

                return {
                    "ok": True,
                    "query": query,
                    "url": url,
                    "result": result,
                }

            except Exception as e:
                logger.error("Async play failed: %s", e)
                return {
                    "ok": False,
                    "error": str(e),
                    "query": query,
                }
            finally:
                # Clean up tracking
                if resolution_id in self._active_resolutions:
                    del self._active_resolutions[resolution_id]

        def play_resolved(player_obj: Any, query: str, url: str) -> Any:
            """Play already-resolved URL."""
            # This bypasses resolution and goes straight to playback
            # We need to manually add to queue and play
            if hasattr(player_obj, "_add_to_queue_and_play"):
                return player_obj._add_to_queue_and_play(query, url)
            else:
                # Fallback: use original play but it will try to resolve again
                # (not ideal but safe)
                logger.warning("Player missing _add_to_queue_and_play, using fallback")
                return original_play(query)

        # Batch async resolution
        async def resolve_batch(queries: list[str], source: str = "ytsearch1") -> list[str | None]:
            """
            Resolve multiple queries in parallel.

            Args:
                queries: List of queries to resolve
                source: Source type

            Returns:
                List of URLs (None for failed resolutions)
            """
            logger.info("🎵 Batch resolving %s queries", len(queries))

            tasks = [async_resolve(query, source) for query in queries]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Convert exceptions to None
            urls: list[str | None] = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error("Batch resolution [%s] failed: %s", i, result)
                    urls.append(None)
                else:
                    urls.append(cast(str | None, result))

            success_count = sum(1 for url in urls if url)
            logger.info(
                "🎵 Batch resolution complete: %s/%s successful",
                success_count,
                len(queries),
            )

            return urls

        # Attach new methods to player
        player.play_async = play_async
        player.resolve_async = async_resolve
        player.resolve_batch = resolve_batch
        player._async_resolution_enhanced = True

        # Optional: Make original play() method async-aware
        def play_with_async_option(query: str, use_async: bool = False, **kwargs: Any) -> Any:
            """Enhanced play that can use async resolution."""
            if use_async:
                # Run async version in event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # If called from async context, schedule as task
                        _task = asyncio.create_task(play_async(query))
                        _task.add_done_callback(_log_task_exception)
                        return {
                            "ok": True,
                            "message": "Resolution started in background",
                        }
                    else:
                        # Run async version
                        return loop.run_until_complete(play_async(query))
                except Exception as e:
                    logger.error("Failed to run async play: %s, falling back to sync", e)
                    return original_play(query, **kwargs)
            else:
                return original_play(query, **kwargs)

        # Replace play method with enhanced version
        # player.play = play_with_async_option  # Optional: uncomment to enable

        logger.info("✅ Enhanced player with async YouTube resolution")
        return player

    async def cleanup(self) -> None:
        """Cleanup thread pool."""
        if self._executor:
            self._executor.shutdown(wait=True)
            logger.info("🎵 Resolution thread pool shut down")
            self._executor = None


def enhance_with_async(
    player: Any,
    max_workers: int = 4,
) -> Any:
    """
    Convenience function to enhance player with async resolution.

    Args:
        player: Music player to enhance
        max_workers: Max parallel resolution threads

    Returns:
        Enhanced player

    Example:
        from utils.enhancements import enhance_with_async
        player = enhance_with_async(player)
        await player.play_async("bohemian rhapsody")
    """
    enhancer = AsyncResolutionEnhancer(max_workers=max_workers)
    return enhancer.enhance(player)
