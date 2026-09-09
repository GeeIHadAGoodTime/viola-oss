"""
Core module for Viola - foundational utilities and orchestration.

Shared application services and utilities.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    # Cache
    "BaseTTLCache",
    "Cache",
    "CacheStats",
    # Validation
    "sanitize_error_message",
    "validate_command_text",
    "validate_file_path",
    "validate_host",
    "validate_port",
    "validate_query",
    "validate_seek_seconds",
    "validate_volume",
]

_CACHE_EXPORTS = {"BaseTTLCache", "Cache", "CacheStats"}
_VALIDATION_EXPORTS = {
    "sanitize_error_message",
    "validate_command_text",
    "validate_file_path",
    "validate_host",
    "validate_port",
    "validate_query",
    "validate_seek_seconds",
    "validate_volume",
}


def __getattr__(name: str) -> Any:
    """Resolve package re-exports lazily to avoid import-time fan-out."""
    if name in _CACHE_EXPORTS:
        from core import cache as _cache

        return getattr(_cache, name)
    if name in _VALIDATION_EXPORTS:
        from core import validation as _validation

        return getattr(_validation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
