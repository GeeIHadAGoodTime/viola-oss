"""
Playback Routing Service

Determines the appropriate playback mechanism for a QueueItem based on:
- Playback mode (embedded_webview, external_browser, vlc_stream)
- Provider requirements (YouTube Music TOS compliance)
- Item capabilities (requires_embedded_player, embedded, video_playback)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem


@dataclass
class PlaybackRoute:
    """
    Result of playback routing decision.

    Attributes:
        route_type: The type of playback route ('embedded_webview', 'embedded_iframe_webview',
                    'external_browser', 'vlc_stream', 'qt_media', 'engine_manager')
        requires_embedded: Whether this item requires embedded player
        provider_id: Identified provider ID
        reason: Human-readable reason for this routing decision
        metadata: Additional metadata about the routing decision
    """

    route_type: str
    requires_embedded: bool = False
    provider_id: str | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize metadata dict if not provided."""
        if self.metadata is None:
            self.metadata = {}


class PlaybackRouter:
    """
    Routes QueueItems to appropriate playback mechanisms.

    Determines:
    - embedded_webview: YouTubeWebBackend with QWebEngineView
    - embedded_iframe_webview: YouTubeIFrameBackend with iframe player
    - external_browser: System browser (non-YTM only)
    - vlc_stream: VLC backend (non-YTM only)
    - qt_media: Qt-native QMediaPlayer backend (local files)
    - engine_manager: Provider-based engine manager path

    This class centralizes all routing logic to make it easier to reason about
    and maintain. It handles TOS compliance checks, provider-specific rules,
    and fallback strategies.
    """

    def __init__(self, logger: logging.Logger | None = None):
        """
        Initialize the playback router.

        Args:
            logger: Optional logger instance. If not provided, creates a default logger.
        """
        self._logger = logger or get_logger("viola.playback.router")

    def route(self, item: QueueItem) -> PlaybackRoute:
        """
        Determine playback route for item.

        This is the main entry point for routing decisions. It considers:
        1. Explicit playback_mode on the item
        2. Provider-specific requirements (YouTube Music TOS)
        3. Item capabilities (requires_embedded_player, embedded)
        4. Fallback strategies

        Args:
            item: QueueItem to route

        Returns:
            PlaybackRoute with routing decision and metadata
        """
        provider_id = getattr(item, "provider", None) or "unknown"
        raw_playback_mode = getattr(item, "playback_mode", None)

        # YouTube providers MUST use embedded playback (TOS compliance).
        # If playback_mode is missing, default to embedded_iframe_webview
        # instead of vlc_stream to prevent SimpleBackend from playing
        # YouTube content directly.
        _yt_providers = {"youtube_music", "youtube_iframe", "youtube"}
        if not raw_playback_mode and provider_id in _yt_providers:
            self._logger.warning(
                "ROUTING_FIX: YouTube provider=%s has no playback_mode; "
                "defaulting to embedded_iframe_webview (not vlc_stream)",
                provider_id,
            )
            raw_playback_mode = "embedded_iframe_webview"

        playback_mode = raw_playback_mode or "vlc_stream"

        # Extract capabilities
        capabilities = getattr(item, "capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}

        requires_embedded = capabilities.get("requires_embedded_player", False)
        embedded = capabilities.get("embedded", False)
        _video_playback = capabilities.get("video_playback", False)

        # Log routing decision context
        self._logger.debug(
            "ROUTING_DECISION item_id=%s provider=%s playback_mode=%s requires_embedded=%s embedded=%s",
            getattr(item, "id", "unknown"),
            provider_id,
            playback_mode,
            requires_embedded,
            embedded,
        )

        # Route based on explicit playback_mode first
        if playback_mode in ("embedded_iframe_webview", "embedded_webview"):
            return self._route_embedded_mode(item, playback_mode, provider_id, requires_embedded, embedded)

        if playback_mode == "external_browser":
            return self._route_external_browser(item, provider_id)

        if playback_mode == "qt_media":
            return self._route_qt_media(item, provider_id)

        if playback_mode == "spotify_cdp":
            return PlaybackRoute(
                route_type="embedded_webview",
                requires_embedded=True,
                provider_id=provider_id,
                reason="Spotify CDP requires dedicated engine (via engine manager)",
                metadata={"playback_mode": "spotify_cdp"},
            )

        # For vlc_stream mode or default, check if embedded is required
        if requires_embedded or embedded:
            # Item requires embedded player but playback_mode doesn't specify it
            # This can happen when capabilities are set but playback_mode wasn't updated
            self._logger.warning(
                "ROUTING_CORRECTION: Item requires embedded player but playback_mode=%s. "
                "Forcing embedded_webview mode. item_id=%s",
                playback_mode,
                getattr(item, "id", "unknown"),
            )
            return PlaybackRoute(
                route_type="embedded_webview",
                requires_embedded=True,
                provider_id=provider_id,
                reason="Item capabilities require embedded player",
                metadata={
                    "original_playback_mode": playback_mode,
                    "requires_embedded_player": requires_embedded,
                    "embedded": embedded,
                },
            )

        # Default to vlc_stream for non-embedded items
        return self._route_vlc_stream(item, provider_id, requires_embedded, embedded)

    def _route_embedded_mode(
        self,
        item: QueueItem,
        playback_mode: str,
        provider_id: str,
        requires_embedded: bool,
        embedded: bool,
    ) -> PlaybackRoute:
        """
        Route items that require embedded playback.

        Args:
            item: QueueItem to route
            playback_mode: Either 'embedded_webview' or 'embedded_iframe_webview'
            provider_id: Provider identifier
            requires_embedded: Whether item requires embedded player
            embedded: Whether item is marked as embedded

        Returns:
            PlaybackRoute for embedded playback
        """
        # Check if we should use engine_manager path first
        # (This will be handled by BackendSelector, but we note it here)
        route_type = playback_mode

        return PlaybackRoute(
            route_type=route_type,
            requires_embedded=True,
            provider_id=provider_id,
            reason=f"Explicit playback_mode={playback_mode} requires embedded player",
            metadata={
                "playback_mode": playback_mode,
                "requires_embedded_player": requires_embedded,
                "embedded": embedded,
            },
        )

    def _route_external_browser(self, item: QueueItem, provider_id: str) -> PlaybackRoute:
        """
        Route items to external browser playback.

        Args:
            item: QueueItem to route
            provider_id: Provider identifier

        Returns:
            PlaybackRoute for external browser

        Raises:
            ValueError: If YouTube Music tries to use external_browser (TOS violation)
        """
        # TOS COMPLIANCE: Block YouTube Music from using external_browser
        if provider_id == "youtube_music":
            # Check if we're in test mode (allow for testing)
            from config.settings import settings

            in_test_mode = settings.pytest_in_progress or settings.test_mode

            if not in_test_mode:
                raise ValueError(
                    "YouTube Music items cannot use external_browser mode. "
                    "Must use embedded_webview mode for legal compliance."
                )

        return PlaybackRoute(
            route_type="external_browser",
            requires_embedded=False,
            provider_id=provider_id,
            reason="Explicit playback_mode=external_browser",
            metadata={"playback_mode": "external_browser"},
        )

    def _route_qt_media(self, item: QueueItem, provider_id: str) -> PlaybackRoute:
        """Route items to Qt-native QMediaPlayer backend (local files).

        Args:
            item: QueueItem to route
            provider_id: Provider identifier

        Returns:
            PlaybackRoute for Qt media playback
        """
        return PlaybackRoute(
            route_type="qt_media",
            requires_embedded=False,
            provider_id=provider_id,
            reason="Explicit playback_mode=qt_media for Qt-native local file playback",
            metadata={"playback_mode": "qt_media"},
        )

    def _route_vlc_stream(
        self,
        item: QueueItem,
        provider_id: str,
        requires_embedded: bool,
        embedded: bool,
    ) -> PlaybackRoute:
        """
        Route items to VLC stream playback.

        Args:
            item: QueueItem to route
            provider_id: Provider identifier
            requires_embedded: Whether item requires embedded player
            embedded: Whether item is marked as embedded

        Returns:
            PlaybackRoute for VLC stream

        Raises:
            ValueError: If YouTube Music tries to use VLC (TOS violation, unless unsafe flag set)
        """
        # TOS COMPLIANCE: Block VLC for YouTube Music (PRD v5.3 section 7.3.1)
        # Hub MUST NOT use any stream extraction mechanism - embedded playback only
        if provider_id == "youtube_music":
            from config.settings import settings

            # Developer-only override: Only allow in explicit dev mode with safety checks
            unsafe_allow_youtube_streaming = False
            try:
                # Check if explicitly enabled AND in developer/test mode
                raw_flag = getattr(settings, "unsafe_allow_youtube_streaming", False)
                is_test_mode = settings.pytest_in_progress or settings.test_mode or settings.developer_mode

                # Only allow if BOTH flag is set AND we're in dev/test mode
                unsafe_allow_youtube_streaming = bool(raw_flag and is_test_mode)

                if raw_flag and not is_test_mode:
                    # Flag is set but not in dev mode - log warning and block
                    self._logger.warning(
                        "ROUTING_SAFETY: unsafe_allow_youtube_streaming is set but not in dev/test mode. "
                        "Blocking VLC for YouTube Music to maintain legal compliance."
                    )
            except Exception as exc:
                # If any error checking the flag, default to blocking (safest)
                self._logger.warning(
                    "ROUTING_SAFETY: Error checking unsafe flag, defaulting to block: %s",
                    exc,
                )
                unsafe_allow_youtube_streaming = False

            if not unsafe_allow_youtube_streaming:
                raise ValueError(
                    "YouTube Music items must use embedded_webview mode, not VLC. "
                    "This is blocked for legal compliance (PRD v5.3 section 7.3.1). "
                    "VLC/stream extraction violates YouTube ToS."
                )

        return PlaybackRoute(
            route_type="vlc_stream",
            requires_embedded=False,
            provider_id=provider_id,
            reason="Default vlc_stream mode for non-embedded items",
            metadata={
                "playback_mode": "vlc_stream",
                "requires_embedded_player": requires_embedded,
                "embedded": embedded,
            },
        )

    def should_use_embedded(self, item: QueueItem) -> bool:
        """
        Check if item requires embedded player.

        This is a convenience method that checks both explicit playback_mode
        and item capabilities.

        Args:
            item: QueueItem to check

        Returns:
            True if item requires embedded player
        """
        playback_mode = getattr(item, "playback_mode", None)
        if playback_mode in ("embedded_webview", "embedded_iframe_webview"):
            return True

        capabilities = getattr(item, "capabilities", {})
        if not isinstance(capabilities, dict):
            return False

        return capabilities.get("requires_embedded_player", False) or capabilities.get("embedded", False)

    def should_use_vlc(self, item: QueueItem) -> bool:
        """
        Check if item can use VLC backend.

        This checks:
        1. Playback mode is vlc_stream (or default)
        2. Item doesn't require embedded player
        3. Provider allows VLC (YouTube Music blocked unless unsafe flag)

        Args:
            item: QueueItem to check

        Returns:
            True if item can use VLC backend
        """
        if self.should_use_embedded(item):
            return False

        playback_mode = getattr(item, "playback_mode", None)
        if playback_mode in (
            "embedded_webview",
            "embedded_iframe_webview",
            "external_browser",
        ):
            return False

        provider_id = getattr(item, "provider", None)
        if provider_id == "youtube_music":
            # TOS COMPLIANCE: YouTube Music must use embedded webview, not VLC
            # See PRD v5.3 section 7.3.1 - Hub MUST NOT use VLC for YouTube
            from config.settings import settings

            # Developer-only override: Only allow in explicit dev mode
            unsafe_allow_youtube_streaming = False
            try:
                raw_flag = getattr(settings, "unsafe_allow_youtube_streaming", False)
                is_test_mode = settings.pytest_in_progress or settings.test_mode or settings.developer_mode
                unsafe_allow_youtube_streaming = bool(raw_flag and is_test_mode)
            except Exception:
                unsafe_allow_youtube_streaming = False

            if not unsafe_allow_youtube_streaming:
                return False  # Block VLC for YouTube Music

        return True
