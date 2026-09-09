from __future__ import annotations

from contracts.api_response import ResponseEnvelope, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)


def _route_exists(context: ApiContext, path: str, method: str) -> bool:
    method = method.upper()
    for route in (*getattr(context.app.router, "routes", ()), *getattr(context.router, "routes", ())):
        if getattr(route, "path", None) != path:
            continue
        route_methods = getattr(route, "methods", set()) or set()
        if method in route_methods:
            return True
    return False


def register_runtime_routes(context: ApiContext) -> None:
    """Register core runtime metadata routes."""
    router = context.router

    if not _route_exists(context, "/v1/system/profile", "GET"):

        @router.get("/v1/system/profile", dependencies=[Depends(require_auth)])
        async def get_system_profile() -> ResponseEnvelope:
            from core.runtime_profile import make_runtime_profile_payload

            return make_runtime_profile_payload(context.bindings.state)

    if not _route_exists(context, "/config", "GET"):

        @router.get("/config", dependencies=[Depends(require_auth)])
        async def get_public_config() -> ResponseEnvelope:
            from config import get_public_config_payload

            return success_response(get_public_config_payload())

    log.debug("Runtime metadata routes registered")


__all__ = ["register_runtime_routes"]
