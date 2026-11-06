"""The one answer to "is Spotify connected?".

Settings, the assistant's tools, the music source fallback and the assistant's
context all read this module, so they can never disagree. Connected means the
player can really work: Spotify's sign-in cookie reached the hidden playback
browser and Spotify's own page, loaded in that browser, accepted it. A cookie
in Viola's sign-in view alone is not connected (1.0.5 build 9: Settings said
Connected from a cached cookie while nothing could play).

States:

``not_signed_in``      Nobody has signed in, or the user disconnected.
``checking``           A sign-in exists and is being tried in the player.
``connected``          The player's page accepted the sign-in.
``session_rejected``   Spotify refused the sign-in in the player: sign in again.
``no_browser``         Neither Edge nor Chrome is installed.
``player_unavailable`` The playback browser could not start or reach Spotify.
``drm_unavailable``    The playback browser cannot decrypt protected audio.

The verdict is kept in memory and in a small file inside the user's playback
profile (no cookie values, nothing secret), so it survives a restart and goes
away with the profile on Disconnect.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

NOT_SIGNED_IN = "not_signed_in"
CHECKING = "checking"
CONNECTED = "connected"
SESSION_REJECTED = "session_rejected"
NO_BROWSER = "no_browser"
PLAYER_UNAVAILABLE = "player_unavailable"
DRM_UNAVAILABLE = "drm_unavailable"

_MESSAGES = {
    NOT_SIGNED_IN: "Spotify isn't connected. Connect it in Settings > Music.",
    CHECKING: "Viola is checking your Spotify sign-in.",
    CONNECTED: "",
    SESSION_REJECTED: "Spotify didn't accept your sign-in. Sign in to Spotify again in Settings > Music.",
    NO_BROWSER: (
        "Spotify needs Microsoft Edge or Google Chrome to play music, and neither was found on this "
        "computer. Install one of them, then try Spotify again."
    ),
    PLAYER_UNAVAILABLE: (
        "Viola's Spotify player couldn't start. Check your internet connection and try again. "
        "If it keeps happening, restart Viola."
    ),
    DRM_UNAVAILABLE: (
        "Microsoft Edge can't play protected music on this computer, so Spotify can't play. "
        "Update Edge, then try again."
    ),
}

PLAY_DID_NOT_START_MESSAGE = (
    "Spotify is connected, but that song didn't start. Try again. If it keeps happening, restart Viola."
)

VERDICT_FILE = "viola_connection.json"

# A play that just failed keeps Spotify out of use for this long, so a retry on
# another service in the same request is treated as the fallback it is.
_RECENT_FAILURE_SECONDS = 120.0


@dataclass(frozen=True)
class SpotifyConnection:
    """What Viola knows about Spotify right now."""

    state: str
    message: str
    checked_at: float | None = None

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    @property
    def playable(self) -> bool:
        """True when playing on Spotify is worth trying (it may still be checking)."""
        return self.state in (CONNECTED, CHECKING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "message": self.message,
            "checked_at": self.checked_at,
        }


_lock = threading.RLock()
_verdicts: dict[str, dict[str, Any]] = {}
_running: set[str] = set()
_last_failures: dict[str, dict[str, Any]] = {}


def message_for(state: str) -> str:
    return _MESSAGES.get(state, _MESSAGES[PLAYER_UNAVAILABLE])


def resolve_user_id(user_id: str | None = None) -> str | None:
    """The install's Spotify user: the request user, else the signed-in account.

    Spotify runs on threads with no request behind them (the music worker, the
    poller, sign-in tasks). They must resolve to the same user as the Settings
    request, or the player and the sign-in end up in two different profiles.
    """
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_or_desktop_active_user_id

        return get_current_or_desktop_active_user_id() or None
    except Exception:
        return None


def _verdict_path(user_id: str) -> Any:
    from music.spotify.cdp_controller import spotify_cdp_profile_path

    return spotify_cdp_profile_path(user_id) / VERDICT_FILE


def _load_verdict(user_id: str) -> dict[str, Any] | None:
    with _lock:
        cached = _verdicts.get(user_id)
    if cached is not None:
        return dict(cached)
    try:
        payload = json.loads(_verdict_path(user_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("state") not in _MESSAGES:
        return None
    with _lock:
        _verdicts[user_id] = dict(payload)
    return payload


def record_verdict(user_id: str, state: str, *, detail: str = "") -> SpotifyConnection:
    """Remember what the player found. Every state change is logged."""
    if state not in _MESSAGES:
        raise ValueError("Unknown Spotify connection state: %s" % state)
    payload = {"state": state, "detail": detail[:200], "checked_at": time.time()}
    with _lock:
        previous = (_verdicts.get(user_id) or {}).get("state")
        _verdicts[user_id] = dict(payload)
        if state == CONNECTED:
            _last_failures.pop(user_id, None)
    try:
        path = _verdict_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        logger.debug("Could not save the Spotify connection verdict", exc_info=True)
    if previous != state:
        logger.info("Spotify connection: %s -> %s (%s)", previous or "unknown", state, detail or "no detail")
    return SpotifyConnection(state=state, message=_MESSAGES[state], checked_at=payload["checked_at"])


def forget_verdict(user_id: str) -> None:
    """Disconnect: no verdict, no recent failure."""
    with _lock:
        _verdicts.pop(user_id, None)
        _last_failures.pop(user_id, None)
    try:
        _verdict_path(user_id).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove the Spotify connection verdict", exc_info=True)


def _sign_in_present(user_id: str) -> bool | None:
    """Does the in-app browser hold Spotify's sign-in cookie? None when unknown."""
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        return get_browser_auth_manager(user_id).refresh_spotify_sign_in()
    except Exception:
        logger.debug("Spotify sign-in read failed", exc_info=True)
        return None


