"""
music.providers.browser.provider
---------------------------------

BrowserPlaybackController -- core engine for browser-native music playback.

Controls a QWebEngineView to play music from any web service by navigating
to URLs, injecting JavaScript, and reading playback state via the W3C
MediaSession API (with multi-level fallbacks).

This is a *playback controller*, not a MusicProvider.  It does not handle
authentication, search, or playlist management -- those are the
responsibility of higher-level site recipes.

Thread Safety
~~~~~~~~~~~~~
QWebEngineView operations (``runJavaScript``, ``load``) must happen on the
Qt main thread.  This controller dispatches all cross-thread calls via
``QCoreApplication.postEvent()`` (custom QEvent), NOT ``QTimer.singleShot``
and NOT signal/slot with ``moveToThread``.

``QTimer.singleShot(0, fn)`` from a non-Qt worker thread creates the timer
on the calling thread's event loop.  Worker threads (e.g. ``MusicWorker``)
have no Qt event loop, so the timer **never fires**.  The signal/slot bridge
uses ``QueuedConnection`` (automatic for cross-thread signals) which
reliably posts to the main thread's event loop via Qt's internal mechanism.

``loadFinished`` signal handler chains post-navigation JS injection and
metadata polling on the main thread.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from core.constants import TIMEOUT_SHUTDOWN
from core.exceptions import ErrorContext, PlaybackError, PlaybackOperationError
from core.logging_config import get_logger

from .js_bridge import (
    js_call_action,
    js_check_track_ended,
    js_get_media_elements,
    js_get_media_session_metadata,
    js_get_page_metadata,
    js_set_volume,
    js_setup_track_end_listener,
)
from .media_session import MediaSessionReader, TrackMetadata

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Cross-thread dispatch to Qt main thread
# ---------------------------------------------------------------------------
# QTimer.singleShot does NOT work from non-Qt threads because the timer is
# created on the calling thread's event loop.  Worker threads (MusicWorker,
# audio-duck-up, etc.) have no Qt event loop, so the timer never fires.
#
# Signal + moveToThread also fails: the signal connection type is fixed
# at connect-time (DirectConnection when sender==receiver thread), and the
# queued events are silently dropped.
#
# We use QCoreApplication.postEvent() — the most primitive and reliable Qt
# cross-thread mechanism.  A _QtMainThreadReceiver QObject lives on the
# main thread and receives _InvokeEvent custom events carrying callables.
# ---------------------------------------------------------------------------
_HAS_QT = False

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QObject

    class _InvokeEvent(QEvent):
        """Custom QEvent carrying a callable for cross-thread dispatch."""

        _EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

        def __init__(self, fn: object) -> None:
            super().__init__(self._EVENT_TYPE)
            self.fn = fn

    class _QtMainThreadReceiver(QObject):
        """Receives _InvokeEvent and executes the callable.

        This QObject must live on the Qt main thread.  Cross-thread dispatch
        uses ``QCoreApplication.postEvent()`` which is thread-safe and
        delivers to the receiver's thread event loop.
        """

        def event(self, event: QEvent) -> bool:
            if isinstance(event, _InvokeEvent):
                try:
                    event.fn()  # type: ignore[operator]
                except Exception:
                    logger.exception("Qt main-thread event execution failed")
                return True
            return super().event(event)

    _HAS_QT = True
except ImportError:
    _HAS_QT = False

# Default metadata polling interval in milliseconds
_DEFAULT_POLL_INTERVAL_MS = 1000

# Maximum URL length logged (truncation for readability)
_MAX_LOG_URL_LEN = 120


class BrowserPlaybackController:
    """Controls QWebEngineView for browser-native music playback.

    Responsibilities:

    1. Navigate QWebEngineView to URLs (music service pages)
    2. Inject JS to interact with pages (play, pause, skip, seek)
    3. Read metadata via W3C MediaSession API with fallback chain
    4. Monitor playback state and track-end events
    5. Broadcast metadata changes via registered callbacks

    The controller is designed to be attached to a QWebEngineView after
    construction.  All methods are safe to call when no webview is
    attached -- they degrade gracefully with log warnings.

    Example::

        controller = BrowserPlaybackController()
        controller.set_webview_controller(my_webview)
        controller.navigate("https://music.youtube.com")
        controller.start_metadata_polling()
        controller.play()
    """

    def __init__(self, webview_controller: Any | None = None) -> None:
        """Initialize the browser playback controller.

        Args:
            webview_controller: Optional QWebEngineView (or compatible object
                exposing ``page().runJavaScript()``).  Can also be set later
                via :meth:`set_webview_controller`.
        """
        self._webview_controller: Any | None = webview_controller
        self._metadata_reader = MediaSessionReader()

        # Playback state (local cache -- authoritative state is in the page)
        self._is_playing: bool = False
        self._is_paused: bool = False
        self._volume: int = 80
        self._current_url: str | None = None

        # Polling machinery
        self._poll_timer: Any | None = None  # QTimer when running
        self._polling_active: bool = False
        self._poll_interval_ms: int = _DEFAULT_POLL_INTERVAL_MS

        # Track-end callback
        self._track_end_callbacks: list[Callable[[], None]] = []
        self._track_end_listener_installed: bool = False

        # Metadata change callback
        self._metadata_callbacks: list[Callable[[TrackMetadata], None]] = []

        # Thread safety
        self._lock = threading.Lock()

        # Cross-thread dispatch bridge (lazy, moved to main thread on first use)
        self._qt_bridge: Any | None = None

        # Pending recipe JS to inject after loadFinished fires
        self._pending_inject_js: str | None = None
        self._pending_inject_callback: Callable[[Any], None] | None = None

        logger.info("BrowserPlaybackController initialized")

    # ------------------------------------------------------------------
    # Webview lifecycle
    # ------------------------------------------------------------------

    def set_webview_controller(self, controller: Any) -> None:
        """Set or replace the QWebEngineView controller.

        If playback was active and a URL was loaded, the new controller
        will be navigated to the current URL.

        Args:
            controller: QWebEngineView or compatible webview object.
        """
        old = self._webview_controller
        self._webview_controller = controller
        logger.info(
            "BPC webview controller %s",
            "replaced" if old is not None else "attached",
        )

        # If we already had a URL loaded, resume on the new controller
        if self._current_url and controller is not None:
            self._dispatch_navigate(self._current_url)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, url: str) -> None:
        """Navigate QWebEngineView to a URL.

        Args:
            url: The URL to load.

        Raises:
            PlaybackError: If no webview controller is attached or
                navigation fails after retries.
        """
        if not url:
            logger.warning("BPC navigate called with empty URL")
            return

        # Reset state for new navigation
        self._track_end_listener_installed = False
        self._metadata_reader.clear_cache()
        self._is_playing = False
        self._is_paused = False

        self._navigate_with_retry(url)
        self._current_url = url
        logger.info("BPC navigating to %s", url[:_MAX_LOG_URL_LEN])

    def _dispatch_navigate(self, url: str) -> None:
        """Load a URL into the webview controller.

        Navigation is marshalled to the Qt main thread because
        QWebEngineView methods must only be called from the GUI thread.

        Raises:
            PlaybackError: If no webview controller is attached.
        """
        ctrl = self._webview_controller
        if ctrl is None:
            logger.error(
                "BPC no webview controller attached — cannot navigate to %s",
                url[:_MAX_LOG_URL_LEN],
            )
            raise PlaybackError(
                "No webview controller attached for browser playback",
                context=ErrorContext(
                    component="playback.browser",
                    operation="dispatch_navigate",
                    params={"url": url},
                    user_message="Browser playback is not available. The webview was not initialized.",
                    recovery_hint="Restart Viola to re-initialize the browser webview.",
                ),
            )

        current_thread = threading.current_thread().name
        is_main = current_thread == "MainThread"

        if is_main:
            self._navigate_on_main(ctrl, url)
        else:
            # Marshal to Qt main thread via signal bridge.
            # QTimer.singleShot does NOT work from worker threads (no event loop).
            logger.info(
                "BPC dispatching navigate to main thread (from %s) url=%s",
                current_thread,
                url[:_MAX_LOG_URL_LEN],
            )
            if not self._post_to_main(lambda: self._navigate_on_main(ctrl, url)):
                logger.warning(
                    "BPC Qt bridge unavailable — calling navigate directly from %s",
                    current_thread,
                )
                self._navigate_on_main(ctrl, url)

    def _load_url_on_ctrl(self, ctrl: Any, url: str) -> None:
        """Actually load the URL on the webview controller (must run on main thread)."""
        try:
            if hasattr(ctrl, "setUrl"):
                try:
                    from PySide6.QtCore import QUrl

                    ctrl.setUrl(QUrl(url))
                except ImportError:
                    logger.warning("BPC PyQt6 not available for setUrl")
            elif hasattr(ctrl, "load"):
                try:
                    from PySide6.QtCore import QUrl

                    ctrl.load(QUrl(url))
                except ImportError:
                    logger.warning("BPC PyQt6 not available for load")
            elif hasattr(ctrl, "load_url"):
                ctrl.load_url(url)
            elif hasattr(ctrl, "set_url"):
                ctrl.set_url(url)
            else:
                logger.warning("BPC controller has no recognised load method")
        except Exception:
            logger.exception("BPC navigation failed for %s", url[:_MAX_LOG_URL_LEN])

    def _navigate_on_main(self, ctrl: Any, url: str) -> None:
        """Set up loadFinished signal and navigate. Must run on Qt main thread.

        Ensures the QWebEngineView is visible and has non-zero dimensions
        before loading — Chromium skips navigation on hidden/zero-size widgets.
        """
        self._ensure_webview_loadable(ctrl)
        self._connect_load_finished(ctrl)
        self._load_url_on_ctrl(ctrl, url)

    @staticmethod
    def _ensure_webview_loadable(ctrl: Any) -> None:
        """Ensure a QWebEngineView is visible and has non-zero dimensions.

        Chromium's rendering pipeline does not process ``page().load()`` on
        widgets that are hidden or have zero size.  This method shows the
        widget at 2x2 minimum, positioned off-screen at (-100, -100) so it
        is functional but invisible to the user.

        The hidden browser is a search engine — it is NEVER shown to the
        user during music playback.  Only the login flow (via the overlay
        controller) should make it visible.

        No-op if the controller is not a QWidget or is already visible with
        non-zero size.
        """
        if not hasattr(ctrl, "isVisible"):
            return  # Not a QWidget (e.g. mock in tests)

        try:
            needs_show = not ctrl.isVisible()
            sz = ctrl.size()
            needs_resize = sz.width() < 2 or sz.height() < 2

            if needs_show or needs_resize:
                if needs_resize:
                    # Use 2x2 (not 1x1) — some Chromium builds skip GPU init
                    # for true 1x1 widgets.
                    ctrl.resize(2, 2)
                # Position off-screen so it's functional but invisible
                ctrl.move(-100, -100)
                if needs_show:
                    ctrl.show()
                logger.debug(
                    "BPC ensured webview loadable (showed=%s, resized=%s, off-screen)",
                    needs_show,
                    needs_resize,
                )
        except Exception:
            logger.exception("BPC failed to ensure webview loadable")

    def _post_to_main(self, fn: Callable[[], None]) -> bool:
        """Post *fn* to execute on the Qt main thread via postEvent.

        Uses ``QCoreApplication.postEvent()`` with a custom QEvent.  This is
        the most primitive and reliable Qt cross-thread mechanism:
        - Thread-safe by Qt guarantee
        - No signal/slot connection quirks
        - No moveToThread required
        - Events delivered when the main thread processes its event loop

        Returns ``True`` if the event was posted, ``False`` if no Qt
        environment is available.
        """
        if not _HAS_QT:
            return False

        # Lazily create a receiver QObject on the main thread.
        # The receiver must live on the main thread so postEvent delivers
        # events there.
        receiver = self._qt_bridge  # reuse attribute name
        if receiver is None:
            try:
                app = QCoreApplication.instance()
                if app is None:
                    logger.warning("BPC postEvent: no QCoreApplication instance")
                    return False
                # Create receiver and move to main thread
                receiver = _QtMainThreadReceiver()
                receiver.moveToThread(app.thread())
                self._qt_bridge = receiver
                logger.debug(
                    "BPC postEvent receiver created on %s, moved to main thread",
                    threading.current_thread().name,
                )
            except Exception:
                logger.exception("BPC failed to create postEvent receiver")
                return False

        try:
            QCoreApplication.postEvent(receiver, _InvokeEvent(fn))
            return True
        except Exception:
            logger.exception("BPC postEvent failed")
            return False

    def _connect_load_finished(self, ctrl: Any) -> None:
        """Connect ``loadFinished`` signal to ``_on_page_loaded``.

        Must be called on the Qt main thread.  Disconnects any previous
        connection first to avoid duplicate fires on successive navigations.
        """
        page = self._get_page(ctrl)
        if page is None:
            return
        try:
            # Disconnect previous connection to avoid duplicate fires
            try:
                page.loadFinished.disconnect(self._on_page_loaded)
            except (TypeError, RuntimeError):
                pass  # Not previously connected
            page.loadFinished.connect(self._on_page_loaded)
            logger.debug("BPC connected loadFinished signal")
        except Exception:
            logger.exception("BPC failed to connect loadFinished signal")

    def _on_page_loaded(self, ok: bool) -> None:
        """Handle page load completion.  Runs on Qt main thread (signal handler).

        This is the key integration point: ``loadFinished`` fires on the Qt
        main thread, so all downstream operations (JS injection, QTimer
        creation for metadata polling) naturally stay on the main thread
        without any cross-thread marshalling.
        """
        if not ok:
            logger.warning("BPC page load failed (ok=False)")
            return

        logger.info(
            "BPC page loaded OK, url=%s",
            (self._current_url or "unknown")[:_MAX_LOG_URL_LEN],
        )

        # Inject pending recipe JS with delay for SPA dynamic rendering.
        # YouTube renders search results asynchronously after the initial
        # page shell loads, so we wait 2 s before injecting.
        pending_js = self._pending_inject_js
        pending_cb = self._pending_inject_callback
        if pending_js:
            self._pending_inject_js = None
            self._pending_inject_callback = None
            try:
                from PySide6.QtCore import QTimer

                logger.info("BPC scheduling recipe JS injection in 2000 ms (post-load)")
                QTimer.singleShot(
                    2000,
                    lambda: self._execute_js_on_main(pending_js, callback=pending_cb),
                )
            except ImportError:
                logger.warning("BPC PyQt6 unavailable for post-load injection")

        # Start metadata polling if not already active — QTimer is created
        # on the main thread since this handler runs on the main thread.
        if not self._polling_active:
            self._start_metadata_polling_main_thread()

    def _execute_js_on_main(
        self,
        js_code: str,
        *,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Execute JS directly.  Must be called on the Qt main thread.

        Used by ``_on_page_loaded`` and QTimer callbacks that are already
        guaranteed to run on the main thread.

        Args:
            js_code: JavaScript to execute.
            callback: Optional callback receiving the JS return value.
        """
        ctrl = self._webview_controller
        if ctrl is None:
            logger.debug("BPC _execute_js_on_main: no webview controller")
            return
        page = self._get_page(ctrl)
        if page is None:
            logger.debug("BPC _execute_js_on_main: no page")
            return
        try:
            if callback is not None:
                page.runJavaScript(js_code, 0, callback)
            else:
                page.runJavaScript(js_code)
            logger.info(
                "BPC recipe JS injected (%d chars, callback=%s)",
                len(js_code),
                callback is not None,
            )
        except Exception:
            logger.exception("BPC recipe JS injection failed")

    def _start_metadata_polling_main_thread(self) -> None:
        """Create and start the metadata polling QTimer.

        Must be called on the Qt main thread.  Safe to call multiple times —
        returns immediately if a timer is already running.
        """
        if self._poll_timer is not None:
            return  # Already running

        self._polling_active = True
        interval = max(100, self._poll_interval_ms)

        try:
            from PySide6.QtCore import QTimer

            timer = QTimer()
            timer.setInterval(interval)
            timer.timeout.connect(self._poll_metadata_qt)
            timer.start()
            self._poll_timer = timer
            logger.info(
                "BPC metadata polling started (QTimer on main thread, %d ms)",
                interval,
            )
        except ImportError:
            self._schedule_thread_poll()
            logger.info(
                "BPC metadata polling started (thread timer fallback, %d ms)",
                interval,
            )

    def _navigate_with_retry(
        self,
        url: str,
        max_attempts: int = 2,
        retry_delay: float = TIMEOUT_SHUTDOWN,
    ) -> None:
        """Navigate to a URL with retry logic for transient failures.

        Attempts navigation up to ``max_attempts`` times, waiting
        ``retry_delay`` seconds between retries.  Only retries when
        ``_dispatch_navigate`` raises an exception (timeout or network
        error).  A page that loads successfully (even with an HTTP error
        status) is not retried.

        Args:
            url: The URL to load.
            max_attempts: Maximum number of navigation attempts (default 2).
            retry_delay: Seconds to wait between retries (default 2.0).
        """
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._dispatch_navigate(url)
                if attempt > 1:
                    logger.info(
                        "BPC navigation succeeded on attempt %d for %s",
                        attempt,
                        url[:_MAX_LOG_URL_LEN],
                    )
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "BPC navigation attempt %d/%d failed for %s: %s",
                    attempt,
                    max_attempts,
                    url[:_MAX_LOG_URL_LEN],
                    exc,
                )

                if attempt < max_attempts:
                    logger.info("BPC retrying navigation in %.1f s", retry_delay)
                    time.sleep(retry_delay)

        # All attempts exhausted
        logger.error(
            "BPC navigation failed after %d attempts for %s",
            max_attempts,
            url[:_MAX_LOG_URL_LEN],
        )
        if last_error is not None:
            raise PlaybackError(
                "Navigation failed after %d attempts" % max_attempts,
                context=ErrorContext(
                    component="playback.browser",
                    operation="navigate",
                    params={"url": url, "attempts": max_attempts},
                    user_message="Could not load the music page. Please try again.",
                    recovery_hint="Check your internet connection.",
                ),
                cause=last_error,
            )

    # ------------------------------------------------------------------
    # JavaScript injection
    # ------------------------------------------------------------------

    def inject_js(self, code: str, callback: Callable[[Any], None] | None = None) -> None:
        """Inject JavaScript into the current page.

        Handles thread safety by marshaling the call to the Qt main thread
        when invoked from a worker thread.

        Args:
            code: JavaScript code string to execute.
            callback: Optional callback receiving the JS return value.
                Only works when called from the Qt main thread (or when
                using a callback-aware dispatch path).
        """
        ctrl = self._webview_controller
        if ctrl is None:
            logger.debug("BPC inject_js skipped: no webview controller")
            return

        current_thread = threading.current_thread().name
        is_main = current_thread == "MainThread"

        try:
            page = self._get_page(ctrl)
            if page is None:
                logger.debug("BPC inject_js skipped: no page object")
                return

            if is_main:
                # Direct call on main thread -- can use callback
                if callback is not None:
                    page.runJavaScript(code, callback)
                else:
                    page.runJavaScript(code)
            else:
                # Marshal to Qt main thread via QTimer.singleShot
                self._run_js_on_main_thread(code, callback)
        except Exception:
            logger.exception(
                "BPC inject_js failed (thread=%s, js=%s)",
                current_thread,
                code[:60],
            )

    def _run_js_on_main_thread(
        self,
        js_code: str,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Execute JavaScript on the Qt main thread via postEvent.

        Safe to call from any thread.  Uses :meth:`_post_to_main` which
        dispatches via ``QCoreApplication.postEvent()``.

        Args:
            js_code: JavaScript code to execute.
            callback: Optional callback receiving the JS return value.
        """
        ctrl = self._webview_controller
        if ctrl is None:
            logger.debug("BPC _run_js_on_main_thread: no webview controller")
            return

        def _execute() -> None:
            page = self._get_page(ctrl)
            if page is None:
                return
            try:
                if callback is not None:
                    page.runJavaScript(js_code, 0, callback)
                else:
                    page.runJavaScript(js_code)
            except Exception:
                logger.exception(
                    "BPC JS execution failed on main thread (js=%s)",
                    js_code[:60],
                )

        if not self._post_to_main(_execute):
            # No bridge — try direct call (unsafe from worker threads but
            # last resort for headless/test environments).
            logger.debug(
                "BPC JS direct call (no bridge, thread=%s)",
                threading.current_thread().name,
            )
            _execute()

    @staticmethod
    def _get_page(ctrl: Any) -> Any | None:
        """Extract the QWebEnginePage from a controller, or return None."""
        if hasattr(ctrl, "page") and callable(ctrl.page):
            try:
                return ctrl.page()
            except Exception:
                return None
        return ctrl

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def play(self) -> None:
        """Send play command via MediaSession / media element.

        Sets ``_is_playing`` only if a webview controller is attached.
        The authoritative state comes from metadata polling; this local
        flag is a fallback for when polling hasn't started yet.
        """
        if self._webview_controller is None:
            logger.warning("BPC play called but no webview controller attached")
            return
        self.inject_js(js_call_action("play"))
        self._is_playing = True
        self._is_paused = False
        logger.debug("BPC play")

        # Install track-end listener on first play
        if not self._track_end_listener_installed:
            self.inject_js(js_setup_track_end_listener())
            self._track_end_listener_installed = True

    def pause(self) -> None:
        """Send pause command via MediaSession / media element."""
        self.inject_js(js_call_action("pause"))
        self._is_playing = False
        self._is_paused = True
        logger.debug("BPC pause")

    def stop(self) -> None:
        """Stop playback (pause + rewind to start)."""
        self.inject_js(js_call_action("stop"))
        self._is_playing = False
        self._is_paused = False
        logger.debug("BPC stop")

    def resume(self) -> None:
        """Resume playback after pause."""
        self.play()
        logger.debug("BPC resume")

    def next_track(self) -> None:
        """Send nexttrack command via MediaSession / UI button click."""
        self.inject_js(js_call_action("nexttrack"))
        logger.debug("BPC next_track")

    def previous_track(self) -> None:
        """Send previoustrack command via MediaSession / UI button click."""
        self.inject_js(js_call_action("previoustrack"))
        logger.debug("BPC previous_track")

    def seek(self, position_seconds: float) -> None:
        """Seek to a specific position.

        Args:
            position_seconds: Target position in seconds.
        """
        self.inject_js(js_call_action("seekto", {"seekTime": float(position_seconds)}))
        logger.debug("BPC seek to %.1f s", position_seconds)

    def set_volume(self, level: int) -> int:
        """Set volume on all media elements in the page.

        Args:
            level: Volume level 0-100.

        Returns:
            The clamped volume level that was applied.
        """
        clamped = max(0, min(100, int(level)))
        self._volume = clamped
        self.inject_js(js_set_volume(clamped))
        logger.debug("BPC set_volume %d", clamped)
        return clamped

    # ------------------------------------------------------------------
    # Metadata reading
    # ------------------------------------------------------------------

    def get_metadata(self) -> dict[str, Any] | None:
        """Get the last cached metadata dictionary.

        Returns ``None`` if no metadata has been collected yet.
        For real-time metadata, use :meth:`start_metadata_polling`.
        """
        cached = self._metadata_reader.get_cached()
        if cached is None:
            return None
        return cached.to_dict()

    def get_playback_state(self) -> str:
        """Get current playback state: ``'playing'``, ``'paused'``, or ``'none'``.

        Returns the cached state.  For real-time accuracy, ensure metadata
        polling is active.
        """
        cached = self._metadata_reader.get_cached()
        if cached is not None:
            return cached.playback_state

        # Fall back to local state tracking
        if self._is_playing:
            return "playing"
        if self._is_paused:
            return "paused"
        return "none"

    def is_playing(self) -> bool:
        """Check if currently playing.

        Uses cached metadata state with local state as fallback.
        """
        state = self.get_playback_state()
        return state == "playing"

    # ------------------------------------------------------------------
    # Metadata polling
    # ------------------------------------------------------------------

    def start_metadata_polling(self, interval_ms: int = _DEFAULT_POLL_INTERVAL_MS) -> None:
        """Start periodic polling for metadata changes.

        Dispatches QTimer creation to the Qt main thread because a QTimer
        created on a worker thread with no event loop will never fire.
        Falls back to a daemon thread timer when Qt is not available.

        Args:
            interval_ms: Polling interval in milliseconds.
        """
        if self._polling_active:
            logger.debug("BPC metadata polling already active")
            return

        self._poll_interval_ms = max(100, interval_ms)  # Floor at 100ms
        self._polling_active = True  # Set early to prevent duplicate calls

        # Dispatch QTimer creation to the Qt main thread via signal bridge.
        if not self._post_to_main(self._start_metadata_polling_main_thread):
            # No bridge — use a threading.Timer loop as fallback
            self._schedule_thread_poll()
            logger.info(
                "BPC metadata polling started (thread timer, %d ms)",
                self._poll_interval_ms,
            )

    def stop_metadata_polling(self) -> None:
        """Stop metadata polling."""
        self._polling_active = False

        if self._poll_timer is not None:
            try:
                self._poll_timer.stop()
            except Exception:
                pass  # Timer may already be dead
            self._poll_timer = None

        logger.info("BPC metadata polling stopped")

    def _schedule_thread_poll(self) -> None:
        """Schedule the next poll using a threading.Timer (non-Qt fallback)."""
        if not self._polling_active:
            return

        interval_s = self._poll_interval_ms / 1000.0
        t = threading.Timer(interval_s, self._poll_metadata_thread)
        t.daemon = True
        t.name = "BPC-MetadataPoll"
        t.start()
        self._poll_timer = t

    def _poll_metadata_thread(self) -> None:
        """Thread-based polling callback.  Fires JS and reschedules."""
        if not self._polling_active:
            return

        try:
            self._request_metadata_from_page()
        except Exception:
            logger.exception("BPC metadata poll error")
        finally:
            self._schedule_thread_poll()

    def _poll_metadata_qt(self) -> None:
        """QTimer-based polling callback.  Runs on Qt main thread."""
        if not self._polling_active:
            return

        try:
            self._request_metadata_from_page_qt()
        except Exception:
            logger.exception("BPC metadata poll error (Qt)")

    def _request_metadata_from_page(self) -> None:
        """Fire-and-forget metadata request (no callback -- for thread context)."""
        self.inject_js(js_get_media_session_metadata())
        self.inject_js(js_get_media_elements())
        self.inject_js(js_get_page_metadata())

        # Also check track-end flag
        if self._track_end_listener_installed:
            self.inject_js(js_check_track_ended())

    def _request_metadata_from_page_qt(self) -> None:
        """Metadata request with callbacks (Qt main thread only)."""
        ctrl = self._webview_controller
        if ctrl is None:
            return

        page = self._get_page(ctrl)
        if page is None:
            return

        # Collect results via chained callbacks
        results: dict[str, Any] = {}

        def on_media_session(val: Any) -> None:
            results["media_session"] = val
            # Chain: next get media elements
            page.runJavaScript(js_get_media_elements(), on_media_elements)

        def on_media_elements(val: Any) -> None:
            results["media_elements"] = val
            # Chain: next get page metadata
            page.runJavaScript(js_get_page_metadata(), on_page_meta)

        def on_page_meta(val: Any) -> None:
            results["page_meta"] = val
            self._process_metadata_results(results)

            # Check track-end flag
            if self._track_end_listener_installed:
                page.runJavaScript(js_check_track_ended(), on_track_end_check)

        def on_track_end_check(val: Any) -> None:
            if val is True:
                self._fire_track_end_callbacks()

        # Start the chain
        page.runJavaScript(js_get_media_session_metadata(), on_media_session)

    def _process_metadata_results(self, results: dict[str, Any]) -> None:
        """Process collected JS results into metadata and notify callbacks."""
        metadata = self._metadata_reader.parse_metadata_response(
            media_session_result=results.get("media_session"),
            media_elements_result=results.get("media_elements"),
            page_metadata_result=results.get("page_meta"),
        )

        # Update local playing state from metadata
        if metadata.playback_state == "playing":
            self._is_playing = True
            self._is_paused = False
        elif metadata.playback_state == "paused":
            self._is_playing = False
            self._is_paused = True

        # Notify metadata change callbacks
        new_dict = metadata.to_dict()
        if self._metadata_reader.has_changed(new_dict):
            logger.debug(
                "BPC metadata changed: title=%s artist=%s state=%s source=%s",
                metadata.title,
                metadata.artist,
                metadata.playback_state,
                metadata.source,
            )
            for cb in self._metadata_callbacks:
                try:
                    cb(metadata)
                except Exception:
                    logger.exception("BPC metadata callback error")

    # ------------------------------------------------------------------
    # Track-end detection
    # ------------------------------------------------------------------

    def on_track_end(self, callback: Callable[[], None]) -> None:
        """Register a callback for track-end detection.

        The callback is invoked (with no arguments) when the currently
        playing media element fires its ``ended`` event.

        Args:
            callback: Zero-argument callable.
        """
        self._track_end_callbacks.append(callback)
        logger.debug(
            "BPC track-end callback registered (total=%d)",
            len(self._track_end_callbacks),
        )

    def _fire_track_end_callbacks(self) -> None:
        """Invoke all registered track-end callbacks."""
        logger.info("BPC track ended, firing %d callbacks", len(self._track_end_callbacks))
        self._is_playing = False
        self._is_paused = False

        for cb in self._track_end_callbacks:
            try:
                cb()
            except Exception:
                logger.exception("BPC track-end callback error")

    # ------------------------------------------------------------------
    # Metadata change subscription
    # ------------------------------------------------------------------

    def on_metadata_change(self, callback: Callable[[TrackMetadata], None]) -> None:
        """Register a callback for metadata changes.

        The callback receives a :class:`TrackMetadata` instance whenever
        a meaningful change is detected (title, artist, album, or state).

        Args:
            callback: Callable accepting a single :class:`TrackMetadata` arg.
        """
        self._metadata_callbacks.append(callback)
        logger.debug(
            "BPC metadata callback registered (total=%d)",
            len(self._metadata_callbacks),
        )

    # ------------------------------------------------------------------
    # Recipe-driven search & play
    # ------------------------------------------------------------------

    def search_and_play(self, query: str, recipe: Any) -> None:
        """Navigate to a search URL and inject recipe JS to play the first result.

        This is a convenience method for recipe-driven playback.  The
        ``recipe`` object should expose:

        - ``search_url(query: str) -> str``: Build the search URL.
        - ``play_first_result_js() -> str``: JS code to click/play the
          first search result.

        Args:
            query: Search query string.
            recipe: Site-specific recipe object.

        Raises:
            PlaybackOperationError: If the recipe does not expose the
                required methods.
        """
        if not hasattr(recipe, "search_url") or not hasattr(recipe, "play_first_result_js"):
            raise PlaybackOperationError(
                "search_and_play",
                "browser",
                "Recipe must expose search_url() and play_first_result_js()",
            )

        url = recipe.search_url(query)
        logger.info("BPC search_and_play query=%s url=%s", query, url[:_MAX_LOG_URL_LEN])
        self.navigate(url)

        # The JS injection happens after page load.  We inject with a
        # small delay to give the page time to render search results.
        # This uses a one-shot timer approach.
        play_js = recipe.play_first_result_js()
        self._inject_after_load(play_js)

    def _inject_after_load(
        self,
        js_code: str,
        delay_ms: int = 2000,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """Schedule JS injection after page load.

        Stores the JS code for injection by :meth:`_on_page_loaded` when the
        ``loadFinished`` signal fires on the Qt main thread.  A safety
        fallback timer ensures injection even if the signal doesn't fire.

        Args:
            js_code: JavaScript to inject.
            delay_ms: Delay in milliseconds before injection (SPA rendering
                time after loadFinished, and fallback timeout base).
            callback: Optional callback receiving the JS return value.
                Used by the search-only engine to receive extracted video IDs.
        """
        # Primary path: store for loadFinished handler
        self._pending_inject_js = js_code
        self._pending_inject_callback = callback
        logger.info(
            "BPC pending recipe JS stored (%d chars, callback=%s)",
            len(js_code),
            callback is not None,
        )

        # Safety fallback: if loadFinished doesn't fire (e.g. single-page
        # app navigation that doesn't trigger a full page load), inject
        # after delay_ms + 3 s via main-thread dispatch.
        fallback_ms = delay_ms + 3000

        def _fallback_inject() -> None:
            if self._pending_inject_js is not None:
                logger.warning(
                    "BPC loadFinished did not fire — fallback inject after %d ms",
                    fallback_ms,
                )
                js = self._pending_inject_js
                cb = self._pending_inject_callback
                self._pending_inject_js = None
                self._pending_inject_callback = None
                self._execute_js_on_main(js, callback=cb)
                if not self._polling_active:
                    self._start_metadata_polling_main_thread()

        def _setup_fallback_timer() -> None:
            """Create fallback QTimer on the main thread."""
            try:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(fallback_ms, _fallback_inject)
            except ImportError:
                pass  # Covered by threading.Timer below

        # Try to dispatch the fallback setup to the main thread
        if not self._post_to_main(_setup_fallback_timer):
            # No Qt bridge — use a threading.Timer
            fallback_s = fallback_ms / 1000.0
            t = threading.Timer(
                fallback_s,
                lambda: self._run_js_on_main_thread(js_code, callback),
            )
            t.daemon = True
            t.name = "BPC-InjectFallback"
            t.start()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Stop polling and release resources."""
        self.stop_metadata_polling()
        self._track_end_callbacks.clear()
        self._metadata_callbacks.clear()
        self._metadata_reader.clear_cache()
        self._webview_controller = None
        self._current_url = None
        self._is_playing = False
        self._is_paused = False
        logger.info("BPC cleanup complete")


__all__ = [
    "BrowserPlaybackController",
]
