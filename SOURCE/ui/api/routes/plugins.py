"""Plugin management API routes.

Endpoints for listing, installing, removing, reloading, and configuring plugins.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_operator_auth

from .common import RouteToolbox

log = get_logger(__name__)


def _get_plugin_manager(request: Request):
    """Get plugin manager from app state."""
    return getattr(request.app.state, "plugin_manager", None)


def register_plugin_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register plugin management endpoints."""
    router = context.router

    @router.get(
        "/v1/plugins",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def list_plugins(request: Request) -> JSONResponse:
        """List all installed plugins."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        plugins = []
        for name in pm.list_plugins():
            info = pm.get_plugin_info(name)
            if info:
                plugins.append(info)

        return JSONResponse(content=success_response({"plugins": plugins}))

    @router.get(
        "/v1/plugins/registry",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def list_registry(request: Request) -> JSONResponse:
        """List all available plugins from the registry."""
        try:
            from plugins.registry import RegistryClient

            registry = RegistryClient()
            entries = [
                {
                    "name": e.name,
                    "description": e.description,
                    "author": e.author,
                    "version": e.version,
                    "tags": e.tags,
                    "repo_url": e.repo_url,
                }
                for e in registry.list_all()
            ]
            return JSONResponse(content=success_response({"registry": entries}))
        except Exception:
            log.exception("Registry list failed")
            return JSONResponse(
                status_code=500,
                content=failure_response("REGISTRY_ERROR", "Couldn't load the plugin registry. Please try again."),
            )

    @router.get(
        "/v1/plugins/registry/search",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def search_registry(request: Request) -> JSONResponse:
        """Search the plugin registry."""
        query = request.query_params.get("q", "")
        if not query:
            return JSONResponse(
                status_code=400,
                content=failure_response("VALIDATION_ERROR", "Query parameter 'q' is required"),
            )
        try:
            from plugins.registry import RegistryClient

            registry = RegistryClient()
            results = [
                {
                    "name": e.name,
                    "description": e.description,
                    "author": e.author,
                    "version": e.version,
                    "tags": e.tags,
                }
                for e in registry.search(query)
            ]
            return JSONResponse(content=success_response({"results": results}))
        except Exception:
            log.exception("Registry search failed")
            return JSONResponse(
                status_code=500,
                content=failure_response("REGISTRY_ERROR", "Couldn't load the plugin registry. Please try again."),
            )

    @router.post(
        "/v1/plugins/install",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def install_plugin(request: Request) -> JSONResponse:
        """Install a plugin by name."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        try:
            body = await request.json()
            name = body.get("name", "")
        except Exception:
            return JSONResponse(
                status_code=400,
                content=failure_response("VALIDATION_ERROR", "Request body must contain 'name'"),
            )

        if not name:
            return JSONResponse(
                status_code=400,
                content=failure_response("VALIDATION_ERROR", "Plugin name is required"),
            )

        try:
            plugin = pm.install(name)
            return JSONResponse(
                content=success_response(
                    {
                        "installed": name,
                        "version": plugin.version,
                    }
                ),
            )
        except Exception:
            log.exception("Plugin install failed for %s", name)
            return JSONResponse(
                status_code=400,
                content=failure_response("PLUGIN_INSTALL_FAILED", "Couldn't install the plugin. Please try again."),
            )

    @router.delete(
        "/v1/plugins/{name}",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def remove_plugin(name: str, request: Request) -> JSONResponse:
        """Remove an installed plugin."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        try:
            pm.remove(name)
            return JSONResponse(content=success_response({"removed": name}))
        except Exception:
            log.exception("Plugin remove failed for %s", name)
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "PLUGIN_NOT_FOUND", "Couldn't remove the plugin. It may not exist or be removable."
                ),
            )

    @router.post(
        "/v1/plugins/{name}/reload",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def reload_plugin(name: str, request: Request) -> JSONResponse:
        """Hot-reload a plugin."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        results = pm.reload(name)
        status = results.get(name, "not_found")
        if status == "reloaded":
            return JSONResponse(content=success_response({"reloaded": name}))
        return JSONResponse(
            status_code=400,
            content=failure_response("PLUGIN_RELOAD_FAILED", status),
        )

    @router.get(
        "/v1/plugins/{name}/config",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def get_plugin_config(name: str, request: Request) -> JSONResponse:
        """Get plugin configuration."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        config = pm.config_manager.get_config(name)
        return JSONResponse(content=success_response({"config": config}))

    @router.put(
        "/v1/plugins/{name}/config",
        tags=["plugins"],
        response_model=None,
        dependencies=[Depends(require_operator_auth)],
    )
    async def update_plugin_config(name: str, request: Request) -> JSONResponse:
        """Update plugin configuration."""
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("PLUGIN_SYSTEM_UNAVAILABLE", "Plugin system not available"),
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=failure_response("VALIDATION_ERROR", "Invalid JSON body"),
            )

        # Validate against schema if plugin defines one
        plugin = pm.get_plugin(name)
        if plugin:
            schema = plugin.get_config_schema()
            if schema:
                is_valid, error = pm.config_manager.validate_config(name, schema, body)
                if not is_valid:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("PLUGIN_CONFIG_INVALID", error or "Invalid config"),
                    )

        if pm.config_manager.set_config(name, body):
            return JSONResponse(content=success_response({"updated": name}))

        return JSONResponse(
            status_code=500,
            content=failure_response("PLUGIN_CONFIG_SAVE_FAILED", "Failed to save config"),
        )
