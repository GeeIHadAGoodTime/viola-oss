from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from core.constants import TIMEOUT_VERY_LONG
from core.database_strategy import postgres_url_for_surface
from core.logging_config import get_logger

from .security import (
    MAX_COMPANION_CAPABILITIES_JSON_BYTES,
    MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
    generate_device_token,
    hash_device_token,
    normalize_capabilities,
    validate_companion_json_payload,
    verify_device_token,
)

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DB_FILENAME = "companion_registry.sqlite3"
_SCHEMA_VERSION = 1
_SCHEMA_LOCK = threading.Lock()
_REGISTRY_SINGLETON: CompanionDeviceRegistry | None = None

_COMMAND_STATUS_PENDING = "pending"
_COMMAND_STATUS_DISPATCHED = "dispatched"
_COMMAND_STATUS_STREAMING = "streaming"
_COMMAND_STATUS_SUCCEEDED = "succeeded"
_COMMAND_STATUS_FAILED = "failed"
_COMMAND_STATUS_TIMED_OUT = "timed_out"
_COMMAND_STATUS_REVOKED = "revoked"
_ACTIVE_STATUSES = frozenset(
    {
        _COMMAND_STATUS_PENDING,
        _COMMAND_STATUS_DISPATCHED,
        _COMMAND_STATUS_STREAMING,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        _COMMAND_STATUS_SUCCEEDED,
        _COMMAND_STATUS_FAILED,
        _COMMAND_STATUS_TIMED_OUT,
        _COMMAND_STATUS_REVOKED,
    }
)
_COMMAND_RESULT_RETENTION_SECONDS = 15 * 60.0

_PG_REQUIRED_RELATIONS = (
    "public.companion_devices",
    "public.companion_commands",
    "public.companion_audit_log",
)

_PG_STATUS_BY_INTERNAL = {
    _COMMAND_STATUS_PENDING: "queued",
    _COMMAND_STATUS_DISPATCHED: "sent",
    _COMMAND_STATUS_STREAMING: "in_progress",
    _COMMAND_STATUS_SUCCEEDED: "completed",
    _COMMAND_STATUS_FAILED: "failed",
    _COMMAND_STATUS_TIMED_OUT: "expired",
    _COMMAND_STATUS_REVOKED: "cancelled",
}
_INTERNAL_STATUS_BY_PG = {value: key for key, value in _PG_STATUS_BY_INTERNAL.items()}


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _json_loads(payload: Any, *, default: Any) -> Any:
    if payload is None:
        return default
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, memoryview):
        payload = payload.tobytes().decode("utf-8")
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str) or not payload:
        return default
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Failed to decode companion JSON payload")
        return default


def _pg_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(float(value), UTC)


def _row_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        resolved = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return resolved.timestamp()
    return float(value)


def _pg_status(status: str) -> str:
    return _PG_STATUS_BY_INTERNAL.get(status, "failed")


def _internal_status(status: str) -> str:
    return _INTERNAL_STATUS_BY_PG.get(status, status)


def _pg_rows_affected(result: str) -> int:
    try:
        return int(str(result).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _require_user_id(user_id: str) -> str:
    resolved = str(user_id or "").strip()
    if not resolved:
        raise ValueError("user_id is required")
    return resolved


def _require_device_id(device_id: str) -> str:
    resolved = str(device_id or "").strip()
    if not resolved:
        raise ValueError("device_id is required")
    return resolved


def _require_request_id(request_id: str) -> str:
    resolved = str(request_id or "").strip()
    if not resolved:
        raise ValueError("request_id is required")
    return resolved


def _require_device_name(device_name: str) -> str:
    resolved = str(device_name or "").strip()
    if not resolved:
        raise ValueError("device_name is required")
    return resolved


@dataclass(slots=True)
class CompanionDevice:
    device_id: str
    user_id: str
    device_name: str
    device_token_hash: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    platform: str = "unknown"
    last_heartbeat: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    revoked_at: float | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def to_public_dict(
        self,
        *,
        online: bool = False,
        latency_ms: float | None = None,
    ) -> dict[str, Any]:
        return {
            "id": self.device_id,
            "user_id": self.user_id,
            "device_name": self.device_name,
            "capabilities": self.capabilities,
            "platform": self.platform,
            "last_heartbeat": self.last_heartbeat,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
            "online": online,
            "latency_ms": latency_ms,
        }


@dataclass(slots=True)
class CompanionCommand:
    request_id: str
    user_id: str
    device_id: str
    message_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = _COMMAND_STATUS_PENDING
    attempt_count: int = 0
    max_attempts: int = 1
    timeout_seconds: float = TIMEOUT_VERY_LONG
    progress_payload: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] = field(default_factory=dict)
    error_text: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "message_type": self.message_type,
            "payload": self.payload,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "progress": self.progress_payload,
            "result": self.result_payload,
            "error": self.error_text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


