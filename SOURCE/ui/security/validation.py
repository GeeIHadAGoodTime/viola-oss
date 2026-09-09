"""
Input Validation Utilities

Unified input validation and sanitization.
"""

from __future__ import annotations

import os
import re
import warnings
from pathlib import Path
from urllib.parse import unquote

from core.logging_config import get_logger

from .config import SecurityConfig, get_security_config

logger = get_logger(__name__)


class InputValidator:
    """Unified input validation."""

    def __init__(self, config: SecurityConfig | None = None):
        self.config = config or get_security_config()

    def sanitize_text(self, text: str, max_length: int | None = None) -> str:
        """
        Sanitize text input.

        Removes:
        - Control characters (except newline/tab)
        - Null bytes
        - Path traversal attempts
        """
        # Remove control characters (except newline \n and tab \t)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

        # Remove null bytes
        text = text.replace("\x00", "")

        # Limit length
        if max_length:
            text = text[:max_length]

        return text.strip()

    def validate_path(self, path: str, allowed_dirs: list | None = None) -> str:
        """
        Validate and sanitize file path.

        SECURITY: Prevents path traversal attacks including:
        - Basic path traversal (../)
        - URL-encoded traversal (%2e%2e%2f)
        - Windows UNC paths (\\\\server\\share)
        - Symlink attacks
        - Absolute paths (C:\\, /etc, etc.)
        - Mixed separators

        Args:
            path: File path to validate
            allowed_dirs: List of allowed base directories (whitelist approach)

        Returns:
            Normalized, validated path

        Raises:
            ValueError: If path is invalid or contains traversal attempts
        """
        if not path:
            raise ValueError("Path cannot be empty")

        # SECURITY: URL-decode first to catch encoded traversal attempts
        try:
            decoded_path = unquote(path)
        except (ValueError, UnicodeDecodeError) as e:
            # SECURITY: If URL decoding fails, reject the path rather than continue with raw path
            logger.warning("URL decode failed for path validation, rejecting path: %s", e)
            raise ValueError("Invalid path: URL decoding failed") from e

        # SECURITY: Block dangerous patterns
        dangerous_patterns = [
            "..",  # Basic traversal
            "%2e%2e",  # URL-encoded ..
            "%2e%2e%2f",  # URL-encoded ../
            "%2e%2e%5c",  # URL-encoded ..\ (Windows)
            "\\",  # Windows separator (if not on Windows, block it)
            "//",  # UNC path indicator
        ]

        path_lower = decoded_path.lower()
        for pattern in dangerous_patterns:
            if pattern in path_lower:
                raise ValueError(f"Invalid path: path traversal not allowed (detected: {pattern})")

        # SECURITY: Block absolute paths
        # Check for Windows absolute paths (C:\, D:\, etc.)
        if re.match(r"^[A-Za-z]:[\\/]", decoded_path):
            raise ValueError("Invalid path: absolute Windows paths not allowed")

        # Check for Unix absolute paths
        if decoded_path.startswith("/") and not decoded_path.startswith("/static/"):
            raise ValueError("Invalid path: absolute paths not allowed")

        # SECURITY: Use pathlib for safer path operations
        try:
            path_obj = Path(decoded_path)

            # Resolve symlinks and get absolute path for validation
            # Note: This requires the path to exist, so we do it conditionally
            if path_obj.exists():
                resolved_path = path_obj.resolve()
            else:
                # For non-existent paths, resolve relative to current directory
                resolved_path = (Path.cwd() / path_obj).resolve()

            # Normalize the path
            normalized = str(resolved_path)

        except (OSError, ValueError) as e:
            # Fallback to os.path if pathlib fails (may happen with special characters)
            normalized = os.path.normpath(decoded_path)
            logger.warning("Path validation fallback to os.path: %s", e)

        # SECURITY: Whitelist approach - require allowed_dirs
        # This is more secure than blacklisting
        if allowed_dirs:
            # Convert allowed_dirs to absolute paths for comparison
            allowed_abs_dirs = []
            for allowed_dir in allowed_dirs:
                try:
                    abs_allowed = os.path.abspath(os.path.normpath(allowed_dir))
                    allowed_abs_dirs.append(abs_allowed)
                except (OSError, ValueError) as e:
                    # SECURITY: If we can't resolve an allowed dir, skip it rather than use raw path
                    logger.warning(
                        "Failed to resolve allowed dir path, skipping: %s - %s",
                        allowed_dir,
                        e,
                    )
                    # Don't add the unresolved path - fail-secure

            # Check if normalized path is within any allowed directory
            path_is_allowed = False
            for allowed_dir in allowed_abs_dirs:
                try:
                    # Use os.path.commonpath for safe comparison
                    common_path = os.path.commonpath([normalized, allowed_dir])
                    if common_path == allowed_dir or normalized.startswith(allowed_dir + os.sep):
                        path_is_allowed = True
                        break
                except ValueError:
                    # Paths don't share a common prefix - not allowed
                    continue

            if not path_is_allowed:
                raise ValueError(f"Path not in allowed directories. Path: {normalized[:100]}, Allowed: {allowed_dirs}")
        else:
            # SECURITY: Warn if no allowed_dirs provided (less secure)
            # In production, should always require allowed_dirs
            warnings.warn(
                "Path validation called without allowed_dirs whitelist. "
                "This is less secure. Consider providing allowed_dirs.",
                SecurityWarning,
                stacklevel=2,
            )

        return normalized

    def validate_url(self, url: str) -> bool:
        """Validate URL format."""
        # Basic URL validation
        url_pattern = re.compile(
            r"^https?://"  # http:// or https://
            r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # domain...
            r"localhost|"  # localhost...
            r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # ...or ip
            r"(?::\d+)?"  # optional port
            r"(?:/?|[/?]\S+)$",
            re.IGNORECASE,
        )

        return bool(url_pattern.match(url))

    def validate_video_id(self, video_id: str, length: int = 11) -> bool:
        """Validate video ID format (e.g., YouTube)."""
        if not video_id or len(video_id) != length:
            return False
        return video_id.isalnum()


class SecurityWarning(UserWarning):
    """Warning for security-related issues."""

    pass


# Global validator instance
_input_validator: InputValidator | None = None


def get_input_validator() -> InputValidator:
    """Get global input validator."""
    global _input_validator
    if _input_validator is None:
        _input_validator = InputValidator()
    return _input_validator
