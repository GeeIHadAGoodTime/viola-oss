"""Is a phone call live right now? Read by the speech path to keep the PC quiet.

The recording of call 3745134a holds the PC's own voice: "The call connected, but the
prank didn't go as planned..." came off the laptop speaker, into Jay's phone, and down
the line. Viola on the phone took it for the recipient, and "call, you" became a turn
that cancelled her reply. So while a call is live the PC says nothing. Replies still
appear on screen, and the outcome is spoken once the call has ended.

Kept free of heavy imports: the speech engines import this.
"""

from __future__ import annotations

import threading
import time

# A call is capped at 10 minutes. This only matters if the end of a call is never
# seen, and it must not leave the PC mute for good.
_LONGEST_CALL_SECS = 720.0

_lock = threading.Lock()
_live_until: dict[str, float] = {}


def note_call_live(call_id: str) -> None:
    if call_id:
        with _lock:
            _live_until[call_id] = time.monotonic() + _LONGEST_CALL_SECS


def note_call_over(call_id: str = "") -> None:
    """The call ended. With no id, every call this device knows about."""
    with _lock:
        if call_id:
            _live_until.pop(call_id, None)
        else:
            _live_until.clear()


def live_call_active() -> bool:
    now = time.monotonic()
    with _lock:
        for call_id in [key for key, until in _live_until.items() if until <= now]:
            del _live_until[call_id]
        return bool(_live_until)
