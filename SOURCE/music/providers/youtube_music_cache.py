"""
Persistent YouTube Music search result caching with sliding TTL.

This module provides SQLite-backed caching for YouTube search results to minimize
API quota usage. Features:

- **Persistent storage**: Survives app restarts (SQLite in user's app data)
- **Sliding TTL**: Cache entries expire 30 days after *last access*, not creation
- **LRU eviction**: Oldest entries removed when exceeding max size
- **Query normalization**: "Drake" and "drake" share the same cache entry
- **Self-healing**: Corrupted database is automatically recreated

ADVERSARIAL CONSIDERATIONS ADDRESSED:
- Database corruption: Caught and file deleted/recreated
- Stale video pointers: evict() method for resolution failures
- Cache poisoning: Invalid JSON evicted on read attempt
- Concurrent access: SQLite WAL mode + thread locks
- Schema migration: Version check, wipe on mismatch
- Large result sets: Capped to 5 items on storage
- File permissions: Restrictive permissions on cache directory
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    from .models import SearchResults

logger = get_logger("viola.music.providers.youtube_music_cache")


class _SearchCache:
    """
    Legacy in-memory cache for backwards compatibility.

    This is kept as a fallback if SQLite initialization fails.
    """

    def __init__(self, max_size: int = 50, ttl_seconds: int = 600):
        from collections import OrderedDict

        self._cache: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> SearchResults | None:
        key = self._normalize_key(key)
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            result_dict, timestamp = self._cache[key]
            if time.time() - timestamp > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None

            # Sliding TTL: update timestamp on access
            self._cache[key] = (result_dict, time.time())
            self._cache.move_to_end(key)
            self._hits += 1
            return self._deserialize(result_dict)

    def put(self, key: str, value: SearchResults) -> None:
        key = self._normalize_key(key)
        with self._lock:
            result_dict = self._serialize(value)
            self._cache[key] = (result_dict, time.time())
            self._cache.move_to_end(key)

            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def evict(self, key: str) -> bool:
        """Remove entry from cache."""
        key = self._normalize_key(key)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self) -> int:
        """Clear all entries. Returns count cleared."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "size": len(self._cache),
                "max_size": self._max_size,
                "ttl_days": self._ttl // (24 * 60 * 60),
                "persistent": False,
            }

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Normalize cache key for better hit rate."""
        return key.lower().strip()

    def _serialize(self, value: SearchResults) -> dict[str, Any]:
        """Convert SearchResults to dict, capping items."""

        result_dict = value.model_dump(mode="json")
        # Cap to 5 items to save storage
        if "items" in result_dict and len(result_dict["items"]) > 5:
            result_dict["items"] = result_dict["items"][:5]
        return result_dict

    def _deserialize(self, data: dict) -> SearchResults:
        """Convert dict back to SearchResults."""
        from .models import SearchResults

        return SearchResults.model_validate(data)


class PersistentSearchCache:
    """
    SQLite-backed search cache with sliding TTL.

    Storage: SQLite database in user's app data directory
    TTL: 30 days from last access (sliding window)
    Eviction: LRU when exceeding max entries
    """

    SCHEMA_VERSION = 1
    DEFAULT_TTL_DAYS = 30
    DEFAULT_MAX_ENTRIES = 500

    def __init__(
        self,
        db_path: Path | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self._ttl_seconds = ttl_days * 24 * 60 * 60
        self._ttl_days = ttl_days
        self._max_entries = max_entries
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

        # Default path: platform data dir / cache / search_cache.db
        if db_path is None:
            db_path = get_data_dir() / "cache" / "search_cache.db"

        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with schema."""
        try:
            # Create cache directory with restrictive permissions
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            # Set restrictive permissions on cache directory (user-only)
            try:
                os.chmod(self._db_path.parent, stat.S_IRWXU)  # 700
            except OSError as e:
                logger.exception("Failed to set cache directory permissions: %s", e)
                pass  # Windows may not support this fully

            with self._get_connection() as conn:
                # Check schema version
                cursor = conn.execute("PRAGMA user_version")
                version = cursor.fetchone()[0]

                if version == 0:
                    # Fresh database - create schema
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS search_cache (
                            cache_key TEXT PRIMARY KEY,
                            result_json TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            last_accessed INTEGER NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS idx_last_accessed
                            ON search_cache(last_accessed);
                        PRAGMA user_version = 1;
                    """)
                    logger.info(
                        "PersistentSearchCache: Created new database at %s",
                        self._db_path,
                    )
                elif version != self.SCHEMA_VERSION:
                    # Schema mismatch - wipe and recreate
                    logger.warning(
                        "PersistentSearchCache: Schema version mismatch (%d != %d), recreating",
                        version,
                        self.SCHEMA_VERSION,
                    )
                    conn.execute("DROP TABLE IF EXISTS search_cache")
                    conn.execute("PRAGMA user_version = 0")
                    self._init_db()  # Recurse to create fresh
                else:
                    # Existing valid database
                    cursor = conn.execute("SELECT COUNT(*) FROM search_cache")
                    count = cursor.fetchone()[0]
                    logger.info(
                        "PersistentSearchCache: Loaded database with %d entries from %s",
                        count,
                        self._db_path,
                    )

        except sqlite3.DatabaseError as exc:
            # Corrupt database - delete and retry
            logger.warning("PersistentSearchCache: Database corrupted (%s), recreating", exc)
            try:
                self._db_path.unlink(missing_ok=True)
            except OSError as e:
                logger.exception("Failed to delete corrupted cache database: %s", e)
                pass
            self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection with WAL mode for concurrency."""
        conn = sqlite3.connect(str(self._db_path), timeout=TIMEOUT_LONG)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _normalize_key(key: str) -> str:
        """
        Normalize cache key for better hit rate.

        Handles: case differences, extra whitespace
        """
        return key.lower().strip()

    def get(self, key: str) -> SearchResults | None:
        """Get cached result, updating last_accessed (sliding TTL)."""
        key = self._normalize_key(key)
        with self._lock:
            try:
                with self._get_connection() as conn:
                    now = int(time.time())
                    cutoff = now - self._ttl_seconds

                    cursor = conn.execute(
                        "SELECT result_json FROM search_cache WHERE cache_key = ? AND last_accessed > ?",
                        (key, cutoff),
                    )
                    row = cursor.fetchone()

                    if row is None:
                        self._misses += 1
                        return None

                    # Try to parse JSON
                    try:
                        result_dict = json.loads(row[0])
                    except json.JSONDecodeError:
                        # Poisoned entry - evict it
                        logger.warning(
                            "PersistentSearchCache: Evicting corrupted entry for key=%s",
                            key[:30],
                        )
                        conn.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
                        self._misses += 1
                        return None

                    # Update last_accessed (sliding window)
                    conn.execute(
                        "UPDATE search_cache SET last_accessed = ? WHERE cache_key = ?",
                        (now, key),
                    )

                    self._hits += 1
                    logger.debug("PersistentSearchCache: Cache hit for key=%s", key[:30])
                    return self._deserialize(result_dict)

            except sqlite3.Error as exc:
                logger.debug("PersistentSearchCache: SQLite error on get: %s", exc)
                self._misses += 1
                return None

    def put(self, key: str, value: SearchResults) -> None:
        """Store result in cache."""
        key = self._normalize_key(key)
        with self._lock:
            try:
                now = int(time.time())
                result_dict = self._serialize(value)
                result_json = json.dumps(result_dict)

                with self._get_connection() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO search_cache
                        (cache_key, result_json, created_at, last_accessed)
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, result_json, now, now),
                    )

                    # LRU eviction if over limit
                    self._evict_if_needed(conn)

                logger.debug("PersistentSearchCache: Cached key=%s", key[:30])

            except sqlite3.Error as exc:
                # Silent fail - cache is optimization, not critical
                logger.debug("PersistentSearchCache: SQLite error on put: %s", exc)
            except OSError as exc:
                # Disk full or other I/O error
                logger.warning("PersistentSearchCache: I/O error on put: %s", exc)

    def evict(self, key: str) -> bool:
        """
        Remove entry from cache.

        Called when resolution fails (e.g., video deleted from YouTube)
        to clean up stale pointers.
        """
        key = self._normalize_key(key)
        with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
                    evicted = cursor.rowcount > 0
                    if evicted:
                        logger.info(
                            "PersistentSearchCache: Evicted stale entry for key=%s",
                            key[:30],
                        )
                    return evicted
            except sqlite3.Error as exc:
                logger.debug("PersistentSearchCache: SQLite error on evict: %s", exc)
                return False

    def _evict_if_needed(self, conn: sqlite3.Connection) -> None:
        """Remove oldest entries if over max size."""
        cursor = conn.execute("SELECT COUNT(*) FROM search_cache")
        count = cursor.fetchone()[0]

        if count > self._max_entries:
            # Delete oldest 10% to avoid frequent evictions
            to_delete = max(1, count - int(self._max_entries * 0.9))
            conn.execute(
                """
                DELETE FROM search_cache WHERE cache_key IN (
                    SELECT cache_key FROM search_cache
                    ORDER BY last_accessed ASC
                    LIMIT ?
                )
                """,
                (to_delete,),
            )
            logger.info(
                "PersistentSearchCache: LRU evicted %d entries (count was %d, max %d)",
                to_delete,
                count,
                self._max_entries,
            )

    def clear(self) -> int:
        """Clear all cached entries. Returns count of entries cleared."""
        with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM search_cache")
                    count = cursor.fetchone()[0]
                    conn.execute("DELETE FROM search_cache")
                    logger.info("PersistentSearchCache: Cleared %d entries", count)
                    return count
            except sqlite3.Error as exc:
                logger.warning("PersistentSearchCache: Error clearing cache: %s", exc)
                return 0

    def stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            try:
                with self._get_connection() as conn:
                    cursor = conn.execute("SELECT COUNT(*) FROM search_cache")
                    size = cursor.fetchone()[0]
            except sqlite3.Error as e:
                logger.exception("Failed to get cache statistics: %s", e)
                size = 0

            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "size": size,
                "max_size": self._max_entries,
                "ttl_days": self._ttl_days,
                "persistent": True,
                "db_path": str(self._db_path),
            }

    def _serialize(self, value: SearchResults) -> dict[str, Any]:
        """Convert SearchResults to dict, capping items to save storage."""
        result_dict = value.model_dump(mode="json")
        # Cap to 5 items - we typically only use the first result anyway
        if "items" in result_dict and len(result_dict["items"]) > 5:
            result_dict["items"] = result_dict["items"][:5]
        return result_dict

    def _deserialize(self, data: dict) -> SearchResults:
        """Convert dict back to SearchResults."""
        from .models import SearchResults

        return SearchResults.model_validate(data)


def create_search_cache(
    persistent: bool = True,
    ttl_days: int = 30,
    max_entries: int = 500,
) -> _SearchCache | PersistentSearchCache:
    """
    Factory function to create appropriate cache implementation.

    Args:
        persistent: If True, use SQLite-backed persistent cache
        ttl_days: Time-to-live in days (sliding window)
        max_entries: Maximum cache entries

    Returns:
        Cache instance (PersistentSearchCache or fallback _SearchCache)
    """
    if persistent:
        try:
            cache = PersistentSearchCache(ttl_days=ttl_days, max_entries=max_entries)
            return cache
        except Exception as exc:
            logger.warning("Failed to create persistent cache, falling back to memory: %s", exc)

    # Fallback to in-memory cache
    ttl_seconds = ttl_days * 24 * 60 * 60
    return _SearchCache(max_size=max_entries, ttl_seconds=ttl_seconds)
