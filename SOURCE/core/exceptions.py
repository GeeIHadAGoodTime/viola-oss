"""
NOVVIOLA Exception Hierarchy

All application-specific exceptions inherit from ViolaError for:
- Consistent error handling patterns
- Easy distinction from third-party errors
- Structured error context for debugging and user feedback

Usage:
    from core.exceptions import PlaybackError, ServiceTimeoutError

    try:
        player.play(url)
    except PlaybackError as e:
        logger.error("Playback failed: %s", e)
        # Show user-friendly message
        show_error(e.user_friendly_message())
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from core.json_types import JsonObject, to_json_object
from core.logging_config import get_logger

if TYPE_CHECKING:
    from diagnostics.error_classification import ErrorCategory


class ErrorSeverity(Enum):
    """Error severity levels for monitoring/alerting."""

    LOW = auto()  # Log and continue
    MEDIUM = auto()  # User notification needed
    HIGH = auto()  # Feature degradation
    CRITICAL = auto()  # Service failure


@dataclass
class ErrorContext:
    """Structured context for error debugging."""

    component: str
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    user_message: str | None = None  # Safe for UI display
    recovery_hint: str | None = None

    def to_details(self) -> JsonObject:
        """Serialise context into API-safe response details."""

        details: JsonObject = {
            "component": self.component,
            "operation": self.operation,
            "params": to_json_object(self.params),
        }
        if self.user_message:
            details["user_message"] = self.user_message
        if self.recovery_hint:
            details["recovery_hint"] = self.recovery_hint
        return details


logger = get_logger(__name__)


class ViolaError(Exception):
    """
    Base exception for all NOVVIOLA errors.

    Provides:
    - Severity classification for monitoring
    - Retryable flag for retry logic
    - Category for expected vs unexpected classification
    - Structured context for debugging
    - User-friendly messages for UI
    """

    severity: ErrorSeverity = ErrorSeverity.MEDIUM
    retryable: bool = False
    # Category is optional - if not set, categorize_exception() will use heuristics
    category: ErrorCategory | None = None

    def __init__(
        self,
        message: str,
        context: ErrorContext | None = None,
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.context = context
        self.cause = cause

        # Telemetry: record error occurrence
        try:
            from admin.instrumentation import record_error

            code = f"E_{type(self).__name__}"
            if context and context.component:
                code = f"E_{context.component}"
            record_error(code)
        except Exception:
            logger.debug("Failed to record error metric for %s", type(self).__name__, exc_info=True)

    def user_friendly_message(self) -> str:
        """Message safe to show to users."""
        if self.context and self.context.user_message:
            return self.context.user_message
        return "An unexpected error occurred. Please try again."

    def recovery_hint(self) -> str | None:
        """Get recovery hint if available."""
        if self.context:
            return self.context.recovery_hint
        return None


def merge_error_details(
    details: Mapping[str, object] | None = None,
    context: ErrorContext | None = None,
) -> JsonObject | None:
    """Merge explicit details with structured ErrorContext."""

    merged: JsonObject = {}
    if details:
        merged.update(to_json_object(details))
    if context is not None:
        merged.update(context.to_details())
    return merged or None


def error_details_for_exception(
    exc: Exception,
    *,
    component: str | None = None,
    operation: str | None = None,
    params: Mapping[str, object] | None = None,
    details: Mapping[str, object] | None = None,
) -> JsonObject | None:
    """Build response details for an exception, preferring ViolaError context."""

    context = exc.context if isinstance(exc, ViolaError) else None
    if context is None and component and operation:
        context = ErrorContext(
            component=component,
            operation=operation,
            params=dict(params or {}),
        )
    return merge_error_details(details=details, context=context)


# --- Configuration Errors ---


class ConfigurationError(ViolaError):
    """Configuration-related errors."""

    severity = ErrorSeverity.HIGH


class MissingKeyError(ConfigurationError):
    """Required configuration key is missing."""

    def __init__(self, key: str, source: str = "config"):
        super().__init__(
            f"Missing required key '{key}' in {source}",
            ErrorContext(
                component="config",
                operation="load",
                params={"key": key, "source": source},
                user_message="A setting is missing. Say connect to finish setup.",
                recovery_hint="Say connect to finish setup.",
            ),
        )
        self.key = key
        self.source = source


class InvalidSettingError(ConfigurationError):
    """Configuration value is invalid."""

    def __init__(self, key: str, value: Any, reason: str = ""):
        msg = f"Invalid value for '{key}': {value}"
        if reason:
            msg += f" - {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="config",
                operation="validate",
                params={"key": key, "value": value},
                user_message="A setting needs attention. Say connect to update it.",
                recovery_hint="Say connect to update it.",
            ),
        )
        self.key = key
        self.value = value


# --- Service Errors ---


class ServiceError(ViolaError):
    """Service-level errors."""

    pass


class ServiceUnavailableError(ServiceError):
    """Service is not available."""

    severity = ErrorSeverity.HIGH
    retryable = True

    def __init__(self, service: str, reason: str = ""):
        msg = f"Service '{service}' unavailable"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=service,
                operation="connect",
                params={"service": service},
                user_message=f"{service} is not available. Check your connection.",
                recovery_hint="Check service status and network connectivity.",
            ),
        )
        self.service = service


class ServiceTimeoutError(ServiceError):
    """Service operation timed out."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, service: str, timeout: float, operation: str = "request"):
        super().__init__(
            f"{service} {operation} timed out after {timeout}s",
            ErrorContext(
                component=service,
                operation=operation,
                params={"timeout": timeout},
                user_message=f"{service} is taking too long. Please try again.",
                recovery_hint="Check network connectivity or service status.",
            ),
        )
        self.service = service
        self.timeout = timeout
        self.operation = operation


