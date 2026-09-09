"""Provider-scoped media search candidate filtering."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from music.providers.models import TrackSummary

_YOUTUBE_MUSIC_PROVIDERS = {"youtube_iframe", "youtube_music"}
_SPOTIFY_PROVIDERS = {"spotify", "spotify_cdp"}
_LOCAL_PROVIDER = "local"
_GENERAL_YOUTUBE_PROVIDER = "youtube"

_MIN_SONG_SECONDS = 60
_MAX_SONG_SECONDS = 600
_UNKNOWN_ARTISTS = {"", "unknown", "unknown artist", "youtube"}

YOUTUBE_MUSIC_EXCLUSION_KEYWORDS: tuple[str, ...] = (
    "parody",
    "reaction",
    "cover",
    "tutorial",
    "lesson",
    "explained",
    "behind the scenes",
    "review",
    "interview",
    "karaoke",
    "remix",
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def filter_candidates(tracks: list[TrackSummary], provider: str, query: str) -> list[TrackSummary]:
    """Apply per-provider quality filter to raw search candidates."""
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return tracks

    provider_id = _provider_id(provider)
    if provider_id in _YOUTUBE_MUSIC_PROVIDERS:
        return _filter_youtube_music(tracks, normalized_query)
    if provider_id == _GENERAL_YOUTUBE_PROVIDER:
        return tracks
    if provider_id == _LOCAL_PROVIDER:
        return _filter_local(tracks, normalized_query)
    if provider_id in _SPOTIFY_PROVIDERS:
        return tracks
    return tracks


def _filter_youtube_music(tracks: list[TrackSummary], query: str) -> list[TrackSummary]:
    filtered = [
        track
        for track in tracks
        if not _title_has_disallowed_keyword(track.title, query) and _duration_is_song_length(track.duration_ms)
    ]
    if not filtered:
        return tracks

    query_tokens = _tokens(query)
    return sorted(
        filtered,
        key=lambda track: _youtube_music_rank(track, query_tokens),
        reverse=True,
    )


def _filter_local(tracks: list[TrackSummary], query: str) -> list[TrackSummary]:
    if len(query.split()) > 1:
        return tracks

    filtered = [track for track in tracks if _local_contains_query(track, query)]
    return sorted(
        filtered,
        key=lambda track: _local_rank(track, query),
        reverse=True,
    )


def _provider_id(provider: Any) -> str:
    raw_provider = getattr(provider, "value", provider)
    return str(raw_provider or "").strip().lower()


def _safe_extras(track: TrackSummary) -> Mapping[str, Any]:
    extras = getattr(track, "extras", None)
    if isinstance(extras, Mapping):
        return extras
    return {}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _tokens(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(value.casefold()) if len(token) >= 3}


def _keyword_present(text: str, keyword: str) -> bool:
    normalized = _normalize_text(text)
    if " " in keyword:
        return keyword in normalized
    return re.search(r"\b%s\b" % re.escape(keyword), normalized) is not None


def _title_has_disallowed_keyword(title: str, query: str) -> bool:
    for keyword in YOUTUBE_MUSIC_EXCLUSION_KEYWORDS:
        if _keyword_present(title, keyword) and not _keyword_present(query, keyword):
            return True
    return False


def _duration_is_song_length(duration_ms: int | None) -> bool:
    if duration_ms is None:
        return True
    try:
        duration_seconds = float(duration_ms) / 1000.0
    except (TypeError, ValueError):
        return True
    return _MIN_SONG_SECONDS <= duration_seconds <= _MAX_SONG_SECONDS


def _artist_text(track: TrackSummary) -> str:
    artist = _normalize_text(getattr(track, "artist_name", ""))
    if artist in _UNKNOWN_ARTISTS:
        return ""
    return artist


def _youtube_music_rank(track: TrackSummary, query_tokens: set[str]) -> tuple[int, int]:
    artist = _artist_text(track)
    if not artist:
        return (0, 0)

    trusted_uploader = int("vevo" in artist or "topic" in artist)
    query_artist_match = int(any(token in artist for token in query_tokens))
    return (trusted_uploader, query_artist_match)


def _local_contains_query(track: TrackSummary, query: str) -> bool:
    return any(query in value for value in _local_search_values(track))


def _local_rank(track: TrackSummary, query: str) -> tuple[int, float]:
    exact_substring = int(_local_contains_query(track, query))
    return (exact_substring, _match_score(track))


def _local_search_values(track: TrackSummary) -> tuple[str, str, str]:
    extras = _safe_extras(track)
    file_name = extras.get("file_name")
    if file_name is None:
        file_name = _basename(extras.get("file_path"))
    return (
        _normalize_text(getattr(track, "title", "")),
        _normalize_text(getattr(track, "artist_name", "")),
        _normalize_text(file_name),
    )


def _basename(path: Any) -> str:
    text = str(path or "")
    if not text:
        return ""
    return text.replace("\\", "/").rsplit("/", 1)[-1]


def _match_score(track: TrackSummary) -> float:
    raw_score = _safe_extras(track).get("match_score")
    if raw_score is None:
        return 0.0
    try:
        return float(raw_score)
    except (TypeError, ValueError):
        return 0.0
