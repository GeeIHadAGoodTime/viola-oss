"""
music.providers.browser.media_session
--------------------------------------

MediaSession metadata reader with multi-level fallback chain.

Extracts track metadata from browser pages using a prioritised cascade:

1. W3C ``navigator.mediaSession.metadata`` (title, artist, album, artwork)
2. HTML ``<audio>``/``<video>`` element state (duration, position, paused)
3. ``document.title`` parsing (most sites put "Artist - Track" in title)
4. Open Graph meta tags (``og:title``, ``og:image``)
5. Site favicon + page title (bare minimum)

The :class:`MediaSessionReader` caches the last-known metadata and exposes
a change-detection helper so the controller only broadcasts updates when
something actually changes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Minimum seconds between metadata cache updates to avoid log spam
_MIN_UPDATE_INTERVAL = 0.25

# Regex patterns for title parsing.  Common formats:
#   "Artist - Track Title"
#   "Track Title | Artist"
#   "Track Title - Artist - SiteName"
_TITLE_DASH_PATTERN = re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*(.+?)(?:\s*[-\u2013\u2014]\s*.+)?$")
_TITLE_PIPE_PATTERN = re.compile(r"^(.+?)\s*\|\s*(.+?)$")


@dataclass
class TrackMetadata:
    """Structured representation of currently-playing track metadata.

    Attributes:
        title: Track title (may be ``None`` if unresolvable).
        artist: Artist or channel name.
        album: Album name (rarely available from browser pages).
        artwork_url: URL of the largest available artwork image.
        duration_seconds: Total track duration in seconds (from media element).
        position_seconds: Current playback position in seconds.
        playback_state: One of ``"playing"``, ``"paused"``, ``"none"``.
        source: Which fallback level provided the data.  One of
            ``"media_session"``, ``"media_element"``, ``"document_title"``,
            ``"og_meta"``, ``"favicon"``, ``"unknown"``.
    """

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    artwork_url: str | None = None
    duration_seconds: float | None = None
    position_seconds: float | None = None
    playback_state: str = "none"
    source: str = "unknown"
    page_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary for StateHub or JSON transport."""
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork_url": self.artwork_url,
            "duration_seconds": self.duration_seconds,
            "position_seconds": self.position_seconds,
            "playback_state": self.playback_state,
            "source": self.source,
            "page_url": self.page_url,
        }

    @property
    def has_content(self) -> bool:
        """Return True if at least a title is available."""
        return self.title is not None and len(self.title.strip()) > 0


