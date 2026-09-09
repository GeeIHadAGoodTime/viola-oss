"""System information endpoints — MCP servers, memory stats, user model, credentials.

These endpoints expose read-only status from internal subsystems via the
REST API so the UI and debugging tools can inspect live state without
going through the voice/command pipeline.
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth, require_operator_auth
from ui.api.routes.common import RouteToolbox
from ui.core.security import get_security_posture_snapshot

log = get_logger(__name__)


def register_system_info_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register system information endpoints on the feature router."""
    router = context.router

    # ------------------------------------------------------------------
    # GET /v1/mcp/servers — list connected MCP servers and their tools
    # ------------------------------------------------------------------

    @router.get("/v1/mcp/servers", dependencies=[Depends(require_operator_auth)])
    async def mcp_servers():
        """List all MCP servers registered with the hub and their status."""

        async def _inner() -> Any:
            try:
                from intent.tools.self_management import _mcp_hub

                if _mcp_hub is None:
                    # Hub is lazy-initialized (first agent command triggers it).
                    # Until then, introspect the in-process core_tools FastMCP
                    # server so callers still see the registered tool surface
                    # (Gmail, Telegram, etc.) instead of an empty list.
                    try:
                        from mcp_servers.core_tools.server import server as _core_server

                        _core_tools = await _core_server.list_tools()
                        _tool_names = sorted(t.name for t in _core_tools)
                    except Exception:
                        _tool_names = []
                    return success_response(
                        {
                            "servers": {
                                "core-tools": {
                                    "status": "available (hub not yet initialized)",
                                    "tools": _tool_names,
                                },
                            },
                            "count": 1,
                            "total_tools": len(_tool_names),
                            "message": "MCP hub is not initialized yet.",
                        }
                    )

                status = _mcp_hub.get_server_status()
                # Also gather tool count per server for richer info
                tool_details: dict[str, list[str]] = {}
                for tool_name, server_name in _mcp_hub._tool_routes.items():
                    tool_details.setdefault(server_name, []).append(tool_name)

                servers_info: dict[str, Any] = {}
                for name, status_str in status.items():
                    servers_info[name] = {
                        "status": status_str,
                        "tools": sorted(tool_details.get(name, [])),
                    }

                return success_response(
                    {
                        "servers": servers_info,
                        "count": len(servers_info),
                        "total_tools": len(_mcp_hub._tool_routes),
                    }
                )
            except Exception:
                log.exception("MCP servers listing failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "mcp_servers_failed",
                        "Couldn't list MCP servers. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/mcp/servers", method="GET")

    # ------------------------------------------------------------------
    # GET /v1/memory/stats — memory store statistics
    # ------------------------------------------------------------------

    @router.get("/v1/memory/stats", dependencies=[Depends(require_auth)])
    async def memory_stats(user_id: str = Depends(get_current_user_id)):
        """Return statistics about the persistent memory store."""

        async def _inner() -> Any:
            try:
                from services.memory.store import get_memory_store

                store = get_memory_store()
                total_active = store.count_active(user_id)

                # Count by category
                category_counts: dict[str, int] = {}
                for category in ("preference", "fact", "correction", "routine", "note"):
                    mems = store.get_by_category(user_id, category, limit=9999)
                    category_counts[category] = len(mems)

                # Recent memories (last 5)
                recent = store.get_recent(user_id, limit=5)
                recent_summaries = [
                    {
                        "id": m.id,
                        "category": m.category,
                        "content_preview": m.content[:80],
                        "access_count": m.access_count,
                        "created_at": m.created_at,
                    }
                    for m in recent
                ]

                # The markdown-backed MemoryStore exposes a storage root (_root),
                # not a single SQLite file — older code assumed a `_db_path`
                # attribute that no longer exists and raised AttributeError here.
                # Report whatever storage location the store actually exposes,
                # without assuming a specific private attribute.
                storage_path = (
                    getattr(store, "_db_path", None) or getattr(store, "_root", None) or getattr(store, "root", None)
                )
                return success_response(
                    {
                        "total_active": total_active,
                        "by_category": category_counts,
                        "recent": recent_summaries,
                        "storage_path": str(storage_path) if storage_path is not None else None,
                    }
                )
            except Exception:
                log.exception("Memory stats failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "memory_stats_failed",
                        "Couldn't load memory statistics. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/memory/stats", method="GET")

    # ------------------------------------------------------------------
    # GET /v1/user-model — user profile/preferences model
    # ------------------------------------------------------------------

    @router.get("/v1/user-model", dependencies=[Depends(require_auth)])
    async def user_model(user_id: str = Depends(get_current_user_id)):
        """Return the learned user model (preferences, facts, patterns)."""

        async def _inner() -> Any:
            try:
                from services.user_model import get_user_model

                model = get_user_model(user_id=user_id)
                data = model.to_dict()

                # Add summary for quick overview
                data["profile_summary"] = model.get_profile_summary()
                data["preference_count"] = sum(
                    len(v) if isinstance(v, dict) else 0 for v in data.get("preferences", {}).values()
                )
                data["fact_count"] = len(data.get("facts", {}))
                data["pattern_count"] = len(data.get("interaction_patterns", {}))

                return success_response(data)
            except Exception:
                log.exception("User model retrieval failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "user_model_failed",
                        "Couldn't load user model. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/user-model", method="GET")

    # ------------------------------------------------------------------
    # GET /v1/credentials — list stored API credentials (no keys)
    # ------------------------------------------------------------------

    @router.get("/v1/credentials", dependencies=[Depends(require_operator_auth)])
    async def credentials():
        """List stored API credentials (service names and metadata only, no keys)."""

        async def _inner() -> Any:
            try:
                from core.user_context import get_current_user_id
                from services.api_vault.vault import get_credential_vault

                vault = get_credential_vault()
                services = vault.list_services(user_id=get_current_user_id())

                return success_response(
                    {
                        "credentials": services,
                        "count": len(services),
                    }
                )
            except Exception:
                log.exception("Credentials listing failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "credentials_failed",
                        "Couldn't list credentials. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/credentials", method="GET")

    @router.get("/v1/security/posture", dependencies=[Depends(require_operator_auth)])
    async def security_posture():
        """Return the current desktop/network security posture."""

        async def _inner() -> Any:
            posture = get_security_posture_snapshot()
            return success_response({"posture": posture.to_dict()})

        return await toolbox.record_and_call(_inner, route="/v1/security/posture", method="GET")

    log.info("System info routes registered (mcp/servers, memory/stats, user-model, credentials, security/posture)")
