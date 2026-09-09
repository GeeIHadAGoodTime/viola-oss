from __future__ import annotations

from core.logging_config import get_logger
from fastapi import Body, Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error
from utils.api_helpers import error_response

log = get_logger(__name__)


def register_rating_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router

    @router.post("/v1/rating/thumbs-up", dependencies=[Depends(require_auth)])
    async def thumbs_up_song(body: dict = Body(...)):
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                video_id = body.get("video_id")
                title = body.get("title", "Unknown")
                artist = body.get("artist")
                if not video_id:
                    return error_response("video_id required", status_code=400)

                rating_system = get_rating_system()
                rating_system.thumbs_up(video_id=video_id, title=title, artist=artist)

                log.info("thumbs up: %s - %s (%s)", title, artist, video_id)
                return {"ok": True, "video_id": video_id, "rating": "thumbs_up"}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "thumbs_up_song")

        return await toolbox.record_and_call(_inner, route="/v1/rating/thumbs-up", method="POST")

    @router.post("/v1/rating/thumbs-down", dependencies=[Depends(require_auth)])
    async def thumbs_down_song(body: dict = Body(...)):
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                video_id = body.get("video_id")
                title = body.get("title", "Unknown")
                artist = body.get("artist")
                if not video_id:
                    return error_response("video_id required", status_code=400)

                rating_system = get_rating_system()
                rating_system.thumbs_down(video_id=video_id, title=title, artist=artist)

                log.info("thumbs down: %s - %s (%s)", title, artist, video_id)
                return {"ok": True, "video_id": video_id, "rating": "thumbs_down"}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "thumbs_down_song")

        return await toolbox.record_and_call(_inner, route="/v1/rating/thumbs-down", method="POST")

    @router.post("/v1/rating", dependencies=[Depends(require_auth)])
    async def set_rating(body: dict = Body(...)):
        """Unified rating endpoint for React UI.

        Accepts: { rating: 'liked' | 'disliked' | null }
        Gets current track info from playback state.
        """

        async def _inner():
            from music.rating_system import get_rating_system

            try:
                rating_value = body.get("rating")  # 'liked', 'disliked', or null

                # Get current track info from playback state
                from ui.core.bindings import get_bindings

                bindings = get_bindings()
                music = bindings.music if bindings else None

                if not music:
                    return error_response("Music backend not available", status_code=400)

                # Get current track
                current_track = getattr(music, "current_track", None)
                if not current_track:
                    # Try alternative methods
                    state = getattr(music, "get_state", lambda: None)()
                    if state:
                        current_track = (
                            getattr(state, "now_playing", None) or state.get("now_playing")
                            if isinstance(state, dict)
                            else None
                        )

                if not current_track:
                    return error_response("No track currently playing", status_code=400)

                # Extract track info
                if hasattr(current_track, "video_id"):
                    video_id = current_track.video_id
                    title = getattr(current_track, "title", "Unknown")
                    artist = getattr(current_track, "artist", None)
                elif isinstance(current_track, dict):
                    video_id = current_track.get("video_id") or current_track.get("id")
                    title = current_track.get("title", "Unknown")
                    artist = current_track.get("artist")
                else:
                    return error_response("Could not get track info", status_code=400)

                if not video_id:
                    return error_response("Track has no video_id", status_code=400)

                rating_system = get_rating_system()

                if rating_value == "liked":
                    rating_system.thumbs_up(video_id=video_id, title=title, artist=artist)
                    log.info("👍 Song rated (liked): %s - %s (%s)", title, artist, video_id)
                    return {"ok": True, "video_id": video_id, "rating": "liked"}
                elif rating_value == "disliked":
                    rating_system.thumbs_down(video_id=video_id, title=title, artist=artist)
                    log.info(
                        "👎 Song rated (disliked): %s - %s (%s)",
                        title,
                        artist,
                        video_id,
                    )
                    return {"ok": True, "video_id": video_id, "rating": "disliked"}
                elif rating_value is None:
                    rating_system.remove_rating(video_id)
                    log.info("🔄 Rating removed: %s - %s (%s)", title, artist, video_id)
                    return {"ok": True, "video_id": video_id, "rating": None}
                else:
                    return error_response(f"Invalid rating value: {rating_value}", status_code=400)
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "set_rating")

        return await toolbox.record_and_call(_inner, route="/v1/rating", method="POST")

    @router.get("/v1/rating/status", dependencies=[Depends(require_auth)])
    async def get_rating_status(video_id: str):
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                rating_system = get_rating_system()
                rating = rating_system.get_rating(video_id)
                # Return rating object with normalized status string for easier client parsing
                if rating is None:
                    return {
                        "ok": True,
                        "video_id": video_id,
                        "rating": None,
                        "status": None,
                    }
                rating_dict = rating.to_dict() if hasattr(rating, "to_dict") else rating
                status = "liked" if rating.rating == 1 else "disliked" if rating.rating == -1 else None
                return {
                    "ok": True,
                    "video_id": video_id,
                    "rating": rating_dict,
                    "status": status,
                }
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "get_rating_status")

        return await toolbox.record_and_call(_inner, route="/v1/rating/status", method="GET")

    @router.delete("/v1/rating/{video_id}", dependencies=[Depends(require_auth)])
    async def remove_rating(video_id: str):
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                rating_system = get_rating_system()
                rating_system.remove_rating(video_id)
                log.info("🔄 Rating removed for: %s", video_id)
                return {"ok": True, "video_id": video_id, "message": "Rating removed"}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "remove_rating")

        return await toolbox.record_and_call(_inner, route="/v1/rating/{video_id}", method="DELETE")

    @router.get("/v1/rating/favorites", dependencies=[Depends(require_auth)])
    async def get_favorites(limit: int = 20):
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                rating_system = get_rating_system()
                favorites = rating_system.get_favorites(limit=limit)
                return {
                    "ok": True,
                    "favorites": [favorite.to_dict() for favorite in favorites],
                    "count": len(favorites),
                }
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "get_favorites")

        return await toolbox.record_and_call(_inner, route="/v1/rating/favorites", method="GET")

    @router.get("/v1/rating/statistics", dependencies=[Depends(require_auth)])
    async def get_rating_statistics():
        async def _inner():
            from music.rating_system import get_rating_system

            try:
                rating_system = get_rating_system()
                stats = rating_system.get_statistics()
                return {"ok": True, "statistics": stats}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "get_rating_statistics")

        return await toolbox.record_and_call(_inner, route="/v1/rating/statistics", method="GET")

    log.info("⭐ Rating routes registered")


__all__ = ["register_rating_routes"]
