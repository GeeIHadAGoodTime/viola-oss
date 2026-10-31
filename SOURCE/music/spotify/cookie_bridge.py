"""Bridge Spotify QWebEngine cookies into the CDP Chrome profile."""

from __future__ import annotations

import threading
import time
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


# ---------------------------------------------------------------------------
# Reading the in-app browser's Spotify cookies
#
# QWebEngineCookieStore has no call that lists cookies. ``loadAllCookies()``
# was used for that, but on the Qt 6.8 that ships it reports nothing for
# cookies the store already holds (checked on this build: neither a persisted
# sp_dc nor cookies set earlier in the same session were reported), so every
# snapshot came back empty and the playback browser never got the sign-in.
# Two sources replace it:
#   * a tracker on cookieAdded / cookieRemoved, attached once to the profile,
#     sees every cookie Spotify sets or clears from then on (the sign-in);
#   * the profile's own cookie database on disk has the sign-ins from earlier
#     sessions. QtWebEngine keeps cookie values unencrypted there and the file
#     stays readable while Viola runs.
# ---------------------------------------------------------------------------

_WINDOWS_EPOCH_OFFSET_SECONDS = 11644473600
_SAME_SITE_BY_DB_VALUE = {0: "None", 1: "Lax", 2: "Strict"}


class _SpotifyCookieTracker:
    """Spotify cookies the in-app browser added or removed since it was attached."""

    def __init__(self, storage_path: str) -> None:
        self.storage_path = storage_path
        self._lock = threading.Lock()
        self._live: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._removed: set[tuple[str, str, str]] = set()
        self.profile: Any = None  # keeps the Qt profile wrapper alive

    def on_added(self, cookie: Any) -> None:
        converted = _qnetwork_cookie_to_playwright(cookie)
        if converted is None:
            return
        key = _cookie_key(converted)
        with self._lock:
            self._live[key] = converted
            self._removed.discard(key)

    def on_removed(self, cookie: Any) -> None:
        converted = _qnetwork_cookie_to_playwright(cookie)
        if converted is None:
            return
        self.mark_removed([converted])

    def mark_removed(self, cookies: list[Mapping[str, Any]]) -> None:
        with self._lock:
            for cookie in cookies:
                key = _cookie_key(cookie)
                self._live.pop(key, None)
                self._removed.add(key)

    def merge(self, disk_cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            merged = {
                _cookie_key(cookie): cookie for cookie in disk_cookies if _cookie_key(cookie) not in self._removed
            }
            merged.update({key: dict(cookie) for key, cookie in self._live.items()})
        return list(merged.values())


_trackers: dict[tuple[str, str], _SpotifyCookieTracker] = {}
_trackers_lock = threading.Lock()


def _profile_storage_path(profile: Any) -> str:
    try:
        if profile.isOffTheRecord():
            return ""
        return str(profile.persistentStoragePath() or "")
    except Exception:
        return ""


def _profile_key(profile: Any) -> tuple[str, str]:
    try:
        name = str(profile.storageName() or "")
    except Exception:
        name = ""
    return (_profile_storage_path(profile), name)


def _attach_tracker(profile: Any) -> _SpotifyCookieTracker:
    """Attach the cookie tracker to *profile*. Must run on the Qt main thread."""
    key = _profile_key(profile)
    with _trackers_lock:
        existing = _trackers.get(key)
    if existing is not None:
        return existing
    tracker = _SpotifyCookieTracker(_profile_storage_path(profile))
    tracker.profile = profile
    cookie_store = profile.cookieStore()
    cookie_store.cookieAdded.connect(tracker.on_added)
    cookie_store.cookieRemoved.connect(tracker.on_removed)
    with _trackers_lock:
        _trackers[key] = tracker
    logger.info("Spotify cookie bridge: watching the in-app browser's Spotify cookies")
    return tracker


def ensure_spotify_cookie_tracker(
    webview_controller: Any | None = None,
    *,
    dispatch_to_main: Callable[[Callable[[], None]], bool] | None = None,
    wait_seconds: float = 2.0,
) -> _SpotifyCookieTracker | None:
    """Start watching the in-app browser's Spotify cookies (idempotent).

    Attach before the user signs in (Viola does it when the sign-in view
    opens) so the sign-in cookie is seen the moment Spotify sets it.
    """
    profile = _profile_from_webview(webview_controller)
    if profile is None:
        return None
    with _trackers_lock:
        existing = _trackers.get(_profile_key(profile))
    if existing is not None:
        return existing
    if _on_qt_main_thread():
        return _attach_tracker(profile)
    if dispatch_to_main is None:
        return None
    if wait_seconds <= 0:
        # Fire and forget (callers on an event loop must not block on Qt).
        dispatch_to_main(lambda: _attach_tracker(profile))
        return None

    done = threading.Event()
    result: dict[str, Any] = {}

    def _attach_on_main() -> None:
        try:
            result["tracker"] = _attach_tracker(profile)
        except Exception as exc:
            result["error"] = exc
        finally:
            done.set()

    if not dispatch_to_main(_attach_on_main):
        return None
    if not done.wait(wait_seconds):
        return None
    if result.get("error") is not None:
        logger.debug("Spotify cookie bridge: tracker attach failed: %s", result["error"])
        return None
    return result.get("tracker")


def _read_cookie_db(storage_path: str) -> list[dict[str, Any]] | None:
    """Spotify cookies from the profile's cookie database, or None if unreadable."""
    if not storage_path:
        return None
    from pathlib import Path

    db_path = Path(storage_path) / "Cookies"
    if not db_path.exists():
        return []

    import sqlite3

    rows: list[tuple[Any, ...]] = []
    for mode in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect("file:%s?%s" % (db_path.as_posix(), mode), uri=True, timeout=1.0)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly, samesite "
                "FROM cookies WHERE host_key = 'spotify.com' OR host_key = '.spotify.com' "
                "OR host_key LIKE '%.spotify.com'"
            ).fetchall()
            break
        except sqlite3.Error:
            rows = []
            continue
        finally:
            conn.close()
    else:
        logger.debug("Spotify cookie bridge: in-app browser cookie database unreadable")
        return None

    now = time.time()
    cookies: list[dict[str, Any]] = []
    for host_key, name, value, path, expires_utc, is_secure, is_httponly, samesite in rows:
        expires: float | None = None
        if expires_utc:
            expires = float(expires_utc) / 1_000_000 - _WINDOWS_EPOCH_OFFSET_SECONDS
            if expires <= now:
                continue
        cookie: dict[str, Any] = {
            "name": str(name or ""),
            "value": str(value or ""),
            "domain": str(host_key or ""),
            "path": str(path or "/") or "/",
            "secure": bool(is_secure),
            "httpOnly": bool(is_httponly),
        }
        same_site = _SAME_SITE_BY_DB_VALUE.get(samesite)
        if same_site:
            cookie["sameSite"] = same_site
        if expires is not None:
            cookie["expires"] = expires
        normalized = _normalize_playwright_cookie(cookie)
        if normalized is not None:
            cookies.append(normalized)
    return cookies


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

    Returns the cookies (possibly an empty list when there are none), or
    ``None`` when they could not be read at all (no webview, no tracker and no
    readable cookie database). Callers use ``None`` to mean "unknown", never
    "signed out". Safe from any thread; fast once the tracker is attached.
    """
    profile = _profile_from_webview(webview_controller)
    if profile is None:
        return None
    tracker = ensure_spotify_cookie_tracker(
        webview_controller,
        dispatch_to_main=dispatch_to_main,
        wait_seconds=timeout_seconds + 1.0,
    )
    storage_path = tracker.storage_path if tracker is not None else _profile_storage_path(profile)
    disk_cookies = _read_cookie_db(storage_path)
    if tracker is None and disk_cookies is None:
        return None
    cookies = tracker.merge(disk_cookies or []) if tracker is not None else list(disk_cookies or [])
    now = time.time()
    return [cookie for cookie in cookies if not (cookie.get("expires") and float(cookie["expires"]) <= now)]


def extract_spotify_cookies_from_qweb(
    webview_controller: Any | None = None,
    *,
    user_id: str | None = None,
    dispatch_to_main: Callable[[Callable[[], None]], bool] | None = None,
    timeout_seconds: float = _LOAD_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Extract Spotify cookies from the QWebEngine profile and cache them."""
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


