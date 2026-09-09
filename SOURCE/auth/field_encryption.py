"""
Field-Level Encryption for Auth Database.

Provides column-level Fernet encryption for sensitive fields (emails, IPs, etc.)
with deterministic HMAC-based blind indexes for searchable encrypted columns.

Architecture:
    - Fernet encryption: AES-128-CBC with HMAC for authenticated encryption
    - Blind index: HMAC-SHA256 produces deterministic hashes for WHERE clauses
    - Two separate keys derived from JWT_SECRET: one for encryption, one for HMAC
    - Key derivation: PBKDF2-HMAC-SHA256 with 600k iterations

Usage:
    encryptor = FieldEncryptor.from_settings()
    encrypted = encryptor.encrypt("user@example.com")
    hmac_hash = encryptor.blind_index("user@example.com")
    plaintext = encryptor.decrypt(encrypted)
"""

from __future__ import annotations

import hashlib
import hmac
from base64 import urlsafe_b64encode
from typing import Final

from core.logging_config import get_logger

logger = get_logger("viola.auth.field_encryption")

# Domain separators for key derivation (prevents key reuse across purposes)
_ENCRYPTION_SALT: Final[bytes] = b"viola-field-encryption-v1"
_HMAC_SALT: Final[bytes] = b"viola-field-hmac-v1"

# PBKDF2 parameters (OWASP 2023)
_PBKDF2_ITERATIONS: Final[int] = 600_000


class FieldEncryptor:
    """
    Encrypts and decrypts individual database fields using Fernet.

    Provides both randomized encryption (for storage) and deterministic
    HMAC hashing (for lookups on encrypted fields).

    Thread-safe: keys are derived once and reused.
    """

    def __init__(
        self,
        encryption_key: bytes,
        hmac_key: bytes,
        previous_encryption_key: bytes | None = None,
        previous_hmac_key: bytes | None = None,
    ) -> None:
        """
        Initialize with pre-derived keys.

        Args:
            encryption_key: 32-byte raw key for Fernet encryption
            hmac_key: 32-byte raw key for blind index HMAC
            previous_encryption_key: Optional previous raw key for rotation reads
            previous_hmac_key: Optional previous raw key for blind-index lookups
        """
        self._fernet_key = urlsafe_b64encode(encryption_key)
        self._hmac_key = hmac_key
        self._previous_fernet_key = urlsafe_b64encode(previous_encryption_key) if previous_encryption_key else None
        self._previous_hmac_key = previous_hmac_key

    @classmethod
    def from_settings(cls) -> FieldEncryptor:
        """
        Create a FieldEncryptor using JWT_SECRET from settings.

        Derives two independent keys:
        - encryption key (for Fernet)
        - HMAC key (for blind indexes)

        Raises:
            ValueError: If JWT_SECRET is not configured
        """
        from config.settings import settings

        if not settings.jwt_secret:
            raise ValueError(
                "JWT_SECRET is required for field encryption. "
                "Set JWT_SECRET to a secure random string (32+ characters)."
            )

        encryption_key, hmac_key = _derive_field_keys(settings.jwt_secret)

        previous_encryption_key = None
        previous_hmac_key = None
        previous_raw = getattr(settings, "jwt_secret_previous", "")
        previous_secret = previous_raw.strip() if isinstance(previous_raw, str) else ""
        if previous_secret and previous_secret != settings.jwt_secret:
            previous_encryption_key, previous_hmac_key = _derive_field_keys(previous_secret)

        return cls(encryption_key, hmac_key, previous_encryption_key, previous_hmac_key)

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt a field value using Fernet (randomized, authenticated).

        Args:
            plaintext: Value to encrypt

        Returns:
            Base64-encoded Fernet ciphertext
        """
        from cryptography.fernet import Fernet

        f = Fernet(self._fernet_key)
        return f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt a Fernet-encrypted field value.

        Args:
            ciphertext: Fernet ciphertext to decrypt

        Returns:
            Decrypted plaintext

        Raises:
            cryptography.fernet.InvalidToken: If ciphertext is invalid or key wrong
        """
        plaintext, _used_previous = self.decrypt_with_rotation(ciphertext)
        return plaintext

    def decrypt_with_rotation(self, ciphertext: str) -> tuple[str, bool]:
        """
        Decrypt a field value and report whether the previous key was used.

        Raises:
            cryptography.fernet.InvalidToken: If ciphertext is invalid or both
                keys are wrong
        """
        from auth.secret_rotation import decrypt_with_fallback

        plaintext, used_previous = decrypt_with_fallback(
            ciphertext,
            self._fernet_key,
            self._previous_fernet_key,
        )
        return plaintext.decode(), used_previous

    def blind_index(self, value: str) -> str:
        """
        Create a deterministic blind index for encrypted field lookups.

        Uses HMAC-SHA256 so the same plaintext always produces the same hash,
        enabling WHERE clause lookups on encrypted columns without decryption.

        The value is lowercased before hashing to support case-insensitive
        lookups (matching the existing COLLATE NOCASE behavior for emails).

        Args:
            value: Plaintext value to hash (will be lowercased)

        Returns:
            Hex-encoded HMAC-SHA256 hash (64 characters)
        """
        return hmac.new(
            self._hmac_key,
            value.lower().encode(),
            hashlib.sha256,
        ).hexdigest()

    def blind_indexes(self, value: str) -> tuple[str, ...]:
        """Return current and optional previous blind indexes for lookups."""
        current = self.blind_index(value)
        if self._previous_hmac_key is None:
            return (current,)

        previous = hmac.new(
            self._previous_hmac_key,
            value.lower().encode(),
            hashlib.sha256,
        ).hexdigest()
        if previous == current:
            return (current,)
        return current, previous


# Module-level singleton (lazy initialized)
_encryptor: FieldEncryptor | None = None


def _derive_field_keys(secret: str) -> tuple[bytes, bytes]:
    secret_bytes = secret.encode()
    encryption_key = hashlib.pbkdf2_hmac(
        "sha256",
        secret_bytes,
        _ENCRYPTION_SALT,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    hmac_key = hashlib.pbkdf2_hmac(
        "sha256",
        secret_bytes,
        _HMAC_SALT,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    return encryption_key, hmac_key


def get_field_encryptor() -> FieldEncryptor:
    """
    Get or create the global FieldEncryptor singleton.

    Returns:
        Configured FieldEncryptor instance

    Raises:
        ValueError: If JWT_SECRET is not configured
    """
    global _encryptor
    if _encryptor is None:
        _encryptor = FieldEncryptor.from_settings()
    return _encryptor
