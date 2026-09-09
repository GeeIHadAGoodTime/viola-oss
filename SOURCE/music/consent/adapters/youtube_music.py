"""Disabled YouTube Music OAuth adapter.

YouTube Music launches through the browser-auth surface, not Google OAuth or
YouTube API scopes. This module stays import-compatible so older consent
registry callers fail closed instead of reviving token exchange paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.logging_config import get_logger
from music.consent import providers as consent_providers
from music.consent.models import ProviderCapability, ProviderMetadata, TokenBundle

logger = get_logger("viola.music.consent.adapters.youtube_music")

DEFAULT_SCOPES: tuple[str, ...] = ()
_DISABLED_MESSAGE = (
    "YouTube Music OAuth is disabled for launch. Use the browser-auth provider " "at /v1/browser/auth instead."
)


class YouTubeMusicOAuthAdapter:
    """Import-compatible adapter that blocks all Google OAuth token operations."""

    provider_id = "youtube_music"
    display_name = "YouTube Music"

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        config_loader=None,
        user_id: str | None = None,
    ) -> None:
        _ = (client_id, client_secret, redirect_uri, config_loader, user_id)

    @property
    def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id=self.provider_id,
            display_name=self.display_name,
            scopes=DEFAULT_SCOPES,
            default_redirect_uri=None,
            capability=ProviderCapability(
                supports_offline=False,
                max_bitrate_kbps=None,
                supports_lyrics=False,
                extras={"requires_embedded_player": True, "browser_auth_only": True},
            ),
            can_rotate=False,
            notes="Browser-auth only; Google OAuth and YouTube API scopes are disabled.",
            is_music_provider=True,
        )

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str] | None = None) -> str:
        _ = (redirect_uri, state, scopes)
        raise RuntimeError(_DISABLED_MESSAGE)

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None) -> TokenBundle:
        _ = (code, redirect_uri, state)
        raise RuntimeError(_DISABLED_MESSAGE)

    def refresh_token(self, refresh_token: str) -> TokenBundle:
        _ = refresh_token
        raise RuntimeError(_DISABLED_MESSAGE)

    def revoke(self, refresh_token: str) -> None:
        _ = refresh_token
        logger.info("YouTube Music OAuth revoke ignored because OAuth is disabled")

    def evaluate_oauth_config(self, user_id: str) -> tuple[str, str | None]:
        _ = user_id
        return ("unavailable", "browser_auth_only")

    def capability(self) -> ProviderCapability:
        return self.metadata.capability


try:
    _adapter = YouTubeMusicOAuthAdapter()
    consent_providers.register_provider(_adapter)
    logger.info("Registered disabled YouTube Music OAuth adapter")
except Exception as exc:
    logger.warning("Failed to register disabled YouTube Music OAuth adapter: %s", exc)
