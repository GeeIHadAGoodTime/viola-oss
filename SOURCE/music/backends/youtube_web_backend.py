"""
YouTube Web Backend - Embedded YouTube embed player backend

This backend uses an embedded QWebEngineView to play YouTube Music tracks
via the official YouTube embed player (https://www.youtube.com/embed/{video_id}).

CANONICAL ARCHITECTURE (PRD v5.3 section 7.3):
- Uses minimal embed format: youtube.com/embed/{video_id}
- Does NOT load full YouTube site (music.youtube.com/watch)
- Does NOT decode or stream media directly
- Only controls the official embed player via QWebEngineView
- Each Spoke is an independent YouTube embed client

See docs/architecture/youtube_playback.md for full specification.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem
from music.backends.base import BackendCapabilities, BaseBackend
from music.exceptions import BackendError


class YouTubeWebBackend(BaseBackend):
    """
    Backend for playing YouTube Music tracks via embedded webview.

    This backend:
    - Does NOT decode or stream YouTube media directly
    - Loads the official YouTube Music watch URL in an embedded QWebEngineView
    - Relies on the official YouTube Music player for all playback
    - Reports minimal capabilities (no seek/volume control via backend)
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        webview_controller: Any | None = None,
    ):
        """
        Initialize YouTube Web backend.

        Args:
            logger: Logger instance
            webview_controller: Optional controller that can load URLs into webview
                              (typically a UI widget that exposes load_url(url) method)
        """
        super().__init__()
        self._logger = logger or get_logger("viola.backend.youtube_web")
        self._webview_controller = webview_controller
        self._current_url: str | None = None
        self._current_item: QueueItem | None = None
        self._is_playing = False
        self._volume = 80  # Stored for compatibility, not applied to webview

        self._logger.info("YouTubeWebBackend initialized")

    def play(self, source: str) -> None:
        """
        Begin playback for the given media source.

        For QueueItem sources, extracts the URL and loads it in the webview.
        For string sources, treats as URL directly.

        Args:
            source: QueueItem instance or URL string
        """
        url = None
        video_id = None
        title = None

        if isinstance(source, QueueItem):
            self._current_item = source
            url = source.url
            video_id = getattr(source, "video_id", None) or getattr(source, "stream_token", None)
            title = getattr(source, "title", None)
        elif isinstance(source, str):
            url = source
        else:
            self._logger.error(
                "YTM_BACKEND_ERROR stage=play error=invalid_source_type source_type=%s",
                type(source).__name__,
            )
            raise BackendError("Invalid source type for YouTubeWebBackend")

        if not url:
            self._logger.error(
                "YTM_BACKEND_ERROR stage=play error=empty_url video_id=%s title=%s",
                video_id or "none",
                title or "none",
            )
            raise BackendError("URL is required for YouTube Web backend")

        # PRD COMPLIANCE: Validate URL is in embed format (runtime check)
        try:
            from music.providers.youtube_url_validator import (
                assert_embed_url,
                validate_and_convert_url,
            )

            validated_url, is_valid = validate_and_convert_url(url)

            if is_valid and validated_url != url:
                # URL was converted to canonical format
                self._logger.info(
                    "YTM_BACKEND_URL_CONVERTED original=%s canonical=%s",
                    url[:80],
                    validated_url[:80],
                )
                url = validated_url
            elif not is_valid:
                # Invalid URL - this is a violation
                self._logger.error(
                    "YTM_BACKEND_INVALID_URL url=%s video_id=%s - URL must be in embed format",
                    url[:100],
                    video_id or "none",
                )
                raise BackendError(
                    f"Invalid YouTube URL format: {url[:100]}. "
                    "URL must be in embed format (youtube.com/embed/VIDEO_ID) per PRD v5.3 section 7.3.3."
                )

            # Runtime assertion for defensive programming
            assert_embed_url(url, context=f"YTM_BACKEND_PLAY video_id={video_id}")
        except ImportError:
            # Validator not available - log warning but continue
            self._logger.warning("YTM_BACKEND: youtube_url_validator not available, skipping URL validation")
        except ValueError as exc:
            # URL validation failed
            self._logger.error("YTM_BACKEND_URL_VALIDATION_FAILED url=%s error=%s", url[:100], str(exc))
            raise BackendError(f"URL validation failed: {exc}") from exc

        # GUARD: Must have controller - this is the critical silent failure fix
        if self._webview_controller is None:
            self._logger.error(
                "YTM_BACKEND_PLAY_REJECTED reason=no_webview_controller video_id=%s url=%s "
                "- cannot produce audio/video output without a webview",
                video_id or "none",
                url[:80],
            )
            raise BackendError(
                "YouTubeWebBackend cannot play: no webview controller attached. "
                "Ensure Qt WebEngine is available and the UI has initialized the player widget."
            )

        self._current_url = url

        # Log with distinctive marker for pipeline tracing
        self._logger.info(
            "YTM_BACKEND_PLAY video_id=%s title=%s url=%s has_controller=%s",
            video_id or "none",
            title or "Unknown",
            url[:80] if len(url) > 80 else url,
            self._webview_controller is not None,
        )

        # Dispatch to webview controller - if this fails, we should NOT be marked as playing
        try:
            if hasattr(self._webview_controller, "load_url"):
                self._webview_controller.load_url(url)
                self._logger.info(
                    "YTM_BACKEND_CONTROLLER_CALLED method=load_url video_id=%s url=%s",
                    video_id or "none",
                    url[:80],
                )
            elif hasattr(self._webview_controller, "set_youtube_url"):
                self._webview_controller.set_youtube_url(url)
                self._logger.info(
                    "YTM_BACKEND_CONTROLLER_CALLED method=set_youtube_url video_id=%s url=%s",
                    video_id or "none",
                    url[:80],
                )
            else:
                self._logger.error(
                    "YTM_BACKEND_PLAY_REJECTED reason=no_controller_method video_id=%s url=%s",
                    video_id or "none",
                    url[:80],
                )
                raise BackendError("YouTubeWebBackend controller has no load_url or set_youtube_url method")
        except BackendError:
            raise
        except Exception as exc:
            self._logger.exception(
                "YTM_BACKEND_PLAY_FAILED video_id=%s url=%s error=%s",
                video_id or "none",
                url[:80],
                str(exc)[:100],
            )
            raise BackendError(f"Failed to load URL in webview: {exc}") from exc

        # Only mark as playing AFTER successful dispatch
        self._is_playing = True
        self._logger.info("YTM_BACKEND_PLAY_SUCCESS video_id=%s url=%s", video_id or "none", url[:80])

    def pause(self) -> None:
        """
        Pause playback.

        Note: The embedded YouTube player controls its own playback.
        This is a no-op that logs a message.
        """
        self._logger.debug("YouTubeWebBackend.pause() - control handled by embedded player")
        # Pause is handled by the embedded YouTube player UI
        # We don't have JS bridge control yet, so this is informational
        self._is_playing = False

    def resume(self) -> None:
        """
        Resume playback.

        Note: The embedded YouTube player controls its own playback.
        This is a no-op that logs a message.
        """
        self._logger.debug("YouTubeWebBackend.resume() - control handled by embedded player")
        # Resume is handled by the embedded YouTube player UI
        # We don't have JS bridge control yet, so this is informational
        self._is_playing = True

    def stop(self) -> None:
        """Stop playback and clear current URL."""
        self._logger.debug("YouTubeWebBackend.stop()")
        self._is_playing = False
        self._current_url = None
        self._current_item = None

    def is_playing(self) -> bool:
        """
        Return True if playback is active.

        Returns False if no controller is attached (can't actually be playing).
        """
        # Cannot be playing if we have no way to output audio/video
        if self._webview_controller is None:
            return False
        # Cannot be playing if we have no URL loaded
        if self._current_url is None:
            return False
        return self._is_playing

    def set_volume(self, level: int) -> int:
        """
        Set volume level (0-100).

        Note: Volume control is not available via this backend.
        The embedded YouTube player handles its own volume.
        This stores the level for compatibility but does not apply it.
        """
        self._volume = max(0, min(100, int(level)))
        self._logger.debug(
            "YouTubeWebBackend.set_volume(%d) - volume handled by embedded player",
            self._volume,
        )
        return self._volume

    def seek(self, position_seconds: float) -> None:
        """
        Seek to position in seconds.

        Note: Seeking is not available via this backend.
        The embedded YouTube player handles its own seeking.
        This is a no-op that logs a message.
        """
        self._logger.debug(
            "YouTubeWebBackend.seek(%.2f) - seek handled by embedded player",
            position_seconds,
        )

    def capabilities(self) -> BackendCapabilities:
        """
        Return capabilities of this backend.

        YouTube Web backend has minimal capabilities:
        - No seek (player controls its own seeking)
        - No volume (player controls its own volume)
        - No position/duration reporting (no JS bridge yet)
        - Streaming: Yes (it streams via the official player)
        """
        return BackendCapabilities(
            streaming=True,
            pause=False,  # Pause/resume controlled by embedded player UI
            resume=False,
            seek=False,  # Seek controlled by embedded player UI
            volume=False,  # Volume controlled by embedded player UI
            position=False,  # No position reporting without JS bridge
            duration=False,  # No duration reporting without JS bridge
            waveform=False,
        )

    def set_webview_controller(self, controller: Any) -> None:
        """
        Set the webview controller for loading URLs.

        Args:
            controller: Object with load_url(url) or set_youtube_url(url) method
        """
        self._webview_controller = controller
        self._logger.debug("YouTubeWebBackend webview controller set")

        # If we have a pending URL, load it now
        if self._current_url and self._is_playing:
            try:
                if hasattr(controller, "load_url"):
                    controller.load_url(self._current_url)
                elif hasattr(controller, "set_youtube_url"):
                    controller.set_youtube_url(self._current_url)
            except Exception as exc:
                self._logger.warning("Failed to load pending URL after setting controller: %r", exc)
