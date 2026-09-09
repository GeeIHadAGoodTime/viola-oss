"""Agent tool handlers for music-provider connection flows."""

from __future__ import annotations

import asyncio
from typing import Any

from config.settings import get_runtime_base_url
from core.constants import TIMEOUT_EXTENDED, TIMEOUT_MINUTE
from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

YOUTUBE_MUSIC_LOGIN_URL = (
    "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fmusic.youtube.com%2F"
)

SUPPORTED_PROVIDER_IDS: tuple[str, ...] = (
    "spotify",
    "youtube_music",
)

_PROVIDER_ALIASES: dict[str, str] = {
    "spotify": "spotify",
    "spotify_cdp": "spotify",
    "youtube": "youtube_music",
    "youtube music": "youtube_music",
    "youtube_music": "youtube_music",
}

_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "spotify": "Spotify",
    "youtube_music": "YouTube Music",
}

_PROVIDER_LOGIN_URLS: dict[str, str] = {
    "spotify": "https://open.spotify.com",
    "youtube_music": YOUTUBE_MUSIC_LOGIN_URL,
}


def supported_music_providers() -> list[dict[str, str]]:
    """Return all music providers the tool accepts, without ranking them."""
    return [
        {
            "id": provider_id,
            "display_name": _PROVIDER_DISPLAY_NAMES[provider_id],
            "login_url": _PROVIDER_LOGIN_URLS[provider_id],
        }
        for provider_id in SUPPORTED_PROVIDER_IDS
    ]


def _normalize_provider(provider: str, *, allow_all: bool = False) -> str:
    raw = str(provider or "").strip().lower().replace("-", "_")
    if allow_all and raw in {"", "all", "providers", "music_providers"}:
        return "all"
    normalized = _PROVIDER_ALIASES.get(raw)
    if normalized is None or normalized not in SUPPORTED_PROVIDER_IDS:
        raise ValueError("Unknown music provider: %s" % provider)
    return normalized


def _runtime_url(path: str) -> str:
    return "%s%s" % (get_runtime_base_url().rstrip("/"), path)


def _auth_headers() -> dict[str, str]:
    from ui.security.bootstrap import load_bootstrap_api_key

    headers: dict[str, str] = {}
    api_key = load_bootstrap_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


async def _post_runtime(
    path: str, payload: dict[str, Any] | None = None, *, timeout: float = TIMEOUT_MINUTE
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_runtime_url(path), json=payload or {}, headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {
            "ok": False,
            "error": "Runtime returned a non-object response",
            "_status_code": resp.status_code,
        }
    data.setdefault("_status_code", resp.status_code)
    return data


async def _get_runtime(path: str, *, timeout: float = TIMEOUT_EXTENDED) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_runtime_url(path), headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {
            "ok": False,
            "error": "Runtime returned a non-object response",
            "_status_code": resp.status_code,
        }
    data.setdefault("_status_code", resp.status_code)
    return data


def _response_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _response_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if error:
        return str(error)
    return "Request failed"


def _error_envelope(provider_id: str, message: str, *, code: str = "music_provider_error") -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "provider": provider_id,
        "display_name": _PROVIDER_DISPLAY_NAMES.get(provider_id, provider_id),
        "error": message,
        "error_code": code,
        "provider_chrome_pid": None,
        "login_url_to_show_user": _PROVIDER_LOGIN_URLS.get(provider_id),
        "supported_providers": supported_music_providers(),
    }


def _with_connection_next_action(result: dict[str, Any]) -> dict[str, Any]:
    # R4-P1-L (2026-05-30): retired ``next_action`` /
    # ``recommended_next_tool`` / ``recommended_next_tool_args``
    # directive fields. These were more prescriptive than the
    # ``next_step`` prose deleted in R3-P1-E — they named the exact
    # next tool call the model should make. The model reads
    # ``connected: false`` + the structured ``provider`` /
    # ``login_url`` fields and decides whether to call
    # ``connect_music_provider``. Function kept (returns result
    # unchanged) so call sites don't need to know the injection is
    # gone.
    return result


def _spotify_chrome_pid() -> int | None:
    try:
        from music.providers.spotify_cdp import get_cdp_controller

        controller = get_cdp_controller()
        chrome_proc = getattr(controller, "_chrome_proc", None)
        if chrome_proc is not None:
            try:
                if chrome_proc.poll() is None:
                    return int(chrome_proc.pid)
            except Exception:
                logger.debug("Spotify CDP launched-process PID check failed")
        pid_finder = getattr(controller, "_find_chrome_cdp_pid", None)
        if callable(pid_finder):
            pid = pid_finder()
            if pid:
                return int(pid)
    except Exception:
        logger.debug("Spotify CDP PID lookup failed")
    return None


