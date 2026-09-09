"""
YouTube Music Resolution Service

Extracted resolution service for YouTube Music-specific track resolution.
Handles provider search, video_id extraction, stream resolution, and metadata building.

This service centralizes YouTube Music resolution logic for the MusicPlayer
facade, making it easier to test, maintain, and extend.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logging_config import get_logger
from music.providers.errors import (
    MusicProviderUnavailableError,
    ProviderNotConfiguredError,
    ProviderNotRegistered,
)
from music.providers.models import (
    PlaybackContext,
    ProviderName,
    StreamInfo,
    TrackSummary,
)
from music.providers.registry import get_provider_class
from music.providers.youtube_availability import YouTubeAvailabilityChecker
from music.resolution.helpers import ResolutionMetadata
from music.resolution.youtube_resolver_search import YouTubeResolverSearchOperations


class YouTubeMusicResolver:
    """
    YouTube Music-specific resolution service.

    Handles:
    1. Search via browser-backed YouTube search
    2. Playability filtering and video ID extraction from search results
    3. Stream resolution (watch URL generation)
    4. ResolutionMetadata construction with all required metadata

    This service is responsible for the complete resolution flow from query
    to ResolutionMetadata, ensuring TOS compliance by requiring search results
    and embedded-player availability before playback.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        availability_checker: YouTubeAvailabilityChecker | None = None,
    ):
        """
        Initialize the YouTube Music resolver.

        Args:
            logger: Optional logger instance. If not provided, a default logger
                   will be created using the module name.
        """
        self._logger = logger or get_logger(__name__)
        self._search_ops = YouTubeResolverSearchOperations(
            self._logger,
            availability_checker=availability_checker,
        )

    def resolve_query(
        self,
        query: str,
        source: str,
        user_id: str,
        active_provider_id: str | None = None,
    ) -> ResolutionMetadata:
        """
        Single entry point for YouTube Music resolution.

        Flow:
        1. Route to youtube_iframe provider (browser search, no OAuth needed)
        2. Search tracks via provider.search_tracks() (browser-based)
        3. Verify candidates in search order and extract video_id from first playable result
        4. Resolve stream via provider.resolve_stream()
        5. Build ResolutionMetadata with all required metadata

        Browser Search Migration: Search always routes through YouTubeIFrameProvider
        which uses BrowserSearchEngine (hidden QWebEngineView). No OAuth tokens or
        API keys are needed for search. The active_provider_id setting ("youtube_music")
        is normalized to "youtube_iframe" for search operations.

        Args:
            query: Search query string
            source: Source type (typically "ytsearch1" for provider resolution)
            active_provider_id: Active provider ID. Both "youtube_music" and
                              "youtube_iframe" are normalized to "youtube_iframe"
                              for search (browser search, no OAuth needed).
            user_id: User ID for provider operations

        Returns:
            ResolutionMetadata with all required fields populated

        Raises:
            MusicProviderUnavailableError: If provider is unavailable or misconfigured
            MusicTrackNotFoundError: If no tracks found for query
            ProviderNotRegistered: If provider class is not registered
        """
        # Validate source type (only text queries, not URLs or local files)
        if source == "url" or query.startswith(("http://", "https://")):
            self._logger.warning(
                "youtube_resolver.resolve_query: EARLY_EXIT reason=url_source query=%r source=%s",
                query,
                source,
            )
            raise ValueError("YouTube Music resolver does not handle URL sources. Use direct URL resolution instead.")
        if source == "local":
            self._logger.warning(
                "youtube_resolver.resolve_query: EARLY_EXIT reason=local_source query=%r source=%s",
                query,
                source,
            )
            raise ValueError("YouTube Music resolver does not handle local file sources.")

        # BROWSER SEARCH MIGRATION: Always use youtube_iframe for search.
        # YouTubeIFrameProvider.search_tracks() uses browser-based search
        # (BrowserSearchEngine) which requires no OAuth tokens or API keys.
        # The active_provider_id setting is respected for non-search operations
        # but search always routes through youtube_iframe.
        if active_provider_id in (None, "youtube_music"):
            self._logger.info(
                "youtube_resolver: routing search through youtube_iframe "
                "(browser search, no OAuth needed) for query=%r original_provider=%s",
                query[:50],
                active_provider_id,
            )
            active_provider_id = "youtube_iframe"

        # Only handle supported providers
        if active_provider_id not in ("youtube_music", "youtube_iframe"):
            self._logger.warning(
                "youtube_resolver.resolve_query: EARLY_EXIT reason=provider_mismatch active_provider_id=%s query=%r source=%s",
                active_provider_id,
                query,
                source,
            )
            raise MusicProviderUnavailableError(
                f"Active provider '{active_provider_id}' is not supported by YouTube Music resolver. "
                f"Only 'youtube_music' and 'youtube_iframe' are supported.",
                technical_details={
                    "root_cause": "provider_mismatch",
                    "provider": active_provider_id,
                    "query": query[:50],
                },
            )

        # Get provider instance (always youtube_iframe for search)
        provider = self._get_provider_instance(active_provider_id, query)

        # Search tracks via provider
        track = self._search_tracks(provider, query, user_id, active_provider_id)

        # Extract video_id from track (TOS compliance: must come from browser search)
        video_id = self._extract_video_id(track, query)

        # Resolve stream
        stream = self._resolve_stream(provider, track, user_id, query)

        # Build ResolutionMetadata
        metadata = self._build_resolution_metadata(
            track=track,
            stream=stream,
            video_id=video_id,
            query=query,
            active_provider_id=active_provider_id,
        )

        return metadata

    def _get_provider_instance(self, active_provider_id: str, query: str) -> Any:  # MusicProvider protocol
        """
        Get provider instance for the active provider ID.

        Args:
            active_provider_id: Provider ID ("youtube_music" or "youtube_iframe")
            query: Query string for error context

        Returns:
            Provider instance

        Raises:
            ProviderNotRegistered: If provider class is not registered
            MusicProviderUnavailableError: If provider instantiation fails
        """
        try:
            provider_name = (
                ProviderName.YOUTUBE_MUSIC if active_provider_id == "youtube_music" else ProviderName.YOUTUBE_IFRAME
            )
            provider_cls = get_provider_class(provider_name)
            provider = provider_cls()
            self._logger.info(
                "youtube_resolver._get_provider_instance: provider_class=%s provider_instance=%s",
                provider_cls.__name__,
                type(provider).__name__,
            )
            return provider
        except ProviderNotRegistered as exc:
            self._logger.warning(
                "youtube_resolver._get_provider_instance: EARLY_EXIT reason=provider_not_registered active_provider_id=%s query=%r error=%s",
                active_provider_id,
                query,
                exc,
            )
            raise MusicProviderUnavailableError(
                "YouTube Music provider is not registered. This is a system error. Please check your installation.",
                technical_details={
                    "root_cause": "provider_not_registered",
                    "provider": active_provider_id,
                    "query": query[:50],
                },
            ) from exc

    def _validate_token(self, user_id: str, query: str) -> None:
        """
        Deprecated token validator for older callers.

        YouTube Music search/playback now routes through youtube_iframe browser
        auth. OAuth-token validation is not part of the runtime path; this
        method fails closed if a legacy caller still invokes it.

        Args:
            user_id: User ID for token lookup
            query: Query string for error context

        Raises:
            MusicProviderUnavailableError: If token is missing or invalid
        """
        try:
            from music.providers.youtube_music_auth import _get_access_token

            access_token = _get_access_token(user_id)
            if not access_token:
                self._logger.warning(
                    "youtube_resolver._validate_token: EARLY_EXIT reason=no_user_token query=%r user_id=%s",
                    query,
                    user_id,
                )
                raise MusicProviderUnavailableError(
                    "YouTube Music OAuth is disabled. Use browser-based YouTube Music sign-in.",
                    technical_details={
                        "root_cause": "no_user_token",
                        "provider": "youtube_music",
                        "user_id": user_id,
                    },
                )
            self._logger.info(
                "youtube_resolver._validate_token: token_check passed has_token=%s user_id=%s",
                bool(access_token),
                user_id,
            )
        except MusicProviderUnavailableError:
            raise
        except Exception as exc:
            self._logger.exception(
                "youtube_resolver._validate_token: EARLY_EXIT reason=token_check_failed query=%r error=%s",
                query,
                exc,
            )
            raise MusicProviderUnavailableError(
                "Failed to verify disabled YouTube Music OAuth state. Use browser-based YouTube Music sign-in.",
                technical_details={
                    "root_cause": "token_check_failed",
                    "provider": "youtube_music",
                    "error": str(exc),
                },
            ) from exc

    def _search_tracks(self, provider: Any, query: str, user_id: str, active_provider_id: str) -> TrackSummary:
        """
        Search for tracks using provider.search_tracks().

        TOS COMPLIANCE: This MUST happen before video_id resolution and playback.
        Browser search must return video_id in the results.

        Args:
            provider: Provider instance
            query: Search query
            user_id: User ID for provider operations
            active_provider_id: Active provider ID for logging

        Returns:
            First playable TrackSummary from search results

        Raises:
            MusicProviderUnavailableError: If search fails or provider is unavailable
            MusicTrackNotFoundError: If no tracks found (provider is healthy but no results)
        """
        return self._search_ops.search_tracks(provider, query, user_id, active_provider_id)

    def _extract_video_id(self, track: TrackSummary, query: str) -> str:
        """
        Extract video_id from track (multiple fallback paths).

        TOS COMPLIANCE: video_id must be present in search result.
        This method tries multiple paths to extract it:
        1. track.provider_track_id
        2. track.extras.get("video_id")
        3. track.id

        Args:
            track: TrackSummary from search results
            query: Query string for error context

        Returns:
            Video ID string (non-empty)

        Raises:
            MusicProviderUnavailableError: If video_id cannot be extracted
        """
        video_id_from_search = track.provider_track_id or track.extras.get("video_id") or track.id
        if not video_id_from_search:
            self._logger.error(
                "YTM_TOS_VIOLATION: Search result missing video_id - browser search must return video_id. "
                "track_id=%s title=%r query=%r",
                track.id,
                track.title,
                query,
            )
            raise MusicProviderUnavailableError(
                f"Search result for '{query}' is missing video_id. This is a system error.",
                technical_details={
                    "root_cause": "search_result_missing_video_id",
                    "provider": "youtube_music",
                    "track_id": track.id,
                    "query": query[:50],
                },
            )

        self._logger.info(
            "YTM_TOS_VIDEO_ID_RESOLVED: video_id=%s title=%r query=%r source=browser_search",
            video_id_from_search,
            track.title,
            query,
        )
        return video_id_from_search

    def _resolve_stream(self, provider: Any, track: TrackSummary, user_id: str, query: str) -> StreamInfo:
        """
        Resolve stream URL for track via provider.resolve_stream().

        Args:
            provider: Provider instance
            track: TrackSummary to resolve
            user_id: User ID for provider operations
            query: Query string for error context

        Returns:
            StreamInfo with URL and playback requirements

        Raises:
            MusicProviderUnavailableError: If stream resolution fails
        """
        playback_context = PlaybackContext(
            device_id=None,
            preferred_bitrate_kbps=None,
            allow_video=True,
        )
        self._logger.info(
            "youtube_resolver._resolve_stream: calling resolve_stream track_id=%s title=%r user_id=%s",
            track.id,
            track.title,
            user_id,
        )
        try:
            stream = provider.resolve_stream(user_id, track, playback=playback_context)
            self._logger.info(
                "youtube_resolver.resolve_stream.result: track_id=%s title=%r has_url=%s embedded=%s",
                track.id,
                track.title,
                bool(stream.url),
                stream.requires_embedded_player,
            )
            return stream
        except ProviderNotConfiguredError as exc:
            # Handle ProviderNotConfiguredError first (more specific than MusicProviderUnavailableError)
            error_msg = str(exc)
            root_cause = "provider_not_configured"
            self._logger.warning(
                "youtube_resolver._resolve_stream: resolve_stream failed track_id=%s title=%r user_id=%s root_cause=%s error=%s",
                track.id,
                track.title,
                user_id,
                root_cause,
                error_msg[:100],
            )
            raise MusicProviderUnavailableError(
                f"Couldn't play '{track.title}'. Try a different song.",
                technical_details={
                    "root_cause": root_cause,
                    "provider": "youtube_music",
                    "track_id": track.id,
                    "user_id": user_id,
                    "stage": "resolve_stream",
                },
            ) from exc
        except MusicProviderUnavailableError:
            # Re-raise as-is (already has root_cause)
            raise
        except Exception as exc:
            # Catch any unexpected exceptions from resolve_stream
            exc_type = type(exc).__name__
            exc_repr = repr(exc)
            self._logger.exception(
                "youtube_resolver._resolve_stream: resolve_stream UNEXPECTED_EXCEPTION track_id=%s title=%r user_id=%s exc_type=%s exc=%r",
                track.id,
                track.title,
                user_id,
                exc_type,
                exc_repr,
            )
            raise MusicProviderUnavailableError(
                f"Couldn't play '{track.title}'. Try a different song.",
                technical_details={
                    "root_cause": "resolve_stream_exception",
                    "provider": "youtube_music",
                    "track_id": track.id,
                    "user_id": user_id,
                    "stage": "resolve_stream",
                    "exc_type": exc_type,
                    "exc": exc_repr,
                },
            ) from exc

    def _build_resolution_metadata(
        self,
        track: TrackSummary,
        stream: StreamInfo,
        video_id: str,
        query: str,
        active_provider_id: str,
    ) -> ResolutionMetadata:
        """
        Build ResolutionMetadata from track, stream, and video_id.

        This method constructs a complete ResolutionMetadata object with all
        required fields, including capabilities, extras, and TOS compliance flags.

        Args:
            track: TrackSummary from search results
            stream: StreamInfo from stream resolution
            video_id: Extracted video ID
            query: Original search query
            active_provider_id: Active provider ID

        Returns:
            Complete ResolutionMetadata ready for QueueItem conversion
        """
        # Extract artwork URL
        artwork_url = None
        if track.artwork_url:
            artwork_url = str(track.artwork_url)
        elif video_id:
            artwork_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

        # Extract playback_mode from stream metadata
        metadata_dict = stream.metadata if isinstance(stream.metadata, dict) else {}
        playback_mode = metadata_dict.get("playback_mode") if metadata_dict else None
        requires_embedded = bool(stream.requires_embedded_player)
        embedded_flag = (
            bool(metadata_dict.get("embedded")) if metadata_dict and "embedded" in metadata_dict else requires_embedded
        )
        video_playback = metadata_dict.get("video_playback", True)
        item_capabilities = {
            "requires_embedded_player": requires_embedded,
            "embedded": embedded_flag,
            "video_playback": video_playback,
        }

        # TOS COMPLIANCE: Log complete flow - browser search -> video_id -> embedded player
        self._logger.info(
            "YTM_TOS_COMPLETE_FLOW: provider=%s query=%s video_id=%s title=%s playback_mode=%s "
            "flow=browser_search->availability_check->video_id->embedded_player",
            active_provider_id,
            query[:50],
            video_id,
            track.title,
            playback_mode or "none",
        )

        # Build extras dict with playback_mode
        extras: dict[str, str | bool] = {}
        if playback_mode:
            extras["playback_mode"] = playback_mode
        elif embedded_flag:
            # Default playback_mode when embedded but metadata didn't provide it
            extras["playback_mode"] = "embedded_webview"
        if requires_embedded:
            extras["requires_embedded_player"] = True

        return ResolutionMetadata(
            url=str(stream.url),
            title=track.title,
            source=(active_provider_id or "url"),
            video_id=video_id,
            artist=track.artist_name,
            provider=active_provider_id,
            resolver_path=f"provider.{active_provider_id}",
            artwork_url=artwork_url,
            thumbnail_url=artwork_url,
            capabilities=item_capabilities,
            extras=extras,  # Include playback_mode in extras
        )
