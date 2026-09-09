"""
Resend Email Provider for Viola.

This module provides integration with Resend.com for email delivery.
Resend offers 3,000 free emails per month with excellent deliverability.

Usage:
    >>> from email.providers.resend import ResendProvider
    >>> provider = ResendProvider(api_key="re_...")
    >>> await provider.send(email)

Setup:
    1. Create account at resend.com
    2. Verify your domain
    3. Create API key
    4. Add to settings: RESEND_API_KEY

Reference:
    https://resend.com/docs/api-reference/emails/send-email
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from core.logging_config import get_logger

if TYPE_CHECKING:
    from email.service import Email

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


class ResendProvider:
    """
    Resend.com email provider.

    Provides email delivery via Resend's API with automatic
    retries and error handling.

    Attributes:
        api_key: Resend API key
    """

    def __init__(self, api_key: str) -> None:
        """
        Initialize Resend provider.

        Args:
            api_key: Resend API key (re_...)
        """
        self.api_key = api_key
        self._http_client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls) -> ResendProvider:
        """
        Create provider from application settings.

        Returns:
            Configured provider

        Raises:
            ValueError: If not configured
        """
        from config.settings import get_settings

        settings = get_settings()
        api_key = getattr(settings, "resend_api_key", None)

        if not api_key:
            raise ValueError("Resend API key not configured")

        return cls(api_key=api_key)

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._http_client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def send(self, email: Email) -> bool:
        """
        Send an email via Resend.

        Args:
            email: Email message

        Returns:
            True if sent successfully
        """
        payload = {
            "from": email.from_address,
            "to": [email.to],
            "subject": email.subject,
            "html": email.html,
        }

        if email.text:
            payload["text"] = email.text

        if email.reply_to:
            payload["reply_to"] = email.reply_to

        # Custom MIME headers (e.g. In-Reply-To / References for threading).
        if email.headers:
            payload["headers"] = email.headers

        try:
            response = await self.http_client.post(RESEND_API_URL, json=payload)

            if response.status_code == 200:
                data = response.json()
                logger.info("Email sent via Resend: %s", data.get("id"))
                return True

            error_data = response.json() if response.content else {}
            logger.error(
                "Resend API error: %s",
                response.status_code,
                extra={
                    "status_code": response.status_code,
                    "error": error_data,
                },
            )
            return False

        except httpx.RequestError as exc:
            logger.error("Resend request failed: %s", exc)
            return False


def is_resend_configured() -> bool:
    """Check if Resend is configured."""
    try:
        from config.settings import get_settings

        settings = get_settings()
        return bool(getattr(settings, "resend_api_key", None))
    except Exception:
        return False
