from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response
from core.logging_config import get_logger
from fastapi import Body, Depends, Path
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth, require_operator_auth
from ui.api.routes.common import RouteToolbox
from ui.api.services import QueueService, QueueServiceError
from ui.core.player_state import to_player_state as _to_player_state

log = get_logger(__name__)


def _queue_service_error_response(exc: QueueServiceError, operation: str) -> JSONResponse:
    """Sanitize queue service errors before they are returned to clients."""
    status_code_obj = getattr(exc, "status_code", 400)
    status_code = int(status_code_obj) if isinstance(status_code_obj, int) else 400
    error_code = str(getattr(exc, "error_code", "queue_error") or "queue_error")
    messages = {
        "cannot_remove_current_item": "You cannot remove the song that is currently playing. Skip it instead.",
        "invalid_index": "Please choose a valid place in the queue.",
        "item_not_found": "The selected queue item was not found.",
        "missing_item_id": "Please choose an item from the queue first.",
        "not_supported": "This queue action is not available right now.",
        "play_not_supported": "Playing a queue item directly is not available right now.",
        "provider_not_linked": "This track needs a linked music provider before it can play.",
        "queue_error": "The queue request could not be completed.",
        "reorder_not_supported": "Reordering the queue is not available right now.",
    }
    log.warning("Queue operation failed (%s): %s", operation, exc)
    return JSONResponse(
        status_code=status_code,
        content=failure_response(
            error_code,
            messages.get(error_code, "The queue request could not be completed."),
        ),
    )


def register_queue_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    service = QueueService(
        music=context.bindings.music,
        state=context.bindings.state,
        hub=context.hub,
        app=context.app,
    )

    @router.get("/v1/queue", dependencies=[Depends(require_auth)])
    async def get_queue():
        async def _inner():
            return await service.snapshot()

        return await toolbox.record_and_call(_inner, route="/v1/queue", method="GET")

    @router.post("/v1/queue/clear", dependencies=[Depends(require_operator_auth)])
    async def clear_queue():
        async def _inner():
            try:
                return await service.clear()
            except QueueServiceError as exc:
                return _queue_service_error_response(exc, "clear_queue")

        return await toolbox.record_and_call(_inner, route="/v1/queue/clear", method="POST")

    @router.post("/v1/queue/reorder", dependencies=[Depends(require_operator_auth)])
    async def reorder_queue(body: dict[str, Any] = Body(...)):
        async def _inner():
            try:
                return await service.reorder(body.get("from_index"), body.get("to_index"))
            except QueueServiceError as exc:
                return _queue_service_error_response(exc, "reorder_queue")

        return await toolbox.record_and_call(_inner, route="/v1/queue/reorder", method="POST")

    @router.post("/v1/queue/play", dependencies=[Depends(require_operator_auth)])
    async def play_queue_item(body: dict[str, Any] = Body(...)):
        async def _inner():
            try:
                return await service.play_item(body.get("item_id"))
            except QueueServiceError as exc:
                return _queue_service_error_response(exc, "play_queue_item")

        return await toolbox.record_and_call(_inner, route="/v1/queue/play", method="POST")

    @router.delete("/v1/queue/item/{item_id}", dependencies=[Depends(require_operator_auth)])
    async def delete_queue_item(item_id: str = Path(...)):
        async def _inner():
            try:
                return await service.remove_item(item_id)
            except QueueServiceError as exc:
                return _queue_service_error_response(exc, "delete_queue_item")

        return await toolbox.record_and_call(_inner, route=f"/v1/queue/item/{item_id}", method="DELETE")

    @router.get("/v1/queue/mini-preview", dependencies=[Depends(require_auth)])
    async def get_queue_mini_preview(max_items: int = 3):
        async def _inner():
            try:
                from ui.queue_discovery import get_queue_discovery_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                queue_sys = get_queue_discovery_system(context.bindings.music, settings_mgr)
                preview = queue_sys.get_mini_queue_preview(max_items)
                return {"ok": True, **preview}
            except Exception as exc:
                log.error("Failed to get mini queue preview: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "queue_preview_unavailable",
                        "Queue preview is temporarily unavailable.",
                        data={
                            "items": [],
                            "total_items": 0,
                            "has_more": False,
                        },
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/queue/mini-preview", method="GET")

    @router.get("/v1/queue/recommendations", dependencies=[Depends(require_auth)])
    async def get_queue_recommendations(max_items: int = 5):
        async def _inner():
            try:
                from ui.queue_discovery import get_queue_discovery_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                queue_sys = get_queue_discovery_system(context.bindings.music, settings_mgr)

                hub_authority = getattr(context.app.state, "hub_state_authority", None)
                current_state = _to_player_state(
                    context.bindings.music,
                    context.bindings.state,
                    hub_authority=hub_authority,
                )
                current_song: dict[str, Any] | None = None
                if current_state.now_playing:
                    if isinstance(current_state.now_playing, dict):
                        current_song = current_state.now_playing
                    else:
                        current_song = {
                            "video_id": getattr(current_state.now_playing, "video_id", None),
                            "title": getattr(current_state.now_playing, "title", "Unknown"),
                            "artist": getattr(current_state.now_playing, "artist", None),
                        }

                recommendations = await queue_sys.get_recommendations(current_song, max_items)
                return {
                    "ok": True,
                    "recommendations": [rec.to_dict() for rec in recommendations],
                }
            except Exception as exc:
                log.error("Failed to get queue recommendations: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "queue_recommendations_unavailable",
                        "Queue recommendations are temporarily unavailable.",
                        data={"recommendations": []},
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/queue/recommendations", method="GET")

    @router.get("/v1/queue/recently-played", dependencies=[Depends(require_auth)])
    async def get_recently_played(max_items: int = 10):
        async def _inner():
            try:
                from ui.queue_discovery import get_queue_discovery_system
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()
                queue_sys = get_queue_discovery_system(context.bindings.music, settings_mgr)
                history = queue_sys.get_recently_played(max_items)
                return {"ok": True, "items": [item.to_dict() for item in history]}
            except Exception as exc:
                log.error("Failed to get recently played: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "recently_played_unavailable",
                        "Recently played items are temporarily unavailable.",
                        data={"items": []},
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/queue/recently-played", method="GET")

    log.info("🎛️ Queue routes registered")
