"""
Local music library SQLite repository.

Provides thread-safe CRUD operations for the local music file index,
liked songs, and playlists. Follows the existing auth DB pattern:
thread-local connections, WAL mode, raw sqlite3, repository class.

Usage:
    >>> from music.providers.local.db import get_local_library_repo
    >>> repo = get_local_library_repo()
    >>> repo.initialize()
    >>> repo.upsert_track({"file_path": "/music/song.mp3", ...})
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import stat
import sys
import threading
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = """
-- Indexed library of all files in the music folder
CREATE TABLE IF NOT EXISTS library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_seconds REAL,
    format TEXT,
    file_size INTEGER,
    file_mtime REAL,
    file_hash TEXT,
    album_art_embedded INTEGER DEFAULT 0,
    artwork_data TEXT,
    media_type TEXT DEFAULT 'audio',
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_library_title ON library(title);
CREATE INDEX IF NOT EXISTS idx_library_artist ON library(artist);
CREATE INDEX IF NOT EXISTS idx_library_album ON library(album);
CREATE INDEX IF NOT EXISTS idx_library_file_path ON library(file_path);

-- Liked songs (provider-scoped, not shared with YouTube)
CREATE TABLE IF NOT EXISTS liked_songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    liked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(library_id)
);

-- Playlists
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Playlist membership
CREATE TABLE IF NOT EXISTS playlist_songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    library_id INTEGER NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(playlist_id, library_id)
);
"""


class LocalLibraryRepo:
    """Thread-safe SQLite repository for the local music library.

    Uses thread-local connections with WAL mode for concurrent read/write
    access from the scanner background thread and the main application thread.

    Multi-tenant note:
        Local music library is Tier-3 desktop-only data (CLAUDE.md storage
        tiers).  Files live on the local filesystem on a single-listener
        machine; there is no per-user keying on these rows because the
        machine itself is the user boundary.  Cloud playlists live in
        ``ui/api/routes/cloud_playlists.py`` and ARE user-scoped.
    """  # mt-ok: desktop-only Tier-3 local music library

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._initialized = False
        self._all_connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self._closed = False

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create a thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            # Restrict data directory to owner-only access (mode 700)
            if sys.platform != "win32":
                os.chmod(self._db_path.parent, stat.S_IRWXU)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.connection = conn
            with self._conn_lock:
                self._all_connections.append(conn)
        return self._local.connection

    @property
    def connection(self) -> sqlite3.Connection:
        """Get the current thread's database connection."""
        return self._get_connection()

    def initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        if self._initialized:
            return
        conn = self._get_connection()
        conn.executescript(SCHEMA)
        self._migrate_add_media_type(conn)
        self._migrate_add_artwork_data(conn)
        conn.commit()
        self._initialized = True
        logger.info("Local library database initialized at %s", self._db_path)

    def _migrate_add_media_type(self, conn: sqlite3.Connection) -> None:
        """Add media_type column if it does not exist (safe for existing DBs)."""
        cursor = conn.execute("PRAGMA table_info(library)")
        columns = {row[1] for row in cursor.fetchall()}
        if "media_type" not in columns:
            conn.execute("ALTER TABLE library ADD COLUMN media_type TEXT DEFAULT 'audio'")
            logger.info("Migration: added media_type column to library table")

    def _migrate_add_artwork_data(self, conn: sqlite3.Connection) -> None:
        """Add artwork_data column if it does not exist (safe for existing DBs)."""
        cursor = conn.execute("PRAGMA table_info(library)")
        columns = {row[1] for row in cursor.fetchall()}
        if "artwork_data" not in columns:
            conn.execute("ALTER TABLE library ADD COLUMN artwork_data TEXT")
            logger.info("Migration: added artwork_data column to library table")

    def close(self) -> None:
        """Close the current thread's database connection."""
        if hasattr(self._local, "connection") and self._local.connection is not None:
            self._local.connection.close()
            self._local.connection = None

    def close_all(self) -> None:
        """Close all tracked connections across all threads. Idempotent."""
        with self._conn_lock:
            if self._closed:
                return
            self._closed = True
            for conn in self._all_connections:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass  # Connection may already be closed or in bad state
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()
            logger.debug("Local library DB connections closed")

    # =========================================================================
    # Library CRUD
    # =========================================================================

    def upsert_track(self, track_data: dict) -> int:
        """Insert or update a track by file_path. Returns the library row id."""
        conn = self.connection
        try:
            conn.execute(
                """
                INSERT INTO library
                    (file_path, file_name, title, artist, album, duration_seconds,
                     format, file_size, file_mtime, file_hash, album_art_embedded,
                     artwork_data, media_type)
                VALUES
                    (:file_path, :file_name, :title, :artist, :album, :duration_seconds,
                     :format, :file_size, :file_mtime, :file_hash, :album_art_embedded,
                     :artwork_data, :media_type)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_name = excluded.file_name,
                    title = excluded.title,
                    artist = excluded.artist,
                    album = excluded.album,
                    duration_seconds = excluded.duration_seconds,
                    format = excluded.format,
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    file_hash = excluded.file_hash,
                    album_art_embedded = excluded.album_art_embedded,
                    artwork_data = COALESCE(excluded.artwork_data, library.artwork_data),
                    media_type = excluded.media_type,
                    indexed_at = CURRENT_TIMESTAMP
                """,
                {
                    "file_path": track_data["file_path"],
                    "file_name": track_data["file_name"],
                    "title": track_data.get("title"),
                    "artist": track_data.get("artist"),
                    "album": track_data.get("album"),
                    "duration_seconds": track_data.get("duration_seconds"),
                    "format": track_data.get("format"),
                    "file_size": track_data.get("file_size"),
                    "file_mtime": track_data.get("file_mtime"),
                    "file_hash": track_data.get("file_hash"),
                    "album_art_embedded": int(bool(track_data.get("album_art_embedded", False))),
                    "artwork_data": track_data.get("artwork_data"),
                    "media_type": track_data.get("media_type", "audio"),
                },
            )
            conn.commit()

            row = conn.execute(
                "SELECT id FROM library WHERE file_path = ?",
                (track_data["file_path"],),
            ).fetchone()
        except Exception:
            logger.exception(
                "Failed to upsert local library track %s",
                track_data.get("file_path", "<unknown>"),
            )
            return 0
        return row["id"]

    def update_artwork(self, file_path: str, artwork_data: str) -> None:
        """Update the cached artwork_data for a track by file_path."""
        conn = self.connection
        conn.execute(
            "UPDATE library SET artwork_data = ?, album_art_embedded = 1 WHERE file_path = ?",
            (artwork_data, file_path),
        )
        conn.commit()

    def get_tracks_missing_artwork(self) -> list[dict]:
        """Get tracks that have no cached artwork_data."""
        conn = self.connection
        rows = conn.execute("SELECT * FROM library WHERE artwork_data IS NULL ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def remove_tracks_not_in(self, file_paths: set[str]) -> int:
        """Remove library rows whose file_path is not in the given set.

        Used during rescan to clean up deleted files. Returns count removed.
        """
        conn = self.connection
        if not file_paths:
            cursor = conn.execute("DELETE FROM library")
            conn.commit()
            return cursor.rowcount

        # Build a temp table for efficient NOT IN check
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_paths (path TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _keep_paths")
        conn.executemany(
            "INSERT INTO _keep_paths (path) VALUES (?)",
            [(p,) for p in file_paths],
        )
        cursor = conn.execute("DELETE FROM library WHERE file_path NOT IN (SELECT path FROM _keep_paths)")
        conn.execute("DROP TABLE IF EXISTS _keep_paths")
        conn.commit()
        return cursor.rowcount

    def remove_tracks_by_paths(self, file_paths: set[str]) -> int:
        """Remove library rows whose file_path is in the given set."""
        if not file_paths:
            return 0

        conn = self.connection
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _remove_paths (path TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _remove_paths")
        conn.executemany(
            "INSERT INTO _remove_paths (path) VALUES (?)",
            [(p,) for p in file_paths],
        )
        cursor = conn.execute("DELETE FROM library WHERE file_path IN (SELECT path FROM _remove_paths)")
        conn.execute("DROP TABLE IF EXISTS _remove_paths")
        conn.commit()
        return cursor.rowcount

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        """Search tracks by title, artist, or album using SQL LIKE."""
        conn = self.connection
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT * FROM library
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? OR file_name LIKE ?
            ORDER BY title
            LIMIT ?
            """,
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_track_by_id(self, library_id: int) -> dict | None:
        """Get a single track by its library id."""
        conn = self.connection
        row = conn.execute("SELECT * FROM library WHERE id = ?", (library_id,)).fetchone()
        return dict(row) if row else None

    def get_track_by_path(self, file_path: str) -> dict | None:
        """Get a single track by its file path."""
        conn = self.connection
        row = conn.execute("SELECT * FROM library WHERE file_path = ?", (file_path,)).fetchone()
        return dict(row) if row else None

    def get_all_tracks(self) -> list[dict]:
        """Get all tracks in the library, ordered by title."""
        conn = self.connection
        rows = conn.execute("SELECT * FROM library ORDER BY title").fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Liked Songs
    # =========================================================================

    def add_like(self, library_id: int) -> None:
        """Mark a track as liked."""
        conn = self.connection
        conn.execute(
            "INSERT OR IGNORE INTO liked_songs (library_id) VALUES (?)",
            (library_id,),
        )
        conn.commit()

    def remove_like(self, library_id: int) -> None:
        """Remove a track from liked songs."""
        conn = self.connection
        conn.execute("DELETE FROM liked_songs WHERE library_id = ?", (library_id,))
        conn.commit()

    def get_liked_songs(self) -> list[dict]:
        """Get all liked songs with their library metadata."""
        conn = self.connection
        rows = conn.execute("""
            SELECT l.*, ls.liked_at
            FROM liked_songs ls
            JOIN library l ON l.id = ls.library_id
            ORDER BY ls.liked_at DESC
            """).fetchall()
        return [dict(row) for row in rows]

    def is_liked(self, library_id: int) -> bool:
        """Check whether a track is liked."""
        conn = self.connection
        row = conn.execute("SELECT 1 FROM liked_songs WHERE library_id = ?", (library_id,)).fetchone()
        return row is not None

    # =========================================================================
    # Playlists
    # =========================================================================

    def create_playlist(self, name: str) -> int:
        """Create a new playlist. Returns the playlist id."""
        conn = self.connection
        conn.execute("INSERT INTO playlists (name) VALUES (?)", (name,))
        conn.commit()
        row = conn.execute("SELECT id FROM playlists WHERE name = ?", (name,)).fetchone()
        return row["id"]

    def delete_playlist(self, playlist_id: int) -> None:
        """Delete a playlist and its song associations."""
        conn = self.connection
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        conn.commit()

    def add_to_playlist(self, playlist_id: int, library_id: int, position: int) -> bool:
        """Add a track to a playlist at the given position.

        Returns True if the track was newly added, False if it was already
        in the playlist.
        """
        conn = self.connection
        # Check if already exists
        existing = conn.execute(
            "SELECT 1 FROM playlist_songs WHERE playlist_id = ? AND library_id = ?",
            (playlist_id, library_id),
        ).fetchone()
        if existing:
            return False

        conn.execute(
            """
            INSERT OR IGNORE INTO playlist_songs (playlist_id, library_id, position)
            VALUES (?, ?, ?)
            """,
            (playlist_id, library_id, position),
        )
        conn.execute(
            "UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (playlist_id,),
        )
        conn.commit()
        return True

    def remove_from_playlist(self, playlist_id: int, library_id: int) -> None:
        """Remove a track from a playlist."""
        conn = self.connection
        conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND library_id = ?",
            (playlist_id, library_id),
        )
        conn.execute(
            "UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (playlist_id,),
        )
        conn.commit()

    def get_playlist_songs(self, playlist_id: int) -> list[dict]:
        """Get all songs in a playlist, ordered by position."""
        conn = self.connection
        rows = conn.execute(
            """
            SELECT l.*, ps.position, ps.added_at AS playlist_added_at
            FROM playlist_songs ps
            JOIN library l ON l.id = ps.library_id
            WHERE ps.playlist_id = ?
            ORDER BY ps.position
            """,
            (playlist_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_playlist_by_name(self, name: str) -> dict | None:
        """Get a single playlist by name (case-insensitive)."""
        conn = self.connection
        row = conn.execute(
            """
            SELECT p.*, COUNT(ps.id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_songs ps ON ps.playlist_id = p.id
            WHERE LOWER(p.name) = LOWER(?)
            GROUP BY p.id
            """,
            (name,),
        ).fetchone()
        return dict(row) if row else None

    def get_playlist_by_id(self, playlist_id: int) -> dict | None:
        """Get a single playlist by its id."""
        conn = self.connection
        row = conn.execute(
            """
            SELECT p.*, COUNT(ps.id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_songs ps ON ps.playlist_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (playlist_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_next_position(self, playlist_id: int) -> int:
        """Get the next available position in a playlist."""
        conn = self.connection
        row = conn.execute(
            "SELECT MAX(position) AS max_pos FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        max_pos = row["max_pos"] if row and row["max_pos"] is not None else -1
        return max_pos + 1

    def reorder_track(self, playlist_id: int, track_id: int, new_position: int) -> bool:
        """Move a track to a new position within a playlist.

        Shifts other tracks to maintain contiguous ordering.
        Returns True if the track was moved, False if not found.
        """
        conn = self.connection
        # Verify the track is in the playlist
        row = conn.execute(
            "SELECT position FROM playlist_songs WHERE playlist_id = ? AND library_id = ?",
            (playlist_id, track_id),
        ).fetchone()
        if row is None:
            return False

        old_position = row["position"]
        if old_position == new_position:
            return True

        # Clamp new_position to valid range
        max_row = conn.execute(
            "SELECT MAX(position) AS max_pos FROM playlist_songs WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        max_pos = max_row["max_pos"] if max_row and max_row["max_pos"] is not None else 0
        new_position = max(0, min(new_position, max_pos))

        if old_position == new_position:
            return True

        # Shift positions of tracks between old and new position
        if old_position < new_position:
            conn.execute(
                """
                UPDATE playlist_songs
                SET position = position - 1
                WHERE playlist_id = ? AND position > ? AND position <= ?
                """,
                (playlist_id, old_position, new_position),
            )
        else:
            conn.execute(
                """
                UPDATE playlist_songs
                SET position = position + 1
                WHERE playlist_id = ? AND position >= ? AND position < ?
                """,
                (playlist_id, new_position, old_position),
            )

        # Place the track at the new position
        conn.execute(
            "UPDATE playlist_songs SET position = ? WHERE playlist_id = ? AND library_id = ?",
            (new_position, playlist_id, track_id),
        )
        conn.execute(
            "UPDATE playlists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (playlist_id,),
        )
        conn.commit()
        return True

    def list_playlists(self) -> list[dict]:
        """List all playlists with their track counts."""
        conn = self.connection
        rows = conn.execute("""
            SELECT p.*, COUNT(ps.id) AS track_count
            FROM playlists p
            LEFT JOIN playlist_songs ps ON ps.playlist_id = p.id
            GROUP BY p.id
            ORDER BY p.name
            """).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Singleton Access
# =============================================================================

_repo: LocalLibraryRepo | None = None
_repo_lock = threading.Lock()


def _atexit_close_repo() -> None:
    """Atexit handler: close all local library DB connections."""
    if _repo is not None:
        try:
            _repo.close_all()
        except Exception:
            pass


def get_local_library_repo() -> LocalLibraryRepo:
    """Get or create the global local library repository singleton."""
    global _repo
    with _repo_lock:
        if _repo is None:
            from core.platform import get_data_dir

            db_path = get_data_dir() / "local_library.db"
            _repo = LocalLibraryRepo(db_path)
            atexit.register(_atexit_close_repo)
        return _repo
