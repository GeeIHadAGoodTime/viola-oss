"""
Local music file provider.

Implements the MusicProvider ABC for local audio files. Searches the
user's music folder using fuzzy matching (rapidfuzz) and resolves
tracks to file paths playable by VLC or SimpleBackend.

Usage:
    >>> from music.providers.local.provider import LocalMusicProvider
    >>> provider = LocalMusicProvider()
    >>> provider.initialize("/home/user/Music")
    >>> results = provider.search_tracks("user-123", "hotel california")
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import HttpUrl  # runtime cast target at line 415 — NameError without this

from config.settings import settings
from core.logging_config import get_logger
from music.providers.base import MusicProvider
from music.providers.models import (
    AuthContext,
    AuthSession,
    PaginatedResult,
    PlaybackContext,
    PlaylistSummary,
    ProviderCapabilities,
    ProviderName,
    SearchResults,
    StreamInfo,
    TrackSummary,
)
from music.providers.registry import auto_register

if TYPE_CHECKING:
    from music.providers.local.db import LocalLibraryRepo

logger = get_logger(__name__)

# Optional dependency: rapidfuzz enables fuzzy search.
# When missing, search degrades to case-insensitive substring matching.
try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:
    _rapidfuzz_fuzz = None  # type: ignore[assignment]
    _HAS_RAPIDFUZZ = False
    logger.warning(
        "rapidfuzz is not installed — local library search will use "
        "exact substring matching instead of fuzzy search. "
        "Install with: pip install rapidfuzz"
    )

# Minimum fuzzy match score (0-100) to include in results.
# Configured via settings.fuzzy_match_threshold; module constant kept as
# a local cache that is refreshed at call-time (see _get_match_threshold).


def _track_id_from_row(row: dict) -> str:
    """Build a stable track ID from a library row."""
    return str(row["id"])


def _track_summary_from_row(row: dict) -> TrackSummary:
    """Convert a LocalLibraryRepo row dict to a TrackSummary."""
    from music.providers.local.scanner import _artist_from_filename

    duration_ms = None
    if row.get("duration_seconds") is not None:
        duration_ms = int(row["duration_seconds"] * 1000)

    artist = row.get("artist")
    if not artist:
        artist = _artist_from_filename(row.get("file_name", "")) or "Unknown Artist"

    artwork = row.get("artwork_data")

    return TrackSummary(
        id=_track_id_from_row(row),
        title=row.get("title") or row.get("file_name", "Unknown"),
        artist_name=artist,
        album_name=row.get("album"),
        duration_ms=duration_ms,
        is_explicit=False,
        artwork_url=artwork,
        provider_track_id=_track_id_from_row(row),
        extras={
            "file_path": row["file_path"],
            "format": row.get("format", ""),
            "library_id": str(row["id"]),
            "album_art_embedded": "1" if row.get("album_art_embedded") else "0",
            "media_type": row.get("media_type", "audio"),
        },
    )


@auto_register(ProviderName.LOCAL)
class LocalMusicProvider(MusicProvider[None]):
    """Local music file provider with fuzzy search.

    Searches the user's indexed music library using rapidfuzz for fuzzy
    string matching. Resolves tracks to local file paths that VLC and
    SimpleBackend can play directly.

    Thread-safe: the underlying LocalLibraryRepo uses thread-local
    connections. This class holds no shared mutable state beyond the
    folder path (set once during initialize).
    """

    display_name = "Local Music"
    provider_name = ProviderName.LOCAL

    def __init__(self) -> None:
        self._folder_path: str | None = None
        self._init_lock = threading.Lock()
        super().__init__()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_repo(self) -> LocalLibraryRepo:
        """Get the singleton repo, initializing tables if needed."""
        from music.providers.local.db import get_local_library_repo

        repo = get_local_library_repo()
        repo.initialize()
        return repo

    def initialize(self, folder_path: str) -> None:
        """Set the music folder and trigger an initial library scan.

        The scan runs in a background thread to avoid blocking the caller.
        """
        with self._init_lock:
            self._folder_path = folder_path

        folder = Path(folder_path)
        if not folder.is_dir():
            logger.warning("Local music folder does not exist: %s", folder_path)
            return

        thread = threading.Thread(
            target=self._background_scan,
            args=(folder_path,),
            name="local-library-scan",
            daemon=True,
        )
        thread.start()

    def _background_scan(self, folder_path: str) -> None:
        """Run scan_and_index in a background thread."""
        start = time.monotonic()
        try:
            from music.providers.local.scanner import scan_and_index

            repo = self._get_repo()
            stats = scan_and_index(folder_path, repo)
            elapsed = time.monotonic() - start
            logger.info(
                "Local library scan complete: %d tracks indexed in %.1fs " "(upserted=%d, removed=%d, errors=%d)",
                stats["scanned"],
                elapsed,
                stats["upserted"],
                stats["removed"],
                stats["errors"],
            )
        except Exception:
            logger.exception("Local library scan failed for %s", folder_path)

    def rescan(self) -> None:
        """Re-scan the configured music folder in the background."""
        folder = self._folder_path
        if not folder:
            logger.warning("Cannot rescan: no music folder configured")
            return
        self.initialize(folder)

    def get_track_count(self) -> int:
        """Return the number of tracks currently in the library index."""
        try:
            repo = self._get_repo()
            return len(repo.get_all_tracks())
        except Exception:
            logger.exception("Failed to get track count")
            return 0

    # ------------------------------------------------------------------
    # MusicProvider ABC implementation
    # ------------------------------------------------------------------

    def authenticate_user(self, user_id: str, context: AuthContext) -> AuthSession:
        """Local files require no authentication."""
        return AuthSession(
            is_linked=True,
            requires_redirect=False,
            scopes=[],
        )

    def list_playlists(
        self,
        user_id: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> PaginatedResult[PlaylistSummary]:
        """List local playlists from the library database."""
        repo = self._get_repo()
        rows = repo.list_playlists()

        summaries: list[PlaylistSummary] = []
        for row in rows:
            summaries.append(
                PlaylistSummary(
                    id=str(row["id"]),
                    name=row["name"],
                    track_count=row.get("track_count", 0),
                    owner_name="Local",
                    is_liked_songs=False,
                )
            )

        # Simple cursor-based pagination
        start = 0
        if cursor is not None:
            try:
                start = int(cursor)
            except ValueError:
                start = 0

        page = summaries[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(summaries) else None

        return PaginatedResult(
            items=page,
            next_cursor=next_cursor,
            total=len(summaries),
        )

    def create_playlist(
        self,
        name: str,
        *,
        user_id: str,
    ) -> dict:
        """Create a new named playlist in the local library.

        Returns a dict with ``url``, ``playlist_id``, and ``display_name``
        suitable for :meth:`PlaylistManager.create_playlist` to consume.
        """
        repo = self._get_repo()
        db_id = repo.create_playlist(name)
        playlist_id_str = str(db_id)
        return {
            "url": "local://playlist/%s" % playlist_id_str,
            "playlist_id": playlist_id_str,
            "display_name": name,
        }

    def get_playlist_items(
        self,
        *,
        playlist_id: str,
        user_id: str,
        limit: int = 100,
        page_token: str | None = None,
    ) -> list[TrackSummary]:
        """Return tracks in a local playlist ordered by position.

        ``playlist_id`` is the string form of the SQLite playlist row id.
        Pagination is not supported — all tracks are returned in one call.
        """
        repo = self._get_repo()
        try:
            db_id = int(playlist_id)
        except (TypeError, ValueError):
            logger.warning(
                "get_playlist_items: invalid playlist_id %r — expected int string",
                playlist_id,
            )
            return []

        rows = repo.get_playlist_songs(db_id)
        return [_track_summary_from_row(row) for row in rows]

    def search_tracks(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 25,
        cursor: str | None = None,
    ) -> SearchResults:
        """Search local library using fuzzy matching.

        Pulls all tracks from SQLite and scores each against the query
        using rapidfuzz.fuzz.WRatio across title, artist, album, and
        filename fields. Returns the top ``limit`` results above the
        minimum score threshold.

        When rapidfuzz is not installed, falls back to case-insensitive
        substring matching so the provider remains usable without the
        optional dependency.
        """
        repo = self._get_repo()
        all_tracks = repo.get_all_tracks()

        if not all_tracks:
            return SearchResults(items=[], next_cursor=None, total=0, query=query)

        query_lower = query.lower()

        search_fields = ("title", "artist", "album", "file_name")
        scored: list[tuple[float, str, dict]] = []

        if _HAS_RAPIDFUZZ:
            # Fuzzy matching via rapidfuzz
            for track in all_tracks:
                field_scores = (
                    (
                        field,
                        _rapidfuzz_fuzz.WRatio(
                            query_lower,
                            (track.get(field) or "").lower(),
                            score_cutoff=0,
                        ),
                    )
                    for field in search_fields
                )
                matched_field, best_score = max(field_scores, key=lambda item: item[1])
                if best_score >= settings.fuzzy_match_threshold:
                    scored.append((float(best_score), matched_field, track))
        else:
            # Fallback: case-insensitive substring matching
            for track in all_tracks:
                fields = [(field, (track.get(field) or "").lower()) for field in search_fields]
                matched_field = next(
                    (field for field, value in fields if query_lower in value),
                    None,
                )
                if matched_field is not None:
                    # Assign a simple relevance score: 100 for exact title
                    # match, 80 for other field matches.
                    score = 100.0 if matched_field == "title" else 80.0
                    scored.append((score, matched_field, track))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Cursor-based offset
        start = 0
        if cursor is not None:
            try:
                start = int(cursor)
            except ValueError:
                start = 0

        page = scored[start : start + limit]
        items = []
        for score, matched_field, row in page:
            item = _track_summary_from_row(row)
            extras = cast("dict[str, object]", item.extras)
            extras["match_score"] = score
            extras["matched_field"] = matched_field
            items.append(item)
        next_cursor = str(start + limit) if start + limit < len(scored) else None

        return SearchResults(
            items=items,
            next_cursor=next_cursor,
            total=len(scored),
            query=query,
        )

    def resolve_stream(
        self,
        user_id: str,
        track: TrackSummary,
        *,
        playback: PlaybackContext,
    ) -> StreamInfo:
        """Resolve a local track to its file path for playback.

        For local files the "stream URL" is the file path itself. VLC and
        SimpleBackend both accept file paths natively.
        """
        file_path = track.extras.get("file_path")

        if not file_path:
            # Fall back to looking up by library ID
            repo = self._get_repo()
            row = repo.get_track_by_id(int(track.id))
            if row:
                file_path = row["file_path"]

        if not file_path:
            raise ValueError("Cannot resolve stream: no file_path for track %s" % track.id)

        # StreamInfo.url is typed as HttpUrl. Local file paths are not
        # valid HTTP URLs, so we use model_construct to bypass Pydantic
        # validation. The downstream QueueItem.url is plain str.
        return StreamInfo.model_construct(
            url=cast(HttpUrl, file_path),
            expires_at=None,
            drm=None,
            content_type=self._content_type_for(file_path),
            bitrate_kbps=None,
            requires_embedded_player=False,
            metadata={
                "provider": self.provider_name.value,
                "playback_mode": "vlc_stream",
                "file_path": file_path,
                "media_type": self._media_type_for(file_path),
            },
        )

    def fetch_artwork(
        self,
        track: TrackSummary,
        *,
        width: int = 512,
        height: int = 512,
    ) -> str | None:
        """Return artwork for a local track.

        Checks the DB cache first, then falls back to live extraction
        (embedded art or video thumbnail). Caches the result in the DB
        for future calls.

        Returns a base64 data URI or None.
        """
        file_path = track.extras.get("file_path")
        if not file_path:
            return None

        # Check DB cache first
        try:
            repo = self._get_repo()
            row = repo.get_track_by_path(file_path)
            if isinstance(row, dict) and row.get("artwork_data"):
                return row["artwork_data"]
        except Exception:
            logger.debug("Failed to check artwork cache for %s", file_path)

        # Live extraction fallback
        try:
            from music.providers.local.artwork import extract_artwork

            artwork = extract_artwork(file_path)
            if artwork:
                # Cache in DB for next time
                try:
                    repo = self._get_repo()
                    repo.update_artwork(file_path, artwork)
                except Exception:
                    logger.debug("Failed to cache artwork for %s", file_path)
                return artwork
        except Exception:
            logger.debug("Failed to extract artwork from %s", file_path)
        return None

    def provider_capabilities(self) -> ProviderCapabilities:
        """Declare capabilities of the local file provider."""
        return ProviderCapabilities(
            name=self.provider_name,
            features=[],
            max_bitrate_kbps=None,
            supports_explicit_filter=False,
            supports_offline_downloads=False,
            notes="Local audio files. Supports MP3, FLAC, WAV, OGG, M4A, AAC, OPUS.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _content_type_for(file_path: str) -> str:
        """Guess MIME type from file extension."""
        ext = Path(file_path).suffix.lower()
        return {
            ".mp3": "audio/mpeg",
            ".flac": "audio/flac",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
            ".m4a": "audio/mp4",
            ".aac": "audio/aac",
            ".wma": "audio/x-ms-wma",
            ".webm": "video/webm",
            ".mp4": "video/mp4",
            ".mkv": "video/x-matroska",
            ".avi": "video/x-msvideo",
            ".mov": "video/quicktime",
            ".wmv": "video/x-ms-wmv",
            ".flv": "video/x-flv",
        }.get(ext, "audio/mpeg")

    @staticmethod
    def _media_type_for(file_path: str) -> str:
        """Classify file as 'audio' or 'video' by extension."""
        from music.providers.local.scanner import classify_media_type

        ext = Path(file_path).suffix
        return classify_media_type(ext)
