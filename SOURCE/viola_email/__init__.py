"""
Email Service Package for Viola.

This package provides email delivery for authentication flows,
subscription notifications, and user communications.

Usage:
    >>> from email import EmailService
    >>> from email.providers import ResendProvider
    >>>
    >>> service = EmailService(provider=ResendProvider(api_key="..."))
    >>> await service.send_magic_link("user@example.com", token)
    >>> await service.send_welcome(user)
    >>> await service.send_subscription_confirmed(user, plan)

Architecture:
    - service.py: Email service abstraction
    - providers/: Email delivery providers
        - resend.py: Resend.com integration
        - smtp.py: SMTP fallback
    - templates/: HTML email templates

Email Types:
    - Magic link (passwordless login)
    - Email verification
    - Welcome email
    - Password reset
    - Subscription confirmed
    - Subscription expiring
    - Subscription canceled
"""

from .service import EmailService, get_email_service, init_email_service

__all__ = [
    "EmailService",
    "get_email_service",
    "init_email_service",
]
