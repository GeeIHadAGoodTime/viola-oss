from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.json_types import JsonDict
from core.logging_config import get_logger
from core.platform import get_data_dir
from services.persistence.backends.protocol import (
    DB_FILENAME,
    LOCAL_USER_ID,
    SCHEMA_VERSION,
    SELF_CHECK_USER_ID,
    SYSTEM_USER_ID,
    PersistenceBackend,
    StateStoreSelfCheckError,
    deserialize_value,
    require_user_id,
    serialize_value,
)

logger = get_logger(__name__)

_SCHEMA_LOCK = threading.Lock()
STATE_DB_SCHEMA_LOCK = _SCHEMA_LOCK
_STORE_SINGLETON: PersistentStateStore | None = None

_BACKEND_BACKED_PUBLIC_METHODS = frozenset(
    {
        "get_setting",
        "set_setting",
        "delete_setting",
        "save_music_state",
        "load_music_state",
        "clear_stale_queue",
        "update_restart_counter",
        "load_restart_counters",
        "save_calibration",
        "load_calibration",
        "load_all_calibrations",
        "upsert_token_metadata",
        "delete_token_metadata",
        "delete_all_token_metadata",
        "load_token_metadata",
        "record_snapshot",
        "list_snapshots",
    }
)


class PersistentStateStore:
    """
    Durable runtime persistence facade.

    SQL and dialect behavior live in PersistenceBackend implementations. This
    class owns the public API, snapshot sidecar files, singleton lifecycle, and
    startup self-check.
    """

    def __init__(self, root: Path | None = None, *, backend: PersistenceBackend | None = None) -> None:
        if root is not None:
            base_path = Path(root)
        else:
            try:
                from config.settings import settings as _cfg

                base_path = Path(_cfg.data_dir)
            except (ImportError, AttributeError, RuntimeError):
                base_path = get_data_dir()

        if backend is None:
            from services.persistence.factory import create_persistence_backend

            backend = create_persistence_backend(root=base_path)

        self._root = base_path
        self._backend: PersistenceBackend = backend
        self._lock = threading.RLock()
        self._snapshot_dir = base_path / "logs" / "state_snapshots"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backend_name(self) -> str:
        return self._backend.backend_name

    @property
    def _db_path(self) -> Path | None:
        return self._backend.db_path

    @property
    def _conn(self) -> Any | None:
        return self._backend.sqlite_connection

    @_conn.setter
    def _conn(self, conn: Any | None) -> None:
        self._backend.replace_sqlite_connection_for_tests(conn)

    @property
    def _use_pg(self) -> bool:
        return self._backend.is_postgres

    @property
    def _pg_initialized(self) -> bool:
        return bool(getattr(self._backend, "pg_initialized", False))

    @staticmethod
    def backend_backed_public_methods() -> frozenset[str]:
        return _BACKEND_BACKED_PUBLIC_METHODS

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        return require_user_id(user_id)

    @staticmethod
    def _serialize(value: object) -> str:
        return serialize_value(value)

    @staticmethod
    def _deserialize(raw: str | None) -> object:
        return deserialize_value(raw)

    async def pg_initialize(self) -> None:
        """Compatibility hook for callers that explicitly pre-initialize PG."""
        initialize_async = getattr(self._backend, "initialize_async", None)
        if self._backend.is_postgres and callable(initialize_async):
            await initialize_async()
            return
        self._backend.initialize()

    def close(self) -> None:
        self._backend.close()

    def self_check(self) -> None:
        """Exercise every public persistence operation against a throwaway user."""
        user_id = SELF_CHECK_USER_ID
        restart_counter = SELF_CHECK_USER_ID
        calibration_profile = "self_check"
        snapshot_type = "state_store_self_check"
        snapshot_path: Path | None = None

        def run(method_name: str, callback: Any) -> Any:
            try:
                return callback()
            except Exception as exc:
                raise StateStoreSelfCheckError(
                    "PersistentStateStore self_check failed for backend=%s method=%s: %s"
                    % (self.backend_name, method_name, exc)
                ) from exc

        try:
            run("set_setting", lambda: self.set_setting(user_id, "self_check", {"ok": True}))
            value = run("get_setting", lambda: self.get_setting(user_id, "self_check"))
            if value != {"ok": True}:
                raise StateStoreSelfCheckError(
                    "PersistentStateStore self_check failed for backend=%s method=get_setting: wrong value"
                    % self.backend_name
                )
            run("delete_setting", lambda: self.delete_setting(user_id, "self_check"))
            run(
                "save_music_state",
                lambda: self.save_music_state(
                    user_id=user_id,
                    queue=[],
                    now_playing=None,
                    volume=0,
                    is_playing=False,
                ),
            )
            run("load_music_state", lambda: self.load_music_state(user_id))
            run("clear_stale_queue", lambda: self.clear_stale_queue(user_id))
            run("update_restart_counter", lambda: self.update_restart_counter(restart_counter, 0))
            run("load_restart_counters", self.load_restart_counters)
            run(
                "save_calibration",
                lambda: self.save_calibration(calibration_profile, {"ok": True}, user_id=user_id),
            )
            run("load_calibration", lambda: self.load_calibration(calibration_profile, user_id=user_id))
            run("load_all_calibrations", lambda: self.load_all_calibrations(user_id=user_id))
            run("upsert_token_metadata", lambda: self.upsert_token_metadata(user_id, "self_check", {"ok": True}))
            run("load_token_metadata", lambda: self.load_token_metadata(user_id))
            run("delete_token_metadata", lambda: self.delete_token_metadata(user_id, "self_check"))
            run("delete_all_token_metadata", lambda: self.delete_all_token_metadata(user_id))
            snapshot_path = run(
                "record_snapshot",
                lambda: self.record_snapshot(snapshot_type, {"ok": True}, user_id=user_id, max_files=1),
            )
            run("list_snapshots", lambda: self.list_snapshots(limit=1, user_id=user_id))
        finally:
            try:
                self._backend.cleanup_self_check(
                    user_id=user_id,
                    restart_counter=restart_counter,
                    calibration_profile=calibration_profile,
                    snapshot_type=snapshot_type,
                )
            except Exception as exc:
                logger.warning("PersistentStateStore self_check cleanup failed: %s", exc)
            if snapshot_path is not None:
                try:
                    snapshot_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("PersistentStateStore self_check snapshot cleanup failed: %s", exc)

    def get_setting(self, user_id: str, key: str, *, default: object = None) -> object:
        return self._backend.get_setting(user_id, key, default=default)

    async def get_setting_async(self, user_id: str, key: str, *, default: object = None) -> object:
        """Async-native single-metadata read for serving-loop callers (CL-20260711-afd7)."""
        return await self._backend.get_setting_async(user_id, key, default=default)

    def set_setting(self, user_id: str, key: str, value: object) -> None:
        self._backend.set_setting(user_id, key, value)

    def delete_setting(self, user_id: str, key: str) -> bool:
        return self._backend.delete_setting(user_id, key)

    def save_music_state(
        self,
        *,
        user_id: str,
        queue: Sequence[dict[str, object]],
        now_playing: dict[str, object] | None,
        volume: int,
        is_playing: bool,
    ) -> None:
        self._backend.save_music_state(
            user_id=user_id,
            queue=queue,
            now_playing=now_playing,
            volume=volume,
            is_playing=is_playing,
        )

    def load_music_state(self, user_id: str) -> dict[str, object]:
        return self._backend.load_music_state(user_id)

    def clear_stale_queue(self, user_id: str) -> int:
        return self._backend.clear_stale_queue(user_id)

    def update_restart_counter(self, name: str, value: int) -> None:
        self._backend.update_restart_counter(name, value)

    def load_restart_counters(self) -> dict[str, int]:
        return self._backend.load_restart_counters()

    def save_calibration(
        self,
        profile: str,
        payload: dict[str, object],
        *,
        user_id: str = SYSTEM_USER_ID,
    ) -> None:
        self._backend.save_calibration(profile, payload, user_id=user_id)

    def load_calibration(
        self,
        profile: str,
        *,
        user_id: str = SYSTEM_USER_ID,
    ) -> dict[str, object] | None:
        return self._backend.load_calibration(profile, user_id=user_id)

    def load_all_calibrations(
        self,
        *,
        user_id: str = SYSTEM_USER_ID,
    ) -> dict[str, dict[str, object]]:
        return self._backend.load_all_calibrations(user_id=user_id)

    def upsert_token_metadata(self, user_id: str, provider_id: str, payload: dict[str, object]) -> None:
        self._backend.upsert_token_metadata(user_id, provider_id, payload)

    def delete_token_metadata(self, user_id: str, provider_id: str) -> bool:
        return self._backend.delete_token_metadata(user_id, provider_id)

    def delete_all_token_metadata(self, user_id: str) -> int:
        return self._backend.delete_all_token_metadata(user_id)

    def load_token_metadata(self, user_id: str) -> dict[str, dict[str, object]]:
        return self._backend.load_token_metadata(user_id)

    @staticmethod
    def _user_snapshot_partition(user_id: str | None) -> str:
        """Return the on-disk partition name for ``user_id``.

        Multi-tenant: snapshots live under
        ``logs/state_snapshots/by_user/<sha256(user_id)>`` so two
        tenants never share a directory.  The system bucket is kept
        addressable as a stable name so admin-tool readers can locate
        it without hashing the sentinel.
        """
        import hashlib

        if user_id is None or user_id == SYSTEM_USER_ID:
            return "system"
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]

    def _user_snapshot_dir(self, user_id: str | None) -> Path:
        return self._snapshot_dir / "by_user" / self._user_snapshot_partition(user_id)

    def record_snapshot(
        self,
        snapshot_type: str,
        payload: JsonDict,
        *,
        user_id: str = SYSTEM_USER_ID,
        max_files: int = 96,
    ) -> Path:
        ts = time.time()
        document = {
            "version": SCHEMA_VERSION,
            "generated_at": ts,
            "snapshot_type": snapshot_type,
            "user_id": user_id,
            "payload": payload,
        }
        serialized = serialize_value(document)
        self._backend.record_snapshot(user_id=user_id, snapshot_type=snapshot_type, serialized=serialized, ts=ts)

        # Multi-tenant: partition on-disk snapshots by user_id so a
        # diagnostic that lists the directory cannot read across
        # tenants.  Each tenant has its own FIFO ring.
        user_dir = self._user_snapshot_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        filename = "%d_%s.json" % (int(ts * 1000), snapshot_type)
        target_path = user_dir / filename
        target_path.write_text(serialized, encoding="utf-8")
        self._prune_snapshot_files(max_files=max_files, user_dir=user_dir)
        return target_path

    def list_snapshots(
        self,
        limit: int = 10,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        """List snapshots.

        Multi-tenant: ``user_id`` is REQUIRED.  Without it we refuse to
        list (rather than silently fan out across every tenant) — call
        the backend's admin-only list with the system user when you
        genuinely need a global view.
        """
        if user_id is None:
            logger.warning("list_snapshots refused: user_id is required")
            return []
        return self._backend.list_snapshots(limit=limit, user_id=user_id)

    def _prune_snapshot_files(self, *, max_files: int, user_dir: Path | None = None) -> None:
        target_dir = user_dir or self._snapshot_dir
        files = sorted(target_dir.glob("*.json"), key=lambda path: path.stat().st_mtime)
        if len(files) <= max_files:
            return
        for path in files[: len(files) - max_files]:
            try:
                path.unlink()
            except OSError as exc:
                logger.debug("Failed to prune snapshot file %s: %s", path, exc)


def _atexit_close_state_store() -> None:
    """Atexit handler: checkpoint WAL and close the state store."""
    if _STORE_SINGLETON is not None:
        try:
            _STORE_SINGLETON.close()
            logger.debug("PersistentStateStore closed via atexit")
        except Exception:
            logger.debug("PersistentStateStore atexit close failed")


def get_state_store() -> PersistentStateStore:
    """Return the process-wide persistent state store singleton."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        with _SCHEMA_LOCK:
            if _STORE_SINGLETON is None:
                from services.persistence.factory import create_state_store

                _STORE_SINGLETON = create_state_store(run_self_check=True)
                atexit.register(_atexit_close_state_store)
    return _STORE_SINGLETON


def reset_state_store_for_tests() -> None:
    """Dispose of the singleton instance."""
    global _STORE_SINGLETON
    with _SCHEMA_LOCK:
        if _STORE_SINGLETON is not None:
            _STORE_SINGLETON.close()
        _STORE_SINGLETON = None


__all__ = [
    "DB_FILENAME",
    "LOCAL_USER_ID",
    "SCHEMA_VERSION",
    "STATE_DB_SCHEMA_LOCK",
    "SYSTEM_USER_ID",
    "PersistentStateStore",
    "StateStoreSelfCheckError",
    "get_state_store",
    "reset_state_store_for_tests",
]
