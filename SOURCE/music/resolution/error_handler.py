"""
music/resolution/error_handler.py

Centralized error handling and classification for YouTube Music resolution.

This module extracts error handling logic from the main player class to:
1. Classify errors into categories (APP_CONFIG, QUOTA, USER_ACCOUNT, NETWORK)
2. Preserve root cause information for debugging
3. Provide consistent error handling across the codebase

Design Principles:
- Single Responsibility: Only handles error classification and decision-making
- Future-Proof: Extensible error classification system
- Root Cause Preservation: Maintains technical_details for debugging
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from core.logging_config import get_logger
from music.providers.errors import MusicProviderUnavailableError


class ErrorKind(str, Enum):
    """
    Classification of resolution errors.

    These categories determine how errors should be handled:
    - APP_CONFIG: Configuration issues (API not enabled, wrong project, etc.)
    - QUOTA: API quota/rate limit exceeded
    - USER_ACCOUNT: OAuth token issues (invalid, expired, revoked)
    - NETWORK: Network/transport errors
    - UNKNOWN: Unclassified errors
    """

    APP_CONFIG = "APP_CONFIG"
    QUOTA = "QUOTA"
    USER_ACCOUNT = "USER_ACCOUNT"
    NETWORK = "NETWORK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ErrorHandlingResult:
    """
    Result of error handling decision.

    Attributes:
        error_kind: Classified error kind
        should_re_raise: Whether the error should be re-raised
        preserved_error: Original error with preserved root_cause (if re-raising)
    """

    error_kind: ErrorKind
    should_re_raise: bool = True
    preserved_error: MusicProviderUnavailableError | None = None


class ResolutionErrorHandler:
    """
    Handles resolution errors and determines appropriate responses.

    This class centralizes error classification and decision-making logic
    that was previously scattered throughout the music player implementation.

    Key Responsibilities:
    1. Classify errors from technical_details
    2. Preserve root_cause information
    3. Provide consistent error handling behavior
    """

    def __init__(self, logger: logging.Logger | None = None):
        """
        Initialize error handler.

        Args:
            logger: Optional logger for error handling decisions
        """
        self._logger = logger or get_logger(__name__)

    def handle_provider_error(
        self,
        error: MusicProviderUnavailableError,
        query: str,
        source: str,
    ) -> ErrorHandlingResult:
        """
        Handle provider error and determine next steps.

        This is the main entry point for error handling. It:
        1. Classifies the error
        2. Preserves root_cause information
        3. Returns a decision result

        Args:
            error: The MusicProviderUnavailableError to handle
            query: The query that failed (for context)
            source: The source type (for context)

        Returns:
            ErrorHandlingResult with handling decision
        """
        technical_details = error.technical_details or {}
        error_kind = self.classify_error(error)

        self._logger.info(
            "Resolution error handler: classified error kind=%s for query='%s'",
            error_kind.value,
            query[:50],
        )

        # Preserve root_cause if present, otherwise add generic one
        preserved_error: MusicProviderUnavailableError | None = None
        if technical_details.get("root_cause"):
            # Re-raise as-is with preserved root_cause
            preserved_error = error
        else:
            # If no root_cause, add generic one
            preserved_error = MusicProviderUnavailableError(
                str(error),
                technical_details={
                    "root_cause": "provider_resolution_failed",
                    "provider": technical_details.get("provider", "unknown"),
                    "query": query[:50],
                    "source": source,
                    "error_kind": error_kind.value,
                },
            )

        return ErrorHandlingResult(
            error_kind=error_kind,
            should_re_raise=True,
            preserved_error=preserved_error,
        )

    def classify_error(self, error: MusicProviderUnavailableError) -> ErrorKind:
        """
        Classify error type from technical_details.

        This method examines the error's technical_details to determine
        the error category. It checks multiple fields for robustness:
        - kind: Primary error kind field
        - error_kind: Alternative error kind field (enum value)
        - data_api_unavailable: Flag for API unavailability
        - status_code: HTTP status code (if available)

        Args:
            error: The error to classify

        Returns:
            ErrorKind enum value
        """
        technical_details = error.technical_details or {}

        # Check primary error kind field
        error_kind = technical_details.get("kind")
        error_kind_value = technical_details.get("error_kind")  # Alternative field
        data_api_unavailable = technical_details.get("data_api_unavailable", False)
        status_code = technical_details.get("status_code")

        # Classify based on multiple indicators
        # APP_CONFIG: Google Cloud project misconfiguration
        if error_kind == "APP_CONFIG" or error_kind_value == "api_auth" or data_api_unavailable:
            return ErrorKind.APP_CONFIG

        # QUOTA: API quota limits exceeded
        if error_kind == "QUOTA" or error_kind_value == "api_quota":
            return ErrorKind.QUOTA

        # USER_ACCOUNT: User OAuth token invalid/expired/revoked
        if error_kind == "USER_ACCOUNT" or error_kind_value == "user_account":
            return ErrorKind.USER_ACCOUNT

        # NETWORK: Network/transport errors (429, 503, etc.)
        if status_code in (429, 503, 504):
            # These could be quota or network, but 429 is typically quota
            if status_code == 429:
                return ErrorKind.QUOTA
            return ErrorKind.NETWORK

        # Default to UNKNOWN if no classification matches
        return ErrorKind.UNKNOWN

    def wrap_provider_error(
        self,
        original_error: Exception,
        query: str,
        user_id: str,
        stage: str,
        root_cause: str,
        user_message: str | None = None,
    ) -> MusicProviderUnavailableError:
        """
        Wrap a generic exception as MusicProviderUnavailableError.

        This is used when catching unexpected exceptions during provider
        operations (e.g., search_tracks, resolve_stream) to ensure consistent
        error handling.

        Args:
            original_error: The original exception
            query: The query that failed
            user_id: The user ID (for context)
            stage: The stage where the error occurred (e.g., "search_tracks")
            root_cause: The root cause identifier
            user_message: Optional user-friendly message (auto-generated if None)

        Returns:
            MusicProviderUnavailableError with structured technical_details
        """
        error_msg = str(original_error)
        exc_type = type(original_error).__name__
        exc_repr = repr(original_error)

        if user_message is None:
            # Auto-generate user message based on error type
            if "Please" in error_msg or "unavailable" in error_msg:
                user_message = error_msg
            else:
                user_message = "YouTube is busy right now. Try again in a moment."

        self._logger.warning(
            "Resolution error handler: Wrapping exception query=%r user_id=%s root_cause=%s stage=%s exc_type=%s",
            query,
            user_id,
            root_cause,
            stage,
            exc_type,
        )

        wrapped_error = MusicProviderUnavailableError(
            f"Unable to resolve '{query}' using youtube_music. {user_message}",
            technical_details={
                "root_cause": root_cause,
                "provider": "youtube_music",
                "query": query[:50],
                "user_id": user_id,
                "stage": stage,
                "exc_type": exc_type,
                "exc": exc_repr,
            },
        )
        # Set the cause to preserve exception chain
        wrapped_error.__cause__ = original_error
        return wrapped_error
