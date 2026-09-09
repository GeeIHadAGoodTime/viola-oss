"""Conversation thread persistence for the React Chat mode."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from core.database_strategy import postgres_url_for_surface
from core.json_types import to_json_value
from core.logging_config import get_logger
from core.platform import get_data_dir
from services.persistence.backends.protocol import require_user_id

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DEFAULT_DB_FILENAME = "chat.sqlite3"

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_threads (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, id)
);

CREATE INDEX IF NOT EXISTS idx_chat_threads_user_updated
    ON chat_threads (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    parent_id TEXT,
    status TEXT NOT NULL DEFAULT 'complete',
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, thread_id)
        REFERENCES chat_threads (user_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_thread_created
    ON chat_messages (user_id, thread_id, created_at ASC);
"""

_PG_REQUIRED_RELATIONS = (
    "public.chat_threads",
    "public.chat_messages",
)


@dataclass(frozen=True)
class ChatThreadRecord:
    id: str
    user_id: str
    title: str
    created_at: float
    updated_at: float
    model: str | None = None
    archived: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "archived": self.archived,
        }


@dataclass(frozen=True)
class ChatMessageRecord:
    id: str
    user_id: str
    thread_id: str
    role: str
    content: str
    created_at: float
    metadata: dict[str, Any]
    parent_id: str | None = None
    status: str = "complete"

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "parent_id": self.parent_id,
            "status": self.status,
        }


