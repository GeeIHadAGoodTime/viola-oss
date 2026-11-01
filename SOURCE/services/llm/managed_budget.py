"""Public managed-AI budget boundary.

Shared product code owns the source selection and response contracts used by
local, BYOK, and hosted execution. Company billing and operator controls are
loaded only after the request is known to use Viola-managed AI. This keeps the
standalone paths usable without weakening the hosted spend boundary.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from core.exceptions import LLMQuotaExceededError
from core.logging_config import get_logger

logger = get_logger(__name__)

# Canonical route surfaced by managed-cap denials and the private checkout
# implementation. Keeping it at the shared contract boundary prevents the UI
# and private service from handing users different destinations (#4215).
EXTRA_USAGE_PURCHASE_PATH = "/billing/extra-usage/checkout"
CAPACITY_MANAGEMENT_PATH = "/billing/capacity"


def public_managed_cap_state(value: object) -> dict[str, Any]:
    """Project a denial without exposing the company's provider-cost ledger."""
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ("plan", "period", "resets_at", "denial_code",
                                          "complimentary_plan", "complimentary_ends_at")
              if isinstance(value.get(key), str)}
    diagnostic_id = value.get("diagnostic_id")
    if isinstance(diagnostic_id, str):
        try:
            result["diagnostic_id"] = str(uuid.UUID(diagnostic_id))
        except ValueError:
            pass
    for key in ("usage_percent", "percent_used", "retry_after_seconds", "complimentary_months"):
        number = value.get(key)
        if type(number) in {int, float} and math.isfinite(number) and number >= 0:
            result[key] = number
    headers = value.get("rate_limit_headers")
    if isinstance(headers, dict):
        result["rate_limit_headers"] = {key: headers[key] for key in
            ("Retry-After", "X-Viola-RateLimit-Period", "X-Viola-RateLimit-Reset")
            if isinstance(headers.get(key), str)}
    result["purchase_url"] = CAPACITY_MANAGEMENT_PATH
    result["capacity_management_url"] = CAPACITY_MANAGEMENT_PATH
    result["byok_setup_url"] = "/account/byok"
    return result


class ManagedLlmSpendCapError(LLMQuotaExceededError):
    """A terminal cloud allowance denial carrying its safe customer state."""

    def __init__(self, public_message: str, cap_state: dict[str, Any]) -> None:
        state = public_managed_cap_state(cap_state)
        message = public_message or "You’ve used your included managed usage. Add more to continue."
        super().__init__(user_id="<managed>", limit_type="managed_llm_spend_cap", current=0, limit=0,
                         reset_at=state.get("resets_at", ""), cap_state=state, public_message=message)


def provider_managed_spend_accounted_remotely(provider: object) -> bool:
    """Only a selected remote-accounting transport on desktop delegates billing.

    This is provider capability metadata, never a request/body setting or an
    inference from a plan, account flag, API key absence, or desktop alone.
    """
    if provider is None:
        return False
    try:
        from config.settings import settings as app_settings

        if str(getattr(app_settings, "app_surface", "")).strip().lower() != "desktop":
            return False
        return getattr(provider, "MANAGED_SPEND_ACCOUNTED_REMOTELY", False) is True
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        return False


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
    public_cap_state: dict[str, Any] | None = None
    estimated_cents: int = 0
    # Set only when the active complimentary (gifted) plan ends with the
    # account back on Free; see complimentary_ending_sentence.
    complimentary_plan: str = ""
    complimentary_ends_at: str = ""
    complimentary_months: int = 0

    @property
    def denial_code(self) -> str:
        if self.reason == "authenticated managed-LLM user required":
            return "authenticated_user_required"
        if self.reason in {"budget counter unavailable", "account capacity unavailable"}:
            return "managed_llm_budget_unavailable"
        if self.reason.startswith("owner safety control"):
            return "owner_safety_control_disabled"
        return "managed_llm_spend_cap"

    @property
    def cap_state(self) -> dict[str, Any]:
        if self.public_cap_state is not None:
            return public_managed_cap_state(self.public_cap_state)
        if self.allowed:
            return {}
        state = {
            "plan": self.plan,
            "denial_code": self.denial_code,
            "period": self.period,
            "resets_at": self.resets_at,
            "retry_after_seconds": self.retry_after_seconds,
            "rate_limit_headers": managed_llm_budget_rate_limit_headers(self),
            "purchase_url": EXTRA_USAGE_PURCHASE_PATH,
            "byok_setup_url": "/account/byok",
        }
        if self.budget_cents > 0:
            state["usage_percent"] = min(100, max(0, 100 * self.spent_cents / self.budget_cents))
        if self.complimentary_ends_at:
            state["complimentary_plan"] = self.complimentary_plan
            state["complimentary_ends_at"] = self.complimentary_ends_at
            state["complimentary_months"] = self.complimentary_months
        return public_managed_cap_state(state)


