from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from core.logging_config import get_logger
from ui.security import bootstrap as bootstrap_module

logger = get_logger(__name__)

SPOKE_TOKEN_HEADER_NAME = "X-Spoke-Token"  # nosec B105
SPOKE_TOKEN_COOKIE_NAME = "viola_spoke"  # nosec B105
SPOKE_TOKEN_QUERY_PARAM = "spoke_token"  # nosec B105
SPOKE_TOKEN_SECRET_FILENAME = "spoke_token_secret"  # nosec B105  # pragma: allowlist secret
SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 90
# Server-side maximum age for a spoke credential, in seconds. Tokens whose
# embedded ``issued_at`` is older than this are rejected even if the HMAC
# signature still matches. Without this check a stolen QR pairing token
# would be valid forever (since the cookie max-age is only a client-side
# hint and the signature has no expiry). Set just under the cookie hint so
# a freshly-issued credential lasts the full advertised window.
#
# 2026-08-01 (#4434): cut from 365 days to 90. A year-long credential far
# outlives the pairing it came from, and the desktop pairing screen used to
# put one on the display in plain text. Paired devices renew transparently
# well before the wall (see ``/bootstrap/spoke-session``), so the shorter
# window costs a live speaker nothing.
SPOKE_TOKEN_MAX_AGE_SECONDS = SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS

# Credentials minted BEFORE this instant were issued under the old 365-day
# policy and are honoured for that full advertised window, so devices paired
# before the upgrade keep working instead of dropping off the network on the
# day it lands. Anything minted after it gets SPOKE_TOKEN_MAX_AGE_SECONDS.
# (2026-08-01T00:00:00Z — the #4434 policy change.)
SPOKE_TOKEN_LEGACY_POLICY_CUTOFF_EPOCH = 1785542400
SPOKE_TOKEN_LEGACY_MAX_AGE_SECONDS = 60 * 60 * 24 * 365

# A credential that has not been used for this long is retired even if its
# signature is still inside the max-age window. A live speaker refreshes this
# every time it connects, so idle expiry only ever reaps devices that stopped
# being speakers — including a credential that leaked and was never used.
SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
# Reject tokens dated more than this many seconds in the future. A small
# skew tolerance lets paired hubs whose clocks drift forward briefly stay
# usable; very-future timestamps almost always mean a forged/tampered token.
SPOKE_TOKEN_FUTURE_SKEW_SECONDS = 5 * 60

_SPOKE_TOKEN_PREFIX = "vspk1"  # nosec B105


@dataclass(frozen=True)
class IssuedSpokeCredential:
    token: str
    device_id: str
    issued_at: int


@dataclass(frozen=True)
class VerifiedSpokeCredential:
    device_id: str | None
    issued_at: int | None
    source: str
    token_id: str
    hub_user_id: str | None


def _secret_dir() -> Path:
    return bootstrap_module.ensure_bootstrap_secret_dir()


def _secret_path() -> Path:
    return _secret_dir() / SPOKE_TOKEN_SECRET_FILENAME


def _apply_secret_file_permissions(path: Path) -> None:
    bootstrap_module.apply_bootstrap_secret_file_permissions(path)


def ensure_spoke_token_secret() -> str:
    path = _secret_path()
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
        logger.warning("Spoke credential secret at %s was empty; regenerating.", path)

    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    _apply_secret_file_permissions(path)
    return secret


def load_spoke_token_secret() -> str | None:
    path = _secret_path()
    if not path.exists():
        return None
    secret = path.read_text(encoding="utf-8").strip()
    return secret or None


def issue_spoke_credential(device_id: str | None = None) -> IssuedSpokeCredential:
    secret = ensure_spoke_token_secret()
    issued_at = int(time.time())
    scoped_device_id = device_id or secrets.token_urlsafe(12)
    nonce = secrets.token_hex(8)
    message = f"{_SPOKE_TOKEN_PREFIX}.{scoped_device_id}.{issued_at}.{nonce}".encode()
    signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    token = f"{_SPOKE_TOKEN_PREFIX}.{scoped_device_id}.{issued_at}.{nonce}.{signature}"
    return IssuedSpokeCredential(token=token, device_id=scoped_device_id, issued_at=issued_at)


