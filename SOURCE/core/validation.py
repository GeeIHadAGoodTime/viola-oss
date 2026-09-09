"""
Input validation utilities.

Application configuration validation.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

from core.constants import LOCALHOST, MAX_COMMAND_TEXT_LENGTH, MAX_QUERY_TEXT_LENGTH
from core.logging_config import get_logger

logger = get_logger(__name__)


def validate_command_text(text: str) -> tuple[bool, str]:
    """
    Validate user command text for security (legacy function, kept for compatibility).

    For new code, use: from utils.failfast import validate
    validate(text, str, non_empty=True, name="command_text")
    """
    try:
        from utils.failfast import validate as v

        v(text, str, non_empty=True, name="command_text")

        # Length validation
        if len(text) > MAX_COMMAND_TEXT_LENGTH:
            return (
                False,
                f"Command too long (max {MAX_COMMAND_TEXT_LENGTH} characters)",
            )

        # Control character validation
        if re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", text):
            return False, "Command contains invalid control characters"

        return True, ""
    except Exception as e:
        logger.debug("Command text validation failed: %s", e)
        return False, str(e)


def validate_query(query: str) -> tuple[bool, str]:
    """
    Validate search query parameter (legacy function, kept for compatibility).

    For new code, use: from utils.failfast import validate
    validate(query, str, non_empty=True, name="query")
    """
    try:
        from utils.failfast import validate as v

        v(query, str, non_empty=True, name="query")

        if len(query) > MAX_QUERY_TEXT_LENGTH:
            return False, f"Query too long (max {MAX_QUERY_TEXT_LENGTH} characters)"

        return True, ""
    except Exception as e:
        logger.debug("Query validation failed: %s", e)
        return False, str(e)


def validate_volume(value: Any) -> tuple[bool, str]:
    """
    Validate volume value (legacy function, kept for compatibility).

    For new code, use: from utils.failfast import validate
    validate(volume, int, min=0, max=100, name="volume")
    """
    try:
        from utils.failfast import validate as v

        v(value, int, min=0, max=100, name="volume")
        return True, ""
    except Exception as e:
        logger.debug("Volume validation failed: %s", e)
        return False, str(e)


def validate_seek_seconds(seconds: Any) -> tuple[bool, str]:
    """
    Validate seek seconds parameter (legacy function, kept for compatibility).

    For new code, use: from utils.failfast import validate
    validate(seconds, int, min=-3600, max=86400, name="seek_seconds")
    """
    try:
        from utils.failfast import validate as v

        MAX_SEEK_SECONDS = 86400  # 24 hours
        MIN_SEEK_SECONDS = -3600  # Allow -1 hour for relative seeks
        v(seconds, int, min=MIN_SEEK_SECONDS, max=MAX_SEEK_SECONDS, name="seek_seconds")
        return True, ""
    except Exception as e:
        logger.debug("Seek seconds validation failed: %s", e)
        return False, str(e)


def validate_host(host: str) -> tuple[bool, str]:
    """Validate host address."""
    if not host or not isinstance(host, str):
        return False, "Host must be a non-empty string"

    # Whitelist approach: Only allow localhost and 127.0.0.1 by default
    # For production, add configurable whitelist
    ALLOWED_HOSTS = [LOCALHOST, "localhost", "0.0.0.0"]  # nosec B104

    # Normalize hostname
    host_lower = host.lower().strip()

    # Allow IP address validation
    if host_lower in ALLOWED_HOSTS:
        return True, ""

    # Validate IP address format
    try:
        socket.inet_aton(host)
        return True, ""
    except OSError:
        # Not a valid IP, check if it's a valid hostname
        if re.match(
            r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$",
            host,
        ):
            logger.warning(
                "Non-localhost host specified: %s. Consider security implications.",
                host,
            )
            return True, ""
        return False, f"Invalid host format: {host}"


def validate_port(port: Any) -> tuple[bool, str]:
    """Validate port number."""
    try:
        p = int(port)
        if p < 1 or p > 65535:
            return False, "Port must be between 1 and 65535"
        return True, ""
    except (ValueError, TypeError):
        return False, "Port must be a valid integer"


def sanitize_error_message(error: Exception) -> str:
    """
    Sanitize error messages before returning to clients.
    Removes sensitive information like file paths, internal methods, etc.
    """
    error_str = str(error)

    # Remove file paths (common patterns)
    error_str = re.sub(r"[A-Za-z]:[\\/][^\s]+", "[path redacted]", error_str)
    error_str = re.sub(r"/[\w/]+\.py", "[file redacted]", error_str)

    # Remove sensitive patterns
    sensitive_patterns = [
        r"password[=:]\s*\S+",
        r"api[_-]?key[=:]\s*\S+",
        r"token[=:]\s*\S+",
        r"secret[=:]\s*\S+",
    ]
    for pattern in sensitive_patterns:
        error_str = re.sub(pattern, "[sensitive data redacted]", error_str, flags=re.IGNORECASE)

    # Truncate long error messages
    MAX_ERROR_LENGTH = 200
    if len(error_str) > MAX_ERROR_LENGTH:
        error_str = error_str[:MAX_ERROR_LENGTH] + "..."

    return error_str


def validate_file_path(file_path: str, expected_base: str | None = None) -> tuple[bool, str]:
    """
    Validate file path is safe and within expected directory.

    Args:
        file_path: Path to validate
        expected_base: Expected base directory (optional)

    Returns:
        (is_valid, error_message)
    """
    if not file_path:
        return False, "File path cannot be empty"

    try:
        path = Path(file_path).resolve()

        # Check if path exists (for deletion operations)
        if not path.exists():
            return False, "File path does not exist"

        # If expected_base is provided, ensure path is within it
        if expected_base:
            base_path = Path(expected_base).resolve()
            try:
                path.relative_to(base_path)
            except ValueError:
                return False, "File path is outside expected directory"

        return True, ""
    except Exception as e:
        logger.debug("File path validation failed: %s", e)
        return False, f"Invalid file path: {sanitize_error_message(e)}"
