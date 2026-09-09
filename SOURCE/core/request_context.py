"""Per-request context for multi-tenant isolation.

Every user turn (voice command, API request) gets a RequestContext created
at ingress. It carries the authenticated identity, resolved plan, and
customer-facing app surface. Frozen after creation and not mutated mid-request.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from auth.models import (
    SubscriptionStatus,
    billing_state_has_paid_access,
    parse_payment_provider,
)
from core.logging_config import get_logger
from core.product import AppSurface, PlanId, coerce_app_surface, coerce_plan_id

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Immutable per-request identity and capability envelope.

    ``plan_id`` is frozen at context creation — that's intentional so that a
    single request's authorization decisions are stable. However billing
    state can change mid-session (upgrade/cancel/past_due). When that
    happens, ``billing.service`` calls
    ``auth.privilege_rotation.rotate_sessions_on_privilege_change`` which
    invalidates the session and forces re-authentication on the next
    request; the new request then gets a fresh ``RequestContext`` with the
    updated plan. AUTH-07: code that wants the *live* plan mid-request
    (billing gates, quota counters) should call ``live_plan_id()``, which
    bypasses the frozen field and re-resolves via
    ``resolve_runtime_plan_id``.
    """

    user_id: str
    plan_id: str  # e.g. "free", "pro_monthly", "max_monthly"
    surface: str  # "desktop" or "cloud"
    trust_level: str  # "local_owner", "cloud_authenticated", "cloud_elevated"
    created_at: float = field(default_factory=time.monotonic)

    def live_plan_id(self) -> str:
        """AUTH-07: re-resolve the plan id from canonical settings.

        Use this when the caller needs the *current* plan and it's acceptable
        to pay the SettingsManager read cost. Falls back to the frozen
        ``plan_id`` on any resolution failure so callers don't have to
        guard against exceptions.
        """
        try:
            return resolve_runtime_plan_id(self.user_id)
        except Exception:  # pragma: no cover — defensive fallback
            logger.exception(
                "live_plan_id fell back to frozen plan_id for user %s",
                self.user_id,
            )
            return self.plan_id


_current_context: contextvars.ContextVar[RequestContext] = contextvars.ContextVar(
    "request_context",
)


def set_request_context(ctx: RequestContext) -> contextvars.Token[RequestContext]:
    """Set the RequestContext for the current async task. Returns a reset token."""
    return _current_context.set(ctx)


def reset_request_context(token: contextvars.Token[RequestContext]) -> None:
    """Reset the RequestContext using a token returned by set_request_context."""
    _current_context.reset(token)


def get_request_context() -> RequestContext | None:
    """Return the current RequestContext, or None if not set."""
    return _current_context.get(None)


def require_request_context() -> RequestContext:
    """Return the current RequestContext or raise RuntimeError."""
    ctx = _current_context.get(None)
    if ctx is None:
        raise RuntimeError("No RequestContext set - this code path requires an authenticated request")
    return ctx


