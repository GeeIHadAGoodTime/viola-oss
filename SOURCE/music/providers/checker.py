"""
Provider linking checker for safety compliance.

This module provides utilities to check if providers are linked before
allowing any streaming or resolution operations.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from music.consent.models import ProviderLinkState
from music.exceptions import ConfigurationError

logger = get_logger("viola.music.providers.checker")


def _resolve_provider_check_user_id(user_id: str | None) -> str | None:
    """Resolve the effective user for provider-link checks.

    Prefer the explicit argument. Otherwise use the ambient request/user
    ContextVar when available. If no scoped user is available, callers should
    fail closed without spamming warnings.
    """
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        resolved_user_id = get_current_user_id()
        if isinstance(resolved_user_id, str) and resolved_user_id:
            return resolved_user_id
    except Exception:
        pass
    return None


def is_provider_linked(provider_id: str, user_id: str | None = None) -> bool:
    """
    Check if a provider is linked for the given user.

    Args:
        provider_id: Provider identifier (e.g., "youtube_music")
        user_id: Optional user ID, defaults to current user

    Returns:
        True if provider is linked, False otherwise
    """
    try:
        from music.consent import get_consent_service

        resolved_user_id = _resolve_provider_check_user_id(user_id)
        if not resolved_user_id:
            logger.debug("Provider %s linking status skipped: no scoped user_id", provider_id)
            return False
        service = get_consent_service()
        statuses = list(service.list_statuses(user_id=resolved_user_id))

        for status in statuses:
            if status.provider_id == provider_id:
                is_linked = status.state == ProviderLinkState.LINKED
                logger.debug(
                    "Provider %s linking status: %s (state=%s)",
                    provider_id,
                    "linked" if is_linked else "not_linked",
                    status.state.value,
                )
                return is_linked

        logger.debug("Provider %s not found in status list", provider_id)
        return False
    except Exception as exc:
        logger.warning("Failed to check provider linking status: %s", exc)
        # Fail closed: assume not linked if check fails
        return False


def _active_provider_is_anonymous() -> bool:
    """Check if the active music provider is anonymous (no OAuth needed).

    Returns True when ``youtube_iframe`` is active or no provider is set
    (which defaults to ``youtube_iframe``).
    """
    try:
        from music.providers.active_provider import get_active_music_provider_id

        active = get_active_music_provider_id()
        # youtube_iframe uses browser-based IFrame playback, no OAuth linking needed.
        # None means no provider configured; the system defaults to youtube_iframe.
        return active in (None, "youtube_iframe")
    except Exception:
        # Import failure or settings not ready — assume anonymous mode is safe.
        return True


def require_provider_linked(
    provider_id: str,
    user_id: str | None = None,
    error_message: str | None = None,
) -> None:
    """
    Raise ConfigurationError if provider is not linked.

    For ``youtube_music``, this is skipped when the active provider is
    ``youtube_iframe`` because browser playback does not require OAuth linking.

    Args:
        provider_id: Provider identifier (e.g., "youtube_music")
        user_id: Optional user ID
        error_message: Custom error message

    Raises:
        ConfigurationError: If provider is not linked
    """
    # Browser-backed YouTube playback never needs OAuth.
    if provider_id == "youtube_music" and _active_provider_is_anonymous():
        return

    if not is_provider_linked(provider_id, user_id=user_id):
        default_message = (
            f"Provider '{provider_id}' is not linked. "
            "Say 'connect spotify' or 'connect youtube' to link a provider for streaming."
        )
        message = error_message or default_message
        error = ConfigurationError(message)
        error.provider_id = provider_id
        error.user_id = user_id
        raise error


def is_youtube_url(url: str) -> bool:
    """
    Check if a URL is a YouTube or YouTube Music URL.

    This is used for detection/blocking purposes only. Detection of googlevideo.com
    URLs helps identify and reject direct stream URLs (which violate our control-layer-only policy).

    Args:
        url: URL to check

    Returns:
        True if URL appears to be from YouTube/YouTube Music
    """
    if not isinstance(url, str):
        return False
    url_lower = url.lower()
    return any(
        domain in url_lower
        for domain in [
            "youtube.com",
            "youtu.be",
            "googlevideo.com",  # Detection only - these URLs are rejected, never streamed
            "youtubemusic.com",
        ]
    )


def is_youtube_track_requiring_provider(item: Any) -> bool:
    """
    Check if a queue item is a YouTube track that requires a linked provider.

    Returns ``False`` when the active provider is ``youtube_iframe`` (anonymous
    playback that never needs OAuth linking).

    Args:
        item: QueueItem or dict-like object with url, source, provider, or video_id fields

    Returns:
        True if the track is from YouTube and requires provider linking
    """
    if item is None:
        return False

    # Anonymous mode (youtube_iframe) never requires provider linking.
    if _active_provider_is_anonymous():
        return False

    # Check provider field
    provider = getattr(item, "provider", None) or (item.get("provider") if isinstance(item, dict) else None)
    if provider == "youtube_music" or provider == "youtube":
        return True

    # Check source field
    source = getattr(item, "source", None) or (item.get("source") if isinstance(item, dict) else None)
    if source == "ytsearch1":
        return True

    # Check URL field
    url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
    if url and is_youtube_url(url):
        return True

    # Check video_id field (YouTube tracks typically have this)
    video_id = getattr(item, "video_id", None) or (item.get("video_id") if isinstance(item, dict) else None)
    if video_id:
        # If it has a video_id, it's likely YouTube
        # But we should be conservative - only mark as requiring provider if we're sure
        # Check if URL or source also suggests YouTube
        if url and is_youtube_url(url):
            return True
        if source == "ytsearch1":
            return True

    return False


def get_youtube_unavailable_reason() -> str:
    """
    Get a user-friendly reason why YouTube tracks are unavailable.

    Returns:
        Error message explaining why YouTube is disabled
    """
    # YouTube scraping has been removed. always_youtube always returns False.
    # Inlined from config.settings.allow_youtube (SP-008 / DFn-001).
    try:
        from config.settings import get_settings

        settings = get_settings()
        if settings.is_monetized_build:
            return "YouTube is disabled in this build"
        else:
            return "YouTube Music is not linked; cannot play this track"
    except Exception as e:
        logger.exception("Config check failed: %s", e)

    # Default fallback
    return "YouTube Music is not linked; cannot play this track"
