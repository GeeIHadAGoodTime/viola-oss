"""
Retired cross-device OAuth token synchronization.

Provider OAuth tokens are Tier 3 user secrets and must not be uploaded to the
auth database. This module remains as a compatibility facade for callers that
previously asked whether cloud sync was allowed, but all upload/read/update
paths fail closed permanently. Deletion remains available to purge legacy rows.

Usage:
    >>> from music.consent.token_sync import TokenSyncService, get_token_sync_service
    >>> sync = get_token_sync_service()
    >>> if sync.should_sync_tokens(user_id):
    ...     await sync.sync_token_to_db(user_id, "spotify", token_bundle)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from auth.models import OAuthProvider
from core.logging_config import get_logger

if TYPE_CHECKING:
    from music.consent.models import TokenBundle

logger = get_logger("viola.music.consent.token_sync")

# User setting key for cross-device token sync consent
CLOUD_SYNC_PROVIDER_TOKENS_KEY = "cloud_sync_provider_tokens"


async def _cloud_sync_allowed(user_id: str | None, *, action: str) -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async("cloud_sync", user_id=user_id, action=action)
    except Exception:
        logger.exception("Token cloud-sync owner safety control check failed closed")
        return False
    if decision.allowed:
        return True
    logger.warning("Token cloud sync blocked by owner safety control: %s", decision.reason)
    return False


class TokenSyncService:
    """
    Compatibility facade for retired cross-device OAuth token synchronization.

    Thread-safety: All methods are safe to call from multiple threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def get_user_consent(self, user_id: str) -> bool:
        """
        Check if user has consented to cloud token sync.

        Args:
            user_id: User identifier (from auth session, not "default")

        Returns:
            True if user has explicitly enabled cloud_sync_provider_tokens

        Note:
            Returns False if:
            - User has no settings record
            - Setting is not present (default is False)
            - Setting is explicitly False
            - Any error occurs (fail-safe)
        """
        del user_id
        return False

    def is_authenticated_user(self, user_id: str) -> bool:
        """
        Check if user_id represents an authenticated user (not "default").

        Args:
            user_id: User identifier

        Returns:
            True if user_id is a valid authenticated user ID
        """
        if not user_id:
            return False
        if user_id == "default":  # mt-ok: explicit rejection
            return False
        # Valid user IDs are UUIDs (32 hex chars) or similar
        # For now, just check it's not the default
        return True

    def should_sync_tokens(self, user_id: str) -> bool:
        """
        Determine if tokens should be synced to cloud for this user.

        This is the main entry point for consent checks. It verifies:
        1. User is authenticated (not "default")
        2. User has explicitly enabled cloud_sync_provider_tokens

        Args:
            user_id: User identifier

        Returns:
            True only if both conditions are met
        """
        del user_id
        return False

    async def sync_token_to_db(
        self,
        user_id: str,
        provider_id: str,
        bundle: TokenBundle,
    ) -> bool:
        """
        Sync token to auth database (when consent is enabled).

        This is called after successful OAuth linking to persist
        tokens to the cloud-synced database.

        Args:
            user_id: User identifier (must be authenticated, not "default")
            provider_id: Music provider ID (e.g., "youtube_music")
            bundle: Token bundle from OAuth exchange

        Returns:
            True if sync succeeded, False otherwise

        Note:
            This method is a no-op if consent is not enabled.
            Failures are logged but don't raise exceptions.
        """
        del bundle
        await _cloud_sync_allowed(user_id, action="provider_token_sync_to_db")
        logger.warning("Provider token cloud sync is disabled; DB write skipped for provider %s", provider_id)
        return False

    async def get_token_from_db(
        self,
        user_id: str,
        provider_id: str,
    ) -> TokenBundle | None:
        """
        Retrieve token from auth database (when consent is enabled).

        This is called during token retrieval to check if cloud-synced
        tokens are available before falling back to device-local vault.

        Args:
            user_id: User identifier (must be authenticated, not "default")
            provider_id: Music provider ID (e.g., "youtube_music")

        Returns:
            TokenBundle if found and consent enabled, None otherwise
        """
        await _cloud_sync_allowed(user_id, action="provider_token_sync_from_db")
        logger.debug("Provider token cloud sync is disabled; DB read skipped for provider %s", provider_id)
        return None

    async def update_token_in_db(
        self,
        user_id: str,
        provider_id: str,
        bundle: TokenBundle,
    ) -> bool:
        """
        Update token in database after refresh.

        Called when token is refreshed to keep DB in sync.

        Args:
            user_id: User identifier
            provider_id: Music provider ID
            bundle: Refreshed token bundle

        Returns:
            True if update succeeded
        """
        # Same as sync_token_to_db - it upserts
        return await self.sync_token_to_db(user_id, provider_id, bundle)

    async def delete_token_from_db(
        self,
        user_id: str,
        provider_id: str,
    ) -> bool:
        """
        Delete token from database when provider is unlinked.

        Args:
            user_id: User identifier
            provider_id: Music provider ID

        Returns:
            True if deletion succeeded
        """
        if not self.is_authenticated_user(user_id):
            return False

        try:
            from auth.database import get_auth_db

            db = get_auth_db()
            oauth_provider = self._get_oauth_provider(provider_id)

            if oauth_provider is None:
                return False

            result = await db.oauth_tokens.delete_tokens(
                user_id=user_id,
                provider=oauth_provider,
            )

            if result:
                logger.info(
                    "Token deleted from DB for user %s provider %s",
                    user_id,
                    provider_id,
                )

            return result

        except Exception as e:
            logger.warning(
                "Token deletion from DB failed: user=%s provider=%s error=%s",
                user_id,
                provider_id,
                e,
            )
            return False

    def _get_oauth_provider(self, provider_id: str) -> OAuthProvider | None:
        """Map music provider ID to OAuthProvider enum."""
        if provider_id == "youtube_music":
            return None
        # Add other mappings as needed
        return None


# =============================================================================
# Module-level singleton
# =============================================================================

_token_sync_service: TokenSyncService | None = None
_service_lock = threading.Lock()


def get_token_sync_service() -> TokenSyncService:
    """Get or create the global TokenSyncService instance."""
    global _token_sync_service
    if _token_sync_service is None:
        with _service_lock:
            if _token_sync_service is None:
                _token_sync_service = TokenSyncService()
    return _token_sync_service


# =============================================================================
# Convenience functions for inline use
# =============================================================================


def should_sync_tokens(user_id: str) -> bool:
    """Check if tokens should be synced for this user (convenience wrapper)."""
    return get_token_sync_service().should_sync_tokens(user_id)


async def sync_token_to_db(user_id: str, provider_id: str, bundle: TokenBundle) -> bool:
    """Sync token to DB if consent enabled (convenience wrapper)."""
    return await get_token_sync_service().sync_token_to_db(user_id, provider_id, bundle)


async def get_token_from_db(user_id: str, provider_id: str) -> TokenBundle | None:
    """Get token from DB if consent enabled (convenience wrapper)."""
    return await get_token_sync_service().get_token_from_db(user_id, provider_id)
