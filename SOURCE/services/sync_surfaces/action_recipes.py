"""Adapter for ``sync_action_recipes``."""

from __future__ import annotations

from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, list_keyed_surface, upsert_surface

ACTION_RECIPES = SurfaceDefinition(
    surface="action_recipes",
    table="sync_action_recipes",
    pk_columns=("user_id", "id"),
    data_columns=(
        "intent",
        "summary",
        "user_text",
        "assistant_text",
        "params_json",
        "steps_json",
        "source",
        "use_count",
        "last_used_at",
    ),
    json_columns=("params_json", "steps_json", "version_vector", "field_versions"),
    order_by=("intent", "commit_seq"),
)


async def list_action_recipes(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, ACTION_RECIPES, user_id, since_seq)


async def upsert_action_recipe(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, ACTION_RECIPES, user_id, payload, incoming)


async def delete_action_recipe(
    conn: asyncpg.Connection,
    user_id: str,
    recipe_id: str,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, ACTION_RECIPES, user_id, recipe_id, incoming)
