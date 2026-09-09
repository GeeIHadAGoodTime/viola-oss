"""
Local media streaming endpoint for multi-room audio.

Serves local audio files via HTTP so they can be played through a WebView
``<audio>`` element.  This allows ProcTap to capture the audio (the WebView
runs as a child process), enabling multi-room sync for local files.

Security:
    - Files are registered by UUID; the endpoint never accepts raw paths.
    - Only allowed audio extensions are accepted.
    - Registrations expire after ``_TTL_SECONDS`` (4 hours).
"""

from __future__ import annotations

import mimetypes
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.responses import Response

from core.logging_config import get_logger
from fastapi import APIRouter, Depends, Request
from ui.api.routes.auth_dependencies import require_auth

if TYPE_CHECKING:
    from ui.api.context import ApiContext

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

_TTL_SECONDS: int = 4 * 60 * 60  # 4 hours

_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".flac", ".wav", ".mp3", ".ogg", ".m4a", ".aac", ".opus"})

_CONTENT_TYPES: dict[str, str] = {
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/opus",
}

# ------------------------------------------------------------------ #
# Registry                                                             #
# ------------------------------------------------------------------ #


class LocalMediaRegistry:
    """In-memory UUID-to-path map with TTL expiry.

    Thread-safe for concurrent reads/writes from FastAPI endpoint handlers
    (GIL protects dict ops).
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[Path, float]] = {}

    def register(self, abs_path: str | Path) -> str | None:
        """Register a local file and return its UUID, or ``None`` on error.

        Validates:
        - File exists
        - Extension is in ``_ALLOWED_EXTENSIONS``
        """
        p = Path(abs_path)
        if not p.is_file():
            logger.warning("LocalMediaRegistry: file not found: %s", abs_path)
            return None
        if p.suffix.lower() not in _ALLOWED_EXTENSIONS:
            logger.warning(
                "LocalMediaRegistry: disallowed extension %s for %s",
                p.suffix,
                abs_path,
            )
            return None

        file_id = uuid.uuid4().hex
        self._entries[file_id] = (p.resolve(), time.monotonic())
        logger.debug("Registered local media: %s -> %s", file_id[:8], p.name)
        return file_id

    def resolve(self, file_id: str) -> Path | None:
        """Return the absolute path for *file_id*, or ``None`` if missing/expired."""
        entry = self._entries.get(file_id)
        if entry is None:
            return None

        path, created = entry
        if time.monotonic() - created > _TTL_SECONDS:
            del self._entries[file_id]
            return None
        return path

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.monotonic()
        expired = [k for k, (_, t) in self._entries.items() if now - t > _TTL_SECONDS]
        for k in expired:
            del self._entries[k]
        return len(expired)


# Module-level singleton
_registry = LocalMediaRegistry()


def get_media_registry() -> LocalMediaRegistry:
    """Return the module-level ``LocalMediaRegistry`` singleton."""
    return _registry


# ------------------------------------------------------------------ #
# Streaming endpoint                                                   #
# ------------------------------------------------------------------ #


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse an HTTP ``Range: bytes=start-end`` header.

    Returns ``(start, end)`` inclusive, or ``None`` if unparseable.
    """
    if not range_header.startswith("bytes="):
        return None
    range_spec = range_header[6:]
    parts = range_spec.split("-", 1)
    if len(parts) != 2:
        return None

    try:
        if parts[0] == "":
            # Suffix range: last N bytes
            suffix = int(parts[1])
            start = max(0, file_size - suffix)
            end = file_size - 1
        elif parts[1] == "":
            # Open-ended range
            start = int(parts[0])
            end = file_size - 1
        else:
            start = int(parts[0])
            end = int(parts[1])
    except ValueError:
        return None

    if start < 0 or end >= file_size or start > end:
        return None
    return (start, end)


def create_local_media_router() -> APIRouter:
    """Create the local media streaming router."""
    router = APIRouter(prefix="/api/v1/media", tags=["local_media"])

    @router.get("/stream/{file_id}", dependencies=[Depends(require_auth)])
    async def stream_local_file(file_id: str, request: Request) -> Response:
        """Stream a registered local audio file with Range request support."""
        path = _registry.resolve(file_id)
        if path is None or not path.is_file():
            return Response(status_code=404, content="Not found")

        file_size = path.stat().st_size
        content_type = _CONTENT_TYPES.get(
            path.suffix.lower(),
            mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        )

        range_header = request.headers.get("range")
        if range_header:
            parsed = _parse_range(range_header, file_size)
            if parsed is None:
                return Response(
                    status_code=416,
                    headers={"Content-Range": "bytes */%d" % file_size},
                )
            start, end = parsed
            length = end - start + 1

            data = path.read_bytes()[start : end + 1]
            return Response(
                content=data,
                status_code=206,
                media_type=content_type,
                headers={
                    "Content-Range": "bytes %d-%d/%d" % (start, end, file_size),
                    "Content-Length": str(length),
                    "Accept-Ranges": "bytes",
                },
            )

        # Full file response
        data = path.read_bytes()
        return Response(
            content=data,
            status_code=200,
            media_type=content_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    return router


# ------------------------------------------------------------------ #
# Route registration                                                   #
# ------------------------------------------------------------------ #


def register_local_media_routes(context: ApiContext) -> None:
    """Register the local media streaming routes on the FastAPI app."""
    try:
        router = create_local_media_router()
        context.app.include_router(router)
        logger.info("Local media streaming routes registered at /api/v1/media/stream/")
    except Exception:
        logger.exception("Failed to register local media routes")


__all__ = [
    "LocalMediaRegistry",
    "get_media_registry",
    "register_local_media_routes",
]
