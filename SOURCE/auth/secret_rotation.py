"""Helpers for bounded JWT-secret rotation overlap."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from cryptography.fernet import Fernet, InvalidToken

_T = TypeVar("_T")


def _as_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8")


def _parse_previous_expires_at(value: datetime | str | None) -> datetime | None:
    """Parse a previous-secret overlap deadline as an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = value.strip()
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _settings_previous_expires_at() -> datetime | None:
    """Return the configured previous-secret deadline, if settings is available."""
    try:
        from config.settings import settings

        raw = getattr(settings, "jwt_secret_previous_expires_at", "")
    except Exception:
        return None
    return _parse_previous_expires_at(raw)


def _ensure_previous_key_within_overlap(
    previous_expires_at: datetime | str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    deadline = (
        _settings_previous_expires_at()
        if previous_expires_at is None
        else _parse_previous_expires_at(previous_expires_at)
    )
    if deadline is None:
        return

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    if current_time.astimezone(UTC) >= deadline:
        raise InvalidToken("previous JWT secret overlap has expired")


def decrypt_with_fallback(
    ciphertext: str | bytes,
    current_key: bytes,
    previous_key: bytes | None = None,
    previous_expires_at: datetime | str | None = None,
) -> tuple[bytes, bool]:
    """Decrypt with the current Fernet key, then optional previous key."""
    token = _as_bytes(ciphertext)
    try:
        return Fernet(current_key).decrypt(token), False
    except InvalidToken:
        if previous_key is None:
            raise
        _ensure_previous_key_within_overlap(previous_expires_at)
        return Fernet(previous_key).decrypt(token), True


def schedule_re_encrypt(
    *,
    used_previous: bool,
    re_encrypt: Callable[[], _T] | None = None,
) -> bool:
    """
    Signal or execute lazy re-encryption after a previous-key read.

    Store-specific callers can pass a callback to perform the write immediately.
    Without a callback this returns True when the caller should queue or perform
    its own re-encrypt step.
    """
    if not used_previous:
        return False
    if re_encrypt is not None:
        re_encrypt()
    return True


__all__ = [
    "decrypt_with_fallback",
    "schedule_re_encrypt",
]
