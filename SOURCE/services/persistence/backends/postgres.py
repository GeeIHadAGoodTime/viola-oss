"""PostgreSQL backend for PersistentStateStore."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from core.asyncio_safe import run_async_synchronously
from core.logging_config import get_logger
from services.persistence.backends.protocol import (
    MUSIC_STATE_KEYS,
    SCHEMA_VERSION,
    SELF_CHECK_USER_ID,
    SYSTEM_USER_ID,
    PersistenceBackend,
    deserialize_value,
    require_user_id,
    serialize_value,
)

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_T = TypeVar("_T")
_TIMEOUT = object()
_SYNC_TIMEOUT_SECONDS = 10.0

_PG_REQUIRED_COLUMNS = {
    "schema_version": {"id", "version"},
    "sync_metadata": {
        "user_id",
        "key",
        "value_json",
        "consent_generation",
        "version_vector",
        "field_versions",
        "lww_hlc",
        "lww_actor_id",
        "commit_seq",
        "updated_by_device_id",
        "last_mutation_id",
        "updated_at",
        "deleted_at",
    },
    "sync_token_metadata": {
        "user_id",
        "provider_id",
        "payload_json",
        "consent_generation",
        "version_vector",
        "field_versions",
        "lww_hlc",
        "lww_actor_id",
        "commit_seq",
        "updated_by_device_id",
        "last_mutation_id",
        "updated_at",
        "deleted_at",
    },
    "sync_queue_items": {
        "user_id",
        "position",
        "payload_json",
        "consent_generation",
        "version_vector",
        "field_versions",
        "lww_hlc",
        "lww_actor_id",
        "commit_seq",
        "updated_by_device_id",
        "last_mutation_id",
        "updated_at",
        "deleted_at",
    },
    "sync_user_preferences": {
        "user_id",
        "key",
        "value_json",
        "consent_generation",
        "deleted_at",
    },
    "sync_change_journal": {
        "commit_seq",
        "user_id",
        "surface",
        "table_name",
        "entity_id",
        "operation",
    },
    "calibration_profiles": {"user_id", "name", "payload", "updated_at"},
    "restart_counters": {"name", "value", "updated_at"},
    "snapshots": {"id", "user_id", "snapshot_type", "payload", "version", "created_at"},
}
_PG_REQUIRED_RELATIONS = ("sync_commit_seq",)
_PG_FORBIDDEN_LEGACY_SYNC_RELATIONS = ("metadata", "queue_items", "token_metadata")
_POSTGRES_SYSTEM_USER_IDS = {SYSTEM_USER_ID, SELF_CHECK_USER_ID}


def _decode_jsonb(value: object) -> object:
    if isinstance(value, str):
        return deserialize_value(value)
    return value


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _pg_nullable_user_id(user_id: str) -> str | None:
    resolved = require_user_id(user_id)
    if resolved in _POSTGRES_SYSTEM_USER_IDS:
        return None
    return resolved


async def _resolved_relation(conn: Any, relation: str) -> tuple[str, str] | None:
    row = await conn.fetchrow(
        """
        SELECT n.nspname AS schema_name, c.relname AS relation_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.oid = to_regclass($1)
        """,
        relation,
    )
    if row is None:
        return None
    return str(row["schema_name"]), str(row["relation_name"])


async def _assert_pg_relations(conn: Any, relations: Sequence[str], *, owner: str) -> None:
    missing = [relation for relation in relations if await _resolved_relation(conn, relation) is None]
    if missing:
        raise RuntimeError("%s missing Alembic-owned PostgreSQL relations: %s" % (owner, ", ".join(sorted(missing))))


async def _assert_pg_columns(conn: Any, columns_by_relation: dict[str, set[str]], *, owner: str) -> None:
    missing: list[str] = []
    for relation, expected_columns in columns_by_relation.items():
        resolved = await _resolved_relation(conn, relation)
        if resolved is None:
            missing.extend("%s.%s" % (relation, column) for column in sorted(expected_columns))
            continue
        schema, table = resolved
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = $1
              AND table_name = $2
            """,
            schema,
            table,
        )
        actual = {str(row["column_name"]) for row in rows}
        missing.extend("%s.%s" % (relation, column) for column in sorted(expected_columns - actual))
    if missing:
        raise RuntimeError("%s missing Alembic-owned PostgreSQL columns: %s" % (owner, ", ".join(missing)))


