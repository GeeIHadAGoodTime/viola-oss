"""
Microsoft Graph calendar token resolution backed by the consent vault.
"""

from __future__ import annotations

import asyncio

from core.logging_config import get_logger

logger = get_logger(__name__)


class MicrosoftGraphAuthError(RuntimeError):
    """Raised when a Microsoft Graph access token cannot be resolved."""


class MicrosoftGraphTokenResolver:
    """Resolve and refresh Microsoft Graph access tokens from the consent vault."""

    def __init__(
        self,
        *,
        user_id: str,
        adapter=None,
        consent_service=None,
        refresh_skew_seconds: int = 300,
    ) -> None:
        if not user_id:
            raise ValueError("user_id is required")
        self._user_id = user_id
        self._adapter = adapter
        self._consent_service = consent_service
        self._refresh_skew_seconds = refresh_skew_seconds

    def _get_consent_service(self):
        if self._consent_service is not None:
            return self._consent_service
        from music.consent import get_consent_service

        self._consent_service = get_consent_service()
        return self._consent_service

    def _get_adapter(self):
        if self._adapter is not None:
            return self._adapter
        from music.consent.adapters.microsoft_calendar import MicrosoftCalendarOAuthAdapter

        self._adapter = MicrosoftCalendarOAuthAdapter(user_id=self._user_id)
        return self._adapter

    async def resolve_access_token(self, *, force_refresh: bool = False) -> str:
        """Resolve a valid access token, refreshing through the adapter if needed."""
        service = self._get_consent_service()
        bundle = service._vault.get_token("microsoft_calendar", user_id=self._user_id)
        if bundle is None or not bundle.refresh_token:
            raise MicrosoftGraphAuthError("Microsoft Calendar is not linked for this user")

        needs_refresh = (
            force_refresh or not bundle.access_token or bundle.is_expired(skew_seconds=self._refresh_skew_seconds)
        )
        if not needs_refresh and bundle.access_token:
            return bundle.access_token

        logger.info(
            "Refreshing Microsoft Graph access token for user %s",
            self._user_id,
        )
        try:
            refreshed_bundle = await asyncio.to_thread(
                self._get_adapter().refresh_token,
                bundle.refresh_token,
            )
        except Exception as exc:
            raise MicrosoftGraphAuthError("Failed to refresh Microsoft Graph access token") from exc

        if not refreshed_bundle.access_token:
            raise MicrosoftGraphAuthError("Microsoft Graph refresh did not return an access token")

        service._vault.update_access_token(
            "microsoft_calendar",
            refreshed_bundle,
            user_id=self._user_id,
        )
        return refreshed_bundle.access_token
