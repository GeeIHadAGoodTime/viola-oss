"""
Provider playback engine registry.

This package exposes concrete implementations that wrap the official SDKs for
the supported music providers.  Each engine adheres to the common
``ProviderPlaybackEngine`` contract defined in ``base.py``.
"""

from __future__ import annotations

from .base import (
    PlaybackCapabilities,
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
)
from .spotify import SpotifyWebPlaybackEngine
from .spotify_cdp import SpotifyCDPEngine
from .youtube import YouTubeEmbeddedEngine

__all__ = [
    "PlaybackCapabilities",
    "PlaybackHandle",
    "ProviderPlaybackEngine",
    "QueueContext",
    "SpotifyCDPEngine",
    "SpotifyWebPlaybackEngine",
    "YouTubeEmbeddedEngine",
]
