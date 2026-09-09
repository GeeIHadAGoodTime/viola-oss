"""Adapter for ``sync_user_profiles``."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, get_single_surface, upsert_surface

USER_PROFILES = SurfaceDefinition(
    surface="user_profiles",
    table="sync_user_profiles",
    pk_columns=("user_id",),
    data_columns=("profile_json",),
    json_columns=("profile_json", "version_vector", "field_versions"),
)


async def get_user_profile(conn: asyncpg.Connection, user_id: str) -> dict[str, Any] | None:
    return await get_single_surface(conn, USER_PROFILES, user_id)


async def upsert_user_profile(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, USER_PROFILES, user_id, payload, incoming)


async def delete_user_profile(conn: asyncpg.Connection, user_id: str) -> None:
    await delete_surface(conn, USER_PROFILES, user_id)
