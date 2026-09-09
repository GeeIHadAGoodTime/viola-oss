"""
Persistent Auth Failure Tracking with SQLite + In-Memory LRU Cache.

Solves two vulnerabilities:
1. C3: Auth failure counts reset on process restart, enabling brute-force
   via restart cycling (~1,440 attempts/day per IP).
2. L1: In-memory _auth_failures dict grows unboundedly (memory leak).

Design:
- SQLite persists failure records across restarts (same data dir as plan_limiter)
- OrderedDict provides an LRU cache (max 10,000 entries) for hot-path performance
- Exponential backoff replaces fixed 5-minute windows (base_window * 2^(failures-threshold))
- TTL eviction on both the cache and SQLite (periodic cleanup)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from core.logging_config import get_logger

log = get_logger(__name__)

# Cache limits
_MAX_CACHE_SIZE = 10_000
_CACHE_TTL_SECONDS = 3600  # Evict cache entries older than 1 hour


class AuthFailureStore:
    """Persistent auth failure tracking with in-memory LRU cache.

    Thread-safe.  All public methods acquire ``_lock``.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        max_failures: int = 5,
        base_window: int = 300,
        max_backoff: int = 86_400,
        max_cache_size: int = _MAX_CACHE_SIZE,
    ):
        self._max_failures = max_failures
        self._base_window = base_window  # 5 minutes
        self._max_backoff = max_backoff  # 24 hours cap
        self._max_cache_size = max_cache_size
        self._lock = threading.Lock()

        # In-memory LRU cache: ip -> (count, last_attempt_ts)
        self._cache: OrderedDict[str, tuple[int, float]] = OrderedDict()

        # SQLite persistence
        self._db_path = str(db_path) if db_path else None
        self._conn: sqlite3.Connection | None = None
        if self._db_path:
            self._init_db()

    # ── SQLite setup ──────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create the auth_failures table if it doesn't exist."""
        try:
            path = Path(self._db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS auth_failures (
                    ip TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_failures_last_attempt " "ON auth_failures(last_attempt)"
            )
            self._conn.commit()
            log.info("Auth failure store initialized at %s", path)
        except Exception:
            log.exception("Failed to initialize auth failure SQLite store")
            self._conn = None

    # ── Backoff calculation ───────────────────────────────────────────────

    def _get_lockout_window(self, failure_count: int) -> int:
        """Exponential backoff: base_window * 2^(failures - threshold).

        Returns the lockout duration in seconds for the given failure count.
        Capped at ``max_backoff`` (default 24h).
        """
        if failure_count <= self._max_failures:
            return self._base_window
        exponent = failure_count - self._max_failures
        window = self._base_window * (2**exponent)
        return min(window, self._max_backoff)

    # ── Core operations ───────────────────────────────────────────────────

    def record_failure(self, ip: str) -> tuple[int, float]:
        """Record an auth failure for *ip*.

        Returns (new_count, timestamp).
        """
        now = time.time()
        with self._lock:
            count, _last = self._get_unlocked(ip)
            # Check if the lockout window for the current count has expired
            window = self._get_lockout_window(count)
            if _last and (now - _last) > window:
                # Window expired — reset counter
                count = 0
            count += 1
            self._put_unlocked(ip, count, now)
            return count, now

    def check_rate_limited(self, ip: str) -> tuple[bool, int]:
        """Check if *ip* is currently rate-limited.

        Returns (is_limited, retry_after_seconds).
        """
        with self._lock:
            count, last_attempt = self._get_unlocked(ip)

        if count <= self._max_failures:
            return False, 0

        now = time.time()
        window = self._get_lockout_window(count)
        elapsed = now - last_attempt
        if elapsed > window:
            # Lockout expired
            return False, 0

        retry_after = int(window - elapsed) + 1
        return True, retry_after

    def get_failure_count(self, ip: str) -> int:
        """Return current failure count for *ip*."""
        with self._lock:
            count, _ = self._get_unlocked(ip)
            return count

    def clear_failures(self, ip: str) -> None:
        """Clear failure history for *ip* (on successful auth)."""
        with self._lock:
            # Remove from cache
            self._cache.pop(ip, None)
            # Remove from SQLite
            if self._conn:
                try:
                    self._conn.execute("DELETE FROM auth_failures WHERE ip = ?", (ip,))
                    self._conn.commit()
                except Exception:
                    log.exception("Failed to clear auth failures for IP from DB")

    def cleanup_expired(self) -> int:
        """Remove entries older than max_backoff from both cache and DB.

        Returns the number of entries removed.
        """
        now = time.time()
        removed = 0
        with self._lock:
            # Clean cache
            expired_ips = [ip for ip, (_, last) in self._cache.items() if (now - last) > self._max_backoff]
            for ip in expired_ips:
                del self._cache[ip]
                removed += 1

            # Clean SQLite
            if self._conn:
                try:
                    cursor = self._conn.execute(
                        "DELETE FROM auth_failures WHERE (? - last_attempt) > ?",
                        (now, self._max_backoff),
                    )
                    removed += cursor.rowcount
                    self._conn.commit()
                except Exception:
                    log.exception("Failed to cleanup expired auth failures from DB")

        if removed > 0:
            log.debug("Cleaned up %d expired auth failure records", removed)
        return removed

    # ── Internal (must hold _lock) ────────────────────────────────────────

    def _get_unlocked(self, ip: str) -> tuple[int, float]:
        """Get failure record from cache, falling back to SQLite.

        Returns (count, last_attempt). (0, 0.0) if not found.
        """
        # Try cache first
        if ip in self._cache:
            self._cache.move_to_end(ip)
            return self._cache[ip]

        # Fall back to SQLite
        if self._conn:
            try:
                row = self._conn.execute(
                    "SELECT failure_count, last_attempt FROM auth_failures WHERE ip = ?",
                    (ip,),
                ).fetchone()
                if row:
                    count, last_attempt = row
                    # Populate cache
                    self._cache[ip] = (count, last_attempt)
                    self._cache.move_to_end(ip)
                    self._evict_cache_if_needed()
                    return count, last_attempt
            except Exception:
                log.exception("Failed to read auth failure from DB")

        return 0, 0.0

    def _put_unlocked(self, ip: str, count: int, ts: float) -> None:
        """Write failure record to cache and SQLite."""
        # Update cache
        self._cache[ip] = (count, ts)
        self._cache.move_to_end(ip)
        self._evict_cache_if_needed()

        # Persist to SQLite
        if self._conn:
            try:
                self._conn.execute(
                    """
                    INSERT INTO auth_failures (ip, failure_count, last_attempt, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(ip) DO UPDATE SET
                        failure_count = excluded.failure_count,
                        last_attempt = excluded.last_attempt
                    """,
                    (ip, count, ts, ts),
                )
                self._conn.commit()
            except Exception:
                log.exception("Failed to persist auth failure to DB")

    def _evict_cache_if_needed(self) -> None:
        """Evict oldest cache entries if over the size cap."""
        while len(self._cache) > self._max_cache_size:
            self._cache.popitem(last=False)

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except (sqlite3.Error, RuntimeError) as exc:
                    log.debug("Failed to close auth failure store connection: %s", exc)
                self._conn = None
