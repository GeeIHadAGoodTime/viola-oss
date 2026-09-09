"""
Authentication and Authorization Package for Viola.

This package provides GoTrue-backed auth facade helpers and subscription-backed
access for the Viola cloud backend.

Usage:
    >>> from auth import get_current_user, require_session_auth
    >>> from auth.models import User, SubscriptionStatus

Architecture:
    - models.py: Pydantic models for User, Session, Subscription
    - gotrue_facade.py: GoTrue JWT/user/session adapter
    - middleware.py: FastAPI middleware for auth extraction
    - routes.py: Auth API endpoints (/auth/*)
    - dependencies.py: FastAPI dependencies for route protection

Related Modules:
    - billing/: Subscription management and payment processing
    - email/: Email service for magic links and notifications
    - config/settings.py: CloudSettings for auth configuration
"""

from __future__ import annotations

from auth.dependencies import (
    get_current_user,
    get_current_user_optional,
    require_session_auth,
)
from auth.models import Session, SubscriptionStatus, User, UserCreate


def reset_auth_globals() -> None:
    """
    Reset all auth module global state.

    Use ONLY in tests to ensure clean state between test runs.
    """
    # Reset database
    from auth import database

    database._auth_db = None

    # Reset registration rate limiter (security hardening singleton)
    from auth import routes as _routes_mod

    _routes_mod._registration_limiter = None


__all__ = [
    "Session",
    "SubscriptionStatus",
    # Models
    "User",
    "UserCreate",
    # Dependencies
    "get_current_user",
    "get_current_user_optional",
    "require_session_auth",
    # Test utilities
    "reset_auth_globals",
]
