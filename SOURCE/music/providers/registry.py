"""
Provider registry utilities.

Adapters register themselves via :func:`register_provider`, allowing the
application to look up provider classes dynamically.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.logging_config import get_logger

from .base import MusicProvider
from .errors import ProviderNotConfiguredError, ProviderNotSupportedError
from .models import ProviderName

logger = get_logger(__name__)

ProviderFactory = type[MusicProvider]

_REGISTRY: dict[ProviderName, ProviderFactory] = {}


class RegistryProviderNotFound(ProviderNotSupportedError):
    """Raised when a provider lookup fails in the provider registry.

    Note: This is distinct from music.consent.exceptions.ConsentProviderNotRegistered
    and music.providers.errors.MusicProviderNotRegistered.
    """


# Backward compatibility alias - deprecated, use RegistryProviderNotFound
ProviderNotRegistered = RegistryProviderNotFound


def register_provider(
    provider_name: ProviderName,
    provider_cls: ProviderFactory,
    *,
    override: bool = False,
) -> None:
    """
    Register a provider class.

    Args:
        provider_name: Identifier being registered.
        provider_cls: Concrete subclass of :class:`MusicProvider`.
        override: Whether to replace an existing registration.
    """

    if not override and provider_name in _REGISTRY:
        raise ProviderNotConfiguredError(
            f"Provider '{provider_name.value}' already registered. Pass override=True to replace.",
            provider_name=provider_name.value,
        )
    _REGISTRY[provider_name] = provider_cls


def get_provider_class(provider_name: ProviderName) -> ProviderFactory:
    """Return the provider class for ``provider_name``."""

    try:
        return _REGISTRY[provider_name]
    except KeyError as exc:
        raise ProviderNotRegistered(
            f"Provider '{provider_name.value}' is not registered.",
            provider_name=provider_name.value,
        ) from exc


def iter_registered_providers() -> Iterable[ProviderName]:
    """Iterate over registered provider identifiers."""

    return tuple(_REGISTRY.keys())


def auto_register(provider_name: ProviderName):
    """
    Class decorator to register a provider at import time.
    """

    def decorator(cls: ProviderFactory) -> ProviderFactory:
        register_provider(provider_name, cls, override=True)
        return cls

    return decorator


def get_active_provider() -> MusicProvider | None:
    """
    Get the currently active music provider instance.

    Returns:
        Active provider instance or None if no provider is active/linked
    """
    try:
        from .active_provider import get_active_music_provider_id

        active_id = get_active_music_provider_id()
        if not active_id:
            return None

        # Convert string ID to ProviderName enum
        try:
            provider_name = ProviderName(active_id)
        except ValueError:
            logger.warning("Unknown provider ID: %s", active_id)
            return None

        # Get provider class and instantiate it
        provider_class = get_provider_class(provider_name)
        return provider_class()
    except Exception as exc:
        logger.warning("Failed to get active provider: %s", exc)
        return None
