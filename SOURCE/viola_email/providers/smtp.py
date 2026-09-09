"""
SMTP Email Provider for Viola.

This module provides a fallback SMTP email delivery option.
Useful for self-hosted setups or testing.

Usage:
    >>> from email.providers.smtp import SMTPProvider
    >>> provider = SMTPProvider(
    ...     host="smtp.example.com",
    ...     port=587,
    ...     username="user",
    ...     password="pass",  # pragma: allowlist secret
    ... )
    >>> await provider.send(email)
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from email.service import Email

logger = get_logger(__name__)


from auth.utils import mask_email as _mask_email


class SMTPProvider:
    """
    SMTP email provider.

    Provides email delivery via standard SMTP protocol.
    Supports TLS and authentication.

    Attributes:
        host: SMTP server hostname
        port: SMTP server port
        username: SMTP username (optional)
        password: SMTP password (optional)
        use_tls: Whether to use TLS
        use_ssl: Whether to use SSL
    """

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        use_ssl: bool = False,
    ) -> None:
        """
        Initialize SMTP provider.

        Args:
            host: SMTP server hostname
            port: SMTP server port
            username: SMTP username
            password: SMTP password
            use_tls: Use STARTTLS
            use_ssl: Use implicit SSL
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl

    @classmethod
    def from_settings(cls) -> SMTPProvider:
        """
        Create provider from application settings.

        Returns:
            Configured provider

        Raises:
            ValueError: If not configured
        """
        from config.settings import get_settings

        settings = get_settings()
        host = getattr(settings, "smtp_host", None)

        if not host:
            raise ValueError("SMTP not configured")

        return cls(
            host=host,
            port=getattr(settings, "smtp_port", 587),
            username=getattr(settings, "smtp_username", None),
            password=getattr(settings, "smtp_password", None),
            use_tls=getattr(settings, "smtp_use_tls", True),
            use_ssl=getattr(settings, "smtp_use_ssl", False),
        )

    async def send(self, email: Email) -> bool:
        """
        Send an email via SMTP.

        Args:
            email: Email message

        Returns:
            True if sent successfully
        """
        # Run SMTP in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, email)

    def _send_sync(self, email: Email) -> bool:
        """
        Synchronous SMTP send.

        Args:
            email: Email message

        Returns:
            True if sent
        """
        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = email.subject
            msg["From"] = email.from_address or "noreply@viola.app"
            msg["To"] = email.to

            if email.reply_to:
                msg["Reply-To"] = email.reply_to

            # Attach text and HTML parts
            if email.text:
                msg.attach(MIMEText(email.text, "plain"))
            msg.attach(MIMEText(email.html, "html"))

            # Connect and send
            server: smtplib.SMTP | smtplib.SMTP_SSL
            if self.use_ssl:
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.host, self.port, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port)

            try:
                if self.use_tls and not self.use_ssl:
                    context = ssl.create_default_context()
                    server.starttls(context=context)

                if self.username and self.password:
                    server.login(self.username, self.password)

                server.send_message(msg)
                logger.info("Email sent via SMTP to %s", _mask_email(email.to))
                return True

            finally:
                server.quit()

        except Exception as exc:
            logger.error("SMTP send failed: %s", exc)
            return False


def is_smtp_configured() -> bool:
    """Check if SMTP is configured."""
    try:
        from config.settings import get_settings

        settings = get_settings()
        return bool(getattr(settings, "smtp_host", None))
    except Exception:
        return False
