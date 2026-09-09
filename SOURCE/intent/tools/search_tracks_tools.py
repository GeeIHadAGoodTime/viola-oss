"""Agent tool handler for music track search.

Exposes provider-backed track discovery to the MCP layer so the AI can
find playlist-ready tracks before calling ``playlist_add_track``.

Wired into ``mcp_servers/core_tools/server.py``.
"""

from __future__ import annotations

from typing import Any

from intent.tool_types import ToolResult
from intent.tools.media_filter import filter_candidates
from music.providers.active_provider import get_active_music_provider_id
from music.providers.checker import is_provider_linked
from music.providers.errors import (
    MusicProviderUnavailableError,
    MusicTrackNotFoundError,
)
from music.providers.models import ProviderName
from music.providers.registry import get_provider_class
from music.recents_service import get_music_recents_service

_SPOTIFY_PROVIDER_ID = ProviderName.SPOTIFY.value
_YOUTUBE_IFRAME_PROVIDER_ID = ProviderName.YOUTUBE_IFRAME.value
_PROVIDER_ALIASES = {
    "youtube": _YOUTUBE_IFRAME_PROVIDER_ID,
    "youtube_music": _YOUTUBE_IFRAME_PROVIDER_ID,
    "yt": _YOUTUBE_IFRAME_PROVIDER_ID,
    "spotify_cdp": _SPOTIFY_PROVIDER_ID,
}
_ALLOWED_PROVIDER_REQUESTS = {"any", "local", "youtube", _SPOTIFY_PROVIDER_ID}
_ALL_LIBRARY_QUERIES = {"all", "*", "library", "my library", "songs", "music", "tracks"}


def _normalize_provider_request(provider: str | None) -> str:
    requested = str(provider or "any").strip().lower()
    if requested in {"youtube_iframe", "youtube_music", "yt"}:
        return "youtube"
    if requested == "spotify_cdp":
        return _SPOTIFY_PROVIDER_ID
    return requested or "any"


