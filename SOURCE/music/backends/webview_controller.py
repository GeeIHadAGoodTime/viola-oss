"""
WebView Controller

Extracted from embedded_backend.py to reduce file size.
Handles webview creation, management, and URL loading logic.
"""

from __future__ import annotations

from typing import Any, Protocol

from music.exceptions import BackendError
from music.youtube_embed import build_embed_url, extract_video_id

QT_AVAILABLE: bool


class _QUrlFactory(Protocol):
    def __call__(self, url: str) -> object: ...


class _QApplicationFactory(Protocol):
    @classmethod
    def instance(cls) -> object | None: ...

    def __call__(self, argv: list[str]) -> object: ...


QUrl: _QUrlFactory | None = None
QApplication: _QApplicationFactory | None = None

try:
    from PySide6.QtCore import QUrl as _QUrlClass
    from PySide6.QtWidgets import QApplication as _QApplicationClass

    QT_AVAILABLE = True
    QUrl = _QUrlClass
    QApplication = _QApplicationClass
except ImportError:
    QT_AVAILABLE = False


class WebViewController:
    """Handles webview creation, management, and URL loading."""

    def __init__(self, backend: Any):
        """
        Initialize webview controller.

        Args:
            backend: The parent EmbeddedPlayerBackend instance
        """
        self._backend = backend
        self._logger = backend._logger
        self._test_mode = backend._test_mode
        self._qt_available = backend._qt_available

    def ensure_webview_target(self) -> Any:
        """
        Ensure a valid webview target is available for playback.

        Returns:
            Webview widget or None in test mode

        Raises:
            BackendError: If webview cannot be acquired and not in test mode
        """
        # Test mode simulation
        if self._test_mode and not self._qt_available:
            self._logger.debug("WEBVIEW_CONTROLLER: Test mode - no webview needed")
            return None

        if not self._qt_available:
            raise BackendError("WebEngine not available")

        # Try to get existing webview
        if self._backend._webview is not None:
            self._logger.debug("WEBVIEW_CONTROLLER: Reusing existing webview")
            return self._backend._webview

        # Create new webview
        try:
            target_widget = self._backend._create_webview()
            if target_widget is None:
                raise BackendError("Failed to create webview")

            self._backend._webview = target_widget
            self._logger.debug("WEBVIEW_CONTROLLER: Created new webview")

            # Connect signals
            if hasattr(target_widget, "loadFinished"):
                target_widget.loadFinished.connect(self._backend._on_page_loaded)
            if hasattr(target_widget, "loadStarted"):
                target_widget.loadStarted.connect(lambda: self._logger.debug("WEBVIEW_CONTROLLER: Load started"))

            return target_widget

        except Exception as exc:
            self._logger.error("WEBVIEW_CONTROLLER: Failed to create/acquire webview: %s", exc)
            raise BackendError(f"Webview acquisition failed: {exc}") from exc

    def load_media_url(self, source: str) -> str:
        """
        Load media URL into the webview.

        Args:
            source: The media source URL

        Returns:
            The embed URL that was loaded

        Raises:
            BackendError: If loading fails
        """
        # Extract video ID and build embed URL
        video_id = extract_video_id(source)
        if not video_id:
            self._logger.warning(
                "WEBVIEW_CONTROLLER: Could not extract video_id from URL: %s",
                source[:80],
            )
            # Try to use URL as-is (may be embed URL already)
            embed_url = source
        else:
            # Build canonical embed URL
            embed_url = build_embed_url(video_id)
            self._logger.debug("WEBVIEW_CONTROLLER: Built embed URL for video_id=%s", video_id)

        # Test mode simulation
        if self._test_mode and not self._qt_available:
            self._logger.info("WEBVIEW_CONTROLLER: (test-mode simulation) loading %s", embed_url[:80])
            return embed_url

        # Get webview target
        target_widget = self.ensure_webview_target()
        if target_widget is None:
            # Test mode returned None - this shouldn't happen here
            raise BackendError("Failed to acquire webview target")

        # Ensure QApplication exists
        if QApplication is None:
            raise BackendError("Qt is not available - cannot create webview")
        app = QApplication.instance()
        if app is None:
            self._logger.warning("WEBVIEW_CONTROLLER: QApplication not found, creating one")
            app = QApplication([])

        # Load URL
        try:
            if QUrl is None:
                raise BackendError("Qt is not available - cannot load URL")
            if video_id:
                # Serve first-party HTML that hosts the iframe player
                html = self._backend._html_generator.generate_youtube_html(video_id)
                self._logger.debug("WEBVIEW_CONTROLLER: Loading HTML with video_id=%s", video_id)
                target_widget.setHtml(html, QUrl("about:blank"))
            else:
                # Fallback for non-YouTube URLs or if extraction failed
                self._logger.debug("WEBVIEW_CONTROLLER: Loading embed URL directly: %s", embed_url[:80])
                target_widget.setUrl(QUrl(embed_url))

            self._logger.info("WEBVIEW_CONTROLLER: URL loaded, webview acquired")
            return embed_url

        except Exception as exc:
            self._logger.exception("WEBVIEW_CONTROLLER: URL loading failed: %s", exc)
            raise BackendError(f"URL loading failed: {exc}") from exc

    def get_embed_url(self, url: str) -> str:
        """Get the embed URL for a given source URL."""
        video_id = extract_video_id(url)
        if video_id:
            return build_embed_url(video_id)
        return url

    def extract_video_id(self, url: str) -> str | None:
        """Extract video ID from URL."""
        return extract_video_id(url)
