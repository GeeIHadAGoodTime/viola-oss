"""Redaction helpers for user-facing support artifacts."""

from __future__ import annotations

import re
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|secret|access[_-]?token|refresh[_-]?token|authorization|auth[_-]?token)"
    r"\b[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)([\"']?)"
)


def redact_support_payload(value: Any) -> Any:
    """Mask secrets, card data, and low-ambiguity PII in support payloads."""
    redacted = value
    try:
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(redacted)
    except (ImportError, TypeError, ValueError) as exc:
        logger.debug("Diagnostic payload redaction unavailable: %s", exc)
    return _mask_secret_values(redacted)


def redact_support_text(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = redact_support_payload(value)
    return redacted if isinstance(redacted, str) else str(redacted)


def _mask_secret_values(value: Any) -> Any:
    try:
        from core.secrets_mask import mask_dict_secrets, mask_secrets_in_text
    except ImportError as exc:
        logger.debug("Secret masking unavailable: %s", exc)
        return value

    if isinstance(value, str):
        redacted = _SECRET_KEY_VALUE_RE.sub(r"\1[REDACTED:SECRET]\3", value)
        return mask_secrets_in_text(redacted)
    if isinstance(value, dict):
        return mask_dict_secrets(value)
    if isinstance(value, list):
        return [_mask_secret_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_secret_values(item) for item in value)
    return value


__all__ = ["redact_support_payload", "redact_support_text"]
