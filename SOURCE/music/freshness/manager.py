"""
music/freshness/manager.py

Proactive URL freshness management to prevent playback failures.
Tracks URL age and triggers refresh before expiration.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

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


if TYPE_CHECKING:
    from models.player import QueueItem
else:  # pragma: no cover - runtime fallback when typing-only import unavailable
    # Use a type alias at runtime when the real QueueItem is unavailable
    QueueItem: type = object


class FreshnessPolicy(Enum):
    """Policy for URL freshness checking"""

    STRICT = "strict"  # Refresh at 4 hours (safe)
    BALANCED = "balanced"  # Refresh at 5 hours (balanced)
    RELAXED = "relaxed"  # Refresh at 5.5 hours (risky but fewer refreshes)


class RefreshFunction(Protocol):
    """Protocol for URL refresh functions"""

    async def __call__(self, item: QueueItem) -> QueueItem | None:
        """Refresh a QueueItem's URL, returning updated item or None on failure"""
        ...


@dataclass
class FreshnessConfig:
    """Configuration for freshness management"""

    max_age_hours: float = 6.0  # YouTube URLs expire after ~6 hours
    stale_threshold_hours: float = 4.0  # Refresh proactively at 4 hours
    check_interval_seconds: float = 300.0  # Check every 5 minutes
    policy: FreshnessPolicy = FreshnessPolicy.BALANCED

    @classmethod
    def from_policy(cls, policy: FreshnessPolicy) -> FreshnessConfig:
        """Create config from policy preset"""
        if policy == FreshnessPolicy.STRICT:
            return cls(stale_threshold_hours=4.0)
        if policy == FreshnessPolicy.BALANCED:
            return cls(stale_threshold_hours=5.0)
        if policy == FreshnessPolicy.RELAXED:
            return cls(stale_threshold_hours=5.5)
        raise ValueError(f"Unsupported freshness policy: {policy}")


