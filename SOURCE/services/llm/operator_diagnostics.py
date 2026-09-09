"""Operator-facing classification for LLM quota, limit, and fallback policy."""

from __future__ import annotations

import asyncio
import re
from contextvars import ContextVar
from dataclasses import dataclass
from threading import current_thread
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Provider-overload signal — used by services/cloud_intent/dispatch.py to
# override the canonical "Something went wrong while processing your
# request." with the user-facing "Viola is experiencing high volume."
# when an LLM 429/quota fired during THIS request's processing. The fix is
# ai_source-aware in dispatch.py: managed/codex/subscription users get the
# friendly message; BYOK users get the honest provider-side message.
#
# Request-scoped signal store:
#
# The ContextVar stores a mutable request-local signal object, not the
# category string directly. That distinction matters: asyncio.wait_for()
# schedules provider calls in a child Task. A child Task receives a copied
# context, so assigning a new ContextVar value in the child would not flow
# back to the parent dispatch Task. Mutating the request-local signal object
# does flow back because the copied context points at the same per-request
# object.
#
# There is intentionally no module-global timestamp fallback. The old
# fallback was cross-request state: a BYOK user's provider 429 could be read
# by an unrelated managed request. If this path is ever moved into raw
# loop.run_in_executor(), propagate the request context explicitly instead
# of reintroducing global attribution state.
#
# dispatch.py also gates on ai_source being managed/codex/subscription before
# applying the friendly message, so BYOK users still get the honest provider
# side message when their own key is exhausted.


@dataclass(slots=True)
class _ProviderOverloadSignal:
    category: str | None = None


_REQUEST_PROVIDER_OVERLOAD: ContextVar[_ProviderOverloadSignal | None] = ContextVar(
    "viola_request_provider_overload",
    default=None,
)


def _running_loop_id() -> int | None:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None


def _get_or_create_provider_overload_signal() -> _ProviderOverloadSignal:
    signal = _REQUEST_PROVIDER_OVERLOAD.get()
    if signal is None:
        signal = _ProviderOverloadSignal()
        _REQUEST_PROVIDER_OVERLOAD.set(signal)
    return signal


def _note_provider_overload(category: str) -> None:
    """Record that the LLM provider just returned a quota/overload signal.

    Mutates the request-scoped signal object. This preserves visibility
    across child asyncio Tasks created inside the request while avoiding
    module-global cross-request attribution state.
    """
    signal = _get_or_create_provider_overload_signal()
    signal.category = category
    logger.debug(
        "Provider overload signal stamped: category=%s thread=%s loop_id=%s signal_id=%s",
        category,
        current_thread().name,
        _running_loop_id(),
        id(signal),
    )


def recent_provider_overload_category(*, max_age_s: float | None = None) -> str | None:
    """Return THIS REQUEST's provider-overload category if available.

    The ``max_age_s`` kwarg is accepted for backward API compatibility; it no
    longer changes behavior because there is no global timestamp fallback.
    """
    del max_age_s
    signal = _REQUEST_PROVIDER_OVERLOAD.get()
    category = signal.category if signal is not None else None
    logger.debug(
        "Provider overload signal read: category=%s thread=%s loop_id=%s signal_id=%s",
        category,
        current_thread().name,
        _running_loop_id(),
        id(signal) if signal is not None else None,
    )
    return category


def reset_provider_overload_for_request() -> None:
    """Start THIS REQUEST with a fresh provider-overload signal object.

    Child asyncio Tasks created after this reset inherit a reference to this
    object and can mutate it; peer requests get their own object.
    """
    signal = _ProviderOverloadSignal()
    _REQUEST_PROVIDER_OVERLOAD.set(signal)
    logger.debug(
        "Provider overload signal reset: thread=%s loop_id=%s signal_id=%s",
        current_thread().name,
        _running_loop_id(),
        id(signal),
    )


def _status_code(exc: BaseException | str) -> int | None:
    if isinstance(exc, BaseException):
        for attr in ("status_code", "status", "code"):
            raw = getattr(exc, attr, None)
            if isinstance(raw, int):
                return raw
            if isinstance(raw, str) and raw.isdigit():
                return int(raw)
        response = getattr(exc, "response", None)
        raw = getattr(response, "status_code", None)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    text = str(exc).lower()
    match = re.search(r"\b(400|401|402|403|404|408|409|429|500|502|503|504|529)\b", text)
    if match:
        return int(match.group(1))
    return None


