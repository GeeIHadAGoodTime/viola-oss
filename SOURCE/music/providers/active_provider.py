"""
Active music provider management and validation.

AI Instructions
===============
This module enforces the single-active-provider model. Use this for validating
provider selection in music intents. Provider-specific logic must stay in
music/providers/ directory only (per PRD §13.2 AI boundaries).

Usage:
    >>> from music.providers.active_provider import (
    ...     get_active_music_provider_id,
    ...     require_active_provider_for_music,
    ...     NoActiveProviderError
    ... )
    >>>
    >>> # Get current active provider (may be None)
    >>> provider_id = get_active_music_provider_id()
    >>>
    >>> # Validate provider for music intent (raises if none linked)
    >>> try:
    ...     active = require_active_provider_for_music("spotify", "play")
    ... except NoActiveProviderError:
    ...     # Handle no provider linked
    ...     pass

Provider Selection Rules:
    1. If explicit_provider is specified and linked, use it
    2. Otherwise, use the default active provider
    3. Raise NoActiveProviderError if no provider is available

Bounded Context (PRD §13.2):
    - Provider logic MUST stay in music/providers/ only
    - UI layer MUST NOT import from music/providers/ directly
    - Hub is the only orchestrator of provider selection

Related Modules:
    - music/providers/registry.py: Provider class registration
    - music/consent/: Provider OAuth and consent management
    - docs/architecture/ai_boundaries.md: Full bounded context rules

See Also:
    - docs/music/provider_capability_matrix.md
    - CHANGELOG_RECENT.md
"""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)


class NoActiveProviderError(Exception):
    """Raised when no active music provider is linked."""

    pass


class ProviderMismatchError(Exception):
    """Raised when an explicit provider doesn't match the active provider."""

    pass


_PROVIDER_ALIASES: dict[str, str] = {
    "youtube": "youtube_iframe",
    "browser_native": "browser",
}
_SUPPORTED_ACTIVE_PROVIDER_IDS = frozenset(
    {
        "spotify",
        "youtube_music",
        "youtube_iframe",
        "local",
        "browser",
    }
)


def get_active_music_provider_id(user_id: str | None = None) -> str | None:
    """
    Get the currently active music provider ID from settings.

    Checks the UI settings manager first, then falls back to the
    ``preferred_music_provider`` config setting.

    Normalizes legacy aliases (e.g. ``"youtube"`` → ``"youtube_iframe"``) so
    callers always receive a canonical provider identifier.

    Returns:
        Provider ID (e.g., "youtube_iframe", "youtube_music", "browser", "local") or None
    """
    active_id: str | None = None

    try:
        from ui.settings_manager import get_settings_manager

        settings_mgr = get_settings_manager()
        raw = settings_mgr.get("active_music_provider_id", None, user_id=user_id)
        if isinstance(raw, str) and raw:
            active_id = raw
    except Exception as exc:
        logger.warning("Failed to get active_music_provider_id from settings manager: %s", exc)

    # Fall back to config.settings.preferred_music_provider
    if not active_id:
        try:
            from config.settings import settings as app_settings

            preferred = app_settings.preferred_music_provider
            if isinstance(preferred, str) and preferred:
                active_id = preferred
        except Exception as exc:
            logger.warning("Failed to get preferred_music_provider from config: %s", exc)

    if not active_id:
        return None

    active_id = active_id.strip().lower().replace("-", "_")
    normalized = _PROVIDER_ALIASES.get(active_id, active_id)
    if normalized not in _SUPPORTED_ACTIVE_PROVIDER_IDS:
        logger.warning("Ignoring unsupported active music provider setting: %s", normalized)
        return None
    return normalized


def require_active_provider_for_music(
    explicit_provider: str | None = None,
    intent_name: str = "music",
) -> str:
    """
    Validate that an active music provider exists and matches any explicit provider.

    Args:
        explicit_provider: Optional explicit provider ID from intent (e.g., "spotify")
        intent_name: Name of the intent for error messages (e.g., "play")

    Returns:
        The active provider ID to use

    Raises:
        NoActiveProviderError: If no active provider is linked
        ProviderMismatchError: If explicit_provider doesn't match the active provider
    """
    active_provider_id = get_active_music_provider_id()

    # No active provider - default to YouTube (free, no auth required)
    if not active_provider_id:
        # YouTube iframe is always available and doesn't require OAuth
        active_provider_id = "youtube_iframe"
        logger.info(
            "No active music provider set for %s intent; defaulting to %s",
            intent_name,
            active_provider_id,
        )

    # Explicit provider specified - must match active provider
    if explicit_provider and explicit_provider != active_provider_id:
        # Get display name for active provider
        active_display_name = _get_provider_display_name(active_provider_id) or active_provider_id
        explicit_display_name = _get_provider_display_name(explicit_provider) or explicit_provider

        logger.warning(
            "Rejected %s intent: provider mismatch (requested=%s, active=%s)",
            intent_name,
            explicit_provider,
            active_provider_id,
        )
        raise ProviderMismatchError(
            f"I'm currently linked to {active_display_name}. "
            f"To use {explicit_display_name}, switch providers in the Accounts screen."
        )

    # Use active provider
    logger.debug(
        "Resolved provider for '%s' intent: %s (active_music_provider_id=%s, explicit_provider=%s)",
        intent_name,
        active_provider_id,
        active_provider_id,
        explicit_provider,
    )
    return active_provider_id


def handle_provider_switch(
    old_provider: str | None,
    new_provider: str | None,
    *,
    music_service: object | None = None,
) -> None:
    """Handle side effects when the active music provider changes.

    Stops current playback and clears the queue so stale items from the
    previous provider don't leak into the new provider's session.

    Called from settings_api.py (UI) and intent/instant_commands/music.py (voice).

    Args:
        old_provider: Previous provider ID (may be None).
        new_provider: New provider ID (may be None).
        music_service: Optional music service with a `stop()` method.
            If not provided, the function is a no-op for playback (the
            setting still changes; caller handles that).
    """
    if old_provider == new_provider:
        return

    logger.info("Provider switch: %s → %s", old_provider, new_provider)

    if music_service is None:
        return

    try:
        stop_fn = getattr(music_service, "stop", None)
        if stop_fn is not None:
            stop_fn()
            logger.info("Provider switch: playback stopped and queue cleared")
    except Exception:
        logger.exception("Provider switch: failed to stop playback")


def _get_provider_display_name(provider_id: str) -> str | None:
    """Get display name for a provider ID."""
    try:
        from music.consent import providers

        metadata = providers.get_metadata(provider_id)
        return metadata.display_name if metadata else None
    except Exception as e:
        logger.exception("Failed to get provider display name: %s", e)
        return None
