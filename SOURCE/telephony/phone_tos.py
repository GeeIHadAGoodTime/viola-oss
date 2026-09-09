"""Phone calling Terms of Service acceptance gate.

Requires users to accept phone calling terms before first use.
Storage: SQLite database at .viola/phone_billing.sqlite3 (shared with billing).

Migration: On first init, any existing JSON files in .viola/phone_tos/
are migrated into the database automatically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

_DB_PATH = get_data_dir() / "phone_billing.sqlite3"
_LEGACY_TOS_DIR = get_data_dir() / "phone_tos"
_PG_PHONE_TOS_RELATION = ("public.phone_tos",)
TOS_VERSION = "2.1"


def _valid_user_id(user_id: str) -> bool:
    normalized = str(user_id or "").strip()
    return bool(normalized) and normalized.lower() != "local"


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings
    except ImportError:
        return False
    return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud"


def _coerce_cloud_user_uuid(user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(user_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("Cloud Phone ToS requires a UUID user_id") from exc


def _parse_pg_rowcount(result: Any) -> int:
    try:
        return int(str(result).split()[-1])
    except (IndexError, ValueError):
        return 0


def _hash_request_attribute(value: str | None) -> str | None:
    """SHA-256 hash an IP address or user-agent string for consent audit storage.

    Matches the shape used by ``auth/audit.py::_hash_text`` so consent provenance
    in ``public.phone_tos`` (cloud Tier 1) joins by the same canonical hash as
    the surrounding auth event log. Empty/blank inputs return ``None`` so the
    column stays NULL rather than carrying a hash of the empty string.
    """
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class TosResult:
    allowed: bool
    reason: str = ""


class PhoneCallingTosGate:
    """Gate that requires ToS acceptance before phone calls.

    Stores acceptance records in SQLite (same DB as billing).
    Thread-safe via RLock + WAL mode.
    """

    def __init__(self) -> None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pg_checked = False
        self._conn = sqlite3.connect(
            str(_DB_PATH),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()
        self._migrate_json_data()

    async def _postgres_auth_db(self) -> Any | None:
        """Return the cloud auth DB when Phone ToS must use Tier 1 storage."""
        if not _is_cloud_surface():
            return None

        from auth.database import get_auth_db

        db = get_auth_db()
        if not getattr(db, "_initialized", True):
            await db.initialize()
        if not hasattr(db, "pool"):
            return None

        if not self._pg_checked:
            async with db.connection() as conn:
                from core.db_backend import assert_pg_relations

                await assert_pg_relations(conn, _PG_PHONE_TOS_RELATION, owner="PhoneCallingTosGate")
            self._pg_checked = True
        return db

    # ------------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        """Create the phone_tos table if it doesn't exist."""
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_tos (
                    user_id TEXT PRIMARY KEY,
                    accepted_at TEXT NOT NULL,
                    version TEXT DEFAULT '1.0'
                )
                """)

    # ------------------------------------------------------------------ JSON migration

    def _migrate_json_data(self) -> None:
        """One-time migration of legacy JSON ToS files into SQLite."""
        if not _LEGACY_TOS_DIR.is_dir():
            return

        json_files = list(_LEGACY_TOS_DIR.glob("*.json"))
        if not json_files:
            return

        # Check whether migration has already run
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM phone_tos").fetchone()
            if row and row["cnt"] > 0:
                return

        migrated_count = 0
        with self._lock, self._conn:
            for json_path in json_files:
                user_id = json_path.stem
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    logger.warning("Skipping corrupt legacy ToS file: %s", json_path.name)
                    continue

                if not data.get("accepted"):
                    continue

                # Convert epoch float to ISO timestamp if needed
                accepted_at_raw = data.get("accepted_at", 0)
                if isinstance(accepted_at_raw, (int, float)) and accepted_at_raw > 0:
                    accepted_at = datetime.fromtimestamp(accepted_at_raw, tz=UTC).isoformat()
                else:
                    accepted_at = str(accepted_at_raw)

                version = data.get("version", TOS_VERSION)
                try:
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO phone_tos (user_id, accepted_at, version)
                        VALUES (?, ?, ?)
                        """,
                        (user_id, accepted_at, version),
                    )
                    migrated_count += 1
                except sqlite3.IntegrityError:
                    logger.debug("Phone ToS migration skipped duplicate row for user %s", user_id)

        if migrated_count:
            logger.info(
                "Migrated %d ToS acceptance records from JSON to SQLite",
                migrated_count,
            )

    # ------------------------------------------------------------------ public API

    async def check(self, user_id: str) -> TosResult:
        """Check whether a user has accepted the current ToS version.

        Args:
            user_id: User identifier.

        Returns:
            TosResult with allowed=True if accepted, else False with reason.
        """
        if not _valid_user_id(user_id):
            return TosResult(
                allowed=False,
                reason="Authentication required before accepting the Phone Calling Terms of Service.",
            )

        pg_db = await self._postgres_auth_db()
        if pg_db is not None:
            try:
                pg_user_id = _coerce_cloud_user_uuid(user_id)
            except ValueError:
                return TosResult(
                    allowed=False,
                    reason="Authentication required before accepting the Phone Calling Terms of Service.",
                )
            async with pg_db.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT version FROM phone_tos WHERE user_id = $1",
                    pg_user_id,
                )
            if row and row["version"] == TOS_VERSION:
                return TosResult(allowed=True)
            return TosResult(
                allowed=False,
                reason="Please accept the Phone Calling Terms of Service to start making calls.",
            )

        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM phone_tos WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if row and row["version"] == TOS_VERSION:
            return TosResult(allowed=True)
        return TosResult(
            allowed=False,
            reason="Please accept the Phone Calling Terms of Service to start making calls.",
        )

    async def accept(
        self,
        user_id: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Record that a user has accepted the current ToS version.

        Args:
            user_id: User identifier.
            ip_address: Optional client IP address from the accept request.
                Hashed before persistence and stored in
                ``public.phone_tos.ip_address_hash`` so cloud consent
                provenance is durable evidence of the accepting client.
            user_agent: Optional ``User-Agent`` header value from the accept
                request. Hashed before persistence and stored in
                ``public.phone_tos.user_agent_hash`` for the same reason.
        """
        if not _valid_user_id(user_id):
            raise ValueError("Authentication required before accepting the Phone Calling Terms of Service.")

        ip_hash = _hash_request_attribute(ip_address)
        ua_hash = _hash_request_attribute(user_agent)

        accepted_at = datetime.now(tz=UTC).isoformat()
        pg_db = await self._postgres_auth_db()
        if pg_db is not None:
            pg_user_id = _coerce_cloud_user_uuid(user_id)
            accepted_at_dt = datetime.now(tz=UTC)
            async with pg_db.connection() as conn:
                # Do not reference excluded.ip_address_hash / excluded.user_agent_hash
                # here unless those columns are also in the INSERT column list above.
                # Postgres resolves an unspecified excluded.* column to the row that
                # WOULD have been inserted, which is the column DEFAULT (NULL) — so
                # listing them here would silently overwrite previously-captured
                # consent evidence with NULL on every re-acceptance.
                # Gate: scripts/check_phone_tos_consent_evidence_contract.py
                await conn.execute(
                    """
                    INSERT INTO phone_tos
                        (user_id, accepted_at, version, ip_address_hash, user_agent_hash)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT(user_id)
                    DO UPDATE SET accepted_at = excluded.accepted_at,
                                  version = excluded.version
                    """,
                    pg_user_id,
                    accepted_at_dt,
                    TOS_VERSION,
                    ip_hash,
                    ua_hash,
                )
            logger.info("Cloud Phone ToS accepted by user %s (version %s)", user_id, TOS_VERSION)
            return

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO phone_tos (user_id, accepted_at, version)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET accepted_at = excluded.accepted_at, version = excluded.version
                """,
                (user_id, accepted_at, TOS_VERSION),
            )
        logger.info("Phone ToS accepted by user %s (version %s)", user_id, TOS_VERSION)

    async def get_status(self, user_id: str) -> dict:
        """Return the current ToS acceptance status for a user.

        Args:
            user_id: User identifier.

        Returns:
            Dict with accepted (bool), version, and accepted_at fields.
        """
        if not _valid_user_id(user_id):
            return {"accepted": False, "version": None, "accepted_at": None}

        pg_db = await self._postgres_auth_db()
        if pg_db is not None:
            try:
                pg_user_id = _coerce_cloud_user_uuid(user_id)
            except ValueError:
                return {"accepted": False, "version": None, "accepted_at": None}
            async with pg_db.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT accepted_at, version FROM phone_tos WHERE user_id = $1",
                    pg_user_id,
                )
            if row:
                accepted_at = row["accepted_at"]
                return {
                    "accepted": row["version"] == TOS_VERSION,
                    "version": row["version"],
                    "accepted_at": accepted_at.isoformat() if hasattr(accepted_at, "isoformat") else accepted_at,
                }
            return {"accepted": False, "version": None, "accepted_at": None}

        with self._lock:
            row = self._conn.execute(
                "SELECT accepted_at, version FROM phone_tos WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if row:
            return {
                "accepted": row["version"] == TOS_VERSION,
                "version": row["version"],
                "accepted_at": row["accepted_at"],
            }
        return {"accepted": False, "version": None, "accepted_at": None}

    async def delete_user_acceptance(self, user_id: str) -> int:
        """Delete phone ToS acceptance data for one user."""
        pg_db = await self._postgres_auth_db()
        if pg_db is not None:
            try:
                pg_user_id = _coerce_cloud_user_uuid(user_id)
            except ValueError:
                return 0
            async with pg_db.connection() as conn:
                result = await conn.execute("DELETE FROM phone_tos WHERE user_id = $1", pg_user_id)
            deleted = _parse_pg_rowcount(result)
            if deleted:
                logger.info("Deleted cloud Phone ToS acceptance for user %s", user_id)
            return deleted

        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM phone_tos WHERE user_id = ?",
                (user_id,),
            )
            deleted = cursor.rowcount
        if deleted:
            logger.info("Deleted phone ToS acceptance for user %s", user_id)
        return deleted

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Checkpoint WAL and close the SQLite connection. Idempotent."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("Phone ToS WAL checkpoint failed during close")
            try:
                self._conn.close()
            except Exception:
                logger.debug("Phone ToS database close failed")


_instance: PhoneCallingTosGate | None = None


def get_phone_tos() -> PhoneCallingTosGate:
    """Return the singleton PhoneCallingTosGate instance."""
    global _instance
    if _instance is None:
        _instance = PhoneCallingTosGate()
    return _instance