class URLFreshnessManager:
    """
    Manages URL freshness for queue items.

    Features:
    - Tracks URL age for all queue items
    - Proactive refresh before expiration
    - Background monitoring
    - Configurable policies

    Usage:
        manager = URLFreshnessManager(refresh_func, config)
        manager.start()

        # Before playing
        fresh_item = await manager.ensure_fresh(queue_item)

        manager.stop()
    """

    def __init__(self, refresh_func: RefreshFunction, config: FreshnessConfig | None = None):
        """
        Initialize freshness manager.

        Args:
            refresh_func: Async function to refresh URLs
            config: Optional configuration (uses defaults if None)
        """
        self._refresh_func: RefreshFunction = refresh_func
        self._config = config or FreshnessConfig()

        # Background monitoring
        self._monitor_task: asyncio.Task[None] | None = None
        self._monitor_lock = threading.Lock()
        self._running = False

        # Stats
        self._refreshes_triggered = 0
        self._refreshes_succeeded = 0
        self._refreshes_failed = 0
        self._items_checked = 0

        logger.info(
            "🔄 URL Freshness Manager initialized (policy: %s, stale threshold: %sh)",
            self._config.policy.value,
            self._config.stale_threshold_hours,
        )

    def _resolved_timestamp(self, item: QueueItem) -> float | None:
        """Safely retrieve resolved_at timestamp."""
        resolved_at = getattr(item, "resolved_at", None)
        if resolved_at is None:
            return None
        try:
            return float(resolved_at)
        except (TypeError, ValueError):
            logger.debug("Invalid resolved_at on queue item %s")
            return None

    def is_expired(self, item: QueueItem) -> bool:
        """Check if item's URL has expired"""
        resolved_at = self._resolved_timestamp(item)
        if resolved_at is None:
            # Legacy item without timestamp - assume not expired
            return False

        age_hours = (time.time() - resolved_at) / 3600
        return age_hours > self._config.max_age_hours

    def is_stale(self, item: QueueItem) -> bool:
        """Check if item's URL is stale and should be refreshed soon"""
        resolved_at = self._resolved_timestamp(item)
        if resolved_at is None:
            return False

        age_hours = (time.time() - resolved_at) / 3600
        return age_hours > self._config.stale_threshold_hours

    def age_hours(self, item: QueueItem) -> float:
        """Get age of item's URL in hours"""
        resolved_at = self._resolved_timestamp(item)
        if resolved_at is None:
            return 0.0

        return (time.time() - resolved_at) / 3600

    async def ensure_fresh(self, item: QueueItem, force: bool = False) -> QueueItem:
        """
        Ensure item has a fresh URL, refreshing if necessary.

        Args:
            item: QueueItem to check
            force: Force refresh even if not stale

        Returns:
            Updated QueueItem with fresh URL (or original if refresh fails)
        """
        self._items_checked += 1

        # Check if refresh needed
        needs_refresh = force or self.is_expired(item)
        should_refresh_soon = self.is_stale(item) and not self.is_expired(item)

        if not needs_refresh and not should_refresh_soon:
            # URL is fresh
            return item

        age = self.age_hours(item)

        if needs_refresh:
            logger.warning(
                "🔄 URL expired for '%s' (%sh old), refreshing immediately...",
                item.title,
                age,
            )
        else:
            logger.info(
                "🔄 URL stale for '%s' (%sh old), proactively refreshing...",
                item.title,
                age,
            )

        # Attempt refresh
        self._refreshes_triggered += 1

        try:
            refreshed = await self._refresh_func(item)

            if refreshed is not None:
                self._refreshes_succeeded += 1
                logger.info("✅ URL refreshed for '%s'", item.title)
                return refreshed
            else:
                self._refreshes_failed += 1
                logger.warning("❌ URL refresh failed for '%s', using old URL", item.title)
                return item

        except Exception as e:
            self._refreshes_failed += 1
            logger.error("❌ URL refresh error for '%s': %s", item.title, e)
            return item

    async def ensure_fresh_batch(self, items: Sequence[QueueItem]) -> list[QueueItem]:
        """
        Ensure multiple items have fresh URLs.

        Args:
            items: List of QueueItems to check

        Returns:
            List of updated QueueItems
        """
        if not items:
            return []

        logger.debug("🔄 Checking freshness for %s items...", len(items))

        # Refresh in parallel
        tasks: list[asyncio.Task[QueueItem]] = [asyncio.create_task(self.ensure_fresh(item)) for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out exceptions
        fresh_items: list[QueueItem] = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.warning("Error refreshing item %s: %s", i, result)
                fresh_items.append(items[i])  # Keep original on error
            else:
                fresh_items.append(result)

        return fresh_items

    def start_monitoring(
        self,
        get_queue_func: Callable[[], Sequence[QueueItem]],
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """
        Start background monitoring of queue items.

        Args:
            get_queue_func: Function to get current queue
            loop: Event loop to run in (uses current loop if None)
        """
        if self._running:
            logger.warning("Freshness monitor already running")
            return

        self._running = True

        async def monitor() -> None:
            """Background monitoring coroutine"""
            logger.info(
                "🔄 Freshness monitor started (check interval: %ss)",
                self._config.check_interval_seconds,
            )

            while self._running:
                try:
                    # Get current queue
                    queue = get_queue_func()

                    if queue:
                        # Check for stale items
                        stale_items = [item for item in queue if self.is_stale(item)]

                        if stale_items:
                            logger.info(
                                "🔄 Found %s stale items, refreshing in background...",
                                len(stale_items),
                            )

                            # Refresh in background (don't block)
                            task = asyncio.create_task(self.ensure_fresh_batch(stale_items))
                            task.add_done_callback(_log_task_exception)

                except Exception as e:
                    logger.error("Error in freshness monitor: %s", e)

                # Sleep until next check
                await asyncio.sleep(self._config.check_interval_seconds)

            logger.info("🔄 Freshness monitor stopped")

        # Start monitoring task
        target_loop = loop or asyncio.get_event_loop()
        self._monitor_task = target_loop.create_task(monitor())
        self._monitor_task.add_done_callback(_log_task_exception)

    def stop_monitoring(self) -> None:
        """Stop background monitoring"""
        if not self._running:
            return

        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

        logger.info("🔄 Freshness monitor stopped")

    def stats(self) -> dict[str, Any]:
        """Get freshness manager statistics"""
        return {
            "policy": self._config.policy.value,
            "stale_threshold_hours": self._config.stale_threshold_hours,
            "max_age_hours": self._config.max_age_hours,
            "items_checked": self._items_checked,
            "refreshes_triggered": self._refreshes_triggered,
            "refreshes_succeeded": self._refreshes_succeeded,
            "refreshes_failed": self._refreshes_failed,
            "success_rate_percent": round(
                ((self._refreshes_succeeded / self._refreshes_triggered * 100) if self._refreshes_triggered > 0 else 0),
                2,
            ),
        }
