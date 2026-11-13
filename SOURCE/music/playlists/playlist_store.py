"""SQLite/PostgreSQL-backed playlist store for Viola."""

from __future__ import annotations

import asyncio
import json
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

_DB_FILENAME = "playlists.sqlite3"
# Where builds up to 1.0.5 build 10 kept voice-made playlists: inside the
# source tree, which for an installed app is the install folder
# (...\\current\\_internal\\.viola\\data\\persistence). An update replaces
# that folder, so every update lost them. Kept only as a place to migrate from.
_LEGACY_INSTALL_DB_PATH = Path(__file__).parent.parent.parent / ".viola" / "data" / "persistence" / _DB_FILENAME
_legacy_migration_lock = threading.Lock()
_legacy_migration_checked: set[str] = set()


def default_playlist_db_path() -> Path:
    """The one playlist database: in the user-data folder, never the install folder.

    It is the file Settings (``PlaylistManager``) already used, so playlists
    made by voice and playlists saved by link now live in one store.
    """
    from core.platform import get_data_dir

    return get_data_dir() / _DB_FILENAME


def _legacy_playlist_db_paths(dest: Path) -> list[Path]:
    from core.platform import get_data_dir

    candidates = [_LEGACY_INSTALL_DB_PATH, get_data_dir() / "data" / "persistence" / _DB_FILENAME]
    found: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.resolve() != dest.resolve() and candidate not in found:
                found.append(candidate)
        except OSError:
            continue
    return found


def migrate_legacy_playlist_db(legacy_path: Path, dest_path: Path) -> dict[str, int]:
    """Copy playlists and tracks from an old playlist database into ``dest_path``.

    Row by row, so it is safe when the destination already holds playlists: a
    playlist that is missing is added with its tracks; a playlist of the same
    name that already exists keeps its tracks and receives, at the end, every
    old track it does not already hold. Nothing in the destination is removed or
    replaced. It runs once per old file (recorded in the destination), so a
    playlist the user deletes later does not come back. The old file is never
    changed.
    """
    counts = {"playlists": 0, "tracks": 0, "skipped": 0}
    source_key = str(legacy_path.resolve())
    dest = sqlite3.connect(str(dest_path))
    dest.row_factory = sqlite3.Row
    try:
        dest.execute(
            "CREATE TABLE IF NOT EXISTS playlist_store_migrations (source TEXT PRIMARY KEY, migrated_at TEXT NOT NULL)"
        )
        if dest.execute("SELECT 1 FROM playlist_store_migrations WHERE source = ?", (source_key,)).fetchone():
            counts["skipped"] = 1
            return counts
        source = sqlite3.connect("file:%s?mode=ro" % legacy_path.as_posix(), uri=True)
        source.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            playlists = source.execute("SELECT * FROM playlists").fetchall() if "playlists" in tables else []
            for playlist in playlists:
                keys = playlist.keys()
                user_id, name = str(playlist["user_id"]), str(playlist["name"])
                existing = dest.execute(
                    "SELECT id FROM playlists WHERE user_id = ? AND name = ?", (user_id, name)
                ).fetchone()
                if existing is None:
                    cursor = dest.execute(
                        "INSERT INTO playlists (user_id, name, created_at, metadata_json) VALUES (?, ?, ?, ?)",
                        (
                            user_id,
                            name,
                            str(playlist["created_at"]),
                            (playlist["metadata_json"] if "metadata_json" in keys else None) or "{}",
                        ),
                    )
                    dest_id = cursor.lastrowid
                    counts["playlists"] += 1
                else:
                    dest_id = existing["id"]
                if "playlist_tracks" not in tables:
                    continue
                held = {
                    (str(row["provider"]), str(row["track_uri"]))
                    for row in dest.execute(
                        "SELECT provider, track_uri FROM playlist_tracks WHERE playlist_id = ?", (dest_id,)
                    )
                }
                max_row = dest.execute(
                    "SELECT MAX(position) AS max_pos FROM playlist_tracks WHERE playlist_id = ?", (dest_id,)
                ).fetchone()
                next_position = 0 if max_row["max_pos"] is None else int(max_row["max_pos"]) + 1
                tracks = [
                    track
                    for track in source.execute(
                        "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position, id", (playlist["id"],)
                    ).fetchall()
                    if (str(track["provider"]), str(track["track_uri"])) not in held
                ]
                for position, track in enumerate(tracks, start=next_position):
                    dest.execute(
                        """
                        INSERT INTO playlist_tracks
                            (playlist_id, user_id, provider, track_uri, title, artist, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            dest_id,
                            user_id,
                            track["provider"],
                            track["track_uri"],
                            track["title"],
                            track["artist"],
                            position,
                        ),
                    )
                    counts["tracks"] += 1
            if "playlist_user_settings" in tables:
                for setting in source.execute("SELECT * FROM playlist_user_settings").fetchall():
                    dest.execute(
                        """
                        INSERT OR IGNORE INTO playlist_user_settings (user_id, default_playlist, json_migrated_at)
                        VALUES (?, ?, ?)
                        """,
                        (setting["user_id"], setting["default_playlist"], setting["json_migrated_at"]),
                    )
        finally:
            source.close()
        dest.execute(
            "INSERT INTO playlist_store_migrations (source, migrated_at) VALUES (?, ?)",
            (source_key, datetime.now(UTC).isoformat()),
        )
        dest.commit()
    except Exception:
        dest.rollback()
        raise
    finally:
        dest.close()
    return counts


def _migrate_legacy_playlist_dbs_once(dest_path: Path) -> None:
    """Bring playlists from the old locations into the one store, once per process."""
    try:
        key = str(dest_path.resolve())
        if key != str(default_playlist_db_path().resolve()):
            return
    except OSError:
        return
    with _legacy_migration_lock:
        if key in _legacy_migration_checked:
            return
        _legacy_migration_checked.add(key)
        for legacy_path in _legacy_playlist_db_paths(dest_path):
            try:
                counts = migrate_legacy_playlist_db(legacy_path, dest_path)
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Could not migrate playlists from %s to %s: %s", legacy_path, dest_path, exc)
                continue
            if not counts["skipped"]:
                logger.info(
                    "Migrated playlists from old location %s to %s: %d playlist(s), %d track(s)",
                    legacy_path,
                    dest_path,
                    counts["playlists"],
                    counts["tracks"],
                )
_SYNC_TIMEOUT_SECONDS = 10.0
_TIMEOUT = object()
_T = TypeVar("_T")

_PLAYLISTS_DDL = """\
CREATE TABLE IF NOT EXISTS playlists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    name        TEXT    NOT NULL COLLATE NOCASE,
    created_at  TEXT    NOT NULL,
    metadata_json TEXT  NOT NULL DEFAULT '{}',
    UNIQUE (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_playlists_user_id
    ON playlists (user_id);
"""

_PLAYLIST_USER_SETTINGS_DDL = """\
CREATE TABLE IF NOT EXISTS playlist_user_settings (
    user_id           TEXT PRIMARY KEY,
    default_playlist  TEXT,
    json_migrated_at  TEXT
);
"""

_PLAYLIST_TRACKS_DDL = """\
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    user_id     TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    track_uri   TEXT    NOT NULL,
    title       TEXT,
    artist      TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, playlist_id, position)
);

