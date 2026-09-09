"""Bug ticket persistence — SQLite-backed issue tracking.

Stores structured bug tickets from self-diagnosis results. Each ticket
captures the failure context, diagnosis, and dedup fingerprint.

Key properties:
- SQLite DB created lazily (never on import)
- Dedup via SHA-256 fingerprint of (error_type, error_message, execution_stage)
- Duplicate tickets increment hit_count instead of creating new rows
- Table auto-created on first write
- No new pip dependencies (stdlib sqlite3, hashlib, json, datetime)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from diagnostics.support_redaction import redact_support_payload, redact_support_text

logger = get_logger(__name__)

_DEFAULT_DB_NAME = "bug_tickets.db"
_TABLE_NAME = "bug_tickets"
_MAX_TICKETS = 500  # Cap total tickets to prevent unbounded growth


@dataclass
class BugTicket:
    """A persisted bug ticket."""

    id: int
    user_id: str
    fingerprint: str
    created_at: str
    updated_at: str
    hit_count: int
    status: str  # open | acknowledged | resolved | wontfix
    error_type: str | None
    error_message: str | None
    execution_stage: str | None
    user_request: str | None
    root_cause: str | None
    category: str | None
    severity: str | None
    user_explanation: str | None
    developer_detail: str | None
    is_transient: bool
    diagnosis_succeeded: bool
    context_json: str | None  # Full DiagnosticContext serialized

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API responses."""
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "hit_count": self.hit_count,
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "execution_stage": self.execution_stage,
            "user_request": self.user_request,
            "root_cause": self.root_cause,
            "category": self.category,
            "severity": self.severity,
            "user_explanation": self.user_explanation,
            "developer_detail": self.developer_detail,
            "is_transient": self.is_transient,
            "diagnosis_succeeded": self.diagnosis_succeeded,
        }