class CircuitOpenError(ServiceError):
    """Circuit breaker is open - service in failure state."""

    severity = ErrorSeverity.HIGH
    retryable = False  # Not immediately retryable

    def __init__(self, service: str, recovery_seconds: float):
        super().__init__(
            f"Circuit breaker open for {service}. Recovery in {recovery_seconds:.0f}s",
            ErrorContext(
                component=service,
                operation="circuit_check",
                params={"recovery_seconds": recovery_seconds},
                user_message=f"{service} is temporarily unavailable. It will recover automatically.",
                recovery_hint=f"Wait {recovery_seconds:.0f} seconds or check service health.",
            ),
        )
        self.service = service
        self.recovery_seconds = recovery_seconds


class RetryExhaustedError(ServiceError):
    """All retry attempts exhausted."""

    severity = ErrorSeverity.HIGH
    retryable = False

    def __init__(self, service: str, attempts: int, last_error: Exception | None = None):
        super().__init__(
            f"{service} failed after {attempts} attempts",
            ErrorContext(
                component=service,
                operation="retry",
                params={"attempts": attempts},
                user_message=f"{service} is not responding. Try again in a moment.",
                recovery_hint="Check service status and try again.",
            ),
            cause=last_error,
        )
        self.service = service
        self.attempts = attempts


# --- Audio Service Errors ---


class AudioServiceError(ServiceError):
    """Base error for audio service operations."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "audio_operation",
        context: ErrorContext | None = None,
    ):
        if context is None:
            context = ErrorContext(
                component="audio_core",
                operation=operation,
                user_message="An audio error occurred.",
                recovery_hint="Check audio device settings.",
            )
        super().__init__(message, context)


class AudioQueueError(AudioServiceError):
    """Error in audio queue operations."""

    def __init__(self, message: str, *, operation: str = "queue_operation"):
        super().__init__(
            message,
            operation=operation,
            context=ErrorContext(
                component="audio_core.queue",
                operation=operation,
                user_message="Could not modify the playback queue.",
                recovery_hint="Try the operation again.",
            ),
        )


class AudioStateError(AudioServiceError):
    """Error in audio state operations."""

    def __init__(self, message: str, *, operation: str = "state_operation"):
        super().__init__(
            message,
            operation=operation,
            context=ErrorContext(
                component="audio_core.state",
                operation=operation,
                user_message="Invalid playback state.",
                recovery_hint="Try stopping and starting playback.",
            ),
        )


class AudioTimeoutError(AudioServiceError, ServiceTimeoutError):
    """Timeout in audio operations."""

    def __init__(self, message: str, *, timeout: float = 0.0):
        AudioServiceError.__init__(
            self,
            message,
            operation="timeout",
            context=ErrorContext(
                component="audio_core",
                operation="timeout",
                params={"timeout": timeout},
                user_message="Audio operation timed out.",
                recovery_hint="Check audio device responsiveness.",
            ),
        )


# --- Playback Errors ---


class PlaybackError(ViolaError):
    """Media playback errors."""

    pass


class BackendUnavailableError(PlaybackError):
    """Playback backend (VLC, etc.) is unavailable."""

    severity = ErrorSeverity.HIGH

    def __init__(self, backend: str, reason: str = ""):
        msg = f"Playback backend '{backend}' unavailable"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"playback.{backend}",
                operation="initialize",
                params={"backend": backend},
                user_message="Audio playback is unavailable. Check audio settings.",
                recovery_hint=f"Install or restart {backend}.",
            ),
        )
        self.backend = backend


class StreamResolutionError(PlaybackError):
    """Failed to resolve stream URL."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, query: str, reason: str = ""):
        msg = f"Failed to resolve stream for '{query}'"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="playback.resolver",
                operation="resolve",
                params={"query": query},
                user_message="Couldn't find that track. Try a different search.",
                recovery_hint="Check your search query or try a different source.",
            ),
        )
        self.query = query