def _parse_subscription_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _resolve_sqlite_subscription_plan_id(user_id: str) -> str | None:
    """Return live subscription plan from SQLite auth DB, or None if unavailable."""
    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        conn = getattr(db, "connection", None)
        if conn is None or not hasattr(conn, "execute"):
            return None
        row = conn.execute(
            """
            SELECT status, plan, payment_provider, current_period_end, activation_pending
            FROM subscriptions
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    except Exception:
        logger.debug("Live subscription lookup failed for user %s", user_id)
        return None

    if row is None:
        return PlanId.FREE.value

    status = SubscriptionStatus(row["status"])
    plan_id = coerce_plan_id(row["plan"])
    provider = parse_payment_provider(row["payment_provider"]) if row["payment_provider"] else None
    current_period_end = _parse_subscription_datetime(row["current_period_end"])
    has_paid_access = billing_state_has_paid_access(
        status,
        plan_id,
        provider,
        current_period_end,
        bool(row["activation_pending"]),
    )
    resolved = plan_id.value if has_paid_access else PlanId.FREE.value
    if not has_paid_access:
        _sync_billing_plan_mirror(user_id, PlanId.FREE.value)
    return resolved


def _run_async_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


async def _fetch_postgres_subscription_row(dsn: str, user_id: str):
    import asyncpg

    from services.sync.middleware_helpers import set_rls_context

    conn = await asyncpg.connect(dsn=dsn)
    try:
        try:
            # subscriptions is RLS owner-scoped (migration 015) and the cloud
            # runtime dsn is the viola_app role (NOSUPERUSER NOBYPASSRLS), so the
            # read must wire app.user_id in the same transaction or RLS hides the
            # row and a paying user resolves as free on cloud plan-gated paths
            # (agent/browser/phone). set_config is transaction-local. (#2106)
            async with conn.transaction():
                await set_rls_context(conn, user_id)
                return await conn.fetchrow(
                    """
                    SELECT status, plan, payment_provider, current_period_end, activation_pending
                    FROM subscriptions
                    WHERE user_id = $1
                    """,
                    user_id,
                )
        except asyncpg.exceptions.UndefinedColumnError:
            logger.warning(
                "PostgreSQL subscription table drift in request_context lookup; skipping DB plan resolution",
                extra={"user_id": user_id, "table": "subscriptions"},
            )
            return None
    finally:
        await conn.close()


def _subscription_row_plan_id(row: Any | None) -> str:
    if row is None:
        return PlanId.FREE.value

    status = SubscriptionStatus(row["status"])
    plan_id = coerce_plan_id(row["plan"])
    provider = parse_payment_provider(row["payment_provider"]) if row["payment_provider"] else None
    current_period_end = _parse_subscription_datetime(row["current_period_end"])
    has_paid_access = billing_state_has_paid_access(
        status,
        plan_id,
        provider,
        current_period_end,
        bool(row["activation_pending"]),
    )
    return plan_id.value if has_paid_access else PlanId.FREE.value


def _resolve_postgres_subscription_plan_id(user_id: str) -> str | None:
    """Return live subscription plan from Postgres auth DB, or None if unavailable."""
    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        if not hasattr(db, "subscriptions"):
            return None
        dsn = getattr(db, "_dsn", None)
        if isinstance(dsn, str) and dsn:
            return _subscription_row_plan_id(_run_async_blocking(_fetch_postgres_subscription_row(dsn, user_id)))
        if not getattr(db, "_initialized", False):
            _run_async_blocking(db.initialize())
        subscription = _run_async_blocking(db.subscriptions.get_subscription(user_id))
    except Exception:
        logger.debug("Postgres live subscription lookup failed for user %s", user_id)
        return None

    if subscription is None:
        return PlanId.FREE.value

    has_paid_access = billing_state_has_paid_access(
        subscription.status,
        subscription.plan_id,
        subscription.payment_provider,
        subscription.current_period_end,
        getattr(subscription, "activation_pending", False),
    )
    resolved = subscription.plan_id.value if has_paid_access else PlanId.FREE.value
    if not has_paid_access:
        _sync_billing_plan_mirror(user_id, PlanId.FREE.value)
    return resolved


def _sync_billing_plan_mirror(user_id: str, plan_id: str) -> None:
    try:
        from ui.settings_manager import get_settings_manager

        get_settings_manager().set_system_value("billing_plan_id", plan_id, user_id=user_id)
    except Exception:
        logger.debug("Failed to sync billing_plan_id mirror for user %s", user_id)


def resolve_runtime_plan_id(user_id: str) -> str:
    """Resolve the active runtime plan id from live subscription state.

    The settings mirror is only a fallback. It is not entitlement-authoritative
    because admin grants and cancellations expire through subscriptions.
    """
    from config.settings import settings
    from ui.settings_manager import get_settings_manager

    ctx = get_request_context()
    if ctx is not None and ctx.user_id == user_id:
        return coerce_plan_id(ctx.plan_id).value

    surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))

    if surface is AppSurface.CLOUD:
        # Safety core (Payments & Billing) — fail CLOSED. On cloud the authoritative
        # subscription read is the ONLY entitlement authority. Both resolvers return
        # None *only* on a read failure / schema-column drift — a genuine "no
        # subscription" answer returns ``free``, not None. So a None here means the
        # authoritative read is UNAVAILABLE (a subscription-read outage), not that the
        # user is unpaid. We therefore fall through to ``free`` (least privilege:
        # smallest spend cap, denied paid-only surfaces, most-restrictive phone tier)
        # and MUST NOT consult the ``billing_plan_id`` settings mirror. That mirror is
        # desktop-only authority; its cloud sync (``_sync_billing_plan_mirror``) is
        # best-effort and swallows failures, so it can be stale-permissive for a
        # cancelled/downgraded user. Reading it here would re-grant paid caps and
        # paid-only surfaces during the outage. (#2738)
        live_plan = _resolve_sqlite_subscription_plan_id(user_id)
        if live_plan is not None:
            return live_plan
        live_plan = _resolve_postgres_subscription_plan_id(user_id)
        if live_plan is not None:
            return live_plan
        logger.warning(
            "Cloud authoritative subscription read unavailable for user %s - failing "
            "closed to free (billing_plan_id mirror not consulted)",
            user_id,
        )
        return PlanId.FREE.value

    # Desktop (one-user-per-install): the settings mirror is the entitlement authority.
    try:
        sm = get_settings_manager()
        plan_raw = sm.get("billing_plan_id", PlanId.FREE.value)
        return coerce_plan_id(plan_raw).value
    except Exception:
        logger.exception("Failed to resolve runtime plan for user %s - defaulting to free", user_id)
        return PlanId.FREE.value


def create_context_for_user(user_id: str) -> RequestContext:
    """Build a RequestContext from the canonical surface and billing plan."""
    from config.settings import settings

    surface = coerce_app_surface(getattr(settings, "app_surface", AppSurface.DESKTOP.value))
    trust_level = "local_owner" if surface is AppSurface.DESKTOP else "cloud_authenticated"
    plan_id = resolve_runtime_plan_id(user_id)

    return RequestContext(
        user_id=user_id,
        plan_id=plan_id,
        surface=surface.value,
        trust_level=trust_level,
    )
