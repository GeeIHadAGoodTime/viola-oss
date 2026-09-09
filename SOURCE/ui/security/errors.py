"""
Error Sanitization

Prevents information disclosure via error messages.
"""

from __future__ import annotations

import re
from typing import Any

from core.logging_config import get_logger

from .config import SecurityConfig, get_security_config

log = get_logger(__name__)


class ErrorSanitizer:
    """
    Sanitizes error messages to prevent information disclosure.

    Removes:
    - File paths
    - Stack traces (unless debug mode)
    - Internal exception details
    - Sensitive data
    """

    def __init__(self, config: SecurityConfig | None = None):
        self.config = config or get_security_config()
        self.enabled = self.config.error_sanitization_enabled

        # Patterns to remove from error messages
        self.path_pattern = re.compile(r'[/\\][^\s"\'<>]+[/\\][^\s"\'<>]+')
        self.trace_pattern = re.compile(r"Traceback \(most recent call last\):.*?(?=\n\w|\Z)", re.DOTALL)

    def sanitize(self, error: Exception, context: dict[str, Any] | None = None) -> str:
        """
        Sanitize error message.

        Args:
            error: Exception to sanitize
            context: Optional context (endpoint, user, etc.)

        Returns:
            Sanitized error message
        """
        if not self.enabled:
            if self.config.show_detailed_errors:
                # Debug mode: show details
                return str(error)
            # Otherwise sanitize

        error_msg = str(error)
        error_type = type(error).__name__

        # Generic error messages by type
        generic_messages = {
            "ValueError": "Invalid input provided",
            "KeyError": "Required field missing",
            "TypeError": "Invalid data type",
            "FileNotFoundError": "File not found",
            "PermissionError": "Permission denied",
            "ConnectionError": "Connection failed",
            "TimeoutError": "Request timed out",
            "ImportError": "Service unavailable",
        }

        # Check for generic message
        if error_type in generic_messages:
            return generic_messages[error_type]

        # Remove file paths
        error_msg = self.path_pattern.sub("[path]", error_msg)

        # Remove stack traces
        error_msg = self.trace_pattern.sub("[traceback]", error_msg)

        # Remove common internal details
        error_msg = re.sub(r"line \d+", "[line]", error_msg)
        error_msg = re.sub(r'file "[^"]+"', "[file]", error_msg)

        # Limit length
        if len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."

        # Generic fallback
        if not error_msg or error_msg.strip() == "":
            return "An error occurred"

        return error_msg

    def sanitize_dict(self, error_dict: dict[str, Any]) -> dict[str, Any]:
        """Sanitize error dictionary."""
        sanitized = {}

        # Always include 'ok' and 'error' fields
        sanitized["ok"] = error_dict.get("ok", False)

        # Sanitize error field
        error_value = error_dict.get("error")
        if error_value:
            if isinstance(error_value, Exception):
                sanitized["error"] = self.sanitize(error_value)
            elif isinstance(error_value, str):
                sanitized["error"] = self.sanitize_string(error_value)
            else:
                sanitized["error"] = "An error occurred"
        else:
            sanitized["error"] = error_dict.get("error", "unknown_error")

        # Copy other safe fields
        for key in ["message", "details"]:
            if key in error_dict:
                value = error_dict[key]
                if isinstance(value, str):
                    sanitized[key] = self.sanitize_string(value)
                else:
                    sanitized[key] = value

        return sanitized

    def sanitize_string(self, text: str) -> str:
        """Sanitize error string."""
        if not self.enabled or self.config.show_detailed_errors:
            return text

        # Remove file paths
        text = self.path_pattern.sub("[path]", text)

        # Remove tracebacks
        text = self.trace_pattern.sub("[traceback]", text)

        # Limit length
        if len(text) > 200:
            text = text[:200] + "..."

        return text

    def log_error(self, error: Exception, context: dict[str, Any] | None = None) -> None:
        """Log error with full details (server-side only)."""
        if context:
            log.exception("Error in %s: %s", context.get("endpoint", "unknown"), error)
        else:
            log.exception("Error: %s", error)


# Global sanitizer instance
_error_sanitizer: ErrorSanitizer | None = None


def get_error_sanitizer() -> ErrorSanitizer:
    """Get global error sanitizer."""
    global _error_sanitizer
    if _error_sanitizer is None:
        _error_sanitizer = ErrorSanitizer()
    return _error_sanitizer


def sanitize_error_response(error: Exception, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Helper function to sanitize error response."""
    sanitizer = get_error_sanitizer()
    sanitizer.log_error(error, context)

    return {
        "ok": False,
        "error": sanitizer.sanitize(error, context),
    }
