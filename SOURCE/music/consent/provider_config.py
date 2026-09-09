"""
Provider OAuth credentials loader.

This module provides a centralized loader for OAuth client credentials (client_id,
client_secret, redirect_uri) used by all OAuth-based providers. It implements the
PRD Provider Credentials Layer with env-override + vault support.

The loader:
- Checks environment variables first (highest priority, dev/CI override)
- Falls back to encrypted token vault for persistent product configs
- Returns None if no credentials found (provider is misconfigured)
- Never logs actual credential values (only booleans and source)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from core.logging_config import get_logger


class _SecureSettingsManager(Protocol):
    """Protocol for SecureSettingsManager to avoid import-time dependency."""

    def __init__(self, app_name: str = ..., fallback_key_file: Path | None = ...) -> None: ...

    @property
    def encryption_enabled(self) -> bool: ...

    def get_secret(self, key: str) -> str | None: ...

    def set_secret(self, key: str, value: str) -> bool: ...

    def load_from_file(self, file_path: Path) -> None: ...

    def save_to_file(self, file_path: Path) -> None: ...


_SecureSettingsManagerClass: type[_SecureSettingsManager] | None = None
SECURE_MANAGER_AVAILABLE = False

try:
    from utils.enhancements.secrets import SecureSettingsManager

    _SecureSettingsManagerClass = SecureSettingsManager
    SECURE_MANAGER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    pass


from config import env
from config.settings import get_runtime_base_url, settings

logger = get_logger("viola.music.consent.provider_config")


def _vault_secrets_path() -> Path:
    """Return the canonical vault secrets path (alongside the manifest)."""
    from core.platform import get_data_dir

    return get_data_dir() / "token_vault.secrets.json"


@dataclass(frozen=True)
class OAuthClientConfig:
    """OAuth client credentials configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    source: Literal["env", "vault"]