async def _assert_pg_relations_absent(conn: Any, relations: Sequence[str], *, owner: str) -> None:
    present = [relation for relation in relations if await _resolved_relation(conn, relation) is not None]
    if present:
        raise RuntimeError(
            "%s found retired legacy sync relations; run the canonical state-store sync migration: %s"
            % (owner, ", ".join(sorted(present)))
        )


class PostgresBackend(PersistenceBackend):
    """PostgreSQL implementation of the state-store backend contract."""

    backend_name = "postgres"
    is_postgres = True
    supports_synthetic_self_check = False

    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise RuntimeError("PostgresBackend requires a PostgreSQL database URL")
        self._database_url = database_url
        self._pg_initialized = False
        self.initialize()

    @property
    def db_path(self) -> Path | None:
        return None

    @property
    def pg_initialized(self) -> bool:
        return self._pg_initialized

    def initialize(self) -> None:
        self._run(self.initialize_async(), operation="initialize")

    async def initialize_async(self) -> None:
        """Initialize PostgreSQL schema."""
        if self._pg_initialized:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await _assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="PersistentStateStore")
            await _assert_pg_columns(conn, _PG_REQUIRED_COLUMNS, owner="PersistentStateStore")
            await _assert_pg_relations_absent(
                conn,
                _PG_FORBIDDEN_LEGACY_SYNC_RELATIONS,
                owner="PersistentStateStore",
            )
        self._pg_initialized = True
        logger.info("PersistentStateStore initialized (PostgreSQL)")

    def close(self) -> None:
        """Shared asyncpg pool lifecycle is owned by core.db_backend."""
        return

    async def _pg_pool(self) -> Any:
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    def _run(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        operation: str,
        timeout: float = _SYNC_TIMEOUT_SECONDS,
    ) -> Any:
        result = run_async_synchronously(
            coro,
            timeout=timeout,
            timeout_result=_TIMEOUT,
            timeout_log_message="State-store PostgreSQL %s timed out after %%.1fs." % operation,
            logger=logger,
        )
        if result is _TIMEOUT:
            raise TimeoutError("State-store PostgreSQL %s timed out after %.1fs" % (operation, timeout))
        return result

    async def _with_sync_connection(
        self,
        user_id: str,
        operation: Callable[[Any], Awaitable[_T]],
        *,
        require_consent: bool = False,
    ) -> _T:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            from services.sync import ConsentRequiredError, set_rls_context
            from services.sync.consent import has_cloud_sync_consent_locked

            await set_rls_context(conn, user_id)
            if require_consent and not await has_cloud_sync_consent_locked(conn, user_id):
                raise ConsentRequiredError()
            return await operation(conn)

    async def _fetch_metadata_async(self, user_id: str, keys: Sequence[str]) -> dict[str, object]:
        if not keys:
            return {}

        async def _operation(conn: Any) -> dict[str, object]:
            rows = await conn.fetch(
                """
                SELECT key, value_json
                FROM sync_metadata
                WHERE user_id = $1::uuid
                  AND key = ANY($2::text[])
                  AND deleted_at IS NULL
                """,
                user_id,
                list(keys),
            )
            return {str(row["key"]): _decode_jsonb(row["value_json"]) for row in rows}

        return await self._with_sync_connection(user_id, _operation)

    async def _upsert_metadata_async(self, user_id: str, key: str, value: object, ts: float) -> None:
        del ts

        async def _operation(conn: Any) -> None:
            from services.sync_surfaces import metadata as metadata_surface

            await metadata_surface.upsert_metadata(conn, user_id, {"key": key, "value_json": value})

        await self._with_sync_connection(user_id, _operation, require_consent=True)

    def get_setting(self, user_id: str, key: str, *, default: object = None) -> object:
        resolved_user_id = require_user_id(user_id)
        result = cast(
            dict[str, object],
            self._run(
                self._fetch_metadata_async(resolved_user_id, [key]),
                operation="get_setting",
            ),
        )
        return result.get(key, default)

    async def get_setting_async(self, user_id: str, key: str, *, default: object = None) -> object:
        # Native async read: awaited directly on the caller's loop with no
        # sync->async `_run` bridge, so it is safe on the cloud FastAPI serving
        # loop where the sync `get_setting` would raise SyncBridgeLoopError
        # (CL-20260711-afd7).
        resolved_user_id = require_user_id(user_id)
        result = await self._fetch_metadata_async(resolved_user_id, [key])
        return result.get(key, default)

    def set_setting(self, user_id: str, key: str, value: object) -> None:
        resolved_user_id = require_user_id(user_id)
        self._run(
            self._upsert_metadata_async(resolved_user_id, key, value, time.time()),
            operation="set_setting",
        )

    def delete_setting(self, user_id: str, key: str) -> bool:
        resolved_user_id = require_user_id(user_id)
        return bool(
            self._run(
                self._delete_setting_async(resolved_user_id, key),
                operation="delete_setting",
            )
        )

    async def _delete_setting_async(self, user_id: str, key: str) -> bool:
        async def _operation(conn: Any) -> bool:
            from services.sync_surfaces import metadata as metadata_surface

            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM sync_metadata
                    WHERE user_id = $1::uuid
                      AND key = $2
                      AND deleted_at IS NULL
                )
                """,
                user_id,
                key,
            )
            if not exists:
                return False
            await metadata_surface.delete_metadata(conn, user_id, key)
            return True

        return await self._with_sync_connection(user_id, _operation, require_consent=True)

    def save_music_state(
        self,
        *,
        user_id: str,
        queue: Sequence[dict[str, object]],
        now_playing: dict[str, object] | None,
        volume: int,
        is_playing: bool,
    ) -> None:
        resolved_user_id = require_user_id(user_id)
        self._run(
            self._save_music_state_async(
                user_id=resolved_user_id,
                queue=list(queue),
                now_playing=now_playing,
                volume=volume,
                is_playing=is_playing,
            ),
            operation="save_music_state",
        )

    async def _save_music_state_async(
        self,
        *,
        user_id: str,
        queue: Sequence[dict[str, object]],
        now_playing: dict[str, object] | None,
        volume: int,
        is_playing: bool,
    ) -> None:
        async def _operation(conn: Any) -> None:
            from services.sync_surfaces import (
                metadata as metadata_surface,
                queue_items as queue_surface,
            )

            existing_rows = await conn.fetch(
                """
                SELECT position
                FROM sync_queue_items
                WHERE user_id = $1::uuid
                  AND deleted_at IS NULL
                """,
                user_id,
            )
            existing_positions = {int(row["position"]) for row in existing_rows}
            replacement_positions = set(range(len(queue)))
            for position in sorted(existing_positions - replacement_positions):
                await queue_surface.delete_queue_item(conn, user_id, position)
            for position, payload in enumerate(queue):
                await queue_surface.upsert_queue_item(
                    conn,
                    user_id,
                    {"position": position, "payload_json": dict(payload)},
                )
            await metadata_surface.upsert_metadata(
                conn,
                user_id,
                {"key": "music.volume", "value_json": int(volume)},
            )
            await metadata_surface.upsert_metadata(
                conn,
                user_id,
                {"key": "music.is_playing", "value_json": bool(is_playing)},
            )
            if now_playing is None:
                await metadata_surface.delete_metadata(conn, user_id, "music.now_playing")
            else:
                await metadata_surface.upsert_metadata(
                    conn,
                    user_id,
                    {"key": "music.now_playing", "value_json": dict(now_playing)},
                )

        await self._with_sync_connection(user_id, _operation, require_consent=True)

    def load_music_state(self, user_id: str) -> dict[str, object]:
        resolved_user_id = require_user_id(user_id)
        return cast(
            dict[str, object],
            self._run(
                self._load_music_state_async(resolved_user_id),
                operation="load_music_state",
            ),
        )

    async def _load_music_state_async(self, user_id: str) -> dict[str, object]:
        async def _operation(conn: Any) -> dict[str, object]:
            queue_rows = await conn.fetch(
                """
                SELECT payload_json
                FROM sync_queue_items
                WHERE user_id = $1::uuid
                  AND deleted_at IS NULL
                ORDER BY position ASC
                """,
                user_id,
            )
            meta_rows = await conn.fetch(
                """
                SELECT key, value_json
                FROM sync_metadata
                WHERE user_id = $1::uuid
                  AND key = ANY($2::text[])
                  AND deleted_at IS NULL
                """,
                user_id,
                list(MUSIC_STATE_KEYS),
            )
            queue = [_decode_jsonb(row["payload_json"]) for row in queue_rows]
            meta = {str(row["key"]): _decode_jsonb(row["value_json"]) for row in meta_rows}
            return {
                "queue": queue,
                "now_playing": meta.get("music.now_playing"),
                "volume": meta.get("music.volume"),
                "is_playing": meta.get("music.is_playing", False),
            }

        return await self._with_sync_connection(user_id, _operation)

    def clear_stale_queue(self, user_id: str) -> int:
        resolved_user_id = require_user_id(user_id)
        return int(
            self._run(
                self._clear_stale_queue_async(resolved_user_id),
                operation="clear_stale_queue",
            )
        )

    async def _clear_stale_queue_async(self, user_id: str) -> int:
        async def _operation(conn: Any) -> int:
            from services.sync_surfaces import (
                metadata as metadata_surface,
                queue_items as queue_surface,
            )

            rows = await conn.fetch(
                """
                SELECT position
                FROM sync_queue_items
                WHERE user_id = $1::uuid
                  AND deleted_at IS NULL
                """,
                user_id,
            )
            positions = [int(row["position"]) for row in rows]
            for position in positions:
                await queue_surface.delete_queue_item(conn, user_id, position)
            if positions:
                logger.info(
                    "Cleared %d stale queue items from persistence for user=%s",
                    len(positions),
                    user_id,
                )
            await metadata_surface.delete_metadata(conn, user_id, "music.now_playing")
            await metadata_surface.delete_metadata(conn, user_id, "music.is_playing")
            return len(positions)

        return await self._with_sync_connection(user_id, _operation, require_consent=True)

    def update_restart_counter(self, name: str, value: int) -> None:
        self._run(
            self._update_restart_counter_async(name, int(value), time.time()),
            operation="update_restart_counter",
        )

    async def _update_restart_counter_async(self, name: str, value: int, ts: float) -> None:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO restart_counters(name, value, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT(name)
                DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                """,
                name,
                value,
                ts,
            )

    def load_restart_counters(self) -> dict[str, int]:
        return cast(
            dict[str, int],
            self._run(self._load_restart_counters_async(), operation="load_restart_counters"),
        )

    async def _load_restart_counters_async(self) -> dict[str, int]:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, value FROM restart_counters")
        return {str(row["name"]): int(row["value"]) for row in rows}

    def save_calibration(self, profile: str, payload: dict[str, object], *, user_id: str = SYSTEM_USER_ID) -> None:
        resolved_user_id = _pg_nullable_user_id(user_id)
        self._run(
            self._save_calibration_async(resolved_user_id, profile.lower(), payload, time.time()),
            operation="save_calibration",
        )

    async def _save_calibration_async(
        self,
        user_id: str | None,
        profile: str,
        payload: dict[str, object],
        ts: float,
    ) -> None:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            if user_id is None:
                await conn.execute(
                    """
                    INSERT INTO calibration_profiles(user_id, name, payload, updated_at)
                    VALUES (NULL, $1, $2, $3)
                    ON CONFLICT(name) WHERE user_id IS NULL
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
                    """,
                    profile,
                    serialize_value(payload),
                    ts,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO calibration_profiles(user_id, name, payload, updated_at)
                    VALUES ($1::uuid, $2, $3, $4)
                    ON CONFLICT(user_id, name) WHERE user_id IS NOT NULL
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
                    """,
                    user_id,
                    profile,
                    serialize_value(payload),
                    ts,
                )

    def load_calibration(self, profile: str, *, user_id: str = SYSTEM_USER_ID) -> dict[str, object] | None:
        resolved_user_id = _pg_nullable_user_id(user_id)
        return cast(
            dict[str, object] | None,
            self._run(
                self._load_calibration_async(resolved_user_id, profile.lower()),
                operation="load_calibration",
            ),
        )

    async def _load_calibration_async(self, user_id: str | None, profile: str) -> dict[str, object] | None:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            if user_id is None:
                row = await conn.fetchrow(
                    "SELECT payload FROM calibration_profiles WHERE user_id IS NULL AND name = $1",
                    profile,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT payload FROM calibration_profiles WHERE user_id = $1::uuid AND name = $2",
                    user_id,
                    profile,
                )
        if row is None:
            return None
        payload = deserialize_value(str(row["payload"]))
        return cast(dict[str, object], payload) if isinstance(payload, dict) else None

    def load_all_calibrations(self, *, user_id: str = SYSTEM_USER_ID) -> dict[str, dict[str, object]]:
        resolved_user_id = _pg_nullable_user_id(user_id)
        return cast(
            dict[str, dict[str, object]],
            self._run(
                self._load_all_calibrations_async(resolved_user_id),
                operation="load_all_calibrations",
            ),
        )

    async def _load_all_calibrations_async(self, user_id: str | None) -> dict[str, dict[str, object]]:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            if user_id is None:
                rows = await conn.fetch(
                    "SELECT name, payload FROM calibration_profiles WHERE user_id IS NULL",
                )
            else:
                rows = await conn.fetch(
                    "SELECT name, payload FROM calibration_profiles WHERE user_id = $1::uuid",
                    user_id,
                )
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            payload = deserialize_value(str(row["payload"]))
            if isinstance(payload, dict):
                result[str(row["name"])] = cast(dict[str, object], payload)
        return result

    def upsert_token_metadata(self, user_id: str, provider_id: str, payload: dict[str, object]) -> None:
        resolved_user_id = require_user_id(user_id)
        self._run(
            self._upsert_token_metadata_async(resolved_user_id, provider_id.lower(), payload, time.time()),
            operation="upsert_token_metadata",
        )

    async def _upsert_token_metadata_async(
        self,
        user_id: str,
        provider_id: str,
        payload: dict[str, object],
        ts: float,
    ) -> None:
        del ts

        async def _operation(conn: Any) -> None:
            from services.sync_surfaces import token_metadata as token_surface
            from services.sync_surfaces.base import sanitize_token_metadata

            safe_payload = sanitize_token_metadata(dict(payload))
            await token_surface.upsert_token_metadata(
                conn,
                user_id,
                {
                    "provider_id": provider_id,
                    "payload_json": safe_payload if isinstance(safe_payload, dict) else {},
                },
            )

        await self._with_sync_connection(user_id, _operation, require_consent=True)

    def delete_token_metadata(self, user_id: str, provider_id: str) -> bool:
        resolved_user_id = require_user_id(user_id)
        return bool(
            self._run(
                self._delete_token_metadata_async(resolved_user_id, provider_id.lower()),
                operation="delete_token_metadata",
            )
        )

    async def _delete_token_metadata_async(self, user_id: str, provider_id: str) -> bool:
        async def _operation(conn: Any) -> bool:
            from services.sync_surfaces import token_metadata as token_surface

            exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM sync_token_metadata
                    WHERE user_id = $1::uuid
                      AND provider_id = $2
                      AND deleted_at IS NULL
                )
                """,
                user_id,
                provider_id,
            )
            if not exists:
                return False
            await token_surface.delete_token_metadata(conn, user_id, provider_id)
            return True

        return await self._with_sync_connection(user_id, _operation, require_consent=True)

    def delete_all_token_metadata(self, user_id: str) -> int:
        resolved_user_id = require_user_id(user_id)
        return int(
            self._run(
                self._delete_all_token_metadata_async(resolved_user_id),
                operation="delete_all_token_metadata",
            )
        )

    async def _delete_all_token_metadata_async(self, user_id: str) -> int:
        async def _operation(conn: Any) -> int:
            from services.sync_surfaces import token_metadata as token_surface

            rows = await conn.fetch(
                """
                SELECT provider_id
                FROM sync_token_metadata
                WHERE user_id = $1::uuid
                  AND deleted_at IS NULL
                """,
                user_id,
            )
            provider_ids = [str(row["provider_id"]) for row in rows]
            for provider_id in provider_ids:
                await token_surface.delete_token_metadata(conn, user_id, provider_id)
            return len(provider_ids)

        return await self._with_sync_connection(user_id, _operation, require_consent=True)

    def load_token_metadata(self, user_id: str) -> dict[str, dict[str, object]]:
        resolved_user_id = require_user_id(user_id)
        return cast(
            dict[str, dict[str, object]],
            self._run(
                self._load_token_metadata_async(resolved_user_id),
                operation="load_token_metadata",
            ),
        )

    async def _load_token_metadata_async(self, user_id: str) -> dict[str, dict[str, object]]:
        async def _operation(conn: Any) -> dict[str, dict[str, object]]:
            from services.sync_surfaces.base import sanitize_token_metadata

            rows = await conn.fetch(
                """
                SELECT provider_id, payload_json
                FROM sync_token_metadata
                WHERE user_id = $1::uuid
                  AND deleted_at IS NULL
                """,
                user_id,
            )
            result: dict[str, dict[str, object]] = {}
            for row in rows:
                payload = sanitize_token_metadata(_decode_jsonb(row["payload_json"]))
                if isinstance(payload, dict):
                    result[str(row["provider_id"])] = cast(dict[str, object], payload)
            return result

        return await self._with_sync_connection(user_id, _operation)

    def record_snapshot(self, *, user_id: str, snapshot_type: str, serialized: str, ts: float) -> None:
        resolved_user_id = _pg_nullable_user_id(user_id)
        self._run(
            self._record_snapshot_async(resolved_user_id, snapshot_type, serialized, ts),
            operation="record_snapshot",
        )

    async def _record_snapshot_async(self, user_id: str | None, snapshot_type: str, serialized: str, ts: float) -> None:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO snapshots(user_id, snapshot_type, payload, version, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5)
                """,
                user_id,
                snapshot_type,
                serialized,
                SCHEMA_VERSION,
                ts,
            )

    def list_snapshots(self, limit: int = 10, *, user_id: str | None = None) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]],
            self._run(
                self._list_snapshots_async(limit, user_id=user_id),
                operation="list_snapshots",
            ),
        )

    async def _list_snapshots_async(self, limit: int = 10, *, user_id: str | None = None) -> list[dict[str, object]]:
        # Multi-tenant: refuse a global list — callers must scope to a
        # single tenant.  Diagnostic tools should pass the system user
        # id for system-owned snapshots.
        if user_id is None:
            return []
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            resolved_user_id = _pg_nullable_user_id(user_id)
            if resolved_user_id is None:
                rows = await conn.fetch(
                    """
                    SELECT snapshot_type, payload, created_at
                    FROM snapshots
                    WHERE user_id IS NULL
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    max(1, int(limit)),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT snapshot_type, payload, created_at
                    FROM snapshots
                    WHERE user_id = $1::uuid
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    resolved_user_id,
                    max(1, int(limit)),
                )
        return [
            {
                "snapshot_type": row["snapshot_type"],
                "created_at": row["created_at"],
                "payload": deserialize_value(str(row["payload"])),
            }
            for row in rows
        ]

    def cleanup_self_check(
        self,
        *,
        user_id: str,
        restart_counter: str,
        calibration_profile: str,
        snapshot_type: str,
    ) -> None:
        self._run(
            self._cleanup_self_check_async(
                user_id=user_id,
                restart_counter=restart_counter,
                calibration_profile=calibration_profile.lower(),
                snapshot_type=snapshot_type,
            ),
            operation="cleanup_self_check",
        )

    async def _cleanup_self_check_async(
        self,
        *,
        user_id: str,
        restart_counter: str,
        calibration_profile: str,
        snapshot_type: str,
    ) -> None:
        await self.initialize_async()
        pool = await self._pg_pool()
        async with pool.acquire() as conn, conn.transaction():
            if _is_uuid(user_id):
                await conn.execute("DELETE FROM sync_metadata WHERE user_id = $1::uuid", user_id)
                await conn.execute("DELETE FROM sync_queue_items WHERE user_id = $1::uuid", user_id)
                await conn.execute("DELETE FROM sync_token_metadata WHERE user_id = $1::uuid", user_id)
            stored_user_id = _pg_nullable_user_id(user_id)
            if stored_user_id is None:
                await conn.execute(
                    "DELETE FROM calibration_profiles WHERE user_id IS NULL AND name = $1",
                    calibration_profile,
                )
            else:
                await conn.execute(
                    "DELETE FROM calibration_profiles WHERE user_id = $1::uuid AND name = $2",
                    stored_user_id,
                    calibration_profile,
                )
            await conn.execute("DELETE FROM restart_counters WHERE name = $1", restart_counter)
            if stored_user_id is None:
                await conn.execute(
                    "DELETE FROM snapshots WHERE user_id IS NULL AND snapshot_type = $1",
                    snapshot_type,
                )
            else:
                await conn.execute(
                    "DELETE FROM snapshots WHERE user_id = $1::uuid AND snapshot_type = $2",
                    stored_user_id,
                    snapshot_type,
                )