class CompanionRegistryBackend(Protocol):
    """Backend contract for companion device, command, and audit persistence.

    Every public operation exposed by :class:`CompanionDeviceRegistry` is
    represented here. Backend-specific SQL and dialect behavior lives in the
    concrete implementations; no ``_use_pg`` strategy branches belong in the
    facade or any backend method.
    """

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def db_path(self) -> Path | None: ...

    @property
    def pg_initialized(self) -> bool: ...

    async def initialize(self) -> None: ...

    def close(self) -> None: ...

    async def register_device(
        self,
        *,
        device_id: str,
        user_id: str,
        device_name: str,
        token_hash: str,
        capabilities: dict[str, Any],
        platform: str,
        now: float,
    ) -> CompanionDevice: ...

    async def verify_device(self, *, device_id: str, token: str) -> CompanionDevice | None: ...

    async def update_heartbeat(
        self,
        *,
        device_id: str,
        capabilities: dict[str, Any] | None,
        now: float,
    ) -> CompanionDevice | None: ...

    async def list_devices(self, *, user_id: str) -> list[CompanionDevice]: ...

    async def get_device(self, *, user_id: str, device_id: str) -> CompanionDevice | None: ...

    async def revoke_device(self, *, user_id: str, device_id: str, now: float) -> bool: ...

    async def create_command(
        self,
        *,
        request_id: str,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
        max_attempts: int,
        timeout_seconds: float,
        now: float,
    ) -> CompanionCommand: ...

    async def mark_command_dispatched(self, *, request_id: str, now: float) -> CompanionCommand | None: ...

    async def mark_command_progress(
        self,
        *,
        request_id: str,
        progress_payload: dict[str, Any],
        status: str,
        now: float,
    ) -> CompanionCommand | None: ...

    async def complete_command(
        self,
        *,
        request_id: str,
        status: str,
        result_payload: dict[str, Any],
        error_text: str | None,
        now: float,
    ) -> CompanionCommand | None: ...

    async def requeue_inflight_commands(
        self,
        *,
        device_id: str,
        reason: str,
        now: float,
    ) -> list[CompanionCommand]: ...

    async def list_pending_commands(self, *, device_id: str, limit: int) -> list[CompanionCommand]: ...

    async def get_command(
        self,
        *,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None: ...

    async def get_command_by_request_id(self, *, request_id: str) -> CompanionCommand | None: ...

    async def expire_stale_commands(self, *, now: float) -> int: ...

    async def append_audit_event(
        self,
        *,
        event_id: str,
        user_id: str,
        device_id: str,
        action: str,
        request_id: str | None,
        details: dict[str, Any],
        now: float,
    ) -> None: ...


def _sqlite_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    try:
        from config.settings import settings

        return Path(settings.data_dir)
    except Exception:
        return Path.cwd()


class SqliteCompanionRegistryBackend:
    """SQLite implementation of companion device/command/audit persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, root: Path | None = None) -> None:
        base_path = _sqlite_root(root)
        self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
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
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema(skip_lock=True)

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._ensure_schema)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("WAL checkpoint failed while closing companion registry")
            try:
                self._conn.close()
            except Exception:
                logger.exception("Failed to close companion registry SQLite connection")

    def _ensure_schema(self, *, skip_lock: bool = False) -> None:
        schema_lock = nullcontext() if skip_lock else _SCHEMA_LOCK
        with schema_lock, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_schema_version (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """)
            row = self._conn.execute(
                """
                SELECT version
                FROM companion_schema_version
                WHERE component = ?
                """,
                ("registry",),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO companion_schema_version(component, version)
                    VALUES (?, ?)
                    """,
                    ("registry", _SCHEMA_VERSION),
                )
            elif int(row["version"]) < _SCHEMA_VERSION:
                self._conn.execute(
                    """
                    UPDATE companion_schema_version
                    SET version = ?
                    WHERE component = ?
                    """,
                    (_SCHEMA_VERSION, "registry"),
                )
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_devices (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    device_token_hash TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    created_at REAL NOT NULL,
                    revoked_at REAL
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_companion_devices_user_id
                ON companion_devices(user_id)
                """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_commands (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    timeout_seconds REAL NOT NULL,
                    progress_payload TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    error_text TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    FOREIGN KEY(device_id) REFERENCES companion_devices(id)
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_companion_commands_device_status
                ON companion_commands(device_id, status, created_at)
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_companion_commands_user_id
                ON companion_commands(user_id, created_at)
                """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_audit_log (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    request_id TEXT,
                    details TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_companion_audit_user_device
                ON companion_audit_log(user_id, device_id, created_at)
                """)

    @staticmethod
    def _row_to_device(row: sqlite3.Row) -> CompanionDevice:
        return CompanionDevice(
            device_id=str(row["id"]),
            user_id=str(row["user_id"]),
            device_name=str(row["device_name"]),
            device_token_hash=str(row["device_token_hash"]),
            capabilities=_json_loads(row["capabilities"], default={}),
            platform=str(row["platform"]),
            last_heartbeat=float(row["last_heartbeat"]),
            created_at=float(row["created_at"]),
            revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
        )

    @staticmethod
    def _row_to_command(row: sqlite3.Row) -> CompanionCommand:
        return CompanionCommand(
            request_id=str(row["request_id"]),
            user_id=str(row["user_id"]),
            device_id=str(row["device_id"]),
            message_type=str(row["message_type"]),
            payload=_json_loads(row["payload"], default={}),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            timeout_seconds=float(row["timeout_seconds"]),
            progress_payload=_json_loads(row["progress_payload"], default={}),
            result_payload=_json_loads(row["result_payload"], default={}),
            error_text=str(row["error_text"]) if row["error_text"] is not None else None,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    async def register_device(
        self,
        *,
        device_id: str,
        user_id: str,
        device_name: str,
        token_hash: str,
        capabilities: dict[str, Any],
        platform: str,
        now: float,
    ) -> CompanionDevice:
        return await asyncio.to_thread(
            self._register_device,
            device_id,
            user_id,
            device_name,
            token_hash,
            capabilities,
            platform,
            now,
        )

    def _register_device(
        self,
        device_id: str,
        user_id: str,
        device_name: str,
        token_hash: str,
        capabilities: dict[str, Any],
        platform: str,
        now: float,
    ) -> CompanionDevice:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO companion_devices(
                    id,
                    user_id,
                    device_name,
                    device_token_hash,
                    capabilities,
                    platform,
                    last_heartbeat,
                    created_at,
                    revoked_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    device_id,
                    user_id,
                    device_name,
                    token_hash,
                    _json_dumps(capabilities),
                    platform,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Companion registration completed without a persisted row")
        return self._row_to_device(row)

    async def verify_device(self, *, device_id: str, token: str) -> CompanionDevice | None:
        return await asyncio.to_thread(self._verify_device, device_id, token)

    def _verify_device(self, device_id: str, token: str) -> CompanionDevice | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE id = ? AND revoked_at IS NULL
                """,
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        if not verify_device_token(token, str(row["device_token_hash"])):
            return None
        return self._row_to_device(row)

    async def update_heartbeat(
        self,
        *,
        device_id: str,
        capabilities: dict[str, Any] | None,
        now: float,
    ) -> CompanionDevice | None:
        return await asyncio.to_thread(self._update_heartbeat, device_id, capabilities, now)

    def _update_heartbeat(
        self,
        device_id: str,
        capabilities: dict[str, Any] | None,
        now: float,
    ) -> CompanionDevice | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE id = ? AND revoked_at IS NULL
                """,
                (device_id,),
            ).fetchone()
            if row is None:
                return None
            persisted_capabilities = (
                capabilities if capabilities is not None else _json_loads(row["capabilities"], default={})
            )
            self._conn.execute(
                """
                UPDATE companion_devices
                SET capabilities = ?, last_heartbeat = ?
                WHERE id = ?
                """,
                (
                    _json_dumps(persisted_capabilities),
                    now,
                    device_id,
                ),
            )
            fresh = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE id = ?
                """,
                (device_id,),
            ).fetchone()
        return self._row_to_device(fresh) if fresh is not None else None

    async def list_devices(self, *, user_id: str) -> list[CompanionDevice]:
        return await asyncio.to_thread(self._list_devices, user_id)

    def _list_devices(self, user_id: str) -> list[CompanionDevice]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE user_id = ? AND revoked_at IS NULL
                ORDER BY last_heartbeat DESC, device_name ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_device(row) for row in rows]

    async def get_device(self, *, user_id: str, device_id: str) -> CompanionDevice | None:
        return await asyncio.to_thread(self._get_device, user_id, device_id)

    def _get_device(self, user_id: str, device_id: str) -> CompanionDevice | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_devices
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (device_id, user_id),
            ).fetchone()
        return self._row_to_device(row) if row is not None else None

    async def revoke_device(self, *, user_id: str, device_id: str, now: float) -> bool:
        return await asyncio.to_thread(self._revoke_device, user_id, device_id, now)

    def _revoke_device(self, user_id: str, device_id: str, now: float) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE companion_devices
                SET revoked_at = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (now, device_id, user_id),
            )
            self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?,
                    error_text = COALESCE(error_text, 'Device revoked'),
                    updated_at = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE device_id = ? AND status IN (?, ?, ?)
                """,
                (
                    _COMMAND_STATUS_REVOKED,
                    now,
                    now,
                    device_id,
                    _COMMAND_STATUS_PENDING,
                    _COMMAND_STATUS_DISPATCHED,
                    _COMMAND_STATUS_STREAMING,
                ),
            )
        return cursor.rowcount > 0

    async def create_command(
        self,
        *,
        request_id: str,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
        max_attempts: int,
        timeout_seconds: float,
        now: float,
    ) -> CompanionCommand:
        return await asyncio.to_thread(
            self._create_command,
            request_id,
            user_id,
            device_id,
            message_type,
            payload,
            max_attempts,
            timeout_seconds,
            now,
        )

    def _create_command(
        self,
        request_id: str,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
        max_attempts: int,
        timeout_seconds: float,
        now: float,
    ) -> CompanionCommand:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO companion_commands(
                    request_id,
                    user_id,
                    device_id,
                    message_type,
                    payload,
                    status,
                    attempt_count,
                    max_attempts,
                    timeout_seconds,
                    progress_payload,
                    result_payload,
                    error_text,
                    created_at,
                    updated_at,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, '{}', '{}', NULL, ?, ?, NULL)
                """,
                (
                    request_id,
                    user_id,
                    device_id,
                    message_type,
                    _json_dumps(payload),
                    _COMMAND_STATUS_PENDING,
                    max_attempts,
                    timeout_seconds,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Command creation completed without a persisted row")
        return self._row_to_command(row)

    async def mark_command_dispatched(self, *, request_id: str, now: float) -> CompanionCommand | None:
        return await asyncio.to_thread(self._mark_command_dispatched, request_id, now)

    def _mark_command_dispatched(self, request_id: str, now: float) -> CompanionCommand | None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE request_id = ? AND status IN (?, ?, ?)
                """,
                (
                    _COMMAND_STATUS_DISPATCHED,
                    now,
                    request_id,
                    _COMMAND_STATUS_PENDING,
                    _COMMAND_STATUS_STREAMING,
                    _COMMAND_STATUS_DISPATCHED,
                ),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._row_to_command(row) if row is not None else None

    async def mark_command_progress(
        self,
        *,
        request_id: str,
        progress_payload: dict[str, Any],
        status: str,
        now: float,
    ) -> CompanionCommand | None:
        return await asyncio.to_thread(
            self._mark_command_progress,
            request_id,
            progress_payload,
            status,
            now,
        )

    def _mark_command_progress(
        self,
        request_id: str,
        progress_payload: dict[str, Any],
        status: str,
        now: float,
    ) -> CompanionCommand | None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?, progress_payload = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    _json_dumps(progress_payload),
                    now,
                    request_id,
                ),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._row_to_command(row) if row is not None else None

    async def complete_command(
        self,
        *,
        request_id: str,
        status: str,
        result_payload: dict[str, Any],
        error_text: str | None,
        now: float,
        expected_device_id: str,
    ) -> CompanionCommand | None:
        return await asyncio.to_thread(
            self._complete_command,
            request_id,
            status,
            result_payload,
            error_text,
            now,
            expected_device_id,
        )

    def _complete_command(
        self,
        request_id: str,
        status: str,
        payload: dict[str, Any],
        error_text: str | None,
        now: float,
        expected_device_id: str,
    ) -> CompanionCommand | None:
        # Phase 5 W2-DEV-002: UPDATE must include device_id predicate so a
        # device on user-B's account that knows user-A's request_id UUID
        # cannot overwrite user-A's command result. Forgery silently no-ops
        # (rowcount=0, SELECT returns None) so the attacker cannot probe
        # whether a request_id exists by varying the device_id.
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?,
                    result_payload = ?,
                    error_text = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE request_id = ? AND device_id = ?
                """,
                (
                    status,
                    _json_dumps(payload),
                    error_text,
                    now,
                    now,
                    request_id,
                    expected_device_id,
                ),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ? AND device_id = ?
                """,
                (request_id, expected_device_id),
            ).fetchone()
        return self._row_to_command(row) if row is not None else None

    async def requeue_inflight_commands(
        self,
        *,
        device_id: str,
        reason: str,
        now: float,
    ) -> list[CompanionCommand]:
        return await asyncio.to_thread(self._requeue_inflight_commands, device_id, reason, now)

    def _requeue_inflight_commands(
        self,
        device_id: str,
        reason: str,
        now: float,
    ) -> list[CompanionCommand]:
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?, error_text = ?, updated_at = ?
                WHERE device_id = ? AND status IN (?, ?)
                """,
                (
                    _COMMAND_STATUS_PENDING,
                    reason,
                    now,
                    device_id,
                    _COMMAND_STATUS_DISPATCHED,
                    _COMMAND_STATUS_STREAMING,
                ),
            )
            rows = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE device_id = ? AND status = ?
                ORDER BY created_at ASC
                """,
                (device_id, _COMMAND_STATUS_PENDING),
            ).fetchall()
        return [self._row_to_command(row) for row in rows]

    async def list_pending_commands(self, *, device_id: str, limit: int) -> list[CompanionCommand]:
        return await asyncio.to_thread(self._list_pending_commands, device_id, limit)

    def _list_pending_commands(self, device_id: str, limit: int) -> list[CompanionCommand]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE device_id = ? AND status = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (device_id, _COMMAND_STATUS_PENDING, limit),
            ).fetchall()
        return [self._row_to_command(row) for row in rows]

    async def get_command(
        self,
        *,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None:
        return await asyncio.to_thread(self._get_command, user_id, device_id, request_id)

    def _get_command(
        self,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ? AND user_id = ? AND device_id = ?
                """,
                (request_id, user_id, device_id),
            ).fetchone()
        return self._row_to_command(row) if row is not None else None

    async def get_command_by_request_id(self, *, request_id: str) -> CompanionCommand | None:
        return await asyncio.to_thread(self._get_command_by_request_id, request_id)

    def _get_command_by_request_id(self, request_id: str) -> CompanionCommand | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._row_to_command(row) if row is not None else None

    async def expire_stale_commands(self, *, now: float) -> int:
        return await asyncio.to_thread(self._expire_stale_commands, now)

    def _expire_stale_commands(self, now: float) -> int:
        cutoff = float(now) - _COMMAND_RESULT_RETENTION_SECONDS
        with self._lock, self._conn:
            expired = self._conn.execute(
                """
                UPDATE companion_commands
                SET status = ?,
                    error_text = COALESCE(error_text, ?),
                    updated_at = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE status IN (?, ?, ?)
                  AND created_at + timeout_seconds <= ?
                """,
                (
                    _COMMAND_STATUS_TIMED_OUT,
                    "Companion command expired",
                    now,
                    now,
                    _COMMAND_STATUS_PENDING,
                    _COMMAND_STATUS_DISPATCHED,
                    _COMMAND_STATUS_STREAMING,
                    now,
                ),
            )
            deleted = self._conn.execute(
                """
                DELETE FROM companion_commands
                WHERE status IN (?, ?, ?, ?)
                  AND completed_at IS NOT NULL
                  AND completed_at <= ?
                """,
                (
                    _COMMAND_STATUS_SUCCEEDED,
                    _COMMAND_STATUS_FAILED,
                    _COMMAND_STATUS_TIMED_OUT,
                    _COMMAND_STATUS_REVOKED,
                    cutoff,
                ),
            )
        return max(0, expired.rowcount) + max(0, deleted.rowcount)

    async def append_audit_event(
        self,
        *,
        event_id: str,
        user_id: str,
        device_id: str,
        action: str,
        request_id: str | None,
        details: dict[str, Any],
        now: float,
    ) -> None:
        await asyncio.to_thread(
            self._append_audit_event,
            event_id,
            user_id,
            device_id,
            action,
            request_id,
            details,
            now,
        )

    def _append_audit_event(
        self,
        event_id: str,
        user_id: str,
        device_id: str,
        action: str,
        request_id: str | None,
        details: dict[str, Any],
        now: float,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO companion_audit_log(
                    id,
                    user_id,
                    device_id,
                    action,
                    request_id,
                    details,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    user_id,
                    device_id,
                    action,
                    request_id,
                    _json_dumps(details),
                    now,
                ),
            )


class PostgresCompanionRegistryBackend:
    """PostgreSQL implementation of companion device/command/audit persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresCompanionRegistryBackend requires a PostgreSQL database URL")
        self._database_url = database_url
        self._initialized = False

    @property
    def db_path(self) -> Path | None:
        return None

    @property
    def pg_initialized(self) -> bool:
        return self._initialized

    async def _pg_pool(self) -> asyncpg.Pool:
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    async def initialize(self) -> None:
        if self._initialized:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="CompanionDeviceRegistry")
        self._initialized = True
        logger.info("CompanionDeviceRegistry initialized (PostgreSQL)")

    def close(self) -> None:
        return

    @staticmethod
    def _row_to_device(row: asyncpg.Record) -> CompanionDevice:
        return CompanionDevice(
            device_id=str(row["id"]),
            user_id=str(row["user_id"]),
            device_name=str(row["device_name"]),
            device_token_hash=str(row["device_token_hash"]),
            capabilities=_json_loads(row["capabilities"], default={}),
            platform=str(row["platform"]),
            last_heartbeat=_row_timestamp(row["last_heartbeat"]),
            created_at=_row_timestamp(row["created_at"]),
            revoked_at=_row_timestamp(row["revoked_at"]) if row["revoked_at"] is not None else None,
        )

    @staticmethod
    def _row_to_command(row: asyncpg.Record) -> CompanionCommand:
        payload = _json_loads(row["payload"], default={})
        if not isinstance(payload, dict):
            payload = {}
        return CompanionCommand(
            request_id=str(row["request_id"]),
            user_id=str(row["user_id"]),
            device_id=str(row["device_id"]),
            message_type=str(row["message_type"]),
            payload=payload,
            status=_internal_status(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            timeout_seconds=float(row["timeout_seconds"]),
            progress_payload=(
                payload.get("progress_payload", {}) if isinstance(payload.get("progress_payload"), dict) else {}
            ),
            result_payload=payload.get("result_payload", {}) if isinstance(payload.get("result_payload"), dict) else {},
            error_text=str(row["error_code"]) if row["error_code"] is not None else None,
            created_at=_row_timestamp(row["created_at"]),
            updated_at=_row_timestamp(row["updated_at"]),
            completed_at=_row_timestamp(row["completed_at"]) if row["completed_at"] is not None else None,
        )

    async def register_device(
        self,
        *,
        device_id: str,
        user_id: str,
        device_name: str,
        token_hash: str,
        capabilities: dict[str, Any],
        platform: str,
        now: float,
    ) -> CompanionDevice:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO companion_devices(
                    id,
                    user_id,
                    device_name,
                    device_token_hash,
                    capabilities,
                    platform,
                    last_heartbeat,
                    created_at,
                    revoked_at
                )
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $7, NULL)
                """,
                device_id,
                user_id,
                device_name,
                token_hash,
                _json_dumps(capabilities),
                platform,
                _pg_timestamp(now),
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_devices
                WHERE id = $1
                """,
                device_id,
            )
        if row is None:
            raise RuntimeError("Companion registration completed without a persisted row")
        return self._row_to_device(row)

    async def verify_device(self, *, device_id: str, token: str) -> CompanionDevice | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_devices
                WHERE id = $1 AND revoked_at IS NULL
                """,
                device_id,
            )
        if row is None:
            return None
        if not await asyncio.to_thread(verify_device_token, token, str(row["device_token_hash"])):
            return None
        return self._row_to_device(row)

    async def update_heartbeat(
        self,
        *,
        device_id: str,
        capabilities: dict[str, Any] | None,
        now: float,
    ) -> CompanionDevice | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT *
                FROM companion_devices
                WHERE id = $1 AND revoked_at IS NULL
                """,
                device_id,
            )
            if existing is None:
                return None
            persisted_capabilities = (
                capabilities if capabilities is not None else _json_loads(existing["capabilities"], default={})
            )
            await conn.execute(
                """
                UPDATE companion_devices
                SET capabilities = $2::jsonb,
                    last_heartbeat = $3
                WHERE id = $1
                """,
                device_id,
                _json_dumps(persisted_capabilities),
                _pg_timestamp(now),
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_devices
                WHERE id = $1
                """,
                device_id,
            )
        return self._row_to_device(row) if row is not None else None

    async def list_devices(self, *, user_id: str) -> list[CompanionDevice]:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM companion_devices
                WHERE user_id = $1 AND revoked_at IS NULL
                ORDER BY last_heartbeat DESC, device_name ASC
                """,
                user_id,
            )
        return [self._row_to_device(row) for row in rows]

    async def get_device(self, *, user_id: str, device_id: str) -> CompanionDevice | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_devices
                WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
                """,
                device_id,
                user_id,
            )
        return self._row_to_device(row) if row is not None else None

    async def revoke_device(self, *, user_id: str, device_id: str, now: float) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE companion_devices
                SET revoked_at = $3
                WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
                """,
                device_id,
                user_id,
                _pg_timestamp(now),
            )
            await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    error_code = COALESCE(error_code, 'Device revoked'),
                    updated_at = $3,
                    completed_at = COALESCE(completed_at, $3)
                WHERE device_id = $1 AND status = ANY($4::text[])
                """,
                device_id,
                _pg_status(_COMMAND_STATUS_REVOKED),
                _pg_timestamp(now),
                [_pg_status(status) for status in _ACTIVE_STATUSES],
            )
        return result.endswith("1")

    async def create_command(
        self,
        *,
        request_id: str,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
        max_attempts: int,
        timeout_seconds: float,
        now: float,
    ) -> CompanionCommand:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO companion_commands(
                    request_id,
                    user_id,
                    device_id,
                    message_type,
                    payload,
                    status,
                    attempt_count,
                    max_attempts,
                    timeout_seconds,
                    result_hash,
                    error_code,
                    created_at,
                    updated_at,
                    completed_at,
                    expires_at
                )
                VALUES (
                    $1, $2, $3, $4, $5::jsonb, $6, 0, $7, $8,
                    NULL, NULL, $9, $9, NULL, $10
                )
                """,
                request_id,
                user_id,
                device_id,
                message_type,
                _json_dumps(payload),
                _pg_status(_COMMAND_STATUS_PENDING),
                max_attempts,
                timeout_seconds,
                _pg_timestamp(now),
                _pg_timestamp(now + timeout_seconds),
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1
                """,
                request_id,
            )
        if row is None:
            raise RuntimeError("Command creation completed without a persisted row")
        return self._row_to_command(row)

    async def mark_command_dispatched(self, *, request_id: str, now: float) -> CompanionCommand | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    attempt_count = attempt_count + 1,
                    updated_at = $3
                WHERE request_id = $1 AND status = ANY($4::text[])
                """,
                request_id,
                _pg_status(_COMMAND_STATUS_DISPATCHED),
                _pg_timestamp(now),
                [
                    _pg_status(_COMMAND_STATUS_PENDING),
                    _pg_status(_COMMAND_STATUS_STREAMING),
                    _pg_status(_COMMAND_STATUS_DISPATCHED),
                ],
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1
                """,
                request_id,
            )
        return self._row_to_command(row) if row is not None else None

    async def mark_command_progress(
        self,
        *,
        request_id: str,
        progress_payload: dict[str, Any],
        status: str,
        now: float,
    ) -> CompanionCommand | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    payload = jsonb_set(payload, '{progress_payload}', $3::jsonb, true),
                    updated_at = $4
                WHERE request_id = $1
                """,
                request_id,
                _pg_status(status),
                _json_dumps(progress_payload),
                _pg_timestamp(now),
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1
                """,
                request_id,
            )
        return self._row_to_command(row) if row is not None else None

    async def complete_command(
        self,
        *,
        request_id: str,
        status: str,
        result_payload: dict[str, Any],
        error_text: str | None,
        now: float,
        expected_device_id: str,
    ) -> CompanionCommand | None:
        # Phase 5 W2-DEV-002: UPDATE keyed on (request_id, device_id) so a
        # device on user-B's account cannot overwrite user-A's command
        # result by guessing the UUID. Forgery silently no-ops.
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    payload = jsonb_set(payload, '{result_payload}', $3::jsonb, true),
                    result_hash = $4,
                    error_code = $5,
                    updated_at = $6,
                    completed_at = $6
                WHERE request_id = $1 AND device_id = $7
                """,
                request_id,
                _pg_status(status),
                _json_dumps(result_payload),
                _hash_payload(result_payload),
                error_text,
                _pg_timestamp(now),
                expected_device_id,
            )
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1 AND device_id = $2
                """,
                request_id,
                expected_device_id,
            )
        return self._row_to_command(row) if row is not None else None

    async def requeue_inflight_commands(
        self,
        *,
        device_id: str,
        reason: str,
        now: float,
    ) -> list[CompanionCommand]:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    error_code = $3,
                    updated_at = $4
                WHERE device_id = $1 AND status = ANY($5::text[])
                """,
                device_id,
                _pg_status(_COMMAND_STATUS_PENDING),
                reason,
                _pg_timestamp(now),
                [_pg_status(_COMMAND_STATUS_DISPATCHED), _pg_status(_COMMAND_STATUS_STREAMING)],
            )
            rows = await conn.fetch(
                """
                SELECT *
                FROM companion_commands
                WHERE device_id = $1 AND status = $2
                ORDER BY created_at ASC
                """,
                device_id,
                _pg_status(_COMMAND_STATUS_PENDING),
            )
        return [self._row_to_command(row) for row in rows]

    async def list_pending_commands(self, *, device_id: str, limit: int) -> list[CompanionCommand]:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM companion_commands
                WHERE device_id = $1 AND status = $2
                ORDER BY created_at ASC
                LIMIT $3
                """,
                device_id,
                _pg_status(_COMMAND_STATUS_PENDING),
                limit,
            )
        return [self._row_to_command(row) for row in rows]

    async def get_command(
        self,
        *,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1 AND user_id = $2 AND device_id = $3
                """,
                request_id,
                user_id,
                device_id,
            )
        return self._row_to_command(row) if row is not None else None

    async def get_command_by_request_id(self, *, request_id: str) -> CompanionCommand | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM companion_commands
                WHERE request_id = $1
                """,
                request_id,
            )
        return self._row_to_command(row) if row is not None else None

    async def expire_stale_commands(self, *, now: float) -> int:
        await self.initialize()
        pool = await self._pg_pool()
        cutoff = _pg_timestamp(float(now) - _COMMAND_RESULT_RETENTION_SECONDS)
        async with pool.acquire() as conn:
            expired = await conn.execute(
                """
                UPDATE companion_commands
                SET status = $2,
                    error_code = COALESCE(error_code, 'Companion command expired'),
                    updated_at = $1,
                    completed_at = COALESCE(completed_at, $1)
                WHERE status = ANY($3::text[])
                  AND expires_at <= $1
                """,
                _pg_timestamp(now),
                _pg_status(_COMMAND_STATUS_TIMED_OUT),
                [_pg_status(status) for status in _ACTIVE_STATUSES],
            )
            deleted = await conn.execute(
                """
                DELETE FROM companion_commands
                WHERE status = ANY($2::text[])
                  AND completed_at IS NOT NULL
                  AND completed_at <= $1
                """,
                cutoff,
                [_pg_status(status) for status in _TERMINAL_STATUSES],
            )
        return _pg_rows_affected(expired) + _pg_rows_affected(deleted)

    async def append_audit_event(
        self,
        *,
        event_id: str,
        user_id: str,
        device_id: str,
        action: str,
        request_id: str | None,
        details: dict[str, Any],
        now: float,
    ) -> None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO companion_audit_log(
                    id,
                    user_id,
                    device_id,
                    action,
                    request_id,
                    details_hash,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                event_id,
                user_id,
                device_id,
                action,
                request_id,
                _hash_payload(details),
                _pg_timestamp(now),
            )


def create_companion_registry_backend(
    *,
    root: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> CompanionRegistryBackend:
    """Create a companion registry backend selected by application surface.

    Desktop always uses SQLite. Cloud always uses PostgreSQL. The env var is
    therefore treated as cloud infrastructure, never as a generic strategy
    toggle inside the registry facade.
    """
    pg_url = postgres_url_for_surface(
        "CompanionDeviceRegistry",
        app_surface=app_surface,
        database_url=database_url,
    )
    if pg_url is None:
        return SqliteCompanionRegistryBackend(root=root)
    return PostgresCompanionRegistryBackend(pg_url)


class CompanionDeviceRegistry:
    """Durable companion device, command, and audit registry.

    Backend-specific SQL and dialect behavior live in
    :class:`CompanionRegistryBackend` implementations. This facade owns the
    public API, input validation, identifier minting, and token hashing.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        backend: CompanionRegistryBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend: CompanionRegistryBackend = backend or create_companion_registry_backend(
            root=root,
            app_surface=app_surface,
            database_url=database_url,
        )

    @property
    def backend_name(self) -> str:
        return self._backend.backend_name

    @property
    def db_path(self) -> Path | None:
        return self._backend.db_path

    @property
    def pg_initialized(self) -> bool:
        return self._backend.pg_initialized

    async def pg_initialize(self) -> None:
        await self._backend.initialize()

    def close(self) -> None:
        self._backend.close()

    async def register_device(
        self,
        *,
        user_id: str,
        device_name: str,
        capabilities: dict[str, Any] | None = None,
        platform: str = "unknown",
    ) -> tuple[CompanionDevice, str]:
        resolved_user_id = _require_user_id(user_id)
        resolved_name = _require_device_name(device_name)
        normalized_capabilities = validate_companion_json_payload(
            normalize_capabilities(capabilities),
            field="companion capabilities",
            max_bytes=MAX_COMPANION_CAPABILITIES_JSON_BYTES,
        )
        now = time.time()
        device_id = uuid.uuid4().hex
        token = generate_device_token()
        token_hash = await asyncio.to_thread(hash_device_token, token)
        device = await self._backend.register_device(
            device_id=device_id,
            user_id=resolved_user_id,
            device_name=resolved_name,
            token_hash=token_hash,
            capabilities=normalized_capabilities,
            platform=platform.strip() or "unknown",
            now=now,
        )
        return device, token

    async def verify_device(
        self,
        *,
        device_id: str,
        token: str,
    ) -> CompanionDevice | None:
        resolved_device_id = _require_device_id(device_id)
        return await self._backend.verify_device(device_id=resolved_device_id, token=token)

    async def verify_device_for_user(
        self,
        *,
        user_id: str,
        device_id: str,
        token: str,
    ) -> CompanionDevice | None:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        device = await self._backend.get_device(user_id=resolved_user_id, device_id=resolved_device_id)
        if device is None:
            return None
        if not await asyncio.to_thread(verify_device_token, token, device.device_token_hash):
            return None
        return device

    async def update_heartbeat(
        self,
        *,
        device_id: str,
        capabilities: dict[str, Any] | None = None,
    ) -> CompanionDevice | None:
        resolved_device_id = _require_device_id(device_id)
        normalized_capabilities = (
            validate_companion_json_payload(
                normalize_capabilities(capabilities),
                field="companion capabilities",
                max_bytes=MAX_COMPANION_CAPABILITIES_JSON_BYTES,
            )
            if capabilities is not None
            else None
        )
        return await self._backend.update_heartbeat(
            device_id=resolved_device_id,
            capabilities=normalized_capabilities,
            now=time.time(),
        )

    async def list_devices(self, *, user_id: str) -> list[CompanionDevice]:
        resolved_user_id = _require_user_id(user_id)
        return await self._backend.list_devices(user_id=resolved_user_id)

    async def get_device(self, *, user_id: str, device_id: str) -> CompanionDevice | None:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        return await self._backend.get_device(user_id=resolved_user_id, device_id=resolved_device_id)

    async def revoke_device(self, *, user_id: str, device_id: str) -> bool:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        return await self._backend.revoke_device(
            user_id=resolved_user_id,
            device_id=resolved_device_id,
            now=time.time(),
        )

    async def create_command(
        self,
        *,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
        max_attempts: int = 1,
        timeout_seconds: float = TIMEOUT_VERY_LONG,
    ) -> CompanionCommand:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        normalized_payload = validate_companion_json_payload(
            payload,
            field="companion command payload",
            max_bytes=MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
        )
        now = time.time()
        await self._backend.expire_stale_commands(now=now)
        return await self._backend.create_command(
            request_id=uuid.uuid4().hex,
            user_id=resolved_user_id,
            device_id=resolved_device_id,
            message_type=message_type,
            payload=normalized_payload,
            max_attempts=max(1, int(max_attempts)),
            timeout_seconds=float(timeout_seconds),
            now=now,
        )

    async def mark_command_dispatched(self, request_id: str) -> CompanionCommand | None:
        resolved_request_id = _require_request_id(request_id)
        return await self._backend.mark_command_dispatched(request_id=resolved_request_id, now=time.time())

    async def mark_command_progress(
        self,
        *,
        request_id: str,
        progress_payload: dict[str, Any],
        status: str | None = None,
        user_id: str | None = None,
        device_id: str | None = None,
    ) -> CompanionCommand | None:
        del user_id, device_id
        resolved_request_id = _require_request_id(request_id)
        normalized_progress_payload = validate_companion_json_payload(
            progress_payload,
            field="companion progress payload",
            max_bytes=MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
        )
        return await self._backend.mark_command_progress(
            request_id=resolved_request_id,
            progress_payload=normalized_progress_payload,
            status=status or _COMMAND_STATUS_STREAMING,
            now=time.time(),
        )

    async def complete_command(
        self,
        *,
        request_id: str,
        status: str,
        expected_device_id: str,
        result_payload: dict[str, Any] | None = None,
        error_text: str | None = None,
        user_id: str | None = None,
        device_id: str | None = None,
    ) -> CompanionCommand | None:
        """Mark a companion command as terminal, scoped to the device that owns it.

        Phase 5 W2-DEV-002 (Opus W2-device verifier, 2026-05-25): without
        ``expected_device_id`` enforcement the underlying UPDATE was keyed
        only on ``request_id``. A device authenticated on user-B's account
        could overwrite user-A's command result by sending an error/success
        WebSocket frame with a guessed UUID. ``expected_device_id`` MUST
        match the WS connection's authenticated device for device-initiated
        completion, or the command's persisted ``device_id`` for server-
        initiated timeout completion. Mismatch returns None silently —
        forging callers cannot probe whether a request_id exists by varying
        the device_id.
        """
        del user_id, device_id
        resolved_request_id = _require_request_id(request_id)
        resolved_device_id = _require_device_id(expected_device_id)
        normalized_status = status if status in _TERMINAL_STATUSES else _COMMAND_STATUS_FAILED
        payload = validate_companion_json_payload(
            result_payload or {},
            field="companion result payload",
            max_bytes=MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES,
        )
        return await self._backend.complete_command(
            request_id=resolved_request_id,
            status=normalized_status,
            result_payload=payload,
            error_text=error_text,
            now=time.time(),
            expected_device_id=resolved_device_id,
        )

    async def requeue_inflight_commands(self, *, device_id: str, reason: str) -> list[CompanionCommand]:
        resolved_device_id = _require_device_id(device_id)
        return await self._backend.requeue_inflight_commands(
            device_id=resolved_device_id,
            reason=reason,
            now=time.time(),
        )

    async def list_pending_commands(
        self,
        *,
        device_id: str,
        limit: int = 100,
    ) -> list[CompanionCommand]:
        resolved_device_id = _require_device_id(device_id)
        await self._backend.expire_stale_commands(now=time.time())
        return await self._backend.list_pending_commands(device_id=resolved_device_id, limit=max(1, int(limit)))

    async def get_command(
        self,
        *,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        resolved_request_id = _require_request_id(request_id)
        await self._backend.expire_stale_commands(now=time.time())
        return await self._backend.get_command(
            user_id=resolved_user_id,
            device_id=resolved_device_id,
            request_id=resolved_request_id,
        )

    async def get_command_by_request_id(self, request_id: str) -> CompanionCommand | None:
        resolved_request_id = _require_request_id(request_id)
        return await self._backend.get_command_by_request_id(request_id=resolved_request_id)

    async def expire_stale_commands(self, *, now: float | None = None) -> int:
        return await self._backend.expire_stale_commands(now=time.time() if now is None else float(now))

    async def append_audit_event(
        self,
        *,
        user_id: str,
        device_id: str,
        action: str,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        resolved_user_id = _require_user_id(user_id)
        resolved_device_id = _require_device_id(device_id)
        action_name = str(action or "").strip()
        if not action_name:
            raise ValueError("action is required")
        await self._backend.append_audit_event(
            event_id=uuid.uuid4().hex,
            user_id=resolved_user_id,
            device_id=resolved_device_id,
            action=action_name,
            request_id=request_id,
            details=details or {},
            now=time.time(),
        )


def get_companion_device_registry(*, root: Path | None = None) -> CompanionDeviceRegistry:
    global _REGISTRY_SINGLETON
    if root is not None:
        return CompanionDeviceRegistry(root=root)
    if _REGISTRY_SINGLETON is None:
        _REGISTRY_SINGLETON = CompanionDeviceRegistry()
    return _REGISTRY_SINGLETON


def reset_companion_device_registry_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _REGISTRY_SINGLETON
    if _REGISTRY_SINGLETON is not None:
        _REGISTRY_SINGLETON.close()
    _REGISTRY_SINGLETON = None


def _close_singleton() -> None:
    if _REGISTRY_SINGLETON is not None:
        _REGISTRY_SINGLETON.close()


atexit.register(_close_singleton)


__all__ = [
    "CompanionCommand",
    "CompanionDevice",
    "CompanionDeviceRegistry",
    "CompanionRegistryBackend",
    "PostgresCompanionRegistryBackend",
    "SqliteCompanionRegistryBackend",
    "create_companion_registry_backend",
    "get_companion_device_registry",
    "reset_companion_device_registry_for_tests",
]