def _public_budget_gate(gate: Any) -> ManagedLlmBudgetGate:
    """Retain private enforcement facts while projecting safe response state."""
    return ManagedLlmBudgetGate(
        allowed=gate.allowed, spent_cents=gate.spent_cents, budget_cents=gate.budget_cents,
        plan=gate.plan, period=gate.period, resets_at=gate.resets_at,
        extra_usage_cents=gate.extra_usage_cents, retry_after_seconds=gate.retry_after_seconds,
        reason=gate.reason, public_message=gate.public_message, reservation=gate.reservation,
        estimated_cents=getattr(gate, "estimated_cents", 0),
        complimentary_plan=str(getattr(gate, "complimentary_plan", "") or ""),
        complimentary_ends_at=str(getattr(gate, "complimentary_ends_at", "") or ""),
        complimentary_months=int(getattr(gate, "complimentary_months", 0) or 0),
    )


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
    if gate.denial_code == "authenticated_user_required":
        return "Sign in to use managed AI."
    if gate.denial_code != "managed_llm_spend_cap" or gate.period not in {"weekly", "monthly"} or not gate.plan:
        return "I can't verify your managed usage capacity right now. Please try again shortly."
    # In the last 35 minutes of a complimentary month checkout is refused, so
    # "Add more usage" would send the user to a button that cannot work.
    checkout_closed = complimentary_checkout_closed(gate)
    if gate.budget_cents > gate.spent_cents:
        if checkout_closed:
            return "This request is larger than your remaining managed usage capacity. Try a shorter request. %s" % (
                complimentary_billing_sentence(gate, ending_shown=False)
            )
        return (
            "This request is larger than your remaining managed usage capacity. "
            "Try a shorter request or choose Add more usage in Billing."
        )
    period = str(gate.period or "current").strip().lower()
    plan = str(gate.plan or "your").strip().replace("_", " ").title()
    plan_label = "%s plan" % plan if plan != "Your" else "your plan"
    if checkout_closed:
        message = "You've reached your %s managed AI limit for the %s." % (period, plan_label)
    else:
        message = (
            "You've reached your %s managed AI limit for the %s. Choose Add more usage in Billing to continue."
        ) % (period, plan_label)
    ending = complimentary_ending_sentence(gate)
    if ending:
        message += " " + ending
    elif gate.resets_at and gate.retry_after_seconds > 0:
        when = friendly_reset_time(gate.resets_at)
        if when:
            message += " Your usage resets %s." % when
    if checkout_closed:
        message += " " + complimentary_billing_sentence(gate, ending_shown=bool(ending))
    return message


# A gift that ends this close after the usage reset (or any time before it)
# means the reset does not bring the gifted plan back: the account is on Free.
# On the 1.0.5 laptop the reset was 15:07:01 and the gift ended 15:45:51 the
# same day, so "Your usage resets on November 23" promised a Pro month that
# never came.
COMPLIMENTARY_RESET_COINCIDENCE = timedelta(days=1)