class ChatStoreBackend(Protocol):
    """Backend contract for chat thread persistence."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def db_path(self) -> Path | None:
        """Return the SQLite path, when applicable."""

    @property
    def pg_initialized(self) -> bool:
        """Return whether PostgreSQL schema initialization has completed."""

    async def initialize(self) -> None:
        """Create backend schema."""

    async def create_thread(
        self,
        user_id: str,
        *,
        title: str,
        thread_id: str | None = None,
        model: str | None = None,
    ) -> ChatThreadRecord:
        """Create a new thread for one user."""

    async def list_threads(self, user_id: str, *, search: str = "", limit: int = 100) -> list[ChatThreadRecord]:
        """List threads for one user."""

    async def get_thread(self, user_id: str, thread_id: str) -> ChatThreadRecord | None:
        """Return one thread for one user."""

    async def rename_thread(self, user_id: str, thread_id: str, title: str) -> ChatThreadRecord | None:
        """Rename one thread."""

    async def set_thread_model(self, user_id: str, thread_id: str, model: str | None) -> ChatThreadRecord | None:
        """Update the model hint for one thread."""

    async def delete_thread(self, user_id: str, thread_id: str) -> bool:
        """Delete one thread and its messages."""

    async def touch_thread(self, user_id: str, thread_id: str) -> None:
        """Update a thread's updated_at timestamp."""

    async def append_message(
        self,
        user_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        parent_id: str | None = None,
        status: str = "complete",
    ) -> ChatMessageRecord:
        """Append one message to a thread."""

    async def list_messages(self, user_id: str, thread_id: str, *, limit: int = 200) -> list[ChatMessageRecord]:
        """List messages in one thread."""

    async def update_message(
        self,
        user_id: str,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChatMessageRecord | None:
        """Patch content, metadata, or status for one message."""

    async def delete_messages_after(self, user_id: str, thread_id: str, message_id: str) -> int:
        """Delete messages in a thread that were created after one message."""

    async def fork_thread(
        self,
        user_id: str,
        source_thread_id: str,
        message_id: str,
        *,
        content: str,
        model: str | None = None,
    ) -> tuple[ChatThreadRecord, list[ChatMessageRecord]]:
        """Create a new thread branched from a prior message."""

    def close(self) -> None:
        """Release backend resources."""


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return str(uuid.uuid4())


def _clean_text(value: str, *, fallback: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned or fallback


def _clean_title(title: str) -> str:
    return _clean_text(title, fallback="New chat")[:140]


def _clean_role(role: str) -> str:
    cleaned = str(role or "").strip().lower()
    if cleaned not in {"system", "user", "assistant", "tool"}:
        raise ValueError("unsupported chat message role")
    return cleaned


def _serialize_metadata(metadata: dict[str, Any] | None) -> str:
    return json.dumps(to_json_value(metadata or {}), ensure_ascii=False, sort_keys=True)


def _deserialize_metadata(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _default_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return get_data_dir() / "persistence" / _DEFAULT_DB_FILENAME


class SqliteChatBackend:
    """SQLite implementation of chat thread persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = _default_db_path(db_path)
        self._lock = threading.RLock()
        self._initialized = False

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _initialize_sync(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_SQLITE_SCHEMA)
                conn.commit()
                self._initialized = True
            finally:
                conn.close()

    async def create_thread(
        self,
        user_id: str,
        *,
        title: str,
        thread_id: str | None = None,
        model: str | None = None,
    ) -> ChatThreadRecord:
        return await asyncio.to_thread(self._create_thread_sync, user_id, title, thread_id, model)

    def _create_thread_sync(
        self,
        user_id: str,
        title: str,
        thread_id: str | None,
        model: str | None,
    ) -> ChatThreadRecord:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id or _new_id(), fallback=_new_id())
        ts = _now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO chat_threads (id, user_id, title, created_at, updated_at, model, archived)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (tid, uid, _clean_title(title), ts, ts, model),
                )
                conn.commit()
            finally:
                conn.close()
        return ChatThreadRecord(
            id=tid, user_id=uid, title=_clean_title(title), created_at=ts, updated_at=ts, model=model
        )

    async def list_threads(self, user_id: str, *, search: str = "", limit: int = 100) -> list[ChatThreadRecord]:
        return await asyncio.to_thread(self._list_threads_sync, user_id, search, limit)

    def _list_threads_sync(self, user_id: str, search: str, limit: int) -> list[ChatThreadRecord]:
        uid = require_user_id(user_id)
        capped_limit = max(1, min(int(limit or 100), 200))
        needle = str(search or "").strip().lower()
        with self._lock:
            conn = self._connect()
            try:
                if needle:
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM chat_threads
                        WHERE user_id = ?
                          AND archived = 0
                          AND (
                              LOWER(title) LIKE ?
                              OR EXISTS (
                                  SELECT 1 FROM chat_messages
                                  WHERE chat_messages.user_id = chat_threads.user_id
                                    AND chat_messages.thread_id = chat_threads.id
                                    AND LOWER(chat_messages.content) LIKE ?
                              )
                          )
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (uid, "%%%s%%" % needle, "%%%s%%" % needle, capped_limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT *
                        FROM chat_threads
                        WHERE user_id = ? AND archived = 0
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """,
                        (uid, capped_limit),
                    ).fetchall()
            finally:
                conn.close()
        return [_thread_from_row(row) for row in rows]

    async def get_thread(self, user_id: str, thread_id: str) -> ChatThreadRecord | None:
        return await asyncio.to_thread(self._get_thread_sync, user_id, thread_id)

    def _get_thread_sync(self, user_id: str, thread_id: str) -> ChatThreadRecord | None:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM chat_threads WHERE user_id = ? AND id = ? AND archived = 0",
                    (uid, tid),
                ).fetchone()
            finally:
                conn.close()
        return _thread_from_row(row) if row is not None else None

    async def rename_thread(self, user_id: str, thread_id: str, title: str) -> ChatThreadRecord | None:
        return await asyncio.to_thread(self._rename_thread_sync, user_id, thread_id, title)

    def _rename_thread_sync(self, user_id: str, thread_id: str, title: str) -> ChatThreadRecord | None:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        ts = _now()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_threads
                    SET title = ?, updated_at = ?
                    WHERE user_id = ? AND id = ? AND archived = 0
                    """,
                    (_clean_title(title), ts, uid, tid),
                )
                conn.commit()
                if cursor.rowcount < 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM chat_threads WHERE user_id = ? AND id = ?",
                    (uid, tid),
                ).fetchone()
            finally:
                conn.close()
        return _thread_from_row(row) if row is not None else None

    async def set_thread_model(self, user_id: str, thread_id: str, model: str | None) -> ChatThreadRecord | None:
        return await asyncio.to_thread(self._set_thread_model_sync, user_id, thread_id, model)

    def _set_thread_model_sync(self, user_id: str, thread_id: str, model: str | None) -> ChatThreadRecord | None:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        ts = _now()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE chat_threads
                    SET model = ?, updated_at = ?
                    WHERE user_id = ? AND id = ? AND archived = 0
                    """,
                    (model, ts, uid, tid),
                )
                conn.commit()
                if cursor.rowcount < 1:
                    return None
                row = conn.execute(
                    "SELECT * FROM chat_threads WHERE user_id = ? AND id = ?",
                    (uid, tid),
                ).fetchone()
            finally:
                conn.close()
        return _thread_from_row(row) if row is not None else None

    async def delete_thread(self, user_id: str, thread_id: str) -> bool:
        return await asyncio.to_thread(self._delete_thread_sync, user_id, thread_id)

    def _delete_thread_sync(self, user_id: str, thread_id: str) -> bool:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return False
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM chat_messages WHERE user_id = ? AND thread_id = ?", (uid, tid))
                cursor = conn.execute("DELETE FROM chat_threads WHERE user_id = ? AND id = ?", (uid, tid))
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def touch_thread(self, user_id: str, thread_id: str) -> None:
        await asyncio.to_thread(self._touch_thread_sync, user_id, thread_id)

    def _touch_thread_sync(self, user_id: str, thread_id: str) -> None:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE chat_threads SET updated_at = ? WHERE user_id = ? AND id = ? AND archived = 0",
                    (_now(), uid, tid),
                )
                conn.commit()
            finally:
                conn.close()

    async def append_message(
        self,
        user_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        parent_id: str | None = None,
        status: str = "complete",
    ) -> ChatMessageRecord:
        return await asyncio.to_thread(
            self._append_message_sync,
            user_id,
            thread_id,
            role,
            content,
            metadata,
            message_id,
            parent_id,
            status,
        )

    def _append_message_sync(
        self,
        user_id: str,
        thread_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None,
        message_id: str | None,
        parent_id: str | None,
        status: str,
    ) -> ChatMessageRecord:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            raise ValueError("thread_id is required")
        mid = _clean_text(message_id or _new_id(), fallback=_new_id())
        cleaned_role = _clean_role(role)
        cleaned_status = _clean_text(status, fallback="complete")
        ts = _now()
        serialized = _serialize_metadata(metadata)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO chat_messages
                        (id, user_id, thread_id, role, content, created_at, metadata_json, parent_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, uid, tid, cleaned_role, str(content or ""), ts, serialized, parent_id, cleaned_status),
                )
                conn.execute(
                    "UPDATE chat_threads SET updated_at = ? WHERE user_id = ? AND id = ? AND archived = 0",
                    (ts, uid, tid),
                )
                conn.commit()
            finally:
                conn.close()
        return ChatMessageRecord(
            id=mid,
            user_id=uid,
            thread_id=tid,
            role=cleaned_role,
            content=str(content or ""),
            created_at=ts,
            metadata=_deserialize_metadata(serialized),
            parent_id=parent_id,
            status=cleaned_status,
        )

    async def list_messages(self, user_id: str, thread_id: str, *, limit: int = 200) -> list[ChatMessageRecord]:
        return await asyncio.to_thread(self._list_messages_sync, user_id, thread_id, limit)

    def _list_messages_sync(self, user_id: str, thread_id: str, limit: int) -> list[ChatMessageRecord]:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return []
        capped_limit = max(1, min(int(limit or 200), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM chat_messages
                    WHERE user_id = ? AND thread_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (uid, tid, capped_limit),
                ).fetchall()
            finally:
                conn.close()
        return [_message_from_row(row) for row in rows]

    async def update_message(
        self,
        user_id: str,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChatMessageRecord | None:
        return await asyncio.to_thread(
            self._update_message_sync,
            user_id,
            thread_id,
            message_id,
            content,
            metadata_patch,
            status,
        )

    def _update_message_sync(
        self,
        user_id: str,
        thread_id: str,
        message_id: str,
        content: str | None,
        metadata_patch: dict[str, Any] | None,
        status: str | None,
    ) -> ChatMessageRecord | None:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        mid = _clean_text(message_id, fallback="")
        if not tid or not mid:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM chat_messages WHERE user_id = ? AND thread_id = ? AND id = ?",
                    (uid, tid, mid),
                ).fetchone()
                if row is None:
                    return None
                merged_metadata = _deserialize_metadata(row["metadata_json"])
                if metadata_patch:
                    merged_metadata.update(to_json_value(metadata_patch))
                next_content = row["content"] if content is None else str(content)
                next_status = row["status"] if status is None else _clean_text(status, fallback="complete")
                conn.execute(
                    """
                    UPDATE chat_messages
                    SET content = ?, metadata_json = ?, status = ?
                    WHERE user_id = ? AND thread_id = ? AND id = ?
                    """,
                    (next_content, _serialize_metadata(merged_metadata), next_status, uid, tid, mid),
                )
                conn.execute(
                    "UPDATE chat_threads SET updated_at = ? WHERE user_id = ? AND id = ?",
                    (_now(), uid, tid),
                )
                conn.commit()
                updated = conn.execute(
                    "SELECT * FROM chat_messages WHERE user_id = ? AND thread_id = ? AND id = ?",
                    (uid, tid, mid),
                ).fetchone()
            finally:
                conn.close()
        return _message_from_row(updated) if updated is not None else None

    async def delete_messages_after(self, user_id: str, thread_id: str, message_id: str) -> int:
        return await asyncio.to_thread(self._delete_messages_after_sync, user_id, thread_id, message_id)

    def _delete_messages_after_sync(self, user_id: str, thread_id: str, message_id: str) -> int:
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        mid = _clean_text(message_id, fallback="")
        if not tid or not mid:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT created_at FROM chat_messages WHERE user_id = ? AND thread_id = ? AND id = ?",
                    (uid, tid, mid),
                ).fetchone()
                if row is None:
                    return 0
                cursor = conn.execute(
                    """
                    DELETE FROM chat_messages
                    WHERE user_id = ? AND thread_id = ? AND created_at > ?
                    """,
                    (uid, tid, float(row["created_at"])),
                )
                conn.execute(
                    "UPDATE chat_threads SET updated_at = ? WHERE user_id = ? AND id = ?",
                    (_now(), uid, tid),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    async def fork_thread(
        self,
        user_id: str,
        source_thread_id: str,
        message_id: str,
        *,
        content: str,
        model: str | None = None,
    ) -> tuple[ChatThreadRecord, list[ChatMessageRecord]]:
        uid = require_user_id(user_id)
        source = await self.get_thread(uid, source_thread_id)
        if source is None:
            raise KeyError("source thread not found")
        messages = await self.list_messages(uid, source_thread_id, limit=500)
        target_index = next((index for index, item in enumerate(messages) if item.id == message_id), -1)
        if target_index < 0:
            raise KeyError("source message not found")
        fork = await self.create_thread(
            uid,
            title="%s (edited)" % source.title[:120],
            model=model if model is not None else source.model,
        )
        copied: list[ChatMessageRecord] = []
        for message in messages[:target_index]:
            copied.append(
                await self.append_message(
                    uid,
                    fork.id,
                    role=message.role,
                    content=message.content,
                    metadata={**message.metadata, "forked_from": message.id},
                    parent_id=message.parent_id,
                    status=message.status,
                )
            )
        copied.append(
            await self.append_message(
                uid,
                fork.id,
                role="user",
                content=content,
                metadata={"forked_from": message_id, "edit_branch": True},
            )
        )
        return fork, copied

    def close(self) -> None:
        return None


class PostgresChatBackend:
    """PostgreSQL implementation of chat thread persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresChatBackend requires a PostgreSQL database URL")
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

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="PostgresChatBackend")
        self._initialized = True

    async def create_thread(
        self,
        user_id: str,
        *,
        title: str,
        thread_id: str | None = None,
        model: str | None = None,
    ) -> ChatThreadRecord:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id or _new_id(), fallback=_new_id())
        ts = _now()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO chat_threads (id, user_id, title, created_at, updated_at, model, archived)
                VALUES ($1, $2, $3, $4, $5, $6, FALSE)
                RETURNING *
                """,
                tid,
                uid,
                _clean_title(title),
                ts,
                ts,
                model,
            )
        return _thread_from_row(row)

    async def list_threads(self, user_id: str, *, search: str = "", limit: int = 100) -> list[ChatThreadRecord]:
        await self.initialize()
        uid = require_user_id(user_id)
        capped_limit = max(1, min(int(limit or 100), 200))
        needle = str(search or "").strip().lower()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            if needle:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM chat_threads
                    WHERE user_id = $1
                      AND archived = FALSE
                      AND (
                          LOWER(title) LIKE $2
                          OR EXISTS (
                              SELECT 1 FROM chat_messages
                              WHERE chat_messages.user_id = chat_threads.user_id
                                AND chat_messages.thread_id = chat_threads.id
                                AND LOWER(chat_messages.content) LIKE $2
                          )
                      )
                    ORDER BY updated_at DESC
                    LIMIT $3
                    """,
                    uid,
                    "%%%s%%" % needle,
                    capped_limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM chat_threads
                    WHERE user_id = $1 AND archived = FALSE
                    ORDER BY updated_at DESC
                    LIMIT $2
                    """,
                    uid,
                    capped_limit,
                )
        return [_thread_from_row(row) for row in rows]

    async def get_thread(self, user_id: str, thread_id: str) -> ChatThreadRecord | None:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM chat_threads WHERE user_id = $1 AND id = $2 AND archived = FALSE",
                uid,
                tid,
            )
        return _thread_from_row(row) if row is not None else None

    async def rename_thread(self, user_id: str, thread_id: str, title: str) -> ChatThreadRecord | None:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE chat_threads
                SET title = $1, updated_at = $2
                WHERE user_id = $3 AND id = $4 AND archived = FALSE
                RETURNING *
                """,
                _clean_title(title),
                _now(),
                uid,
                tid,
            )
        return _thread_from_row(row) if row is not None else None

    async def set_thread_model(self, user_id: str, thread_id: str, model: str | None) -> ChatThreadRecord | None:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return None
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE chat_threads
                SET model = $1, updated_at = $2
                WHERE user_id = $3 AND id = $4 AND archived = FALSE
                RETURNING *
                """,
                model,
                _now(),
                uid,
                tid,
            )
        return _thread_from_row(row) if row is not None else None

    async def delete_thread(self, user_id: str, thread_id: str) -> bool:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return False
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM chat_messages WHERE user_id = $1 AND thread_id = $2", uid, tid)
                command = await conn.execute("DELETE FROM chat_threads WHERE user_id = $1 AND id = $2", uid, tid)
        return _row_count(command) > 0

    async def touch_thread(self, user_id: str, thread_id: str) -> None:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE chat_threads SET updated_at = $1 WHERE user_id = $2 AND id = $3 AND archived = FALSE",
                _now(),
                uid,
                tid,
            )

    async def append_message(
        self,
        user_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        parent_id: str | None = None,
        status: str = "complete",
    ) -> ChatMessageRecord:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            raise ValueError("thread_id is required")
        mid = _clean_text(message_id or _new_id(), fallback=_new_id())
        ts = _now()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO chat_messages
                        (id, user_id, thread_id, role, content, created_at, metadata_json, parent_id, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING *
                    """,
                    mid,
                    uid,
                    tid,
                    _clean_role(role),
                    str(content or ""),
                    ts,
                    _serialize_metadata(metadata),
                    parent_id,
                    _clean_text(status, fallback="complete"),
                )
                await conn.execute(
                    "UPDATE chat_threads SET updated_at = $1 WHERE user_id = $2 AND id = $3 AND archived = FALSE",
                    ts,
                    uid,
                    tid,
                )
        return _message_from_row(row)

    async def list_messages(self, user_id: str, thread_id: str, *, limit: int = 200) -> list[ChatMessageRecord]:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        if not tid:
            return []
        capped_limit = max(1, min(int(limit or 200), 500))
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM chat_messages
                WHERE user_id = $1 AND thread_id = $2
                ORDER BY created_at ASC
                LIMIT $3
                """,
                uid,
                tid,
                capped_limit,
            )
        return [_message_from_row(row) for row in rows]

    async def update_message(
        self,
        user_id: str,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChatMessageRecord | None:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        mid = _clean_text(message_id, fallback="")
        if not tid or not mid:
            return None
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                "SELECT * FROM chat_messages WHERE user_id = $1 AND thread_id = $2 AND id = $3",
                uid,
                tid,
                mid,
            )
            if current is None:
                return None
            merged_metadata = _deserialize_metadata(current["metadata_json"])
            if metadata_patch:
                merged_metadata.update(to_json_value(metadata_patch))
            row = await conn.fetchrow(
                """
                UPDATE chat_messages
                SET content = $1, metadata_json = $2, status = $3
                WHERE user_id = $4 AND thread_id = $5 AND id = $6
                RETURNING *
                """,
                current["content"] if content is None else str(content),
                _serialize_metadata(merged_metadata),
                current["status"] if status is None else _clean_text(status, fallback="complete"),
                uid,
                tid,
                mid,
            )
            await conn.execute(
                "UPDATE chat_threads SET updated_at = $1 WHERE user_id = $2 AND id = $3",
                _now(),
                uid,
                tid,
            )
        return _message_from_row(row) if row is not None else None

    async def delete_messages_after(self, user_id: str, thread_id: str, message_id: str) -> int:
        await self.initialize()
        uid = require_user_id(user_id)
        tid = _clean_text(thread_id, fallback="")
        mid = _clean_text(message_id, fallback="")
        if not tid or not mid:
            return 0
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    "SELECT created_at FROM chat_messages WHERE user_id = $1 AND thread_id = $2 AND id = $3",
                    uid,
                    tid,
                    mid,
                )
                if target is None:
                    return 0
                command = await conn.execute(
                    """
                    DELETE FROM chat_messages
                    WHERE user_id = $1 AND thread_id = $2 AND created_at > $3
                    """,
                    uid,
                    tid,
                    float(target["created_at"]),
                )
                await conn.execute(
                    "UPDATE chat_threads SET updated_at = $1 WHERE user_id = $2 AND id = $3",
                    _now(),
                    uid,
                    tid,
                )
        return _row_count(command)

    async def fork_thread(
        self,
        user_id: str,
        source_thread_id: str,
        message_id: str,
        *,
        content: str,
        model: str | None = None,
    ) -> tuple[ChatThreadRecord, list[ChatMessageRecord]]:
        uid = require_user_id(user_id)
        source = await self.get_thread(uid, source_thread_id)
        if source is None:
            raise KeyError("source thread not found")
        messages = await self.list_messages(uid, source_thread_id, limit=500)
        target_index = next((index for index, item in enumerate(messages) if item.id == message_id), -1)
        if target_index < 0:
            raise KeyError("source message not found")
        fork = await self.create_thread(
            uid,
            title="%s (edited)" % source.title[:120],
            model=model if model is not None else source.model,
        )
        copied: list[ChatMessageRecord] = []
        for message in messages[:target_index]:
            copied.append(
                await self.append_message(
                    uid,
                    fork.id,
                    role=message.role,
                    content=message.content,
                    metadata={**message.metadata, "forked_from": message.id},
                    parent_id=message.parent_id,
                    status=message.status,
                )
            )
        copied.append(
            await self.append_message(
                uid,
                fork.id,
                role="user",
                content=content,
                metadata={"forked_from": message_id, "edit_branch": True},
            )
        )
        return fork, copied

    def close(self) -> None:
        return None


