"""
Error Recovery System - Intelligent Error Handling
Provides contextual error messages with actionable solutions

Features:
- Smart error mapping
- Contextual help
- Auto-recovery
- Actionable suggestions
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class ErrorSeverity(Enum):
    """
    User-facing error severity levels for UI error display.

    Note: This is distinct from core.exceptions.ErrorSeverity which uses
    LOW/MEDIUM/HIGH/CRITICAL for monitoring/alerting. This enum uses
    log-level-style values that map directly to UI display states.

    See also: music.error_handler.ErrorSeverity (similar purpose for music errors)
    """

    CRITICAL = "critical"  # App cannot function
    ERROR = "error"  # Feature unavailable
    WARNING = "warning"  # Degraded functionality
    INFO = "info"  # Informational only


class ErrorCategory(Enum):
    """Categories of errors for better organization"""

    NETWORK = "network"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    PERMISSION = "permission"
    RESOURCE = "resource"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


@dataclass
class ErrorAction:
    """Actionable suggestion for error recovery"""

    label: str
    description: str
    callback: str | None = None  # JavaScript function name or route
    is_primary: bool = True
    opens_settings: bool = False
    settings_tab: str | None = None  # Which settings tab to open


@dataclass
class ErrorInfo:
    """Comprehensive error information"""

    code: str
    title: str
    message: str
    severity: ErrorSeverity
    category: ErrorCategory
    icon: str
    actions: list[ErrorAction]
    help_url: str | None = None
    can_retry: bool = False
    can_continue: bool = True
    technical_details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization"""
        return {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "severity": self.severity.value,
            "category": self.category.value,
            "icon": self.icon,
            "actions": [
                {
                    "label": a.label,
                    "description": a.description,
                    "callback": a.callback,
                    "is_primary": a.is_primary,
                    "opens_settings": a.opens_settings,
                    "settings_tab": a.settings_tab,
                }
                for a in self.actions
            ],
            "help_url": self.help_url,
            "can_retry": self.can_retry,
            "can_continue": self.can_continue,
            "technical_details": self.technical_details,
        }


