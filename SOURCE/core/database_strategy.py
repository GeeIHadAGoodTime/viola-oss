"""Surface-based database strategy selection for dual-backend stores."""

from __future__ import annotations

import os

from core.db_backend import get_database_url
from core.logging_config import get_logger

logger = get_logger(__name__)


def configured_app_surface(app_surface: str | None = None) -> str:
    """Return the normalized application surface."""
    if app_surface is not None:
        return app_surface.strip().lower()
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    except (ImportError, AttributeError, RuntimeError):
        return "desktop"


def postgres_url_for_surface(
    store_name: str,
    *,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> str | None:
    """Return a PostgreSQL URL only when the active surface is cloud.

    Desktop always uses SQLite, even if a PostgreSQL URL is present in the
    environment. Cloud always requires PostgreSQL.
    """
    surface = configured_app_surface(app_surface)
    raw_database_url = (database_url or os.environ.get("VIOLA_DATABASE_URL", "")).strip()

    if surface == "desktop":
        if raw_database_url:
            logger.warning(
                "VIOLA_DATABASE_URL is ignored on desktop surface for %s; using SQLite.",
                store_name,
            )
        return None

    if surface == "cloud":
        resolved = database_url or get_database_url()
        if not resolved:
            raise RuntimeError(
                "%s cloud surface requires a valid PostgreSQL VIOLA_DATABASE_URL and asyncpg. "
                "SQLite fallback is forbidden when VIOLA_APP_SURFACE=cloud." % store_name
            )
        return resolved

    raise RuntimeError("Unsupported VIOLA_APP_SURFACE for %s: %s" % (store_name, surface))


__all__ = ["configured_app_surface", "postgres_url_for_surface"]
