"""Adapter for ``sync_user_preferences``."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync.consent import SETTINGS_CONSENT_OPTIONAL
from services.sync_surfaces.base import (
    SurfaceDefinition,
    SyncSurfaceError,
    Tier3SyncPayloadError,
    delete_surface,
    get_keyed_surface,
    is_tier3_sync_key,
    list_keyed_surface,
    rows_to_dicts,
    upsert_surface,
)

USER_PREFERENCE_TIER3_SAFE_INDICATOR_KEYS = frozenset()
LOCKED_CLOUD_USER_PREFERENCE_KEYS = frozenset({"llm_provider", "llm_model", "agent_model"})


class UserPreferencePolicyError(SyncSurfaceError):
    status_code = 403
    error_code = "setting_policy_forbidden"


USER_PREFERENCES = SurfaceDefinition(
    surface="user_preferences",
    table="sync_user_preferences",
    pk_columns=("user_id", "key"),
    data_columns=("value_json",),
    json_columns=("value_json", "version_vector", "field_versions"),
)


def _is_tier3_user_preference_key(key: str) -> bool:
    from ui.settings_manager import is_user_facing_secret_key

    return key not in USER_PREFERENCE_TIER3_SAFE_INDICATOR_KEYS and (
        is_tier3_sync_key(key) or is_user_facing_secret_key(key)
    )


def _validate_user_preference_value(key: str, value: Any) -> Any:
    from ui.settings_api import _validate_settings
    from ui.settings_manager import get_settings_manager

    clean, errors = _validate_settings({key: value}, get_settings_manager().DEFAULT_SETTINGS)
    if key in clean:
        return clean[key]
    if errors:
        raise ValueError(errors[0])
    raise ValueError("Setting '%s' did not pass validation" % key)


def prepare_user_preference_payload(payload: dict[str, Any]) -> dict[str, Any]:
    from ui.settings_manager import is_cloud_user_scoped_setting_key, normalize_setting_key

    clean = dict(payload)
    raw_key = str(clean.get("key") or "").strip()
    if not raw_key:
        raise ValueError("user_preferences row requires key")

    normalized = normalize_setting_key(raw_key)
    if _is_tier3_user_preference_key(raw_key) or _is_tier3_user_preference_key(normalized):
        raise Tier3SyncPayloadError(
            "Tier-3 setting key is not allowed on user_preferences: %s" % normalized,
            details={"surface": USER_PREFERENCES.surface, "field": "key"},
        )

    clean["key"] = normalized
    if normalized in SETTINGS_CONSENT_OPTIONAL:
        if normalized == "consent_cloud_sync" and "value_json" in clean:
            value = clean["value_json"]
            if not isinstance(value, (bool, str)):
                raise ValueError("Setting 'consent_cloud_sync' must be a boolean")
            if isinstance(value, str) and value not in {"true", "false"}:
                raise ValueError("Setting 'consent_cloud_sync' must be true or false")
        elif normalized == "consent_generation" and "value_json" in clean and not isinstance(clean["value_json"], int):
            raise ValueError("Setting 'consent_generation' must be an integer")
        return clean

    try:
        is_cloud_user_scoped = is_cloud_user_scoped_setting_key(normalized, strict=True)
    except KeyError:
        raise ValueError("Unknown cloud setting key: %s" % normalized) from None

    if not is_cloud_user_scoped:
        raise UserPreferencePolicyError(
            "Only allowlisted user-scoped settings are available on user_preferences: %s" % normalized,
            details={"surface": USER_PREFERENCES.surface, "field": "key"},
        )
    if normalized in LOCKED_CLOUD_USER_PREFERENCE_KEYS:
        raise UserPreferencePolicyError(
            "Setting '%s' must be changed through the desktop BYOK/local settings flow, not cloud sync" % normalized,
            details={"surface": USER_PREFERENCES.surface, "field": "key"},
        )

    if "value_json" in clean:
        clean["value_json"] = _validate_user_preference_value(normalized, clean["value_json"])
    return clean


async def list_user_preferences(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, USER_PREFERENCES, user_id, since_seq)


async def list_current_user_preferences(conn: asyncpg.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT *
        FROM sync_user_preferences
        WHERE user_id = $1::uuid
          AND deleted_at IS NULL
        ORDER BY key
        """,
        user_id,
    )
    return rows_to_dicts(rows, USER_PREFERENCES)


async def get_user_preference(conn: asyncpg.Connection, user_id: str, key: str) -> dict[str, Any] | None:
    return await get_keyed_surface(conn, USER_PREFERENCES, user_id, key)


async def upsert_user_preference(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    clean_payload = prepare_user_preference_payload(payload)
    return await upsert_surface(conn, USER_PREFERENCES, user_id, clean_payload, incoming)


async def delete_user_preference(
    conn: asyncpg.Connection,
    user_id: str,
    key: str,
    incoming: IncomingMutation | None = None,
) -> None:
    clean_payload = prepare_user_preference_payload({"key": key})
    await delete_surface(conn, USER_PREFERENCES, user_id, clean_payload["key"], incoming)
