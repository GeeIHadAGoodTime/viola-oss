"""Codex subscription authentication API routes."""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from contracts.api_response import failure_response, success_response
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth


class CodexSignOutRequest(BaseModel):
    """Explicit confirmation body for deleting the Codex CLI auth file."""

    confirm: bool = False


def register_codex_auth_routes(context: ApiContext) -> None:
    """Register Codex auth routes on the feature router."""

    router = context.router

    @router.get(
        "/v1/codex/auth/status",
        tags=["codex-auth"],
        dependencies=[Depends(require_auth)],
    )
    async def codex_auth_status() -> dict[str, Any]:
        """Return the current Codex CLI sign-in status."""

        from services.llm.codex_auth import get_auth_status

        return success_response(get_auth_status())

    @router.post(
        "/v1/codex/auth/refresh",
        tags=["codex-auth"],
        dependencies=[Depends(require_auth)],
    )
    async def codex_auth_refresh() -> dict[str, Any]:
        """Re-read the Codex CLI auth file and return status."""

        from services.llm.codex_auth import get_auth_status

        return success_response(get_auth_status())

    @router.post(
        "/v1/codex/auth/launch",
        tags=["codex-auth"],
        dependencies=[Depends(require_auth)],
    )
    async def codex_auth_launch() -> Any:
        """Launch the Codex CLI login flow if the user is not signed in."""

        from services.llm.codex_auth import launch_codex_login

        result = launch_codex_login()
        if result.get("launched") or result.get("already_signed_in"):
            return success_response(result)

        return JSONResponse(
            status_code=200,
            content=failure_response(
                "codex_login_launch_failed",
                str(result.get("error") or "Could not start Codex login."),
                data=result,
            ),
        )

    @router.post(
        "/v1/codex/auth/signout",
        tags=["codex-auth"],
        dependencies=[Depends(require_auth)],
    )
    async def codex_auth_signout(payload: CodexSignOutRequest | None = None) -> Any:
        """Delete the Codex CLI auth file after explicit confirmation."""

        if payload is None or not payload.confirm:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "confirmation_required",
                    "Set confirm=true to sign out of Codex.",
                    data={
                        "signed_out": False,
                        "deleted": False,
                    },
                ),
            )

        from services.llm.codex_auth import sign_out_codex_auth

        result = sign_out_codex_auth()
        if result.get("signed_out"):
            return success_response(result)

        return JSONResponse(
            status_code=500,
            content=failure_response(
                "codex_signout_failed",
                str(result.get("error") or "Could not sign out of Codex."),
                data=result,
            ),
        )
