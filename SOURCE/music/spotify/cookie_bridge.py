"""Bridge Spotify QWebEngine cookies into the CDP Chrome profile."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

SPOTIFY_COOKIE_URLS = [
    "https://open.spotify.com",
    "https://accounts.spotify.com",
    "https://www.spotify.com",
]

# Spotify's sign-in cookie: set on .spotify.com only after a real sign-in.
SPOTIFY_AUTH_COOKIE = "sp_dc"

_LOAD_TIMEOUT_SECONDS = 1.0
_cookie_lock = threading.RLock()
_cookie_cache_by_user: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}


def _to_text(value: Any) -> str:
    if hasattr(value, "data") and callable(value.data):
        value = value.data()
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="ignore")
    return str(value or "")


def _is_spotify_domain(domain: str) -> bool:
    normalized = domain.strip().lower().lstrip(".")
    return normalized == "spotify.com" or normalized.endswith(".spotify.com")


def _cookie_key(cookie: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("domain") or "").lower().lstrip("."),
        str(cookie.get("path") or "/"),
        str(cookie.get("name") or ""),
    )


def _same_site_value(cookie: Any) -> str | None:
    policy_fn = getattr(cookie, "sameSitePolicy", None)
    if not callable(policy_fn):
        return None

    raw_policy = policy_fn()
    raw_name = str(getattr(raw_policy, "name", raw_policy)).lower()
    if "strict" in raw_name:
        return "Strict"
    if "lax" in raw_name:
        return "Lax"
    if "none" in raw_name:
        return "None"
    return None


def _cookie_expires(cookie: Any) -> float | None:
    is_session_fn = getattr(cookie, "isSessionCookie", None)
    if callable(is_session_fn) and is_session_fn():
        return None

    expiration_fn = getattr(cookie, "expirationDate", None)
    if not callable(expiration_fn):
        return None

    expiration = expiration_fn()
    epoch_fn = getattr(expiration, "toSecsSinceEpoch", None)
    if callable(epoch_fn):
        epoch = int(epoch_fn())
        return float(epoch) if epoch > 0 else None

    timestamp_fn = getattr(expiration, "timestamp", None)
    if callable(timestamp_fn):
        epoch = int(timestamp_fn())
        return float(epoch) if epoch > 0 else None

    return None


def _qnetwork_cookie_to_playwright(cookie: Any) -> dict[str, Any] | None:
    name_fn = getattr(cookie, "name", None)
    value_fn = getattr(cookie, "value", None)
    domain_fn = getattr(cookie, "domain", None)
    path_fn = getattr(cookie, "path", None)
    if not all(callable(fn) for fn in (name_fn, value_fn, domain_fn, path_fn)):
        return None

    name = _to_text(name_fn()).strip()
    value = _to_text(value_fn())
    domain = _to_text(domain_fn()).strip()
    path = _to_text(path_fn()).strip() or "/"
    if not name or not domain or not _is_spotify_domain(domain):
        return None

    pw_cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": bool(cookie.isSecure()) if hasattr(cookie, "isSecure") else True,
        "httpOnly": bool(cookie.isHttpOnly()) if hasattr(cookie, "isHttpOnly") else False,
    }

    same_site = _same_site_value(cookie)
    if same_site:
        pw_cookie["sameSite"] = same_site

    expires = _cookie_expires(cookie)
    if expires is not None:
        pw_cookie["expires"] = expires

    return pw_cookie


def _normalize_playwright_cookie(cookie: Mapping[str, Any]) -> dict[str, Any] | None:
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    domain = str(cookie.get("domain") or "").strip()
    path = str(cookie.get("path") or "/").strip() or "/"
    if not name or not domain or not _is_spotify_domain(domain):
        return None

    normalized: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
        "secure": bool(cookie.get("secure", True)),
        "httpOnly": bool(cookie.get("httpOnly", cookie.get("http_only", False))),
    }
    same_site = cookie.get("sameSite") or cookie.get("same_site")
    if same_site in {"Strict", "Lax", "None"}:
        normalized["sameSite"] = same_site
    expires = cookie.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        normalized["expires"] = float(expires)
    return normalized


def cache_spotify_cookies(cookies: list[Mapping[str, Any]], *, user_id: str) -> list[dict[str, Any]]:
    """Store Playwright-shaped Spotify cookies for one authenticated user."""
    if not user_id:
        raise ValueError("user_id is required to cache Spotify cookies")

    normalized = [
        cookie for cookie in (_normalize_playwright_cookie(raw_cookie) for raw_cookie in cookies) if cookie is not None
    ]
    with _cookie_lock:
        bucket = _cookie_cache_by_user.setdefault(user_id, {})
        for cookie in normalized:
            bucket[_cookie_key(cookie)] = dict(cookie)

    logger.info("Cached %d Spotify QWebEngine cookies for user %s", len(normalized), user_id)
    return get_cached_spotify_cookies(user_id=user_id)


def get_cached_spotify_cookies(*, user_id: str) -> list[dict[str, Any]]:
    """Return cached Spotify cookies for one concrete authenticated user."""
    if not user_id:
        raise ValueError("user_id is required to read cached Spotify cookies")
    with _cookie_lock:
        bucket = _cookie_cache_by_user.get(user_id, {})
        return [dict(cookie) for cookie in bucket.values()]


def _profile_from_webview(webview_controller: Any | None) -> Any | None:
    if webview_controller is None:
        return None
    page_fn = getattr(webview_controller, "page", None)
    if not callable(page_fn):
        return None
    page = page_fn()
    profile_fn = getattr(page, "profile", None)
    return profile_fn() if callable(profile_fn) else None


def _on_qt_main_thread() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        return bool(app is not None and app.thread() == QThread.currentThread())
    except Exception:
        return False


def _collect_qt_profile_cookies(profile: Any, timeout_seconds: float) -> list[dict[str, Any]]:
    from PySide6.QtCore import QEventLoop, QTimer

    cookie_store = profile.cookieStore()
    collected: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _on_cookie_added(cookie: Any) -> None:
        converted = _qnetwork_cookie_to_playwright(cookie)
        if converted is not None:
            collected[_cookie_key(converted)] = converted

    cookie_store.cookieAdded.connect(_on_cookie_added)
    try:
        cookie_store.loadAllCookies()
        loop = QEventLoop()
        QTimer.singleShot(max(1, int(timeout_seconds * 1000)), loop.quit)
        loop.exec()
    finally:
        try:
            cookie_store.cookieAdded.disconnect(_on_cookie_added)
        except Exception:
            logger.debug("Spotify cookie bridge: cookieAdded disconnect skipped")

    return [dict(cookie) for cookie in collected.values()]


def has_spotify_auth_cookie(cookies: list[Mapping[str, Any]] | None) -> bool:
    """Return True when *cookies* hold Spotify's sign-in cookie.

    Spotify sets ``sp_dc`` on ``.spotify.com`` only after a successful sign-in
    (the CDP side checks the same cookie, see ``has_cdp_session_cookies``).
    Anonymous visits get other cookies (``sp_t``, ``sp_landing``), so "some
    Spotify cookies exist" is not proof of a sign-in; this is.
    """
    for cookie in cookies or []:
        if not isinstance(cookie, Mapping):
            continue
        if str(cookie.get("name") or "") != SPOTIFY_AUTH_COOKIE:
            continue
        if not str(cookie.get("value") or ""):
            continue
        if _is_spotify_domain(str(cookie.get("domain") or "")):
            return True
    return False


def read_spotify_cookies_from_qweb(
    webview_controller: Any | None = None,
    *,
    dispatch_to_main: Callable[[Callable[[], None]], bool] | None = None,
    timeout_seconds: float = _LOAD_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    """Read Spotify cookies from the QWebEngine profile WITHOUT caching them.

    Returns the cookies (possibly an empty list when the store has none), or
    ``None`` when the store could not be read at all (no webview, no Qt
    dispatcher, timeout). Callers use ``None`` to mean "unknown", never
    "signed out".

    The Qt cookie store must be read on the Qt main thread. Runtime callers
    pass BrowserPlaybackController._post_to_main as dispatch_to_main. This
    blocks the calling thread for about ``timeout_seconds``; async callers run
    it in a worker thread.
    """
    profile = _profile_from_webview(webview_controller)
    if profile is None:
        return None

    if _on_qt_main_thread():
        return _collect_qt_profile_cookies(profile, timeout_seconds)
    if dispatch_to_main is None:
        logger.debug("Spotify cookie bridge: no Qt dispatcher for QWebEngine extraction")
        return None

    done = threading.Event()
    result: dict[str, Any] = {}

    def _collect_on_main() -> None:
        try:
            result["cookies"] = _collect_qt_profile_cookies(profile, timeout_seconds)
        except Exception as exc:
            result["error"] = exc
        finally:
            done.set()

    if not dispatch_to_main(_collect_on_main):
        logger.debug("Spotify cookie bridge: Qt main-thread dispatch unavailable")
        return None
    if not done.wait(timeout_seconds + 2.0):
        logger.warning("Spotify cookie bridge: timed out reading QWebEngine cookies")
        return None
    if result.get("error") is not None:
        logger.debug("Spotify cookie bridge extraction failed: %s", result["error"])
        return None
    return list(result.get("cookies") or [])


def extract_spotify_cookies_from_qweb(
    webview_controller: Any | None = None,
    *,
    user_id: str | None = None,
    dispatch_to_main: Callable[[Callable[[], None]], bool] | None = None,
    timeout_seconds: float = _LOAD_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Extract Spotify cookies from the QWebEngine profile and cache them.

    The Qt cookie store must be read on the Qt main thread.  Runtime callers
    pass BrowserPlaybackController._post_to_main as dispatch_to_main.
    """
    cookies = read_spotify_cookies_from_qweb(
        webview_controller,
        dispatch_to_main=dispatch_to_main,
        timeout_seconds=timeout_seconds,
    )
    if not cookies:
        return []
    if not user_id:
        raise ValueError("user_id is required to cache Spotify cookies")
    return cache_spotify_cookies(cookies, user_id=user_id)


