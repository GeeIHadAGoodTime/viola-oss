"""REST endpoints for the user Workbench folder."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, File, UploadFile, status
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error
from ui.api.routes.workbench_upload import read_workbench_upload

log = get_logger(__name__)

WORKBENCH_FILES_ROUTE = "/api/workbench/files"
WORKBENCH_FILE_ROUTE = "/api/workbench/files/{name:path}"
WORKBENCH_OPEN_FOLDER_ROUTE = "/api/workbench/open-folder"


def _workbench_not_found_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=failure_response("workbench_not_found", "Workbench file not found."),
    )


def register_workbench_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register Workbench file endpoints."""
    del toolbox
    router = context.router

    @router.get(WORKBENCH_FILES_ROUTE, dependencies=[Depends(require_auth)])
    async def list_workbench_files(user_id: str = Depends(get_current_user_id)):
        try:
            from services.workbench.dir import get_workbench_dir

            return success_response({"files": get_workbench_dir(user_id).list()})
        except Exception as exc:
            log.debug("List workbench files failed: %s", exc)
            return handle_route_error(exc, "list_workbench_files")

    @router.post(WORKBENCH_FILES_ROUTE, dependencies=[Depends(require_auth)])
    async def upload_workbench_file(file: UploadFile = File(...), user_id: str = Depends(get_current_user_id)):
        try:
            from services.workbench.dir import get_workbench_dir

            content = await read_workbench_upload(file)
            result = get_workbench_dir(user_id).add(file.filename or "upload.bin", content, replace=True)
            return success_response(result)
        except Exception as exc:
            log.debug("Upload workbench file failed: %s", exc)
            return handle_route_error(exc, "upload_workbench_file")

    @router.delete(WORKBENCH_FILE_ROUTE, dependencies=[Depends(require_auth)])
    async def delete_workbench_file(name: str, user_id: str = Depends(get_current_user_id)):
        try:
            from services.workbench.dir import get_workbench_dir

            removed = get_workbench_dir(user_id).remove(name)
            if not removed:
                return _workbench_not_found_response()
            return success_response({"deleted": True, "name": name})
        except Exception as exc:
            log.debug("Delete workbench file failed: %s", exc)
            return handle_route_error(exc, "delete_workbench_file")

    @router.get(WORKBENCH_OPEN_FOLDER_ROUTE, dependencies=[Depends(require_auth)])
    async def open_workbench_folder(user_id: str = Depends(get_current_user_id)):
        try:
            from services.workbench.folder_open import open_user_folder

            return success_response(open_user_folder(user_id))
        except Exception as exc:
            log.debug("Open workbench folder failed: %s", exc)
            return handle_route_error(exc, "open_workbench_folder")

    log.info("Workbench routes registered")


__all__ = [
    "WORKBENCH_FILES_ROUTE",
    "WORKBENCH_FILE_ROUTE",
    "WORKBENCH_OPEN_FOLDER_ROUTE",
    "register_workbench_routes",
]
