"""Custom exceptions for the consent orchestrator.

All exceptions inherit from core.exceptions.ViolaError for consistent
error handling across the application.
"""

from __future__ import annotations

from core.exceptions import ErrorContext, ErrorSeverity, ViolaError


class ConsentError(ViolaError):
    """Base error for consent orchestration failures.

    Inherits from ViolaError for consistent error handling.
    """

    severity = ErrorSeverity.HIGH

    def __init__(self, message: str, context: ErrorContext | None = None) -> None:
        if context is None:
            context = ErrorContext(
                component="music.consent",
                operation="consent",
                user_message="Authorization issue. Please re-link your account.",
                recovery_hint="Say 'connect spotify' or 'connect youtube' to re-link your account.",
            )
        super().__init__(message, context=context)


class ConsentProviderNotRegistered(ConsentError):
    """Raised when a consent provider is not registered in the consent registry.

    Note: This is distinct from music.providers.errors.MusicProviderNotRegistered
    and music.providers.registry.RegistryProviderNotFound. This class is specifically
    for consent orchestrator provider registration issues.
    """

    def __init__(self, provider_id: str) -> None:
        error_context = ErrorContext(
            component="music.consent",
            operation="get_provider",
            params={"provider_id": provider_id},
            user_message=f"Music service '{provider_id}' is not available.",
            recovery_hint="Check that the music service is properly configured.",
        )
        super().__init__(
            f"Consent provider '{provider_id}' is not registered",
            context=error_context,
        )
        self.provider_id = provider_id


# Backward compatibility alias - deprecated, use ConsentProviderNotRegistered
ProviderNotRegistered = ConsentProviderNotRegistered


class SessionNotFound(ConsentError):
    """Raised when attempting to access an unknown session."""

    def __init__(self, session_id: str) -> None:
        error_context = ErrorContext(
            component="music.consent.session",
            operation="get_session",
            params={"session_id": session_id},
            user_message="Authorization session not found. Please try again.",
            recovery_hint="Start the authorization process again.",
        )
        super().__init__(
            f"Consent session '{session_id}' not found",
            context=error_context,
        )
        self.session_id = session_id


class SessionExpired(ConsentError):
    """Raised when a session is no longer active."""

    def __init__(self, session_id: str) -> None:
        error_context = ErrorContext(
            component="music.consent.session",
            operation="validate_session",
            params={"session_id": session_id},
            user_message="Authorization session expired. Please try again.",
            recovery_hint="Start the authorization process again.",
        )
        super().__init__(
            f"Consent session '{session_id}' has expired",
            context=error_context,
        )
        self.session_id = session_id


class TokenVaultError(ConsentError):
    """Raised for vault persistence/encryption issues."""

    def __init__(self, message: str = "Token vault operation failed") -> None:
        error_context = ErrorContext(
            component="music.consent.vault",
            operation="vault_operation",
            user_message="Secure storage issue. Please re-link your account.",
            recovery_hint="Try re-connecting your music service.",
        )
        super().__init__(message, context=error_context)


class TokenRotationError(ConsentError):
    """Raised when token rotation fails in a non-recoverable way."""

    retryable = True

    def __init__(self, message: str = "Token rotation failed") -> None:
        error_context = ErrorContext(
            component="music.consent.token",
            operation="rotate_token",
            user_message="Token refresh failed. Please re-link your account.",
            recovery_hint="Say 'connect spotify' or 'connect youtube' to re-link your account.",
        )
        super().__init__(message, context=error_context)
