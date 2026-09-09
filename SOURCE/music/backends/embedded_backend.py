"""
music/backends/embedded_backend.py

Embedded player backend using Qt WebEngineView for YouTube and other embedded content.
Supports headless mode and visible UI integration.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from config import settings
from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger
from music.backends.base import BackendCapabilities, BaseBackend
from music.exceptions import BackendError
from music.youtube_embed import DEFAULT_EMBED_ORIGIN

from .embedded_backend_html import EmbeddedBackendHTMLGenerator
from .embedded_backend_js import EmbeddedBackendJSCommunicator
from .embedded_backend_progress import EmbeddedBackendProgressManager
from .playback_controller import PlaybackController
from .webview_controller import WebViewController

# Qt availability flag - visible to mypy
QT_AVAILABLE = False

if TYPE_CHECKING:
    # Type stubs for PyQt6 - used only for type checking
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
else:
    # Runtime imports with fallback stubs
    try:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        QT_AVAILABLE = True
    except ImportError:  # pragma: no cover - non-Qt environments
        # Runtime stubs for when Qt is not available
        class QObject:
            """Minimal QObject stub when Qt is not available."""

            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__()

        class _DummySignal:
            def connect(self, *_: object, **__: object) -> None:
                return None

            def emit(self, *_: object, **__: object) -> None:
                return None

        def Signal(*_: object, **__: object) -> _DummySignal:
            return _DummySignal()

        class QWebEngineSettings:
            class WebAttribute:  # minimal placeholder
                JavascriptEnabled = 0
                PlaybackRequiresUserGesture = 1
                LocalContentCanAccessRemoteUrls = 2

        class QWebEngineView:
            pass


# WebEngine capability detection
try:
    from ui.qt_native.webengine_capability import is_available as webengine_is_available
except ImportError:

    def webengine_is_available() -> bool:
        return QT_AVAILABLE


class EmbeddedPlayerSignals(QObject):
    """Qt signals for embedded player events."""

    playback_started = Signal()
    playback_paused = Signal()
    playback_resumed = Signal()
    playback_finished = Signal()
    playback_error = Signal(str)


class EmbeddedPlayerBackend(BaseBackend):
    """Qt WebEngineView backend for embedded players."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        video_widget: object | None = None,
        headless: bool = True,
    ) -> None:
        super().__init__()
        self._logger = logger or get_logger("viola.backend.embedded")
        self._video_widget = video_widget
        self._headless = headless

        # Test mode detection
        self._test_mode = settings.test_mode
        e2e_mode = settings.embedded_only

        # Check WebEngine availability
        self._qt_available = QT_AVAILABLE and webengine_is_available()

        if e2e_mode and not self._qt_available:
            self._logger.warning("EMBEDDED_BACKEND: E2E mode requires WebEngine but it's unavailable. Tests may fail.")

        self._webview: QWebEngineView | None = None
        self._current_url: str | None = None
        self._video_id: str | None = None
        self._is_playing = False
        self._is_paused = False
        self._pending_start = False
        self._volume = 50
        self._position_ms = 0
        self._duration_ms: int | None = None
        self._progress_manager = EmbeddedBackendProgressManager(self)
        self._progress_thread: threading.Thread | None = None
        self._progress_stop: threading.Event | None = None

        if self._qt_available:
            self._signals = EmbeddedPlayerSignals()
        else:
            self._signals = None

        if not self._qt_available:
            if self._test_mode:
                self._logger.info("EMBEDDED_BACKEND: WebEngine not available, running in test-mode simulation")
            else:
                self._logger.warning(
                    "EMBEDDED_BACKEND: WebEngine not available. "
                    "YouTube playback will fail unless VIOLA_TEST_MODE is set."
                )

        self._js_communicator = EmbeddedBackendJSCommunicator(self)
        self._html_generator = EmbeddedBackendHTMLGenerator(self)
        self._webview_controller = WebViewController(self)
        self._playback_controller = PlaybackController(self)

    def _extract_video_id(self, url: str) -> str | None:
        """Extract YouTube video ID from URL."""
        return self._html_generator.extract_video_id(url)

    def _get_origin(self) -> str:
        """Get origin for YouTube embed (required for CORS)."""
        return DEFAULT_EMBED_ORIGIN

    def _create_webview(self) -> QWebEngineView | None:
        """Create or return the WebEngineView widget.

        Parented to the Viola main window's content stack when one exists,
        so the widget is a child in the main window's HWND tree rather than
        a top-level Windows window. A parentless QWebEngineView would
        otherwise become an offscreen "ghost" HWND (see project memory
        project_stage_browser_use_visible.md for the analogous bug in
        music/providers/browser_search.py that was fixed in 730da65a).
        Falls back to a parentless construction only in headless/test mode
        where no main window exists; in that mode no top-level HWND is
        observable to a user anyway.
        """
        if not self._qt_available:
            return None

        if self._webview is None:
            parent = self._find_hidden_parent()
            self._webview = QWebEngineView(parent) if parent is not None else QWebEngineView()

            # Configure settings for better compatibility
            settings = self._webview.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

            # Hide by default if headless (or always when parented into the
            # main window's hierarchy — the widget needs to exist so JS can
            # run, but the user must never see it as a separate window).
            if self._headless or parent is not None:
                self._webview.hide()
            else:
                self._webview.show()

            # Connect page load finished signal
            self._webview.page().loadFinished.connect(self._on_page_loaded)

        return self._webview

    def _find_hidden_parent(self) -> object | None:
        """Locate the Viola main window's content stack to parent the webview into.

        Searches QApplication.topLevelWidgets() for a ViolaWebViewWindow and
        returns its `_content_stack` (the same QStackedWidget that owns the
        Stage overlay and CEF placeholder). Returns None when no main window
        is available (early-init / headless / unit-test contexts).
        """
        if not self._qt_available:
            return None
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            return None
        app = QApplication.instance()
        if app is None:
            return None
        for widget in app.topLevelWidgets():
            cls_name = type(widget).__name__
            if cls_name != "ViolaWebViewWindow":
                continue
            stack = getattr(widget, "_content_stack", None)
            if stack is not None:
                return stack
            return widget
        return None

    def _on_page_loaded(self, success: bool) -> None:
        """Handle page load completion."""
        if success:
            self._logger.info("EMBEDDED_BACKEND: page loaded successfully")
            # When we navigated directly to the YouTube embed URL we still need
            # to inject our listener JS. If we are serving our own HTML the
            # script is already part of the document and no further injection
            # is necessary.
            if self._video_id and "youtube.com/embed" in (self._current_url or ""):
                self._inject_player_api()
        else:
            self._logger.error("EMBEDDED_BACKEND: page load failed")
            if self._signals:
                self._signals.playback_error.emit("Page load failed")

    def _inject_player_api(self) -> None:
        """Inject YouTube IFrame Player API JavaScript to monitor playback."""
        if not self._webview or not self._qt_available:
            return

        js_code = """
        (function() {
            // Wait for YouTube IFrame Player API to load
            function checkYT() {
                if (typeof YT !== 'undefined' && typeof YT.Player !== 'undefined') {
                    var player = new YT.Player(document.body, {
                        videoId: '%s',
                        events: {
                            'onReady': function(event) {
                                event.target.playVideo();
                                window.embeddedPlayerReady = true;
                            },
                            'onStateChange': function(event) {
                                // 0: ended, 1: playing, 2: paused, 3: buffering, -1: unstarted
                                if (event.data === YT.PlayerState.PLAYING) {
                                    window.embeddedPlayerState = 'playing';
                                } else if (event.data === YT.PlayerState.PAUSED) {
                                    window.embeddedPlayerState = 'paused';
                                } else if (event.data === YT.PlayerState.ENDED) {
                                    window.embeddedPlayerState = 'ended';
                                }
                            }
                        },
                        playerVars: {
                            'autoplay': 1,
                            'controls': 1,
                            'enablejsapi': 1,
                            'origin': '%s'
                        }
                    });
                    window.embeddedPlayer = player;
                } else {
                    setTimeout(checkYT, 100);
                }
            }
            checkYT();
        })();
        """ % (
            self._video_id or "",
            self._get_origin(),
        )

        try:
            self._webview.page().runJavaScript(js_code)
            self._logger.debug("EMBEDDED_BACKEND: injected player API JavaScript")
        except Exception as exc:
            self._logger.warning("EMBEDDED_BACKEND: failed to inject JavaScript: %s", exc)

    def _get_player_state(self) -> str | None:
        """Get current player state from JavaScript."""
        if not self._webview or not self._qt_available:
            return None

        try:
            result = [None]
            event = threading.Event()

            def callback(value):
                result[0] = value
                event.set()

            self._webview.page().runJavaScript("window.embeddedPlayerState || 'unknown'", callback)
            # Wait up to 0.1 seconds for result
            if event.wait(0.1):
                return result[0]
        except Exception as exc:
            self._logger.debug("JS get player state not ready: %r", exc)
        return None

    def _get_position_from_player(self) -> int | None:
        """Get current position from YouTube player."""
        if not self._webview or not self._qt_available or not self._video_id:
            return None

        try:
            result = [None]
            event = threading.Event()

            def callback(value):
                result[0] = value
                event.set()

            self._webview.page().runJavaScript(
                "window.embeddedPlayer ? (window.embeddedPlayer.getCurrentTime() * 1000) : 0",
                callback,
            )
            if event.wait(0.1):
                return int(result[0]) if result[0] is not None else None
        except Exception as exc:
            self._logger.debug("JS get position not ready: %r", exc)
        return None

    def _get_duration_from_player(self) -> int | None:
        """Get duration from YouTube player."""
        if not self._webview or not self._qt_available or not self._video_id:
            return None

        try:
            result = [None]
            event = threading.Event()

            def callback(value):
                result[0] = value
                event.set()

            self._webview.page().runJavaScript(
                "window.embeddedPlayer ? (window.embeddedPlayer.getDuration() * 1000) : 0",
                callback,
            )
            if event.wait(0.1):
                return int(result[0]) if result[0] is not None and result[0] > 0 else None
        except Exception as exc:
            self._logger.debug("JS get duration not ready: %r", exc)
        return None

    def _check_player_ready(self) -> bool:
        """Check if YouTube player is actually ready and accessible via JS.

        Returns True if:
        - window.embeddedPlayerReady === true
        - window.embeddedPlayer is defined
        - window.embeddedPlayerState is defined

        This is used to verify that playback actually started, not just that
        the page loaded.
        """
        if not self._webview or not self._qt_available or not self._video_id:
            return False

        try:
            result = [None]
            event = threading.Event()

            def callback(value):
                result[0] = value
                event.set()

            # Check if player is ready and state is available
            self._webview.page().runJavaScript(
                "window.embeddedPlayerReady === true && "
                "typeof window.embeddedPlayer !== 'undefined' && "
                "window.embeddedPlayerState !== undefined",
                callback,
            )
            if event.wait(0.1):
                return bool(result[0])
        except Exception as exc:
            self._logger.debug("JS check player ready not available: %r", exc)
        return False

    def play(self, source: str) -> None:
        """
        Begin playback for the given media source (YouTube URL).

        Phase 2: Refactored to be honest - no phantom playback.
        Only sets is_playing=True when webview is actually acquired and URL is loaded.
        """
        self._logger.info(
            "EMBEDDED_BACKEND: play() called with URL=%s",
            source[:80] if source else "None",
        )

        if not source:
            raise BackendError("EmbeddedPlayerBackend: no URL provided")

        self._current_url = source

        # Phase 2: Test mode handling - simulation allowed only in test mode
        if self._test_mode and not self._qt_available:
            # Test mode simulation
            self._logger.info("EMBEDDED_BACKEND: (test-mode simulation) playing %s", source[:80])
            self._is_playing = True
            self._is_paused = False
            self._pending_start = False
            self._start_progress_pump()
            if self._signals:
                self._signals.playback_started.emit()
            return

        # Phase 2: Load media URL using webview controller
        try:
            embed_url = self._webview_controller.load_media_url(source)
            # Phase 3: Mark start as pending until playback is verified
            self._playback_controller.start_playback(embed_url)
            self._logger.info("EMBEDDED_BACKEND: play() - URL loaded, webview acquired")
        except Exception as exc:
            # Phase 2: On failure, do NOT set is_playing=True
            self._logger.exception("EMBEDDED_BACKEND: play() failed: %s", exc)
            self._is_playing = False
            self._is_paused = False
            self._pending_start = False
            if self._signals:
                self._signals.playback_error.emit(str(exc))
            raise BackendError(f"EmbeddedPlayerBackend play failed: {exc}") from exc

    def pause(self) -> None:
        """Pause playback."""
        self._logger.info("EMBEDDED_BACKEND: pause()")
        self._playback_controller.pause_playback()

    def resume(self) -> None:
        """Resume playback."""
        self._logger.info("EMBEDDED_BACKEND: resume()")
        self._playback_controller.resume_playback()

    def stop(self) -> None:
        """Stop playback and release resources."""
        self._logger.info("EMBEDDED_BACKEND: stop()")
        self._playback_controller.stop_playback()

    def is_playing(self) -> bool:
        """Return True while playback is active."""
        if self._test_mode:
            return self._is_playing and not self._is_paused

        # Check JavaScript player state if available
        state = self._get_player_state()
        if state:
            return state == "playing"

        # Fall back to internal state
        return self._is_playing and not self._is_paused

    def set_volume(self, level: int) -> int:
        """Adjust volume (0-100)."""
        return self._playback_controller.set_volume_level(level)

    def seek(self, position_seconds: float) -> None:
        """Seek to position in seconds."""
        self._logger.debug("EMBEDDED_BACKEND: seek() to %s seconds", position_seconds)
        self._playback_controller.seek_to_position(position_seconds)

    def get_position(self) -> int:
        """Get current position in seconds."""
        if self._test_mode:
            return int(self._position_ms / 1000) if self._position_ms else 0

        position = self._get_position_from_player()
        if position is not None:
            self._position_ms = position
            return int(position / 1000)

        return int(self._position_ms / 1000) if self._position_ms else 0

    def get_duration(self) -> int:
        """Get duration in seconds."""
        if self._test_mode:
            return int(self._duration_ms / 1000) if self._duration_ms else 0

        duration = self._get_duration_from_player()
        if duration is not None:
            self._duration_ms = duration
            return int(duration / 1000)

        return int(self._duration_ms / 1000) if self._duration_ms else 0

    def capabilities(self) -> BackendCapabilities:
        """Return backend capabilities."""
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
        )

    def current_position_ms(self) -> int | None:
        """Return current position in milliseconds."""
        if self._test_mode:
            return self._position_ms

        position = self._get_position_from_player()
        if position is not None:
            self._position_ms = position
            return position

        return self._position_ms

    def current_duration_ms(self) -> int | None:
        """Return duration in milliseconds."""
        if self._test_mode:
            return self._duration_ms

        duration = self._get_duration_from_player()
        if duration is not None:
            self._duration_ms = duration
            return duration

        return self._duration_ms

    def _start_progress_pump(self) -> None:
        """Start progress reporting thread."""
        self._progress_manager.start_progress_pump()

    def _stop_progress_pump(self) -> None:
        """Stop progress reporting thread."""
        self._progress_manager.stop_progress_pump()

    def __del__(self):
        """Clean up resources on deletion."""
        try:
            if hasattr(self, "_progress_thread") and self._progress_thread and self._progress_thread.is_alive():
                if hasattr(self, "_progress_stop") and self._progress_stop is not None:
                    self._progress_stop.set()
                self._progress_thread.join(timeout=TIMEOUT_SHUTDOWN)
        except Exception as exc:
            if hasattr(self, "_logger"):
                self._logger.exception(
                    "Progress thread cleanup failed during deletion, continuing: %s",
                    exc,
                )

    def _progress_loop(self) -> None:
        """Flip internal state once playback has actually started."""
        if not self._pending_start:
            return
        self._pending_start = False
        self._is_playing = True
        self._is_paused = False
        if self._signals:
            self._signals.playback_started.emit()
        self._logger.info("EMBEDDED_BACKEND: playback verified (player ready)")

    def set_video_output(self, widget_or_id) -> None:
        """
        Set video output widget (for compatibility with VLC interface).

        CRITICAL: This method must ensure _webview is set so that JavaScript controls
        (pause/resume/seek) work correctly. The widget provided here is used for both
        display and JavaScript execution.
        """
        self._video_widget = widget_or_id

        # CRITICAL: Always set _webview to the widget so JavaScript controls work
        # This ensures pause/resume/seek can run JavaScript against the correct webview
        if widget_or_id is not None:
            if self._webview and hasattr(widget_or_id, "setWidget"):
                # If widget is a container, add our webview to it
                widget_or_id.setWidget(self._webview)
            else:
                # Use the widget directly as the webview for JavaScript controls
                self._webview = widget_or_id
                self._logger.debug("EMBEDDED_BACKEND: set_video_output() - Set _webview to widget for controls")
