"""What the React YouTube iframe itself reported, and waiting for it.

On the desktop a YouTube track plays inside the React UI's own ``<iframe>``.
The backend sets ``now_playing`` and an optimistic PLAYING phase when it hands
a track to React, but only the iframe knows whether sound is coming out. It
says so in two ways over the WebSocket:

* ``youtube_state`` frames: the YouTube player state (PLAYING, PAUSED, ...)
  with the video id React has loaded;
* ``position_update`` frames, about once a second, with the current position.
  They keep arriving while the player is paused, with a frozen position.

1.0.5 build 7, session 4: "play Drake" picked the video that was already
loaded and paused at 21.7 s. The backend marked it playing and Viola said so,
but nothing reached the iframe; it stayed paused for 10+ minutes, and the
embedded watchdog counted that time as playing and skipped to the next song
out loud. This module keeps the iframe's own reports on the player so that
play requests can be confirmed (``await_iframe_playback``) and the watchdog
can count only time the position really advanced.

All ``record_*`` helpers must be called with ``player._cv`` held.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

IFRAME_PLAYING = "PLAYING"

# How long a play request waits for the iframe to report PLAYING, and when it
# re-sends ``play`` to the iframe if nothing has arrived yet.
PLAY_CONFIRM_TIMEOUT_S = 8.0
PLAY_CONFIRM_NUDGE_AFTER_S = 3.0
_PLAY_CONFIRM_POLL_S = 0.1

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
SUPERSEDED = "superseded"


def _now() -> float:
    return time.monotonic()


def record_iframe_state(player: Any, state: str, video_id: str | None, position_ms: int) -> None:
    """Remember the iframe's latest ``youtube_state`` report."""
    now = _now()
    try:
        player._iframe_state = state
        player._iframe_state_video_id = video_id or None
        player._iframe_state_at = now
        if state == IFRAME_PLAYING:
            player._iframe_playing_at = now
            player._iframe_playing_video_id = video_id or None
        player._iframe_last_position_ms = int(position_ms)
    except (AttributeError, TypeError, ValueError):
        logger.debug("Could not record iframe state on %s", type(player).__name__, exc_info=True)


def record_iframe_position(player: Any, position_ms: int) -> None:
    """Remember an iframe ``position_update``; note when it really moved forward."""
    try:
        position = int(position_ms)
    except (TypeError, ValueError):
        return
    previous = getattr(player, "_iframe_last_position_ms", None)
    try:
        if (
            isinstance(previous, int)
            and position > previous
            and getattr(player, "_iframe_state", None) == IFRAME_PLAYING
        ):
            player._iframe_position_advanced_at = _now()
        player._iframe_last_position_ms = position
    except AttributeError:
        logger.debug("Could not record iframe position on %s", type(player).__name__, exc_info=True)


def _same_video(reported: str | None, wanted: str | None) -> bool:
    # React stamps every youtube_state frame with the video it has loaded.
    # A frame without one (older UI build) cannot contradict the request.
    return not reported or not wanted or reported == wanted


def iframe_confirmed_playing(player: Any, *, since: float, video_id: str | None) -> bool:
    """True when the iframe reported, after ``since``, that ``video_id`` is playing.

    Evidence is either a PLAYING state report or the position moving forward
    while the iframe's last state was PLAYING (a re-play of the video that is
    already playing need not produce a new state change). The iframe's
    current state must still be PLAYING: a PLAYING that was followed by a
    PAUSED does not count.
    """
    if getattr(player, "_iframe_state", None) != IFRAME_PLAYING:
        return False
    if not _same_video(getattr(player, "_iframe_state_video_id", None), video_id):
        return False
    playing_at = getattr(player, "_iframe_playing_at", None)
    if isinstance(playing_at, (int, float)) and playing_at >= since:
        return _same_video(getattr(player, "_iframe_playing_video_id", None), video_id)
    advanced_at = getattr(player, "_iframe_position_advanced_at", None)
    return isinstance(advanced_at, (int, float)) and advanced_at >= since


def _now_playing_video_id(player: Any) -> str | None:
    state = getattr(player, "_state", None)
    now_playing = getattr(state, "now_playing", None)
    video_id = getattr(now_playing, "video_id", None)
    return video_id if isinstance(video_id, str) and video_id else None


async def await_iframe_playback(
    player: Any,
    *,
    since: float,
    video_id: str | None,
    timeout_s: float = PLAY_CONFIRM_TIMEOUT_S,
    nudge_after_s: float = PLAY_CONFIRM_NUDGE_AFTER_S,
    nudge: Callable[[], Awaitable[Any]] | None = None,
) -> str:
    """Wait until the iframe confirms ``video_id`` is playing.

    Returns ``CONFIRMED``, ``SUPERSEDED`` (another track became current while
    waiting; that request owns the confirmation), or ``UNCONFIRMED`` after
    ``timeout_s``. ``nudge`` (re-send ``play`` to the iframe) runs once after
    ``nudge_after_s`` without confirmation.
    """
    cv = getattr(player, "_cv", None)
    started = _now()
    nudged = False
    while True:
        if cv is not None:
            with cv:
                confirmed = iframe_confirmed_playing(player, since=since, video_id=video_id)
                current_video = _now_playing_video_id(player)
        else:
            confirmed = iframe_confirmed_playing(player, since=since, video_id=video_id)
            current_video = _now_playing_video_id(player)
        if confirmed:
            return CONFIRMED
        if video_id and current_video and current_video != video_id:
            return SUPERSEDED
        elapsed = _now() - started
        if nudge is not None and not nudged and elapsed >= nudge_after_s:
            nudged = True
            try:
                await nudge()
            except Exception:
                logger.warning("PLAY_CONFIRM: re-sending play to the player failed", exc_info=True)
        if elapsed >= timeout_s:
            return UNCONFIRMED
        await asyncio.sleep(_PLAY_CONFIRM_POLL_S)


__all__ = [
    "CONFIRMED",
    "IFRAME_PLAYING",
    "PLAY_CONFIRM_NUDGE_AFTER_S",
    "PLAY_CONFIRM_TIMEOUT_S",
    "SUPERSEDED",
    "UNCONFIRMED",
    "await_iframe_playback",
    "iframe_confirmed_playing",
    "record_iframe_position",
    "record_iframe_state",
]
