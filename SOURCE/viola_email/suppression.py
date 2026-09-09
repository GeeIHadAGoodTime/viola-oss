"""Email suppression list backed by local SQLite."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from core.platform import get_data_dir

_DEFAULT_DB_PATH = get_data_dir() / "email_suppression.sqlite3"
_singleton: SuppressionList | None = None
_singleton_lock = threading.Lock()


class SuppressionList:
    """Minimal email suppression list for delivery failure webhooks."""

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = self._open_db()
        self._ensure_schema()

    def _open_db(self) -> sqlite3.Connection:
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS email_suppression (
                email TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                added_at REAL NOT NULL
            )
            """)

    @staticmethod
    def _normalize_email(email: str) -> str:
        return str(email or "").strip().lower()

    def add(self, email: str, reason: str) -> None:
        normalized = self._normalize_email(email)
        if not normalized:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO email_suppression (email, reason, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    reason = excluded.reason,
                    added_at = excluded.added_at
                """,
                (normalized, str(reason or "unknown"), time.time()),
            )

    def is_suppressed(self, email: str) -> bool:
        normalized = self._normalize_email(email)
        if not normalized:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM email_suppression WHERE email = ?",
                (normalized,),
            ).fetchone()
        return row is not None

    def remove(self, email: str) -> None:
        normalized = self._normalize_email(email)
        if not normalized:
            return
        with self._lock:
            self._conn.execute("DELETE FROM email_suppression WHERE email = ?", (normalized,))


def get_suppression_list() -> SuppressionList:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SuppressionList()
    return _singleton
