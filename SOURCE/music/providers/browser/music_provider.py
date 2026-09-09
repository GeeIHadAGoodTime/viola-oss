"""
Browser-native music provider.

Wraps BrowserPlaybackController as a MusicProvider so it can be selected
via the standard active-provider pipeline.  Unlike API-based providers
(Spotify, YouTube Music), this provider does not perform server-side search.
Instead it:

1. Builds a search URL using the recipe system
2. Returns a synthetic TrackSummary pointing at that URL
3. Returns a StreamInfo with ``requires_embedded_player=True``

The actual playback is handled by BrowserPlaybackController in the
QWebEngineView when the playback system encounters the embedded-player
flag.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote_plus

from pydantic import HttpUrl

from core.logging_config import get_logger

from ..base import MusicProvider
from ..models import (
    AuthContext,
    AuthSession,
    PaginatedResult,
    PlaybackContext,
    PlaylistSummary,
    ProviderCapabilities,
    ProviderFeature,
    ProviderName,
    SearchResults,
    StreamInfo,
    TrackSummary,
)
from ..registry import auto_register

logger = get_logger(__name__)

# Default recipe target for browser-native search
_DEFAULT_RECIPE = "youtube_music"


def _get_recipe_search_url(query: str, recipe_name: str = _DEFAULT_RECIPE) -> str:
    """Build a search URL using the recipe system.

    Falls back to a hardcoded YouTube Music search URL if the recipe
    system is unavailable.

    Args:
        query: Raw search query text.
        recipe_name: Recipe identifier (default: youtube_music).

    Returns:
        Fully-formed search URL.
    """
    try:
        from music.providers.browser.recipes import get_recipe

        recipe = get_recipe(recipe_name)
        return recipe.get_search_url(query)
    except Exception:
        logger.exception(
            "Failed to get recipe %s, using fallback URL template",
            recipe_name,
        )
        # Fallback: hardcoded YouTube Music search URL
        return "https://music.youtube.com/search?q=%s" % quote_plus(query)


@auto_register(ProviderName.BROWSER)
class BrowserMusicProvider(MusicProvider[None]):
    """Browser-native music provider.

    Uses QWebEngineView to play music from any web service.
    Search is done by navigating to the provider's search page.
    Playback is controlled via W3C MediaSession API.

    This provider is gated behind the ``browser_provider_enabled`` feature
    flag in settings.  It must be explicitly enabled before use.
    """

    display_name = "Browser"
    provider_name = ProviderName.BROWSER

    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        """Browser provider uses the user's existing web session.

        No additional OAuth flow is required -- the user authenticates
        directly in the QWebEngineView (cookies persist across sessions).
        """
        return AuthSession(
            is_linked=True,
            requires_redirect=False,
            scopes=[],
        )

    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PaginatedResult[PlaylistSummary]:
        """Browser provider does not support playlist listing.

        Playlists are accessed by navigating to the service's playlist page
        within the browser.
        """
        return PaginatedResult(items=[], next_cursor=None, total=0)

    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResults:
        """Build a synthetic search result pointing to a browser search page.

        The browser provider doesn't do API-level search.  Instead, it
        creates a single TrackSummary whose ``extras["search_url"]`` points
        to the service's search page.  When this track is played, the
        playback system loads the URL in QWebEngineView and the recipe JS
        clicks the first result.

        Because that click is blind, at resolution time there is genuinely no
        way to know the track that will actually play.  The synthetic result is
        therefore honest about that: ``title`` carries the user's query only as
        a display placeholder and is flagged ``extras["title_unverified"] =
        True`` so downstream consumers can tell a query-echo apart from a
        confirmed match, and ``artist_name`` is left empty rather than a
        fabricated label (there is no known artist yet).  The real title/artist
        are corrected once the embedded player reports the track that started
        (see the MediaSession metadata listener in ``queue_manager.py``).

        Args:
            user_id: User identifier.
            query: Search query string.
            limit: Ignored (always returns one synthetic result).
            cursor: Ignored (no pagination).

        Returns:
            SearchResults with a single synthetic TrackSummary.
        """
        search_url = _get_recipe_search_url(query)
        track_id = "browser-%s" % uuid.uuid4().hex[:12]

        track = TrackSummary(
            id=track_id,
            title=query,
            artist_name="",
            album_name=None,
            duration_ms=None,
            is_explicit=False,
            artwork_url=None,
            provider_track_id=track_id,
            extras={
                "search_url": search_url,
                "query": query,
                "recipe": _DEFAULT_RECIPE,
                # The title is the raw query echoed back for display, NOT a
                # resolved track title -- the blind first-result click means we
                # cannot know the real track until it starts playing. Mark it so
                # _classify_query_match and any future consumer never mistake a
                # query-echo for a confirmed "exact" match. (extras values are
                # strings; the flag is read via bool() downstream.)
                "title_unverified": "true",
            },
        )

        logger.info(
            "Browser provider created synthetic track for query=%s url=%s",
            query,
            search_url,
        )

        return SearchResults(
            items=[track],
            next_cursor=None,
            total=1,
            query=query,
        )

    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        """Return a StreamInfo that signals embedded browser playback.

        The URL is the search page URL from the track extras.  The
        ``requires_embedded_player`` flag tells the playback system to
        route this to the QWebEngineView / BrowserPlaybackController
        rather than to VLC or another streaming backend.

        Args:
            user_id: User identifier.
            track: TrackSummary (from search_tracks).
            playback: Playback context hints.

        Returns:
            StreamInfo with requires_embedded_player=True.
        """
        search_url = track.extras.get("search_url", "")
        if not search_url:
            # Reconstruct from query if extras are missing
            query = track.extras.get("query", track.title)
            search_url = _get_recipe_search_url(query)

        recipe_name = track.extras.get("recipe", _DEFAULT_RECIPE)

        # Long expiry -- the search URL is stable
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        logger.info(
            "Browser provider resolving stream: url=%s recipe=%s",
            search_url,
            recipe_name,
        )

        return StreamInfo(
            url=cast(HttpUrl, search_url),
            expires_at=expires_at,
            drm=None,
            content_type="text/html",
            bitrate_kbps=320,
            requires_embedded_player=True,
            metadata={
                "provider": self.provider_name.value,
                "recipe": recipe_name,
                "query": track.extras.get("query", track.title),
                "playback_mode": "browser_native",
            },
        )

    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        """Browser provider does not pre-fetch artwork.

        Artwork is extracted from the page via MediaSession API once
        playback starts.
        """
        return None

    def provider_capabilities(self) -> ProviderCapabilities:
        """Declare browser-native provider capabilities."""
        return ProviderCapabilities(
            name=self.provider_name,
            features=[ProviderFeature.VIDEO_PLAYBACK],
            max_bitrate_kbps=320,
            supports_explicit_filter=False,
            supports_offline_downloads=False,
            notes="Browser-native playback via QWebEngineView + MediaSession API",
        )