@dataclass
class MediaSessionReader:
    """Reads and caches MediaSession metadata with a multi-level fallback chain.

    Fallback order:
        1. ``navigator.mediaSession.metadata`` (title, artist, album, artwork)
        2. HTML ``<audio>``/``<video>`` element detection (state, duration, position)
        3. ``document.title`` parsing (most sites put "Artist - Track" in title)
        4. Open Graph meta tags (``og:title``, ``og:image``)
        5. Site favicon + page title (bare minimum)

    Usage::

        reader = MediaSessionReader()
        # After JS evaluation returns raw results ...
        metadata = reader.parse_metadata_response(raw_js_result)
        if reader.has_changed(metadata.to_dict()):
            # broadcast to StateHub
            ...
    """

    _cached_metadata: dict[str, Any] | None = field(default=None, repr=False)
    _last_update: float = field(default=0.0, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_metadata_response(
        self,
        media_session_result: dict | None = None,
        media_elements_result: list | None = None,
        page_metadata_result: dict | None = None,
    ) -> TrackMetadata:
        """Parse raw JS evaluation results into structured :class:`TrackMetadata`.

        Each argument corresponds to the return value of one of the JS bridge
        functions.  Pass as many as you have; the method walks the fallback
        chain automatically.

        Args:
            media_session_result: Return value of :func:`js_get_media_session_metadata`.
            media_elements_result: Return value of :func:`js_get_media_elements`.
            page_metadata_result: Return value of :func:`js_get_page_metadata`.

        Returns:
            A populated :class:`TrackMetadata` instance.
        """
        metadata = TrackMetadata()

        # --- Level 1: MediaSession metadata (best quality) ---
        if media_session_result and isinstance(media_session_result, dict):
            ms_title = media_session_result.get("title")
            if ms_title and str(ms_title).strip():
                metadata.title = str(ms_title).strip()
                metadata.artist = _safe_str(media_session_result.get("artist"))
                metadata.album = _safe_str(media_session_result.get("album"))
                metadata.artwork_url = _safe_str(media_session_result.get("artwork"))
                metadata.source = "media_session"

                pb_state = media_session_result.get("playbackState", "none")
                if pb_state in ("playing", "paused", "none"):
                    metadata.playback_state = pb_state

        # --- Level 2: Media element state (duration, position, playing) ---
        if media_elements_result and isinstance(media_elements_result, list):
            active_el = _find_active_media_element(media_elements_result)
            if active_el:
                metadata.duration_seconds = active_el.get("duration")
                metadata.position_seconds = active_el.get("currentTime", 0.0)

                el_paused = active_el.get("paused", True)
                # Only override playback state if MediaSession didn't provide one
                if metadata.playback_state == "none":
                    metadata.playback_state = "paused" if el_paused else "playing"

                # If we still have no title, we at least know something is loaded
                if metadata.source == "unknown":
                    metadata.source = "media_element"

        # --- Level 3: document.title parsing ---
        if not metadata.has_content and page_metadata_result:
            raw_title = _safe_str(page_metadata_result.get("title"))
            if raw_title:
                parsed = _parse_document_title(raw_title)
                if parsed:
                    metadata.title = parsed.get("title")
                    metadata.artist = parsed.get("artist")
                    metadata.source = "document_title"

        # --- Level 4: Open Graph meta tags ---
        if not metadata.has_content and page_metadata_result:
            og_title = _safe_str(page_metadata_result.get("ogTitle"))
            if og_title:
                metadata.title = og_title
                metadata.source = "og_meta"

            og_image = _safe_str(page_metadata_result.get("ogImage"))
            if og_image and not metadata.artwork_url:
                metadata.artwork_url = og_image

        # --- Level 5: Favicon + raw page title (last resort) ---
        if not metadata.has_content and page_metadata_result:
            raw_title = _safe_str(page_metadata_result.get("title"))
            if raw_title:
                metadata.title = raw_title
                metadata.source = "favicon"

            favicon = _safe_str(page_metadata_result.get("favicon"))
            if favicon and not metadata.artwork_url:
                metadata.artwork_url = favicon

        # --- Page URL (always extract if available) ---
        if page_metadata_result:
            page_url = _safe_str(page_metadata_result.get("url"))
            if page_url:
                metadata.page_url = page_url

        # Update cache
        now = time.monotonic()
        if now - self._last_update >= _MIN_UPDATE_INTERVAL:
            self._cached_metadata = metadata.to_dict()
            self._last_update = now

        return metadata

    def has_changed(self, new_metadata: dict[str, Any]) -> bool:
        """Check if metadata has changed since last cache update.

        Compares only the identifying fields (title, artist, album,
        playback_state) to avoid constant updates from position drift.

        Args:
            new_metadata: Dictionary representation of the new metadata
                (typically from :meth:`TrackMetadata.to_dict`).

        Returns:
            ``True`` if the metadata has meaningfully changed.
        """
        if self._cached_metadata is None:
            return True

        # Compare identifying fields only (not position which always changes)
        check_keys = ("title", "artist", "album", "playback_state", "artwork_url")
        for key in check_keys:
            old_val = self._cached_metadata.get(key)
            new_val = new_metadata.get(key)
            if old_val != new_val:
                return True

        return False

    def get_cached(self) -> TrackMetadata | None:
        """Return the last cached metadata as a :class:`TrackMetadata`, or ``None``."""
        if self._cached_metadata is None:
            return None
        return TrackMetadata(**self._cached_metadata)

    def clear_cache(self) -> None:
        """Reset the metadata cache."""
        self._cached_metadata = None
        self._last_update = 0.0


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    """Convert a value to a stripped string, returning ``None`` for empty/None."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _find_active_media_element(elements: list[dict]) -> dict | None:
    """Pick the most relevant media element from a list.

    Prefers the element that is currently playing.  If none are playing,
    returns the first element that has a non-zero duration (i.e. has
    loaded something).  Returns ``None`` if the list is empty.
    """
    if not elements:
        return None

    # Prefer the currently playing element
    for el in elements:
        if not el.get("paused", True):
            return el

    # Fall back to the first element with meaningful duration
    for el in elements:
        dur = el.get("duration")
        if dur is not None and dur > 0:
            return el

    # Last resort: first element
    return elements[0] if elements else None


def _parse_document_title(title: str) -> dict[str, str] | None:
    """Attempt to extract artist and track from a page title.

    Common patterns:
        ``"Artist - Track Title"``
        ``"Track Title | Artist"``
        ``"Track Title - Artist - SiteName"``

    Returns:
        A dict with ``title`` and ``artist`` keys, or ``None`` if
        the title does not match any known pattern.
    """
    if not title:
        return None

    # Try "Artist - Track" pattern (most YouTube / Spotify titles)
    match = _TITLE_DASH_PATTERN.match(title)
    if match:
        part_a = match.group(1).strip()
        part_b = match.group(2).strip()
        # Heuristic: on YouTube, it is typically "Artist - Track"
        # We return part_a as artist and part_b as title
        if part_a and part_b:
            return {"artist": part_a, "title": part_b}

    # Try "Track | Artist" pattern (some music sites)
    match = _TITLE_PIPE_PATTERN.match(title)
    if match:
        part_a = match.group(1).strip()
        part_b = match.group(2).strip()
        if part_a and part_b:
            return {"title": part_a, "artist": part_b}

    return None


__all__ = [
    "MediaSessionReader",
    "TrackMetadata",
]
