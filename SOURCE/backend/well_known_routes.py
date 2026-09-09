"""Static .well-known/* routes for RFC compliance.

Currently serves:
  * /.well-known/security.txt — RFC 9116 security contact + safe harbor.

Mounted at the cloud surface only; desktop builds do not register this.
If the source file is missing, the route fails closed with 503 instead
of serving a weaker fallback that can be mistaken for launch compliance.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import PlainTextResponse

from core.logging_config import get_logger
from fastapi import APIRouter, status

_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_TXT_PATH = _ROOT / "public" / "well-known" / "security.txt"
_SECURITY_TXT_MISSING_BODY = "security.txt source file is not available\n"
_SECURITY_TXT_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "CDN-Cache-Control": "public, max-age=86400",
    "Cloudflare-CDN-Cache-Control": "public, max-age=86400",
}

logger = get_logger(__name__)


def create_well_known_router() -> APIRouter:
    router = APIRouter(prefix="/.well-known", include_in_schema=False)

    @router.get("/security.txt", response_class=PlainTextResponse)
    async def _security_txt() -> PlainTextResponse:
        try:
            body = _SECURITY_TXT_PATH.read_text(encoding="utf-8")
        except OSError:
            logger.error("security.txt source file is unavailable: %s", _SECURITY_TXT_PATH)
            return PlainTextResponse(
                content=_SECURITY_TXT_MISSING_BODY,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                media_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        return PlainTextResponse(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers=_SECURITY_TXT_CACHE_HEADERS,
        )

    return router
