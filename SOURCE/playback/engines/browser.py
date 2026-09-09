"""
Browser-native search engine using QWebEngineView.

The hidden browser is a SEARCH ENGINE, not a player.  It navigates to
youtube.com search results, extracts video IDs from the page, and sets
them on QueueItems.  Actual playback happens in the YouTubeEmbed iframe
in SmartDisplay (one source for audio + video).

Delegates to BrowserPlaybackController for:
- Navigation to youtube.com search pages
- JavaScript injection for video ID extraction

Thread Safety
~~~~~~~~~~~~~
All QWebEngineView operations are marshalled to the Qt main thread by
BrowserPlaybackController.  This engine's public methods may be called
from any thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from models.player import QueueItem

from .base import (
    ArtworkCallback,
    PlaybackCapabilities,
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
)

if TYPE_CHECKING:
    from music.providers.browser.provider import BrowserPlaybackController

logger = get_logger(__name__)


# 11-char YouTube video ID pattern
_YT_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


class BrowserPlaybackEngine(ProviderPlaybackEngine):
    """Browser-native search engine — extracts video IDs, never plays audio.

    The hidden browser navigates to youtube.com search results, extracts
    video IDs via JS injection, and sets them on QueueItems.  Playback is
    handled entirely by the YouTubeEmbed iframe in SmartDisplay.

    Delegates to :class:`BrowserPlaybackController` for navigation and
    JavaScript injection.  The engine lazily initialises the controller on
    first use so that import-time side effects (PyQt6, QWebEngine) are
    avoided.

    Args:
        controller: Optional pre-built controller instance.  When ``None``
            the engine creates one on first use.
        recipe_name: Default recipe identifier used for search URL
            construction (e.g. ``"youtube_music"``).
        logger: Optional logger override.
    """

    def __init__(
        self,
        *,
        controller: BrowserPlaybackController | None = None,
        recipe_name: str = "youtube_music",
        logger_override: logging.Logger | None = None,
    ) -> None:
        capabilities = PlaybackCapabilities(
            gapless=False,
            hot_buffer=False,
            artwork_sync=True,
            max_bitrate_kbps=320,
            supports_offline=False,
            supports_lyrics=False,
        )
        super().__init__("browser", "Browser Native", capabilities)

        self._controller = controller
        self._recipe_name = recipe_name
        self._logger = logger_override or logger
        self._lock = threading.Lock()
        self._handles: dict[str, PlaybackHandle] = {}
        self._active_handle: PlaybackHandle | None = None

        if controller is not None:
            self._logger.info("BrowserPlaybackEngine initialised with pre-built controller")
        else:
            self._logger.info(
                "BrowserPlaybackEngine initialised (lazy controller, recipe=%s)",
                recipe_name,
            )

    # ------------------------------------------------------------------
    # Controller lifecycle
    # ------------------------------------------------------------------

    def _ensure_controller(self) -> BrowserPlaybackController:
        """Lazy-initialise and return the BrowserPlaybackController.

        Raises:
            EnginePlaybackError: If the controller cannot be created.
        """
        if self._controller is not None:
            return self._controller

        from .base import EnginePlaybackError

        try:
            from music.providers.browser.provider import BrowserPlaybackController

            self._controller = BrowserPlaybackController()
            self._logger.info("BrowserPlaybackEngine created BrowserPlaybackController")
        except Exception as exc:
            self._logger.exception("Failed to create BrowserPlaybackController")
            self._mark_unavailable("BrowserPlaybackController creation failed")
            raise EnginePlaybackError("Failed to create BrowserPlaybackController") from exc

        return self._controller

    def set_controller(self, controller: BrowserPlaybackController) -> None:
        """Replace the underlying controller (e.g. after UI initialisation).

        Args:
            controller: A fully-initialised BrowserPlaybackController.
        """
        self._controller = controller
        self._logger.info("BrowserPlaybackEngine controller replaced")

    # ------------------------------------------------------------------
    # Recipe helpers
    # ------------------------------------------------------------------

    def _get_recipe(self, name: str | None = None) -> Any:
        """Import and instantiate a recipe by name.

        Args:
            name: Recipe identifier.  Falls back to ``self._recipe_name``.

        Returns:
            A :class:`ProviderRecipe` instance.

        Raises:
            EnginePlaybackError: If the recipe is unknown.
        """
        from .base import EnginePlaybackError

        recipe_name = name or self._recipe_name
        try:
            from music.providers.browser.recipes import get_recipe

            return get_recipe(recipe_name)
        except ValueError as exc:
            raise EnginePlaybackError("No browser recipe for provider: %s" % recipe_name) from exc

    # ------------------------------------------------------------------
    # ProviderPlaybackEngine interface
    # ------------------------------------------------------------------

    def resolve_track(self, query: str) -> QueueItem:
        """Resolve a search query to a :class:`QueueItem`.

        For the browser provider the "resolution" builds the search URL
        from the active recipe.  Actual playback starts when :meth:`play`
        navigates to the URL and the recipe JS clicks the first result.

        Args:
            query: Free-text search query.

        Returns:
            A QueueItem with ``playback_mode="embedded_webview"`` and
            metadata describing the browser recipe.
        """
        recipe = self._get_recipe()
        search_url = recipe.get_search_url(query)

        item = QueueItem(
            id="browser-%s" % uuid.uuid4().hex[:12],
            url=search_url,
            title=query,  # Updated later via MediaSession once playing
            artist=None,
            source="browser",
            provider="browser",
            playback_mode="embedded_webview",
            resolved_at=time.time(),
            metadata={
                "browser_query": query,
                "browser_recipe": self._recipe_name,
                "requires_embedded_player": True,
            },
        )

        self._logger.info(
            "BrowserEngine resolved query=%s url=%s",
            query,
            search_url[:120],
        )
        return item

    def play(
        self,
        item: QueueItem,
        *,
        queue_context: QueueContext,
        on_artwork: ArtworkCallback | None = None,
    ) -> PlaybackHandle:
        """Search youtube.com and extract video_id for embed playback.

        The method:
        1. Navigates the hidden browser to youtube.com search results
        2. After loadFinished, injects JS to extract video IDs
        3. Sets ``item.video_id`` from the first result
        4. Emits state so SmartDisplay renders the YouTubeEmbed

        The hidden browser NEVER plays audio.  Playback happens in the
        YouTubeEmbed iframe in SmartDisplay.

        Args:
            item: The queue item to play.
            queue_context: Current queue snapshot for prefetch decisions.
            on_artwork: Optional callback for artwork updates.

        Returns:
            A :class:`PlaybackHandle` tracking this search session.

        Raises:
            EnginePlaybackError: If the controller or recipe is unavailable.
        """
        from .base import EnginePlaybackError

        controller = self._ensure_controller()
        recipe_name = (getattr(item, "metadata", None) or {}).get("browser_recipe", self._recipe_name)
        recipe = self._get_recipe(recipe_name)

        if not item.url:
            raise EnginePlaybackError("Browser QueueItem missing URL")

        handle = PlaybackHandle(item, self.provider_id)
        with self._lock:
            self._handles[item.id] = handle
            self._active_handle = handle

        self._logger.info(
            "BROWSER_SEARCH: Navigating to %s (title=%s)",
            (item.url or "")[:120],
            item.title or "Unknown",
        )

        # 1. Navigate hidden browser to youtube.com search results
        try:
            controller.navigate(item.url)
        except Exception as exc:
            self._logger.exception(
                "BROWSER_SEARCH: Navigation failed for url=%s",
                (item.url or "")[:120],
            )
            handle.mark_finished(exc)
            raise EnginePlaybackError("Browser navigation failed") from exc

        # 2. Inject video ID extraction JS after page load
        extract_js = recipe.get_extract_video_ids_js()

        def _on_video_ids_extracted(result: Any) -> None:
            """Callback when JS returns video IDs from search results."""
            video_ids = self._parse_video_ids(result)
            if video_ids:
                # Set first video_id on the QueueItem
                item.video_id = video_ids[0]
                self._logger.info(
                    "BROWSER_SEARCH: Extracted video_id=%s (total=%d)",
                    video_ids[0],
                    len(video_ids),
                )
                # Trigger state emit so SmartDisplay renders the embed
                self._emit_state_update()
            else:
                self._logger.warning("BROWSER_SEARCH: No video IDs found in results")

        try:
            controller._inject_after_load(
                extract_js,
                callback=_on_video_ids_extracted,
            )
        except Exception as exc:
            self._logger.warning("BROWSER_SEARCH: Video ID extraction injection failed: %r", exc)

        handle.mark_started()
        self._logger.info(
            "BROWSER_SEARCH: Search initiated for item=%s recipe=%s",
            item.id,
            recipe_name,
        )
        return handle

    def pause(self, handle: PlaybackHandle) -> None:
        """No-op: the embed handles pause via postMessage.

        The hidden browser is idle — there's nothing to pause.
        """
        self._logger.debug("BrowserEngine pause no-op (embed handles playback)")

    def resume(self, handle: PlaybackHandle) -> None:
        """No-op: the embed handles resume via postMessage.

        The hidden browser is idle — there's nothing to resume.
        """
        self._logger.debug("BrowserEngine resume no-op (embed handles playback)")

    def stop(self, handle: PlaybackHandle) -> None:
        """Stop playback and release resources.

        Args:
            handle: The active playback handle.
        """
        controller = self._controller
        if controller is not None:
            try:
                handle.request_stop()
                controller.stop()
                controller.stop_metadata_polling()
                self._logger.info("BrowserEngine stopped item=%s", handle.item.id)
            except Exception as exc:
                self._logger.warning("BrowserEngine stop failed: %r", exc)

        with self._lock:
            self._handles.pop(handle.item.id, None)
            if self._active_handle is handle:
                self._active_handle = None

        handle.mark_finished()

    def set_volume(self, handle: PlaybackHandle, level: int) -> None:
        """No-op: the embed handles volume via postMessage.

        The hidden browser is idle — there's no media element to set volume on.
        """
        self._logger.debug("BrowserEngine set_volume no-op (embed handles playback)")

    def current_position(self, handle: PlaybackHandle) -> float:
        """Return 0.0 — position tracking is handled by the embed."""
        return 0.0

    def duration(self, handle: PlaybackHandle) -> float:
        """Return 0.0 — duration tracking is handled by the embed."""
        return 0.0

    # ------------------------------------------------------------------
    # Video ID extraction helpers
    # ------------------------------------------------------------------

    def _parse_video_ids(self, result: Any) -> list[str]:
        """Parse video IDs from the JS extraction result.

        The JS returns a JSON string: ``{video_ids: [...], count: N}``.
        The QWebEnginePage.runJavaScript callback may receive:
        - A string (JSON) — needs json.loads
        - A dict (auto-parsed by Qt) — use directly
        - None — JS error or no result

        Args:
            result: Raw result from runJavaScript callback.

        Returns:
            List of valid 11-character video IDs, or empty list.
        """
        if result is None:
            self._logger.debug("BROWSER_SEARCH: JS returned None")
            return []

        try:
            if isinstance(result, str):
                data = json.loads(result)
            elif isinstance(result, dict):
                data = result
            else:
                self._logger.warning(
                    "BROWSER_SEARCH: Unexpected result type: %s",
                    type(result).__name__,
                )
                return []
        except (json.JSONDecodeError, TypeError) as exc:
            self._logger.warning("BROWSER_SEARCH: Failed to parse result: %r", exc)
            return []

        if "error" in data:
            self._logger.warning("BROWSER_SEARCH: JS error: %s", data["error"])

        raw_ids = data.get("video_ids", [])
        if not isinstance(raw_ids, list):
            return []

        # Validate each ID is exactly 11 chars matching [A-Za-z0-9_-]
        valid = [vid for vid in raw_ids if isinstance(vid, str) and _YT_VIDEO_ID_RE.match(vid)]
        return valid

    def _emit_state_update(self) -> None:
        """Emit player state so SmartDisplay picks up the new video_id.

        Accesses the player via the engine manager's backreference.
        """
        try:
            # The engine is wrapped by EngineBackedBackend which is set as
            # player._backend.  We need to reach the player to call _emit().
            # This is done by the engine manager that holds the player ref.
            from playback.engine_manager import _get_player_for_emit

            player = _get_player_for_emit()
            if player is not None:
                player._emit()
                self._logger.debug("BROWSER_SEARCH: State emitted after video_id set")
            else:
                self._logger.warning("BROWSER_SEARCH: Could not find player for state emit")
        except Exception:
            self._logger.exception("BROWSER_SEARCH: State emit failed")

    # ------------------------------------------------------------------
    # Extended API (not in base class)
    # ------------------------------------------------------------------

    def seek(self, handle: PlaybackHandle, position_seconds: float) -> None:
        """No-op: the embed handles seek via postMessage."""
        self._logger.debug("BrowserEngine seek no-op (embed handles playback)")

    def next_track(self, handle: PlaybackHandle) -> None:
        """No-op: queue advance is handled by the player/embed."""
        self._logger.debug("BrowserEngine next_track no-op (embed handles playback)")

    def previous_track(self, handle: PlaybackHandle) -> None:
        """No-op: queue navigation is handled by the player/embed."""
        self._logger.debug("BrowserEngine previous_track no-op (embed handles playback)")

    def cleanup(self) -> None:
        """Release all resources and clean up the controller."""
        controller = self._controller
        if controller is not None:
            try:
                controller.cleanup()
                self._logger.info("BrowserEngine controller cleaned up")
            except Exception as exc:
                self._logger.warning("BrowserEngine cleanup failed: %r", exc)

        with self._lock:
            self._handles.clear()
            self._active_handle = None

        self._controller = None
        self._logger.info("BrowserPlaybackEngine cleanup complete")


__all__ = [
    "BrowserPlaybackEngine",
]
