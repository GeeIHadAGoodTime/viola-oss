"""
Base contract for compliant music providers.

Each production provider must implement the :class:`MusicProvider`
abstract class defined here. The interface purposely mirrors the
capabilities expected by the queue engine and higher-level orchestration
layers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from core.logging_config import get_logger

from .models import (
    AuthContext,
    AuthSession,
    PaginatedResult,
    PlaybackContext,
    PlaylistSummary,
    ProviderCapabilities,
    ProviderName,
    SearchResults,
    StreamInfo,
    TrackSummary,
)

logger = get_logger(__name__)

PlaylistPage = PaginatedResult[PlaylistSummary]
TrackSearchPage = SearchResults
T = TypeVar("T")


class MusicProvider(ABC, Generic[T]):
    """
    Abstract base class all music providers must implement.

    Providers are expected to be stateless; any per-user credentials
    should be managed by external services (see Batch B).  Implementations
    may keep lightweight caches but must stay thread-safe.
    """

    #: Human-readable provider name (e.g. "Spotify")
    display_name: str
    #: Stable provider identifier used in configuration/registry.
    provider_name: ProviderName

    def __init__(self) -> None:
        """Initialize provider with validated capabilities."""
        # Validate capabilities on initialization
        self._validate_capabilities()

    def _validate_capabilities(self) -> None:
        """
        Validate provider capabilities table.

        Ensures that provider_capabilities() returns a valid ProviderCapabilities
        object that matches the provider's actual implementation.
        """
        try:
            capabilities = self.provider_capabilities()

            # Validate return type
            if not isinstance(capabilities, ProviderCapabilities):
                raise TypeError(
                    f"provider_capabilities() must return ProviderCapabilities, got {type(capabilities).__name__}"
                )

            # Validate provider name matches
            if capabilities.name != self.provider_name:
                logger.warning(
                    "Capability provider_name mismatch: expected %s, got %s",
                    self.provider_name.value,
                    capabilities.name.value,
                )
                # Auto-correct the mismatch
                capabilities.name = self.provider_name

            # Validate bitrate is non-negative if provided
            if capabilities.max_bitrate_kbps is not None:
                if capabilities.max_bitrate_kbps < 0:
                    logger.warning(
                        "Invalid max_bitrate_kbps: %s, must be non-negative",
                        capabilities.max_bitrate_kbps,
                    )

        except Exception as exc:
            logger.exception(
                "Failed to validate capabilities for %s: %s",
                self.provider_name.value,
                exc,
            )
            raise

    def _normalize_search_results(self, results: SearchResults, query: str) -> SearchResults:
        """
        Normalize search results to ensure consistent shape.

        Args:
            results: Raw search results
            query: Original search query

        Returns:
            Normalized SearchResults with consistent structure
        """
        # Ensure query is set
        if not hasattr(results, "query") or results.query != query:
            # Create normalized result
            return SearchResults(
                items=results.items,
                next_cursor=results.next_cursor,
                total=results.total,
                query=query,
            )
        return results

    def _normalize_paginated_result(self, result: PaginatedResult[PlaylistSummary]) -> PaginatedResult[PlaylistSummary]:
        """
        Normalize paginated result to ensure consistent shape.

        Args:
            result: Raw paginated result

        Returns:
            Normalized PaginatedResult with consistent structure
        """
        # Ensure items is a list
        if not isinstance(result.items, list):
            return PaginatedResult[PlaylistSummary](
                items=list(result.items) if result.items else [],
                next_cursor=result.next_cursor,
                total=result.total,
            )
        return result

    def _normalize_stream_info(self, stream: StreamInfo) -> StreamInfo:
        """
        Normalize stream info to ensure consistent shape.

        Args:
            stream: Raw stream info

        Returns:
            Normalized StreamInfo with consistent structure
        """
        # Validate URL is present
        if not stream.url:
            raise ValueError("StreamInfo must have a valid URL")

        # Ensure metadata is a dict
        if not isinstance(stream.metadata, dict):
            normalized_metadata = dict(stream.metadata) if stream.metadata else {}
            return StreamInfo(
                url=stream.url,
                expires_at=stream.expires_at,
                drm=stream.drm,
                content_type=stream.content_type,
                bitrate_kbps=stream.bitrate_kbps,
                requires_embedded_player=stream.requires_embedded_player,
                metadata=normalized_metadata,
            )
        return stream

    def _to_canonical_queue_item(self, track: TrackSummary, stream: StreamInfo) -> dict[str, Any]:
        """
        Convert provider track and stream to canonical QueueItem shape.

        This helper method maps provider-specific data (TrackSummary + StreamInfo)
        to the canonical QueueItem format used by the Hub state system.

        Args:
            track: Provider track summary
            stream: Resolved stream info

        Returns:
            Dictionary compatible with QueueItem model, ready for Hub state mapping
        """
        import time

        # Map provider capabilities to queue item capabilities
        capabilities = self.provider_capabilities()
        item_capabilities: dict[str, Any] = {
            "provider": self.provider_name.value,
            "bitrate_kbps": stream.bitrate_kbps or capabilities.max_bitrate_kbps,
            "requires_embedded_player": stream.requires_embedded_player,
            "embedded": stream.requires_embedded_player,  # Alias for compatibility
        }

        # Add feature flags based on provider capabilities
        if capabilities.features:
            from .models import ProviderFeature

            for feature in capabilities.features:
                if feature == ProviderFeature.GAPLESS:
                    item_capabilities["gapless"] = True
                elif feature == ProviderFeature.LYRICS:
                    item_capabilities["lyrics"] = True
                elif feature == ProviderFeature.VIDEO_PLAYBACK:
                    item_capabilities["video"] = True
                elif feature == ProviderFeature.OFFLINE_CACHE:
                    item_capabilities["offline"] = True

        # Extract playback_mode from stream metadata (defaults to "vlc_stream" for backward compatibility)
        playback_mode = None
        if stream.metadata:
            playback_mode = stream.metadata.get("playback_mode")

        # Build canonical queue item shape
        return {
            "id": track.id,
            "title": track.title,
            "url": str(stream.url),
            "source": self.provider_name.value,
            "video_id": track.provider_track_id or track.id,
            "artist": track.artist_name,
            "provider": self.provider_name.value,
            "artwork_url": str(track.artwork_url) if track.artwork_url else None,
            "stream_token": (stream.metadata.get("stream_token") if stream.metadata else None),
            "capabilities": item_capabilities,
            "resolved_at": time.time(),
            "playback_mode": playback_mode,  # Propagate playback_mode from stream metadata
        }

    def _map_to_hub_state_format(self, track: TrackSummary, stream: StreamInfo, position: int = 0) -> dict[str, Any]:
        """
        Map provider track/stream to canonical Hub state format.

        This creates a PlayerState-compatible dictionary that can be
        processed by HubStateAuthority.reconcile_provider_state().

        Args:
            track: Provider track summary
            stream: Resolved stream info
            position: Current playback position in seconds

        Returns:
            Dictionary compatible with PlayerState model, ready for Hub reconciliation
        """
        queue_item = self._to_canonical_queue_item(track, stream)

        # Build player state shape
        capabilities = self.provider_capabilities()
        from .models import ProviderFeature

        return {
            "is_playing": False,  # Default, should be updated by caller
            "now_playing": queue_item,
            "queue": [],  # Empty queue, should be populated by caller
            "volume": 80,  # Default volume
            "position": position,
            "duration": (track.duration_ms // 1000) if track.duration_ms else 0,
            "position_percentage": 0.0,
            "backend": "streaming",
            "backend_display_name": self.display_name,
            "backend_capabilities": {
                "pause": True,
                "seek": True,
                "volume": True,
                "gapless": any(f == ProviderFeature.GAPLESS for f in capabilities.features),
            },
            "playback_capabilities": queue_item.get("capabilities", {}),
            "resolver_info": {
                "provider": self.provider_name.value,
                "expires_at": (stream.expires_at.isoformat() if stream.expires_at else None),
                "bitrate_kbps": stream.bitrate_kbps,
                "requires_embedded_player": stream.requires_embedded_player,
            },
            "playback_errors": [],
            "metadata": {
                "provider": self.provider_name.value,
                "album": track.album_name,
                "is_explicit": track.is_explicit,
            },
        }

    @abstractmethod
    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        """
        Kick off or refresh user authentication/authorization.

        Returns an :class:`AuthSession` describing the current link status
        and optional redirect URL the client should send the user to.
        """

    @abstractmethod
    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PlaylistPage:
        """Return paginated playlists owned or followed by the user."""

    @abstractmethod
    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> TrackSearchPage:
        """Search tracks accessible to the user."""

    @abstractmethod
    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        """
        Resolve a playable stream for the given track.

        Implementations must honor provider ToS (e.g. DRM, expiring URLs).
        """

    @abstractmethod
    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        """
        Return a URL or data URI for track artwork.

        Implementations should prefer HTTPS URLs that are safe to cache.
        """

    @abstractmethod
    def provider_capabilities(self) -> ProviderCapabilities:
        """
        Describe static capabilities for this provider.

        This metadata is used to tailor playback behavior and UI affordances.

        Returns:
            ProviderCapabilities with validated structure matching provider_name
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{self.__class__.__name__} provider_name={self.provider_name!r}>"
