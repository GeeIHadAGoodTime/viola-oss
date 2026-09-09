"""Agent tool handlers for timer management."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from intent.tool_types import ToolResult
from services.scheduler.actions import decode_tool_action, encode_tool_action


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve user_id from the explicit arg or the request ContextVar.

    F-050: timer tool handlers must NOT silently fall back to a desktop
    pseudo-user. If a tool is invoked without any user context the
    caller — agent loop, scheduler, instant command — is responsible for
    passing one. We raise so the bug surfaces at the call site instead of
    quietly mutating the wrong tenant's timer list.
    """
    if user_id:
        return user_id
    from core.user_context import get_current_user_id

    try:
        return get_current_user_id()
    except LookupError as exc:
        raise PermissionError("Timer tool requires an explicit user_id or an authenticated request context") from exc


async def list_timers_handler(user_id: str = "") -> ToolResult:
    """Return structured data for all active timers."""
    from services.timer_service import get_timer_service

    try:
        service = get_timer_service()
        timers = service.get_all_timers(user_id=_resolve_user_id(user_id))

        if not timers:
            return ToolResult(
                ok=True,
                data={
                    "message": "No active timers",
                    "count": 0,
                    "timers": [],
                },
            )

        timer_items = []
        for timer in timers:
            timer_items.append(
                {
                    "timer_id": timer.timer_id,
                    "label": timer.label,
                    "remaining_seconds": max(0, math.ceil(timer.remaining_seconds)),
                    "created_at": timer.created_at.isoformat(),
                }
            )

        return ToolResult(
            ok=True,
            data={
                "message": "Active timers",
                "count": len(timer_items),
                "timers": timer_items,
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="list_timers failed: %s" % exc)


async def set_timer_handler(minutes: float, label: str = "", user_id: str = "") -> ToolResult:
    """Create a new timer. Returns the timer_id and confirmation."""
    from services.timer_service import get_timer_service

    try:
        if minutes <= 0 or minutes > 1440:
            return ToolResult(
                ok=False,
                data=None,
                error="Duration must be between 0 and 1440 minutes (24 hours)",
            )

        service = get_timer_service()
        duration_seconds = int(minutes * 60)
        timer_id = service.add_timer(duration_seconds, label=label or "", user_id=_resolve_user_id(user_id))

        # Format for user
        if minutes >= 60:
            hours = int(minutes // 60)
            mins = int(minutes % 60)
            time_str = f"{hours} hour{'s' if hours > 1 else ''}"
            if mins:
                time_str += f" {mins} minute{'s' if mins > 1 else ''}"
        elif minutes == int(minutes):
            time_str = f"{int(minutes)} minute{'s' if minutes != 1 else ''}"
        else:
            time_str = f"{minutes} minutes"

        msg = f"Timer set for {time_str}"
        if label:
            msg = f"Timer '{label}' set for {time_str}"

        return ToolResult(
            ok=True,
            data={
                "timer_id": timer_id,
                "duration_seconds": duration_seconds,
                "label": label,
                "message": msg,
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="set_timer failed: %s" % exc)


async def cancel_timer_handler(timer_id: str = "", user_id: str = "") -> ToolResult:
    """Cancel a timer by ID, or the most recently created timer if no ID is given."""
    from services.timer_service import get_timer_service

    try:
        service = get_timer_service()

        uid = _resolve_user_id(user_id)

        if timer_id:
            cancelled = service.cancel_timer(timer_id, user_id=uid)
            if cancelled:
                return ToolResult(
                    ok=True,
                    data={"message": f"Timer {timer_id} cancelled", "cancelled_count": 1},
                )
            # List active timers so the model can disambiguate
            active = service.get_all_timers(user_id=uid)
            active_desc = ", ".join("%s (%s)" % (t.label or t.timer_id, t.timer_id) for t in active) or "none"
            return ToolResult(
                ok=False,
                data={"active_timers": active_desc},
                error="Timer '%s' not found. Active timers: %s" % (timer_id, active_desc),
            )

        active = service.get_all_timers(user_id=uid)
        if not active:
            return ToolResult(
                ok=True,
                data={"message": "No active timers to cancel", "cancelled_count": 0},
            )

        most_recent = max(active, key=lambda timer: getattr(timer, "created_at", datetime.min))
        if hasattr(service, "cancel_most_recent"):
            try:
                cancelled = service.cancel_most_recent(user_id=uid)
            except TypeError:
                # A service whose cancel_most_recent takes no user_id resolves
                # the owner from ambient state, so it can cancel a different
                # tenant's timer than the one this result then names as
                # cancelled. Cancel the timer we actually identified instead:
                # same outcome for a single-tenant service, and the reported
                # timer_id is the one that was really cancelled.
                cancelled = service.cancel_timer(most_recent.timer_id, user_id=uid)
        else:
            cancelled = service.cancel_timer(most_recent.timer_id, user_id=uid)

        if cancelled:
            label = most_recent.label or most_recent.timer_id
            return ToolResult(
                ok=True,
                data={
                    "message": "Timer %s cancelled" % label,
                    "timer_id": most_recent.timer_id,
                    "cancelled_count": 1,
                },
            )
        return ToolResult(ok=False, data=None, error="Most recent timer could not be cancelled")
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="cancel_timer failed: %s" % exc)


async def cancel_all_timers_handler(user_id: str = "") -> ToolResult:
    """Cancel all active timers explicitly."""
    from services.timer_service import get_timer_service

    try:
        service = get_timer_service()
        uid = _resolve_user_id(user_id)
        count = service.cancel_all(user_id=uid)
        # cancel_all reports how many timers it *tried* to cancel: it snapshots
        # the ids and then cancels each, discarding the per-timer result
        # (services/timer_core.py). Read back what is still armed so a timer
        # that survived the sweep is named instead of being counted as
        # cancelled and left running under a success message.
        still_active = [timer.timer_id for timer in service.get_all_timers(user_id=uid)]
        cancelled = max(0, count - len(still_active))
        data: dict[str, object] = {
            "message": "Cancelled %d timer%s" % (cancelled, "" if cancelled == 1 else "s"),
            "cancelled_count": cancelled,
        }
        if still_active:
            data["still_active_timer_ids"] = still_active
            return ToolResult(
                ok=False,
                data=data,
                error="%d timer(s) are still running after cancel_all: %s"
                % (len(still_active), ", ".join(still_active)),
            )
        return ToolResult(ok=True, data=data)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="cancel_all_timers failed: %s" % exc)


def _is_sleep_timer_schedule(schedule: object) -> bool:
    decoded = decode_tool_action(getattr(schedule, "action", ""))
    if not decoded:
        return False
    args = decoded.get("args")
    return (
        decoded.get("tool") == "playback"
        and isinstance(args, dict)
        and args.get("action") == "stop"
        and args.get("reason") == "sleep_timer"
    )


async def set_sleep_timer_handler(minutes: float, label: str = "", user_id: str = "") -> ToolResult:
    """Schedule playback to stop after a duration."""
    try:
        if minutes <= 0 or minutes > 1440:
            return ToolResult(
                ok=False,
                data=None,
                error="Duration must be between 0 and 1440 minutes (24 hours)",
            )

        from services.scheduler.service import get_scheduler_service

        uid = _resolve_user_id(user_id)
        duration_seconds = int(minutes * 60)
        fire_at = datetime.now(UTC) + timedelta(seconds=duration_seconds)
        sleep_label = " ".join((label or "Sleep timer").strip().split()) or "Sleep timer"
        scheduler = get_scheduler_service()
        schedule_id = scheduler.add(
            uid,
            label=sleep_label,
            action=encode_tool_action(
                "playback",
                {
                    "action": "stop",
                    "reason": "sleep_timer",
                },
            ),
            one_shot_at=fire_at.isoformat(),
        )
        return ToolResult(
            ok=True,
            data={
                "sleep_timer_scheduled": True,
                "schedule_id": schedule_id,
                "duration_seconds": duration_seconds,
                "label": sleep_label,
                "when": fire_at.isoformat(),
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="set_sleep_timer failed: %s" % exc)


def _describe_sleep_timer_ids(ids: list[int]) -> str:
    return ", ".join(str(value) for value in ids) or "none"


async def cancel_sleep_timers_handler(schedule_id: str = "", user_id: str = "") -> ToolResult:
    """Cancel pending sleep timers."""
    try:
        from services.scheduler.service import get_scheduler_service

        uid = _resolve_user_id(user_id)
        scheduler = get_scheduler_service()
        sleep_timers = [
            schedule
            for schedule in scheduler.list_schedules(uid, enabled_only=True)
            if _is_sleep_timer_schedule(schedule)
        ]
        pending_ids = [int(schedule.id) for schedule in sleep_timers]

        requested = str(schedule_id).strip()
        if requested:
            # A caller that named a specific timer is asking about that timer.
            # Filtering it down to nothing and then returning success with
            # cancelled_count 0 told the user the sleep timer they named had
            # been cancelled when no such timer existed -- and an unparseable
            # id mapped to the sentinel -1, which matches nothing, so a typo
            # produced the same false confirmation.
            try:
                wanted_id = int(requested)
            except ValueError:
                return ToolResult(
                    ok=False,
                    data={
                        "cancelled_count": 0,
                        "cancelled_sleep_timer_ids": [],
                        "pending_sleep_timer_ids": pending_ids,
                    },
                    error="%r is not a sleep timer id. Pending sleep timers: %s"
                    % (requested, _describe_sleep_timer_ids(pending_ids)),
                )
            sleep_timers = [schedule for schedule in sleep_timers if int(schedule.id) == wanted_id]
            if not sleep_timers:
                return ToolResult(
                    ok=False,
                    data={
                        "cancelled_count": 0,
                        "cancelled_sleep_timer_ids": [],
                        "pending_sleep_timer_ids": pending_ids,
                    },
                    error="No pending sleep timer with id=%d, so nothing was cancelled. Pending sleep timers: %s"
                    % (wanted_id, _describe_sleep_timer_ids(pending_ids)),
                )

        cancelled: list[int] = []
        not_cancelled: list[int] = []
        for schedule in sleep_timers:
            current_id = int(schedule.id)
            if scheduler.disable(uid, current_id):
                cancelled.append(current_id)
            else:
                not_cancelled.append(current_id)

        data: dict[str, object] = {
            "cancelled_count": len(cancelled),
            "cancelled_sleep_timer_ids": cancelled,
        }
        if not_cancelled:
            # The scheduler refused to disable these, so they are still armed
            # and playback will still stop. Counting only the successes and
            # reporting success hid the ones that survived.
            data["not_cancelled_sleep_timer_ids"] = not_cancelled
            return ToolResult(
                ok=False,
                data=data,
                error="Cancelled sleep timer(s) %s, but %s could not be cancelled and are still armed."
                % (
                    _describe_sleep_timer_ids(cancelled),
                    _describe_sleep_timer_ids(not_cancelled),
                ),
            )
        if not cancelled:
            data["message"] = "No pending sleep timers to cancel"
        return ToolResult(ok=True, data=data)
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="cancel_sleep_timer failed: %s" % exc)
