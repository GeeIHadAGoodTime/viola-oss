"""
Email Providers for Viola.

This package provides email delivery integrations.

Providers:
    - ResendProvider: Resend.com API integration
    - SMTPProvider: Standard SMTP fallback

Usage:
    >>> from viola_email.providers import ResendProvider
    >>> provider = ResendProvider(api_key="re_...")
"""

from viola_email.providers.resend import ResendProvider
from viola_email.providers.smtp import SMTPProvider

__all__ = [
    "ResendProvider",
    "SMTPProvider",
]