def _compute_fingerprint(
    error_type: str | None,
    error_message: str | None,
    execution_stage: str | None,
) -> str:
    """Compute a dedup fingerprint for a bug ticket.

    Uses SHA-256 of (error_type, error_message_prefix, execution_stage).
    The error_message is truncated to the first 200 chars to group
    similar errors that differ only in details.
    """
    parts = [
        error_type or "",
        (error_message or "")[:200],
        execution_stage or "",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _require_user_id(user_id: str | None) -> str:
    normalized = str(user_id or "").strip()
    if not normalized:
        raise ValueError("Bug tickets require a concrete user_id")
    return normalized


class BugTicketStore:
    """SQLite-backed bug ticket storage.

    The database and table are created lazily on first write.
    Thread-safe via SQLite's built-in locking.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Initialize the store.

        Args:
            db_path: Path to SQLite database file. If None, uses
                     the default location in the data directory.
        """
        if db_path is None:
            from core.platform import get_data_dir

            db_path = get_data_dir() / _DEFAULT_DB_NAME
        self._db_path = Path(db_path)
        self._initialized = False

    def _ensure_table(self) -> None:
        """Create the bug_tickets table if it doesn't exist."""
        if self._initialized:
            return

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS %s (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    hit_count INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'open',
                    error_type TEXT,
                    error_message TEXT,
                    execution_stage TEXT,
                    user_request TEXT,
                    root_cause TEXT,
                    category TEXT,
                    severity TEXT,
                    user_explanation TEXT,
                    developer_detail TEXT,
                    is_transient INTEGER DEFAULT 0,
                    diagnosis_succeeded INTEGER DEFAULT 1,
                    context_json TEXT
                )
            """ % _TABLE_NAME)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % _TABLE_NAME).fetchall()}
            if "user_id" not in columns:
                conn.execute("ALTER TABLE %s ADD COLUMN user_id TEXT" % _TABLE_NAME)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_fingerprint
                ON %s (fingerprint)
            """ % _TABLE_NAME)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_fingerprint
                ON %s (user_id, fingerprint)
            """ % _TABLE_NAME)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_status
                ON %s (user_id, status)
            """ % _TABLE_NAME)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status
                ON %s (status)
            """ % _TABLE_NAME)
            conn.commit()
        finally:
            conn.close()
        self._initialized = True

    def file_ticket(
        self,
        context: Any,  # DiagnosticContext
        diagnosis: Any,  # DiagnosisResult
        *,
        user_id: str,
    ) -> BugTicket:
        """File a new bug ticket or increment an existing one.

        Args:
            context: DiagnosticContext with failure details.
            diagnosis: DiagnosisResult from self-diagnosis engine.

        Returns:
            The created or updated BugTicket.
        """
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()

        # A user-authored support/bug-report body is a deliberate message to our own
        # support channel: store it VERBATIM. Auto-captured agent-diagnostic context
        # (the default) stays redacted -- those payloads capture things the user never
        # chose to share. The distinction rides on the DiagnosticContext.user_authored
        # flag the bug-report intake path sets.
        user_authored = bool(getattr(context, "user_authored", False))

        def _maybe_redact_text(value: str | None) -> str | None:
            return value if user_authored else redact_support_text(value)

        error_type = getattr(context, "error_type", None)
        error_message = _maybe_redact_text(getattr(context, "error_message", None))
        execution_stage = getattr(context, "execution_stage", None)
        fingerprint = _compute_fingerprint(error_type, error_message, execution_stage)

        now = datetime.now(tz=UTC).isoformat()

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Check for existing ticket with same fingerprint
            existing = conn.execute(
                "SELECT * FROM %s WHERE user_id = ? AND fingerprint = ?" % _TABLE_NAME,
                (owner_user_id, fingerprint),
            ).fetchone()

            if existing:
                # Dedup: increment hit_count, update timestamp
                conn.execute(
                    "UPDATE %s SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?" % _TABLE_NAME,
                    (now, existing["id"]),
                )
                conn.commit()

                # Re-read to get updated values
                row = conn.execute(
                    "SELECT * FROM %s WHERE id = ?" % _TABLE_NAME,
                    (existing["id"],),
                ).fetchone()
                return self._row_to_ticket(row)

            # Serialize full context
            context_json = None
            if hasattr(context, "to_dict"):
                try:
                    raw_payload = context.to_dict()
                    redacted_payload = redact_support_payload(raw_payload)
                    if user_authored and isinstance(redacted_payload, dict) and isinstance(raw_payload, dict):
                        # Keep the user-authored body fields verbatim; the rest of the
                        # snapshot (auto-captured system_state / trace context /
                        # settings) stays redacted.
                        for verbatim_key in ("user_request", "error_message"):
                            if verbatim_key in raw_payload:
                                redacted_payload[verbatim_key] = raw_payload[verbatim_key]
                    context_json = json.dumps(redacted_payload, default=str)
                except Exception:
                    logger.debug("Context serialization failed", exc_info=True)

            # Insert new ticket
            conn.execute(
                """INSERT INTO %s (
                    user_id, fingerprint, created_at, updated_at, status,
                    error_type, error_message, execution_stage, user_request,
                    root_cause, category, severity, user_explanation, developer_detail,
                    is_transient, diagnosis_succeeded, context_json
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """ % _TABLE_NAME,
                (
                    owner_user_id,
                    fingerprint,
                    now,
                    now,
                    error_type,
                    (error_message or "")[:1000],
                    execution_stage,
                    _maybe_redact_text(getattr(context, "user_request", None)),
                    redact_support_text(getattr(diagnosis, "root_cause", None)),
                    getattr(diagnosis, "category", None),
                    getattr(diagnosis, "severity", None),
                    redact_support_text(getattr(diagnosis, "user_explanation", None)),
                    redact_support_text(getattr(diagnosis, "developer_detail", None)),
                    1 if getattr(diagnosis, "is_transient", False) else 0,
                    1 if getattr(diagnosis, "diagnosis_succeeded", False) else 0,
                    context_json,
                ),
            )
            conn.commit()

            # Prune old tickets if over cap
            self._prune(conn, owner_user_id)

            # Read back the inserted ticket
            row = conn.execute(
                "SELECT * FROM %s WHERE user_id = ? AND fingerprint = ? ORDER BY id DESC LIMIT 1" % _TABLE_NAME,
                (owner_user_id, fingerprint),
            ).fetchone()
            return self._row_to_ticket(row)
        finally:
            conn.close()

    def get_ticket(self, ticket_id: int, *, user_id: str) -> BugTicket | None:
        """Get a ticket by ID."""
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM %s WHERE id = ? AND user_id = ?" % _TABLE_NAME,
                (ticket_id, owner_user_id),
            ).fetchone()
            return self._row_to_ticket(row) if row else None
        finally:
            conn.close()

    def list_tickets(
        self,
        *,
        user_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BugTicket]:
        """List tickets, optionally filtered by status.

        Args:
            status: Filter by status (open, acknowledged, resolved, wontfix).
            limit: Max tickets to return.
            offset: Pagination offset.

        Returns:
            List of BugTicket objects, most recent first.
        """
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM %s WHERE user_id = ? AND status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?"
                    % _TABLE_NAME,
                    (owner_user_id, status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM %s WHERE user_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?" % _TABLE_NAME,
                    (owner_user_id, limit, offset),
                ).fetchall()
            return [self._row_to_ticket(r) for r in rows]
        finally:
            conn.close()

    def update_status(self, ticket_id: int, status: str, *, user_id: str) -> bool:
        """Update a ticket's status.

        Args:
            ticket_id: The ticket ID.
            status: New status (open, acknowledged, resolved, wontfix).

        Returns:
            True if the ticket was found and updated.
        """
        valid_statuses = {"open", "acknowledged", "resolved", "wontfix"}
        if status not in valid_statuses:
            return False

        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        try:
            now = datetime.now(tz=UTC).isoformat()
            cursor = conn.execute(
                "UPDATE %s SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?" % _TABLE_NAME,
                (status, now, ticket_id, owner_user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_stats(self, *, user_id: str) -> dict[str, Any]:
        """Get aggregate stats about tickets."""
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE user_id = ?" % _TABLE_NAME, (owner_user_id,)
            ).fetchone()[0]

            by_status = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM %s WHERE user_id = ? GROUP BY status" % _TABLE_NAME,
                (owner_user_id,),
            ).fetchall():
                by_status[row[0]] = row[1]

            by_category = {}
            for row in conn.execute(
                "SELECT category, COUNT(*) FROM %s WHERE user_id = ? GROUP BY category" % _TABLE_NAME,
                (owner_user_id,),
            ).fetchall():  # nosec B608
                by_category[row[0] or "unknown"] = row[1]

            by_severity = {}
            for row in conn.execute(
                "SELECT severity, COUNT(*) FROM %s WHERE user_id = ? GROUP BY severity" % _TABLE_NAME,
                (owner_user_id,),
            ).fetchall():  # nosec B608
                by_severity[row[0] or "unknown"] = row[1]

            total_hits = conn.execute(
                "SELECT COALESCE(SUM(hit_count), 0) FROM %s WHERE user_id = ?" % _TABLE_NAME,
                (owner_user_id,),
            ).fetchone()[
                0
            ]  # nosec B608

            return {
                "total_tickets": total,
                "total_hits": total_hits,
                "by_status": by_status,
                "by_category": by_category,
                "by_severity": by_severity,
            }
        finally:
            conn.close()

    def export_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Export all bug tickets for a user for data portability."""
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM %s WHERE user_id = ? ORDER BY updated_at DESC" % _TABLE_NAME,
                (owner_user_id,),
            ).fetchall()
            exported: list[dict[str, Any]] = []
            for row in rows:
                ticket = self._row_to_ticket(row)
                payload = ticket.to_dict()
                payload["context"] = _parse_context_json(ticket.context_json)
                exported.append(payload)
            return exported
        finally:
            conn.close()

    def delete_all_for_user(self, user_id: str) -> int:
        """Delete all bug tickets for a user and return the removed row count."""
        owner_user_id = _require_user_id(user_id)
        self._ensure_table()
        conn = sqlite3.connect(str(self._db_path))
        try:
            cursor = conn.execute(
                "DELETE FROM %s WHERE user_id = ?" % _TABLE_NAME,
                (owner_user_id,),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def _prune(self, conn: sqlite3.Connection, user_id: str) -> None:
        """Remove oldest resolved/wontfix tickets if over cap."""
        count = conn.execute("SELECT COUNT(*) FROM %s WHERE user_id = ?" % _TABLE_NAME, (user_id,)).fetchone()[
            0
        ]  # nosec B608
        if count <= _MAX_TICKETS:
            return

        excess = count - _MAX_TICKETS
        # Delete oldest resolved/wontfix first
        conn.execute(
            """DELETE FROM %s WHERE id IN (
                SELECT id FROM %s
                WHERE user_id = ? AND status IN ('resolved', 'wontfix')
                ORDER BY updated_at ASC
                LIMIT ?
            )""" % (_TABLE_NAME, _TABLE_NAME),  # nosec B608
            (user_id, excess),
        )
        conn.commit()

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> BugTicket:
        """Convert a sqlite3.Row to a BugTicket."""
        return BugTicket(
            id=row["id"],
            user_id=row["user_id"] or "",
            fingerprint=row["fingerprint"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            hit_count=row["hit_count"],
            status=row["status"],
            error_type=row["error_type"],
            error_message=row["error_message"],
            execution_stage=row["execution_stage"],
            user_request=row["user_request"],
            root_cause=row["root_cause"],
            category=row["category"],
            severity=row["severity"],
            user_explanation=row["user_explanation"],
            developer_detail=row["developer_detail"],
            is_transient=bool(row["is_transient"]),
            diagnosis_succeeded=bool(row["diagnosis_succeeded"]),
            context_json=row["context_json"],
        )


def _parse_context_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
