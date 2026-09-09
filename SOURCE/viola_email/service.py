"""
Email Service for Viola.

This module provides the core email service that abstracts over
different email providers (Resend, SMTP, etc.).

Usage:
    >>> from email.service import EmailService
    >>> service = EmailService(provider=ResendProvider(...))
    >>> await service.send_magic_link("user@example.com", token)
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlencode

from auth.utils import mask_email
from core.logging_config import get_logger
from viola_email.suppression import get_suppression_list

if TYPE_CHECKING:
    from auth.models import User
    from billing.models import Plan

logger = get_logger(__name__)


async def _external_channels_allowed(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled

        decision = await asyncio.to_thread(require_enabled, "external_channels", user_id=None, action=action)
    except Exception:
        logger.exception("Email owner safety control check failed closed")
        return False
    if decision.allowed:
        return True
    logger.warning("Email send blocked by owner safety control: %s", decision.reason)
    return False


# =============================================================================
# Email Models
# =============================================================================


@dataclass
class Email:
    """Email message to send."""

    to: str
    subject: str
    html: str
    text: str | None = None
    from_address: str | None = None
    reply_to: str | None = None
    # Extra MIME headers passed through to the provider. Used for reply
    # threading: {"In-Reply-To": "<msgid>", "References": "<msgid> ..."}.
    headers: dict[str, str] | None = None


# =============================================================================
# Provider Protocol
# =============================================================================


class EmailProviderProtocol(Protocol):
    """Protocol that all email providers must implement."""

    async def send(self, email: Email) -> bool:
        """
        Send an email.

        Args:
            email: Email message

        Returns:
            True if sent successfully
        """
        ...


# =============================================================================
# Email Service
# =============================================================================


class EmailService:
    """
    Email service for sending transactional emails.

    Supports multiple providers and template rendering.

    Attributes:
        provider: Email delivery provider
        from_address: Default from address
        base_url: Base URL for links in emails
    """

    def __init__(
        self,
        provider: EmailProviderProtocol | None = None,
        from_address: str = "Viola <noreply@useviola.com>",
        base_url: str = "https://useviola.com",
        api_base_url: str = "https://api.useviola.com",
    ) -> None:
        """
        Initialize email service.

        Args:
            provider: Email provider (uses null provider if None)
            from_address: Default from address
            base_url: Marketing-site URL for human-facing links (login, settings, docs)
            api_base_url: API origin for token-handling endpoints (verify-email,
                magic-link/verify, reset-password). Marketing site has no /auth
                routes, so token links must hit the API directly.
        """
        self.provider = provider
        self.from_address = from_address
        self.base_url = base_url.rstrip("/")
        self.api_base_url = api_base_url.rstrip("/")

    @property
    def is_configured(self) -> bool:
        """Return True if a real email provider is set."""
        return self.provider is not None

    async def send(self, email: Email) -> bool:
        """
        Send an email.

        Args:
            email: Email message

        Returns:
            True if sent successfully
        """
        if not await _external_channels_allowed("email_service_send"):
            return False

        if self.provider is None:
            logger.warning("No email provider configured, not sending email to %s", email.to)
            return False

        if get_suppression_list().is_suppressed(email.to):
            logger.info("Email suppressed: %s", mask_email(email.to))
            return False

        # Set default from address
        if email.from_address is None:
            email.from_address = self.from_address

        try:
            return await self.provider.send(email)
        except Exception as exc:
            logger.error("Failed to send email: %s", exc)
            return False

    # =========================================================================
    # Authentication Emails
    # =========================================================================

    async def send_magic_link(
        self,
        email_address: str,
        token: str,
        *,
        short_code: str | None = None,
    ) -> bool:
        """Send a magic link for passwordless login.

        Args:
            email_address: Recipient email
            token: Magic link URL token (long, 32+ char URL-safe base64)
            short_code: Optional 8-character human-typeable code that
                redeems the same magic link via
                ``POST /auth/magic-link/verify-code`` (Path C2). When set,
                the email body shows both the URL link AND the code so the
                user can sign in by either clicking or typing.

        Returns:
            True if sent
        """
        magic_link_url = f"{self.api_base_url}/auth/magic-link/verify?token={token}"

        # Render code block conditionally so legacy callers (no short_code)
        # still get the original "click the link" email.
        if short_code:
            code_html_block = f"""
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #666; font-size: 14px; line-height: 1.5;">
                Or, if you started sign-in inside the Viola desktop app,
                enter this code there:
            </p>
            <p style="margin: 12px 0 24px 0; text-align: center;">
                <code style="display: inline-block; padding: 14px 22px; background: #f4f4f5; border: 1px solid #e4e4e7; border-radius: 8px; font-size: 24px; letter-spacing: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #18181b;">{short_code}</code>
            </p>
            """
            code_text_block = (
                "\n"
                "Or, if you started sign-in inside the Viola desktop app, "
                "enter this code there:\n\n"
                f"    {short_code}\n"
            )
        else:
            code_html_block = ""
            code_text_block = ""

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Sign in to Viola</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Click the button below to sign in to your Viola account. This link will expire in 15 minutes.
            </p>
            <p style="margin: 30px 0;">
                <a href="{magic_link_url}" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Sign in to Viola
                </a>
            </p>
            {code_html_block}
            <p style="color: #999; font-size: 14px;">
                If you didn't request this email, you can safely ignore it.
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                This email was sent by Viola. If the button doesn't work, copy and paste this link into your browser:
                <br><a href="{magic_link_url}" style="color: #6366f1; word-break: break-all;">{magic_link_url}</a>
            </p>
        </body>
        </html>
        """

        text = f"""
Sign in to Viola

Click the link below to sign in to your Viola account. This link will expire in 15 minutes.

{magic_link_url}
{code_text_block}
If you didn't request this email, you can safely ignore it.
        """

        return await self.send(
            Email(
                to=email_address,
                subject="Sign in to Viola",
                html=html,
                text=text,
            )
        )

    async def send_checkout_activation(
        self,
        email_address: str,
        token: str,
        plan_name: str | None = None,
        *,
        short_code: str | None = None,
    ) -> bool:
        """Send a post-checkout activation link for a paid guest checkout.

        Args:
            email_address: Recipient email
            token: 24-hour magic-link URL token
            plan_name: Human-readable plan name
            short_code: Optional 8-character typed code (Path C2). When set,
                the email body shows both the URL link and the code so a
                stranger who paid via web → installed desktop → opens the
                "I have a code" tab can sign in without clicking the link.

        Returns:
            True if sent
        """
        query = urlencode({"magic_token": token, "redirect": "account.html"})
        activation_url = f"{self.base_url}/login.html?{query}"
        plan_label = plan_name or "paid"

        if short_code:
            code_html_block = f"""
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #666; font-size: 14px; line-height: 1.5;">
                Or, if you started sign-in inside the Viola desktop app,
                enter this code there:
            </p>
            <p style="margin: 12px 0 24px 0; text-align: center;">
                <code style="display: inline-block; padding: 14px 22px; background: #f4f4f5; border: 1px solid #e4e4e7; border-radius: 8px; font-size: 24px; letter-spacing: 4px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #18181b;">{short_code}</code>
            </p>
            """
            code_text_block = (
                "\n"
                "Or, if you started sign-in inside the Viola desktop app, "
                "enter this code there:\n\n"
                f"    {short_code}\n"
            )
        else:
            code_html_block = ""
            code_text_block = ""

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Activate your Viola subscription</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Your {plan_label} subscription is ready. Click the button below to verify your email and open your account.
            </p>
            <p style="margin: 30px 0;">
                <a href="{activation_url}" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Activate Viola
                </a>
            </p>
            <p style="color: #999; font-size: 14px;">
                This one-time link expires in 24 hours.
            </p>
            {code_html_block}
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If the button doesn't work, copy and paste this link into your browser:
                <br><a href="{activation_url}" style="color: #6366f1; word-break: break-all;">{activation_url}</a>
            </p>
        </body>
        </html>
        """

        text = f"""
