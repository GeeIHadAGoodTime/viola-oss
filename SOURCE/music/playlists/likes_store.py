"""SQLite/PostgreSQL-backed liked-songs store for Viola."""

from __future__ import annotations

import asyncio
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

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / ".viola" / "data" / "persistence" / "liked_songs.sqlite3"
_SYNC_TIMEOUT_SECONDS = 10.0
_TIMEOUT = object()
_T = TypeVar("_T")

_DDL = """\
CREATE TABLE IF NOT EXISTS liked_songs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT    NOT NULL,
    title               TEXT,
    artist              TEXT,
    provider            TEXT    NOT NULL,
    provider_track_id   TEXT    NOT NULL,
    url                 TEXT,
    liked_at            TEXT    NOT NULL,
    unliked_at          TEXT,
    UNIQUE (user_id, provider, provider_track_id)
);

CREATE INDEX IF NOT EXISTS idx_liked_songs_user_id
    ON liked_songs (user_id);

CREATE INDEX IF NOT EXISTS idx_liked_songs_liked_at
    ON liked_songs (liked_at);
"""

_PG_REQUIRED_RELATIONS = ("public.sync_liked_songs",)
_SYNC_ACTOR_ID = "likes-store"
_SYNC_DEVICE_ID = "cloud"


@dataclass
class LikedSongRecord:
    """Represents a single row in the liked_songs table."""

    id: int
    user_id: str
    title: str | None
    artist: str | None
    provider: str
    provider_track_id: str
    url: str | None
    liked_at: str
    unliked_at: str | None

    @property
    def is_liked(self) -> bool:
        """True when the song is currently liked."""
        return self.unliked_at is None


