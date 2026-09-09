"""Caller name validation for phone calls."""

from __future__ import annotations

import re

_ASSISTANT_CALLER_NAMES = frozenset(
    {
        "ai assistant",
        "assistant",
        "the ai assistant",
        "the assistant",
        "the viola assistant",
        "viola",
        "viola ai",
        "viola assistant",
    }
)


def _normalized_caller_name(name: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split())


def is_assistant_caller_name(name: object) -> bool:
    """Return True when the owner/on-behalf-of slot contains the assistant."""

    return _normalized_caller_name(name) in _ASSISTANT_CALLER_NAMES


def validate_caller_name(name: str) -> str | None:
    """Validate and sanitize caller name.

    Returns sanitized name or None if invalid.
    """
    if not name or not name.strip():
        return None
    name = name.strip()[:50]  # Max 50 chars
    if len(name) < 2:
        return None
    return name