class ErrorRecoverySystem:
    """
    Intelligent error handling system with contextual help and recovery suggestions.
    """

    def __init__(self):
        self._error_map = self._build_error_map()
        self._retry_strategies: dict[str, Callable] = {}
        self._error_history: list[ErrorInfo] = []
        self._max_history = 50

    def _build_error_map(self) -> dict[str, ErrorInfo]:
        """Build comprehensive error mapping"""
        return {
            # OpenAI / AI Errors
            "openai_api_key_missing": ErrorInfo(
                code="openai_api_key_missing",
                title="AI Features Disabled",
                message="An API key is required to use AI chat, voice responses, and smart recommendations.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="🔑",
                actions=[
                    ErrorAction(
                        label="Set Up AI",
                        description="I need an API key to use AI features. Say 'set up AI' and I'll walk you through it.",
                        callback="openSettings",
                        is_primary=True,
                        opens_settings=True,
                        settings_tab="advanced",
                    ),
                    ErrorAction(
                        label="Get API Key",
                        description="Open your AI provider's API key page",
                        callback="openUrl:https://platform.openai.com/api-keys",
                        is_primary=False,
                    ),
                ],
                help_url="/docs/setup#openai-api-key",
                can_continue=True,
            ),
            "openai_api_error": ErrorInfo(
                code="openai_api_error",
                title="AI Service Unavailable",
                message="Couldn't connect to the AI service. This might be a temporary issue or your API key may be invalid.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.NETWORK,
                icon="🌐",
                actions=[
                    ErrorAction(
                        label="Retry",
                        description="Try the request again",
                        callback="retryLastRequest",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Check API Key",
                        description="Verify your API key is correct",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="advanced",
                    ),
                    ErrorAction(
                        label="Check Status",
                        description="See if the AI service is down",
                        callback="openUrl:https://status.openai.com",
                        is_primary=False,
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            "openai_rate_limit": ErrorInfo(
                code="openai_rate_limit",
                title="Rate Limit Reached",
                message="You've sent too many requests to the AI service. Please wait a moment before trying again.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.RESOURCE,
                icon="⏱️",
                actions=[
                    ErrorAction(
                        label="Wait and Retry",
                        description="Automatically retry in a few seconds",
                        callback="retryWithDelay:5",
                        is_primary=True,
                    )
                ],
                can_retry=True,
                can_continue=True,
            ),
            # Transcription Errors
            "transcription_failed": ErrorInfo(
                code="transcription_failed",
                title="Couldn't Understand Audio",
                message="The audio transcription failed. Try speaking more clearly, checking your microphone, or reducing background noise.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.VALIDATION,
                icon="🎤",
                actions=[
                    ErrorAction(
                        label="Try Again",
                        description="Record your voice again",
                        callback="retryVoiceInput",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Test Microphone",
                        description="Check if your microphone is working",
                        callback="testMicrophone",
                    ),
                    ErrorAction(
                        label="Audio Settings",
                        description="Adjust microphone and audio settings",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="audio",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            "no_speech_detected": ErrorInfo(
                code="no_speech_detected",
                title="No Speech Detected",
                message="Couldn't detect any speech in the audio. Make sure you're speaking while recording.",
                severity=ErrorSeverity.INFO,
                category=ErrorCategory.VALIDATION,
                icon="🔇",
                actions=[
                    ErrorAction(
                        label="Try Again",
                        description="Hold the button and speak your command",
                        callback="retryVoiceInput",
                        is_primary=True,
                    )
                ],
                can_retry=True,
                can_continue=True,
            ),
            # Music Player Errors
            "music_search_failed": ErrorInfo(
                code="music_search_failed",
                title="Couldn't Find Music",
                message="No results found for that search. Try different keywords or check your spelling.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.VALIDATION,
                icon="🔍",
                actions=[
                    ErrorAction(
                        label="Try Different Search",
                        description="Use different keywords or artist name",
                        callback="focusChatInput",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Browse Playlists",
                        description="Play from your saved playlists instead",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="music",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            "empty_queue": ErrorInfo(
                code="empty_queue",
                title="Queue is Empty",
                message="There's nothing in the queue to play. Add some music first!",
                severity=ErrorSeverity.INFO,
                category=ErrorCategory.VALIDATION,
                icon="🎵",
                actions=[
                    ErrorAction(
                        label="Play Music",
                        description="Search for songs to play",
                        callback="focusChatInput",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Browse Playlists",
                        description="Play from saved playlists",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="music",
                    ),
                ],
                can_continue=True,
            ),
            "playback_error": ErrorInfo(
                code="playback_error",
                title="Playback Failed",
                message="Couldn't play this song. It might be unavailable or there could be a connection issue.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.RESOURCE,
                icon="⚠️",
                actions=[
                    ErrorAction(
                        label="Skip Song",
                        description="Try playing the next song",
                        callback="skipTrack",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Retry",
                        description="Try playing this song again",
                        callback="retryPlayback",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            # Network Errors
            "network_error": ErrorInfo(
                code="network_error",
                title="Connection Problem",
                message="Couldn't connect to the internet. Check your network connection and try again.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.NETWORK,
                icon="🌐",
                actions=[
                    ErrorAction(
                        label="Retry",
                        description="Try connecting again",
                        callback="retryLastRequest",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Check Connection",
                        description="Open network settings",
                        callback="openNetworkSettings",
                    ),
                ],
                can_retry=True,
                can_continue=False,
            ),
            # Configuration Errors
            "microphone_permission_denied": ErrorInfo(
                code="microphone_permission_denied",
                title="Microphone Access Denied",
                message="Viola needs microphone permission for voice commands. Please allow access in your system settings.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.PERMISSION,
                icon="🎤",
                actions=[
                    ErrorAction(
                        label="Grant Permission",
                        description="Open system settings to grant microphone permission",
                        callback="openSystemSettings:microphone",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Use Text Input",
                        description="Type commands instead of speaking",
                        callback="focusChatInput",
                    ),
                ],
                help_url="/docs/troubleshooting#microphone-permission",
                can_continue=True,
            ),
            "vlc_not_found": ErrorInfo(
                code="vlc_not_found",
                title="VLC Not Installed",
                message="VLC Media Player is required for music playback but wasn't found on your system.",
                severity=ErrorSeverity.CRITICAL,
                category=ErrorCategory.CONFIGURATION,
                icon="🎬",
                actions=[
                    ErrorAction(
                        label="Download VLC",
                        description="Get VLC from the official website",
                        callback="openUrl:https://www.videolan.org/vlc/",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Installation Guide",
                        description="See step-by-step installation instructions",
                        callback="openUrl:/docs/setup#vlc-installation",
                    ),
                ],
                help_url="/docs/setup#vlc-installation",
                can_continue=False,
            ),
            # Music Provider Errors
            "no_active_music_provider": ErrorInfo(
                code="no_active_music_provider",
                title="No Music Provider Linked",
                message="No music provider is linked. Say 'connect Spotify' or 'connect YouTube Music' and I'll set it up for you.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="🔗",
                actions=[
                    ErrorAction(
                        label="Connect Provider",
                        description="Say 'connect Spotify' or 'connect YouTube Music' and I'll set it up",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Learn More",
                        description="See how to link providers",
                        callback="openUrl:/docs/setup#music-providers",
                    ),
                ],
                can_continue=True,
            ),
            "music_provider_unavailable": ErrorInfo(
                code="music_provider_unavailable",
                title="Music Provider Unavailable",
                message="Your current music provider is disabled or unsupported in this build. Say 'switch provider' and I'll help you pick another one.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="⚠️",
                actions=[
                    ErrorAction(
                        label="Switch Provider",
                        description="Say 'switch provider' and I'll help you pick another one",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Learn More",
                        description="See available providers",
                        callback="openUrl:/docs/setup#music-providers",
                    ),
                ],
                can_continue=True,
            ),
            "youtube_music_provider_failed": ErrorInfo(
                code="youtube_music_provider_failed",
                title="YouTube Music Error",
                message="I couldn't play that using YouTube Music. Say 'reconnect YouTube Music' and I'll fix the link for you.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="⚠️",
                actions=[
                    ErrorAction(
                        label="Reconnect",
                        description="Say 'reconnect YouTube Music' and I'll fix the link",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Retry",
                        description="Try playing again",
                        callback="retryLastRequest",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            # Music Provider Auth Errors (H9)
            "spotify_connection_failed": ErrorInfo(
                code="spotify_connection_failed",
                title="Spotify Connection Failed",
                message="Couldn't connect to Spotify. Make sure you have a Spotify Premium subscription, then say 'reconnect Spotify' and I'll handle it.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.CONFIGURATION,
                icon="🎵",
                actions=[
                    ErrorAction(
                        label="Reconnect Spotify",
                        description="Say 'reconnect Spotify' and I'll handle it",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Check Spotify Status",
                        description="See if Spotify services are available",
                        callback="openUrl:https://status.spotify.com",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            ),
            "spotify_auth_failure": ErrorInfo(
                code="spotify_auth_failure",
                title="Spotify Authentication Failed",
                message="Your Spotify session has expired or authorization was revoked. Say 'reconnect Spotify' and I'll re-authenticate for you.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.CONFIGURATION,
                icon="🔐",
                actions=[
                    ErrorAction(
                        label="Reconnect Spotify",
                        description="Say 'reconnect Spotify' and I'll re-authenticate for you",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                ],
                can_retry=False,
                can_continue=True,
            ),
            "youtube_region_unavailable": ErrorInfo(
                code="youtube_region_unavailable",
                title="YouTube Unavailable in Your Region",
                message="This YouTube content isn't available in your region. Try a different track, or say 'reconnect YouTube Music' to re-authenticate.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="🌍",
                actions=[
                    ErrorAction(
                        label="Reconnect YouTube",
                        description="Say 'reconnect YouTube Music' and I'll re-authenticate for you",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Try Different Track",
                        description="Search for alternative content",
                        callback="focusChatInput",
                    ),
                ],
                can_retry=False,
                can_continue=True,
            ),
            "music_provider_reauth_required": ErrorInfo(
                code="music_provider_reauth_required",
                title="Music Provider Session Expired",
                message="Your music provider session expired. Say 'reconnect' followed by your provider name and I'll handle it.",
                severity=ErrorSeverity.WARNING,
                category=ErrorCategory.CONFIGURATION,
                icon="🔄",
                actions=[
                    ErrorAction(
                        label="Reconnect",
                        description="Say 'reconnect' followed by your provider name and I'll handle it",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                ],
                can_retry=False,
                can_continue=True,
            ),
            "music_provider_auth_failure": ErrorInfo(
                code="music_provider_auth_failure",
                title="Music Provider Auth Failed",
                message="Couldn't authenticate with your music provider. Say 'reconnect' followed by your provider name and I'll fix it.",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.CONFIGURATION,
                icon="🔑",
                actions=[
                    ErrorAction(
                        label="Reconnect",
                        description="Say 'reconnect' followed by your provider name and I'll fix it",
                        callback="openSettings",
                        opens_settings=True,
                        settings_tab="accounts",
                        is_primary=True,
                    ),
                ],
                can_retry=False,
                can_continue=True,
            ),
        }

    def get_error_info(
        self,
        error_code: str,
        technical_details: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ErrorInfo:
        """
        Get comprehensive error information for a given error code.
        Falls back to generic error if code not found.
        """
        error_info = self._error_map.get(error_code)

        # Special handling for music_provider_unavailable to show accurate messages
        if error_code == "music_provider_unavailable":
            error_info = self._handle_music_provider_unavailable(technical_details, context)

        if not error_info:
            # Generic fallback error
            error_info = ErrorInfo(
                code=error_code,
                title="Something Went Wrong",
                message=f"An unexpected error occurred: {error_code}",
                severity=ErrorSeverity.ERROR,
                category=ErrorCategory.UNKNOWN,
                icon="⚠️",
                actions=[
                    ErrorAction(
                        label="Retry",
                        description="Try the action again",
                        callback="retryLastRequest",
                        is_primary=True,
                    ),
                    ErrorAction(
                        label="Report Issue",
                        description="Let us know about this problem",
                        callback="reportIssue",
                    ),
                ],
                can_retry=True,
                can_continue=True,
            )

        # Add technical details if provided
        if technical_details:
            error_info.technical_details = technical_details

        # Add to history
        self._add_to_history(error_info)

        return error_info

    def _handle_music_provider_unavailable(
        self, technical_details: str | None, context: dict[str, Any] | None
    ) -> ErrorInfo:
        """
        Handle music_provider_unavailable error with smart message selection.

        Distinguishes between:
        - Provider actually disabled in build (show "disabled in this build")
        - Provider linked but failing (show actionable message about checking connection)
        """
        # Check technical details and context for clues
        details_lower = (technical_details or "").lower()
        context_str = str(context or {}).lower()
        combined = f"{details_lower} {context_str}"

        # Check if error indicates provider is actually disabled in build
        is_build_disabled = any(
            phrase in combined
            for phrase in [
                "disabled in this build",
                "unsupported in this build",
                "not supported in this build",
                "scraping disabled",
            ]
        )

        # Check if it's a YouTube Music provider issue
        is_youtube_music = any(
            phrase in combined
            for phrase in [
                "youtube",
                "youtube_music",
                "using youtube music",
            ]
        )

        if is_build_disabled:
            # Provider actually disabled - show build restriction message
            base_info = self._error_map.get("music_provider_unavailable")
            if base_info:
                return base_info
        elif is_youtube_music:
            # YouTube Music linked but failing - show actionable message
            youtube_info = self._error_map.get("youtube_music_provider_failed")
            if youtube_info:
                return youtube_info
        else:
            # Generic provider failure - use default but with better message
            base_info = self._error_map.get("music_provider_unavailable")
            if base_info:
                # Create a copy with updated message
                return ErrorInfo(
                    code=base_info.code,
                    title=base_info.title,
                    message="I couldn't play that using your music provider. Say 'reconnect' followed by your provider name and I'll fix it.",
                    severity=base_info.severity,
                    category=base_info.category,
                    icon=base_info.icon,
                    actions=base_info.actions,
                    can_retry=True,
                    can_continue=base_info.can_continue,
                )

        # Fallback to generic error if no specific mapping found
        return ErrorInfo(
            code="music_provider_error",
            title="Music Provider Error",
            message="I couldn't play that using your music provider. Say 'reconnect' followed by your provider name and I'll fix it.",
            severity=ErrorSeverity.ERROR,
            category=ErrorCategory.RESOURCE,
            icon="music_off",
            actions=[
                ErrorAction(
                    label="Reconnect",
                    description="Say 'reconnect' followed by your provider name and I'll fix it",
                    callback="open_settings",
                    opens_settings=True,
                    settings_tab="accounts",
                )
            ],
            can_retry=True,
            can_continue=True,
        )

    def _add_to_history(self, error_info: ErrorInfo):
        """Add error to history (for analytics/debugging)"""
        self._error_history.append(error_info)
        if len(self._error_history) > self._max_history:
            self._error_history = self._error_history[-self._max_history :]

    def get_error_history(self) -> list[ErrorInfo]:
        """Get recent error history"""
        return self._error_history.copy()

    def register_retry_strategy(self, error_code: str, strategy: Callable):
        """Register a custom retry strategy for an error code"""
        self._retry_strategies[error_code] = strategy

    async def attempt_auto_recovery(self, error_code: str, context: dict | None = None) -> bool:
        """
        Attempt automatic recovery for certain errors.
        Returns True if recovery was successful.
        """
        strategy = self._retry_strategies.get(error_code)
        if strategy:
            try:
                result = await strategy(context) if asyncio.iscoroutinefunction(strategy) else strategy(context)
                return bool(result)
            except Exception as e:
                logger.error("Auto-recovery failed for %s: %s", error_code, e)
                return False
        return False


# Global singleton
_error_recovery_system: ErrorRecoverySystem | None = None


def get_error_recovery_system() -> ErrorRecoverySystem:
    """Get or create global error recovery system"""
    global _error_recovery_system
    if _error_recovery_system is None:
        _error_recovery_system = ErrorRecoverySystem()
    return _error_recovery_system


def handle_error(
    error_code: str,
    technical_details: str | None = None,
    context: dict[str, Any] | None = None,
    show_to_user: bool = True,
) -> ErrorInfo:
    """
    Convenience function to handle an error.
    Returns ErrorInfo for display to user.
    """
    system = get_error_recovery_system()
    error_info = system.get_error_info(error_code, technical_details, context)

    logger.error("Error [%s]: %s", error_code, error_info.message)
    if technical_details:
        logger.debug("Technical details: %s", technical_details)

    return error_info