def _spotify_cdp_session_present() -> bool:
    """Cookie-store probe for the external CDP Chrome profile.

    The Qt overlay's `logged_in` flag only reflects Qt's cookie jar, but Spotify
    playback runs in a SEPARATE Chrome process whose cookies live in
    `.viola-chrome-profile/Default/Network/Cookies`. After a Viola restart the
    Qt jar is empty, so the recipe reports `logged_in=False` even though the
    CDP profile still has a valid sp_dc / sp_key (1-year expiry). Treating that
    as "needs login" forces a redundant re-auth on every cold start.
    """
    try:
        from music.spotify.cdp_controller import has_cdp_session_cookies

        return bool(has_cdp_session_cookies())
    except Exception:
        return False


def _spotify_status_from_data(data: dict[str, Any]) -> dict[str, Any]:
    logged_in = bool(data.get("logged_in"))
    connected = bool(data.get("connected"))
    chrome_pid = _spotify_chrome_pid()
    cdp_session_present = False
    if not logged_in:
        cdp_session_present = _spotify_cdp_session_present()
        if cdp_session_present:
            logged_in = True
    if logged_in:
        status = "connected"
    elif connected or chrome_pid:
        status = "awaiting_user_login"
    else:
        status = "not_connected"

    return _with_connection_next_action(
        {
            "ok": True,
            "status": status,
            "provider": "spotify",
            "display_name": _PROVIDER_DISPLAY_NAMES["spotify"],
            "connected": logged_in,
            "logged_in": logged_in,
            "session_expired": False,
            "profile_exists": bool(data.get("profile_exists")),
            "provider_chrome_pid": chrome_pid,
            "login_url_to_show_user": _PROVIDER_LOGIN_URLS["spotify"],
            "needs_login": not logged_in,
            "supported_providers": supported_music_providers(),
        }
    )


def _browser_status_from_data(provider_id: str, data: dict[str, Any]) -> dict[str, Any]:
    logged_in = bool(data.get("logged_in"))
    session_expired = bool(data.get("session_expired"))
    if logged_in:
        status = "connected"
    elif session_expired:
        status = "session_expired"
    else:
        status = "not_connected"

    login_url = data.get("login_url")
    if (
        not isinstance(login_url, str)
        or not login_url.strip()
        or (
            provider_id == "youtube_music"
            and login_url.rstrip("/")
            in {
                "https://www.youtube.com",
                "https://music.youtube.com",
            }
        )
    ):
        login_url = _PROVIDER_LOGIN_URLS[provider_id]

    result: dict[str, Any] = {
        "ok": True,
        "status": status,
        "provider": provider_id,
        "display_name": str(data.get("display_name") or _PROVIDER_DISPLAY_NAMES[provider_id]),
        "connected": logged_in,
        "logged_in": logged_in,
        "session_expired": session_expired,
        "provider_chrome_pid": None,
        "login_url_to_show_user": login_url,
        "needs_login": not logged_in,
        "supported_providers": supported_music_providers(),
    }
    if "last_checked" in data:
        result["last_checked"] = data.get("last_checked")
    return _with_connection_next_action(result)


async def _spotify_status() -> dict[str, Any]:
    # Spotify is special-cased: even when the Qt overlay's recipe reports
    # logged_in=False, the external CDP Chrome profile may still hold a valid
    # sp_dc / sp_key (1-year expiry). _spotify_status_from_data layers a
    # cookie-store probe on top of the browser auth payload so the agent
    # doesn't pointlessly re-prompt for login on every Viola restart.
    payload = await _get_runtime("/v1/browser/auth/status/spotify")
    if payload.get("ok") is False:
        return _error_envelope("spotify", _response_error(payload), code="browser_provider_status_failed")
    return _spotify_status_from_data(_response_data(payload))


async def _browser_provider_status(provider_id: str) -> dict[str, Any]:
    payload = await _get_runtime("/v1/browser/auth/status/%s" % provider_id)
    if payload.get("ok") is False:
        return _error_envelope(provider_id, _response_error(payload), code="browser_provider_status_failed")
    return _browser_status_from_data(provider_id, _response_data(payload))


async def _provider_status(provider_id: str) -> dict[str, Any]:
    if provider_id == "spotify":
        return await _spotify_status()
    return await _browser_provider_status(provider_id)