def _player_profile_signed_in(user_id: str) -> bool:
    """Does the playback profile still hold a sign-in cookie? (Sign-in store unreadable or empty.)"""
    try:
        from music.spotify.cdp_controller import has_cdp_session_cookies

        return bool(has_cdp_session_cookies(user_id=user_id))
    except Exception:
        return False


def get_spotify_connection(user_id: str | None = None) -> SpotifyConnection:
    """The current answer. Blocking but cheap; async callers use ``asyncio.to_thread``.

    Never starts the playback browser itself. With a sign-in and no verdict yet
    (an upgrade, or a sign-in made before this check existed) it starts one
    background check and reports ``checking``.
    """
    resolved = resolve_user_id(user_id)
    if not resolved:
        return SpotifyConnection(NOT_SIGNED_IN, _MESSAGES[NOT_SIGNED_IN])

    with _lock:
        checking = resolved in _running
    if checking:
        return SpotifyConnection(CHECKING, _MESSAGES[CHECKING])

    verdict = _load_verdict(resolved)
    sign_in = _sign_in_present(resolved)
    has_sign_in = bool(sign_in) or _player_profile_signed_in(resolved)
    if sign_in is None and verdict is not None:
        # The sign-in store can't be read right now: the last verdict stands.
        has_sign_in = True

    if not has_sign_in:
        return SpotifyConnection(NOT_SIGNED_IN, _MESSAGES[NOT_SIGNED_IN])

    if verdict is None:
        start_verification(resolved, reason="sign_in_without_verdict")
        return SpotifyConnection(CHECKING, _MESSAGES[CHECKING])

    state = str(verdict.get("state"))
    return SpotifyConnection(state, _MESSAGES[state], verdict.get("checked_at"))


def peek_state(user_id: str | None = None) -> str:
    """The last known state, with no cookie read and no check started.

    For code on the event loop, such as building the assistant's context.
    """
    resolved = resolve_user_id(user_id)
    if not resolved:
        return NOT_SIGNED_IN
    with _lock:
        if resolved in _running:
            return CHECKING
    verdict = _load_verdict(resolved)
    return str(verdict.get("state")) if verdict else NOT_SIGNED_IN


