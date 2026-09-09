"""Persistent outbound phone-call queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from core.database_strategy import postgres_url_for_surface
from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DB_PATH = get_data_dir() / "phone_call_queue.sqlite3"
_PG_TABLE = "phone_call_queue"
_DEFAULT_QUEUE_TTL_SECONDS = 24 * 60 * 60
_REQUIRED_PG_COLUMNS = {
    "id",
    "queue_id",
    "user_id",
    "phone_number_e164",
    "task_summary",
    "task_payload_hash",
    "caller_name",
    "max_duration_seconds",
    "user_tier",
    "issuer_channel_info",
    "info_manifest",
    "queued_at",
    "expires_at",
    "created_at",
}


def _pg_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _datetime_from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def _epoch_from_datetime(value: Any) -> float:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).timestamp()


def _payload_hash(item: QueuedOutboundCall) -> str:
    payload = json.dumps(
        {
            "task": item.task,
            "extra_context": item.extra_context,
            "issuer_channel_info": item.issuer_channel_info,
            "info_manifest": item.info_manifest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QueuedOutboundCall:
    """A confirmed outbound call waiting for the single live phone slot."""

    queue_id: str
    user_id: str
    phone_number: str
    task: str
    caller_name: str
    extra_context: str = ""
    max_duration: int | None = None
    user_tier: str = "free"
    issuer_channel_info: dict[str, Any] = field(default_factory=dict)
    info_manifest: dict[str, Any] = field(default_factory=dict)
    queued_at: float = field(default_factory=time.time)
    position: int = 0

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        phone_number: str,
        task: str,
        caller_name: str,
        extra_context: str = "",
        max_duration: int | None = None,
        user_tier: str = "free",
        issuer_channel_info: dict[str, Any] | None = None,
        info_manifest: dict[str, Any] | None = None,
    ) -> QueuedOutboundCall:
        return cls(
            queue_id=uuid.uuid4().hex,
            user_id=user_id,
            phone_number=phone_number,
            task=task,
            caller_name=caller_name,
            extra_context=extra_context,
            max_duration=max_duration,
            user_tier=user_tier,
            issuer_channel_info=dict(issuer_channel_info or {}),
            info_manifest=dict(info_manifest or {}),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "position": int(self.position),
            "user_id": self.user_id,
            "phone_number": self.phone_number,
            "task": self.task,
            "caller_name": self.caller_name,
            "queued_at": self.queued_at,
            "status": "queued",
        }


class PhoneCallQueueBackend(Protocol):
    """Backend contract for queued outbound calls."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def pg_initialized(self) -> bool: ...

    async def initialize(self) -> None: ...

    async def enqueue(self, item: QueuedOutboundCall) -> QueuedOutboundCall: ...

    async def list(self, user_id: str | None = None) -> list[QueuedOutboundCall]: ...

    async def remove_position(self, position: int, user_id: str | None = None) -> QueuedOutboundCall | None: ...

    async def pop_next(self) -> QueuedOutboundCall | None: ...

    async def count(self) -> int: ...

    def close(self) -> None: ...


def _json_loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _sqlite_row_to_item(row: sqlite3.Row, position: int) -> QueuedOutboundCall:
    return QueuedOutboundCall(
        queue_id=str(row["queue_id"]),
        user_id=str(row["user_id"]),
        phone_number=str(row["phone_number"]),
        task=str(row["task"]),
        caller_name=str(row["caller_name"]),
        extra_context=str(row["extra_context"] or ""),
        max_duration=int(row["max_duration"]) if row["max_duration"] is not None else None,
        user_tier=str(row["user_tier"] or "free"),
        issuer_channel_info=_json_loads(row["issuer_channel_info"]),
        info_manifest=_json_loads(row["info_manifest"]),
        queued_at=float(row["queued_at"]),
        position=position,
    )


