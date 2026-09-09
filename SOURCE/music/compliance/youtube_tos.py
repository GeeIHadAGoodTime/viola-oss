"""
YouTube Music Terms of Service (TOS) Compliance Enforcement

This module centralizes all YouTube Music TOS compliance checks to ensure:
1. Video IDs are resolved via search API before playback
2. Embedded player is used (never VLC or external browser)
3. All TOS violations are logged and tracked

This enforcer addresses root causes by:
- Centralizing all TOS logic in one place (single source of truth)
- Providing clear, actionable error messages
- Enabling comprehensive violation tracking and auditing
- Making compliance requirements explicit and testable
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem

logger = get_logger(__name__)


class ViolationType(str, Enum):
    """Types of TOS violations that can occur."""

    MISSING_VIDEO_ID = "missing_video_id"
    """Video ID was not resolved via search API before playback."""

    INVALID_PLAYBACK_MODE = "invalid_playback_mode"
    """Playback mode is not embedded (e.g., VLC or external browser)."""

    VLC_BACKEND_ATTEMPTED = "vlc_backend_attempted"
    """VLC backend was attempted for YouTube Music (not allowed)."""

    EXTERNAL_BROWSER_ATTEMPTED = "external_browser_attempted"
    """External browser was attempted for YouTube Music (not allowed)."""


@dataclass
class TOSComplianceResult:
    """
    Result of a TOS compliance check.

    Attributes:
        is_compliant: True if the item passes all TOS checks
        violation_type: Type of violation if not compliant
        violation_message: Human-readable violation message
        auto_fixed: True if violation was automatically corrected
        details: Additional context about the violation or fix
    """

    is_compliant: bool
    violation_type: ViolationType | None = None
    violation_message: str | None = None
    auto_fixed: bool = False
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Initialize details dict if not provided."""
        if self.details is None:
            self.details = {}


