"""
Player state contract utilities for FastAPI app.
"""

from __future__ import annotations

from contracts.player_state import ensure_player_state_schema
from core.logging_config import get_logger
from fastapi import FastAPI

logger = get_logger(__name__)


def has_route(app: FastAPI, path: str, method: str = "GET") -> bool:
    """Check if a route exists in the FastAPI app."""
    for route in getattr(app.router, "routes", []):
        route_path = getattr(route, "path", None)
        if route_path != path:
            continue
        methods = getattr(route, "methods", set()) or set()
        if method in methods:
            return True
    return False


def ensure_player_state_contract(app: FastAPI) -> None:
    """
    Ensure player state contract is installed on the FastAPI app.

    This validates that the player state schema is properly configured
    and that the required /v1/state route exists.
    """
    if getattr(app.state, "player_state_contract_installed", False):
        return

    if not has_route(app, "/v1/state"):
        logger.warning("Player state contract skipped: /v1/state route missing.")
        return

    ensure_player_state_schema()
    app.state.player_state_contract_installed = True
