"""
music/exceptions.py
Centralized exception definitions for music player.

Breaking circular dependencies between the MusicPlayer facade and
error_handler.py by extracting shared exception classes to a separate module.

All exceptions inherit from core.exceptions.ViolaError for consistent
error handling across the application.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.exceptions import ErrorContext, ViolaError


class PlayerError(ViolaError):
    """Base exception for all music player errors.

    Inherits from ViolaError for consistent error handling.
    """

    def __init__(self, message: str, context: ErrorContext | None = None) -> None:
        super().__init__(message, context=context)


class ResolutionError(PlayerError):
    """
    Error resolving a music query (e.g., YouTube search failed).

    Includes structured metadata for downstream consumers to determine whether
    retries are appropriate and to emit consistent failure envelopes.
    """

    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        error_context = ErrorContext(
            component="music.resolution",
            operation="resolve",
            params=dict(context or {}),
            user_message="Couldn't find that track. Try a different search.",
            recovery_hint="Check your search query or try a different source.",
        )
        super().__init__(message, context=error_context)
        self.code = code or message
        self.resolution_context = dict(context or {})
        self.retryable = retryable


class BackendError(PlayerError):
    """Error with the playback backend (VLC, ffplay, etc.)."""

    def __init__(self, message: str, backend: str = "unknown") -> None:
        error_context = ErrorContext(
            component=f"music.backend.{backend}",
            operation="playback",
            params={"backend": backend},
            user_message="Audio playback failed. Try playing again or check audio settings.",
            recovery_hint=f"Check {backend} installation and audio settings.",
        )
        super().__init__(message, context=error_context)
        self.backend = backend


class InvalidOperation(PlayerError):
    """Invalid operation attempted (e.g., skip when queue empty)."""

    def __init__(self, message: str, operation: str = "unknown") -> None:
        error_context = ErrorContext(
            component="music.player",
            operation=operation,
            params={"operation": operation},
            user_message="That action isn't available right now.",
            recovery_hint="Check the current playback state.",
        )
        super().__init__(message, context=error_context)
        self.operation = operation


class MusicQueueError(PlayerError):
    """Music player queue operation error.

    This is for high-level music player queue operations.
    """

    def __init__(self, message: str, operation: str = "queue") -> None:
        error_context = ErrorContext(
            component="music.queue",
            operation=operation,
            user_message="Queue operation failed.",
            recovery_hint="Try the operation again.",
        )
        super().__init__(message, context=error_context)


# Backward compatibility alias - deprecated, use MusicQueueError
QueueError = MusicQueueError


class MusicConfigurationError(PlayerError):
    """Music-specific configuration or setup error.

    Used specifically for music player configuration issues.
    """

    provider_id: str | None = None
    user_id: str | None = None

    def __init__(
        self,
        message: str,
        provider_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        error_context = ErrorContext(
            component="music.config",
            operation="configure",
            params={"provider_id": provider_id, "user_id": user_id},
            user_message="Music isn't set up yet. Say connect spotify or connect youtube.",
            recovery_hint="Verify your music provider settings.",
        )
        super().__init__(message, context=error_context)
        self.provider_id = provider_id
        self.user_id = user_id


# Backward compatibility alias - deprecated, use MusicConfigurationError
ConfigurationError = MusicConfigurationError