def verify_now(user_id: str, *, reason: str) -> SpotifyConnection:
    """Try the sign-in in the playback browser and record the verdict. Blocking."""
    from music.providers.spotify_cdp import get_cdp_controller

    logger.info("Spotify connection: checking the sign-in in the player (%s)", reason)
    report = get_cdp_controller(user_id=user_id).check_session()
    state = str(report.get("state") or PLAYER_UNAVAILABLE)
    return record_verdict(user_id, state, detail=str(report.get("detail") or reason))


def start_verification(user_id: str, *, reason: str, notify_failure: bool = False) -> bool:
    """Run ``verify_now`` on a background thread. One at a time per user."""
    with _lock:
        if user_id in _running:
            return False
        _running.add(user_id)

    def _run() -> None:
        result: SpotifyConnection | None = None
        try:
            result = verify_now(user_id, reason=reason)
        except Exception as exc:
            logger.exception("Spotify connection check failed")
            result = record_verdict(user_id, PLAYER_UNAVAILABLE, detail=type(exc).__name__)
        finally:
            with _lock:
                _running.discard(user_id)
        if notify_failure and result is not None and not result.connected:
            notify_spotify(user_id, result.message, code="spotify_%s" % result.state)

    threading.Thread(target=_run, name="SpotifyConnectionCheck", daemon=True).start()
    return True


def record_play_failure(user_id: str | None, *, state: str, message: str, code: str) -> None:
    """A play on Spotify failed. Remember why, so tools and Settings report it."""
    resolved = resolve_user_id(user_id)
    if not resolved:
        return
    with _lock:
        _last_failures[resolved] = {"state": state, "message": message, "code": code, "at": time.monotonic()}
    if state != CONNECTED:
        record_verdict(resolved, state, detail=code)


def recent_play_failure(user_id: str | None = None) -> dict[str, Any] | None:
    """Why the last Spotify play failed, when that was just now."""
    resolved = resolve_user_id(user_id)
    if not resolved:
        return None
    with _lock:
        failure = _last_failures.get(resolved)
        if failure is None:
            return None
        if time.monotonic() - float(failure["at"]) > _RECENT_FAILURE_SECONDS:
            _last_failures.pop(resolved, None)
            return None
        return dict(failure)


def clear_play_failure(user_id: str | None = None) -> None:
    """A new Spotify start is under way; an older failure no longer describes it."""
    resolved = resolve_user_id(user_id)
    if resolved:
        with _lock:
            _last_failures.pop(resolved, None)


def spotify_usable(user_id: str | None = None) -> bool:
    """Is Spotify worth trying right now?"""
    if recent_play_failure(user_id) is not None:
        return False
    return get_spotify_connection(user_id).playable


def notify_spotify(user_id: str | None, message: str, *, code: str, level: str = "info") -> bool:
    """Say something about Spotify on screen. Returns True when it went out."""
    try:
        from music.providers.browser.auth_manager import notify_user_about_spotify

        return notify_user_about_spotify(user_id, message, code=code, level=level)
    except Exception:
        logger.debug("Spotify notice failed", exc_info=True)
        return False


__all__ = [
    "CHECKING",
    "CONNECTED",
    "DRM_UNAVAILABLE",
    "NOT_SIGNED_IN",
    "NO_BROWSER",
    "PLAYER_UNAVAILABLE",
    "SESSION_REJECTED",
    "SpotifyConnection",
    "clear_play_failure",
    "forget_verdict",
    "get_spotify_connection",
    "message_for",
    "notify_spotify",
    "peek_state",
    "recent_play_failure",
    "record_play_failure",
    "record_verdict",
    "resolve_user_id",
    "spotify_usable",
    "start_verification",
    "verify_now",
]
