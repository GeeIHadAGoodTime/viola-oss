"""
music/backends/browser_backend.py

Backend that controls playback via BrowserPlaybackController.

Similar to YouTubeIFrameBackend but uses the W3C MediaSession API
(via BrowserPlaybackController) instead of YouTube's IFrame Player API.
This provides full playback control (pause, resume, seek, volume, position,
duration) for any web music service that exposes MediaSession metadata.

Thread Safety
~~~~~~~~~~~~~
All QWebEngineView operations are marshalled to the Qt main thread by
BrowserPlaybackController.  This backend's public methods may be called
from any thread.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logging_config import get_logger
from music.backends.base import BackendCapabilities, BaseBackend

logger = get_logger(__name__)

# Truncation limit for URLs in log messages
_MAX_LOG_URL_LEN = 120


class BrowserBackend(BaseBackend):
    """Backend that controls playback via BrowserPlaybackController.

    Wraps :class:`BrowserPlaybackController` to implement the
    :class:`BaseBackend` interface.  Unlike ``YouTubeIFrameBackend``
    which can only control YouTube's IFrame Player API, this backend
    leverages the MediaSession API to control *any* web music service.

    The backend lazily imports and creates the controller to avoid
    import-time PyQt6 side effects.

    Args:
        webview_controller: Optional QWebEngineView (or compatible object).
            Can be set later via :meth:`set_webview_controller`.
        logger: Optional logger override.
    """

    def __init__(
        self,
        *,
        webview_controller: Any | None = None,
        logger_override: logging.Logger | None = None,
    ) -> None:
        super().__init__()
        self._logger = logger_override or logger
        self._webview_controller = webview_controller
        self._controller: Any | None = None  # Lazy BrowserPlaybackController

        # Local state cache
        self._is_playing: bool = False
        self._is_paused: bool = False
        self._volume: int = 80
        self._current_url: str | None = None

        self._logger.info("BrowserBackend initialized")

    # ------------------------------------------------------------------
    # Controller lifecycle
    # ------------------------------------------------------------------

    def _ensure_controller(self) -> Any:
        """Lazy-initialise and return the BrowserPlaybackController.

        Returns:
            The active BrowserPlaybackController instance.

        Raises:
            RuntimeError: If the controller cannot be created.
        """
        if self._controller is not None:
            return self._controller

        try:
            from music.providers.browser.provider import BrowserPlaybackController

            self._controller = BrowserPlaybackController(webview_controller=self._webview_controller)
            self._logger.info("BrowserBackend created BrowserPlaybackController")
        except Exception:
            self._logger.exception("BrowserBackend failed to create controller")
            raise

        return self._controller

    def set_webview_controller(self, controller: Any) -> None:
        """Set or replace the QWebEngineView controller.

        If a BrowserPlaybackController already exists, updates its
        webview reference.  Otherwise stores the reference for lazy
        initialisation.

        Args:
            controller: QWebEngineView or compatible webview object.
        """
        self._webview_controller = controller

        if self._controller is not None:
            self._controller.set_webview_controller(controller)
            self._logger.info("BrowserBackend webview controller replaced on existing BPC")
        else:
            self._logger.info("BrowserBackend webview controller stored for lazy init")

        # If we had a URL loaded and were playing, resume on the new controller
        if self._current_url and self._is_playing and self._controller is not None:
            try:
                self._controller.navigate(self._current_url)
                self._logger.info(
                    "BrowserBackend re-navigated to %s after controller swap",
                    self._current_url[:_MAX_LOG_URL_LEN],
                )
            except Exception as exc:
                self._logger.warning(
                    "BrowserBackend re-navigation failed after controller swap: %r",
                    exc,
                )

    # ------------------------------------------------------------------
    # BaseBackend implementation
    # ------------------------------------------------------------------

    def play(self, source: str) -> None:
        """Navigate to URL and start playback.

        Args:
            source: URL to navigate to and play.

        Raises:
            RuntimeError: If the controller cannot be created.
        """
        if not source:
            self._logger.warning("BrowserBackend play called with empty source")
            return

        controller = self._ensure_controller()
        self._current_url = source

        self._logger.info(
            "BrowserBackend play url=%s",
            source[:_MAX_LOG_URL_LEN],
        )

        try:
            controller.navigate(source)
            controller.start_metadata_polling()
            controller.play()
        except Exception:
            self._logger.exception(
                "BrowserBackend play failed for url=%s",
                source[:_MAX_LOG_URL_LEN],
            )
            self._is_playing = False
            self._current_url = None
            raise

        self._is_playing = True
        self._is_paused = False

    def pause(self) -> None:
        """Pause playback via MediaSession."""
        if self._controller is None:
            self._logger.warning("BrowserBackend pause called but no controller")
            return

        try:
            self._controller.pause()
            self._is_playing = False
            self._is_paused = True
            self._logger.debug("BrowserBackend paused")
        except Exception as exc:
            self._logger.warning("BrowserBackend pause failed: %r", exc)

    def resume(self) -> None:
        """Resume playback via MediaSession."""
        if self._controller is None:
            self._logger.warning("BrowserBackend resume called but no controller")
            return

        try:
            self._controller.resume()
            self._is_playing = True
            self._is_paused = False
            self._logger.debug("BrowserBackend resumed")
        except Exception as exc:
            self._logger.warning("BrowserBackend resume failed: %r", exc)

    def stop(self) -> None:
        """Stop playback and release resources."""
        if self._controller is not None:
            try:
                self._controller.stop()
                self._controller.stop_metadata_polling()
                self._logger.debug("BrowserBackend stopped")
            except Exception as exc:
                self._logger.warning("BrowserBackend stop failed: %r", exc)

        self._is_playing = False
        self._is_paused = False
        self._current_url = None

    def is_playing(self) -> bool:
        """Check whether playback is currently active.

        Queries the controller's cached state with a local fallback.

        Returns:
            ``True`` if audio is being rendered.
        """
        if self._controller is None:
            return False
        if self._current_url is None:
            return False

        # Prefer the controller's authoritative state
        try:
            return self._controller.is_playing()
        except Exception:
            return self._is_playing and not self._is_paused

    def set_volume(self, level: int) -> int:
        """Set volume on all media elements in the page.

        Args:
            level: Volume level 0-100.

        Returns:
            The clamped volume level that was applied.
        """
        clamped = max(0, min(100, int(level)))
        self._volume = clamped

        if self._controller is not None:
            try:
                self._controller.set_volume(clamped)
            except Exception as exc:
                self._logger.warning("BrowserBackend set_volume failed: %r", exc)

        self._logger.debug("BrowserBackend volume set to %d", clamped)
        return clamped

    def seek(self, position_seconds: float) -> None:
        """Seek to a specific position via MediaSession seekto action.

        Args:
            position_seconds: Target position in seconds.
        """
        if self._controller is None:
            self._logger.warning("BrowserBackend seek called but no controller")
            return

        try:
            self._controller.seek(position_seconds)
            self._logger.debug("BrowserBackend seek to %.1f s", position_seconds)
        except Exception as exc:
            self._logger.warning("BrowserBackend seek failed: %r", exc)

    def capabilities(self) -> BackendCapabilities:
        """Return the feature set supported by this backend.

        The browser backend has full capabilities via MediaSession
        and JavaScript media element control.

        Returns:
            A :class:`BackendCapabilities` instance.
        """
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
            waveform=False,
        )

    # ------------------------------------------------------------------
    # Position / duration queries
    # ------------------------------------------------------------------

    def current_position_ms(self) -> int | None:
        """Return the current playback position in milliseconds.

        Reads from the cached MediaSession metadata.

        Returns:
            Position in milliseconds, or ``None`` if unavailable.
        """
        if self._controller is None:
            return None

        try:
            cached = self._controller.get_metadata()
            if cached and cached.get("position_seconds") is not None:
                return int(float(cached["position_seconds"]) * 1000)
        except Exception as exc:
            self._logger.debug("BrowserBackend current_position_ms failed: %r", exc)

        return None

    def current_duration_ms(self) -> int | None:
        """Return the media duration in milliseconds.

        Reads from the cached MediaSession metadata.

        Returns:
            Duration in milliseconds, or ``None`` if unavailable.
        """
        if self._controller is None:
            return None

        try:
            cached = self._controller.get_metadata()
            if cached and cached.get("duration_seconds") is not None:
                return int(float(cached["duration_seconds"]) * 1000)
        except Exception as exc:
            self._logger.debug("BrowserBackend current_duration_ms failed: %r", exc)

        return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Stop playback, release controller resources."""
        if self._controller is not None:
            try:
                self._controller.cleanup()
                self._logger.info("BrowserBackend controller cleaned up")
            except Exception as exc:
                self._logger.warning("BrowserBackend cleanup failed: %r", exc)

        self._controller = None
        self._webview_controller = None
        self._is_playing = False
        self._is_paused = False
        self._current_url = None
        self._logger.info("BrowserBackend cleanup complete")


__all__ = [
    "BrowserBackend",
]
