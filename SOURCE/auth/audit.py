"""
Auth event audit logger.

Records authentication events (login, logout, session, OAuth) in the auth
database with salted SHA256 hashing for tamper detection.

Design:
    - Stored in its own ``auth_events`` table inside the existing auth.db.
    - Every entry gets a per-entry random salt; the ``entry_hash`` column is
      SHA256(salt || canonical_json(entry_fields)) so tampering is detectable.
    - PII (email, IP) is stored for incident investigation but can be purged
      per GDPR via ``purge_user_events()``.
    - All public methods are non-blocking: failures are logged but never
      propagate to the caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from core.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class AuthEventType:
    """Constants for auth event types."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    REGISTER = "register"
    SIGNUP_FAILURE = "signup_failure"  # account-creation attempt rejected before any user/email existed
    PASSWORD_CHANGE = "password_change"  # pragma: allowlist secret
    SESSION_CREATE = "session_create"
    SESSION_USED = "session_used"
    SESSION_REVOKE = "session_revoke"
    TOKEN_ROTATE = "token_rotate"
    OAUTH_LINK = "oauth_link"
    OAUTH_UNLINK = "oauth_unlink"
    ACCOUNT_LOCK = "account_lock"
    ACCOUNT_UNLOCK = "account_unlock"
    MAGIC_LINK_REQUEST = "magic_link_request"
    MAGIC_LINK_VERIFY = "magic_link_verify"
    EMAIL_VERIFY = "email_verify"
    EMAIL_CHANGE = "email_change"
    PASSWORD_RESET_REQUEST = "password_reset_request"  # pragma: allowlist secret
    PASSWORD_RESET_COMPLETE = "password_reset_complete"  # pragma: allowlist secret
    MFA_SETUP = "mfa_setup"
    MFA_VERIFY = "mfa_verify"
    MFA_DISABLE = "mfa_disable"
    MFA_BACKUP_CODES_GENERATE = "mfa_backup_codes_generate"
    OAUTH_TOKEN_DYING = "oauth_token_dying"  # 3+ consecutive refresh failures
    OAUTH_TOKEN_DEAD = "oauth_token_dead"  # 5+ consecutive refresh failures, needs reauth
    SPOKE_SCOPE_DENIED = "spoke_scope_denied"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AuthEvent:
    """A single auth audit event."""

    event_type: str
    timestamp: float = field(default_factory=time.time)
    user_id: str | None = None
    email: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    outcome: str = "success"  # "success", "failure", "blocked"
    details: dict[str, Any] | None = None
    salt: str = field(default_factory=lambda: secrets.token_hex(16))
    entry_hash: str = ""

    def compute_hash(self) -> str:
        """Compute tamper-detection hash for this entry."""
        canonical = {
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            "ip_address": self.ip_address,
            "outcome": self.outcome,
        }
        payload = self.salt + json.dumps(canonical, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

AUTH_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    user_id TEXT,
    email TEXT,
    ip_address TEXT,
    user_agent TEXT,
    outcome TEXT NOT NULL DEFAULT 'success',
    details TEXT,
    salt TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_events_timestamp ON auth_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_auth_events_event_type ON auth_events(event_type);
CREATE INDEX IF NOT EXISTS idx_auth_events_user_id ON auth_events(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_events_ip ON auth_events(ip_address);
"""


# ---------------------------------------------------------------------------
# Backend contract and helpers
# ---------------------------------------------------------------------------


class AuthAuditBackend(ABC):
    """Backend contract for auth audit persistence."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    @abstractmethod
    def db_path(self) -> Path | None:
        """Return SQLite path, when applicable."""

    @property
    @abstractmethod
    def pg_initialized(self) -> bool:
        """Return whether PostgreSQL schema validation has completed."""

    @abstractmethod
    def log_event(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an auth event."""

    async def alog_event(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Async event-recording variant for request/webhook paths."""
        await asyncio.to_thread(
            self.log_event,
            event_type,
            user_id=user_id,
            email=email,
            ip_address=ip_address,
            user_agent=user_agent,
            outcome=outcome,
            details=details,
        )

    @abstractmethod
    def get_events(
        self,
        *,
        event_type: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit events."""

    async def aget_events(
        self,
        *,
        event_type: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Async event-querying variant."""
        return await asyncio.to_thread(
            self.get_events,
            event_type=event_type,
            user_id=user_id,
            ip_address=ip_address,
            since=since,
            limit=limit,
        )

    @abstractmethod
    def purge_user_events(self, user_id: str) -> int:
        """Delete all audit events for one user."""

    async def apurge_user_events(self, user_id: str) -> int:
        """Async user purge variant."""
        return await asyncio.to_thread(self.purge_user_events, user_id)

    @abstractmethod
    def verify_integrity(self) -> dict[str, int | list[int]]:
        """Verify audit log hashes."""

    async def averify_integrity(self) -> dict[str, int | list[int]]:
        """Async integrity verification variant."""
        return await asyncio.to_thread(self.verify_integrity)

    @abstractmethod
    def rotate_old_events(self) -> int:
        """Delete expired audit events."""

    async def arotate_old_events(self) -> int:
        """Async retention-rotation variant."""
        return await asyncio.to_thread(self.rotate_old_events)


def _run_coroutine_blocking(coro: Awaitable[Any]) -> Any:
    from core.asyncio_safe import run_async_synchronously

    return run_async_synchronously(coro)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except Exception as exc:
        logger.error("Background auth audit write failed: %s", exc)


def _run_coroutine_fire_and_forget(coro: Awaitable[Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception as exc:
            logger.error("Auth audit write failed: %s", exc)
        return

    task = loop.create_task(coro)
    task.add_done_callback(_log_task_exception)


def _event_timestamp(event: AuthEvent) -> datetime:
    return datetime.fromtimestamp(event.timestamp, UTC)


def _hash_text(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _details_hash(details: dict[str, Any] | None) -> str | None:
    if not details:
        return None
    payload = json.dumps(details, sort_keys=True, default=str, separators=(",", ":"))
    return _hash_text(payload)


def _email_hmac(email: str | None) -> str | None:
    if not email:
        return None
    try:
        from auth.field_encryption import FieldEncryptor

        return FieldEncryptor.from_settings().blind_index(email)
    except Exception as exc:
        logger.warning("Falling back to SHA256 email audit hash: %s", exc)
        return _hash_text(email.lower())


def _coerce_pg_user_uuid(user_id: str | uuid.UUID | None) -> uuid.UUID | None:
    if user_id is None:
        return None
    return user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))


def _pg_entry_hash(
    *,
    event_type: str,
    timestamp: datetime,
    user_id: uuid.UUID | None,
    subject_user_hash: str | None,
    email_hmac: str | None,
    ip_address_hash: str | None,
    user_agent_hash: str | None,
    outcome: str,
    details_hash: str | None,
) -> str:
    canonical = {
        "event_type": event_type,
        "timestamp": timestamp.isoformat(),
        "user_id": str(user_id) if user_id is not None else None,
        "subject_user_hash": subject_user_hash,
        "email_hmac": email_hmac,
        "ip_address_hash": ip_address_hash,
        "user_agent_hash": user_agent_hash,
        "outcome": outcome,
        "details_hash": details_hash,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _pg_command_count(command: Any) -> int:
    if not isinstance(command, str):
        return 0
    parts = command.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


_PG_AUTH_EVENTS_COLUMNS = {
    "id",
    "event_type",
    "timestamp",
    "user_id",
    "subject_user_hash",
    "email_hmac",
    "ip_address_hash",
    "user_agent_hash",
    "outcome",
    "details_hash",
    "entry_hash",
}


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class AuthAuditLogger(AuthAuditBackend):
    """Non-blocking auth event audit logger backed by SQLite."""

    backend_name = "sqlite"
    is_postgres = False
    RETENTION_DAYS = 90

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._initialized = False

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    def _ensure_schema(self) -> None:
        """Create the auth_events table if it doesn't exist."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            try:
                conn = sqlite3.connect(str(self._db_path))
                conn.executescript(AUTH_EVENTS_SCHEMA)
                conn.commit()
                conn.close()
                self._initialized = True
            except Exception as exc:
                logger.error("Failed to initialize auth_events table: %s", exc)

    def log_event(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record an auth event. Never raises."""
        try:
            self._ensure_schema()

            event = AuthEvent(
                event_type=event_type,
                user_id=user_id,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
                outcome=outcome,
                details=details,
            )
            event.entry_hash = event.compute_hash()
            self._save(event)
        except Exception as exc:
            # Non-blocking: audit failures must never break auth flows
            logger.error("Failed to log auth event %s: %s", event_type, exc)

    def _save(self, event: AuthEvent) -> None:
        """Persist event to SQLite."""
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                INSERT INTO auth_events
                    (event_type, timestamp, user_id, email, ip_address,
                     user_agent, outcome, details, salt, entry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_type,
                    event.timestamp,
                    event.user_id,
                    event.email,
                    event.ip_address,
                    event.user_agent,
                    event.outcome,
                    json.dumps(event.details) if event.details else None,
                    event.salt,
                    event.entry_hash,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_events(
        self,
        *,
        event_type: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit events. Returns list of dicts."""
        self._ensure_schema()
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM auth_events WHERE 1=1"
            params: list[Any] = []

            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            if ip_address:
                query += " AND ip_address = ?"
                params.append(ip_address)
            if since:
                query += " AND timestamp >= ?"
                params.append(since)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.error("Failed to query auth events: %s", exc)
            return []

    def purge_user_events(self, user_id: str) -> int:
        """Delete all events for a user (GDPR right to erasure). Returns count."""
        self._ensure_schema()
        try:
            conn = sqlite3.connect(str(self._db_path))
            cursor = conn.execute("DELETE FROM auth_events WHERE user_id = ?", (user_id,))
            count = cursor.rowcount
            conn.commit()
            conn.close()
            return count
        except Exception as exc:
            logger.error("Failed to purge events for user %s: %s", user_id, exc)
            return 0

    def verify_integrity(self) -> dict[str, int | list[int]]:
        """Verify integrity of all audit log entries.

        Re-computes the hash for each entry and compares it against the
        stored entry_hash. Any mismatch indicates potential tampering.

        Returns:
            Dict with keys:
                total: Number of entries checked
                valid: Number of entries with matching hashes
                tampered: Number of entries with mismatched hashes
                tampered_ids: List of row IDs with mismatched hashes
                errors: Number of entries that could not be verified
        """
        self._ensure_schema()

        result: dict[str, int | list[int]] = {
            "total": 0,
            "valid": 0,
            "tampered": 0,
            "tampered_ids": [],
            "errors": 0,
        }

        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            # mt-ok: integrity check scans every audit row by design
            rows = conn.execute(
                "SELECT id, event_type, timestamp, user_id, ip_address, "
                "outcome, salt, entry_hash FROM auth_events ORDER BY id"
            ).fetchall()
            conn.close()

            result["total"] = len(rows)
            tampered_ids: list[int] = []

            for row in rows:
                try:
                    # Reconstruct the event to compute expected hash
                    event = AuthEvent(
                        event_type=row["event_type"],
                        timestamp=row["timestamp"],
                        user_id=row["user_id"],
                        ip_address=row["ip_address"],
                        outcome=row["outcome"],
                        salt=row["salt"],
                    )
                    expected_hash = event.compute_hash()
                    stored_hash = row["entry_hash"]

                    if expected_hash == stored_hash:
                        result["valid"] = int(result["valid"]) + 1
                    else:
                        result["tampered"] = int(result["tampered"]) + 1
                        tampered_ids.append(row["id"])
                        logger.critical(
                            "AUDIT INTEGRITY VIOLATION: entry id=%d "
                            "(event_type=%s, timestamp=%s) hash mismatch. "
                            "Expected=%s, stored=%s",
                            row["id"],
                            row["event_type"],
                            row["timestamp"],
                            expected_hash[:16],
                            stored_hash[:16],
                        )
                except Exception as exc:
                    result["errors"] = int(result["errors"]) + 1
                    logger.error("Failed to verify entry id=%d: %s", row["id"], exc)

            result["tampered_ids"] = tampered_ids

            if tampered_ids:
                logger.critical(
                    "AUDIT INTEGRITY CHECK: %d tampered entries detected out of %d total (IDs: %s)",
                    len(tampered_ids),
                    result["total"],
                    tampered_ids[:20],  # Log first 20 IDs
                )
            else:
                logger.info(
                    "Audit integrity check passed: %d entries verified",
                    result["total"],
                )

        except Exception as exc:
            logger.error("Failed to run integrity verification: %s", exc)

        return result

    def rotate_old_events(self) -> int:
        """Delete events older than retention period. Returns count."""
        self._ensure_schema()
        cutoff = time.time() - (self.RETENTION_DAYS * 86400)
        try:
            conn = sqlite3.connect(str(self._db_path))
            # mt-ok: retention rotation deletes across all users by design
            cursor = conn.execute("DELETE FROM auth_events WHERE timestamp < ?", (cutoff,))
            count = cursor.rowcount
            conn.commit()
            conn.close()
            if count:
                logger.info(
                    "Rotated %d auth events older than %d days",
                    count,
                    self.RETENTION_DAYS,
                )
            return count
        except Exception as exc:
            logger.error("Failed to rotate auth events: %s", exc)
            return 0


class SqliteAuthAuditBackend(AuthAuditLogger):
    """SQLite implementation of auth audit persistence."""


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class PostgresAuthAuditBackend(AuthAuditBackend):
    """PostgreSQL implementation backed by migration-owned public.auth_events."""

    backend_name = "postgres"
    is_postgres = True
    RETENTION_DAYS = AuthAuditLogger.RETENTION_DAYS

    def __init__(
        self,
        database_url: str,
        *,
        pool_factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        if not database_url:
            raise RuntimeError("PostgresAuthAuditBackend requires a PostgreSQL database URL")
        self._database_url = database_url
        self._pool_factory = pool_factory
        self._initialized = False

    @property
    def db_path(self) -> Path | None:
        return None

    @property
    def pg_initialized(self) -> bool:
        return self._initialized

    async def _pg_pool(self) -> Any:
        if self._pool_factory is not None:
            return await self._pool_factory()
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    async def initialize(self) -> None:
        if self._initialized:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'auth_events'
                """)
        columns = {str(row["column_name"]) for row in rows}
        missing = sorted(_PG_AUTH_EVENTS_COLUMNS - columns)
        if missing:
            raise RuntimeError("Postgres auth_events schema missing columns: %s" % ", ".join(missing))
        self._initialized = True

    def log_event(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        _run_coroutine_fire_and_forget(
            self.alog_event(
                event_type,
                user_id=user_id,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
                outcome=outcome,
                details=details,
            )
        )

    async def alog_event(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.initialize()
            event = AuthEvent(
                event_type=event_type,
                user_id=user_id,
                email=email,
                ip_address=ip_address,
                user_agent=user_agent,
                outcome=outcome,
                details=details,
            )
            await self._save_event(event)
        except Exception as exc:
            logger.error("Failed to log auth event %s to PostgreSQL: %s", event_type, exc)

    async def _save_event(self, event: AuthEvent) -> None:
        timestamp = _event_timestamp(event)
        user_uuid = _coerce_pg_user_uuid(event.user_id)
        subject_hash = _hash_text(str(user_uuid)) if user_uuid is not None else None
        email_hash = _email_hmac(event.email)
        ip_hash = _hash_text(event.ip_address)
        agent_hash = _hash_text(event.user_agent)
        detail_hash = _details_hash(event.details)
        entry_hash = _pg_entry_hash(
            event_type=event.event_type,
            timestamp=timestamp,
            user_id=user_uuid,
            subject_user_hash=subject_hash,
            email_hmac=email_hash,
            ip_address_hash=ip_hash,
            user_agent_hash=agent_hash,
            outcome=event.outcome,
            details_hash=detail_hash,
        )

        pool = await self._pg_pool()
        db_user_token = None
        if user_uuid is not None:
            from core.db_backend import set_db_user_id

            db_user_token = set_db_user_id(str(user_uuid))
        try:
            await pool.execute(
                """
                INSERT INTO public.auth_events
                    (event_type, timestamp, user_id, subject_user_hash, email_hmac,
                     ip_address_hash, user_agent_hash, outcome, details_hash, entry_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                event.event_type,
                timestamp,
                user_uuid,
                subject_hash,
                email_hash,
                ip_hash,
                agent_hash,
                event.outcome,
                detail_hash,
                entry_hash,
            )
        finally:
            if db_user_token is not None:
                from core.db_backend import reset_db_user_id

                reset_db_user_id(db_user_token)

    def get_events(
        self,
        *,
        event_type: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return _run_coroutine_blocking(
            self.aget_events(
                event_type=event_type,
                user_id=user_id,
                ip_address=ip_address,
                since=since,
                limit=limit,
            )
        )

    async def aget_events(
        self,
        *,
        event_type: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.initialize()
        user_uuid = _coerce_pg_user_uuid(user_id) if user_id else None
        ip_hash = _hash_text(ip_address) if ip_address else None
        since_dt = datetime.fromtimestamp(since, UTC) if since else None

        pool = await self._pg_pool()
        rows = await pool.fetch(
            """
            SELECT
                id,
                event_type,
                EXTRACT(EPOCH FROM timestamp) AS timestamp,
                user_id::text AS user_id,
                subject_user_hash,
                email_hmac,
                ip_address_hash,
                user_agent_hash,
                outcome,
                details_hash,
                entry_hash
            FROM public.auth_events
            WHERE ($1::text IS NULL OR event_type = $1)
              AND ($2::uuid IS NULL OR user_id = $2)
              AND ($3::text IS NULL OR ip_address_hash = $3)
              AND ($4::timestamptz IS NULL OR timestamp >= $4)
            ORDER BY timestamp DESC
            LIMIT $5
            """,
            event_type,
            user_uuid,
            ip_hash,
            since_dt,
            int(limit),
        )
        return [dict(row) for row in rows]

    def purge_user_events(self, user_id: str) -> int:
        return int(_run_coroutine_blocking(self.apurge_user_events(user_id)))

    async def apurge_user_events(self, user_id: str) -> int:
        await self.initialize()
        pool = await self._pg_pool()
        command = await pool.execute(
            "DELETE FROM public.auth_events WHERE user_id = $1",
            _coerce_pg_user_uuid(user_id),
        )
        return _pg_command_count(command)

    def verify_integrity(self) -> dict[str, int | list[int]]:
        return _run_coroutine_blocking(self.averify_integrity())

    async def averify_integrity(self) -> dict[str, int | list[int]]:
        await self.initialize()
        result: dict[str, int | list[int]] = {
            "total": 0,
            "valid": 0,
            "tampered": 0,
            "tampered_ids": [],
            "errors": 0,
        }
        pool = await self._pg_pool()
        rows = await pool.fetch("""
            SELECT id, event_type, timestamp, user_id::text AS user_id,
                   subject_user_hash, email_hmac, ip_address_hash, user_agent_hash,
                   outcome, details_hash, entry_hash
            FROM public.auth_events
            ORDER BY id
            """)
        result["total"] = len(rows)
        tampered_ids: list[int] = []
        for row in rows:
            try:
                timestamp = row["timestamp"]
                if not isinstance(timestamp, datetime):
                    timestamp = datetime.fromtimestamp(float(timestamp), UTC)
                expected = _pg_entry_hash(
                    event_type=row["event_type"],
                    timestamp=timestamp,
                    user_id=_coerce_pg_user_uuid(row["user_id"]),
                    subject_user_hash=row["subject_user_hash"],
                    email_hmac=row["email_hmac"],
                    ip_address_hash=row["ip_address_hash"],
                    user_agent_hash=row["user_agent_hash"],
                    outcome=row["outcome"],
                    details_hash=row["details_hash"],
                )
                if expected == row["entry_hash"]:
                    result["valid"] = int(result["valid"]) + 1
                else:
                    result["tampered"] = int(result["tampered"]) + 1
                    tampered_ids.append(int(row["id"]))
            except Exception as exc:
                result["errors"] = int(result["errors"]) + 1
                logger.error("Failed to verify PostgreSQL auth event id=%s: %s", row["id"], exc)
        result["tampered_ids"] = tampered_ids
        return result

    def rotate_old_events(self) -> int:
        return int(_run_coroutine_blocking(self.arotate_old_events()))

    async def arotate_old_events(self) -> int:
        await self.initialize()
        cutoff = datetime.fromtimestamp(time.time() - (self.RETENTION_DAYS * 86400), UTC)
        pool = await self._pg_pool()
        command = await pool.execute(
            "DELETE FROM public.auth_events WHERE timestamp < $1",
            cutoff,
        )
        count = _pg_command_count(command)
        if count:
            logger.info(
                "Rotated %d PostgreSQL auth events older than %d days",
                count,
                self.RETENTION_DAYS,
            )
        return count


def create_auth_audit_backend(
    db_path: Path | str | None = None,
    *,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> AuthAuditBackend:
    """Create the auth audit backend selected by the application surface."""
    from core.database_strategy import postgres_url_for_surface

    pg_url = postgres_url_for_surface(
        "AuthAuditLogger",
        app_surface=app_surface,
        database_url=database_url,
    )
    if pg_url:
        return PostgresAuthAuditBackend(pg_url)

    if db_path is None:
        from config.settings import settings

        db_path = Path(settings.data_dir) / "auth.db"
    return SqliteAuthAuditBackend(db_path)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_audit_logger: AuthAuditBackend | None = None
_audit_lock = threading.Lock()


def get_auth_audit_logger() -> AuthAuditBackend:
    """Get or create the global auth audit logger.

    Desktop uses local SQLite auth.db. Cloud writes to migration-owned
    public.auth_events and forbids SQLite fallback.
    """
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    with _audit_lock:
        if _audit_logger is not None:
            return _audit_logger

        _audit_logger = create_auth_audit_backend()
        return _audit_logger


def reset_auth_audit_logger() -> None:
    """Reset global instance (for tests)."""
    global _audit_logger
    with _audit_lock:
        _audit_logger = None
