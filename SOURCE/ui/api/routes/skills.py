from __future__ import annotations

from core.logging_config import get_logger
from fastapi import Depends, Path, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error
from utils.api_helpers import error_response

log = get_logger(__name__)


def register_skill_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router

    @router.get("/v1/skills", dependencies=[Depends(require_auth)])
    async def list_skills(include_disabled: bool = Query(default=True)):
        async def _inner():
            try:
                from skills.manager import get_skill_manager

                manager = get_skill_manager()
                skills = manager.get_skills(include_disabled=include_disabled)
                return {"ok": True, "skills": [skill.to_dict() for skill in skills]}
            except Exception as exc:
                log.exception("Operation failed: %s", exc)
                return handle_route_error(exc, "list_skills")

        return await toolbox.record_and_call(_inner, route="/v1/skills", method="GET")

    @router.get("/v1/skills/{skill_name}", dependencies=[Depends(require_auth)])
    async def get_skill(skill_name: str = Path(...)):
        async def _inner():
            try:
                from skills.manager import get_skill_manager

                manager = get_skill_manager()
                skill = manager.get_skill(skill_name)
                if skill:
                    return {"ok": True, "skill": skill.to_dict()}
                return error_response("skill_not_found", status_code=404)
            except Exception as exc:
                log.exception("Operation failed: %s", exc)
                return handle_route_error(exc, "get_skill")

        return await toolbox.record_and_call(_inner, route=f"/v1/skills/{skill_name}", method="GET")

    @router.post("/v1/skills/{skill_name}/toggle", dependencies=[Depends(require_auth)])
    async def toggle_skill(skill_name: str = Path(...)):
        async def _inner():
            try:
                from skills.manager import get_skill_manager

                manager = get_skill_manager()
                toggled = manager.toggle_skill(skill_name)
                return {"ok": True, "skill": toggled.to_dict()}
            except ValueError:
                return error_response("skill_not_found", status_code=404)
            except Exception as exc:
                log.exception("Operation failed: %s", exc)
                return handle_route_error(exc, "toggle_skill")

        return await toolbox.record_and_call(_inner, route=f"/v1/skills/{skill_name}/toggle", method="POST")

    log.info("🧩 Skill routes registered")
