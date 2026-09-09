"""Shared LLM pricing and token-cost helpers for product and billing consumers.

This module is the canonical source for model token prices. Plan enforcement
uses these helpers, and offline calculators should import them instead of
duplicating pricing tables.

Pricing shape parity with Claude Code (see ``modelCost.ts``):

- ``input``: cents per 1M input tokens (non-cached prompt)
- ``output``: cents per 1M output tokens
- ``cached``: cents per 1M cache-read tokens (re-using a cached prefix)
- ``cache_write``: cents per 1M cache-creation/write tokens
- ``web_search``: cents per web-search server-tool request (per-request,
  NOT per-token)

Older callers that pass only ``input``/``output``/``cached`` continue to
work — ``calculate_cost_cents`` accepts the broader usage shape via
keyword arguments with default ``0``.
"""

from __future__ import annotations

import contextvars
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Cents per 1M tokens (input/output/cached/cache_write) and cents per
# request (web_search). Unset numeric components default to ``0`` at lookup.
LLM_PRICING_CENTS: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {
        "input": 15,
        "output": 60,
        "cached": 7.5,
        "cache_write": 18.75,
        "web_search": 1.0,
    },
    "gpt-5": {
        "input": 125,
        "output": 1000,
        "cached": 12.5,
        "cache_write": 156.25,
        "web_search": 1.0,
    },
    "gpt-5-mini": {
        "input": 25,
        "output": 200,
        "cached": 2.5,
        "cache_write": 31.25,
        "web_search": 1.0,
    },
    "gpt-5-nano": {
        "input": 5,
        "output": 40,
        "cached": 0.5,
        "cache_write": 6.25,
        "web_search": 1.0,
    },
    "gpt-5.4-mini": {
        "input": 75,
        "output": 450,
        "cached": 7.5,
        "cache_write": 93.75,
        "web_search": 1.0,
    },
    "gpt-5.4-nano": {
        "input": 20,
        "output": 125,
        "cached": 2,
        "cache_write": 25.0,
        "web_search": 1.0,
    },
    "gpt-5.4": {
        "input": 250,
        "output": 1500,
        "cached": 25,
        "cache_write": 312.5,
        "web_search": 1.0,
    },
}

_PRICING_COMPONENTS = ("input", "output", "cached", "cache_write", "web_search")

# Session/request scoped mirror of Claude Code's ``hasUnknownModelCost`` flag.
# This used to be process-global, which leaked one user's unknown-model warning
# into unrelated sessions. ContextVar keeps the warning in the active execution
# context; SessionCostTracker persists it on the session snapshot when needed.
_unknown_models_seen: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "pricing_unknown_models_seen",
    default=frozenset(),
)


UNKNOWN_MODEL_COST_WARNING = "costs may be inaccurate due to usage of unknown models"


def _normalize_pricing(raw: dict[str, float]) -> dict[str, float]:
    """Return a pricing dict with every component present (default 0.0)."""
    return {component: float(raw.get(component, 0.0)) for component in _PRICING_COMPONENTS}


def set_has_unknown_model_cost(model: str | None = None) -> None:
    """Mark the current pricing context as having encountered an unknown model.

    Mirrors Claude Code's ``setHasUnknownModelCost()``. Once set, the
    cost-summary UI should display ``UNKNOWN_MODEL_COST_WARNING`` next to
    the total cost so users know the figure is best-effort.
    """
    seen = set(_unknown_models_seen.get())
    seen.add(str(model or "<unknown>"))
    _unknown_models_seen.set(frozenset(seen))


def has_unknown_model_cost() -> bool:
    """Return True if unknown-model fallback fired in this context."""
    return bool(_unknown_models_seen.get())


def unknown_models_seen() -> tuple[str, ...]:
    """Return the (sorted) set of model identifiers that hit the fallback."""
    return tuple(sorted(_unknown_models_seen.get()))


def reset_unknown_model_cost_state() -> None:
    """Clear the unknown-model flag for the current context."""
    _unknown_models_seen.set(frozenset())


