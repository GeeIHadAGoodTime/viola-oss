"""REST routes for the SQLite-backed named playlist store.

Endpoints
---------
GET    /v1/playlists              — list all playlists (name + track count)
POST   /v1/playlists              — create a named playlist  {name}
DELETE /v1/playlists/{name}       — delete a playlist by name
GET    /v1/playlists/{name}/tracks — list tracks in a playlist
POST   /v1/playlists/{name}/tracks — add a track           {provider, track_uri, title?, artist?}
DELETE /v1/playlists/{name}/tracks/{track_id} — remove a track
POST   /v1/playlists/{name}/play  — start playback (shuffled)
"""

from __future__ import annotations

from core.logging_config import get_logger
from fastapi import Body, Depends, Path, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)


def register_playlist_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register /v1/playlists routes onto the feature router."""
    router = context.router

    # ------------------------------------------------------------------
    # List playlists
    # ------------------------------------------------------------------

    @router.get("/v1/playlists", dependencies=[Depends(require_auth)])
    async def list_playlists(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                store = PlaylistStore()
                records = store.list_playlists(user_id)
                items = [
                    {
                        "name": pl.name,
                        "created_at": pl.created_at,
                        "track_count": len(store.get_tracks(user_id, pl.name)),
                    }
                    for pl in records
                ]
                return {"ok": True, "playlists": items, "count": len(items)}
            except Exception as exc:
                log.debug("list_playlists failed: %s", exc)
                return handle_route_error(exc, "list_playlists")

        return await toolbox.record_and_call(_inner, route="/v1/playlists", method="GET")

    # ------------------------------------------------------------------
    # Create playlist
    # ------------------------------------------------------------------

    @router.post("/v1/playlists", dependencies=[Depends(require_auth)])
    async def create_playlist(body: dict = Body(...), user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                name = (body.get("name") or "").strip()
                if not name:
                    from utils.api_helpers import error_response

                    return error_response("name_required", status_code=400, message="name is required")

                store = PlaylistStore()
                record = store.create_playlist(user_id, name)
                log.info("Created playlist %r", name)
                return {"ok": True, "playlist": {"id": record.id, "name": record.name, "created_at": record.created_at}}
            except ValueError as exc:
                from utils.api_helpers import error_response

                log.debug("create_playlist duplicate: %s", exc)
                return error_response(
                    "duplicate_playlist",
                    status_code=409,
                    message="A playlist with that name already exists. Please choose a different name.",
                )
            except Exception as exc:
                log.debug("create_playlist failed: %s", exc)
                return handle_route_error(exc, "create_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/playlists", method="POST")

    # ------------------------------------------------------------------
    # Delete playlist
    # ------------------------------------------------------------------

    @router.delete("/v1/playlists/{name}", dependencies=[Depends(require_auth)])
    async def delete_playlist(name: str = Path(...), user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                store = PlaylistStore()
                deleted = store.delete_playlist(user_id, name)
                if not deleted:
                    from utils.api_helpers import error_response

                    return error_response("not_found", status_code=404, message="Playlist %r not found" % name)
                log.info("Deleted playlist %r", name)
                return {"ok": True, "deleted": True, "name": name}
            except Exception as exc:
                log.debug("delete_playlist failed: %s", exc)
                return handle_route_error(exc, "delete_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/playlists/{name}", method="DELETE")

    # ------------------------------------------------------------------
    # List tracks
    # ------------------------------------------------------------------

    @router.get("/v1/playlists/{name}/tracks", dependencies=[Depends(require_auth)])
    async def list_tracks(name: str = Path(...), user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                store = PlaylistStore()
                if store.get_playlist(user_id, name) is None:
                    from utils.api_helpers import error_response

                    return error_response("not_found", status_code=404, message="Playlist %r not found" % name)
                tracks = store.get_tracks(user_id, name)
                items = [
                    {
                        "id": t.id,
                        "provider": t.provider,
                        "track_uri": t.track_uri,
                        "title": t.title,
                        "artist": t.artist,
                        "position": t.position,
                    }
                    for t in tracks
                ]
                return {"ok": True, "playlist_name": name, "tracks": items, "count": len(items)}
            except Exception as exc:
                log.debug("list_tracks failed: %s", exc)
                return handle_route_error(exc, "list_tracks")

        return await toolbox.record_and_call(_inner, route="/v1/playlists/{name}/tracks", method="GET")

    # ------------------------------------------------------------------
    # Add track
    # ------------------------------------------------------------------

    @router.post("/v1/playlists/{name}/tracks", dependencies=[Depends(require_auth)])
    async def add_track(name: str = Path(...), body: dict = Body(...), user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                provider = (body.get("provider") or "").strip()
                track_uri = (body.get("track_uri") or "").strip()
                title = body.get("title") or None
                artist = body.get("artist") or None

                if not provider:
                    from utils.api_helpers import error_response

                    return error_response("provider_required", status_code=400, message="provider is required")
                if not track_uri:
                    from utils.api_helpers import error_response

                    return error_response("track_uri_required", status_code=400, message="track_uri is required")

                store = PlaylistStore()
                record = store.add_track(
                    user_id=user_id,
                    playlist_name=name,
                    provider=provider,
                    track_uri=track_uri,
                    title=title,
                    artist=artist,
                )
                log.info("Added track %r to playlist %r", track_uri, name)
                return {
                    "ok": True,
                    "track": {
                        "id": record.id,
                        "provider": record.provider,
                        "track_uri": record.track_uri,
                        "title": record.title,
                        "artist": record.artist,
                        "position": record.position,
                    },
                }
            except ValueError as exc:
                from utils.api_helpers import error_response

                log.debug("add_track playlist not found: %s", exc)
                return error_response(
                    "not_found",
                    status_code=404,
                    message="That playlist doesn't exist. Please check the name and try again.",
                )
            except Exception as exc:
                log.debug("add_track failed: %s", exc)
                return handle_route_error(exc, "add_track")

        return await toolbox.record_and_call(_inner, route="/v1/playlists/{name}/tracks", method="POST")

    # ------------------------------------------------------------------
    # Remove track
    # ------------------------------------------------------------------

    @router.delete("/v1/playlists/{name}/tracks/{track_id}", dependencies=[Depends(require_auth)])
    async def remove_track(
        name: str = Path(...), track_id: int = Path(...), user_id: str = Depends(get_current_user_id)
    ):
        async def _inner():
            from music.playlists.playlist_store import PlaylistStore

            try:
                store = PlaylistStore()
                deleted = store.remove_track(user_id, track_id)
                if not deleted:
                    from utils.api_helpers import error_response

                    return error_response("not_found", status_code=404, message="Track id=%d not found" % track_id)
                log.info("Removed track id=%d from playlist %r", track_id, name)
                return {"ok": True, "deleted": True, "track_id": track_id}
            except Exception as exc:
                log.debug("remove_track failed: %s", exc)
                return handle_route_error(exc, "remove_track")

        return await toolbox.record_and_call(_inner, route="/v1/playlists/{name}/tracks/{track_id}", method="DELETE")

    # ------------------------------------------------------------------
    # Play playlist
    # ------------------------------------------------------------------

    @router.post("/v1/playlists/{name}/play", dependencies=[Depends(require_auth)])
    async def play_playlist(name: str = Path(...), user_id: str = Depends(get_current_user_id)):
        async def _inner():
            from music.playlists.playlist_player import PlaylistPlayer
            from music.playlists.playlist_store import PlaylistStore

            try:
                store = PlaylistStore()
                if store.get_playlist(user_id, name) is None:
                    from utils.api_helpers import error_response

                    return error_response("not_found", status_code=404, message="Playlist %r not found" % name)

                player = PlaylistPlayer(store=store)
                started = await player.play(user_id, name, shuffle=True)
                if not started:
                    from utils.api_helpers import error_response

                    return error_response(
                        "playlist_empty",
                        status_code=422,
                        message="Playlist %r has no tracks to play" % name,
                    )
                track_count = player.get_track_count(user_id, name)
                log.info("Started playlist %r (%d tracks)", name, track_count)
                return {"ok": True, "playlist_name": name, "track_count": track_count, "shuffle": True}
            except Exception as exc:
                log.debug("play_playlist failed: %s", exc)
                return handle_route_error(exc, "play_playlist")

        return await toolbox.record_and_call(_inner, route="/v1/playlists/{name}/play", method="POST")
