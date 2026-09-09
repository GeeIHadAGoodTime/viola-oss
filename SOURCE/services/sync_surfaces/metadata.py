"""Adapter for ``sync_metadata``."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, list_keyed_surface, upsert_surface

METADATA = SurfaceDefinition(
    surface="metadata",
    table="sync_metadata",
    pk_columns=("user_id", "key"),
    data_columns=("value_json",),
    json_columns=("value_json", "version_vector", "field_versions"),
)


async def list_metadata(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, METADATA, user_id, since_seq)


async def upsert_metadata(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, METADATA, user_id, payload, incoming)


async def delete_metadata(
    conn: asyncpg.Connection,
    user_id: str,
    key: str,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, METADATA, user_id, key, incoming)
