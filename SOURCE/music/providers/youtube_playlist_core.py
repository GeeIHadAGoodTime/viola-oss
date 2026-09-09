"""
Legacy YouTube playlist API shim.

YouTube playback/search policy is browser-based youtube_iframe/browser_search.
This module remains importable for older callers, but playlist API runtime use
fails closed before any network access.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import Literal

from core.logging_config import get_logger

from .errors import MusicProviderUnavailableError
from .models import TrackSummary

logger = get_logger("viola.music.providers.youtube_playlist_core")


def _playlist_external_api_disabled_error(
    provider_id: str,
    operation: str,
    *,
    playlist_id: str | None = None,
) -> Exception:
    details: dict[str, str] = {
        "root_cause": "youtube_browser_provider_required",
        "kind": "PROVIDER_DISABLED",
        "provider": provider_id,
        "operation": operation,
        "required_path": "youtube_iframe.browser_search",
    }
    if playlist_id:
        details["playlist_id"] = playlist_id
    return MusicProviderUnavailableError(
        "Legacy YouTube playlist API access is disabled. Use browser-based YouTube paths instead.",
        technical_details=details,
    )


def _raise_playlist_external_api_disabled(
    provider_id: str,
    operation: str,
    *,
    playlist_id: str | None = None,
) -> None:
    raise _playlist_external_api_disabled_error(provider_id, operation, playlist_id=playlist_id)


class YouTubePlaylistCore:
    """
    Legacy playlist API shim.

    API-key and OAuth playlist runtime paths are disabled. The class remains
    import-compatible so legacy users fail closed with a clear error.
    """

    def __init__(
        self,
        auth_mode: Literal["oauth", "api_key"],
        *,
        get_oauth_token: Callable[[str], str | None] | None = None,
        get_api_key: Callable[[], str | None] | None = None,
        provider_id: str = "youtube_playlist",
    ):
        self.auth_mode = auth_mode
        self._get_oauth_token = get_oauth_token
        self._get_api_key = get_api_key
        self.provider_id = provider_id
        self._api_call_count = 0
        self._api_call_lock = threading.RLock()

        if auth_mode == "api_key":
            logger.warning(
                "youtube_playlist_core: API-key playlist fallback disabled for provider=%s",
                provider_id,
            )

    def get_playlist_items(
        self,
        playlist_id: str,
        user_id: str,
        limit: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[TrackSummary], str | None]:
        """Fail closed for the retired playlist request path."""
        _ = (user_id, limit, page_token)
        _raise_playlist_external_api_disabled(self.provider_id, "retired_playlist_lookup", playlist_id=playlist_id)
        return [], None

    def _make_api_request(
        self,
        playlist_id: str,
        limit: int,
        user_id: str,
        page_token: str | None,
    ) -> dict:
        """Fail closed for the retired playlist request path."""
        _ = (limit, user_id, page_token)
        _raise_playlist_external_api_disabled(self.provider_id, "retired_playlist_lookup", playlist_id=playlist_id)
        return {}

    def _handle_http_error(
        self,
        exc: BaseException,
        playlist_id: str,
    ) -> tuple[list[TrackSummary], str | None]:
        """Fail closed for retired API HTTP error handling."""
        raise _playlist_external_api_disabled_error(
            self.provider_id,
            "retired_playlist_lookup",
            playlist_id=playlist_id,
        ) from exc

    def _parse_items(self, data: dict) -> tuple[list[TrackSummary], str | None]:
        """Fail closed for retired API response parsing."""
        _ = data
        _raise_playlist_external_api_disabled(self.provider_id, "retired_playlist_lookup")
        return [], None

    def _get_test_items(
        self,
        playlist_id: str,
        limit: int,
    ) -> tuple[list[TrackSummary], str | None]:
        """Test mode no longer bypasses the retired API path."""
        _ = (playlist_id, limit)
        _raise_playlist_external_api_disabled(self.provider_id, "retired_playlist_lookup", playlist_id=playlist_id)
        return [], None


def extract_playlist_id(url: str) -> str | None:
    """
    Extract YouTube playlist ID from a URL.

    Supports:
    - https://www.youtube.com/playlist?list=PLxxxxx
    - https://youtube.com/watch?v=xxx&list=PLxxxxx
    - https://music.youtube.com/playlist?list=PLxxxxx
    """
    if not url:
        return None

    match = re.search(r"[?&]list=([^&]+)", url)
    if match:
        return match.group(1)

    return None
