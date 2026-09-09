"""Shared HMAC helpers for webhook providers without SDK verifiers."""

from __future__ import annotations

import hashlib
import hmac


def verify_hmac_sha256_hexdigest(
    *,
    payload: bytes,
    signature: str,
    secret: str,
    prefix: str = "",
) -> bool:
    """Verify a hex SHA-256 HMAC in constant time."""

    if not signature or not secret:
        return False

    candidate = str(signature).strip()
    if prefix and candidate.startswith(prefix):
        candidate = candidate[len(prefix) :]
    if not candidate:
        return False

    expected = hmac.new(
        str(secret).encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(candidate.lower(), expected)
