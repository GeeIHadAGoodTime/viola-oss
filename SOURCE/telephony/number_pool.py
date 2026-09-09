"""Phone number pool management for multi-user SaaS.

Manages a pool of Telnyx phone numbers for outbound calls.
In single-number mode (default, backward compatible), the pool contains
only the configured phone_number. In pool mode, numbers are allocated
round-robin and released after each call ends.

Thread-safe: all mutations are protected by a lock.

PHONE-07: the pool persists its in-use state to a small sqlite file with
per-claim heartbeats. On initialization, any claim whose heartbeat is
older than ``stale_ttl_seconds`` is reclaimed — this prevents a crashed
worker from permanently pinning a number.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

_DEFAULT_DB_PATH = get_data_dir() / "number_pool.sqlite3"
_DEFAULT_STALE_TTL_SECONDS = 300  # 5 minutes — tunable for tests.


class NumberPool:
    """Manages a pool of Telnyx phone numbers for outbound calls.

    In single-number mode (default, backward compatible), uses the configured
    phone_number. In pool mode, allocates from available numbers using
    least-recently-used ordering, and persists in-use claims to sqlite
    with a per-claim heartbeat so orphaned holds can be reclaimed after
    a crash (PHONE-07).

    Usage::

        # Single number (backward compatible)
        pool = NumberPool([], default="+17792559104")
        num = pool.acquire()  # always returns "+17792559104"
        pool.release(num)

        # Pool mode
        pool = NumberPool(["+17792559104", "+17792559105", "+17792559106"])
        num = pool.acquire()  # returns least-recently-used available number
        pool.heartbeat(num)   # call periodically during long operations
        pool.release(num)
    """

    def __init__(
        self,
        numbers: list[str],
        default: str = "",
        *,
        db_path: Path | str | None = None,
        stale_ttl_seconds: int = _DEFAULT_STALE_TTL_SECONDS,
        owner_id: str | None = None,
    ) -> None:
        """Initialize the number pool.

        Args:
            numbers: Pool of E.164 phone numbers. Empty list = single-number mode.
            default: Default phone number for single-number mode. Used when
                numbers list is empty.
            db_path: Optional sqlite path for persistent claim state. ``None``
                selects the default. Use ``":memory:"`` in tests to skip disk.
            stale_ttl_seconds: Heartbeat TTL; claims older than this are
                reclaimed on init and on every acquire.
            owner_id: Optional worker/process identifier stored alongside
                each claim. Defaults to the pid + a uuid4 hex.
        """
        self._lock = threading.Lock()
        self._default = default
        self._stale_ttl = max(1, int(stale_ttl_seconds))
        self._owner_id = owner_id or "%d:%s" % (os.getpid(), uuid.uuid4().hex[:8])

        if numbers:
            # Pool mode: deque gives O(1) append/popleft for LRU ordering
            self._available: deque[str] = deque(numbers)
            self._in_use: set[str] = set()
            self._pool_mode = True
            self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
            self._conn = self._open_db()
            self._ensure_schema()
            self._reclaim_stale()
            logger.info(
                "Number pool initialized with %d numbers (pool mode, ttl=%ds)",
                len(numbers),
                self._stale_ttl,
            )
        else:
            # Single-number mode: unlimited acquire/release of the default
            self._available = deque()
            self._in_use = set()
            self._pool_mode = False
            self._db_path = None
            self._conn = None
            logger.info(
                "Number pool initialized in single-number mode: %s",
                default,
            )

    # -- Persistence helpers --------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        path = self._db_path
        if path is None:
            raise RuntimeError("_open_db called on single-number pool")
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        assert self._conn is not None
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS number_claims (
                phone_number TEXT PRIMARY KEY,
                owner_id     TEXT NOT NULL,
                acquired_at  REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            )
            """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_number_claims_heartbeat ON number_claims(heartbeat_at)")

    def _reclaim_stale(self) -> None:
        """Release any claim whose heartbeat is older than the TTL.

        Called on init AND on every acquire so a crash in one worker
        is cleaned up before the next worker tries to dial.
        """
        if not self._conn:
            return
        cutoff = time.time() - self._stale_ttl
        cursor = self._conn.execute(
            "SELECT phone_number, owner_id, heartbeat_at FROM number_claims WHERE heartbeat_at < ?",
            (cutoff,),
        )
        stale = cursor.fetchall()
        if not stale:
            return
        for row in stale:
            self._conn.execute(
                "DELETE FROM number_claims WHERE phone_number = ?",
                (row["phone_number"],),
            )
            logger.warning(
                "Number pool reclaim: %s (owner=%s, last_hb=%.0fs ago)",
                row["phone_number"],
                row["owner_id"],
                time.time() - row["heartbeat_at"],
            )

    # -- Public API -----------------------------------------------------------

    def acquire(self) -> str:
        """Get an available phone number from the pool.

        Returns:
            An E.164 phone number string.

        Raises:
            RuntimeError: If no numbers are available in pool mode.
        """
        with self._lock:
            if not self._pool_mode:
                # Single-number mode: always return the default
                return self._default

            # Scrub any persisted-but-stale claims first so a crashed
            # worker's holds are recoverable.
            self._reclaim_stale()

            # Skip numbers that are currently claimed in the DB by a
            # live owner (possibly a peer worker process).
            now = time.time()
            cutoff = now - self._stale_ttl
            live_rows = self._conn.execute(
                "SELECT phone_number FROM number_claims WHERE heartbeat_at >= ?",
                (cutoff,),
            ).fetchall()
            live_claims = {row["phone_number"] for row in live_rows}

            while self._available:
                candidate = self._available.popleft()
                if candidate in live_claims:
                    # Another worker holds it; rotate it to the back.
                    self._available.append(candidate)
                    # If every remaining number is claimed elsewhere,
                    # bail instead of spinning forever.
                    if all(n in live_claims for n in self._available):
                        raise RuntimeError(
                            "No phone numbers available in pool (%d in use across workers)" % len(live_claims)
                        )
                    continue

                self._in_use.add(candidate)
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO number_claims
                        (phone_number, owner_id, acquired_at, heartbeat_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (candidate, self._owner_id, now, now),
                )
                logger.debug(
                    "Acquired number %s (%d available, %d in use)",
                    candidate,
                    len(self._available),
                    len(self._in_use),
                )
                return candidate

            raise RuntimeError("No phone numbers available in pool (%d in use)" % len(self._in_use))

    def release(self, number: str) -> None:
        """Return a phone number to the pool after call ends.

        Args:
            number: The E.164 phone number to release.
        """
        with self._lock:
            if not self._pool_mode:
                # Single-number mode: nothing to track
                return

            if number in self._in_use:
                self._in_use.discard(number)
                self._available.append(number)  # Goes to back (LRU)
                if self._conn is not None:
                    self._conn.execute(
                        "DELETE FROM number_claims WHERE phone_number = ? AND owner_id = ?",
                        (number, self._owner_id),
                    )
                logger.debug(
                    "Released number %s (%d available, %d in use)",
                    number,
                    len(self._available),
                    len(self._in_use),
                )
            else:
                logger.warning(
                    "Attempted to release number %s that was not in use",
                    number,
                )

    def heartbeat(self, number: str) -> None:
        """Refresh the heartbeat for a currently-held number.

        Callers should invoke this periodically during long-lived calls
        (or rely on the default TTL covering the call-duration cap).
        """
        if not self._pool_mode or self._conn is None:
            return
        with self._lock:
            if number not in self._in_use:
                return
            now = time.time()
            self._conn.execute(
                "UPDATE number_claims SET heartbeat_at = ? WHERE phone_number = ? AND owner_id = ?",
                (now, number, self._owner_id),
            )

    @property
    def available_count(self) -> int:
        """Number of phone numbers currently available for allocation."""
        with self._lock:
            if not self._pool_mode:
                # Single-number mode: always 1 available
                return 1
            return len(self._available)

    @property
    def in_use_count(self) -> int:
        """Number of phone numbers currently in use."""
        with self._lock:
            if not self._pool_mode:
                return 0
            return len(self._in_use)

    @property
    def total_count(self) -> int:
        """Total number of phone numbers in the pool."""
        with self._lock:
            if not self._pool_mode:
                return 1
            return len(self._available) + len(self._in_use)

    @property
    def stale_ttl_seconds(self) -> int:
        """Current stale-claim TTL in seconds."""
        return self._stale_ttl

    @property
    def is_pool_mode(self) -> bool:
        """Whether the pool is in multi-number mode."""
        return self._pool_mode

    def close(self) -> None:
        """Close the sqlite connection (idempotent)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
