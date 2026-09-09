"""
Playback backend implementations.

This package contains provider-specific playback engine adapters.
The canonical VLC backend is located at music/backends/vlc.py (VLCBackend).
See ADR-0003 for rationale on backend consolidation.

Key components:
- engine_manager.py: PlaybackEngineManager coordinating provider engines
- engines/: Provider-specific playback engines for launched providers
- queue_engine.py: PlaybackQueueEngine for queue management
- feature_flags.py: PlaybackFeatureFlags for runtime configuration
"""

from __future__ import annotations

from .engine_manager import EngineBackedBackend, PlaybackEngineManager
from .feature_flags import PlaybackFeatureFlags
from .queue_engine import (
    PlaybackQueueAction,
    PlaybackQueueCoordinator,
    PlaybackQueueEngine,
    PlaybackTransport,
)

__all__ = [
    "EngineBackedBackend",
    "PlaybackEngineManager",
    "PlaybackFeatureFlags",
    "PlaybackQueueAction",
    "PlaybackQueueCoordinator",
    "PlaybackQueueEngine",
    "PlaybackTransport",
]