def clear_cached_spotify_cookies(*, user_id: str) -> None:
    """Forget the cached Spotify cookies for one user (Disconnect)."""
    if not user_id:
        raise ValueError("user_id is required to clear cached Spotify cookies")
    with _cookie_lock:
        _cookie_cache_by_user.pop(user_id, None)


def _delete_qt_profile_spotify_cookies(profile: Any, timeout_seconds: float) -> int:
    from PySide6.QtCore import QEventLoop, QTimer

    cookie_store = profile.cookieStore()
    found: list[Any] = []

    def _on_cookie_added(cookie: Any) -> None:
        domain_fn = getattr(cookie, "domain", None)
        if callable(domain_fn) and _is_spotify_domain(_to_text(domain_fn())):
            found.append(cookie)

    cookie_store.cookieAdded.connect(_on_cookie_added)
    try:
        cookie_store.loadAllCookies()
        loop = QEventLoop()
        QTimer.singleShot(max(1, int(timeout_seconds * 1000)), loop.quit)
        loop.exec()
    finally:
        try:
            cookie_store.cookieAdded.disconnect(_on_cookie_added)
        except Exception:
            logger.debug("Spotify cookie bridge: cookieAdded disconnect skipped")

    for cookie in found:
        cookie_store.deleteCookie(cookie)
    return len(found)