def complimentary_ending_sentence(gate: Any, *, utc: bool = False) -> str | None:
    """Say that complimentary access ends and Free follows, when that is what the reset means.

    ``gate.complimentary_ends_at`` is filled by the server only when the
    active gift (plus any gift queued right after it) ends with the account on
    the Free plan. Returns None when that is unknown, unreadable, or the gift
    outlasts the next reset by more than ``COMPLIMENTARY_RESET_COINCIDENCE``
    (then the plain reset sentence is the honest one).
    """
    ends_text = str(getattr(gate, "complimentary_ends_at", "") or "").strip()
    if not ends_text:
        return None
    ends = _parse_utc(ends_text)
    if ends is None:
        return None
    resets = _parse_utc(getattr(gate, "resets_at", ""))
    if resets is not None and ends > resets + COMPLIMENTARY_RESET_COINCIDENCE:
        return None
    when = friendly_reset_time(ends_text, utc=utc)
    if not when:
        return None
    plan = str(getattr(gate, "complimentary_plan", "") or "").strip().replace("_", " ").title() or "Pro"
    span = "month" if int(getattr(gate, "complimentary_months", 0) or 0) <= 1 else "access"
    zone = " (UTC)" if utc else ""
    return "Your complimentary %s %s ends %s%s. After that you're on the Free plan." % (plan, span, when, zone)


# Checkout started during a complimentary month charges nothing until the gift
# ends. Inside the last 48 hours it anchors the first charge at the gift end
# instead of using a trial, which needs the session to close before the gift
# ends; Stripe sessions stay open at least 30 minutes. So only while less than
# this is left does the server refuse it with "gift_checkout_window" (private
# billing/capacity_service.py, GIFT_CHECKOUT_MIN_LEAD_SECONDS = 35 minutes).
# Usage-limit copy must not point at "Add more usage" during that window.
COMPLIMENTARY_CHECKOUT_MIN_REMAINING = timedelta(minutes=35)


def _utcnow() -> datetime:
    """The current time; one seam so copy tests can pin the clock."""
    return datetime.now(UTC)


def complimentary_checkout_closed(gate: Any, *, now: datetime | None = None) -> bool:
    """True when the gift on this denial ends too soon for checkout to start.

    Only a gift that ends with the account on Free carries
    ``complimentary_ends_at``, and that is exactly the case where "Add more
    usage" is a new checkout. Unknown or unreadable means False, so the usual
    advice stays.
    """
    ends = _parse_utc(getattr(gate, "complimentary_ends_at", ""))
    if ends is None:
        return False
    moment = now or _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return ends < moment + COMPLIMENTARY_CHECKOUT_MIN_REMAINING


