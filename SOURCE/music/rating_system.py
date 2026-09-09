"""
music/rating_system.py
Track user preferences for songs (favorites/dislikes) to improve autoplay recommendations.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from core.asyncio_safe import run_async_synchronously
from core.database_strategy import postgres_url_for_surface
from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    import asyncpg

log = get_logger("viola.music.rating")

_DB_FILENAME = "state.sqlite3"
_ENC_PREFIX = "enc:"
_LEGACY_FILENAME = "song_ratings.json"
_SCHEMA_LOCK = threading.Lock()

_SQLITE_CREATE_SONG_RATINGS = """
CREATE TABLE IF NOT EXISTS song_ratings (
    user_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT,
    score INTEGER NOT NULL CHECK (score IN (-1, 0, 1)),
    play_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    rated_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, video_id)
)
"""

_SQLITE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_song_ratings_user_id ON song_ratings(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_song_ratings_user_score_rated_at ON song_ratings(user_id, score, rated_at DESC)",
)

_PG_REQUIRED_RELATIONS = ("public.sync_song_ratings",)
_SYNC_ACTOR_ID = "rating-system"
_SYNC_DEVICE_ID = "cloud"

SerializedRatingRecord = tuple[str, str, str, str | None, int, int, int, str, str, str]


def _pg_timestamptz(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _copy_legacy_ratings_if_missing(legacy_path: Path, new_path: Path) -> None:
    if new_path.exists() or not legacy_path.exists():
        return
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, new_path)
        log.info("Migrated legacy ratings JSON from %s to %s", legacy_path, new_path)
    except OSError as exc:
        log.warning(
            "Could not migrate legacy ratings JSON from %s to %s: %s",
            legacy_path,
            new_path,
            exc,
        )


@dataclass
class SongRating:
    """Represents a user's rating of a song."""

    video_id: str
    title: str
    artist: str | None = None
    rating: int = 0  # 1 = thumbs up, -1 = thumbs down, 0 = neutral
    rated_at: str = ""
    play_count: int = 0
    skip_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SongRating:
        return cls(
            video_id=str(data.get("video_id", "")),
            title=str(data.get("title", "")),
            artist=data.get("artist"),
            rating=int(data.get("rating", 0)),
            rated_at=str(data.get("rated_at", "")),
            play_count=int(data.get("play_count", 0)),
            skip_count=int(data.get("skip_count", 0)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


class RatingBackend(Protocol):
    """Backend contract for song-rating persistence."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool]

    @property
    def db_path(self) -> Path | None:
        """Return the SQLite DB path for diagnostics."""

    @property
    def pg_initialized(self) -> bool:
        """Return whether the PostgreSQL schema is initialized."""

    def initialize(self) -> None:
        """Create backend schema."""

    async def initialize_async(self) -> None:
        """Create backend schema from async startup code."""

    def load_rows(self) -> list[Any]:
        """Load all persisted rating rows."""

    async def load_rows_async(self) -> list[Any]:
        """Load all persisted rating rows from an async runtime path."""

    def write_many(self, records: list[SerializedRatingRecord]) -> int:
        """Upsert serialized ratings."""

    def delete_rating(self, user_id: str, video_id: str) -> None:
        """Delete one rating."""

    def rating_count(self) -> int:
        """Return persisted rating row count."""

    def close(self) -> None:
        """Close backend resources."""


class SqliteRatingBackend:
    """SQLite implementation of song-rating persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def pg_initialized(self) -> bool:
        return False

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("RatingSystem SQLite backend is closed")
        return self._conn

    def initialize(self) -> None:
        conn = self._connection()
        with _SCHEMA_LOCK, conn:
            conn.execute(_SQLITE_CREATE_SONG_RATINGS)
            for stmt in _SQLITE_INDEXES:
                conn.execute(stmt)

    async def initialize_async(self) -> None:
        self.initialize()

    def load_rows(self) -> list[Any]:
        rows = self._connection().execute("""
            SELECT user_id, video_id, title, artist, score, play_count, skip_count, rated_at, created_at, updated_at
            FROM song_ratings
            ORDER BY user_id ASC, updated_at DESC, video_id ASC
            """).fetchall()
        return list(rows)

    async def load_rows_async(self) -> list[Any]:
        import asyncio

        return await asyncio.to_thread(self.load_rows)

    def write_many(self, records: list[SerializedRatingRecord]) -> int:
        if not records:
            return 0
        with self._connection():
            self._connection().executemany(
                """
                INSERT INTO song_ratings (
                    user_id, video_id, title, artist, score, play_count, skip_count, rated_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, video_id)
                DO UPDATE SET
                    title = excluded.title,
                    artist = excluded.artist,
                    score = excluded.score,
                    play_count = excluded.play_count,
                    skip_count = excluded.skip_count,
                    rated_at = excluded.rated_at,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                records,
            )
        return len(records)

    def delete_rating(self, user_id: str, video_id: str) -> None:
        with self._connection():
            self._connection().execute(
                "DELETE FROM song_ratings WHERE user_id = ? AND video_id = ?",
                (user_id, video_id),
            )

    def rating_count(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) AS cnt FROM song_ratings").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception as exc:
            log.warning("Failed to close rating database connection: %s", exc)
        finally:
            self._conn = None


class PostgresRatingBackend:
    """PostgreSQL implementation of song-rating persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._ready = False

    @property
    def db_path(self) -> Path | None:
        return None

    @property
    def pg_initialized(self) -> bool:
        return self._ready

    @staticmethod
    def _run(coro: Any) -> Any:
        return run_async_synchronously(coro, logger=log)

    async def _pool(self) -> asyncpg.Pool:
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    def initialize(self) -> None:
        self._run(self.initialize_async())

    async def initialize_async(self) -> None:
        if self._ready:
            return
        pool = await self._pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="RatingSystem")
        self._ready = True
        log.info("RatingSystem initialized (PostgreSQL)")

    def load_rows(self) -> list[Any]:
        return list(self._run(self.load_rows_async()))

    async def load_rows_async(self) -> list[Any]:
        await self.initialize_async()
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT user_id, video_id, title, artist, score, play_count, skip_count, rated_at, created_at, updated_at
                FROM sync_song_ratings
                ORDER BY user_id ASC, updated_at DESC, video_id ASC
                """)
        return list(rows)

    def write_many(self, records: list[SerializedRatingRecord]) -> int:
        return int(self._run(self.write_many_async(records)))

    async def write_many_async(self, records: list[SerializedRatingRecord]) -> int:
        if not records:
            return 0
        await self.initialize_async()
        pool = await self._pool()
        pg_records = [
            (
                record[0],
                record[1],
                record[2],
                record[3],
                record[4],
                record[5],
                record[6],
                _pg_timestamptz(record[7]),
                _pg_timestamptz(record[8]),
                _pg_timestamptz(record[9]),
                (record[9] or datetime.now(UTC).isoformat()),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
            )
            for record in records
        ]
        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                """
                INSERT INTO sync_song_ratings (
                    user_id, video_id, title, artist, score, play_count, skip_count, rated_at, created_at, updated_at,
                    consent_generation, version_vector, field_versions, lww_hlc, lww_actor_id,
                    updated_by_device_id, last_mutation_id, deleted_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        0, '{}'::jsonb, '{}'::jsonb, $11, $12, $13, $14, NULL)
                ON CONFLICT(user_id, video_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    artist = EXCLUDED.artist,
                    score = EXCLUDED.score,
                    play_count = EXCLUDED.play_count,
                    skip_count = EXCLUDED.skip_count,
                    rated_at = EXCLUDED.rated_at,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    lww_hlc = EXCLUDED.lww_hlc,
                    lww_actor_id = EXCLUDED.lww_actor_id,
                    updated_by_device_id = EXCLUDED.updated_by_device_id,
                    last_mutation_id = EXCLUDED.last_mutation_id,
                    deleted_at = NULL
                """,
                pg_records,
            )
        return len(records)

    def delete_rating(self, user_id: str, video_id: str) -> None:
        self._run(self.delete_rating_async(user_id, video_id))

    async def delete_rating_async(self, user_id: str, video_id: str) -> None:
        await self.initialize_async()
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM sync_song_ratings WHERE user_id = $1 AND video_id = $2",
                user_id,
                video_id,
            )

    def rating_count(self) -> int:
        return int(self._run(self.rating_count_async()))

    async def rating_count_async(self) -> int:
        await self.initialize_async()
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM sync_song_ratings")
        return int(row["cnt"]) if row is not None else 0

    def close(self) -> None:
        return


def create_rating_backend(
    db_path: Path,
    *,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> RatingBackend:
    """Create the rating backend for the active application surface."""
    pg_url = postgres_url_for_surface(
        "RatingSystem",
        app_surface=app_surface,
        database_url=database_url,
    )
    if pg_url:
        return PostgresRatingBackend(pg_url)
    return SqliteRatingBackend(db_path)


class RatingSystem:
    """
    Manages user song ratings and preferences.
    Persists favorites and dislikes for autoplay context.

    Multi-user: ratings are stored per-user in a nested dict
    ``{user_id: {video_id: SongRating}}``. The ``"default"`` user is
    used for backward-compatible desktop single-user mode.
    """

    def __init__(
        self,
        storage_path: str | None = None,
        *,
        backend: RatingBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
        defer_load: bool = False,
    ):
        self._lock = threading.RLock()
        self._encryptor = self._init_encryptor()
        self._user_ratings: dict[str, dict[str, SongRating]] = {}

        self._legacy_storage_path, resolved_db_path = self._resolve_storage_paths(storage_path)
        self._legacy_storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._backend = backend or create_rating_backend(
            resolved_db_path,
            app_surface=app_surface,
            database_url=database_url,
        )
        self._db_path = self._backend.db_path

        # defer_load lets an async construction path (get_rating_system_async)
        # build the instance WITHOUT the synchronous ``load()`` -> Postgres
        # ``_run`` bridge, which raises SyncBridgeLoopError when constructed on
        # the cloud FastAPI serving loop (CL-20260711-afd7). The caller must
        # then ``await load_async()`` before use.
        if not defer_load:
            self.load()

        log.info("RatingSystem initialized with %s rated songs", self._total_rated_songs())

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
        """Initialize PostgreSQL schema when this instance uses PostgreSQL."""
        await self._backend.initialize_async()

    @staticmethod
    def _resolve_storage_paths(storage_path: str | None) -> tuple[Path, Path]:
        if storage_path:
            configured_path = Path(storage_path)
            if configured_path.suffix.lower() == ".json":
                root = configured_path.parent
                return configured_path, root / "data" / "persistence" / _DB_FILENAME
            return configured_path.parent / _LEGACY_FILENAME, configured_path

        root = get_data_dir()
        legacy_path = root / _LEGACY_FILENAME
        _copy_legacy_ratings_if_missing(Path.home().joinpath(".viola", _LEGACY_FILENAME), legacy_path)
        return legacy_path, root / "data" / "persistence" / _DB_FILENAME

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _resolve_user_id(user_id: str | None = None) -> str:
        """Resolve user_id from param or ambient context."""
        if user_id:
            return user_id
        try:
            from core.user_context import get_current_user_id

            return get_current_user_id()
        except Exception:
            from core.user_context import get_device_user_id

            device_id = get_device_user_id()
            log.warning(
                "Rating user_id resolved to device fallback (%s) because ContextVar is not set; ratings may merge across users",
                device_id,
            )
            return device_id

    def _get_user_ratings(self, user_id: str) -> dict[str, SongRating]:
        """Return the ratings dict for a user, creating on first access."""
        with self._lock:
            if user_id not in self._user_ratings:
                self._user_ratings[user_id] = {}
            return self._user_ratings[user_id]

    def _total_rated_songs(self) -> int:
        """Return the total number of rated songs across all users."""
        with self._lock:
            return sum(len(user_ratings) for user_ratings in self._user_ratings.values())

    @staticmethod
    def _init_encryptor():
        """Try to initialize field encryptor for title/artist encryption at rest."""
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
            log.info("Song rating field encryption enabled")
            return encryptor
        except (ValueError, ImportError, Exception) as exc:
            log.warning(
                "Song rating encryption disabled (title/artist stored as plaintext): %s",
                exc,
            )
            return None

    def _decrypt_field(self, value: str | None, field_name: str, video_id: str) -> str | None:
        """Decrypt a field value if it has the enc: prefix."""
        if not value or not isinstance(value, str) or not value.startswith(_ENC_PREFIX):
            return value
        if self._encryptor is None:
            log.warning(
                "Encrypted %s found for %s but no decryptor available",
                field_name,
                video_id,
            )
            return value
        try:
            return self._encryptor.decrypt(value[len(_ENC_PREFIX) :])
        except Exception as exc:
            log.warning("Failed to decrypt %s for %s: %s", field_name, video_id, exc)
            return value

    def _encrypt_field(self, value: str | None) -> str | None:
        """Encrypt a field value with enc: prefix, or return plaintext if unavailable."""
        if not value or self._encryptor is None:
            return value
        return _ENC_PREFIX + self._encryptor.encrypt(value)

    def _row_to_song_rating(self, row: Any) -> SongRating:
        video_id = str(row["video_id"])
        return SongRating(
            video_id=video_id,
            title=self._decrypt_field(row["title"], "title", video_id) or "",
            artist=self._decrypt_field(row["artist"], "artist", video_id),
            rating=int(row["score"]),
            rated_at=str(row["rated_at"] or ""),
            play_count=int(row["play_count"] or 0),
            skip_count=int(row["skip_count"] or 0),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    def _load_from_rows(self, rows: list[Any]) -> dict[str, dict[str, SongRating]]:
        loaded_ratings: dict[str, dict[str, SongRating]] = {}
        for row in rows:
            user_id = str(row["user_id"])
            user_ratings = loaded_ratings.setdefault(user_id, {})
            song_rating = self._row_to_song_rating(row)
            user_ratings[song_rating.video_id] = song_rating
        return loaded_ratings

    def _load_legacy_json(self) -> dict[str, dict[str, SongRating]]:
        if not self._legacy_storage_path.exists():
            return {}

        try:
            with self._legacy_storage_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            log.error(
                "Failed to load legacy ratings JSON %s: %s",
                self._legacy_storage_path,
                exc,
            )
            return {}

        if not isinstance(data, dict):
            log.warning(
                "Legacy ratings file has invalid top-level type: %s",
                type(data).__name__,
            )
            return {}

        loaded_ratings: dict[str, dict[str, SongRating]] = {}
        is_flat_legacy = False
        if data:
            first_value = next(iter(data.values()), None)
            is_flat_legacy = isinstance(first_value, dict) and "video_id" in first_value

        try:
            if is_flat_legacy:
                user_ratings: dict[str, SongRating] = {}
                for video_id, rating_data in data.items():
                    if not isinstance(video_id, str) or not isinstance(rating_data, dict):
                        continue
                    payload = dict(rating_data)
                    payload["title"] = self._decrypt_field(payload.get("title"), "title", video_id)
                    payload["artist"] = self._decrypt_field(payload.get("artist"), "artist", video_id)
                    payload.setdefault("video_id", video_id)
                    user_ratings[video_id] = SongRating.from_dict(payload)
                loaded_ratings["default"] = user_ratings
            else:
                for user_id, user_data in data.items():
                    if not isinstance(user_id, str) or not isinstance(user_data, dict):
                        continue
                    user_ratings = loaded_ratings.setdefault(user_id, {})
                    for video_id, rating_data in user_data.items():
                        if not isinstance(video_id, str) or not isinstance(rating_data, dict):
                            continue
                        payload = dict(rating_data)
                        payload["title"] = self._decrypt_field(payload.get("title"), "title", video_id)
                        payload["artist"] = self._decrypt_field(payload.get("artist"), "artist", video_id)
                        payload.setdefault("video_id", video_id)
                        user_ratings[video_id] = SongRating.from_dict(payload)
        except Exception as exc:
            log.error(
                "Failed to parse legacy ratings JSON %s: %s",
                self._legacy_storage_path,
                exc,
            )
            return {}

        return loaded_ratings

    def _serialized_records(
        self,
        ratings_by_user: dict[str, dict[str, SongRating]],
    ) -> list[SerializedRatingRecord]:
        records: list[SerializedRatingRecord] = []
        for user_id, user_ratings in ratings_by_user.items():
            for video_id, rating in user_ratings.items():
                created_at = rating.created_at or rating.updated_at or rating.rated_at or self._now_iso()
                updated_at = rating.updated_at or rating.rated_at or created_at
                rated_at = rating.rated_at or updated_at
                records.append(
                    (
                        user_id,
                        video_id,
                        self._encrypt_field(rating.title) or "",
                        self._encrypt_field(rating.artist),
                        int(rating.rating),
                        int(rating.play_count),
                        int(rating.skip_count),
                        rated_at,
                        created_at,
                        updated_at,
                    )
                )
        return records

    def _write_many(self, ratings_by_user: dict[str, dict[str, SongRating]]) -> int:
        return self._backend.write_many(self._serialized_records(ratings_by_user))

    def _upsert_rating(self, user_id: str, rating: SongRating) -> None:
        self._write_many({user_id: {rating.video_id: rating}})

    def _delete_rating(self, user_id: str, video_id: str) -> None:
        self._backend.delete_rating(user_id, video_id)

    def _db_rating_count(self) -> int:
        return self._backend.rating_count()

    def _mark_legacy_migrated(self) -> None:
        if not self._legacy_storage_path.exists():
            return
        migrated_path = self._legacy_storage_path.with_suffix(self._legacy_storage_path.suffix + ".migrated")
        try:
            self._legacy_storage_path.replace(migrated_path)
            log.info(
                "Migrated legacy ratings JSON to database and renamed %s to %s",
                self._legacy_storage_path,
                migrated_path,
            )
        except Exception as exc:
            log.warning(
                "Migrated legacy ratings JSON to database but could not rename %s: %s",
                self._legacy_storage_path,
                exc,
            )

    def _migrate_legacy_json_if_needed(self) -> None:
        if self._db_rating_count() > 0 or not self._legacy_storage_path.exists():
            return

        migrated_ratings = self._load_legacy_json()
        if not migrated_ratings:
            return

        migrated_count = self._write_many(migrated_ratings)
        if migrated_count <= 0:
            return

        self._user_ratings = migrated_ratings
        self._mark_legacy_migrated()
        log.info(
            "Migrated %s song ratings from legacy JSON into the database",
            migrated_count,
        )

    def load(self) -> None:
        """Load ratings from the configured database and migrate legacy JSON once."""
        with self._lock:
            self._backend.initialize()
            try:
                self._user_ratings = self._load_from_rows(self._backend.load_rows())
            except Exception as exc:
                log.error("Failed to load ratings from database: %s", exc)
                self._user_ratings = {}
                return

            try:
                self._migrate_legacy_json_if_needed()
            except Exception as exc:
                log.error("Failed during legacy ratings migration: %s", exc)

            if self._user_ratings:
                log.info(
                    "Loaded song ratings for %s user(s) from %s",
                    len(self._user_ratings),
                    self.backend_name,
                )
            else:
                log.info("No existing ratings found in the database, starting fresh")

    async def load_async(self) -> None:
        """Async-native load for serving-loop construction (CL-20260711-afd7).

        Mirrors ``load()`` but awaits the backend's async-native
        ``initialize_async`` / ``load_rows_async`` instead of the synchronous
        Postgres ``_run`` bridge, so it is safe to call while running ON the
        cloud FastAPI serving loop. The one-time legacy-JSON migration is a
        desktop/SQLite concern and is intentionally skipped here (cloud uses
        Postgres and has no legacy JSON file to migrate).
        """
        await self._backend.initialize_async()
        try:
            rows = await self._backend.load_rows_async()
        except Exception as exc:  # noqa: BLE001, RUF100 - fail-open: reset ratings on load error
            log.error("Failed to load ratings from database (async): %s", exc)
            with self._lock:
                self._user_ratings = {}
            return
        with self._lock:
            self._user_ratings = self._load_from_rows(rows)
        if self._user_ratings:
            log.info(
                "Loaded song ratings for %s user(s) from %s (async)",
                len(self._user_ratings),
                self.backend_name,
            )

    def save(self) -> None:
        """Flush the current cache to the backing store using upserts."""
        with self._lock:
            flushed = self._write_many(self._user_ratings)
            log.debug("Flushed %s cached song ratings to the database", flushed)

    def close(self) -> None:
        """Close backend resources when this instance is no longer needed."""
        with self._lock:
            self._backend.close()

    def __enter__(self) -> RatingSystem:
        """Context-manager entry."""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Context-manager exit."""
        self.close()

    def __del__(self) -> None:
        """Best-effort connection cleanup when the instance is garbage-collected."""
        try:
            self.close()
        except Exception:
            pass

    def rate_song(
        self,
        video_id: str,
        title: str,
        rating: int,
        artist: str | None = None,
        user_id: str | None = None,
    ) -> SongRating:
        """Rate a song (thumbs up = 1, thumbs down = -1)."""
        if rating not in [-1, 1]:
            raise ValueError("Rating must be 1 (thumbs up) or -1 (thumbs down)")

        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            now_iso = self._now_iso()

            if video_id in user_ratings:
                song_rating = user_ratings[video_id]
                song_rating.title = title
                song_rating.artist = artist
                song_rating.rating = rating
                song_rating.rated_at = now_iso
                song_rating.updated_at = now_iso
                if not song_rating.created_at:
                    song_rating.created_at = now_iso
            else:
                song_rating = SongRating(
                    video_id=video_id,
                    title=title,
                    artist=artist,
                    rating=rating,
                    rated_at=now_iso,
                    play_count=1,
                    skip_count=0,
                    created_at=now_iso,
                    updated_at=now_iso,
                )
                user_ratings[video_id] = song_rating

            self._upsert_rating(uid, song_rating)

        rating_text = "liked" if rating == 1 else "disliked"
        log.info("User %s song: %s (%s)", rating_text, title, video_id)
        return song_rating

    def thumbs_up(
        self,
        video_id: str,
        title: str,
        artist: str | None = None,
        user_id: str | None = None,
    ) -> SongRating:
        """Mark song as liked (thumbs up)."""
        return self.rate_song(video_id, title, 1, artist, user_id=user_id)

    def thumbs_down(
        self,
        video_id: str,
        title: str,
        artist: str | None = None,
        user_id: str | None = None,
    ) -> SongRating:
        """Mark song as disliked (thumbs down)."""
        return self.rate_song(video_id, title, -1, artist, user_id=user_id)

    def remove_rating(self, video_id: str, user_id: str | None = None) -> None:
        """Remove rating for a song (return to neutral)."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            if video_id in user_ratings:
                del user_ratings[video_id]
                self._delete_rating(uid, video_id)
        log.info("Removed rating for song: %s", video_id)

    def get_rating(self, video_id: str, user_id: str | None = None) -> SongRating | None:
        """Get rating for a specific song."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            return user_ratings.get(video_id)

    def get_favorites(self, limit: int | None = None, user_id: str | None = None) -> list[SongRating]:
        """Get all liked songs."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            favorites = [item for item in user_ratings.values() if item.rating == 1]
            favorites.sort(key=lambda item: item.rated_at, reverse=True)
            if limit:
                return favorites[:limit]
            return favorites

    def get_dislikes(self, limit: int | None = None, user_id: str | None = None) -> list[SongRating]:
        """Get all disliked songs."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            dislikes = [item for item in user_ratings.values() if item.rating == -1]
            dislikes.sort(key=lambda item: item.rated_at, reverse=True)
            if limit:
                return dislikes[:limit]
            return dislikes

    def get_disliked_video_ids(self, user_id: str | None = None) -> set[str]:
        """Get set of all disliked video IDs (for filtering)."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            return {video_id for video_id, rating in user_ratings.items() if rating.rating == -1}

    def is_disliked(self, video_id: str, user_id: str | None = None) -> bool:
        """Check if a song is disliked by video_id."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            rating = user_ratings.get(video_id)
            return rating is not None and rating.rating == -1

    def is_liked(self, video_id: str, user_id: str | None = None) -> bool:
        """Check if a song is liked by video_id."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            rating = user_ratings.get(video_id)
            return rating is not None and rating.rating == 1

    def increment_play_count(self, video_id: str, user_id: str | None = None) -> None:
        """Increment play count for a song."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            if video_id in user_ratings:
                song_rating = user_ratings[video_id]
                song_rating.play_count += 1
                song_rating.updated_at = self._now_iso()
                self._upsert_rating(uid, song_rating)

    def increment_skip_count(self, video_id: str, user_id: str | None = None) -> None:
        """Increment skip count for a song."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            if video_id in user_ratings:
                song_rating = user_ratings[video_id]
                song_rating.skip_count += 1
                song_rating.updated_at = self._now_iso()
                self._upsert_rating(uid, song_rating)

    def get_autoplay_context(self, max_favorites: int = 20, user_id: str | None = None) -> dict[str, Any]:
        """Get context for autoplay recommendations."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            favorites = self.get_favorites(limit=max_favorites, user_id=uid)
            dislikes = self.get_dislikes(user_id=uid)

            return {
                "favorites": [
                    {
                        "title": item.title,
                        "artist": item.artist,
                        "video_id": item.video_id,
                    }
                    for item in favorites
                ],
                "dislikes": [
                    {
                        "title": item.title,
                        "artist": item.artist,
                        "video_id": item.video_id,
                    }
                    for item in dislikes
                ],
                "disliked_ids": list(self.get_disliked_video_ids(user_id=uid)),
                "favorite_count": sum(1 for rating in user_ratings.values() if rating.rating == 1),
                "dislike_count": sum(1 for rating in user_ratings.values() if rating.rating == -1),
                "total_rated": len(user_ratings),
            }

    def get_statistics(self, user_id: str | None = None) -> dict[str, Any]:
        """Get statistics about user ratings."""
        with self._lock:
            uid = self._resolve_user_id(user_id)
            user_ratings = self._get_user_ratings(uid)
            favorites = self.get_favorites(user_id=uid)
            dislikes = self.get_dislikes(user_id=uid)

            return {
                "total_songs_rated": len(user_ratings),
                "favorites": len(favorites),
                "dislikes": len(dislikes),
                "neutral": len(user_ratings) - len(favorites) - len(dislikes),
                "like_ratio": len(favorites) / len(user_ratings) if user_ratings else 0,
                "most_played": sorted(
                    user_ratings.values(),
                    key=lambda item: item.play_count,
                    reverse=True,
                )[:10],
            }


_rating_system: RatingSystem | None = None
_rating_system_lock = threading.Lock()


def get_rating_system() -> RatingSystem:
    """Get or create the global rating system instance."""
    global _rating_system
    if _rating_system is None:
        _rating_system = RatingSystem()
    return _rating_system


async def get_rating_system_async() -> RatingSystem:
    """Async-native singleton accessor for serving-loop callers (CL-20260711-afd7).

    When the rating system has not yet been constructed and we are running ON
    the cloud FastAPI serving loop, the synchronous ``get_rating_system()``
    would build the instance and call ``load()`` -> Postgres ``_run`` bridge,
    raising SyncBridgeLoopError. This variant constructs with ``defer_load`` and
    then ``await load_async()``. If the singleton already exists (e.g. built on
    the desktop/worker path), it is returned unchanged.
    """
    global _rating_system
    if _rating_system is not None:
        return _rating_system
    instance = RatingSystem(defer_load=True)
    await instance.load_async()
    with _rating_system_lock:
        if _rating_system is None:
            _rating_system = instance
    return _rating_system
