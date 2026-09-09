"""
Calendar fallback storage.

Stores offline calendar writes per user so they can be replayed once a provider
is available again.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)


def _safe_user_component(user_id: str) -> str:
    return user_id.replace("\\", "_").replace("/", "_").replace(":", "_")


class CalendarFallbackStorage:
    """Handles per-user fallback event storage."""

    def __init__(self, storage_path: Path | None = None):
        self._storage_path = storage_path or Path(settings.data_dir) / "calendar_fallback"
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def store_path(self) -> Path:
        return self._storage_path

    def user_storage_dir(self, user_id: str) -> Path:
        """Per-user fallback directory (pure path computation, no mkdir).

        Public so GDPR export/purge can locate a user's fallback records
        without duplicating the partitioning scheme.
        """
        return self._storage_path / _safe_user_component(user_id)

    def _events_file(self, user_id: str) -> Path:
        user_dir = self.user_storage_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir / "events.json"

    def load_fallback_events(self, *, user_id: str) -> list[dict[str, Any]]:
        events_file = self._events_file(user_id)
        if not events_file.exists():
            return []
        try:
            with self._lock, open(events_file, encoding="utf-8") as f:
                data = json.load(f)
                events = data.get("events", [])
                return events if isinstance(events, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load fallback events for user %s: %s", user_id, exc)
            return []

    def persist_fallback_events(self, events: list[dict[str, Any]], *, user_id: str) -> None:
        events_file = self._events_file(user_id)
        try:
            with self._lock, open(events_file, "w", encoding="utf-8") as f:
                json.dump({"events": events}, f, indent=2, default=str)
        except OSError as exc:
            logger.error("Failed to persist fallback events for user %s: %s", user_id, exc)

    def store_fallback_event(
        self,
        *,
        user_id: str,
        title: str,
        start_time: str,
        end_time: str | None = None,
        description: str | None = None,
        location: str | None = None,
        provider: str = "fallback",
        calendar_id: str | None = None,
        all_day: bool = False,
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid4())
        event = {
            "id": event_id,
            "title": title,
            "start_time": start_time,
            "end_time": end_time,
            "description": description,
            "location": location,
            "provider": provider,
            "calendar_id": calendar_id,
            "all_day": all_day,
            "created_at": datetime.now().isoformat(),
        }

        events = self.load_fallback_events(user_id=user_id)
        events.append(event)
        self.persist_fallback_events(events, user_id=user_id)

        logger.info("Stored fallback event for user %s: %s", user_id, title)
        return event

    def get_fallback_events(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        events = self.load_fallback_events(user_id=user_id)
        return events[-limit:] if limit > 0 else events

    def delete_fallback_event(self, event_id: str, *, user_id: str) -> bool:
        events = self.load_fallback_events(user_id=user_id)
        original_count = len(events)
        events = [event for event in events if event.get("id") != event_id]
        if len(events) < original_count:
            self.persist_fallback_events(events, user_id=user_id)
            logger.info("Deleted fallback event for user %s: %s", user_id, event_id)
            return True
        return False
