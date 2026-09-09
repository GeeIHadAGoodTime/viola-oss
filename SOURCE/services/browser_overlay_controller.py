"""Browser overlay lifecycle controller.

Manages when the browser QWebEngineView overlay is shown or hidden
based on application state (music playback, agentic navigation, login flows).

State machine::

    HIDDEN (default)
      -> MUSIC:   browser provider starts playing
      -> AGENTIC: user requests web navigation / agent task starts browser tools
      -> LOGIN:   login flow triggered

    MUSIC_VISIBLE
      -> HIDDEN:  music stops
      -> AGENTIC: agentic task interrupts

    AGENTIC_VISIBLE
      -> HIDDEN:  task completes or "go back"
      -> MUSIC:   user says "play something"

    LOGIN_VISIBLE
      -> HIDDEN:  login detected or cancelled

Module-level singleton:
    Use ``set_overlay_controller()`` / ``get_overlay_controller()`` to wire
    the instance created in ``viola_qt.py`` so that other subsystems (e.g.
    ``AgentExecutor``) can access it without circular imports.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: BrowserOverlayController | None = None


def set_overlay_controller(controller: BrowserOverlayController) -> None:
    """Register the application-wide overlay controller singleton."""
    global _instance
    _instance = controller


def get_overlay_controller() -> BrowserOverlayController | None:
    """Return the overlay controller singleton, or None if not yet wired."""
    return _instance


def _dispatch_to_main_thread(fn: Callable[[], None]) -> None:
    """Ensure *fn* runs on the Qt main thread.

    If called from the main thread already, *fn* is invoked immediately.
    Otherwise it is posted to the main-thread event loop via
    ``QTimer.singleShot(0, fn)`` so that Qt widget operations (show/hide)
    never execute on a non-GUI thread — which would cause a fatal
    Windows exception (0x80000003).
    """
    try:
        from PySide6.QtCore import QThread, QTimer
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            # No Qt application running — call directly (e.g. in tests).
            fn()
            return
        if QThread.currentThread() == app.thread():
            fn()
        else:
            # IMPORTANT: pass `app` as the context QObject so the singleshot
            # runs on the Qt main thread. Without the context arg, the slot
            # runs on the thread that called singleShot -- which here is the
            # agent's asyncio worker thread with no Qt event loop, so the
            # slot never fires. That silently swallows every
            # show_browser_overlay / hide_browser_overlay call and leaves the
            # overlay invisible at its default 100x30 size. This was a
            # regression introduced by commit 4cf6d082 ("fix: Qt thread
            # safety for browser overlay") -- the intent (push to main
            # thread) was right, the QTimer API usage was wrong.
            QTimer.singleShot(0, app, fn)
    except ImportError:
        # PyQt6 not available — fall back to direct call.
        fn()


class OverlayState(str, Enum):
    """Overlay visibility states."""

    HIDDEN = "hidden"
    MUSIC = "music"
    AGENTIC = "agentic"
    LOGIN = "login"


class BrowserOverlayController:
    """Controls when the browser overlay shows and hides.

    This is the brain that decides what the user sees.  It coordinates
    between the Qt overlay methods, the browser playback controller,
    and the React frontend via WebSocket state broadcasts.

    Parameters
    ----------
    show_fn : callable
        Called with no args to make the overlay visible (Qt side).
    hide_fn : callable
        Called with no args to hide the overlay (Qt side).
    broadcast_fn : callable or None
        Called with a dict payload to broadcast overlay state to React
        via WebSocket.  The payload has keys: ``visible``, ``mode``, ``url``.
    """

    def __init__(
        self,
        show_fn: Any = None,
        hide_fn: Any = None,
        broadcast_fn: Any = None,
        default_user_id: str | None = None,
    ) -> None:
        self._state = OverlayState.HIDDEN
        self._show_fn = show_fn
        self._hide_fn = hide_fn
        self._broadcast_fn = broadcast_fn
        self._broadcast_user_id = (default_user_id or "").strip() or None
        self._current_url: str = ""
        # Agent task tracking (populated during AGENTIC state)
        self._agent_task_description: str = ""
        self._agent_task_status: str = ""  # working, completing, payment_review
        self._agent_phase: str = ""  # acting, thinking, user_input
        self._hide_outcome: str = ""  # done, error, cancelled
        # Frame streamer lifecycle: start when AGENTIC + spokes, stop when not
        self._frame_streamer_active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current overlay state as a string."""
        return self._state.value

    def show_music(self) -> None:
        """Show overlay for music playback."""
        self._transition(OverlayState.MUSIC)

    def show_agentic(self, url: str = "", task_description: str = "", *, user_id: str | None = None) -> None:
        """Show overlay for an agentic web navigation task."""
        if user_id:
            self._broadcast_user_id = user_id
        self._current_url = url
        self._agent_task_description = task_description
        self._agent_task_status = "working"
        self._agent_phase = "acting"
        self._transition(OverlayState.AGENTIC)

    def update_agentic_status(
        self,
        *,
        url: str | None = None,
        status: str | None = None,
        description: str | None = None,
        phase: str | None = None,
    ) -> None:
        """Update agent task progress without a state transition.

        Only takes effect when already in AGENTIC state.

        Parameters
        ----------
        phase : str or None
            One of ``"acting"`` (executing a tool), ``"thinking"``
            (waiting for LLM response), or ``"user_input"`` (payment
            review / takeover).
        """
        if self._state != OverlayState.AGENTIC:
            return
        if url is not None:
            self._current_url = url
        if status is not None:
            self._agent_task_status = status
        if description is not None:
            self._agent_task_description = description
        if phase is not None:
            self._agent_phase = phase
        self._broadcast()

    def show_login(self, provider: str = "") -> None:
        """Show overlay for a provider login flow."""
        self._current_url = provider
        self._transition(OverlayState.LOGIN)

    def hide(self, *, outcome: str = "") -> None:
        """Hide the overlay, returning to React media area.

        Parameters
        ----------
        outcome : str
            If set, included in the final broadcast so the frontend can
            show a completion toast.  One of ``"done"``, ``"error"``,
            ``"cancelled"``, or ``""`` (no toast).
        """
        self._agent_task_description = ""
        self._agent_task_status = ""
        self._agent_phase = ""
        self._current_url = ""
        self._hide_outcome = outcome
        self._transition(OverlayState.HIDDEN)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _transition(self, new_state: OverlayState) -> None:
        """Perform a state transition and fire side effects."""
        old = self._state
        self._state = new_state

        if new_state == OverlayState.HIDDEN:
            if self._hide_fn is not None:
                try:
                    _dispatch_to_main_thread(self._hide_fn)
                except Exception:
                    logger.exception("hide_fn failed")
        else:
            if self._show_fn is not None:
                try:

                    def _show() -> None:
                        try:
                            self._show_fn(new_state.value)
                        except TypeError:
                            self._show_fn()

                    _dispatch_to_main_thread(_show)
                except Exception:
                    logger.exception("show_fn failed")

        self._broadcast()

        # Frame streamer lifecycle: start on AGENTIC entry, stop on exit
        if new_state == OverlayState.AGENTIC and not self._frame_streamer_active:
            self._start_frame_streaming()
        elif new_state != OverlayState.AGENTIC and self._frame_streamer_active:
            self._stop_frame_streaming()

        if old != new_state:
            logger.info(
                "Browser overlay: %s -> %s (url=%s)",
                old.value,
                new_state.value,
                self._current_url[:80] if self._current_url else "",
            )

    def _start_frame_streaming(self) -> None:
        """Start the agent frame streamer if spokes are connected."""
        try:
            from services.agent_frame_streamer import get_frame_streamer

            streamer = get_frame_streamer()
            if streamer is None:
                return
            started = streamer.start_streaming(user_id=self._broadcast_user_id)
            self._frame_streamer_active = bool(started)
            if started:
                logger.debug("Frame streaming started for agentic overlay")
        except Exception:
            logger.exception("Failed to start frame streaming")

    def _stop_frame_streaming(self) -> None:
        """Stop the agent frame streamer."""
        try:
            from services.agent_frame_streamer import get_frame_streamer

            streamer = get_frame_streamer()
            if streamer is not None:
                # Pass the scoped user_id so a cloud streamer can tear down the
                # right per-user session. The desktop AgentFrameStreamer takes
                # no user_id arg -- guard with a TypeError fallback so both
                # streamers work unchanged.
                try:
                    streamer.stop_streaming(user_id=self._broadcast_user_id)
                except TypeError:
                    streamer.stop_streaming()
            self._frame_streamer_active = False
            logger.debug("Frame streaming stopped")
        except Exception:
            logger.exception("Failed to stop frame streaming")

    def _broadcast(self) -> None:
        """Send overlay state to React via WebSocket."""
        if self._broadcast_fn is None:
            return
        try:
            payload: dict[str, Any] = {
                "type": "browser_overlay_state",
                "visible": self._state != OverlayState.HIDDEN,
                "mode": self._state.value,
                "url": self._current_url,
                "agent_busy": self._state == OverlayState.AGENTIC,
                "browser_activity": {
                    "active": self._state == OverlayState.AGENTIC,
                    "recently_active": False,
                    "mode": self._state.value,
                    "url": self._current_url,
                    "description": self._agent_task_description,
                    "status": self._agent_task_status,
                    "phase": self._agent_phase,
                    "outcome": "",
                },
            }
            # Include agent task info when in agentic state
            if self._state == OverlayState.AGENTIC:
                payload["agent_task"] = {
                    "description": self._agent_task_description,
                    "status": self._agent_task_status,
                    "phase": self._agent_phase,
                }
            # Include outcome when hiding (for completion toasts)
            outcome = getattr(self, "_hide_outcome", "")
            if self._state == OverlayState.HIDDEN and outcome:
                payload["agent_outcome"] = outcome
                payload["browser_activity"]["recently_active"] = True
                payload["browser_activity"]["outcome"] = outcome
                self._hide_outcome = ""
            if self._broadcast_user_id:
                try:
                    self._broadcast_fn(payload, user_id=self._broadcast_user_id)
                    return
                except TypeError:
                    # Test shims and older call sites accepted only the
                    # payload argument. Real EventHub wiring supplies user_id.
                    pass
            self._broadcast_fn(payload)
        except Exception:
            logger.exception("broadcast_fn failed")
