"""Agent tool handlers for playlist management.

These functions are called by the MCP tool wrappers in
``mcp_servers/core_tools/server.py``.

Uses the SQLite-backed ``PlaylistStore`` for CRUD and ``PlaylistPlayer``
for playback.  The old JSON-based ``PlaylistManager`` is no longer used
here so that all playlist data lives in one place (the SQLite store).
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# Verification verdicts for a playback claim, matching intent.tools.music_tools
# so both music surfaces label the same evidence the same way.
VERIFY_PLAYING = "live_state_playing"
VERIFY_NOT_PLAYING = "live_state_not_playing"
VERIFY_UNAVAILABLE = "runtime_state_unavailable"


def _runtime_url(path: str) -> str:
    from config.settings import settings

    port = getattr(settings, "api_port", None) or 8756
    return "http://127.0.0.1:%s%s" % (port, path)


def _auth_headers() -> dict[str, str]:
    # The local runtime requires auth by default; /v1/* is not exempt, so a
    # request without the bootstrap key returns 401.
    from ui.security.bootstrap import load_bootstrap_api_key

    headers: dict[str, str] = {}
    api_key = load_bootstrap_api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


async def _post_runtime(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 15.0) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(_runtime_url(path), json=payload or {}, headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {"ok": False, "error": "Runtime returned a non-object response"}
    data.setdefault("_status_code", resp.status_code)
    return data


async def _get_runtime(path: str, *, timeout: float = 5.0) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(_runtime_url(path), headers=_auth_headers())
        data = resp.json()

    if not isinstance(data, dict):
        return {"ok": False, "error": "Runtime returned a non-object response"}
    data.setdefault("_status_code", resp.status_code)
    return data


def _runtime_call_errors() -> tuple[type[BaseException], ...]:
    """Exception types a local-runtime HTTP call can realistically raise.

    Resolved lazily because ``httpx`` is imported lazily inside
    ``_post_runtime`` / ``_get_runtime``. Narrow on purpose: a genuinely
    unexpected exception should keep travelling rather than be quietly turned
    into "the probe failed".
    """
    base: tuple[type[BaseException], ...] = (OSError, ValueError, RuntimeError, TypeError, AttributeError)
    try:
        import httpx
    except ImportError:
        return base
    http_error = getattr(httpx, "HTTPError", None)
    if isinstance(http_error, type) and issubclass(http_error, BaseException):
        return (http_error, *base)
    return base


def _runtime_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    if error:
        return str(error)
    message = payload.get("message")
    if message:
        return str(message)
    return "Request failed"


def _playable_query(track: dict[str, Any]) -> str:
    """The string the runtime can actually resolve for a playlist entry."""
    url = str(track.get("url") or "").strip()
    title = str(track.get("title") or "").strip()
    artist = str(track.get("artist") or "").strip()
    named = "%s %s" % (title, artist) if title and artist else title

    if url.startswith("ytsearch1:"):
        # A search directive, not a locator -- send the human-readable name
        # when we have one so the runtime resolves it the usual way.
        return named or url[len("ytsearch1:") :].strip()
    if url:
        return url
    return named


async def _confirm_runtime_playback() -> tuple[str, dict[str, Any] | None]:
    """Ask the player what it is actually doing.

    Returns ``(verification, now_playing)``. The verdict distinguishes "the
    player says it is playing this track" from "the player says nothing is
    playing" from "the player could not be reached" -- collapsing the last two
    into a bare failure-to-find is what let a caller treat an unanswered probe
    as confirmation.
    """
    try:
        payload = await _get_runtime("/v1/player/state", timeout=5.0)
    except _runtime_call_errors() as exc:
        logger.debug("player-state probe failed: %s", exc)
        return VERIFY_UNAVAILABLE, None
    if not payload.get("ok"):
        return VERIFY_UNAVAILABLE, None

    state = payload.get("data")
    if not isinstance(state, dict):
        state = payload
    now = state.get("now_playing") or state.get("current")
    now = now if isinstance(now, dict) else None
    title = str(now.get("title") or now.get("name") or "").strip() if now else ""
    if state.get("is_playing") and title:
        return VERIFY_PLAYING, now
    return VERIFY_NOT_PLAYING, now


async def create_playlist_handler(
    name: str,
    url: str = "",
    provider: str = "youtube_music",
    shuffle: bool = True,
    user_id: str = "",
) -> ToolResult:
    """Create a new named playlist in Viola's SQLite store.

    Args:
        name: Friendly name (e.g. "workout", "chill Sunday").
        url: Ignored — kept for backwards-compatibility with the old JSON
             playlist manager interface.  Individual tracks are added via
             ``add_track_to_playlist``.
        provider: Default provider hint (unused at creation time).
        shuffle: Stored for display; actual shuffling happens at play time.

    Returns:
        ToolResult confirming the playlist was created or an error.
    """
    from music.playlists.playlist_store import PlaylistStore

    name = name.strip()
    if not name:
        return ToolResult(ok=False, data=None, error="Playlist name cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        store = PlaylistStore()
        # Async-native: the cloud `playlist` tool runs ON the FastAPI serving loop,
        # so sync store methods (Postgres `_run` bridge) would raise
        # SyncBridgeLoopError. Await the async-native variants (CL-20260711-afd7).
        await store.create_playlist_async(resolved_user_id, name)
        return ToolResult(
            ok=True,
            data="Playlist %r created. Use add_track_to_playlist to add tracks." % name,
        )
    except ValueError as exc:
        # create_playlist raises ValueError for duplicates
        msg = str(exc)
        if "already exists" in msg:
            msg += " Use playlist_add_track to add tracks to it, or list_playlists to see its contents."
        return ToolResult(ok=False, data=None, error=msg)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="create_playlist failed: %s" % exc)


async def list_playlists_handler(user_id: str = "") -> ToolResult:
    """List all saved playlists.

    Returns:
        ToolResult with a formatted list of playlists or an empty message.
    """
    from music.playlists.playlist_store import PlaylistStore

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        store = PlaylistStore()
        # Async-native: cloud `playlist` tool on the serving loop (CL-20260711-afd7).
        playlists = await store.list_playlists_async(resolved_user_id)
        if not playlists:
            return ToolResult(
                ok=True,
                data="No playlists saved. Use create_playlist to add one.",
            )
        lines = []
        for i, pl in enumerate(playlists, 1):
            track_count = len(await store.get_tracks_async(resolved_user_id, pl.name))
            lines.append("%d. %s (%d tracks)" % (i, pl.name, track_count))
        return ToolResult(ok=True, data="\n".join(lines))
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="list_playlists failed: %s" % exc)


async def delete_playlist_handler(name: str, user_id: str = "") -> ToolResult:
    """Delete a saved playlist by name.

    Args:
        name: Exact name of the playlist to delete (case-insensitive).

    Returns:
        ToolResult confirming deletion or an error if not found.
    """
    from music.playlists.playlist_store import PlaylistStore

    name = name.strip()
    if not name:
        return ToolResult(ok=False, data=None, error="Playlist name cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        store = PlaylistStore()
        # Async-native: cloud `playlist` tool on the serving loop (CL-20260711-afd7).
        deleted = await store.delete_playlist_async(resolved_user_id, name)
        if deleted:
            return ToolResult(ok=True, data="Playlist %r deleted." % name)
        return ToolResult(ok=False, data=None, error="Playlist %r not found." % name)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="delete_playlist failed: %s" % exc)


async def play_playlist_handler(name: str, user_id: str = "") -> ToolResult:
    """Play a saved playlist by name (MCP-callable, shuffled).

    Arming the playlist session is not playback. ``PlaylistPlayer`` caches the
    tracks on the playback session controller and flips the playback mode so
    autoplay can refill from the playlist, but it hands no track to a player
    and reads no player state -- so the old "Playing <name> - N songs,
    shuffled." was minted from an in-memory object having been constructed.
    This handler now starts the first track through the same local runtime the
    rest of the music tools use, then reads the player back, and reports which
    of three things happened: it is playing, it is not, or the player could not
    be reached to say.

    Args:
        name: Playlist name to play (case-insensitive).

    Returns:
        ToolResult with playback status.
    """
    from music.playlists.playlist_player import PlaylistPlayer
    from music.playlists.playlist_store import PlaylistStore

    name = name.strip()
    if not name:
        return ToolResult(ok=False, data=None, error="Playlist name cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        store = PlaylistStore()
        # Async-native: cloud `playlist` tool on the serving loop (CL-20260711-afd7).
        playlist = await store.get_playlist_async(resolved_user_id, name)
        if playlist is None:
            all_playlists = await store.list_playlists_async(resolved_user_id)
            all_names = [pl.name for pl in all_playlists]
            hint = "Available: %s" % ", ".join(all_names) if all_names else "No playlists saved yet."
            return ToolResult(
                ok=False,
                data=None,
                error="Playlist %r not found. %s" % (name, hint),
            )

        player = PlaylistPlayer(store=store)
        outcome = await player.arm(resolved_user_id, name, shuffle=True)
        if not outcome.session_armed:
            if outcome.reason == "playlist_session_error":
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Could not load playlist %r into the player." % name,
                )
            return ToolResult(
                ok=False,
                data=None,
                error=(
                    "Could not start playlist %r — it has no tracks. "
                    "Use search_tracks to find songs, then playlist_add_track "
                    "to add them before retrying play_playlist."
                )
                % name,
            )

        first_track = outcome.first_track or {}
        play_query = _playable_query(first_track)
        if not play_query:
            return ToolResult(
                ok=False,
                data=None,
                error="Playlist %r has tracks, but none of them carry a playable URL or title." % name,
            )

        response = await _post_runtime("/v1/play", {"query": play_query})
        if int(response.get("_status_code", 0) or 0) != 200 or not response.get("ok"):
            return ToolResult(
                ok=False,
                data={
                    "playlist": name,
                    "track_count": outcome.track_count,
                    "requested_track": first_track.get("title") or play_query,
                    "response": response,
                },
                error=_runtime_error(response),
            )

        verification, now_playing = await _confirm_runtime_playback()
        data: dict[str, Any] = {
            "playlist": name,
            "track_count": outcome.track_count,
            "queued_after_current": outcome.remaining_in_session,
            "shuffled": True,
            "requested_track": first_track.get("title") or play_query,
            "verification": verification,
            "playback_verified": verification == VERIFY_PLAYING,
        }
        if now_playing:
            data["now_playing"] = now_playing

        if verification == VERIFY_PLAYING:
            playing_label = str(now_playing.get("title") or "").strip() if now_playing else ""
            data["message"] = "Playing %r \u2014 %d songs, shuffled.%s" % (
                name,
                outcome.track_count,
                (" Now playing: %s." % playing_label) if playing_label else "",
            )
            return ToolResult(ok=True, data=data)

        if verification == VERIFY_UNAVAILABLE:
            data["message"] = (
                "I started playlist %r (%d songs, shuffled) but could not reach the player to confirm it is playing."
                % (name, outcome.track_count)
            )
        else:
            data["message"] = (
                "I started playlist %r (%d songs, shuffled), but the player does not report anything playing yet."
                % (name, outcome.track_count)
            )
        return ToolResult(ok=True, data=data, unverified=True)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="play_playlist failed: %s" % exc)


async def play_favorites_handler(
    shuffle: bool = True,
    limit: int = 15,
    user_id: str = "",
) -> ToolResult:
    """Start playback from the user's liked/favorited songs.

    This MCP-facing helper keeps the agent on a single tool call for
    favorites-style requests like "play something I like". It intentionally
    returns a user-facing empty-state message when no favorites exist so the
    agent does not burn extra steps inventing a fallback mood search.
    """
    import random

    from music.rating_system import get_rating_system_async

    resolved_user_id = str(user_id or "").strip()
    if not resolved_user_id:
        # Favorites are per-user. The MCP wrapper injects the caller's user_id
        # but does not establish a user_scope, so we must NOT drop it and fall
        # back to the ambient/device rating store (that merges favorites across
        # accounts). Fail loudly instead.
        raise ValueError("user_id is required for multi-user data isolation")

    try:
        capped_limit = max(1, min(int(limit), 30))
    except Exception:
        capped_limit = 15

    try:
        # Async-native construction/load: this handler runs ON the cloud serving
        # loop, where the first sync ``get_rating_system()`` would build the
        # singleton and trip the Postgres ``_run`` bridge (CL-20260711-afd7).
        rating_system = await get_rating_system_async()
        favorites = list(rating_system.get_favorites(user_id=resolved_user_id) or [])
        if not favorites:
            return ToolResult(
                ok=True,
                data={
                    "message": "You haven't liked any songs yet. Use thumbs up on songs you want me to remember.",
                    "total_songs": 0,
                    "shuffled": bool(shuffle),
                },
            )

        playable: list[dict[str, str]] = []
        for fav in favorites:
            query = str(
                getattr(fav, "video_id", "")
                or getattr(fav, "url", "")
                or getattr(fav, "provider_track_id", "")
                or getattr(fav, "title", "")
                or ""
            ).strip()
            if not query:
                continue
            playable.append(
                {
                    "query": query,
                    "title": str(getattr(fav, "title", "") or "").strip(),
                    "artist": str(getattr(fav, "artist", "") or "").strip(),
                }
            )

        if not playable:
            return ToolResult(
                ok=True,
                data={
                    "message": "Your favorites are empty or missing playable tracks right now.",
                    "total_songs": 0,
                    "shuffled": bool(shuffle),
                },
            )

        if shuffle and len(playable) > capped_limit:
            selected = random.sample(playable, capped_limit)
        else:
            selected = playable[:capped_limit]
        if shuffle:
            random.shuffle(selected)

        first = selected[0]
        payload = await _post_runtime("/v1/play", {"query": first["query"]})

        if int(payload.get("_status_code", 0) or 0) != 200 or not payload.get("ok"):
            return ToolResult(ok=False, data=None, error=_runtime_error(payload))

        # /v1/play's plain (no target_room) success response is
        # {"ok": True, "enqueued": {...}}, which ensure_envelope
        # (contracts/api_response.py) nests under "data" on the wire since
        # the handler set no top-level "data" key itself -- so the real
        # enqueued track lives at payload["data"]["enqueued"], not at
        # payload["data"] directly (same envelope-nesting class as #4787).
        envelope_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        enqueued = payload.get("enqueued") or envelope_data.get("enqueued") or {}
        # Only the runtime knows what it resolved. Falling all the way back to
        # the favorite record (and then to a generic phrase) reports the
        # REQUEST as the outcome, so flag the provenance when that happens.
        resolved_title = str(enqueued.get("title") or "").strip()
        title = resolved_title or str(first["title"] or "one of your favorite songs")
        artist = str(enqueued.get("artist") or first["artist"] or "")

        verification, now_playing = await _confirm_runtime_playback()
        data: dict[str, Any] = {
            "title": title,
            "artist": artist,
            # Only ONE track is actually started via /v1/play; the rest of
            # `selected` is never enqueued. Report the real count so the
            # model does not tell the user it queued a 15-song session.
            "total_songs": 1,
            "favorites_available": len(playable),
            "shuffled": bool(shuffle),
            "verification": verification,
            "playback_verified": verification == VERIFY_PLAYING,
        }
        if not resolved_title:
            data["title_unverified"] = True
            data["title_source"] = "favorite_record"
        if now_playing:
            data["now_playing"] = now_playing

        if verification == VERIFY_PLAYING:
            live_title = str(now_playing.get("title") or "").strip() if now_playing else ""
            label = live_title or title
            live_artist = str(now_playing.get("artist") or "").strip() if now_playing else ""
            if live_title:
                data["title"] = live_title
                data.pop("title_unverified", None)
                data.pop("title_source", None)
                if live_artist:
                    data["artist"] = live_artist
            message = "Playing one of your favorite songs now: %s" % label
            if data["artist"]:
                message += " by %s" % data["artist"]
            data["message"] = message
            return ToolResult(ok=True, data=data)

        if verification == VERIFY_UNAVAILABLE:
            data["message"] = (
                "I asked the player for one of your favorites (%s) but could not reach it to confirm it is playing."
                % title
            )
        else:
            data["message"] = (
                "I asked the player for one of your favorites (%s), but it does not report anything playing yet."
                % title
            )
        return ToolResult(ok=True, data=data, unverified=True)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="play_favorites failed: %s" % exc)


async def add_track_to_playlist_handler(
    playlist_name: str,
    provider: str,
    track_uri: str,
    title: str = "",
    artist: str = "",
    user_id: str = "",
) -> ToolResult:
    """Add a track to a named playlist.

    Args:
        playlist_name: Target playlist name.
        provider: Music provider (youtube, spotify, local).
        track_uri: Track URI or URL to add.
        title: Optional track title for display.
        artist: Optional artist name for display.

    Returns:
        ToolResult confirming the track was added or an error.
    """
    from music.playlists.playlist_store import PlaylistStore

    playlist_name = playlist_name.strip()
    track_uri = track_uri.strip()

    if not playlist_name:
        return ToolResult(ok=False, data=None, error="Playlist name cannot be empty.")
    if not track_uri:
        return ToolResult(ok=False, data=None, error="Track URI cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        store = PlaylistStore()
        # Async-native: cloud `playlist` tool on the serving loop (CL-20260711-afd7).
        await store.add_track_async(
            user_id=resolved_user_id,
            playlist_name=playlist_name,
            provider=provider,
            track_uri=track_uri,
            title=title or None,
            artist=artist or None,
        )
        display = title if title else track_uri
        return ToolResult(
            ok=True,
            data="Added %r to playlist %r." % (display, playlist_name),
        )
    except ValueError as exc:
        # add_track raises ValueError when playlist not found
        return ToolResult(
            ok=False,
            data=None,
            error="%s Use create_playlist first." % exc,
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="add_track_to_playlist failed: %s" % exc)
