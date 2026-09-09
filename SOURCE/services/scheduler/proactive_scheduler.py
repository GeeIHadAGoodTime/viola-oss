"""Proactive Scheduler -- cron-based automation with heartbeat and active hours.

Extends the existing SchedulerService with:
- Proactive task storage in SQLite/PostgreSQL (`proactive_tasks` table)
- Active hours enforcement (default 7am-11pm)
- Missed schedule recovery (run missed tasks on startup, max 1 per task)
- Heartbeat: special 30-minute task for checking changes
- Result delivery to messaging channels

This module complements the existing ``services/scheduler/service.py`` by
adding higher-level scheduling features while reusing the same scheduler
infrastructure for execution.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from croniter import croniter

from core.logging_config import get_logger
from services.persistence.state_store import STATE_DB_SCHEMA_LOCK
from services.sync.journal import write_journal

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_LOCK = threading.Lock()
_SINGLETON: ProactiveScheduler | None = None

_CHECK_INTERVAL = 60  # seconds between checks
_HEARTBEAT_INTERVAL = 1800  # 30 minutes
_MAX_MISSED_RECOVERY = 1  # max missed executions to recover per task
_DEFAULT_ACTIVE_HOURS_START = 7  # 7 AM
_DEFAULT_ACTIVE_HOURS_END = 23  # 11 PM

_DB_FILENAME = "state.sqlite3"

_PG_REQUIRED_RELATIONS = ("public.sync_proactive_tasks",)
_SYNC_ACTOR_ID = "proactive-scheduler"
_SYNC_DEVICE_ID = "cloud"

_EXECUTION_STATUS_OK = "ok"
_EXECUTION_STATUS_TIMEOUT = "timeout"
_EXECUTION_STATUS_ERROR = "error"
_MAX_LAST_ERROR_CHARS = 1000


def _sync_hlc(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


async def _write_proactive_task_journal(conn: asyncpg.Connection, row: Any, op: str) -> None:
    await write_journal(
        conn,
        str(row["user_id"]),
        "proactive_tasks",
        str(row["id"]),
        int(row["commit_seq"]),
        "delete" if op == "delete" else "upsert",
        str(row["last_mutation_id"]),
    )


def _utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ProactiveTask:
    """A proactive scheduled task record."""

    id: int
    user_id: str
    name: str
    cron_expr: str
    timezone: str
    task_prompt: str
    enabled: bool
    last_run: str | None
    next_run: str
    created_at: str
    delivery_channel: str | None
    active_hours_start: int
    active_hours_end: int
    last_status: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ProactiveTaskExecutionResult:
    """Structured result for one proactive task attempt."""

    status: str
    message: str | None = None
    error: str | None = None
    advance_next_run: bool = True

    @property
    def succeeded(self) -> bool:
        return self.status == _EXECUTION_STATUS_OK


class ProactiveTaskStore:
    """SQLite/PostgreSQL-backed storage for proactive scheduled tasks.

    Uses a separate table from the existing schedules table to avoid
    coupling with the base scheduler's schema.
    """

    def __init__(self, root: Path | None = None) -> None:
        from core.db_backend import get_database_url

        self._pg_url = get_database_url()
        self._use_pg = self._pg_url is not None

        if root is not None:
            base_path = Path(root)
        else:
            try:
                from config.settings import settings as _cfg

                base_path = Path(_cfg.data_dir)
            except Exception:
                base_path = Path.cwd()

        self._lock = threading.RLock()
        if not self._use_pg:
            self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_schema()
        else:
            self._db_path = None  # type: ignore[assignment]
            self._conn = None  # type: ignore[assignment]
            self._pg_initialized = False

    async def _pg_pool(self) -> asyncpg.Pool:
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    async def pg_initialize(self) -> None:
        """Initialize PostgreSQL schema. Must be called once before use in PG mode."""
        if not self._use_pg or self._pg_initialized:
            return

        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="ProactiveTaskStore")
        self._pg_initialized = True
        logger.info("ProactiveTaskStore initialized (PostgreSQL)")

    async def _require_cloud_sync_consent_for_read(self, conn: asyncpg.Connection, user_id: str) -> None:
        from services.sync.consent import ConsentRequiredError, has_cloud_sync_consent

        if not await has_cloud_sync_consent(conn, user_id):  # consent-read-only-ok
            raise ConsentRequiredError()

    async def _require_cloud_sync_consent_for_write(self, conn: asyncpg.Connection, user_id: str) -> None:
        from services.sync.consent import (
            ConsentRequiredError,
            has_cloud_sync_consent_locked,
        )

        if not await has_cloud_sync_consent_locked(conn, user_id):
            raise ConsentRequiredError()

    def _ensure_schema(self) -> None:
        """Create the proactive_tasks table if absent."""
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_tasks (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id             TEXT NOT NULL,
                    name                TEXT NOT NULL,
                    cron_expr           TEXT NOT NULL,
                    timezone            TEXT NOT NULL DEFAULT 'UTC',
                    task_prompt         TEXT NOT NULL,
                    enabled             INTEGER NOT NULL DEFAULT 1,
                    last_run            TEXT,
                    next_run            TEXT NOT NULL,
                    created_at          TEXT NOT NULL,
                    delivery_channel    TEXT,
                    active_hours_start  INTEGER NOT NULL DEFAULT 7,
                    active_hours_end    INTEGER NOT NULL DEFAULT 23,
                    last_status         TEXT,
                    last_error          TEXT
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_proactive_next_run
                ON proactive_tasks(next_run) WHERE enabled = 1
                """)
            if "user_id" not in self._table_columns("proactive_tasks"):
                # Assign existing tasks to the device user so desktop upgrades work
                try:
                    from core.user_context import get_device_user_id

                    # mt-ok: schema migration assigns pre-multi-user rows to
                    # the single desktop owner; new rows carry real user_id.
                    _fallback = get_device_user_id()
                except Exception:
                    _fallback = ""
                self._conn.execute(
                    "ALTER TABLE proactive_tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT '%s'"
                    % _fallback.replace("'", "''")
                )
            columns = set(self._table_columns("proactive_tasks"))
            if "last_status" not in columns:
                self._conn.execute("ALTER TABLE proactive_tasks ADD COLUMN last_status TEXT")
            if "last_error" not in columns:
                self._conn.execute("ALTER TABLE proactive_tasks ADD COLUMN last_error TEXT")
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_proactive_user_next_run
                ON proactive_tasks(user_id, next_run)
                """)
        logger.debug("ProactiveTaskStore schema ensured at %s", self._db_path)

    def _table_columns(self, table_name: str) -> list[str]:
        rows = self._conn.execute("PRAGMA table_info(%s)" % table_name).fetchall()  # nosec B608
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        resolved_user_id = user_id.strip()
        if not resolved_user_id:
            raise ValueError("user_id is required for multi-user data isolation")
        return resolved_user_id

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> ProactiveTask:
        return ProactiveTask(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            cron_expr=row["cron_expr"],
            timezone=row["timezone"],
            task_prompt=row["task_prompt"],
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
            next_run=row["next_run"],
            created_at=row["created_at"],
            delivery_channel=row["delivery_channel"],
            active_hours_start=row["active_hours_start"],
            active_hours_end=row["active_hours_end"],
            last_status=row["last_status"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _pg_row_to_task(row: asyncpg.Record) -> ProactiveTask:
        return ProactiveTask(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            cron_expr=row["cron_expr"],
            timezone=row["timezone"],
            task_prompt=row["task_prompt"],
            enabled=bool(row["enabled"]),
            last_run=str(row["last_run_at"]) if row["last_run_at"] else None,
            next_run=str(row["next_run_at"]),
            created_at=str(row["created_at"]),
            delivery_channel=row["delivery_channel"],
            active_hours_start=row["active_hours_start"],
            active_hours_end=row["active_hours_end"],
            last_status=row["last_status"],
            last_error=row["last_error"],
        )

    # ------------------------------------------------------------------ CRUD

    def add(
        self,
        user_id: str,
        name: str,
        cron_expr: str,
        task_prompt: str,
        timezone: str = "UTC",
        delivery_channel: str | None = None,
        active_hours_start: int = _DEFAULT_ACTIVE_HOURS_START,
        active_hours_end: int = _DEFAULT_ACTIVE_HOURS_END,
    ) -> int:
        """Create a new proactive task. Returns the task ID."""
        if self._use_pg:
            raise RuntimeError("Use add_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        name_value = name.strip()
        task_prompt_value = task_prompt.strip()
        timezone_value = timezone.strip() or "UTC"
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        try:
            cron = croniter(cron_expr, datetime.now(UTC))
            next_run = cron.get_next(datetime).isoformat()
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid cron expression %r: %s" % (cron_expr, exc)) from exc

        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO proactive_tasks
                    (user_id, name, cron_expr, timezone, task_prompt, enabled,
                      next_run, created_at, delivery_channel,
                      active_hours_start, active_hours_end)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_user_id,
                    name_value,
                    cron_expr,
                    timezone_value,
                    task_prompt_value,
                    next_run,
                    now,
                    delivery_channel,
                    active_hours_start,
                    active_hours_end,
                ),
            )
            task_id = cursor.lastrowid

        logger.info(
            "Proactive task created id=%d user=%s name=%r cron=%s next=%s",
            task_id,
            resolved_user_id,
            name,
            cron_expr,
            next_run,
        )
        return task_id

    async def add_async(
        self,
        user_id: str,
        name: str,
        cron_expr: str,
        task_prompt: str,
        timezone: str = "UTC",
        delivery_channel: str | None = None,
        active_hours_start: int = _DEFAULT_ACTIVE_HOURS_START,
        active_hours_end: int = _DEFAULT_ACTIVE_HOURS_END,
    ) -> int:
        """Create a new proactive task (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(
                self.add,
                user_id,
                name,
                cron_expr,
                task_prompt,
                timezone,
                delivery_channel,
                active_hours_start,
                active_hours_end,
            )

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        name_value = name.strip()
        task_prompt_value = task_prompt.strip()
        timezone_value = timezone.strip() or "UTC"
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        try:
            cron = croniter(cron_expr, now_dt)
            next_run_dt = cron.get_next(datetime)
            next_run = next_run_dt.isoformat()
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid cron expression %r: %s" % (cron_expr, exc)) from exc

        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            await self._require_cloud_sync_consent_for_write(conn, resolved_user_id)
            row = await conn.fetchrow(
                """
                INSERT INTO sync_proactive_tasks
                    (user_id, name, cron_expr, timezone, task_prompt, enabled,
                     next_run_at, created_at, delivery_channel, active_hours_start, active_hours_end,
                     consent_generation, version_vector, field_versions, lww_hlc, lww_actor_id,
                     updated_by_device_id, last_mutation_id, updated_at, deleted_at)
                VALUES ($1, $2, $3, $4, $5, TRUE, $6, $7, $8, $9, $10,
                        0, '{}'::jsonb, '{}'::jsonb, $11, $12, $13, $14, $7, NULL)
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                resolved_user_id,
                name_value,
                cron_expr,
                timezone_value,
                task_prompt_value,
                _utc_datetime(next_run_dt),
                now_dt,
                delivery_channel,
                active_hours_start,
                active_hours_end,
                now,
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
            )
            await _write_proactive_task_journal(conn, row, "upsert")
        task_id = row["id"]
        logger.info(
            "Proactive task created id=%d user=%s name=%r cron=%s next=%s",
            task_id,
            resolved_user_id,
            name,
            cron_expr,
            next_run,
        )
        return task_id

    def list_tasks(self, user_id: str, enabled_only: bool = True) -> list[ProactiveTask]:
        """List all proactive tasks."""
        if self._use_pg:
            raise RuntimeError("Use list_tasks_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        with self._lock:
            if enabled_only:
                rows = self._conn.execute(
                    """
                    SELECT *
                    FROM proactive_tasks
                    WHERE user_id = ? AND enabled = 1
                    ORDER BY next_run ASC
                    """,
                    (resolved_user_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT *
                    FROM proactive_tasks
                    WHERE user_id = ?
                    ORDER BY next_run ASC
                    """,
                    (resolved_user_id,),
                ).fetchall()
        return [self._row_to_task(row) for row in rows]

    async def list_tasks_async(self, user_id: str, enabled_only: bool = True) -> list[ProactiveTask]:
        """List all proactive tasks (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.list_tasks, user_id, enabled_only)

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from services.sync.consent import ConsentRequiredError

            try:
                await self._require_cloud_sync_consent_for_read(conn, resolved_user_id)
            except ConsentRequiredError:
                return []
            if enabled_only:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM sync_proactive_tasks
                    WHERE user_id = $1 AND enabled = TRUE AND deleted_at IS NULL
                    ORDER BY next_run_at ASC
                    """,
                    resolved_user_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM sync_proactive_tasks
                    WHERE user_id = $1 AND deleted_at IS NULL
                    ORDER BY next_run_at ASC
                    """,
                    resolved_user_id,
                )
        return [self._pg_row_to_task(row) for row in rows]

    def get(self, user_id: str, task_id: int) -> ProactiveTask | None:
        """Get a task by ID."""
        if self._use_pg:
            raise RuntimeError("Use get_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proactive_tasks WHERE user_id = ? AND id = ?",
                (resolved_user_id, task_id),
            ).fetchone()
        return self._row_to_task(row) if row else None

    async def get_async(self, user_id: str, task_id: int) -> ProactiveTask | None:
        """Get a task by ID (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.get, user_id, task_id)

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from services.sync.consent import ConsentRequiredError

            try:
                await self._require_cloud_sync_consent_for_read(conn, resolved_user_id)
            except ConsentRequiredError:
                return None
            row = await conn.fetchrow(
                "SELECT * FROM sync_proactive_tasks WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL",
                resolved_user_id,
                task_id,
            )
        return self._pg_row_to_task(row) if row else None

    def delete(self, user_id: str, task_id: int) -> bool:
        """Delete a task. Returns True if deleted."""
        if self._use_pg:
            raise RuntimeError("Use delete_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM proactive_tasks WHERE user_id = ? AND id = ?",
                (resolved_user_id, task_id),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Proactive task deleted user=%s id=%d", resolved_user_id, task_id)
        return deleted

    async def delete_async(self, user_id: str, task_id: int) -> bool:
        """Delete a task (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.delete, user_id, task_id)

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        pool = await self._pg_pool()
        now = datetime.now(UTC)
        async with pool.acquire() as conn, conn.transaction():
            await self._require_cloud_sync_consent_for_write(conn, resolved_user_id)
            row = await conn.fetchrow(
                """
                UPDATE sync_proactive_tasks
                SET deleted_at = $1, updated_at = $1, lww_hlc = $2,
                    lww_actor_id = $3, updated_by_device_id = $4,
                    last_mutation_id = $5, commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $6 AND id = $7 AND deleted_at IS NULL
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                now,
                _sync_hlc(now),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
                resolved_user_id,
                task_id,
            )
            deleted = row is not None
            if deleted:
                await _write_proactive_task_journal(conn, row, "delete")
        if deleted:
            logger.info("Proactive task deleted user=%s id=%d", resolved_user_id, task_id)
        return deleted

    def update_enabled(self, user_id: str, task_id: int, enabled: bool) -> bool:
        """Enable or disable a task."""
        if self._use_pg:
            raise RuntimeError("Use update_enabled_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE proactive_tasks SET enabled = ? WHERE user_id = ? AND id = ?",
                (int(enabled), resolved_user_id, task_id),
            )
            return cursor.rowcount > 0

    async def update_enabled_async(self, user_id: str, task_id: int, enabled: bool) -> bool:
        """Enable or disable a task (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.update_enabled, user_id, task_id, enabled)

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        pool = await self._pg_pool()
        now = datetime.now(UTC)
        async with pool.acquire() as conn, conn.transaction():
            await self._require_cloud_sync_consent_for_write(conn, resolved_user_id)
            row = await conn.fetchrow(
                """
                UPDATE sync_proactive_tasks
                SET enabled = $1, updated_at = $2, lww_hlc = $3,
                    lww_actor_id = $4, updated_by_device_id = $5,
                    last_mutation_id = $6, commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $7 AND id = $8 AND deleted_at IS NULL
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                bool(enabled),
                now,
                _sync_hlc(now),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
                resolved_user_id,
                task_id,
            )
            if row is None:
                return False
            await _write_proactive_task_journal(conn, row, "upsert")
            return True

    @staticmethod
    def _clean_last_error(error: str | None) -> str | None:
        if error is None:
            return None
        stripped = str(error).strip()
        if not stripped:
            return None
        return stripped[:_MAX_LAST_ERROR_CHARS]

    def record_execution(
        self,
        user_id: str,
        task_id: int,
        next_run: str,
        *,
        status: str = _EXECUTION_STATUS_OK,
        error: str | None = None,
    ) -> None:
        """Record that a task was executed and set the next run time."""
        if self._use_pg:
            raise RuntimeError("Use record_execution_async() in PostgreSQL mode")

        resolved_user_id = self._require_user_id(user_id)
        now = datetime.now(UTC).isoformat()
        last_error = self._clean_last_error(error)
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE proactive_tasks
                SET last_run = ?, next_run = ?, last_status = ?, last_error = ?
                WHERE user_id = ? AND id = ?
                """,
                (now, next_run, status, last_error, resolved_user_id, task_id),
            )

    async def record_execution_async(
        self,
        user_id: str,
        task_id: int,
        next_run: str,
        *,
        status: str = _EXECUTION_STATUS_OK,
        error: str | None = None,
    ) -> None:
        """Record that a task was executed and set the next run time (async, both backends)."""
        if not self._use_pg:
            await asyncio.to_thread(
                self.record_execution,
                user_id,
                task_id,
                next_run,
                status=status,
                error=error,
            )
            return

        await self.pg_initialize()
        resolved_user_id = self._require_user_id(user_id)
        now = datetime.now(UTC)
        last_error = self._clean_last_error(error)
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            await self._require_cloud_sync_consent_for_write(conn, resolved_user_id)
            row = await conn.fetchrow(
                """
                UPDATE sync_proactive_tasks
                SET last_run_at = $1, next_run_at = $2, last_status = $3, last_error = $4, updated_at = $1,
                    lww_hlc = $5, lww_actor_id = $6, updated_by_device_id = $7,
                    last_mutation_id = $8, commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $9 AND id = $10 AND deleted_at IS NULL
                RETURNING id, user_id::text AS user_id, commit_seq, last_mutation_id
                """,
                now,
                _utc_datetime(next_run),
                status,
                last_error,
                _sync_hlc(now),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
                resolved_user_id,
                task_id,
            )
            if row is not None:
                await _write_proactive_task_journal(conn, row, "upsert")

    def get_due_tasks(self, now_iso: str) -> list[ProactiveTask]:
        """Return enabled tasks whose next_run is at or before now.

        NOTE: Returns tasks for ALL users -- intended for the system scheduler
        daemon.  Each task's ``user_id`` is used to set ``user_scope()``
        before execution in ``ProactiveScheduler._execute_task()``.
        """
        if self._use_pg:
            raise RuntimeError("Use get_due_tasks_async() in PostgreSQL mode")

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM proactive_tasks
                WHERE enabled = 1 AND next_run <= ?
                ORDER BY next_run ASC
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get_due_tasks_async(self, now_iso: str) -> list[ProactiveTask]:
        """Return due tasks for all users (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.get_due_tasks, now_iso)

        await self.pg_initialize()
        pool = await self._pg_pool()
        rows = await pool.fetch(
            """
            SELECT *
            FROM sync_proactive_tasks
            WHERE enabled = TRUE AND next_run_at <= $1 AND deleted_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM sync_user_preferences consent
                  WHERE consent.user_id = sync_proactive_tasks.user_id
                    AND consent.key = 'consent_cloud_sync'
                    AND consent.deleted_at IS NULL
                    AND consent.value_json IN ('true'::jsonb, '"true"'::jsonb)
              )
            ORDER BY next_run_at ASC
            """,
            _utc_datetime(now_iso),
        )
        return [self._pg_row_to_task(row) for row in rows]

    def get_missed_tasks(self) -> list[ProactiveTask]:
        """Return tasks that were due but not executed (daemon was down).

        A task is considered missed if next_run is in the past and
        last_run is either null or before next_run.
        """
        if self._use_pg:
            raise RuntimeError("Use get_missed_tasks_async() in PostgreSQL mode")

        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM proactive_tasks
                WHERE enabled = 1 AND next_run < ?
                  AND (last_run IS NULL OR last_run < next_run)
                ORDER BY next_run ASC
                """,
                (now_iso,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get_missed_tasks_async(self) -> list[ProactiveTask]:
        """Return missed tasks for all users (async, both backends)."""
        if not self._use_pg:
            return await asyncio.to_thread(self.get_missed_tasks)

        await self.pg_initialize()
        now_dt = datetime.now(UTC)
        pool = await self._pg_pool()
        rows = await pool.fetch(
            """
            SELECT *
            FROM sync_proactive_tasks
            WHERE enabled = TRUE AND next_run_at < $1 AND deleted_at IS NULL
              AND (last_run_at IS NULL OR last_run_at < next_run_at)
              AND EXISTS (
                  SELECT 1
                  FROM sync_user_preferences consent
                  WHERE consent.user_id = sync_proactive_tasks.user_id
                    AND consent.key = 'consent_cloud_sync'
                    AND consent.deleted_at IS NULL
                    AND consent.value_json IN ('true'::jsonb, '"true"'::jsonb)
              )
            ORDER BY next_run_at ASC
            """,
            now_dt,
        )
        return [self._pg_row_to_task(row) for row in rows]

    def close(self) -> None:
        """Close the database connection."""
        if self._use_pg:
            return
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass


class ProactiveScheduler:
    """Manages proactive scheduled tasks with active hours and heartbeat.

    Runs an asyncio background loop checking for due tasks every 60 seconds.
    When a task is due and within active hours, it executes the task_prompt
    through the intent pipeline and delivers results via the messaging hub.
    """

    def __init__(self, store: ProactiveTaskStore | None = None) -> None:
        self._store = store or ProactiveTaskStore()
        self._running = False
        self._check_task: asyncio.Task[None] | None = None
        self._intent_pipeline: Any = None
        self._semaphore = asyncio.Semaphore(2)  # max 2 concurrent tasks
        self._inflight: set[asyncio.Task[None]] = set()
        self._inflight_task_ids: set[int] = set()

    def set_intent_pipeline(self, pipeline: Any) -> None:
        """Wire the intent pipeline for task execution."""
        self._intent_pipeline = pipeline
        logger.debug("Proactive scheduler wired to intent pipeline")

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the proactive scheduler loop."""
        if self._running:
            logger.debug("Proactive scheduler already running")
            return

        if self._store._use_pg:
            await self._store.pg_initialize()

        self._running = True

        # Recover missed tasks on startup (max 1 per task)
        await self._recover_missed()

        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("Proactive scheduler started")

    async def stop(self) -> None:
        """Stop the proactive scheduler."""
        self._running = False

        if self._check_task is not None:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None

        # Wait for in-flight tasks
        if self._inflight:
            logger.info("Waiting for %d in-flight proactive task(s)...", len(self._inflight))
            _done, pending = await asyncio.wait(self._inflight, timeout=10.0)
            for task in pending:
                task.cancel()
            self._inflight.clear()
            self._inflight_task_ids.clear()

        logger.info("Proactive scheduler stopped")

    # ------------------------------------------------------------------ CRUD wrappers

    def create_task(
        self,
        user_id: str,
        name: str,
        cron_expr: str,
        prompt: str,
        timezone: str = "UTC",
        delivery_channel: str | None = None,
    ) -> int:
        """Create a new proactive scheduled task."""
        if self._store._use_pg:
            raise RuntimeError("Use create_task_async() in PostgreSQL mode")

        return self._store.add(
            user_id=user_id,
            name=name,
            cron_expr=cron_expr,
            task_prompt=prompt,
            timezone=timezone,
            delivery_channel=delivery_channel,
        )

    async def create_task_async(
        self,
        user_id: str,
        name: str,
        cron_expr: str,
        prompt: str,
        timezone: str = "UTC",
        delivery_channel: str | None = None,
    ) -> int:
        """Create a new proactive scheduled task (async, both backends)."""
        return await self._store.add_async(
            user_id=user_id,
            name=name,
            cron_expr=cron_expr,
            task_prompt=prompt,
            timezone=timezone,
            delivery_channel=delivery_channel,
        )

    def list_tasks(self, user_id: str, enabled_only: bool = True) -> list[ProactiveTask]:
        """List all proactive tasks."""
        if self._store._use_pg:
            raise RuntimeError("Use list_tasks_async() in PostgreSQL mode")
        return self._store.list_tasks(user_id, enabled_only=enabled_only)

    async def list_tasks_async(self, user_id: str, enabled_only: bool = True) -> list[ProactiveTask]:
        """List all proactive tasks (async, both backends)."""
        return await self._store.list_tasks_async(user_id, enabled_only=enabled_only)

    def delete_task(self, user_id: str, task_id: int) -> bool:
        """Delete a proactive task."""
        if self._store._use_pg:
            raise RuntimeError("Use delete_task_async() in PostgreSQL mode")
        return self._store.delete(user_id, task_id)

    async def delete_task_async(self, user_id: str, task_id: int) -> bool:
        """Delete a proactive task (async, both backends)."""
        return await self._store.delete_async(user_id, task_id)

    def pause_task(self, user_id: str, task_id: int) -> bool:
        """Pause (disable) a proactive task."""
        if self._store._use_pg:
            raise RuntimeError("Use pause_task_async() in PostgreSQL mode")
        return self._store.update_enabled(user_id, task_id, False)

    async def pause_task_async(self, user_id: str, task_id: int) -> bool:
        """Pause (disable) a proactive task (async, both backends)."""
        return await self._store.update_enabled_async(user_id, task_id, False)

    def resume_task(self, user_id: str, task_id: int) -> bool:
        """Resume (enable) a paused proactive task."""
        if self._store._use_pg:
            raise RuntimeError("Use resume_task_async() in PostgreSQL mode")
        return self._store.update_enabled(user_id, task_id, True)

    async def resume_task_async(self, user_id: str, task_id: int) -> bool:
        """Resume (enable) a paused proactive task (async, both backends)."""
        return await self._store.update_enabled_async(user_id, task_id, True)

    # ------------------------------------------------------------------ active hours

    @staticmethod
    def _is_within_active_hours(
        task: ProactiveTask,
        now: datetime | None = None,
    ) -> bool:
        """Check if the current time is within the task's active hours."""
        if now is None:
            now = datetime.now(UTC)

        # Resolve timezone offset for the task
        try:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(task.timezone)
            local_now = now.astimezone(tz)
            hour = local_now.hour
        except Exception:
            # Fallback to UTC hour
            hour = now.hour

        start = task.active_hours_start
        end = task.active_hours_end

        if start <= end:
            return start <= hour < end
        else:
            # Wraps around midnight (e.g., 22 to 6)
            return hour >= start or hour < end

    # ------------------------------------------------------------------ check loop

    async def _check_loop(self) -> None:
        """Background loop checking for due tasks every 60 seconds."""
        while self._running:
            try:
                await self._check_and_fire()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Proactive scheduler check error: %s", exc)

            try:
                await asyncio.sleep(_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _check_and_fire(self) -> None:
        """Check for due tasks and execute them."""
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        due_tasks = await self._store.get_due_tasks_async(now_iso)
        if not due_tasks:
            return

        logger.info("Found %d due proactive task(s)", len(due_tasks))

        for task in due_tasks:
            # Check active hours
            if not self._is_within_active_hours(task, now):
                logger.debug(
                    "Proactive task id=%d skipped (outside active hours %d-%d)",
                    task.id,
                    task.active_hours_start,
                    task.active_hours_end,
                )
                # Still advance next_run so we don't keep checking
                await self._advance_next_run(task)
                continue

            self._start_execution_task(task, name="proactive-%d" % task.id)

    def _start_execution_task(self, task: ProactiveTask, *, name: str) -> bool:
        """Start a proactive run once per task id while a prior run is active."""
        if task.id in self._inflight_task_ids:
            logger.info("Proactive task id=%d skipped because it is already running", task.id)
            return False

        self._inflight_task_ids.add(task.id)
        execution = asyncio.create_task(self._guarded_execute(task), name=name)
        self._inflight.add(execution)
        execution.add_done_callback(self._inflight.discard)
        return True

    async def _guarded_execute(self, task: ProactiveTask) -> None:
        """Execute with concurrency limit.

        F-049: best-effort registration with ``AgentRegistry`` so a
        proactive run is inspectable / cancellable through the same
        TaskOutput / stop surfaces as foreground agents.
        """
        async with self._semaphore:
            registry = None
            agent_id: str | None = None
            try:
                from services.agent_runtime.registry import agent_registry as _registry

                registry = _registry
            except ImportError:
                logger.debug(
                    "AgentRegistry unavailable; proactive task %d will not be registered",
                    task.id,
                )

            if registry is not None:
                try:
                    from services.agent_runtime.registry import AgentRegistryError

                    agent_id, _cancel, _bg = await registry.register_foreground(
                        user_id=task.user_id,
                        task=task.task_prompt,
                        reason="proactive:%d" % task.id,
                        subagent_type="proactive",
                        name=task.name or None,
                    )
                except (AgentRegistryError, ValueError, RuntimeError):
                    logger.debug(
                        "Could not register proactive task %d in agent registry; "
                        "execution will proceed unregistered.",
                        task.id,
                        exc_info=True,
                    )
                    agent_id = None

            failure_msg: str | None = None
            try:
                result = await self._execute_task(task)
                if not result.succeeded:
                    failure_msg = result.error or result.status
            except Exception as exc:
                failure_msg = str(exc) or exc.__class__.__name__
                raise
            finally:
                if registry is not None and agent_id is not None:
                    from services.agent_runtime.registry import AgentRegistryError

                    try:
                        if failure_msg is None:
                            await registry.mark_complete_explicit(task.user_id, agent_id, "proactive task completed")
                        else:
                            await registry.mark_failed_explicit(task.user_id, agent_id, failure_msg)
                    except (AgentRegistryError, ValueError, RuntimeError):
                        logger.debug(
                            "Could not finalize proactive-run agent %s in registry",
                            agent_id,
                            exc_info=True,
                        )
                self._inflight_task_ids.discard(task.id)

    async def _execute_task(self, task: ProactiveTask) -> ProactiveTaskExecutionResult:
        """Execute a proactive task and deliver results."""
        start = time.monotonic()
        logger.info(
            "Executing proactive task id=%d user=%s name=%r",
            task.id,
            task.user_id,
            task.name,
        )

        result_text: str | None = None
        status = _EXECUTION_STATUS_OK
        error: str | None = None

        try:
            if self._intent_pipeline is not None:
                from core.user_context import user_scope

                process = getattr(self._intent_pipeline, "process", None)
                if not callable(process):
                    raise RuntimeError("ProactiveScheduler requires canonical IntentPipeline.process")

                with user_scope(task.user_id):
                    result = await asyncio.wait_for(
                        process(task.task_prompt, user_key=task.user_id),
                        timeout=120.0,
                    )
                if isinstance(result, dict):
                    result_text = result.get("message") or result.get("data", {}).get("message", "Task completed")
                elif hasattr(result, "data") and isinstance(result.data, dict):
                    result_text = result.data.get("message") or result.data.get("answer") or "Task completed"
                else:
                    result_text = str(result) if result else "Task completed"

                elapsed = time.monotonic() - start
                logger.info(
                    "Proactive task id=%d completed in %.1fs",
                    task.id,
                    elapsed,
                )
            else:
                logger.warning(
                    "No intent pipeline for proactive task id=%d",
                    task.id,
                )
                status = _EXECUTION_STATUS_ERROR
                error = "Intent pipeline unavailable"
                result_text = None
        except TimeoutError:
            logger.warning("Proactive task id=%d timed out", task.id)
            status = _EXECUTION_STATUS_TIMEOUT
            error = "Timed out after 120 seconds"
            result_text = "Scheduled task '%s' timed out." % task.name
        except Exception as exc:
            logger.exception("Proactive task id=%d failed: %s", task.id, exc)
            status = _EXECUTION_STATUS_ERROR
            error = str(exc) or exc.__class__.__name__
            result_text = "Scheduled task '%s' failed: %s" % (task.name, exc)

        # Deliver result to messaging channel
        if result_text:
            await self._deliver_result(task, result_text)

        execution = ProactiveTaskExecutionResult(
            status=status,
            message=result_text,
            error=error,
            advance_next_run=True,
        )
        if execution.advance_next_run:
            await self._advance_next_run(task, status=execution.status, error=execution.error)
        return execution

    async def _advance_next_run(
        self,
        task: ProactiveTask,
        *,
        status: str = _EXECUTION_STATUS_OK,
        error: str | None = None,
    ) -> None:
        """Compute and store the next run time for a task."""
        try:
            cron = croniter(task.cron_expr, datetime.now(UTC))
            next_run = cron.get_next(datetime).isoformat()
            await self._store.record_execution_async(task.user_id, task.id, next_run, status=status, error=error)
        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "Failed to compute next run for proactive task %d: %s",
                task.id,
                exc,
            )

    async def _deliver_result(self, task: ProactiveTask, message: str) -> None:
        """Deliver task result via messaging hub."""
        try:
            from messaging.hub import get_messaging_hub

            hub = get_messaging_hub()
            if hub is None:
                logger.debug("No messaging hub available for delivery")
                return

            formatted = "[Scheduled: %s]\n%s" % (task.name, message)

            if task.delivery_channel:
                sent = await hub.send(task.delivery_channel, formatted)
                if sent:
                    logger.info("Proactive result delivered via %s", task.delivery_channel)
                    return

            logger.info(
                "Proactive result for user=%s had no configured delivery channel",
                task.user_id,
            )
        except Exception as exc:
            logger.debug("Proactive result delivery failed: %s", exc)

    # ------------------------------------------------------------------ missed recovery

    async def _recover_missed(self) -> None:
        """Recover tasks that were missed while the daemon was down.

        For each missed task, execute it once (max 1 missed per task),
        then advance the next_run to the proper future time.
        """
        missed = await self._store.get_missed_tasks_async()
        if not missed:
            return

        logger.info("Recovering %d missed proactive task(s)", len(missed))

        for task in missed[: _MAX_MISSED_RECOVERY * len(missed)]:
            now = datetime.now(UTC)
            if self._is_within_active_hours(task, now):
                logger.info(
                    "Recovering missed proactive task id=%d name=%r",
                    task.id,
                    task.name,
                )
                self._start_execution_task(task, name="proactive-recover-%d" % task.id)
            else:
                # Outside active hours -- just advance next_run
                await self._advance_next_run(task)


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


def get_proactive_scheduler(
    store: ProactiveTaskStore | None = None,
) -> ProactiveScheduler:
    """Return the process-wide ProactiveScheduler singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = ProactiveScheduler(store=store)
    return _SINGLETON


def reset_proactive_scheduler_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
