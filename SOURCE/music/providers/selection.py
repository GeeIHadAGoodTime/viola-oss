"""Deterministic provider selection for first-run music playback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.logging_config import get_logger
from music.consent.models import ProviderLinkState
from music.providers.errors import MusicProviderUnavailableError

logger = get_logger(__name__)

ProviderSource = Literal["ytsearch1", "local", "spotify_cdp", "browser"]

ANONYMOUS_YOUTUBE_PROVIDER_ID = "youtube_iframe"
SUPPORTED_CONNECTED_PROVIDERS = ("spotify", "youtube_music")
TOKEN_PROVIDER_PRIORITY = ("spotify", "youtube_music")


@dataclass(frozen=True)
class MusicProviderSelection:
    """Selected provider plus the resolver source needed by ProviderRouter."""

    provider_id: str
    source: ProviderSource
    reason: str


def _configured_active_provider_id() -> str | None:
    """Return the user-configured provider without applying config defaults."""
    try:
        from ui.settings_manager import get_settings_manager

        raw = get_settings_manager().get("active_music_provider_id", None)
    except Exception as exc:
        logger.debug("First-run provider selection could not read active provider: %s", exc)
        return None

    if isinstance(raw, str) and raw.strip():
        provider_id = raw.strip()
        if provider_id == "youtube":
            return ANONYMOUS_YOUTUBE_PROVIDER_ID
        return provider_id
    return None


def _source_for_provider(provider_id: str) -> ProviderSource | None:
    if provider_id == "local":
        return "local"
    if provider_id in ("spotify", "spotify_cdp"):
        return "spotify_cdp"
    if provider_id in ("youtube_music", ANONYMOUS_YOUTUBE_PROVIDER_ID):
        return "ytsearch1"
    if provider_id == "browser":
        try:
            from config.settings import settings

            if settings.browser_provider_enabled:
                return "browser"
        except Exception as exc:
            logger.debug("Browser provider availability check failed: %s", exc)
    return None


def _current_user_id() -> str | None:
    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
    except Exception:
        return None
    return user_id if isinstance(user_id, str) and user_id else None


def _token_is_valid(provider_id: str, user_id: str) -> bool:
    try:
        from music.consent import get_consent_service

        token_data = get_consent_service().resolve_access_token(provider_id, user_id=user_id)
    except Exception as exc:
        logger.debug("Token check failed for provider %s: %s", provider_id, exc)
        return False

    if not isinstance(token_data, dict):
        return False
    access_token = token_data.get("access_token")
    return isinstance(access_token, str) and bool(access_token.strip())


def _connected_provider_with_valid_token(user_id: str | None) -> str | None:
    if not user_id:
        return None

    try:
        from music.consent import get_consent_service

        statuses = list(get_consent_service().list_statuses(user_id=user_id))
    except Exception as exc:
        logger.debug("Connected provider status check failed: %s", exc)
        return None

    linked = {status.provider_id for status in statuses if getattr(status, "state", None) == ProviderLinkState.LINKED}
    for provider_id in TOKEN_PROVIDER_PRIORITY:
        if provider_id not in linked:
            continue
        if provider_id not in SUPPORTED_CONNECTED_PROVIDERS:
            logger.debug("Connected provider %s is not supported by desktop playback yet", provider_id)
            continue
        if _token_is_valid(provider_id, user_id):
            return provider_id
    return None


def _local_library_has_match(query: str, user_id: str | None) -> bool:
    if not user_id:
        logger.debug("Skipping local library match without authenticated user_id")
        return False
    try:
        from music.providers.local.provider import LocalMusicProvider

        provider = LocalMusicProvider()
        results = provider.search_tracks(user_id, query, limit=1)
    except Exception as exc:
        logger.debug("Local library first-run check failed for %r: %s", query[:50], exc)
        return False
    return bool(getattr(results, "items", None))


def is_youtube_anonymous_search_available() -> bool:
    """Return True when anonymous YouTube search/playback can be attempted."""
    try:
        from config.settings import settings

        if not bool(getattr(settings, "browser_search_enabled", True)):
            return False
    except Exception as exc:
        logger.debug("YouTube anonymous search settings check failed: %s", exc)

    try:
        import music.providers  # Importing registers bundled providers.
        from music.providers.models import ProviderName
        from music.providers.registry import get_provider_class

        get_provider_class(ProviderName.YOUTUBE_IFRAME)
    except Exception as exc:
        logger.debug("YouTube iframe provider is unavailable: %s", exc)
        return False
    return True


def select_first_run_music_provider(query: str) -> MusicProviderSelection:
    """Select a provider for a generic music query before resolution starts."""

    active_provider_id = _configured_active_provider_id()
    if active_provider_id:
        source = _source_for_provider(active_provider_id)
        if source is not None:
            return MusicProviderSelection(
                provider_id=active_provider_id,
                source=source,
                reason="configured_active_provider",
            )
        logger.info(
            "Configured music provider %s is unsupported; falling through to first-run cascade", active_provider_id
        )

    user_id = _current_user_id()
    connected_provider_id = _connected_provider_with_valid_token(user_id)
    if connected_provider_id:
        source = _source_for_provider(connected_provider_id)
        if source is not None:
            return MusicProviderSelection(
                provider_id=connected_provider_id,
                source=source,
                reason="connected_provider",
            )

    if _local_library_has_match(query, user_id):
        return MusicProviderSelection(provider_id="local", source="local", reason="local_library_match")

    if is_youtube_anonymous_search_available():
        return MusicProviderSelection(
            provider_id=ANONYMOUS_YOUTUBE_PROVIDER_ID,
            source="ytsearch1",
            reason="anonymous_youtube_fallback",
        )

    raise MusicProviderUnavailableError(
        "I can't play music right now - connect Spotify in Settings or add local files.",
        technical_details={
            "root_cause": "no_first_run_music_provider",
            "query": query[:50],
            "attempted_providers": [
                "connected_token_provider",
                "local",
                ANONYMOUS_YOUTUBE_PROVIDER_ID,
            ],
            "recoverable": True,
            "suggested_actions": ["connect_spotify", "add_local_files"],
        },
    )
