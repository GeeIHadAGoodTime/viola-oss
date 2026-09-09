"""
Cross-Device Preference Sync API.

Provides GET/PUT/PATCH on ``/api/v1/preferences`` for reading and writing
per-user preferences stored in the auth database's ``user_settings`` table.

This is the public API surface for cross-device preference synchronization.
Preferences include theme, locale, volume, consent flags, etc.  The full
schema is defined in ``auth.settings_schema``.

Authentication is required.  On desktop (auth disabled), the local user's
preferences are returned.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request, status

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])


# ---------------------------------------------------------------------------
# Auth dependency — resolve the current user ID
# ---------------------------------------------------------------------------


async def _get_current_user_id(request: Request) -> str:
    """Extract the current user ID from the request.

    Tries the auth middleware's ``request.state.user`` first (cloud/JWT auth).
    Falls back to the device user when auth is disabled (desktop mode).
    """
    # Check if auth middleware set the user
    user = getattr(getattr(request, "state", None), "user", None)
    if user is not None:
        user_id = getattr(user, "id", None)
        if user_id:
            return str(user_id)

    # Check if auth is disabled (desktop mode)
    try:
        from ui.security.config import get_security_config

        config = get_security_config()
        if not config.auth_enabled:
            # #2646 / M-BILL-1 (#337): preferences are user-scoped settings, so
            # prefer the signed-in desktop account over the anonymous device id.
            # Signed-out installs still resolve the device id.
            from core.user_context import get_current_or_desktop_active_user_id

            return get_current_or_desktop_active_user_id()
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=failure_response("not_authenticated", "Authentication required"),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _get_auth_db():
    """Get the auth database instance."""
    from auth.database import get_auth_db

    db = get_auth_db()
    if not db._initialized:
        await db.initialize()
    return db


def _should_write_through_local_settings() -> bool:
    """Return False when cloud routes would write into a process-global SettingsManager."""
    from config.settings import settings

    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", surface) or surface).strip().lower()
    return surface != "cloud" and deployment != "cloud"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def get_preferences(
    request: Request,
    user_id: str = Depends(_get_current_user_id),
    db=Depends(_get_auth_db),
) -> JSONResponse:
    """Get all user preferences.

    Returns current preferences with schema version and sync metadata.
    Creates default preferences if none exist yet.
    """
    from auth.settings_schema import migrate_settings, normalize_settings, settings_changed

    result = await db.user_settings.get_settings(user_id)

    if result is None:
        # Create default preferences
        preferences = normalize_settings({})
        preferences_str = _json.dumps(preferences)
        version = await db.user_settings.save_settings(user_id, preferences_str, 1)
        return JSONResponse(
            content=success_response(
                {
                    "preferences": preferences,
                    "version": version,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        )

    preferences, version = result

    # Auto-migrate if needed
    migrated = migrate_settings(preferences)
    if settings_changed(preferences, migrated):
        preferences_str = _json.dumps(migrated)
        version = await db.user_settings.save_settings(user_id, preferences_str, version)
        preferences = migrated

    return JSONResponse(
        content=success_response(
            {
                "preferences": preferences,
                "version": version,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    )


@router.put("")
async def set_preferences(
    request: Request,
    user_id: str = Depends(_get_current_user_id),
    db=Depends(_get_auth_db),
) -> JSONResponse:
    """Replace all user preferences.

    The request body should be a JSON object with a ``preferences`` key
    containing the full preferences dict.  The version is incremented
    automatically.
    """
    from auth.settings_schema import normalize_settings, validate_settings_patch

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_json", "Request body must be valid JSON"),
        )

    new_prefs = body.get("preferences", {})
    if not isinstance(new_prefs, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_request", "'preferences' must be an object"),
        )

    # Validate all patchable fields
    errors = validate_settings_patch(new_prefs)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_settings", "Validation failed", details=errors),
        )

    # Get current version for increment
    result = await db.user_settings.get_settings(user_id)
    current_version = result[1] if result else 0

    normalized = normalize_settings(new_prefs)
    new_version = current_version + 1
    preferences_str = _json.dumps(normalized)
    await db.user_settings.save_settings(user_id, preferences_str, new_version)

    now = datetime.now(UTC).isoformat()
    logger.info("Preferences replaced for user=%s (v%d -> v%d)", user_id, current_version, new_version)

    return JSONResponse(
        content=success_response(
            {
                "preferences": normalized,
                "version": new_version,
                "updated_at": now,
            }
        )
    )


@router.patch("")
async def update_preferences(
    request: Request,
    user_id: str = Depends(_get_current_user_id),
    db=Depends(_get_auth_db),
) -> JSONResponse:
    """Partial update of user preferences.

    Only the keys present in ``preferences`` are updated; others remain
    unchanged.  Supports optimistic locking via optional ``version`` field.
    """
    from auth.settings_schema import normalize_settings, validate_settings_patch

    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_json", "Request body must be valid JSON"),
        )

    patch_prefs = body.get("preferences", {})
    client_version = body.get("version")

    if not isinstance(patch_prefs, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_request", "'preferences' must be an object"),
        )

    if not patch_prefs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("empty_patch", "No preferences to update"),
        )

    # Validate patch payload
    errors = validate_settings_patch(patch_prefs)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=failure_response("invalid_settings", "Validation failed", details=errors),
        )

    # Get current preferences
    result = await db.user_settings.get_settings(user_id)
    if result is None:
        current_prefs = normalize_settings({})
        current_version = 1
    else:
        current_prefs, current_version = result

    # Optimistic locking
    if client_version is not None and client_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=failure_response(
                "version_conflict",
                "Preferences have been modified by another device",
                details={"current_version": current_version},
            ),
        )

    # Shallow merge + normalize
    merged = {**current_prefs, **patch_prefs}
    normalized = normalize_settings(merged)
    new_version = current_version + 1

    preferences_str = _json.dumps(normalized)
    await db.user_settings.save_settings(user_id, preferences_str, new_version)

    now = datetime.now(UTC).isoformat()
    logger.info(
        "Preferences patched for user=%s (v%d -> v%d, keys=%s)",
        user_id,
        current_version,
        new_version,
        ",".join(sorted(patch_prefs.keys())),
    )

    # Write-through consent flags to local SettingsManager
    _consent_keys = {k: v for k, v in patch_prefs.items() if k.startswith("consent_")}
    if _consent_keys and _should_write_through_local_settings():
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            for k, v in _consent_keys.items():
                sm.set(k, v, save_immediately=False)
            sm.save()
        except Exception:
            logger.debug("Failed to write-through consent keys to SettingsManager")

    return JSONResponse(
        content=success_response(
            {
                "preferences": normalized,
                "version": new_version,
                "updated_at": now,
            }
        )
    )
