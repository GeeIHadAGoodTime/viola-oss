"""Canonical product model for NOVVIOLA.

This module separates four concerns that were previously conflated:

1. App surface: where the customer uses Viola (desktop vs cloud)
2. Commercial plan: which paid/free tier the customer is on
3. Billing interval: monthly vs annual pricing cadence
4. AI source: who pays for LLM execution and where it runs

Internal operator/admin concerns must not be encoded as customer plan or
surface values.
"""

from __future__ import annotations

from enum import Enum


class AppSurface(str, Enum):
    """Customer-facing surface where Viola runs."""

    DESKTOP = "desktop"
    CLOUD = "cloud"


class BillingInterval(str, Enum):
    """Commercial billing cadence for paid plans."""

    MONTH = "month"
    YEAR = "year"


class PlanFamily(str, Enum):
    """Customer commercial tier."""

    FREE = "free"
    PRO = "pro"
    MAX = "max"


class PlanId(str, Enum):
    """Exact commercial plan identifiers used across billing and auth."""

    FREE = "free"
    PRO_MONTHLY = "pro_monthly"
    PRO_ANNUAL = "pro_annual"
    MAX_MONTHLY = "max_monthly"
    MAX_ANNUAL = "max_annual"


class AiSource(str, Enum):
    """Who pays for LLM execution and where it runs."""

    MANAGED = "managed"
    BYOK = "byok"
    CODEX = "codex"
    LOCAL = "local"


_PLAN_FAMILY_BY_ID: dict[PlanId, PlanFamily] = {
    PlanId.FREE: PlanFamily.FREE,
    PlanId.PRO_MONTHLY: PlanFamily.PRO,
    PlanId.PRO_ANNUAL: PlanFamily.PRO,
    PlanId.MAX_MONTHLY: PlanFamily.MAX,
    PlanId.MAX_ANNUAL: PlanFamily.MAX,
}

_PLAN_INTERVAL_BY_ID: dict[PlanId, BillingInterval | None] = {
    PlanId.FREE: None,
    PlanId.PRO_MONTHLY: BillingInterval.MONTH,
    PlanId.PRO_ANNUAL: BillingInterval.YEAR,
    PlanId.MAX_MONTHLY: BillingInterval.MONTH,
    PlanId.MAX_ANNUAL: BillingInterval.YEAR,
}


def plan_family_for_id(plan_id: PlanId | str | None) -> PlanFamily:
    """Return the commercial family for a plan id."""
    if not plan_id:
        return PlanFamily.FREE
    return _PLAN_FAMILY_BY_ID[PlanId(plan_id)]


def billing_interval_for_plan(plan_id: PlanId | str | None) -> BillingInterval | None:
    """Return the billing interval for a plan id."""
    if not plan_id:
        return None
    return _PLAN_INTERVAL_BY_ID[PlanId(plan_id)]


def is_paid_plan(plan_id: PlanId | str | None) -> bool:
    """Return True when the plan is Pro or Max."""
    return plan_family_for_id(plan_id) is not PlanFamily.FREE


def ai_source_uses_viola_managed_llm(ai_source: AiSource | str | None) -> bool:
    """Return True when LLM cost should count against Viola-managed spend."""
    if not ai_source:
        return True
    normalized = str(ai_source).strip().lower()
    if normalized == "subscription":
        return True
    return AiSource(ai_source) is AiSource.MANAGED


def coerce_plan_id(plan_id: PlanId | str | None, default: PlanId = PlanId.FREE) -> PlanId:
    """Normalize an arbitrary plan identifier to a canonical plan id."""
    if isinstance(plan_id, PlanId):
        return plan_id
    if not plan_id:
        return default
    try:
        return PlanId(str(plan_id).strip().lower())
    except ValueError:
        return default


def cloud_llm_consent_default_for_plan(plan_id: PlanId | str | None) -> bool:
    """Return the managed cloud-LLM consent default for a commercial plan."""
    return is_paid_plan(coerce_plan_id(plan_id))


def coerce_app_surface(surface: AppSurface | str | None, default: AppSurface = AppSurface.DESKTOP) -> AppSurface:
    """Normalize an arbitrary surface value to a canonical app surface."""
    if isinstance(surface, AppSurface):
        return surface
    if not surface:
        return default
    try:
        return AppSurface(str(surface).strip().lower())
    except ValueError:
        return default


def coerce_ai_source(ai_source: AiSource | str | None, default: AiSource = AiSource.MANAGED) -> AiSource:
    """Normalize an arbitrary AI source to a canonical source."""
    if isinstance(ai_source, AiSource):
        return ai_source
    if not ai_source:
        return default
    try:
        return AiSource(str(ai_source).strip().lower())
    except ValueError:
        return default
