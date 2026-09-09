"""Spotify CDP session API routes.

Provides endpoints for checking and disconnecting the Spotify CDP playback
session. Spotify login is handled only by the in-app BrowserAuth overlay.

Endpoints:
    GET  /v1/spotify/cdp/status  - Check Spotify CDP login status
    POST /v1/spotify/cdp/disconnect - Disconnect Spotify CDP session
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/spotify/cdp", tags=["spotify-cdp"])
status_router = APIRouter(prefix="/v1/spotify", tags=["spotify"])
_cdp_disconnect_in_progress = False


def _get_controller(user_id: str) -> Any:
    """Import and return the SpotifyCDPController singleton.

    Deferred import to avoid circular dependencies and to allow the
    module to load even when Spotify/Playwright is not installed.
    """
    from music.providers.spotify_cdp import get_cdp_controller

    return get_cdp_controller(user_id=user_id)


def _get_profile_path(user_id: str) -> str:
    """Return the Chrome profile path (deferred import)."""
    from music.spotify.cdp_controller import spotify_cdp_profile_path

    return str(spotify_cdp_profile_path(user_id))


def _has_cdp_session_cookies(user_id: str) -> bool:
    """Return whether the CDP Chrome profile has Spotify auth cookies."""
    try:
        from music.spotify.cdp_controller import has_cdp_session_cookies

        return bool(has_cdp_session_cookies(user_id=user_id))
    except Exception:
        logger.exception("Spotify CDP cookie probe failed")
        return False


def _mark_cdp_session_cookies_stale() -> None:
    """Clear the CDP cookie-presence cache after an explicit disconnect."""
    try:
        from music.spotify import cdp_controller

        reset_cache = getattr(cdp_controller, "mark_cdp_session_cookies_stale", None) or getattr(
            cdp_controller,
            "_invalidate_cdp_session_cache",
            None,
        )
        if callable(reset_cache):
            reset_cache()
    except Exception:
        logger.exception("Spotify CDP cookie cache reset failed")


def _coerce_login_status(raw_status: Any) -> tuple[bool, dict[str, Any]]:
    """Normalize bool and future dict-shaped controller login results."""
    if isinstance(raw_status, dict):
        logged_in = bool(
            raw_status.get("logged_in") or raw_status.get("is_logged_in") or raw_status.get("authenticated")
        )
        return logged_in, raw_status
    return bool(raw_status), {}


def _controller_supports_login_status(controller: Any) -> bool:
    """Return True when the real controller exposes structured login status."""
    return callable(getattr(type(controller), "get_login_status", None))


def _public_login_detail(login_details: dict[str, Any], key: str, default: Any = None) -> Any:
    value = login_details.get(key, default)
    if key == "login_error_code" and value is not None:
        allowed = {"identifier_rejected", "interactive_challenge_required"}
        return value if value in allowed else "login_state_error"
    return value


def _get_cached_cookie_bridge_status(user_id: str) -> bool:
    """Return whether BrowserAuth has cached Spotify cookies for CDP injection."""
    try:
        from music.spotify.cookie_bridge import get_cached_spotify_cookies

        return bool(get_cached_spotify_cookies(user_id=user_id))
    except Exception:
        logger.exception("Spotify cookie bridge status check failed")
        return False


def _get_browser_auth_snapshot(user_id: str) -> dict[str, Any]:
    """Return the BrowserAuth status cache without requiring the gated route."""
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        manager = get_browser_auth_manager(user_id)
        status = manager.get_provider_status("spotify")
        payload = status.to_dict()
        payload["available"] = True
        return payload
    except Exception:
        logger.exception("Spotify BrowserAuth status check failed")
        return {
            "name": "spotify",
            "display_name": "Spotify",
            "logged_in": False,
            "session_expired": False,
            "available": False,
        }


async def _build_cdp_status_payload(user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the CDP status payload plus optional controller detail fields."""
    try:
        controller = _get_controller(user_id)
        profile_path = _get_profile_path(user_id)
    except Exception:
        raise

    cookie_session_present = False if _cdp_disconnect_in_progress else _has_cdp_session_cookies(user_id)
    profile_exists = Path(profile_path).exists() or cookie_session_present
    login_details: dict[str, Any] = {}

    if controller.is_connected:
        try:
            if _controller_supports_login_status(controller):
                raw_logged_in = await asyncio.to_thread(controller.get_login_status)
            else:
                raw_logged_in = await asyncio.to_thread(controller.is_logged_in)
            logged_in, login_details = _coerce_login_status(raw_logged_in)
        except Exception:
            logger.exception("Spotify CDP login check failed")
            logged_in = cookie_session_present
    else:
        logged_in = cookie_session_present

    return (
        {
            "logged_in": logged_in,
            "profile_exists": profile_exists,
            "connected": controller.is_connected,
            "auth_state": _public_login_detail(
                login_details,
                "auth_state",
                (
                    "authenticated_cookie"
                    if logged_in
                    else ("login_required" if controller.is_connected else "not_connected")
                ),
            ),
            "login_error_code": _public_login_detail(login_details, "login_error_code"),
            "challenge_required": bool(_public_login_detail(login_details, "challenge_required", False)),
        },
        login_details,
    )


