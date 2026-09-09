"""
Error taxonomy for provider integrations.

All exceptions inherit from core.exceptions.ViolaError for consistent
error handling across the application.
"""

from __future__ import annotations

from core.exceptions import ErrorContext, ErrorSeverity, ViolaError


class ProviderError(ViolaError):
    """Base class for provider-related failures.

    Inherits from ViolaError for consistent error handling.
    """

    provider_name: str | None = None
    error_code: str | None = None

    def __init__(
        self,
        message: str,
        *,
        provider_name: str | None = None,
        error_code: str | None = None,
    ) -> None:
        error_context = ErrorContext(
            component=f"music.provider.{provider_name or 'unknown'}",
            operation="api_call",
            params={"provider_name": provider_name, "error_code": error_code},
            user_message=f"Issue with {provider_name or 'music'} provider.",
            recovery_hint="Check your account connection and try again.",
        )
        super().__init__(message, context=error_context)
        self.provider_name = provider_name
        self.error_code = error_code


class ProviderAuthenticationError(ProviderError):
    """Raised when user authorization is invalid or expired."""

    severity = ErrorSeverity.HIGH


class ProviderRateLimitError(ProviderError):
    """Raised when the provider signals quota or rate-limit issues."""

    severity = ErrorSeverity.MEDIUM
    retryable = True


class ProviderNetworkError(ProviderError):
    """Raised for transient network faults."""

    severity = ErrorSeverity.MEDIUM
    retryable = True


class ProviderNotSupportedError(ProviderError):
    """Raised when a requested feature is not supported by the provider."""

    severity = ErrorSeverity.LOW


class NoActiveMusicProviderError(ViolaError):
    """Raised when a music action requires an active provider but none is configured."""

    severity = ErrorSeverity.HIGH

    def __init__(self, message: str = "No active music provider configured") -> None:
        error_context = ErrorContext(
            component="music.provider",
            operation="get_active",
            user_message="No music provider is set up. Say 'connect spotify' or 'connect youtube' to get started.",
            recovery_hint="Connect a music service like Spotify or YouTube Music.",
        )
        super().__init__(message, context=error_context)


class MusicProviderUnavailableError(ViolaError):
    """
    Raised when the configured active provider is not usable in the current build/profile.

    Attributes:
        technical_details: Optional dict with structured error details (root_cause, provider, etc.)
    """

    severity = ErrorSeverity.HIGH

    def __init__(self, message: str, *, technical_details: dict | None = None) -> None:
        error_context = ErrorContext(
            component="music.provider",
            operation="access",
            params={"technical_details": technical_details or {}},
            user_message="Music provider is not available. Check your settings.",
            recovery_hint="Re-link your music service or try a different provider.",
        )
        super().__init__(message, context=error_context)
        self.technical_details = technical_details or {}


class MusicProviderNotRegistered(MusicProviderUnavailableError):
    """Raised when a music provider is not registered in the provider registry.

    Note: This is distinct from music.consent.exceptions.ConsentProviderNotRegistered
    and music.providers.registry.RegistryProviderNotFound.
    """


# Backward compatibility alias - deprecated, use MusicProviderNotRegistered
ProviderNotRegistered = MusicProviderNotRegistered


class ProviderNotConfiguredError(MusicProviderUnavailableError):
    """Raised when credentials/configuration for a provider are missing."""

    def __init__(
        self,
        message: str,
        *,
        provider_name: str | None = None,
        technical_details: dict | None = None,
    ) -> None:
        super().__init__(message, technical_details=technical_details)
        self.provider_name = provider_name


class MusicTrackNotFoundError(ViolaError):
    """
    Raised when a track cannot be found via the provider, but the provider itself is healthy.

    This is distinct from MusicProviderUnavailableError, which indicates the provider
    is not linked, misconfigured, or experiencing auth/API issues.

    Attributes:
        technical_details: Optional dict with structured error details (root_cause, provider, query, etc.)
    """

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, message: str, *, technical_details: dict | None = None) -> None:
        error_context = ErrorContext(
            component="music.provider",
            operation="search",
            params={"technical_details": technical_details or {}},
            user_message="Couldn't find that track. Try a different search.",
            recovery_hint="Check your search query or try a different service.",
        )
        super().__init__(message, context=error_context)
        self.technical_details = technical_details or {}


__all__ = [
    "MusicProviderUnavailableError",
    "MusicTrackNotFoundError",
    "NoActiveMusicProviderError",
    "ProviderAuthenticationError",
    "ProviderError",
    "ProviderNetworkError",
    "ProviderNotConfiguredError",
    "ProviderNotRegistered",
    "ProviderNotSupportedError",
    "ProviderRateLimitError",
]
