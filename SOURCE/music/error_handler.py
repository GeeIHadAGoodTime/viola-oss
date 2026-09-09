"""
music/error_handler.py
Unified error handling and user-friendly messaging for music player.

Design Principles:
- Classify errors into categories (transient, permanent, user-fixable)
- Convert technical errors to user-friendly messages
- Provide actionable feedback
- Support retry logic for transient errors
- Plugin-friendly with custom error handlers
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar, cast

from core.logging_config import get_logger
from music.exceptions import BackendError, ResolutionError
from music.providers.errors import (
    MusicProviderUnavailableError,
    MusicTrackNotFoundError,
    NoActiveMusicProviderError,
)


class ErrorSeverity(Enum):
    """
    User-facing error severity levels for music error messages.

    Note: This is distinct from core.exceptions.ErrorSeverity which uses
    LOW/MEDIUM/HIGH/CRITICAL for monitoring/alerting. This enum uses
    log-level-style values (INFO/WARNING/ERROR/CRITICAL) that map directly
    to user-facing display states in the UI.
    """

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorCategory(Enum):
    """Error categories for classification."""

    TRANSIENT = "transient"  # Temporary, retry may work
    PERMANENT = "permanent"  # Won't work without changes
    CONFIGURATION = "configuration"  # User settings issue
    NETWORK = "network"  # Network connectivity
    RESOURCE = "resource"  # Resource unavailable (geo-block, private, etc)
    UNKNOWN = "unknown"  # Unknown error


@dataclass
class ErrorInfo:
    """
    Structured error information.

    Attributes:
        category: Error category
        severity: Error severity
        message: User-friendly message
        technical_details: Technical error details
        suggestion: Actionable suggestion for user
        retryable: Whether retry might succeed
    """

    category: ErrorCategory
    severity: ErrorSeverity
    message: str
    technical_details: str
    suggestion: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "message": self.message,
            "technical_details": self.technical_details,
            "suggestion": self.suggestion,
            "retryable": self.retryable,
        }


class ErrorClassifier:
    """
    Classifies errors and generates user-friendly messages.

    Extensible design allows plugins to register custom error handlers.
    """

    def __init__(self):
        """Initialize error classifier."""
        self._handlers: dict[type[BaseException], Callable[[Exception], ErrorInfo]] = {}
        self._pattern_handlers: list[tuple[str, Callable[[Exception], ErrorInfo]]] = []
        self._register_default_handlers()

    def _register_default_handlers(self):
        """Register default error handlers."""
        # YouTube signature/SABR issues
        self.register_pattern_handler(
            ["signature extraction failed", "sabr streaming", "player client"],
            lambda e: ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.ERROR,
                message="YouTube blocked playback due to outdated session.",
                technical_details=str(e),
                suggestion="Say 'reconnect YouTube Music' and I'll refresh the link for you.",
                retryable=False,
            ),
        )

        # Video unavailable/private (only for actual provider search failures, not provider linking issues)
        # Note: This should only match when the provider API actually reports a missing/blocked track,
        # not when there's no provider linked (that's handled by NoActiveMusicProviderError)
        self.register_pattern_handler(
            ["unavailable", "private video", "video not available", "removed"],
            lambda e: ErrorInfo(
                category=ErrorCategory.RESOURCE,
                severity=ErrorSeverity.WARNING,
                message="Video is unavailable, private, or has been removed.",
                technical_details=str(e),
                suggestion="Try searching for an alternative version.",
                retryable=False,
            ),
        )

        # Geo-restrictions
        self.register_pattern_handler(
            ["not available in your country", "geo", "region"],
            lambda e: ErrorInfo(
                category=ErrorCategory.RESOURCE,
                severity=ErrorSeverity.WARNING,
                message="Video is not available in your region.",
                technical_details=str(e),
                suggestion="Try a different version or use a VPN.",
                retryable=False,
            ),
        )

        # Network errors
        self.register_pattern_handler(
            ["network", "connection", "timeout", "timed out", "unreachable"],
            lambda e: ErrorInfo(
                category=ErrorCategory.NETWORK,
                severity=ErrorSeverity.ERROR,
                message="Network connection issue.",
                technical_details=str(e),
                suggestion="Check your internet connection and try again.",
                retryable=True,
            ),
        )

        # URL expiration
        self.register_pattern_handler(
            ["403", "expired", "invalid url", "url not found"],
            lambda e: ErrorInfo(
                category=ErrorCategory.TRANSIENT,
                severity=ErrorSeverity.WARNING,
                message="Stream URL expired or is invalid.",
                technical_details=str(e),
                suggestion="Retrying with fresh URL...",
                retryable=True,
            ),
        )

        # Resolution errors
        self.register_type_handler(
            ResolutionError,
            lambda e: ErrorInfo(
                category=ErrorCategory.TRANSIENT,
                severity=ErrorSeverity.ERROR,
                message="Could not find or resolve the requested song.",
                technical_details=str(e),
                suggestion="Try a different search query or URL.",
                retryable=True,
            ),
        )

        # Backend errors
        self.register_type_handler(
            BackendError,
            lambda e: ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.CRITICAL,
                message="Playback backend error.",
                technical_details=str(e),
                suggestion="Check VLC installation or try restarting the app.",
                retryable=False,
            ),
        )

        # No active music provider
        self.register_type_handler(
            NoActiveMusicProviderError,
            lambda e: ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.WARNING,
                message="No music provider is linked.",
                technical_details=str(e),
                suggestion="Say 'connect Spotify' or 'connect YouTube Music' and I'll get it linked for you.",
                retryable=False,
            ),
        )

        # Music provider unavailable
        self.register_type_handler(
            MusicProviderUnavailableError,
            lambda e: self._handle_music_provider_unavailable(cast(MusicProviderUnavailableError, e)),
        )

        # Track not found (provider healthy but no results)
        self.register_type_handler(
            MusicTrackNotFoundError,
            lambda e: ErrorInfo(
                category=ErrorCategory.RESOURCE,
                severity=ErrorSeverity.WARNING,
                message=(str(e) if str(e) else "I couldn't find that track. Try a different name or artist?"),
                technical_details=str(e),
                suggestion="Try searching with a different query or check the spelling.",
                retryable=True,
            ),
        )

        # NotImplementedError (legacy scraping removed)
        # This should only be hit in dev/test paths or explicit legacy toggles
        self.register_type_handler(
            NotImplementedError,
            lambda e: ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.ERROR,
                message="This feature is not available. YouTube scraping has been removed.",
                technical_details=str(e),
                suggestion="Use a linked music provider (Spotify or YouTube Music) instead.",
                retryable=False,
            ),
        )

    def _handle_music_provider_unavailable(self, error: MusicProviderUnavailableError) -> ErrorInfo:
        """
        Handle MusicProviderUnavailableError with smart message selection.

        Distinguishes between:
        - Provider actually disabled in build (show "disabled in this build")
        - App-level config issues (Data API unavailable) - show developer config message
        - User account issues (token expired, revoked) - show "check your link" message
        - Generic provider failures
        """
        error_msg = str(error).lower()
        technical_details = getattr(error, "technical_details", None) or {}

        # Check if error indicates provider is actually disabled in build
        # vs just failing to resolve/connect
        is_build_disabled = any(
            phrase in error_msg
            for phrase in [
                "disabled in this build",
                "unsupported in this build",
                "not supported in this build",
                "scraping disabled",
            ]
        )

        # Check error kind from technical_details
        error_kind = technical_details.get("kind")
        data_api_unavailable = technical_details.get("data_api_unavailable", False)
        is_app_config = error_kind == "APP_CONFIG" or data_api_unavailable

        # Check if it's a YouTube Music provider issue
        is_youtube_music = (
            "youtube" in error_msg
            or "youtube_music" in error_msg
            or technical_details.get("provider") == "youtube_music"
        )

        if is_build_disabled:
            # Provider actually disabled - show build restriction message
            return ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.WARNING,
                message="Your current music provider is disabled or unsupported in this build.",
                technical_details=str(error),
                suggestion="Say 'connect Spotify' or 'connect YouTube Music' and I'll set it up.",
                retryable=False,
            )
        elif is_app_config and is_youtube_music:
            # App-level configuration issue (Data API unavailable)
            # This is NOT a user account problem - it's a developer configuration issue
            # When degraded mode is used, this error should not be shown to the user
            # (degraded mode silently falls back to embedded search page)
            # However, if we reach here, degraded mode might not have been triggered
            # So we show a non-blaming message
            return ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.INFO,  # Lower severity - not a user error
                message="YouTube Music's search API isn't available, so I opened the YouTube Music search page instead.",
                technical_details=str(error),
                suggestion="You can use the embedded YouTube Music window to search and play music.",
                retryable=False,
            )
        elif error_kind == "USER_ACCOUNT" and is_youtube_music:
            # User account issue (token expired, revoked, etc.)
            return ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.WARNING,
                message="I couldn't play that using YouTube Music — the link may need refreshing.",
                technical_details=str(error),
                suggestion="Say 'reconnect YouTube Music' and I'll refresh the link for you.",
                retryable=True,
            )
        elif is_youtube_music:
            # YouTube Music linked but failing - show actionable message
            # Default to user account issue unless we know otherwise
            return ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.WARNING,
                message="I couldn't play that using YouTube Music — the link may need refreshing.",
                technical_details=str(error),
                suggestion="Say 'reconnect YouTube Music' and I'll refresh the link for you.",
                retryable=True,
            )
        else:
            # Generic provider failure
            return ErrorInfo(
                category=ErrorCategory.CONFIGURATION,
                severity=ErrorSeverity.WARNING,
                message="I couldn't play that using your music provider — the connection may need refreshing.",
                technical_details=str(error),
                suggestion="Say 'reconnect Spotify' or 'reconnect YouTube Music' and I'll refresh the link for you.",
                retryable=True,
            )

    def register_type_handler(
        self,
        error_type: type[BaseException],
        handler: Callable[[Exception], ErrorInfo],
    ) -> None:
        """
        Register handler for specific error type.

        Args:
            error_type: Exception type to handle
            handler: Function that converts exception to ErrorInfo
        """
        self._handlers[error_type] = handler

    def register_pattern_handler(
        self,
        patterns: Sequence[str],
        handler: Callable[[Exception], ErrorInfo],
    ) -> None:
        """
        Register handler for error message patterns.

        Args:
            patterns: List of string patterns to match (case-insensitive)
            handler: Function that converts exception to ErrorInfo
        """
        for pattern in patterns:
            self._pattern_handlers.append((pattern.lower(), handler))

    def classify(self, error: Exception) -> ErrorInfo:
        """
        Classify error and generate user-friendly information.

        Args:
            error: Exception to classify

        Returns:
            ErrorInfo with classification and friendly message
        """
        # Try type-based handlers first
        for error_type, handler in self._handlers.items():
            if isinstance(error, error_type):
                return handler(error)

        # Try pattern-based handlers
        error_str = str(error).lower()
        for pattern, handler in self._pattern_handlers:
            if pattern in error_str:
                return handler(error)

        # Default: unknown error
        error_info = ErrorInfo(
            category=ErrorCategory.UNKNOWN,
            severity=ErrorSeverity.ERROR,
            message="An unexpected error occurred.",
            technical_details=str(error),
            suggestion="Check logs for details or report if issue persists.",
            retryable=False,
        )

        try:
            from music.compliance.services import (
                get_services,
            )  # Lazy import to avoid cycles

            services = get_services()
            services.telemetry.record_error("unknown", error_info.category.value)
            if error_info.severity in {ErrorSeverity.ERROR, ErrorSeverity.CRITICAL}:
                services.audit.record_compliance_note(
                    provider="unknown",
                    message=f"{error_info.category.value}:{error_info.message}",
                    severity=error_info.severity.value,
                )
        except Exception:
            get_logger(__name__).exception("Failed to record compliance audit for error")

        return error_info


T = TypeVar("T")


class RetryStrategy:
    """
    Retry strategy for transient errors.

    Configurable retry logic with exponential backoff.
    """

    def __init__(
        self,
        max_retries: int = 2,
        initial_delay: float = 0.5,
        max_delay: float = 5.0,
        exponential_base: float = 2.0,
    ):
        """
        Initialize retry strategy.

        Args:
            max_retries: Maximum retry attempts
            initial_delay: Initial delay in seconds
            max_delay: Maximum delay in seconds
            exponential_base: Base for exponential backoff
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base

    def get_delay(self, attempt: int) -> float:
        """
        Calculate delay for given attempt.

        Args:
            attempt: Attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        delay = self.initial_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)

    async def execute(
        self,
        func: Callable[..., T | Awaitable[T]],
        *args,
        on_retry: Callable[[int, Exception], None] | None = None,
        **kwargs,
    ) -> T:
        """
        Execute function with retry logic.

        Args:
            func: Function to execute
            *args: Function arguments
            on_retry: Callback called on each retry (attempt_number, error)
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            Last exception if all retries fail
        """
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                # Try to execute function
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await cast(Awaitable[T], result)
                return result

            except Exception as e:
                last_exception = e

                # Don't retry on last attempt
                if attempt >= self.max_retries:
                    break

                # Check if error is retryable
                classifier = ErrorClassifier()
                error_info = classifier.classify(e)

                if not error_info.retryable:
                    # Don't retry non-retryable errors
                    break

                # Call retry callback
                if on_retry:
                    on_retry(attempt, e)

                # Wait before retry
                delay = self.get_delay(attempt)
                await asyncio.sleep(delay)

        # All retries failed, raise last exception
        if last_exception is not None:
            raise last_exception
        raise RuntimeError("RetryStrategy failed without encountering an exception.")


# Global instances
_error_classifier = ErrorClassifier()
_retry_strategy = RetryStrategy()


def classify_error(error: Exception) -> ErrorInfo:
    """
    Classify error using global classifier.

    Args:
        error: Exception to classify

    Returns:
        ErrorInfo with classification
    """
    return _error_classifier.classify(error)


def get_friendly_message(error: Exception) -> str:
    """
    Get user-friendly message for error.

    Args:
        error: Exception

    Returns:
        Friendly error message
    """
    error_info = classify_error(error)

    message = error_info.message
    if error_info.suggestion:
        message += f" {error_info.suggestion}"

    return message


async def retry_with_strategy(
    func: Callable[..., T | Awaitable[T]],
    *args,
    max_retries: int = 2,
    on_retry: Callable[[int, Exception], None] | None = None,
    **kwargs,
) -> T:
    """
    Execute function with retry strategy.

    Args:
        func: Function to execute
        *args: Function arguments
        max_retries: Maximum retry attempts
        on_retry: Callback on retry
        **kwargs: Function keyword arguments

    Returns:
        Function result
    """
    strategy = RetryStrategy(max_retries=max_retries)
    return await strategy.execute(func, *args, on_retry=on_retry, **kwargs)


# Export public API
__all__ = [
    "ErrorCategory",
    "ErrorClassifier",
    "ErrorInfo",
    "ErrorSeverity",
    "RetryStrategy",
    "classify_error",
    "get_friendly_message",
    "retry_with_strategy",
]
