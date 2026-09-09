"""
Google Calendar OAuth provider adapter.

Uses the app's Google OAuth 2.0 credentials with calendar-specific scopes.
This enables the Account tab "Connect Calendar" flow to generate a real
authorization URL and exchange tokens.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from config.settings import get_runtime_base_url
from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger
from music.consent import providers as consent_providers
from music.consent.models import ProviderCapability, ProviderMetadata, TokenBundle
from music.consent.provider_config import OAuthClientConfig, get_provider_config_loader

logger = get_logger(__name__)

# Google OAuth 2.0 endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Google Calendar API scopes
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_RW_SCOPE = "https://www.googleapis.com/auth/calendar.events"

DEFAULT_SCOPES = (
    CALENDAR_READONLY_SCOPE,
    CALENDAR_EVENTS_RW_SCOPE,
)


class GoogleCalendarOAuthAdapter:
    """OAuth adapter for Google Calendar using Google OAuth 2.0."""

    provider_id = "google_calendar"
    display_name = "Google Calendar"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        config_loader=None,
        user_id: str | None = None,
    ):
        self._config_loader = config_loader or get_provider_config_loader()
        self._config_user_id = user_id
        oauth_config: OAuthClientConfig | None = None

        if not client_id and not client_secret:
            oauth_config = self._load_oauth_config()

        self._client_id = client_id or (oauth_config.client_id if oauth_config else None)
        self._client_secret = client_secret or (oauth_config.client_secret if oauth_config else None)

        if redirect_uri:
            self._default_redirect_uri = redirect_uri
        elif oauth_config and oauth_config.redirect_uri:
            self._default_redirect_uri = oauth_config.redirect_uri
        else:
            runtime_base_url = get_runtime_base_url()
            self._default_redirect_uri = f"{runtime_base_url}/v1/consent/callback"

        self._config_source = (
            "parameter" if (client_id or client_secret) else (oauth_config.source if oauth_config else "none")
        )

        logger.info(
            "Google Calendar OAuth config: client_id_present=%s, secret_present=%s, source=%s",
            bool(self._client_id),
            bool(self._client_secret),
            self._config_source,
        )

    def _load_oauth_config(self) -> OAuthClientConfig | None:
        if self._config_user_id:
            return self._config_loader.load_oauth_config(
                "google_calendar",
                user_id=self._config_user_id,
            )
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            return None
        return self._config_loader.load_oauth_config(
            "google_calendar",
            user_id=user_id,
        )

    def _ensure_oauth_config_loaded(self) -> None:
        if self._client_id and self._client_secret:
            return
        oauth_config = self._load_oauth_config()
        if oauth_config is None:
            return
        self._client_id = self._client_id or oauth_config.client_id
        self._client_secret = self._client_secret or oauth_config.client_secret
        if not getattr(self, "_default_redirect_uri", None):
            self._default_redirect_uri = oauth_config.redirect_uri
        self._config_source = oauth_config.source

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            display_name=self.display_name,
            scopes=DEFAULT_SCOPES,
            default_redirect_uri=self._default_redirect_uri,
            capability=ProviderCapability(
                supports_offline=True,
                max_bitrate_kbps=None,
                supports_lyrics=False,
                extras={"calendar_provider": True},
            ),
            can_rotate=True,
            notes="Google Calendar read-write access via OAuth 2.0.",
            is_music_provider=False,
        )

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str] | None = None) -> str:
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            raise RuntimeError(
                "Google Calendar OAuth client ID not configured. "
                "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env."
            )

        logger.info("Calendar OAuth scopes updated to read-write; existing users must re-authorize")
        effective_scopes = scopes or DEFAULT_SCOPES
        scope_string = " ".join(effective_scopes)

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope_string,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }

        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        logger.info(
            "Generated Google Calendar authorization URL (redirect_uri=%s, scopes=%s)",
            redirect_uri,
            scope_string,
        )
        return auth_url

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError("Google Calendar OAuth credentials not configured")

        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=TIMEOUT_EXTENDED,
        )
        response.raise_for_status()
        data = response.json()

        expires_in = data.get("expires_in", 3600)
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(),
        )

    def refresh_token(self, refresh_token_str: str) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError("Google Calendar OAuth credentials not configured")

        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token_str,
                "grant_type": "refresh_token",
            },
            timeout=TIMEOUT_EXTENDED,
        )
        response.raise_for_status()
        data = response.json()

        expires_in = data.get("expires_in", 3600)
        return TokenBundle(
            access_token=data["access_token"],
            refresh_token=refresh_token_str,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(),
        )

    def revoke(self, token: str) -> None:
        httpx.post(
            GOOGLE_REVOKE_URL,
            params={"token": token},
            timeout=TIMEOUT_EXTENDED,
        )

    def get_scopes(self) -> list[str]:
        return list(DEFAULT_SCOPES)

    def get_authorization_url(self) -> str | None:
        """Get a pre-built authorization URL (used by session manager)."""
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            return None
        try:
            runtime_base_url = get_runtime_base_url()
            redirect_uri = f"{runtime_base_url}/v1/consent/callback"
            return self.authorization_url(
                redirect_uri=redirect_uri,
                state="placeholder",
                scopes=DEFAULT_SCOPES,
            )
        except Exception:
            return None


def _auto_register() -> None:
    """Auto-register Google Calendar adapter on module import."""
    adapter = GoogleCalendarOAuthAdapter()
    consent_providers.register_provider(adapter)


_auto_register()
