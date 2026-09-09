"""Bridge TimerService lifecycle events to the desktop webview EventHub.

Why this exists (issue #1404): a timer set by voice/command used to expire
*silently* on the desktop launch surface. The only user-facing signal was a
one-shot TTS utterance plus a ``push_service.send(...)`` call that never
reaches the desktop UI:

* ``PushNotificationService`` skipped every *user-scoped* notification on the
  OS-toast legs (``services/notifications/push_service.py`` -- the toast was
  only attempted for ``user_id is None`` broadcasts),
* web push returns 0 because a desktop install has no browser push
  subscription, and
* during quiet hours the notification is queued and never shown.

So the user got no on-screen notification, no transcript line, and there was no
active-timer countdown surface at all (nothing consumed the ``TimerService``
signals).

The first and third of those were separately fixed in #4790 -- the toast legs
now key their refusal on the cloud *surface* rather than on the presence of a
``user_id``, the queue processor is actually started, and a user-requested
notification is not deferred by quiet hours -- so the push leg does reach a
desktop user today. This notifier is still the right thing and is not
redundant: it is the *in-app* surface, it carries the live countdown that no
notification can express, and it does not depend on the OS notification
platform being available or permitted. Keep both.

This notifier works by broadcasting timer lifecycle straight to
the webview over the same EventHub the rest of the desktop UI already listens
to -- a live countdown snapshot while timers run and an on-screen notification
when one expires -- so a timer the user explicitly set is *always* surfaced,
regardless of quiet hours, web-push subscriptions, or the OS toast path.

The audible announcement (TTS) and the cloud web-push path stay where they are
(``bootstrap/factory``); this is purely the additive in-app visual surface.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from services.timer_core import TimerEventListener

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# WebSocket event types consumed by the desktop React UI (SmartDisplay /
# useTimers). Keep these strings in sync with ui/react-app/src.
TIMER_UPDATE_EVENT = "timer_update"
TIMER_COMPLETED_EVENT = "timer_completed"


# The closed set of failures the defensive helpers below may hit (mirrors the
# ``_DELIVERY_ERRORS`` tuple convention in push_service.py). Broadcasting a timer
# event must never crash the timer-checker thread, but a blind ``except`` would
# also swallow real programmer errors -- so catch the concrete import/runtime set.
_BRIDGE_ERRORS = (
    ImportError,
    RuntimeError,
    OSError,
    TypeError,
    ValueError,
    AttributeError,
    LookupError,
)


def _default_hub() -> Any | None:
    try:
        from ui.websocket.event_hub import get_event_hub
    except ImportError:  # pragma: no cover - ui layer absent (headless)
        return None
    return get_event_hub()


def _default_loop() -> Any | None:
    """Return a running main event loop for scheduling the async broadcast.

    Prefer the canonically-registered main loop; fall back to the EventHub's
    captured loop (the path ``bootstrap._broadcast_startup_warning`` already
    uses on desktop).
    """
    loop = None
    try:
        from core.asyncio_safe import get_main_loop

        loop = get_main_loop()
    except ImportError:  # pragma: no cover - defensive
        loop = None
    if loop is not None and loop.is_running():
        return loop
    hub = _default_hub()
    loop = getattr(hub, "_main_loop", None) if hub is not None else None
    if loop is not None and getattr(loop, "is_running", lambda: False)():
        return loop
    return None


def _default_service() -> Any | None:
    try:
        from services.timer_service import get_timer_service
    except ImportError:  # pragma: no cover - defensive
        return None
    return get_timer_service()


class TimerNotifier(TimerEventListener):
    """Broadcast timer countdown + expiry events to the desktop webview.

    Register on the process ``TimerService`` via ``add_listener``. Every
    lifecycle callback resolves the current active-timer snapshot for the
    affected user and pushes it (and, on completion, an on-screen
    notification) to that user's WebSocket clients.

    All collaborators are injectable so the behaviour is unit-testable without
    a running Qt app, an event loop, or a live WebSocket: ``emit`` receives the
    fully-built ``(user_id, event_type, payload)`` and defaults to scheduling
    the async EventHub broadcast on the main loop.
    """

    def __init__(
        self,
        *,
        hub_getter: Callable[[], Any] | None = None,
        loop_getter: Callable[[], Any] | None = None,
        service_getter: Callable[[], Any] | None = None,
        emit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._hub_getter = hub_getter or _default_hub
        self._loop_getter = loop_getter or _default_loop
        self._service_getter = service_getter or _default_service
        self._emit = emit or self._default_emit

    # ------------------------------------------------------------------
    # TimerEventListener API
    # ------------------------------------------------------------------
    def timer_added(self, user_id: str, timer: Any) -> None:
        self._broadcast_snapshot(user_id)

    def timer_cancelled(self, user_id: str, timer_id: str, timer: Any) -> None:
        self._broadcast_snapshot(user_id)

    def timer_updated(self, user_id: str, timer: Any) -> None:
        self._broadcast_snapshot(user_id)

    def timers_changed(self, user_id: str | None = None) -> None:
        # The global (user_id=None) fan-out carries no user scope; the
        # per-user callback already delivers the authoritative snapshot.
        if user_id:
            self._broadcast_snapshot(user_id)

    def timer_completed(self, user_id: str, timer_id: str, label: str) -> None:
        message = "Your %s is done!" % label if label else "Your timer is done!"
        self._emit(
            user_id,
            TIMER_COMPLETED_EVENT,
            {"timer_id": timer_id, "label": label, "message": message},
        )
        # Refresh the countdown so the just-fired timer drops off the UI.
        self._broadcast_snapshot(user_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _active_timers(self, user_id: str) -> list[dict[str, Any]]:
        service = self._service_getter()
        if service is None:
            return []
        try:
            timers = service.get_all_timers(user_id=user_id)
        except _BRIDGE_ERRORS as exc:
            logger.debug("TimerNotifier could not read timers for user=%s: %s", user_id, exc)
            return []
        snapshot: list[dict[str, Any]] = []
        for timer in timers:
            if getattr(timer, "is_expired", False):
                continue
            snapshot.append(
                {
                    "timer_id": timer.timer_id,
                    "label": timer.label,
                    "remaining_seconds": max(0, math.ceil(timer.remaining_seconds)),
                    "duration_seconds": timer.duration_seconds,
                    "end_time": timer.end_time.isoformat(),
                }
            )
        return snapshot

    def _broadcast_snapshot(self, user_id: str | None) -> None:
        if not user_id:
            return
        timers = self._active_timers(user_id)
        self._emit(user_id, TIMER_UPDATE_EVENT, {"timers": timers, "count": len(timers)})

    def _default_emit(self, user_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if not user_id:
            return
        hub = self._hub_getter()
        loop = self._loop_getter()
        if hub is None or loop is None or not loop.is_running():
            logger.debug("Timer %s broadcast skipped; EventHub/loop unavailable", event_type)
            return
        try:
            asyncio.run_coroutine_threadsafe(
                hub.broadcast_to_user(user_id, event_type, payload),
                loop,
            )
        except _BRIDGE_ERRORS:
            logger.exception("Timer %s broadcast failed for user=%s", event_type, user_id)


__all__ = ["TIMER_COMPLETED_EVENT", "TIMER_UPDATE_EVENT", "TimerNotifier"]
