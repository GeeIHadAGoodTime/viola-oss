"""Shared, store-independent facts for cloud-authoritative month gifts."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

from core.product import PlanFamily, PlanId, plan_family_for_id


class SubscriptionFacts(BaseModel):
    """Public entitlement facts; provider records remain owned by their backend."""

    plan_id: PlanId
    plan_family: PlanFamily
    status: Literal["free", "active", "canceled", "past_due", "trialing"]
    has_paid_access: StrictBool
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    payment_provider: str | None = None
    subscription_source: str = "commercial"
    activation_pending: StrictBool = False
    canceled_at: datetime | None = None
    cancel_at_period_end: StrictBool = False
    monthly_price_cents: int | None = Field(default=None, ge=0)
    capacity_multiplier: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    funded_paid_through_at: datetime | None = None
    usage_anchor_at: datetime | None = None
    provider_renews_at: datetime | None = None
    provider_deferred_renews_at: datetime | None = None

    @model_validator(mode="after")
    def matching_plan_family(self) -> SubscriptionFacts:
        if self.plan_family != plan_family_for_id(self.plan_id):
            raise ValueError("Entitlement plan family does not match its plan")
        return self


class GiftWindowFacts(BaseModel):
    """A confirmed calendar-month gift supplied by the authoritative server."""

    window_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    selected_level: Literal["pro", "max", "flexible"]
    monthly_price_cents: int = Field(gt=0)
    capacity_multiplier: float = Field(gt=0, allow_inf_nan=False)
    start_at: datetime
    end_at: datetime
    calendar_anchor_at: datetime
    duration_unit: Literal["month"] = "month"
    duration_months: int = Field(default=1, gt=0)
    state: Literal["confirmed"] = "confirmed"

    @field_validator("start_at", "end_at", "calendar_anchor_at")
    @classmethod
    def aware_window_dates(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Gift window dates must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered_window(self) -> GiftWindowFacts:
        if self.end_at <= self.start_at:
            raise ValueError("Gift window end must follow its start")
        anchor = self.calendar_anchor_at
        offset = (self.start_at.year - anchor.year) * 12 + self.start_at.month - anchor.month

        def anchored_month(months: int) -> datetime:
            year, month = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
            month += 1
            return anchor.replace(year=year, month=month, day=min(anchor.day, monthrange(year, month)[1]))

        if (
            offset < 0
            or self.start_at != anchored_month(offset)
            or self.end_at != anchored_month(offset + self.duration_months)
        ):
            raise ValueError("Gift dates must follow the anchored calendar-month duration")
        return self

    def is_active(self, at: datetime | None = None) -> bool:
        instant = at or datetime.now(UTC)
        return self.start_at <= instant < self.end_at


class GiftReturnFacts(BaseModel):
    """Current server answer about the base plan after complimentary access."""

    plan_id: PlanId
    has_paid_access: StrictBool
    base_canceled: StrictBool = False
    gift_survives_base_cancellation: StrictBool = True
    cancellation_outcome: str | None = None
    resumes_at: datetime | None = None


class EntitlementProjection(BaseModel):
    """One identity's effective access alongside its untouched commercial facts."""

    user_id: str = Field(min_length=1)
    effective: SubscriptionFacts
    base_subscription: SubscriptionFacts
    active_gift: GiftWindowFacts | None = None
    queued_gifts: list[GiftWindowFacts] = Field(default_factory=list)
    return_after_gift: GiftReturnFacts | None = None

    @model_validator(mode="after")
    def identity_bound_windows(self) -> EntitlementProjection:
        windows = ([self.active_gift] if self.active_gift else []) + self.queued_gifts
        if any(window.user_id != self.user_id for window in windows):
            raise ValueError("Gift window belongs to another account")
        if self.effective.subscription_source == "owner_month_gift" and self.active_gift is None:
            raise ValueError("Effective gift access requires a confirmed gift window")
        if self.effective.subscription_source == "owner_month_gift" and self.active_gift is not None:
            if (
                self.effective.current_period_start != self.active_gift.start_at
                or self.effective.current_period_end != self.active_gift.end_at
            ):
                raise ValueError("Effective gift access must use its calendar-month window")
        return self


class GiftEntitlementResolver(Protocol):
    """Optional injected server resolver; shared code imports no private store."""

    async def __call__(
        self,
        user_id: str,
        base_snapshot: dict[str, Any],
        *,
        at: datetime | None = None,
        conn: Any | None = None,
    ) -> EntitlementProjection | dict[str, Any]: ...


class EntitlementResolverSupport:
    """Registration seam shared with privately supplied cloud DB adapters."""

    _entitlement_resolver: GiftEntitlementResolver | None = None

    def set_entitlement_resolver(self, resolver: GiftEntitlementResolver | None) -> None:
        self._entitlement_resolver = resolver

    async def resolve_entitlement_projection(
        self,
        user_id: str,
        base_snapshot: dict[str, Any],
        *,
        at: datetime | None = None,
        conn: Any | None = None,
    ) -> EntitlementProjection | None:
        resolver = self._entitlement_resolver
        if resolver is None:
            return None
        value = await resolver(user_id, base_snapshot, at=at, conn=conn)
        projection = EntitlementProjection.model_validate(value)
        if projection.user_id != user_id:
            raise ValueError("Entitlement projection belongs to another account")
        return projection
