"""Backward-compatible Knowledge routes backed by the Workbench folder."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, File, Form, UploadFile, status
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error
from ui.api.routes.workbench_upload import read_workbench_upload

log = get_logger(__name__)

KNOWLEDGE_ROUTE = "/v1/knowledge"
KNOWLEDGE_TEXT_ROUTE = "/v1/knowledge/text"
KNOWLEDGE_STATS_ROUTE = "/v1/knowledge/stats"
KNOWLEDGE_CHECK_FILENAME_ROUTE = "/v1/knowledge/check_filename"
KNOWLEDGE_OPEN_FOLDER_ROUTE = "/v1/knowledge/open_folder"
KNOWLEDGE_BY_ID_ROUTE = "/v1/knowledge/{item_id}"
KNOWLEDGE_BLOB_ROUTE = "/v1/knowledge/{item_id}/blob"


class KnowledgeTextRequest(BaseModel):
    content: str
    title: str | None = None


class KnowledgeUpdateRequest(BaseModel):
    extracted_text: str | None = None


def _size_label(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    if size < 1024 * 1024:
        return "%.1f KB" % (size / 1024)
    return "%.1f MB" % (size / (1024 * 1024))


def _row(item: dict) -> dict:
    name = str(item["name"])
    return {
        "id": name,
        "item_id": name,
        "filename": name,
        "title": name,
        "byte_size": item["size"],
        "size": item["size"],
        "size_label": _size_label(int(item["size"])),
        "mime_type": item["mime"],
        "mime": item["mime"],
        "updated_at": item["modified_at"],
        "created_at": item["modified_at"],
        "metadata": {"updated_at": item["modified_at"], "has_blob": True},
    }


def _knowledge_not_found_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=failure_response("knowledge_not_found", "Workbench file not found."),
    )


def _knowledge_empty_update_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=failure_response("knowledge_update_empty", "No text supplied."),
    )


def _store(user_id: str):
    from services.workbench.dir import get_workbench_dir

    return get_workbench_dir(user_id)


def _find(user_id: str, item_id: str) -> Path:
    return _store(user_id).path_for(item_id)


def register_knowledge_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register legacy ``/v1/knowledge`` endpoints over WorkbenchDir."""
    del toolbox
    router = context.router

    @router.post(KNOWLEDGE_ROUTE, dependencies=[Depends(require_auth)])
    async def ingest_knowledge_upload(
        file: UploadFile = File(...),
        on_filename_clash: str = Form(""),
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            store = _store(user_id)
            filename = file.filename or "upload.bin"
            existing = any(item["name"].lower() == filename.lower() for item in store.list())
            if existing and on_filename_clash not in {"replace", "keep_both"}:
                row = next(item for item in store.list() if item["name"].lower() == filename.lower())
                return success_response({"action_required": "filename_clash", "existing": _row(row)})
            content = await read_workbench_upload(file)
            result = store.add(filename, content, replace=on_filename_clash != "keep_both")
            return success_response(_row(result))
        except Exception as exc:
            log.debug("Workbench upload failed through legacy route: %s", exc)
            return handle_route_error(exc, "ingest_knowledge_upload")

    @router.post(KNOWLEDGE_TEXT_ROUTE, dependencies=[Depends(require_auth)])
    async def ingest_knowledge_text(body: KnowledgeTextRequest, user_id: str = Depends(get_current_user_id)):
        try:
            filename = (body.title or "note.txt").strip()
            if not filename.lower().endswith(".txt"):
                filename += ".txt"
            result = _store(user_id).add(filename, body.content.encode("utf-8"), replace=True)
            return success_response(_row(result))
        except Exception as exc:
            log.debug("Workbench text ingest failed through legacy route: %s", exc)
            return handle_route_error(exc, "ingest_knowledge_text")

    @router.get(KNOWLEDGE_ROUTE, dependencies=[Depends(require_auth)])
    async def list_knowledge(limit: int = 100, user_id: str = Depends(get_current_user_id)):
        try:
            rows = [_row(item) for item in _store(user_id).list()[: max(1, min(int(limit), 500))]]
            return success_response({"items": rows, "count": len(rows)})
        except Exception as exc:
            log.debug("List workbench failed through legacy route: %s", exc)
            return handle_route_error(exc, "list_knowledge")

    @router.get(KNOWLEDGE_STATS_ROUTE, dependencies=[Depends(require_auth)])
    async def knowledge_stats(user_id: str = Depends(get_current_user_id)):
        try:
            rows = _store(user_id).list()
            total = sum(int(row["size"]) for row in rows)
            return success_response(
                {
                    "total_items": len(rows),
                    "total_size": total,
                    "total_size_label": _size_label(total),
                    "byte_limit_label": "",
                }
            )
        except Exception as exc:
            log.debug("Workbench stats failed through legacy route: %s", exc)
            return handle_route_error(exc, "knowledge_stats")

    @router.get(KNOWLEDGE_CHECK_FILENAME_ROUTE, dependencies=[Depends(require_auth)])
    async def check_filename(filename: str, user_id: str = Depends(get_current_user_id)):
        try:
            exists = any(item["name"].lower() == filename.lower() for item in _store(user_id).list())
            return success_response({"exists": exists, "filename": filename})
        except Exception as exc:
            log.debug("Workbench filename check failed through legacy route: %s", exc)
            return handle_route_error(exc, "check_filename")

    @router.get(KNOWLEDGE_OPEN_FOLDER_ROUTE, dependencies=[Depends(require_auth)])
    async def open_knowledge_folder(user_id: str = Depends(get_current_user_id)):
        try:
            from services.workbench.folder_open import open_user_folder

            return success_response(open_user_folder(user_id))
        except Exception as exc:
            log.debug("Workbench open folder failed through legacy route: %s", exc)
            return handle_route_error(exc, "open_knowledge_folder")

    @router.get(KNOWLEDGE_BY_ID_ROUTE, dependencies=[Depends(require_auth)])
    async def get_knowledge_item(item_id: str, user_id: str = Depends(get_current_user_id)):
        try:
            path = _find(user_id, item_id)
            stat = path.stat()
            item = _row(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": "",
                    "mime": "text/plain" if path.suffix.lower() in {".txt", ".md"} else "application/octet-stream",
                }
            )
            if path.suffix.lower() in {".txt", ".md", ".json", ".csv", ".log"}:
                item["extracted_text"] = path.read_text(encoding="utf-8", errors="replace")
            return success_response(item)
        except FileNotFoundError:
            return _knowledge_not_found_response()
        except Exception as exc:
            log.debug("Get workbench item failed through legacy route: %s", exc)
            return handle_route_error(exc, "get_knowledge_item")

    @router.get(KNOWLEDGE_BLOB_ROUTE, dependencies=[Depends(require_auth)])
    async def get_knowledge_blob(item_id: str, user_id: str = Depends(get_current_user_id)):
        try:
            path = _find(user_id, item_id)
            return FileResponse(str(path), filename=path.name)
        except FileNotFoundError:
            return _knowledge_not_found_response()
        except Exception as exc:
            log.debug("Get workbench blob failed through legacy route: %s", exc)
            return handle_route_error(exc, "get_knowledge_blob")

    @router.put(KNOWLEDGE_BY_ID_ROUTE, dependencies=[Depends(require_auth)])
    async def update_knowledge_item(
        item_id: str,
        body: KnowledgeUpdateRequest,
        user_id: str = Depends(get_current_user_id),
    ):
        try:
            if body.extracted_text is None:
                return _knowledge_empty_update_response()
            result = _store(user_id).add(item_id, body.extracted_text.encode("utf-8"), replace=True)
            return success_response(_row(result))
        except Exception as exc:
            log.debug("Update workbench item failed through legacy route: %s", exc)
            return handle_route_error(exc, "update_knowledge_item")

    @router.delete(KNOWLEDGE_BY_ID_ROUTE, dependencies=[Depends(require_auth)])
    async def forget_knowledge_item(item_id: str, user_id: str = Depends(get_current_user_id)):
        try:
            if not _store(user_id).remove(item_id):
                return _knowledge_not_found_response()
            return success_response({"deleted": True, "id": item_id})
        except Exception as exc:
            log.debug("Forget workbench item failed through legacy route: %s", exc)
            return handle_route_error(exc, "forget_knowledge_item")

    log.info("Knowledge compatibility routes registered over Workbench")


__all__ = [
    "KNOWLEDGE_BLOB_ROUTE",
    "KNOWLEDGE_BY_ID_ROUTE",
    "KNOWLEDGE_CHECK_FILENAME_ROUTE",
    "KNOWLEDGE_OPEN_FOLDER_ROUTE",
    "KNOWLEDGE_ROUTE",
    "KNOWLEDGE_STATS_ROUTE",
    "KNOWLEDGE_TEXT_ROUTE",
    "KnowledgeTextRequest",
    "register_knowledge_routes",
]