class SqlitePhoneCallQueueBackend:
    """SQLite implementation of the outbound-call queue."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _DB_PATH
        self._lock = threading.RLock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    @property
    def pg_initialized(self) -> bool:
        return False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._ensure_schema)

    def _ensure_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS phone_call_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    phone_number TEXT NOT NULL,
                    task TEXT NOT NULL,
                    caller_name TEXT NOT NULL,
                    extra_context TEXT NOT NULL DEFAULT '',
                    max_duration INTEGER,
                    user_tier TEXT NOT NULL DEFAULT 'free',
                    issuer_channel_info TEXT NOT NULL DEFAULT '{}',
                    info_manifest TEXT NOT NULL DEFAULT '{}',
                    queued_at REAL NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """)
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_phone_call_queue_user_id ON phone_call_queue(user_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_phone_call_queue_queued_at ON phone_call_queue(queued_at, id)"
            )

    def _all_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM phone_call_queue ORDER BY queued_at ASC, id ASC").fetchall())

    def _visible_rows(self, user_id: str | None = None) -> list[sqlite3.Row]:
        rows = self._all_rows()
        if not user_id:
            return rows
        return [row for row in rows if str(row["user_id"]) == user_id]

    def _list_sync(self, user_id: str | None = None) -> list[QueuedOutboundCall]:
        with self._lock:
            items: list[QueuedOutboundCall] = []
            for index, row in enumerate(self._visible_rows(user_id), start=1):
                items.append(_sqlite_row_to_item(row, index))
            return items

    async def enqueue(self, item: QueuedOutboundCall) -> QueuedOutboundCall:
        def _enqueue_sync() -> QueuedOutboundCall:
            with self._lock, self._conn:
                self._conn.execute(
                    """
                    INSERT INTO phone_call_queue (
                        queue_id, user_id, phone_number, task, caller_name, extra_context,
                        max_duration, user_tier, issuer_channel_info, info_manifest, queued_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.queue_id,
                        item.user_id,
                        item.phone_number,
                        item.task,
                        item.caller_name,
                        item.extra_context,
                        item.max_duration,
                        item.user_tier,
                        json.dumps(item.issuer_channel_info),
                        json.dumps(item.info_manifest),
                        item.queued_at,
                    ),
                )
                for existing in self._list_sync(item.user_id):
                    if existing.queue_id == item.queue_id:
                        return existing
                return replace(item, position=0)

        return await asyncio.to_thread(_enqueue_sync)

    async def list(self, user_id: str | None = None) -> list[QueuedOutboundCall]:
        return await asyncio.to_thread(self._list_sync, user_id)

    async def remove_position(self, position: int, user_id: str | None = None) -> QueuedOutboundCall | None:
        def _remove_sync() -> QueuedOutboundCall | None:
            if position < 1:
                return None
            with self._lock, self._conn:
                rows = self._visible_rows(user_id)
                if position > len(rows):
                    return None
                row = rows[position - 1]
                item = _sqlite_row_to_item(row, position)
                self._conn.execute("DELETE FROM phone_call_queue WHERE queue_id = ?", (item.queue_id,))
                return item

        return await asyncio.to_thread(_remove_sync)

    async def pop_next(self) -> QueuedOutboundCall | None:
        def _pop_sync() -> QueuedOutboundCall | None:
            with self._lock, self._conn:
                rows = self._all_rows()
                if not rows:
                    return None
                item = _sqlite_row_to_item(rows[0], 1)
                self._conn.execute("DELETE FROM phone_call_queue WHERE queue_id = ?", (item.queue_id,))
                return item

        return await asyncio.to_thread(_pop_sync)

    async def count(self) -> int:
        def _count_sync() -> int:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) AS cnt FROM phone_call_queue").fetchone()
                return int(row["cnt"] if row else 0)

        return await asyncio.to_thread(_count_sync)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def _pg_row_to_item(row: Any, position: int) -> QueuedOutboundCall:
    return QueuedOutboundCall(
        queue_id=str(row["queue_id"]),
        user_id=str(row["user_id"]),
        phone_number=str(row["phone_number_e164"]),
        task=str(row["task_summary"]),
        caller_name=str(row["caller_name"]),
        extra_context="",
        max_duration=int(row["max_duration_seconds"]) if row["max_duration_seconds"] is not None else None,
        user_tier=str(row["user_tier"] or "free"),
        issuer_channel_info=_json_loads(row["issuer_channel_info"]),
        info_manifest=_json_loads(row["info_manifest"]),
        queued_at=_epoch_from_datetime(row["queued_at"]),
        position=position,
    )


class PostgresPhoneCallQueueBackend:
    """PostgreSQL implementation of the outbound-call queue."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresPhoneCallQueueBackend requires a PostgreSQL database URL")
        self._database_url = database_url
        self._initialized = False

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
        logger.info("PhoneCallQueue initialized (PostgreSQL)")

    @staticmethod
    async def _assert_schema(conn: Any) -> None:
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
                "PhoneCallQueue PostgreSQL schema mismatch for %s; missing migration-owned columns: %s"
                % (_PG_TABLE, ", ".join(missing))
            )

    async def _all_rows(self, conn: Any | None = None) -> list[Any]:
        query = "SELECT * FROM phone_call_queue ORDER BY queued_at ASC, id ASC"
        if conn is not None:
            return list(await conn.fetch(query))
        pool = await self._pg_pool()
        return list(await pool.fetch(query))

    @staticmethod
    def _filter_rows_by_user(rows: list[Any], user_id: str | None = None) -> list[Any]:
        if not user_id:
            return rows
        expected = str(_pg_uuid(user_id))
        return [row for row in rows if str(row["user_id"]) == expected]

    async def enqueue(self, item: QueuedOutboundCall) -> QueuedOutboundCall:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            queued_at = _datetime_from_epoch(item.queued_at)
            ttl_seconds = max(item.max_duration or 0, _DEFAULT_QUEUE_TTL_SECONDS)
            await conn.execute(
                """
                INSERT INTO phone_call_queue (
                    queue_id, user_id, phone_number_e164, task_summary, task_payload_hash, caller_name,
                    max_duration_seconds, user_tier, issuer_channel_info, info_manifest, queued_at, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12)
                """,
                item.queue_id,
                _pg_uuid(item.user_id),
                item.phone_number,
                item.task,
                _payload_hash(item),
                item.caller_name,
                item.max_duration,
                item.user_tier,
                json.dumps(item.issuer_channel_info),
                json.dumps(item.info_manifest),
                queued_at,
                queued_at + timedelta(seconds=ttl_seconds),
            )
            rows = await self._all_rows(conn)
        for index, row in enumerate(self._filter_rows_by_user(rows, item.user_id), start=1):
            if str(row["queue_id"]) == item.queue_id:
                return _pg_row_to_item(row, index)
        return replace(item, position=0)

    async def list(self, user_id: str | None = None) -> list[QueuedOutboundCall]:
        await self.initialize()
        rows = self._filter_rows_by_user(await self._all_rows(), user_id)
        items: list[QueuedOutboundCall] = []
        for index, row in enumerate(rows, start=1):
            items.append(_pg_row_to_item(row, index))
        return items

    async def remove_position(self, position: int, user_id: str | None = None) -> QueuedOutboundCall | None:
        if position < 1:
            return None
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = self._filter_rows_by_user(await self._all_rows(conn), user_id)
            if position > len(rows):
                return None
            row = rows[position - 1]
            item = _pg_row_to_item(row, position)
            await conn.execute("DELETE FROM phone_call_queue WHERE queue_id = $1", item.queue_id)
            return item

    async def pop_next(self) -> QueuedOutboundCall | None:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = await self._all_rows(conn)
            if not rows:
                return None
            item = _pg_row_to_item(rows[0], 1)
            await conn.execute("DELETE FROM phone_call_queue WHERE queue_id = $1", item.queue_id)
            return item

    async def count(self) -> int:
        await self.initialize()
        pool = await self._pg_pool()
        row = await pool.fetchrow("SELECT COUNT(*) AS cnt FROM phone_call_queue")
        return int(row["cnt"] if row else 0)

    def close(self) -> None:
        return


