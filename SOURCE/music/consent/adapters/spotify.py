"""
Spotify OAuth provider adapter.

This adapter implements the OAuthProviderAdapter protocol for Spotify,
using Spotify Authorization Code flows. It handles authorization URL
generation, code exchange, token refresh, and graceful revocation handling.
"""

from __future__ import annotations

import base64
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

logger = get_logger("viola.music.consent.adapters.spotify")


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

DEFAULT_SCOPES = (
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "streaming",
    "playlist-read-private",
    "user-library-read",
    "user-library-modify",
)


class SpotifyOAuthAdapter:
    """
    OAuth adapter for Spotify using the Authorization Code flow.

    This adapter handles:
    - Building authorization URLs
    - Exchanging authorization codes for tokens
    - Refreshing access tokens
    - Graceful revocation handling (Spotify has no revoke endpoint)
    """

    provider_id = "spotify"
    display_name = "Spotify"

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
            "Spotify OAuth config: client_id_present=%s, secret_present=%s, source=%s",
            bool(self._client_id),
            bool(self._client_secret),
            self._config_source,
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
            "spotify",
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
            provider_id="spotify",
            display_name="Spotify",
            scopes=DEFAULT_SCOPES,
            default_redirect_uri=self._default_redirect_uri,
            capability=ProviderCapability(
                supports_offline=True,
                max_bitrate_kbps=320,
                supports_lyrics=True,
                extras={"requires_premium": True, "drm": True},
            ),
            can_rotate=True,
            notes="Uses Spotify Authorization Code flow. Requires Premium for playback.",
            is_music_provider=True,
        )

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str] | None = None) -> str:
        self._ensure_oauth_config_loaded()
        if not self._client_id:
            raise RuntimeError(
                "Spotify OAuth client ID not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        effective_scopes = scopes or DEFAULT_SCOPES
        scope_string = " ".join(effective_scopes)

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope_string,
            "state": state,
            "show_dialog": "true",
        }

        auth_url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"
        logger.info(
            "Generated Spotify authorization URL (redirect_uri=%s, scopes=%s)",
            redirect_uri,
            scope_string,
        )
        return auth_url

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "Spotify OAuth credentials not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        logger.info(
            "Exchanging Spotify authorization code (code_length=%d, redirect_uri=%s)",
            len(code),
            redirect_uri,
        )

        auth_bytes = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_bytes}"}
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }

        try:
            with httpx.Client(timeout=TIMEOUT_EXTENDED) as client:
                response = client.post(SPOTIFY_TOKEN_URL, data=token_data, headers=headers)
                if response.status_code >= 400:
                    error_detail = "Unknown error"
                    try:
                        error_json = response.json()
                        error_detail = error_json.get("error_description") or error_json.get(
                            "error", "Invalid authorization code"
                        )
                        if "invalid_grant" in error_detail.lower() or "invalid" in error_detail.lower():
                            error_detail = (
                                "Invalid authorization code. The code may have expired or already been used. "
                                "Please get a new code."
                            )
                        elif "redirect_uri" in error_detail.lower():
                            error_detail = "Redirect URI mismatch. Please check OAuth configuration."
                    except Exception:
                        error_detail = "HTTP %d: %s" % (
                            response.status_code,
                            (response.text[:200] if hasattr(response, "text") else "Unknown error"),
                        )
                    logger.error(
                        "HTTP error exchanging Spotify authorization code: %s",
                        error_detail,
                    )
                    raise RuntimeError("OAuth error: %s" % error_detail)
                response.raise_for_status()
                token_response = response.json()
        except RuntimeError:
            raise
        except httpx.TimeoutException as exc:
            logger.error("Timeout exchanging Spotify authorization code: %s", exc)
            raise RuntimeError(
                "Network timeout: Failed to connect to Spotify's OAuth service. Please check your internet "
                "connection and try again."
            ) from exc
        except httpx.ConnectError as exc:
            logger.error("Connection error exchanging Spotify authorization code: %s", exc)
            raise RuntimeError(
                "Network error: Could not connect to Spotify's OAuth service. Please check your internet "
                "connection and try again."
            ) from exc
        except httpx.RequestError as exc:
            logger.error("Failed to exchange Spotify authorization code: %s", exc)
            raise RuntimeError("Network error: Failed to exchange authorization code. %s" % exc) from exc

        access_token = token_response.get("access_token")
        refresh_token = token_response.get("refresh_token")
        expires_in = token_response.get("expires_in", 3600)
        token_type = token_response.get("token_type", "Bearer")
        scope_string = token_response.get("scope", "")

        if not refresh_token:
            logger.warning("Spotify OAuth exchange did not return refresh credential")

        if not access_token:
            raise RuntimeError("Token exchange response missing access_token")

        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        scopes = tuple(scope_string.split()) if scope_string else DEFAULT_SCOPES

        logger.info(
            "Successfully exchanged Spotify authorization code (expires_in=%s, has_refresh_credential=%s, scopes=%s)",
            expires_in,
            bool(refresh_token),
            scope_string,
        )

        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token or "",
            expires_at=expires_at,
            scopes=scopes,
            token_type=token_type,
            metadata={"provider": "spotify"},
        )

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        self._ensure_oauth_config_loaded()
        if not self._client_id or not self._client_secret:
            raise RuntimeError(
                "Spotify OAuth credentials not configured. "
                "Configure OAuth credentials via environment variables or token vault."
            )

        logger.debug("Refreshing Spotify access credential")

        auth_bytes = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        headers = {"Authorization": f"Basic {auth_bytes}"}
        token_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        try:
            with httpx.Client(timeout=TIMEOUT_EXTENDED) as client:
                response = client.post(SPOTIFY_TOKEN_URL, data=token_data, headers=headers)
                if response.status_code >= 400:
                    try:
                        error_body = response.json()
                    except Exception:
                        error_body = {"raw": response.text[:500]}
                    logger.error(
                        "Spotify token refresh failed: status=%d error=%s description=%s",
                        response.status_code,
                        error_body.get("error", "unknown"),
                        error_body.get("error_description", "no description"),
                    )
                    raise RuntimeError(
                        "Token refresh failed: %s - %s"
                        % (
                            error_body.get("error", "HTTP %d" % response.status_code),
                            error_body.get("error_description", response.text[:200]),
                        )
                    )
                token_response = response.json()
        except RuntimeError:
            raise
        except httpx.RequestError as exc:
            logger.error("Failed to refresh Spotify access credential: %s", exc)
            raise RuntimeError("Token refresh failed: %s" % exc) from exc

        access_token = token_response.get("access_token")
        expires_in = token_response.get("expires_in", 3600)
        token_type = token_response.get("token_type", "Bearer")
        scope_string = token_response.get("scope", "")

        if not access_token:
            raise RuntimeError("Token refresh response missing access_token")

        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        scopes = tuple(scope_string.split()) if scope_string else DEFAULT_SCOPES

        logger.debug(
            "Successfully refreshed Spotify access credential (expires_in=%s, scopes=%s)",
            expires_in,
            scope_string,
        )

        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes,
            token_type=token_type,
            metadata={"provider": "spotify"},
        )

    def revoke(self, refresh_token: str) -> None:
        logger.warning(
            "Spotify does not provide an official token revoke endpoint; treating token as locally revoked only"
        )

    def evaluate_oauth_config(self, user_id: str) -> tuple[str, str | None]:
        from music.consent.models import ProviderLinkState

        if not self._client_id or not self._client_secret:
            oauth_config = self._load_oauth_config(user_id)
            if oauth_config:
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
                reason = f"missing {', '.join(missing)}"
                return (ProviderLinkState.UNAVAILABLE.value, reason)

        return (ProviderLinkState.NOT_LINKED.value, None)

    def capability(self) -> ProviderCapability:
        return self.metadata.capability


try:
    _adapter = SpotifyOAuthAdapter()
    consent_providers.register_provider(_adapter)
    logger.info("Registered Spotify OAuth adapter")
except Exception as exc:
    logger.warning("Failed to auto-register Spotify OAuth adapter: %s", exc)
