"""Public managed-AI budget boundary.

Shared product code owns the source selection and response contracts used by
local, BYOK, and hosted execution. Company billing and operator controls are
loaded only after the request is known to use Viola-managed AI. This keeps the
standalone paths usable without weakening the hosted spend boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Canonical route surfaced by managed-cap denials and the private checkout
# implementation. Keeping it at the shared contract boundary prevents the UI
# and private service from handing users different destinations (#4215).
EXTRA_USAGE_PURCHASE_PATH = "/billing/extra-usage/checkout"


@dataclass(frozen=True, slots=True)
class ManagedLlmBudgetGate:
    """Provider-neutral result returned by managed spend enforcement."""

    allowed: bool
    spent_cents: int = 0
    budget_cents: int = 0
    plan: str = ""
    period: str = ""
    resets_at: str = ""
    extra_usage_cents: int = 0
    retry_after_seconds: int = 0
    reason: str = ""
    public_message: str = ""
    reservation: Any | None = None

    @property
    def cap_state(self) -> dict[str, Any]:
        if self.allowed or not self.period:
            return {}
        return {
            "plan": self.plan,
            "period": self.period,
            "spent_cents": self.spent_cents,
            "limit_cents": self.budget_cents,
            "extra_usage_cents": self.extra_usage_cents,
            "resets_at": self.resets_at,
            "retry_after_seconds": self.retry_after_seconds,
            "rate_limit_headers": managed_llm_budget_rate_limit_headers(self),
            "purchase_url": EXTRA_USAGE_PURCHASE_PATH,
            "byok_setup_url": "/account/byok",
        }


def cap_state_from_response(payload: object) -> dict[str, Any]:
    """Return a cap denial's state from either supported response envelope."""
    if not isinstance(payload, dict):
        return {}
    for candidate in (payload, payload.get("data")):
        if not isinstance(candidate, dict):
            continue
        cap_state = candidate.get("cap_state")
        if isinstance(cap_state, dict) and cap_state:
            return dict(cap_state)
    return {}


def retry_after_seconds_for_reset(resets_at: str, *, now: datetime | None = None) -> int:
    """Return seconds until a managed-AI cap resets, or zero when unknown."""
    reset_text = str(resets_at or "").strip()
    if not reset_text:
        return 0
    try:
        parsed = datetime.fromisoformat(reset_text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    now_dt = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    return max(0, math.ceil((parsed.astimezone(UTC) - now_dt).total_seconds()))


def managed_llm_budget_rate_limit_headers(gate: ManagedLlmBudgetGate) -> dict[str, str]:
    """Return HTTP-style reset headers for a managed-AI cap denial."""
    if gate.allowed or gate.retry_after_seconds <= 0:
        return {}
    headers = {
        "Retry-After": str(gate.retry_after_seconds),
        "X-Viola-RateLimit-Period": str(gate.period or ""),
    }
    if gate.resets_at:
        headers["X-Viola-RateLimit-Reset"] = gate.resets_at
    return headers


def managed_llm_budget_message(gate: ManagedLlmBudgetGate) -> str:
    """Return user-facing copy for a managed-AI budget denial."""
    if gate.public_message:
        return gate.public_message
    period = str(gate.period or "current").strip().lower()
    plan = str(gate.plan or "your").strip().replace("_", " ").title()
    plan_label = "%s plan" % plan if plan != "Your" else "your plan"
    message = (
        "You've reached your %s managed AI limit for the %s. Upgrade or top up with extra usage to continue."
    ) % (period, plan_label)
    if gate.resets_at and gate.retry_after_seconds > 0:
        message += " Retry after %s." % gate.resets_at
    return message


def per_command_spend_cap_message() -> str:
    """Return copy for a single command halted by its managed spend bound."""
    return (
        "I stopped here because this one request was about to use too much of your AI "
        "allowance at once. Your overall balance is still fine, this was a safety limit on "
        "a single command. Try a simpler or more specific request."
    )


def app_surface_is_cloud() -> bool:
    """Return whether the current deployment surface is cloud."""
    try:
        from config.settings import settings as app_settings
    except (AttributeError, ImportError, RuntimeError):
        return False
    return str(getattr(app_settings, "app_surface", "desktop")).strip().lower() == "cloud"


def user_uses_managed_llm(user_id: str | None) -> bool:
    """Resolve whether this user's selected AI source spends Viola funds.

    Resolution fails closed to managed so a broken or missing setting cannot
    silently bypass company spend controls.
    """
    try:
        from config.defaults import DEFAULT_AI_SOURCE
        from core.product import ai_source_uses_viola_managed_llm
        from ui.settings_manager import get_settings_manager

        ai_source: object = DEFAULT_AI_SOURCE
        if user_id:
            ai_source = get_settings_manager().get("ai_source", DEFAULT_AI_SOURCE, user_id=user_id)
        return ai_source_uses_viola_managed_llm(ai_source)
    except (AttributeError, ImportError, KeyError, RuntimeError, ValueError) as exc:
        logger.debug("Managed-LLM source lookup failed for user %s: %s", user_id, exc)
        return True


def check_managed_llm_spend_cap(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    estimated_cents: int = 1,
) -> ManagedLlmBudgetGate:
    """Check the managed allowance without loading billing for local/BYOK."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed:
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import check_managed_llm_spend_cap as _check_managed

    return _check_managed(user_id, managed_llm=True, estimated_cents=estimated_cents)


async def check_managed_llm_spend_cap_async(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    estimated_cents: int = 1,
) -> ManagedLlmBudgetGate:
    """Async managed-allowance check with a private-service-free bypass."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed:
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import check_managed_llm_spend_cap_async as _check_managed

    return await _check_managed(user_id, managed_llm=True, estimated_cents=estimated_cents)


async def reserve_managed_llm_spend_cap_async(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    estimated_cents: int = 1,
) -> ManagedLlmBudgetGate:
    """Reserve managed spend without loading billing for local/BYOK."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed:
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import reserve_managed_llm_spend_cap_async as _reserve_managed

    return await _reserve_managed(user_id, managed_llm=True, estimated_cents=estimated_cents)


def build_per_command_spend_guard(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
) -> Any | None:
    """Build the private managed guard only when Viola funds the request."""
    if not user_id:
        return None
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed:
        return None

    from billing.per_command_spend_guard import build_per_command_spend_guard as _build_managed

    return _build_managed(user_id, managed_llm=True)


__all__ = [
    "EXTRA_USAGE_PURCHASE_PATH",
    "ManagedLlmBudgetGate",
    "app_surface_is_cloud",
    "build_per_command_spend_guard",
    "cap_state_from_response",
    "check_managed_llm_spend_cap",
    "check_managed_llm_spend_cap_async",
    "managed_llm_budget_message",
    "managed_llm_budget_rate_limit_headers",
    "per_command_spend_cap_message",
    "reserve_managed_llm_spend_cap_async",
    "retry_after_seconds_for_reset",
    "user_uses_managed_llm",
]
