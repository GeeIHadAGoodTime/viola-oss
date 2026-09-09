"""
Clock HTTP Endpoint for Multi-Room Sync.

Provides Hub monotonic clock time for Spoke synchronization.
Spokes poll this endpoint to measure drift and align playback.

Usage:
    GET /api/v1/clock

Response:
    {
        "ok": true,
        "data": {
            "hub_time": 12345.6789,     # Hub monotonic clock (time.monotonic())
            "wall_time": 1706969123.456, # Wall clock (time.time())
            "server_id": "hub-abc123",   # Hub identifier
            "sync_mode": "hub"           # Always "hub" for clock endpoint
        }
    }
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import APIRouter

logger = get_logger(__name__)


# Response Models


class ClockData(BaseModel):
    """Clock data payload for multi-room sync."""

    hub_time: float = Field(..., description="Hub monotonic clock (time.monotonic())")
    wall_time: float = Field(..., description="Wall clock (time.time())")
    server_id: str = Field(..., description="Hub identifier")
    sync_mode: Literal["hub"] = Field(default="hub", description="Always 'hub' for clock endpoint")


class ClockResponse(BaseModel):
    """Response envelope for clock endpoint."""

    ok: Literal[True] = True
    error: None = None
    data: ClockData


# Cache server ID for the lifetime of the process
_SERVER_ID: str | None = None


def _get_server_id() -> str:
    """Get or generate the Hub server ID."""
    global _SERVER_ID
    if _SERVER_ID is None:
        # Generate a short, unique ID for this Hub instance
        _SERVER_ID = f"hub-{uuid.uuid4().hex[:8]}"
        logger.info("Generated Hub server ID: %s", _SERVER_ID)
    return _SERVER_ID


def create_clock_router() -> APIRouter:
    """Create the clock router with sync endpoints."""
    router = APIRouter(prefix="/api/v1", tags=["sync"])

    # Security audit 2026-03-13 (raven/S16): endpoint is unauthenticated by design.
    # Rationale: Spokes poll this from LAN without auth tokens; it returns only
    # monotonic + wall clock timestamps and a process-scoped UUID — no user data,
    # no secrets, no side-effects. Adding auth would break multiroom sync.
    # Verdict: ACCEPTABLE without auth. Re-assess if sensitive fields are added.
    @router.get("/clock", response_model=ClockResponse)
    async def get_clock() -> ClockResponse:
        """
        Get Hub clock time for multi-room synchronization.

        Returns monotonic clock time that Spokes use to measure drift
        and align playback with the Hub.
        """
        hub_time = time.monotonic()
        wall_time = time.time()
        server_id = _get_server_id()

        logger.debug(
            "Clock request: hub_time=%s, wall_time=%s",
            format(hub_time, ".6f"),
            format(wall_time, ".3f"),
        )

        return success_response(
            {
                "hub_time": hub_time,
                "wall_time": wall_time,
                "server_id": server_id,
                "sync_mode": "hub",
            }
        )

    return router


__all__ = ["ClockData", "ClockResponse", "create_clock_router"]
