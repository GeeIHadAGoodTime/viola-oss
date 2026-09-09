from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from core.logging_config import get_logger
from utils.circuit_breaker import CircuitBreaker

logger = get_logger(__name__)

# Default thresholds for music provider circuit breakers
_DEFAULT_PROVIDER_FAILURE_THRESHOLD = 5
_DEFAULT_PROVIDER_RECOVERY_TIMEOUT = 30.0


def _create_provider_breaker() -> CircuitBreaker:
    """Factory for creating provider-specific circuit breakers with music defaults."""
    return CircuitBreaker(
        failure_threshold=_DEFAULT_PROVIDER_FAILURE_THRESHOLD,
        recovery_timeout=_DEFAULT_PROVIDER_RECOVERY_TIMEOUT,
    )


class PrefetchPool:
    """Manage concurrent prefetch tasks to keep queue warm."""

    def __init__(self, max_concurrent: int = 3) -> None:
        self._max_concurrent = max_concurrent
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        if len(self._tasks) >= self._max_concurrent:
            return
        task = asyncio.create_task(self._run_task(coro_factory))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_task(self, coro_factory: Callable[[], Awaitable[None]]) -> None:
        try:
            await coro_factory()
        except asyncio.CancelledError:  # pragma: no cover - only on shutdown
            raise
        except Exception as exc:
            logger.debug("Prefetch task failed: %s", exc)


class ResilienceCoordinator:
    """Coordinate circuit breakers, prefetching, and resilience hooks."""

    def __init__(self) -> None:
        self._breakers: defaultdict[str, CircuitBreaker] = defaultdict(_create_provider_breaker)
        self._prefetchers: defaultdict[str, PrefetchPool] = defaultdict(PrefetchPool)

    def allow_request(self, provider: str) -> bool:
        return self._breakers[provider.lower()].allow_request()

    def record_success(self, provider: str) -> None:
        self._breakers[provider.lower()].record_success()

    def record_failure(self, provider: str) -> None:
        self._breakers[provider.lower()].record_failure()

    def schedule_prefetch(self, provider: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
        self._prefetchers[provider.lower()].schedule(coro_factory)

    def breaker_state(self, provider: str) -> str:
        return self._breakers[provider.lower()].state.value
