"""REST endpoints for Claude-style Viola memory files."""

from __future__ import annotations

from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)

MEMORY_VIOLA_ROUTE = "/api/memory/viola"
MEMORY_ENTRIES_ROUTE = "/api/memory/entries"
MEMORY_ENTRY_ROUTE = "/api/memory/entries/{line_or_section:path}"
MEMORY_TOPIC_ROUTE = "/api/memory/topics/{name}"
MEMORY_TOPIC_ENTRY_ROUTE = "/api/memory/topics/{name}/entries/{line_or_section:path}"
MEMORY_AUDIT_ROUTE = "/api/memory/audit"

# Backward-compatible D-layout routes. New UI uses the routes above.
MEMORY_FILES_ROUTE = "/api/memory/files"
MEMORY_FILE_ROUTE = "/api/memory/files/{file_path:path}"
MEMORY_FILE_LINE_ROUTE = "/api/memory/files/{file_path:path}/lines/{line_number}"
MEMORY_FILE_SECTION_ROUTE = "/api/memory/files/{file_path:path}/sections/{section_title}"


class MemoryFileWriteRequest(BaseModel):
    content: str = Field("", max_length=200000)


def register_memory_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register memory endpoints."""
    del toolbox
    router = context.router

    @router.get(MEMORY_VIOLA_ROUTE, dependencies=[Depends(require_auth)])
    async def read_viola_file(user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response({"path": "VIOLA.md", "content": get_memory_dir(user_id).read_viola_file()})
        except Exception as exc:
            log.debug("Read VIOLA.md failed: %s", exc)
            return handle_route_error(exc, "read_viola_file")

    @router.put(MEMORY_VIOLA_ROUTE, dependencies=[Depends(require_auth)])
    async def write_viola_file(body: MemoryFileWriteRequest, user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response(get_memory_dir(user_id).write_viola_file(body.content, agent_initiated=False))
        except Exception as exc:
            log.debug("Write VIOLA.md failed: %s", exc)
            return handle_route_error(exc, "write_viola_file")

    @router.get(MEMORY_ENTRIES_ROUTE, dependencies=[Depends(require_auth)])
    async def list_memory_entries(user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response(get_memory_dir(user_id).list_entries())
        except Exception as exc:
            log.debug("List memory entries failed: %s", exc)
            return handle_route_error(exc, "list_memory_entries")

    @router.delete(MEMORY_ENTRY_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_memory_entry(line_or_section: str, user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            result = get_memory_dir(user_id).delete_entry(line_or_section, agent_initiated=False)
            if not result.get("ok"):
                return failure_response("memory_delete_failed", str(result.get("error") or "Delete failed."))
            return success_response(result)
        except Exception as exc:
            log.debug("Delete memory entry failed: %s", exc)
            return handle_route_error(exc, "delete_memory_entry")

    @router.delete(MEMORY_TOPIC_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_memory_topic(name: str, user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            result = get_memory_dir(user_id).delete_topic_file(name, agent_initiated=False)
            if not result.get("ok"):
                return failure_response("memory_topic_delete_failed", str(result.get("error") or "Delete failed."))
            return success_response(result)
        except Exception as exc:
            log.debug("Delete memory topic failed: %s", exc)
            return handle_route_error(exc, "delete_memory_topic")

    @router.delete(MEMORY_TOPIC_ENTRY_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_memory_topic_entry(
        name: str,
        line_or_section: str,
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            from services.memory.dir import get_memory_dir

            result = get_memory_dir(user_id).delete_topic_entry(name, line_or_section, agent_initiated=False)
            if not result.get("ok"):
                return failure_response("memory_delete_failed", str(result.get("error") or "Delete failed."))
            return success_response(result)
        except Exception as exc:
            log.debug("Delete memory topic entry failed: %s", exc)
            return handle_route_error(exc, "delete_memory_topic_entry")

    @router.get(MEMORY_AUDIT_ROUTE, dependencies=[Depends(require_auth)])
    async def memory_audit(limit: int = 20, user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response({"entries": get_memory_dir(user_id).audit(limit=limit)})
        except Exception as exc:
            log.debug("Memory audit failed: %s", exc)
            return handle_route_error(exc, "memory_audit")

    @router.get(MEMORY_FILES_ROUTE, dependencies=[Depends(require_auth)])
    async def list_memory_files(user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response({"files": get_memory_dir(user_id).list()})
        except Exception as exc:
            log.debug("List memory files failed: %s", exc)
            return handle_route_error(exc, "list_memory_files")

    @router.get(MEMORY_FILE_ROUTE, dependencies=[Depends(require_auth)])
    async def read_memory_file(file_path: str, user_id: str = Depends(get_current_user_id)):
        try:
            from services.memory.dir import get_memory_dir

            return success_response({"path": file_path, "content": get_memory_dir(user_id).read(file_path)})
        except Exception as exc:
            log.debug("Read memory file failed: %s", exc)
            return handle_route_error(exc, "read_memory_file")

    @router.put(MEMORY_FILE_ROUTE, dependencies=[Depends(require_auth)])
    async def write_memory_file(
        file_path: str,
        body: MemoryFileWriteRequest,
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            from services.memory.dir import get_memory_dir

            if file_path in {"VIOLA.md", "USER_MEMORY.md"}:
                result = get_memory_dir(user_id).write_viola_file(body.content, agent_initiated=False)
            else:
                result = get_memory_dir(user_id).write(
                    body.content,
                    where=file_path,
                    position="replace",
                    agent_initiated=False,
                )
            return success_response(result)
        except Exception as exc:
            log.debug("Write memory file failed: %s", exc)
            return handle_route_error(exc, "write_memory_file")

    @router.delete(MEMORY_FILE_LINE_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_memory_line(
        file_path: str,
        line_number: int,
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            from services.memory.dir import get_memory_dir

            result = get_memory_dir(user_id).delete_line(file_path, line_number, agent_initiated=False)
            if not result.get("ok"):
                return failure_response("memory_delete_failed", str(result.get("error") or "Delete failed."))
            return success_response(result)
        except Exception as exc:
            log.debug("Delete memory line failed: %s", exc)
            return handle_route_error(exc, "delete_memory_line")

    @router.delete(MEMORY_FILE_SECTION_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_memory_section(
        file_path: str,
        section_title: str,
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            from services.memory.dir import get_memory_dir

            result = get_memory_dir(user_id).delete_section(file_path, section_title, agent_initiated=False)
            if not result.get("ok"):
                return failure_response("memory_delete_failed", str(result.get("error") or "Delete failed."))
            return success_response(result)
        except Exception as exc:
            log.debug("Delete memory section failed: %s", exc)
            return handle_route_error(exc, "delete_memory_section")

    log.info("Memory routes registered")


__all__ = [
    "MEMORY_AUDIT_ROUTE",
    "MEMORY_ENTRIES_ROUTE",
    "MEMORY_ENTRY_ROUTE",
    "MEMORY_FILES_ROUTE",
    "MEMORY_FILE_ROUTE",
    "MEMORY_TOPIC_ROUTE",
    "MEMORY_VIOLA_ROUTE",
    "MemoryFileWriteRequest",
    "register_memory_routes",
]