class LikesBackend(Protocol):
    """Backend contract for liked-song persistence."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def db_path(self) -> Path | None:
        """Return the SQLite DB path for diagnostics."""

    @property
    def pg_initialized(self) -> bool:
        """Return whether the PostgreSQL schema has been initialized."""

    async def initialize(self) -> None:
        """Create backend schema."""

    def add_like(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        """Record a like synchronously."""

    async def add_like_async(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        """Record a like asynchronously."""

    def remove_like(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        """Mark a track as unliked synchronously."""

    async def remove_like_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        """Mark a track as unliked asynchronously."""

    def is_liked(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        """Return whether a track is currently liked synchronously."""

    async def is_liked_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        """Return whether a track is currently liked asynchronously."""

    def get_liked(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        """Return currently-liked songs synchronously."""

    async def get_liked_async(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        """Return currently-liked songs asynchronously."""

    def get_all_liked(self, user_id: str) -> list[LikedSongRecord]:
        """Return all currently-liked songs synchronously."""

    async def get_all_liked_async(self, user_id: str) -> list[LikedSongRecord]:
        """Return all currently-liked songs asynchronously."""

    def get_history(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        """Return like/unlike history synchronously."""

    async def get_history_async(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        """Return like/unlike history asynchronously."""


def _validate_track_key(provider: str, provider_track_id: str) -> tuple[str, str]:
    if not provider or not provider.strip():
        raise ValueError("provider must not be empty")
    if not provider_track_id or not str(provider_track_id).strip():
        raise ValueError("provider_track_id must not be empty")
    return provider, str(provider_track_id)


def _row_count(command: object) -> int:
    parts = str(command).rsplit(" ", 1)
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


class SqliteLikesBackend:
    """SQLite implementation of liked-song persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        logger.info("LikesStore initialised at %s", self._db_path)

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._init_db)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(_DDL)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> LikedSongRecord:
        return LikedSongRecord(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            artist=row["artist"],
            provider=row["provider"],
            provider_track_id=row["provider_track_id"],
            url=row["url"],
            liked_at=row["liked_at"],
            unliked_at=row["unliked_at"],
        )

    def add_like(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        liked_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO liked_songs
                        (user_id, title, artist, provider, provider_track_id, url, liked_at, unliked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(user_id, provider, provider_track_id) DO UPDATE SET
                        title      = excluded.title,
                        artist     = excluded.artist,
                        url        = excluded.url,
                        liked_at   = excluded.liked_at,
                        unliked_at = NULL
                    """,
                    (user_id, title, artist, provider, provider_track_id, url, liked_at),
                )
                conn.commit()
                row = conn.execute(
                    """
                    SELECT * FROM liked_songs
                    WHERE user_id = ? AND provider = ? AND provider_track_id = ?
                    """,
                    (user_id, provider, provider_track_id),
                ).fetchone()
                return self._row_to_record(row)
            finally:
                conn.close()

    async def add_like_async(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        return await asyncio.to_thread(self.add_like, user_id, title, artist, provider, provider_track_id, url)

    def remove_like(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        unliked_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE liked_songs
                    SET unliked_at = ?
                    WHERE user_id = ? AND provider = ? AND provider_track_id = ? AND unliked_at IS NULL
                    """,
                    (unliked_at, user_id, provider, provider_track_id),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def remove_like_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return await asyncio.to_thread(self.remove_like, user_id, provider, provider_track_id)

    def is_liked(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT id FROM liked_songs
                    WHERE user_id = ? AND provider = ? AND provider_track_id = ? AND unliked_at IS NULL
                    """,
                    (user_id, provider, provider_track_id),
                ).fetchone()
                return row is not None
            finally:
                conn.close()

    async def is_liked_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return await asyncio.to_thread(self.is_liked, user_id, provider, provider_track_id)

    def get_liked(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        clauses: list[str] = ["user_id = ?", "unliked_at IS NULL"]
        params: list[object] = [user_id]
        if since is not None:
            clauses.append("liked_at > ?")
            params.append(since)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        where = " AND ".join(clauses)
        params.append(limit)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM liked_songs WHERE %s ORDER BY liked_at DESC LIMIT ?" % where,  # nosec B608
                    params,
                ).fetchall()
                return [self._row_to_record(row) for row in rows]
            finally:
                conn.close()

    async def get_liked_async(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return await asyncio.to_thread(self.get_liked, user_id, since, provider, limit)

    def get_all_liked(self, user_id: str) -> list[LikedSongRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM liked_songs
                    WHERE user_id = ? AND unliked_at IS NULL
                    ORDER BY liked_at DESC
                    """,
                    (user_id,),
                ).fetchall()
                return [self._row_to_record(row) for row in rows]
            finally:
                conn.close()

    async def get_all_liked_async(self, user_id: str) -> list[LikedSongRecord]:
        return await asyncio.to_thread(self.get_all_liked, user_id)

    def get_history(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        params: list[object] = [user_id]
        where = "WHERE user_id = ?"
        if provider is not None:
            where = "WHERE user_id = ? AND provider = ?"
            params.append(provider)
        params.append(limit)
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM liked_songs %s ORDER BY liked_at DESC LIMIT ?" % where,  # nosec B608
                    params,
                ).fetchall()
                return [self._row_to_record(row) for row in rows]
            finally:
                conn.close()

    async def get_history_async(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return await asyncio.to_thread(self.get_history, user_id, provider, limit)


class PostgresLikesBackend:
    """PostgreSQL implementation of liked-song persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresLikesBackend requires a PostgreSQL database URL")
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

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="PostgresLikesBackend")
        self._initialized = True

    @staticmethod
    def _row_to_record(row: asyncpg.Record) -> LikedSongRecord:
        return LikedSongRecord(
            id=row["id"],
            user_id=row["user_id"],
            title=row["title"],
            artist=row["artist"],
            provider=row["provider"],
            provider_track_id=row["provider_track_id"],
            url=row["url"],
            liked_at=str(row["liked_at"]),
            unliked_at=str(row["unliked_at"]) if row["unliked_at"] else None,
        )

    def _run(self, coro: Coroutine[Any, Any, _T], *, operation: str) -> _T:
        result = run_async_synchronously(
            coro,
            timeout=_SYNC_TIMEOUT_SECONDS,
            timeout_result=_TIMEOUT,
            timeout_log_message="LikesStore PostgreSQL %s timed out after %%.1fs." % operation,
            logger=logger,
        )
        if result is _TIMEOUT:
            raise TimeoutError("LikesStore PostgreSQL %s timed out after %.1fs" % (operation, _SYNC_TIMEOUT_SECONDS))
        return cast(_T, result)

    def add_like(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        return self._run(
            self.add_like_async(user_id, title, artist, provider, provider_track_id, url),
            operation="add_like",
        )

    async def add_like_async(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        await self.initialize()
        liked_at = datetime.now(UTC)
        mutation_id = uuid.uuid4().hex
        pool = await self._pg_pool()
        await pool.execute(
            """
            INSERT INTO sync_liked_songs
                (user_id, title, artist, provider, provider_track_id, url, liked_at, unliked_at,
                 consent_generation, version_vector, field_versions, lww_hlc, lww_actor_id,
                 updated_by_device_id, last_mutation_id, updated_at, deleted_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, 0, '{}'::jsonb, '{}'::jsonb,
                    $8, $9, $10, $11, $7, NULL)
            ON CONFLICT(user_id, provider, provider_track_id) DO UPDATE SET
                title      = EXCLUDED.title,
                artist     = EXCLUDED.artist,
                url        = EXCLUDED.url,
                liked_at   = EXCLUDED.liked_at,
                unliked_at = NULL,
                lww_hlc = EXCLUDED.lww_hlc,
                lww_actor_id = EXCLUDED.lww_actor_id,
                updated_by_device_id = EXCLUDED.updated_by_device_id,
                last_mutation_id = EXCLUDED.last_mutation_id,
                updated_at = EXCLUDED.updated_at,
                deleted_at = NULL
            """,
            user_id,
            title,
            artist,
            provider,
            provider_track_id,
            url,
            liked_at,
            liked_at.isoformat(),
            _SYNC_ACTOR_ID,
            _SYNC_DEVICE_ID,
            mutation_id,
        )
        row = await pool.fetchrow(
            """
            SELECT * FROM sync_liked_songs
            WHERE user_id = $1 AND provider = $2 AND provider_track_id = $3
            """,
            user_id,
            provider,
            provider_track_id,
        )
        return self._row_to_record(row)

    def remove_like(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return self._run(self.remove_like_async(user_id, provider, provider_track_id), operation="remove_like")

    async def remove_like_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        await self.initialize()
        unliked_at = datetime.now(UTC)
        pool = await self._pg_pool()
        result = await pool.execute(
            """
            UPDATE sync_liked_songs
            SET unliked_at = $1,
                updated_at = $1,
                last_mutation_id = $5
            WHERE user_id = $2 AND provider = $3 AND provider_track_id = $4 AND unliked_at IS NULL
            """,
            unliked_at,
            user_id,
            provider,
            provider_track_id,
            uuid.uuid4().hex,
        )
        return _row_count(result) > 0

    def is_liked(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return self._run(self.is_liked_async(user_id, provider, provider_track_id), operation="is_liked")

    async def is_liked_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        provider, provider_track_id = _validate_track_key(provider, provider_track_id)
        await self.initialize()
        pool = await self._pg_pool()
        row = await pool.fetchrow(
            """
            SELECT id FROM sync_liked_songs
            WHERE user_id = $1 AND provider = $2 AND provider_track_id = $3 AND unliked_at IS NULL
            """,
            user_id,
            provider,
            provider_track_id,
        )
        return row is not None

    def get_liked(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return self._run(self.get_liked_async(user_id, since, provider, limit), operation="get_liked")

    async def get_liked_async(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        await self.initialize()
        clauses: list[str] = ["user_id = $1", "unliked_at IS NULL"]
        params: list[object] = [user_id]
        idx = 2
        if since is not None:
            clauses.append("liked_at > $%d" % idx)
            params.append(since)
            idx += 1
        if provider is not None:
            clauses.append("provider = $%d" % idx)
            params.append(provider)
            idx += 1
        clauses_str = " AND ".join(clauses)
        params.append(limit)
        pool = await self._pg_pool()
        rows = await pool.fetch(
            "SELECT * FROM sync_liked_songs WHERE %s ORDER BY liked_at DESC LIMIT $%d"
            % (clauses_str, idx),  # nosec B608
            *params,
        )
        return [self._row_to_record(row) for row in rows]

    def get_all_liked(self, user_id: str) -> list[LikedSongRecord]:
        return self._run(self.get_all_liked_async(user_id), operation="get_all_liked")

    async def get_all_liked_async(self, user_id: str) -> list[LikedSongRecord]:
        await self.initialize()
        pool = await self._pg_pool()
        rows = await pool.fetch(
            """
            SELECT * FROM sync_liked_songs
            WHERE user_id = $1 AND unliked_at IS NULL
            ORDER BY liked_at DESC
            """,
            user_id,
        )
        return [self._row_to_record(row) for row in rows]

    def get_history(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return self._run(self.get_history_async(user_id, provider, limit), operation="get_history")

    async def get_history_async(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        await self.initialize()
        params: list[object] = [user_id]
        if provider is not None:
            where = "WHERE user_id = $1 AND provider = $2"
            params.append(provider)
            limit_idx = 3
        else:
            where = "WHERE user_id = $1"
            limit_idx = 2
        params.append(limit)
        pool = await self._pg_pool()
        rows = await pool.fetch(
            "SELECT * FROM sync_liked_songs %s ORDER BY liked_at DESC LIMIT $%d" % (where, limit_idx),  # nosec B608
            *params,
        )
        return [self._row_to_record(row) for row in rows]


def create_likes_backend(
    *,
    db_path: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> LikesBackend:
    """Create a liked-song backend selected by app surface."""
    pg_url = postgres_url_for_surface("LikesStore", app_surface=app_surface, database_url=database_url)
    if pg_url is None:
        return SqliteLikesBackend(db_path=db_path)
    return PostgresLikesBackend(pg_url)


class LikesStore:
    """Backend-agnostic facade for cross-provider liked songs."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        backend: LikesBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend = backend or create_likes_backend(
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
        """Compatibility hook for callers that explicitly pre-initialize PG."""
        await self._backend.initialize()

    def add_like(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        return self._backend.add_like(user_id, title, artist, provider, provider_track_id, url)

    async def add_like_async(
        self,
        user_id: str,
        title: str | None,
        artist: str | None,
        provider: str,
        provider_track_id: str,
        url: str | None = None,
    ) -> LikedSongRecord:
        return await self._backend.add_like_async(user_id, title, artist, provider, provider_track_id, url)

    def remove_like(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return self._backend.remove_like(user_id, provider, provider_track_id)

    async def remove_like_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return await self._backend.remove_like_async(user_id, provider, provider_track_id)

    def is_liked(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return self._backend.is_liked(user_id, provider, provider_track_id)

    async def is_liked_async(self, user_id: str, provider: str, provider_track_id: str) -> bool:
        return await self._backend.is_liked_async(user_id, provider, provider_track_id)

    def get_liked(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return self._backend.get_liked(user_id, since, provider, limit)

    async def get_liked_async(
        self,
        user_id: str,
        since: str | None = None,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return await self._backend.get_liked_async(user_id, since, provider, limit)

    def get_all_liked(self, user_id: str) -> list[LikedSongRecord]:
        return self._backend.get_all_liked(user_id)

    async def get_all_liked_async(self, user_id: str) -> list[LikedSongRecord]:
        return await self._backend.get_all_liked_async(user_id)

    def get_history(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return self._backend.get_history(user_id, provider, limit)

    async def get_history_async(
        self,
        user_id: str,
        provider: str | None = None,
        limit: int = 200,
    ) -> list[LikedSongRecord]:
        return await self._backend.get_history_async(user_id, provider, limit)


__all__ = [
    "LikedSongRecord",
    "LikesBackend",
    "LikesStore",
    "PostgresLikesBackend",
    "SqliteLikesBackend",
    "create_likes_backend",
]
