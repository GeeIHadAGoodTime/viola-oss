"""
music/backends/youtube_iframe_backend.py

Backend that controls playback via a local HTML asset embedding the official
YouTube IFrame Player API. It does not download or intercept media streams.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem
from music.backends.base import BackendCapabilities, BaseBackend
from music.exceptions import BackendError
from music.youtube_embed import build_embed_url, extract_video_id


class YouTubeIFrameBackend(BaseBackend):
    """
    Backend for playing YouTube videos using a local HTML asset that wraps
    the official YouTube IFrame Player API.

    This backend:
    - Does NOT decode or stream media directly
    - Loads /static/webviews/youtube_iframe.html into a WebView
    - Controls playback by invoking simple JS helpers when available
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        webview_controller: Any | None = None,
        static_base_path: str = "/static/webviews",
    ) -> None:
        super().__init__()
        self._logger = logger or get_logger("viola.backend.youtube_iframe")
        self._webview_controller = webview_controller
        self._static_base_path = static_base_path.rstrip("/") or "/static/webviews"

        self._current_item: QueueItem | None = None
        self._current_video_id: str | None = None
        self._current_url: str | None = None
        self._is_playing: bool = False
        self._paused: bool = False
        self._volume: int = 80

        self._logger.info("YouTubeIFrameBackend initialized")

    # ---------- helpers ----------

    def _extract_video_id(self, source: QueueItem | str) -> tuple[str | None, str | None]:
        """
        Return (video_id, url) using QueueItem fields when available.
        For strings, use the canonical extract_video_id function.
        """
        if isinstance(source, QueueItem):
            vid = getattr(source, "video_id", None)
            url = getattr(source, "url", None)
            # If no explicit video_id, try extracting from URL
            if vid is None and url:
                vid = extract_video_id(url)
            return vid, url

        if isinstance(source, str):
            url = source
            vid = extract_video_id(url)
            return vid, url

        return None, None

    def _build_asset_url(self, video_id: str | None) -> str:
        """
        Build the local asset URL for the iframe page using canonical function.
        """
        if video_id:
            return build_embed_url(video_id, use_iframe_html=True)
        # Fallback to base path without video_id
        return f"{self._static_base_path}/youtube_iframe.html"

    def _dispatch_load(self, url: str) -> None:
        """
        Load the provided URL into the webview controller if present.
        """
        if self._webview_controller is None:
            self._logger.info("YTI_NO_CONTROLLER url=%s reason=ui_will_handle_via_state", url[:80])
            return
        try:
            if hasattr(self._webview_controller, "load_url"):
                self._webview_controller.load_url(url)
                self._logger.info("YTI_CONTROLLER.load_url url=%s", url[:80])
            elif hasattr(self._webview_controller, "set_url"):
                self._webview_controller.set_url(url)
                self._logger.info("YTI_CONTROLLER.set_url url=%s", url[:80])
            else:
                self._logger.warning("YTI_CONTROLLER.no_load_method url=%s", url[:80])
        except Exception as exc:
            self._logger.warning("YTI_CONTROLLER.load_failed url=%s error=%r", url[:80], exc)

    def _try_eval_js(self, js: str) -> None:
        """
        Best-effort JS invocation on the webview controller when supported.
        No-ops if the controller doesn't expose an evaluation method.

        THREAD SAFETY: Qt's runJavaScript must be called from the main thread.
        When called from other threads (e.g., wake-detector-thread for audio ducking),
        we use QMetaObject.invokeMethod with QueuedConnection to marshal the call.
        """
        ctrl = self._webview_controller
        if ctrl is None:
            self._logger.warning(
                "YTI_JS_SKIP: No webview_controller set (js=%s thread=%s)",
                js[:50],
                threading.current_thread().name,
            )
            return

        current_thread = threading.current_thread().name
        is_main_thread = current_thread == "MainThread"

        try:
            if hasattr(ctrl, "eval_js"):
                ctrl.eval_js(js)
                return
            if hasattr(ctrl, "run_js"):
                ctrl.run_js(js)
                return
            if hasattr(ctrl, "runJavaScript"):
                if is_main_thread:
                    ctrl.runJavaScript(js)
                else:
                    # Marshal to Qt main thread for thread safety
                    self._invoke_js_on_main_thread(ctrl, js)
                return
        except Exception as exc:
            self._logger.debug("YTI_JS_INVOKE_FAILED thread=%s error=%r", current_thread, exc)

    def _invoke_js_on_main_thread(self, ctrl: Any, js: str) -> None:
        """
        Marshal JavaScript execution to the Qt main thread.

        Uses QMetaObject.invokeMethod with QueuedConnection for fire-and-forget
        execution. This is critical for audio ducking which triggers from
        the wake-detector-thread.
        """
        try:
            from PySide6.QtCore import Q_ARG, QMetaObject, Qt

            # Get the page object which has runJavaScript
            page = ctrl.page() if hasattr(ctrl, "page") else ctrl

            QMetaObject.invokeMethod(
                page,
                "runJavaScript",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, js),
            )
            self._logger.debug(
                "YTI_JS_QUEUED thread=%s js=%s",
                threading.current_thread().name,
                js[:60],
            )
        except ImportError:
            self._logger.warning("YTI_JS_MARSHAL_FAILED: PyQt6 not available")
        except Exception as exc:
            self._logger.warning(
                "YTI_JS_MARSHAL_FAILED thread=%s error=%r",
                threading.current_thread().name,
                exc,
            )

    # ---------- BaseBackend implementation ----------

    def play(self, source: str | QueueItem) -> None:
        """
        Load the local iframe page with the requested video queued to play.

        Raises:
            BackendError: If no webview controller is available or video_id is missing.
        """
        video_id, _url = self._extract_video_id(source)

        # GUARD: Must have video_id or URL
        if video_id is None and isinstance(source, QueueItem) and not source.url:
            self._logger.error("YTI_PLAY_REJECTED reason=missing_video_id_and_url source=%r", source)
            raise BackendError("YouTubeIFrameBackend requires video_id or URL to play")

        # GUARD: Must have controller - this is the critical silent failure fix
        if self._webview_controller is None:
            self._logger.error(
                "YTI_PLAY_REJECTED reason=no_webview_controller video_id=%s "
                "- cannot produce audio/video output without a webview",
                video_id,
            )
            raise BackendError(
                "YouTubeIFrameBackend cannot play: no webview controller attached. "
                "Ensure Qt WebEngine is available and the UI has initialized the player widget."
            )

        # Set up state BEFORE attempting dispatch (so we can clean up on failure)
        self._current_item = source if isinstance(source, QueueItem) else None
        self._current_video_id = video_id
        self._current_url = self._build_asset_url(video_id)

        # Attempt to load - if this fails, we should NOT be marked as playing.
        # SKIP dispatch when embedded source is active (EMBEDDED_DEFER path).
        # React renders the iframe via state broadcast + YouTubeEmbed component.
        # Loading here would navigate the Qt webview away from React, creating
        # a duplicate unmuted iframe that causes echo/static.
        try:
            from audio_core.streaming.pipeline_wiring import is_embedded_source_active

            skip_dispatch = is_embedded_source_active()
            self._logger.info(
                "YTI_PLAY_DISPATCH_CHECK embedded_active=%s video_id=%s",
                skip_dispatch,
                video_id,
            )
        except Exception as exc:
            skip_dispatch = False
            self._logger.warning("YTI_PLAY_DISPATCH_CHECK failed (defaulting to dispatch): %r", exc)

        if skip_dispatch:
            self._logger.info(
                "YTI_PLAY_SKIP_DISPATCH reason=embedded_source_active video_id=%s " "(React handles iframe via state)",
                video_id,
            )
        else:
            try:
                self._dispatch_load(self._current_url)
            except Exception as e:
                self._logger.error(
                    "YTI_PLAY_FAILED reason=dispatch_error video_id=%s error=%s",
                    video_id,
                    e,
                )
                self._is_playing = False
                self._current_url = None
                raise BackendError(f"Failed to load YouTube player: {e}") from e

        # Only mark as playing AFTER successful dispatch
        self._is_playing = True
        self._paused = False

        # Hub-local muting removed — source plays at full volume,
        # ProcTap captures, WebSocket broadcasts to spokes.

        self._logger.info(
            "YTI_PLAY_SUCCESS video_id=%s url=%s",
            video_id or "none",
            self._current_url[:80],
        )

    def pause(self) -> None:
        self._paused = True
        self._is_playing = False
        self._try_eval_js("window.violaYoutube && window.violaYoutube.pause && window.violaYoutube.pause();")
        self._logger.debug("YouTubeIFrameBackend.pause()")

    def resume(self) -> None:
        self._paused = False
        self._is_playing = True
        self._try_eval_js("window.violaYoutube && window.violaYoutube.play && window.violaYoutube.play();")
        self._logger.debug("YouTubeIFrameBackend.resume()")

    def stop(self) -> None:
        self._paused = False
        self._is_playing = False
        self._try_eval_js("window.violaYoutube && window.violaYoutube.stop && window.violaYoutube.stop();")
        self._logger.debug("YouTubeIFrameBackend.stop()")

    def is_playing(self) -> bool:
        """
        Check if backend is currently playing.

        Returns False if no controller is attached (can't actually be playing).
        """
        # Cannot be playing if we have no way to output audio/video
        if self._webview_controller is None:
            return False
        # Cannot be playing if we have no URL loaded
        if self._current_url is None:
            return False
        return bool(self._is_playing and not self._paused)

    def set_volume(self, level: int) -> int:
        """Set volume on the YouTube IFrame player via JS API."""
        self._volume = max(0, min(100, int(level)))
        # Actually control YouTube player volume via JavaScript
        self._try_eval_js(
            f"window.violaYoutube && window.violaYoutube.setVolume && window.violaYoutube.setVolume({self._volume});"
        )
        self._logger.debug("YouTubeIFrameBackend.set_volume(%d)", self._volume)
        return self._volume

    def seek(self, position_seconds: float) -> None:
        # Optional: if the page exposes a seek helper, call it.
        self._try_eval_js(
            f"window.violaYoutube && window.violaYoutube.seekTo && window.violaYoutube.seekTo({float(position_seconds):.3f});"
        )

    def capabilities(self) -> BackendCapabilities:
        # Minimal capabilities; the embedded player UI handles details.
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=False,  # Not guaranteed without robust bridge; can enable later
            volume=False,
            position=False,
            duration=False,
            waveform=False,
        )

    def set_webview_controller(self, controller: Any) -> None:
        """Set/replace the webview controller."""
        self._webview_controller = controller
        # If we already have a URL to show, load it now
        if self._current_url and self._is_playing:
            self._dispatch_load(self._current_url)
