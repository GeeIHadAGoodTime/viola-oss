from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from core.logging_config import get_logger
from fastapi import FastAPI

log = get_logger(__name__)


class NoCacheMiddleware:
    """Add no-cache headers for specific files (Qt WebEngine HTML hot-reload).

    Pure-ASGI (not BaseHTTPMiddleware) — see ``auth/middleware.py`` for the
    asyncpg cross-loop reasoning. Even though this middleware itself never
    touches DB, BaseHTTPMiddleware spawns inner sub-tasks via anyio, which
    breaks asyncpg cross-loop futures when ANY sibling middleware does.
    """

    NO_CACHE_PATHS = (
        "/static/webviews/youtube_iframe.html",
        "/static/webviews/youtube_iframe_v3.html",
    )

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if path not in self.NO_CACHE_PATHS:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(n, v) for n, v in headers if n.lower() not in (b"cache-control", b"pragma", b"expires")]
                headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                headers.append((b"pragma", b"no-cache"))
                headers.append((b"expires", b"0"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ImmutableAssetWrapper:
    """Wrap the ``/static/react`` StaticFiles mount with per-tier ``Cache-Control``.

    Vite content-hashes every chunk filename (``[name]-[hash].js``), so any
    ``/assets/`` URL is stable forever and is marked ``immutable`` — the Qt
    webview disk cache keeps it indefinitely.

    Everything ELSE under the mount — ``index.html`` above all, plus
    ``manifest.webmanifest``, ``sw.js``, ``favicon.ico`` and any other Vite
    build-root file — is NOT content-addressed: the URL is identical across
    every build. Starlette's ``StaticFiles``/``FileResponse`` sets only
    ``Last-Modified``/``ETag`` and no ``Cache-Control``, so per RFC 7234 §4.2.2
    QtWebEngine heuristically caches it. The Qt shell loads
    ``/static/react/index.html`` DIRECTLY (see ``_build_shell_url`` in
    ``ui/qt_native/webview_window.py``) — not the ``/`` route, which already
    sets no-cache but is never loaded — so after a desktop update the webview
    keeps serving the previous build's cached ``index.html``, pointing at the
    old content-hashed JS bundle, against the new backend until that heuristic
    freshness window expires. That is issue #994: the OLD UI silently runs
    against the NEW backend. Serving the entry files ``no-cache`` forces the
    webview to revalidate them on every load, so a fresh build always delivers
    a new chunk map (whose hashed chunks are in turn cached immutably).

    Wake-word ONNX models under ``/wake/`` are not hash-named either but are a
    deliberately-moderate-cached class, mirroring the already-fixed cloud
    sibling (``CloudStaticAssetCacheMiddleware``, #1037).

    Pure-ASGI wrapper (same rationale as NoCacheMiddleware — avoids
    BaseHTTPMiddleware's anyio sub-task issues with asyncpg cross-loop futures).

    NOTE: Starlette keeps the FULL request path in ``scope['path']`` and records
    the mount point in ``scope['root_path']``, so we strip ``root_path`` to get
    the mount-relative path before matching tiers. A request for
    ``/static/react/index.html`` arrives as ``path='/static/react/index.html'``,
    ``root_path='/static/react'`` -> relative ``'/index.html'``; the bare mount
    root arrives relative ``'/'`` (served as index.html via ``html=True``).
    """

    # Content-hashed Vite chunks — safe to cache forever.
    _IMMUTABLE_PREFIX = "/assets/"
    _IMMUTABLE_HEADER = b"public, max-age=31536000, immutable"

    # Wake-word models — not hash-named, but a bounded moderate cache is fine.
    _MODERATE_PREFIX = "/wake/"
    _MODERATE_HEADER = b"public, max-age=86400"

    # Everything else under the mount (index.html, manifest, sw.js, ...) — never
    # heuristically cacheable, so the entry document always revalidates (#994).
    _NO_CACHE_HEADER = b"no-cache, no-store, must-revalidate"

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        root_path = scope.get("root_path", "") or ""
        # Mount-relative path — Starlette leaves the mount prefix in scope['path']
        # and records it in scope['root_path'].
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"

        is_no_cache_entry = False
        if path.startswith(self._IMMUTABLE_PREFIX):
            cache_control = self._IMMUTABLE_HEADER
        elif path.startswith(self._MODERATE_PREFIX):
            cache_control = self._MODERATE_HEADER
        else:
            # index.html (both "/index.html" and the bare mount root via
            # html=True), manifest.webmanifest, sw.js, favicon.ico, ...
            cache_control = self._NO_CACHE_HEADER
            is_no_cache_entry = True

        async def send_wrapper(message):
            # Only rewrite a genuine 200 — never cache a 304/404 (a not-yet-built
            # asset's 404 must not be remembered for a year).
            if message.get("type") == "http.response.start" and message.get("status") == 200:
                headers = list(message.get("headers", []))
                headers = [(n, v) for n, v in headers if n.lower() not in (b"cache-control", b"pragma", b"expires")]
                headers.append((b"cache-control", cache_control))
                if is_no_cache_entry:
                    headers.append((b"pragma", b"no-cache"))
                    headers.append((b"expires", b"0"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _render_missing_react_ui_html() -> str:
    """Return a friendly fallback page when the React bundle is absent."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Viola</title>
    <style>
        body {
            background: #0d0d0d;
            color: white;
            font-family: 'Segoe UI', -apple-system, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 24px;
        }
        main {
            width: min(520px, 100%);
            text-align: center;
        }
        h1 {
            font-weight: 300;
            margin: 0 0 12px;
        }
        p {
            color: rgba(255,255,255,0.7);
            margin: 0 0 12px;
            line-height: 1.5;
        }
        code {
            color: #f5f5f5;
            font-family: Consolas, monospace;
        }
        .hint {
            color: rgba(255,255,255,0.5);
            font-size: 14px;
        }
    </style>
</head>
<body>
    <main>
        <h1>Viola</h1>
        <p id="status">Checking desktop UI bundle...</p>
        <p id="details" class="hint" hidden>
            The React desktop UI is not built in this checkout yet. Start Viola with
            <code>viola_control.py</code> or run <code>python scripts/build_react_ui.py</code>.
        </p>
        <noscript>
            <p class="hint">JavaScript is required to load the desktop UI.</p>
        </noscript>
    </main>
    <script>
        (async function () {
            const target = "/static/react/index.html";
            try {
                const response = await fetch(target, {
                    method: "GET",
                    cache: "no-store",
                    headers: { "Accept": "text/html" }
                });
                if (response.ok) {
                    window.location.replace(target);
                    return;
                }
            } catch (error) {
                console.warn("React bundle probe failed", error);
            }

            document.title = "Viola - UI Build Required";
            document.getElementById("status").textContent =
                "Desktop UI bundle not found in this checkout.";
            document.getElementById("details").hidden = false;
        })();
    </script>
</body>
</html>
"""


def configure_static_assets(app: FastAPI) -> None:
    """
    Register handlers for serving the SPA index and static assets.

    Serves React UI (if built) at / and /static/react/, with legacy UI
    available at /static/ as fallback.

    This preserves legacy behaviour where the UI bundle lives under
    ``ui/static`` while allowing the API layer to be imported without
    eagerly mounting files when the directory is missing (e.g., in tests).
    """
    static_dir = Path(__file__).resolve().parent.parent / "static"
    react_dir = static_dir / "react"
    react_public_dir = Path(__file__).resolve().parent.parent / "react-app" / "public"
    react_index = react_dir / "index.html"

    def _first_existing_path(*paths: Path) -> Path | None:
        for path in paths:
            if path.exists():
                return path
        return None

    if not static_dir.exists():
        log.warning("Static directory missing at %s; serving placeholder index.", static_dir)

        @app.get("/", response_class=HTMLResponse)
        async def serve_placeholder() -> HTMLResponse:
            return HTMLResponse(content="<h1>Viola API</h1><p>Static assets unavailable.</p>")

        return

    log.info("📦 Serving UI static assets from %s", static_dir)

    # Add middleware to prevent caching of youtube iframe files
    app.add_middleware(NoCacheMiddleware)
    log.info("🔒 Added no-cache middleware for youtube iframe files")

    favicon_path = _first_existing_path(react_dir / "favicon.ico", react_public_dir / "favicon.ico")
    viola_icon_path = _first_existing_path(react_dir / "viola_icon.png", react_public_dir / "viola_icon.png")
    icons_dir = _first_existing_path(react_dir / "icons", react_public_dir / "icons")

    if favicon_path is not None:

        @app.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon() -> FileResponse:
            return FileResponse(favicon_path, media_type="image/x-icon")

    if viola_icon_path is not None:

        @app.get("/viola_icon.png", include_in_schema=False)
        async def serve_viola_icon() -> FileResponse:
            return FileResponse(viola_icon_path, media_type="image/png")

    if icons_dir is not None:
        app.mount(
            "/icons",
            StaticFiles(directory=str(icons_dir)),
            name="react-icons",
        )

    # Check if React build exists
    if react_dir.exists() and react_index.exists():
        log.info("🌐 React UI found at %s - serving as default", react_dir)

        @app.get("/", response_class=HTMLResponse)
        async def serve_react_index() -> HTMLResponse:
            """Serve React UI as the default route (no-cache so phones get latest build)."""
            return HTMLResponse(
                content=react_index.read_text(encoding="utf-8"),
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        # Mount React UI static files (priority).
        # Hashed assets under /static/react/assets/ are served with
        # Cache-Control: immutable — safe because Vite content-hashes every
        # chunk filename, so a given URL never changes content.
        app.mount(
            "/static/react",
            ImmutableAssetWrapper(StaticFiles(directory=str(react_dir), html=True)),
            name="react",
        )
        log.info("✅ React UI mounted at /static/react/")
    else:
        log.warning(
            "React UI build missing at %s. Serving bootstrap fallback and keeping /static/ available.",
            react_index,
        )

        @app.get("/", response_class=HTMLResponse)
        async def serve_legacy_index() -> HTMLResponse:
            return HTMLResponse(
                content=_render_missing_react_ui_html(),
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

    # Serve youtube_iframe HTML files with no-cache headers to ensure latest version is always used
    from starlette.responses import Response

    for iframe_name in ["youtube_iframe.html", "youtube_iframe_v3.html"]:
        iframe_path = static_dir / "webviews" / iframe_name

        if iframe_path.exists():
            # Create closure to capture the correct path for each route
            def create_iframe_handler(path):
                async def handler() -> Response:
                    """Serve YouTube iframe HTML with no-cache headers."""
                    content = path.read_text(encoding="utf-8")
                    return Response(
                        content=content,
                        media_type="text/html",
                        headers={
                            "Cache-Control": "no-cache, no-store, must-revalidate",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        },
                    )

                return handler

            app.add_api_route(
                f"/static/webviews/{iframe_name}",
                create_iframe_handler(iframe_path),
                methods=["GET"],
            )

    # Mount legal documents (privacy policy, terms of service)
    legal_dir = Path(__file__).resolve().parent.parent.parent / "docs" / "legal"
    if legal_dir.exists():
        from diagnostics.startup_telemetry import register_post_bind_initializer

        def _mount_legal_docs() -> None:
            app.mount(
                "/docs/legal",
                StaticFiles(directory=str(legal_dir)),
                name="legal_docs",
            )
            log.info("Legal documents mounted at /docs/legal/")

        register_post_bind_initializer(app, "legal_static_assets", _mount_legal_docs, registers_routes=True)

    # Mount legacy UI (always available at /static/)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
