"""Per-user preferences backed by the ``user_preferences`` DB table.

Stores user-scoped settings (delivery_address, weather_location, etc.)
separately from device-scoped settings that remain in ``settings.json``.

Usage::

    from services.user_model.preferences_store import get_user_preferences_store

    store = get_user_preferences_store()
    store.set(user_id, "weather_location", "Milwaukee, WI")
    loc = store.get(user_id, "weather_location")
    all_prefs = store.get_all(user_id)
"""

from __future__ import annotations

import json
import threading
from typing import Any

from core.asyncio_safe import run_async_synchronously
from core.logging_config import get_logger

logger = get_logger(__name__)

# Keys that belong to the user (not the device).
# These are migrated from settings.json into the per-user DB on first run.
USER_SCOPED_KEYS = frozenset(
    {
        "delivery_address",
        "weather_location",
        "user_name",
        "default_music_provider",
        "active_music_provider_id",
        "locale",
        "allow_explicit",
        "autoplay_enabled",
        "ai_autoplay_enabled",
        "autoplay_min_queue",
        "speak_all_replies",
        "time_display_format",
    }
)


def _run_async(coro: Any, *, timeout: float = 3.0) -> Any:
    """Run *coro* synchronously, even when called from inside a running loop.

    On the cloud (Postgres) backend the asyncpg pool is bound to the main
    event loop; spawning a fresh loop in a worker thread leaves the
    coroutine waiting forever on a connection it cannot acquire. We bound
    the wait so callers fail fast instead of stalling the request. Returns
    ``None`` on timeout — public methods on this store treat that as
    "no value", which falls back to the caller's default. (TODO: rewire
    callers to async so the cloud path can actually read/write user
    preferences.)
    """
    try:
        return run_async_synchronously(
            coro,
            timeout=timeout,
            timeout_result=None,
            timeout_log_message=(
                "User-preferences async call timed out after %.1fs (likely "
                "Postgres pool bound to another loop); returning None."
            ),
            logger=logger,
        )
    except RuntimeError as exc:
        _msg = str(exc)
        # Match cloud's cross-loop guard message AND desktop's asyncpg
        # pool-bound message; same class of failure on both surfaces.
        if (
            "Cannot synchronously wait on the shared asyncio worker loop from itself" in _msg
            or "pool is bound to a different event loop" in _msg
        ):
            logger.warning(
                "preferences_store sync dispatch blocked by ASYNC-1 cross-loop guard; "
                "returning None (preference unavailable). Async callers should "
                "migrate to await-able APIs.",
            )
            try:
                coro.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            return None
        raise


def _get_auth_db() -> Any:
    from auth.database import get_auth_db

    db = get_auth_db()
    if not db._initialized:
        _run_async(db.initialize())
    return db


class UserPreferencesStore:
    """Thin wrapper around the ``user_preferences`` DB table.

    All public methods are synchronous (they block on the async DB layer
    using ``_run_async``).  Values are stored as JSON-encoded strings so
    dicts and lists are supported.
    """

    def get(self, user_id: str, key: str, default: Any = None) -> Any:
        """Return a single preference, deserialised from JSON."""
        db = _get_auth_db()
        raw = _run_async(db.user_preferences.get(user_id, key))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def set(self, user_id: str, key: str, value: Any) -> None:
        """Persist a single preference (JSON-encoded)."""
        db = _get_auth_db()
        encoded = json.dumps(value, ensure_ascii=False)
        _run_async(db.user_preferences.set(user_id, key, encoded))

    def get_all(self, user_id: str) -> dict[str, Any]:
        """Return all preferences for *user_id* (values JSON-decoded)."""
        db = _get_auth_db()
        raw_all = _run_async(db.user_preferences.get_all(user_id))
        result: dict[str, Any] = {}
        for k, v in raw_all.items():
            try:
                result[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                result[k] = v
        return result

    def delete(self, user_id: str, key: str) -> bool:
        """Remove a single preference. Returns True if it existed."""
        db = _get_auth_db()
        return _run_async(db.user_preferences.delete(user_id, key))

    def migrate_from_settings(self, user_id: str) -> int:
        """One-time migration: copy user-scoped keys from ``settings.json`` into the DB.

        Skips keys that already have a DB value (DB wins).  Returns the
        count of keys actually migrated.
        """
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
        except Exception:
            logger.debug("Settings manager not available for preference migration")
            return 0

        existing = self.get_all(user_id)
        migrated = 0

        for key in USER_SCOPED_KEYS:
            if key in existing:
                continue  # DB already has this key
            value = sm.get(key)
            if value is None or value == "" or value == {}:
                continue
            self.set(user_id, key, value)
            migrated += 1
            logger.debug("Migrated user preference %s to DB for user %s", key, user_id)

        if migrated:
            logger.info(
                "Migrated %d user-scoped settings from settings.json to DB for user %s",
                migrated,
                user_id,
            )
        return migrated


_store_lock = threading.Lock()
_store_singleton: UserPreferencesStore | None = None


def get_user_preferences_store() -> UserPreferencesStore:
    """Return the process-wide ``UserPreferencesStore`` singleton."""
    global _store_singleton
    if _store_singleton is None:
        with _store_lock:
            if _store_singleton is None:
                _store_singleton = UserPreferencesStore()
    return _store_singleton


def reset_user_preferences_store_for_tests() -> None:
    """Clear the singleton (test helper)."""
    global _store_singleton
    with _store_lock:
        _store_singleton = None
