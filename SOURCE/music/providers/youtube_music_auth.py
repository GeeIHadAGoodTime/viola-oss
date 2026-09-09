"""Browser-only YouTube Music auth compatibility helpers.

The launch path uses browser session state, not stored Google OAuth tokens.
These helpers remain import-compatible for older callers and fail closed before
any token lookup, refresh, or external request can occur.
"""

from __future__ import annotations

from core.logging_config import get_logger

from .errors import ProviderNotConfiguredError

logger = get_logger("viola.music.providers.youtube_music")


def _get_access_token(user_id: str | None = None) -> str | None:
    _ = user_id
    logger.info("ytm.access_credential: skipped because YouTube Music uses browser auth only")
    return None


def _not_configured() -> ProviderNotConfiguredError:
    return ProviderNotConfiguredError(
        "YouTube Music OAuth is disabled. Use browser-based YouTube Music sign-in/playback.",
        provider_name="youtube_music",
    )
