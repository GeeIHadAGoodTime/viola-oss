"""Per-User Rate Limiting for Authenticated API Requests.

Provides rate limiting keyed by authenticated user_id, independent of
per-IP rate limiting. This ensures that users behind shared IPs (VPNs,
corporate proxies, CGNAT) each get their own rate limit bucket.

Design:
    - ``limits`` Redis moving-window storage when ``VIOLA_REDIS_URL`` is configured
      (survives restarts, shared across instances)
    - Falls back to ``limits`` in-memory moving-window storage when Redis is
      unavailable (resets on restart, single-instance only)
    - Same default limits as IP-based rate limiting
    - Tracks by user_id from session/token, not by IP
    - Returns 429 with Retry-After header when limit exceeded
    - Non-blocking: rate limit check is fast and lock-free for reads

Usage:
    >>> from auth.per_user_rate_limit import get_user_rate_limiter
    >>> limiter = get_user_rate_limiter()
    >>> allowed, retry_after = limiter.check("user-123", "/api/v1/command")
    >>> if not allowed:
    ...     return JSONResponse(status_code=429, ...)
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from services.cache.redis_backend import RedisBackend

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default: 200 requests per minute per user (matches typical IP-based default)
DEFAULT_REQUESTS_PER_MINUTE = 200

# Burst allowance: momentary spike tolerance
DEFAULT_BURST_ALLOWANCE = 50

# Window duration in seconds
WINDOW_SECONDS = 60

# Redis key prefix for per-user rate limit ZSETs
# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------


class PerUserRateLimiter:
    """Per-authenticated-user rate limiter using sliding window counters.

    Thread-safe. Each user_id gets an independent rate limit bucket.
    Users behind shared IPs are tracked separately from IP-based limits.

    When a :class:`~services.cache.redis_backend.RedisBackend` is provided,
    the sliding window is stored through ``limits`` Redis storage. When Redis
    is not available, the same ``limits`` moving-window strategy runs in memory.
    """

    def __init__(
        self,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        burst_allowance: int = DEFAULT_BURST_ALLOWANCE,
        window_seconds: int = WINDOW_SECONDS,
        scope: str = "user",
    ) -> None:
        self._limit = requests_per_minute
        self._burst = burst_allowance
        self._window = window_seconds
        self._scope = scope
        from services.cache.rate_limit import LimitsSlidingWindowStore

        self._memory = LimitsSlidingWindowStore()

        # Redis backend (set asynchronously after init)
        self._redis: RedisBackend | None = None
        self._last_warned_cache: dict[str, float] = {}

    def set_redis(self, redis_backend: RedisBackend | None) -> None:
        """Attach a Redis backend for cross-instance rate limiting."""
        self._redis = redis_backend
        if redis_backend is not None:
            logger.info("Per-user rate limiter upgraded to Redis backend")

    def _effective_limit(self) -> int:
        return self._limit + self._burst

    def _warn_if_needed(self, user_id: str, endpoint: str, *, count: int, effective_limit: int, backend: str) -> None:
        now = time.time()
        last_warned = self._last_warned_cache.get(user_id, 0.0)
        if now - last_warned <= 60:
            return
        backend_label = " (%s)" % backend if backend else ""
        logger.warning(
            "Per-user rate limit exceeded%s: user=%s count=%d limit=%d endpoint=%s",
            backend_label,
            user_id[:12],
            count,
            effective_limit,
            endpoint,
        )
        self._last_warned_cache[user_id] = now

    # -- Async Redis path ---------------------------------------------------

    async def check_async(self, user_id: str, endpoint: str = "") -> tuple[bool, int]:
        """Async rate limit check — uses Redis when available, else in-memory.

        Args:
            user_id: The authenticated user ID.
            endpoint: Optional endpoint path (for logging context).

        Returns:
            Tuple of (allowed, retry_after_seconds).
        """
        if self._redis is not None:
            from services.cache.rate_limit import cloud_rate_limit_fail_closed, redis_rate_limit_enabled

            if redis_rate_limit_enabled():
                allowed, retry_after, redis_error = await self._check_redis(user_id, endpoint)
                if not redis_error or cloud_rate_limit_fail_closed():
                    return allowed, retry_after
        return self.check(user_id, endpoint)

    async def _check_redis(self, user_id: str, endpoint: str = "") -> tuple[bool, int, bool]:
        """Sliding window check via ``limits`` Redis storage."""
        effective_limit = self._effective_limit()
        from services.cache.rate_limit import check_redis_sliding_window

        decision = await check_redis_sliding_window(
            self._redis,
            scope=self._scope,
            identifier=user_id,
            limit=effective_limit,
            window_seconds=self._window,
        )
        if not decision.allowed and not decision.redis_error:
            self._warn_if_needed(
                user_id, endpoint, count=decision.count, effective_limit=effective_limit, backend="Redis"
            )
        return decision.allowed, decision.retry_after, decision.redis_error

    # -- Sync in-memory path -----------------------------------------------

    def check(self, user_id: str, endpoint: str = "") -> tuple[bool, int]:
        """Check if a request is allowed under the user's rate limit.

        Args:
            user_id: The authenticated user ID.
            endpoint: Optional endpoint path (for logging context).

        Returns:
            Tuple of (allowed, retry_after_seconds).
            If allowed is True, retry_after is 0.
            If allowed is False, retry_after is the suggested wait time.
        """
        effective_limit = self._effective_limit()
        decision = self._memory.check(
            scope=self._scope,
            identifier=user_id,
            limit=effective_limit,
            window_seconds=self._window,
        )
        if not decision.allowed:
            self._warn_if_needed(
                user_id,
                endpoint,
                count=decision.count,
                effective_limit=effective_limit,
                backend="",
            )
        return decision.allowed, decision.retry_after

    def record(self, user_id: str) -> None:
        """Record a request for a user without checking the limit.

        Useful when the check was already done at a different layer
        but you still want to track the request count.
        """
        self._memory.record(
            scope=self._scope,
            identifier=user_id,
            limit=self._effective_limit(),
            window_seconds=self._window,
        )

    def get_remaining(self, user_id: str) -> int:
        """Get the number of remaining requests for a user in the current window.

        Args:
            user_id: The authenticated user ID.

        Returns:
            Number of remaining allowed requests.
        """
        effective_limit = self._effective_limit()
        return self._memory.remaining(
            scope=self._scope,
            identifier=user_id,
            limit=effective_limit,
            window_seconds=self._window,
        )

    def reset_user(self, user_id: str) -> None:
        """Reset rate limit state for a specific user.

        Args:
            user_id: The user ID to reset.
        """
        self._memory.clear(
            scope=self._scope,
            identifier=user_id,
            limit=self._effective_limit(),
            window_seconds=self._window,
        )

    def _cleanup_stale_buckets(self, now: float) -> None:
        """Compatibility no-op; ``limits`` storage owns expiry cleanup."""
        del now


# ---------------------------------------------------------------------------
# Module-level Singleton
# ---------------------------------------------------------------------------

_limiter: PerUserRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_user_rate_limiter() -> PerUserRateLimiter:
    """Get or create the global per-user rate limiter."""
    global _limiter
    if _limiter is not None:
        return _limiter

    with _limiter_lock:
        if _limiter is not None:
            return _limiter

        _limiter = PerUserRateLimiter()
        return _limiter


def reset_user_rate_limiter() -> None:
    """Reset global instance (for tests)."""
    global _limiter
    with _limiter_lock:
        _limiter = None
