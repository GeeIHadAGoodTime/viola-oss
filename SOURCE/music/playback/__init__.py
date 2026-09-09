"""
Playback Routing and Backend Selection

This module provides centralized routing and backend selection for music playback.
It extracts complex decision logic from the main MusicPlayer class to improve
maintainability and testability.
"""

from __future__ import annotations

from music.playback.backend_selector import BackendSelector
from music.playback.routing import PlaybackRoute, PlaybackRouter

__all__ = ["BackendSelector", "PlaybackRoute", "PlaybackRouter"]