class YouTubeMusicTOSEnforcer:
    """
    Centralized TOS compliance enforcement for YouTube Music playback.

    Ensures:
    1. Video IDs are resolved via search API before playback
    2. Embedded player is used (never VLC or external browser)
    3. All TOS violations are logged and tracked

    This class is designed to be:
    - Stateless (no mutable state)
    - Thread-safe (can be used from multiple threads)
    - Testable (all logic is pure functions)
    - Extensible (easy to add new compliance checks)
    """

    # Valid embedded playback modes for YouTube Music
    VALID_EMBEDDED_MODES = frozenset({"embedded_webview", "embedded_iframe_webview"})

    def __init__(
        self,
        logger_instance: Any | None = None,
        violation_callback: Callable[[QueueItem, ViolationType, dict[str, Any]], None] | None = None,
    ):
        """
        Initialize the TOS enforcer.

        Args:
            logger_instance: Optional logger instance (defaults to loguru logger)
            violation_callback: Optional callback to record violations
                (e.g., for integration with MusicPlayer._record_queue_failure)
        """
        self._logger = logger_instance or logger
        self._violation_callback = violation_callback

    def validate_video_id_resolved(self, item: QueueItem) -> tuple[bool, str | None]:
        """
        Check if video_id was resolved via search API.

        Args:
            item: QueueItem to validate

        Returns:
            Tuple of (is_valid, error_message)
            - is_valid: True if video_id is present
            - error_message: Human-readable error if invalid, None if valid
        """
        if not self._is_youtube_music(item):
            return True, None  # Not a YouTube Music item, skip check

        video_id = getattr(item, "video_id", None)
        if not video_id:
            error_msg = (
                f"YouTube Music item missing video_id - search API must resolve video_id before playback. "
                f"item_id={item.id} url={item.url[:80] if item.url else 'none'}"
            )
            return False, error_msg

        return True, None

    def enforce_embedded_player(self, item: QueueItem, auto_fix: bool = True) -> tuple[bool, str | None, bool]:
        """
        Ensure item uses embedded player mode.

        Args:
            item: QueueItem to validate
            auto_fix: If True, automatically fix invalid playback_mode to embedded_webview

        Returns:
            Tuple of (is_valid, error_message, was_fixed)
            - is_valid: True if playback_mode is valid
            - error_message: Human-readable error if invalid, None if valid
            - was_fixed: True if playback_mode was automatically corrected
        """
        if not self._is_youtube_music(item):
            return True, None, False  # Not a YouTube Music item, skip check

        playback_mode = getattr(item, "playback_mode", None)
        if playback_mode not in self.VALID_EMBEDDED_MODES:
            error_msg = (
                f"YouTube Music item using non-embedded playback mode. "
                f"playback_mode={playback_mode} video_id={getattr(item, 'video_id', None)} - "
                f"must use embedded_webview or embedded_iframe_webview"
            )

            if auto_fix:
                # Force embedded mode for TOS compliance
                item.playback_mode = "embedded_webview"
                self._logger.warning(
                    "YTM_TOS_ENFORCEMENT: Forced playback_mode=embedded_webview for YouTube Music item video_id=%s",
                    getattr(item, "video_id", None),
                )
                return True, None, True  # Fixed, no error

            return False, error_msg, False

        return True, None, False

    def validate_compliance(self, item: QueueItem, auto_fix_playback_mode: bool = True) -> TOSComplianceResult:
        """
        Comprehensive TOS compliance check.

        Performs all compliance checks in order:
        1. Video ID validation
        2. Embedded player enforcement

        Args:
            item: QueueItem to validate
            auto_fix_playback_mode: If True, automatically fix invalid playback_mode

        Returns:
            TOSComplianceResult with compliance status and details
        """
        if not self._is_youtube_music(item):
            # Not a YouTube Music item - always compliant
            return TOSComplianceResult(
                is_compliant=True,
                details={
                    "provider": getattr(item, "provider", None),
                    "reason": "not_youtube_music",
                },
            )

        # Check 1: Video ID validation
        video_id_valid, video_id_error = self.validate_video_id_resolved(item)
        if not video_id_valid:
            violation = ViolationType.MISSING_VIDEO_ID
            result = TOSComplianceResult(
                is_compliant=False,
                violation_type=violation,
                violation_message=video_id_error,
                details={
                    "item_id": item.id,
                    "url": item.url[:80] if item.url else None,
                    "provider": getattr(item, "provider", None),
                },
            )
            if result.details is not None:
                self._record_violation(item, violation, result.details)
            return result

        # Check 2: Embedded player enforcement
        playback_valid, playback_error, was_fixed = self.enforce_embedded_player(item, auto_fix_playback_mode)
        if not playback_valid:
            violation = ViolationType.INVALID_PLAYBACK_MODE
            result = TOSComplianceResult(
                is_compliant=False,
                violation_type=violation,
                violation_message=playback_error,
                auto_fixed=was_fixed,
                details={
                    "item_id": item.id,
                    "video_id": getattr(item, "video_id", None),
                    "playback_mode": getattr(item, "playback_mode", None),
                    "provider": getattr(item, "provider", None),
                },
            )
            if result.details is not None:
                self._record_violation(item, violation, result.details)
            return result

        # All checks passed
        video_id = getattr(item, "video_id", None)
        playback_mode = getattr(item, "playback_mode", None)
        self._logger.info(
            "YTM_TOS_COMPLIANT: provider=youtube_music video_id=%s title=%s playback_mode=%s "
            "flow=search_api->video_id->embedded_player",
            video_id,
            getattr(item, "title", "Unknown")[:50],
            playback_mode,
        )
        return TOSComplianceResult(
            is_compliant=True,
            auto_fixed=was_fixed,  # Indicate if playback mode was auto-fixed
            details={
                "video_id": video_id,
                "playback_mode": playback_mode,
                "provider": "youtube_music",
            },
        )

    def check_vlc_backend_allowed(self, item: QueueItem) -> tuple[bool, str | None]:
        """
        Check if VLC backend is allowed for this item.

        Args:
            item: QueueItem to check

        Returns:
            Tuple of (is_allowed, error_message)
            - is_allowed: True if VLC backend can be used
            - error_message: Human-readable error if not allowed, None if allowed
        """
        if not self._is_youtube_music(item):
            return True, None  # Not YouTube Music, VLC is allowed

        # VLC backend is never allowed for YouTube Music
        error_msg = (
            f"Attempted to select VLC backend for YouTube Music item. "
            f"YouTube Music MUST use embedded player only. "
            f"item_id={item.id} video_id={getattr(item, 'video_id', None) or 'none'}"
        )
        return False, error_msg

    def check_external_browser_allowed(self, item: QueueItem) -> tuple[bool, str | None]:
        """
        Check if external browser is allowed for this item.

        Args:
            item: QueueItem to check

        Returns:
            Tuple of (is_allowed, error_message)
            - is_allowed: True if external browser can be used
            - error_message: Human-readable error if not allowed, None if allowed
        """
        if not self._is_youtube_music(item):
            return True, None  # Not YouTube Music, external browser is allowed

        # External browser is never allowed for YouTube Music
        error_msg = (
            f"Attempted to use external browser for YouTube Music item. "
            f"YouTube Music MUST use embedded player only. "
            f"item_id={item.id} video_id={getattr(item, 'video_id', None) or 'none'}"
        )
        return False, error_msg

    def validate_video_id_from_search_result(
        self, track_id: str, track_title: str, query: str, video_id: str | None
    ) -> tuple[bool, str | None]:
        """
        Validate that video_id was extracted from search result.

        This is used during resolution to ensure search API returns video_id.

        Args:
            track_id: Track ID from search result
            track_title: Track title from search result
            query: Original search query
            video_id: Extracted video_id (may be None)

        Returns:
            Tuple of (is_valid, error_message)
            - is_valid: True if video_id is present
            - error_message: Human-readable error if invalid, None if valid
        """
        if not video_id:
            error_msg = (
                f"Search result for '{query}' is missing video_id. "
                f"This is a system error. track_id={track_id} title={track_title!r}"
            )
            return False, error_msg

        return True, None

    def record_violation(self, item: QueueItem, violation_type: ViolationType, details: dict[str, Any]) -> None:
        """
        Record TOS violation for auditing.

        This method can be overridden or extended to integrate with
        violation tracking systems.

        Args:
            item: QueueItem that violated TOS
            violation_type: Type of violation
            details: Additional context about the violation
        """
        self._record_violation(item, violation_type, details)

    def _record_violation(self, item: QueueItem, violation_type: ViolationType, details: dict[str, Any]) -> None:
        """
        Internal method to record violations.

        Logs the violation and calls the violation callback if provided.
        """
        self._logger.error(
            "YTM_TOS_VIOLATION: violation_type=%s item_id=%s video_id=%s details=%s",
            violation_type.value,
            item.id,
            getattr(item, "video_id", None),
            details,
        )

        if self._violation_callback:
            try:
                self._violation_callback(item, violation_type, details)
            except Exception as exc:
                self._logger.exception("Failed to call violation callback: %r", exc)

    # All YouTube provider identifiers that require embedded playback (TOS).
    _YOUTUBE_PROVIDERS = frozenset({"youtube_music", "youtube_iframe", "youtube"})

    @staticmethod
    def _is_youtube_music(item: QueueItem) -> bool:
        """
        Check if item is from any YouTube provider.

        Returns True for youtube_music, youtube_iframe, and youtube providers.
        All YouTube variants require embedded playback for TOS compliance.

        Args:
            item: QueueItem to check

        Returns:
            True if item is from a YouTube provider
        """
        provider_id = getattr(item, "provider", None)
        return provider_id in YouTubeMusicTOSEnforcer._YOUTUBE_PROVIDERS
