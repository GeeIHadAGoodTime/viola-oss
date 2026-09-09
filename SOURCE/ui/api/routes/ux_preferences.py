"""REST API endpoints for UX preferences.

Provides GET/POST /v1/ux/preferences to read and update user-facing
display preferences via SettingsManager (the runtime source of truth
for user preferences).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)

# Keys in SettingsManager that correspond to UX preferences
_UX_KEYS = {
    "theme": "theme",
    "display_density": "display_density",
    "sidebar_collapsed": "sidebar_collapsed",
}

# Defaults for UX preferences (used when not yet stored)
_UX_DEFAULTS: dict[str, object] = {
    "theme": "dark",
    "display_density": "comfortable",
    "sidebar_collapsed": False,
}


class UXPreferencesUpdateRequest(BaseModel):
    """Request body for updating UX preferences.

    All fields are optional; only provided fields are updated.
    """

    theme: str | None = Field(None, description="UI theme: 'dark' or 'light'")
    display_density: str | None = Field(None, description="Display density: 'compact' or 'comfortable'")
    sidebar_collapsed: bool | None = Field(None, description="Whether sidebar is collapsed")


def _read_preferences() -> dict[str, object]:
    """Read current UX preferences from SettingsManager."""
    from ui.settings_manager import get_settings_manager

    sm = get_settings_manager()
    prefs: dict[str, object] = {}
    for pref_key, sm_key in _UX_KEYS.items():
        prefs[pref_key] = sm.get(sm_key, _UX_DEFAULTS.get(pref_key))
    return prefs


def register_ux_preferences_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register UX preferences REST endpoints."""
    router = context.router

    @router.get("/v1/ux/preferences", dependencies=[Depends(require_auth)])
    async def get_ux_preferences():
        """Return current UX preferences."""

        async def _inner():
            try:
                prefs = _read_preferences()
                return success_response(prefs)
            except Exception as exc:
                log.debug("Get UX preferences failed: %s", exc)
                return handle_route_error(exc, "get_ux_preferences")

        return await toolbox.record_and_call(_inner, route="/v1/ux/preferences", method="GET")

    @router.post("/v1/ux/preferences", dependencies=[Depends(require_auth)])
    async def update_ux_preferences(body: UXPreferencesUpdateRequest):
        """Update UX preferences.

        Only fields present in the request body are updated; others remain
        unchanged.  Persists via SettingsManager.
        """

        async def _inner():
            try:
                from ui.settings_manager import get_settings_manager

                sm = get_settings_manager()

                updates = body.model_dump(exclude_none=True)
                if not updates:
                    # Nothing to update, just return current state
                    return success_response(_read_preferences())

                # Validate enum-like fields
                if "theme" in updates and updates["theme"] not in ("dark", "light"):
                    return JSONResponse(
                        status_code=400,
                        content=failure_response(
                            "invalid_theme",
                            "theme must be 'dark' or 'light'",
                        ),
                    )
                if "display_density" in updates and updates["display_density"] not in (
                    "compact",
                    "comfortable",
                ):
                    return JSONResponse(
                        status_code=400,
                        content=failure_response(
                            "invalid_display_density",
                            "display_density must be 'compact' or 'comfortable'",
                        ),
                    )

                # Write each updated key to SettingsManager
                for pref_key, value in updates.items():
                    sm_key = _UX_KEYS.get(pref_key, pref_key)
                    sm.set(sm_key, value, save_immediately=False)
                sm.save()

                log.info("UX preferences updated: %s", list(updates.keys()))

                # Return the full preference set after update
                return success_response(_read_preferences())
            except Exception as exc:
                log.debug("Update UX preferences failed: %s", exc)
                return handle_route_error(exc, "update_ux_preferences")

        return await toolbox.record_and_call(_inner, route="/v1/ux/preferences", method="POST")

    log.info("UX preferences routes registered")


__all__ = ["register_ux_preferences_routes"]