def _canonical_provider_id(provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    normalized = str(provider_id).strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _providers_for_request(provider: str, *, user_id: str) -> list[str]:
    if provider == "local":
        return ["local"]
    if provider == "youtube":
        return [_YOUTUBE_IFRAME_PROVIDER_ID]
    if provider == _SPOTIFY_PROVIDER_ID:
        return [_SPOTIFY_PROVIDER_ID]

    providers = ["local"]
    active = _canonical_provider_id(get_active_music_provider_id(user_id=user_id))
    if active and active not in providers:
        providers.append(active)
    if _spotify_provider_is_connected(user_id) and _SPOTIFY_PROVIDER_ID not in providers:
        providers.append(_SPOTIFY_PROVIDER_ID)
    if _YOUTUBE_IFRAME_PROVIDER_ID not in providers:
        providers.append(_YOUTUBE_IFRAME_PROVIDER_ID)
    return providers


def _track_dict(item: Any, provider_id: str) -> dict[str, Any]:
    extras = item.extras if isinstance(getattr(item, "extras", None), dict) else {}
    raw_match_score = extras.get("match_score")
    match_score = None
    if raw_match_score is not None:
        try:
            match_score = float(raw_match_score)
        except (TypeError, ValueError):
            match_score = None
    raw_matched_field = extras.get("matched_field")
    return {
        "title": item.title,
        "artist": item.artist_name,
        "provider": ("youtube" if provider_id == _YOUTUBE_IFRAME_PROVIDER_ID else provider_id),
        "track_uri": item.provider_track_id or item.id,
        "album": item.album_name,
        "duration_ms": item.duration_ms,
        "match_score": match_score,
        "matched_field": (str(raw_matched_field) if raw_matched_field is not None else None),
    }


def _spotify_provider_is_connected(user_id: str) -> bool:
    return is_provider_linked(_SPOTIFY_PROVIDER_ID, user_id=user_id)


async def _recently_played_tracks(user_id: str) -> list[dict]:
    # Async-native: the cloud media/search tool runs ON the FastAPI serving loop,
    # where the sync recents read (PersistentStateStore Postgres `_run` bridge)
    # would raise SyncBridgeLoopError (CL-20260711-afd7).
    return await get_music_recents_service().list_recently_played_async(user_id, limit=5)


def _local_row_track_dict(row: dict[str, Any]) -> dict[str, Any]:
    duration_ms = None
    if row.get("duration_seconds") is not None:
        duration_ms = int(float(row["duration_seconds"]) * 1000)
    return {
        "title": row.get("title") or row.get("file_name") or "Unknown",
        "artist": row.get("artist") or "Unknown Artist",
        "provider": "local",
        "track_uri": str(row.get("id") or ""),
        "album": row.get("album"),
        "duration_ms": duration_ms,
    }


def _search_local_library_all(limit: int) -> list[dict[str, Any]]:
    from music.providers.local.db import get_local_library_repo

    repo = get_local_library_repo()
    repo.initialize()
    rows = repo.get_all_tracks()[:limit]
    return [_local_row_track_dict(row) for row in rows]


def _search_provider_tracks(
    provider_id: str,
    query: str,
    *,
    limit: int,
    user_id: str,
) -> list[dict[str, Any]]:
    if provider_id == "local" and query.strip().lower() in _ALL_LIBRARY_QUERIES:
        return _search_local_library_all(limit)

    provider_name = ProviderName(provider_id)
    provider_cls = get_provider_class(provider_name)
    provider = provider_cls()
    results = provider.search_tracks(user_id, query, limit=limit)
    filtered_items = filter_candidates(results.items, provider_id, query)
    return [_track_dict(item, provider_id) for item in filtered_items]


async def search_tracks_handler(
    query: str,
    limit: int = 10,
    provider: str = "any",
    user_id: str = "",
) -> ToolResult:
    """Search local and provider-backed tracks and return playlist-ready results.

    Args:
        query: Search description, such as a title, artist, genre, or mood.
        limit: Maximum number of tracks to return (default 10, max 25).
        provider: ``any`` searches local plus the active/Spotify/YouTube path;
            ``local`` searches the offline library only; ``spotify`` searches
            Spotify; ``youtube`` uses browser YouTube.
        user_id: Optional caller user ID injected by MCP.

    Returns:
        ToolResult with ``tracks`` containing playlist-compatible result dicts:
        ``title``, ``artist``, ``provider``, ``track_uri``, ``album``,
        ``duration_ms``, ``match_score``, and ``matched_field``.
    """
    query = query.strip()
    if not query:
        return ToolResult(ok=False, data=None, error="Search query cannot be empty.")

    limit = max(1, min(limit, 25))
    requested_provider = _normalize_provider_request(provider)
    if requested_provider not in _ALLOWED_PROVIDER_REQUESTS:
        return ToolResult(
            ok=False,
            data=None,
            error="search_tracks failed: provider must be one of 'any', 'local', 'youtube', or 'spotify'.",
        )

    tracks: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    searched_providers = _providers_for_request(requested_provider, user_id=user_id)
    for provider_id in searched_providers:
        try:
            provider_tracks = _search_provider_tracks(provider_id, query, limit=limit, user_id=user_id)
        except MusicTrackNotFoundError:
            provider_tracks = []
        except (MusicProviderUnavailableError, ValueError) as exc:
            if requested_provider == "any":
                warnings.append("%s: %s" % (provider_id, exc))
                continue
            return ToolResult(ok=False, data=None, error="search_tracks failed: %s" % exc)
        except Exception as exc:
            if requested_provider == "any":
                warnings.append("%s: %s" % (provider_id, exc))
                continue
            return ToolResult(ok=False, data=None, error="search_tracks failed: %s" % exc)
        for track in provider_tracks:
            key = (
                str(track.get("provider") or provider_id),
                str(track.get("track_uri") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            tracks.append(track)
            if len(tracks) >= limit:
                break
        if len(tracks) >= limit:
            break

    recently_played = await _recently_played_tracks(user_id)
    if not tracks:
        return ToolResult(
            ok=True,
            data={
                "tracks": [],
                "recently_played": recently_played,
                "count": 0,
                "provider": requested_provider,
                "searched_providers": searched_providers,
                "query": query,
                "warnings": warnings,
                "message": "No tracks found for %r." % query,
            },
        )

    return ToolResult(
        ok=True,
        data={
            "tracks": tracks,
            "recently_played": recently_played,
            "count": len(tracks),
            "provider": requested_provider,
            "searched_providers": searched_providers,
            "query": query,
            "warnings": warnings,
            # R3-P1-E (2026-05-30): retired next_step prose hint. The
            # model reads the tracks list as structured data and decides
            # the follow-up tool. Runtime suggesting "Use playlist_add_track"
            # is the boxing pattern compass deleted from media_tools.
        },
    )
