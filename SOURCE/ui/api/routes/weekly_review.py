"""Weekly-review API routes.

Exposes the WeeklyReviewService so the Settings UI can read the most
recent LLM-generated summary and optionally trigger a manual run.
Opt-in via `weekly_review_enabled` setting (wired in the daemon).
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import (
    get_current_user_id as get_auth_user_id,
    require_auth,
)

logger = get_logger(__name__)


def register_weekly_review_routes(context: ApiContext) -> None:
    """Register weekly-review routes on the feature router."""

    router = context.router

    @router.get(
        "/v1/ai/weekly-review/latest",
        tags=["ai"],
        dependencies=[Depends(require_auth)],
    )
    async def get_latest_weekly_review(user_id: str = Depends(get_auth_user_id)) -> Any:
        """Return the most recent weekly-review analysis for the caller (or empty)."""

        try:
            from services.meta_analysis.weekly_review import (
                get_analysis_summary,
                get_latest_analysis,
            )
        except Exception as exc:
            logger.debug("weekly_review service unavailable: %s", exc)
            return success_response({"analysis": None, "summary": "Weekly review is unavailable."})

        # F-017: scope the lookup to the authenticated caller; the prior
        # call site read whichever owner's report happened to live under
        # the global cwd directory.
        analysis = get_latest_analysis(user_id=user_id)
        summary = get_analysis_summary(user_id=user_id)
        return success_response(
            {
                "analysis": analysis,
                "summary": summary,
                "has_analysis": analysis is not None,
            }
        )

    @router.post(
        "/v1/ai/weekly-review/trigger",
        tags=["ai"],
        dependencies=[Depends(require_auth)],
    )
    async def trigger_weekly_review(user_id: str = Depends(get_auth_user_id)) -> Any:
        """Run an on-demand weekly analysis.  Respects the opt-in toggle."""

        from ui.settings_manager import get_settings_manager

        if not get_settings_manager().get("weekly_review_enabled", False):
            return JSONResponse(
                status_code=403,
                content=failure_response(
                    "weekly_review_disabled",
                    "Enable 'Weekly Review' in Settings > AI & Agents before triggering.",
                ),
            )

        try:
            from services.meta_analysis.weekly_review import get_weekly_review_service
        except Exception as exc:
            logger.debug("weekly_review service unavailable: %s", exc)
            return JSONResponse(
                status_code=503,
                content=failure_response(
                    "weekly_review_unavailable",
                    "Weekly-review service could not be loaded.",
                ),
            )

        service = get_weekly_review_service()
        result = await service.trigger_manual(user_id)
        return success_response({"analysis": result, "triggered": result is not None})
