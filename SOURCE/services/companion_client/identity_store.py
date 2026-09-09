"""Durable storage for the desktop's companion device identity.

When the desktop registers as a companion device of the user's cloud
account, the cloud returns a ``device_id`` and a one-time ``device_token``.
The token is the bearer credential the desktop presents on every
``/ws/companion/{device_id}`` connection, so it MUST survive restarts.

This store keeps that identity in a small project-relative SQLite file.
It is scoped by ``(cloud_url, account_key)`` so a single machine can be
paired with more than one cloud account / endpoint without collisions —
each account gets its own row. ``account_key`` is an opaque, stable
fingerprint of the cloud credential (never the credential itself).

The device token is a secret. It is stored in this local file the same way
the cloud stores OAuth/session secrets locally; it is never written to
``settings.json`` (which syncs) and never logged.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

_DB_FILENAME = "companion_client_identity.sqlite3"
_SCHEMA_VERSION = 1


@dataclass(slots=True, frozen=True)
class CompanionIdentity:
    """A persisted desktop<->cloud companion pairing."""

    cloud_url: str
    account_key: str
    device_id: str
    device_token: str
    device_name: str
    platform: str
    registered_at: float

    def to_log_dict(self) -> dict[str, Any]:
        """Return a redacted dict safe for logging (no token)."""
        return {
            "cloud_url": self.cloud_url,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "platform": self.platform,
            "registered_at": self.registered_at,
        }


class CompanionIdentityStore:
    """SQLite-backed store for companion device identities.

    Thread-safe: a single connection guarded by an ``RLock``. The store is
    tiny (one row per paired account) so a process-wide singleton is fine.
    """

    def __init__(self, root: Path | None = None) -> None:
        base = Path(root) if root is not None else get_data_dir()
        self._db_path = base / "data" / "persistence" / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_client_schema_version (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """)
            row = self._conn.execute(
                "SELECT version FROM companion_client_schema_version WHERE component = ?",
                ("identity",),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO companion_client_schema_version(component, version) VALUES (?, ?)",
                    ("identity", _SCHEMA_VERSION),
                )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_client_identity (
                    cloud_url TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    device_token TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    registered_at REAL NOT NULL,
                    PRIMARY KEY (cloud_url, account_key)
                )
                """)

    def load(self, *, cloud_url: str, account_key: str) -> CompanionIdentity | None:
        """Return the stored identity for this account, or ``None``."""
        cloud_url = _require(cloud_url, "cloud_url")
        account_key = _require(account_key, "account_key")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_client_identity
                WHERE cloud_url = ? AND account_key = ?
                """,
                (cloud_url, account_key),
            ).fetchone()
        if row is None:
            return None
        return _row_to_identity(row)

    def save(
        self,
        *,
        cloud_url: str,
        account_key: str,
        device_id: str,
        device_token: str,
        device_name: str,
        platform: str,
    ) -> CompanionIdentity:
        """Persist (or replace) the identity for this account."""
        identity = CompanionIdentity(
            cloud_url=_require(cloud_url, "cloud_url"),
            account_key=_require(account_key, "account_key"),
            device_id=_require(device_id, "device_id"),
            device_token=_require(device_token, "device_token"),
            device_name=device_name.strip() or "Viola Desktop",
            platform=platform.strip() or "desktop",
            registered_at=time.time(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO companion_client_identity(
                    cloud_url, account_key, device_id, device_token,
                    device_name, platform, registered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cloud_url, account_key) DO UPDATE SET
                    device_id = excluded.device_id,
                    device_token = excluded.device_token,
                    device_name = excluded.device_name,
                    platform = excluded.platform,
                    registered_at = excluded.registered_at
                """,
                (
                    identity.cloud_url,
                    identity.account_key,
                    identity.device_id,
                    identity.device_token,
                    identity.device_name,
                    identity.platform,
                    identity.registered_at,
                ),
            )
        logger.info("Stored companion device identity %s", identity.to_log_dict())
        return identity

    def clear(self, *, cloud_url: str, account_key: str) -> None:
        """Drop a stored identity (e.g. after the cloud revokes the device)."""
        with self._lock, self._conn:
            self._conn.execute(
                """
                DELETE FROM companion_client_identity
                WHERE cloud_url = ? AND account_key = ?
                """,
                (_require(cloud_url, "cloud_url"), _require(account_key, "account_key")),
            )
        logger.info("Cleared companion device identity for %s", cloud_url)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("WAL checkpoint failed while closing companion identity store")
            try:
                self._conn.close()
            except Exception:
                logger.exception("Failed to close companion identity store connection")


def _row_to_identity(row: sqlite3.Row) -> CompanionIdentity:
    return CompanionIdentity(
        cloud_url=str(row["cloud_url"]),
        account_key=str(row["account_key"]),
        device_id=str(row["device_id"]),
        device_token=str(row["device_token"]),
        device_name=str(row["device_name"]),
        platform=str(row["platform"]),
        registered_at=float(row["registered_at"]),
    )


def _require(value: str, name: str) -> str:
    resolved = str(value or "").strip()
    if not resolved:
        raise ValueError("%s is required" % name)
    return resolved
