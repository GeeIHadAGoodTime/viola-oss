"""State-store backend implementations."""

from __future__ import annotations

from services.persistence.backends.postgres import PostgresBackend
from services.persistence.backends.protocol import (
    DB_FILENAME,
    LOCAL_USER_ID,
    MUSIC_STATE_KEYS,
    SCHEMA_VERSION,
    SYSTEM_USER_ID,
    PersistenceBackend,
    StateStoreSelfCheckError,
)
from services.persistence.backends.sqlite import SqliteBackend

__all__ = [
    "DB_FILENAME",
    "LOCAL_USER_ID",
    "MUSIC_STATE_KEYS",
    "SCHEMA_VERSION",
    "SYSTEM_USER_ID",
    "PersistenceBackend",
    "PostgresBackend",
    "SqliteBackend",
    "StateStoreSelfCheckError",
]
