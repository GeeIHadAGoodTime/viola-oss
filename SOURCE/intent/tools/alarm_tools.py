"""Agent tool handlers for alarms."""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from intent.tool_types import ToolResult
from intent.tools._schedule_time import parse_schedule_time
from services.scheduler.actions import decode_tool_action, encode_tool_action


def _resolve_user_id(user_id: str | None = None) -> str:
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError as exc:
        raise PermissionError("Alarm tool requires an explicit user_id or an authenticated request context") from exc


def _parse_alarm_time(when: str, *, now: datetime | None = None) -> datetime | None:
    """Parse common alarm times into an aware UTC datetime.

    Delegates to the shared ``alarm``/``notify`` parser (#2774) so both tools
    understand the same absolute-clock and relative-duration phrasing.
    """
    return parse_schedule_time(when, now=now)


def _is_alarm_schedule(schedule: Any) -> bool:
    decoded = decode_tool_action(getattr(schedule, "action", ""))
    if decoded and decoded.get("tool") == "alarm":
        return True
    label = str(getattr(schedule, "label", "") or "").lower()
    action = str(getattr(schedule, "action", "") or "").lower()
    return "alarm" in label or action.strip() == "play alarm sound"


def _matches_alarm(schedule: Any, *, label: str = "", alarm_id: str = "") -> bool:
    if alarm_id:
        try:
            if int(alarm_id) == int(schedule.id):
                return True
        except (TypeError, ValueError):
            return str(getattr(schedule, "id", "")) == alarm_id
    wanted = " ".join((label or "").strip().lower().split())
    if not wanted:
        return True
    schedule_label = str(getattr(schedule, "label", "") or "").lower()
    decoded = decode_tool_action(getattr(schedule, "action", ""))
    decoded_label = ""
    if decoded:
        args = decoded.get("args")
        if isinstance(args, dict):
            decoded_label = str(args.get("label") or "").lower()
    return wanted in schedule_label or wanted in decoded_label


async def set_alarm_handler(when: str, label: str = "Alarm", user_id: str = "") -> ToolResult:
    """Schedule an alarm sound for a specific time."""
    uid = _resolve_user_id(user_id)
    alarm_at = _parse_alarm_time(when)
    if alarm_at is None:
        return ToolResult(ok=False, data=None, error="Alarm time could not be parsed.")

    alarm_label = " ".join((label or "Alarm").strip().split()) or "Alarm"
    try:
        from services.scheduler.service import get_scheduler_service

        scheduler = get_scheduler_service()
        action = encode_tool_action("alarm", {"action": "sound", "label": alarm_label})
        schedule_id = scheduler.add(
            uid,
            label=alarm_label,
            action=action,
            one_shot_at=alarm_at.isoformat(),
        )
        return ToolResult(
            ok=True,
            data={
                "alarm_scheduled": True,
                "schedule_id": schedule_id,
                "label": alarm_label,
                "when": alarm_at.isoformat(),
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="set_alarm failed: %s" % exc)


async def cancel_alarm_handler(
    label: str = "",
    alarm_id: str = "",
    cancel_all: bool = False,
    user_id: str = "",
) -> ToolResult:
    """Cancel pending alarms by id, label, or all when requested."""
    uid = _resolve_user_id(user_id)
    try:
        from services.scheduler.service import get_scheduler_service

        scheduler = get_scheduler_service()
        alarms = [
            schedule for schedule in scheduler.list_schedules(uid, enabled_only=True) if _is_alarm_schedule(schedule)
        ]
        if not alarms:
            return ToolResult(ok=True, data={"cancelled_count": 0, "alarms": []})

        if cancel_all:
            matches = alarms
        elif label or alarm_id:
            matches = [schedule for schedule in alarms if _matches_alarm(schedule, label=label, alarm_id=alarm_id)]
        elif len(alarms) == 1:
            # No label/id given, but there's only one alarm pending -- that is
            # unambiguous, so cancel it (matches cancel_timer's single-target
            # behavior).
            matches = alarms
        else:
            # No label/id given and MULTIPLE alarms are pending: guessing which
            # one the caller means (e.g. "most recent") risks silently cancelling
            # the wrong alarm, which is worse than a timer miss -- the user could
            # oversleep. Resolve or ask instead of guessing (#2774).
            pending = [
                {
                    "schedule_id": int(schedule.id),
                    "label": str(getattr(schedule, "label", "") or ""),
                    "next_run_at": getattr(schedule, "next_run_at", None),
                }
                for schedule in alarms
            ]
            described = ", ".join(
                "%s (id=%s)" % (item["label"] or item["schedule_id"], item["schedule_id"]) for item in pending
            )
            return ToolResult(
                ok=False,
                data={"cancelled_count": 0, "alarms": pending},
                error=(
                    "Multiple alarms are pending (%s). Specify which alarm by label or id, "
                    "or pass cancel_all=True to cancel all of them." % described
                ),
            )

        cancelled: list[int] = []
        for schedule in matches:
            schedule_id = int(schedule.id)
            if scheduler.disable(uid, schedule_id):
                cancelled.append(schedule_id)

        return ToolResult(
            ok=True,
            data={
                "cancelled_count": len(cancelled),
                "cancelled_alarm_ids": cancelled,
                "matched_count": len(matches),
            },
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="cancel_alarm failed: %s" % exc)


async def list_alarms_handler(user_id: str = "") -> ToolResult:
    """List pending alarms for the user.

    Reads through ``list_schedules_async``, NOT the sync ``list_schedules``. On the
    cloud surface the store is ``PostgresScheduleBackend``, whose sync methods bridge
    to async via ``core.asyncio_safe.run_async_synchronously``; that bridge raises
    ``SyncBridgeLoopError("Cannot synchronously wait on the shared asyncio worker loop
    from itself")`` when the caller is already on the loop it would dispatch to --
    which is exactly where this coroutine runs, because cloud lifespan registers the
    serving loop as the main loop (`core/asyncio_safe.py:225-227`). The sqlite backend
    has no such bridge, so every sqlite-backed test passed while the cloud path raised.
    Reproduced directly against the real class on 2026-08-02: constructing
    ``PostgresScheduleBackend`` and calling ``.list_schedules(...)`` from inside a
    running loop registered via ``set_main_loop`` raises before it ever touches a pool.
    """
    uid = _resolve_user_id(user_id)
    try:
        from services.scheduler.service import get_scheduler_service

        scheduler = get_scheduler_service()
        schedules = await scheduler.list_schedules_async(uid, enabled_only=True)
        alarms = [
            {
                "schedule_id": schedule.id,
                "label": schedule.label,
                "next_run_at": schedule.next_run_at,
            }
            for schedule in schedules
            if _is_alarm_schedule(schedule)
        ]
        return ToolResult(ok=True, data={"count": len(alarms), "alarms": alarms})
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="list_alarms failed: %s" % exc)