def create_phone_call_queue_backend(
    *,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> PhoneCallQueueBackend:
    """Create a call queue backend selected by app surface."""
    pg_url = postgres_url_for_surface("PhoneCallQueue", app_surface=app_surface, database_url=database_url)
    if pg_url is None:
        return SqlitePhoneCallQueueBackend()
    return PostgresPhoneCallQueueBackend(pg_url)


class PhoneCallQueue:
    """Backend-agnostic facade for outbound phone-call queue operations."""

    def __init__(
        self,
        *,
        backend: PhoneCallQueueBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend = backend or create_phone_call_queue_backend(
            app_surface=app_surface,
            database_url=database_url,
        )

    @property
    def backend_name(self) -> str:
        return self._backend.backend_name

    @property
    def pg_initialized(self) -> bool:
        return self._backend.pg_initialized

    async def pg_initialize(self) -> None:
        await self._backend.initialize()

    async def enqueue(self, item: QueuedOutboundCall) -> QueuedOutboundCall:
        return await self._backend.enqueue(item)

    async def list(self, user_id: str | None = None) -> list[QueuedOutboundCall]:
        return await self._backend.list(user_id)

    async def remove_position(self, position: int, user_id: str | None = None) -> QueuedOutboundCall | None:
        return await self._backend.remove_position(position, user_id)

    async def pop_next(self) -> QueuedOutboundCall | None:
        return await self._backend.pop_next()

    async def count(self) -> int:
        return await self._backend.count()

    def close(self) -> None:
        self._backend.close()


__all__ = [
    "PhoneCallQueue",
    "PhoneCallQueueBackend",
    "PostgresPhoneCallQueueBackend",
    "QueuedOutboundCall",
    "SqlitePhoneCallQueueBackend",
    "create_phone_call_queue_backend",
]
