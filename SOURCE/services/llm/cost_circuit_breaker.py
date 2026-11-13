"""Cost Circuit Breaker -- hard safety limits that CANNOT be bypassed by dev_mode.

This module is separate from rate_limiter.py and runs BEFORE any dev_mode check.
It enforces two independent guards:

1. **Per-minute call cap**: sliding-window deque of timestamps.  Default 30/min.
2. **Monthly estimated cost cap**: accumulates estimated USD cost per call using a
   conservative per-token cost model.  Default $10/month.  Resets on the first
   of the calendar month at 00:00 UTC.

Both caps are GLOBAL (process-wide) by default.  The per-minute call cap can
also be counted per ``scope`` (for example one window per account); no caller
passes a scope yet, so today the cloud's 30 calls a minute are shared by every
account.  Per-user managed spend caps are handled by plan_limiter; legacy
quota counters are telemetry.

A caller that is refused by the per-minute cap is told how long to wait
(``retry_after_seconds``).  ``call_waiting_out_per_minute_limit`` is the
desktop side of that: it waits for a free slot and sends the same turn again
instead of ending the task, so the cap slows a busy run down but still never
lets it make more calls per minute than the cap allows.

Usage:
    from services.llm.cost_circuit_breaker import get_circuit_breaker

    breaker = get_circuit_breaker()
    breaker.check_and_record()  # raises CostCircuitBreakerError if tripped
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
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


PER_MINUTE_LIMIT_TYPE = "per_minute_calls"
_PER_MINUTE_WINDOW_S = 60.0
# How long one model turn may wait for a free per-minute slot before the task
# is stopped after all.  Longer than two full windows, so a turn only gives up
# when the cap stayed full the whole time.
PER_MINUTE_WAIT_BUDGET_S = 150.0
_PER_MINUTE_WAIT_STEP_MAX_S = 30.0
_PER_MINUTE_WAIT_DEFAULT_STEPS_S = (5.0, 10.0, 15.0, 20.0)
# The reason text an older cloud sends without ``limit_type`` (see
# ``per_minute_denial_wait_seconds``).
_PER_MINUTE_REASON_PREFIX = "Per-minute LLM call limit reached"
# Looked up at call time so tests can replace the wait.
_sleep = asyncio.sleep


def _cost_limit_error(
    reason: str,
    *,
    limit_type: str,
    retry_after_seconds: float = 0.0,
) -> CostCircuitBreakerError:
    exc = CostCircuitBreakerError(reason, limit_type=limit_type)
    exc.retry_after_seconds = max(0.0, float(retry_after_seconds))  # type: ignore[attr-defined]
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
        # Per-minute sliding window of the process-wide scope ("")
        self._call_timestamps: deque[float] = deque()
        # Per-minute sliding windows of named scopes (cloud: one per account)
        self._scoped_call_timestamps: dict[str, deque[float]] = {}
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

    def _window_for_scope(self, scope: str, now: float) -> deque[float]:
        """The per-minute window of ``scope`` with expired calls removed (lock held)."""
        if not scope:
            window = self._call_timestamps
        else:
            existing = self._scoped_call_timestamps.get(scope)
            if existing is None:
                # Drop other scopes' expired windows so idle accounts cost nothing.
                expired = [
                    key
                    for key, calls in self._scoped_call_timestamps.items()
                    if not calls or (now - calls[-1]) >= _PER_MINUTE_WINDOW_S
                ]
                for key in expired:
                    del self._scoped_call_timestamps[key]
                existing = self._scoped_call_timestamps.setdefault(scope, deque())
            window = existing
        while window and (now - window[0]) >= _PER_MINUTE_WINDOW_S:
            window.popleft()
        return window

    def check_and_record(self, estimated_tokens: int = 150, *, scope: str = "") -> None:
        """Check hard limits and record this call.  Raises on breach.

        Args:
            estimated_tokens: Estimated total tokens for this call.
            scope: Whose per-minute window this call counts in.  Empty (the
                desktop app) is the whole process; the cloud passes the
                account id.  The monthly cost cap is always process-wide.

        Raises:
            CostCircuitBreakerError: If per-minute or monthly cost cap is exceeded.
        """
        with self._lock:
            self._maybe_reset_monthly()
            now = time.monotonic()

            # --- Per-minute cap ---
            cap = settings.llm_cost_per_minute_cap
            window = self._window_for_scope(scope, now)

            if len(window) >= cap:
                logger.warning(
                    "Cost circuit breaker: per-minute cap hit (%d/%d)",
                    len(window),
                    cap,
                )
                reason = "Per-minute LLM call limit reached (%d calls in 60s). Wait a moment before trying again." % cap
                # The oldest call in the window leaves it first: that is when a slot frees.
                retry_after = (_PER_MINUTE_WINDOW_S - (now - window[0])) if window else _PER_MINUTE_WINDOW_S
                raise _cost_limit_error(
                    reason,
                    limit_type=PER_MINUTE_LIMIT_TYPE,
                    retry_after_seconds=max(1.0, retry_after),
                )

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
            window.append(now)
            self._monthly_cost_usd += est_cost
            self._monthly_call_count += 1

    async def check_and_record_async(self, estimated_tokens: int = 150, *, scope: str = "") -> None:
        """Async Redis-aware version of check_and_record for LLM hot paths."""
        if self._redis is None:
            self.check_and_record(estimated_tokens, scope=scope)
            return

        from services.cache.rate_limit import (
            check_redis_sliding_window,
            cloud_rate_limit_fail_closed,
            increment_redis_fixed_window,
            redis_rate_limit_enabled,
        )

        if not redis_rate_limit_enabled():
            self.check_and_record(estimated_tokens, scope=scope)
            return

        cap = settings.llm_cost_per_minute_cap
        minute_decision = await check_redis_sliding_window(
            self._redis,
            scope="llm.cost.minute",
            identifier=scope or "global",
            limit=cap,
            window_seconds=60,
        )
        if not minute_decision.allowed:
            if minute_decision.redis_error and not cloud_rate_limit_fail_closed():
                self.check_and_record(estimated_tokens, scope=scope)
                return
            logger.warning(
                "Cost circuit breaker: per-minute cap hit (%d/%d calls)",
                minute_decision.count,
                cap,
            )
            reason = "Per-minute LLM call limit reached (%d calls in 60s). Wait a moment before trying again." % cap
            raise _cost_limit_error(
                reason,
                limit_type=PER_MINUTE_LIMIT_TYPE,
                retry_after_seconds=max(1.0, float(minute_decision.retry_after or 0)),
            )

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
                self.check_and_record(estimated_tokens, scope=scope)
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


def per_minute_denial_wait_seconds(response: object, attempt: int = 0) -> float | None:
    """How long to wait before resending a turn the per-minute cap refused.

    ``response`` is what the provider returned for the turn.  Returns ``None``
    when it is anything other than a per-minute refusal (an answer, a tool
    call, a spend-cap or monthly-cap refusal): those are never waited out.

    The managed cloud answers a refused turn with ``_policy_denial`` and
    ``error == "cost_circuit_breaker"``.  A cloud from 1.0.5 build 11 on also
    sends ``limit_type`` and ``retry_after_seconds``; an older one sends only
    its own fixed reason text, which is what the last check reads.
    """
    if not isinstance(response, dict) or not response.get("_policy_denial"):
        return None
    if str(response.get("error") or "") != "cost_circuit_breaker":
        return None
    limit_type = str(response.get("limit_type") or "")
    if limit_type:
        if limit_type != PER_MINUTE_LIMIT_TYPE:
            return None
    elif not str(response.get("answer") or "").startswith(_PER_MINUTE_REASON_PREFIX):
        return None
    try:
        told = float(response.get("retry_after_seconds") or 0.0)
    except (TypeError, ValueError):
        told = 0.0
    if told <= 0.0:
        steps = _PER_MINUTE_WAIT_DEFAULT_STEPS_S
        told = steps[min(max(0, attempt), len(steps) - 1)]
    return min(_PER_MINUTE_WAIT_STEP_MAX_S, max(1.0, told))


async def call_waiting_out_per_minute_limit(
    call: Callable[[], Awaitable[Any]],
    *,
    cancelled: Callable[[], bool] | None = None,
    budget_s: float = PER_MINUTE_WAIT_BUDGET_S,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    label: str = "",
) -> Any:
    """Run one model turn; when the per-minute cap refuses it, wait and resend.

    The cap is unchanged: a refused turn was never sent to the model, and it
    is sent again only after the wait the cap asked for.  Anything else the
    turn returns (or raises) passes straight through.  After ``budget_s`` of
    waiting, or once ``cancelled()`` is true, the refusal itself is returned
    and the caller ends the task as before.
    """
    pause = sleep or _sleep
    waited = 0.0
    attempt = 0
    while True:
        response = await call()
        wait_s = per_minute_denial_wait_seconds(response, attempt)
        if wait_s is None:
            if attempt:
                logger.info(
                    "Per-minute call limit: %s went through after waiting %.0fs (%d waits)",
                    label or "turn",
                    waited,
                    attempt,
                )
            return response
        # Helpers that were refused together must not all come back together.
        wait_s += random.uniform(0.0, 2.0)  # noqa: S311 - retry spacing, not security
        if waited + wait_s > budget_s or (cancelled is not None and cancelled()):
            logger.warning(
                "Per-minute call limit: %s still refused after waiting %.0fs (%d waits); stopping",
                label or "turn",
                waited,
                attempt,
            )
            return response
        attempt += 1
        logger.info(
            "Per-minute call limit reached; %s waits %.0fs for a free slot (wait %d, %.0fs so far)",
            label or "turn",
            wait_s,
            attempt,
            waited,
        )
        remaining = wait_s
        while remaining > 0:
            step = min(1.0, remaining)
            await pause(step)
            remaining -= step
            if cancelled is not None and cancelled():
                return response
        waited += wait_s


__all__ = [
    "PER_MINUTE_LIMIT_TYPE",
    "PER_MINUTE_WAIT_BUDGET_S",
    "CostCircuitBreaker",
    "build_cost_limit_frame",
    "call_waiting_out_per_minute_limit",
    "get_circuit_breaker",
    "per_minute_denial_wait_seconds",
]
