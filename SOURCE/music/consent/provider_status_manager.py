"""
Provider Status Manager - Handles provider status listing and formatting.

Extracted from music/consent/service.py to reduce LOC violations.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.logging_config import get_logger
from core.user_context import user_scope
from music.consent import providers
from music.consent.models import ProviderLinkState

from .models import ProviderStatus

logger = get_logger(__name__)


class ProviderStatusManager:
    """Handles provider status listing and formatting operations."""

    def __init__(self, service_instance):
        self.service = service_instance

    def list_statuses(self, user_id: str) -> Iterable[ProviderStatus]:
        """
        List provider statuses for a user.

        This method handles the complex logic of gathering provider information,
        checking vault records, determining active providers, and generating
        authorization URLs when needed.

        Args:
            user_id: User identifier

        Yields:
            ProviderStatus objects with comprehensive provider information
        """
        known_providers = {entry.metadata.provider_id: entry for entry in providers.list_providers()}

        # Safely get vault records - handle errors gracefully
        try:
            vault_records = {
                record.provider_id: record for record in self.service._vault.list_providers(user_id=user_id)
            }
        except Exception as exc:
            logger.warning("Failed to list vault providers for user %s: %s", user_id, exc)
            vault_records = {}

        active_music_provider_id = self.service._get_active_music_provider_id(user_id)

        for provider_id, entry in known_providers.items():
            metadata = entry.metadata
            record = vault_records.get(provider_id)

            # Extract status information
            if record:
                state = record.status
                last_linked = record.linked_at
                expires_at = record.expires_at
                scopes = record.scopes
                last_error = record.last_error
            else:
                state = ProviderLinkState.NOT_LINKED
                last_linked = None
                expires_at = None
                scopes = tuple(metadata.scopes)
                last_error = None

            # Determine if this is the active music provider
            is_active_music_provider = (
                metadata.is_music_provider
                and provider_id == active_music_provider_id
                and state == ProviderLinkState.LINKED
            )

            # Generate authorization URL if needed
            authorization_url = self._generate_authorization_url(
                entry,
                provider_id,
                state,
                user_id,
            )

            yield ProviderStatus(
                provider_id=provider_id,
                display_name=metadata.display_name,
                state=state,
                is_music_provider=metadata.is_music_provider,
                is_active_music_provider=is_active_music_provider,
                last_linked_at=last_linked,
                expires_at=expires_at,
                scopes=scopes,
                capability=getattr(metadata, "capability", None),
                authorization_url=authorization_url,
                last_error=last_error,
            )

    def _generate_authorization_url(
        self,
        entry,
        provider_id: str,
        state: ProviderLinkState,
        user_id: str,
    ) -> str | None:
        """Generate authorization URL for a provider if appropriate."""
        try:
            adapter = entry.adapter
            if adapter is None:
                return None

            with user_scope(user_id):
                # For YouTube Music, check config status
                if provider_id == "youtube_music" and hasattr(
                    adapter,
                    "evaluate_oauth_config",
                ):
                    try:
                        config_status, _config_reason = adapter.evaluate_oauth_config(user_id)
                        if config_status == ProviderLinkState.UNAVAILABLE.value:
                            return None
                    except Exception as exc:
                        logger.debug(
                            "YouTube Music config evaluation failed: %s",
                            exc,
                        )

                # Generate URL if provider supports it
                if hasattr(adapter, "get_authorization_url"):
                    try:
                        return adapter.get_authorization_url()
                    except Exception as exc:
                        logger.debug(
                            "Failed to generate authorization URL for %s: %s",
                            provider_id,
                            exc,
                        )

                # Adapter uses authorization_url(redirect_uri=, state=) — requires
                # session params so we can't generate a real URL at listing time.
                # Return the session endpoint path to signal OAuth is available;
                # the UI drives the real flow via POST /v1/consent/session.
                if hasattr(adapter, "authorization_url") and callable(getattr(adapter, "authorization_url", None)):
                    return "/v1/consent/session"

        except Exception as exc:
            logger.debug("Authorization URL generation failed for %s: %s", provider_id, exc)

        return None
