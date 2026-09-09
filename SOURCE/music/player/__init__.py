"""
Music Player Package.

Consolidated music player components for the Viola application.
This package reorganizes the previously fragmented music_player_*.py files
into a cleaner structure.

Modules:
    core: Main MusicPlayer class and initialization
    fallback: Autoplay fallback handling
    playback: Playback control and embedded playback
    state: State management, queue management, and artwork
    backends: Backend management and lifecycle
"""

from __future__ import annotations

from utils.async_helpers import wait_for_condition

from .backends import MusicPlayerBackendManager
from .core import MusicPlayer, MusicPlayerInitializer
from .fallback import MusicPlayerFallback
from .playback import MusicPlayerEmbeddedPlayback, MusicPlayerPlaybackController
from .state import (
    MusicPlayerArtworkHandler,
    MusicPlayerQueueManager,
    MusicPlayerStateManager,
)

__all__ = [
    "MusicPlayer",
    "MusicPlayerArtworkHandler",
    "MusicPlayerBackendManager",
    "MusicPlayerEmbeddedPlayback",
    "MusicPlayerFallback",
    "MusicPlayerInitializer",
    "MusicPlayerPlaybackController",
    "MusicPlayerQueueManager",
    "MusicPlayerStateManager",
    "wait_for_condition",
]
