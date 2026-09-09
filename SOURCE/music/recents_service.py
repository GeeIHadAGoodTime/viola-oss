from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Protocol, cast

RECENTLY_PLAYED_KEY = "music.recently_played.v1"
RECENTLY_PLAYED_MAX_ENTRIES = 50
DEFAULT_RECENTLY_PLAYED_LIMIT = 10


class _StateStore(Protocol):
    def get_setting(self, user_id: str, key: str, *, default: object = None) -> object: ...

    async def get_setting_async(self, user_id: str, key: str, *, default: object = None) -> object: ...

    def set_setting(self, user_id: str, key: str, value: object) -> None: ...


class MusicRecentsService:
    """Durable per-user recently played ring buffer backed by PersistentStateStore."""

    def __init__(self, state_store: _StateStore | None = None) -> None:
        if state_store is None:
            from services.persistence.state_store import get_state_store

            state_store = get_state_store()
        self._state_store = state_store
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def record_play(self, user_id: str, track: dict) -> None:
        resolved_user_id = self._require_user_id(user_id)
        entry = self._normalize_track(track)
        with self._lock_for_user(resolved_user_id):
            entries = self._load_entries(resolved_user_id)
            entries.insert(0, entry)
            self._state_store.set_setting(
                resolved_user_id,
                RECENTLY_PLAYED_KEY,
                entries[:RECENTLY_PLAYED_MAX_ENTRIES],
            )

    def list_recently_played(self, user_id: str, limit: int = DEFAULT_RECENTLY_PLAYED_LIMIT) -> list[dict]:
        resolved_user_id = self._require_user_id(user_id)
        if limit <= 0:
            return []
        with self._lock_for_user(resolved_user_id):
            entries = self._load_entries(resolved_user_id)
        return [dict(entry) for entry in entries[: min(limit, RECENTLY_PLAYED_MAX_ENTRIES)]]

    async def list_recently_played_async(self, user_id: str, limit: int = DEFAULT_RECENTLY_PLAYED_LIMIT) -> list[dict]:
        """Async-native recents read for serving-loop callers (CL-20260711-afd7).

        The cloud ``media``/``search_tracks`` tool runs ON the FastAPI serving
        loop; the sync ``get_setting`` (Postgres ``_run`` bridge) would raise
        SyncBridgeLoopError. Awaits the async-native state-store read instead.
        The per-user lock only guards the read-modify-write in ``record_play``;
        a plain read does not take it (the sync path does not hold it across the
        store call either).
        """
        resolved_user_id = self._require_user_id(user_id)
        if limit <= 0:
            return []
        entries = await self._load_entries_async(resolved_user_id)
        return [dict(entry) for entry in entries[: min(limit, RECENTLY_PLAYED_MAX_ENTRIES)]]

    def _load_entries(self, user_id: str) -> list[dict[str, object]]:
        value = self._state_store.get_setting(user_id, RECENTLY_PLAYED_KEY, default=[])
        return self._coerce_entries(value)

    async def _load_entries_async(self, user_id: str) -> list[dict[str, object]]:
        value = await self._state_store.get_setting_async(user_id, RECENTLY_PLAYED_KEY, default=[])
        return self._coerce_entries(value)

    @staticmethod
    def _coerce_entries(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        entries: list[dict[str, object]] = []
        for item in value[:RECENTLY_PLAYED_MAX_ENTRIES]:
            if isinstance(item, dict):
                entries.append(cast(dict[str, object], dict(item)))
        return entries

    def _lock_for_user(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[user_id] = lock
            return lock

    @staticmethod
    def _normalize_track(track: dict) -> dict[str, str]:
        return {
            "provider": _string_field(track, "provider"),
            "track_uri": _string_field(track, "track_uri", "uri", "id", "url", "video_id"),
            "title": _string_field(track, "title"),
            "artist": _string_field(track, "artist"),
            "played_at_iso": _string_field(track, "played_at_iso") or _utc_now_iso(),
        }

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        resolved_user_id = user_id.strip()
        if not resolved_user_id:
            raise ValueError("user_id is required for music recently played")
        return resolved_user_id


_SERVICE_SINGLETON: MusicRecentsService | None = None
_SERVICE_LOCK = threading.Lock()


def get_music_recents_service() -> MusicRecentsService:
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        with _SERVICE_LOCK:
            if _SERVICE_SINGLETON is None:
                _SERVICE_SINGLETON = MusicRecentsService()
    return _SERVICE_SINGLETON


def _string_field(track: dict, *keys: str) -> str:
    for key in keys:
        value = track.get(key)
        if value is not None:
            return str(value)
    return ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_RECENTLY_PLAYED_LIMIT",
    "RECENTLY_PLAYED_KEY",
    "RECENTLY_PLAYED_MAX_ENTRIES",
    "MusicRecentsService",
    "get_music_recents_service",
]
