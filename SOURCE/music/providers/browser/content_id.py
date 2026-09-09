"""
music.providers.browser.content_id
-----------------------------------

Extract content identifiers from provider page URLs using simple string parsing.

No API calls.  No scraping.  Just URL pattern matching to determine:
1. Which provider the URL belongs to
2. The content ID (video ID, track ID, etc.)
3. The appropriate embed URL for displaying in SmartDisplay
4. The embed type (video, player, album_art)

The YouTube embed is handled by SmartDisplay's existing YouTubeEmbed component
via the ``video_id`` field on QueueItem.  Spotify uses its official embeddable
player widget.  Other providers fall back to album art.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.logging_config import get_logger

logger = get_logger(__name__)

# Regex for YouTube video ID (11 alphanumeric + dash + underscore chars)
_YT_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")

# Spotify content path pattern: /track/ID, /album/ID, /playlist/ID
_SPOTIFY_CONTENT_RE = re.compile(r"^/(?:track|album|playlist|episode|show)/([a-zA-Z0-9]+)")


@dataclass
class ContentDisplay:
    """Display information extracted from a provider page URL."""

    provider: str
    """Provider identifier such as youtube_music or spotify."""

    content_id: str | None
    """Provider-specific content ID (video_id, track_id, etc.)."""

    embed_url: str | None
    """Full embed URL ready for iframe src, or None for album_art mode.
    For YouTube this is None — SmartDisplay builds its own embed URL from video_id."""

    embed_type: str
    """One of: video, player, album_art, loading."""

    artwork_url: str | None
    """Artwork URL from MediaSession metadata (fallback display)."""


def extract_content_display(
    url: str,
    provider: str,
    media_session_metadata: dict[str, Any] | None = None,
) -> ContentDisplay:
    """Extract display info from the current page URL + metadata.

    Args:
        url: The current page URL (from ``window.location.href``).
        provider: Provider identifier (e.g. ``"youtube_music"``).
        media_session_metadata: Optional MediaSession metadata dict with
            ``artwork_url`` key for fallback display.

    Returns:
        A :class:`ContentDisplay` with extracted information.
    """
    artwork = _extract_artwork(media_session_metadata)

    if provider in ("youtube_music", "youtube"):
        return _extract_youtube(url, provider, artwork)

    if provider == "spotify":
        return _extract_spotify(url, artwork)

    # Unknown provider — album art fallback
    return ContentDisplay(
        provider=provider,
        content_id=None,
        embed_url=None,
        embed_type="album_art",
        artwork_url=artwork,
    )


# ---------------------------------------------------------------------------
# Provider-specific extractors
# ---------------------------------------------------------------------------


def _extract_youtube(url: str, provider: str, artwork: str | None) -> ContentDisplay:
    """Extract YouTube / YouTube Music video ID from URL.

    Handles:
    - ``music.youtube.com/watch?v=VIDEO_ID``
    - ``youtube.com/watch?v=VIDEO_ID``
    - ``youtu.be/VIDEO_ID``
    - Search pages (no video ID yet)
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    # Check for v= query parameter (watch pages)
    if "watch" in parsed.path or "v" in parse_qs(parsed.query):
        params = parse_qs(parsed.query)
        video_ids = params.get("v", [])
        if video_ids and _YT_VIDEO_ID_RE.match(video_ids[0]):
            video_id = video_ids[0]
            return ContentDisplay(
                provider=provider,
                content_id=video_id,
                embed_url=None,  # SmartDisplay builds YouTube embed URL
                embed_type="video",
                artwork_url=artwork,
            )

    # youtu.be short links: /VIDEO_ID
    if "youtu.be" in host:
        path_id = parsed.path.strip("/")
        if path_id and _YT_VIDEO_ID_RE.match(path_id):
            return ContentDisplay(
                provider=provider,
                content_id=path_id,
                embed_url=None,
                embed_type="video",
                artwork_url=artwork,
            )

    # No video ID found — search page, browse page, etc.
    return ContentDisplay(
        provider=provider,
        content_id=None,
        embed_url=None,
        embed_type="loading",
        artwork_url=artwork,
    )


def _extract_spotify(url: str, artwork: str | None) -> ContentDisplay:
    """Extract Spotify content ID and build embed URL.

    Handles:
    - ``open.spotify.com/track/TRACK_ID``
    - ``open.spotify.com/album/ALBUM_ID``
    - ``open.spotify.com/playlist/PLAYLIST_ID``
    - Search pages (no content ID yet)
    """
    parsed = urlparse(url)

    match = _SPOTIFY_CONTENT_RE.match(parsed.path)
    if match:
        content_id = match.group(1)
        # Build the embed path: /track/ID → /embed/track/ID
        # Strip query params but keep the content type
        path_parts = parsed.path.strip("/").split("/")
        content_type = path_parts[0] if path_parts else "track"
        embed_url = "https://open.spotify.com/embed/%s/%s?utm_source=generator&theme=0" % (content_type, content_id)
        return ContentDisplay(
            provider="spotify",
            content_id=content_id,
            embed_url=embed_url,
            embed_type="player",
            artwork_url=artwork,
        )

    # No content ID found — search or browse page
    return ContentDisplay(
        provider="spotify",
        content_id=None,
        embed_url=None,
        embed_type="loading",
        artwork_url=artwork,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_artwork(metadata: dict[str, Any] | None) -> str | None:
    """Extract artwork URL from MediaSession metadata dict."""
    if not metadata:
        return None

    # Direct artwork_url field
    artwork = metadata.get("artwork_url")
    if artwork:
        return str(artwork)

    # MediaSession artwork array format
    artwork_list = metadata.get("artwork")
    if isinstance(artwork_list, list) and artwork_list:
        # Prefer largest image (usually last in the array)
        for item in reversed(artwork_list):
            if isinstance(item, dict) and item.get("src"):
                return str(item["src"])

    return None


__all__ = [
    "ContentDisplay",
    "extract_content_display",
]
