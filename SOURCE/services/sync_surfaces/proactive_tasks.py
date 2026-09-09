"""Adapter for ``sync_proactive_tasks``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import asyncpg

from services.sync import IncomingMutation
from services.sync_surfaces.base import SurfaceDefinition, delete_surface, list_keyed_surface, upsert_surface

PROACTIVE_TASKS = SurfaceDefinition(
    surface="proactive_tasks",
    table="sync_proactive_tasks",
    pk_columns=("user_id", "id"),
    data_columns=(
        "name",
        "cron_expr",
        "timezone",
        "task_prompt",
        "enabled",
        "last_run_at",
        "next_run_at",
        "last_status",
        "last_error",
        "delivery_channel",
        "active_hours_start",
        "active_hours_end",
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


def prepare_proactive_task_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    is_create = _is_blank(out.get("id"))

    name = _optional_text(out, "name")
    if name is not None:
        out["name"] = name
    elif is_create:
        raise ValueError("Proactive task name cannot be empty")

    task_prompt = _optional_text(out, "task_prompt")
    if task_prompt is not None:
        out["task_prompt"] = task_prompt
    elif is_create:
        raise ValueError("Proactive task prompt cannot be empty")

    cron_expr = _optional_text(out, "cron_expr")
    if cron_expr is not None:
        out["cron_expr"] = cron_expr
    elif is_create:
        raise ValueError("Proactive task cron_expr cannot be empty")
    else:
        out.pop("cron_expr", None)

    timezone = _optional_text(out, "timezone")
    if timezone is not None:
        out["timezone"] = timezone
    else:
        out.pop("timezone", None)

    delivery_channel = _optional_text(out, "delivery_channel")
    if delivery_channel is not None:
        out["delivery_channel"] = delivery_channel
    else:
        out.pop("delivery_channel", None)

    last_run_at = _parse_optional_datetime(out.get("last_run_at"), "last_run_at")
    if last_run_at is not None:
        out["last_run_at"] = last_run_at
    else:
        out.pop("last_run_at", None)

    last_status = _optional_text(out, "last_status")
    if last_status is not None:
        if last_status not in {"ok", "timeout", "error"}:
            raise ValueError("last_status must be ok, timeout, or error")
        out["last_status"] = last_status
    else:
        out.pop("last_status", None)

    last_error = _optional_text(out, "last_error")
    if last_error is not None:
        out["last_error"] = last_error
    else:
        out.pop("last_error", None)

    next_run_at = _parse_optional_datetime(out.get("next_run_at"), "next_run_at")
    if cron_expr is not None:
        out["next_run_at"] = next_run_at or _compute_next_run_at(cron_expr)
    elif next_run_at is not None:
        out["next_run_at"] = next_run_at
    else:
        out.pop("next_run_at", None)

    return out


async def list_proactive_tasks(conn: asyncpg.Connection, user_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    return await list_keyed_surface(conn, PROACTIVE_TASKS, user_id, since_seq)


async def upsert_proactive_task(
    conn: asyncpg.Connection,
    user_id: str,
    payload: dict[str, Any],
    incoming: IncomingMutation | None = None,
) -> dict[str, Any]:
    return await upsert_surface(conn, PROACTIVE_TASKS, user_id, prepare_proactive_task_payload(payload), incoming)


async def delete_proactive_task(
    conn: asyncpg.Connection,
    user_id: str,
    task_id: int,
    incoming: IncomingMutation | None = None,
) -> None:
    await delete_surface(conn, PROACTIVE_TASKS, user_id, task_id, incoming)
