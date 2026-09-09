"""YouTube Music compatibility provider backed by browser-based YouTube paths.

The official YouTube account API path is retired for launch. This module keeps
the public ``YouTubeMusicProvider`` surface importable, but search/playback now
delegates to ``YouTubeIFrameProvider`` and account playlist sync returns empty
instead of making external Google API calls.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from utils.circuit_breaker import CircuitBreaker

from .base import MusicProvider
from .errors import MusicProviderUnavailableError
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
from .youtube_core import clear_shared_search_cache, get_shared_cache_stats, get_shared_search_cache
from .youtube_iframe import YouTubeIFrameProvider
from .youtube_music_auth import _get_access_token, _not_configured

logger = get_logger("viola.music.providers.youtube_music")

_youtube_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, success_threshold=1)


def _refresh_access_token(*args: Any, **kwargs: Any) -> None:
    """Compatibility stub for older tests/imports; account API refresh is retired."""

    _ = (args, kwargs)
    return None


def _youtube_account_api_disabled(operation: str) -> MusicProviderUnavailableError:
    return MusicProviderUnavailableError(
        "YouTube account playlist operations are disabled for launch. Use browser-based YouTube playback instead.",
        technical_details={
            "root_cause": "youtube_browser_provider_required",
            "kind": "PROVIDER_DISABLED",
            "provider": "youtube_music",
            "operation": operation,
            "required_path": "youtube_iframe.browser_search",
        },
    )


@auto_register(ProviderName.YOUTUBE_MUSIC)
class YouTubeMusicProvider(MusicProvider[None]):
    """Compatibility wrapper over the browser-only YouTube IFrame provider."""

    display_name = "YouTube Music"
    provider_name = ProviderName.YOUTUBE_MUSIC

    def __init__(self) -> None:
        super().__init__()
        self._iframe_provider = YouTubeIFrameProvider()
        self._cache = get_shared_search_cache()

    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        """Browser-based playback/search does not require Google account OAuth."""

        _ = (user_id, context)
        return AuthSession(
            is_linked=True,
            requires_redirect=False,
            scopes=[],
            metadata={"auth_mode": "browser_only", "account_playlist_sync": "disabled"},
        )

    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PaginatedResult[PlaylistSummary]:
        """Return no account playlists; the external account API is retired."""

        _ = (user_id, limit, cursor)
        return PaginatedResult(items=[], next_cursor=None, total=0)

    def get_playlist_items(
        self,
        playlist_id: str,
        user_id: str,
        *,
        limit: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[TrackSummary], str | None]:
        """Fail closed for account playlist item sync."""

        _ = (user_id, limit, page_token)
        _ = playlist_id
        raise _youtube_account_api_disabled("retired_playlist_lookup")

    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResults:
        return self._iframe_provider.search_tracks(user_id, query, limit=limit, cursor=cursor)

    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        stream = self._iframe_provider.resolve_stream(user_id, track, playback=playback)
        metadata = dict(stream.metadata or {})
        metadata["provider"] = self.provider_name.value
        return StreamInfo(
            url=stream.url,
            expires_at=stream.expires_at,
            drm=stream.drm,
            content_type=stream.content_type,
            bitrate_kbps=stream.bitrate_kbps,
            requires_embedded_player=stream.requires_embedded_player,
            metadata=metadata,
        )

    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        return self._iframe_provider.fetch_artwork(track, width=width, height=height)

    def evict_search_cache(self, query: str, user_id: str) -> bool:
        cache_key = f"{user_id}:{query}:1"
        return self._cache.evict(cache_key)

    def clear_search_cache(self) -> int:
        return clear_shared_search_cache()

    def search_cache_stats(self) -> dict[str, Any]:
        return get_shared_cache_stats()

    def provider_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.provider_name,
            features=[ProviderFeature.VIDEO_PLAYBACK],
            max_bitrate_kbps=None,
            supports_explicit_filter=False,
            supports_offline_downloads=False,
            notes="Browser-based embedded YouTube playback; no account API calls.",
        )


def get_youtube_music_provider() -> YouTubeMusicProvider:
    return YouTubeMusicProvider()


def evict_youtube_search_cache(query: str, user_id: str) -> bool:
    return get_youtube_music_provider().evict_search_cache(query, user_id)


def clear_youtube_search_cache() -> int:
    return clear_shared_search_cache()


def get_youtube_search_cache_stats() -> dict[str, Any]:
    return get_shared_cache_stats()


__all__ = [
    "YouTubeMusicProvider",
    "_get_access_token",
    "_not_configured",
    "_refresh_access_token",
    "_youtube_circuit",
    "clear_youtube_search_cache",
    "evict_youtube_search_cache",
    "get_youtube_music_provider",
    "get_youtube_search_cache_stats",
]
