"""Metadata resolution helper for ProviderRouter.

Extracted to reduce complexity in provider_router.py while maintaining
the same resolution logic.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from music.providers.checker import is_youtube_url
from music.resolution.helpers import ResolutionMetadata
from music.resolution.youtube_resolver import YouTubeMusicResolver
from music.youtube_embed import extract_video_id

if TYPE_CHECKING:
    from music.resolution.provider_router import Source


class CustomResolver(Protocol):
    """Protocol for custom resolver implementations."""

    def resolve(self, query: str, source: Source) -> ResolutionMetadata: ...


class ProviderRouterMetadataResolver:
    """Handles metadata resolution for different query/source combinations.

    This class encapsulates the logic for resolving queries to ResolutionMetadata
    objects, supporting custom resolvers, URL sources, and local sources.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @staticmethod
    def _current_user_id() -> str:
        from core.user_context import get_current_user_id

        return get_current_user_id()

    def resolve_metadata(
        self,
        query: str,
        source: Source,
        custom_resolver: CustomResolver | None,
    ) -> ResolutionMetadata:
        """Resolve a query to metadata.

        Args:
            query: The query string (URL, search term, or file path)
            source: Source hint (ytsearch1, url, local)
            custom_resolver: Optional custom resolver to use

        Returns:
            ResolutionMetadata with resolved track information

        Raises:
            ResolutionError: If resolution fails
        """
        # Try custom resolver first if available.
        # Skip it for YouTube search queries (ytsearch1) — the provider path
        # sets playback_mode correctly (embedded_iframe_webview), which the
        # custom resolver (yt-dlp direct) does not.  This ensures YouTube
        # tracks route through the embedded player and can use CEF audio
        # capture for multiroom.
        _skip_custom = source == "ytsearch1"
        if custom_resolver is not None and not _skip_custom:
            try:
                metadata = custom_resolver.resolve(query, source)
                if metadata is not None:
                    self._logger.debug(
                        "Custom resolver produced metadata for query=%s source=%s",
                        query[:50] if query else "",
                        source,
                    )
                    return metadata
            except Exception as exc:
                self._logger.debug(
                    "Custom resolver failed for query=%s: %s",
                    query[:50] if query else "",
                    exc,
                )
                # Fall through to default resolution

        # Handle URL sources
        if source == "url" or query.startswith(("http://", "https://")):
            return self._resolve_url_source(query, source or "url")

        # Auto-detect local file paths (absolute, relative, or file:// URLs)
        query_clean = query
        if query_clean.startswith("file:///"):
            query_clean = query_clean[8:]
        elif query_clean.startswith("file://"):
            query_clean = query_clean[7:]

        try:
            if Path(query_clean).is_file():
                self._logger.info(
                    "Auto-detected local file path: %s (source was: %s)",
                    query_clean[:80],
                    source,
                )
                return self._resolve_local_source(query_clean, "local")
        except (OSError, ValueError):
            # Not a valid path, continue to normal resolution
            pass

        # Handle local sources
        if source == "local":
            return self._resolve_local_source(query, source)

        # Default: treat as search query
        return self._resolve_search_source(query, source or "ytsearch1")

    def _resolve_url_source(self, query: str, source: Source) -> ResolutionMetadata:
        """Resolve a URL query to metadata."""
        normalized_url = query.strip()

        if is_youtube_url(normalized_url):
            video_id = extract_video_id(normalized_url)
            extras = {"playback_mode": "embedded_webview"}

            self._logger.debug(
                "Resolved YouTube URL: url=%s video_id=%s",
                normalized_url[:80],
                video_id,
            )

            return ResolutionMetadata(
                url=normalized_url,
                title=normalized_url,  # Title will be enriched later by embed player
                source=source,
                video_id=video_id,
                provider="youtube_music",
                resolver_path="url.youtube",
                extras=extras,
            )

        # Non-YouTube URL
        return ResolutionMetadata(
            url=normalized_url,
            title=normalized_url,
            source=source,
            provider=None,
            resolver_path="url.direct",
        )

    def _resolve_local_source(self, query: str, source: Source) -> ResolutionMetadata:
        """Resolve a local file path or search query to metadata.

        Distinguishes between file paths (resolved directly) and search
        queries (resolved via LocalMusicProvider fuzzy search).
        """
        import os

        path = query.strip()

        if self._looks_like_file_path(path):
            basename = os.path.basename(path)
            artist = None
            artwork_url = None

            # Default title: clean filename (strip extension, underscores, etc.)
            from music.providers.local.scanner import (
                _artist_from_filename,
                _title_from_filename,
            )

            title = _title_from_filename(basename) if basename else path

            # Try to extract metadata from the file itself
            if os.path.exists(path):
                try:
                    from tinytag import TinyTag

                    tag = TinyTag.get(path, image=True)
                    if tag.title:
                        title = tag.title
                    if tag.artist:
                        artist = tag.artist
                    else:
                        artist = _artist_from_filename(basename)

                    from music.providers.local.artwork import extract_artwork

                    artwork_url = extract_artwork(path)
                except Exception:
                    self._logger.debug("Metadata extraction failed for %s", path)

            return ResolutionMetadata(
                url=path,
                title=title,
                artist=artist,
                artwork_url=artwork_url,
                source=source,
                provider="local",
                resolver_path="local.file",
                extras={"playback_mode": "vlc_stream", "file_path": path},
            )

        return self._resolve_local_search(query, source)

    @staticmethod
    def _looks_like_file_path(query: str) -> bool:
        """Check if query looks like a file path rather than a search query."""
        import os

        if os.path.exists(query):
            return True
        if query.startswith(("/", "./", "../")):
            return True
        # Windows drive letter paths (C:\, D:\, etc.)
        if len(query) >= 3 and query[1] == ":" and query[2] in ("/", "\\"):
            return True
        # Common audio file extensions
        lower = query.lower()
        return any(
            lower.endswith(ext)
            for ext in (
                ".mp3",
                ".flac",
                ".wav",
                ".ogg",
                ".m4a",
                ".aac",
                ".opus",
                ".mp4",
                ".mkv",
                ".avi",
                ".mov",
                ".wmv",
                ".flv",
                ".webm",
            )
        )

    def _resolve_local_search(self, query: str, source: Source) -> ResolutionMetadata:
        """Resolve a search query using LocalMusicProvider fuzzy search."""
        from music.providers.errors import MusicTrackNotFoundError

        try:
            from music.providers.local.provider import LocalMusicProvider

            provider = LocalMusicProvider()
            user_id = self._current_user_id()
            results = provider.search_tracks(user_id, query, limit=1)
            if results.items:
                track = results.items[0]
                file_path = track.extras.get("file_path", "")
                duration_secs = None
                if track.duration_ms is not None:
                    duration_secs = track.duration_ms // 1000

                # Use artwork from track summary (cached in DB) or fetch lazily
                artwork_url = str(track.artwork_url) if track.artwork_url else None
                if not artwork_url:
                    try:
                        artwork_url = provider.fetch_artwork(track)
                    except Exception:
                        self._logger.debug("Artwork extraction failed for %s", file_path)

                return ResolutionMetadata(
                    url=file_path,
                    title=track.title,
                    artist=track.artist_name,
                    artwork_url=artwork_url,
                    duration=duration_secs,
                    source=source,
                    provider="local",
                    resolver_path="local.search",
                    extras={"playback_mode": "vlc_stream", "file_path": file_path},
                )
        except MusicTrackNotFoundError:
            raise
        except Exception:
            self._logger.exception("Local search failed for query=%s", query[:50])

        raise MusicTrackNotFoundError(f"'{query}' not found in your local music library")

    def _resolve_search_source(self, query: str, source: Source) -> ResolutionMetadata:
        """Resolve a search query via the active provider's resolver.

        Routes to YouTubeMusicResolver for YouTube providers, or directly
        to SpotifyCDPProvider for Spotify.
        """
        from music.exceptions import ResolutionError
        from music.providers.active_provider import get_active_music_provider_id
        from music.providers.errors import (
            MusicProviderUnavailableError,
            MusicTrackNotFoundError,
            NoActiveMusicProviderError,
        )

        self._logger.info(
            "provider_router._resolve_search_source: ENTRY query=%r source=%s",
            query[:50] if query else "",
            source,
        )

        # Get active provider
        try:
            active_provider_id = get_active_music_provider_id()
        except Exception:
            active_provider_id = None

        if not active_provider_id:
            active_provider_id = "youtube_iframe"
            self._logger.info(
                "No active provider set; defaulting to youtube_iframe for query=%s",
                query[:50] if query else "",
            )

        # Spotify CDP — resolve via CDP provider, not YouTube
        if active_provider_id in ("spotify", "spotify_cdp"):
            return self._resolve_spotify_cdp_search(query, source)

        # Validate provider for YouTube path
        if active_provider_id not in ("youtube_music", "youtube_iframe"):
            self._logger.warning(
                "Rejected play intent: active provider '%s' not supported (query=%s)",
                active_provider_id,
                query[:50] if query else "",
            )
            raise MusicProviderUnavailableError(
                f"Active provider '{active_provider_id}' is not supported. "
                + "Say 'connect spotify' or 'connect youtube' to set up a supported provider."
            )

        # Resolve via YouTube Music resolver
        try:
            resolver = YouTubeMusicResolver(logger=self._logger)
            user_id = self._current_user_id()
            metadata = resolver.resolve_query(
                query=query,
                source=source,
                active_provider_id=active_provider_id,
                user_id=user_id,
            )
            self._logger.info(
                "provider_router._resolve_search_source: SUCCESS query=%r video_id=%s",
                query[:50] if query else "",
                metadata.video_id if metadata else None,
            )
            return metadata
        except (
            MusicTrackNotFoundError,
            MusicProviderUnavailableError,
            NoActiveMusicProviderError,
        ):
            raise
        except ResolutionError:
            raise
        except ValueError as exc:
            self._logger.warning(
                "provider_router._resolve_search_source: ValueError query=%s: %s",
                query[:50] if query else "",
                exc,
            )
            raise ResolutionError(f"Invalid query: {exc}") from exc
        except Exception as exc:
            self._logger.exception(
                "provider_router._resolve_search_source: UNEXPECTED_ERROR query=%s",
                query[:50] if query else "",
            )
            raise ResolutionError(f"Unexpected resolution error: {exc}") from exc

    def _resolve_spotify_cdp_search(self, query: str, source: Source) -> ResolutionMetadata:
        """Resolve a search query via Spotify CDP provider.

        Returns ResolutionMetadata with Spotify track info so the caller
        can build a QueueItem routed to SpotifyCDPEngine.
        """
        from music.providers.errors import MusicTrackNotFoundError
        from music.providers.spotify_cdp import SpotifyCDPProvider

        self._logger.info(
            "provider_router._resolve_spotify_cdp_search: query=%r source=%s",
            query[:50] if query else "",
            source,
        )

        try:
            provider = SpotifyCDPProvider()
            user_id = self._current_user_id()
            results = provider.search_tracks(user_id, query, limit=1)

            if not results.items:
                raise MusicTrackNotFoundError(f"Spotify returned no results for '{query}'")

            track = results.items[0]
            from music.providers.models import PlaybackContext

            stream = provider.resolve_stream(
                user_id,
                track,
                playback=PlaybackContext(),
            )

            search_index = track.extras.get("search_index", "0")
            spotify_uri = track.extras.get("uri", "")

            self._logger.info(
                "provider_router._resolve_spotify_cdp_search: SUCCESS query=%r title=%s",
                query[:50] if query else "",
                track.title,
            )

            return ResolutionMetadata(
                url=str(stream.url),
                title=track.title or query,
                source=source,
                video_id=None,
                artist=track.artist_name,
                provider="spotify",
                resolver_path="spotify_cdp.search",
                extras={
                    "playback_mode": "spotify_cdp",
                    "search_index": search_index,
                    "uri": spotify_uri,
                },
            )
        except MusicTrackNotFoundError:
            raise
        except Exception as exc:
            self._logger.exception(
                "provider_router._resolve_spotify_cdp_search: ERROR query=%s",
                query[:50] if query else "",
            )
            from music.exceptions import ResolutionError

            raise ResolutionError(f"Spotify CDP search failed: {exc}") from exc
