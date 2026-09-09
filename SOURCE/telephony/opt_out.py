"""Business opt-out list for AI phone calls.

Storage: SQLite database in Viola's app-data directory (shared with billing/ToS).

Migration: On first init, any existing JSON opt-out file at
~/.viola/phone_opt_out.json is migrated into the database automatically.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

_DB_PATH = get_data_dir() / "phone_billing.sqlite3"
_LEGACY_OPT_OUT_PATH = Path.home().joinpath(".viola", "phone_opt_out.json")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Return the module-level SQLite connection, creating it on first call."""
    global _conn
    if _conn is not None:
        return _conn

    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(
        str(_DB_PATH),
        check_same_thread=False,
        isolation_level=None,
    )
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema()
    _migrate_json_data()
    return _conn


def _ensure_schema() -> None:
    """Create the phone_opt_out table if it doesn't exist."""
    conn = _conn
    if conn is None:
        return
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS phone_opt_out (
                phone_number TEXT PRIMARY KEY,
                reason TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)


def _migrate_json_data() -> None:
    """One-time migration of the legacy JSON opt-out file into SQLite."""
    conn = _conn
    if conn is None:
        return

    if not _LEGACY_OPT_OUT_PATH.exists():
        return

    # Check whether migration has already run
    row = conn.execute("SELECT COUNT(*) AS cnt FROM phone_opt_out").fetchone()
    if row and row["cnt"] > 0:
        return

    try:
        data = json.loads(_LEGACY_OPT_OUT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Skipping corrupt legacy opt-out file: %s", _LEGACY_OPT_OUT_PATH)
        return

    if not isinstance(data, dict):
        return

    migrated_count = 0
    with conn:
        for number, info in data.items():
            reason = info.get("reason", "") if isinstance(info, dict) else ""
            added_at = info.get("added", "") if isinstance(info, dict) else ""
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO phone_opt_out (phone_number, reason, added_at)
                VALUES (?, ?, ?)
                """,
                (number, reason, added_at or None),
            )
            if cursor.rowcount:
                migrated_count += 1

    if migrated_count:
        logger.info("Migrated %d opt-out records from JSON to SQLite", migrated_count)


def _normalize(phone_number: str) -> str:
    """Normalize phone number to last 10 digits."""
    digits = re.sub(r"\D", "", phone_number)
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits[-10:]


def is_opted_out(phone_number: str) -> bool:
    """Check if a business has opted out of AI calls.

    Args:
        phone_number: E.164 or raw phone number to check.

    Returns:
        True if the number is on the opt-out list.
    """
    normalized = _normalize(phone_number)
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT 1 FROM phone_opt_out WHERE phone_number = ?",
            (normalized,),
        ).fetchone()
    return row is not None


def add_opt_out(phone_number: str, reason: str = "") -> None:
    """Add a number to the opt-out list.

    Args:
        phone_number: E.164 or raw phone number to opt out.
        reason: Optional reason for the opt-out.
    """
    normalized = _normalize(phone_number)
    conn = _get_conn()
    with _lock, conn:
        conn.execute(
            """
            INSERT INTO phone_opt_out (phone_number, reason)
            VALUES (?, ?)
            ON CONFLICT(phone_number)
            DO UPDATE SET reason = excluded.reason, added_at = CURRENT_TIMESTAMP
            """,
            (normalized, reason),
        )
    logger.info("Opt-out added for number ending in ...%s", normalized[-4:])