async def play_alarm_sound_handler(
    label: str = "",
    tts: Any | None = None,
    user_id: str = "",
) -> ToolResult:
    """Fire the alarm notification through TTS and local messaging."""
    uid = _resolve_user_id(user_id)
    alarm_label = " ".join((label or "Alarm").strip().split()) or "Alarm"
    notification = "%s alarm" % alarm_label
    spoken = False

    if tts is not None:
        try:
            from core.user_context import user_scope

            speak = getattr(tts, "speak", None) or getattr(tts, "say", None)
            if callable(speak):
                with user_scope(uid):
                    result = speak(notification)
                    if inspect.isawaitable(result):
                        await result
                    spoken = True
        except Exception:
            spoken = False

    delivered = False
    try:
        from intent.tools.notification_tools import deliver_notification_handler

        # user_requested: an alarm is the single most user-requested notification
        # there is, and the whole point of one is to fire at the moment the user
        # named -- including inside quiet hours, which is when most alarms are
        # set for (#4790). Without this, quiet hours parked the visual delivery,
        # so `alarm_fired` below depended entirely on TTS: an alarm set for 6am
        # with TTS muted or its engine unavailable woke nobody and could only
        # report the failure after the fact.
        delivery = await deliver_notification_handler(
            notification,
            user_id=uid,
            metadata={"tool": "alarm", "alarm_label": alarm_label},
            user_requested=True,
        )
        delivered = bool(
            delivery.ok and isinstance(delivery.data, dict) and delivery.data.get("notification_sent") is True
        )
    except Exception:
        delivered = False

    # `alarm_fired` must mean the user was actually alerted right now -- either
    # spoken via TTS or genuinely delivered (not merely queued for later, see
    # notify's queued-vs-sent distinction). If TTS raised/never ran AND
    # delivery failed or was only queued, the user got nothing and the tool
    # must say so instead of unconditionally reporting success (#2774).
    alarm_fired = spoken or delivered

    return ToolResult(
        ok=alarm_fired,
        data={
            "alarm_fired": alarm_fired,
            "label": alarm_label,
            "spoken": spoken,
            "delivered": delivered,
            "broadcast": False,
        },
        error=None if alarm_fired else "Alarm could not be spoken or delivered through any channel.",
    )


__all__ = [
    "cancel_alarm_handler",
    "list_alarms_handler",
    "play_alarm_sound_handler",
    "set_alarm_handler",
]