def _pricing_for_spend_tracking(model: str) -> dict[str, float]:
    """Return model pricing, falling back conservatively for unknown models.

    On miss this also sets the ``has_unknown_model_cost`` flag so the
    cost-summary surface can warn the user that totals are best-effort.
    """
    pricing = LLM_PRICING_CENTS.get(model)
    if pricing:
        return _normalize_pricing(pricing)
    fallback = max(LLM_PRICING_CENTS.values(), key=lambda p: p["output"])
    set_has_unknown_model_cost(model)
    logger.warning(
        "Unknown model '%s' - using most expensive pricing for spend tracking (totals may be inaccurate)",
        model,
    )
    return _normalize_pricing(fallback)


def calculate_cost_cents(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    *,
    cache_write_tokens: int = 0,
    web_search_requests: int = 0,
) -> float:
    """Calculate the cost of an LLM call in cents.

    ``input_tokens`` is the *total* prompt tokens reported by the provider
    (which already includes both cached and uncached portions on OpenAI).
    We subtract ``cached_tokens`` to get the uncached input portion and
    price that at the full input rate; cached tokens get the cache-read
    rate. ``cache_write_tokens`` is priced separately at the cache-write
    rate, and ``web_search_requests`` is a per-request charge for the
    web-search server tool.
    """
    pricing = _pricing_for_spend_tracking(model)
    uncached_input = max(0, int(input_tokens) - int(cached_tokens))
    cents = (
        uncached_input * pricing["input"] / 1_000_000
        + int(cached_tokens) * pricing["cached"] / 1_000_000
        + int(output_tokens) * pricing["output"] / 1_000_000
        + int(cache_write_tokens) * pricing["cache_write"] / 1_000_000
        + int(web_search_requests) * pricing["web_search"]
    )
    return cents


def calculate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    *,
    cache_write_tokens: int = 0,
    web_search_requests: int = 0,
) -> float:
    """Calculate the cost of an LLM call in USD."""
    return (
        calculate_cost_cents(
            model,
            input_tokens,
            output_tokens,
            cached_tokens,
            cache_write_tokens=cache_write_tokens,
            web_search_requests=web_search_requests,
        )
        / 100
    )


def get_llm_pricing_usd(model: str) -> dict[str, float]:
    """Return LLM pricing in USD per 1M tokens (and per request for web_search) for a given model."""
    pricing_cents = _pricing_for_spend_tracking(model)
    return {k: v / 100 for k, v in pricing_cents.items()}


def format_cost_warning_suffix() -> str:
    """Return ``" (<warning>)"`` if unknown-model cost was hit, else ``""``.

    Designed to be appended directly to a formatted cost string in any
    user-visible cost summary (CLI, dashboard, telemetry blob).
    """
    return f" ({UNKNOWN_MODEL_COST_WARNING})" if has_unknown_model_cost() else ""


def usage_to_pricing_kwargs(usage: Any) -> dict[str, int]:
    """Best-effort extract pricing-relevant token counts from a usage object.

    Accepts any of:
      - ``services.llm.spend_accounting.LlmTokenUsage``
      - OpenAI/Anthropic SDK usage objects
      - plain dicts

    Returns kwargs suitable for ``calculate_cost_cents`` (``cached_tokens``,
    ``cache_write_tokens``, ``web_search_requests``). Missing fields default
    to ``0``. Caller still passes ``input_tokens``/``output_tokens`` directly.
    """

    def _get(name: str) -> int:
        if usage is None:
            return 0
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if value is None:
            # Anthropic shape: cache_creation_input_tokens / cache_read_input_tokens
            anthropic_map = {
                "cached_tokens": "cache_read_input_tokens",
                "cache_write_tokens": "cache_creation_input_tokens",
            }
            alt = anthropic_map.get(name)
            if alt:
                value = usage.get(alt) if isinstance(usage, dict) else getattr(usage, alt, None)
        if value is None and name == "web_search_requests":
            server_tool = (
                usage.get("server_tool_use") if isinstance(usage, dict) else getattr(usage, "server_tool_use", None)
            )
            if server_tool is not None:
                value = (
                    server_tool.get("web_search_requests")
                    if isinstance(server_tool, dict)
                    else getattr(server_tool, "web_search_requests", None)
                )
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "cached_tokens": _get("cached_tokens"),
        "cache_write_tokens": _get("cache_write_tokens"),
        "web_search_requests": _get("web_search_requests"),
    }
