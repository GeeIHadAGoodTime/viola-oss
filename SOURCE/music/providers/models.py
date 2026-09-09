"""
Shared data models for the provider SDK.

All models use Pydantic for validation so they can be safely serialized
across service boundaries (REST, WebSocket, etc.).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum, auto
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, HttpUrl


class ProviderName(str, Enum):
    """Stable identifiers for supported providers."""

    SPOTIFY = "spotify"
    YOUTUBE_MUSIC = "youtube_music"
    YOUTUBE_IFRAME = "youtube_iframe"
    LOCAL = "local"
    BROWSER = "browser"


class ProviderFeature(Enum):
    """Feature flags that providers may support."""

    OFFLINE_CACHE = auto()
    HIGH_FIDELITY = auto()
    LYRICS = auto()
    GAPLESS = auto()
    VIDEO_PLAYBACK = auto()
    PODCASTS = auto()


class ProviderCapabilities(BaseModel):
    """
    Capability declaration returned by providers.

    Attributes:
        name: The provider identifier.
        features: Enumerated features supported by this provider.
        max_bitrate_kbps: Upper bitrate advertised by the provider.
        supports_explicit_filter: True if explicit tracks can be filtered.
    """

    name: ProviderName
    features: Sequence[ProviderFeature] = Field(default_factory=list)
    max_bitrate_kbps: int | None = None
    supports_explicit_filter: bool = False
    supports_offline_downloads: bool = False
    notes: str | None = None


class AuthContext(BaseModel):
    """
    Parameters used during authentication flows.

    The orchestrator (Batch B) will populate these fields; providers use
    them to craft redirect URLs or exchange codes.
    """

    redirect_uri: str | None = None
    state: str | None = None
    scopes: list[str] | None = None
    force_reauth: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


class AuthSession(BaseModel):
    """
    Response returned from :meth:`MusicProvider.authenticate_user`.

    Attributes:
        is_linked: True if the user already granted access.
        requires_redirect: Indicates a browser redirect is needed.
        authorization_url: Destination for the user to complete consent.
        linked_at: Timestamp when authorization was completed.
        scopes: Effective scopes granted to the integration.
    """

    is_linked: bool
    requires_redirect: bool = False
    authorization_url: HttpUrl | None = None
    linked_at: datetime | None = None
    scopes: Sequence[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class PlaylistSummary(BaseModel):
    """Lightweight playlist metadata used across providers."""

    id: str
    name: str
    description: str | None = None
    track_count: int | None = None
    owner_name: str | None = None
    artwork_url: HttpUrl | None = None
    is_liked_songs: bool = False
    provider_playlist_id: str | None = None


class TrackSummary(BaseModel):
    """Canonical track representation for browsing/searching."""

    id: str
    title: str
    artist_name: str
    album_name: str | None = None
    duration_ms: int | None = None
    is_explicit: bool = False
    artwork_url: str | None = None
    provider_track_id: str | None = None
    extras: dict[str, str] = Field(default_factory=dict)


class ArtworkInfo(BaseModel):
    """Artwork metadata returned by :meth:`MusicProvider.fetch_artwork`."""

    url: HttpUrl
    width: int | None = None
    height: int | None = None
    color_palette: list[str] | None = None
    expires_at: datetime | None = None


class PlaybackContext(BaseModel):
    """
    Preference hints when resolving a stream.

    Attributes:
        device_id: Identifier of the playback device.
        preferred_bitrate_kbps: Target bitrate requested by the client.
        allow_video: True if video streams are acceptable.
        extras: Provider-specific hints (e.g. volume normalization).
    """

    device_id: str | None = None
    preferred_bitrate_kbps: int | None = None
    allow_video: bool = False
    extras: dict[str, str] = Field(default_factory=dict)


class StreamInfo(BaseModel):
    """Details about a resolved playback stream."""

    url: HttpUrl
    expires_at: datetime | None = None
    drm: dict[str, str] | None = None
    content_type: str | None = None
    bitrate_kbps: int | None = None
    requires_embedded_player: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


T = TypeVar("T")


class PaginatedResult(BaseModel, Generic[T]):
    """Generic pagination wrapper."""

    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class SearchResults(PaginatedResult[TrackSummary]):
    """Convenience alias for search pages."""

    query: str