class PlaybackOperationError(PlaybackError):
    """Playback operation (play/pause/stop/seek) failed."""

    severity = ErrorSeverity.LOW
    retryable = True

    def __init__(self, operation: str, backend: str, reason: str = ""):
        msg = f"Playback {operation} failed on {backend}"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"playback.{backend}",
                operation=operation,
                params={"backend": backend},
                user_message=f"Couldn't {operation} playback. Try again.",
                recovery_hint="Restart playback if the issue persists.",
            ),
        )
        self.operation = operation
        self.backend = backend


class PlaybackTimeoutError(PlaybackError):
    """Playback operation timed out."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, operation: str, timeout: float, backend: str = "vlc"):
        super().__init__(
            f"Playback {operation} timed out after {timeout}s",
            ErrorContext(
                component=f"playback.{backend}",
                operation=operation,
                params={"timeout": timeout, "backend": backend},
                user_message="Playback is taking too long. Please try again.",
                recovery_hint="Check audio device and restart playback.",
            ),
        )
        self.operation = operation
        self.timeout = timeout
        self.backend = backend


# --- Voice Errors ---


class VoiceError(ViolaError):
    """Voice pipeline errors."""

    pass


class WakeDetectionError(VoiceError):
    """Wake word detection failed."""

    severity = ErrorSeverity.MEDIUM

    def __init__(self, reason: str = ""):
        msg = "Wake word detection failed"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="voice.wake",
                operation="detect",
                user_message="Wake word detection failed. Try saying the wake word again.",
                recovery_hint="Check microphone settings and ambient noise levels.",
            ),
        )


class WakeDetectorUnavailableError(VoiceError):
    """Wake detector is not available."""

    severity = ErrorSeverity.HIGH

    def __init__(self, reason: str = ""):
        msg = "Wake word detector unavailable"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="voice.wake",
                operation="initialize",
                user_message="Voice activation is not available. Check microphone permissions.",
                recovery_hint="Enable microphone access and restart the application.",
            ),
        )


class TranscriptionError(VoiceError):
    """Speech-to-text failed."""

    retryable = True

    def __init__(self, reason: str = "", audio_path: str | None = None):
        msg = "Speech transcription failed"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="voice.stt",
                operation="transcribe",
                params={"audio_path": audio_path} if audio_path else {},
                user_message="Couldn't understand that. Please try again.",
                recovery_hint="Speak clearly and reduce background noise.",
            ),
        )
        self.audio_path = audio_path


class SynthesisError(VoiceError):
    """Text-to-speech failed."""

    retryable = True

    def __init__(self, reason: str = "", text: str | None = None):
        msg = "Speech synthesis failed"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="voice.tts",
                operation="synthesize",
                params={"text_length": len(text) if text else 0},
                user_message="Couldn't speak the response. Check audio settings.",
                recovery_hint="Check audio output device and volume settings.",
            ),
        )


class AudioDeviceError(VoiceError):
    """Audio device error."""

    severity = ErrorSeverity.HIGH

    def __init__(self, device_type: str, reason: str = ""):
        msg = f"Audio {device_type} error"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"voice.audio.{device_type}",
                operation="access",
                params={"device_type": device_type},
                user_message=f"Audio {device_type} is not working. Check your audio settings.",
                recovery_hint=f"Check {device_type} device connection and permissions.",
            ),
        )
        self.device_type = device_type


class WakeCallbackError(VoiceError):
    """Wake word callback execution failed."""

    severity = ErrorSeverity.MEDIUM

    def __init__(self, callback_type: str, reason: str = ""):
        msg = f"Wake callback error ({callback_type})"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="voice.wake.callback",
                operation="execute",
                params={"callback_type": callback_type},
                user_message="Voice command processing had an issue. Please try again.",
                recovery_hint="If persistent, restart the application.",
            ),
        )
        self.callback_type = callback_type


# --- LLM Errors ---


class LLMError(ViolaError):
    """LLM/GPT related errors."""

    pass


class ProviderUnavailableError(LLMError):
    """LLM provider is unavailable."""

    severity = ErrorSeverity.HIGH

    def __init__(self, provider: str, reason: str = ""):
        msg = f"LLM provider '{provider}' unavailable"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"llm.{provider}",
                operation="connect",
                params={"provider": provider},
                user_message="AI features are temporarily unavailable.",
                recovery_hint="Check API key and network connection.",
            ),
        )
        self.provider = provider


class RateLimitError(LLMError):
    """LLM rate limit exceeded."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, provider: str, retry_after: float | None = None):
        msg = f"{provider} rate limit exceeded"
        if retry_after:
            msg += f". Retry after {retry_after}s"
        super().__init__(
            msg,
            ErrorContext(
                component=f"llm.{provider}",
                operation="api_call",
                params={"retry_after": retry_after},
                user_message="I'm getting rate-limited — give it a moment.",
                recovery_hint=f"Wait {retry_after or 60}s before retrying.",
            ),
        )
        self.provider = provider
        self.retry_after = retry_after


