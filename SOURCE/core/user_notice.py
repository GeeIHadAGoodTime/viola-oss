"""Tell the person using Viola that something broke.

Viola already has a working road for this and has had one for a while: an
``error`` event on the desktop EventHub arrives at the React app as
``{"type": "error", "payload": {...}}``, ``usePlayerState`` hands it to the
error callback, and ``SmartDisplay`` raises a toast. Exactly one caller has
ever driven on it (``bootstrap.factory._broadcast_startup_warning``, for two
music-folder warnings), so every other subsystem that needed to say something
either grew a private path or settled for a log line the user will never open.

Audio is the worst case of that: local TTS playback catches every exception and
returns, and startup logs a dead output device and carries on, so a user whose
speaker is broken watches Viola answer in text and never learns why it went
quiet.

This module is the connector, not a new subsystem: the transport (EventHub),
the wording (``core.error_messages``), and the toast are all pre-existing. It
adds thread-safety and a single import for callers who are nowhere near an
event loop, which is where the audio code lives.

Notices are best-effort by construction. Nothing here may raise into a caller,
because the caller is always in the middle of doing something more important
than complaining about it.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from core.error_messages import get_user_message
from core.logging_config import get_logger

logger = get_logger(__name__)

# The event type the React app listens for (ui/react-app/src/hooks/usePlayerState.js).
USER_NOTICE_EVENT = "error"

VALID_LEVELS = frozenset({"error", "warning", "info"})

# Notices raised before the UI exists — an audio device that failed validation
# during bootstrap is the motivating case — would otherwise be broadcast into an
# empty room and lost. They are held here and delivered when the first client
# connects (ui/websocket/event_hub.py::connect drains this), which is the same
# treatment the hub already gives the cached state snapshot.
#
# Bounded, because a failure that recurs must not grow this without limit; the
# dedup on `code` means a repeating failure occupies exactly one slot.
_MAX_PENDING = 10
_pending: list[dict[str, Any]] = []
_pending_lock = threading.Lock()

# The closed failure set a best-effort notice may hit. A bare ``except`` here
# would swallow real programmer errors; these are the concrete ways the
# transport can be absent or half-built (mirrors the ``_BRIDGE_ERRORS``
# convention in services/notifications/timer_notifier.py).
_NOTICE_ERRORS = (
    ImportError,
    RuntimeError,
    OSError,
    TypeError,
    ValueError,
    AttributeError,
    LookupError,
)


def _resolve_hub() -> Any | None:
    try:
        from ui.websocket.event_hub import get_event_hub
    except ImportError:  # pragma: no cover - headless / cloud, no desktop UI layer
        return None
    return get_event_hub()


def _resolve_loop(hub: Any | None) -> Any | None:
    """Find a running main loop to schedule the async broadcast on.

    Callers are frequently on a worker thread (local TTS playback runs under
    ``asyncio.to_thread``), so ``get_running_loop`` is not available to them.
    """
    loop = None
    try:
        from core.asyncio_safe import get_main_loop

        loop = get_main_loop()
    except ImportError:  # pragma: no cover - defensive
        loop = None
    if loop is not None and getattr(loop, "is_running", lambda: False)():
        return loop
    loop = getattr(hub, "_main_loop", None) if hub is not None else None
    if loop is not None and getattr(loop, "is_running", lambda: False)():
        return loop
    return None


def _hold_for_later(payload: dict[str, Any]) -> None:
    """Keep an undeliverable notice for the first client that connects."""
    with _pending_lock:
        for existing in _pending:
            if existing.get("code") == payload.get("code"):
                return
        if len(_pending) >= _MAX_PENDING:
            return
        _pending.append(payload)


def drain_pending_notices() -> list[dict[str, Any]]:
    """Take every notice raised before any client could receive it.

    Called by the EventHub as a client connects. Draining (rather than
    peeking) means a notice is shown once, not re-raised on every reconnect.
    """
    with _pending_lock:
        drained = list(_pending)
        _pending.clear()
    return drained


def notify_user(
    code: str,
    message: str | None = None,
    *,
    level: str = "error",
    user_id: str | None = None,
    hold_if_undeliverable: bool = True,
) -> bool:
    """Surface *code* to the user as an on-screen notice. Never raises.

    Args:
        code: Machine code for the failure. Used to look up wording when
            *message* is omitted, and passed through so the UI can react to a
            specific failure rather than parsing prose.
        message: The sentence to show. Defaults to this code's entry in
            ``core.error_messages``.
        level: ``error``, ``warning`` or ``info``; drives the toast styling.
        user_id: Whose screen to reach. Defaults to this device's user, which
            is the right answer on desktop (one install, one account) and keeps
            the broadcast tenant-scoped rather than global.
        hold_if_undeliverable: Keep the notice for the first client to connect
            when there is nobody to receive it yet. On by default because the
            failures worth reporting at boot are exactly the ones raised before
            any UI exists.

    Returns:
        True when the notice was handed to the transport now. False means it
        was held for later, or dropped — the caller's own logging remains the
        record in that case.
    """
    if level not in VALID_LEVELS:
        level = "error"

    text = message or get_user_message(code)
    payload: dict[str, Any] = {
        "code": code,
        "message": text,
        "user_message": text,
        "level": level,
    }

    if user_id is None:
        # Raised from places with no request context — a TTS worker thread,
        # bootstrap — so resolve the install's logged-in account rather than a
        # device-merge identity. On a cloud surface this raises LookupError,
        # which is the right answer and must NOT be held: a hub-local speaker
        # failure is a fact about one desktop, and holding it would hand it to
        # whichever tenant connects next. Unaddressable means dropped.
        try:
            from core.user_context import get_current_or_desktop_active_user_id

            user_id = get_current_or_desktop_active_user_id()
        except _NOTICE_ERRORS as exc:
            logger.debug("User notice %s dropped: no user to address it to (%s)", code, exc)
            return False

    try:
        hub = _resolve_hub()
        loop = _resolve_loop(hub) if hub is not None else None
        if hub is None or loop is None:
            logger.debug("User notice %s has no live transport yet", code)
            if hold_if_undeliverable:
                _hold_for_later(payload)
            return False

        # force=True skips the flicker throttle; the hub's content-hash dedup
        # still collapses an identical notice repeated in quick succession,
        # which is what keeps a failing speaker from raising one toast per
        # spoken sentence.
        asyncio.run_coroutine_threadsafe(
            hub.broadcast(USER_NOTICE_EVENT, payload, user_id=user_id, force=True),
            loop,
        )
    except _NOTICE_ERRORS as exc:
        logger.debug("User notice %s not delivered: %s", code, exc)
        if hold_if_undeliverable:
            _hold_for_later(payload)
        return False
    return True


__all__ = [
    "USER_NOTICE_EVENT",
    "VALID_LEVELS",
    "drain_pending_notices",
    "notify_user",
]
