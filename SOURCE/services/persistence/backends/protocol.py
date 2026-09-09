"""Backend contract for PersistentStateStore.

Every public data operation exposed by PersistentStateStore must be represented
here. New store methods that are not added to this ABC are caught by the parity
tests; backend subclasses that omit a method fail at construction time.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from core.json_types import to_json_value
from core.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 3
DB_FILENAME = "state.sqlite3"
# Retired compatibility id for tests and deliberate legacy-data migration.
# Production desktop paths must use the device binding helper.
LOCAL_USER_ID = "-".join(("local", "user"))
SYSTEM_USER_ID = "__system__"
SELF_CHECK_USER_ID = "__state_store_self_check__"
MUSIC_STATE_KEYS = ("music.volume", "music.now_playing", "music.is_playing")


class StateStoreSelfCheckError(RuntimeError):
    """Raised when a state-store backend fails its startup self-check."""


def serialize_value(value: object) -> str:
    return json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True)


def deserialize_value(raw: str | None) -> object:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.debug("Failed to deserialize JSON, returning raw string: %s", exc)
        return raw


def require_user_id(user_id: str) -> str:
    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    return resolved_user_id


class PersistenceBackend(ABC):
    """Synchronous contract implemented by every state-store backend."""

    backend_name: ClassVar[str]
    is_postgres: ClassVar[bool] = False
    supports_synthetic_self_check: ClassVar[bool] = True

    @property
    @abstractmethod
    def db_path(self) -> Path | None:
        """Return the SQLite DB path for local backends, otherwise None."""

    @property
    def sqlite_connection(self) -> Any | None:
        """Testing/diagnostic escape hatch for SQLite-only probes."""
        return None

    def replace_sqlite_connection_for_tests(self, conn: Any | None) -> None:
        """Testing hook implemented by SQLite backend."""
        raise RuntimeError("%s backend does not expose a SQLite connection" % self.backend_name)

    @abstractmethod
    def initialize(self) -> None:
        """Create and migrate backend schema."""

    @abstractmethod
    def close(self) -> None:
        """Release backend resources owned by this store."""

    @abstractmethod
    def get_setting(self, user_id: str, key: str, *, default: object = None) -> object:
        """Retrieve one metadata value."""

    async def get_setting_async(self, user_id: str, key: str, *, default: object = None) -> object:
        """Async-native single-metadata read for serving-loop callers.

        Default offloads the sync read to a worker thread so it never bridges on
        the caller's own loop; the Postgres backend overrides this to await its
        native async query directly (CL-20260711-afd7).
        """
        import asyncio

        return await asyncio.to_thread(self.get_setting, user_id, key, default=default)

    @abstractmethod
    def set_setting(self, user_id: str, key: str, value: object) -> None:
        """Store one metadata value."""

    @abstractmethod
    def delete_setting(self, user_id: str, key: str) -> bool:
        """Delete one metadata value."""

    @abstractmethod
    def save_music_state(
        self,
        *,
        user_id: str,
        queue: Sequence[dict[str, object]],
        now_playing: dict[str, object] | None,
        volume: int,
        is_playing: bool,
    ) -> None:
        """Persist the music queue and playback metadata atomically."""

    @abstractmethod
    def load_music_state(self, user_id: str) -> dict[str, object]:
        """Load persisted music state for one user."""

    @abstractmethod
    def clear_stale_queue(self, user_id: str) -> int:
        """Clear persisted queue and playback-active metadata for one user."""

    @abstractmethod
    def update_restart_counter(self, name: str, value: int) -> None:
        """Persist a host-level restart counter."""

    @abstractmethod
    def load_restart_counters(self) -> dict[str, int]:
        """Load host-level restart counters."""

    @abstractmethod
    def save_calibration(self, profile: str, payload: dict[str, object], *, user_id: str = SYSTEM_USER_ID) -> None:
        """Persist a wake calibration profile."""

    @abstractmethod
    def load_calibration(self, profile: str, *, user_id: str = SYSTEM_USER_ID) -> dict[str, object] | None:
        """Load a wake calibration profile."""

    @abstractmethod
    def load_all_calibrations(self, *, user_id: str = SYSTEM_USER_ID) -> dict[str, dict[str, object]]:
        """Load all wake calibration profiles for a user/system scope."""

    @abstractmethod
    def upsert_token_metadata(self, user_id: str, provider_id: str, payload: dict[str, object]) -> None:
        """Persist non-secret provider token metadata."""

    @abstractmethod
    def delete_token_metadata(self, user_id: str, provider_id: str) -> bool:
        """Delete one provider token metadata row."""

    @abstractmethod
    def delete_all_token_metadata(self, user_id: str) -> int:
        """Delete all provider token metadata rows for one user."""

    @abstractmethod
    def load_token_metadata(self, user_id: str) -> dict[str, dict[str, object]]:
        """Load all non-secret provider token metadata for one user."""

    @abstractmethod
    def record_snapshot(self, *, user_id: str, snapshot_type: str, serialized: str, ts: float) -> None:
        """Persist snapshot metadata and payload."""

    @abstractmethod
    def list_snapshots(self, limit: int = 10, *, user_id: str | None = None) -> list[dict[str, object]]:
        """List persisted snapshots."""

    @abstractmethod
    def cleanup_self_check(
        self,
        *,
        user_id: str,
        restart_counter: str,
        calibration_profile: str,
        snapshot_type: str,
    ) -> None:
        """Remove rows written by PersistentStateStore.self_check()."""
