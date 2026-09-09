"""Authentication models for Viola user/session/subscription state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from email_validator.exceptions import EmailSyntaxError
from email_validator.syntax import split_email, validate_email_domain_literal, validate_email_local_part
from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator

from config import env as config_env
from core.product import PlanFamily, PlanId, is_paid_plan, plan_family_for_id

_MAX_EMAIL_LENGTH = 254
_MAX_EMAIL_LOCAL_PART_LENGTH = 64
_MAX_EMAIL_DOMAIN_LENGTH = 253
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_RESERVED_TEST_EMAIL_TLDS = frozenset({"invalid", "local", "test", "example"})
_INTERNAL_RESERVED_TLD_EMAILS = frozenset({"deploy-preflight@viola.local"})


def _reserved_tld_email_bypass_enabled() -> bool:
    return config_env.get_bool("VIOLA_DEV_MODE", default=False) or config_env.get_bool(
        "VIOLA_TEST_BYPASS_LIMITS",
        default=False,
    )


def _internal_reserved_tld_email_allowed(email: str) -> bool:
    return email.strip().lower() in _INTERNAL_RESERVED_TLD_EMAILS


def _normalize_user_email(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Email address is required")
    try:
        display_name, local_part, domain, quoted_local_part = split_email(raw)
        if display_name is not None:
            raise EmailSyntaxError("Display names are not allowed in user email addresses.")
        local_result = validate_email_local_part(
            local_part,
            allow_smtputf8=True,
            quoted_local_part=quoted_local_part,
        )
        normalized_local = str(local_result["local_part"])
        normalized_domain = _normalize_user_email_domain(domain, raw_email=raw)
    except EmailSyntaxError as exc:
        raise ValueError(str(exc)) from exc
    normalized = "%s@%s" % (normalized_local, normalized_domain)
    if len(normalized) > _MAX_EMAIL_LENGTH:
        raise ValueError("Email address is too long")
    if len(normalized_local.encode("utf-8")) > _MAX_EMAIL_LOCAL_PART_LENGTH:
        raise ValueError("Email address local part is too long")
    return normalized


def _normalize_user_email_domain(domain: str, *, raw_email: str | None = None) -> str:
    if domain.startswith("[") and domain.endswith("]"):
        try:
            literal = validate_email_domain_literal(domain[1:-1])
        except EmailSyntaxError as exc:
            raise ValueError(str(exc)) from exc
        return str(literal["domain"])
    if domain.endswith("."):
        raise ValueError("Email address domain must not end with a period")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("Email address domain is invalid") from exc
    if len(ascii_domain) > _MAX_EMAIL_DOMAIN_LENGTH:
        raise ValueError("Email address domain is too long")
    labels = ascii_domain.split(".")
    if not labels or any(not label for label in labels):
        raise ValueError("Email address domain is invalid")
    for label in labels:
        if not _DOMAIN_LABEL_RE.fullmatch(label):
            raise ValueError("Email address domain is invalid")
    if (
        labels[-1].lower() in _RESERVED_TEST_EMAIL_TLDS
        and not _reserved_tld_email_bypass_enabled()
        and not _internal_reserved_tld_email_allowed(raw_email or "")
    ):
        raise ValueError(
            "Email address uses an RFC 6761 reserved test TLD; set VIOLA_DEV_MODE=1 "
            "or VIOLA_TEST_BYPASS_LIMITS=1 for tests"
        )
    return ascii_domain.lower()


class SubscriptionStatus(str, Enum):
    """Subscription status values."""

    FREE = "free"
    ACTIVE = "active"
    CANCELED = "canceled"
    PAST_DUE = "past_due"
    TRIALING = "trialing"


class SubscriptionSource(str, Enum):
    """Operational source of a subscription record."""

    COMMERCIAL = "commercial"
    ADMIN_GRANT = "admin_grant"
    ADMIN_EXTENSION = "admin_extension"


class OAuthProvider(str, Enum):
    """Supported OAuth identity providers."""

    GOOGLE = "google"
    APPLE = "apple"


# =============================================================================
# User Models
# =============================================================================


class UserBase(BaseModel):
    """Base user fields shared across models."""

    email: str
    email_verified: bool = False

    @field_validator("email", mode="before")
    @classmethod
    def validate_email(cls, value: Any) -> str:
        return _normalize_user_email(value)


class UserCreate(UserBase):
    """User creation request model."""

    password: str | None = Field(
        default=None,
        min_length=12,
        max_length=128,
        description="Password (required unless using OAuth)",
    )


class User(UserBase):
    """
    User account model.

    Represents a registered Viola user with billing state.

    Attributes:
        id: Unique user identifier (UUID)
        email: User's email address
        email_verified: Whether email has been verified
        subscription_status: Current subscription status
        plan_id: Current commercial plan id
        created_at: Account creation timestamp
        updated_at: Last update timestamp
    """

    id: str
    subscription_status: SubscriptionStatus = SubscriptionStatus.FREE
    plan_id: PlanId = PlanId.FREE
    current_period_end: datetime | None = Field(default=None, exclude=True)
    activation_pending: bool = Field(default=False, exclude=True)
    phone: str | None = None
    phone_verified: bool = False
    sms_consent: bool = False
    sms_consent_at: datetime | None = None
    sms_consent_text_version: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def plan_family(self) -> PlanFamily:
        """Return the commercial family for the current plan."""
        return plan_family_for_id(self.plan_id)

    @computed_field
    @property
    def has_paid_access(self) -> bool:
        """Return True when the user's current billing state grants paid access.

        User-level answer, derived from denormalized subscription fields
        loaded from the subscriptions table. Runtime callers with a full
        Subscription should use ``subscription_has_paid_access(subscription)``
        so user and subscription checks share the same expiry rules.
        """
        return billing_state_has_paid_access(
            self.subscription_status,
            self.plan_id,
            current_period_end=self.current_period_end,
            activation_pending=self.activation_pending,
        )

    def compute_display_name(self) -> str:
        """Friendly display name derived from email (e.g., 'john.doe@example.com' -> 'John Doe').

        AUTH-11: intentionally NOT a ``@computed_field`` — surfacing a name
        derived from the plaintext email local-part via every ``model_dump()``
        leaks a portion of the email into every JSON response (logs, audit
        exports, admin tools). Callers that need the display name must ask
        for it explicitly via this method or via
        ``build_user_payload_with_entitlement`` which opts in.
        """
        if not self.email:
            return ""
        local_part = str(self.email).split("@")[0]
        # Replace common separators with spaces and title-case
        return local_part.replace(".", " ").replace("_", " ").replace("-", " ").title()


class UserInDB(User):
    """User model with database-internal fields."""

    password_hash: str | None = None


# =============================================================================
# Session Models
# =============================================================================


class SessionBase(BaseModel):
    """Base session fields."""

    device_name: str | None = None
    device_id: str | None = None


class Session(SessionBase):
    """
    User session model.

    Represents an authenticated session for a user-device pair.

    Attributes:
        id: Unique session identifier
        user_id: Associated user ID
        device_name: Human-readable device name
        device_id: Unique device identifier (for multi-device management)
        expires_at: Session expiration timestamp
        created_at: Session creation timestamp
        last_used_at: Last activity timestamp
    """

    id: str
    user_id: str
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime

    @property
    def is_expired(self) -> bool:
        """Check if session has expired."""
        return datetime.now(UTC) > self.expires_at

    @property
    def is_active(self) -> bool:
        """Check if session is still valid."""
        return not self.is_expired


class SessionInDB(Session):
    """Session model with database-internal fields."""

    token_hash: str
    # SECURITY: Token binding fields for anomaly detection
    ip_address: str | None = None  # IP at session creation
    user_agent_hash: str | None = None  # Hash of User-Agent at creation


# =============================================================================
# Magic Link Models
# =============================================================================


class MagicLinkCreate(BaseModel):
    """Magic link creation request."""

    email: EmailStr


class MagicLink(BaseModel):
    """
    Magic link token for passwordless authentication.

    Attributes:
        id: Unique magic link identifier
        email: Target email address
        expires_at: Token expiration (typically 15 minutes)
        used_at: When the link was used (None if unused)
        created_at: Creation timestamp
    """

    id: str
    email: EmailStr
    expires_at: datetime
    used_at: datetime | None = None
    created_at: datetime

    @property
    def is_expired(self) -> bool:
        """Check if magic link has expired."""
        return datetime.now(UTC) > self.expires_at

    @property
    def is_used(self) -> bool:
        """Check if magic link has been used."""
        return self.used_at is not None

    @property
    def is_valid(self) -> bool:
        """Check if magic link can still be used."""
        return not self.is_expired and not self.is_used


class MagicLinkInDB(MagicLink):
    """Magic link with database-internal fields.

    ``short_code`` is the 8-character human-typeable code emitted alongside
    the URL token (Path C2 — desktop sign-in by typing a code from the
    activation email). ``None`` for legacy rows created before C2 landed
    (those rows can still be redeemed via the URL token).
    """

    token_hash: str
    short_code: str | None = None


# =============================================================================
# OAuth Identity Models
# =============================================================================


class OAuthIdentity(BaseModel):
    """
    OAuth identity link connecting external provider to user account.

    Attributes:
        id: Unique identity record ID
        user_id: Associated Viola user ID
        provider: OAuth provider name
        provider_user_id: User ID from the OAuth provider
        email: Email from OAuth provider
        created_at: When the link was created
    """

    id: str
    user_id: str
    provider: OAuthProvider
    provider_user_id: str
    email: EmailStr | None = None
    created_at: datetime


# =============================================================================
# Subscription Models
# =============================================================================


class PaymentProvider(str, Enum):
    """Supported payment providers."""

    STRIPE = "stripe"
    BTCPAY = "btcpay"
    MANUAL = "manual"


_SUPPORTED_PAYMENT_PROVIDERS: frozenset[PaymentProvider] = frozenset(
    {PaymentProvider.STRIPE, PaymentProvider.BTCPAY, PaymentProvider.MANUAL}
)


def parse_payment_provider(value: str) -> PaymentProvider:
    provider = PaymentProvider(value)
    if provider not in _SUPPORTED_PAYMENT_PROVIDERS:
        raise ValueError(f"Unsupported payment provider: {value}")
    return provider


class Subscription(BaseModel):
    """
    User subscription record.

    Attributes:
        id: Unique subscription ID
        user_id: Associated user ID
        status: Current subscription status
        plan_id: Exact commercial plan id
        payment_provider: Payment processing provider
        external_subscription_id: ID from payment provider
        current_period_start: Current billing period start
        current_period_end: Current billing period end (access expires after)
        canceled_at: When subscription was canceled (if applicable)
        created_at: Subscription creation timestamp
        updated_at: Last update timestamp
        activation_pending: True when payment succeeded but email is still
            unverified. Such subscriptions do NOT grant paid access until
            the buyer verifies their email.
    """

    id: str
    user_id: str
    status: SubscriptionStatus = SubscriptionStatus.FREE
    plan_id: PlanId = PlanId.FREE
    payment_provider: PaymentProvider | None = None
    external_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    canceled_at: datetime | None = None
    subscription_source: SubscriptionSource = SubscriptionSource.COMMERCIAL
    granted_by_admin_token_digest: str | None = None
    granted_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    activation_pending: bool = False

    @property
    def is_active(self) -> bool:
        """Check if subscription is currently active or trialing."""
        return self.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)

    @computed_field
    @property
    def plan_family(self) -> PlanFamily:
        """Return the commercial family for the subscription plan."""
        return plan_family_for_id(self.plan_id)

    @property
    def days_remaining(self) -> int | None:
        """Calculate days remaining in current period."""
        if self.current_period_end is None:
            return None
        delta = self.current_period_end - datetime.now(UTC)
        return max(0, delta.days)


@dataclass(frozen=True, slots=True)
class ResolvedEntitlement:
    """Canonical billing truth for auth payloads and runtime consumers."""

    plan_id: PlanId
    plan_family: PlanFamily
    has_paid_access: bool
    subscription_status: SubscriptionStatus
    current_period_end: datetime | None = None
    canceled_at: datetime | None = None
    payment_provider: PaymentProvider | None = None
    subscription_source: SubscriptionSource = SubscriptionSource.COMMERCIAL


def billing_state_has_paid_access(
    subscription_status: SubscriptionStatus,
    plan_id: PlanId,
    payment_provider: PaymentProvider | None = None,
    current_period_end: datetime | None = None,
    activation_pending: bool = False,
) -> bool:
    """Return whether a billing state grants paid access.

    ``activation_pending=True`` means the subscription was created after a
    successful payment, but the buyer's email has not been verified yet.
    Such subscriptions never grant paid access regardless of status so that
    someone who types another person's email into guest checkout cannot
    unlock features on behalf of that account.
    """
    if not is_paid_plan(plan_id):
        return False

    if activation_pending:
        return False

    now = datetime.now(UTC)
    if subscription_status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING):
        if current_period_end is not None and current_period_end <= now:
            return False
        if payment_provider == PaymentProvider.BTCPAY:
            return current_period_end is not None and current_period_end > now
        return True

    if subscription_status == SubscriptionStatus.PAST_DUE:
        if payment_provider == PaymentProvider.BTCPAY:
            return False
        return current_period_end is not None and current_period_end > now

    if subscription_status == SubscriptionStatus.CANCELED:
        return current_period_end is not None and current_period_end > now

    return False


def subscription_has_paid_access(subscription: Subscription | None) -> bool:
    """Return whether a subscription grants paid access."""
    if subscription is None:
        return False
    return billing_state_has_paid_access(
        subscription.status,
        subscription.plan_id,
        subscription.payment_provider,
        subscription.current_period_end,
        getattr(subscription, "activation_pending", False),
    )


def resolve_user_entitlement(
    user: User | None,
    subscription: Subscription | None = None,
) -> ResolvedEntitlement:
    """Resolve canonical entitlement truth for a user and optional subscription."""
    if subscription is not None:
        return ResolvedEntitlement(
            plan_id=subscription.plan_id,
            plan_family=subscription.plan_family,
            has_paid_access=subscription_has_paid_access(subscription),
            subscription_status=subscription.status,
            current_period_end=subscription.current_period_end,
            canceled_at=subscription.canceled_at,
            payment_provider=subscription.payment_provider,
            subscription_source=subscription.subscription_source,
        )

    if user is not None:
        return ResolvedEntitlement(
            plan_id=user.plan_id,
            plan_family=user.plan_family,
            has_paid_access=user.has_paid_access,
            subscription_status=user.subscription_status,
            current_period_end=user.current_period_end,
        )

    return ResolvedEntitlement(
        plan_id=PlanId.FREE,
        plan_family=PlanFamily.FREE,
        has_paid_access=False,
        subscription_status=SubscriptionStatus.FREE,
    )


def build_user_payload_with_entitlement(
    user: User,
    subscription: Subscription | None = None,
    *,
    include_display_name: bool = True,
) -> dict[str, Any]:
    """Build a JSON-ready user payload with canonical entitlement fields.

    AUTH-11: ``display_name`` is derived from the email local-part, so it is
    an explicit opt-in here rather than a silent ``@computed_field`` that
    would leak name material into every ``user.model_dump()`` — including
    logs and audit exports. Auth routes default to including it (legacy
    behavior); other callers should pass ``include_display_name=False``.
    """
    entitlement = resolve_user_entitlement(user, subscription)
    payload = user.model_dump(mode="json")
    if include_display_name:
        payload["display_name"] = user.compute_display_name()
    payload.update(
        {
            "plan_id": entitlement.plan_id.value,
            "plan_family": entitlement.plan_family.value,
            "subscription_status": entitlement.subscription_status.value,
            "has_paid_access": entitlement.has_paid_access,
        }
    )
    if entitlement.current_period_end is not None:
        payload["current_period_end"] = entitlement.current_period_end.isoformat()
    if entitlement.canceled_at is not None:
        payload["canceled_at"] = entitlement.canceled_at.isoformat()
    if entitlement.payment_provider is not None:
        payload["payment_provider"] = entitlement.payment_provider.value
    return payload


class LoginRequest(BaseModel):
    """Login request with email/password."""

    email: EmailStr
    password: str = Field(max_length=128)
    device_name: str | None = None
    device_id: str | None = None


class LogoutRequest(BaseModel):
    """Logout request (optional session ID to revoke specific session)."""

    session_id: str | None = None
    all_sessions: bool = False


# =============================================================================
# Utility Functions (consolidated in auth/utils.py)
# =============================================================================

# generate_id is re-exported for convenience (widely used).
# All other token functions should be imported from auth.utils directly.
