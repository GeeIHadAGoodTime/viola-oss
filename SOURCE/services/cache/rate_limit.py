"""Shared library-backed rate-limit primitives.

The Redis backend is used for multi-instance cloud correctness. Desktop and
test runs can keep the in-memory backend by setting VIOLA_RATE_LIMITER_BACKEND
to ``memory``. Storage and moving/fixed-window mechanics are delegated to the
``limits`` library so Viola does not own hand-written Redis rate-limit Lua.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Any

from limits import RateLimitItemPerSecond
from limits.errors import ConfigurationError, StorageError
from limits.storage import MemoryStorage, RedisStorage as LimitsRedisStorage
from limits.strategies import FixedWindowRateLimiter, MovingWindowRateLimiter

from core.logging_config import get_logger

logger = get_logger(__name__)

RATE_LIMIT_KEY_PREFIX = "vio:ratelimit"
_COUNTER_SCALE = 1_000_000
_SYNC_REDIS_STORAGE: LimitsRedisStorage | None = None
_SYNC_REDIS_UNAVAILABLE = False
_SYNC_REDIS_LOCK = threading.Lock()
_RATE_LIMIT_STORAGE_ERRORS = (ConfigurationError, StorageError, RuntimeError, OSError, ValueError, TypeError)

# A transient Redis-store blip (embedded-DNS name-resolution failure, a dropped
# idle connection, a 1-round-trip timeout) should not surface as a limiter error
# on the first try — retry a couple of times with a tiny backoff so the vast
# majority of blips are absorbed BEFORE the fail-open/closed policy is consulted.
# Runs inside asyncio.to_thread, so the blocking sleep does not stall the loop.
_REDIS_OP_ATTEMPTS = 3
_REDIS_OP_BACKOFF_SECONDS = 0.05

# Reading AppConfig or emitting a best-effort instrumentation signal must never
# raise into the limiter hot path; these are the realistic failure modes to
# swallow (import/attribute/type/value/runtime/OS errors) without a bare-Exception
# catch that would also mask programming bugs.
_SETTINGS_READ_ERRORS = (ImportError, AttributeError, RuntimeError, ValueError, TypeError, OSError)


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    count: int = 0
    redis_error: bool = False


@dataclass(frozen=True, slots=True)
class CounterLimitDecision:
    allowed: bool
    value: float = 0.0
    retry_after: int = 0
    redis_error: bool = False


def configured_rate_limiter_backend() -> str:
    try:
        from config.settings import settings

        backend = str(getattr(settings, "rate_limiter_backend", "memory") or "memory").strip().lower()
    except Exception:
        return "memory"
    return "redis" if backend == "redis" else "memory"


def redis_rate_limit_enabled() -> bool:
    return configured_rate_limiter_backend() == "redis"


def rate_limit_fail_open_on_error() -> bool:
    """Policy: when the Redis store is unreachable (after retries), allow or block?

    ``True`` (default, availability-first) means the request is served — the IP
    limiter degrades to an in-memory per-instance window (still bounded) and the
    counter limiters allow-and-log. ``False`` means fail closed on the cloud
    surface. Controlled by ``VIOLA_RATE_LIMIT_FAIL_OPEN``; see the settings
    field for the full security tradeoff. Fails safe (open) if settings cannot
    be read, matching desktop/in-memory behavior.
    """
    try:
        from config.settings import settings

        return bool(getattr(settings, "rate_limit_fail_open", True))
    except _SETTINGS_READ_ERRORS:
        return True


def cloud_rate_limit_fail_closed() -> bool:
    """Whether a Redis-store error must BLOCK the request (fail closed).

    Only the cloud production surface with the Redis backend is a candidate, and
    even there we fail closed ONLY when the operator has explicitly disabled
    fail-open (``VIOLA_RATE_LIMIT_FAIL_OPEN=false``). By default this returns
    ``False`` everywhere, so a transient Redis/DNS blip degrades gracefully
    instead of amplifying into a total auth/phone outage. Desktop and dev never
    fail closed.
    """
    try:
        from config.settings import settings

        surface = str(getattr(settings, "app_surface", "desktop")).strip().lower()
        env = str(getattr(settings, "env", "dev")).strip().lower()
    except _SETTINGS_READ_ERRORS:
        return False
    if not (surface == "cloud" and env != "dev" and redis_rate_limit_enabled()):
        return False
    return not rate_limit_fail_open_on_error()


def _record_rate_limit_degraded(scope: str, operation: str, *, fail_open: bool) -> None:
    """Emit an abuse-signal metric when a Redis-store error forces a fail decision.

    The IP path degrading to its in-memory fallback (fail_open) is bounded; the
    counter paths serving unlimited (fail_open) or blocking (not fail_open) are
    the operationally interesting cases. Recorded as ``critical`` so it surfaces
    on the abuse dashboard and the operator can correlate with a Redis outage.

    Only fires when the Redis backend is actually the configured limiter store:
    on desktop/in-memory deployments a missing Redis is expected, not a degrade.
    """
    if not redis_rate_limit_enabled():
        return
    try:
        from admin.instrumentation import record_abuse_signal

        record_abuse_signal(
            "rate_limit_redis_degraded",
            severity="critical",
            details={
                "scope": scope,
                "operation": operation,
                "posture": "open" if fail_open else "closed",
            },
        )
    except _SETTINGS_READ_ERRORS as exc:
        logger.debug("rate_limit_redis_degraded signal emission skipped: %s", exc)


def rate_limit_identifier(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]


def _window_label(window_seconds: int | float | str) -> str:
    if isinstance(window_seconds, str):
        stripped = window_seconds.strip().lower().replace(" ", "_").replace(":", ".")
        if not stripped:
            return "0"
        try:
            return str(int(float(stripped)))
        except ValueError:
            return stripped
    return str(int(float(window_seconds)))


def rate_limit_key(scope: str, identifier: str, window_seconds: int | float | str) -> str:
    normalized_scope = scope.strip().lower().replace(" ", "_").replace(":", ".")
    return "%s:%s:%s:%s" % (
        RATE_LIMIT_KEY_PREFIX,
        normalized_scope,
        _window_label(window_seconds),
        rate_limit_identifier(identifier),
    )


def _normalized_scope(scope: str) -> str:
    return scope.strip().lower().replace(" ", "_").replace(":", ".")


def _moving_window_item(
    *,
    limit: int,
    window_seconds: int | float,
) -> RateLimitItemPerSecond:
    return RateLimitItemPerSecond(
        max(0, int(limit)),
        max(1, int(window_seconds)),
        namespace=RATE_LIMIT_KEY_PREFIX,
    )


class _FixedWindowItem(RateLimitItemPerSecond):
    """RateLimitItem that keeps key-window and TTL separate."""

    __slots__ = ("_expiry_seconds",)

    def __init__(
        self,
        amount: int,
        *,
        expiry_seconds: int,
        key_window_seconds: int,
    ) -> None:
        super().__init__(
            amount,
            max(1, key_window_seconds),
            namespace=RATE_LIMIT_KEY_PREFIX,
        )
        self._expiry_seconds = max(1, int(expiry_seconds))

    def get_expiry(self) -> int:
        return self._expiry_seconds


def _identifiers(scope: str, identifier: str) -> tuple[str, str]:
    return (_normalized_scope(scope), rate_limit_identifier(identifier))


def _retry_after(reset_time: int | float, fallback_seconds: int | float) -> int:
    return max(1, math.ceil(float(reset_time) - time.time()) or int(fallback_seconds) or 1)


def _window_count(limiter: MovingWindowRateLimiter, item: RateLimitItemPerSecond, identifiers: tuple[str, str]) -> int:
    stats = limiter.get_window_stats(item, *identifiers)
    return max(0, item.amount - int(stats.remaining))


def _record_for_existing_window(
    storage: Any,
    key: str,
    *,
    expiry_seconds: int,
) -> None:
    _window_start, current = storage.get_moving_window(key, current_limit := 2_147_483_647, expiry_seconds)
    storage.acquire_entry(key, min(current + 1, current_limit), expiry_seconds)


def _sliding_window_decision(
    limiter: MovingWindowRateLimiter,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
    record: bool,
    record_rejected: bool = False,
) -> RateLimitDecision:
    item = _moving_window_item(limit=limit, window_seconds=window_seconds)
    identifiers = _identifiers(scope, identifier)
    if item.amount <= 0:
        return RateLimitDecision(allowed=False, retry_after=max(1, int(window_seconds)), count=0)

    allowed = limiter.hit(item, *identifiers) if record else limiter.test(item, *identifiers)
    if record and not allowed and record_rejected:
        _record_for_existing_window(
            limiter.storage,
            item.key_for(*identifiers),
            expiry_seconds=item.get_expiry(),
        )
    stats = limiter.get_window_stats(item, *identifiers)
    count = _window_count(limiter, item, identifiers)
    retry_after = 0 if allowed else _retry_after(stats.reset_time, window_seconds)
    return RateLimitDecision(allowed=allowed, retry_after=retry_after, count=count)


def _scaled_counter(value: float) -> int:
    if value <= 0:
        return 0
    return max(1, math.ceil(value * _COUNTER_SCALE))


def _unscaled_counter(value: int) -> float:
    return float(value) / _COUNTER_SCALE


def _fixed_window_decision(
    limiter: FixedWindowRateLimiter,
    *,
    scope: str,
    identifier: str,
    amount: float,
    limit: float,
    ttl_seconds: int,
    key_window: int | float | str | None = None,
) -> CounterLimitDecision:
    amount_scaled = _scaled_counter(float(amount))
    limit_scaled = _scaled_counter(float(limit))
    key_window_seconds = int(float(key_window if key_window is not None else ttl_seconds))
    item = _FixedWindowItem(
        limit_scaled,
        expiry_seconds=int(ttl_seconds),
        key_window_seconds=key_window_seconds,
    )
    identifiers = _identifiers(scope, identifier)
    key = item.key_for(*identifiers)

    if limit_scaled <= 0 or amount_scaled > limit_scaled or not limiter.test(item, *identifiers, cost=amount_scaled):
        value = limiter.storage.get(key)
        stats = limiter.get_window_stats(item, *identifiers)
        return CounterLimitDecision(
            allowed=False,
            value=_unscaled_counter(value),
            retry_after=_retry_after(stats.reset_time, ttl_seconds),
        )

    allowed = limiter.hit(item, *identifiers, cost=amount_scaled)
    value = limiter.storage.get(key)
    stats = limiter.get_window_stats(item, *identifiers)
    return CounterLimitDecision(
        allowed=allowed,
        value=_unscaled_counter(value),
        retry_after=0 if allowed else _retry_after(stats.reset_time, ttl_seconds),
    )


class LimitsSlidingWindowStore:
    """Small wrapper around ``limits`` moving-window strategy."""

    def __init__(self, *, storage: Any | None = None, record_rejected: bool = False) -> None:
        self._storage = storage if storage is not None else MemoryStorage()
        self._limiter = MovingWindowRateLimiter(self._storage)
        self._record_rejected = record_rejected

    def check(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int | float,
    ) -> RateLimitDecision:
        return _sliding_window_decision(
            self._limiter,
            scope=scope,
            identifier=identifier,
            limit=limit,
            window_seconds=window_seconds,
            record=True,
            record_rejected=self._record_rejected,
        )

    def peek(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int | float,
    ) -> RateLimitDecision:
        return _sliding_window_decision(
            self._limiter,
            scope=scope,
            identifier=identifier,
            limit=limit,
            window_seconds=window_seconds,
            record=False,
        )

    def record(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int | float,
    ) -> None:
        item = _moving_window_item(limit=limit, window_seconds=window_seconds)
        key = item.key_for(*_identifiers(scope, identifier))
        _record_for_existing_window(self._storage, key, expiry_seconds=item.get_expiry())

    def remaining(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int | float,
    ) -> int:
        decision = self.peek(scope=scope, identifier=identifier, limit=limit, window_seconds=window_seconds)
        return max(0, int(limit) - decision.count)

    def clear(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int | float,
    ) -> int:
        item = _moving_window_item(limit=limit, window_seconds=window_seconds)
        identifiers = _identifiers(scope, identifier)
        before = _window_count(self._limiter, item, identifiers)
        self._limiter.clear(item, *identifiers)
        return before


def _redis_url() -> str:
    from config.settings import settings

    return (getattr(settings, "redis_url", None) or "").strip()


def _redis_retryable_errors() -> list[type[Exception]]:
    """redis-py exception types a single blip should retry rather than fail on."""
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

        return [RedisConnectionError, RedisTimeoutError]
    except ImportError:
        return []


def _redis_connect_retry() -> Any:
    """A redis-py ``Retry`` with exponential backoff, or ``None`` if unavailable."""
    try:
        from redis.backoff import ExponentialBackoff
        from redis.retry import Retry

        return Retry(ExponentialBackoff(cap=0.5, base=0.05), retries=2)
    except ImportError:
        return None


def _get_sync_redis_storage() -> LimitsRedisStorage | None:
    """Return a cached ``limits`` Redis storage instance for rate limiters."""
    global _SYNC_REDIS_STORAGE, _SYNC_REDIS_UNAVAILABLE

    if _SYNC_REDIS_UNAVAILABLE:
        return None
    if _SYNC_REDIS_STORAGE is not None:
        return _SYNC_REDIS_STORAGE

    with _SYNC_REDIS_LOCK:
        if _SYNC_REDIS_STORAGE is not None:
            return _SYNC_REDIS_STORAGE
        if _SYNC_REDIS_UNAVAILABLE:
            return None

        try:
            url = _redis_url()
        except (AttributeError, RuntimeError, ValueError):
            url = ""
        if not url:
            _SYNC_REDIS_UNAVAILABLE = True
            return None

        try:
            _SYNC_REDIS_STORAGE = LimitsRedisStorage(
                url,
                wrap_exceptions=True,
                decode_responses=True,
                # Bumped 1s -> 3s: a 1s budget could not absorb an embedded-DNS
                # re-resolution plus TCP connect on a busy host, so a blip that
                # would have resolved in ~1.2s tipped the limiter into its fail
                # branch. 3s still bounds request latency during a real outage.
                socket_connect_timeout=3,
                socket_timeout=3,
                retry_on_timeout=True,
                # Resolve-once-and-survive-blips: keep pooled connections warm
                # (TCP keepalive) and PING-validate any idle connection before
                # reuse, so a stale/half-open socket is replaced transparently
                # instead of hanging until socket_timeout and fail-tripping the
                # limiter. The pool re-resolves the redis hostname only when it
                # must open a NEW connection — a live keepalive connection does
                # not depend on the flaky embedded DNS every call.
                health_check_interval=30,
                socket_keepalive=True,
                # In-client retry with exponential backoff on connection/timeout
                # errors (which is how an embedded-DNS name-resolution failure
                # surfaces) so a single blip reconnects within the redis-py call
                # rather than propagating as a StorageError.
                retry=_redis_connect_retry(),
                retry_on_error=_redis_retryable_errors(),
            )
            return _SYNC_REDIS_STORAGE
        except _RATE_LIMIT_STORAGE_ERRORS as exc:
            _SYNC_REDIS_UNAVAILABLE = True
            logger.warning("Limits Redis rate limit storage unavailable: %s", exc)
            return None


def _limits_storage_from_backend(redis_backend: Any) -> Any | None:
    storage = getattr(redis_backend, "limits_storage", None)
    if storage is not None:
        return storage() if callable(storage) else storage
    return _get_sync_redis_storage()


def _redis_sliding_window_decision(
    redis_backend: Any,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
    record: bool,
    fail_open_on_error: bool | None,
) -> RateLimitDecision:
    window = int(window_seconds)
    operation = "check" if record else "peek"

    def _degraded_decision() -> RateLimitDecision:
        allowed = not cloud_rate_limit_fail_closed() if fail_open_on_error is None else fail_open_on_error
        _record_rate_limit_degraded(scope, operation, fail_open=allowed)
        return RateLimitDecision(
            allowed=allowed,
            retry_after=max(1, window),
            redis_error=True,
        )

    storage = _limits_storage_from_backend(redis_backend)
    if storage is None:
        return _degraded_decision()

    last_exc: Exception | None = None
    for attempt in range(_REDIS_OP_ATTEMPTS):
        try:
            limiter = MovingWindowRateLimiter(storage)
            return _sliding_window_decision(
                limiter,
                scope=scope,
                identifier=identifier,
                limit=limit,
                window_seconds=window_seconds,
                record=record,
            )
        except _RATE_LIMIT_STORAGE_ERRORS as exc:
            last_exc = exc
            if attempt + 1 < _REDIS_OP_ATTEMPTS:
                time.sleep(_REDIS_OP_BACKOFF_SECONDS * (attempt + 1))

    logger.warning(
        "Redis rate limit %s failed for scope=%s after %d attempts: %s",
        operation,
        scope,
        _REDIS_OP_ATTEMPTS,
        last_exc,
    )
    return _degraded_decision()


async def check_redis_sliding_window(
    redis_backend: Any,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
    fail_open_on_error: bool | None = None,
) -> RateLimitDecision:
    """Check and record one sliding-window request using ``limits`` storage."""
    return await asyncio.to_thread(
        _redis_sliding_window_decision,
        redis_backend,
        scope=scope,
        identifier=identifier,
        limit=limit,
        window_seconds=window_seconds,
        record=True,
        fail_open_on_error=fail_open_on_error,
    )


async def peek_redis_sliding_window(
    redis_backend: Any,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
    fail_open_on_error: bool | None = None,
) -> RateLimitDecision:
    """Check a sliding-window bucket without recording a new request."""
    return await asyncio.to_thread(
        _redis_sliding_window_decision,
        redis_backend,
        scope=scope,
        identifier=identifier,
        limit=limit,
        window_seconds=window_seconds,
        record=False,
        fail_open_on_error=fail_open_on_error,
    )


async def clear_redis_sliding_window(
    redis_backend: Any,
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
) -> int:
    """Clear one ``limits`` Redis sliding-window bucket."""

    def _clear() -> int:
        storage = _limits_storage_from_backend(redis_backend)
        if storage is None:
            return 0
        store = LimitsSlidingWindowStore(storage=storage)
        return store.clear(scope=scope, identifier=identifier, limit=limit, window_seconds=window_seconds)

    return await asyncio.to_thread(_clear)


def check_redis_sliding_window_sync(
    *,
    scope: str,
    identifier: str,
    limit: int,
    window_seconds: int | float,
) -> RateLimitDecision:
    """Sync variant for legacy provider paths that cannot await."""
    return _redis_sliding_window_decision(
        object(),
        scope=scope,
        identifier=identifier,
        limit=limit,
        window_seconds=window_seconds,
        record=True,
        fail_open_on_error=None,
    )


async def increment_redis_fixed_window(
    redis_backend: Any,
    *,
    scope: str,
    identifier: str,
    amount: float,
    limit: float,
    ttl_seconds: int,
    key_window: int | float | str | None = None,
) -> CounterLimitDecision:
    """Increment a fixed-window counter using ``limits`` storage."""

    def _degraded_decision() -> CounterLimitDecision:
        allowed = not cloud_rate_limit_fail_closed()
        _record_rate_limit_degraded(scope, "counter", fail_open=allowed)
        return CounterLimitDecision(
            allowed=allowed,
            retry_after=max(1, int(ttl_seconds)),
            redis_error=True,
        )

    def _check() -> CounterLimitDecision:
        storage = _limits_storage_from_backend(redis_backend)
        if storage is None:
            return _degraded_decision()
        last_exc: Exception | None = None
        for attempt in range(_REDIS_OP_ATTEMPTS):
            try:
                limiter = FixedWindowRateLimiter(storage)
                return _fixed_window_decision(
                    limiter,
                    scope=scope,
                    identifier=identifier,
                    amount=amount,
                    limit=limit,
                    ttl_seconds=ttl_seconds,
                    key_window=key_window,
                )
            except _RATE_LIMIT_STORAGE_ERRORS as exc:
                last_exc = exc
                if attempt + 1 < _REDIS_OP_ATTEMPTS:
                    time.sleep(_REDIS_OP_BACKOFF_SECONDS * (attempt + 1))
        logger.warning(
            "Redis fixed-window counter failed for scope=%s after %d attempts: %s",
            scope,
            _REDIS_OP_ATTEMPTS,
            last_exc,
        )
        return _degraded_decision()

    return await asyncio.to_thread(_check)
