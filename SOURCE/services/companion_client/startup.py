"""Desktop startup wiring for the companion client.

``setup_companion_client(app)`` attaches the client to a desktop FastAPI
app's lifecycle, mirroring the ``_setup_sync_engine`` pattern in
``backend/fastapi_app.py``: an ``@app.on_event("startup")`` builds and
starts the client on the app's own event loop, and an
``@app.on_event("shutdown")`` stops it cleanly.

It runs only on the desktop surface (``app_surface == "desktop"``) -- the
cloud surface IS the bridge and must not also be a companion of itself. If
the user has not enabled the companion feature or is not signed into a
cloud account, the client starts in an idle state and simply does nothing,
so wiring it unconditionally is safe.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _is_desktop_surface() -> bool:
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop")).lower() != "cloud"
    except Exception:
        return True


def setup_companion_client(app: Any) -> None:
    """Wire the companion client into a desktop FastAPI app.

    Safe to call unconditionally: it is a no-op on the cloud surface and
    starts an idle (do-nothing) client when the feature is disabled or the
    user is signed out.
    """
    if not _is_desktop_surface():
        logger.debug("Companion client not wired: not a desktop surface")
        return

    @app.on_event("startup")
    async def _start_companion_client() -> None:
        try:
            from .client import build_companion_client

            client = await build_companion_client()
            started = await client.start()
            app.state.companion_client = client
            if started:
                logger.info("Companion client wired and started")
            else:
                logger.info("Companion client wired but idle (disabled or signed out)")
        except Exception:
            logger.exception("Failed to start companion client")

    @app.on_event("shutdown")
    async def _stop_companion_client() -> None:
        client = getattr(app.state, "companion_client", None)
        if client is None:
            return
        try:
            await client.stop()
        except Exception:
            logger.exception("Failed to stop companion client cleanly")
