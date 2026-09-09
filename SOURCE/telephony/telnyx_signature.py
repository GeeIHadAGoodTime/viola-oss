"""Shared Telnyx webhook signature verification helpers."""

from __future__ import annotations

import base64
import binascii
import re
import time

from telnyx import TelnyxError
from telnyx.lib import verify_signature as verify_telnyx_sdk_signature

from core.logging_config import get_logger

logger = get_logger(__name__)

TELNYX_TIMESTAMP_TOLERANCE_SECONDS = 300

# A 64-char hex string is the raw 32-byte Ed25519 public key written as hex.
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def telnyx_timestamp_is_fresh(timestamp: str, *, now: float | None = None) -> bool:
    """Return whether a Telnyx webhook timestamp is within the replay window."""
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    return abs((time.time() if now is None else now) - ts) <= TELNYX_TIMESTAMP_TOLERANCE_SECONDS


def _public_key_candidates(public_key: str) -> list[str]:
    """Candidate encodings to try, base64 first.

    Telnyx's portal hands the Ed25519 webhook public key out **base64**-encoded
    (the raw 32 bytes -> 44 chars). The Telnyx SDK base64-decodes whatever string
    it is given. When ``TELNYX_WEBHOOK_PUBLIC_KEY`` is instead stored as 64-char
    **hex** (the raw 32 bytes as hex), base64-decoding those 64 chars yields 48
    bytes and the SDK rejects it with "expected 32 bytes, got 48 bytes" -- which
    silently breaks EVERY call-control webhook (2026-06-25 incident, call
    3aa610d6). Accept both: the key as given, and -- when it is 64 hex chars --
    its hex->base64 re-encoding so the SDK decodes the correct 32 bytes.
    """
    key = (public_key or "").strip()
    if not key:
        return []
    candidates = [key]
    if _HEX64_RE.match(key):
        try:
            candidates.append(base64.b64encode(binascii.unhexlify(key)).decode("ascii"))
        except (binascii.Error, ValueError):
            pass
    return candidates


def verify_telnyx_signature(raw_body: bytes, timestamp: str, signature_b64: str, public_key: str) -> bool:
    """Verify a Telnyx Ed25519 webhook signature.

    Accepts the public key as base64 (Telnyx portal default) OR 64-char hex; a
    hex key is normalized to base64 before the SDK decodes it, so a hex-formatted
    ``TELNYX_WEBHOOK_PUBLIC_KEY`` no longer fails closed on every webhook.
    """
    try:
        body = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.warning("Telnyx webhook signature verification failed: %s", exc)
        return False

    headers = {
        "telnyx-signature-ed25519": signature_b64,
        "telnyx-timestamp": timestamp,
    }
    candidates = _public_key_candidates(public_key)
    if not candidates:
        logger.warning("Telnyx webhook signature verification failed: empty public key")
        return False

    last_exc: Exception | None = None
    for key in candidates:
        try:
            verify_telnyx_sdk_signature(body, headers, key)
            return True
        except TelnyxError as exc:
            last_exc = exc
    logger.warning("Telnyx webhook signature verification failed: %s", last_exc)
    return False
