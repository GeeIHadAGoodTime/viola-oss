"""
Voice API Routes
=================

Hub-side endpoints for remote wake-word triggers from spoke daemons.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)


class WakeTriggerIn(BaseModel):
    """Payload sent by spoke_wake_daemon when the wake word is detected."""

    room_id: str = Field(..., description="Room identifier of the triggering spoke")
    timestamp: float = Field(..., description="Unix timestamp of the detection")


class WakeTriggerOut(BaseModel):
    """Response returned to the spoke after a wake trigger."""

    ok: bool
    room_id: str
    received_at: float


from ui.api.routes._guards import require_dev_mode as _require_dev_mode


def create_voice_router() -> APIRouter:
    """Create and return the voice API router."""
    router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

    @router.post(
        "/wake-trigger",
        response_model=WakeTriggerOut,
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def wake_trigger(body: WakeTriggerIn, request: Request) -> dict[str, Any]:
        """Accept a remote wake-word trigger from a spoke daemon.

        The spoke sends this when its local ViolaWake model detects the
        wake word.  For now the hub simply logs the event; full
        integration (active-room selection, STT handoff) will follow.

        NOTE: Spoke daemons must include the hub's API key (X-API-Key header
        or Authorization: Bearer) when POSTing to this endpoint.  When
        per-spoke mTLS is implemented the spoke service certificate will
        satisfy this dependency automatically; no route-level change will
        be needed at that point.
        """
        received_at = time.time()
        log.info(
            "Remote wake trigger received: room=%s spoke_ts=%.3f latency_ms=%.0f",
            body.room_id,
            body.timestamp,
            (received_at - body.timestamp) * 1000,
        )
        return {
            "ok": True,
            "room_id": body.room_id,
            "received_at": received_at,
        }

    return router


__all__ = ["create_voice_router"]
