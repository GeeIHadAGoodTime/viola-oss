from __future__ import annotations

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)


def register_ux_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    ux_manager = context.ux_manager

    @router.get("/v1/ux/status", dependencies=[Depends(require_auth)])
    async def get_ux_status():
        async def _inner():
            if not ux_manager:
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "ux_manager_unavailable",
                        "UX features are temporarily unavailable.",
                    ),
                )

            try:
                status = ux_manager.get_status()
                return {"ok": True, **status}
            except Exception as exc:
                log.debug("Operation failed: %s", exc)
                return handle_route_error(exc, "get_ux_status")

        return await toolbox.record_and_call(_inner, route="/v1/ux/status", method="GET")

    @router.get("/v1/ux/operations", dependencies=[Depends(require_auth)])
    async def get_active_operations():
        async def _inner():
            if not ux_manager:
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "ux_manager_unavailable",
                        "UX features are temporarily unavailable.",
                        data={"operations": []},
                    ),
                )

            try:
                operations = ux_manager.get_active_operations()
                return {"ok": True, "operations": operations}
            except Exception as exc:
                log.warning("Failed to get operations: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "ux_operations_unavailable",
                        "UX operations are temporarily unavailable.",
                        data={"operations": []},
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/ux/operations", method="GET")

    log.info("🎨 UX routes registered")


__all__ = ["register_ux_routes"]
