"""Message deduplication cache for the messaging subsystem.

Prevents duplicate message processing that can occur from platform retries,
webhook replays, or network hiccups. Uses a TTL-based cache keyed by
(channel_type, chat_id, message_id).

For messages without a unique platform ID, a fingerprint is computed from
(sender, content, timestamp) with a 2-second timestamp window to catch
near-duplicates.
"""

from __future__ import annotations

import hashlib
import time

from core.logging_config import get_logger

logger = get_logger(__name__)

# Default TTL for dedup entries
_DEFAULT_TTL = 60.0  # seconds

# Cleanup runs every N checks to avoid scanning on every message
_CLEANUP_INTERVAL = 100


class DedupCache:
    """TTL-based message deduplication cache.

    Thread-safe for single-threaded async usage (all messaging code is async).

    Usage::

        cache = DedupCache()

        if cache.is_duplicate("discord", "chan123", "msg456"):
            return  # skip duplicate

        # ... process the message ...
    """

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._entries: dict[str, float] = {}  # cache_key -> expiry timestamp
        self._check_count = 0

    def is_duplicate(
        self,
        channel_type: str,
        chat_id: str,
        message_id: str | None = None,
        sender: str = "",
        content: str = "",
        timestamp: float = 0.0,
    ) -> bool:
        """Check if a message is a duplicate.

        If ``message_id`` is provided, the dedup key is exact:
        ``(channel_type, chat_id, message_id)``.

        If ``message_id`` is None or empty, a fingerprint is computed from
        ``(sender, content, timestamp)`` with a 2-second window to catch
        near-duplicates from retries.

        Returns True if the message was already seen within the TTL window.
        """
        self._check_count += 1
        if self._check_count % _CLEANUP_INTERVAL == 0:
            self._cleanup()

        now = time.monotonic()

        if message_id:
            key = self._exact_key(channel_type, chat_id, message_id)
        else:
            key = self._fingerprint_key(channel_type, chat_id, sender, content, timestamp)

        expiry = self._entries.get(key)
        if expiry is not None and now < expiry:
            logger.debug(
                "Dedup hit: %s/%s (key=%s)",
                channel_type,
                chat_id,
                key[:32],
            )
            return True

        # Mark as seen
        self._entries[key] = now + self._ttl
        return False

    def _exact_key(self, channel_type: str, chat_id: str, message_id: str) -> str:
        """Build a dedup key from exact platform identifiers."""
        return "%s|%s|%s" % (channel_type, chat_id, message_id)

    def _fingerprint_key(
        self,
        channel_type: str,
        chat_id: str,
        sender: str,
        content: str,
        timestamp: float,
    ) -> str:
        """Build a dedup key from content fingerprint with 2s timestamp window.

        The timestamp is bucketed into 2-second windows so that messages
        arriving within the same window are treated as duplicates.
        """
        # Round timestamp to nearest 2-second window
        ts_bucket = int(timestamp / 2.0) if timestamp else 0
        raw = "%s|%s|%s|%s|%d" % (channel_type, chat_id, sender, content, ts_bucket)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return "fp|%s" % digest

    def _cleanup(self) -> int:
        """Remove expired entries. Returns count removed."""
        now = time.monotonic()
        expired = [key for key, expiry in self._entries.items() if now >= expiry]
        for key in expired:
            del self._entries[key]
        if expired:
            logger.debug("Dedup cache cleanup: removed %d expired entries", len(expired))
        return len(expired)

    @property
    def size(self) -> int:
        """Current number of entries in the cache (including expired)."""
        return len(self._entries)

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()
        self._check_count = 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: DedupCache | None = None


def get_dedup_cache() -> DedupCache:
    """Return the singleton DedupCache, creating it on first call."""
    global _instance
    if _instance is None:
        _instance = DedupCache()
    return _instance
