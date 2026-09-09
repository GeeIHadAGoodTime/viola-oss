from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.logging_config import get_logger
from fastapi import FastAPI

logger = get_logger(__name__)


def setup_static_routes(
    app: FastAPI,
    *,
    static_dir: Path | None = None,
) -> None:
    """
    Mount UI static assets and provide a safe fallback when they are unavailable.

    The behaviour mirrors the legacy inline implementation but is shared so
    alternate app factories can reuse the same logic.
    """

    target_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent.parent / "ui" / "static"
    if target_dir.exists():
        index_path = target_dir / "index.html"

        async def _serve_index() -> HTMLResponse:
            try:
                return HTMLResponse(index_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                logger.warning("index.html missing in %s; serving placeholder.", target_dir)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to read index.html from %s: %s", target_dir, exc)
            return HTMLResponse(content="<h1>Viola UI</h1><p>Static assets unavailable.</p>")

        app.add_api_route(
            "/",
            _serve_index,
            methods=["GET"],
            response_class=HTMLResponse,
        )
        app.mount("/static", StaticFiles(directory=str(target_dir)), name="static")
        return

    logger.warning("Static directory not found at %s; serving placeholder UI.", target_dir)

    async def _serve_placeholder() -> HTMLResponse:
        return HTMLResponse(content="<h1>Viola UI</h1><p>Static assets unavailable.</p>")

    app.add_api_route(
        "/",
        _serve_placeholder,
        methods=["GET"],
        response_class=HTMLResponse,
    )


__all__ = ["setup_static_routes"]
