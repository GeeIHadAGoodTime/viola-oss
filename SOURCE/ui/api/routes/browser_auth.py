"""Browser authentication API routes.

Provides endpoints for checking and managing browser-native music provider
authentication.  Users log in through the QWebEngineView directly (real
provider websites); these endpoints report and control that session state.

Endpoints:
    GET  /v1/browser/auth/status                  - All providers auth status
    GET  /v1/browser/auth/status/{provider_name}  - Single provider auth status
    POST /v1/browser/auth/login/{provider_name}   - Initiate login flow
    POST /v1/browser/auth/refresh/{provider_name} - Force re-check auth state
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/browser/auth", tags=["browser-auth"])


class DefaultProviderRequest(BaseModel):
    provider: str


def _get_auth_manager(user_id: str) -> Any:
    """Import and return the authenticated user's BrowserAuthManager.

    Deferred import to avoid circular dependencies and to allow the
    module to load even when the browser subsystem is not installed.

    Returns:
        The BrowserAuthManager instance, or ``None`` if the browser
        subsystem is not available.
    """
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        return get_browser_auth_manager(user_id)
    except ImportError:
        logger.debug("Browser auth manager not available")
        return None


def _get_default_provider_id(user_id: str) -> str | None:
    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get("active_music_provider_id", None, user_id=user_id)
        return str(value) if value else None
    except Exception:
        logger.debug("Could not read default music provider setting", exc_info=True)
        return None


def _status_payload(status: Any, default_provider: str | None) -> dict[str, Any]:
    payload = status.to_dict()
    payload["is_default"] = payload.get("name") == default_provider
    return payload


@router.get(
    "/status",
    dependencies=[Depends(require_auth)],
)
async def get_auth_status(user_id: str = Depends(get_current_user_id)) -> Any:
    """Return auth status for all known providers.

    Response::

        {
            "ok": true,
            "data": {
                "providers": [
                    {
                        "name": "youtube_music",
                        "display_name": "YouTube Music",
                        "logged_in": true,
                        "session_expired": false,
                        "last_checked": 1700000000.0,
                        "login_url": "https://music.youtube.com",
                        "icon_url": "https://music.youtube.com/img/favicon_144.png"
                    },
                    ...
                ]
            }
        }
    """
    mgr = _get_auth_manager(user_id)
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "service_unavailable",
                "Browser auth manager is not available",
            ),
        )

    try:
        all_status = mgr.get_auth_status()
        default_provider = _get_default_provider_id(user_id)
        providers = [_status_payload(status, default_provider) for status in all_status.values()]
        return success_response({"providers": providers})
    except Exception:
        logger.exception("Failed to get browser auth status")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "auth_status_failed",
                "Couldn't load your sign-in status. Please try again.",
            ),
        )


@router.get(
    "/status/{provider_name}",
    dependencies=[Depends(require_auth)],
)
async def get_provider_status(provider_name: str, user_id: str = Depends(get_current_user_id)) -> Any:
    """Check login status for a specific provider.

    Injects recipe JS to check if logged in and returns the result.

    Response::

        {
            "ok": true,
            "data": {
                "name": "youtube_music",
                "display_name": "YouTube Music",
                "logged_in": true,
                "session_expired": false,
                "last_checked": 1700000000.0,
                "login_url": "https://music.youtube.com",
                "icon_url": "https://music.youtube.com/img/favicon_144.png"
            }
        }
    """
    mgr = _get_auth_manager(user_id)
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "service_unavailable",
                "Browser auth manager is not available",
            ),
        )

    try:
        status = mgr.get_provider_status(provider_name)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content=failure_response(
                "provider_not_found",
                "Unknown provider: %s" % provider_name,
            ),
        )

    try:
        await mgr.check_login_state(provider_name)
        # Re-fetch status after the check (it may have been updated)
        status = mgr.get_provider_status(provider_name)
        return success_response(_status_payload(status, _get_default_provider_id(user_id)))
    except Exception:
        logger.exception("Failed to check login status for %s", provider_name)
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "auth_status_unavailable",
                "Login status is temporarily unavailable right now.",
            ),
        )


@router.post(
    "/login/{provider_name}",
    dependencies=[Depends(require_auth)],
)
async def initiate_login(provider_name: str, user_id: str = Depends(get_current_user_id)) -> Any:
    """Navigate QWebEngineView to provider login page.

    Response::

        {
            "ok": true,
            "data": {
                "provider": "youtube_music",
                "login_url": "https://music.youtube.com",
                "message": "Navigate to the login page in the browser view"
            }
        }
    """
    mgr = _get_auth_manager(user_id)
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "service_unavailable",
                "Browser auth manager is not available",
            ),
        )

    try:
        login_url = mgr.initiate_login(provider_name)
        return success_response(
            {
                "provider": provider_name,
                "login_url": login_url,
                "controller_attached": mgr._controller is not None,
                "message": "Navigate to the login page in the browser view",
            }
        )
    except KeyError:
        return JSONResponse(
            status_code=404,
            content=failure_response(
                "provider_not_found",
                "Unknown provider: %s" % provider_name,
            ),
        )
    except RuntimeError as exc:
        logger.warning("Browser login initiation failed for %s: %s", provider_name, exc)
        return JSONResponse(
            status_code=400,
            content=failure_response(
                "login_unavailable",
                # SECURITY: Don't expose RuntimeError internals to client
                "Login is not available for this provider right now. Please try again.",
            ),
        )
    except Exception:
        logger.exception("Failed to initiate login for %s", provider_name)
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "login_failed",
                "Couldn't start the sign-in process. Please try again.",
            ),
        )


@router.post(
    "/refresh/{provider_name}",
    dependencies=[Depends(require_auth)],
)
async def refresh_auth_check(provider_name: str, user_id: str = Depends(get_current_user_id)) -> Any:
    """Force re-check of auth status for a provider.

    Runs both login detection and session expiry checks, then returns
    the updated status.  Useful after a login flow completes.

    Response::

        {
            "ok": true,
            "data": {
                "name": "youtube_music",
                "display_name": "YouTube Music",
                "logged_in": true,
                "session_expired": false,
                "last_checked": 1700000000.0,
                "login_url": "https://music.youtube.com",
                "icon_url": "https://music.youtube.com/img/favicon_144.png"
            }
        }
    """
    mgr = _get_auth_manager(user_id)
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "service_unavailable",
                "Browser auth manager is not available",
            ),
        )

    try:
        mgr.get_provider_status(provider_name)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content=failure_response(
                "provider_not_found",
                "Unknown provider: %s" % provider_name,
            ),
        )

    try:
        await mgr.check_login_state(provider_name)
        await mgr.check_session_expired(provider_name)

        status = mgr.get_provider_status(provider_name)
        return success_response(_status_payload(status, _get_default_provider_id(user_id)))
    except Exception:
        logger.exception("Failed to refresh auth check for %s", provider_name)
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "auth_refresh_failed",
                "Couldn't refresh your sign-in status. Please try again.",
            ),
        )


@router.post(
    "/default",
    dependencies=[Depends(require_auth)],
)
async def set_default_provider(body: DefaultProviderRequest, user_id: str = Depends(get_current_user_id)) -> Any:
    """Set the browser music provider used when the user does not name one."""
    provider_name = body.provider.strip()
    mgr = _get_auth_manager(user_id)
    if mgr is None:
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "service_unavailable",
                "Browser auth manager is not available",
            ),
        )

    try:
        mgr.get_provider_status(provider_name)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content=failure_response(
                "provider_not_found",
                "Unknown provider: %s" % provider_name,
            ),
        )

    try:
        from ui.settings_manager import get_settings_manager

        settings_mgr = get_settings_manager()
        settings_mgr.set_user_setting(user_id, "active_music_provider_id", provider_name)
        return success_response({"provider": provider_name, "is_default": True})
    except Exception:
        logger.exception("Failed to set default browser provider: %s", provider_name)
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "default_provider_failed",
                "Couldn't save your default music provider. Please try again.",
            ),
        )


def create_browser_auth_router() -> APIRouter:
    """Factory function for the browser auth router."""
    return router


__all__ = [
    "create_browser_auth_router",
    "router",
]