class CostCircuitBreakerError(LLMError):
    """Hard cost circuit breaker tripped -- NOT bypassable by dev_mode."""

    severity = ErrorSeverity.CRITICAL
    retryable = False

    def __init__(self, reason: str, limit_type: str = "circuit_breaker"):
        super().__init__(
            reason,
            ErrorContext(
                component="llm.cost_circuit_breaker",
                operation="check",
                params={"limit_type": limit_type},
                user_message="Safety limit reached. Please wait before trying again.",
                recovery_hint="Wait a minute or check your monthly cost settings.",
            ),
        )
        self.limit_type = limit_type


class LLMQuotaExceededError(LLMError):
    """Legacy LLM quota/account gate exceeded."""

    severity = ErrorSeverity.MEDIUM
    retryable = False

    def __init__(
        self,
        user_id: str,
        limit_type: str,
        current: int,
        limit: int,
        reset_at: str = "",
    ):
        msg = f"User {user_id} exceeded {limit_type}: {current}/{limit}"
        if reset_at:
            msg += f" (resets at {reset_at})"
        super().__init__(
            msg,
            ErrorContext(
                component="llm.rate_limiter",
                operation="check_quota",
                params={
                    "user_id": user_id,
                    "limit_type": limit_type,
                    "current": current,
                    "limit": limit,
                },
                user_message="You've reached your usage limit for now.",
                recovery_hint=(f"Quota resets at {reset_at}." if reset_at else "Quota resets periodically."),
            ),
        )
        self.user_id = user_id
        self.limit_type = limit_type
        self.current = current
        self.limit = limit
        self.reset_at = reset_at


class InvalidResponseError(LLMError):
    """LLM returned an invalid response."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, provider: str, reason: str = ""):
        msg = f"{provider} returned invalid response"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"llm.{provider}",
                operation="parse_response",
                params={"provider": provider},
                user_message="AI response was unclear. Please try again.",
                recovery_hint="Rephrase your request and try again.",
            ),
        )
        self.provider = provider


class ContextLengthError(LLMError):
    """Request exceeds LLM context length."""

    severity = ErrorSeverity.MEDIUM

    def __init__(self, provider: str, token_count: int, max_tokens: int):
        super().__init__(
            f"{provider} context length exceeded: {token_count} > {max_tokens}",
            ErrorContext(
                component=f"llm.{provider}",
                operation="validate_context",
                params={"token_count": token_count, "max_tokens": max_tokens},
                user_message="Request is too long. Try a shorter message.",
                recovery_hint="Reduce message length or clear conversation history.",
            ),
        )
        self.provider = provider
        self.token_count = token_count
        self.max_tokens = max_tokens


class LLMTimeoutError(LLMError):
    """LLM request timed out."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, provider: str, timeout: float, operation: str = "request"):
        super().__init__(
            f"{provider} {operation} timed out after {timeout}s",
            ErrorContext(
                component=f"llm.{provider}",
                operation=operation,
                params={"timeout": timeout},
                user_message="AI is taking too long. Please try again.",
                recovery_hint="Try a simpler request or check your connection.",
            ),
        )
        self.provider = provider
        self.timeout = timeout


# --- Plugin Errors ---


class PluginError(ViolaError):
    """Base error for plugin system operations."""

    pass


class PluginLoadError(PluginError):
    """Plugin failed to load."""

    severity = ErrorSeverity.HIGH

    def __init__(self, plugin_name: str, reason: str = ""):
        msg = f"Failed to load plugin '{plugin_name}'"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"plugin.{plugin_name}",
                operation="load",
                params={"plugin_name": plugin_name},
                user_message=f"Plugin '{plugin_name}' could not be loaded.",
                recovery_hint="Check the plugin is installed correctly and try reloading.",
            ),
        )
        self.plugin_name = plugin_name


