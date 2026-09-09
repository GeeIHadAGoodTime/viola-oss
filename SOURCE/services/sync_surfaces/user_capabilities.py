"""Adapter for ``sync_user_capabilities``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg
from pydantic import ValidationError

from services.sync import IncomingMutation
from services.sync_surfaces.base import (
    SurfaceDefinition,
    SyncSurfaceDatabaseError,
    delete_surface,
    list_keyed_surface,
    upsert_surface,
)
from services.user_capabilities.schema import UserCapability, validate_capability_id

USER_CAPABILITIES = SurfaceDefinition(
    surface="user_capabilities",
    table="sync_user_capabilities",
    pk_columns=("user_id", "capability_id"),
    data_columns=("capability_json",),
    json_columns=("capability_json", "version_vector", "field_versions"),
)


def _invalid_user_capability_payload(
    *, column: str | None = None, field: str | None = None
) -> SyncSurfaceDatabaseError:
    details: dict[str, Any] = {"surface": "user_capabilities"}
    if column:
        details["column"] = column
    if field:
        details["field"] = field
    return SyncSurfaceDatabaseError("Invalid user_capabilities payload.", details=details)


def prepare_user_capability_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    capability_id = str(clean.get("capability_id") or "").strip()
    if capability_id:
        try:
            clean["capability_id"] = validate_capability_id(capability_id)
        except ValueError as exc:
            raise _invalid_user_capability_payload(field="capability_id") from exc

    if "capability_json" not in clean:
        return clean

    raw_capability = clean.get("capability_json")
    if not isinstance(raw_capability, Mapping):
        raise _invalid_user_capability_payload(column="capability_json")
    try:
        capability = UserCapability.model_validate(dict(raw_capability))
    except (TypeError, ValueError, ValidationError) as exc:
        raise _invalid_user_capability_payload(column="capability_json") from exc

    if capability_id and capability.id != clean["capability_id"]:
        raise _invalid_user_capability_payload(field="capability_id")

    clean["capability_json"] = capability.model_dump(mode="json")
    return clean


async def list_user_capabilities(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, USER_CAPABILITIES, user_id, since_seq)


async def upsert_user_capability(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, USER_CAPABILITIES, user_id, prepare_user_capability_payload(payload), incoming)


async def delete_user_capability(
    conn: asyncpg.Connection,
    user_id: str,
    capability_id: str,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, USER_CAPABILITIES, user_id, capability_id, incoming)