def complimentary_billing_sentence(gate: Any, *, ending_shown: bool, utc: bool = False) -> str:
    """The Billing advice to give while checkout is closed: wait for the gift to end."""
    if ending_shown:
        return "You can add more usage in Billing once it ends."
    plan = str(getattr(gate, "complimentary_plan", "") or "").strip().replace("_", " ").title() or "Pro"
    span = "month" if int(getattr(gate, "complimentary_months", 0) or 0) <= 1 else "access"
    when = friendly_reset_time(getattr(gate, "complimentary_ends_at", ""), utc=utc)
    if not when:
        return "You can add more usage in Billing after your complimentary %s %s ends." % (plan, span)
    zone = " (UTC)" if utc else ""
    return "You can add more usage in Billing after your complimentary %s %s ends %s%s." % (plan, span, when, zone)


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def friendly_reset_time(resets_at: object, *, utc: bool = False) -> str | None:
    """Say when usage resets the way a person would: "on Monday, November 23 at 9:07 AM".

    The cap message used to print the raw ISO stamp ("Retry after
    2026-11-23T15:07:01.319869Z.", 1.0.5 build-7 laptop report). The desktop
    shows this computer's local time; ``utc=True`` (a server that does not
    know the user's time zone) names the day only. None when unreadable, so
    the caller leaves the sentence out rather than printing a raw value.
    """
    text = str(resets_at or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if utc:
        moment = moment.astimezone(UTC)
        return "on %s, %s %d" % (_WEEKDAYS[moment.weekday()], _MONTHS[moment.month - 1], moment.day)
    moment = moment.astimezone()
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return "on %s, %s %d at %d:%02d %s" % (
        _WEEKDAYS[moment.weekday()],
        _MONTHS[moment.month - 1],
        moment.day,
        hour,
        moment.minute,
        meridiem,
    )


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
        from config.settings import settings as app_settings
        from core.product import ai_source_uses_viola_managed_llm
        from ui.settings_manager import get_settings_manager

        ai_source_override = (getattr(app_settings, "ai_source_override", "") or "").strip()
        ai_source: object = ai_source_override or DEFAULT_AI_SOURCE
        if user_id and not ai_source_override:
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
    provider: object = None,
) -> ManagedLlmBudgetGate:
    """Check the managed allowance without loading billing for local/BYOK."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed or provider_managed_spend_accounted_remotely(provider):
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import check_managed_llm_spend_cap as _check_managed

    return _public_budget_gate(_check_managed(user_id, managed_llm=True, estimated_cents=estimated_cents))


async def check_managed_llm_spend_cap_async(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    estimated_cents: int = 1,
    provider: object = None,
) -> ManagedLlmBudgetGate:
    """Async managed-allowance check with a private-service-free bypass."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed or provider_managed_spend_accounted_remotely(provider):
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import check_managed_llm_spend_cap_async as _check_managed

    return _public_budget_gate(await _check_managed(user_id, managed_llm=True, estimated_cents=estimated_cents))


async def reserve_managed_llm_spend_cap_async(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    estimated_cents: int = 1,
    provider: object = None,
) -> ManagedLlmBudgetGate:
    """Reserve managed spend without loading billing for local/BYOK."""
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed or provider_managed_spend_accounted_remotely(provider):
        return ManagedLlmBudgetGate(allowed=True)

    from billing.managed_llm_budget import reserve_managed_llm_spend_cap_async as _reserve_managed

    return _public_budget_gate(await _reserve_managed(user_id, managed_llm=True, estimated_cents=estimated_cents))


def build_per_command_spend_guard(
    user_id: str | None,
    *,
    managed_llm: bool | None = None,
    provider: object = None,
) -> Any | None:
    """Build the private managed guard only when Viola funds the request."""
    if not user_id:
        return None
    uses_managed = user_uses_managed_llm(user_id) if managed_llm is None else bool(managed_llm)
    if not uses_managed:
        return None

    from billing.per_command_spend_guard import build_per_command_spend_guard as _build_managed

    if provider_managed_spend_accounted_remotely(provider):
        return _build_managed(user_id, managed_llm=True, remotely_accounted=True)
    return _build_managed(user_id, managed_llm=True)


__all__ = [
    "CAPACITY_MANAGEMENT_PATH",
    "EXTRA_USAGE_PURCHASE_PATH",
    "ManagedLlmBudgetGate",
    "ManagedLlmSpendCapError",
    "app_surface_is_cloud",
    "build_per_command_spend_guard",
    "cap_state_from_response",
    "check_managed_llm_spend_cap",
    "check_managed_llm_spend_cap_async",
    "complimentary_ending_sentence",
    "managed_llm_budget_message",
    "managed_llm_budget_rate_limit_headers",
    "per_command_spend_cap_message",
    "provider_managed_spend_accounted_remotely",
    "public_managed_cap_state",
    "reserve_managed_llm_spend_cap_async",
    "retry_after_seconds_for_reset",
    "user_uses_managed_llm",
]
