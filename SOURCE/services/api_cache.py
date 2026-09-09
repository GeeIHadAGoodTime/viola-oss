"""Shared API response cache for lightweight tool integrations.

Two tiers are supported:

- Public cache: identical responses shared across users and processes
- Private cache: process-local, user-scoped responses for personal queries

The public tier is backed by SQLite so quota-sensitive public provider calls
survive process restarts.  The private tier intentionally stays user-scoped and
process-local unless a caller provides an explicit ``user_id``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_PUBLIC_USER_SCOPE = ""
_PUBLIC_TIER = "public"
_PRIVATE_TIER = "private"
_SQLITE_TIMEOUT_SECONDS = 2.0


class ApiCache:
    """Thread-safe TTL cache with durable public and user-scoped private tiers."""

    def __init__(
        self,
        max_public: int = 2000,
        max_private_per_user: int = 200,
        *,
        db_path: str | Path | None = None,
        durable_public: bool = True,
    ):
        self._max_public = max(1, int(max_public))
        self._max_private_per_user = max(1, int(max_private_per_user))
        self._public: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._private: dict[str, OrderedDict[str, tuple[float, Any]]] = {}
        self._lock = threading.Lock()
        self._db_path = Path(db_path) if db_path is not None else _default_public_cache_path()
        self._durable_public = durable_public
        self._db_ready = False

    def get_public(self, key: str) -> Any | None:
        """Return a cached public value or ``None`` if missing/expired."""
        storage_key = self._stable_cache_key(key)
        with self._lock:
            value = self._get_from_bucket(self._public, storage_key)
            if value is not None:
                return value

        value = self._get_durable_public(storage_key)
        if value is not None:
            with self._lock:
                self._set_in_bucket(
                    self._public,
                    storage_key,
                    value,
                    self._remaining_ttl_public(storage_key),
                    max_entries=self._max_public,
                )
        return value

    def set_public(self, key: str, value: Any, ttl_seconds: int) -> None:
        """Store a public value with TTL and LRU eviction."""
        storage_key = self._stable_cache_key(key)
        normalized_ttl = max(1, int(ttl_seconds))
        with self._lock:
            self._set_in_bucket(
                self._public,
                storage_key,
                value,
                normalized_ttl,
                max_entries=self._max_public,
            )
        self._set_durable_public(storage_key, value, normalized_ttl)

    def delete_public(self, key: str) -> None:
        """Delete a public cache entry from memory and the durable store."""
        storage_key = self._stable_cache_key(key)
        with self._lock:
            self._public.pop(storage_key, None)
        self._delete_durable(_PUBLIC_TIER, _PUBLIC_USER_SCOPE, storage_key)

    def get_private(self, user_id: str, key: str) -> Any | None:
        """Return a cached user-scoped value or ``None`` if missing/expired."""
        if not user_id:
            raise ValueError("user_id is required for private API cache access")
        with self._lock:
            bucket = self._private.get(user_id)
            if bucket is None:
                return None
            value = self._get_from_bucket(bucket, key)
            if value is None and not bucket:
                self._private.pop(user_id, None)
            return value

    def set_private(self, user_id: str, key: str, value: Any, ttl_seconds: int) -> None:
        """Store a user-scoped value with TTL and per-user LRU eviction."""
        if not user_id:
            raise ValueError("user_id is required for private API cache access")
        with self._lock:
            bucket = self._private.setdefault(user_id, OrderedDict())
            self._set_in_bucket(
                bucket,
                key,
                value,
                ttl_seconds,
                max_entries=self._max_private_per_user,
            )

    def delete_private(self, user_id: str, key: str) -> None:
        """Delete a user-scoped private cache entry."""
        if not user_id:
            raise ValueError("user_id is required for private API cache access")
        with self._lock:
            bucket = self._private.get(user_id)
            if bucket is not None:
                bucket.pop(key, None)
                if not bucket:
                    self._private.pop(user_id, None)

    def _get_from_bucket(
        self,
        bucket: OrderedDict[str, tuple[float, Any]],
        key: str,
    ) -> Any | None:
        entry = bucket.get(key)
        if entry is None:
            return None

        expires_at, value = entry
        if expires_at <= time.time():
            bucket.pop(key, None)
            return None

        bucket.move_to_end(key)
        return value

    def _set_in_bucket(
        self,
        bucket: OrderedDict[str, tuple[float, Any]],
        key: str,
        value: Any,
        ttl_seconds: int,
        *,
        max_entries: int,
    ) -> None:
        expires_at = time.time() + max(1, int(ttl_seconds))
        bucket[key] = (expires_at, value)
        bucket.move_to_end(key)
        self._prune_expired(bucket)
        while len(bucket) > max_entries:
            bucket.popitem(last=False)

    def _prune_expired(self, bucket: OrderedDict[str, tuple[float, Any]]) -> None:
        now = time.time()
        expired_keys = [key for key, (expires_at, _) in bucket.items() if expires_at <= now]
        for key in expired_keys:
            bucket.pop(key, None)

    def _stable_cache_key(self, key: str) -> str:
        """Hash public cache keys so durable storage never embeds raw queries/IPs."""
        return public_cache_key("api_cache", str(key))

    def _ensure_db(self) -> bool:
        if not self._durable_public:
            return False
        if self._db_ready:
            return True
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS api_cache (
                        tier TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        cache_key TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (tier, user_id, cache_key)
                    )
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache(expires_at)")
            self._db_ready = True
            return True
        except (OSError, sqlite3.Error) as exc:
            logger.debug("API cache durable store unavailable at %s: %s", self._db_path, exc)
            return False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_durable_public(self, key: str) -> Any | None:
        if not self._ensure_db():
            return None
        now = time.time()
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT value_json, expires_at
                    FROM api_cache
                    WHERE tier = ? AND user_id = ? AND cache_key = ?
                    """,
                    (_PUBLIC_TIER, _PUBLIC_USER_SCOPE, key),
                ).fetchone()
                if row is None:
                    return None

                value_json, expires_at = row
                if float(expires_at) <= now:
                    conn.execute(
                        """
                        DELETE FROM api_cache
                        WHERE tier = ? AND user_id = ? AND cache_key = ?
                        """,
                        (_PUBLIC_TIER, _PUBLIC_USER_SCOPE, key),
                    )
                    return None

                conn.execute(
                    """
                    UPDATE api_cache
                    SET updated_at = ?
                    WHERE tier = ? AND user_id = ? AND cache_key = ?
                    """,
                    (now, _PUBLIC_TIER, _PUBLIC_USER_SCOPE, key),
                )
                return json.loads(str(value_json))
        except (OSError, json.JSONDecodeError, sqlite3.Error, TypeError, ValueError) as exc:
            logger.debug("API cache public read failed for key %s: %s", key, exc)
            self._delete_durable(_PUBLIC_TIER, _PUBLIC_USER_SCOPE, key)
            return None

    def _set_durable_public(self, key: str, value: Any, ttl_seconds: int) -> None:
        if not self._ensure_db():
            return
        try:
            value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            logger.debug("API cache value for key %s is not JSON-serializable: %s", key, exc)
            return

        now = time.time()
        expires_at = now + max(1, int(ttl_seconds))
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    INSERT INTO api_cache (tier, user_id, cache_key, value_json, expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tier, user_id, cache_key) DO UPDATE SET
                        value_json = excluded.value_json,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (_PUBLIC_TIER, _PUBLIC_USER_SCOPE, key, value_json, expires_at, now),
                )
                self._prune_durable_public(conn, now)
        except (OSError, sqlite3.Error) as exc:
            logger.debug("API cache public write failed for key %s: %s", key, exc)

    def _delete_durable(self, tier: str, user_id: str, key: str) -> None:
        if not self._ensure_db():
            return
        try:
            with self._connection() as conn:
                conn.execute(
                    """
                    DELETE FROM api_cache
                    WHERE tier = ? AND user_id = ? AND cache_key = ?
                    """,
                    (tier, user_id, key),
                )
        except (OSError, sqlite3.Error) as exc:
            logger.debug("API cache delete failed for key %s: %s", key, exc)

    def _remaining_ttl_public(self, key: str) -> int:
        if not self._ensure_db():
            return 1
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT expires_at
                    FROM api_cache
                    WHERE tier = ? AND user_id = ? AND cache_key = ?
                    """,
                    (_PUBLIC_TIER, _PUBLIC_USER_SCOPE, key),
                ).fetchone()
            if row is None:
                return 1
            return max(1, int(float(row[0]) - time.time()))
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            logger.debug("API cache TTL lookup failed for key %s: %s", key, exc)
            return 1

    def _prune_durable_public(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("DELETE FROM api_cache WHERE expires_at <= ?", (now,))
        conn.execute(
            """
            DELETE FROM api_cache
            WHERE rowid IN (
                SELECT rowid
                FROM api_cache
                WHERE tier = ? AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (_PUBLIC_TIER, _PUBLIC_USER_SCOPE, self._max_public),
        )


def _default_public_cache_path() -> Path:
    try:
        from config.settings import settings

        return Path(str(settings.cache_dir)) / "api_cache.sqlite3"
    except Exception:
        return Path(".viola") / "cache" / "api_cache.sqlite3"


def public_cache_key(namespace: str, *parts: Any) -> str:
    """Build a stable public cache key without embedding raw query material."""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "%s:%s" % (namespace.strip().lower(), digest)


_CACHE_SINGLETON: ApiCache | None = None
_CACHE_SINGLETON_LOCK = threading.Lock()


def get_api_cache() -> ApiCache:
    """Return the process-wide API cache singleton."""
    global _CACHE_SINGLETON
    if _CACHE_SINGLETON is None:
        with _CACHE_SINGLETON_LOCK:
            if _CACHE_SINGLETON is None:
                _CACHE_SINGLETON = ApiCache()
    return _CACHE_SINGLETON


__all__ = ["ApiCache", "get_api_cache", "public_cache_key"]
