"""Session-cost API routes (F-052).

Exposes ``SessionCostTracker`` through ``/v1/ai/cost`` so the UI and any
exit-summary hook can render a Claude-Code-style ``/cost`` summary. The
formatter returns the same structure ``format_session_summary`` produces
(per-model usage, API duration, unknown-model warning) in addition to a
machine-readable snapshot of the raw state.
"""

from __future__ import annotations

from typing import Any

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import Depends, HTTPException, status
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

logger = get_logger(__name__)


def register_cost_routes(context: ApiContext) -> None:
    """Register ``/v1/ai/cost`` on the feature router."""

    router = context.router

    @router.get(
        "/v1/ai/cost",
        tags=["ai"],
        dependencies=[Depends(require_auth)],
    )
    async def get_session_cost() -> Any:
        """Return the calling user's active session cost summary and snapshot."""
        try:
            from core.user_context import get_current_user_id

            user_id = str(get_current_user_id() or "").strip()
        except LookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            ) from exc
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        try:
            from services.llm.session_cost_tracker import (
                empty_session_cost_snapshot,
                format_empty_session_cost_summary,
                get_active_session_cost_tracker,
            )
        except ImportError as exc:
            logger.debug("session_cost_tracker unavailable: %s", exc)
            return success_response(
                {
                    "summary": "Session cost is unavailable.",
                    "snapshot": None,
                    "total_cost_usd": 0.0,
                }
            )

        tracker = get_active_session_cost_tracker(user_id=user_id)
        if tracker is None:
            snapshot = empty_session_cost_snapshot()
            summary = format_empty_session_cost_summary()
        else:
            snapshot = tracker.snapshot()
            summary = tracker.format_session_summary()
        return success_response(
            {
                "summary": summary,
                "snapshot": snapshot,
                "total_cost_usd": float(snapshot.get("total_cost_usd", 0.0)),
            }
        )
