"""SQLite backend for PersistentStateStore."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import cast

from core.logging_config import get_logger
from core.platform import get_data_dir
from services.persistence.backends.protocol import (
    DB_FILENAME,
    MUSIC_STATE_KEYS,
    SCHEMA_VERSION,
    SYSTEM_USER_ID,
    PersistenceBackend,
    deserialize_value,
    require_user_id,
    serialize_value,
)

logger = get_logger(__name__)


class SqliteBackend(PersistenceBackend):
    """SQLite implementation of the state-store backend contract."""

    backend_name = "sqlite"
    is_postgres = False

    def __init__(self, root: Path | None = None, *, schema_lock: threading.Lock | None = None) -> None:
        self._root = Path(root) if root is not None else get_data_dir()
        self._db_path = self._root / "data" / "persistence" / DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = schema_lock or threading.Lock()
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = self._connect_sqlite()
        self.initialize()

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @property
    def sqlite_connection(self) -> sqlite3.Connection | None:
        return self._conn

    def replace_sqlite_connection_for_tests(self, conn: object | None) -> None:
        if conn is not None and not isinstance(conn, sqlite3.Connection):
            raise TypeError("conn must be sqlite3.Connection or None")
        self._conn = conn

    def initialize(self) -> None:
        self._ensure_schema(skip_lock=True)

    def close(self) -> None:
        """Checkpoint WAL and close the SQLite connection. Idempotent."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                logger.debug("WAL checkpoint failed on state_store close, connection may already be closed")
            try:
                conn.close()
                self._conn = None
            except sqlite3.Error as exc:
                logger.debug("PersistentStateStore close() failed: %s", exc)

    def _connect_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_connection(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is not None:
            return conn
        conn = self._connect_sqlite()
        self._conn = conn
        self._ensure_schema(skip_lock=True)
        return conn

    def _ensure_schema(self, skip_lock: bool = False) -> None:
        """Create tables and verify schema version."""
        schema_lock = nullcontext() if skip_lock else self._schema_lock
        conn = self._ensure_connection()
        with schema_lock, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL
                )
                """)
            current = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
            if current is None:
                conn.execute(
                    "INSERT INTO schema_version(id, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif current["version"] > SCHEMA_VERSION:
                logger.warning(
                    "Persistent state schema newer than supported (db=%s, supported=%s)",
                    current["version"],
                    SCHEMA_VERSION,
                )
            elif current["version"] < SCHEMA_VERSION:
                conn.execute(
                    "UPDATE schema_version SET version = ? WHERE id = 1",
                    (SCHEMA_VERSION,),
                )

            self._create_metadata_schema(conn)
            self._create_token_metadata_schema(conn)
            self._ensure_queue_items_schema(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calibration_profiles (
                    user_id TEXT NOT NULL DEFAULT '__system__',
                    name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (user_id, name)
                )
                """)
            self._migrate_calibration_profiles(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS restart_counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT '__system__',
                    snapshot_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """)
            self._migrate_snapshots(conn)

    @staticmethod
    def _create_metadata_schema(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metadata_user_id
            ON metadata(user_id)
            """)

    @staticmethod
    def _create_token_metadata_schema(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_metadata (
                user_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, provider_id)
            )
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_metadata_user_id
            ON token_metadata(user_id)
            """)

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
        rows = conn.execute("PRAGMA table_info(%s)" % table_name).fetchall()  # nosec B608
        return [str(row["name"]) for row in rows]

    @staticmethod
    def _table_info(conn: sqlite3.Connection, table_name: str) -> list[sqlite3.Row]:
        return conn.execute("PRAGMA table_info(%s)" % table_name).fetchall()  # nosec B608

    @staticmethod
    def _pk_columns(table_info: Sequence[sqlite3.Row]) -> list[str]:
        keyed = sorted(
            (row for row in table_info if int(row["pk"]) > 0),
            key=lambda row: int(row["pk"]),
        )
        return [str(row["name"]) for row in keyed]

    def _ensure_queue_items_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue_items (
                user_id TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, position)
            )
            """)
        table_info = self._table_info(conn, "queue_items")
        columns = [str(row["name"]) for row in table_info]
        if columns == ["user_id", "position", "payload", "updated_at"] and self._pk_columns(table_info) == [
            "user_id",
            "position",
        ]:
            return

        logger.info("Migrating queue_items schema for per-user queue isolation")
        conn.execute("DROP TABLE IF EXISTS queue_items_v2")
        conn.execute("""
            CREATE TABLE queue_items_v2 (
                user_id TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, position)
            )
            """)
        if "user_id" in columns:
            try:
                from core.user_context import get_device_user_id

                # mt-ok: schema migration assigns legacy NULL user_id rows to
                # the single desktop owner — pre-multi-user data.
                fallback_user_id = get_device_user_id()
            except (ImportError, RuntimeError, ValueError):
                fallback_user_id = ""
            conn.execute(
                """
                INSERT INTO queue_items_v2(user_id, position, payload, updated_at)
                SELECT CASE WHEN user_id IS NULL OR user_id = '' THEN ? ELSE user_id END,
                       position, payload, updated_at
                FROM queue_items
                """,
                (fallback_user_id,),
            )
        else:
            try:
                from core.user_context import get_device_user_id

                # mt-ok: schema migration assigns legacy NULL user_id rows to
                # the single desktop owner — pre-multi-user data.
                device_user_id = get_device_user_id()
            except (ImportError, RuntimeError, ValueError):
                device_user_id = ""
            conn.execute(
                """
                INSERT INTO queue_items_v2(user_id, position, payload, updated_at)
                SELECT ?, position, payload, updated_at
                FROM queue_items
                """,
                (device_user_id,),
            )
        conn.execute("DROP TABLE queue_items")
        conn.execute("ALTER TABLE queue_items_v2 RENAME TO queue_items")

    def _migrate_calibration_profiles(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "calibration_profiles")
        if "user_id" in columns:
            return
        logger.info("Migrating calibration_profiles schema: adding user_id column")
        conn.execute("DROP TABLE IF EXISTS calibration_profiles_v2")
        conn.execute("""
            CREATE TABLE calibration_profiles_v2 (
                user_id TEXT NOT NULL DEFAULT '__system__',
                name TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, name)
            )
            """)
        conn.execute("""
            INSERT INTO calibration_profiles_v2(user_id, name, payload, updated_at)
            SELECT '__system__', name, payload, updated_at
            FROM calibration_profiles
            """)
        conn.execute("DROP TABLE calibration_profiles")
        conn.execute("ALTER TABLE calibration_profiles_v2 RENAME TO calibration_profiles")

    def _migrate_snapshots(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "snapshots")
        if "user_id" in columns:
            return
        logger.info("Migrating snapshots schema: adding user_id column")
        conn.execute("DROP TABLE IF EXISTS snapshots_v2")
        conn.execute("""
            CREATE TABLE snapshots_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL DEFAULT '__system__',
                snapshot_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """)
        conn.execute("""
            INSERT INTO snapshots_v2(id, user_id, snapshot_type, payload, version, created_at)
            SELECT id, '__system__', snapshot_type, payload, version, created_at
            FROM snapshots
            """)
        conn.execute("DROP TABLE snapshots")
        conn.execute("ALTER TABLE snapshots_v2 RENAME TO snapshots")

    def _upsert_metadata(self, conn: sqlite3.Connection, user_id: str, key: str, value: object, ts: float) -> None:
        conn.execute(
            """
            INSERT INTO metadata (user_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (user_id, key, serialize_value(value), ts),
        )

    def _fetch_metadata(self, conn: sqlite3.Connection, user_id: str, keys: Iterable[str]) -> dict[str, object]:
        key_tuple = tuple(keys)
        placeholders = ",".join("?" for _ in key_tuple)
        if not placeholders:
            return {}
        rows = conn.execute(
            "SELECT key, value FROM metadata WHERE user_id = ? AND key IN (%s)" % placeholders,  # nosec B608
            (user_id, *key_tuple),
        ).fetchall()
        return {str(row["key"]): deserialize_value(row["value"]) for row in rows}

    def get_setting(self, user_id: str, key: str, *, default: object = None) -> object:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            result = self._fetch_metadata(self._ensure_connection(), resolved_user_id, [key])
        return result.get(key, default)

    def set_setting(self, user_id: str, key: str, value: object) -> None:
        resolved_user_id = require_user_id(user_id)
        ts = time.time()
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                self._upsert_metadata(conn, resolved_user_id, key, value, ts)

    def delete_setting(self, user_id: str, key: str) -> bool:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                cursor = conn.execute(
                    "DELETE FROM metadata WHERE user_id = ? AND key = ?",
                    (resolved_user_id, key),
                )
        return cursor.rowcount > 0

    def save_music_state(
        self,
        *,
        user_id: str,
        queue: Sequence[dict[str, object]],
        now_playing: dict[str, object] | None,
        volume: int,
        is_playing: bool,
    ) -> None:
        resolved_user_id = require_user_id(user_id)
        ts = time.time()
        payloads = [serialize_value(item) for item in queue]
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute("DELETE FROM queue_items WHERE user_id = ?", (resolved_user_id,))
                for idx, payload in enumerate(payloads):
                    conn.execute(
                        """
                        INSERT INTO queue_items(user_id, position, payload, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (resolved_user_id, idx, payload, ts),
                    )
                self._upsert_metadata(conn, resolved_user_id, "music.volume", int(volume), ts)
                self._upsert_metadata(conn, resolved_user_id, "music.is_playing", bool(is_playing), ts)
                if now_playing is None:
                    conn.execute(
                        "DELETE FROM metadata WHERE user_id = ? AND key = 'music.now_playing'",
                        (resolved_user_id,),
                    )
                else:
                    self._upsert_metadata(conn, resolved_user_id, "music.now_playing", now_playing, ts)

    def load_music_state(self, user_id: str) -> dict[str, object]:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            rows = conn.execute(
                """
                SELECT payload
                FROM queue_items
                WHERE user_id = ?
                ORDER BY position ASC
                """,
                (resolved_user_id,),
            ).fetchall()
            queue = [deserialize_value(row["payload"]) for row in rows]
            meta = self._fetch_metadata(conn, resolved_user_id, MUSIC_STATE_KEYS)
        return {
            "queue": queue,
            "now_playing": meta.get("music.now_playing"),
            "volume": meta.get("music.volume"),
            "is_playing": meta.get("music.is_playing", False),
        }

    def clear_stale_queue(self, user_id: str) -> int:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM queue_items WHERE user_id = ?",
                    (resolved_user_id,),
                ).fetchone()
                count = int(count_row["cnt"]) if count_row else 0

                if count > 0:
                    conn.execute("DELETE FROM queue_items WHERE user_id = ?", (resolved_user_id,))
                    logger.info("Cleared %d stale queue items from persistence for user=%s", count, resolved_user_id)

                conn.execute(
                    "DELETE FROM metadata WHERE user_id = ? AND key = 'music.now_playing'",
                    (resolved_user_id,),
                )
                conn.execute(
                    "DELETE FROM metadata WHERE user_id = ? AND key = 'music.is_playing'",
                    (resolved_user_id,),
                )
                return count

    def update_restart_counter(self, name: str, value: int) -> None:
        ts = time.time()
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO restart_counters(name, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name)
                    DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (name, int(value), ts),
                )

    def load_restart_counters(self) -> dict[str, int]:
        with self._lock:
            rows = self._ensure_connection().execute("SELECT name, value FROM restart_counters").fetchall()
        return {str(row["name"]): int(row["value"]) for row in rows}

    def save_calibration(self, profile: str, payload: dict[str, object], *, user_id: str = SYSTEM_USER_ID) -> None:
        resolved_user_id = require_user_id(user_id)
        ts = time.time()
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO calibration_profiles(user_id, name, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, name)
                    DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                    """,
                    (resolved_user_id, profile.lower(), serialize_value(payload), ts),
                )

    def load_calibration(self, profile: str, *, user_id: str = SYSTEM_USER_ID) -> dict[str, object] | None:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            row = (
                self._ensure_connection()
                .execute(
                    "SELECT payload FROM calibration_profiles WHERE user_id = ? AND name = ?",
                    (resolved_user_id, profile.lower()),
                )
                .fetchone()
            )
        if row is None:
            return None
        payload = deserialize_value(row["payload"])
        return cast(dict[str, object], payload) if isinstance(payload, dict) else None

    def load_all_calibrations(self, *, user_id: str = SYSTEM_USER_ID) -> dict[str, dict[str, object]]:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            rows = (
                self._ensure_connection()
                .execute(
                    "SELECT name, payload FROM calibration_profiles WHERE user_id = ?",
                    (resolved_user_id,),
                )
                .fetchall()
            )
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = deserialize_value(row["payload"])
            if isinstance(payload, dict):
                result[str(row["name"])] = cast(dict[str, object], payload)
        return result

    def upsert_token_metadata(self, user_id: str, provider_id: str, payload: dict[str, object]) -> None:
        resolved_user_id = require_user_id(user_id)
        ts = time.time()
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO token_metadata(user_id, provider_id, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, provider_id)
                    DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                    """,
                    (resolved_user_id, provider_id.lower(), serialize_value(payload), ts),
                )

    def delete_token_metadata(self, user_id: str, provider_id: str) -> bool:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                cursor = conn.execute(
                    "DELETE FROM token_metadata WHERE user_id = ? AND provider_id = ?",
                    (resolved_user_id, provider_id.lower()),
                )
        return cursor.rowcount > 0

    def delete_all_token_metadata(self, user_id: str) -> int:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                cursor = conn.execute("DELETE FROM token_metadata WHERE user_id = ?", (resolved_user_id,))
        return int(cursor.rowcount)

    def load_token_metadata(self, user_id: str) -> dict[str, dict[str, object]]:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            rows = (
                self._ensure_connection()
                .execute(
                    "SELECT provider_id, payload FROM token_metadata WHERE user_id = ?",
                    (resolved_user_id,),
                )
                .fetchall()
            )
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = deserialize_value(row["payload"])
            if isinstance(payload, dict):
                result[str(row["provider_id"])] = cast(dict[str, object], payload)
        return result

    def record_snapshot(self, *, user_id: str, snapshot_type: str, serialized: str, ts: float) -> None:
        resolved_user_id = require_user_id(user_id)
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute(
                    """
                    INSERT INTO snapshots(user_id, snapshot_type, payload, version, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (resolved_user_id, snapshot_type, serialized, SCHEMA_VERSION, ts),
                )

    def list_snapshots(self, limit: int = 10, *, user_id: str | None = None) -> list[dict[str, object]]:
        # Multi-tenant: refuse a global list — callers must scope to a
        # single tenant.  Diagnostic tools should pass the system user
        # id for system-owned snapshots.
        if user_id is None:
            return []
        with self._lock:
            conn = self._ensure_connection()
            resolved_user_id = require_user_id(user_id)
            rows = conn.execute(
                """
                SELECT snapshot_type, payload, created_at
                FROM snapshots
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (resolved_user_id, max(1, int(limit))),
            ).fetchall()
        return [
            {
                "snapshot_type": row["snapshot_type"],
                "created_at": row["created_at"],
                "payload": deserialize_value(row["payload"]),
            }
            for row in rows
        ]

    def cleanup_self_check(
        self,
        *,
        user_id: str,
        restart_counter: str,
        calibration_profile: str,
        snapshot_type: str,
    ) -> None:
        with self._lock:
            conn = self._ensure_connection()
            with conn:
                conn.execute("DELETE FROM metadata WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM queue_items WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM token_metadata WHERE user_id = ?", (user_id,))
                conn.execute(
                    "DELETE FROM calibration_profiles WHERE user_id = ? AND name = ?",
                    (user_id, calibration_profile.lower()),
                )
                conn.execute("DELETE FROM restart_counters WHERE name = ?", (restart_counter,))
                conn.execute(
                    "DELETE FROM snapshots WHERE user_id = ? AND snapshot_type = ?",
                    (user_id, snapshot_type),
                )
