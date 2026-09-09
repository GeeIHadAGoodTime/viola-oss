"""
YouTube IFrame Provider

- Provider ID: "youtube_iframe"
- Playback mode: "embedded_iframe_webview"
- Transport: Official YouTube IFrame Player API inside an embedded WebView.

This provider uses browser-based search exclusively. No Google account API
or OAuth tokens are needed for search operations. The BrowserSearchEngine
(hidden QWebEngineView) scrapes YouTube search results directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import HttpUrl

from core.exceptions import ServiceUnavailableError
from core.logging_config import get_logger
from music.youtube_embed import extract_video_id  # Canonical implementation

from .base import MusicProvider
from .models import (
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
from .registry import auto_register
from .youtube_availability import check_youtube_video_playable
from .youtube_core import YOUTUBE_VIDEO_ID_RE, get_shared_search_cache

logger = get_logger("viola.music.providers.youtube_iframe")

# Canonical watch URL
YOUTUBE_WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"

# Process-wide video_id -> metadata cache. Populated whenever a successful
# browser search returns results (each TrackSummary's id, title, artist,
# duration, artwork). Looked up in the bare-id-as-query path so playing by
# track_uri (e.g. media tool's play mode) shows the real track title and
# artist instead of the generic "YouTube video" placeholder.
# Bounded to avoid unbounded growth; oldest entries evicted via FIFO.
_VIDEO_METADATA_CACHE: dict[str, dict[str, object]] = {}
_VIDEO_METADATA_CACHE_MAX = 1000


def _remember_video_metadata(item: TrackSummary) -> None:
    """Record a search-result TrackSummary's display metadata by video_id."""
    vid = (item.provider_track_id or item.id or "").strip()
    if not vid or (item.title in ("", "YouTube video") and item.artist_name in ("", "YouTube")):
        return
    _VIDEO_METADATA_CACHE[vid] = {
        "title": item.title,
        "artist_name": item.artist_name,
        "album_name": item.album_name,
        "duration_ms": item.duration_ms,
        "artwork_url": item.artwork_url,
    }
    if len(_VIDEO_METADATA_CACHE) > _VIDEO_METADATA_CACHE_MAX:
        # Drop oldest 10% to amortize trim cost.
        excess = len(_VIDEO_METADATA_CACHE) - _VIDEO_METADATA_CACHE_MAX
        for key in list(_VIDEO_METADATA_CACHE.keys())[: max(excess, _VIDEO_METADATA_CACHE_MAX // 10)]:
            _VIDEO_METADATA_CACHE.pop(key, None)


def _get_settings():
    try:
        from config.settings import get_settings
    except Exception as e:
        logger.exception("Failed to import settings: %s", e)
        return None
    return get_settings()


def _is_browser_search_enabled() -> bool:
    settings = _get_settings()
    if not settings:
        return True
    return bool(getattr(settings, "browser_search_enabled", True))


def _canonical_watch_url(video_id: str) -> str:
    return YOUTUBE_WATCH_URL_TEMPLATE.format(video_id=video_id)


@auto_register(ProviderName.YOUTUBE_IFRAME)
class YouTubeIFrameProvider(MusicProvider[None]):
    """Provider for plain YouTube videos via the IFrame Player API.

    Browser search is the only search path. No Google account API or OAuth
    tokens are needed for search operations.
    """

    display_name = "YouTube"
    provider_name = ProviderName.YOUTUBE_IFRAME

    def __init__(self):
        """Initialize with shared search cache."""
        super().__init__()

        # Shared persistent cache for browser search results
        self._cache = get_shared_search_cache()

    # Authentication is not used for public video search with API key.
    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        return AuthSession(is_linked=True, requires_redirect=False, scopes=[])

    # Playlist listing requires OAuth (user's YouTube account). YouTubeIFrameProvider
    # uses API-key auth only; OAuth support is tracked in GOALS.md under P4.
    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PaginatedResult[PlaylistSummary]:
        return PaginatedResult(items=[], next_cursor=None, total=0)

    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResults:
        """
        Resolve query to YouTube videos.

        - If query is a URL or bare ID, return that as the top result with minimal metadata.
        - Else, use browser-based search (no API quota, no OAuth tokens needed).

        Browser Search Migration: this is the sole search path. There is no
        remote API fallback; playlist resolution has its own browser-first path
        in PlaylistManager.

        Features:
        - Shared cache: browser results are stored in the persistent SQLite cache
          (30-day sliding TTL), so repeat queries skip the browser
        - Browser search: zero API quota, uses hidden QWebEngineView
        """
        # Direct URL / bare ID path - no API call needed
        vid = extract_video_id(query)
        if vid:
            # Look up enriched metadata from prior browser searches so playing
            # by track_uri (media tool play mode) shows the real title/artist.
            cached_meta = _VIDEO_METADATA_CACHE.get(vid, {})
            track = TrackSummary(
                id=vid,
                title=cast("str", cached_meta.get("title") or "YouTube video"),
                artist_name=cast("str", cached_meta.get("artist_name") or "YouTube"),
                album_name=cast("str | None", cached_meta.get("album_name")),
                duration_ms=cast("int | None", cached_meta.get("duration_ms")),
                artwork_url=cast("str | None", cached_meta.get("artwork_url")),
                provider_track_id=vid,
                extras={"video_id": vid},
            )
            return SearchResults(items=[track], next_cursor=None, total=1, query=query)

        # Check shared cache before any browser call.
        cache_key = f"{user_id}:{query}:{limit}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug(
                "search_tracks: cache hit for query=%r (skipping browser search)",
                query,
            )
            # Re-populate the in-process metadata cache (lost on restart)
            # so subsequent bare-id play calls find real title/artist.
            for item in cached.items:
                _remember_video_metadata(item)
            return cached

        # Browser search path — the only search path (no API fallback).
        browser_result = self._search_tracks_browser(query, limit)
        if browser_result is not None:
            logger.info(
                "search_tracks: method=browser query=%r results=%d first_id=%s",
                query,
                len(browser_result.items),
                browser_result.items[0].id if browser_result.items else "none",
            )
            # Populate shared cache so repeat queries within TTL skip browser search
            if browser_result.items:
                self._cache.put(cache_key, browser_result)
            return browser_result

        # Browser search returned no results or is unavailable.
        logger.warning(
            "search_tracks: browser search returned no usable results for query=%r",
            query,
        )
        return SearchResults(items=[], next_cursor=None, total=0, query=query)

    def _search_tracks_browser(self, query: str, limit: int) -> SearchResults | None:
        """Try browser-based YouTube search.

        Returns None when browser search is disabled or yields no usable results.
        Raises ServiceUnavailableError when the browser search engine fails.
        """
        if not _is_browser_search_enabled():
            return None

        try:
            from .browser_search import BrowserSearchEngine
        except ImportError as exc:
            logger.exception("search_tracks: browser search import failed for query=%r", query)
            raise ServiceUnavailableError(
                "youtube browser search",
                str(exc) or "browser search engine import failed",
            ) from exc

        try:
            engine = BrowserSearchEngine.get_instance()
            raw_results = engine.search(query, limit=limit)
            if not raw_results:
                logger.warning(
                    "search_tracks: browser search returned empty for query=%r",
                    query,
                )
                return None

            # Map browser results to TrackSummary (exact same structure as API)
            items: list[TrackSummary] = []
            for item in raw_results:
                video_id = item.get("video_id", "")
                if not video_id:
                    continue
                thumb = item.get("thumbnail_url") or None
                # Validate URL then convert to str — TrackSummary.artwork_url is str|None,
                # and Pydantic 2.12 rejects HttpUrl objects passed to str fields
                # (string_type ValidationError). Must call str() explicitly.
                artwork_url: str | None = None
                if thumb and thumb.startswith("http"):
                    try:
                        artwork_url = str(HttpUrl(thumb))
                    except Exception:
                        artwork_url = None
                items.append(
                    TrackSummary(
                        id=video_id,
                        # Use `or` fallback (not dict default) — browser may return
                        # explicit None for title/channel, which .get(k, default) won't catch.
                        title=item.get("title") or "YouTube video",
                        artist_name=item.get("channel") or "YouTube",
                        album_name=None,
                        duration_ms=None,
                        is_explicit=False,
                        artwork_url=artwork_url,
                        provider_track_id=video_id,
                        extras={"video_id": video_id},
                    )
                )

            if not items:
                return None

            # Populate the video_id->metadata cache so subsequent bare-id
            # play_music calls (e.g. from media tool play mode) can show the
            # real title/artist instead of "YouTube video" placeholder.
            for item in items:
                _remember_video_metadata(item)

            return SearchResults(
                items=items,
                next_cursor=None,
                total=len(items),
                query=query,
            )
        except TimeoutError as exc:
            logger.exception("search_tracks: browser search timed out for query=%r", query)
            raise ServiceUnavailableError(
                "youtube browser search",
                str(exc) or "search timed out",
            ) from exc
        except RuntimeError as exc:
            logger.exception("search_tracks: browser search runtime failure for query=%r", query)
            raise ServiceUnavailableError(
                "youtube browser search",
                str(exc) or "runtime failure",
            ) from exc
        except Exception as exc:
            logger.exception("search_tracks: browser search failed for query=%r", query)
            raise ServiceUnavailableError(
                "youtube browser search",
                str(exc) or type(exc).__name__,
            ) from exc

    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        """
        Build StreamInfo pointing to YouTube watch URL for embedded IFrame playback.
        """
        video_id = track.provider_track_id or track.extras.get("video_id") or track.id
        if not isinstance(video_id, str) or not YOUTUBE_VIDEO_ID_RE.match(video_id):
            raise ValueError("youtube_iframe.resolve_stream requires a valid video_id")

        if track.extras.get("availability_checked") == "true":
            playable = track.extras.get("availability_playable") == "true"
            embeddable = track.extras.get("availability_embeddable") == "true"
            if not playable or not embeddable:
                reason = track.extras.get("availability_reason", "availability_not_playable")
                raise ServiceUnavailableError("youtube iframe playback", reason)
        else:
            availability = check_youtube_video_playable(video_id)
            if not availability.playable or not availability.embeddable:
                raise ServiceUnavailableError("youtube iframe playback", availability.reason)

        watch_url = _canonical_watch_url(video_id)
        # Long expiry; these links are stable
        expires_at = datetime.now(UTC) + timedelta(days=365)

        # Critical: indicate iframe/webview playback mode
        return StreamInfo(
            url=cast(HttpUrl, watch_url),
            expires_at=expires_at,
            drm=None,
            content_type="video/youtube",
            bitrate_kbps=None,
            requires_embedded_player=True,
            metadata={
                "video_id": video_id,
                "provider": self.provider_name.value,
                "stream_token": video_id,
                "playback_mode": "embedded_iframe_webview",
            },
        )

    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        video_id = track.provider_track_id or track.extras.get("video_id") or track.id
        if not isinstance(video_id, str) or not YOUTUBE_VIDEO_ID_RE.match(video_id):
            return None
        return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.provider_name,
            features=[ProviderFeature.VIDEO_PLAYBACK],
            max_bitrate_kbps=None,
            supports_explicit_filter=False,
            supports_offline_downloads=False,
            notes="Uses embedded IFrame Player; no stream extraction.",
        )