def _spoke_token_id(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def _default_hub_user_id() -> str | None:
    from config.settings import settings
    from core.user_context import get_current_or_device_user_id

    if str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud":
        return None
    return get_current_or_device_user_id()


def _max_age_for(issued_at: int) -> int:
    """Return the signed lifetime that applies to a credential.

    Credentials minted before the #4434 policy change keep the 365-day window
    they were advertised with, so an upgrade never unpairs a working speaker.
    Everything minted since gets the current, much shorter window.
    """
    if issued_at < SPOKE_TOKEN_LEGACY_POLICY_CUTOFF_EPOCH:
        return SPOKE_TOKEN_LEGACY_MAX_AGE_SECONDS
    return SPOKE_TOKEN_MAX_AGE_SECONDS


def _device_is_allowed(device_id: str, *, issued_at: int) -> bool:
    """Apply per-device revocation and idle expiry, and record the use.

    Registry problems must never silently open the door, so a device that is
    known-revoked or known-idle is refused; an unexpected registry failure is
    logged and the credential is refused too (fail closed).
    """
    from ui.security import spoke_device_registry

    try:
        record = spoke_device_registry.get_device(device_id)
        if record is not None:
            if record.revoked:
                logger.info("Spoke credential rejected: device %s is revoked", device_id)
                return False
            idle = int(time.time()) - record.last_seen
            if record.last_seen and idle > SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS:
                logger.info(
                    "Spoke credential rejected: device %s unused for %ds (idle max %ds)",
                    device_id,
                    idle,
                    SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS,
                )
                return False
        # First sight of a device paired before this hub kept a registry: adopt
        # it so it is listed and revocable from now on.
        spoke_device_registry.record_device_seen(
            device_id,
            issued_at=issued_at,
            legacy=issued_at < SPOKE_TOKEN_LEGACY_POLICY_CUTOFF_EPOCH,
        )
    except Exception:
        logger.exception("Spoke device registry check failed for device %s; refusing the credential", device_id)
        return False
    return True


def verify_spoke_credential(token: str) -> VerifiedSpokeCredential | None:
    candidate = token.strip()
    if not candidate:
        return None

    parts = candidate.split(".", 4)
    if len(parts) == 5:
        prefix, device_id, issued_at_str, nonce, signature = parts
        if prefix != _SPOKE_TOKEN_PREFIX:
            return None
        secret = load_spoke_token_secret()
        if not secret:
            return None
        try:
            issued_at = int(issued_at_str)
        except ValueError:
            return None
        message = f"{prefix}.{device_id}.{issued_at}.{nonce}".encode()
        expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        # Enforce server-side expiry: a paired spoke credential must not be
        # accepted past SPOKE_TOKEN_MAX_AGE_SECONDS. Without this an HMAC
        # token sniffed once would remain valid until the spoke secret is
        # rotated (which invalidates every paired device at once).
        now = int(time.time())
        age = now - issued_at
        max_age = _max_age_for(issued_at)
        if age > max_age:
            logger.info(
                "Spoke credential rejected: token age %ds exceeds max %ds (device %s)",
                age,
                max_age,
                device_id,
            )
            return None
        if age < -SPOKE_TOKEN_FUTURE_SKEW_SECONDS:
            logger.warning(
                "Spoke credential rejected: token issued_at is %ds in the future (device %s)",
                -age,
                device_id,
            )
            return None
        # Per-device state: one device can be revoked without rotating the
        # secret (which would unpair the whole house), and a device that
        # stopped being a speaker is retired on idle rather than trusted for
        # the full signed lifetime.
        if not _device_is_allowed(device_id, issued_at=issued_at):
            return None
        return VerifiedSpokeCredential(
            device_id=device_id,
            issued_at=issued_at,
            source="paired",
            token_id=_spoke_token_id(candidate),
            hub_user_id=_default_hub_user_id(),
        )

    legacy_shared_secret = os.environ.get("VIOLA_SPOKE_TOKEN", "").strip()
    if legacy_shared_secret and hmac.compare_digest(candidate, legacy_shared_secret):
        return VerifiedSpokeCredential(
            device_id=None,
            issued_at=None,
            source="legacy_shared_secret",
            token_id=_spoke_token_id(candidate),
            hub_user_id=_default_hub_user_id(),
        )
    return None


def validate_spoke_token(token: str) -> VerifiedSpokeCredential | None:
    return verify_spoke_credential(token)


def get_presented_spoke_token(websocket) -> str | None:
    header_value = getattr(websocket, "headers", {}).get(SPOKE_TOKEN_HEADER_NAME, "").strip()
    if header_value:
        return header_value
    cookie_value = getattr(websocket, "cookies", {}).get(SPOKE_TOKEN_COOKIE_NAME, "").strip()
    if cookie_value:
        return cookie_value
    query_value = getattr(websocket, "query_params", {}).get(SPOKE_TOKEN_QUERY_PARAM, "").strip()
    if query_value:
        return query_value
    return None


def get_verified_spoke_credential(websocket) -> VerifiedSpokeCredential | None:
    token = get_presented_spoke_token(websocket)
    if not token:
        return None
    return verify_spoke_credential(token)


__all__ = [
    "SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS",
    "SPOKE_TOKEN_COOKIE_NAME",
    "SPOKE_TOKEN_HEADER_NAME",
    "SPOKE_TOKEN_IDLE_MAX_AGE_SECONDS",
    "SPOKE_TOKEN_LEGACY_MAX_AGE_SECONDS",
    "SPOKE_TOKEN_LEGACY_POLICY_CUTOFF_EPOCH",
    "SPOKE_TOKEN_MAX_AGE_SECONDS",
    "SPOKE_TOKEN_QUERY_PARAM",
    "IssuedSpokeCredential",
    "VerifiedSpokeCredential",
    "ensure_spoke_token_secret",
    "get_presented_spoke_token",
    "get_verified_spoke_credential",
    "issue_spoke_credential",
    "load_spoke_token_secret",
    "validate_spoke_token",
    "verify_spoke_credential",
]
