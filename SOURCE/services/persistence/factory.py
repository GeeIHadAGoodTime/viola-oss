"""Surface-aware factory for PersistentStateStore."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from core.db_backend import get_database_url
from core.logging_config import get_logger
from core.platform import get_data_dir
from services.persistence.backends.postgres import PostgresBackend
from services.persistence.backends.protocol import PersistenceBackend
from services.persistence.backends.sqlite import SqliteBackend

if TYPE_CHECKING:
    from services.persistence.state_store import PersistentStateStore

logger = get_logger(__name__)

_DESKTOP_DATABASE_URL_WARNING = (
    "VIOLA_DATABASE_URL is ignored on desktop surface; this is a cloud-only config. "
    "PersistentStateStore is using SQLite."
)


def _configured_surface(app_surface: str | None = None) -> str:
    if app_surface is not None:
        return app_surface.strip().lower()
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    except (ImportError, AttributeError, RuntimeError):
        return "desktop"


def _raw_database_url() -> str:
    return os.environ.get("VIOLA_DATABASE_URL", "").strip()


def create_persistence_backend(
    *,
    root: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
) -> PersistenceBackend:
    """Create the state-store backend selected by the application surface.

    Desktop always uses SQLite. Cloud always uses PostgreSQL. The env var is
    therefore treated as cloud infrastructure, never as a generic strategy
    toggle inside PersistentStateStore.
    """
    surface = _configured_surface(app_surface)
    backend_root = Path(root) if root is not None else get_data_dir()

    if surface == "desktop":
        if database_url or _raw_database_url():
            logger.warning(_DESKTOP_DATABASE_URL_WARNING)
        return SqliteBackend(backend_root)

    if surface == "cloud":
        pg_url = database_url or get_database_url()
        if not pg_url:
            raise RuntimeError(
                "PersistentStateStore cloud surface requires a valid PostgreSQL VIOLA_DATABASE_URL and asyncpg. "
                "SQLite fallback is forbidden when VIOLA_APP_SURFACE=cloud."
            )
        return PostgresBackend(pg_url)

    raise RuntimeError("Unsupported VIOLA_APP_SURFACE for PersistentStateStore: %s" % surface)


def create_state_store(
    *,
    root: Path | None = None,
    app_surface: str | None = None,
    database_url: str | None = None,
    backend: PersistenceBackend | None = None,
    run_self_check: bool = True,
) -> PersistentStateStore:
    """Create a PersistentStateStore with startup validation."""
    from services.persistence.state_store import PersistentStateStore

    selected_backend = backend or create_persistence_backend(
        root=root,
        app_surface=app_surface,
        database_url=database_url,
    )
    store = PersistentStateStore(root=root, backend=selected_backend)
    if run_self_check:
        if selected_backend.supports_synthetic_self_check:
            store.self_check()
        else:
            logger.info(
                "PersistentStateStore synthetic self_check skipped for backend=%s; "
                "startup schema validation already ran and sync writes require a real cloud user.",
                selected_backend.backend_name,
            )
    return store


__all__ = ["create_persistence_backend", "create_state_store"]
