"""
Consent Service Token Management.

This module contains token management logic
extracted from the main ConsentService class to comply with code constraints.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from core.logging_config import get_logger
from core.user_context import user_scope
from music.consent import providers
from music.consent.exceptions import ProviderNotRegistered
from music.consent.models import (
    RotationOutcome,
    TokenBundle,
)

logger = get_logger(__name__)


class ConsentTokenManager:
    """Handles token storage, retrieval, and rotation."""

    def __init__(self, service_instance):
        """
        Initialize token manager.

        Args:
            service_instance: The ConsentService instance
        """
        self.service = service_instance

    def resolve_access_token(self, provider_id: str, user_id: str | None = None) -> dict[str, str | int | None] | None:
        """
        Resolve access token for a provider.

        Args:
            provider_id: Provider identifier
            user_id: User identifier (optional)

        Returns:
            Token data or None if not available

        Raises:
            ProviderNotRegistered: If provider doesn't exist
        """
        adapter = providers.get_provider(provider_id)
        if adapter is None:
            raise ProviderNotRegistered(f"Provider {provider_id} not registered")

        effective_user = self.service._effective_user_id(user_id)

        try:
            # Get token bundle from vault
            bundle = self.service._vault.get_token(provider_id, user_id=effective_user)

            if not bundle:
                return None

            # Check if access token is expired
            if self._is_token_expired(bundle):
                # Try to refresh token
                refreshed_bundle = self._refresh_token(
                    provider_id,
                    bundle,
                    effective_user,
                )

                if refreshed_bundle:
                    # Store refreshed tokens
                    self.service._vault.update_access_token(provider_id, refreshed_bundle, user_id=effective_user)
                    bundle = refreshed_bundle
                else:
                    # Refresh failed
                    return None

            # Return token data
            expires_in = None
            if bundle.expires_at:
                expires_in = int((bundle.expires_at - datetime.now(UTC)).total_seconds())

            return {
                "access_token": bundle.access_token,
                "token_type": bundle.token_type or "Bearer",
                "expires_in": expires_in,
                "scope": " ".join(bundle.scopes) if bundle.scopes else None,
            }

        except Exception as e:
            logger.exception(
                "Failed to resolve access credential for provider %s: %s",
                provider_id,
                e,
            )
            return None

    def rotate_tokens(
        self,
        user_id: str | None = None,
        provider_ids: Iterable[str] | None = None,
        force: bool = False,
    ) -> Iterable[RotationOutcome]:
        """
        Rotate tokens for providers.

        Args:
            user_id: User identifier (None for all users)
            provider_ids: Provider IDs to rotate (None for all)
            force: Force rotation even if not expired

        Yields:
            Rotation outcomes
        """
        effective_user = self.service._effective_user_id(user_id)

        if provider_ids is not None:
            target_providers = list(provider_ids)
        else:
            # Get all registered provider IDs
            target_providers = [entry.metadata.provider_id for entry in providers.list_providers()]

        for provider_id in target_providers:
            try:
                outcome = self._rotate_provider_tokens(effective_user, provider_id, force)
                yield outcome

            except Exception as e:
                logger.exception("Failed to rotate credentials for provider %s: %s", provider_id, e)
                yield RotationOutcome(
                    provider_id=provider_id,
                    refreshed=False,
                    reason="rotation_failed",
                    expires_at=None,
                    error=str(e),
                )

    def _is_token_expired(self, bundle: TokenBundle) -> bool:
        """
        Check if a token bundle is expired.

        Args:
            bundle: Token bundle

        Returns:
            True if expired
        """
        if not bundle.expires_at:
            return False

        # Add buffer time (5 minutes) to prevent edge cases
        buffer_time = timedelta(minutes=5)
        return datetime.now(UTC) > (bundle.expires_at - buffer_time)

    def _refresh_token(
        self,
        provider_id: str,
        bundle: TokenBundle,
        user_id: str,
    ) -> TokenBundle | None:
        """
        Refresh an access token.

        Args:
            provider_id: Provider identifier
            bundle: Current token bundle

        Returns:
            Refreshed token bundle or None if refresh failed
        """
        try:
            if not bundle.refresh_token:
                return None

            adapter = providers.get_provider(provider_id)
            if not adapter:
                return None

            # Refresh token
            with user_scope(user_id):
                new_bundle = adapter.refresh_token(bundle.refresh_token)

            if new_bundle:
                logger.info("Refreshed access credential for provider %s", provider_id)
                return new_bundle
            else:
                logger.warning("Refresh failed for provider %s", provider_id)
                return None

        except Exception as e:
            logger.exception("Refresh failed for provider %s: %s", provider_id, e)
            return None

    def _rotate_provider_tokens(self, user_id: str | None, provider_id: str, force: bool) -> RotationOutcome:
        """
        Rotate tokens for a specific provider.

        Args:
            user_id: User identifier
            provider_id: Provider identifier
            force: Force rotation

        Returns:
            Rotation outcome
        """
        try:
            effective_user = self.service._effective_user_id(user_id)

            # Get current bundle
            bundle = self.service._vault.get_token(provider_id, user_id=effective_user)

            if not bundle:
                return RotationOutcome(
                    provider_id=provider_id,
                    refreshed=False,
                    reason="no_tokens",
                    expires_at=None,
                    error="No tokens found for provider",
                )

            # Check if rotation is needed
            needs_rotation = force or self._is_token_expired(bundle)

            if not needs_rotation:
                return RotationOutcome(
                    provider_id=provider_id,
                    refreshed=True,
                    reason="tokens_valid",
                    expires_at=bundle.expires_at,
                )

            # Rotate token
            if bundle.refresh_token:
                new_bundle = self._refresh_token(provider_id, bundle, effective_user)

                if new_bundle:
                    # Store new tokens
                    self.service._vault.update_access_token(provider_id, new_bundle, user_id=effective_user)

                    # Reset dead-token counter on success
                    try:
                        from services.oauth.dead_token_detector import get_dead_token_detector

                        get_dead_token_detector().record_success(effective_user, provider_id)
                    except Exception:
                        pass  # Non-blocking

                    return RotationOutcome(
                        provider_id=provider_id,
                        refreshed=True,
                        reason="rotation_success",
                        expires_at=new_bundle.expires_at,
                    )
                else:
                    # Track consecutive failures for dead token detection
                    try:
                        from services.oauth.dead_token_detector import get_dead_token_detector

                        get_dead_token_detector().record_failure(
                            effective_user, provider_id, "Token refresh returned None"
                        )
                    except Exception:
                        pass  # Non-blocking

                    return RotationOutcome(
                        provider_id=provider_id,
                        refreshed=False,
                        reason="rotation_failed",
                        expires_at=None,
                        error="Token refresh failed",
                    )
            else:
                return RotationOutcome(
                    provider_id=provider_id,
                    refreshed=False,
                    reason="no_refresh_token",
                    expires_at=None,
                    error="No refresh token available",
                )

        except Exception as e:
            logger.exception("Rotation failed for provider %s: %s", provider_id, e)
            return RotationOutcome(
                provider_id=provider_id,
                refreshed=False,
                reason="exception",
                expires_at=None,
                error=str(e),
            )