def _thread_from_row(row: Any) -> ChatThreadRecord:
    return ChatThreadRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=str(row["title"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        model=row["model"],
        archived=bool(row["archived"]),
    )


def _message_from_row(row: Any) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        thread_id=str(row["thread_id"]),
        role=str(row["role"]),
        content=str(row["content"]),
        created_at=float(row["created_at"]),
        metadata=_deserialize_metadata(row["metadata_json"]),
        parent_id=row["parent_id"],
        status=str(row["status"]),
    )


def _row_count(command: object) -> int:
    parts = str(command).split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0


def create_chat_backend(
    *,
    db_path: str | Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> ChatStoreBackend:
    pg_url = postgres_url_for_surface("ChatStore", app_surface=app_surface, database_url=database_url)
    if pg_url is None:
        return SqliteChatBackend(db_path=db_path)
    return PostgresChatBackend(pg_url)


class ChatStore:
    """Backend-agnostic facade for chat threads and messages."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        backend: ChatStoreBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend = backend or create_chat_backend(
            db_path=db_path,
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

    async def initialize(self) -> None:
        await self._backend.initialize()

    async def create_thread(
        self,
        user_id: str,
        *,
        title: str = "New chat",
        thread_id: str | None = None,
        model: str | None = None,
    ) -> ChatThreadRecord:
        return await self._backend.create_thread(user_id, title=title, thread_id=thread_id, model=model)

    async def list_threads(self, user_id: str, *, search: str = "", limit: int = 100) -> list[ChatThreadRecord]:
        return await self._backend.list_threads(user_id, search=search, limit=limit)

    async def get_thread(self, user_id: str, thread_id: str) -> ChatThreadRecord | None:
        return await self._backend.get_thread(user_id, thread_id)

    async def rename_thread(self, user_id: str, thread_id: str, title: str) -> ChatThreadRecord | None:
        return await self._backend.rename_thread(user_id, thread_id, title)

    async def set_thread_model(self, user_id: str, thread_id: str, model: str | None) -> ChatThreadRecord | None:
        return await self._backend.set_thread_model(user_id, thread_id, model)

    async def delete_thread(self, user_id: str, thread_id: str) -> bool:
        return await self._backend.delete_thread(user_id, thread_id)

    async def touch_thread(self, user_id: str, thread_id: str) -> None:
        await self._backend.touch_thread(user_id, thread_id)

    async def append_message(
        self,
        user_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
        parent_id: str | None = None,
        status: str = "complete",
    ) -> ChatMessageRecord:
        return await self._backend.append_message(
            user_id,
            thread_id,
            role=role,
            content=content,
            metadata=metadata,
            message_id=message_id,
            parent_id=parent_id,
            status=status,
        )

    async def list_messages(self, user_id: str, thread_id: str, *, limit: int = 200) -> list[ChatMessageRecord]:
        return await self._backend.list_messages(user_id, thread_id, limit=limit)

    async def update_message(
        self,
        user_id: str,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> ChatMessageRecord | None:
        return await self._backend.update_message(
            user_id,
            thread_id,
            message_id,
            content=content,
            metadata_patch=metadata_patch,
            status=status,
        )

    async def delete_messages_after(self, user_id: str, thread_id: str, message_id: str) -> int:
        return await self._backend.delete_messages_after(user_id, thread_id, message_id)

    async def fork_thread(
        self,
        user_id: str,
        source_thread_id: str,
        message_id: str,
        *,
        content: str,
        model: str | None = None,
    ) -> tuple[ChatThreadRecord, list[ChatMessageRecord]]:
        return await self._backend.fork_thread(user_id, source_thread_id, message_id, content=content, model=model)

    def close(self) -> None:
        self._backend.close()


_chat_store: ChatStore | None = None
_chat_store_lock = threading.Lock()


def get_chat_store() -> ChatStore:
    global _chat_store
    if _chat_store is None:
        with _chat_store_lock:
            if _chat_store is None:
                _chat_store = ChatStore()
    return _chat_store


def reset_chat_store_for_tests() -> None:
    global _chat_store
    with _chat_store_lock:
        if _chat_store is not None:
            _chat_store.close()
        _chat_store = None


__all__ = [
    "ChatMessageRecord",
    "ChatStore",
    "ChatStoreBackend",
    "ChatThreadRecord",
    "PostgresChatBackend",
    "SqliteChatBackend",
    "create_chat_backend",
    "get_chat_store",
    "reset_chat_store_for_tests",
]
