"""Shared LLM spend/rate accounting helpers for direct provider clients.

Accounting shape: mirrors Claude Code's ``BetaUsage``/``ModelCosts`` shape
in ``src/utils/modelCost.ts`` so cost accounting includes cache-write and
server-tool (web-search) charges in addition to plain input/output/cache-read.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LlmTokenUsage:
    """Per-call token usage in the broader Claude-Code-parity shape.

    Field map (Viola name → Anthropic/Claude Code name):
      - ``input_tokens``          → ``input_tokens``
      - ``output_tokens``         → ``output_tokens``
      - ``cached_tokens``         → ``cache_read_input_tokens``
      - ``cache_write_tokens``    → ``cache_creation_input_tokens``
      - ``web_search_requests``   → ``server_tool_use.web_search_requests``

    Older callers that only set ``input_tokens``/``output_tokens``/
    ``cached_tokens`` continue to work — the new fields default to ``0``.
    """

    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    web_search_requests: int = 0

    @property
    def total_tokens(self) -> int:
        return max(1, int(self.input_tokens) + int(self.output_tokens))


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return max(0, int(stripped))
    return default


def _get_mapping_or_attr(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def estimate_openai_payload_usage(payload: dict[str, Any], *, default_output_tokens: int) -> LlmTokenUsage:
    requested_output = (
        _coerce_nonnegative_int(payload.get("max_output_tokens"))
        or _coerce_nonnegative_int(payload.get("max_completion_tokens"))
        or _coerce_nonnegative_int(payload.get("max_tokens"))
        or max(1, int(default_output_tokens))
    )
    try:
        serialized = json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        serialized = str(payload)
    estimated_input = max(1, math.ceil(len(serialized) / 4))
    return LlmTokenUsage(input_tokens=estimated_input, output_tokens=requested_output)


def usage_from_openai_usage(value: Any, fallback: LlmTokenUsage) -> LlmTokenUsage:
    if value is None:
        return fallback

    input_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(value, "input_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "prompt_tokens"))
        or fallback.input_tokens
    )
    output_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(value, "output_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "completion_tokens"))
        or fallback.output_tokens
    )
    input_details = _get_mapping_or_attr(value, "input_tokens_details") or _get_mapping_or_attr(
        value,
        "prompt_tokens_details",
    )
    cached_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(input_details, "cached_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cached_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cache_read_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cache_read_input_tokens"))
    )
    # Cache-write tokens (Anthropic native; some OpenAI-compat servers also expose it).
    cache_write_tokens = (
        _coerce_nonnegative_int(_get_mapping_or_attr(value, "cache_creation_input_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(value, "cache_write_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(input_details, "cache_creation_tokens"))
        or _coerce_nonnegative_int(_get_mapping_or_attr(input_details, "cache_write_tokens"))
    )
    # Web-search server-tool requests (Anthropic ``server_tool_use``).
    server_tool = _get_mapping_or_attr(value, "server_tool_use")
    web_search_requests = (
        _coerce_nonnegative_int(_get_mapping_or_attr(server_tool, "web_search_requests"))
        if server_tool is not None
        else _coerce_nonnegative_int(_get_mapping_or_attr(value, "web_search_requests"))
    )

    return LlmTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        web_search_requests=web_search_requests,
    )


def usage_from_openai_response(value: Any, fallback: LlmTokenUsage) -> LlmTokenUsage:
    response = _get_mapping_or_attr(value, "response") or value
    return usage_from_openai_usage(_get_mapping_or_attr(response, "usage"), fallback)


def usage_from_chat_completion_payload(payload: dict[str, Any], fallback: LlmTokenUsage) -> LlmTokenUsage:
    return usage_from_openai_usage(payload.get("usage") if isinstance(payload, dict) else None, fallback)


def estimated_spend_cents(model: str, usage: LlmTokenUsage) -> int:
    try:
        from services.llm.pricing import calculate_cost_cents

        cents = calculate_cost_cents(
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            web_search_requests=usage.web_search_requests,
        )
    except Exception as exc:
        logger.debug("LLM spend estimate failed for model %s: %s", model, exc)
        return 1
    return max(1, math.ceil(float(cents)))


class LlmSpendReservation:
    """Pre-network reservation and post-network settlement for direct LLM calls."""

    def __init__(
        self,
        *,
        user_id: str,
        model: str,
        estimated_usage: LlmTokenUsage,
        operation: str,
        settle_spend: bool = True,
        fail_closed_on_settle_error: bool = False,
        reserve_tokens: bool = True,
    ) -> None:
        self.user_id = user_id or ""
        self.model = model
        self.estimated_usage = estimated_usage
        self.operation = operation
        self.settle_spend = settle_spend
        self.fail_closed_on_settle_error = fail_closed_on_settle_error
        self.reserve_tokens = reserve_tokens
        self._reserved = False
        self._settled = False
        self._spend_reservation: Any | None = None
        self._last_gate: Any | None = None
        self._managed_llm: bool | None = None

    async def reserve(self) -> None:
        from core.exceptions import LLMQuotaExceededError
        from services.llm.managed_budget import reserve_managed_llm_spend_cap_async, user_uses_managed_llm

        self._managed_llm = user_uses_managed_llm(self.user_id)

        gate = await reserve_managed_llm_spend_cap_async(
            self.user_id,
            managed_llm=self._managed_llm,
            estimated_cents=estimated_spend_cents(self.model, self.estimated_usage),
        )
        self._last_gate = gate
        if not gate.allowed:
            raise LLMQuotaExceededError(
                user_id=self.user_id or "<missing>",
                limit_type="managed_llm_spend_cap",
                current=gate.spent_cents,
                limit=gate.budget_cents,
                reset_at=gate.resets_at,
            )
        self._spend_reservation = gate.reservation

        if self.reserve_tokens:
            from services.llm.rate_limiter import get_rate_limiter

            await get_rate_limiter().reserve(
                self.user_id,
                estimated_tokens=self.estimated_usage.total_tokens,
            )
            self._reserved = True

    async def settle(self, actual_usage: LlmTokenUsage | None = None, *, failed: bool = False) -> None:
        if self._settled:
            return
        self._settled = True

        usage = actual_usage or self.estimated_usage
        actual_tokens = 0 if failed else usage.total_tokens
        if self._reserved:
            try:
                from services.llm.rate_limiter import get_rate_limiter

                await get_rate_limiter().settle(
                    self.user_id,
                    self.estimated_usage.total_tokens,
                    actual_tokens,
                )
            except Exception:
                # Rate-limiter settle failure leaks reserved tokens — over time
                # the user's bucket fills with un-released reservations and they
                # get rate-limited out. Logging at DEBUG previously hid this in
                # production (INFO-level log filter). Promote to ERROR via
                # logger.exception so the failure surfaces in prod logs.
                logger.exception(
                    "LLM rate-limiter settle failed (reserved tokens leaked) operation=%s user_id=%s",
                    self.operation,
                    self.user_id,
                )

        if self._managed_llm is None:
            from services.llm.managed_budget import user_uses_managed_llm

            self._managed_llm = user_uses_managed_llm(self.user_id)
        if not self.settle_spend or not self.user_id or not self._managed_llm:
            return

        try:
            from billing.plan_limiter import get_plan_limiter

            limiter = get_plan_limiter()
            if self._spend_reservation is not None:
                limiter.settle_spend_reservation(
                    self._spend_reservation,
                    actual_cents=estimated_spend_cents(self.model, usage),
                    failed=failed,
                )
            elif not failed:
                limiter.settle_spend(
                    self.user_id,
                    self.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cached_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    web_search_requests=usage.web_search_requests,
                )
        except Exception as exc:
            # A settle failure (in practice a transient Postgres timeout in the
            # plan-limiter worker loop — billing/plan_limiter.py:_pg_run's 15s
            # future.result) means we could not reconcile this call's reservation
            # down to its actual cost. The reserved estimate was already debited
            # fail-closed at reserve time, so the worst-case impact is a small
            # over-hold against the user's OWN spend counter — never a spend-cap
            # *breach* (an under-charge or unmetered call). It MUST NOT block the
            # user. This is the same class as the 2026-06-23 phone-billing
            # lockout (commit 3fa40607): a transient DB blip during settle on one
            # call added the paying user to the live worker's in-memory
            # _blocked_users set (which has no auto-unblock), so every later
            # managed-LLM request — chat, vision, cloud intent, cloud STT,
            # background tasks — denied for the worker's lifetime even though the
            # DB spend and blocked_users tables were empty. A transient infra
            # fault is not a spend-cap breach. Surface the (possibly leaked)
            # reservation loudly for ops reconciliation, but leave the account
            # fully usable; do NOT block_user and do NOT raise billing-closed.
            logger.exception(
                "LLM spend settle failed for operation=%s user_id=%s model=%s; "
                "reservation may be leaked (transient settle error, NOT a cap breach) — "
                "leaving account usable. Reconcile the leaked hold via billing ops if it recurs.",
                self.operation,
                self.user_id,
                self.model,
            )
            if self.fail_closed_on_settle_error:
                # Callers that opt in still learn the settle failed (so they can,
                # e.g., mark the turn) — but a transient settle fault NEVER
                # permanently blocks the user.
                raise RuntimeError("LLM spend settlement failed") from exc


__all__ = [
    "LlmSpendReservation",
    "LlmTokenUsage",
    "estimate_openai_payload_usage",
    "estimated_spend_cents",
    "usage_from_chat_completion_payload",
    "usage_from_openai_response",
    "usage_from_openai_usage",
]