def _diagnostic_text(exc: BaseException | str) -> str:
    """Return exception text plus structured provider error payloads."""

    parts = [str(exc)]
    if isinstance(exc, BaseException):
        body = getattr(exc, "body", None)
        if body:
            parts.append(str(body))
        response = getattr(exc, "response", None)
        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text:
            parts.append(response_text)
    return " ".join(parts)


def _with_fallback_policy(
    *,
    category: str,
    source: str,
    status: int | None,
    exc: BaseException | str,
    message: str,
    should_fallback: bool,
    retry_primary_first: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "category": category,
        "source": source,
        "status_code": status,
        "exception_type": type(exc).__name__ if isinstance(exc, BaseException) else None,
        "message": message,
        "fallback_policy": {
            "should_fallback": should_fallback,
            "retry_primary_first": retry_primary_first,
            "reason": reason or category,
        },
    }


def classify_llm_operator_error(exc: BaseException | str) -> dict[str, Any]:
    """Return a stable diagnostic that separates provider failures from Viola caps."""

    text = _diagnostic_text(exc)
    lowered = text.lower()
    status = _status_code(exc)
    if (
        (isinstance(exc, BaseException) and exc.__class__.__name__ == "ProviderUnavailableError")
        or "no llm provider available" in lowered
        or "provider 'all' unavailable" in lowered
    ):
        return _with_fallback_policy(
            category="provider_unavailable",
            source="llm_provider",
            status=status,
            exc=exc,
            message="No LLM provider is currently available for the active user context.",
            should_fallback=False,
            reason="no_available_provider",
        )
    if "insufficient_quota" in lowered or "you exceeded your current quota" in lowered:
        _note_provider_overload("provider_api_key_quota")
        return _with_fallback_policy(
            category="provider_api_key_quota",
            source="openai_api_key",
            status=status,
            exc=exc,
            message=(
                "OpenAI returned insufficient_quota for the configured API key. "
                "This is provider API-key quota/billing, not a Viola managed-spend cap "
                "and not a Codex CLI usage limit."
            ),
            should_fallback=False,
            reason="provider_quota_exhausted",
        )
    if "managed spend cap" in lowered or "plan_limiter" in lowered:
        return _with_fallback_policy(
            category="viola_managed_spend_cap",
            source="viola_spend_cap",
            status=status,
            exc=exc,
            message="Viola's managed-LLM spend cap blocked the request.",
            should_fallback=False,
            reason="viola_policy_cap",
        )
    if "cost circuit breaker" in lowered or "monthly cost limit" in lowered or "per-minute llm call limit" in lowered:
        return _with_fallback_policy(
            category="viola_runtime_circuit_breaker",
            source="viola_runtime_safety",
            status=status,
            exc=exc,
            message="Viola's runtime cost circuit breaker blocked the request.",
            should_fallback=False,
            reason="viola_runtime_safety_cap",
        )
    if "codex" in lowered and ("usage limit" in lowered or "limit reached" in lowered):
        return _with_fallback_policy(
            category="codex_cli_usage_limit",
            source="codex_cli",
            status=status,
            exc=exc,
            message="The Codex CLI usage limit blocked a delegated Codex run.",
            should_fallback=False,
            reason="codex_cli_limit",
        )
    if status == 429:
        _note_provider_overload("provider_rate_limit")
        return _with_fallback_policy(
            category="provider_rate_limit",
            source="llm_provider",
            status=status,
            exc=exc,
            message=(
                "The LLM provider returned HTTP 429. Check provider rate limits or key pool state; "
                "this is distinct from Viola spend caps and Codex CLI limits."
            ),
            should_fallback=True,
            reason="provider_rate_limited",
        )
    if status in {500, 502, 503, 504, 529}:
        return _with_fallback_policy(
            category="provider_transient_error",
            source="llm_provider",
            status=status,
            exc=exc,
            message="The LLM provider returned a transient server error.",
            should_fallback=True,
            retry_primary_first=True,
            reason="provider_transient_server_error",
        )
    if status in {401, 402, 403, 404}:
        return _with_fallback_policy(
            category="provider_auth_or_config_error",
            source="llm_provider",
            status=status,
            exc=exc,
            message="The LLM provider rejected the configured key, billing state, or model.",
            should_fallback=False,
            reason="provider_auth_billing_or_model_config",
        )
    if status == 400 or any(
        marker in lowered
        for marker in (
            "bad request",
            "invalid request",
            "invalid_request_error",
            "invalid schema",
            "tool schema",
            "function schema",
        )
    ):
        return _with_fallback_policy(
            category="provider_bad_request",
            source="llm_provider",
            status=status,
            exc=exc,
            message="The LLM provider rejected the request shape; fallback is disabled for bad requests.",
            should_fallback=False,
            reason="bad_request_or_tool_schema",
        )

    transient_markers = (
        "timeout",
        "timed out",
        "connection error",
        "connection reset",
        "connection refused",
        "temporary failure",
        "service unavailable",
    )
    if any(marker in lowered for marker in transient_markers):
        return _with_fallback_policy(
            category="provider_connection_error",
            source="llm_provider",
            status=status,
            exc=exc,
            message="The LLM provider connection failed or timed out.",
            should_fallback=True,
            retry_primary_first=True,
            reason="provider_connection_or_timeout",
        )
    return _with_fallback_policy(
        category="llm_provider_error",
        source="llm_provider",
        status=status,
        exc=exc,
        message="The LLM provider failed; inspect the exception type and provider request trace.",
        should_fallback=False,
        reason="unclassified_provider_error",
    )


