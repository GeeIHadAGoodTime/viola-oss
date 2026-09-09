"""Agent tool handlers for persistent scheduled automation.

These functions are called by the MCP tool wrappers in
``mcp_servers/core_tools/server.py``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)


def _created_schedule_payload(
    *,
    schedule_id: int,
    label: str,
    schedule_type: str,
    spec_field: str,
    spec_value: str,
    saved: Any,
) -> dict[str, Any]:
    """Describe a newly created schedule using only what was read back.

    The old wording -- "%r will run on cron %s" -- promised a future execution
    that nothing here established. Two things are actually known at this point:
    the store accepted the row (it returned an id) and, once the row is read
    back, the ``next_run_at`` the service computed for it. Both are reported;
    the promise is not.

    Deliberately absent: a "the scheduler is running" flag. On cloud the rows
    are executed by a separate scheduler worker process
    (scripts/check_cloud_scheduler_worker_startup.py), so this process's own
    loop state says nothing about whether the schedule will fire -- reading it
    here would just replace an optimistic guess with a pessimistic one.
    """
    next_run_at = getattr(saved, "next_run_at", None) if saved is not None else None
    stored = saved is not None

    message = "%s schedule created (id=%d): %r stored with %s %s" % (
        "One-shot" if schedule_type == "one_shot" else "Cron",
        schedule_id,
        label,
        spec_field.replace("_", " "),
        spec_value,
    )
    if next_run_at:
        message += ", next run at %s" % next_run_at
    if not stored:
        message += ". The store accepted it but the row could not be read back, so it is not confirmed stored"

    payload: dict[str, Any] = {
        "schedule_id": schedule_id,
        "label": label,
        "schedule_type": "one-shot" if schedule_type == "one_shot" else "cron",
        spec_field: spec_value,
        "stored": stored,
        "next_run_at": next_run_at,
        "message": message,
    }
    if stored:
        payload["enabled"] = bool(getattr(saved, "enabled", True))
    return payload


async def schedule_create_handler(
    label: str,
    action: str,
    schedule: str,
    user_id: str = "",
) -> ToolResult:
    """Create a new scheduled action.

    Args:
        label: Human-readable name (e.g. "Morning jazz").
        action: What to do when it triggers, as a voice command
                (e.g. "play jazz playlist").
        schedule: Cron expression ("0 7 * * 1-5") or ISO datetime
                  for one-shot ("2026-03-01T09:00:00").

    Returns:
        ToolResult with the new schedule ID or an error.
    """
    from services.scheduler.service import get_scheduler_service, validate_cron_expr_detail

    label = label.strip()
    action = action.strip()
    schedule = schedule.strip()

    if not label:
        return ToolResult(ok=False, data=None, error="Label cannot be empty.")
    if not action:
        return ToolResult(ok=False, data=None, error="Action cannot be empty.")
    if not schedule:
        return ToolResult(ok=False, data=None, error="Schedule expression cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        svc = get_scheduler_service()

        # Determine if this is a cron expression or a one-shot datetime.
        cron_detail = validate_cron_expr_detail(schedule)
        if cron_detail is None:
            # Valid cron — svc.add() may raise ValueError for semantic issues
            # (e.g. schedule limit reached).  Let those propagate to the outer
            # except so the caller gets the real reason.
            schedule_id = await svc.add_async(
                resolved_user_id,
                label=label,
                action=action,
                cron_expr=schedule,
            )
            saved = await svc.get_async(resolved_user_id, schedule_id)
            payload = _created_schedule_payload(
                schedule_id=schedule_id,
                label=label,
                schedule_type="cron",
                spec_field="cron_expr",
                spec_value=schedule,
                saved=saved,
            )
            # An id with no readable row behind it does not prove persistence,
            # which is the only thing this result can honestly assert.
            return ToolResult(ok=True, data=payload, unverified=not payload["stored"])

        # Try parsing as ISO datetime for one-shot.
        # Only catch ValueError/TypeError from the *parsing step* itself, NOT
        # from svc.add() — that would silently swallow meaningful errors such
        # as the per-user schedule limit and produce a misleading
        # "Could not parse schedule" failure.
        dt: datetime | None = None
        try:
            dt = datetime.fromisoformat(schedule)
        except (ValueError, TypeError):
            pass

        if dt is not None:
            # svc.add_async() may raise ValueError (e.g. schedule limit reached).
            # Let it propagate to the outer except so the real reason surfaces.
            # Async-native: this handler is awaited ON the cloud FastAPI serving
            # loop, so the sync svc.add()/svc.get() (ScheduleStore Postgres `_run`
            # bridge) would raise SyncBridgeLoopError. Mirror the cron branch's
            # add_async above (CL-20260711-afd7).
            one_shot_at = dt.isoformat() if dt.tzinfo is not None and dt.utcoffset() is not None else schedule
            schedule_id = await svc.add_async(
                resolved_user_id,
                label=label,
                action=action,
                one_shot_at=one_shot_at,
            )
            saved = await svc.get_async(resolved_user_id, schedule_id)
            scheduled_at = saved.one_shot_at if saved and saved.one_shot_at else one_shot_at
            payload = _created_schedule_payload(
                schedule_id=schedule_id,
                label=label,
                schedule_type="one_shot",
                spec_field="one_shot_at",
                spec_value=scheduled_at,
                saved=saved,
            )
            return ToolResult(ok=True, data=payload, unverified=not payload["stored"])

        return ToolResult(
            ok=False,
            data=None,
            error=(
                "Could not parse schedule %r. Provide a valid cron expression "
                "(e.g. '0 7 * * 1-5' for 7am weekdays) or an ISO datetime "
                "(e.g. '2026-03-01T09:00:00'). Cron validation error: %s." % (schedule, cron_detail)
            ),
        )
    except ValueError as exc:
        logger.warning("Schedule create validation error: %s", exc)
        return ToolResult(
            ok=False,
            data=None,
            error=str(exc),
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to create schedule: %s" % exc)


async def schedule_list_handler(enabled_only: bool = True, user_id: str = "") -> ToolResult:
    """List all scheduled actions.

    Args:
        enabled_only: If True, only show active schedules.

    Returns:
        ToolResult with formatted schedule list.
    """
    from services.scheduler.service import get_scheduler_service

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        svc = get_scheduler_service()
        schedules = await svc.list_schedules_async(resolved_user_id, enabled_only=enabled_only)

        if not schedules:
            return ToolResult(ok=True, data="No schedules found.")

        lines = ["%d schedule(s):" % len(schedules)]
        for s in schedules:
            status_str = ""
            if s.last_status:
                status_str = " (last: %s)" % s.last_status
            schedule_type = "cron %s" % s.cron_expr if s.cron_expr else "one-shot"

            # Format next_run_at for readability
            try:
                next_dt = datetime.fromisoformat(s.next_run_at)
                next_str = next_dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                next_str = s.next_run_at

            enabled_str = "" if s.enabled else " [DISABLED]"
            lines.append(
                "[id=%d] %s — %r (%s, next: %s, runs: %d)%s%s"
                % (
                    s.id,
                    s.label,
                    s.action,
                    schedule_type,
                    next_str,
                    s.run_count,
                    status_str,
                    enabled_str,
                )
            )
        return ToolResult(ok=True, data="\n".join(lines))
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to list schedules: %s" % exc)


async def schedule_delete_handler(schedule_id: str, user_id: str = "") -> ToolResult:
    """Delete a scheduled action by ID or label.

    Args:
        schedule_id: Numeric schedule ID or label string for fuzzy matching.

    Returns:
        ToolResult indicating success or failure.
    """
    from services.scheduler.service import get_scheduler_service

    schedule_id_str = schedule_id.strip()
    if not schedule_id_str:
        return ToolResult(ok=False, data=None, error="Schedule ID or label cannot be empty.")

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        svc = get_scheduler_service()

        # Try as numeric ID first. ONLY the int() parse belongs inside this
        # try: with the store calls inside it, a ValueError raised by
        # get_async/delete_async was caught by the `except ValueError: pass`
        # and the handler silently fell through to label matching, so a store
        # failure surfaced as "No schedule found matching '5'" -- a wrong
        # answer about the wrong thing.
        try:
            sid: int | None = int(schedule_id_str)
        except ValueError:
            sid = None

        if sid is not None:
            schedule = await svc.get_async(resolved_user_id, sid)
            if schedule is None:
                return ToolResult(ok=False, data=None, error="No schedule with id=%d." % sid)
            if not await svc.delete_async(resolved_user_id, sid):
                # delete_async returns rowcount > 0. Discarding it meant a
                # delete that removed nothing still reported "Deleted".
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Schedule id=%d was not deleted; the store removed no row." % sid,
                )
            return ToolResult(
                ok=True,
                data="Deleted schedule id=%d: %s" % (sid, schedule.label),
            )

        all_schedules = await svc.list_schedules_async(resolved_user_id, enabled_only=False)
        match = next((s for s in all_schedules if s.label.lower() == schedule_id_str.lower()), None)
        if match is None:
            # Try partial match
            matches = [s for s in all_schedules if schedule_id_str.lower() in s.label.lower()]
            if not matches:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="No schedule found matching %r." % schedule_id_str,
                )
            if len(matches) == 1:
                if not await svc.delete_async(resolved_user_id, matches[0].id):
                    return ToolResult(
                        ok=False,
                        data=None,
                        error="Schedule id=%d (%s) was not deleted; the store removed no row."
                        % (matches[0].id, matches[0].label),
                    )
                return ToolResult(
                    ok=True,
                    data="Deleted schedule id=%d: %s" % (matches[0].id, matches[0].label),
                )
            lines = ["Multiple schedules match %r:" % schedule_id_str]
            for s in matches:
                lines.append("  [id=%d] %s" % (s.id, s.label))
            lines.append("Please specify the exact ID to delete.")
            return ToolResult(ok=False, data=None, error="\n".join(lines))

        if not await svc.delete_async(resolved_user_id, match.id):
            return ToolResult(
                ok=False,
                data=None,
                error="Schedule id=%d (%s) was not deleted; the store removed no row." % (match.id, match.label),
            )
        return ToolResult(
            ok=True,
            data="Deleted schedule id=%d: %s" % (match.id, match.label),
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to delete schedule: %s" % exc)


async def schedule_update_handler(
    schedule_id: int,
    label: str = "",
    action: str = "",
    cron_expr: str = "",
    enabled: str = "",
    user_id: str = "",
) -> ToolResult:
    """Modify an existing schedule.

    Args:
        schedule_id: Numeric ID of the schedule to update.
        label: New label (empty = no change).
        action: New action command (empty = no change).
        cron_expr: New cron expression (empty = no change).
        enabled: "true" or "false" to toggle (empty = no change).

    Returns:
        ToolResult indicating success or failure.
    """
    from services.scheduler.service import get_scheduler_service

    resolved_user_id = user_id.strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for multi-user data isolation")
    try:
        svc = get_scheduler_service()

        schedule = await svc.get_async(resolved_user_id, schedule_id)
        if schedule is None:
            return ToolResult(
                ok=False,
                data=None,
                error="No schedule with id=%d." % schedule_id,
            )

        kwargs: dict[str, object] = {}
        if label.strip():
            kwargs["label"] = label.strip()
        if action.strip():
            kwargs["action"] = action.strip()
        if cron_expr.strip():
            kwargs["cron_expr"] = cron_expr.strip()
        if enabled.strip():
            val = enabled.strip().lower()
            if val in ("true", "1", "yes", "on"):
                kwargs["enabled"] = True
            elif val in ("false", "0", "no", "off"):
                kwargs["enabled"] = False
            else:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Invalid enabled value %r. Use 'true' or 'false'." % enabled,
                )

        if not kwargs:
            return ToolResult(ok=False, data=None, error="No fields to update.")

        updated = await svc.update_async(resolved_user_id, schedule_id, **kwargs)
        if updated:
            return ToolResult(
                ok=True,
                data="Updated schedule id=%d (%s): %s"
                % (
                    schedule_id,
                    schedule.label,
                    ", ".join("%s=%r" % (k, v) for k, v in kwargs.items()),
                ),
            )
        return ToolResult(ok=False, data=None, error="Update had no effect.")
    except ValueError as exc:
        logger.warning("Schedule update validation error: %s", exc)
        return ToolResult(
            ok=False,
            data=None,
            error=str(exc),
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Failed to update schedule: %s" % exc)