class PluginExecutionError(PluginError):
    """Plugin handler crashed during execution."""

    severity = ErrorSeverity.MEDIUM
    retryable = True

    def __init__(self, plugin_name: str, operation: str = "handle", reason: str = ""):
        msg = f"Plugin '{plugin_name}' failed during {operation}"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"plugin.{plugin_name}",
                operation=operation,
                params={"plugin_name": plugin_name},
                user_message=f"The {plugin_name} plugin ran into an error. Try again.",
                recovery_hint="If the problem persists, try reloading the plugin.",
            ),
        )
        self.plugin_name = plugin_name
        self.operation = operation


class PluginNotFoundError(PluginError):
    """Plugin not found in registry or filesystem."""

    severity = ErrorSeverity.MEDIUM

    def __init__(self, plugin_name: str):
        super().__init__(
            f"Plugin '{plugin_name}' not found",
            ErrorContext(
                component="plugin.registry",
                operation="lookup",
                params={"plugin_name": plugin_name},
                user_message=f"I couldn't find a plugin called '{plugin_name}'.",
                recovery_hint="Check the plugin name or search the registry.",
            ),
        )
        self.plugin_name = plugin_name


class PluginConfigError(PluginError):
    """Plugin configuration is invalid or missing."""

    severity = ErrorSeverity.MEDIUM

    def __init__(self, plugin_name: str, reason: str = ""):
        msg = f"Plugin '{plugin_name}' configuration error"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component=f"plugin.{plugin_name}",
                operation="configure",
                params={"plugin_name": plugin_name},
                user_message=f"The {plugin_name} plugin needs to be configured.",
                recovery_hint="Say connect to finish plugin setup.",
            ),
        )
        self.plugin_name = plugin_name


class PluginInstallError(PluginError):
    """Plugin installation failed."""

    severity = ErrorSeverity.HIGH

    def __init__(self, plugin_name: str, reason: str = ""):
        msg = f"Failed to install plugin '{plugin_name}'"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="plugin.installer",
                operation="install",
                params={"plugin_name": plugin_name},
                user_message=f"Couldn't install the {plugin_name} plugin.",
                recovery_hint="Check your internet connection and try again.",
            ),
        )
        self.plugin_name = plugin_name


# --- Convenience function for error enrichment ---


def enrich_exception(
    exc: Exception,
    component: str,
    operation: str,
    **extra_params: Any,
) -> ViolaError:
    """
    Wrap a generic exception in a ViolaError with context.

    Use this to convert third-party or generic exceptions to ViolaError
    while preserving the original exception as the cause.

    Args:
        exc: Original exception
        component: Component name (e.g., "playback.vlc")
        operation: Operation that failed (e.g., "play")
        **extra_params: Additional context parameters

    Returns:
        ViolaError wrapping the original exception

    Example:
        try:
            vlc.play()
        except Exception as e:
            raise enrich_exception(e, "playback.vlc", "play", url=url)
    """
    return ViolaError(
        str(exc),
        ErrorContext(
            component=component,
            operation=operation,
            params=extra_params,
        ),
        cause=exc,
    )


__all__ = [
    "AudioDeviceError",
    "BackendUnavailableError",
    "CircuitOpenError",
    # Configuration
    "ConfigurationError",
    "ContextLengthError",
    "CostCircuitBreakerError",
    "ErrorContext",
    "ErrorSeverity",
    "InvalidResponseError",
    "InvalidSettingError",
    # LLM
    "LLMError",
    "LLMTimeoutError",
    "MissingKeyError",
    # Playback
    "PlaybackError",
    "PlaybackOperationError",
    "PlaybackTimeoutError",
    # Plugin
    "PluginConfigError",
    "PluginError",
    "PluginExecutionError",
    "PluginInstallError",
    "PluginLoadError",
    "PluginNotFoundError",
    "ProviderUnavailableError",
    "RateLimitError",
    "RetryExhaustedError",
    # Service
    "ServiceError",
    "ServiceTimeoutError",
    "ServiceUnavailableError",
    "StreamResolutionError",
    "SynthesisError",
    "TranscriptionError",
    # Base
    "ViolaError",
    # Voice
    "VoiceError",
    "WakeCallbackError",
    "WakeDetectionError",
    "WakeDetectorUnavailableError",
    # Utilities
    "enrich_exception",
    "error_details_for_exception",
    "merge_error_details",
]