def should_fallback_for_operator_diagnostic(diagnostic: dict[str, Any] | None) -> bool:
    """Return True when a classified LLM failure should try another provider."""

    if not isinstance(diagnostic, dict):
        return False
    policy = diagnostic.get("fallback_policy")
    if isinstance(policy, dict) and isinstance(policy.get("should_fallback"), bool):
        return bool(policy["should_fallback"])
    return diagnostic.get("category") in {
        "provider_api_key_quota",
        "provider_rate_limit",
        "provider_transient_error",
        "provider_connection_error",
        "provider_auth_or_config_error",
    }


def user_message_for_operator_diagnostic(diagnostic: dict[str, Any]) -> str | None:
    """Return a concise user-visible message for user-actionable LLM failures.

    Every category emitted by ``classify_llm_operator_error`` MUST have an
    entry here. A missing case silently falls through to None, which lets
    the agent-loop bail message ("I stopped before I could finish") mask the
    real cause from the user. The ``test-agent-loop-quota-error-surfaces-
    user-message`` gate asserts coverage of every category.
    """

    category = diagnostic.get("category")
    if category == "provider_api_key_quota":
        return (
            "I'm having trouble reaching my AI provider right now. "
            "The team has been notified - please try again in a moment."
        )
    if category == "provider_unavailable":
        return "My AI service isn't available right now - check your settings or try again in a sec."
    if category == "provider_rate_limit":
        return (
            "My AI provider is rate-limiting requests right now. "
            "Try again in a moment, or switch your AI source in Settings if this keeps happening."
        )
    if category == "viola_managed_spend_cap":
        return (
            "Your managed-AI usage hit Viola's spend cap. "
            "Wait for the next cap window, or switch to BYOK in Settings."
        )
    if category == "viola_runtime_circuit_breaker":
        return "Viola's safety brake stopped the AI call to keep costs in check. " "Wait a minute and try again."
    if category == "codex_cli_usage_limit":
        return (
            "Your ChatGPT Plus usage limit blocked the Codex AI source. "
            "Switch to managed or BYOK in Settings, or wait for the limit to reset."
        )
    if category == "provider_transient_error":
        return "My AI provider had a transient hiccup. Please try the request again."
    if category == "provider_auth_or_config_error":
        return (
            "My AI provider rejected the configured key or model. "
            "Check your AI source in Settings - the key may be expired or revoked."
        )
    if category == "provider_bad_request":
        return (
            "I couldn't shape the request my AI provider expected. "
            "If this happens for the same prompt twice, please share it with us."
        )
    if category == "provider_connection_error":
        return "I couldn't reach my AI provider just now. Check your connection and try again."
    return None


__all__ = [
    "classify_llm_operator_error",
    "should_fallback_for_operator_diagnostic",
    "user_message_for_operator_diagnostic",
]