CREATE INDEX IF NOT EXISTS idx_playlist_tracks_user_playlist
    ON playlist_tracks (user_id, playlist_id, position);
"""

_PG_REQUIRED_RELATIONS = (
    "public.sync_playlists",
    "public.sync_playlist_tracks",
    "public.sync_playlist_user_settings",
)
_SYNC_ACTOR_ID = "playlist-store"
_SYNC_DEVICE_ID = "cloud"


@dataclass
class PlaylistRecord:
    """Represents a single playlist row."""

    id: int
    user_id: str
    name: str
    created_at: str
    metadata_json: str | None = None


@dataclass
class TrackRecord:
    """Represents a single track row within a playlist."""

    id: int
    playlist_id: int
    user_id: str
    provider: str
    track_uri: str
    title: str | None
    artist: str | None
    position: int


class PlaylistBackend(Protocol):
    """Backend contract for playlist persistence."""

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

    def create_playlist(self, user_id: str, name: str) -> PlaylistRecord: ...

    async def create_playlist_async(self, user_id: str, name: str) -> PlaylistRecord: ...

    def get_playlist(self, user_id: str, name: str) -> PlaylistRecord | None: ...

    async def get_playlist_async(self, user_id: str, name: str) -> PlaylistRecord | None: ...

    def list_playlists(self, user_id: str) -> list[PlaylistRecord]: ...

    async def list_playlists_async(self, user_id: str) -> list[PlaylistRecord]: ...

    def load_playlist_metadata_map(self, user_id: str) -> dict[str, dict[str, object]]: ...

    def save_playlist_metadata_map(self, user_id: str, playlists: dict[str, dict[str, object]]) -> bool: ...

    def get_default_playlist_name(self, user_id: str) -> str | None: ...

    def set_default_playlist_name(self, user_id: str, default_playlist: str | None) -> bool: ...

    def has_json_migration_marker(self, user_id: str) -> bool: ...

    def mark_json_migrated(self, user_id: str) -> bool: ...

    def delete_playlist(self, user_id: str, name: str) -> bool: ...

    async def delete_playlist_async(self, user_id: str, name: str) -> bool: ...

    def add_track(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord: ...

    async def add_track_async(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord: ...

    def remove_track(self, user_id: str, track_id: int) -> bool: ...

    async def remove_track_async(self, user_id: str, track_id: int) -> bool: ...

    def get_tracks(self, user_id: str, playlist_name: str) -> list[TrackRecord]: ...

    async def get_tracks_async(self, user_id: str, playlist_name: str) -> list[TrackRecord]: ...


def _validate_playlist_name(name: str) -> str:
    if not name or not name.strip():
        raise ValueError("Playlist name must not be empty")
    return name


def _row_count(command: object) -> int:
    parts = str(command).rsplit(" ", 1)
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


def _serialize_playlist_metadata(metadata: dict[str, object]) -> str:
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)


def _deserialize_playlist_metadata(metadata_json: str | None) -> dict[str, object]:
    if not metadata_json:
        return {}
    try:
        decoded = json.loads(metadata_json)
    except (TypeError, ValueError):
        logger.warning("Invalid playlist metadata JSON encountered; defaulting to empty payload")
        return {}
    if isinstance(decoded, dict):
        return cast(dict[str, object], decoded)
    return {}


def _sanitize_playlist_metadata_map(playlists: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    sanitized: dict[str, dict[str, object]] = {}
    for name, metadata in playlists.items():
        if not isinstance(name, str) or not name:
            continue
        sanitized[name] = dict(metadata) if isinstance(metadata, dict) else {}
    return sanitized


class SqlitePlaylistBackend:
    """SQLite implementation of playlist persistence."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else default_playlist_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()
        _migrate_legacy_playlist_dbs_once(self._db_path)
        logger.debug("PlaylistStore initialised at %s", self._db_path)

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
                self._create_schema(conn)
                conn.commit()
            finally:
                conn.close()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_PLAYLISTS_DDL)
        conn.executescript(_PLAYLIST_TRACKS_DDL)
        conn.executescript(_PLAYLIST_USER_SETTINGS_DDL)
        columns = self._table_columns(conn, "playlists")
        if "metadata_json" not in columns:
            conn.execute("ALTER TABLE playlists ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute("UPDATE playlists SET metadata_json = '{}' WHERE metadata_json IS NULL")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
        rows = conn.execute("PRAGMA table_info(%s)" % table_name).fetchall()  # nosec B608
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _row_to_playlist(row: sqlite3.Row) -> PlaylistRecord:
        return PlaylistRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=row["created_at"],
            metadata_json=row["metadata_json"] if "metadata_json" in row.keys() else None,
        )

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> TrackRecord:
        return TrackRecord(
            id=row["id"],
            playlist_id=row["playlist_id"],
            user_id=row["user_id"],
            provider=row["provider"],
            track_uri=row["track_uri"],
            title=row["title"],
            artist=row["artist"],
            position=row["position"],
        )

    def create_playlist(self, user_id: str, name: str) -> PlaylistRecord:
        name = _validate_playlist_name(name)
        created_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
                    (user_id, name),
                ).fetchone()
                if existing is not None:
                    raise ValueError("Playlist '%s' already exists" % name)
                cursor = conn.execute(
                    "INSERT INTO playlists (user_id, name, created_at) VALUES (?, ?, ?)",
                    (user_id, name, created_at),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM playlists WHERE id = ? AND user_id = ?",
                    (cursor.lastrowid, user_id),
                ).fetchone()
                return self._row_to_playlist(row)
            finally:
                conn.close()

    async def create_playlist_async(self, user_id: str, name: str) -> PlaylistRecord:
        return await asyncio.to_thread(self.create_playlist, user_id, name)

    def get_playlist(self, user_id: str, name: str) -> PlaylistRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM playlists WHERE user_id = ? AND name = ?",
                    (user_id, name),
                ).fetchone()
                return self._row_to_playlist(row) if row else None
            finally:
                conn.close()

    async def get_playlist_async(self, user_id: str, name: str) -> PlaylistRecord | None:
        return await asyncio.to_thread(self.get_playlist, user_id, name)

    def list_playlists(self, user_id: str) -> list[PlaylistRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT * FROM playlists
                    WHERE user_id = ?
                    ORDER BY name COLLATE NOCASE
                    """,
                    (user_id,),
                ).fetchall()
                return [self._row_to_playlist(row) for row in rows]
            finally:
                conn.close()

    async def list_playlists_async(self, user_id: str) -> list[PlaylistRecord]:
        return await asyncio.to_thread(self.list_playlists, user_id)

    def load_playlist_metadata_map(self, user_id: str) -> dict[str, dict[str, object]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT name, metadata_json
                    FROM playlists
                    WHERE user_id = ?
                    ORDER BY name COLLATE NOCASE
                    """,
                    (user_id,),
                ).fetchall()
                return {str(row["name"]): _deserialize_playlist_metadata(row["metadata_json"]) for row in rows}
            finally:
                conn.close()

    def save_playlist_metadata_map(self, user_id: str, playlists: dict[str, dict[str, object]]) -> bool:
        sanitized = _sanitize_playlist_metadata_map(playlists)
        created_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                # Rows with empty metadata are playlists made by voice (a list of
                # tracks, no link). They share this table with the link playlists
                # this map describes, and a map that never held them must not
                # delete them.
                existing_rows = conn.execute(
                    "SELECT name FROM playlists WHERE user_id = ? AND metadata_json != '{}'", (user_id,)
                ).fetchall()
                existing_names = {str(row["name"]) for row in existing_rows}
                incoming_names = set(sanitized)
                for name in existing_names - incoming_names:
                    conn.execute("DELETE FROM playlists WHERE user_id = ? AND name = ?", (user_id, name))
                for name, metadata in sanitized.items():
                    conn.execute(
                        """
                        INSERT INTO playlists (user_id, name, created_at, metadata_json)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id, name) DO UPDATE SET
                            metadata_json = excluded.metadata_json
                        """,
                        (user_id, name, created_at, _serialize_playlist_metadata(metadata)),
                    )
                conn.commit()
                return True
            finally:
                conn.close()

    def get_default_playlist_name(self, user_id: str) -> str | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT default_playlist
                    FROM playlist_user_settings
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    return None
                default_playlist = row["default_playlist"]
                return str(default_playlist) if isinstance(default_playlist, str) and default_playlist else None
            finally:
                conn.close()

    def set_default_playlist_name(self, user_id: str, default_playlist: str | None) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO playlist_user_settings (user_id, default_playlist, json_migrated_at)
                    VALUES (?, ?, NULL)
                    ON CONFLICT(user_id) DO UPDATE SET
                        default_playlist = excluded.default_playlist
                    """,
                    (user_id, default_playlist),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def has_json_migration_marker(self, user_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT json_migrated_at
                    FROM playlist_user_settings
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                return row is not None and isinstance(row["json_migrated_at"], str) and bool(row["json_migrated_at"])
            finally:
                conn.close()

    def mark_json_migrated(self, user_id: str) -> bool:
        migrated_at = datetime.now(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO playlist_user_settings (user_id, default_playlist, json_migrated_at)
                    VALUES (?, NULL, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        json_migrated_at = excluded.json_migrated_at
                    """,
                    (user_id, migrated_at),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def delete_playlist(self, user_id: str, name: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM playlists WHERE user_id = ? AND name = ?",
                    (user_id, name),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def delete_playlist_async(self, user_id: str, name: str) -> bool:
        return await asyncio.to_thread(self.delete_playlist, user_id, name)

    def add_track(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        with self._lock:
            conn = self._connect()
            try:
                pl_row = conn.execute(
                    "SELECT * FROM playlists WHERE user_id = ? AND name = ?",
                    (user_id, playlist_name),
                ).fetchone()
                if pl_row is None:
                    raise ValueError("Playlist '%s' not found" % playlist_name)
                playlist_id: int = pl_row["id"]
                max_pos_row = conn.execute(
                    """
                    SELECT MAX(position) AS max_pos
                    FROM playlist_tracks
                    WHERE user_id = ? AND playlist_id = ?
                    """,
                    (user_id, playlist_id),
                ).fetchone()
                next_position = (max_pos_row["max_pos"] or 0) + 1
                cursor = conn.execute(
                    """
                    INSERT INTO playlist_tracks
                        (playlist_id, user_id, provider, track_uri, title, artist, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (playlist_id, user_id, provider, track_uri, title, artist, next_position),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM playlist_tracks WHERE id = ? AND user_id = ?",
                    (cursor.lastrowid, user_id),
                ).fetchone()
                return self._row_to_track(row)
            finally:
                conn.close()

    async def add_track_async(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        return await asyncio.to_thread(self.add_track, user_id, playlist_name, provider, track_uri, title, artist)

    def remove_track(self, user_id: str, track_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM playlist_tracks WHERE id = ? AND user_id = ?",
                    (track_id, user_id),
                )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    async def remove_track_async(self, user_id: str, track_id: int) -> bool:
        return await asyncio.to_thread(self.remove_track, user_id, track_id)

    def get_tracks(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        with self._lock:
            conn = self._connect()
            try:
                pl_row = conn.execute(
                    "SELECT id FROM playlists WHERE user_id = ? AND name = ?",
                    (user_id, playlist_name),
                ).fetchone()
                if pl_row is None:
                    return []
                rows = conn.execute(
                    """
                    SELECT * FROM playlist_tracks
                    WHERE user_id = ? AND playlist_id = ?
                    ORDER BY position ASC
                    """,
                    (user_id, pl_row["id"]),
                ).fetchall()
                return [self._row_to_track(row) for row in rows]
            finally:
                conn.close()

    async def get_tracks_async(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        return await asyncio.to_thread(self.get_tracks, user_id, playlist_name)


class PostgresPlaylistBackend:
    """PostgreSQL implementation of playlist persistence."""

    backend_name = "postgres"
    is_postgres = True

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresPlaylistBackend requires a PostgreSQL database URL")
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

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="PostgresPlaylistBackend")
        self._initialized = True

    def _run(self, coro: Coroutine[Any, Any, _T], *, operation: str) -> _T:
        result = run_async_synchronously(
            coro,
            timeout=_SYNC_TIMEOUT_SECONDS,
            timeout_result=_TIMEOUT,
            timeout_log_message="PlaylistStore PostgreSQL %s timed out after %%.1fs." % operation,
            logger=logger,
        )
        if result is _TIMEOUT:
            raise TimeoutError("PlaylistStore PostgreSQL %s timed out after %.1fs" % (operation, _SYNC_TIMEOUT_SECONDS))
        return cast(_T, result)

    @staticmethod
    def _row_to_playlist(row: asyncpg.Record) -> PlaylistRecord:
        return PlaylistRecord(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            created_at=str(row["created_at"]),
            metadata_json=(
                _serialize_playlist_metadata(row["metadata_json"])
                if isinstance(row["metadata_json"], dict)
                else row["metadata_json"]
            ),
        )

    @staticmethod
    def _row_to_track(row: asyncpg.Record) -> TrackRecord:
        return TrackRecord(
            id=row["id"],
            playlist_id=row["playlist_id"],
            user_id=row["user_id"],
            provider=row["provider"],
            track_uri=row["track_uri"],
            title=row["title"],
            artist=row["artist"],
            position=row["position"],
        )

    def create_playlist(self, user_id: str, name: str) -> PlaylistRecord:
        return self._run(self.create_playlist_async(user_id, name), operation="create_playlist")

    async def create_playlist_async(self, user_id: str, name: str) -> PlaylistRecord:
        name = _validate_playlist_name(name)
        await self.initialize()
        pool = await self._pg_pool()
        existing = await pool.fetchrow(
            "SELECT id FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2) AND deleted_at IS NULL",
            user_id,
            name,
        )
        if existing is not None:
            raise ValueError("Playlist '%s' already exists" % name)
        row = await pool.fetchrow(
            """
            INSERT INTO sync_playlists
                (user_id, name, metadata_json, consent_generation, version_vector, field_versions,
                 lww_hlc, lww_actor_id, updated_by_device_id, last_mutation_id, created_at, updated_at, deleted_at)
            VALUES ($1, $2, '{}'::jsonb, 0, '{}'::jsonb, '{}'::jsonb, $3, $4, $5, $6, $7, $7, NULL)
            RETURNING *
            """,
            user_id,
            name,
            datetime.now(UTC).isoformat(),
            _SYNC_ACTOR_ID,
            _SYNC_DEVICE_ID,
            uuid.uuid4().hex,
            datetime.now(UTC),
        )
        return self._row_to_playlist(row)

    def get_playlist(self, user_id: str, name: str) -> PlaylistRecord | None:
        return self._run(self.get_playlist_async(user_id, name), operation="get_playlist")

    async def get_playlist_async(self, user_id: str, name: str) -> PlaylistRecord | None:
        await self.initialize()
        pool = await self._pg_pool()
        row = await pool.fetchrow(
            "SELECT * FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2) AND deleted_at IS NULL",
            user_id,
            name,
        )
        return self._row_to_playlist(row) if row else None

    def list_playlists(self, user_id: str) -> list[PlaylistRecord]:
        return self._run(self.list_playlists_async(user_id), operation="list_playlists")

    async def list_playlists_async(self, user_id: str) -> list[PlaylistRecord]:
        await self.initialize()
        pool = await self._pg_pool()
        rows = await pool.fetch(
            "SELECT * FROM sync_playlists WHERE user_id = $1 AND deleted_at IS NULL ORDER BY LOWER(name)",
            user_id,
        )
        return [self._row_to_playlist(row) for row in rows]

    def load_playlist_metadata_map(self, user_id: str) -> dict[str, dict[str, object]]:
        return self._run(self._load_playlist_metadata_map_async(user_id), operation="load_playlist_metadata_map")

    async def _load_playlist_metadata_map_async(self, user_id: str) -> dict[str, dict[str, object]]:
        await self.initialize()
        pool = await self._pg_pool()
        rows = await pool.fetch(
            """
            SELECT name, metadata_json
            FROM sync_playlists
            WHERE user_id = $1 AND deleted_at IS NULL
            ORDER BY LOWER(name)
            """,
            user_id,
        )
        return {str(row["name"]): _deserialize_playlist_metadata(row["metadata_json"]) for row in rows}

    def save_playlist_metadata_map(self, user_id: str, playlists: dict[str, dict[str, object]]) -> bool:
        return self._run(
            self._save_playlist_metadata_map_async(user_id, _sanitize_playlist_metadata_map(playlists)),
            operation="save_playlist_metadata_map",
        )

    async def _save_playlist_metadata_map_async(
        self,
        user_id: str,
        playlists: dict[str, dict[str, object]],
    ) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing_rows = await conn.fetch(
                "SELECT name FROM sync_playlists WHERE user_id = $1 AND deleted_at IS NULL",
                user_id,
            )
            existing_name_map = {str(row["name"]).lower(): str(row["name"]) for row in existing_rows}
            incoming_name_map = {name.lower(): name for name in playlists}
            created_at = datetime.now(UTC).isoformat()
            for lower_name, existing_name in existing_name_map.items():
                if lower_name not in incoming_name_map:
                    await conn.execute(
                        "DELETE FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2)",
                        user_id,
                        existing_name,
                    )
            for name, metadata in playlists.items():
                if name.lower() in existing_name_map:
                    await conn.execute(
                        """
                        UPDATE sync_playlists
                        SET metadata_json = $3::jsonb,
                            updated_at = $4,
                            last_mutation_id = $5
                        WHERE user_id = $1 AND LOWER(name) = LOWER($2)
                        """,
                        user_id,
                        name,
                        _serialize_playlist_metadata(metadata),
                        datetime.now(UTC),
                        uuid.uuid4().hex,
                    )
                    continue
                await conn.execute(
                    """
                    INSERT INTO sync_playlists
                        (user_id, name, created_at, metadata_json, consent_generation, version_vector, field_versions,
                         lww_hlc, lww_actor_id, updated_by_device_id, last_mutation_id, updated_at, deleted_at)
                    VALUES ($1, $2, $3, $4::jsonb, 0, '{}'::jsonb, '{}'::jsonb, $5, $6, $7, $8, $3, NULL)
                    """,
                    user_id,
                    name,
                    datetime.fromisoformat(created_at),
                    _serialize_playlist_metadata(metadata),
                    created_at,
                    _SYNC_ACTOR_ID,
                    _SYNC_DEVICE_ID,
                    uuid.uuid4().hex,
                )
        return True

    def get_default_playlist_name(self, user_id: str) -> str | None:
        return self._run(self._get_default_playlist_name_async(user_id), operation="get_default_playlist_name")

    async def _get_default_playlist_name_async(self, user_id: str) -> str | None:
        await self.initialize()
        pool = await self._pg_pool()
        row = await pool.fetchrow(
            """
            SELECT default_playlist
            FROM sync_playlist_user_settings
            WHERE user_id = $1
            """,
            user_id,
        )
        if row is None:
            return None
        default_playlist = row["default_playlist"]
        return str(default_playlist) if isinstance(default_playlist, str) and default_playlist else None

    def set_default_playlist_name(self, user_id: str, default_playlist: str | None) -> bool:
        return self._run(
            self._set_default_playlist_name_async(user_id, default_playlist),
            operation="set_default_playlist_name",
        )

    async def _set_default_playlist_name_async(self, user_id: str, default_playlist: str | None) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        await pool.execute(
            """
            INSERT INTO sync_playlist_user_settings
                (user_id, default_playlist, json_migrated_at, consent_generation, version_vector, field_versions,
                 lww_hlc, lww_actor_id, updated_by_device_id, last_mutation_id, updated_at, deleted_at)
            VALUES ($1, $2, NULL, 0, '{}'::jsonb, '{}'::jsonb, $3, $4, $5, $6, $7, NULL)
            ON CONFLICT (user_id) DO UPDATE SET
                default_playlist = EXCLUDED.default_playlist,
                updated_at = EXCLUDED.updated_at,
                last_mutation_id = EXCLUDED.last_mutation_id
            """,
            user_id,
            default_playlist,
            datetime.now(UTC).isoformat(),
            _SYNC_ACTOR_ID,
            _SYNC_DEVICE_ID,
            uuid.uuid4().hex,
            datetime.now(UTC),
        )
        return True

    def has_json_migration_marker(self, user_id: str) -> bool:
        return self._run(self._has_json_migration_marker_async(user_id), operation="has_json_migration_marker")

    async def _has_json_migration_marker_async(self, user_id: str) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        row = await pool.fetchrow(
            """
            SELECT json_migrated_at
            FROM sync_playlist_user_settings
            WHERE user_id = $1
            """,
            user_id,
        )
        return row is not None and row["json_migrated_at"] is not None

    def mark_json_migrated(self, user_id: str) -> bool:
        return self._run(
            self._mark_json_migrated_async(user_id, datetime.now(UTC).isoformat()),
            operation="mark_json_migrated",
        )

    async def _mark_json_migrated_async(self, user_id: str, migrated_at: str) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        await pool.execute(
            """
            INSERT INTO sync_playlist_user_settings
                (user_id, default_playlist, json_migrated_at, consent_generation, version_vector, field_versions,
                 lww_hlc, lww_actor_id, updated_by_device_id, last_mutation_id, updated_at, deleted_at)
            VALUES ($1, NULL, $2, 0, '{}'::jsonb, '{}'::jsonb, $3, $4, $5, $6, $7, NULL)
            ON CONFLICT (user_id) DO UPDATE SET
                json_migrated_at = EXCLUDED.json_migrated_at,
                updated_at = EXCLUDED.updated_at,
                last_mutation_id = EXCLUDED.last_mutation_id
            """,
            user_id,
            migrated_at,
            migrated_at,
            _SYNC_ACTOR_ID,
            _SYNC_DEVICE_ID,
            uuid.uuid4().hex,
            datetime.now(UTC),
        )
        return True

    def delete_playlist(self, user_id: str, name: str) -> bool:
        return self._run(self.delete_playlist_async(user_id, name), operation="delete_playlist")

    async def delete_playlist_async(self, user_id: str, name: str) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        result = await pool.execute(
            "DELETE FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2)",
            user_id,
            name,
        )
        return _row_count(result) > 0

    def add_track(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        return self._run(
            self.add_track_async(user_id, playlist_name, provider, track_uri, title, artist),
            operation="add_track",
        )

    async def add_track_async(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        await self.initialize()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            pl_row = await conn.fetchrow(
                "SELECT * FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2) AND deleted_at IS NULL",
                user_id,
                playlist_name,
            )
            if pl_row is None:
                raise ValueError("Playlist '%s' not found" % playlist_name)
            max_row = await conn.fetchrow(
                "SELECT MAX(position) AS max_pos FROM sync_playlist_tracks WHERE user_id = $1 AND playlist_id = $2",
                user_id,
                pl_row["id"],
            )
            next_position = ((max_row["max_pos"] if max_row else None) or 0) + 1
            row = await conn.fetchrow(
                """
                INSERT INTO sync_playlist_tracks
                    (playlist_id, user_id, provider, track_uri, title, artist, position,
                     consent_generation, version_vector, field_versions, lww_hlc, lww_actor_id,
                     updated_by_device_id, last_mutation_id, created_at, updated_at, deleted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 0, '{}'::jsonb, '{}'::jsonb,
                        $8, $9, $10, $11, $12, $12, NULL)
                RETURNING *
                """,
                pl_row["id"],
                user_id,
                provider,
                track_uri,
                title,
                artist,
                next_position,
                datetime.now(UTC).isoformat(),
                _SYNC_ACTOR_ID,
                _SYNC_DEVICE_ID,
                uuid.uuid4().hex,
                datetime.now(UTC),
            )
        return self._row_to_track(row)

    def remove_track(self, user_id: str, track_id: int) -> bool:
        return self._run(self.remove_track_async(user_id, track_id), operation="remove_track")

    async def remove_track_async(self, user_id: str, track_id: int) -> bool:
        await self.initialize()
        pool = await self._pg_pool()
        result = await pool.execute(
            "DELETE FROM sync_playlist_tracks WHERE id = $1 AND user_id = $2",
            track_id,
            user_id,
        )
        return _row_count(result) > 0

    def get_tracks(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        return self._run(self.get_tracks_async(user_id, playlist_name), operation="get_tracks")

    async def get_tracks_async(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        await self.initialize()
        pool = await self._pg_pool()
        pl_row = await pool.fetchrow(
            "SELECT id FROM sync_playlists WHERE user_id = $1 AND LOWER(name) = LOWER($2) AND deleted_at IS NULL",
            user_id,
            playlist_name,
        )
        if pl_row is None:
            return []
        rows = await pool.fetch(
            """
            SELECT * FROM sync_playlist_tracks
            WHERE user_id = $1 AND playlist_id = $2
            ORDER BY position ASC
            """,
            user_id,
            pl_row["id"],
        )
        return [self._row_to_track(row) for row in rows]


def create_playlist_backend(
    *,
    db_path: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> PlaylistBackend:
    """Create a playlist backend selected by app surface."""
    pg_url = postgres_url_for_surface("PlaylistStore", app_surface=app_surface, database_url=database_url)
    if pg_url is None:
        return SqlitePlaylistBackend(db_path=db_path)
    return PostgresPlaylistBackend(pg_url)


class PlaylistStore:
    """Backend-agnostic facade for named playlists and track metadata."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        backend: PlaylistBackend | None = None,
        app_surface: str | None = None,
        database_url: str | None = None,
    ) -> None:
        self._backend = backend or create_playlist_backend(
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

    def create_playlist(self, user_id: str, name: str) -> PlaylistRecord:
        return self._backend.create_playlist(user_id, name)

    async def create_playlist_async(self, user_id: str, name: str) -> PlaylistRecord:
        return await self._backend.create_playlist_async(user_id, name)

    def get_playlist(self, user_id: str, name: str) -> PlaylistRecord | None:
        return self._backend.get_playlist(user_id, name)

    async def get_playlist_async(self, user_id: str, name: str) -> PlaylistRecord | None:
        return await self._backend.get_playlist_async(user_id, name)

    def list_playlists(self, user_id: str) -> list[PlaylistRecord]:
        return self._backend.list_playlists(user_id)

    async def list_playlists_async(self, user_id: str) -> list[PlaylistRecord]:
        return await self._backend.list_playlists_async(user_id)

    def load_playlist_metadata_map(self, user_id: str) -> dict[str, dict[str, object]]:
        return self._backend.load_playlist_metadata_map(user_id)

    def save_playlist_metadata_map(self, user_id: str, playlists: dict[str, dict[str, object]]) -> bool:
        return self._backend.save_playlist_metadata_map(user_id, playlists)

    def get_default_playlist_name(self, user_id: str) -> str | None:
        return self._backend.get_default_playlist_name(user_id)

    def set_default_playlist_name(self, user_id: str, default_playlist: str | None) -> bool:
        return self._backend.set_default_playlist_name(user_id, default_playlist)

    def has_json_migration_marker(self, user_id: str) -> bool:
        return self._backend.has_json_migration_marker(user_id)

    def mark_json_migrated(self, user_id: str) -> bool:
        return self._backend.mark_json_migrated(user_id)

    def delete_playlist(self, user_id: str, name: str) -> bool:
        return self._backend.delete_playlist(user_id, name)

    async def delete_playlist_async(self, user_id: str, name: str) -> bool:
        return await self._backend.delete_playlist_async(user_id, name)

    def add_track(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        return self._backend.add_track(user_id, playlist_name, provider, track_uri, title, artist)

    async def add_track_async(
        self,
        user_id: str,
        playlist_name: str,
        provider: str,
        track_uri: str,
        title: str | None = None,
        artist: str | None = None,
    ) -> TrackRecord:
        return await self._backend.add_track_async(user_id, playlist_name, provider, track_uri, title, artist)

    def remove_track(self, user_id: str, track_id: int) -> bool:
        return self._backend.remove_track(user_id, track_id)

    async def remove_track_async(self, user_id: str, track_id: int) -> bool:
        return await self._backend.remove_track_async(user_id, track_id)

    def get_tracks(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        return self._backend.get_tracks(user_id, playlist_name)

    async def get_tracks_async(self, user_id: str, playlist_name: str) -> list[TrackRecord]:
        return await self._backend.get_tracks_async(user_id, playlist_name)


__all__ = [
    "PlaylistBackend",
    "PlaylistRecord",
    "PlaylistStore",
    "PostgresPlaylistBackend",
    "SqlitePlaylistBackend",
    "TrackRecord",
    "create_playlist_backend",
]
