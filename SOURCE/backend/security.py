"""
Backend Security Configuration

Thin wrapper around UI security configuration to avoid layering violations.
The backend needs security configuration for the FastAPI application.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from ui.core.security import configure_security as _configure_security


def configure_backend_security(app: FastAPI) -> Any:
    """
    Configure security for the backend FastAPI application.

    This is a thin wrapper around the UI security configuration
    to maintain proper layering (backend shouldn't import UI modules directly).
    """
    return _configure_security(app)
