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
``signing_in``         The user is signing in, in the player's own window.

There are two ways in. Normally the user signs in inside Viola and the cookie is
handed to the player. If Spotify refuses that, or the user prefers it, the player's
own profile opens in a small visible window and the user signs in there; that
sign-in lives in the player's profile (``sign_in_source`` = ``player``), needs no
handoff and survives a restart like any browser sign-in.

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
SIGNING_IN = "signing_in"

SIGN_IN_SOURCE_PLAYER = "player"

_MESSAGES = {
    NOT_SIGNED_IN: "Spotify isn't connected. Connect it in Settings > Music.",
    CHECKING: "Viola is checking your Spotify sign-in.",
    CONNECTED: "",
    SIGNING_IN: "Finish signing in to Spotify in the window Viola opened.",
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

HANDOVER_REFUSED_MESSAGE = "Spotify wants you to sign in once more. Finish signing in in the window Viola opened."
DIRECT_SIGN_IN_TIMEOUT_SECONDS = 600.0
DIRECT_SIGN_IN_POLL_SECONDS = 2.0

VERDICT_FILE = "viola_connection.json"

# A play that just failed keeps Spotify out of use for this long, so a retry on
# another service in the same request is treated as the fallback it is.
_RECENT_FAILURE_SECONDS = 45.0
# Being offline or a browser hiccup passes. After this long, try the player again
# rather than leaving Spotify marked as not working until someone presses Connect.
_RECHECK_SECONDS = 60.0
_RECHECKED_STATES = (PLAYER_UNAVAILABLE, NO_BROWSER, DRM_UNAVAILABLE)


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
_signing_in: dict[str, threading.Event] = {}
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


def record_verdict(
    user_id: str,
    state: str,
    *,
    detail: str = "",
    sign_in_source: str | None = None,
) -> SpotifyConnection:
    """Remember what the player found. Every state change is logged."""
    if state not in _MESSAGES:
        raise ValueError("Unknown Spotify connection state: %s" % state)
    payload = {"state": state, "detail": detail[:200], "checked_at": time.time()}
    earlier = _load_verdict(user_id) or {}
    source = sign_in_source or earlier.get("sign_in_source")
    if source:
        payload["sign_in_source"] = source
    with _lock:
        previous = earlier.get("state")
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


def signed_in_inside_the_player(user_id: str | None) -> bool:
    """Did the user sign in in the player's own window? Then there is nothing to hand over."""
    resolved = resolve_user_id(user_id)
    if not resolved:
        return False
    return (_load_verdict(resolved) or {}).get("sign_in_source") == SIGN_IN_SOURCE_PLAYER


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


def _login_watch_active(user_id: str) -> bool:
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        return get_browser_auth_manager(user_id).is_login_watch_active("spotify")
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
        signing_in = resolved in _signing_in
    if signing_in:
        return SpotifyConnection(SIGNING_IN, _MESSAGES[SIGNING_IN])
    if checking:
        return SpotifyConnection(CHECKING, _MESSAGES[CHECKING])

    verdict = _load_verdict(resolved)
    if verdict is not None and verdict.get("sign_in_source") == SIGN_IN_SOURCE_PLAYER:
        # The sign-in lives in the player's own profile, and Disconnect removes
        # this verdict before anything else, so leftovers can't bring it back.
        state = str(verdict.get("state"))
        return SpotifyConnection(state, _MESSAGES[state], verdict.get("checked_at"))
    # The in-app store is the only sign-in there is. Files left in the playback
    # profile are not: Disconnect can't always delete them at once, and they must
    # not bring Spotify back.
    sign_in = _sign_in_present(resolved)
    if sign_in is None and verdict is not None:
        sign_in = True  # the store can't be read right now: the last verdict stands

    if not sign_in:
        return SpotifyConnection(NOT_SIGNED_IN, _MESSAGES[NOT_SIGNED_IN])

    if verdict is None:
        if not _login_watch_active(resolved):  # the watch starts the check itself when it finishes
            start_verification(resolved, reason="sign_in_without_verdict", notify_failure=True)
        return SpotifyConnection(CHECKING, _MESSAGES[CHECKING])

    state = str(verdict.get("state"))
    checked_at = verdict.get("checked_at")
    if state in _RECHECKED_STATES and time.time() - float(checked_at or 0.0) > _RECHECK_SECONDS:
        start_verification(resolved, reason="recheck")
        return SpotifyConnection(CHECKING, _MESSAGES[CHECKING])
    return SpotifyConnection(state, _MESSAGES[state], checked_at)


def peek_state(user_id: str | None = None) -> str:
    """The last known state, with no cookie read and no check started.

    For code on the event loop, such as building the assistant's context.
    """
    resolved = resolve_user_id(user_id)
    if not resolved:
        return NOT_SIGNED_IN
    with _lock:
        if resolved in _signing_in:
            return SIGNING_IN
        if resolved in _running:
            return CHECKING
    verdict = _load_verdict(resolved)
    return str(verdict.get("state")) if verdict else NOT_SIGNED_IN


def verify_now(user_id: str, *, reason: str) -> SpotifyConnection:
    """Try the sign-in in the playback browser and record the verdict. Blocking."""
    from music.providers.spotify_cdp import get_cdp_controller

    logger.info("Spotify connection: checking the sign-in in the player (%s)", reason)
    controller = get_cdp_controller(user_id=user_id)
    report = controller.check_session()
    state = str(report.get("state") or PLAYER_UNAVAILABLE)
    result = record_verdict(user_id, state, detail=str(report.get("detail") or reason))
    try:
        from music.spotify.diagnostic import log_diagnostic

        log_diagnostic(controller, reason="check_%s" % reason)
    except Exception:
        logger.debug("Spotify diagnostic after the check did not run", exc_info=True)
    if not result.connected:
        # A player nobody can use has no reason to keep running.
        controller.shutdown()
    return result


def start_verification(
    user_id: str,
    *,
    reason: str,
    notify_failure: bool = False,
    direct_sign_in_if_refused: bool = False,
) -> bool:
    """Run ``verify_now`` on a background thread. One at a time per user.

    ``direct_sign_in_if_refused``: the user has just signed in and is watching,
    so if Spotify refuses the handover, open the player's own sign-in right away.
    """
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
        if direct_sign_in_if_refused and result is not None and result.state == SESSION_REJECTED:
            notify_spotify(user_id, HANDOVER_REFUSED_MESSAGE, code="spotify_sign_in_in_window")
            start_direct_sign_in(user_id)
            return
        if notify_failure and result is not None and not result.connected:
            notify_spotify(user_id, result.message, code="spotify_%s" % result.state)

    threading.Thread(target=_run, name="SpotifyConnectionCheck", daemon=True).start()
    return True


def start_direct_sign_in(user_id: str, *, select_as_music_source: bool = False) -> bool:
    """Open the player's own window for the user to sign in. Returns False if already open."""
    cancel = threading.Event()
    with _lock:
        if user_id in _signing_in:
            return False
        _signing_in[user_id] = cancel

    def _run() -> None:
        try:
            _direct_sign_in(user_id, cancel, select_as_music_source=select_as_music_source)
        except Exception:
            logger.exception("Spotify sign-in in the player's window failed")
            notify_spotify(user_id, message_for(PLAYER_UNAVAILABLE), code="spotify_player_unavailable", level="error")
        finally:
            with _lock:
                _signing_in.pop(user_id, None)

    threading.Thread(target=_run, name="SpotifyDirectSignIn", daemon=True).start()
    return True


def cancel_direct_sign_in(user_id: str) -> bool:
    """Stop waiting and close the sign-in window. Returns True when one was open."""
    with _lock:
        cancel = _signing_in.get(user_id)
    if cancel is None:
        return False
    cancel.set()
    return True


def _direct_sign_in(user_id: str, cancel: threading.Event, *, select_as_music_source: bool) -> None:
    from music.providers.spotify_cdp import get_cdp_controller
    from music.spotify.cdp_controller import SpotifyCDPError

    controller = get_cdp_controller(user_id=user_id)
    try:
        controller.open_direct_sign_in()
    except SpotifyCDPError as exc:
        logger.warning("Spotify sign-in window did not open: %s", exc)
        record_verdict(user_id, exc.state, detail="sign-in window")
        notify_spotify(user_id, message_for(exc.state), code="spotify_%s" % exc.state, level="error")
        return

    deadline = time.monotonic() + DIRECT_SIGN_IN_TIMEOUT_SECONDS
    status = "waiting"
    while status == "waiting" and not cancel.is_set() and time.monotonic() < deadline:
        cancel.wait(DIRECT_SIGN_IN_POLL_SECONDS)
        status = controller.direct_sign_in_status()

    if status != "signed_in" or cancel.is_set():
        logger.info("Spotify sign-in window closed without a sign-in (%s)", status)
        controller.close_direct_sign_in()
        return

    logger.info("Spotify sign-in seen in the player's window; hiding it and checking")
    controller.close_direct_sign_in()
    record_verdict(user_id, CHECKING, detail="direct sign-in", sign_in_source=SIGN_IN_SOURCE_PLAYER)
    with _lock:
        _signing_in.pop(user_id, None)
        _running.add(user_id)
    try:
        result = verify_now(user_id, reason="direct_sign_in")
    finally:
        with _lock:
            _running.discard(user_id)
    if result.connected:
        notify_spotify(user_id, "Spotify is connected.", code="spotify_connected")
        if select_as_music_source:
            _select_as_music_source(user_id)
    else:
        notify_spotify(user_id, result.message, code="spotify_%s" % result.state, level="error")


def _select_as_music_source(user_id: str) -> None:
    import asyncio

    from core.asyncio_safe import get_main_loop
    from music.providers.browser.auth_manager import get_browser_auth_manager

    loop = get_main_loop()
    if loop is None or not loop.is_running():
        logger.warning("Spotify is connected, but the music source could not be saved (no event loop)")
        return
    asyncio.run_coroutine_threadsafe(get_browser_auth_manager(user_id)._select_music_source("spotify"), loop)


def record_play_failure(user_id: str | None, *, state: str, message: str, code: str) -> None:
    """A play on Spotify failed. Remember why, so tools and Settings report it."""
    resolved = resolve_user_id(user_id)
    if not resolved:
        return
    with _lock:
        _last_failures[resolved] = {"state": state, "message": message, "code": code, "at": time.monotonic()}
    if state not in (CONNECTED, SIGNING_IN):
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
    "SIGNING_IN",
    "SpotifyConnection",
    "cancel_direct_sign_in",
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
    "signed_in_inside_the_player",
    "spotify_usable",
    "start_direct_sign_in",
    "start_verification",
    "verify_now",
]