async def _connect_spotify() -> dict[str, Any]:
    return await _connect_browser_provider("spotify")


async def _connect_browser_provider(provider_id: str) -> dict[str, Any]:
    status_before = await _browser_provider_status(provider_id)
    if status_before.get("connected") is True:
        return status_before

    payload = await _post_runtime("/v1/browser/auth/login/%s" % provider_id, timeout=TIMEOUT_EXTENDED)
    if payload.get("ok") is False:
        return _error_envelope(provider_id, _response_error(payload), code="browser_provider_login_failed")

    data = _response_data(payload)
    if data.get("controller_attached") is False:
        result = _error_envelope(
            provider_id,
            "Browser login requires the Viola desktop browser controller.",
            code="browser_controller_unavailable",
        )
        result["login_url_to_show_user"] = data.get("login_url") or _PROVIDER_LOGIN_URLS[provider_id]
        return result

    status_after = await _browser_provider_status(provider_id)
    if status_after.get("ok") is False:
        return status_after
    status_after["status"] = "connected" if status_after.get("connected") else "awaiting_user_login"
    status_after["login_started"] = True
    status_after["login_url_to_show_user"] = data.get("login_url") or status_after.get("login_url_to_show_user")
    return status_after


async def connect_music_provider_data(provider: str) -> dict[str, Any]:
    try:
        provider_id = _normalize_provider(provider)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "error",
            "provider": provider,
            "error": str(exc),
            "error_code": "unknown_music_provider",
            "provider_chrome_pid": None,
            "login_url_to_show_user": None,
            "supported_providers": supported_music_providers(),
        }

    try:
        if provider_id == "spotify":
            return await _connect_spotify()
        return await _connect_browser_provider(provider_id)
    except Exception as exc:
        logger.exception("connect_music_provider failed for provider=%s", provider_id)
        return _error_envelope(provider_id, str(exc), code="connect_music_provider_failed")


async def check_music_provider_status_data(provider: str = "all") -> dict[str, Any]:
    try:
        provider_id = _normalize_provider(provider, allow_all=True)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "error",
            "provider": provider,
            "error": str(exc),
            "error_code": "unknown_music_provider",
            "provider_chrome_pid": None,
            "login_url_to_show_user": None,
            "supported_providers": supported_music_providers(),
        }

    try:
        if provider_id == "all":
            provider_results = await asyncio.gather(
                *(_provider_status(candidate) for candidate in SUPPORTED_PROVIDER_IDS)
            )
            connected = [
                str(result.get("provider"))
                for result in provider_results
                if result.get("ok") is True and result.get("connected") is True
            ]
            result = {
                "ok": True,
                "status": "ok",
                "providers": provider_results,
                "connected_providers": connected,
                "supported_providers": supported_music_providers(),
            }
            # R4-P1-L: retired next_action / recommended_next_tool
            # directive fields. The model reads connected_providers list
            # and decides whether to call connect_music_provider.
            return result
        return await _provider_status(provider_id)
    except Exception as exc:
        logger.exception("check_music_provider_status failed for provider=%s", provider_id)
        return _error_envelope(provider_id, str(exc), code="check_music_provider_status_failed")


def _envelope_ok(payload: dict[str, Any]) -> bool:
    """Read the verdict out of the payload instead of asserting success.

    Every ``*_data`` path in this module stamps an explicit ``ok`` -- the
    status builders set ``ok: True``, ``_error_envelope`` and the
    unknown-provider branches set ``ok: False``. Wrapping the payload in a
    hard-coded ``ToolResult(ok=True, ...)`` threw that verdict away: an error
    envelope surfaced to the model as a successful tool call, and any future
    error path that forgets a literal ``ok: False`` would too. Missing/other
    is treated as NOT ok so an envelope that never states success cannot be
    read as success.
    """
    return payload.get("ok") is True


def _envelope_result(payload: dict[str, Any]) -> ToolResult:
    if _envelope_ok(payload):
        return ToolResult(ok=True, data=payload)
    error_code = str(payload.get("error_code") or "").strip()
    return ToolResult(
        ok=False,
        data=payload,
        error=_response_error(payload),
        error_category=error_code or None,
    )


async def connect_music_provider_handler(provider: str) -> ToolResult:
    return _envelope_result(await connect_music_provider_data(provider))


async def check_music_provider_status_handler(provider: str = "all") -> ToolResult:
    return _envelope_result(await check_music_provider_status_data(provider))
