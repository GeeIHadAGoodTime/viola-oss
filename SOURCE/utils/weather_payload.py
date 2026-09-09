"""
Utilities for normalizing weather payloads returned by the Viola backend.

This module provides a single helper to ensure all weather responses expose a
stable interface to every client:

- The canonical data lives under the ``data`` key for structured access.
- Legacy clients continue to read top-level fields like ``temperature``.
- Errors propagate consistently while keeping ``data`` present (possibly empty).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


def build_weather_payload(
    weather_data: Mapping[str, Any] | None,
) -> MutableMapping[str, Any]:
    """
    Create a normalized, envelope-ready weather payload.

    Args:
        weather_data: Raw weather fields (temperature, condition, etc.).

    Returns:
        A dictionary containing only domain weather fields. Control keys such as
        ``ok``/``error`` are stripped so callers can embed the result inside the
        canonical response envelope.
    """

    sanitized = dict(weather_data or {})
    sanitized.pop("ok", None)
    sanitized.pop("error", None)
    return sanitized
