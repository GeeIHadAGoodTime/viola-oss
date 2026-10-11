"""Shared gift projections never rewrite commercial state or trust queued access."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, timedelta
import sys
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from auth.entitlement_projection import EntitlementProjection, EntitlementResolverSupport, GiftWindowFacts
from auth.models import (
    SubscriptionStatus,
    User,
    apply_user_entitlement_projection,
    billing_state_has_paid_access,
    current_entitlement_projection,
)
from core.product import PlanId


def projection(now: datetime, *, base_canceled: bool = False) -> dict:
    start = now - timedelta(days=1)
    following = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
    end = following.replace(day=min(start.day, monthrange(following.year, following.month)[1]))
    base = {
        "plan_id": "pro_annual",
        "plan_family": "pro",
        "status": "canceled" if base_canceled else "active",
        "has_paid_access": not base_canceled,
        "current_period_end": (start if base_canceled else end + timedelta(days=90)),
        "payment_provider": "stripe",
        "subscription_source": "commercial",
        "funded_paid_through_at": start if base_canceled else end + timedelta(days=90),
    }
    return {
        "user_id": "account-a",
        "base_subscription": base,
        "effective": {
            "plan_id": "max_monthly",
            "plan_family": "max",
            "status": "active",
            "has_paid_access": True,
            "subscription_source": "owner_month_gift",
            "current_period_start": start,
            "current_period_end": end,
            "monthly_price_cents": 2000,
            "capacity_multiplier": 2,
        },
        "active_gift": {
            "window_id": "confirmed-window",
            "user_id": "account-a",
            "selected_level": "max",
            "monthly_price_cents": 2000,
            "capacity_multiplier": 2,
            "start_at": start,
            "end_at": end,
            "calendar_anchor_at": start,
            "duration_unit": "month",
            "duration_months": 1,
            "state": "confirmed",
        },
        "queued_gifts": [],
    }


def user(now: datetime) -> User:
    return User(
        id="account-a",
        email="owner@example.com",
        email_verified=True,
        plan_id=PlanId.PRO_ANNUAL,
        subscription_status="active",
        current_period_end=now + timedelta(days=120),
        created_at=now,
        updated_at=now,
    )


class MonthGiftEntitlementTests(unittest.TestCase):
    def test_max_runtime_is_effective_while_original_paid_facts_are_untouched(self):
        now = datetime.now(UTC)
        original = user(now)
        payload = projection(now)
        applied = apply_user_entitlement_projection(original, payload, at=now)
        self.assertIs(applied.plan_id, PlanId.MAX_MONTHLY)
        self.assertTrue(applied.has_paid_access)
        self.assertIs(original.plan_id, PlanId.PRO_ANNUAL)
        self.assertIs(applied.entitlement_projection.base_subscription.plan_id, PlanId.PRO_ANNUAL)
        self.assertEqual(applied.entitlement_projection.base_subscription.payment_provider, "stripe")
        self.assertEqual(applied.current_period_end, payload["active_gift"]["end_at"])

    def test_exact_expiry_removes_only_gift_and_never_revives_canceled_base(self):
        for cancelled, expected in [(False, PlanId.PRO_ANNUAL), (True, PlanId.FREE)]:
            with self.subTest(cancelled=cancelled):
                now = datetime.now(UTC)
                value = EntitlementProjection.model_validate(projection(now, base_canceled=cancelled))
                result = current_entitlement_projection(value, at=value.active_gift.end_at)
                self.assertIsNone(result.active_gift)
                self.assertIs(result.effective.plan_id, expected)
                self.assertIs(result.effective.has_paid_access, not cancelled)
                self.assertEqual(result.base_subscription, value.base_subscription)

    def test_queued_confirmed_month_does_not_grant_access_before_start(self):
        now = datetime.now(UTC)
        payload = projection(now, base_canceled=True)
        gift = payload.pop("active_gift")
        gift["start_at"] = now + timedelta(days=1)
        gift["calendar_anchor_at"] = gift["start_at"]
        following = (gift["start_at"].replace(day=1) + timedelta(days=32)).replace(day=1)
        gift["end_at"] = following.replace(
            day=min(gift["start_at"].day, monthrange(following.year, following.month)[1])
        )
        payload["active_gift"] = None
        payload["queued_gifts"] = [gift]
        payload["effective"] = payload["base_subscription"]
        self.assertIs(apply_user_entitlement_projection(user(now), payload, at=now).plan_id, PlanId.FREE)

    def test_invalid_scope_or_non_month_confirmation_cannot_activate_gift(self):
        for fault in ["other_user", "annual_period", "pending_window", "wrong_unit"]:
            with self.subTest(fault=fault):
                payload = projection(datetime.now(UTC))
                if fault == "other_user":
                    payload["active_gift"]["user_id"] = "account-b"
                if fault == "annual_period":
                    payload["effective"]["current_period_end"] += timedelta(days=335)
                    payload["active_gift"]["end_at"] = payload["effective"]["current_period_end"]
                if fault == "pending_window":
                    payload["active_gift"]["state"] = "uncertain"
                if fault == "wrong_unit":
                    payload["active_gift"]["duration_unit"] = "year"
                with self.assertRaises(ValidationError):
                    EntitlementProjection.model_validate(payload)

    def test_future_paid_term_is_not_admitted_before_its_start(self):
        now = datetime.now(UTC)
        self.assertFalse(
            billing_state_has_paid_access(
                SubscriptionStatus.ACTIVE,
                PlanId.MAX_ANNUAL,
                current_period_start=now + timedelta(days=30),
                current_period_end=now + timedelta(days=395),
            )
        )

    def test_sqlite_user_readback_preserves_future_paid_start_for_runtime_admission(self):
        from auth.database import SQLiteUserRepository

        now = datetime.now(UTC)
        start = now + timedelta(days=30)
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE subscriptions (user_id, status, plan, current_period_start, current_period_end, activation_pending)"
            )
            connection.execute(
                "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "account-a",
                    "active",
                    "max_annual",
                    start.isoformat(),
                    (start + timedelta(days=365)).isoformat(),
                    0,
                ),
            )
            repository = SQLiteUserRepository(SimpleNamespace(connection=connection))
            result = repository._row_to_user(
                {
                    "id": "account-a",
                    "email": "account@example.com",
                    "email_verified": True,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            )
            self.assertEqual(result.current_period_start, start)
            self.assertFalse(result.has_paid_access)

    def test_explicit_authority_clock_resolves_future_fixture_without_duplicate_rules(self):
        for at, expected in [(datetime(2030, 1, 1, tzinfo=UTC), True), (datetime(2031, 1, 1, tzinfo=UTC), False)]:
            with self.subTest(at=at):
                self.assertIs(
                    billing_state_has_paid_access(
                        SubscriptionStatus.ACTIVE,
                        PlanId.MAX_ANNUAL,
                        current_period_start=datetime(2029, 1, 1, tzinfo=UTC),
                        current_period_end=datetime(2031, 1, 1, tzinfo=UTC),
                        at=at,
                    ),
                    expected,
                )

    def test_january_anchor_clips_february_then_returns_to_march31(self):
        anchor = datetime(2028, 1, 31, 15, tzinfo=UTC)
        window = GiftWindowFacts(
            window_id="extension",
            user_id="account-a",
            selected_level="flexible",
            monthly_price_cents=3000,
            capacity_multiplier=3,
            calendar_anchor_at=anchor,
            start_at=datetime(2028, 2, 29, 15, tzinfo=UTC),
            end_at=datetime(2028, 3, 31, 15, tzinfo=UTC),
        )
        self.assertTrue(window.is_active(datetime(2028, 3, 1, tzinfo=UTC)))
        self.assertFalse(window.is_active(window.end_at))

    def test_base_trial_pending_does_not_suppress_a_confirmed_funded_gift(self):
        now = datetime.now(UTC)
        payload = projection(now)
        payload["base_subscription"].update(
            status="trialing",
            has_paid_access=False,
            activation_pending=True,
            current_period_start=payload["active_gift"]["end_at"],
        )
        applied = apply_user_entitlement_projection(user(now), payload, at=now)
        self.assertTrue(applied.has_paid_access)
        self.assertIs(applied.plan_id, PlanId.MAX_MONTHLY)
        self.assertTrue(applied.entitlement_projection.base_subscription.activation_pending)

    def test_cloud_background_callback_selects_exact_max_or_fails_closed(self):
        from core import request_context as runtime

        for failure in [False, True]:
            with (
                self.subTest(failure=failure),
                patch.dict(
                    sys.modules,
                    {
                        "config.settings": SimpleNamespace(settings=SimpleNamespace(app_surface="cloud")),
                        "ui.settings_manager": SimpleNamespace(get_settings_manager=lambda: None),
                    },
                ),
                patch.object(runtime, "_resolve_sqlite_subscription_plan_id", return_value="pro_monthly"),
                patch.object(runtime, "logger"),
            ):
                calls = []

                def callback(user_id, base_plan):
                    calls.append((user_id, base_plan))
                    if failure:
                        raise OSError("synthetic gift read unavailable")
                    return "max_monthly"

                runtime.register_runtime_entitlement_resolver(callback)
                try:
                    self.assertEqual(runtime.resolve_runtime_plan_id("account-a"), "free" if failure else "max_monthly")
                    self.assertEqual(calls, [("account-a", "pro_monthly")])
                finally:
                    runtime.register_runtime_entitlement_resolver(None)

    def test_desktop_local_plan_does_not_call_cloud_gift_plugin(self):
        from core import request_context as runtime

        class Settings:
            def get(self, name, default=None, **kwargs):
                return None if name == "billing_plan_snapshot" else "pro_monthly"

        with patch.dict(
            sys.modules,
            {
                "config.settings": SimpleNamespace(settings=SimpleNamespace(app_surface="desktop")),
                "ui.settings_manager": SimpleNamespace(get_settings_manager=lambda: Settings()),
            },
        ):
            runtime.register_runtime_entitlement_resolver(
                lambda *args: self.fail("Cloud callback used for desktop/local")
            )
            try:
                self.assertEqual(runtime.resolve_runtime_plan_id("account-a"), "pro_monthly")
            finally:
                runtime.register_runtime_entitlement_resolver(None)


class MonthGiftResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_resolver_is_empty_by_default_and_binds_original_subject(self):
        adapter = EntitlementResolverSupport()
        original = {"plan_id": "pro_annual", "provider_renews_at": "2027-01-31T00:00:00Z"}
        self.assertIsNone(await adapter.resolve_entitlement_projection("account-a", original))
        calls = []

        async def resolver(user_id, base_snapshot, *, at=None, conn=None):
            calls.append((user_id, base_snapshot, at, conn))
            return projection(datetime.now(UTC))

        adapter.set_entitlement_resolver(resolver)
        result = await adapter.resolve_entitlement_projection("account-a", original)
        self.assertIs(result.effective.plan_id, PlanId.MAX_MONTHLY)
        self.assertEqual(calls[0][0], "account-a")
        self.assertIs(calls[0][1], original)
        with self.assertRaisesRegex(ValueError, "another account"):
            await adapter.resolve_entitlement_projection("account-b", original)

    async def test_configured_resolution_failure_does_not_silently_grant_a_base_fallback(self):
        adapter = EntitlementResolverSupport()

        async def broken(*args, **kwargs):
            raise OSError("synthetic store unavailable")

        adapter.set_entitlement_resolver(broken)
        with self.assertRaises(OSError):
            await adapter.resolve_entitlement_projection("account-a", {})


if __name__ == "__main__":
    unittest.main()