Activate your Viola subscription

Your {plan_label} subscription is ready. Open this one-time link to verify your email and open your account:

{activation_url}
{code_text_block}
This link expires in 24 hours.
        """

        return await self.send(
            Email(
                to=email_address,
                subject="Activate your Viola subscription",
                html=html,
                text=text,
            )
        )

    async def send_verification_email(self, email_address: str, token: str) -> bool:
        """
        Send an email verification link.

        Args:
            email_address: Recipient email
            token: Verification token

        Returns:
            True if sent
        """
        verify_url = f"{self.api_base_url}/auth/verify-email?token={token}"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Verify your email</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Thanks for signing up for Viola! Please verify your email address by clicking the button below.
            </p>
            <p style="margin: 30px 0;">
                <a href="{verify_url}" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Verify Email
                </a>
            </p>
            <p style="color: #999; font-size: 14px;">
                This link will expire in 24 hours.
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=email_address,
                subject="Verify your Viola email",
                html=html,
            )
        )

    async def send_password_reset(self, email_address: str, token: str) -> bool:
        """
        Send a password reset link.

        Args:
            email_address: Recipient email
            token: Reset token

        Returns:
            True if sent
        """
        reset_url = f"{self.api_base_url}/auth/reset-password?token={token}"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Reset your password</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                We received a request to reset your Viola password. Click the button below to choose a new password.
            </p>
            <p style="margin: 30px 0;">
                <a href="{reset_url}" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Reset Password
                </a>
            </p>
            <p style="color: #999; font-size: 14px;">
                This link will expire in 1 hour. If you didn't request a password reset, you can safely ignore this email.
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=email_address,
                subject="Reset your Viola password",
                html=html,
            )
        )

    async def send_welcome(self, user: User) -> bool:
        """
        Send a welcome email to new users.

        Args:
            user: New user

        Returns:
            True if sent
        """
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Welcome to Viola!</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Thanks for joining Viola, your personal voice assistant. Here are some things you can do:
            </p>
            <ul style="color: #666; font-size: 16px; line-height: 1.8;">
                <li>Play music with your voice</li>
                <li>Set timers and alarms</li>
                <li>Control smart home devices</li>
                <li>Get answers to questions</li>
            </ul>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Just say "Viola" to get started!
            </p>
            <p style="margin: 30px 0;">
                <a href="{self.base_url}/docs/getting-started" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Get Started Guide
                </a>
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=user.email,
                subject="Welcome to Viola!",
                html=html,
            )
        )

    # =========================================================================
    # Subscription Emails
    # =========================================================================

    async def send_subscription_confirmed(self, user: User, plan: Plan) -> bool:
        """
        Send subscription confirmation email.

        Args:
            user: Subscriber
            plan: Purchased plan

        Returns:
            True if sent
        """
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">You're now a Viola Premium member!</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Thank you for subscribing to {plan.name}. Your premium features are now active.
            </p>
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <h3 style="color: #333; margin-top: 0;">Your Premium Features:</h3>
                <ul style="color: #666; font-size: 14px; line-height: 1.8;">
                    <li>Multi-room audio sync</li>
                    <li>Cloud sync across devices</li>
                    <li>All music providers</li>
                    <li>Advanced wake word tuning</li>
                    <li>Priority support</li>
                </ul>
            </div>
            <p style="color: #999; font-size: 14px;">
                You can manage your subscription at any time in Settings.
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=user.email,
                subject=f"Welcome to {plan.name}!",
                html=html,
            )
        )

    async def send_subscription_expiring(
        self,
        user: User,
        days_remaining: int,
    ) -> bool:
        """
        Send subscription expiration warning.

        Args:
            user: User with expiring subscription
            days_remaining: Days until expiration

        Returns:
            True if sent
        """
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Your Viola Premium subscription is expiring</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Your premium subscription will expire in {days_remaining} days. Renew now to keep your premium features.
            </p>
            <p style="margin: 30px 0;">
                <a href="{self.base_url}/settings/subscription" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Renew Subscription
                </a>
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=user.email,
                subject="Your Viola Premium subscription is expiring soon",
                html=html,
            )
        )

    async def send_subscription_canceled(self, user: User, access_until: str) -> bool:
        """
        Send subscription cancellation confirmation.

        Args:
            user: User who canceled
            access_until: Date when access ends

        Returns:
            True if sent
        """
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Your subscription has been canceled</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                We're sorry to see you go! Your Viola Premium subscription has been canceled.
            </p>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                You'll continue to have access to premium features until <strong>{access_until}</strong>.
            </p>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Changed your mind? You can resubscribe at any time.
            </p>
            <p style="margin: 30px 0;">
                <a href="{self.base_url}/settings/subscription" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Resubscribe
                </a>
            </p>
        </body>
        </html>
        """

        return await self.send(
            Email(
                to=user.email,
                subject="Your Viola subscription has been canceled",
                html=html,
            )
        )

    async def send_renewal_reminder(
        self,
        user: User,
        plan_name: str,
        renewal_date: str,
        price_display: str,
        days_until_renewal: int,
    ) -> bool:
        """
        Send a pre-renewal reminder email.

        Required by Terms of Service Section 4.3: users must be notified
        7 days before monthly renewal, 30 days before annual renewal.

        Args:
            user: Subscriber to notify
            plan_name: Display name of the plan (e.g. "Viola Pro")
            renewal_date: Human-readable renewal date (e.g. "May 15, 2026")
            price_display: Price string (e.g. "$12.00/month")
            days_until_renewal: Number of days until renewal

        Returns:
            True if sent
        """
        subject = "Your %s subscription renews in %d days" % (
            plan_name,
            days_until_renewal,
        )

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; max-width: 600px; margin: 0 auto;">
            <h1 style="color: #333;">Upcoming subscription renewal</h1>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                Hi! This is a friendly reminder that your <strong>{plan_name}</strong> subscription will automatically renew on <strong>{renewal_date}</strong>.
            </p>
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="color: #666; padding: 8px 0;">Plan</td>
                        <td style="color: #333; font-weight: 500; text-align: right; padding: 8px 0;">{plan_name}</td>
                    </tr>
                    <tr>
                        <td style="color: #666; padding: 8px 0;">Renewal date</td>
                        <td style="color: #333; font-weight: 500; text-align: right; padding: 8px 0;">{renewal_date}</td>
                    </tr>
                    <tr>
                        <td style="color: #666; padding: 8px 0;">Amount</td>
                        <td style="color: #333; font-weight: 500; text-align: right; padding: 8px 0;">{price_display}</td>
                    </tr>
                </table>
            </div>
            <p style="color: #666; font-size: 16px; line-height: 1.5;">
                No action is needed if you'd like to continue your subscription. If you'd like to make changes or cancel, you can do so from your account settings.
            </p>
            <p style="margin: 30px 0;">
                <a href="{self.base_url}/settings/subscription" style="background-color: #6366f1; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
                    Manage Subscription
                </a>
            </p>
            <p style="color: #999; font-size: 14px;">
                You're receiving this email because your Viola subscription is set to auto-renew. You can cancel at any time from your <a href="{self.base_url}/settings/subscription" style="color: #6366f1;">account settings</a>.
            </p>
        </body>
        </html>
        """

        text = (
            "Upcoming subscription renewal\n\n"
            "Your %s subscription will automatically renew on %s "
            "for %s.\n\n"
            "No action needed to continue. To make changes or cancel, "
            "visit: %s/settings/subscription\n"
        ) % (plan_name, renewal_date, price_display, self.base_url)

        return await self.send(
            Email(
                to=user.email,
                subject=subject,
                html=html,
                text=text,
            )
        )


# =============================================================================
# Global Service Instance
# =============================================================================


_email_service: EmailService | None = None
_service_lock = threading.Lock()


def _service_from_settings() -> EmailService:
    from config.settings import get_settings
    from viola_email.providers.resend import ResendProvider, is_resend_configured
    from viola_email.providers.smtp import SMTPProvider, is_smtp_configured

    app_settings = get_settings()
    provider: EmailProviderProtocol | None
    if is_resend_configured():
        provider = ResendProvider.from_settings()
    elif is_smtp_configured():
        provider = SMTPProvider.from_settings()
    else:
        provider = None
    return EmailService(
        provider=provider,
        from_address=getattr(app_settings, "email_from_address", "Viola <noreply@useviola.com>"),
        base_url=getattr(app_settings, "website_base_url", "https://useviola.com"),
        api_base_url=getattr(app_settings, "api_base_url", "https://api.useviola.com"),
    )


def init_email_service(
    provider: EmailProviderProtocol | None = None,
    from_address: str = "Viola <noreply@useviola.com>",
    base_url: str = "https://useviola.com",
    api_base_url: str = "https://api.useviola.com",
) -> EmailService:
    """Initialize global email service."""
    global _email_service
    with _service_lock:
        _email_service = EmailService(
            provider=provider,
            from_address=from_address,
            base_url=base_url,
            api_base_url=api_base_url,
        )
        return _email_service


def get_email_service() -> EmailService:
    """Get global email service."""
    global _email_service
    if _email_service is None:
        with _service_lock:
            if _email_service is None:
                try:
                    _email_service = _service_from_settings()
                except Exception:
                    logger.exception("Email service lazy initialization failed")
                    _email_service = EmailService()
    return _email_service
