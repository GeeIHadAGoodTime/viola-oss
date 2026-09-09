"""
music.providers
---------------

Unified provider SDK for compliant music streaming integrations.

This package exposes the abstract provider contract, shared data models,
error taxonomy, and a provider registry used to load concrete adapters
for Spotify, YouTube Music, YouTube iframe playback, local music, and the
browser playback adapter.
"""

# Ensure bundled providers register themselves when package imported.

from __future__ import annotations

from . import (
    local,
    spotify_cdp,
    youtube_iframe,
    youtube_music,
)
from .base import MusicProvider
from .browser import music_provider as _browser_provider  # triggers @auto_register
from .models import (
    ArtworkInfo,
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
from .registry import (
    ProviderFactory,
    ProviderNotRegistered,
    get_provider_class,
    iter_registered_providers,
    register_provider,
)

__all__ = [
    "ArtworkInfo",
    "AuthContext",
    "AuthSession",
    "MusicProvider",
    "PaginatedResult",
    "PlaybackContext",
    "PlaylistSummary",
    "ProviderCapabilities",
    "ProviderFactory",
    "ProviderFeature",
    "ProviderName",
    "ProviderNotRegistered",
    "SearchResults",
    "StreamInfo",
    "TrackSummary",
    "get_provider_class",
    "iter_registered_providers",
    "local",
    "register_provider",
    "spotify_cdp",
    "youtube_iframe",
    "youtube_music",
]
