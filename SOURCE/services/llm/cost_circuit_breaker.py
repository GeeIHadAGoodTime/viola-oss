"""Cost Circuit Breaker -- hard safety limits that CANNOT be bypassed by dev_mode.

This module is separate from rate_limiter.py and runs BEFORE any dev_mode check.
It enforces two independent guards:

1. **Per-minute call cap**: sliding-window deque of timestamps.  Default 30/min.
2. **Monthly estimated cost cap**: accumulates estimated USD cost per call using a
   conservative per-token cost model.  Default $10/month.  Resets on the first
   of the calendar month at 00:00 UTC.

These are GLOBAL (process-wide) infrastructure safety nets.  Per-user managed
spend caps are handled by plan_limiter; legacy quota counters are telemetry.

Usage:
    from services.llm.cost_circuit_breaker import get_circuit_breaker

    breaker = get_circuit_breaker()
    breaker.check_and_record()  # raises CostCircuitBreakerError if tripped
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from config.defaults import DEFAULT_GPT_MODEL
from config.settings import settings
from core.exceptions import CostCircuitBreakerError
from core.logging_config import get_logger
from services.conversation.context_frames import Frame, FrameKind, system_reminder_frame
from services.llm.pricing import get_llm_pricing_usd

logger = get_logger(__name__)

# Rough cost model (USD per 1K tokens) -- conservative estimates.
# Anchor to the canonical default managed model instead of duplicating a stale
# pricing table here, so baseline safety tracks the actual product default.
_BASELINE_MODEL = DEFAULT_GPT_MODEL
_BASELINE_PRICING_USD_PER_1M = get_llm_pricing_usd(_BASELINE_MODEL)
_COST_PER_1K_INPUT = _BASELINE_PRICING_USD_PER_1M["input"] / 1000
_COST_PER_1K_OUTPUT = _BASELINE_PRICING_USD_PER_1M["output"] / 1000


def build_cost_limit_frame(
    *,
    reason: str,
    limit_type: str,
    session_id: str | None = None,
) -> Frame:
    """Build the canonical model-visible stop frame for cost safety trips."""

    return system_reminder_frame(
        kind=FrameKind.SYSTEM_REMINDER,
        text="LLM request stopped by Viola's cost safety limit (%s): %s" % (limit_type, reason),
        source_tag="llm_cost_limit",
        origin="provider_policy",
        session_id=session_id,
    )


def _cost_limit_error(reason: str, *, limit_type: str) -> CostCircuitBreakerError:
    exc = CostCircuitBreakerError(reason, limit_type=limit_type)
    exc.viola_stop_reason = "cost_limit"  # type: ignore[attr-defined]
    exc.viola_error_category = "cost_limit"  # type: ignore[attr-defined]
    exc.viola_failure_frames = [  # type: ignore[attr-defined]
        build_cost_limit_frame(reason=reason, limit_type=limit_type)
    ]
    return exc


def _seconds_until_next_month_utc() -> int:
    now = datetime.now(UTC)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_month - now).total_seconds()))


class CostCircuitBreaker:
    """Hard cost limiter that cannot be disabled by dev_mode.

    This is a GLOBAL infrastructure safety net.  It protects against runaway
    processes, not individual users.  Per-user managed spend caps are enforced
    by plan_limiter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-minute sliding window
        self._call_timestamps: deque[float] = deque()
        # Monthly cost accumulator
        self._monthly_cost_usd: float = 0.0
        self._monthly_call_count: int = 0
        self._cost_reset_month: str = datetime.now(UTC).strftime("%Y-%m")
        self._redis: Any | None = None

    def set_redis(self, redis_backend: Any | None) -> None:
        """Attach Redis for cloud-wide circuit-breaker counters."""
        self._redis = redis_backend
        if redis_backend is not None:
            logger.info("LLM cost circuit breaker upgraded to Redis backend")

    def _maybe_reset_monthly(self) -> None:
        """Reset monthly counters when the UTC calendar month changes."""
        month = datetime.now(UTC).strftime("%Y-%m")
        if month != self._cost_reset_month:
            self._monthly_cost_usd = 0.0
            self._monthly_call_count = 0
            self._cost_reset_month = month

    def check_and_record(self, estimated_tokens: int = 150) -> None:
        """Check hard limits and record this call.  Raises on breach.

        Args:
            estimated_tokens: Estimated total tokens for this call.

        Raises:
            CostCircuitBreakerError: If per-minute or monthly cost cap is exceeded.
        """
        with self._lock:
            self._maybe_reset_monthly()
            now = time.monotonic()

            # --- Per-minute cap ---
            cap = settings.llm_cost_per_minute_cap
            # Evict timestamps older than 60s
            while self._call_timestamps and (now - self._call_timestamps[0]) >= 60.0:
                self._call_timestamps.popleft()

            if len(self._call_timestamps) >= cap:
                logger.warning(
                    "Cost circuit breaker: per-minute cap hit (%d/%d)",
                    len(self._call_timestamps),
                    cap,
                )
                reason = "Per-minute LLM call limit reached (%d calls in 60s). Wait a moment before trying again." % cap
                raise _cost_limit_error(reason, limit_type="per_minute_calls")

            # --- Monthly cost cap ---
            est_input = estimated_tokens * 0.7  # rough input/output split
            est_output = estimated_tokens * 0.3
            est_cost = (est_input / 1000) * _COST_PER_1K_INPUT + (est_output / 1000) * _COST_PER_1K_OUTPUT

            monthly_cap = settings.llm_monthly_cost_cap_usd
            if self._monthly_cost_usd + est_cost > monthly_cap:
                logger.warning(
                    "Cost circuit breaker: monthly cost cap hit ($%.2f/$%.2f)",
                    self._monthly_cost_usd,
                    monthly_cap,
                )
                reason = (
                    "Monthly cost limit reached ($%.2f of $%.2f). Resets on the first of next month at 00:00 UTC."
                    % (
                        self._monthly_cost_usd,
                        monthly_cap,
                    )
                )
                raise _cost_limit_error(reason, limit_type="monthly_cost_usd")

            # Record the call
            self._call_timestamps.append(now)
            self._monthly_cost_usd += est_cost
            self._monthly_call_count += 1

    async def check_and_record_async(self, estimated_tokens: int = 150) -> None:
        """Async Redis-aware version of check_and_record for LLM hot paths."""
        if self._redis is None:
            self.check_and_record(estimated_tokens)
            return

        from services.cache.rate_limit import (
            check_redis_sliding_window,
            cloud_rate_limit_fail_closed,
            increment_redis_fixed_window,
            redis_rate_limit_enabled,
        )

        if not redis_rate_limit_enabled():
            self.check_and_record(estimated_tokens)
            return

        cap = settings.llm_cost_per_minute_cap
        minute_decision = await check_redis_sliding_window(
            self._redis,
            scope="llm.cost.minute",
            identifier="global",
            limit=cap,
            window_seconds=60,
        )
        if not minute_decision.allowed:
            if minute_decision.redis_error and not cloud_rate_limit_fail_closed():
                self.check_and_record(estimated_tokens)
                return
            logger.warning(
                "Cost circuit breaker: per-minute cap hit (%d/%d calls)",
                minute_decision.count,
                cap,
            )
            reason = "Per-minute LLM call limit reached (%d calls in 60s). Wait a moment before trying again." % cap
            raise _cost_limit_error(reason, limit_type="per_minute_calls")

        est_input = estimated_tokens * 0.7
        est_output = estimated_tokens * 0.3
        est_cost = (est_input / 1000) * _COST_PER_1K_INPUT + (est_output / 1000) * _COST_PER_1K_OUTPUT
        monthly_cap = settings.llm_monthly_cost_cap_usd
        ttl = _seconds_until_next_month_utc()
        month = datetime.now(UTC).strftime("%Y-%m")
        monthly_decision = await increment_redis_fixed_window(
            self._redis,
            scope="llm.cost.monthly_usd",
            identifier=month,
            amount=est_cost,
            limit=monthly_cap,
            ttl_seconds=ttl,
            key_window=ttl,
        )
        if not monthly_decision.allowed:
            if monthly_decision.redis_error and not cloud_rate_limit_fail_closed():
                self.check_and_record(estimated_tokens)
                return
            logger.warning(
                "Cost circuit breaker: monthly cost cap hit ($%.2f/$%.2f)",
                monthly_decision.value,
                monthly_cap,
            )
            reason = "Monthly cost limit reached ($%.2f of $%.2f). Resets on the first of next month at 00:00 UTC." % (
                monthly_decision.value,
                monthly_cap,
            )
            raise _cost_limit_error(reason, limit_type="monthly_cost_usd")

    def record_agent_iteration(self, is_premium: bool = False) -> None:
        """Compatibility no-op.

        Agent task/chain count caps are not part of the canonical cap model.
        Runtime loop protection still happens via the per-task max-iteration
        safety limit owned by the agent executor.
        """
        del is_premium

    @property
    def monthly_cost(self) -> float:
        """Current estimated monthly cost in USD."""
        with self._lock:
            self._maybe_reset_monthly()
            return self._monthly_cost_usd

    async def record_agent_iteration_async(self, is_premium: bool = False) -> None:
        """Compatibility no-op for async agent loops."""
        self.record_agent_iteration(is_premium)

    @property
    def daily_cost(self) -> float:
        """Compatibility alias for old diagnostics."""
        return self.monthly_cost

    @property
    def daily_agent_iterations(self) -> int:
        """Compatibility alias; count enforcement was removed."""
        return 0


# Thread-safe singleton
_breaker: CostCircuitBreaker | None = None
_breaker_lock = threading.Lock()


def get_circuit_breaker() -> CostCircuitBreaker:
    """Get the global cost circuit breaker instance (thread-safe)."""
    global _breaker
    if _breaker is None:
        with _breaker_lock:
            if _breaker is None:
                _breaker = CostCircuitBreaker()
    return _breaker


__all__ = [
    "CostCircuitBreaker",
    "build_cost_limit_frame",
    "get_circuit_breaker",
]
