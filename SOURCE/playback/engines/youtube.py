"""
YouTube embedded player integration.

The engine delegates actual rendering/playback to a UI-supplied controller that
wraps the official IFrame Player API.  This keeps the backend transport-agnostic
while ensuring we comply with YouTube's Terms of Service (no raw stream URL
extraction).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Protocol

import requests

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from models.player import QueueItem
from music.youtube_embed import extract_video_id as canonical_extract_video_id

from .base import (
    PlaybackCapabilities,
    PlaybackError,
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
)


class YouTubeEmbedController(Protocol):
    """
    UI bridge for the official YouTube IFrame Player API.

    Implementations live in the Qt/Web UI layers and are responsible for
    executing JavaScript in a trusted WebView context.
    """

    def play(self, video_id: str, handle: PlaybackHandle, *, start_ms: int = 0) -> None: ...

    def pause(self, handle: PlaybackHandle) -> None: ...

    def resume(self, handle: PlaybackHandle) -> None: ...

    def stop(self, handle: PlaybackHandle) -> None: ...

    def set_volume(self, handle: PlaybackHandle, volume: int) -> None: ...

    def current_position(self, handle: PlaybackHandle) -> float:  # pragma: no cover - UI callback
        ...

    def duration(self, handle: PlaybackHandle) -> float:  # pragma: no cover
        ...


class YouTubeEmbeddedEngine(ProviderPlaybackEngine):
    """
    Playback engine that delegates to the official YouTube embedded player.
    """

    def __init__(
        self,
        controller: YouTubeEmbedController | None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        capabilities = PlaybackCapabilities(
            gapless=False,
            hot_buffer=False,
            artwork_sync=True,
            max_bitrate_kbps=None,
            supports_offline=False,
            supports_lyrics=True,
        )
        super().__init__("youtube_music", "YouTube Embedded Player", capabilities)
        self._controller = controller
        self._logger = logger or get_logger("viola.playback.youtube_embed")
        self._lock = threading.Lock()
        self._handles: dict[str, PlaybackHandle] = {}

        if controller is None:
            self._mark_unavailable("YouTube embed controller not provided")

    # ------------------------------------------------------------------#
    # Helpers
    # ------------------------------------------------------------------#
    def _extract_video_id(self, query: str) -> str | None:
        """Extract video ID using canonical implementation."""
        return canonical_extract_video_id(query)

    def _fetch_oembed(self, video_id: str) -> dict[str, Any]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        response = requests.get(
            "https://www.youtube.com/oembed",
            params={"format": "json", "url": url},
            timeout=TIMEOUT_LONG,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------#
    # ProviderPlaybackEngine
    # ------------------------------------------------------------------#
    def resolve_track(self, query: str) -> QueueItem:
        video_id = self._extract_video_id(query)
        if video_id is None:
            raise PlaybackError(f"Unable to parse YouTube video id from '{query}'")

        metadata = {}
        try:
            metadata = self._fetch_oembed(video_id)
        except Exception as exc:
            self._logger.debug("YouTube oEmbed lookup failed: %s", exc)

        title = metadata.get("title") or "YouTube Video"
        author = metadata.get("author_name") or "Unknown Creator"
        thumbnail_url = metadata.get("thumbnail_url")

        item = QueueItem(
            id=f"youtube-{video_id}",
            title=title,
            url=f"https://www.youtube.com/watch?v={video_id}",
            source="youtube",
            video_id=video_id,
            artist=author,
            provider="youtube_music",
            stream_token=video_id,
            resolved_at=time.time(),
            artwork_url=thumbnail_url,
        )
        if thumbnail_url:
            item.capabilities["thumbnail_url"] = thumbnail_url
        return item

    def play(
        self,
        item: QueueItem,
        *,
        queue_context: QueueContext,
        on_artwork=None,
    ) -> PlaybackHandle:
        if not self.is_available():
            error_msg = self.availability_error() or "YouTube controller missing"
            self._logger.error(
                "YTM_ENGINE_ERROR stage=play error=controller_unavailable message=%s",
                error_msg,
            )
            raise PlaybackError(
                "YouTube playback is not available. Please check that the YouTube player is initialized."
            )
        if not self._controller:
            self._logger.error("YTM_ENGINE_ERROR stage=play error=controller_not_configured")
            raise PlaybackError(
                "YouTube playback is not available. Please check that the YouTube player is initialized."
            )

        # Extract video_id from QueueItem - prefer video_id field, then stream_token, then extract from URL
        video_id = item.video_id or item.stream_token or self._extract_video_id(item.url or "")
        if not video_id:
            raise PlaybackError("YouTube QueueItem missing video identifier")

        # Log with distinctive marker for pipeline tracing
        self._logger.info(
            "YTM_ENGINE_PLAY video_id=%s title=%s",
            video_id,
            item.title or "Unknown",
        )

        handle = PlaybackHandle(item, self.provider_id)
        with self._lock:
            self._handles[item.id] = handle

        if item.artwork_url and on_artwork:
            on_artwork(item, item.artwork_url)

        try:
            self._controller.play(video_id, handle, start_ms=0)
            handle.mark_started()
            self._logger.info(
                "YouTube engine: playback started successfully for video_id=%s",
                video_id,
            )
        except Exception as exc:
            # Log with distinctive marker for errors
            error_msg = str(exc) if exc else "Unknown error"
            self._logger.error(
                "YTM_ENGINE_ERROR video_id=%s error=%s",
                video_id,
                error_msg[:100],  # Truncate to avoid log spam
            )
            handle.mark_finished(exc)
            raise PlaybackError(
                f"YouTube playback failed: {exc}. Please check that the video is available and your account is linked."
            ) from exc

        return handle

    def pause(self, handle: PlaybackHandle) -> None:
        if self._controller is None:
            self._logger.warning("YouTube engine: pause called but controller is None (wiring issue?)")
            return
        try:
            self._controller.pause(handle)
            handle.mark_paused()
            self._logger.info("YouTube engine: playback paused for video_id=%s", handle.item.video_id)
        except Exception as exc:
            self._logger.warning("YouTube engine: pause failed: %r", exc)
            # Don't raise - pause failures are non-critical

    def resume(self, handle: PlaybackHandle) -> None:
        if self._controller is None:
            self._logger.warning("YouTube engine: resume called but controller is None (wiring issue?)")
            return
        try:
            self._controller.resume(handle)
            handle.mark_resumed()
            self._logger.info("YouTube engine: playback resumed for video_id=%s", handle.item.video_id)
        except Exception as exc:
            self._logger.warning("YouTube engine: resume failed: %r", exc)
            # Don't raise - resume failures are non-critical

    def stop(self, handle: PlaybackHandle) -> None:
        if self._controller is None:
            self._logger.warning("YouTube engine: stop called but controller is None (wiring issue?)")
        else:
            try:
                handle.request_stop()
                self._controller.stop(handle)
                self._logger.info(
                    "YouTube engine: playback stopped for video_id=%s",
                    handle.item.video_id,
                )
            except Exception as exc:
                self._logger.warning("YouTube engine: stop failed: %r", exc)
                # Still mark as finished even if stop fails
        with self._lock:
            self._handles.pop(handle.item.id, None)
        handle.mark_finished()

    def set_volume(self, handle: PlaybackHandle, level: int) -> None:
        if self._controller is None:
            self._logger.warning("YouTube engine: set_volume called but controller is None (wiring issue?)")
            return
        self._controller.set_volume(handle, level)

    def current_position(self, handle: PlaybackHandle) -> float:
        if self._controller is None:
            return 0.0  # No controller = no position info available
        try:
            return float(self._controller.current_position(handle))
        except Exception as exc:
            self._logger.debug("YouTube engine: current_position query failed: %r", exc)
            return 0.0

    def duration(self, handle: PlaybackHandle) -> float:
        if self._controller is None:
            return 0.0  # No controller = no duration info available
        try:
            return float(self._controller.duration(handle))
        except Exception as exc:
            self._logger.debug("YouTube engine: duration query failed: %r", exc)
            return 0.0
