"""Adapter for ``sync_schedules``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, list_keyed_surface, upsert_surface

SCHEDULES = SurfaceDefinition(
    surface="schedules",
    table="sync_schedules",
    pk_columns=("user_id", "id"),
    data_columns=(
        "label",
        "action",
        "cron_expr",
        "one_shot_at",
        "enabled",
        "last_run_at",
        "next_run_at",
        "run_count",
        "last_status",
        "last_error",
        "consecutive_failures",
    ),
    json_columns=("version_vector", "field_versions"),
    generated_pk=True,
    order_by=("next_run_at", "commit_seq"),
)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _optional_text(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if _is_blank(value):
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    return value.strip()


def _parse_optional_datetime(value: Any, field: str) -> datetime | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("%s must be an ISO 8601 datetime" % field) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _compute_next_run_at(cron_expr: str) -> datetime:
    from services.scheduler.service import compute_next_run

    next_run = _parse_optional_datetime(compute_next_run(cron_expr), "next_run_at")
    if next_run is None:
        raise ValueError("next_run_at could not be computed")
    return next_run


def prepare_schedule_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    is_create = _is_blank(out.get("id"))

    label = _optional_text(out, "label")
    if label is not None:
        out["label"] = label
    elif is_create:
        raise ValueError("Schedule label cannot be empty")

    action = _optional_text(out, "action")
    if action is not None:
        out["action"] = action
    elif is_create:
        raise ValueError("Schedule action cannot be empty")

    cron_expr = _optional_text(out, "cron_expr")
    one_shot_at = _parse_optional_datetime(out.get("one_shot_at"), "one_shot_at")
    next_run_at = _parse_optional_datetime(out.get("next_run_at"), "next_run_at")
    last_run_at = _parse_optional_datetime(out.get("last_run_at"), "last_run_at")

    if cron_expr is not None:
        out["cron_expr"] = cron_expr
    else:
        out.pop("cron_expr", None)
    if one_shot_at is not None:
        out["one_shot_at"] = one_shot_at
    else:
        out.pop("one_shot_at", None)
    if last_run_at is not None:
        out["last_run_at"] = last_run_at
    else:
        out.pop("last_run_at", None)

    if cron_expr is not None and one_shot_at is not None:
        raise ValueError("Cannot provide both cron_expr and one_shot_at")
    if is_create and cron_expr is None and one_shot_at is None:
        raise ValueError("Either cron_expr or one_shot_at must be provided")

    if cron_expr is not None:
        out["next_run_at"] = next_run_at or _compute_next_run_at(cron_expr)
    elif one_shot_at is not None:
        out["next_run_at"] = next_run_at or one_shot_at
    elif next_run_at is not None:
        out["next_run_at"] = next_run_at
    else:
        out.pop("next_run_at", None)

    return out


async def list_schedules(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, SCHEDULES, user_id, since_seq)


async def upsert_schedule(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, SCHEDULES, user_id, prepare_schedule_payload(payload), incoming)


async def delete_schedule(
    conn: asyncpg.Connection,
    user_id: str,
    schedule_id: int,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, SCHEDULES, user_id, schedule_id, incoming)
