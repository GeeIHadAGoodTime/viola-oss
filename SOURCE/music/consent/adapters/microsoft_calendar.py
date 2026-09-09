"""
Microsoft Calendar OAuth provider adapter backed by Microsoft Graph.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from config import env
from config.settings import get_runtime_base_url
from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger
from music.consent import providers as consent_providers
from music.consent.models import ProviderCapability, ProviderMetadata, TokenBundle
from music.consent.provider_config import OAuthClientConfig, get_provider_config_loader

logger = get_logger(__name__)

MICROSOFT_AUTHORITY_BASE_URL = "https://login.microsoftonline.com"

DEFAULT_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "Calendars.ReadWrite",
)


class MicrosoftCalendarOAuthAdapter:
    """OAuth adapter for Microsoft Graph calendar access."""

    provider_id = "microsoft_calendar"
    display_name = "Microsoft Calendar"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        tenant_id: str | None = None,
        config_loader=None,
        user_id: str | None = None,
    ) -> None:
        self._config_loader = config_loader or get_provider_config_loader()
        self._config_user_id = user_id
        oauth_config: OAuthClientConfig | None = None

        if not client_id and not client_secret:
            oauth_config = self._load_oauth_config()

        self._client_id = client_id or (oauth_config.client_id if oauth_config else None)
        self._client_secret = client_secret or (oauth_config.client_secret if oauth_config else None)
        self._tenant_id = tenant_id or env.get("VIOLA_MICROSOFT_TENANT_ID") or "common"

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
            "Microsoft Calendar OAuth config: client_id_present=%s, secret_present=%s, source=%s, tenant=%s",
            bool(self._client_id),
            bool(self._client_secret),
            self._config_source,
            self._tenant_id,
        )

    def _load_oauth_config(self, user_id: str | None = None) -> OAuthClientConfig | None:
        resolved_user_id = user_id or self._config_user_id
        if resolved_user_id is None:
            try:
                from core.user_context import get_current_user_id

                resolved_user_id = get_current_user_id()
            except LookupError:
                return None
        return self._config_loader.load_oauth_config(
            "microsoft_calendar",
            user_id=resolved_user_id,
        )

    def _ensure_oauth_config_loaded(self, user_id: str | None = None) -> None:
        if self._client_id and self._client_secret:
            return
        oauth_config = self._load_oauth_config(user_id)
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
                extras={
                    "calendar_provider": True,
                    "oauth_provider": "microsoft",
                },
            ),
            can_rotate=True,
            notes="Microsoft Graph calendar access via OAuth 2.0.",
            is_music_provider=False,
        )

    @property
    def _authorization_url(self) -> str:
        return "%s/%s/oauth2/v2.0/authorize" % (
            MICROSOFT_AUTHORITY_BASE_URL,
            self._tenant_id,
        )

    @property
    def _token_url(self) -> str:
        return "%s/%s/oauth2/v2.0/token" % (
            MICROSOFT_AUTHORITY_BASE_URL,
            self._tenant_id,
        )

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        scopes: Sequence[str] | None = None,
    ) -> str:
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            raise RuntimeError(
                "Microsoft Calendar OAuth client ID not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        effective_scopes = scopes or DEFAULT_SCOPES
        scope_string = " ".join(effective_scopes)
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": scope_string,
            "state": state,
            "prompt": "select_account",
        }

        auth_url = "%s?%s" % (self._authorization_url, urlencode(params))
        logger.info(
            "Generated Microsoft Calendar authorization URL (redirect_uri=%s, scopes=%s)",
            redirect_uri,
            scope_string,
        )
        return auth_url

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        state: str | None = None,
    ) -> TokenBundle:
        _ = state
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "Microsoft Calendar OAuth credentials not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        token_data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": " ".join(DEFAULT_SCOPES),
        }
        token_response = self._token_request(token_data)
        return self._build_token_bundle(
            token_response,
            original_refresh_token=None,
            require_refresh_token=True,
        )

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "Microsoft Calendar OAuth credentials not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        token_response = self._token_request(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(DEFAULT_SCOPES),
            }
        )
        return self._build_token_bundle(
            token_response,
            original_refresh_token=refresh_token,
            require_refresh_token=False,
        )

    def revoke(self, refresh_token: str) -> None:
        _ = refresh_token
        logger.warning(
            "Microsoft Calendar does not expose a direct OAuth token revoke endpoint; treating token as locally revoked only"
        )

    def evaluate_oauth_config(self, user_id: str) -> tuple[str, str | None]:
        from music.consent.models import ProviderLinkState

        if not self._client_id or not self._client_secret:
            oauth_config = self._load_oauth_config(user_id)
            if oauth_config is not None:
                self._client_id = oauth_config.client_id
                self._client_secret = oauth_config.client_secret
                self._default_redirect_uri = oauth_config.redirect_uri
                self._config_source = oauth_config.source
            else:
                missing = []
                if not self._client_id:
                    missing.append("client_id")
                if not self._client_secret:
                    missing.append("client_secret")
                return (ProviderLinkState.UNAVAILABLE.value, "missing %s" % ", ".join(missing))

        return (ProviderLinkState.NOT_LINKED.value, None)

    def capability(self) -> ProviderCapability:
        return self.metadata.capability

    def get_authorization_url(self) -> str | None:
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

    def _token_request(self, token_data: Mapping[str, Any]) -> dict[str, Any]:
        logger.info(
            "Microsoft Calendar token request starting (tenant=%s, grant_type=%s)",
            self._tenant_id,
            token_data.get("grant_type"),
        )
        try:
            with httpx.Client(timeout=TIMEOUT_EXTENDED) as client:
                response = client.post(self._token_url, data=dict(token_data))
                if response.status_code >= 400:
                    raise RuntimeError(self._token_error_detail(response))
                response.raise_for_status()
                return response.json()
        except RuntimeError:
            raise
        except httpx.TimeoutException as exc:
            raise RuntimeError("Network timeout: Failed to connect to Microsoft's OAuth service.") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError("Network error: Could not connect to Microsoft's OAuth service.") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("Network error: Failed to exchange Microsoft OAuth token.") from exc

    def _build_token_bundle(
        self,
        token_response: Mapping[str, Any],
        *,
        original_refresh_token: str | None,
        require_refresh_token: bool,
    ) -> TokenBundle:
        access_token = token_response.get("access_token")
        refresh_token = token_response.get("refresh_token") or original_refresh_token
        expires_in = token_response.get("expires_in", 3600)
        scope_string = token_response.get("scope", "")
        token_type = token_response.get("token_type", "Bearer")

        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Token exchange response missing access_token")
        if require_refresh_token and (not isinstance(refresh_token, str) or not refresh_token):
            raise RuntimeError("Token exchange response missing refresh_token; ensure offline_access is granted")

        scopes = tuple(scope_string.split()) if isinstance(scope_string, str) and scope_string else DEFAULT_SCOPES
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token or "",
            expires_at=expires_at,
            scopes=scopes,
            token_type=token_type if isinstance(token_type, str) else "Bearer",
            metadata={
                "provider": "microsoft_calendar",
                "tenant_id": self._tenant_id,
            },
        )

    @staticmethod
    def _token_error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            description = payload.get("error_description")
            if isinstance(description, str) and description:
                return description
            if isinstance(error, str) and error:
                return error

        return "HTTP %d: %s" % (response.status_code, response.text[:200])


try:
    _adapter = MicrosoftCalendarOAuthAdapter()
    consent_providers.register_provider(_adapter)
    logger.info("Registered Microsoft Calendar OAuth adapter")
except Exception as exc:
    logger.warning("Failed to auto-register Microsoft Calendar OAuth adapter: %s", exc)
