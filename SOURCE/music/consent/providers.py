"""
Provider registry and interfaces for OAuth-capable music integrations.

Batch B orchestrates the consent workflow but delegates provider-specific logic
to adapters implemented in Batch A.  This module exposes the registry and the
protocol that adapters must satisfy.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from core.logging_config import get_logger

from .models import ProviderCapability, ProviderMetadata, TokenBundle

logger = get_logger("viola.music.consent.providers")


class OAuthProviderAdapter(Protocol):
    """
    Adapter contract for production music providers.

    Implementations live in Batch A (one per provider) and are responsible for
    generating authorization URLs, exchanging auth codes for tokens, refreshing
    access tokens, and revoking credentials when the user withdraws consent.
    """

    provider_id: str
    display_name: str

    @property
    def metadata(self) -> ProviderMetadata: ...

    def authorization_url(self, *, redirect_uri: str, state: str, scopes: Sequence[str] | None = None) -> str: ...

    def exchange_code(self, *, code: str, redirect_uri: str, state: str | None = None) -> TokenBundle: ...

    def refresh_token(self, refresh_token: str) -> TokenBundle: ...

    def revoke(self, refresh_token: str) -> None: ...

    def capability(self) -> ProviderCapability: ...


@dataclass
class RegisteredProvider:
    adapter: OAuthProviderAdapter
    metadata: ProviderMetadata


# Backward-compatibility alias expected by tests
ProviderEntry = RegisteredProvider


_registry: dict[str, RegisteredProvider] = {}
_AUTO_IMPORT_CANDIDATES = (
    "music.consent.adapters.youtube_music",  # Disabled compatibility adapter; browser-auth is authoritative.
    "music.consent.adapters.google_calendar",  # Google Calendar OAuth adapter
    "music.consent.adapters.microsoft_calendar",  # Microsoft Calendar OAuth adapter
    "music.consent.adapters.spotify",  # Spotify OAuth adapter
    "music.providers.spotify",
    "music.providers.youtube_music",
    "music.providers.calendar",
)
_AUTO_IMPORT_RAN = False


def register_provider(adapter: OAuthProviderAdapter) -> None:
    """Register an OAuth provider adapter."""
    provider_id = adapter.provider_id.lower()
    metadata = adapter.metadata
    _registry[provider_id] = RegisteredProvider(adapter=adapter, metadata=metadata)
    logger.info("Registered consent provider %s (%s)", provider_id, metadata.display_name)


def deregister_provider(provider_id: str) -> None:
    _registry.pop(provider_id.lower(), None)
    logger.info("Deregistered consent provider %s", provider_id)


def get_provider(provider_id: str) -> OAuthProviderAdapter | None:
    auto_register_builtin_providers()
    entry = _registry.get(provider_id.lower())
    return entry.adapter if entry else None


def get_metadata(provider_id: str) -> ProviderMetadata | None:
    auto_register_builtin_providers()
    entry = _registry.get(provider_id.lower())
    return entry.metadata if entry else None


def list_providers() -> Iterable[RegisteredProvider]:
    auto_register_builtin_providers()
    return tuple(_registry.values())


def auto_register_builtin_providers(force: bool = False) -> None:
    global _AUTO_IMPORT_RAN
    if _AUTO_IMPORT_RAN and not force:
        return
    _AUTO_IMPORT_RAN = True

    for module_name in _AUTO_IMPORT_CANDIDATES:
        try:
            importlib.import_module(module_name)
            logger.debug("Auto-loaded consent provider module %s", module_name)
        except ModuleNotFoundError:
            logger.debug("Consent provider module %s not present; skipping", module_name)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to auto-import %s: %s", module_name, exc)


def build_placeholder_metadata(
    provider_id: str, display_name: str, is_music_provider: bool = False
) -> ProviderMetadata:
    """
    Helper to expose providers before Batch A adapters land.

    These placeholders allow the UI to show planned providers with a
    NOT_LINKED state even when the adapter implementation is unavailable.
    """

    return ProviderMetadata(
        provider_id=provider_id,
        display_name=display_name,
        scopes=(),
        capability=ProviderCapability(),
        can_rotate=False,
        notes="Adapter not registered yet",
        is_music_provider=is_music_provider,
    )


def ensure_placeholder_providers() -> None:
    """
    Ensure that current launch providers plus calendar providers appear in the
    registry even when adapters are not yet available.

    Note: YouTube Music is browser-auth only; the registered consent adapter is
    disabled compatibility metadata, not a live OAuth path.
    """
    # Auto-register OAuth adapters first
    auto_register_builtin_providers()

    placeholders = (
        ("spotify", "Spotify", True),  # (provider_id, display_name, is_music_provider)
        ("google_calendar", "Google Calendar", False),
        ("microsoft_calendar", "Microsoft Calendar", False),
    )

    for provider_id, display_name, is_music in placeholders:
        if provider_id in _registry:
            continue
        metadata = build_placeholder_metadata(provider_id, display_name)
        metadata.is_music_provider = is_music
        metadata.notes = "Not implemented yet"  # Be honest about status
        placeholder: Any = _PlaceholderAdapter(metadata=metadata)  # Placeholder doesn't fully implement protocol
        _registry[provider_id] = RegisteredProvider(adapter=placeholder, metadata=metadata)


class _PlaceholderAdapter:
    """
    Minimal adapter used until production adapters are registered.

    All operations raise RuntimeError to make it obvious that the provider is
    not ready for linking yet.
    """

    def __init__(self, metadata: ProviderMetadata):
        self.metadata = metadata
        self.provider_id = metadata.provider_id
        self.display_name = metadata.display_name

    def capability(self) -> ProviderCapability:
        return self.metadata.capability

    def authorization_url(self, **_: str) -> str:
        raise RuntimeError(f"Provider '{self.provider_id}' is not yet available")

    def exchange_code(self, **_: str) -> TokenBundle:
        raise RuntimeError(f"Provider '{self.provider_id}' is not yet available")

    def refresh_token(self, _refresh_token: str) -> TokenBundle:
        raise RuntimeError(f"Provider '{self.provider_id}' is not yet available")

    def revoke(self, _refresh_token: str) -> None:
        raise RuntimeError(f"Provider '{self.provider_id}' is not yet available")
