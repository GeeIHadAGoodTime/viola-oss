"""REST API endpoint for track rating.

Provides a unified POST /v1/track/rate endpoint that wraps the existing
rating system to support both local library likes and provider-agnostic
thumbs up/down ratings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)


class TrackRateRequest(BaseModel):
    """Request body for rating a track."""

    track_id: str = Field(..., min_length=1, description="Track or video ID to rate")
    rating: Literal["up", "down"] = Field(..., description="Rating direction: 'up' or 'down'")
    title: str | None = Field(None, description="Optional track title for metadata")
    artist: str | None = Field(None, description="Optional artist name for metadata")


def register_track_rating_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register track rating REST endpoints."""
    router = context.router

    @router.post("/v1/track/rate", dependencies=[Depends(require_auth)])
    async def rate_track(body: TrackRateRequest):
        """Rate a track with thumbs up or down.

        Delegates to the existing RatingSystem for provider-agnostic tracks,
        and also records a local library like if the track_id is a numeric
        local library ID.
        """

        async def _inner():
            try:
                track_id = body.track_id
                rating = body.rating
                title = body.title or "Unknown"
                artist = body.artist

                # Try the provider-agnostic rating system first
                try:
                    from music.rating_system import get_rating_system

                    rating_system = get_rating_system()
                    if rating == "up":
                        rating_system.thumbs_up(video_id=track_id, title=title, artist=artist)
                    else:
                        rating_system.thumbs_down(video_id=track_id, title=title, artist=artist)
                except Exception as exc:
                    log.warning("Rating system unavailable, skipping: %s", exc)

                # Also try local library like/unlike if track_id is numeric
                try:
                    numeric_id = int(track_id)
                    from music.providers.local.db import get_local_library_repo

                    repo = get_local_library_repo()
                    repo.initialize()
                    track = repo.get_track_by_id(numeric_id)
                    if track:
                        if rating == "up":
                            repo.add_like(numeric_id)
                        else:
                            repo.remove_like(numeric_id)
                except (ValueError, TypeError):
                    # Not a numeric ID, skip local library
                    pass
                except Exception as exc:
                    log.debug("Local library like/unlike skipped: %s", exc)

                log.info(
                    "Track rated: track_id=%s rating=%s title=%s",
                    track_id,
                    rating,
                    title,
                )
                return success_response({"track_id": track_id, "rating": rating})
            except Exception as exc:
                log.debug("Rate track failed: %s", exc)
                return handle_route_error(exc, "rate_track")

        return await toolbox.record_and_call(_inner, route="/v1/track/rate", method="POST")

    log.info("Track rating routes registered")


__all__ = ["register_track_rating_routes"]
