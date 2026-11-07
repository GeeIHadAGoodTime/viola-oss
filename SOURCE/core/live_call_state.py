"""Which speech waits for a phone call to end, decided by where it came from.

The recording of call 3745134a holds the PC's own report: "The call connected, but the
prank didn't go as planned..." came off the laptop speaker, into Jay's phone, and down
the line. Viola on the phone took it for the recipient.

So the turn that placed a call stays quiet about it while it is live: its progress
lines and its reply are shown on screen and not spoken. That is the only speech held.
Anything the user asks on the PC during a call is a new turn and speaks normally, even
a question about the call, and so do reminders and everything else. Nothing here looks
at wording: a turn is either the one that placed the call or it is not.

When the call ends the held report is spoken, as one line in its after-the-call form.
What the turn wrote while the call ran ("still going", "calling them now") would be
false by then, so the line is the cloud's summary when the call's end event carries
one.

Kept free of heavy imports: the speech path imports this.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable

from core.logging_config import get_logger

logger = get_logger(__name__)

# A call is capped at 10 minutes. This only matters if the end of a call is never
# seen: the turn stops being held, and a held report is spoken anyway.
_LONGEST_CALL_SECS = 720.0
_NO_SUMMARY_LINE = "The Phone tab has how it went."

_lock = threading.Lock()
_live_until: dict[str, float] = {}
_narrating_turns: dict[str, str] = {}
_held: dict[str, tuple[asyncio.AbstractEventLoop | None, threading.Timer]] = {}
_speaker: Callable[[str], Awaitable[None]] | None = None


def set_held_report_speaker(speaker: Callable[[str], Awaitable[None]] | None) -> None:
    """The real speech path, used for a held report once its call has ended."""
    global _speaker
    _speaker = speaker


def note_call_live(call_id: str) -> None:
    if call_id:
        with _lock:
            _live_until[call_id] = time.monotonic() + _LONGEST_CALL_SECS


def call_is_live(call_id: str) -> bool:
    with _lock:
        until = _live_until.get(call_id)
    return until is not None and time.monotonic() < until


def note_turn_placed_call(turn_id: str | None, call_id: str) -> None:
    """Everything this turn says from here is about its call."""
    if turn_id and call_id:
        with _lock:
            _narrating_turns[turn_id] = call_id


def live_call_of_turn(turn_id: str | None) -> str:
    """The live call this turn placed, or "" when it may speak."""
    if not turn_id:
        return ""
    with _lock:
        call_id = _narrating_turns.get(turn_id, "")
    return call_id if call_id and call_is_live(call_id) else ""


def hold_report(call_id: str) -> None:
    """Keep this call's report for when it ends. Once per call, however many lines."""
    if not call_id:
        return
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    with _lock:
        if call_id in _held:
            return
        remaining = max(0.0, _live_until.get(call_id, 0.0) - time.monotonic())
        timer = threading.Timer(remaining, note_call_over, args=(call_id,))
        timer.daemon = True
        _held[call_id] = (loop, timer)
    timer.start()


def note_call_over(call_id: str = "", summary: str = "") -> None:
    """The call ended. With no id, every call this device knows about."""
    with _lock:
        ended = [call_id] if call_id else list({*_live_until, *_held})
        released = [_held.pop(ended_id) for ended_id in ended if ended_id in _held]
        for ended_id in ended:
            _live_until.pop(ended_id, None)
        for turn_id in [turn for turn, owner in _narrating_turns.items() if owner in ended]:
            del _narrating_turns[turn_id]
    for loop, timer in released:
        timer.cancel()
        _speak_after_call(loop, summary)


def _speak_after_call(loop: asyncio.AbstractEventLoop | None, summary: str) -> None:
    speaker = _speaker
    if speaker is None:
        return
    line = "The call has ended. %s" % (summary.strip() or _NO_SUMMARY_LINE)

    async def speak() -> None:
        try:
            await speaker(line)
        # A report that cannot be spoken must not disturb whoever ended the call.
        except Exception:  # noqa: BLE001, RUF100
            logger.warning("The held call report could not be spoken", exc_info=True)

    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(speak(), loop)
        return
    try:
        asyncio.get_running_loop().create_task(speak())
    except RuntimeError:
        threading.Thread(target=asyncio.run, args=(speak(),), daemon=True).start()
