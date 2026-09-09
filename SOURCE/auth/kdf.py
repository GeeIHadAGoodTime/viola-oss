"""
Key Derivation Functions for OAuth Token Encryption.

This module provides versioned key derivation to support secure token encryption
with migration paths between versions.

Key Versions:
- VERSION_LEGACY (0): SHA256 hash (fast but not suitable for passwords/secrets)
- VERSION_1 (1): PBKDF2-HMAC-SHA256 with 600,000 iterations, fixed salt (OWASP 2023)
- VERSION_2 (2): PBKDF2-HMAC-SHA256 with 600,000 iterations, per-user salt

Usage:
    from auth.kdf import derive_fernet_key, KEY_VERSION_2

    # Derive per-user key for new tokens (always use latest version)
    key = derive_fernet_key(secret, KEY_VERSION_2, user_salt=user_salt)

    # Derive key for decrypting legacy tokens
    legacy_key = derive_fernet_key(secret, KEY_VERSION_LEGACY)
"""

from __future__ import annotations

import hashlib
import secrets
from base64 import urlsafe_b64encode
from typing import Final

from core.logging_config import get_logger

logger = get_logger("viola.auth.kdf")

# Key version constants
KEY_VERSION_LEGACY: Final[int] = 0  # SHA256 hash (original, weak)
KEY_VERSION_1: Final[int] = 1  # PBKDF2-HMAC-SHA256, fixed salt (OWASP 2023)
KEY_VERSION_2: Final[int] = 2  # PBKDF2-HMAC-SHA256, per-user salt

# Current version for new tokens
KEY_VERSION_CURRENT: Final[int] = KEY_VERSION_2

# PBKDF2 parameters (OWASP 2023 recommendations)
PBKDF2_ITERATIONS: Final[int] = 600_000  # OWASP 2023 minimum for SHA256
PBKDF2_SALT: Final[bytes] = b"viola-oauth-token-encryption-v1"  # Fixed salt for v1


def generate_user_salt() -> str:
    """
    Generate a cryptographically secure per-user salt.

    Returns:
        32-byte URL-safe base64 encoded salt string (43 characters)
    """
    return secrets.token_urlsafe(32)


def derive_encryption_key(
    secret: str,
    version: int = KEY_VERSION_CURRENT,
    *,
    user_salt: str | None = None,
) -> bytes:
    """
    Derive a 32-byte encryption key from a secret using the specified version.

    Args:
        secret: The secret to derive from (typically JWT_SECRET)
        version: Key derivation version
        user_salt: Per-user salt (required for KEY_VERSION_2)

    Returns:
        32-byte key suitable for Fernet encryption

    Raises:
        ValueError: If version is not recognized or user_salt missing for v2
    """
    if version == KEY_VERSION_LEGACY:
        # Legacy: Simple SHA256 hash (kept for backward compatibility)
        return hashlib.sha256(secret.encode()).digest()

    if version == KEY_VERSION_1:
        # PBKDF2-HMAC-SHA256 with 600,000 iterations, fixed salt
        return hashlib.pbkdf2_hmac(
            hash_name="sha256",
            password=secret.encode(),
            salt=PBKDF2_SALT,
            iterations=PBKDF2_ITERATIONS,
            dklen=32,  # 32 bytes for Fernet
        )

    if version == KEY_VERSION_2:
        # PBKDF2-HMAC-SHA256 with 600,000 iterations, per-user salt
        if not user_salt:
            raise ValueError("user_salt is required for KEY_VERSION_2")
        # Combine fixed domain separator with per-user salt
        combined_salt = b"viola-oauth-v2:" + user_salt.encode()
        return hashlib.pbkdf2_hmac(
            hash_name="sha256",
            password=secret.encode(),
            salt=combined_salt,
            iterations=PBKDF2_ITERATIONS,
            dklen=32,
        )

    raise ValueError("Unknown key version: %d" % version)


def derive_fernet_key(
    secret: str,
    version: int = KEY_VERSION_CURRENT,
    *,
    user_salt: str | None = None,
) -> bytes:
    """
    Derive a base64-encoded Fernet key from a secret.

    Fernet requires a 32-byte key that is URL-safe base64 encoded.
    This function handles both the key derivation and encoding.

    Args:
        secret: The secret to derive from (typically JWT_SECRET)
        version: Key derivation version
        user_salt: Per-user salt (required for KEY_VERSION_2)

    Returns:
        44-byte base64-encoded key suitable for Fernet

    Raises:
        ValueError: If version is not recognized
    """
    raw_key = derive_encryption_key(secret, version, user_salt=user_salt)
    return urlsafe_b64encode(raw_key)


def derive_fernet_keys(
    secret: str,
    previous_secret: str | None = None,
    version: int = KEY_VERSION_CURRENT,
    *,
    user_salt: str | None = None,
) -> tuple[bytes, bytes | None]:
    """
    Derive current and optional previous Fernet keys for secret rotation.

    Args:
        secret: Current secret to derive from.
        previous_secret: Previous secret accepted during the overlap window.
        version: Key derivation version.
        user_salt: Per-user salt (required for KEY_VERSION_2).

    Returns:
        Tuple of (current_key, previous_key_or_none).
    """
    current_key = derive_fernet_key(secret, version, user_salt=user_salt)
    previous_value = previous_secret.strip() if isinstance(previous_secret, str) else ""
    if not previous_value or previous_value == secret:
        return current_key, None
    previous_key = derive_fernet_key(previous_value, version, user_salt=user_salt)
    return current_key, previous_key


def get_version_name(version: int) -> str:
    """
    Get a human-readable name for a key version.

    Args:
        version: Key version number

    Returns:
        Human-readable version name
    """
    if version == KEY_VERSION_LEGACY:
        return "legacy-sha256"
    if version == KEY_VERSION_1:
        return "pbkdf2-sha256-v1"
    if version == KEY_VERSION_2:
        return "pbkdf2-sha256-v2-per-user"
    return "unknown-v%d" % version


__all__ = [
    "KEY_VERSION_1",
    "KEY_VERSION_2",
    "KEY_VERSION_CURRENT",
    "KEY_VERSION_LEGACY",
    "PBKDF2_ITERATIONS",
    "derive_encryption_key",
    "derive_fernet_key",
    "derive_fernet_keys",
    "generate_user_salt",
    "get_version_name",
]
