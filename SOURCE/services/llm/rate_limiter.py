"""
LLM quota accounting for LLM requests.

This module keeps the legacy reserve/settle accounting surface but no longer
enforces daily request or monthly token quotas. Canonical pricing caps are
managed spend caps in billing.plan_limiter; the per-minute circuit breaker is
the anti-abuse rate limit.

Token reservation pattern prevents concurrent requests from exceeding budget:
  1. Estimate token cost before the API call
  2. Reserve (debit) the estimate atomically
  3. Make the API call
  4. Refund the difference between estimate and actual usage

Usage:
    from services.llm.rate_limiter import get_rate_limiter

    limiter = get_rate_limiter()

    # Before API call: reserve estimated tokens
    await limiter.reserve(user_id, estimated_tokens=150)

    try:
        response = await openai_call(...)
        actual_tokens = response.usage.total_tokens
    except Exception:
        actual_tokens = 0
        raise
    finally:
        # After API call: settle the reservation
        await limiter.settle(user_id, estimated_tokens=150, actual_tokens=actual_tokens)
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from config.settings import settings
from core.exceptions import LLMQuotaExceededError
from core.logging_config import get_logger

if TYPE_CHECKING:
    from auth.database import SQLiteAuthDatabase

logger = get_logger(__name__)


def _is_cloud_surface() -> bool:
    return str(getattr(settings, "app_surface", "desktop")).lower() == "cloud"


def _is_pseudo_user_id(user_id: str | None) -> bool:
    return (
        not user_id or user_id.startswith("device-") or user_id.startswith("local-") or user_id.startswith("session:")
    )


@dataclass
class UserLimits:
    """Compatibility shape for callers that still request quota metadata."""

    daily_requests: int
    monthly_tokens: int


class RemainingQuota(TypedDict):
    """Remaining quota information for compatibility callers."""

    remaining_daily_requests: int
    remaining_monthly_tokens: int
    daily_reset_at: str
    monthly_reset_at: str


class LLMRateLimiter:
    """
    Legacy quota accounting for LLM requests with token reservation.

    Uses a reserve/settle pattern to prevent race conditions:
    - reserve(): debits estimated tokens atomically
    - settle(): Refunds the difference between estimate and actual usage

    The old daily/monthly quota settings are retained only for config
    compatibility; they are not enforcement caps.
    """

    # Premium status cache TTL (seconds) -- avoids a DB query per LLM call
    _PREMIUM_CACHE_TTL: float = 120.0  # 2 minutes

    def __init__(self, db: SQLiteAuthDatabase | None = None) -> None:
        self._db = db
        self._reservation_lock = asyncio.Lock()
        # Cache: user_id -> (is_premium, monotonic_timestamp)
        self._premium_cache: dict[str, tuple[bool, float]] = {}

    def _get_db(self) -> SQLiteAuthDatabase:
        """Get the database instance, initializing if needed."""
        if self._db is None:
            from auth.database import get_auth_db

            self._db = get_auth_db()
        return self._db

    def get_user_limits(self, is_premium: bool) -> UserLimits:
        """Return unlimited compatibility limits."""
        del is_premium
        return UserLimits(daily_requests=-1, monthly_tokens=-1)

    async def _resolve_premium(self, user_id: str) -> bool:
        """Auto-resolve premium status from auth DB.

        Returns ``True`` if the user has an active premium subscription,
        ``False`` for unknown or free-tier users.  Device-based IDs
        (``device-*``) are never premium.

        Results are cached for ``_PREMIUM_CACHE_TTL`` seconds to avoid
        a DB query on every LLM call.
        """
        if not user_id or user_id.startswith("device-"):
            return False

        import time

        now = time.monotonic()
        cached = self._premium_cache.get(user_id)
        if cached is not None:
            is_premium, cached_at = cached
            if (now - cached_at) < self._PREMIUM_CACHE_TTL:
                return is_premium

        try:
            db = self._get_db()
            user = await db.users.get_user_by_id(user_id)
            result = bool(
                user is not None
                and (bool(getattr(user, "has_paid_access", False)) or bool(getattr(user, "is_premium", False)))
            )
            self._premium_cache[user_id] = (result, now)
            # Bound cache size
            if len(self._premium_cache) > 1000:
                # Evict oldest entries
                entries = sorted(self._premium_cache.items(), key=lambda x: x[1][1])
                for uid, _ in entries[: len(entries) - 500]:
                    del self._premium_cache[uid]
            return result
        except Exception:
            logger.debug("Could not resolve premium status for %s", user_id)
        return False

    async def reserve(
        self,
        user_id: str,
        estimated_tokens: int,
        is_premium: bool | None = None,
    ) -> None:
        """
        Reserve estimated tokens before an API call.

        This keeps legacy quota counters for telemetry. It does not enforce
        daily request or monthly token caps.

        Args:
            user_id: User ID
            estimated_tokens: Estimated token cost (e.g., settings.llm_max_tokens_cap)
            is_premium: Whether the user has a premium subscription.
                If ``None`` (default), auto-resolved from the auth DB so
                callers don't need to look up subscription status themselves.

        Raises:
            LLMQuotaExceededError: If cloud managed usage has no account user.
        """
        # B1: Cost circuit breaker runs FIRST -- cannot be bypassed by dev_mode
        from services.llm.cost_circuit_breaker import get_circuit_breaker

        circuit_breaker = get_circuit_breaker()
        async_check = getattr(circuit_breaker, "check_and_record_async", None)
        if async_check is not None:
            result = async_check(estimated_tokens)
        else:
            result = circuit_breaker.check_and_record(estimated_tokens)
        if inspect.isawaitable(result):
            await result

        # B4: device-based and desktop-local users cannot be tracked in the
        # DB due to FK constraint on llm_quotas.user_id (requires a real user
        # row).  The cost circuit breaker (above) still protects against
        # runaway usage.  Skip DB-based quota for these users to avoid
        # triggering expensive auth DB init (10s+ icacls + schema migration)
        # on the command processing hot path.
        _skip_db = _is_pseudo_user_id(user_id)
        if _skip_db and _is_cloud_surface():
            safe_user_id = user_id or "<missing>"
            logger.warning("Rejecting cloud LLM reservation for non-account user %s", safe_user_id)
            raise LLMQuotaExceededError(
                user_id=safe_user_id,
                limit_type="authenticated_user_required",
                current=1,
                limit=0,
            )

        # B6: dev_mode only skips legacy quota accounting, NOT the circuit breaker.
        if not settings.llm_rate_limit_enabled or settings.dev_mode:
            return

        if _skip_db:
            logger.debug("Skipping DB quota for non-account user %s (circuit breaker active)", user_id)
            return

        del is_premium

        async with self._reservation_lock:
            db = self._get_db()
            await db.llm_quotas.get_or_create_quota(user_id)
            await db.llm_quotas.increment_usage(user_id, requests=1, tokens=estimated_tokens)

        logger.debug("Reserved %s tokens for user %s", estimated_tokens, user_id)

    async def settle(
        self,
        user_id: str,
        estimated_tokens: int,
        actual_tokens: int,
    ) -> None:
        """
        Settle a token reservation after the API call completes.

        Refunds the difference between the estimated and actual token usage.
        If actual > estimated (should be rare with a proper cap), no refund is
        issued and the overage is absorbed.

        Args:
            user_id: User ID
            estimated_tokens: The amount originally reserved
            actual_tokens: The actual tokens used (from API response)
        """
        # B6: dev_mode skips quota DB writes but we still log the settlement
        if not settings.llm_rate_limit_enabled or settings.dev_mode:
            logger.debug(
                "Settle skipped (dev_mode/disabled): user=%s est=%d actual=%d",
                user_id,
                estimated_tokens,
                actual_tokens,
            )
            return

        refund = estimated_tokens - actual_tokens
        if refund <= 0:
            # Actual usage met or exceeded estimate; nothing to refund.
            # If actual > estimated, the overshoot is small (capped by max_tokens)
            # and will be caught on the next reservation check.
            return

        db = self._get_db()
        await db.llm_quotas.increment_usage(user_id, requests=0, tokens=-refund)

        logger.debug(
            "Settled reservation for user %s: estimated=%s actual=%s refund=%s",
            user_id,
            estimated_tokens,
            actual_tokens,
            refund,
        )

    async def check_quota(self, user_id: str, is_premium: bool | None = None) -> None:
        """
        Compatibility no-op.

        Args:
            user_id: User ID to check
            is_premium: Whether the user has a premium subscription.
                If ``None`` (default), auto-resolved from the auth DB.

        Daily request and monthly token quotas are not enforced.
        """
        del user_id, is_premium
        return

    async def get_remaining_quota(
        self,
        user_id: str,
        is_premium: bool | None = None,
    ) -> RemainingQuota:
        """Get remaining quota for a user.

        Args:
            user_id: User ID to check
            is_premium: Whether the user has a premium subscription.
                If ``None`` (default), auto-resolved from the auth DB.
        """
        db = self._get_db()
        quota = await db.llm_quotas.get_or_create_quota(user_id)
        del is_premium

        return RemainingQuota(
            remaining_daily_requests=-1,
            remaining_monthly_tokens=-1,
            daily_reset_at=quota["daily_reset_at"],
            monthly_reset_at=quota["monthly_reset_at"],
        )


# Thread-safe singleton
_rate_limiter: LLMRateLimiter | None = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> LLMRateLimiter:
    """Get the global rate limiter instance (thread-safe)."""
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_limiter_lock:
            # Double-check after acquiring lock
            if _rate_limiter is None:
                _rate_limiter = LLMRateLimiter()
    return _rate_limiter


__all__ = [
    "LLMRateLimiter",
    "RemainingQuota",
    "UserLimits",
    "get_rate_limiter",
]
