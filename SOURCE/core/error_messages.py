"""
User-friendly error message mappings.

This module provides safe, user-friendly messages for each error code.
These messages are designed to be helpful without leaking internal details.

Usage:
    from core.error_messages import get_user_message
    from core.constants import ErrorCodes

    message = get_user_message(ErrorCodes.PLAY_FAILED)
"""

from __future__ import annotations

from typing import Any

from core.constants import ErrorCodes

# Mapping of error codes to user-friendly messages.
# These should sound like a helpful person — warm, brief, and action-oriented.
ERROR_MESSAGES: dict[str, str] = {
    # General errors
    ErrorCodes.INTERNAL_ERROR: "Something went sideways — try that again?",
    ErrorCodes.OPERATION_FAILED: "That didn't work. Try again?",
    ErrorCodes.SERVICE_UNAVAILABLE: "I'm having trouble reaching that service. Give me a sec and try again.",
    ErrorCodes.TIMEOUT: "That took too long — want me to try again?",
    # Playback errors
    ErrorCodes.PLAY_FAILED: "Couldn't play that. Try again, or tell me what you want to hear.",
    ErrorCodes.PAUSE_FAILED: "Couldn't pause — try again?",
    ErrorCodes.RESUME_FAILED: "Couldn't resume — try again?",
    ErrorCodes.STOP_FAILED: "Couldn't stop playback — try again?",
    ErrorCodes.SEEK_FAILED: "Couldn't skip to that spot. Try again?",
    ErrorCodes.VOLUME_FAILED: "Couldn't change the volume. Try again?",
    ErrorCodes.NEXT_FAILED: "Couldn't skip to the next track. Try again?",
    ErrorCodes.PREVIOUS_FAILED: "Couldn't go back a track. Try again?",
    ErrorCodes.QUEUE_FAILED: "Couldn't add that to the queue. Try again?",
    # Calendar errors
    ErrorCodes.CALENDAR_ADD_FAILED: "Couldn't add that event to your local calendar. Try again?",
    ErrorCodes.CALENDAR_LIST_FAILED: "Couldn't fetch your local calendar. Try again?",
    ErrorCodes.CALENDAR_DELETE_FAILED: "Couldn't remove that event. Try again?",
    ErrorCodes.CALENDAR_NOT_CONFIGURED: "Couldn't reach your local calendar. Try again?",
    # Weather errors
    ErrorCodes.WEATHER_FETCH_FAILED: "Couldn't get the weather. Try with a city name, like 'weather in Chicago'.",
    ErrorCodes.WEATHER_LOCATION_INVALID: "I'm not sure where that is. Try 'weather in Chicago' or another city name.",
    # Transcription errors
    ErrorCodes.TRANSCRIBER_INIT_FAILED: "Voice input is warming up — give me a moment and try again.",
    # Diagnostics errors
    ErrorCodes.DIAGNOSTICS_FAILED: "Couldn't run diagnostics right now. Try again in a sec.",
    ErrorCodes.STATE_FETCH_FAILED: "Couldn't check the system state. Try again?",
    # Auth errors
    ErrorCodes.AUTH_FAILED: "I couldn't verify your identity. Try signing in again?",
    ErrorCodes.OAUTH_FAILED: "Sign-in didn't go through. Want to try again?",
    ErrorCodes.SESSION_INVALID: "Your session expired — just sign in again and you're good.",
    # Validation errors
    ErrorCodes.VALIDATION_ERROR: "Something about that didn't look right. Could you rephrase?",
    ErrorCodes.INVALID_INPUT: "That input didn't match a supported request. Could you rephrase?",
    # Context errors
    ErrorCodes.CONTEXT_SYNC_FAILED: "Lost track of the context — try that again?",
    # Audio-device failures surfaced through core.user_notice.
    #
    # Keyed by the lowercase wire code rather than an ErrorCodes member because
    # these strings travel to the browser and are matched verbatim by
    # ui/react-app/src/utils/describeError.js — the same lowercase convention
    # the failure_response codes already use (no_speech_detected,
    # ducking_unavailable). Keep the two sides identical.
    "local_playback_failed": ("I answered, but I couldn't play it out loud. Check your speaker or output device."),
    "audio_output_unavailable": ("I can't reach an audio output device, so you'll see my replies but not hear them."),
    "audio_input_unavailable": ("I can't reach a microphone, so voice input won't work until one is connected."),
}

# Default message for unknown error codes
DEFAULT_ERROR_MESSAGE = "Something went wrong — try that again?"


ERROR_PAYLOADS: dict[str, dict[str, Any]] = {
    ErrorCodes.TRANSCRIPTION_FAILED: {
        "type": "transcription_failed",
        "reason": "audio_not_understood",
        "retryable": True,
        "error_state": {
            "type": "transcription_failed",
            "reason": "audio_not_understood",
            "retryable": True,
        },
    },
}


def get_user_message(error_code: str | ErrorCodes) -> str:
    """
    Get a user-friendly message for an error code.

    Args:
        error_code: The error code (string or ErrorCodes enum)

    Returns:
        User-friendly error message
    """
    code = str(error_code)
    return ERROR_MESSAGES.get(code, DEFAULT_ERROR_MESSAGE)


def get_error_payload(error_code: str | ErrorCodes) -> dict[str, Any]:
    """
    Get structured error metadata for an error code.

    Args:
        error_code: The error code (string or ErrorCodes enum)

    Returns:
        Structured error payload. LLM-aware response builders should phrase
        user-facing language from this metadata instead of relying on a fixed line.
    """
    code = str(error_code)
    if code in ERROR_PAYLOADS:
        return dict(ERROR_PAYLOADS[code])
    return {
        "type": "error",
        "code": code,
        "message": get_user_message(code),
        "retryable": False,
    }


# Exception type to error code mapping for error_handler.py
EXCEPTION_ERROR_CODES: dict[type, tuple[str, str]] = {
    ValueError: (ErrorCodes.VALIDATION_ERROR, "Invalid input provided."),
    KeyError: (ErrorCodes.VALIDATION_ERROR, "Required field missing."),
    TypeError: (ErrorCodes.VALIDATION_ERROR, "Invalid data type provided."),
    TimeoutError: (ErrorCodes.TIMEOUT, "Try again in a moment"),
    ConnectionError: (
        ErrorCodes.SERVICE_UNAVAILABLE,
        "Try again in a moment",
    ),
}


def get_error_for_exception(exc: Exception) -> tuple[str, str]:
    """
    Get error code and message for an exception type.

    Args:
        exc: The exception instance

    Returns:
        Tuple of (error_code, user_message)
    """
    for exc_type, (code, message) in EXCEPTION_ERROR_CODES.items():
        if isinstance(exc, exc_type):
            return code, message
    return ErrorCodes.INTERNAL_ERROR, DEFAULT_ERROR_MESSAGE


__all__ = [
    "DEFAULT_ERROR_MESSAGE",
    "ERROR_MESSAGES",
    "ERROR_PAYLOADS",
    "EXCEPTION_ERROR_CODES",
    "get_error_for_exception",
    "get_error_payload",
    "get_user_message",
]