def _delete_cookies_in_store(profile: Any, cookies: list[dict[str, Any]]) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtNetwork import QNetworkCookie

    cookie_store = profile.cookieStore()
    for cookie in cookies:
        domain = str(cookie["domain"])
        qt_cookie = QNetworkCookie(str(cookie["name"]).encode("utf-8"), str(cookie.get("value") or "").encode("utf-8"))
        qt_cookie.setDomain(domain)
        qt_cookie.setPath(str(cookie.get("path") or "/"))
        qt_cookie.setSecure(bool(cookie.get("secure", True)))
        qt_cookie.setHttpOnly(bool(cookie.get("httpOnly", False)))
        # Without an explicit origin Qt derives one from a leading-dot domain
        # (".spotify.com") that Chromium rejects, and nothing is deleted.
        cookie_store.deleteCookie(qt_cookie, QUrl("https://%s/" % domain.lstrip(".")))


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
    cookies = read_spotify_cookies_from_qweb(
        webview_controller,
        dispatch_to_main=dispatch_to_main,
        timeout_seconds=timeout_seconds,
    )
    if cookies is None:
        return None
    if not cookies:
        return 0

    if _on_qt_main_thread():
        _delete_cookies_in_store(profile, cookies)
    else:
        if dispatch_to_main is None:
            return None
        done = threading.Event()
        result: dict[str, Any] = {}

        def _delete_on_main() -> None:
            try:
                _delete_cookies_in_store(profile, cookies)
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

    # The database on disk is only rewritten when the browser flushes it, so
    # remember the deletion for reads in the meantime.
    with _trackers_lock:
        tracker = _trackers.get(_profile_key(profile))
    if tracker is not None:
        tracker.mark_removed(cookies)
    return len(cookies)


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
    "ensure_spotify_cookie_tracker",
    "extract_spotify_cookies_from_qweb",
    "get_cached_spotify_cookies",
    "has_spotify_auth_cookie",
    "inject_spotify_cookies_into_playwright",
    "read_spotify_cookies_from_qweb",
]
