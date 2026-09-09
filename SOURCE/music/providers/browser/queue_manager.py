"""
music.providers.browser.queue_manager
--------------------------------------

Browser-specific queue management.

Handles auto-advance, track-end detection, pre-resolution, and native
playlist support for browser-native playback.

Works alongside the existing PlaylistCursor/QueueEngine system -- the
browser queue manager is called by BrowserPlaybackEngine when it needs
to determine what to play next.

Voice command mapping (wired externally):

- "play [song]"           -> clear() + enqueue(query) + play_item(first)
- "play [song] next"      -> insert_next(query)
- "add [song] to queue"   -> enqueue(query)
- "skip" / "next"         -> advance()
- "what's next"           -> get_upcoming()
- "clear queue"           -> clear()
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from music.providers.browser.media_session import TrackMetadata
    from music.providers.browser.provider import BrowserPlaybackController
    from music.providers.browser.recipes.base import ProviderRecipe

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class BrowserQueueItem:
    """A single item in the browser queue.

    Attributes:
        id: Unique identifier for this queue entry.
        query: Original search query text.
        url: Resolved search URL (built from the recipe template).
        title: Track title -- updated via MediaSession when playing.
        artist: Artist name -- updated via MediaSession when playing.
        artwork_url: Artwork image URL -- updated via MediaSession.
        recipe_name: Which recipe to use for this item's playback.
        is_playlist_url: ``True`` when the URL points to a native playlist
            rather than a single-track search.  Native playlists let the
            provider handle track advancement, so auto-advance is skipped.
        played: ``True`` after this item has finished playback.
    """

    id: str = field(default_factory=lambda: "bq-%s" % uuid.uuid4().hex[:12])
    query: str = ""
    url: str | None = None
    title: str | None = None
    artist: str | None = None
    artwork_url: str | None = None
    recipe_name: str = "youtube_music"
    is_playlist_url: bool = False
    played: bool = False


# ---------------------------------------------------------------------------
# Queue manager
# ---------------------------------------------------------------------------

# Maximum queue size to prevent unbounded memory growth
_MAX_QUEUE_SIZE = 500


class BrowserQueueManager:
    """Manages queue behaviour for browser-native playback.

    Responsibilities:

    1. Track-end detection and auto-advance to next queue item.
    2. Pre-resolution of the next search URL while the current track plays.
    3. Native playlist URL support (let the provider handle advancement).
    4. Queue manipulation (add, insert next, clear).

    The manager registers itself as a track-end listener on the
    :class:`BrowserPlaybackController`.  When a track ends it calls
    :meth:`advance` which navigates to the next item in the internal
    queue.

    Thread Safety
    ~~~~~~~~~~~~~
    All public methods acquire ``_lock`` so the queue is safe to mutate
    from any thread (Qt main thread, worker threads, async bridges).
    """

    def __init__(
        self,
        controller: BrowserPlaybackController | None = None,
        recipe_name: str = "youtube_music",
    ) -> None:
        self._controller: BrowserPlaybackController | None = controller
        self._recipe_name = recipe_name
        self._internal_queue: list[BrowserQueueItem] = []
        self._current_index: int = -1
        self._pre_resolved_url: str | None = None
        self._auto_advance_enabled: bool = True
        self._on_track_advance_callbacks: list[Callable[[BrowserQueueItem], None]] = []
        self._lock = threading.Lock()

        # If a controller was provided at init, wire up track-end listener
        if controller is not None:
            self._register_track_end_listener(controller)

        logger.info(
            "BrowserQueueManager initialised (recipe=%s, controller=%s)",
            recipe_name,
            "attached" if controller is not None else "deferred",
        )

    # ------------------------------------------------------------------
    # Controller lifecycle
    # ------------------------------------------------------------------

    def set_controller(self, controller: BrowserPlaybackController) -> None:
        """Attach (or replace) the BrowserPlaybackController.

        Registers this manager as a track-end listener so that
        :meth:`on_track_end` is called automatically when a track finishes.

        Args:
            controller: A fully-initialised BrowserPlaybackController.
        """
        with self._lock:
            self._controller = controller

        self._register_track_end_listener(controller)
        logger.info("BrowserQueueManager controller attached")

    def _register_track_end_listener(self, controller: BrowserPlaybackController) -> None:
        """Register :meth:`on_track_end` as a track-end callback."""
        controller.on_track_end(self.on_track_end)
        logger.debug("BrowserQueueManager registered track-end listener")

    # ------------------------------------------------------------------
    # Recipe helpers
    # ------------------------------------------------------------------

    def _get_recipe(self, name: str | None = None) -> ProviderRecipe:
        """Import and return a recipe instance.

        Args:
            name: Recipe identifier; defaults to ``self._recipe_name``.

        Returns:
            A :class:`ProviderRecipe`.

        Raises:
            ValueError: If the recipe name is unknown.
        """
        from music.providers.browser.recipes import get_recipe

        return get_recipe(name or self._recipe_name)

    def _build_search_url(self, query: str, recipe_name: str | None = None) -> str:
        """Build a search URL for *query* using the given recipe.

        Args:
            query: Raw search text.
            recipe_name: Optional recipe override.

        Returns:
            Fully-formed URL with the query encoded.
        """
        recipe = self._get_recipe(recipe_name)
        return recipe.get_search_url(query)

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def enqueue(self, query: str, recipe_name: str | None = None) -> BrowserQueueItem:
        """Add a track to the end of the queue.

        If the queue was previously empty and nothing is currently playing,
        the item's URL is pre-resolved immediately so playback can start
        without delay.

        Args:
            query: Free-text search query.
            recipe_name: Optional recipe override for this item.

        Returns:
            The newly created :class:`BrowserQueueItem`.
        """
        effective_recipe = recipe_name or self._recipe_name
        url = self._build_search_url(query, effective_recipe)

        item = BrowserQueueItem(
            query=query,
            url=url,
            recipe_name=effective_recipe,
        )

        with self._lock:
            if len(self._internal_queue) >= _MAX_QUEUE_SIZE:
                logger.warning(
                    "BrowserQueueManager queue full (%d items), dropping enqueue for %s",
                    _MAX_QUEUE_SIZE,
                    query,
                )
                return item

            self._internal_queue.append(item)
            queue_len = len(self._internal_queue)

        logger.info(
            "BrowserQueueManager enqueue query=%s pos=%d url=%s",
            query,
            queue_len - 1,
            (url or "")[:120],
        )

        # Pre-resolve next if this is the first upcoming item
        self._maybe_pre_resolve_next()
        return item

    def insert_next(self, query: str, recipe_name: str | None = None) -> BrowserQueueItem:
        """Insert a track immediately after the currently playing item.

        The new item will be the next to play when the current track ends
        or the user skips.

        Args:
            query: Free-text search query.
            recipe_name: Optional recipe override for this item.

        Returns:
            The newly created :class:`BrowserQueueItem`.
        """
        effective_recipe = recipe_name or self._recipe_name
        url = self._build_search_url(query, effective_recipe)

        item = BrowserQueueItem(
            query=query,
            url=url,
            recipe_name=effective_recipe,
        )

        with self._lock:
            if len(self._internal_queue) >= _MAX_QUEUE_SIZE:
                logger.warning(
                    "BrowserQueueManager queue full (%d items), dropping insert_next for %s",
                    _MAX_QUEUE_SIZE,
                    query,
                )
                return item

            insert_pos = self._current_index + 1 if self._current_index >= 0 else 0
            self._internal_queue.insert(insert_pos, item)
            # Bump current index if we inserted before it (shouldn't happen
            # here since we always insert after, but defensive).
            if self._current_index >= 0 and insert_pos <= self._current_index:
                self._current_index += 1

        logger.info(
            "BrowserQueueManager insert_next query=%s pos=%d url=%s",
            query,
            insert_pos,
            (url or "")[:120],
        )

        # Invalidate pre-resolved URL since the "next" item changed
        with self._lock:
            self._pre_resolved_url = None

        self._maybe_pre_resolve_next()
        return item

    def clear(self) -> int:
        """Clear all items from the queue.

        The currently playing item (if any) is *not* stopped -- only
        upcoming items are removed.

        Returns:
            The number of items removed.
        """
        with self._lock:
            count = len(self._internal_queue)
            self._internal_queue.clear()
            self._current_index = -1
            self._pre_resolved_url = None

        logger.info("BrowserQueueManager cleared %d items", count)
        return count

    def get_queue(self) -> list[BrowserQueueItem]:
        """Return a snapshot of the full internal queue.

        Returns:
            A shallow copy of the queue list.
        """
        with self._lock:
            return list(self._internal_queue)

    def get_current(self) -> BrowserQueueItem | None:
        """Return the currently playing item, or ``None``.

        Returns:
            The active :class:`BrowserQueueItem` or ``None``.
        """
        with self._lock:
            if 0 <= self._current_index < len(self._internal_queue):
                return self._internal_queue[self._current_index]
            return None

    def get_upcoming(self) -> list[BrowserQueueItem]:
        """Return a list of items that have not yet been played.

        The list starts from the item *after* the current one and
        excludes items already marked as played.

        Returns:
            List of upcoming :class:`BrowserQueueItem` instances.
        """
        with self._lock:
            start = self._current_index + 1 if self._current_index >= 0 else 0
            return [item for item in self._internal_queue[start:] if not item.played]

    @property
    def queue_length(self) -> int:
        """Total number of items in the queue (played + unplayed)."""
        with self._lock:
            return len(self._internal_queue)

    @property
    def upcoming_count(self) -> int:
        """Number of items still to be played."""
        return len(self.get_upcoming())

    # ------------------------------------------------------------------
    # Playback flow
    # ------------------------------------------------------------------

    def play_item(self, item: BrowserQueueItem) -> None:
        """Play a specific queue item via the BrowserPlaybackController.

        Navigates the webview to the item's search URL and injects the
        recipe's play-first-result JavaScript.

        Args:
            item: The queue item to play.

        Raises:
            RuntimeError: If no controller is attached.
        """
        controller = self._controller
        if controller is None:
            logger.error("BrowserQueueManager play_item called with no controller")
            raise RuntimeError("No BrowserPlaybackController attached")

        if not item.url:
            logger.warning(
                "BrowserQueueManager play_item called with no URL for query=%s",
                item.query,
            )
            return

        # Update the current index to point at this item
        with self._lock:
            try:
                idx = self._internal_queue.index(item)
                self._current_index = idx
            except ValueError:
                # Item not in queue (e.g. played directly); append it
                self._internal_queue.append(item)
                self._current_index = len(self._internal_queue) - 1

        logger.info(
            "BrowserQueueManager play_item query=%s url=%s",
            item.query,
            (item.url or "")[:120],
        )

        # Navigate + inject play JS via the recipe
        try:
            recipe = self._get_recipe(item.recipe_name)
            controller.search_and_play(item.query, recipe)
        except Exception:
            logger.exception(
                "BrowserQueueManager play_item failed for query=%s",
                item.query,
            )

        # Register metadata listener to update item title/artist
        self._register_metadata_updater(item)

        # Pre-resolve the next item in background
        self._maybe_pre_resolve_next()

    def advance(self) -> BrowserQueueItem | None:
        """Advance to the next queue item.

        Marks the current item as played, moves the cursor forward, and
        plays the next unplayed item.  If the queue is exhausted, returns
        ``None`` and does nothing.

        Returns:
            The next :class:`BrowserQueueItem` now playing, or ``None``
            if there are no more items.
        """
        with self._lock:
            # Mark current as played
            if 0 <= self._current_index < len(self._internal_queue):
                self._internal_queue[self._current_index].played = True

            # Find next unplayed item
            search_start = self._current_index + 1 if self._current_index >= 0 else 0
            next_item: BrowserQueueItem | None = None
            next_idx = -1

            for idx in range(search_start, len(self._internal_queue)):
                if not self._internal_queue[idx].played:
                    next_item = self._internal_queue[idx]
                    next_idx = idx
                    break

            if next_item is None:
                self._current_index = len(self._internal_queue)
                logger.info("BrowserQueueManager advance: queue exhausted")
                return None

            self._current_index = next_idx
            # Clear pre-resolved URL as we're consuming it
            self._pre_resolved_url = None

        logger.info(
            "BrowserQueueManager advance to index=%d query=%s",
            next_idx,
            next_item.query,
        )

        # Fire advance callbacks
        self._fire_advance_callbacks(next_item)

        # Play the item
        self.play_item(next_item)
        return next_item

    def on_track_end(self) -> None:
        """Called when the current track ends.

        If auto-advance is enabled and the current item is *not* a native
        playlist (where the provider handles advancement), this method
        calls :meth:`advance` to move to the next queue item.
        """
        current = self.get_current()

        # For native playlists, the provider handles track advancement.
        # We just listen for metadata changes to stay in sync.
        if current is not None and current.is_playlist_url:
            logger.info("BrowserQueueManager track ended (native playlist, skipping advance)")
            return

        if not self._auto_advance_enabled:
            logger.info("BrowserQueueManager track ended (auto-advance disabled)")
            return

        logger.info("BrowserQueueManager track ended, auto-advancing")
        self.advance()

    # ------------------------------------------------------------------
    # Pre-resolution
    # ------------------------------------------------------------------

    def pre_resolve_next(self) -> None:
        """Pre-resolve the next queue item's URL.

        "Pre-resolution" for browser playback means constructing the
        search URL from the recipe template so that when :meth:`advance`
        is called the navigation is instant (no recipe lookup latency).

        This is a lightweight, synchronous operation (URL string
        construction only -- no network requests).
        """
        with self._lock:
            search_start = self._current_index + 1 if self._current_index >= 0 else 0
            next_item: BrowserQueueItem | None = None

            for idx in range(search_start, len(self._internal_queue)):
                if not self._internal_queue[idx].played:
                    next_item = self._internal_queue[idx]
                    break

            if next_item is None:
                self._pre_resolved_url = None
                return

        # Build the URL outside the lock (no mutation needed)
        if next_item.url is None:
            try:
                url = self._build_search_url(next_item.query, next_item.recipe_name)
                next_item.url = url
                with self._lock:
                    self._pre_resolved_url = url
                logger.debug(
                    "BrowserQueueManager pre-resolved next: query=%s url=%s",
                    next_item.query,
                    url[:120],
                )
            except Exception:
                logger.exception(
                    "BrowserQueueManager pre-resolve failed for query=%s",
                    next_item.query,
                )
        else:
            with self._lock:
                self._pre_resolved_url = next_item.url

    def _maybe_pre_resolve_next(self) -> None:
        """Pre-resolve the next item if we don't already have one cached."""
        with self._lock:
            if self._pre_resolved_url is not None:
                return
        self.pre_resolve_next()

    # ------------------------------------------------------------------
    # Native playlist support
    # ------------------------------------------------------------------

    def play_playlist_url(self, url: str, recipe_name: str | None = None) -> BrowserQueueItem:
        """Navigate directly to a playlist URL.

        The provider (e.g. YouTube Music) handles track advancement
        natively within the playlist page.  Viola monitors MediaSession
        for metadata changes but does not auto-advance to the next
        internal queue item.

        Auto-advance is automatically disabled for this item.

        Args:
            url: Full playlist URL.
            recipe_name: Optional recipe override.

        Returns:
            The :class:`BrowserQueueItem` representing the playlist.

        Raises:
            RuntimeError: If no controller is attached.
        """
        controller = self._controller
        if controller is None:
            logger.error("BrowserQueueManager play_playlist_url called with no controller")
            raise RuntimeError("No BrowserPlaybackController attached")

        effective_recipe = recipe_name or self._recipe_name

        item = BrowserQueueItem(
            query=url,
            url=url,
            title="Playlist",
            recipe_name=effective_recipe,
            is_playlist_url=True,
        )

        with self._lock:
            self._internal_queue.append(item)
            self._current_index = len(self._internal_queue) - 1

        logger.info(
            "BrowserQueueManager play_playlist_url url=%s",
            url[:120],
        )

        # Navigate directly -- no search-and-play, just load the URL
        controller.navigate(url)

        # Register metadata listener to track what's playing
        self._register_metadata_updater(item)

        return item

    # ------------------------------------------------------------------
    # Track advance callbacks
    # ------------------------------------------------------------------

    def on_track_advance(self, callback: Callable[[BrowserQueueItem], None]) -> None:
        """Register a callback for when the queue advances to the next track.

        The callback receives the :class:`BrowserQueueItem` that is about
        to start playing.

        Args:
            callback: Callable accepting a single BrowserQueueItem.
        """
        with self._lock:
            self._on_track_advance_callbacks.append(callback)
        logger.debug(
            "BrowserQueueManager advance callback registered (total=%d)",
            len(self._on_track_advance_callbacks),
        )

    def _fire_advance_callbacks(self, item: BrowserQueueItem) -> None:
        """Invoke all registered advance callbacks."""
        with self._lock:
            callbacks = list(self._on_track_advance_callbacks)

        for cb in callbacks:
            try:
                cb(item)
            except Exception:
                logger.exception("BrowserQueueManager advance callback error")

    # ------------------------------------------------------------------
    # Auto-advance control
    # ------------------------------------------------------------------

    @property
    def auto_advance_enabled(self) -> bool:
        """Whether the queue automatically advances on track end."""
        return self._auto_advance_enabled

    @auto_advance_enabled.setter
    def auto_advance_enabled(self, value: bool) -> None:
        self._auto_advance_enabled = value
        logger.info("BrowserQueueManager auto_advance_enabled=%s", value)

    # ------------------------------------------------------------------
    # Metadata integration
    # ------------------------------------------------------------------

    def _register_metadata_updater(self, item: BrowserQueueItem) -> None:
        """Register a metadata callback that updates *item* fields.

        When the BrowserPlaybackController detects a MediaSession metadata
        change, this callback writes the title, artist, and artwork URL
        back into the queue item so that :meth:`get_current` and
        :meth:`get_queue` return up-to-date information.
        """
        controller = self._controller
        if controller is None:
            return

        def _on_metadata(metadata: TrackMetadata) -> None:
            if metadata.title:
                item.title = metadata.title
            if metadata.artist:
                item.artist = metadata.artist
            if metadata.artwork_url:
                item.artwork_url = metadata.artwork_url

        controller.on_metadata_change(_on_metadata)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Release all resources and clear the queue."""
        with self._lock:
            self._internal_queue.clear()
            self._current_index = -1
            self._pre_resolved_url = None
            self._on_track_advance_callbacks.clear()
            self._controller = None

        logger.info("BrowserQueueManager cleanup complete")


__all__ = [
    "BrowserQueueItem",
    "BrowserQueueManager",
]
