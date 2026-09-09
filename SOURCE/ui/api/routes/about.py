"""Version / build-info endpoint."""

from __future__ import annotations

import os
import sys

from contracts.api_response import ResponseEnvelope, success_response
from core.constants import VIOLA_VERSION
from fastapi import APIRouter

router = APIRouter()


def _read_version() -> str:
    """Read installed package metadata, falling back to the app constant."""
    try:
        from importlib.metadata import version

        return version("viola")
    except Exception:
        return VIOLA_VERSION


@router.get("/api/about")
async def about() -> ResponseEnvelope:
    """Return version, build date, and Python version."""
    return success_response(
        {
            "version": _read_version(),
            "build_date": os.environ.get("BUILD_DATE", "unknown"),
            "python_version": sys.version.split()[0],
        }
    )


__all__ = ["router"]
