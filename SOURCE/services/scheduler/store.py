"""Persistent schedule store backed by SQLite or PostgreSQL."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar, cast

from core.asyncio_safe import run_async_synchronously
from core.database_strategy import postgres_url_for_surface
from core.logging_config import get_logger
from services.persistence.state_store import STATE_DB_SCHEMA_LOCK
from services.sync.journal import write_journal

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DB_FILENAME = "state.sqlite3"
_STORE_LOCK = threading.Lock()
_STORE_SINGLETON: ScheduleStore | None = None
_SYNC_TIMEOUT_SECONDS = 10.0
_TIMEOUT = object()
_T = TypeVar("_T")

MAX_CONSECUTIVE_FAILURES = 3
MAX_ENABLED_SCHEDULES = 20
_PG_TABLE = "sync_schedules"
_SYNC_ACTOR_ID = "schedule-store"
_SYNC_DEVICE_ID = "cloud"
_REQUIRED_PG_COLUMNS = {
    "id",
    "user_id",
    "label",
    "action",
    "cron_expr",
    "one_shot_at",
    "enabled",
    "last_run_at",
    "next_run_at",
    "run_count",
    "last_status",
    "last_error",
    "consecutive_failures",
    "consent_generation",
    "version_vector",
    "field_versions",
    "lww_hlc",
    "lww_actor_id",
    "commit_seq",
    "updated_by_device_id",
    "last_mutation_id",
    "created_at",
    "updated_at",
    "deleted_at",
}


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pg_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _pg_datetime(value: str | datetime | None, *, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise ValueError("Required PostgreSQL timestamp is missing")
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _pg_bool(value: bool | None) -> bool | None:
    return None if value is None else bool(value)


def _sync_hlc(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat()


def _mutation_id() -> str:
    return uuid.uuid4().hex


async def _write_schedule_journal(conn: asyncpg.Connection, row: Any, op: str) -> None:
    await write_journal(
        conn,
        str(row["user_id"]),
        "schedules",
        str(row["id"]),
        int(row["commit_seq"]),
        "delete" if op == "delete" else "upsert",
        str(row["last_mutation_id"]),
    )


@dataclass(frozen=True)
class Schedule:
    """A single schedule record."""

    id: int
    user_id: str
    label: str
    action: str
    cron_expr: str | None
    one_shot_at: str | None
    enabled: bool
    created_at: str
    updated_at: str
    last_run_at: str | None
    next_run_at: str
    run_count: int
    last_status: str | None
    last_error: str | None
    consecutive_failures: int = 0


class ScheduleBackend(Protocol):
    """Backend contract for scheduled action persistence."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def db_path(self) -> Path | None: ...

    @property
    def pg_initialized(self) -> bool: ...

    async def initialize(self) -> None: ...

    def add(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int: ...

    async def add_async(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int: ...

    def get(self, user_id: str, schedule_id: int) -> Schedule | None: ...

    async def get_async(self, user_id: str, schedule_id: int) -> Schedule | None: ...

    def get_by_label(self, user_id: str, label: str) -> Schedule | None: ...

    async def get_by_label_async(self, user_id: str, label: str) -> Schedule | None: ...

    def list_schedules(self, user_id: str, enabled_only: bool = True) -> list[Schedule]: ...

    async def list_schedules_async(self, user_id: str, enabled_only: bool = True) -> list[Schedule]: ...

    def get_due_schedules(self, user_id: str, now_iso: str) -> list[Schedule]: ...

    async def get_due_schedules_async(self, user_id: str, now_iso: str) -> list[Schedule]: ...

    def get_due_schedules_for_all_users(self, now_iso: str) -> list[Schedule]: ...

    async def get_due_schedules_for_all_users_async(self, now_iso: str) -> list[Schedule]: ...

    def update(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool: ...

    async def update_async(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool: ...

    def record_execution(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None: ...

    async def record_execution_async(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None: ...

    def reset_consecutive_failures(self, user_id: str, schedule_id: int) -> bool: ...

    async def reset_consecutive_failures_async(self, user_id: str, schedule_id: int) -> bool: ...

    def delete(self, user_id: str, schedule_id: int) -> bool: ...

    async def delete_async(self, user_id: str, schedule_id: int) -> bool: ...

    def close(self) -> None: ...

    def count_enabled(self, user_id: str) -> int: ...

    async def count_enabled_async(self, user_id: str) -> int: ...

    def count_enabled_all(self) -> int: ...

    async def count_enabled_all_async(self) -> int: ...


def _validate_schedule_inputs(
    label: str,
    action: str,
    cron_expr: str | None,
    one_shot_at: str | None,
) -> tuple[str, str]:
    label = label.strip()
    action = action.strip()
    if not label:
        raise ValueError("Schedule label cannot be empty")
    if not action:
        raise ValueError("Schedule action cannot be empty")
    if not cron_expr and not one_shot_at:
        raise ValueError("Either cron_expr or one_shot_at must be provided")
    if cron_expr and one_shot_at:
        raise ValueError("Cannot provide both cron_expr and one_shot_at")
    return label, action


def _sqlite_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    try:
        from config.settings import settings as cfg

        return Path(cfg.data_dir)
    except Exception:
        return Path.cwd()


class SqliteScheduleBackend:
    """SQLite implementation of scheduled action persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, root: Path | None = None) -> None:
        base_path = _sqlite_root(root)
        self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._ensure_schema)

    def _ensure_schema(self) -> None:
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id               TEXT NOT NULL,
                    label                 TEXT NOT NULL,
                    action                TEXT NOT NULL,
                    cron_expr             TEXT,
                    one_shot_at           TEXT,
                    enabled               INTEGER NOT NULL DEFAULT 1,
                    created_at            TEXT NOT NULL,
                    updated_at            TEXT NOT NULL,
                    last_run_at           TEXT,
                    next_run_at           TEXT NOT NULL,
                    run_count             INTEGER NOT NULL DEFAULT 0,
                    last_status           TEXT,
                    last_error            TEXT,
                    consecutive_failures  INTEGER NOT NULL DEFAULT 0,

                    CHECK (cron_expr IS NOT NULL OR one_shot_at IS NOT NULL),
                    CHECK (NOT (cron_expr IS NOT NULL AND one_shot_at IS NOT NULL))
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_user_next_run
                ON schedules(user_id, next_run_at) WHERE enabled = 1
                """)
            self._migrate_add_consecutive_failures()
        logger.debug("Schedule store schema ensured at %s", self._db_path)

    def _migrate_add_consecutive_failures(self) -> None:
        try:
            columns = [row["name"] for row in self._conn.execute("PRAGMA table_info(schedules)").fetchall()]
            if "consecutive_failures" not in columns:
                self._conn.execute("ALTER TABLE schedules ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
                logger.info("Migrated schedules table: added consecutive_failures column")
        except Exception:
            logger.exception("Failed to migrate consecutive_failures column")

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"],
            user_id=str(row["user_id"]),
            label=row["label"],
            action=row["action"],
            cron_expr=row["cron_expr"],
            one_shot_at=row["one_shot_at"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            run_count=row["run_count"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
        )

    def add(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        label, action = _validate_schedule_inputs(label, action, cron_expr, one_shot_at)
        enabled_count = self.count_enabled(user_id)
        if enabled_count >= MAX_ENABLED_SCHEDULES:
            existing = self.list_schedules(user_id, enabled_only=True)
            existing.sort(key=lambda schedule: schedule.last_run_at or "")
            schedule_list = "\n".join(
                "  id=%d label=%r last_run=%s" % (item.id, item.label, item.last_run_at or "never") for item in existing
            )
            raise ValueError(
                "Maximum number of enabled schedules (%d) reached. "
                "Delete or disable one first. Existing enabled schedules "
                "(oldest-run first):\n%s" % (MAX_ENABLED_SCHEDULES, schedule_list)
            )
        now = _utcnow_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO schedules
                    (user_id, label, action, cron_expr, one_shot_at, enabled,
                     created_at, updated_at, next_run_at, run_count)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 0)
                """,
                (user_id, label, action, cron_expr, one_shot_at, now, now, next_run_at),
            )
            return int(cursor.lastrowid)

    async def add_async(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        return await asyncio.to_thread(self.add, user_id, label, action, next_run_at, cron_expr, one_shot_at)

    def get(self, user_id: str, schedule_id: int) -> Schedule | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE user_id = ? AND id = ?",
                (user_id, schedule_id),
            ).fetchone()
        return self._row_to_schedule(row) if row else None

    async def get_async(self, user_id: str, schedule_id: int) -> Schedule | None:
        return await asyncio.to_thread(self.get, user_id, schedule_id)

    def get_by_label(self, user_id: str, label: str) -> Schedule | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM schedules
                WHERE user_id = ? AND LOWER(label) = LOWER(?) AND enabled = 1
                """,
                (user_id, label.strip()),
            ).fetchone()
        return self._row_to_schedule(row) if row else None

    async def get_by_label_async(self, user_id: str, label: str) -> Schedule | None:
        return await asyncio.to_thread(self.get_by_label, user_id, label)

    def list_schedules(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        with self._lock:
            if enabled_only:
                rows = self._conn.execute(
                    "SELECT * FROM schedules WHERE user_id = ? AND enabled = 1 ORDER BY next_run_at ASC",
                    (user_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM schedules WHERE user_id = ? ORDER BY next_run_at ASC",
                    (user_id,),
                ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    async def list_schedules_async(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        return await asyncio.to_thread(self.list_schedules, user_id, enabled_only)

    def get_due_schedules(self, user_id: str, now_iso: str) -> list[Schedule]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM schedules
                WHERE user_id = ? AND enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (user_id, now_iso),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    async def get_due_schedules_async(self, user_id: str, now_iso: str) -> list[Schedule]:
        return await asyncio.to_thread(self.get_due_schedules, user_id, now_iso)

    def get_due_schedules_for_all_users(self, now_iso: str) -> list[Schedule]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM schedules
                WHERE enabled = 1 AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    async def get_due_schedules_for_all_users_async(self, now_iso: str) -> list[Schedule]:
        return await asyncio.to_thread(self.get_due_schedules_for_all_users, now_iso)

    def update(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        updates: list[str] = []
        params: list[object] = []
        if label is not None:
            updates.append("label = ?")
            params.append(label.strip())
        if action is not None:
            updates.append("action = ?")
            params.append(action.strip())
        if cron_expr is not None:
            updates.append("cron_expr = ?")
            params.append(cron_expr)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(int(enabled))
        if next_run_at is not None:
            updates.append("next_run_at = ?")
            params.append(next_run_at)
        if not updates:
            return False
        updates.append("updated_at = ?")
        params.append(_utcnow_iso())
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE schedules SET %s WHERE user_id = ? AND id = ?" % ", ".join(updates),  # nosec B608
                [*params, user_id, schedule_id],
            )
            return cursor.rowcount > 0

    async def update_async(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self.update,
            user_id,
            schedule_id,
            label=label,
            action=action,
            cron_expr=cron_expr,
            enabled=enabled,
            next_run_at=next_run_at,
        )

    def record_execution(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        now = _utcnow_iso()
        is_failure = status in ("error", "timeout")
        with self._lock, self._conn:
            if next_run_at is not None:
                failure_expr = "consecutive_failures + 1" if is_failure else "0"
                self._conn.execute(
                    """
                    UPDATE schedules
                    SET last_run_at = ?, last_status = ?, last_error = ?,
                        run_count = run_count + 1, next_run_at = ?,
                        consecutive_failures = %s, updated_at = ?
                    WHERE user_id = ? AND id = ?
                    """ % failure_expr,  # nosec B608
                    (now, status, error, next_run_at, now, user_id, schedule_id),
                )
            else:
                failure_expr = "consecutive_failures + 1" if is_failure else "0"
                self._conn.execute(
                    """
                    UPDATE schedules
                    SET last_run_at = ?, last_status = ?, last_error = ?,
                        run_count = run_count + 1, enabled = false,
                        consecutive_failures = %s, updated_at = ?
                    WHERE user_id = ? AND id = ?
                    """ % failure_expr,  # nosec B608
                    (now, status, error, now, user_id, schedule_id),
                )
            if is_failure and next_run_at is not None:
                row = self._conn.execute(
                    "SELECT consecutive_failures FROM schedules WHERE user_id = ? AND id = ?",
                    (user_id, schedule_id),
                ).fetchone()
                if row and row["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                    self._conn.execute(
                        "UPDATE schedules SET enabled = 0, updated_at = ? WHERE user_id = ? AND id = ?",
                        (now, user_id, schedule_id),
                    )
                    logger.warning(
                        "Schedule id=%d auto-disabled after %d consecutive failures (circuit breaker).",
                        schedule_id,
                        row["consecutive_failures"],
                    )

    async def record_execution_async(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        await asyncio.to_thread(self.record_execution, user_id, schedule_id, status, next_run_at, error)

    def reset_consecutive_failures(self, user_id: str, schedule_id: int) -> bool:
        now = _utcnow_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE schedules SET consecutive_failures = 0, updated_at = ? WHERE user_id = ? AND id = ?",
                (now, user_id, schedule_id),
            )
            return cursor.rowcount > 0

    async def reset_consecutive_failures_async(self, user_id: str, schedule_id: int) -> bool:
        return await asyncio.to_thread(self.reset_consecutive_failures, user_id, schedule_id)

    def delete(self, user_id: str, schedule_id: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM schedules WHERE user_id = ? AND id = ?",
                (user_id, schedule_id),
            )
            return cursor.rowcount > 0

    async def delete_async(self, user_id: str, schedule_id: int) -> bool:
        return await asyncio.to_thread(self.delete, user_id, schedule_id)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("WAL checkpoint failed on ScheduleStore close, connection may already be closed")
            try:
                self._conn.close()
            except Exception as exc:
                logger.debug("ScheduleStore close() error: %s", exc)

    def count_enabled(self, user_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM schedules WHERE user_id = ? AND enabled = 1",
                (user_id,),
            ).fetchone()
        return int(row["cnt"] if row else 0)

    async def count_enabled_async(self, user_id: str) -> int:
        return await asyncio.to_thread(self.count_enabled, user_id)

    def count_enabled_all(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) as cnt FROM schedules WHERE enabled = 1").fetchone()
        return int(row["cnt"] if row else 0)

    async def count_enabled_all_async(self) -> int:
        return await asyncio.to_thread(self.count_enabled_all)


class PostgresScheduleBackend:
    """PostgreSQL implementation of scheduled action persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresScheduleBackend requires a PostgreSQL database URL")
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
            await self._assert_schema(conn)
        self._initialized = True
        logger.info("ScheduleStore initialized (PostgreSQL)")

    @staticmethod
    async def _assert_schema(conn: asyncpg.Connection) -> None:
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = $1
            """,
            _PG_TABLE,
        )
        actual = {str(row["column_name"]) for row in rows}
        missing = sorted(_REQUIRED_PG_COLUMNS - actual)
        if missing:
            raise RuntimeError(
                "ScheduleStore PostgreSQL schema mismatch for %s; missing migration-owned columns: %s"
                % (_PG_TABLE, ", ".join(missing))
            )

    def _run(self, coro: Coroutine[Any, Any, _T], *, operation: str) -> _T:
        result = run_async_synchronously(
            coro,
            timeout=_SYNC_TIMEOUT_SECONDS,
            timeout_result=_TIMEOUT,
            timeout_log_message="ScheduleStore PostgreSQL %s timed out after %%.1fs." % operation,
            logger=logger,
        )
        if result is _TIMEOUT:
            raise TimeoutError("ScheduleStore PostgreSQL %s timed out after %.1fs" % (operation, _SYNC_TIMEOUT_SECONDS))
        return cast(_T, result)

    @staticmethod
    def _row_to_schedule(row: asyncpg.Record) -> Schedule:
        return Schedule(
            id=row["id"],
            user_id=row["user_id"],
            label=row["label"],
            action=row["action"],
            cron_expr=row["cron_expr"],
            one_shot_at=str(row["one_shot_at"]) if row["one_shot_at"] else None,
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_run_at=str(row["last_run_at"]) if row["last_run_at"] else None,
            next_run_at=str(row["next_run_at"]),
            run_count=row["run_count"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            consecutive_failures=row["consecutive_failures"],
        )

    @contextlib.asynccontextmanager
    async def _user_conn(self, user_id: str, *, require_consent: bool = False) -> Any:
        pg_user_id = str(_pg_uuid(user_id))
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            from services.sync.middleware_helpers import set_rls_context

            await set_rls_context(conn, pg_user_id)
            if require_consent:
                await self._require_cloud_sync_consent(conn, pg_user_id)
            yield conn

    @contextlib.asynccontextmanager
    async def _scheduler_worker_conn(self) -> Any:
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.scheduler_worker', 'on', true)")
            yield conn

    @staticmethod
    async def _require_cloud_sync_consent(conn: Any, user_id: str) -> None:
        from services.sync.consent import (
            ConsentRequiredError,
            has_cloud_sync_consent_locked,
        )

        if not await has_cloud_sync_consent_locked(conn, user_id):
            raise ConsentRequiredError()

    @staticmethod
    async def _count_enabled_with_conn(conn: Any, user_id: str) -> int:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM sync_schedules WHERE user_id = $1 AND enabled IS TRUE AND deleted_at IS NULL",
            _pg_uuid(user_id),
        )
        return int(row["cnt"] if row else 0)

    def add(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        return self._run(
            self.add_async(user_id, label, action, next_run_at, cron_expr, one_shot_at),
            operation="add",
        )

    async def add_async(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        label, action = _validate_schedule_inputs(label, action, cron_expr, one_shot_at)
        await self.initialize()
        now = _utcnow()
        async with self._user_conn(user_id, require_consent=True) as conn:
            enabled_count = await self._count_enabled_with_conn(conn, user_id)
            if enabled_count >= MAX_ENABLED_SCHEDULES:
                raise ValueError("Maximum number of enabled schedules (%d) reached." % MAX_ENABLED_SCHEDULES)
            row = await conn.fetchrow(
                """
                INSERT INTO sync_schedules
                    (user_id, label, action, cron_expr, one_shot_at, enabled,
                     created_at, updated_at, next_run_at, run_count,
                     consent_generation, version_vector, field_versions, lww_hlc, lww_actor_id,
                     updated_by_device_id, last_mutation_id)
                VALUES ($1, $2, $3, $4, $5, true, $6, $7, $8, 0,
                        0, '{}'::jsonb, '{}'::jsonb, $9, $10, $11, $12)
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                _pg_uuid(user_id),
                label,
                action,
                cron_expr,
                _pg_datetime(one_shot_at),
                now,
                now,
                _pg_datetime(next_run_at, required=True),
                _sync_hlc(now),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                _mutation_id(),
            )
            await _write_schedule_journal(conn, row, "upsert")
        return int(row["id"])

    def get(self, user_id: str, schedule_id: int) -> Schedule | None:
        return self._run(self.get_async(user_id, schedule_id), operation="get")

    async def get_async(self, user_id: str, schedule_id: int) -> Schedule | None:
        await self.initialize()
        async with self._user_conn(user_id) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sync_schedules WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL",
                _pg_uuid(user_id),
                schedule_id,
            )
        return self._row_to_schedule(row) if row else None

    def get_by_label(self, user_id: str, label: str) -> Schedule | None:
        return self._run(self.get_by_label_async(user_id, label), operation="get_by_label")

    async def get_by_label_async(self, user_id: str, label: str) -> Schedule | None:
        await self.initialize()
        async with self._user_conn(user_id) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sync_schedules WHERE user_id = $1 AND LOWER(label) = LOWER($2) AND enabled IS TRUE AND deleted_at IS NULL",
                _pg_uuid(user_id),
                label.strip(),
            )
        return self._row_to_schedule(row) if row else None

    def list_schedules(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        return self._run(self.list_schedules_async(user_id, enabled_only), operation="list_schedules")

    async def list_schedules_async(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        await self.initialize()
        async with self._user_conn(user_id) as conn:
            if enabled_only:
                rows = await conn.fetch(
                    "SELECT * FROM sync_schedules WHERE user_id = $1 AND enabled IS TRUE AND deleted_at IS NULL ORDER BY next_run_at ASC",
                    _pg_uuid(user_id),
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM sync_schedules WHERE user_id = $1 AND deleted_at IS NULL ORDER BY next_run_at ASC",
                    _pg_uuid(user_id),
                )
        return [self._row_to_schedule(row) for row in rows]

    def get_due_schedules(self, user_id: str, now_iso: str) -> list[Schedule]:
        return self._run(
            self.get_due_schedules_async(user_id, now_iso),
            operation="get_due_schedules",
        )

    async def get_due_schedules_async(self, user_id: str, now_iso: str) -> list[Schedule]:
        await self.initialize()
        async with self._user_conn(user_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM sync_schedules WHERE user_id = $1 AND enabled IS TRUE AND deleted_at IS NULL AND next_run_at <= $2 ORDER BY next_run_at ASC",
                _pg_uuid(user_id),
                _pg_datetime(now_iso, required=True),
            )
        return [self._row_to_schedule(row) for row in rows]

    def get_due_schedules_for_all_users(self, now_iso: str) -> list[Schedule]:
        return self._run(
            self.get_due_schedules_for_all_users_async(now_iso),
            operation="get_due_schedules_for_all_users",
        )

    async def get_due_schedules_for_all_users_async(self, now_iso: str) -> list[Schedule]:
        await self.initialize()
        async with self._scheduler_worker_conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM sync_schedules WHERE enabled IS TRUE AND deleted_at IS NULL AND next_run_at <= $1 ORDER BY next_run_at ASC",
                _pg_datetime(now_iso, required=True),
            )
        return [self._row_to_schedule(row) for row in rows]

    def update(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        return self._run(
            self.update_async(
                user_id,
                schedule_id,
                label=label,
                action=action,
                cron_expr=cron_expr,
                enabled=enabled,
                next_run_at=next_run_at,
            ),
            operation="update",
        )

    async def update_async(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        await self.initialize()
        updates: list[str] = []
        params: list[object] = []
        idx = 1
        if label is not None:
            updates.append("label = $%d" % idx)
            params.append(label.strip())
            idx += 1
        if action is not None:
            updates.append("action = $%d" % idx)
            params.append(action.strip())
            idx += 1
        if cron_expr is not None:
            updates.append("cron_expr = $%d" % idx)
            params.append(cron_expr)
            idx += 1
        if enabled is not None:
            updates.append("enabled = $%d" % idx)
            params.append(_pg_bool(enabled))
            idx += 1
        if next_run_at is not None:
            updates.append("next_run_at = $%d" % idx)
            params.append(_pg_datetime(next_run_at, required=True))
            idx += 1
        if not updates:
            return False
        updates.append("updated_at = $%d" % idx)
        now = _utcnow()
        params.append(now)
        idx += 1
        updates.append("lww_hlc = $%d" % idx)
        params.append(_sync_hlc(now))
        idx += 1
        updates.append("updated_by_device_id = $%d" % idx)
        params.append(_SYNC_DEVICE_ID)
        idx += 1
        updates.append("last_mutation_id = $%d" % idx)
        params.append(_mutation_id())
        idx += 1
        updates.append("commit_seq = nextval('sync_commit_seq')")
        params.extend([_pg_uuid(user_id), schedule_id])
        async with self._user_conn(user_id, require_consent=True) as conn:
            row = await conn.fetchrow(
                (
                    "UPDATE sync_schedules SET %s WHERE user_id = $%d AND id = $%d AND deleted_at IS NULL "
                    "RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id"
                )
                % (", ".join(updates), idx, idx + 1),  # nosec B608
                *params,
            )
            if row is None:
                return False
            await _write_schedule_journal(conn, row, "upsert")
            return True

    def record_execution(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        self._run(
            self.record_execution_async(user_id, schedule_id, status, next_run_at, error),
            operation="record_execution",
        )

    async def record_execution_async(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        await self.initialize()
        now = _utcnow()
        is_failure = status in ("error", "timeout")
        async with self._user_conn(user_id, require_consent=True) as conn:
            if next_run_at is not None:
                failure_expr = "consecutive_failures + 1" if is_failure else "0"
                row = await conn.fetchrow(
                    """
                    UPDATE sync_schedules
                    SET last_run_at = $1, last_status = $2, last_error = $3,
                        run_count = run_count + 1, next_run_at = $4,
                        consecutive_failures = %s, updated_at = $5,
                        lww_hlc = $6, updated_by_device_id = $7, last_mutation_id = $8,
                        commit_seq = nextval('sync_commit_seq')
                    WHERE user_id = $9 AND id = $10 AND deleted_at IS NULL
                    RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                    """ % failure_expr,  # nosec B608
                    now,
                    status,
                    error,
                    _pg_datetime(next_run_at, required=True),
                    now,
                    _sync_hlc(now),
                    _SYNC_DEVICE_ID,
                    _mutation_id(),
                    _pg_uuid(user_id),
                    schedule_id,
                )
                if row is not None:
                    await _write_schedule_journal(conn, row, "upsert")
            else:
                failure_expr = "consecutive_failures + 1" if is_failure else "0"
                row = await conn.fetchrow(
                    """
                    UPDATE sync_schedules
                    SET last_run_at = $1, last_status = $2, last_error = $3,
                        run_count = run_count + 1, enabled = false,
                        consecutive_failures = %s, updated_at = $4,
                        lww_hlc = $5, updated_by_device_id = $6, last_mutation_id = $7,
                        commit_seq = nextval('sync_commit_seq')
                    WHERE user_id = $8 AND id = $9 AND deleted_at IS NULL
                    RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                    """ % failure_expr,  # nosec B608
                    now,
                    status,
                    error,
                    now,
                    _sync_hlc(now),
                    _SYNC_DEVICE_ID,
                    _mutation_id(),
                    _pg_uuid(user_id),
                    schedule_id,
                )
                if row is not None:
                    await _write_schedule_journal(conn, row, "upsert")
            if is_failure and next_run_at is not None:
                row = await conn.fetchrow(
                    "SELECT consecutive_failures FROM sync_schedules WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL",
                    _pg_uuid(user_id),
                    schedule_id,
                )
                if row and row["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
                    disabled_row = await conn.fetchrow(
                        """
                        UPDATE sync_schedules
                        SET enabled = false, updated_at = $1, lww_hlc = $2,
                            updated_by_device_id = $3, last_mutation_id = $4,
                            commit_seq = nextval('sync_commit_seq')
                        WHERE user_id = $5 AND id = $6 AND deleted_at IS NULL
                        RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                        """,
                        now,
                        _sync_hlc(now),
                        _SYNC_DEVICE_ID,
                        _mutation_id(),
                        _pg_uuid(user_id),
                        schedule_id,
                    )
                    if disabled_row is not None:
                        await _write_schedule_journal(conn, disabled_row, "upsert")
                    logger.warning(
                        "Schedule id=%d auto-disabled after %d consecutive failures (circuit breaker).",
                        schedule_id,
                        row["consecutive_failures"],
                    )

    def reset_consecutive_failures(self, user_id: str, schedule_id: int) -> bool:
        return self._run(
            self.reset_consecutive_failures_async(user_id, schedule_id),
            operation="reset_consecutive_failures",
        )

    async def reset_consecutive_failures_async(self, user_id: str, schedule_id: int) -> bool:
        await self.initialize()
        now = _utcnow()
        async with self._user_conn(user_id, require_consent=True) as conn:
            row = await conn.fetchrow(
                """
                UPDATE sync_schedules
                SET consecutive_failures = 0, updated_at = $1, lww_hlc = $2,
                    updated_by_device_id = $3, last_mutation_id = $4,
                    commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $5 AND id = $6 AND deleted_at IS NULL
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                now,
                _sync_hlc(now),
                _SYNC_DEVICE_ID,
                _mutation_id(),
                _pg_uuid(user_id),
                schedule_id,
            )
            if row is None:
                return False
            await _write_schedule_journal(conn, row, "upsert")
            return True

    def delete(self, user_id: str, schedule_id: int) -> bool:
        return self._run(self.delete_async(user_id, schedule_id), operation="delete")

    async def delete_async(self, user_id: str, schedule_id: int) -> bool:
        await self.initialize()
        now = _utcnow()
        async with self._user_conn(user_id, require_consent=True) as conn:
            row = await conn.fetchrow(
                """
                UPDATE sync_schedules
                SET deleted_at = $1, updated_at = $1, lww_hlc = $2,
                    updated_by_device_id = $3, last_mutation_id = $4,
                    commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $5 AND id = $6 AND deleted_at IS NULL
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                now,
                _sync_hlc(now),
                _SYNC_DEVICE_ID,
                _mutation_id(),
                _pg_uuid(user_id),
                schedule_id,
            )
            if row is None:
                return False
            await _write_schedule_journal(conn, row, "delete")
            return True

    def close(self) -> None:
        return

    def count_enabled(self, user_id: str) -> int:
        return self._run(self.count_enabled_async(user_id), operation="count_enabled")

    async def count_enabled_async(self, user_id: str) -> int:
        await self.initialize()
        async with self._user_conn(user_id) as conn:
            return await self._count_enabled_with_conn(conn, user_id)

    def count_enabled_all(self) -> int:
        return self._run(self.count_enabled_all_async(), operation="count_enabled_all")

    async def count_enabled_all_async(self) -> int:
        await self.initialize()
        async with self._scheduler_worker_conn() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM sync_schedules WHERE enabled IS TRUE AND deleted_at IS NULL"
            )
        return int(row["cnt"] if row else 0)


def create_schedule_backend(
    *,
    root: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> ScheduleBackend:
    """Create a schedule backend selected by app surface."""
    pg_url = postgres_url_for_surface("ScheduleStore", app_surface=app_surface, database_url=database_url)
    if pg_url is None:
        return SqliteScheduleBackend(root=root)
    return PostgresScheduleBackend(pg_url)


class ScheduleStore:
    """Backend-agnostic facade for scheduled actions."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        backend: ScheduleBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend = backend or create_schedule_backend(
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

    def self_check(self) -> None:
        """Verify the schedule store backend is usable.

        F-018: the prior implementation inserted a row with a literal
        sentinel ``user_id="__schedule_store_self_check__"``. On the
        Postgres backend that string is coerced through ``uuid.UUID(...)``
        and raises ``ValueError`` before the insert ever runs; even when
        a valid UUID is supplied, the FK to ``app_user_profiles(user_id)``
        rejects any unenrolled principal. Either way the self-check
        could fail before scheduler startup.

        The backend-specific shape now:

        * Postgres: rely on the schema check that ``initialize()`` runs
          (``_assert_schema``) and do not mutate ``sync_schedules`` from a
          singleton constructor. We just trigger initialize() so any
          schema drift surfaces here.
        * sqlite: perform the legacy round-trip with the string sentinel
          so a genuinely broken sqlite file is caught at startup.
        """
        if getattr(self._backend, "is_postgres", False):
            from core.asyncio_safe import run_async_synchronously

            run_async_synchronously(
                self._backend.initialize(),
                timeout=_SYNC_TIMEOUT_SECONDS,
                timeout_result=_TIMEOUT,
                timeout_log_message="ScheduleStore Postgres self_check init timed out after %.1fs.",
                logger=logger,
            )
            return

        user_id = "__schedule_store_self_check__"
        schedule_id: int | None = None
        try:
            schedule_id = self.add(
                user_id=user_id,
                label="self-check",
                action="noop",
                next_run_at=_utcnow_iso(),
                one_shot_at=_utcnow_iso(),
            )
            if self.get(user_id, schedule_id) is None:
                raise RuntimeError("ScheduleStore self_check failed to reload inserted schedule")
            self.count_enabled(user_id)
        finally:
            if schedule_id is not None:
                try:
                    self.delete(user_id, schedule_id)
                except Exception as exc:
                    logger.warning("ScheduleStore self_check cleanup failed: %s", exc)

    def add(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        return self._backend.add(user_id, label, action, next_run_at, cron_expr, one_shot_at)

    async def add_async(
        self,
        user_id: str,
        label: str,
        action: str,
        next_run_at: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        return await self._backend.add_async(user_id, label, action, next_run_at, cron_expr, one_shot_at)

    def get(self, user_id: str, schedule_id: int) -> Schedule | None:
        return self._backend.get(user_id, schedule_id)

    async def get_async(self, user_id: str, schedule_id: int) -> Schedule | None:
        return await self._backend.get_async(user_id, schedule_id)

    def get_by_label(self, user_id: str, label: str) -> Schedule | None:
        return self._backend.get_by_label(user_id, label)

    async def get_by_label_async(self, user_id: str, label: str) -> Schedule | None:
        return await self._backend.get_by_label_async(user_id, label)

    def list_schedules(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        return self._backend.list_schedules(user_id, enabled_only)

    async def list_schedules_async(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        return await self._backend.list_schedules_async(user_id, enabled_only)

    def get_due_schedules(self, user_id: str, now_iso: str) -> list[Schedule]:
        return self._backend.get_due_schedules(user_id, now_iso)

    async def get_due_schedules_async(self, user_id: str, now_iso: str) -> list[Schedule]:
        return await self._backend.get_due_schedules_async(user_id, now_iso)

    def get_due_schedules_for_all_users(self, now_iso: str) -> list[Schedule]:
        return self._backend.get_due_schedules_for_all_users(now_iso)

    async def get_due_schedules_for_all_users_async(self, now_iso: str) -> list[Schedule]:
        return await self._backend.get_due_schedules_for_all_users_async(now_iso)

    def update(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        return self._backend.update(
            user_id,
            schedule_id,
            label=label,
            action=action,
            cron_expr=cron_expr,
            enabled=enabled,
            next_run_at=next_run_at,
        )

    async def update_async(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
        next_run_at: str | None = None,
    ) -> bool:
        return await self._backend.update_async(
            user_id,
            schedule_id,
            label=label,
            action=action,
            cron_expr=cron_expr,
            enabled=enabled,
            next_run_at=next_run_at,
        )

    def record_execution(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        self._backend.record_execution(user_id, schedule_id, status, next_run_at, error)

    async def record_execution_async(
        self,
        user_id: str,
        schedule_id: int,
        status: str,
        next_run_at: str | None,
        error: str | None = None,
    ) -> None:
        await self._backend.record_execution_async(user_id, schedule_id, status, next_run_at, error)

    def reset_consecutive_failures(self, user_id: str, schedule_id: int) -> bool:
        return self._backend.reset_consecutive_failures(user_id, schedule_id)

    async def reset_consecutive_failures_async(self, user_id: str, schedule_id: int) -> bool:
        return await self._backend.reset_consecutive_failures_async(user_id, schedule_id)

    def delete(self, user_id: str, schedule_id: int) -> bool:
        return self._backend.delete(user_id, schedule_id)

    async def delete_async(self, user_id: str, schedule_id: int) -> bool:
        return await self._backend.delete_async(user_id, schedule_id)

    def close(self) -> None:
        self._backend.close()

    def count_enabled(self, user_id: str) -> int:
        return self._backend.count_enabled(user_id)

    async def count_enabled_async(self, user_id: str) -> int:
        return await self._backend.count_enabled_async(user_id)

    def count_enabled_all(self) -> int:
        return self._backend.count_enabled_all()

    async def count_enabled_all_async(self) -> int:
        return await self._backend.count_enabled_all_async()


def get_schedule_store(root: Path | None = None) -> ScheduleStore:
    """Return the process-wide ScheduleStore singleton."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        with _STORE_LOCK:
            if _STORE_SINGLETON is None:
                _STORE_SINGLETON = ScheduleStore(root=root)
                _STORE_SINGLETON.self_check()
    return _STORE_SINGLETON


def reset_schedule_store_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _STORE_SINGLETON
    with _STORE_LOCK:
        if _STORE_SINGLETON is not None:
            _STORE_SINGLETON.close()
        _STORE_SINGLETON = None


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_ENABLED_SCHEDULES",
    "PostgresScheduleBackend",
    "Schedule",
    "ScheduleBackend",
    "ScheduleStore",
    "SqliteScheduleBackend",
    "create_schedule_backend",
    "get_schedule_store",
    "reset_schedule_store_for_tests",
]
