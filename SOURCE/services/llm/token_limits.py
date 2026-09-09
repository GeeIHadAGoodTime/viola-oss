"""Token-limit coercion helpers for LLM provider configuration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_LLM_MAX_TOKENS_CAP = 150


def coerce_token_limit(value: Any, *, default: int = DEFAULT_LLM_MAX_TOKENS_CAP) -> int:
    """Return a positive integer token limit, ignoring mock/config contaminants."""

    if isinstance(value, bool):
        logger.warning("Ignoring boolean token limit %r; using default %d", value, default)
        return default
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float) and value.is_integer():
        return max(1, int(value))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return max(1, int(stripped))
    logger.warning(
        "Ignoring invalid token limit type %s; using default %d",
        type(value).__name__,
        default,
    )
    return default


def configured_llm_max_tokens_cap(
    *,
    default: int = DEFAULT_LLM_MAX_TOKENS_CAP,
    settings_getter: Callable[[], Any] | None = None,
) -> int:
    """Read settings.llm_max_tokens_cap with runtime type hardening."""

    try:
        if settings_getter is None:
            from config.settings import settings

            raw = getattr(settings, "llm_max_tokens_cap", default)
        else:
            raw = getattr(settings_getter(), "llm_max_tokens_cap", default)
    except Exception as exc:
        logger.warning("Could not read llm_max_tokens_cap: %s", exc)
        return default
    return coerce_token_limit(raw, default=default)


def clamp_max_tokens(max_tokens: Any, *, default: int = DEFAULT_LLM_MAX_TOKENS_CAP) -> int:
    """Clamp a requested max_tokens value to the configured cap."""

    requested = coerce_token_limit(max_tokens, default=default)
    return min(requested, configured_llm_max_tokens_cap(default=default))


__all__ = [
    "DEFAULT_LLM_MAX_TOKENS_CAP",
    "clamp_max_tokens",
    "coerce_token_limit",
    "configured_llm_max_tokens_cap",
]
