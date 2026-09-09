"""Always-on local calendar provider."""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from config.settings import settings
from services.calendar.datetime_utils import CalendarDateTimeUtils
from services.calendar.providers.base import normalize_calendar, normalize_event

_LOCAL_CALENDAR_ID = "local-primary"


def _user_calendar_timezone_name() -> str:
    """The zone this calendar is presented in for the current user (#3557)."""
    from services.user_timezone import active_timezone_name

    return active_timezone_name() or settings.calendar_timezone


def _require_user_id(user_id: str) -> str:
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


def _safe_user_component(user_id: str) -> str:
    uid = _require_user_id(user_id)
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()


class LocalCalendarProvider:
    """SQLite-backed per-user local calendar."""

    provider_id = "local"

    def __init__(
        self,
        *,
        storage_root: Path | None = None,
        datetime_utils: CalendarDateTimeUtils | None = None,
    ) -> None:
        self._storage_root = storage_root or Path(settings.data_dir) / "calendar" / "local"
        self._datetime_utils = datetime_utils or CalendarDateTimeUtils()
        self._lock = threading.Lock()

    async def is_configured(self, user_id: str) -> bool:
        _require_user_id(user_id)
        return True

    async def list_calendars(self, user_id: str) -> list[dict[str, Any]]:
        _require_user_id(user_id)
        return [
            normalize_calendar(
                provider=self.provider_id,
                calendar_id=_LOCAL_CALENDAR_ID,
                name="Viola Calendar",
                description="Always-on local calendar",
                primary=True,
                writable=True,
                timezone=_user_calendar_timezone_name(),
                raw={"storage": "local"},
            )
        ]

    async def list_events(
        self,
        user_id: str,
        *,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        max_results: int,
        calendar_id: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = calendar_id
        if max_results <= 0:
            return []
        start_dt = self._normalise_datetime(start_date)
        end_dt = self._normalise_datetime(end_date)
        with self._lock, self._session(user_id) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM events
                WHERE start_time <= ?
                  AND COALESCE(end_time, start_time) >= ?
                ORDER BY start_time ASC
                LIMIT ?
                """,
                (end_dt.isoformat(), start_dt.isoformat(), max_results),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    async def get_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        _ = calendar_id
        with self._lock, self._session(user_id) as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._row_to_event(row) if row is not None else None

    async def create_event(
        self,
        user_id: str,
        *,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        attendees: list[str] | None = None,
        recurrence: str | list[str] | None = None,
    ) -> dict[str, Any] | None:
        start_dt = self._normalise_datetime(start_time)
        end_dt = self._normalise_datetime(end_time)
        now = datetime.datetime.now(datetime.UTC).isoformat()
        event_id = "local-%s" % uuid.uuid4()
        recurrence_value = self._encode_recurrence(recurrence)
        attendees_json = json.dumps([{"email": attendee} for attendee in attendees or []])
        with self._lock, self._session(user_id) as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, calendar_id, title, description, location, start_time,
                    end_time, all_day, recurrence, attendees_json, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    calendar_id or _LOCAL_CALENDAR_ID,
                    title or "Untitled Event",
                    description,
                    location,
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    1 if all_day else 0,
                    recurrence_value,
                    attendees_json,
                    None,
                    now,
                    now,
                ),
            )
        return await self.get_event(user_id, event_id=event_id, calendar_id=calendar_id)

    async def update_event(
        self,
        user_id: str,
        *,
        event_id: str,
        title: str | None = None,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        recurrence: str | list[str] | None = None,
    ) -> dict[str, Any] | None:
        _ = calendar_id
        with self._lock, self._session(user_id) as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                return None

            updates: dict[str, object] = {"updated_at": datetime.datetime.now(datetime.UTC).isoformat()}
            if title is not None:
                updates["title"] = title
            if description is not None:
                updates["description"] = description
            if location is not None:
                updates["location"] = location
            if start_time is not None:
                updates["start_time"] = self._normalise_datetime(start_time).isoformat()
            if end_time is not None:
                updates["end_time"] = self._normalise_datetime(end_time).isoformat()
            if all_day or start_time is not None or end_time is not None:
                updates["all_day"] = 1 if all_day else 0
            if recurrence is not None:
                updates["recurrence"] = self._encode_recurrence(recurrence)

            assignments = ", ".join("%s = ?" % key for key in updates)
            # Column names come only from the fixed updates dict above.
            query = "UPDATE events SET %s WHERE event_id = ?" % assignments  # nosec B608
            conn.execute(
                query,
                (*updates.values(), event_id),
            )
            updated = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._row_to_event(updated) if updated is not None else None

    async def delete_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> bool:
        _ = calendar_id
        with self._lock, self._session(user_id) as conn:
            cursor = conn.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
            return cursor.rowcount > 0

    async def respond_to_event(
        self,
        user_id: str,
        *,
        event_id: str,
        response_status: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        _ = calendar_id
        with self._lock, self._session(user_id) as conn:
            cursor = conn.execute(
                """
                UPDATE events
                SET status = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (response_status, datetime.datetime.now(datetime.UTC).isoformat(), event_id),
            )
            if cursor.rowcount == 0:
                return None
        return await self.get_event(user_id, event_id=event_id, calendar_id=calendar_id)

    async def find_free_time(
        self,
        user_id: str,
        *,
        attendees: list[str],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        _ = attendees
        start_dt = self._normalise_datetime(start_date)
        end_dt = self._normalise_datetime(end_date)
        duration = datetime.timedelta(minutes=duration_minutes)
        if duration <= datetime.timedelta() or start_dt + duration > end_dt:
            return {"available": False, "busy_events": []}

        events = await self.list_events(
            user_id,
            start_date=start_dt,
            end_date=end_dt,
            max_results=1000,
            calendar_id=calendar_id,
        )
        cursor = start_dt
        for event in events:
            event_start = event.get("start_time")
            event_end = event.get("end_time") or event_start
            if not isinstance(event_start, datetime.datetime) or not isinstance(event_end, datetime.datetime):
                continue
            if cursor + duration <= event_start:
                return {
                    "available": True,
                    "start_time": cursor,
                    "end_time": cursor + duration,
                    "busy_events": events,
                }
            if event_end > cursor:
                cursor = event_end

        if cursor + duration <= end_dt:
            return {
                "available": True,
                "start_time": cursor,
                "end_time": cursor + duration,
                "busy_events": events,
            }
        return {"available": False, "busy_events": events}

    def user_storage_dir(self, user_id: str) -> Path:
        """Per-user storage directory (pure path computation, no mkdir).

        Public so GDPR export/purge can locate a user's calendar data without
        duplicating the partitioning scheme.
        """
        return self._storage_root / _safe_user_component(_require_user_id(user_id))

    def _db_path(self, user_id: str) -> Path:
        user_dir = self.user_storage_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "events.db"

    def _connect(self, user_id: str) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(user_id))
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        return conn

    @contextmanager
    def _session(self, user_id: str) -> Iterator[sqlite3.Connection]:
        """Commit-on-success session that ALWAYS closes the connection.

        ``sqlite3.Connection.__enter__/__exit__`` only commit/rollback — they
        never close, so the old ``with self._connect(...) as conn:`` pattern
        leaked one open file handle per call (and on Windows kept the per-user
        events.db locked, which broke GDPR purge of the user's store).
        """
        conn = self._connect(user_id)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                calendar_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                location TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                recurrence TEXT,
                attendees_json TEXT NOT NULL DEFAULT '[]',
                status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_time_range
                ON events (start_time, end_time);
            """)

    def _normalise_datetime(self, value: datetime.datetime) -> datetime.datetime:
        return self._datetime_utils.normalise_datetime(value)

    def _parse_datetime(self, value: str | None) -> datetime.datetime | None:
        if not value:
            return None
        parsed = self._datetime_utils.parse_iso_datetime(value)
        return self._normalise_datetime(parsed) if parsed else None

    @staticmethod
    def _encode_recurrence(value: str | list[str] | None) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return json.dumps(value)
        return value

    @staticmethod
    def _decode_recurrence(value: str | None) -> str | list[str] | None:
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return parsed if isinstance(parsed, list) else value

    @staticmethod
    def _decode_attendees(value: str | None) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        recurrence = self._decode_recurrence(row["recurrence"])
        raw = {
            "event_id": row["event_id"],
            "calendar_id": row["calendar_id"],
            "recurrence": recurrence,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        event = normalize_event(
            provider=self.provider_id,
            calendar_id=row["calendar_id"],
            event_id=row["event_id"],
            title=row["title"],
            description=row["description"],
            location=row["location"],
            start_time=self._parse_datetime(row["start_time"]),
            end_time=self._parse_datetime(row["end_time"]),
            all_day=bool(row["all_day"]),
            attendees=self._decode_attendees(row["attendees_json"]),
            status=row["status"],
            display_timezone=self._datetime_utils.get_user_display_timezone(),
            raw=raw,
        )
        if recurrence is not None:
            event["recurrence"] = recurrence
        return event
