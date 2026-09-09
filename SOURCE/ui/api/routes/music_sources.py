"""Music source status and selection routes."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth


def register_music_source_routes(context: ApiContext) -> None:
    """Register music source routes on the feature router."""

    router = context.router

    @router.get("/v1/music/sources", tags=["music"], dependencies=[Depends(require_auth)])
    async def list_music_sources(user_id: str = Depends(get_current_user_id)) -> dict[str, Any]:
        from services.connectors.status import build_music_sources_payload

        return success_response(await build_music_sources_payload(user_id))

    @router.post("/v1/music/sources/{source_id}/select", tags=["music"], dependencies=[Depends(require_auth)])
    async def select_music_source(source_id: str, user_id: str = Depends(get_current_user_id)) -> Any:
        from services.connectors.status import select_music_source as select_source

        try:
            return success_response(select_source(user_id, source_id))
        except ValueError as exc:
            return JSONResponse(
                status_code=404,
                content=failure_response("unknown_music_source", str(exc)),
            )


__all__ = ["register_music_source_routes"]
