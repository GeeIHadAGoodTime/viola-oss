"""
Multi-Room Sync API Routers

Provides FastAPI routers for multi-room synchronization endpoints.

Usage:
    from services.multiroom.api import health_router

    app.include_router(health_router, prefix="/v1")
"""

from __future__ import annotations

from .health import router as health_router

__all__ = [
    "health_router",
]
