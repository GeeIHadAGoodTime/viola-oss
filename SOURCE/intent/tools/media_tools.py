"""Unified media search/play tool handler."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult
from intent.tools.music_tools import play_music_handler as _play_music_handler
from intent.tools.search_tracks_tools import (
    search_tracks_handler as _search_tracks_handler,
)
from music.youtube_embed import extract_video_id

logger = get_logger(__name__)

_MAX_SEARCH_LIMIT = 25
_DEFAULT_SEARCH_LIMIT = 10
_GENERIC_MUSIC_QUERY = "music"
_SPOTIFY_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_SEARCH_PROVIDER_ALIASES = {
    "spotify_cdp": "spotify",
    "youtube_iframe": "youtube",
    "youtube_music": "youtube",
    "yt": "youtube",
}
_FILTER_PROVIDER_ALIASES = {
    "spotify_cdp": "spotify",
    "yt": "youtube",
}
_PLAY_PROVIDER_ALIASES = {
    "spotify_cdp": "spotify",
    "youtube_iframe": "youtube",
    "youtube_music": "youtube",
    "yt": "youtube",
    "my_library": "local",
    "my library": "local",
    "local_files": "local",
    "local files": "local",
}
_SEARCH_PROVIDERS = {"any", "local", "spotify", "youtube"}
_MEDIA_ACTIONS = {"auto", "search", "play", "search_play"}
_MEDIA_CANDIDATE_FIELDS = (
    "title",
    "artist",
    "provider",
    "track_uri",
    "album",
    "duration_ms",
    "match_score",
    "matched_field",
)
_LOCAL_FILE_EXTENSIONS = (
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


def _clean_provider(value: str | None, default: str = "auto") -> str:
    provider = str(value or default).strip().lower().replace("-", "_")
    return provider or default


def _active_provider_id(user_id: str = "") -> str:
    try:
        from music.providers.active_provider import get_active_music_provider_id

        return str(get_active_music_provider_id(user_id=user_id or None) or "").strip().lower()
    except Exception:
        logger.debug("media could not read active music provider")
        return ""


def _resolve_search_provider(provider: str | None, user_id: str = "") -> tuple[str, str, str]:
    requested = _clean_provider(provider)
    if requested == "auto":
        active = _active_provider_id(user_id)
        if not active:
            return requested, "any", "any"
        search_provider = _SEARCH_PROVIDER_ALIASES.get(active, active)
        if search_provider not in _SEARCH_PROVIDERS:
            search_provider = "any"
        filter_provider = _FILTER_PROVIDER_ALIASES.get(active, active)
        return requested, search_provider, filter_provider

    search_provider = _SEARCH_PROVIDER_ALIASES.get(requested, requested)
    if search_provider not in _SEARCH_PROVIDERS:
        raise ValueError("provider must be one of 'auto', 'any', 'local', 'spotify', 'youtube', or 'youtube_music'.")
    filter_provider = _FILTER_PROVIDER_ALIASES.get(requested, requested)
    return requested, search_provider, filter_provider


def _candidate_track_uri(track: dict[str, Any]) -> str:
    provider = str(track.get("provider") or "").strip().lower()
    track_uri = str(track.get("track_uri") or "").strip()
    if provider == "spotify" and _SPOTIFY_TRACK_ID_RE.match(track_uri):
        return "spotify:track:%s" % track_uri
    return track_uri


def _shape_candidate(track: dict[str, Any]) -> dict[str, Any]:
    shaped = {field: track.get(field) for field in _MEDIA_CANDIDATE_FIELDS}
    shaped["track_uri"] = _candidate_track_uri(track)
    return shaped


def _filter_key_from_dict(track: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(track.get("track_uri") or ""),
        str(track.get("title") or ""),
        str(track.get("artist") or ""),
    )


def _filter_key_from_summary(track: Any) -> tuple[str, str, str]:
    return (
        str(getattr(track, "provider_track_id", None) or getattr(track, "id", "") or ""),
        str(getattr(track, "title", "") or ""),
        str(getattr(track, "artist_name", "") or ""),
    )


def _summary_from_track_dict(track: dict[str, Any]) -> Any:
    from music.providers.models import TrackSummary

    track_uri = str(track.get("track_uri") or "")
    title = str(track.get("title") or track_uri or "Unknown")
    artist = str(track.get("artist") or "Unknown Artist")
    extras: dict[str, str] = {
        "provider": str(track.get("provider") or ""),
        "track_uri": track_uri,
    }
    if track.get("match_score") is not None:
        extras["match_score"] = str(track.get("match_score"))
    if track.get("matched_field") is not None:
        extras["matched_field"] = str(track.get("matched_field"))

    return TrackSummary(
        id=track_uri or title,
        title=title,
        artist_name=artist,
        album_name=track.get("album"),
        duration_ms=track.get("duration_ms"),
        provider_track_id=track_uri or None,
        extras=extras,
    )


def _track_dict_from_summary(track: Any) -> dict[str, Any]:
    extras = getattr(track, "extras", None)
    if not isinstance(extras, dict):
        extras = {}
    match_score = extras.get("match_score")
    try:
        parsed_match_score = float(match_score) if match_score is not None else None
    except (TypeError, ValueError):
        parsed_match_score = None
    return {
        "title": getattr(track, "title", None),
        "artist": getattr(track, "artist_name", None),
        "provider": extras.get("provider"),
        "track_uri": getattr(track, "provider_track_id", None) or getattr(track, "id", None),
        "album": getattr(track, "album_name", None),
        "duration_ms": getattr(track, "duration_ms", None),
        "match_score": parsed_match_score,
        "matched_field": extras.get("matched_field"),
    }


def _load_filter_candidates() -> Callable[[list[Any], str, str], list[Any]] | None:
    try:
        from intent.tools.media_filter import filter_candidates
    except ImportError:
        return None
    return filter_candidates


def _coerce_filtered_tracks(
    filtered: list[Any],
    lookup: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    for item in filtered:
        if isinstance(item, dict):
            tracks.append(item)
            continue
        key = _filter_key_from_summary(item)
        original = lookup.get(key)
        if original is not None:
            tracks.append(original)
            continue
        tracks.append(_track_dict_from_summary(item))
    return tracks


def _run_filter_for_group(
    filter_candidates: Callable[[list[Any], str, str], list[Any]],
    tracks: list[dict[str, Any]],
    provider: str,
    query: str,
) -> list[dict[str, Any]]:
    summaries = [_summary_from_track_dict(track) for track in tracks]
    lookup = {_filter_key_from_dict(track): track for track in tracks}
    filtered = filter_candidates(summaries, provider, query)
    if not isinstance(filtered, list):
        logger.warning("media filter returned non-list for provider=%s", provider)
        return tracks
    return _coerce_filtered_tracks(filtered, lookup)


def _apply_quality_filter(tracks: list[dict[str, Any]], provider: str, query: str) -> list[dict[str, Any]]:
    filter_candidates = _load_filter_candidates()
    if filter_candidates is None:
        return tracks
    try:
        if provider == "any":
            filtered_tracks: list[dict[str, Any]] = []
            by_provider: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for track in tracks:
                track_provider = str(track.get("provider") or "any").strip().lower() or "any"
                if track_provider not in by_provider:
                    by_provider[track_provider] = []
                    order.append(track_provider)
                by_provider[track_provider].append(track)
            for track_provider in order:
                filtered_tracks.extend(
                    _run_filter_for_group(
                        filter_candidates,
                        by_provider[track_provider],
                        track_provider,
                        query,
                    )
                )
            return filtered_tracks
        return _run_filter_for_group(filter_candidates, tracks, provider, query)
    except Exception:
        logger.exception("media filter failed")
        return tracks


def _spotify_uri_from_track_uri(track_uri: str) -> str:
    value = track_uri.strip()
    if value.startswith("spotify:track:"):
        return value
    if "open.spotify.com/track/" in value:
        track_id = value.split("open.spotify.com/track/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        if track_id:
            return "spotify:track:%s" % track_id
    return "spotify:track:%s" % value


def _looks_like_local_path(value: str) -> bool:
    lowered = value.lower()
    return (
        "\\" in value
        or "/" in value
        or (len(value) >= 3 and value[1] == ":")
        or lowered.endswith(_LOCAL_FILE_EXTENSIONS)
    )


def _local_track_id_from_uri(track_uri: str) -> int | None:
    value = track_uri.strip()
    for prefix in ("local://track/", "local://library/", "local:"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    if value.isdigit():
        return int(value)
    return None


def _resolve_local_file_path(track_uri: str) -> str:
    if _looks_like_local_path(track_uri):
        return track_uri.strip()

    library_id = _local_track_id_from_uri(track_uri)
    if library_id is None:
        raise ValueError("local track_uri must be a library row id or local file path.")

    from music.providers.local.db import get_local_library_repo

    repo = get_local_library_repo()
    repo.initialize()
    row = repo.get_track_by_id(library_id)
    if not row:
        raise ValueError("Local track_uri %s was not found in the music library." % track_uri)
    file_path = str(row.get("file_path") or "").strip()
    if not file_path:
        raise ValueError("Local track_uri %s has no file_path." % track_uri)
    return file_path


def _infer_play_provider(provider: str | None, track_uri: str) -> str:
    requested = _clean_provider(provider)
    if requested not in {"", "auto", "any"}:
        return _PLAY_PROVIDER_ALIASES.get(requested, requested)

    value = track_uri.strip()
    lowered = value.lower()
    if lowered.startswith("spotify:track:") or "open.spotify.com/track/" in lowered:
        return "spotify"
    if lowered.startswith("local:") or value.isdigit() or _looks_like_local_path(value):
        return "local"
    if extract_video_id(value) or value:
        return "youtube"

    active = _active_provider_id()
    if active:
        return _PLAY_PROVIDER_ALIASES.get(active, active)
    return "youtube"


async def _handle_search_mode(query: str, provider: str, limit: int, user_id: str) -> ToolResult:
    requested_provider, search_provider, filter_provider = _resolve_search_provider(provider, user_id)
    result = await _search_tracks_handler(query, limit=limit, provider=search_provider, user_id=user_id)
    if not result.ok:
        return result
    data = dict(result.data or {}) if isinstance(result.data, dict) else {}
    raw_tracks = [track for track in data.get("tracks", []) if isinstance(track, dict)]
    filtered_tracks = _apply_quality_filter(raw_tracks, filter_provider, query)
    shaped_tracks = [_shape_candidate(track) for track in filtered_tracks[:limit]]
    data.update(
        {
            "mode": "search",
            "tracks": shaped_tracks,
            "count": len(shaped_tracks),
            "provider": requested_provider,
            "resolved_provider": search_provider,
            "filter_provider": filter_provider,
            "query": query,
            # R3-P1-E (2026-05-30): retired next_step prose hint. The model
            # reads the tracks list with track_uri fields and decides
            # whether to call media again to play one; runtime-injected
            # next-call prose is the same boxing pattern compass deleted
            # from this file in commit 422012e7.
        }
    )
    data.setdefault("recently_played", [])
    return ToolResult(ok=True, data=data)


async def _handle_play_mode(track_uri: str, provider: str, target_room: str, user_id: str) -> ToolResult:
    resolved_provider = _infer_play_provider(provider, track_uri)
    if resolved_provider == "local":
        try:
            play_query = _resolve_local_file_path(track_uri)
        except ValueError as exc:
            return ToolResult(ok=False, data=None, error=str(exc))
        play_provider = "local"
    elif resolved_provider == "spotify":
        # TODO: Spotify CDP currently resolves playback by searching the web player.
        # Passing the Spotify URI preserves the exact identifier for the future direct-open path.
        play_query = _spotify_uri_from_track_uri(track_uri)
        play_provider = "spotify"
    elif resolved_provider == "youtube":
        play_query = extract_video_id(track_uri) or track_uri
        play_provider = "youtube"
    else:
        play_query = track_uri
        play_provider = resolved_provider

    result = await _play_music_handler(play_query, provider=play_provider, target_room=target_room, user_id=user_id)
    if isinstance(result.data, dict):
        data = dict(result.data)
        data.setdefault("mode", "play")
        data.setdefault("track_uri", track_uri)
        data.setdefault("resolved_provider", resolved_provider)
        return ToolResult(
            ok=result.ok,
            data=data,
            error=result.error,
            truncated=result.truncated,
            error_category=result.error_category,
            retryable=result.retryable,
            required_tier=result.required_tier,
        )
    return result


def _playback_started_evidence(play_ok: bool, play_data: object) -> bool:
    """Derive playback_started from structured play evidence, not a bare 200.

    The lane-3 MF-A chain minted ``playback_started: true`` from a coerced
    HTTP 200 while the backend play had raised. Beyond the (now honest)
    ``play_result.ok``, respect explicit structured not-played markers from
    the play pipeline. Structured result fields only — no prose parsing.
    """
    if not play_ok:
        return False
    if isinstance(play_data, dict):
        if play_data.get("ok") is False:
            return False
        if play_data.get("state") == "not_played":
            return False
        if play_data.get("playback_status") == "candidate_not_played":
            return False
    return True


# --- No-confident-match honesty (issue #1403) --------------------------------
# ``search_play`` auto-selects the top candidate and starts playback in one turn
# (the schema tells the model to use it for "play X"). Providers do NOT return a
# trustworthy relevance signal for that auto-commit decision: YouTube browser
# search returns *something* for any string, and the local provider's rapidfuzz
# WRatio inflates to 85+ for a gibberish query against an unrelated title (its
# token_set / partial components reward incidental token overlap on filler
# words). So a request for a song that does not exist used to auto-play an
# unrelated track and narrate success — trace cc5efd7deba5: query "blorptexican
# quazzlefrump by nonexistent artist zzyzx" -> local "Capital Steez ..." with
# match_score 85.5 -> playback_started true -> "Playing the closest match ...".
#
# The fix is a provider-agnostic, directional relevance check on the SELECTED
# candidate: how many of the distinctive tokens the USER asked for actually
# appear in the candidate's title/artist/album. This is match-quality DATA about
# the search result, not a query-intent classifier and not a model-output
# parser: when the top candidate does not correspond to the request the runtime
# plays nothing and hands the decision back to the model as structured data
# (``ok: false`` + ``no_confident_match``), and the model authors the honest
# reply itself — the deboxing rule (give the model context; don't steer it).
_MATCH_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MATCH_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "from", "feat", "ft", "prod", "your", "you", "song", "track", "play", "music"}
)
_MATCH_MIN_TOKEN_LEN = 3
_MATCH_FUZZY_CUTOFF = 85.0
_MATCH_CONFIDENT_COVERAGE = 0.5


def _match_tokens(text: str) -> list[str]:
    return [
        token
        for token in _MATCH_TOKEN_RE.findall(str(text or "").casefold())
        if len(token) >= _MATCH_MIN_TOKEN_LEN and token not in _MATCH_STOPWORDS
    ]


def _token_is_covered(token: str, candidate_text: str, candidate_tokens: list[str]) -> bool:
    if token in candidate_text:
        return True
    try:
        from rapidfuzz import fuzz
    except ImportError:
        # No fuzzy dep -> substring-only (typo tolerance lost, exact match kept).
        return False
    if fuzz.partial_ratio(token, candidate_text) >= _MATCH_FUZZY_CUTOFF:
        return True
    return any(fuzz.ratio(token, candidate_token) >= _MATCH_FUZZY_CUTOFF for candidate_token in candidate_tokens)


def _media_match_is_confident(query: str, track: dict[str, Any]) -> bool:
    """Does ``track`` confidently correspond to what the user asked for?

    Directional token coverage: at least half of the distinctive query tokens
    (len >= 3, minus a tiny filler set) must appear — as a substring or a
    fuzzy near-match (typo tolerance) — in the candidate title/artist/album.
    A query with no distinctive tokens (a bare mood/filler request) cannot be
    judged this way, so it is treated as confident (behaviour unchanged).
    """
    query_tokens = _match_tokens(query)
    if not query_tokens:
        return True
    candidate_text = " ".join(str(track.get(field) or "") for field in ("title", "artist", "album")).casefold()
    candidate_tokens = _match_tokens(candidate_text)
    covered = sum(1 for token in query_tokens if _token_is_covered(token, candidate_text, candidate_tokens))
    return covered / len(query_tokens) >= _MATCH_CONFIDENT_COVERAGE


def _select_playable_index(query: str, tracks: list[dict[str, Any]], *, generic_request: bool) -> int | None:
    """Index of the candidate to auto-play, or ``None`` for an honest no-match.

    A generic "just play music" request has no specific query, so its top
    candidate is played. Otherwise pick the FIRST candidate that confidently
    matches the request — not blindly ``tracks[0]``. Provider search often
    ranks a weak local false-match above the real track (the local rapidfuzz
    WRatio inflates unrelated titles), and the broad ``any`` search lists local
    before YouTube; scanning for the first real match keeps "play <song not in
    the local library>" a single turn instead of forcing the model to retry
    with a narrower provider (issue #1403 trace 2f6f4420ce69). When nothing
    matches, return ``None`` and let the caller report an honest no-match.
    """
    if not tracks:
        return None
    if generic_request:
        return 0
    for index, track in enumerate(tracks):
        if _media_match_is_confident(query, track):
            return index
    return None


async def _handle_search_play_mode(
    query: str,
    provider: str,
    limit: int,
    target_room: str,
    user_id: str,
    *,
    generic_request: bool = False,
) -> ToolResult:
    search_result = await _handle_search_mode(query, provider, limit, user_id)
    if not search_result.ok:
        return search_result

    data = dict(search_result.data or {}) if isinstance(search_result.data, dict) else {}
    tracks = [track for track in data.get("tracks", []) if isinstance(track, dict)]

    # Pure internal DATA fallback (NOT a model-steering hint): when the user did
    # not pin a provider, ``auto`` resolves to the active provider (e.g.
    # ``local``). If that provider returns no candidate that actually matches the
    # request — the song is not in the small local library, and the local
    # rapidfuzz WRatio over-matches unrelated titles (issue #1403) — widen once
    # to YouTube, the universal browser-search catalog. YouTube is used rather
    # than the broad ``any`` because ``any`` lists the crowding local matches
    # first and fills the result limit before YouTube is reached (trace
    # 2f6f4420ce69: an ``any`` search for "Blinding Lights The Weeknd" returned
    # ten local false-matches and zero YouTube results). Without this, the model
    # would be told there is no match for a song that is on YouTube — a false
    # no-match worse than the original bug. Skipped for a generic "just play
    # music" request, and an EXPLICIT provider is respected, never widened.
    if (
        _clean_provider(provider) == "auto"
        and not generic_request
        and _select_playable_index(query, tracks, generic_request=generic_request) is None
    ):
        resolved_provider = str(data.get("resolved_provider") or "").strip().lower()
        if resolved_provider and resolved_provider != "youtube":
            broad_result = await _handle_search_mode(query, "youtube", limit, user_id)
            if broad_result.ok and isinstance(broad_result.data, dict):
                broad_tracks = [track for track in broad_result.data.get("tracks", []) if isinstance(track, dict)]
                if _select_playable_index(query, broad_tracks, generic_request=generic_request) is not None:
                    data = dict(broad_result.data)
                    tracks = broad_tracks

    data.update(
        {
            "mode": "search_play",
            "action": "search_play",
            "candidate_count": len(tracks),
            "needs_selection": False,
            "playback_started": False,
            "generic_request": generic_request,
        }
    )
    if not tracks:
        # R3-P1-E: retired next_step prose hint. The model sees
        # needs_selection=True + error="No playable media candidates found"
        # and decides how to respond (ask the user, retry with a different
        # query, etc.) — runtime-injected "Ask the user for…" steers the
        # voice the model should use, which the parity bar forbids.
        data["needs_selection"] = True
        return ToolResult(ok=False, data=data, error="No playable media candidates found.")

    # No-confident-match honesty (issue #1403): pick the first candidate that
    # actually corresponds to the request. If none does — a request for a song
    # that does not exist — do NOT auto-play an unrelated track and narrate
    # success. Play nothing and hand the honest-no-match decision to the model
    # as structured DATA (no query classifier, no injected prose). A generic
    # "just play music" request has no specific query to match and is exempt.
    selected_index = _select_playable_index(query, tracks, generic_request=generic_request)
    if selected_index is None:
        data["needs_selection"] = True
        data["no_confident_match"] = True
        data["playback_started"] = False
        data["selected_track"] = dict(tracks[0])
        return ToolResult(ok=False, data=data, error="No confident match for the requested media.")

    selected = dict(tracks[selected_index])
    selected_track_uri = str(selected.get("track_uri") or "").strip()
    if not selected_track_uri:
        # R3-P1-E: retired next_step prose hint; the structured error
        # message already carries the signal.
        data["needs_selection"] = True
        data["selected_track"] = selected
        return ToolResult(ok=False, data=data, error="Top media candidate has no playable track_uri.")

    selected_provider = str(
        selected.get("playback_provider") or selected.get("provider") or data.get("playback_provider") or provider
    ).strip()
    play_result = await _handle_play_mode(selected_track_uri, selected_provider, target_room, user_id)
    play_data = dict(play_result.data or {}) if isinstance(play_result.data, dict) else play_result.data
    data.update(
        {
            "selected_track": selected,
            "selected_track_uri": selected_track_uri,
            "selected_provider": selected_provider,
            "tracks": [selected],
            "count": 1,
            "playback_started": _playback_started_evidence(play_result.ok, play_data),
            "play_result": play_data,
        }
    )
    if isinstance(play_data, dict):
        message = str(play_data.get("message") or "").strip()
        if message:
            data.setdefault("message", message)
        for _field in (
            "fallback_used",
            "fallback_from_provider",
            "fallback_to_provider",
            "fallback_reason",
            "login_url_to_show_user",
        ):
            if _field in play_data and _field not in data:
                data[_field] = play_data[_field]
    if play_result.ok:
        fallback_message = "Playing some music." if generic_request else "Playing %s." % query
        data.setdefault("message", fallback_message)
        data.setdefault("voice_summary", data["message"])
        # R3-P1-F (2026-05-30): retired ``terminal_response`` field. The
        # prior shape let ``intent/agent_executor.py:_terminal_response_from_tool_result``
        # short-circuit the model's final answer with this tool-authored
        # string. The model now sees ``message`` + ``voice_summary`` as
        # structured data and produces the one-line acknowledgement itself
        # (matching Claude Code TS, which issues one more assistant turn).
        return ToolResult(ok=True, data=data, truncated=play_result.truncated)

    # R3-P1-E: retired next_step prose hint. The play_result.error
    # already carries the failure signal; "Pick another candidate or
    # provider" is the model's call, not the runtime's.
    return ToolResult(
        ok=False,
        data=data,
        error=play_result.error or "Failed to play selected media candidate.",
        truncated=play_result.truncated,
        error_category=play_result.error_category,
        retryable=play_result.retryable,
        required_tier=play_result.required_tier,
    )


async def media_handler(
    query: str = "",
    *,
    track_uri: str = "",
    action: str = "auto",
    provider: str = "auto",
    limit: int = _DEFAULT_SEARCH_LIMIT,
    target_room: str = "",
    user_id: str = "",
) -> ToolResult:
    """Search media candidates or play one exact candidate by track_uri."""
    cleaned_track_uri = str(track_uri or "").strip()
    cleaned_query = str(query or "").strip()
    cleaned_action = str(action or "auto").strip().lower()
    cleaned_provider = str(provider or "auto").strip()
    cleaned_target_room = str(target_room or "").strip()
    if cleaned_action not in _MEDIA_ACTIONS:
        return ToolResult(
            ok=False,
            data=None,
            error="media action must be one of auto, search, play, or search_play.",
        )

    generic_request = False
    if not cleaned_track_uri and not cleaned_query and cleaned_action in {"auto", "search_play"}:
        cleaned_query = _GENERIC_MUSIC_QUERY
        cleaned_action = "search_play"
        generic_request = True

    # Cloud surface has no local runtime; route to the user-scoped cloud
    # playback session service (search/resolve/play in one call) instead of the
    # localhost /v1/play path, which errors for cloud users. Desktop is untouched.
    from intent.tools.cloud_music_bridge import (
        cloud_media,
        should_route_to_cloud_playback,
    )

    if should_route_to_cloud_playback():
        return await cloud_media(
            query=cleaned_query,
            track_uri=cleaned_track_uri,
            action=cleaned_action,
            source=None,
            user_id=user_id,
        )

    if cleaned_track_uri or cleaned_action == "play":
        if not cleaned_track_uri:
            return ToolResult(ok=False, data=None, error="Pass track_uri when action is play.")
        return await _handle_play_mode(cleaned_track_uri, cleaned_provider, cleaned_target_room, user_id)

    if not cleaned_query:
        return ToolResult(ok=False, data=None, error="Pass query to search or track_uri to play.")

    try:
        limited = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
    except (TypeError, ValueError):
        limited = _DEFAULT_SEARCH_LIMIT

    try:
        if cleaned_action == "search_play":
            return await _handle_search_play_mode(
                cleaned_query,
                cleaned_provider,
                limited,
                cleaned_target_room,
                user_id,
                generic_request=generic_request,
            )
        return await _handle_search_mode(cleaned_query, cleaned_provider, limited, user_id)
    except ValueError as exc:
        return ToolResult(ok=False, data=None, error="media search failed: %s" % exc)
