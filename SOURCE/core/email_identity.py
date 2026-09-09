"""Canonical email identity helpers for account-adjacent records."""

from __future__ import annotations

import unicodedata
from typing import Any


def canonical_email_identity(value: Any) -> str:
    """Return the stable comparison key for an email-like value."""
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value).strip()).casefold()
