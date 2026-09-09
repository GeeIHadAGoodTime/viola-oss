"""Extension management routes for MCP servers and local plugins."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)

_VALID_EXTENSION_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]*$")
_DISABLED_MCP_CONFIGS: dict[str, Any] = {}
_MCP_REGISTER_FAILURE_MESSAGE = (
    "Failed to register MCP server. Check the command, args, and required environment variables."
)
_MCP_ENABLE_FAILURE_MESSAGE = (
    "Failed to enable MCP server. Check the saved command, args, and required environment variables."
)


class MCPRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=260)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    transport: str = "stdio"


def _get_mcp_hub() -> Any | None:
    try:
        from intent.tools import self_management

        return getattr(self_management, "_mcp_hub", None)
    except Exception:
        return None


def _tool_count_for_server(hub: Any, name: str) -> int:
    routes = getattr(hub, "_tool_routes", {}) or {}
    return sum(1 for server_name in routes.values() if server_name == name)


def _serialize_mcp_servers(hub: Any) -> list[dict[str, Any]]:
    names: set[str] = set()
    names.update((getattr(hub, "_configs", {}) or {}).keys())
    names.update((getattr(hub, "_sessions", {}) or {}).keys())
    names.update((getattr(hub, "_deferred_server_configs", {}) or {}).keys())
    names.update(_DISABLED_MCP_CONFIGS.keys())

    statuses = {}
    if hasattr(hub, "get_server_status"):
        statuses = hub.get_server_status()

    servers = []
    sessions = getattr(hub, "_sessions", {}) or {}
    for name in sorted(names):
        status = statuses.get(name)
        if not status:
            status = (
                "connected" if name in sessions else "disabled" if name in _DISABLED_MCP_CONFIGS else "disconnected"
            )
        servers.append(
            {
                "name": name,
                "status": status,
                "tool_count": _tool_count_for_server(hub, name),
                "enabled": name not in _DISABLED_MCP_CONFIGS,
            }
        )
    return servers


async def _connect_mcp_server(hub: Any, config: Any) -> None:
    await hub.connect_server(config.name, config)


async def _disconnect_mcp_server(hub: Any, name: str, *, keep_config: bool) -> None:
    config = (getattr(hub, "_configs", {}) or {}).get(name)
    deferred_config = (getattr(hub, "_deferred_server_configs", {}) or {}).get(name)
    saved_config = config or deferred_config
    if name in (getattr(hub, "_sessions", {}) or {}):
        await hub.disconnect_server(name)
    if keep_config and saved_config is not None:
        _DISABLED_MCP_CONFIGS[name] = saved_config
    else:
        _DISABLED_MCP_CONFIGS.pop(name, None)
        try:
            getattr(hub, "_deferred_server_configs", {}).pop(name, None)
        except Exception:
            pass


def _get_plugin_manager(request: Request) -> Any | None:
    pm = getattr(request.app.state, "plugin_manager", None)
    if pm is not None:
        return pm
    try:
        from plugins.singleton import get_plugin_manager

        return get_plugin_manager()
    except Exception:
        return None


def _plugin_kind(pm: Any, path: Path) -> str:
    try:
        if path.resolve().is_relative_to(Path(pm.user_dir).resolve()):
            return "user"
    except Exception:
        pass
    return "builtin"


def _serialize_plugins(pm: Any) -> list[dict[str, Any]]:
    plugins: dict[str, dict[str, Any]] = {}
    for plugin_path in pm.discover_plugins():
        try:
            from plugins.manifest import PluginManifest

            manifest = PluginManifest.from_file(plugin_path / "plugin.json")
            config = pm.config_manager.get_config(manifest.name)
            plugins[manifest.name] = {
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "enabled": bool(config.get("enabled", True)),
                "kind": _plugin_kind(pm, plugin_path),
                "tool_count": 0,
                "path": str(plugin_path),
            }
        except Exception as exc:
            log.debug("Skipping plugin manifest at %s: %s", plugin_path, exc)

    for name in pm.list_plugins():
        info = pm.get_plugin_info(name) or {}
        existing = plugins.get(name, {})
        sandbox = getattr(pm, "plugin_sandboxes", {}).get(name)
        config = pm.config_manager.get_config(name)
        plugins[name] = {
            **existing,
            **info,
            "name": name,
            "enabled": bool(config.get("enabled", info.get("enabled", True)))
            and not bool(getattr(sandbox, "is_disabled", False)),
            "tool_count": len(info.get("capabilities", []) or []),
        }
    return sorted(plugins.values(), key=lambda item: item.get("name", ""))


def _set_plugin_enabled(pm: Any, name: str, enabled: bool) -> bool:
    if hasattr(pm, "set_plugin_enabled"):
        try:
            return bool(pm.set_plugin_enabled(name, enabled))
        except Exception:
            log.exception("Plugin %s enable-state change failed", name)
            return False

    config = pm.config_manager.get_config(name)
    config["enabled"] = enabled
    saved = pm.config_manager.set_config(name, config)
    sandbox = getattr(pm, "plugin_sandboxes", {}).get(name)
    if sandbox is not None:
        if enabled and hasattr(sandbox, "reset"):
            sandbox.reset()
        elif not enabled:
            sandbox._disabled = True
            plugin = getattr(pm, "loaded_plugins", {}).get(name)
            if plugin is not None:
                try:
                    plugin.on_stop()
                except Exception:
                    log.debug("Plugin %s on_stop failed during disable", name, exc_info=True)
    if enabled and name not in getattr(pm, "loaded_plugins", {}):
        for plugin_path in pm.discover_plugins():
            if plugin_path.name == name:
                pm.load_plugin(plugin_path)
                break
    return saved


def _suggested_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "github",
            "name": "GitHub MCP",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env_vars_required": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
            "description": "Read GitHub PRs, issues, contributions",
            "command_install_prompt": "Install and register the GitHub MCP server using npx -y @modelcontextprotocol/server-github.",
        },
        {
            "id": "notion",
            "name": "Notion MCP",
            "command": "npx",
            "args": ["-y", "mcp-remote", "https://mcp.notion.com/mcp"],
            "env_vars_required": [],
            "description": "Connect to Notion's hosted MCP server through the official mcp-remote stdio bridge.",
            "command_install_prompt": "Install and register the Notion MCP server using npx -y mcp-remote https://mcp.notion.com/mcp.",
        },
        {
            "id": "filesystem",
            "name": "Filesystem MCP",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "<path>"],
            "env_vars_required": [],
            "description": "Expose a local folder as a controlled filesystem MCP server.",
            "command_install_prompt": "Open the MCP registration form for Filesystem MCP so I can choose a local folder path.",
        },
    ]


def register_extensions_routes(context: ApiContext) -> None:
    router = context.router

    @router.get("/v1/extensions/mcp", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None)
    async def list_mcp() -> dict[str, Any] | JSONResponse:
        hub = _get_mcp_hub()
        if hub is None:
            return success_response(
                {
                    "servers": [],
                    "available": False,
                    "message": "MCP hub has not been initialized yet.",
                }
            )
        return success_response({"servers": _serialize_mcp_servers(hub), "available": True})

    @router.post(
        "/v1/extensions/mcp/register", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None
    )
    async def register_mcp(body: MCPRegisterRequest) -> dict[str, Any] | JSONResponse:
        if not _VALID_EXTENSION_NAME.match(body.name):
            return JSONResponse(
                status_code=422,
                content=failure_response(
                    "invalid_name", "MCP server name must be alphanumeric with hyphens or underscores."
                ),
            )
        if body.transport != "stdio":
            return JSONResponse(
                status_code=422,
                content=failure_response("unsupported_transport", "Only stdio MCP transport is supported."),
            )
        hub = _get_mcp_hub()
        if hub is None:
            return JSONResponse(
                status_code=503,
                content=failure_response("mcp_unavailable", "MCP hub is not available."),
            )

        try:
            if body.env:
                from mcp_hub.types import ServerConfig

                config = ServerConfig(
                    name=body.name,
                    transport=body.transport,
                    command=body.command,
                    args=body.args,
                    env=body.env,
                    enabled=True,
                    namespace=True,
                )
                await _connect_mcp_server(hub, config)
                tool_count = _tool_count_for_server(hub, body.name)
            else:
                from intent.tools.self_management import register_mcp_server

                result = await register_mcp_server(
                    name=body.name,
                    command=body.command,
                    args=body.args,
                    transport=body.transport,
                )
                if not getattr(result, "ok", False):
                    # The downstream error string may include the user-submitted
                    # command/args (e.g. a token passed as `--token=...`) or
                    # internal subprocess details.  Surface a generic message
                    # and log only the result-error class for diagnostics.
                    raw_error = getattr(result, "error", "") or ""
                    log.warning(
                        "MCP register failed for %s (no-env path): %d-char error suppressed",
                        body.name,
                        len(raw_error),
                    )
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("mcp_register_failed", _MCP_REGISTER_FAILURE_MESSAGE),
                    )
                data = getattr(result, "data", {}) or {}
                tool_count = int(data.get("tool_count", 0)) if isinstance(data, dict) else 0
            _DISABLED_MCP_CONFIGS.pop(body.name, None)
            return success_response({"registered": body.name, "tool_count": tool_count})
        except Exception as exc:
            log.warning("MCP register failed for %s: %s", body.name, type(exc).__name__)
            return JSONResponse(
                status_code=400,
                content=failure_response("mcp_register_failed", _MCP_REGISTER_FAILURE_MESSAGE),
            )

    @router.post(
        "/v1/extensions/mcp/{name}/enable",
        tags=["extensions"],
        dependencies=[Depends(require_auth)],
        response_model=None,
    )
    async def enable_mcp(name: str) -> dict[str, Any] | JSONResponse:
        hub = _get_mcp_hub()
        if hub is None:
            return JSONResponse(
                status_code=503, content=failure_response("mcp_unavailable", "MCP hub is not available.")
            )
        config = _DISABLED_MCP_CONFIGS.pop(name, None) or (getattr(hub, "_deferred_server_configs", {}) or {}).get(name)
        if config is None and name in (getattr(hub, "_sessions", {}) or {}):
            return success_response({"enabled": name})
        if config is None:
            return JSONResponse(status_code=404, content=failure_response("mcp_not_found", "MCP server not found."))
        try:
            await _connect_mcp_server(hub, config)
            return success_response({"enabled": name, "tool_count": _tool_count_for_server(hub, name)})
        except Exception as exc:
            log.warning("MCP enable failed for %s: %s", name, type(exc).__name__)
            return JSONResponse(
                status_code=400,
                content=failure_response("mcp_enable_failed", _MCP_ENABLE_FAILURE_MESSAGE),
            )

    @router.post(
        "/v1/extensions/mcp/{name}/disable",
        tags=["extensions"],
        dependencies=[Depends(require_auth)],
        response_model=None,
    )
    async def disable_mcp(name: str) -> dict[str, Any] | JSONResponse:
        hub = _get_mcp_hub()
        if hub is None:
            return JSONResponse(
                status_code=503, content=failure_response("mcp_unavailable", "MCP hub is not available.")
            )
        await _disconnect_mcp_server(hub, name, keep_config=True)
        return success_response({"disabled": name})

    @router.delete(
        "/v1/extensions/mcp/{name}", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None
    )
    async def remove_mcp(name: str) -> dict[str, Any] | JSONResponse:
        hub = _get_mcp_hub()
        if hub is None:
            return JSONResponse(
                status_code=503, content=failure_response("mcp_unavailable", "MCP hub is not available.")
            )
        await _disconnect_mcp_server(hub, name, keep_config=False)
        return success_response({"removed": name})

    @router.get(
        "/v1/extensions/plugins", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None
    )
    async def list_plugins(request: Request) -> dict[str, Any] | JSONResponse:
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503, content=failure_response("plugins_unavailable", "Plugin system is not available.")
            )
        return success_response({"plugins": _serialize_plugins(pm)})

    @router.post(
        "/v1/extensions/plugins/{name}/enable",
        tags=["extensions"],
        dependencies=[Depends(require_auth)],
        response_model=None,
    )
    async def enable_plugin(name: str, request: Request) -> dict[str, Any] | JSONResponse:
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503, content=failure_response("plugins_unavailable", "Plugin system is not available.")
            )
        if not _set_plugin_enabled(pm, name, True):
            return JSONResponse(status_code=404, content=failure_response("plugin_not_found", "Plugin not found."))
        return success_response({"enabled": name})

    @router.post(
        "/v1/extensions/plugins/{name}/disable",
        tags=["extensions"],
        dependencies=[Depends(require_auth)],
        response_model=None,
    )
    async def disable_plugin(name: str, request: Request) -> dict[str, Any] | JSONResponse:
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503, content=failure_response("plugins_unavailable", "Plugin system is not available.")
            )
        if not _set_plugin_enabled(pm, name, False):
            return JSONResponse(status_code=404, content=failure_response("plugin_not_found", "Plugin not found."))
        return success_response({"disabled": name})

    @router.post(
        "/v1/extensions/plugins/reload", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None
    )
    async def reload_plugins(request: Request) -> dict[str, Any] | JSONResponse:
        pm = _get_plugin_manager(request)
        if pm is None:
            return JSONResponse(
                status_code=503, content=failure_response("plugins_unavailable", "Plugin system is not available.")
            )
        results = pm.reload()
        results.update(pm.discover_and_load_all())
        return success_response({"reloaded": results})

    @router.get(
        "/v1/extensions/suggested", tags=["extensions"], dependencies=[Depends(require_auth)], response_model=None
    )
    async def suggested_extensions() -> dict[str, Any]:
        return success_response({"catalog": _suggested_catalog()})
