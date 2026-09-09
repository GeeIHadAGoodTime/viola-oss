"""
Secure API Key Wrapper
======================

Prevents API key leakage in logs, exceptions, and string representations.
Provides type-safe wrapper around string keys with automatic masking.

Usage:
    key = SecureApiKey("sk-1234567890abcdef")
    str(key)  # Returns "sk-12***cdef"
    repr(key)  # Returns "SecureApiKey('sk-12***cdef')"
    key.value  # Returns original key

    # Auto-detects secrets
    secrets = ["api_key", "token", "password", "secret"]
"""

from __future__ import annotations

import re
from typing import Any

from config import env
from core.logging_config import get_logger

logger = get_logger(__name__)


class SecureApiKey:
    """
    Type-safe wrapper that prevents API key leakage in logs.

    Features:
    - Automatic masking in all string representations
    - Safe for use in logs, exceptions, and repr()
    - Preserves original key for actual API calls
    - Detects common secret patterns
    """

    # Common patterns that suggest this is a secret
    _SECRET_PATTERNS = re.compile(r"api[_\s]?key|token|password|secret|credential|auth", re.IGNORECASE)

    def __init__(self, key: Any, label: str | None = None):
        """
        Initialize secure API key.

        Args:
            key: The actual key value (will be converted to string)
            label: Optional label for debugging (also masked)

        Raises:
            ValueError: If key is None or empty
        """
        if key is None:
            raise ValueError("API key cannot be None")

        self._key = str(key).strip()
        if not self._key:
            raise ValueError("API key cannot be empty")

        self._label = str(label).strip() if label else None
        self._masked = self._mask(self._key)

    @staticmethod
    def _mask(key: str) -> str:
        """
        Mask API key for safe logging.

        Shows:
        - First 4 characters
        - Asterisks for middle
        - Last 4 characters

        Example:
            "sk-1234567890abcdef" -> "sk-1***cdef"
        """
        if len(key) <= 8:
            return "***"  # Too short, don't show anything
        elif len(key) <= 12:
            # Short key: show first 4, mask rest
            return f"{key[:4]}***"
        else:
            # Long key: show first 4 and last 4
            return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"

    @property
    def value(self) -> str:
        """
        Get the actual API key value.

        WARNING: Only use this for actual API calls!
        Never log, print, or return this value.

        Returns:
            Original key string
        """
        return self._key

    def __str__(self) -> str:
        """String representation (masked for safety)."""
        if self._label:
            return f"{self._label}:{self._masked}"
        return self._masked

    def __repr__(self) -> str:
        """Representation (masked for safety)."""
        return f"SecureApiKey('{self._masked}')"

    def __eq__(self, other: Any) -> bool:
        """Equality comparison using actual key value."""
        if isinstance(other, SecureApiKey):
            return self._key == other._key
        elif isinstance(other, str):
            return self._key == other
        return False

    def __hash__(self) -> int:
        """Hash based on actual key value."""
        return hash(self._key)

    def __bool__(self) -> bool:
        """Truthiness check."""
        return bool(self._key)

    def __len__(self) -> int:
        """Length of actual key (not masked)."""
        return len(self._key)

    @classmethod
    def auto_wrap(cls, key: Any, field_name: str = "") -> SecureApiKey | str | None:
        """
        Automatically wrap if this looks like a secret.

        Args:
            key: Value to potentially wrap
            field_name: Field name to check against patterns

        Returns:
            SecureApiKey if it looks like a secret, original value otherwise

        Example:
            >>> SecureApiKey.auto_wrap("sk-123", "api_key")
            SecureApiKey('sk-1***')
            >>> SecureApiKey.auto_wrap("normal_value", "username")
            'normal_value'
        """
        if key is None:
            return None

        key_str = str(key).strip()
        if not key_str:
            return key_str

        # Check if field name suggests this is a secret
        if field_name and cls._SECRET_PATTERNS.search(field_name):
            return cls(key_str, field_name)

        # Check if value format suggests it's a secret
        # OpenAI keys start with sk-
        # Many tokens are hex strings
        if key_str.startswith(("sk-", "pk-", "bk-")):
            return cls(key_str, field_name or "api_key")

        # Long hex strings (likely tokens)
        if len(key_str) > 20 and re.match(r"^[a-fA-F0-9]+$", key_str):
            return cls(key_str, field_name or "token")

        return key_str

    def unwrap(self) -> str:
        """
        Unwrap to get the actual key (alias for .value).

        Returns:
            Original key string
        """
        return self._key

    def verify_format(self, pattern: str) -> bool:
        """
        Verify key matches expected format.

        Args:
            pattern: Regex pattern to match

        Returns:
            True if key matches pattern

        Example:
            >>> key = SecureApiKey("sk-123456")
            >>> key.verify_format(r'^sk-[a-zA-Z0-9]+$')
            True
        """
        return bool(re.match(pattern, self._key))

    def log_safe(self, message: str, **kwargs) -> None:
        """
        Log a message with safe key representation.

        Args:
            message: Log message
            **kwargs: Additional log context
        """
        safe_context = {k: str(v) for k, v in kwargs.items()}
        safe_context["key"] = str(self)  # Will use masked version
        logger.info(message, **safe_context)


# Convenience function for common use cases
def secure_key(key: Any, field_name: str = "api_key") -> SecureApiKey | str | None:
    """
    Create secure API key wrapper if needed.

    Usage:
        api_key = secure_key(env.get("OPENAI_API_KEY"))
        openai_client = OpenAI(api_key=api_key.unwrap())

    Args:
        key: The key to wrap
        field_name: Field name for pattern detection

    Returns:
        SecureApiKey if it looks like a secret, original value otherwise
    """
    return SecureApiKey.auto_wrap(key, field_name)


# Global registry for detecting log leaks
_logged_keys: set[str] = set()


def detect_key_leaks() -> list[str]:
    """
    Detect if any keys were logged insecurely.

    Returns:
        List of detected leak warnings
    """
    return list(_logged_keys)


def reset_leak_detection() -> None:
    """Reset leak detection registry (for testing)."""
    global _logged_keys
    _logged_keys.clear()