def delete_spotify_cookies_from_qweb(
    webview_controller: Any | None = None,
    *,
    dispatch_to_main: Callable[[Callable[[], None]], bool] | None = None,
    timeout_seconds: float = _LOAD_TIMEOUT_SECONDS,
) -> int | None:
    """Sign Viola's in-app browser out of Spotify by deleting its Spotify cookies.

    Disconnect must reach this store too: the sign-in check reads it, so a
    Disconnect that only cleared the playback browser's profile would be undone
    by the next status check. Returns how many cookies were deleted, or
    ``None`` when the store could not be reached.
    """
    profile = _profile_from_webview(webview_controller)
    if profile is None:
        return None
    if _on_qt_main_thread():
        return _delete_qt_profile_spotify_cookies(profile, timeout_seconds)
    if dispatch_to_main is None:
        return None

    done = threading.Event()
    result: dict[str, Any] = {}

    def _delete_on_main() -> None:
        try:
            result["count"] = _delete_qt_profile_spotify_cookies(profile, timeout_seconds)
        except Exception as exc:
            result["error"] = exc
        finally:
            done.set()

    if not dispatch_to_main(_delete_on_main):
        return None
    if not done.wait(timeout_seconds + 2.0):
        logger.warning("Spotify cookie bridge: timed out deleting QWebEngine cookies")
        return None
    if result.get("error") is not None:
        logger.debug("Spotify cookie bridge delete failed: %s", result["error"])
        return None
    return int(result.get("count") or 0)


def inject_spotify_cookies_into_playwright(playwright_context: Any, *, user_id: str) -> int:
    """Inject cached Spotify cookies into a Playwright browser context.

    Returns the number of new or changed cookies sent to Playwright.
    """
    cookies = get_cached_spotify_cookies(user_id=user_id)
    if not cookies:
        return 0

    existing: list[dict[str, Any]] = []
    cookies_fn = getattr(playwright_context, "cookies", None)
    if callable(cookies_fn):
        try:
            existing = cookies_fn(SPOTIFY_COOKIE_URLS)
        except TypeError:
            existing = cookies_fn()
        except Exception:
            logger.debug("Spotify cookie bridge: existing Playwright cookie read failed")

    existing_by_key = {
        _cookie_key(cookie): cookie
        for cookie in existing
        if isinstance(cookie, Mapping) and _is_spotify_domain(str(cookie.get("domain") or ""))
    }
    to_add = [
        cookie for cookie in cookies if existing_by_key.get(_cookie_key(cookie), {}).get("value") != cookie.get("value")
    ]
    if not to_add:
        return 0

    playwright_context.add_cookies(to_add)
    logger.info("Injected %d Spotify cookies into Playwright CDP context", len(to_add))
    return len(to_add)


__all__ = [
    "SPOTIFY_AUTH_COOKIE",
    "SPOTIFY_COOKIE_URLS",
    "cache_spotify_cookies",
    "clear_cached_spotify_cookies",
    "delete_spotify_cookies_from_qweb",
    "extract_spotify_cookies_from_qweb",
    "get_cached_spotify_cookies",
    "has_spotify_auth_cookie",
    "inject_spotify_cookies_into_playwright",
    "read_spotify_cookies_from_qweb",
]
