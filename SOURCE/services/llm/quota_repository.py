"""
LLM Quota Repository - Database operations for LLM quota management.

This module provides access to LLM quota data stored in the auth database.
It wraps the SQLiteLLMQuotaRepository from auth/database.py for use by
the rate limiter and other LLM services.

Usage:
    from services.llm.quota_repository import LLMQuotaRepository

    repo = LLMQuotaRepository()
    quota = await repo.get_or_create_quota(user_id)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, TypedDict

from core.logging_config import get_logger

if TYPE_CHECKING:
    from auth.database import SQLiteAuthDatabase

logger = get_logger(__name__)


class QuotaInfo(TypedDict):
    """Quota information for a user."""

    user_id: str
    daily_requests: int
    daily_tokens: int
    monthly_requests: int
    monthly_tokens: int
    daily_reset_at: str
    monthly_reset_at: str


class LLMQuotaRepository:
    """
    Repository for LLM quota operations.

    Provides a clean interface to the underlying database repository,
    with additional business logic and error handling.
    """

    def __init__(self, db: SQLiteAuthDatabase | None = None) -> None:
        """
        Initialize the quota repository.

        Args:
            db: Optional database instance. If not provided, uses get_auth_db().
        """
        self._db = db

    def _get_db(self) -> SQLiteAuthDatabase:
        """Get the database instance, initializing if needed."""
        if self._db is None:
            from auth.database import get_auth_db

            self._db = get_auth_db()
        return self._db

    async def get_quota(self, user_id: str) -> QuotaInfo | None:
        """
        Get quota for a user.

        Args:
            user_id: User ID

        Returns:
            QuotaInfo or None if not found
        """
        db = self._get_db()
        row = await db.llm_quotas.get_quota(user_id)
        if row is None:
            return None

        return QuotaInfo(
            user_id=row["user_id"],
            daily_requests=row["daily_requests"],
            daily_tokens=row["daily_tokens"],
            monthly_requests=row["monthly_requests"],
            monthly_tokens=row["monthly_tokens"],
            daily_reset_at=row["daily_reset_at"],
            monthly_reset_at=row["monthly_reset_at"],
        )

    async def get_or_create_quota(self, user_id: str) -> QuotaInfo:
        """
        Get quota for a user, creating if it doesn't exist.

        Also handles automatic resets if reset times have passed.

        Args:
            user_id: User ID

        Returns:
            QuotaInfo
        """
        db = self._get_db()
        row = await db.llm_quotas.get_or_create_quota(user_id)

        return QuotaInfo(
            user_id=row["user_id"],
            daily_requests=row["daily_requests"],
            daily_tokens=row["daily_tokens"],
            monthly_requests=row["monthly_requests"],
            monthly_tokens=row["monthly_tokens"],
            daily_reset_at=row["daily_reset_at"],
            monthly_reset_at=row["monthly_reset_at"],
        )

    async def increment_usage(
        self,
        user_id: str,
        requests: int = 1,
        tokens: int = 0,
    ) -> QuotaInfo:
        """
        Increment usage counters for a user.

        Args:
            user_id: User ID
            requests: Number of requests to add (default 1)
            tokens: Number of tokens to add (default 0)

        Returns:
            Updated QuotaInfo
        """
        db = self._get_db()
        row = await db.llm_quotas.increment_usage(user_id, requests, tokens)

        return QuotaInfo(
            user_id=row["user_id"],
            daily_requests=row["daily_requests"],
            daily_tokens=row["daily_tokens"],
            monthly_requests=row["monthly_requests"],
            monthly_tokens=row["monthly_tokens"],
            daily_reset_at=row["daily_reset_at"],
            monthly_reset_at=row["monthly_reset_at"],
        )


# Thread-safe singleton
_quota_repository: LLMQuotaRepository | None = None
_quota_repository_lock = threading.Lock()


def get_quota_repository() -> LLMQuotaRepository:
    """Get the global quota repository instance (thread-safe)."""
    global _quota_repository
    if _quota_repository is None:
        with _quota_repository_lock:
            # Double-check after acquiring lock
            if _quota_repository is None:
                _quota_repository = LLMQuotaRepository()
    return _quota_repository


__all__ = [
    "LLMQuotaRepository",
    "QuotaInfo",
    "get_quota_repository",
]
