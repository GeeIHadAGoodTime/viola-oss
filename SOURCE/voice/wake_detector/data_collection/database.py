"""SQLite database for wake word data collection.

Thread-safe implementation following auth/database.py pattern:
- Thread-local connections via threading.local()
- WAL mode, foreign keys ON, sqlite3.Row factory
- Singleton via get_data_collection_db()

DB path: {settings.data_dir}/wake_data.db
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    audio_path TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    confidence_score REAL NOT NULL,
    classification TEXT NOT NULL,
    classification_method TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    review_result TEXT,
    uploaded INTEGER NOT NULL DEFAULT 0,
    upload_batch_id TEXT,
    anonymized INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clips_classification ON clips(classification);
CREATE INDEX IF NOT EXISTS idx_clips_uploaded ON clips(uploaded);
CREATE INDEX IF NOT EXISTS idx_clips_reviewed ON clips(reviewed);
CREATE INDEX IF NOT EXISTS idx_clips_timestamp ON clips(timestamp);
"""


class DataCollectionDB:
    """SQLite database for wake word clip management."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._initialized = False

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.connection = conn
        return self._local.connection

    @property
    def connection(self) -> sqlite3.Connection:
        return self._get_connection()

    def initialize(self) -> None:
        if self._initialized:
            return
        conn = self._get_connection()
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate_schema(conn)
        self._initialized = True
        logger.info("Data collection DB initialized at %s", self.db_path)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Add columns that may not exist in older databases."""
        rows = conn.execute("PRAGMA table_info(clips)").fetchall()
        existing = {row["name"] for row in rows}

        migrations: list[str] = []
        if "validation_status" not in existing:
            migrations.append("ALTER TABLE clips ADD COLUMN validation_status TEXT NOT NULL DEFAULT 'pending'")
        if "validation_failure_reason" not in existing:
            migrations.append("ALTER TABLE clips ADD COLUMN validation_failure_reason TEXT")
        if "upload_capped" not in existing:
            migrations.append("ALTER TABLE clips ADD COLUMN upload_capped INTEGER NOT NULL DEFAULT 0")

        for stmt in migrations:
            conn.execute(stmt)

        if migrations:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_validation " "ON clips(validation_status)")
            conn.commit()
            logger.info("Migrated %d new columns into clips table", len(migrations))

    def insert_clip(
        self,
        *,
        timestamp: float,
        audio_path: str,
        duration_ms: int,
        confidence_score: float,
        classification: str,
        classification_method: str,
        content_hash: str | None = None,
    ) -> int:
        conn = self.connection
        cursor = conn.execute(
            """
            INSERT INTO clips
            (timestamp, audio_path, duration_ms, confidence_score,
             classification, classification_method, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                audio_path,
                duration_ms,
                confidence_score,
                classification,
                classification_method,
                content_hash,
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def update_classification(
        self,
        clip_id: int,
        classification: str,
        classification_method: str,
    ) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET classification = ?, classification_method = ? WHERE id = ?",
            (classification, classification_method, clip_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def mark_reviewed(
        self,
        clip_id: int,
        review_result: str,
        new_classification: str | None = None,
    ) -> bool:
        conn = self.connection
        if new_classification:
            cursor = conn.execute(
                "UPDATE clips SET reviewed = 1, review_result = ?, classification = ?, "
                "classification_method = 'manual_review' WHERE id = ?",
                (review_result, new_classification, clip_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE clips SET reviewed = 1, review_result = ? WHERE id = ?",
                (review_result, clip_id),
            )
        conn.commit()
        return cursor.rowcount > 0

    def mark_uploaded(self, clip_id: int, batch_id: str) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET uploaded = 1, upload_batch_id = ? WHERE id = ?",
            (batch_id, clip_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def mark_anonymized(self, clip_id: int, content_hash: str) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET anonymized = 1, content_hash = ? WHERE id = ?",
            (content_hash, clip_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def get_clip(self, clip_id: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return dict(row) if row else None

    def get_clips(
        self,
        *,
        classification: str | None = None,
        reviewed: bool | None = None,
        uploaded: bool | None = None,
        anonymized: bool | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        min_timestamp: float | None = None,
        max_timestamp: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[object] = []

        if classification is not None:
            conditions.append("classification = ?")
            params.append(classification)
        if reviewed is not None:
            conditions.append("reviewed = ?")
            params.append(int(reviewed))
        if uploaded is not None:
            conditions.append("uploaded = ?")
            params.append(int(uploaded))
        if anonymized is not None:
            conditions.append("anonymized = ?")
            params.append(int(anonymized))
        if min_score is not None:
            conditions.append("confidence_score >= ?")
            params.append(min_score)
        if max_score is not None:
            conditions.append("confidence_score <= ?")
            params.append(max_score)
        if min_timestamp is not None:
            conditions.append("timestamp >= ?")
            params.append(min_timestamp)
        if max_timestamp is not None:
            conditions.append("timestamp <= ?")
            params.append(max_timestamp)

        where = " AND ".join(conditions) if conditions else "1=1"
        query = (
            f"SELECT * FROM clips WHERE {where} "  # nosec B608
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        rows = self.connection.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        conn = self.connection
        total = conn.execute("SELECT COUNT(*) as c FROM clips").fetchone()["c"]
        by_class = {}
        for row in conn.execute("SELECT classification, COUNT(*) as c FROM clips GROUP BY classification").fetchall():
            by_class[row["classification"]] = row["c"]

        unreviewed = conn.execute(
            "SELECT COUNT(*) as c FROM clips WHERE reviewed = 0 AND classification != 'near_miss'"
        ).fetchone()["c"]
        uploaded_count = conn.execute("SELECT COUNT(*) as c FROM clips WHERE uploaded = 1").fetchone()["c"]

        return {
            "total": total,
            "by_classification": by_class,
            "unreviewed": unreviewed,
            "uploaded": uploaded_count,
        }

    def get_daily_trends(self, days: int = 30) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT date(created_at) as day, classification, COUNT(*) as count
            FROM clips
            WHERE created_at >= datetime('now', ?)
            GROUP BY day, classification
            ORDER BY day DESC
            """,
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    def hash_exists(self, content_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM clips WHERE content_hash = ? AND uploaded = 1 LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row is not None

    def get_clips_for_upload(self, limit: int = 10) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE anonymized = 1 AND uploaded = 0 " "ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_clips_for_anonymization(self, limit: int = 50) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE anonymized = 0 AND validation_status = 'passed' "
            "AND classification IN ('false_positive', 'true_positive') "
            "ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_clip(self, clip_id: int) -> bool:
        conn = self.connection
        cursor = conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        conn.commit()
        return cursor.rowcount > 0

    def delete_clips_by_ids(self, clip_ids: list[int]) -> int:
        if not clip_ids:
            return 0
        conn = self.connection
        placeholders = ",".join("?" for _ in clip_ids)
        cursor = conn.execute(
            f"DELETE FROM clips WHERE id IN ({placeholders})",  # nosec B608
            clip_ids,
        )
        conn.commit()
        return cursor.rowcount

    def delete_all_clips(self) -> int:
        """Delete all clips from the database. Used for GDPR data purge."""
        conn = self.connection
        cursor = conn.execute("DELETE FROM clips")
        conn.commit()
        count = cursor.rowcount
        logger.info("Deleted all clips from database: %d rows", count)
        return count

    def get_oldest_near_misses(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE classification = 'near_miss' " "ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_old_uploaded_triggers(self, before_timestamp: float) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE uploaded = 1 AND timestamp < ? " "AND classification != 'near_miss'",
            (before_timestamp,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_clips_for_validation(self, limit: int = 50) -> list[dict]:
        """Get clips with validation_status='pending' for pre-upload validation."""
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE validation_status = 'pending' "
            "AND classification IN ('false_positive', 'true_positive') "
            "ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_validation_passed(self, clip_id: int) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET validation_status = 'passed' WHERE id = ?",
            (clip_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    def mark_validation_failed(self, clip_id: int, reason: str) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET validation_status = 'failed', " "validation_failure_reason = ? WHERE id = ?",
            (reason, clip_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def mark_upload_capped(self, clip_id: int) -> bool:
        conn = self.connection
        cursor = conn.execute(
            "UPDATE clips SET upload_capped = 1 WHERE id = ?",
            (clip_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    def count_uploads_today(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) as c FROM clips WHERE uploaded = 1 " "AND date(created_at) = date('now')"
        ).fetchone()
        return row["c"]

    def get_capped_clips(self, limit: int = 50) -> list[dict]:
        """Get clips that were capped in a previous cycle (FIFO carry-over)."""
        rows = self.connection.execute(
            "SELECT * FROM clips WHERE upload_capped = 1 AND uploaded = 0 "
            "AND anonymized = 1 ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


_db: DataCollectionDB | None = None
_db_lock = threading.Lock()


def get_data_collection_db() -> DataCollectionDB:
    global _db
    with _db_lock:
        if _db is None:
            from config.settings import settings

            db_path = Path(settings.data_dir) / "wake_data.db"
            _db = DataCollectionDB(db_path)
            _db.initialize()
        return _db