class ProviderConfigLoader:
    """
    Centralized loader for provider OAuth credentials.

    Loads credentials from environment variables (highest priority) or encrypted
    token vault (fallback). Never logs actual credential values.
    """

    def __init__(
        self,
        *,
        secure_manager: _SecureSettingsManager | None = None,
        default_redirect_uri: str | None = None,
    ):
        """
        Initialize provider config loader.

        Args:
            secure_manager: Optional secure settings manager (creates default if None)
            default_redirect_uri: Optional default redirect URI for OAuth flows
                (if None, uses runtime base URL)
        """
        self._secure_manager = secure_manager
        # Use provided redirect URI or compute from runtime base URL
        if default_redirect_uri:
            self._default_redirect_uri = default_redirect_uri
        else:
            runtime_base_url = get_runtime_base_url()
            self._default_redirect_uri = f"{runtime_base_url}/v1/consent/callback"
        self._secure_manager_instance: _SecureSettingsManager | None = None

    def _get_secure_manager(self) -> _SecureSettingsManager | None:
        """Get or create secure settings manager instance."""
        if self._secure_manager:
            return self._secure_manager
        if self._secure_manager_instance:
            return self._secure_manager_instance
        if SECURE_MANAGER_AVAILABLE and _SecureSettingsManagerClass is not None:
            try:
                self._secure_manager_instance = _SecureSettingsManagerClass(app_name="viola-token-vault")
                # Load existing secrets if available

                secrets_path = _vault_secrets_path()
                if secrets_path.exists() and self._secure_manager_instance is not None:
                    self._secure_manager_instance.load_from_file(secrets_path)
                return self._secure_manager_instance
            except Exception as exc:
                logger.debug(
                    "Failed to create secure settings manager for credential loading: %s",
                    exc,
                )
                return None
        return None

    def _env_var_for_provider(self, provider_id: str, kind: str) -> str | None:
        """
        Get credential value for a provider from centralized settings.

        Args:
            provider_id: Provider identifier (e.g., "youtube_music", "spotify")
            kind: Credential kind ("client_id" or "client_secret")

        Returns:
            Credential value or None if not configured
        """
        provider_id_lower = provider_id.lower()

        # Use centralized settings if available
        if settings is not None:
            # Provider-specific mappings
            if provider_id_lower == "youtube_music":
                return None
            elif provider_id_lower == "spotify":
                if kind == "client_id":
                    return settings.spotify_client_id
                elif kind == "client_secret":
                    return settings.spotify_client_secret
            elif provider_id_lower == "google_calendar":
                # Reuse the app's Google OAuth credentials.
                if kind == "client_id":
                    return settings.google_client_id
                elif kind == "client_secret":
                    return settings.google_client_secret
            elif provider_id_lower == "microsoft_calendar":
                if kind == "client_id":
                    return getattr(settings, "microsoft_client_id", None) or env.get("VIOLA_MICROSOFT_CLIENT_ID")
                elif kind == "client_secret":
                    return getattr(settings, "microsoft_client_secret", None) or env.get(
                        "VIOLA_MICROSOFT_CLIENT_SECRET"
                    )

        return None

    def _redirect_uri_for_provider(self, provider_id: str) -> str:
        """
        Get redirect URI for a provider from centralized settings.

        Args:
            provider_id: Provider identifier

        Returns:
            Redirect URI string
        """
        provider_id_lower = provider_id.lower()

        # Use centralized settings if available
        if settings is not None:
            # Provider-specific redirect URI mappings
            if provider_id_lower == "youtube_music":
                return self._default_redirect_uri
            elif provider_id_lower == "spotify":
                return settings.spotify_redirect_uri or self._default_redirect_uri
            elif provider_id_lower == "google_calendar":
                return settings.google_redirect_uri or self._default_redirect_uri
            elif provider_id_lower == "microsoft_calendar":
                return (
                    getattr(settings, "microsoft_redirect_uri", None)
                    or env.get("VIOLA_MICROSOFT_REDIRECT_URI")
                    or self._default_redirect_uri
                )

        return self._default_redirect_uri

    def _vault_key_for_provider(self, provider_id: str, kind: str, user_id: str) -> str:
        """
        Get vault key for a provider credential.

        Args:
            provider_id: Provider identifier
            kind: Credential kind ("client_id" or "client_secret")
            user_id: User ID for the credential namespace

        Returns:
            Vault key string
        """
        return f"token_vault:{user_id}:oauth.{provider_id.lower()}.{kind}"

    def load_oauth_config(self, provider_id: str, *, user_id: str) -> OAuthClientConfig | None:
        """
        Load OAuth client credentials for a provider.

        Priority:
        1. Environment variables (highest priority)
        2. Encrypted token vault (fallback)
        3. None if no credentials found (misconfigured)

        Persistence behavior:
        - If env has both client_id and client_secret AND vault has nothing/incomplete
          → persist env values to vault (seeds vault on first full pair)
        - If env has both client_id and client_secret AND vault already has credentials
          → use env values at runtime (env overrides) but do NOT overwrite vault
        - If only one of client_id/client_secret is present (env or vault) → do not persist

        Args:
            provider_id: Provider identifier (e.g., "youtube_music", "spotify")
            user_id: User ID for vault lookup

        Returns:
            OAuthClientConfig if credentials found, None otherwise

        Logs:
            Only logs booleans and source (never actual credential values)
        """
        provider_id_lower = provider_id.lower()

        # Step 1: Check environment variables (highest priority)
        env_client_id = self._env_var_for_provider(provider_id_lower, "client_id")
        env_client_secret = self._env_var_for_provider(provider_id_lower, "client_secret")

        # Get secure manager early to check vault state
        secure_manager = self._get_secure_manager()

        # If env has both credentials, check vault state and optionally persist
        if env_client_id and env_client_secret:
            # Check vault to determine if we should persist env values
            if secure_manager:
                try:
                    vault_client_id_key = self._vault_key_for_provider(provider_id_lower, "client_id", user_id)
                    vault_client_secret_key = self._vault_key_for_provider(provider_id_lower, "client_secret", user_id)

                    vault_client_id = secure_manager.get_secret(vault_client_id_key)
                    vault_client_secret = secure_manager.get_secret(vault_client_secret_key)

                    # If vault is empty or incomplete (missing either credential), persist env values
                    if not vault_client_id or not vault_client_secret:
                        # Persist env values to vault (seeds vault on first full pair)
                        try:
                            secure_manager.set_secret(vault_client_id_key, env_client_id)
                            secure_manager.set_secret(vault_client_secret_key, env_client_secret)
                            # Save to file if using default instance (not injected for tests)
                            # Only auto-save if we created the manager ourselves (not injected)
                            if self._secure_manager is None:
                                # We're using our own instance - save to default location

                                secrets_path = _vault_secrets_path()
                                try:
                                    secure_manager.save_to_file(secrets_path)
                                except Exception as e:
                                    logger.debug(
                                        "Credential vault save failed (non-critical): %s",
                                        e,
                                    )
                            # If secure_manager was injected (tests), caller handles saving
                            logger.info(
                                "%s OAuth: persisted env credentials to vault (client_id_present=True, secret_present=True)",
                                provider_id_lower,
                            )
                        except Exception as exc:
                            # Log but don't fail - env override still works
                            logger.debug(
                                "Failed to persist env credentials to vault for %s: %s",
                                provider_id_lower,
                                exc,
                            )
                    # If vault already has credentials, do NOT overwrite (env is pure override)
                except Exception as exc:
                    logger.debug("Failed to check vault state for %s: %s", provider_id_lower, exc)

            logger.info(
                "%s OAuth: loaded credentials from env (client_id_present=True, secret_present=True)",
                provider_id_lower,
            )
            return OAuthClientConfig(
                client_id=env_client_id,
                client_secret=env_client_secret,
                redirect_uri=self._redirect_uri_for_provider(provider_id_lower),
                source="env",
            )

        # Step 2: Check encrypted token vault (fallback)
        if secure_manager:
            try:
                vault_client_id_key = self._vault_key_for_provider(provider_id_lower, "client_id", user_id)
                vault_client_secret_key = self._vault_key_for_provider(provider_id_lower, "client_secret", user_id)

                vault_client_id = secure_manager.get_secret(vault_client_id_key)
                vault_client_secret = secure_manager.get_secret(vault_client_secret_key)

                if vault_client_id and vault_client_secret:
                    logger.info(
                        "%s OAuth: loaded credentials from vault (client_id_present=True, secret_present=True)",
                        provider_id_lower,
                    )
                    return OAuthClientConfig(
                        client_id=vault_client_id,
                        client_secret=vault_client_secret,
                        redirect_uri=self._redirect_uri_for_provider(provider_id_lower),
                        source="vault",
                    )
            except Exception as exc:
                logger.debug(
                    "Failed to load credentials from vault for %s: %s",
                    provider_id_lower,
                    exc,
                )

        # Step 3: No credentials found (misconfigured)
        # Check if we have partial credentials (only one of client_id/client_secret)
        has_partial = (env_client_id and not env_client_secret) or (env_client_secret and not env_client_id)
        if has_partial:
            logger.info(
                "%s OAuth: partial credentials found (missing either client_id or client_secret, source=none)",
                provider_id_lower,
            )
        else:
            logger.info(
                "%s OAuth: no credentials found (client_id_present=False, secret_present=False, source=none)",
                provider_id_lower,
            )
        return None


# Global singleton instance (created on first use)
_loader_instance: ProviderConfigLoader | None = None


def get_provider_config_loader() -> ProviderConfigLoader:
    """
    Get the global ProviderConfigLoader instance.

    Returns:
        ProviderConfigLoader singleton
    """
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = ProviderConfigLoader()
    return _loader_instance
