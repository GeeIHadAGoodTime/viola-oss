"""
Idempotency Key Handling Module
Handle idempotency keys for API request deduplication.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class IdempotencyKeyManager:
    """Manage idempotency keys for API requests"""

    # Hard upper bound on cached idempotency entries. Prevents unbounded
    # growth under sustained high-throughput request bursts that each
    # carry unique idempotency keys.
    _MAX_CACHE_SIZE: int = 10_000

    def __init__(self, ttl_seconds: int = 3600):
        """
        Initialize idempotency key manager.

        Args:
            ttl_seconds: Time-to-live for cached results (default 1 hour)
        """
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[Any, float]] = {}  # key -> (result, expiry_time)
        self._last_cleanup = time.time()

    def get_cached_result(self, idempotency_key: str) -> Any | None:
        """
        Get cached result for idempotency key.

        Args:
            idempotency_key: Idempotency key

        Returns:
            Cached result or None if not found/expired
        """
        with self._lock:
            self._cleanup_expired()

            if idempotency_key in self._cache:
                result, expiry = self._cache[idempotency_key]
                if time.time() < expiry:
                    logger.debug(
                        "Returning cached result for idempotency key: %s...",
                        idempotency_key[:8],
                    )
                    return result
                else:
                    # Expired, remove it
                    del self._cache[idempotency_key]

            return None

    def cache_result(self, idempotency_key: str, result: Any) -> None:
        """
        Cache result for idempotency key.

        Args:
            idempotency_key: Idempotency key
            result: Result to cache
        """
        with self._lock:
            expiry = time.time() + self.ttl_seconds
            self._cache[idempotency_key] = (result, expiry)
            logger.debug("Cached result for idempotency key: %s...", idempotency_key[:8])

            # Periodic cleanup
            self._cleanup_expired()

            # Enforce hard size limit after cleanup
            if len(self._cache) > self._MAX_CACHE_SIZE:
                self._evict_oldest()

    def _cleanup_expired(self) -> None:
        """Remove expired entries from cache"""
        now = time.time()

        # Only cleanup every 60 seconds to avoid overhead
        if now - self._last_cleanup < 60:
            return

        self._last_cleanup = now

        expired_keys = [key for key, (_, expiry) in self._cache.items() if now >= expiry]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logger.debug("Cleaned up %s expired idempotency keys", len(expired_keys))

    def _evict_oldest(self) -> None:
        """Evict oldest entries to stay within _MAX_CACHE_SIZE (lock must be held)."""
        overage = len(self._cache) - self._MAX_CACHE_SIZE
        if overage <= 0:
            return
        # Sort by expiry ascending (oldest expiry = inserted longest ago)
        sorted_keys = sorted(
            self._cache.keys(),
            key=lambda k: self._cache[k][1],
        )
        for key in sorted_keys[:overage]:
            del self._cache[key]
        if overage > 0:
            logger.debug("Evicted %s idempotency keys (cache at max size)", overage)

    def clear_cache(self) -> None:
        """Clear all cached results"""
        with self._lock:
            self._cache.clear()
            logger.info("Cleared idempotency key cache")

    def get_cache_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        with self._lock:
            self._cleanup_expired()
            return {"cache_size": len(self._cache), "ttl_seconds": self.ttl_seconds}


def generate_idempotency_key(request_data: Any) -> str:
    """
    Generate idempotency key from request data.

    Args:
        request_data: Request data (dict, string, etc.)

    Returns:
        Idempotency key (hex digest)
    """
    try:
        if isinstance(request_data, dict):
            # Sort keys for consistent hashing
            sorted_data = json.dumps(request_data, sort_keys=True)
        else:
            sorted_data = str(request_data)

        # Generate hash
        key_hash = hashlib.sha256(sorted_data.encode("utf-8")).hexdigest()
        return key_hash
    except Exception as e:
        logger.error("Error generating idempotency key: %s", e)
        # Fallback: use timestamp
        return hashlib.sha256(str(time.time()).encode("utf-8")).hexdigest()


# Global idempotency manager instance
_idempotency_manager: IdempotencyKeyManager | None = None
_idempotency_manager_lock = threading.Lock()


def get_idempotency_manager() -> IdempotencyKeyManager:
    """
    Get global idempotency manager instance.

    Returns:
        IdempotencyKeyManager instance
    """
    global _idempotency_manager
    with _idempotency_manager_lock:
        if _idempotency_manager is None:
            _idempotency_manager = IdempotencyKeyManager()
        return _idempotency_manager
