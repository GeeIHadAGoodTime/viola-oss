"""Optional Redis backend for shared ephemeral state.

Provides a thin async wrapper around ``redis.asyncio`` for components that
need cross-instance state (rate limiters, MFA sessions, OAuth nonces).

**Design principles:**

- Redis is OPTIONAL.  When ``VIOLA_REDIS_URL`` is not set (or the ``redis``
  package is not installed), every public function returns ``None`` and
  callers fall back to their in-memory implementations.
- Lazy connection: the first call to :func:`get_redis` creates the pool.
- All public helpers are ``async``; callers in sync code should keep their
  in-memory fallback instead of bridging.

Usage::

    from services.cache.redis_backend import get_redis

    r = await get_redis()
    if r is not None:
        await r.set("key", "value", ttl=300)
    else:
        # fallback to in-memory dict
        ...
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy connection state
# ---------------------------------------------------------------------------

_redis_client: Any | None = None
_redis_init_lock: asyncio.Lock | None = None
_redis_unavailable: bool = False  # True after a failed connect attempt


def _get_init_lock() -> asyncio.Lock:
    """Return (and lazily create) the asyncio lock for connection init."""
    global _redis_init_lock
    if _redis_init_lock is None:
        _redis_init_lock = asyncio.Lock()
    return _redis_init_lock


async def get_redis() -> RedisBackend | None:
    """Return a connected :class:`RedisBackend`, or ``None`` if unavailable.

    Returns ``None`` when:
    - ``VIOLA_REDIS_URL`` is not configured
    - The ``redis`` package is not installed
    - The connection attempt failed (logged once, not retried until restart)
    """
    global _redis_client, _redis_unavailable

    if _redis_unavailable:
        return None

    if _redis_client is not None:
        return _redis_client

    lock = _get_init_lock()
    async with lock:
        # Double-check after acquiring lock
        if _redis_client is not None:
            return _redis_client
        if _redis_unavailable:
            return None

        url = _get_redis_url()
        if not url:
            _redis_unavailable = True
            logger.debug("VIOLA_REDIS_URL not set; Redis backend disabled (in-memory fallback)")
            return None

        try:
            import redis.asyncio as aioredis
        except ImportError:
            _redis_unavailable = True
            logger.debug("redis package not installed; Redis backend disabled (in-memory fallback)")
            return None

        try:
            pool = aioredis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                # Validate a pooled connection with PING before reuse so a stale/
                # dead idle connection can't hang until socket_timeout and surface
                # as a spurious "Timeout reading from socket" — which fail-closes
                # the cloud rate limiter and blocks legitimate calls. Redis itself
                # is sub-3ms; the failure mode was unvalidated idle connections.
                health_check_interval=30,
                socket_keepalive=True,
            )
            # Verify connectivity
            await pool.ping()
            _redis_client = RedisBackend(pool)
            logger.info("Redis backend connected: %s", _sanitize_url(url))
            return _redis_client
        except Exception:
            _redis_unavailable = True
            logger.warning(
                "Redis connection failed; falling back to in-memory state (url=%s)",
                _sanitize_url(url),
            )
            return None


def _get_redis_url() -> str | None:
    """Read the Redis URL from AppConfig (backed by VIOLA_REDIS_URL env var)."""
    try:
        from config.settings import get_settings

        return get_settings().redis_url
    except Exception:
        return None


def _sanitize_url(url: str) -> str:
    """Mask password in Redis URL for safe logging."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.password:
            return url.replace(parsed.password, "***")
    except Exception:
        pass
    return url


# ---------------------------------------------------------------------------
# Backend wrapper
# ---------------------------------------------------------------------------


class RedisBackend:
    """Thin async wrapper providing the operations needed by Viola components.

    All methods are safe to call concurrently from async code.  The
    underlying ``redis.asyncio`` client handles connection pooling.
    """

    def __init__(self, client: Any) -> None:
        self._r = client

    # -- Key/Value ----------------------------------------------------------

    async def get(self, key: str) -> str | None:
        """Get a string value by key."""
        return await self._r.get(key)

    async def set(self, key: str, value: str, *, ttl: int | None = None) -> None:
        """Set a string value, optionally with a TTL in seconds."""
        if ttl is not None:
            await self._r.setex(key, ttl, value)
        else:
            await self._r.set(key, value)

    async def delete(self, key: str) -> None:
        """Delete a key."""
        await self._r.delete(key)

    async def exists(self, key: str) -> bool:
        """Check if a key exists."""
        return bool(await self._r.exists(key))

    # -- Hash ---------------------------------------------------------------

    async def hset(self, key: str, field: str, value: str) -> None:
        """Set a hash field."""
        await self._r.hset(key, field, value)

    async def hget(self, key: str, field: str) -> str | None:
        """Get a hash field."""
        return await self._r.hget(key, field)

    async def hgetall(self, key: str) -> dict[str, str]:
        """Get all fields and values from a hash."""
        return await self._r.hgetall(key)

    async def hdel(self, key: str, field: str) -> None:
        """Delete a hash field."""
        await self._r.hdel(key, field)

    async def expire(self, key: str, ttl: int) -> None:
        """Set a TTL (in seconds) on an existing key."""
        await self._r.expire(key, ttl)

    async def eval(self, script: str, numkeys: int, *args: object) -> Any:
        """Run a Redis Lua script.

        Used by rate limiters so check-and-record happens atomically in one
        round trip instead of split ZREM/ZCARD/ZADD calls.
        """
        return await self._r.eval(script, numkeys, *args)

    # -- Sorted Set (ZSET) --------------------------------------------------
    # Used for sliding-window rate limiters: score = timestamp, member = unique request ID.

    async def zadd(self, key: str, score: float, member: str) -> None:
        """Add a member with a score to a sorted set."""
        await self._r.zadd(key, {member: score})

    async def zrangebyscore(self, key: str, min_score: float, max_score: float) -> list[str]:
        """Return members with scores between min and max (inclusive)."""
        return await self._r.zrangebyscore(key, min_score, max_score)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        """Remove members with scores between min and max (inclusive). Returns count removed."""
        return await self._r.zremrangebyscore(key, min_score, max_score)

    async def zcard(self, key: str) -> int:
        """Return the number of members in a sorted set."""
        return await self._r.zcard(key)

    # -- JSON convenience ---------------------------------------------------

    async def get_json(self, key: str) -> Any | None:
        """Get and JSON-decode a value."""
        raw = await self._r.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def set_json(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        """JSON-encode and set a value, optionally with TTL."""
        encoded = json.dumps(value, separators=(",", ":"), default=str)
        if ttl is not None:
            await self._r.setex(key, ttl, encoded)
        else:
            await self._r.set(key, encoded)

    # -- Cleanup ------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying connection pool."""
        try:
            await self._r.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level teardown
# ---------------------------------------------------------------------------


async def close_redis() -> None:
    """Gracefully close the Redis connection (call on app shutdown)."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None
