"""
music.providers.browser
-----------------------

Browser-native playback controller for QWebEngineView.

Controls launch-supported web-based music services by navigating
QWebEngineView to the service page, injecting JavaScript to
interact with the player, and reading metadata via the W3C MediaSession API.

This is NOT a MusicProvider (no auth, search, playlists).  It is a playback
controller -- an alternative to YouTubeIFrameBackend that works with any
site exposing MediaSession or standard <audio>/<video> elements.

- BrowserPlaybackController: Core engine for webview playback control
- MediaSessionReader: Metadata extraction with fallback chain
- TrackMetadata: Structured metadata dataclass
- JS bridge utilities: JavaScript code generators for injection
"""

from __future__ import annotations

from .js_bridge import (
    js_call_action,
    js_get_media_elements,
    js_get_media_session_metadata,
    js_get_page_metadata,
    js_get_playback_state,
    js_set_volume,
    js_setup_track_end_listener,
)
from .media_session import MediaSessionReader, TrackMetadata
from .provider import BrowserPlaybackController

__all__ = [
    "BrowserPlaybackController",
    "MediaSessionReader",
    "TrackMetadata",
    "js_call_action",
    "js_get_media_elements",
    "js_get_media_session_metadata",
    "js_get_page_metadata",
    "js_get_playback_state",
    "js_set_volume",
    "js_setup_track_end_listener",
]
