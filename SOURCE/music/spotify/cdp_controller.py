"""
Spotify CDP Controller — Low-level Chrome/Playwright bridge.

All Spotify DOM interaction goes through this single class.
No other module touches Playwright or Chrome directly.

Architecture:
    Viola  --CDP-->  Chrome (dedicated profile)  --plays-->  open.spotify.com
    - Chrome launched with --remote-debugging-port
    - Per-user Viola profile under the local data directory
    - Playwright connects via connect_over_cdp()
    - Keyboard shortcuts preferred over DOM clicks for resilience
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from core.logging_config import get_logger
from core.platform import get_data_dir
from core.subprocess_utils import popen_silent
from music.spotify.cookie_bridge import get_cached_spotify_cookies, inject_spotify_cookies_into_playwright

logger = get_logger(__name__)

_POSITION_RE = re.compile(r"(\d+):(\d+)")
_DIRECT_TRACK_PROGRESS_DELTA_S = 0.75
_DIRECT_TRACK_STALL_RETRY_S = 4.0
_DIRECT_TRACK_POLL_S = 0.5

# Ground-truth playback probe: the Spotify web player streams audio through a
# single <audio>/<video> media element whose currentTime advances ONLY while
# audio is genuinely playing. The now-playing *widget* (_NOW_PLAYING_JS) is
# laggy/unreliable and can read empty even while audio plays, which made
# verification report "did not start" on real playback. This probe also
# resumes a media element that landed paused (the row-Play click + footer-nudge
# can leave it paused), which is the actual fix for the stuck-paused start.
_ENSURE_AUDIO_PLAYING_JS = r"""
(() => {
    const el = document.querySelector('audio, video');
    if (!el) return {present: false, t: null, paused: null};
    if (el.paused) { try { el.play(); } catch (e) {} }
    return {present: true, t: Number(el.currentTime) || 0, paused: !!el.paused};
})()
"""


def _playwright_error_types() -> tuple[type[BaseException], ...]:
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return (RuntimeError,)
    return (PlaywrightError, RuntimeError)


def _safe_spotify_cdp_user(user_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (user_id or "").strip()).strip("._")
    if not safe:
        raise ValueError("user_id is required for Spotify CDP")
    return safe


def _resolve_spotify_cdp_user_id(user_id: str | None = None) -> str:
    if user_id:
        return user_id
    from core.user_context import get_current_or_device_user_id

    return get_current_or_device_user_id()


def spotify_cdp_profile_path(user_id: str) -> Path:
    """Return the per-user local Chrome profile path for Spotify CDP."""
    return get_data_dir() / "spotify_cdp_profiles" / _safe_spotify_cdp_user(user_id)


def _parse_time_str(time_str: str | None) -> float:
    """Parse "M:SS" or "H:MM:SS" time string to seconds."""
    if not time_str:
        return 0.0
    match = _POSITION_RE.search(time_str)
    if not match:
        return 0.0
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        logger.debug("Ignoring unparsable Spotify time string: %r", time_str)
    return 0.0


def _now_playing_position_seconds(payload: Mapping[str, Any] | None) -> float:
    if not isinstance(payload, Mapping):
        return 0.0
    return _parse_time_str(str(payload.get("position") or ""))


def _normalize_track_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = str(value).lower()
    cleaned = cleaned.replace("| spotify", " ")
    cleaned = cleaned.replace(" - song and lyrics by ", " ")
    cleaned = cleaned.replace(" \u2022 ", " ")
    cleaned = cleaned.replace(" ? ", " ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", cleaned).split())


def _track_text_matches(expected: str | None, observed: str | None) -> bool:
    expected_norm = _normalize_track_text(expected)
    observed_norm = _normalize_track_text(observed)
    if not expected_norm or not observed_norm:
        return False
    return observed_norm in expected_norm or expected_norm in observed_norm


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CDP_PORT = 9223  # 9222 is used by the MCP browser server; Spotify CDP uses its own port
CDP_PORT_ENV = "VIOLA_SPOTIFY_CDP_PORT"


def _configured_cdp_port(environ: Mapping[str, str] | None = None) -> int:
    raw_port = (environ or os.environ).get(CDP_PORT_ENV, "").strip()
    if not raw_port:
        return DEFAULT_CDP_PORT
    try:
        port = int(raw_port)
    except ValueError:
        return DEFAULT_CDP_PORT
    if port <= 0 or port > 65535:
        return DEFAULT_CDP_PORT
    return port


CDP_PORT = _configured_cdp_port()
SPOTIFY_URL = "https://open.spotify.com"

SPOTIFY_RECONNECTING_TOAST = "Spotify Chrome closed unexpectedly — reconnecting."
SPOTIFY_RECONNECT_FAILED_TOAST = "Reconnect failed — open Settings → Music to relink Spotify."


# Module-level cache: once we've ever observed valid sp_dc/sp_key, remember it.
# Chrome locks the cookies file exclusively on Windows while running, so the
# file probe only works when Chrome is *not* running (e.g., right at startup
# before the controller launches Chrome, or briefly between sessions). Once we
# see valid cookies on disk we treat the session as present until something
# (logout signal, controller error) explicitly invalidates it.
_cdp_session_known_present_by_user: set[str] = set()


def _invalidate_cdp_session_cache() -> None:
    """Reset the cached "session known present" flag.

    Call from the controller when an authoritative signal (e.g., redirect to
    /login during a play attempt) proves the cached cookies are stale.
    """
    _cdp_session_known_present_by_user.clear()


def has_cdp_session_cookies(user_id: str | None = None) -> bool:
    """Return True when the external CDP Chrome profile has unexpired Spotify auth cookies.

    Used by intent/tools/music_connect to bypass the Qt-overlay-only "logged_in" check.
    The Qt overlay's cookie store is independent from the CDP Chrome's; a fresh Viola
    restart finds Qt empty even when CDP's per-user Chrome Cookies database still has
    valid sp_dc + sp_key (1-year expiry). Without this bypass the agent would
    pointlessly re-prompt for login on every restart.

    Strategy:
    1. If we've previously observed valid cookies (cache flag set), return True.
    2. Try to read the cookies file directly. If it's unlocked (Chrome not running)
       and contains unexpired sp_dc + sp_key, set cache and return True.
    3. If the file is locked (PermissionError — Windows-specific Chrome behavior)
       AND a CDP controller process is alive, assume Chrome loaded a valid profile
       at startup and return True optimistically. A subsequent CDP play is the
       authoritative check; if cookies were server-invalidated the engine will
       surface that error path naturally.
    4. Otherwise return False.

    We don't decrypt the cookie value (DPAPI / Chrome local-state) — only check
    presence + expiry.
    """
    try:
        resolved_user_id = _resolve_spotify_cdp_user_id(user_id)
    except LookupError:
        return False

    if resolved_user_id in _cdp_session_known_present_by_user:
        return True

    import sqlite3
    import time as _t

    cookies_db = spotify_cdp_profile_path(resolved_user_id) / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        return False

    # ── Direct read (works only when Chrome is not running on Windows) ──
    rows: list[tuple[str, int]] = []
    file_was_locked = False
    try:
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT name, expires_utc FROM cookies " "WHERE host_key='.spotify.com' AND name IN ('sp_dc','sp_key')"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # "unable to open database file" — Chrome holds exclusive lock.
        file_was_locked = True
    except PermissionError:
        file_was_locked = True
    except Exception:
        return False

    if rows:
        if len(rows) < 2:
            return False
        now_unix = _t.time()
        for _name, expires_utc in rows:
            if not expires_utc:
                continue
            unix_expiry = expires_utc / 1e6 - 11644473600
            if unix_expiry <= now_unix:
                return False
        _cdp_session_known_present_by_user.add(resolved_user_id)
        return True

    if not file_was_locked:
        return False

    # ── Fallback: file locked, check if controller's Chrome is alive ──
    # If Chrome is running with this profile, it loaded the cookies at startup
    # and is using them. Optimistically report "session present" so the agent
    # doesn't pointlessly re-prompt. If the cookies were server-invalidated,
    # the engine.play() path will surface the auth wall.
    try:
        from music.providers.spotify_cdp import get_cdp_controller

        controller = get_cdp_controller(user_id=resolved_user_id)
        chrome_proc = getattr(controller, "_chrome_proc", None)
        if chrome_proc is not None and chrome_proc.poll() is None:
            _cdp_session_known_present_by_user.add(resolved_user_id)
            return True
    except (ImportError, OSError, RuntimeError) as exc:
        logger.debug("Spotify CDP running-cookie probe skipped: %s", exc)

    return False


# Browser search paths — Windows
_CHROME_PATHS_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
_EDGE_PATHS_WIN = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Browser search paths — Linux
_CHROME_PATHS_LINUX = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
]
_EDGE_PATHS_LINUX = [
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]

# Browser search paths — macOS (#2594: find_browser() previously had no darwin
# branch at all, so Spotify CDP could never find a browser on Mac and silently
# fell through to the "no Chrome, Edge, or Chromium browser found" error even
# with Chrome installed in the normal /Applications location). Brave and
# Chromium are Chromium-family browsers that also speak the CDP protocol
# find_browser() needs, so they ride the "Chrome" search list the same way
# Windows/Linux fold Chromium variants into their own chrome_paths.
_CHROME_PATHS_DARWIN = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_EDGE_PATHS_DARWIN = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# JS snippets for DOM queries
_LOGIN_CHECK_JS = r"""
(() => {
    const userWidget = document.querySelector('[data-testid="user-widget-link"]');
    const accountButton = document.querySelector('button[data-testid="user-widget-link"]');
    const loginButton = document.querySelector('[data-testid="login-button"]');
    const passwordField = document.querySelector('input[type="password"], #password, input[name="password"]');
    const usernameField = document.querySelector('#username, input[autocomplete="username"], input[name="username"]');
    const href = String(window.location.href || "").toLowerCase();
    const isChallengePage = href.includes("challenge.spotify.com") ||
                            href.includes("/challenge") ||
                            href.includes("/email?flow_ctx") ||
                            href.includes("/otp") ||
                            href.includes("/mfa");
    const isLoginPage = href.includes('/login') ||
                        href.includes('accounts.spotify.com');
    const visibleRecaptchaChallenge = Array.from(document.querySelectorAll('iframe')).some((frame) => {
        const src = String(frame.src || "");
        const title = String(frame.title || "");
        const visible = !!(frame.offsetWidth || frame.offsetHeight || frame.getClientRects().length);
        return visible && src.includes("recaptcha") && (src.includes("/bframe") || /challenge/i.test(title));
    });
    const statusText = Array.from(document.querySelectorAll('[role="alert"], [aria-live], div, p, span'))
        .map((node) => String(node.innerText || node.textContent || "").trim())
        .filter(Boolean)
        .join("\n")
        .toLowerCase();
    const identifierRejected = statusText.includes("isn't linked to a spotify account") ||
                               statusText.includes("not linked to a spotify account") ||
                               statusText.includes("enter a valid email");
    const challengeRequired = visibleRecaptchaChallenge ||
                              isChallengePage ||
                              statusText.includes("enter the code") ||
                              statusText.includes("verification code") ||
                              statusText.includes("check your email") ||
                              statusText.includes("two-step") ||
                              statusText.includes("two factor") ||
                              statusText.includes("two-factor") ||
                              statusText.includes("authenticator") ||
                              (statusText.includes("captcha") &&
                               (statusText.includes("verify") || statusText.includes("robot")));
    const loggedIn = !!(userWidget || accountButton) && !loginButton && !isLoginPage;
    let authState = "unknown";
    let loginErrorCode = null;
    if (loggedIn) {
        authState = "authenticated";
    } else if (identifierRejected) {
        authState = "identifier_rejected";
        loginErrorCode = "identifier_rejected";
    } else if (challengeRequired) {
        authState = "interactive_challenge_required";
        loginErrorCode = "interactive_challenge_required";
    } else if (passwordField) {
        authState = "password_required";
    } else if (usernameField || isLoginPage) {
        authState = "login_required";
    }
    return JSON.stringify({
        logged_in: loggedIn,
        has_user_widget: !!(userWidget || accountButton),
        has_login_button: !!loginButton,
        is_login_page: isLoginPage,
        has_password_field: !!passwordField,
        has_username_field: !!usernameField,
        challenge_required: challengeRequired,
        auth_state: authState,
        login_error_code: loginErrorCode
    });
})();
"""

_ACCOUNT_TIER_JS = r"""
(async () => {
    const base = {
        product: "unknown",
        is_premium: null,
        source: "unavailable",
        premium_required_for_full_playback: true,
        full_track_playback_supported: null
    };

    function applyProduct(payload, product, source) {
        const normalized = String(product || "unknown").toLowerCase();
        payload.product = normalized;
        payload.source = source;
        if (normalized === "premium") {
            payload.is_premium = true;
            payload.full_track_playback_supported = true;
        } else if (normalized && normalized !== "unknown") {
            payload.is_premium = false;
            payload.full_track_playback_supported = false;
        }
        return payload;
    }

    function telemetryTier() {
        const payload = Object.assign({}, base);
        payload.source = "spotify_web_player_telemetry";
        try {
            const entries = performance && performance.getEntriesByType
                ? performance.getEntriesByType("resource")
                : [];
            for (let i = entries.length - 1; i >= 0; i--) {
                const rawUrl = entries[i] && entries[i].name ? entries[i].name : "";
                const decoded = decodeURIComponent(rawUrl);
                const match = decoded.match(/[?&]ep\.is_premium=([^&]+)/);
                if (!match) continue;
                const raw = String(match[1] || "").toLowerCase();
                if (raw === "true" || raw === "1") {
                    return applyProduct(payload, "premium", "spotify_web_player_telemetry");
                }
                if (raw === "false" || raw === "0") {
                    return applyProduct(payload, "free", "spotify_web_player_telemetry");
                }
            }
        } catch (e) {
            payload.source = "spotify_web_player_telemetry_error";
        }
        return payload;
    }

    const fallback = telemetryTier();
    try {
        const tokenResp = await fetch("https://open.spotify.com/api/token", {
            credentials: "include",
            cache: "no-store"
        });
        if (!tokenResp.ok) {
            fallback.web_token_status = tokenResp.status;
            return JSON.stringify(fallback);
        }

        const tokenPayload = await tokenResp.json();
        const token = tokenPayload && (tokenPayload.accessToken || tokenPayload.access_token);
        if (!token || tokenPayload.isAnonymous === true) {
            fallback.web_token_status = tokenResp.status;
            return JSON.stringify(fallback);
        }

        const profileResp = await fetch("https://api.spotify.com/v1/me", {
            headers: { Authorization: "Bearer " + token },
            cache: "no-store"
        });
        if (!profileResp.ok) {
            fallback.web_api_status = profileResp.status;
            return JSON.stringify(fallback);
        }

        const profile = await profileResp.json();
        const product = profile && profile.product ? profile.product : "unknown";
        const payload = applyProduct(
            Object.assign({}, base),
            product,
            "spotify_web_api_v1_me"
        );
        payload.web_api_status = profileResp.status;
        return JSON.stringify(payload);
    } catch (e) {
        fallback.source = fallback.is_premium === null
            ? "spotify_account_tier_unavailable"
            : fallback.source;
        return JSON.stringify(fallback);
    }
})();
"""

_NOW_PLAYING_JS = r"""
(() => {
    const result = {};

    // Track name — these testids live inside [data-testid="now-playing-widget"]
    // and only populate when a track is actively playing.
    const trackName = document.querySelector('[data-testid="context-item-info-title"]');
    const trackLink = document.querySelector('[data-testid="context-item-link"]');
    result.track = (trackName && trackName.textContent && trackName.textContent.trim()) ||
                   (trackLink && trackLink.textContent && trackLink.textContent.trim()) || null;
    result.track_url = trackLink && trackLink.href ? trackLink.href : null;
    result.track_id = null;
    if (result.track_url) {
        const trackMatch = result.track_url.match(/\/track\/([A-Za-z0-9]+)/);
        if (trackMatch) result.track_id = trackMatch[1];
    }

    // Artist — "context-item-info-subtitles" (note plural) is the 2026 testid
    const artistEl = document.querySelector('[data-testid="context-item-info-artist"]');
    const artistSub = document.querySelector('[data-testid="context-item-info-subtitles"]');
    result.artist = (artistEl && artistEl.textContent && artistEl.textContent.trim()) ||
                    (artistSub && artistSub.textContent && artistSub.textContent.trim()) || null;

    // Artwork — prefer highest resolution available
    // Spotify CDN URL pattern: https://i.scdn.co/image/ab67616d{SIZE}{HASH}
    // Known size codes (low to high quality):
    //   00004851 -> 64x64   (thumbnail used in collapsed widget; what we
    //                        were defaulting to, looked pixelated when
    //                        scaled up by SmartDisplay)
    //   00001e02 -> 150x150
    //   0000b273 -> 300x300 (standard "large" — universally available)
    //   0000ace4 -> 640x640 (extra-large — not always exposed in DOM)
    // We always upgrade to 0000b273 (300x300) which is large enough for the
    // SmartDisplay album-art card without becoming a wasteful download. If
    // the page exposes a 640x640 entry already, we keep it.
    const coverArt = document.querySelector('[data-testid="cover-art-image"]');
    if (coverArt) {
        let bestUrl = coverArt.src || null;
        // Check srcset for higher-res variants
        const srcset = coverArt.getAttribute('srcset');
        if (srcset) {
            const entries = srcset.split(',').map(s => s.trim().split(/\s+/));
            let bestW = 0;
            for (const entry of entries) {
                const w = parseInt((entry[1] || '').replace('w', ''), 10) || 0;
                if (w > bestW && entry[0]) { bestW = w; bestUrl = entry[0]; }
            }
        }
        // Also scan ALL cover-art images on the page; the now-playing widget
        // sometimes hosts both a small thumb (00004851) and a larger version
        // (0000b273) in the same DOM. Prefer the larger by size-code rank.
        const sizeRank = {
            '00004851': 1,
            '00001e02': 2,
            '0000b273': 3,
            '0000ace4': 4,
        };
        const rankOf = (url) => {
            if (!url) return 0;
            const m = url.match(/ab67616d([0-9a-f]{8})/);
            return m ? (sizeRank[m[1]] || 0) : 0;
        };
        document.querySelectorAll('img[src*="i.scdn.co"], img[srcset*="i.scdn.co"]').forEach(img => {
            const candidates = [img.getAttribute('src')].filter(Boolean);
            const ss = img.getAttribute('srcset') || '';
            ss.split(',').forEach(part => {
                const u = part.trim().split(/\s+/)[0];
                if (u) candidates.push(u);
            });
            for (const u of candidates) {
                if (rankOf(u) > rankOf(bestUrl)) bestUrl = u;
            }
        });
        // Force any low-res Spotify URL to 0000b273 (300x300). Leaves the
        // 0000ace4 (640) variant untouched if we already have it.
        if (bestUrl && bestUrl.includes('i.scdn.co')) {
            bestUrl = bestUrl.replace(/\/ab67616d(00004851|00001e02)/, '/ab67616d0000b273');
        }
        result.artwork_url = bestUrl;
    } else {
        result.artwork_url = null;
    }

    // Position / duration
    const posEl = document.querySelector('[data-testid="playback-position"]');
    const durEl = document.querySelector('[data-testid="playback-duration"]');
    result.position = posEl ? posEl.textContent : null;
    result.duration = durEl ? durEl.textContent : null;

    // Play/pause state
    const ppBtn = document.querySelector('button[data-testid="control-button-playpause"]');
    if (ppBtn) {
        const ariaLabel = (ppBtn.getAttribute('aria-label') || '').toLowerCase();
        result.is_playing = ariaLabel.includes('pause');
    } else {
        result.is_playing = false;
    }

    return JSON.stringify(result);
})();
"""

_SEARCH_RESULTS_JS = r"""
(() => {
    const results = [];
    // Spotify search results use various selectors. Try the track list rows.
    const rows = document.querySelectorAll('[data-testid="tracklist-row"]');
    rows.forEach((row, i) => {
        if (i >= 10) return;
        // Title: inside the track link's inner div (Spotify 2026 DOM)
        const trackLink = row.querySelector('a[href*="/track/"]');
        const titleEl = (trackLink && trackLink.querySelector('div')) ||
                        row.querySelector('a[data-testid="internal-track-link"] div') ||
                        row.querySelector('[data-testid="internal-track-link"]');
        const artistEls = row.querySelectorAll('a[href*="/artist/"]');
        const artists = [];
        artistEls.forEach(a => {
            const t = (a.textContent || '').trim();
            if (t) artists.push(t);
        });
        // Extract track URI from the track link
        let uri = null;
        if (trackLink) {
            const href = trackLink.getAttribute('href') || '';
            const match = href.match(/\/track\/([A-Za-z0-9]+)/);
            if (match) uri = 'spotify:track:' + match[1];
        }
        results.push({
            title: titleEl ? (titleEl.textContent || '').trim() : null,
            artist: artists.join(', ') || null,
            uri: uri,
            index: i
        });
    });

    // Fallback: try card-based search results if tracklist-row not found
    if (results.length === 0) {
        const cards = document.querySelectorAll('[data-testid="search-tracks-result"]');
        cards.forEach((card, i) => {
            if (i >= 10) return;
            const titleEl = card.querySelector('a[href*="/track/"] div') ||
                            card.querySelector('[data-testid="internal-track-link"]');
            const subtitleEl = card.querySelector('span[data-testid]');
            results.push({
                title: titleEl ? (titleEl.textContent || '').trim() : null,
                artist: subtitleEl ? (subtitleEl.textContent || '').trim() : null,
                uri: null,
                index: i
            });
        });
    }

    return JSON.stringify(results);
})();
"""


class SpotifyCDPError(Exception):
    """Raised when CDP operations fail."""


class SpotifyCDPController:
    """
    Low-level Chrome/Playwright bridge for Spotify web player control.

    All Spotify DOM interaction goes through this single class.
    Thread-safety: all Playwright operations are serialized on a dedicated
    thread via ``_in_cdp_thread()``.  Callers may invoke public methods from
    any thread (including asyncio worker threads).
    """

    def __init__(self, *, user_id: str | None = None) -> None:
        self._user_id = _resolve_spotify_cdp_user_id(user_id)
        self._profile_path = spotify_cdp_profile_path(self._user_id)
        self._browser: Any = None
        self._page: Any = None
        self._playwright: Any = None
        self._pw_context_manager: Any = None
        self._pw_context: Any = None
        self._chrome_proc: subprocess.Popen | None = None
        self._connected = False
        self._cookie_bridge_last_injected = False
        self._account_tier_cache: dict[str, Any] | None = None
        self._account_tier_cache_at: float = 0.0
        # Dedicated single thread for all Playwright sync API calls.
        # Playwright sync API is thread-affine and cannot run inside an
        # asyncio event loop.  Routing through this executor ensures all
        # calls happen on the same non-asyncio thread.
        self._cdp_executor_lock = threading.RLock()
        self._cdp_executor = self._new_cdp_executor()
        self._cdp_thread_id: int | None = None

    # ------------------------------------------------------------------
    # Thread routing
    # ------------------------------------------------------------------
    @staticmethod
    def _new_cdp_executor() -> concurrent.futures.ThreadPoolExecutor:
        return concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="spotify-cdp",
        )

    def _in_cdp_thread(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run *fn* on the dedicated CDP thread.  Safe from any context.

        If already on the CDP thread, calls directly to avoid deadlock.
        On first entry the thread's event-loop policy is forced to
        ``WindowsProactorEventLoopPolicy`` so that Playwright can spawn
        its Node server subprocess (``SelectorEventLoop`` cannot do
        subprocess on Windows, and Qt may install it as the default).
        """
        if threading.current_thread().ident == self._cdp_thread_id:
            return fn(*args, **kwargs)

        def _wrapper() -> Any:
            import asyncio

            if self._cdp_thread_id is None:
                self._cdp_thread_id = threading.current_thread().ident
                # Qt (via qasync) may set WindowsSelectorEventLoopPolicy
                # as the process-wide default.  Playwright's sync_playwright()
                # calls asyncio.new_event_loop() which inherits that policy,
                # creating a SelectorEventLoop that cannot create subprocess
                # transports on Windows.  Force ProactorEventLoopPolicy so
                # Playwright gets a ProactorEventLoop with full subprocess
                # support.
                if sys.platform == "win32":
                    old_policy = type(asyncio.get_event_loop_policy()).__name__
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    logger.info(
                        "CDP thread: forced WindowsProactorEventLoopPolicy " "(was %s)",
                        old_policy,
                    )

            return fn(*args, **kwargs)

        with self._cdp_executor_lock:
            executor = self._cdp_executor
        future = executor.submit(_wrapper)
        return future.result(timeout=120)

    def _rotate_cdp_executor_after_failed_cleanup(self) -> None:
        """Move future CDP work to a fresh thread after Playwright cleanup fails.

        Playwright's sync API owns a running asyncio loop in the CDP thread
        while active. If cleanup fails, starting a second sync_playwright()
        on that same thread raises Playwright's "Sync API inside asyncio loop"
        guard. A fresh single-worker executor gives the reconnect path a clean
        thread without weakening the thread-affinity rule for normal calls.
        """
        with self._cdp_executor_lock:
            old_executor = self._cdp_executor
            self._cdp_executor = self._new_cdp_executor()
            self._cdp_thread_id = None
        old_executor.shutdown(wait=False, cancel_futures=True)

    def _stop_playwright_session(self) -> bool:
        """Stop Playwright and clear connection objects.

        Returns True when the Playwright loop was stopped or was not active.
        """
        stopped = True

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.debug("Browser close failed during cleanup (non-critical): %s", exc)

        if self._playwright is not None:
            try:
                # sync_playwright().start() attaches stop() to the returned
                # Playwright object, not to the context manager.
                self._playwright.stop()
            except Exception as exc:
                logger.debug("Playwright stop failed during cleanup: %s", exc)
                stopped = False
        elif self._pw_context_manager is not None:
            try:
                self._pw_context_manager.__exit__(None, None, None)
            except Exception as exc:
                logger.debug("Playwright context cleanup failed (non-critical): %s", exc)
                stopped = False

        self._pw_context_manager = None
        self._playwright = None
        self._browser = None
        self._pw_context = None
        self._page = None
        self._connected = False
        return stopped

    def _chrome_process_exited(self) -> bool:
        return self._chrome_proc is not None and self._chrome_proc.poll() is not None

    def _clear_dead_chrome_process(self) -> bool:
        if not self._chrome_process_exited():
            return False

        assert self._chrome_proc is not None
        pid = self._chrome_proc.pid
        exit_code = self._chrome_proc.poll()
        logger.warning(
            "Tracked Spotify Chrome process %d exited unexpectedly with code %s",
            pid,
            exit_code,
        )
        self._unregister_chrome_for_capture()
        self._chrome_proc = None
        self._connected = False
        self._page = None
        return True

    def _begin_unexpected_reconnect_notice(self) -> bool:
        if self._chrome_process_exited() or not self._is_port_open():
            self._notify_user(SPOTIFY_RECONNECTING_TOAST)
            return True
        return False

    def _finish_unexpected_reconnect_notice(self, reconnect_notice: bool, *, success: bool) -> None:
        if not reconnect_notice:
            return
        self._notify_user("" if success else SPOTIFY_RECONNECT_FAILED_TOAST)

    # ------------------------------------------------------------------
    # Browser discovery
    # ------------------------------------------------------------------
    @staticmethod
    def find_browser() -> tuple[str, str]:
        """Find Chrome or Edge on the system.

        Returns:
            Tuple of (browser_name, browser_path).

        Raises:
            SpotifyCDPError: If no supported browser is found.
        """
        is_windows = sys.platform == "win32"
        is_darwin = sys.platform == "darwin"

        if is_windows:
            chrome_paths = _CHROME_PATHS_WIN
            edge_paths = _EDGE_PATHS_WIN
        elif is_darwin:
            chrome_paths = _CHROME_PATHS_DARWIN
            edge_paths = _EDGE_PATHS_DARWIN
        else:
            chrome_paths = _CHROME_PATHS_LINUX
            edge_paths = _EDGE_PATHS_LINUX

        for path in chrome_paths:
            expanded = os.path.expandvars(path)
            if os.path.isfile(expanded):
                return ("Chrome", expanded)

        for path in edge_paths:
            expanded = os.path.expandvars(path)
            if os.path.isfile(expanded):
                return ("Edge", expanded)

        # Try PATH
        for name, cmd in [
            ("Chrome", "chrome"),
            ("Chrome", "google-chrome"),
            ("Edge", "msedge"),
            ("Edge", "microsoft-edge"),
            ("Chromium", "chromium-browser"),
            ("Chromium", "chromium"),
        ]:
            found = shutil.which(cmd)
            if found:
                return (name, found)

        raise SpotifyCDPError(
            "No Chrome, Edge, or Chromium browser found. " "Install Chrome or Edge to use Spotify integration."
        )

    # ------------------------------------------------------------------
    # Port check
    # ------------------------------------------------------------------
    @staticmethod
    def _is_port_open(port: int = CDP_PORT) -> bool:
        """Check if CDP port is open."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0

    # ------------------------------------------------------------------
    # DRM / Protected content
    # ------------------------------------------------------------------
    @staticmethod
    def _find_widevine_cdm() -> tuple[str, str] | None:
        """Find the system Widevine CDM path and version.

        Chrome with ``--user-data-dir`` pointing to a custom profile may
        not load the Widevine CDM component automatically.  By passing
        ``--widevine-cdm-path`` and ``--widevine-cdm-version`` we force
        Chrome to use the system-installed CDM.

        Returns:
            (cdm_path, version) tuple, or None if not found.
        """
        if platform.system() != "Windows":
            return None  # Linux/macOS handle CDM differently

        # Scan Chrome installation directories for WidevineCdm
        chrome_base = Path(r"C:\Program Files\Google\Chrome\Application")
        if not chrome_base.exists():
            chrome_base = Path(r"C:\Program Files (x86)\Google\Chrome\Application")
        if not chrome_base.exists():
            return None

        # Find version directories (e.g. 145.0.7632.160)
        for version_dir in sorted(chrome_base.iterdir(), reverse=True):
            wv_dir = version_dir / "WidevineCdm"
            manifest_path = wv_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    cdm_version = manifest.get("version", "")
                    if cdm_version:
                        logger.info(
                            "Found system Widevine CDM v%s at %s",
                            cdm_version,
                            wv_dir,
                        )
                        return (str(wv_dir), cdm_version)
                except (json.JSONDecodeError, OSError) as exc:
                    logger.debug("Could not read Widevine manifest at %s: %s", manifest_path, exc)

        return None

    def _enable_protected_content_in_profile(self) -> None:
        """Ensure the Chrome profile allows DRM-protected media playback.

        Two things are required for Widevine DRM with a custom profile:

        1. ``protected_media_identifier = 1`` in the Preferences file
           (mirrors the toggle at ``chrome://settings/content/protectedContent``).

        2. The WidevineCdm folder from the system Chrome installation must
           be present inside the profile directory.  Chrome with a custom
           ``--user-data-dir`` does NOT auto-discover the system CDM;
           the component updater directory inside the profile must contain
           the CDM files or ``requestMediaKeySystemAccess`` will fail with
           "Unsupported keySystem".
        """
        profile_root = self._profile_path

        # --- 1. Preferences: enable protected content ---
        prefs_path = profile_root / "Default" / "Preferences"
        if not prefs_path.exists():
            prefs_path.parent.mkdir(parents=True, exist_ok=True)
            prefs = {}
        else:
            try:
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read Chrome Preferences, will recreate: %s", exc)
                prefs = {}

        profile = prefs.setdefault("profile", {})
        dcsv = profile.setdefault("default_content_setting_values", {})

        changed = False
        if dcsv.get("protected_media_identifier") != 1:
            dcsv["protected_media_identifier"] = 1
            changed = True

        if changed:
            try:
                prefs_path.write_text(json.dumps(prefs, separators=(",", ":")), encoding="utf-8")
                logger.info("Enabled protected content (Widevine DRM) in Chrome profile")
            except OSError as exc:
                logger.warning("Could not write Chrome Preferences: %s", exc)

        # --- 2. Copy system WidevineCdm into profile if missing ---
        dst_wv = profile_root / "WidevineCdm"
        dst_dll = dst_wv / "_platform_specific" / "win_x64" / "widevinecdm.dll"
        if not dst_dll.exists():
            wv_info = self._find_widevine_cdm()
            if wv_info:
                src_wv = Path(wv_info[0])
                try:
                    if dst_wv.exists():
                        shutil.rmtree(dst_wv)
                    shutil.copytree(str(src_wv), str(dst_wv))
                    logger.info("Copied Widevine CDM v%s into profile", wv_info[1])
                except OSError as exc:
                    logger.warning("Could not copy Widevine CDM: %s", exc)

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------
    def launch(self) -> None:
        """Verify browser availability before CDP connection.

        Actual Chrome launch is deferred to ``connect()``. This method only
        verifies that a supported browser is available. Spotify login is
        handled by the in-app BrowserAuth overlay; the CDP playback browser is
        always launched off-screen.

        Raises:
            SpotifyCDPError: If no supported browser is found.
        """
        # Verify browser is available (will be launched by connect())
        self.find_browser()
        logger.info("Spotify CDP launch requested (port=%d, offscreen=True)", CDP_PORT)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Connect to Chrome via CDP (thread-safe wrapper)."""
        self._in_cdp_thread(self._connect_impl)

    def _connect_impl(self) -> None:
        """Connect to Chrome via CDP and get a page for open.spotify.com.

        Launches Chrome via subprocess (not Playwright's launch_persistent_context)
        to preserve native Widevine CDM loading.  Playwright's launch methods
        strip or fail to initialise Widevine, causing ``requestMediaKeySystemAccess``
        to return "Unsupported keySystem" — which means Spotify can never decrypt
        audio.  Subprocess launch lets Chrome handle its own component updater.

        Playwright then connects via ``connect_over_cdp`` for DOM control only.

        Raises:
            SpotifyCDPError: If connection fails.
        """
        # Already connected — nothing to do
        if self._connected and self._page:
            return

        self._cookie_bridge_last_injected = False
        self._clear_dead_chrome_process()

        # Clean up stale Playwright state if any
        if self._pw_context_manager or self._playwright or self._browser:
            if not self._stop_playwright_session():
                logger.warning("Playwright cleanup failed; retrying Spotify CDP reconnect on a fresh thread")
                self._rotate_cdp_executor_after_failed_cleanup()
                self._in_cdp_thread(self._connect_impl)
                return

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SpotifyCDPError("playwright not installed. Run: pip install playwright") from exc

        try:
            # Ensure Chrome profile allows DRM/protected content (Widevine).
            self._enable_protected_content_in_profile()

            # ---- Step 1: Launch Chrome via subprocess ----
            # This preserves native Widevine CDM loading that Playwright strips.
            target_url = SPOTIFY_URL
            if not self._is_port_open(CDP_PORT):
                browser_name, browser_path = self.find_browser()

                chrome_args = [
                    browser_path,
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--remote-allow-origins=*",
                    "--remote-debugging-port=%d" % CDP_PORT,
                    "--autoplay-policy=no-user-gesture-required",
                    "--user-data-dir=%s" % self._profile_path,
                    # Spotify's Web Playback UI needs a real headed page for
                    # the CDP click/DRM/user-gesture path. Keep normal playback
                    # off-screen but headed so the click path can start audio.
                    "--window-size=1280,720",
                    "--window-position=-32000,-32000",
                ]
                if sys.platform == "win32":
                    # Multi-room, WINDOWS-ONLY (#2533, same rationale as
                    # #2423's viola_qt.py fix, different process topology):
                    # Chrome's audio service runs out-of-process by default in
                    # a sandboxed subprocess. Windows Process Audio Tap
                    # (ProcTap) can't read PCM from a sandboxed audio service
                    # that isn't a child of our target PID, so even with the
                    # right Chrome browser PID registered, ProcTap captures
                    # silence (verified live: capture_rms=0.0004 vs 0.028 for
                    # in-process Qt-Chromium audio).
                    #
                    # Pulling the audio service in-process AND unsandboxing
                    # it puts the audio rendering on the Chrome browser PID
                    # we already register with ProcTap (see
                    # _register_chrome_for_capture() below), so the
                    # per-process tap actually has something to read.
                    #
                    # ProcTap is Windows-only
                    # (audio_core/capture/proctap_provider.py::is_available()
                    # gates on sys.platform == "win32"), and it is the ONLY
                    # consumer of this Chrome subprocess's in-process audio:
                    # register_external_audio_pid()/find_audio_child_pid() in
                    # audio_core/capture/hub_audio_controller.py are read
                    # exclusively by ProcTapProvider -- no macOS
                    # (CoreAudioCaptureProvider) or Linux capture path touches
                    # them. Linux/macOS therefore keep Chromium's upstream
                    # default (out-of-process, sandboxed) audio service.
                    chrome_args.append("--disable-features=AudioServiceSandbox,AudioServiceOutOfProcess")
                logger.info(
                    "Launching %s via subprocess on port %d (offscreen=True)",
                    browser_name,
                    CDP_PORT,
                )

                # CREATE_NO_WINDOW on Windows prevents a console flash
                creation_flags = 0
                if sys.platform == "win32":
                    creation_flags = subprocess.CREATE_NO_WINDOW

                self._chrome_proc = popen_silent(
                    chrome_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                logger.info("Chrome PID: %d", self._chrome_proc.pid)

                # Bind Chrome to the Viola-lifetime Job Object so the OS
                # kernel forcibly terminates Chrome (and all its child
                # renderers / audio service processes) when Viola exits —
                # clean shutdown OR crash. Without this, Chrome keeps
                # rendering audio after Viola dies and the user has no UI
                # to stop it (orphan-music UX bug, observed 2026-05-07
                # 15:47 with PID 57252 surviving Viola for 12 minutes).
                try:
                    from core.win32_job import assign_to_lifetime_job

                    assign_to_lifetime_job(self._chrome_proc.pid)
                except (ImportError, OSError, RuntimeError) as exc:
                    logger.debug("Failed to bind Chrome to lifetime job: %s", exc)

                # Wait for CDP port to become available
                for _ in range(30):
                    if self._is_port_open(CDP_PORT):
                        break
                    time.sleep(0.5)
                else:
                    raise SpotifyCDPError("Chrome did not start CDP on port %d within 15s" % CDP_PORT)

                # Small extra wait for Chrome to finish initializing
                time.sleep(1.5)

            # ---- Step 2: Connect Playwright via connect_over_cdp ----
            self._pw_context_manager = sync_playwright()
            self._playwright = self._pw_context_manager.start()

            cdp_url = "http://127.0.0.1:%d" % CDP_PORT
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)

            contexts = self._browser.contexts
            if not contexts:
                raise SpotifyCDPError("No browser contexts found after connect_over_cdp")

            context = contexts[0]
            self._pw_context = context

            try:
                injected_count = inject_spotify_cookies_into_playwright(context, user_id=self._user_id)
                self._cookie_bridge_last_injected = injected_count > 0
            except Exception:
                logger.warning("Spotify QWebEngine cookie injection failed; BrowserAuth overlay may be required")

            # Register Chrome PID for ProcTap capture
            self._register_chrome_for_capture()

            # Find or create Spotify page
            spotify_page = None
            for page in context.pages:
                page_url = page.url or ""
                if "open.spotify.com" in page_url:
                    spotify_page = page
                    break

            if spotify_page is not None and self._cookie_bridge_last_injected:
                logger.info("Reloading existing Spotify page after QWebEngine cookie injection")
                spotify_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)

            if spotify_page is None:
                spotify_page = context.pages[0] if context.pages else context.new_page()
                logger.info("Navigating to %s", target_url)
                spotify_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
            self._page = spotify_page
            self._connected = True
            logger.info("CDP connected to Spotify (URL: %s)", self._page.url)

        except Exception as exc:
            self._connected = False
            self._page = None
            import traceback

            logger.error(
                "CDP connect error: type=%s module=%s repr=%s\n%s",
                type(exc).__name__,
                getattr(type(exc), "__module__", "?"),
                repr(exc),
                traceback.format_exc(),
            )
            if "playwright" not in str(getattr(type(exc), "__module__", "")):
                raise SpotifyCDPError("CDP connection failed: %s" % (repr(exc),)) from exc
            raise SpotifyCDPError("Playwright CDP connection failed: %s" % (repr(exc),)) from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def ensure_running(self, *, wait_for_login: bool = True) -> None:
        """Ensure Chrome is running, connected, and logged in (thread-safe)."""
        self._in_cdp_thread(self._ensure_running_impl, wait_for_login=wait_for_login)

    def _ensure_running_impl(self, *, wait_for_login: bool = True) -> None:
        """Ensure Chrome is running, connected, and logged in.

        This is the single entry point that the engine should call.
        Handles first-run login flow automatically when ``wait_for_login`` is
        enabled. Playback/search callers pass ``wait_for_login=False`` so a
        logged-out browser fails fast instead of blocking the command route.
        """
        reconnect_notice = False
        if self._connected and self._page:
            # Verify page is still responsive
            try:
                self._page.evaluate("1 + 1")
                # Ensure ProcTap knows about Chrome even on cached connections
                self._ensure_chrome_registered_for_capture()
            except Exception:
                reconnect_notice = self._begin_unexpected_reconnect_notice()
                logger.warning("CDP page unresponsive, reconnecting...")
                self._connected = False
                self._page = None
            else:
                if wait_for_login and not self._is_logged_in_impl():
                    if self._try_cookie_bridge_login():
                        return
                    if not self._handle_login_flow(is_first_run=False):
                        raise SpotifyCDPError("Spotify login timed out. User did not authenticate.")
                return

        # Detect first-run (no profile directory yet)
        is_first_run = not self._profile_path.exists()
        has_bridge_cookies = bool(get_cached_spotify_cookies(user_id=self._user_id))

        # Launch if not already running
        if not self._is_port_open():
            self.launch()

        try:
            self._connect_impl()
            # Ensure ProcTap registration after fresh connect
            self._ensure_chrome_registered_for_capture()
        except Exception:
            self._finish_unexpected_reconnect_notice(reconnect_notice, success=False)
            raise

        # Check login and handle first-run flow
        logged_in = self._is_logged_in_impl()
        if not logged_in and self._cookie_bridge_last_injected:
            logged_in = self._is_logged_in_impl()
            if logged_in:
                logger.info("Spotify login restored from QWebEngine cookies; skipping Chrome login flow")

        if not logged_in:
            if self._try_cookie_bridge_login(already_injected=self._cookie_bridge_last_injected):
                self._finish_unexpected_reconnect_notice(reconnect_notice, success=True)
                return
            if not wait_for_login:
                self._finish_unexpected_reconnect_notice(reconnect_notice, success=False)
                raise SpotifyCDPError("Spotify login required. Say 'connect Spotify' to sign in.")
            if not self._handle_login_flow(is_first_run=is_first_run):
                self._finish_unexpected_reconnect_notice(reconnect_notice, success=False)
                raise SpotifyCDPError("Spotify login timed out. User did not authenticate.")

        self._finish_unexpected_reconnect_notice(reconnect_notice, success=True)

    def _try_cookie_bridge_login(self, *, already_injected: bool = False) -> bool:
        """Try QWebEngine-authenticated Spotify cookies before prompting login."""
        if self._pw_context is None:
            return False

        injected_count = 0
        if not already_injected:
            try:
                injected_count = inject_spotify_cookies_into_playwright(self._pw_context, user_id=self._user_id)
                self._cookie_bridge_last_injected = injected_count > 0
            except Exception:
                logger.warning("Spotify QWebEngine cookie injection failed during login check")
                return False
        elif self._cookie_bridge_last_injected:
            injected_count = 1

        if injected_count <= 0:
            return False

        if self._page is not None:
            try:
                logger.info("Refreshing Spotify after QWebEngine cookie injection")
                self._page.goto(SPOTIFY_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
            except Exception:
                logger.debug("Spotify refresh after cookie injection skipped")

        if self._is_logged_in_impl():
            logger.info("Spotify login restored from QWebEngine cookies; skipping Chrome login flow")
            return True
        logger.info("Spotify QWebEngine cookies did not authenticate CDP context")
        return False

    @property
    def is_connected(self) -> bool:
        """Whether we have an active CDP connection."""
        return self._connected and self._page is not None

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------
    def _handle_login_flow(self, *, is_first_run: bool = False) -> bool:
        """Handle Spotify login through the in-app BrowserAuth overlay.

        Args:
            is_first_run: Whether this is the first time using the profile.

        Returns:
            True if login succeeded, False on timeout.
        """
        context = "First-run" if is_first_run else "Re-authentication"
        logger.info(
            "%s: Spotify login required. Starting BrowserAuth overlay...",
            context,
        )

        # Notify user via toast that login is needed.
        if is_first_run:
            self._notify_user("Spotify login required - sign in inside Viola.")
        else:
            self._notify_user("Spotify session expired - please sign in again.")

        if not self._start_browser_auth_overlay():
            self._notify_user("Open Settings > Music and connect Spotify, then try again.")
            return False

        # Poll for login success
        timeout = 120  # 2 minutes
        poll_interval = 2
        start = time.time()

        while time.time() - start < timeout:
            if self._try_cookie_bridge_login() or self.is_logged_in():
                logger.info(
                    "%s: Spotify login confirmed from BrowserAuth cookies.",
                    context,
                )
                self._notify_user("Spotify login successful!")
                self.hide_window()
                # Navigate to Spotify home after login
                self.navigate_home()
                return True
            time.sleep(poll_interval)

        # Timeout — notify user and hide window
        logger.warning(
            "%s: Spotify login timed out after %ds.",
            context,
            timeout,
        )
        self._notify_user("Spotify login timed out. Falling back to YouTube for playback.")
        self.hide_window()
        return False

    @staticmethod
    def _start_browser_auth_overlay() -> bool:
        """Start Spotify BrowserAuth through the local runtime endpoint."""
        try:
            import httpx

            from config.settings import settings
            from core.constants import DEFAULT_API_PORT
            from ui.security.bootstrap import load_bootstrap_api_key

            port = getattr(settings, "api_port", DEFAULT_API_PORT)
            headers: dict[str, str] = {}
            api_key = load_bootstrap_api_key()
            if api_key:
                headers["X-API-Key"] = api_key

            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    "http://127.0.0.1:%d/v1/browser/auth/login/spotify" % port,
                    headers=headers,
                )
            if resp.status_code != 200:
                logger.warning("Spotify BrowserAuth start returned status %d", resp.status_code)
                return False
            payload = resp.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            return not (isinstance(data, dict) and data.get("controller_attached") is False)
        except Exception:
            logger.debug("Spotify BrowserAuth start failed")
            return False

    @staticmethod
    def _notify_user(message: str) -> None:
        """Send a toast notification to the UI (best-effort)."""
        try:
            from core.state_hub import SetToastMessage, get_state_hub

            get_state_hub().dispatch(SetToastMessage(message=message))
        except Exception:
            logger.debug("Toast dispatch skipped (state hub not initialized)")

    def handle_reauth(self) -> None:
        """Handle re-authentication (thread-safe)."""
        self._in_cdp_thread(self._handle_reauth_impl)

    def _handle_reauth_impl(self) -> None:
        """Handle re-authentication when session expires during playback.

        Called by the engine's poll loop when is_logged_in() returns False.
        """
        reconnect_notice = False
        needs_reconnect = (
            not self._connected or self._page is None or self._chrome_process_exited() or not self._is_port_open()
        )
        if not needs_reconnect and self._page is not None:
            try:
                self._page.evaluate("1 + 1")
            except Exception:
                logger.warning("CDP page unresponsive during re-auth, reconnecting...")
                needs_reconnect = True

        if needs_reconnect:
            reconnect_notice = self._begin_unexpected_reconnect_notice()
            try:
                if not self._is_port_open():
                    self.launch()
                self._connect_impl()
                self._ensure_chrome_registered_for_capture()
            except Exception:
                self._finish_unexpected_reconnect_notice(reconnect_notice, success=False)
                raise

        if self._try_cookie_bridge_login() or self._is_logged_in_impl():
            self._finish_unexpected_reconnect_notice(reconnect_notice, success=True)
            return

        self._finish_unexpected_reconnect_notice(reconnect_notice, success=True)
        if not self._handle_login_flow(is_first_run=False):
            raise SpotifyCDPError("Spotify re-authentication failed.")

    # ------------------------------------------------------------------
    # Login detection
    # ------------------------------------------------------------------
    def is_logged_in(self) -> bool:
        """Check if user is logged into Spotify (thread-safe)."""
        return self._in_cdp_thread(self._is_logged_in_impl)

    def get_login_status(self) -> dict[str, Any]:
        """Return sanitized Spotify login state details (thread-safe)."""
        return self._in_cdp_thread(self._login_status_impl)

    def _login_status_impl(self) -> dict[str, Any]:
        """Return login state without exposing cookies, tokens, or identifiers."""
        if not self._page:
            return {
                "logged_in": False,
                "auth_state": "not_connected",
                "login_error_code": None,
                "challenge_required": False,
            }

        result = self._safe_evaluate(_LOGIN_CHECK_JS, "login check")
        cookie_authenticated = self._has_authenticated_spotify_cookies_impl()
        if result and isinstance(result, dict):
            login_page_state = any(
                bool(result.get(marker))
                for marker in (
                    "is_login_page",
                    "has_password_field",
                    "has_username_field",
                    "challenge_required",
                )
            )
            if (
                not bool(result.get("logged_in", False))
                and cookie_authenticated
                and not login_page_state
                and not result.get("login_error_code")
            ):
                return {
                    "logged_in": True,
                    "auth_state": "authenticated_cdp_cookies",
                    "login_error_code": None,
                    "challenge_required": False,
                    "is_login_page": False,
                    "has_password_field": False,
                    "has_username_field": False,
                }
            return {
                "logged_in": bool(result.get("logged_in", False)),
                "auth_state": str(result.get("auth_state") or "unknown"),
                "login_error_code": result.get("login_error_code"),
                "challenge_required": bool(result.get("challenge_required", False)),
                "is_login_page": bool(result.get("is_login_page", False)),
                "has_password_field": bool(result.get("has_password_field", False)),
                "has_username_field": bool(result.get("has_username_field", False)),
            }
        if cookie_authenticated:
            return {
                "logged_in": True,
                "auth_state": "authenticated_cdp_cookies",
                "login_error_code": None,
                "challenge_required": False,
                "is_login_page": False,
                "has_password_field": False,
                "has_username_field": False,
            }
        return {
            "logged_in": False,
            "auth_state": "unknown",
            "login_error_code": None,
            "challenge_required": False,
        }

    def _has_authenticated_spotify_cookies_impl(self) -> bool:
        """Return True when the live CDP context has Spotify auth cookies."""
        playwright_errors = _playwright_error_types()
        contexts: list[Any] = []
        if self._pw_context is not None:
            contexts.append(self._pw_context)
        page_context = getattr(self._page, "context", None) if self._page is not None else None
        if page_context is not None and page_context not in contexts:
            contexts.append(page_context)

        for context in contexts:
            try:
                cookies = context.cookies("https://open.spotify.com")
            except TypeError as exc:
                logger.debug("Spotify CDP cookie probe string-form read failed: %s", exc)
                try:
                    cookies = context.cookies(["https://open.spotify.com"])
                except playwright_errors as list_exc:
                    logger.debug("Spotify CDP cookie probe list-form read failed: %s", list_exc)
                    cookies = []
            except playwright_errors as exc:
                logger.debug("Spotify CDP cookie probe read failed: %s", exc)
                cookies = []
            if not cookies:
                continue
            cookie_names = {
                str(cookie.get("name") or "") for cookie in cookies if "spotify" in str(cookie.get("domain") or "")
            }
            if {"sp_dc", "sp_key"}.issubset(cookie_names):
                _cdp_session_known_present_by_user.add(self._user_id)
                return True
        return False

    def _is_logged_in_impl(self) -> bool:
        """Check if user is logged into Spotify.

        Returns:
            True if logged in, False otherwise.
        """
        return bool(self._login_status_impl().get("logged_in", False))

    # ------------------------------------------------------------------
    # Account tier
    # ------------------------------------------------------------------
    @staticmethod
    def _account_tier_status(
        *,
        product: str = "unknown",
        is_premium: bool | None = None,
        source: str = "unavailable",
        message: str | None = None,
        web_api_status: int | None = None,
    ) -> dict[str, Any]:
        normalized_product = (product or "unknown").lower()
        # Free tier plays full tracks with ads — we deliberately do NOT report
        # premium_required_for_full_playback=True because that gets interpreted
        # by upstream callers as a hard playback gate. Premium is only required
        # for an ad-free experience and arbitrary on-demand seek; a Free user
        # can listen to the same songs Viola plays for a Premium user.
        payload: dict[str, Any] = {
            "product": normalized_product,
            "is_premium": is_premium,
            "source": source,
            "premium_required_for_full_playback": False,
            "full_track_playback_supported": is_premium if is_premium is not None else None,
            "ads_between_tracks": is_premium is False if is_premium is not None else None,
        }
        if web_api_status is not None:
            payload["web_api_status"] = web_api_status

        if message is None:
            if is_premium is True:
                message = "Spotify Premium detected. Ad-free playback available."
            elif is_premium is False:
                message = "Spotify Free detected. Full tracks play with ads between songs."
            else:
                message = "Spotify account tier could not be confirmed from the current browser session."
        payload["message"] = message
        return payload

    def get_account_tier_status(
        self,
        *,
        max_age_seconds: float = 900.0,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Return Spotify Premium/Free status from the connected browser session."""
        return self._in_cdp_thread(
            self._get_account_tier_status_impl,
            max_age_seconds=max_age_seconds,
            force_refresh=force_refresh,
        )

    def _get_account_tier_status_impl(
        self,
        *,
        max_age_seconds: float = 900.0,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Check account tier without exposing Spotify tokens or profile details."""
        now = time.time()
        if (
            not force_refresh
            and self._account_tier_cache is not None
            and max_age_seconds > 0
            and now - self._account_tier_cache_at <= max_age_seconds
        ):
            return dict(self._account_tier_cache)

        if not self._page or not self._connected:
            payload = self._account_tier_status(
                source="not_connected",
                message="Spotify account tier is unavailable until CDP is connected.",
            )
            self._account_tier_cache = dict(payload)
            self._account_tier_cache_at = now
            return payload

        if not self._is_logged_in_impl():
            payload = self._account_tier_status(
                source="not_logged_in",
                message="Sign in to Spotify before checking Premium status.",
            )
            self._account_tier_cache = dict(payload)
            self._account_tier_cache_at = now
            return payload

        result = self._safe_evaluate(_ACCOUNT_TIER_JS, "account tier")
        if not isinstance(result, dict):
            payload = self._account_tier_status()
            self._account_tier_cache = dict(payload)
            self._account_tier_cache_at = now
            return payload

        product = str(result.get("product") or "unknown").lower()
        is_premium_raw = result.get("is_premium")
        is_premium = is_premium_raw if isinstance(is_premium_raw, bool) else None
        source = str(result.get("source") or "unavailable")
        web_api_status_raw = result.get("web_api_status")
        web_api_status = web_api_status_raw if isinstance(web_api_status_raw, int) else None

        payload = self._account_tier_status(
            product=product,
            is_premium=is_premium,
            source=source,
            web_api_status=web_api_status,
        )
        self._account_tier_cache = dict(payload)
        self._account_tier_cache_at = now
        return payload

    # ------------------------------------------------------------------
    # Now playing
    # ------------------------------------------------------------------
    def get_now_playing(self) -> dict[str, Any] | None:
        """Get current playback state (thread-safe)."""
        return self._in_cdp_thread(self._get_now_playing_impl)

    def _get_now_playing_impl(self) -> dict[str, Any] | None:
        """Get current playback state from the Spotify DOM.

        Returns:
            Dict with keys: track, artist, artwork_url, position, duration, is_playing.
            Returns None if no data can be read.
        """
        if not self._page:
            return None

        result = self._safe_evaluate(_NOW_PLAYING_JS, "now playing")
        if result and isinstance(result, dict):
            return result
        return None

    # ------------------------------------------------------------------
    # Transport controls (keyboard shortcuts — more resilient than DOM)
    # ------------------------------------------------------------------
    @staticmethod
    def _now_playing_is_target(payload: Any, track_id: str, target_title: str | None) -> bool:
        if not isinstance(payload, dict):
            return False
        if str(payload.get("track_id") or "") == track_id:
            return True
        expected_title = str(target_title) if isinstance(target_title, str) else None
        return _track_text_matches(expected_title, str(payload.get("track") or ""))

    def _nudge_footer_playback(self) -> None:
        """Pause/resume the footer transport once to recover Spotify false-starts."""
        if not self._page:
            return
        try:
            button = self._page.query_selector('button[data-testid="control-button-playpause"]')
            if button:
                button.click(timeout=5000, force=True)
            else:
                self._page.keyboard.press("Space")
            time.sleep(0.35)
            button = self._page.query_selector('button[data-testid="control-button-playpause"]')
            if button:
                button.click(timeout=5000, force=True)
            else:
                self._page.keyboard.press("Space")
            logger.info("Spotify CDP: nudged footer play/pause after stalled direct-track start")
        except _playwright_error_types() as exc:
            logger.debug("Spotify CDP footer playback nudge failed: %s", exc)

    def _wait_for_target_track_progress(self, track_id: str, target_title: str | None, deadline: float) -> None:
        # Prefer the media element's advancing currentTime as ground truth, and
        # resume the element if it landed paused. Spotify can hide the media
        # element after an in-page track switch; in that shape, require the
        # target now-playing widget to report playing and its position to
        # advance before accepting playback.
        first_audio_t: float | None = None
        first_audio_at: float | None = None
        first_widget_pos: float | None = None
        first_widget_at: float | None = None
        nudged = False
        last_payload: Any = None
        while time.time() < deadline:
            audio_state = self._safe_evaluate(_ENSURE_AUDIO_PLAYING_JS, "ensure spotify audio playing")
            now_playing = self._safe_evaluate(_NOW_PLAYING_JS, "now playing after direct URI click")
            last_payload = now_playing
            audio_present = bool(isinstance(audio_state, dict) and audio_state.get("present"))
            audio_t = float(audio_state["t"]) if audio_present and audio_state.get("t") is not None else None
            # If the widget reports a track it must be our target; an empty
            # widget (it lags badly) is trusted since we clicked the target row.
            widget_has_track = isinstance(now_playing, dict) and bool(now_playing.get("track"))
            on_target = (not widget_has_track) or self._now_playing_is_target(now_playing, track_id, target_title)
            widget_pos = _now_playing_position_seconds(now_playing if isinstance(now_playing, Mapping) else None)
            widget_playing_on_target = (
                widget_has_track
                and on_target
                and bool(isinstance(now_playing, dict) and now_playing.get("is_playing"))
                and widget_pos > 0
            )
            if audio_t is not None and on_target:
                first_widget_pos = None
                first_widget_at = None
                if first_audio_t is None or audio_t < first_audio_t:
                    # baseline, or re-baseline when a new track resets currentTime
                    first_audio_t = audio_t
                    first_audio_at = time.time()
                elif audio_t >= first_audio_t + _DIRECT_TRACK_PROGRESS_DELTA_S:
                    return  # media element genuinely advancing -> playing
                elif (
                    not nudged
                    and first_audio_at is not None
                    and time.time() - first_audio_at >= _DIRECT_TRACK_STALL_RETRY_S
                ):
                    self._nudge_footer_playback()
                    nudged = True
                    first_audio_t = None
                    first_audio_at = None
            elif widget_playing_on_target:
                first_audio_t = None
                first_audio_at = None
                if first_widget_pos is None or widget_pos < first_widget_pos:
                    first_widget_pos = widget_pos
                    first_widget_at = time.time()
                elif widget_pos >= first_widget_pos + _DIRECT_TRACK_PROGRESS_DELTA_S:
                    return
                elif (
                    not nudged
                    and first_widget_at is not None
                    and time.time() - first_widget_at >= _DIRECT_TRACK_STALL_RETRY_S
                ):
                    self._nudge_footer_playback()
                    nudged = True
                    first_widget_pos = None
                    first_widget_at = None
            else:
                first_audio_t = None
                first_audio_at = None
                first_widget_pos = None
                first_widget_at = None
            time.sleep(_DIRECT_TRACK_POLL_S)
        raise SpotifyCDPError(
            "Track page Play did not start advancing playback for Spotify track id=%s; last_now_playing=%r"
            % (track_id, last_payload)
        )

    def play_pause(self) -> None:
        """Toggle play/pause (thread-safe)."""
        self._in_cdp_thread(self._play_pause_impl)

    def _play_pause_impl(self) -> None:
        """Toggle play/pause using the footer transport button."""
        self._ensure_page()
        try:
            button = self._page.query_selector('button[data-testid="control-button-playpause"]')
            if button:
                button.click(timeout=5000, force=True)
            else:
                self._page.keyboard.press("Space")
        except Exception as exc:
            raise SpotifyCDPError("play_pause failed: %s" % exc) from exc

    def next_track(self) -> None:
        """Skip to next track (thread-safe)."""
        self._in_cdp_thread(self._next_track_impl)

    def _next_track_impl(self) -> None:
        """Skip to next track using the footer control."""
        self._ensure_page()
        try:
            button = self._page.query_selector('button[data-testid="control-button-skip-forward"]')
            if button:
                button.click(timeout=5000, force=True)
            else:
                self._page.keyboard.press("Control+ArrowRight")
        except Exception as exc:
            raise SpotifyCDPError("next_track failed: %s" % exc) from exc

    def prev_track(self) -> None:
        """Go to previous track (thread-safe)."""
        self._in_cdp_thread(self._prev_track_impl)

    def _prev_track_impl(self) -> None:
        """Go to previous track using the footer control."""
        self._ensure_page()
        try:
            button = self._page.query_selector('button[data-testid="control-button-skip-back"]')
            if button:
                button.click(timeout=5000, force=True)
            else:
                self._page.keyboard.press("Control+ArrowLeft")
        except Exception as exc:
            raise SpotifyCDPError("prev_track failed: %s" % exc) from exc

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self, query: str, *, max_wait: float = 10.0) -> list[dict[str, Any]]:
        """Search Spotify (thread-safe)."""
        return self._in_cdp_thread(self._search_impl, query, max_wait)

    def _search_impl(self, query: str, max_wait: float = 10.0) -> list[dict[str, Any]]:
        """Search Spotify by navigating to the search URL.

        Args:
            query: Search query string.
            max_wait: Max seconds to wait for results.

        Returns:
            List of dicts: [{"title": str, "artist": str, "uri": str|None, "index": int}]
        """
        self._ensure_page()
        encoded = quote(query)
        # Land directly on the songs view, not the multi-section search page.
        # `/search/<query>` shows top-result + artists + albums + a small
        # "Songs" preview that does NOT use the `tracklist-row` testid; the
        # `/tracks` variant is the songs-only table whose rows DO carry the
        # `tracklist-row` testid the scraper expects.
        search_url = "https://open.spotify.com/search/%s/tracks" % encoded

        try:
            self._page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            raise SpotifyCDPError("Search navigation failed: %s" % exc) from exc

        # Wait for Spotify's SPA to finish hydrating the table. networkidle
        # rarely fires on Spotify (analytics / telemetry never settles) so we
        # use a short ceiling and rely on the poll loop below for correctness.
        try:
            self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            logger.debug("networkidle timed out, falling back to poll loop")

        # Polling fallback — covers cases where the selector above misses
        deadline = time.time() + max_wait
        results: list[dict[str, Any]] = []

        while time.time() < deadline:
            data = self._safe_evaluate(_SEARCH_RESULTS_JS, "search results")
            if data and isinstance(data, list) and len(data) > 0:
                results = data
                break
            time.sleep(0.8)

        if not results:
            logger.warning("Spotify search returned no results for query=%r", query)

        return results

    def play_track_by_uri(self, uri: str, *, max_wait: float = 10.0) -> dict[str, str | None]:
        """Navigate to a Spotify track page by URI and click Play (thread-safe).

        Bypasses the search-then-click path, which can pick a different
        track when Spotify's search ranking diverges from the LLM's choice.
        Use when you have an exact spotify:track:<id> from search_tracks.

        Returns the human metadata read from the track page —
        ``{"track_id", "title", "artist"}`` — so the caller can replace a
        raw-URI queue-item title with the real track name at play start
        (the direct-URI resolution path has no other metadata source).
        """
        return self._in_cdp_thread(self._play_track_by_uri_impl, uri, max_wait)

    def _play_track_by_uri_impl(self, uri: str, max_wait: float = 10.0) -> dict[str, str | None]:
        """Navigate directly to /track/<id> and click the page's Play button.

        Spotify's track page shows a single track header with a large Play
        button (button[data-testid="play-button"][aria-label^="Play"]).
        Clicking it requires real OS mouse events — same on-screen-then-
        off-screen dance the search-result click uses.
        """
        # Accept "spotify:track:abc", full URL, or bare id; extract just the id.
        raw = uri.strip()
        if not raw:
            raise SpotifyCDPError("Empty Spotify track URI")
        if raw.startswith("spotify:track:"):
            track_id = raw[len("spotify:track:") :]
        elif "/track/" in raw:
            track_id = raw.split("/track/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        else:
            track_id = raw
        track_id = track_id.strip()
        if not track_id:
            raise SpotifyCDPError("Could not extract track id from %r" % uri)

        self._ensure_page()
        track_url = "https://open.spotify.com/track/%s" % track_id

        try:
            self._page.goto(track_url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            raise SpotifyCDPError("Track page navigation failed: %s" % exc) from exc

        # Spotify SPA hydration; networkidle rarely fires (analytics keep it busy).
        try:
            self._page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            logger.debug("networkidle timed out on track page; continuing")

        # Move window on-screen for a real click that triggers Spotify's React handlers.
        try:
            self._page.evaluate("window.moveTo(0, 0); window.resizeTo(1280, 720);")
        except Exception:
            logger.debug("Window move/resize eval failed (non-critical)")
        time.sleep(0.3)

        try:
            # Poll for the Play button — the SPA may still be hydrating.
            play_btn = None
            deadline = time.time() + max_wait
            selectors = (
                '[data-testid="action-bar"] button[data-testid="play-button"]',
                '[data-testid="action-bar-row"] button[data-testid="play-button"]',
                'main button[data-testid="play-button"]',
            )
            while time.time() < deadline:
                for sel in selectors:
                    candidates = self._page.query_selector_all(sel)
                    for candidate in candidates:
                        box = candidate.bounding_box()
                        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
                            play_btn = candidate
                            break
                    if play_btn is not None:
                        break
                if play_btn:
                    break
                time.sleep(0.5)

            if not play_btn:
                raise SpotifyCDPError("Play button not found on track page %s after %.1fs" % (track_url, max_wait))
            track_page_meta = self._safe_evaluate(
                r"""
(() => {
    const metaTitle = document.querySelector('meta[property="og:title"]')?.content;
    const pageTitle = document.title;
    const h1 = document.querySelector('h1')?.textContent;
    // Display title: og:title / h1 are the clean track name; document.title
    // carries " - song and lyrics by ... | Spotify" noise, so it is only a
    // matching fallback, never the display value.
    const displayTitle = ((metaTitle || h1 || "").trim()) || null;
    const artistLink = document.querySelector('a[data-testid="creator-link"]');
    const artist = (artistLink && artistLink.textContent && artistLink.textContent.trim()) || null;
    return {
        match_title: (metaTitle || pageTitle || h1 || "").trim(),
        title: displayTitle,
        artist: artist,
    };
})();
""",
                "target track metadata",
            )
            display_title: str | None = None
            display_artist: str | None = None
            target_title: str | None = None
            if isinstance(track_page_meta, dict):
                target_title = str(track_page_meta.get("match_title") or "").strip() or None
                display_title = str(track_page_meta.get("title") or "").strip() or None
                display_artist = str(track_page_meta.get("artist") or "").strip() or None

            now_playing = self._safe_evaluate(_NOW_PLAYING_JS, "now playing before direct URI click")
            already_playing_target = self._now_playing_is_target(now_playing, track_id, target_title) and bool(
                isinstance(now_playing, dict) and now_playing.get("is_playing")
            )
            if not already_playing_target:
                play_btn.click(timeout=10000, force=True)
                logger.info("Clicked Play on Spotify track page id=%s", track_id)

            self._wait_for_target_track_progress(track_id, target_title, time.time() + max_wait)
            # Page-level og:title can be stale after an SPA session restore
            # (observed live 2026-07-02: "Your Library" reported as the track
            # title). Once the target track is verified playing, the
            # now-playing widget is the authoritative metadata source —
            # prefer it, keeping the page meta only as fallback.
            now_playing = self._safe_evaluate(_NOW_PLAYING_JS, "now playing after target confirmed")
            if isinstance(now_playing, dict) and self._now_playing_is_target(now_playing, track_id, target_title):
                widget_track = str(now_playing.get("track") or "").strip()
                widget_artist = str(now_playing.get("artist") or "").strip()
                if widget_track:
                    display_title = widget_track
                if widget_artist:
                    display_artist = widget_artist
            return {"track_id": track_id, "title": display_title, "artist": display_artist}
        finally:
            time.sleep(0.3)
            try:
                self._page.evaluate("window.moveTo(-32000, -32000);")
            except Exception:
                logger.debug("Window moveTo off-screen failed (non-critical)")

    def play_track_from_search(self, index: int = 0) -> None:
        """Click the nth search result (thread-safe)."""
        self._in_cdp_thread(self._play_track_from_search_impl, index)

    def _play_track_from_search_impl(self, index: int = 0) -> None:
        """Click the nth search result to start playback.

        Strategy: temporarily move Chrome on-screen, perform a real
        Playwright click (which generates OS-level mouse events that
        trigger Spotify's React handlers), then move it back off-screen.

        Synthetic events (JS .click(), dispatch_event) do NOT trigger
        Spotify's React handlers.  Playwright's native .click() generates
        real OS mouse events but requires the window to be on-screen.

        Args:
            index: Zero-based index of the result to play.
        """
        self._ensure_page()

        rows = self._page.query_selector_all('[data-testid="tracklist-row"]')
        if index >= len(rows):
            raise SpotifyCDPError("Row index %d out of range (only %d rows)" % (index, len(rows)))

        row = rows[index]

        # Move window on-screen for the click, then back off-screen.
        try:
            self._page.evaluate("window.moveTo(0, 0); window.resizeTo(1280, 720);")
        except Exception:
            logger.debug("Window move/resize eval failed (non-critical)")
        time.sleep(0.3)

        try:
            # Primary: click the in-row play button (aria-label="Play <track>...")
            # force=True bypasses actionability checks — needed because the
            # album art <img> overlaps the play button and "intercepts pointer
            # events" according to Playwright's hit-test.
            play_btn = row.query_selector('button[aria-label^="Play"]')
            if play_btn:
                play_btn.click(timeout=10000, force=True)
                logger.info("Clicked row play button for search result #%d", index)
            else:
                # Fallback: double-click the row itself (NOT the link)
                row.dblclick(timeout=10000, force=True)
                logger.info("Fallback: double-clicked row for search result #%d", index)
        finally:
            # Move back off-screen
            time.sleep(0.3)
            try:
                self._page.evaluate("window.moveTo(-32000, -32000);")
            except Exception:
                logger.debug("Window moveTo off-screen failed (non-critical)")

    # ------------------------------------------------------------------
    # Window management
    # ------------------------------------------------------------------
    def hide_window(self) -> None:
        """Move Chrome window off-screen (thread-safe)."""
        self._in_cdp_thread(self._hide_window_impl)

    def _hide_window_impl(self) -> None:
        """Move Chrome window off-screen."""
        self._ensure_page()
        try:
            self._page.evaluate("window.moveTo(-32000, -32000);")
        except Exception as exc:
            logger.warning("hide_window failed: %s", exc)

    # ------------------------------------------------------------------
    # Volume control (slider click)
    # ------------------------------------------------------------------
    def set_volume(self, level: int) -> None:
        """Set volume by clicking the volume slider at the target position (thread-safe).

        Args:
            level: Volume level 0-100.
        """
        self._in_cdp_thread(self._set_volume_impl, level)

    def _set_volume_impl(self, level: int) -> None:
        """Set volume by clicking the volume slider at the target position."""
        self._ensure_page()
        level = max(0, min(100, level))
        playwright_errors = _playwright_error_types()

        try:
            try:
                self._page.evaluate("window.moveTo(0, 0); window.resizeTo(1280, 720);")
                time.sleep(0.2)
            except playwright_errors:
                logger.debug("Window move/resize eval failed before volume click (non-critical)")

            dom_result = self._page.evaluate(
                """level => {
                    const input = Array.from(document.querySelectorAll('input[type="range"]')).find(el => {
                        const min = Number.parseFloat(el.min || '0');
                        const max = Number.parseFloat(el.max || '0');
                        const box = el.getBoundingClientRect();
                        return min === 0 && max === 1 && box.width > 0 && box.height > 0;
                    });
                    if (!input) {
                        return {ok: false, reason: 'volume_input_not_found'};
                    }
                    const value = String(Math.max(0, Math.min(1, level / 100)));
                    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                    if (descriptor && descriptor.set) {
                        descriptor.set.call(input, value);
                    } else {
                        input.value = value;
                    }
                    input.dispatchEvent(new InputEvent('input', {
                        bubbles: true,
                        composed: true,
                        inputType: 'insertText',
                        data: value,
                    }));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    return {ok: true, value: input.value};
                }""",
                level,
            )
            if isinstance(dom_result, dict) and dom_result.get("ok"):
                logger.info("Volume input set to %d%% (value=%s)", level, dom_result.get("value"))
                return

            volume_input = None
            for candidate in self._page.query_selector_all('input[type="range"], input'):
                box = candidate.bounding_box()
                if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
                    continue
                value = candidate.evaluate("""el => {
                        const value = Number.parseFloat(el.value);
                        if (!Number.isFinite(value)) return null;
                        return value;
                    }""")
                if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
                    volume_input = candidate
                    break

            if volume_input is not None:
                box = volume_input.bounding_box()
            else:
                volume_bar = self._page.query_selector('[data-testid="volume-bar"]')
                if not volume_bar:
                    logger.warning("Volume slider not found in DOM")
                    return
                box = volume_bar.bounding_box()
                if not box:
                    logger.warning("Volume slider has no bounding box (off-screen?)")
                    return

            # Volume bar is horizontal: left = 0%, right = 100%
            target_x = box["x"] + (box["width"] * level / 100)
            target_y = box["y"] + box["height"] / 2

            self._page.mouse.click(target_x, target_y)
            logger.info("Volume slider clicked at %d%% (x=%.0f y=%.0f)", level, target_x, target_y)
        except playwright_errors as exc:
            logger.warning("set_volume failed: %s", exc)
        finally:
            try:
                self._page.evaluate("window.moveTo(-32000, -32000);")
            except playwright_errors:
                logger.debug("Window moveTo off-screen failed after volume click (non-critical)")

    # ------------------------------------------------------------------
    # Seek (progress bar click)
    # ------------------------------------------------------------------
    def seek(self, position_ms: int) -> None:
        """Seek by clicking the progress bar at a calculated position (thread-safe).

        Args:
            position_ms: Target position in milliseconds.
        """
        self._in_cdp_thread(self._seek_impl, position_ms)

    def _seek_impl(self, position_ms: int) -> None:
        """Seek by clicking the progress bar at a calculated position."""
        self._ensure_page()
        playwright_errors = _playwright_error_types()
        try:
            self._page.evaluate("window.moveTo(0, 0); window.resizeTo(1280, 720);")
            time.sleep(0.2)
        except playwright_errors:
            logger.debug("Window move/resize eval failed before seek click (non-critical)")

        # Read duration from DOM to calculate click percentage
        try:
            dur_text = self._safe_evaluate(
                'document.querySelector(\'[data-testid="playback-duration"]\')?.textContent || ""',
                "seek duration",
            )
            duration_s = _parse_time_str(dur_text) if isinstance(dur_text, str) else 0.0
            if duration_s <= 0:
                logger.warning("Seek: cannot determine track duration")
                return

            duration_ms = duration_s * 1000
            pct = max(0.0, min(1.0, position_ms / duration_ms))

            # Try multiple selectors for the progress bar
            progress_bar = None
            for selector in (
                '[data-testid="playback-progressbar"]',
                ".playback-progressbar",
                ".x-progressBar-progressBarBg",
                ".progress-bar",
            ):
                progress_bar = self._page.query_selector(selector)
                if progress_bar:
                    break

            if not progress_bar:
                logger.warning("Seek: progress bar not found in DOM")
                return

            box = progress_bar.bounding_box()
            if not box:
                logger.warning("Seek: progress bar has no bounding box")
                return

            # Progress bar is horizontal: left = 0%, right = 100%
            target_x = box["x"] + (box["width"] * pct)
            target_y = box["y"] + box["height"] / 2

            self._page.mouse.click(target_x, target_y)
            logger.info(
                "Seek: clicked progress bar at %.1f%% (target=%dms, duration=%dms)",
                pct * 100,
                position_ms,
                int(duration_ms),
            )
        finally:
            try:
                self._page.evaluate("window.moveTo(-32000, -32000);")
            except playwright_errors:
                logger.debug("Window moveTo off-screen failed after seek click (non-critical)")

    # ------------------------------------------------------------------
    # Playlist playback (URL navigation)
    # ------------------------------------------------------------------
    def play_playlist(self, playlist_id: str) -> None:
        """Navigate to a Spotify playlist and start playback (thread-safe).

        Args:
            playlist_id: Spotify playlist ID (e.g., "37i9dQZF1DXcBWIGoYBM5M").
        """
        self._in_cdp_thread(self._play_playlist_impl, playlist_id)

    def _play_playlist_impl(self, playlist_id: str) -> None:
        """Navigate to a Spotify playlist and click play."""
        self._ensure_page()

        playlist_url = "https://open.spotify.com/playlist/%s" % playlist_id

        try:
            self._page.goto(
                playlist_url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as exc:
            raise SpotifyCDPError("Playlist navigation failed: %s" % exc) from exc

        try:
            self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.debug("networkidle timed out for playlist page")

        # Extra settle time for Spotify SPA rendering
        time.sleep(1.5)

        # Click the large play button on the playlist page
        try:
            play_btn = self._page.query_selector('button[data-testid="play-button"]')
            if not play_btn:
                # Fallback: aria-label based selector
                play_btn = self._page.query_selector('button[aria-label="Play"]')
            if play_btn:
                play_btn.click()
                logger.info(
                    "Playlist play button clicked for %s",
                    playlist_id,
                )
            else:
                raise SpotifyCDPError("Could not find play button on playlist page")
        except SpotifyCDPError:
            raise
        except Exception as exc:
            raise SpotifyCDPError("Playlist play failed: %s" % exc) from exc

    # ------------------------------------------------------------------
    # Navigate to Spotify home
    # ------------------------------------------------------------------
    def navigate_home(self) -> None:
        """Navigate back to Spotify home page (thread-safe)."""
        self._in_cdp_thread(self._navigate_home_impl)

    def _navigate_home_impl(self) -> None:
        """Navigate back to Spotify home page."""
        self._ensure_page()
        try:
            self._page.goto(SPOTIFY_URL, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
        except Exception as exc:
            logger.warning("navigate_home failed: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def disconnect(self) -> None:
        """Disconnect the CDP session and reset controller state."""
        self.shutdown()

    def shutdown(self) -> None:
        """Shut down CDP controller (thread-safe)."""
        self._in_cdp_thread(self._shutdown_impl)

    def _shutdown_impl(self) -> None:
        """Close Playwright connection and kill Viola's Chrome process.

        Only kills the Chrome process that Viola launched (tracked by PID).
        Does NOT kill other Chrome processes.
        """
        logger.info("Shutting down Spotify CDP controller")
        self._connected = False
        self._page = None
        self._account_tier_cache = None
        self._account_tier_cache_at = 0.0

        self._stop_playwright_session()

        if self._chrome_proc:
            pid = self._chrome_proc.pid
            self._unregister_chrome_for_capture()
            try:
                self._chrome_proc.terminate()
                self._chrome_proc.wait(timeout=5)
                logger.info("Chrome process %d terminated", pid)
            except Exception:
                try:
                    self._chrome_proc.kill()
                    logger.info("Chrome process %d killed", pid)
                except Exception:
                    logger.warning("Failed to kill Chrome process %d", pid)
            self._chrome_proc = None

    # ------------------------------------------------------------------
    # Multi-room ProcTap integration
    # ------------------------------------------------------------------
    _chrome_registered_for_capture: bool = False

    def _ensure_chrome_registered_for_capture(self) -> None:
        """Idempotent: register Chrome PIDs for ProcTap if not already done."""
        if self._chrome_registered_for_capture:
            return
        if self._chrome_proc is not None:
            self._register_chrome_for_capture()
        else:
            self._register_existing_chrome_for_capture()

    def _register_existing_chrome_for_capture(self) -> None:
        """Register an already-running Chrome's PID for ProcTap capture.

        When Chrome is already listening on the CDP port (launched outside
        Viola), find its PID via the listening socket and register it.
        Uses multiple discovery strategies since net_connections may need
        elevated privileges on Windows.
        """
        try:
            import psutil
        except ImportError:
            return

        try:
            from audio_core.capture.source_audio_controller import (
                register_external_audio_pid,
            )
            from audio_core.streaming.pipeline_wiring import notify_proctap_pid
        except ImportError:
            logger.debug("source_audio_controller not available; skipping ProcTap registration")
            return

        chrome_pid = self._find_chrome_cdp_pid()
        if not chrome_pid:
            logger.warning("Could not find Chrome CDP PID for ProcTap registration")
            return

        registered = [chrome_pid]
        register_external_audio_pid(chrome_pid)
        notify_proctap_pid(chrome_pid)
        try:
            for child in psutil.Process(chrome_pid).children(recursive=True):
                register_external_audio_pid(child.pid)
                registered.append(child.pid)
        except Exception:
            logger.debug("Chrome PID child registration failed (process may have exited)")

        self._chrome_registered_for_capture = True
        logger.info(
            "Registered Chrome PID %d (+%d children) for ProcTap",
            chrome_pid,
            len(registered) - 1,
        )

    def _find_chrome_cdp_pid(self) -> int | None:
        """Find the Chrome PID listening on the CDP port.

        Tries multiple strategies:
        1. psutil.net_connections (fast, may need elevated privileges)
        2. Iterate all chrome.exe processes and check cmdline for CDP port
        """
        import psutil

        # Strategy 1: net_connections (may fail with AccessDenied on Windows)
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.laddr.port == CDP_PORT and conn.status == "LISTEN":
                    if conn.pid:
                        logger.debug("Found Chrome CDP PID %d via net_connections", conn.pid)
                        return conn.pid
        except (psutil.AccessDenied, PermissionError):
            logger.debug("net_connections needs elevated privileges, trying process scan")
        except Exception:
            logger.debug("net_connections failed, trying process scan")

        # Strategy 2: Scan chrome.exe processes for our CDP port in cmdline
        port_flag = "--remote-debugging-port=%d" % CDP_PORT
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["name"] and "chrome" in proc.info["name"].lower():
                        cmdline = proc.info.get("cmdline") or []
                        if any(port_flag in arg for arg in cmdline):
                            logger.debug("Found Chrome CDP PID %d via cmdline scan", proc.pid)
                            return proc.pid
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            logger.debug("Process scan for Chrome CDP also failed")

        return None

    def get_capture_chrome_pid(self) -> int | None:
        """Return the Chrome PID whose audio ProcTap should capture.

        Prefers the Chrome process this controller launched (when still
        alive); falls back to discovering the PID listening on the CDP
        port (Chrome launched outside Viola).  Used by the playback
        engine to point ProcTap at the KNOWN Chrome PID on resume,
        instead of rescanning for whoever happens to be audible.
        """
        proc = self._chrome_proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    return proc.pid
            except OSError:
                logger.debug("Chrome process liveness check failed", exc_info=True)
        return self._find_chrome_cdp_pid()

    def _register_chrome_for_capture(self) -> None:
        """Register Chrome's PID tree for ProcTap audio capture.

        When Spotify CDP launches Chrome, its audio plays in Chrome's
        process tree (not Viola's QtWebEngine).  ProcTap only scans Viola's
        children by default.  Registering the PID makes the Chrome audio
        session discoverable by the multi-room capture pipeline.
        """
        if self._chrome_proc is None:
            return

        chrome_pid = self._chrome_proc.pid
        try:
            from audio_core.capture.source_audio_controller import (
                register_external_audio_pid,
            )
            from audio_core.streaming.pipeline_wiring import notify_proctap_pid

            # Register the main Chrome PID.  Its children (renderer, audio
            # utility) are found by psutil.Process(chrome_pid).children().
            register_external_audio_pid(chrome_pid)
            notify_proctap_pid(chrome_pid)

            # Also register immediate children (renderer, GPU, audio service)
            # in case Chrome re-parents them outside the main process tree.
            try:
                import psutil

                for child in psutil.Process(chrome_pid).children(recursive=True):
                    register_external_audio_pid(child.pid)
            except Exception:
                logger.debug("Chrome PID registration failed (psutil unavailable or process exited)")

            self._chrome_registered_for_capture = True
            logger.info("Registered launched Chrome PID %d for ProcTap", chrome_pid)

        except ImportError:
            logger.debug("source_audio_controller not available; skipping ProcTap registration")
        except Exception:
            logger.exception("Failed to register Chrome PID for ProcTap")

    def _unregister_chrome_for_capture(self) -> None:
        """Remove Chrome PIDs from ProcTap capture candidates."""
        if self._chrome_proc is None:
            self._chrome_registered_for_capture = False
            return

        chrome_pid = self._chrome_proc.pid
        try:
            from audio_core.capture.source_audio_controller import (
                unregister_external_audio_pid,
            )

            unregister_external_audio_pid(chrome_pid)

            try:
                import psutil

                for child in psutil.Process(chrome_pid).children(recursive=True):
                    unregister_external_audio_pid(child.pid)
            except Exception:
                logger.debug("Chrome PID unregistration failed (non-critical)")
        except ImportError:
            logger.debug("psutil unavailable while unregistering Chrome child PIDs")
        except Exception:
            logger.exception("Failed to unregister Chrome PID from ProcTap")
        finally:
            self._chrome_registered_for_capture = False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _ensure_page(self) -> None:
        """Verify we have an active page, raising if not."""
        if not self._page or not self._connected:
            raise SpotifyCDPError("Not connected to Spotify. Call ensure_running() first.")

    def _safe_evaluate(self, js: str, description: str = "") -> Any:
        """Safely evaluate JS in the page, returning None on error."""
        if not self._page:
            return None
        try:
            result = self._page.evaluate(js)
            if isinstance(result, str):
                try:
                    return json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    return result
            return result
        except Exception as exc:
            logger.debug(
                "JS eval error (%s): %s",
                description,
                exc,
            )
            return None
