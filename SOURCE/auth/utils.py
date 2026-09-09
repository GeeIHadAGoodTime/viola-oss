"""Shared authentication utilities."""

from __future__ import annotations

import hashlib
import importlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Protocol, cast

from core.logging_config import get_logger

logger = get_logger("viola.auth.utils")

DEFAULT_PASSWORD_HASH_ROUNDS = 12


def generate_id() -> str:
    """Generate a secure random UUID4 string for database records."""
    return str(uuid.uuid4())


def generate_secure_token(length: int = 32) -> str:
    """Generate a URL-safe high-entropy token."""
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """Hash a high-entropy token for storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def expires_at(
    *,
    minutes: int | None = None,
    days: int | None = None,
) -> datetime:
    """Calculate a UTC expiration datetime."""
    if minutes is None and days is None:
        raise ValueError("Must provide at least one of: minutes, days")

    return datetime.now(UTC) + timedelta(
        minutes=minutes or 0,
        days=days or 0,
    )


def session_expires_at(days: int = 30) -> datetime:
    """Calculate a session expiration datetime."""
    return expires_at(days=days)


def magic_link_expires_at(minutes: int = 15) -> datetime:
    """Calculate a magic-link expiration datetime."""
    return expires_at(minutes=minutes)


class _BcryptModule(Protocol):
    def gensalt(self, rounds: int) -> bytes: ...

    def hashpw(self, password: bytes, salt: bytes) -> bytes: ...

    def checkpw(self, password: bytes, hashed_password: bytes) -> bool: ...


class _PasslibBcrypt(Protocol):
    def using(self, *, rounds: int) -> _PasslibBcrypt: ...

    def hash(self, secret: str) -> str: ...

    def verify(self, secret: str, hash: str) -> bool: ...


class _PasslibHashModule(Protocol):
    bcrypt: _PasslibBcrypt


def _import_module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _load_bcrypt() -> _BcryptModule | None:
    try:
        return cast(_BcryptModule, _import_module("bcrypt"))
    except ImportError:
        return None


def _load_passlib_bcrypt() -> _PasslibBcrypt | None:
    try:
        module = cast(_PasslibHashModule, _import_module("passlib.hash"))
    except ImportError:
        return None
    return module.bcrypt


def _prehash_password(password: str) -> bytes:
    """SHA-256 prehash to avoid bcrypt's 72-byte input truncation."""
    import base64
    import hashlib

    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


_prehash = _prehash_password


def hash_password(password: str, rounds: int = DEFAULT_PASSWORD_HASH_ROUNDS) -> str:
    """Hash a secret with bcrypt-compatible storage semantics."""
    if not password:
        raise ValueError("Password cannot be empty")

    prehashed = _prehash_password(password)
    bcrypt_module = _load_bcrypt()
    if bcrypt_module is not None:
        salt = bcrypt_module.gensalt(rounds=rounds)
        hashed = bcrypt_module.hashpw(prehashed, salt)
        return hashed.decode("utf-8")

    passlib_bcrypt = _load_passlib_bcrypt()
    if passlib_bcrypt is None:
        raise RuntimeError("No credential hashing backend available; install `bcrypt` or `passlib`.")

    logger.warning("bcrypt backend not available; using passlib fallback")
    return passlib_bcrypt.using(rounds=rounds).hash(prehashed.decode("ascii"))


def verify_password(password: str, password_hash: str) -> bool:
    """Return True when a plaintext secret matches a stored bcrypt hash."""
    if not password or not password_hash:
        return False

    try:
        prehashed = _prehash_password(password)
        hash_bytes = password_hash.encode("utf-8")
        bcrypt_module = _load_bcrypt()
        if bcrypt_module is not None:
            if bcrypt_module.checkpw(prehashed, hash_bytes):
                return True
            return bcrypt_module.checkpw(password.encode("utf-8"), hash_bytes)

        passlib_bcrypt = _load_passlib_bcrypt()
        if passlib_bcrypt is None:
            return False

        logger.warning("bcrypt backend not available; using passlib fallback")
        if passlib_bcrypt.verify(prehashed.decode("ascii"), password_hash):
            return True
        return passlib_bcrypt.verify(password, password_hash)
    except Exception as exc:
        logger.error("Credential verification error: %s", exc)
        return False


def mask_email(email: str) -> str:
    """Mask an email address for safe logging.

    Example::

        >>> mask_email("john@example.com")
        'j***@example.com'
    """
    if not email or "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    return f"{local[0]}***@{domain}" if local else f"***@{domain}"


def mask_email_for_display(email: str) -> str:
    """AUTH-13: stable 1+3 display mask for user-facing UI.

    Always emits ``<first-char>***@<domain>`` for addresses with a non-empty
    local-part, ``***@<domain>`` when the local-part is empty, and ``***``
    when the address is entirely malformed (no ``@`` or empty string).

    >>> mask_email_for_display("alice@example.com")
    'a***@example.com'
    >>> mask_email_for_display("")
    '***'
    """
    if not email:
        return "***"
    if "@" not in email:
        return "***"
    local, domain = email.rsplit("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"
