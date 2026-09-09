"""Adapter for ``sync_queue_items``."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, list_keyed_surface, upsert_surface

QUEUE_ITEMS = SurfaceDefinition(
    surface="queue_items",
    table="sync_queue_items",
    pk_columns=("user_id", "position"),
    data_columns=("payload_json",),
    json_columns=("payload_json", "version_vector", "field_versions"),
    order_by=("position", "commit_seq"),
)


async def list_queue_items(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, QUEUE_ITEMS, user_id, since_seq)


async def upsert_queue_item(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, QUEUE_ITEMS, user_id, payload, incoming)


async def delete_queue_item(
    conn: asyncpg.Connection,
    user_id: str,
    position: int,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, QUEUE_ITEMS, user_id, position, incoming)