@router.get(
    "/status",
    dependencies=[Depends(require_auth)],
)
async def get_spotify_cdp_status(user_id: str = Depends(get_current_user_id)) -> Any:
    """Return current Spotify CDP login status.

    Lightweight check — does NOT launch Chrome or connect.
    If Chrome is already connected, checks login state via DOM.
    If not connected, uses the persisted CDP profile cookies as the ready
    signal used by playback.
    """
    try:
        payload, _ = await _build_cdp_status_payload(user_id)
    except Exception:
        logger.exception("Spotify CDP controller not available")
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "spotify_cdp_unavailable",
                "Spotify CDP controller is not available",
            ),
        )

    return success_response(payload)


@status_router.get(
    "/status",
    dependencies=[Depends(require_auth)],
)
async def get_spotify_status(user_id: str = Depends(get_current_user_id)) -> Any:
    """Return aggregate Spotify auth state for Settings UI."""
    try:
        cdp_status, login_details = await _build_cdp_status_payload(user_id)
    except Exception:
        logger.exception("Spotify CDP controller not available")
        cdp_status = {
            "logged_in": False,
            "profile_exists": False,
            "connected": False,
            "auth_state": "unavailable",
            "login_error_code": None,
            "challenge_required": False,
        }
        login_details = {}

    browser_auth = _get_browser_auth_snapshot(user_id)
    cookie_bridge_cached = _get_cached_cookie_bridge_status(user_id)
    browser_logged_in = bool(browser_auth.get("logged_in"))
    cdp_logged_in = bool(cdp_status.get("logged_in"))
    logged_in = cdp_logged_in or browser_logged_in or cookie_bridge_cached

    if cdp_status.get("connected") and cdp_logged_in:
        auth_source = "cdp_dom"
    elif cdp_logged_in:
        auth_source = "cdp_cookies"
    elif browser_logged_in:
        auth_source = "browser_auth"
    elif cookie_bridge_cached:
        auth_source = "cookie_bridge"
    else:
        auth_source = "none"

    account_email = (
        login_details.get("email")
        or login_details.get("account_email")
        or browser_auth.get("email")
        or browser_auth.get("account_email")
    )

    return success_response(
        {
            "provider": "spotify",
            "display_name": "Spotify",
            "logged_in": logged_in,
            "connected": logged_in,
            "profile_exists": bool(cdp_status.get("profile_exists")),
            "account_email": account_email,
            "auth_source": auth_source,
            "auth_state": cdp_status.get("auth_state"),
            "login_error_code": cdp_status.get("login_error_code"),
            "challenge_required": bool(cdp_status.get("challenge_required")),
            "cdp": cdp_status,
            "browser_auth": browser_auth,
            "cookie_bridge_cached": cookie_bridge_cached,
        }
    )


@router.post(
    "/disconnect",
    dependencies=[Depends(require_auth)],
)
async def disconnect_spotify_cdp(user_id: str = Depends(get_current_user_id)) -> Any:
    """Disconnect Spotify CDP and clear the dedicated Chrome profile."""
    global _cdp_disconnect_in_progress
    try:
        controller = _get_controller(user_id)
        profile_path = Path(_get_profile_path(user_id))
    except Exception:
        logger.exception("Spotify CDP controller not available")
        return JSONResponse(
            status_code=503,
            content=failure_response(
                "spotify_cdp_unavailable",
                "Spotify CDP controller is not available",
            ),
        )

    _cdp_disconnect_in_progress = True
    _mark_cdp_session_cookies_stale()
    try:
        await asyncio.to_thread(controller.disconnect)
        if profile_path.exists():
            await asyncio.to_thread(shutil.rmtree, profile_path, True)
        return success_response({"message": "Spotify disconnected"})
    except Exception:
        logger.exception("Failed to disconnect Spotify CDP session")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "spotify_cdp_disconnect_failed",
                "Failed to disconnect Spotify",
            ),
        )
    finally:
        _mark_cdp_session_cookies_stale()
        _cdp_disconnect_in_progress = False


def create_spotify_cdp_router() -> APIRouter:
    """Factory function for the Spotify CDP router."""
    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(status_router)
    return combined


__all__ = [
    "create_spotify_cdp_router",
    "get_spotify_status",
    "router",
]
