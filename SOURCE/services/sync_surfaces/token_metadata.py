"""Adapter for ``sync_token_metadata`` provider status metadata."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import (
    SurfaceDefinition,
    delete_surface,
    list_keyed_surface,
    reject_tier3_sync_payload,
    sanitize_token_metadata,
    upsert_surface,
)

TOKEN_METADATA = SurfaceDefinition(
    surface="token_metadata",
    table="sync_token_metadata",
    pk_columns=("user_id", "provider_id"),
    data_columns=("payload_json",),
    json_columns=("payload_json", "version_vector", "field_versions"),
)


def _validated_payload_json(payload: dict[str, Any]) -> Any:
    raw_payload = payload.get("payload_json", payload.get("payload", {}))
    reject_tier3_sync_payload(TOKEN_METADATA.surface, raw_payload)
    return sanitize_token_metadata(raw_payload)


async def list_token_metadata(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    rows = await list_keyed_surface(conn, TOKEN_METADATA, user_id, since_seq)
    for row in rows:
        row["payload_json"] = sanitize_token_metadata(row.get("payload_json", {}))
    return rows


async def upsert_token_metadata(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    clean = dict(payload)
    clean["payload_json"] = _validated_payload_json(clean)
    row = await upsert_surface(conn, TOKEN_METADATA, user_id, clean, incoming)
    row["payload_json"] = sanitize_token_metadata(row.get("payload_json", {}))
    return row


async def delete_token_metadata(
    conn: asyncpg.Connection,
    user_id: str,
    provider_id: str,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, TOKEN_METADATA, user_id, provider_id, incoming)
