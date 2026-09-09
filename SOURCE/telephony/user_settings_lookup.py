"""Async-safe user settings lookup helpers for phone runtime paths."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _is_sqlite_auth_settings_backend(db: object, repo: object) -> bool:
    db_type = type(db)
    repo_type = type(repo)
    return (db_type.__module__ == "auth.database" and db_type.__name__ == "SQLiteAuthDatabase") or (
        repo_type.__module__ == "auth.database" and repo_type.__name__ == "SQLiteUserSettingsRepository"
    )


def first_setting_value(settings_blob: dict[str, object], keys: Iterable[str]) -> object:
    """Return the first non-empty value from a user settings blob."""

    for key in keys:
        value = settings_blob.get(key)
        if value not in (None, "", {}):
            return value
    return None


async def load_cloud_user_settings_blob(user_id: str, *, fail_closed: bool = False) -> dict[str, object] | None:
    """Read cloud user settings without using the sync SettingsManager bridge.

    Returns ``None`` when the active auth DB is not the cloud/Postgres settings
    repository so callers can fall back to the desktop sync API.
    """

    if not user_id:
        return None
    cloud_repository_seen = False
    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        if not getattr(db, "_initialized", True):
            initialize = getattr(db, "initialize", None)
            if callable(initialize):
                await initialize()
        repo = getattr(db, "user_settings", None)
        if _is_sqlite_auth_settings_backend(db, repo):
            return None
        cloud_repository_seen = True
        get_settings = getattr(repo, "get_settings", None)
        if not callable(get_settings):
            return None
        loaded = await get_settings(user_id)
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Cloud user settings unavailable for user %s: %s", user_id, exc)
        if fail_closed and cloud_repository_seen:
            raise
        return {} if cloud_repository_seen else None
    if not loaded:
        return {}
    raw_blob = loaded[0] if isinstance(loaded, tuple) and loaded else loaded
    if not isinstance(raw_blob, dict):
        return {}
    return dict(raw_blob)
