"""Daily briefing aggregator tool for the agent executor.

The daily briefing is a single, spoken summary of the user's day assembled from
three real sources: current weather, today's calendar events, and the user's
active scheduled tasks/reminders. It is the aggregator behind requests like
"give me my daily briefing" or "what's my morning briefing".

Design notes:
- This is a *read-only aggregator* the model chooses to call. It is not a
  runtime classifier and it does not steer the model: it fetches real data and
  returns it, exactly as ``weather_handler`` returns a composed ``message``.
- Each source reports an honest per-section status -- ``ok`` (has content),
  ``empty`` (source reachable but nothing for today), or ``unavailable``
  (source errored or not connected). The composed message never fabricates a
  section and never claims a briefing was delivered when every source failed.
- Scheduled delivery needs no extra code: the existing ``schedule`` tool runs
  the "daily briefing" command text through the agent, which invokes this tool.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.logging_config import get_logger
from intent.tool_types import ToolResult
from services.calendar.datetime_utils import CalendarDateTimeUtils

logger = get_logger(__name__)

_DATETIME_UTILS = CalendarDateTimeUtils()

# Per-section status vocabulary. Kept tiny and explicit so the model (and the
# tests) can reason about partial results without parsing prose.
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"


def _display_timezone() -> datetime.tzinfo:
    """The zone that decides what "today" and "morning" mean for this user.

    #3557: this used to be ``settings.calendar_timezone``, a per-process value.
    In a cloud container that is UTC, so the briefing greeted a user in
    America/Chicago with "Good evening" at lunchtime and summarised the wrong
    calendar day either side of midnight.
    """
    try:
        return _DATETIME_UTILS.get_user_display_timezone()
    except (AttributeError, ValueError, KeyError):
        return ZoneInfo("UTC")


def _local_now(now: datetime.datetime | None, tz: datetime.tzinfo) -> datetime.datetime:
    if now is not None:
        if now.tzinfo is None:
            return now.replace(tzinfo=tz)
        return now.astimezone(tz)
    return datetime.datetime.now(tz)


def _greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 21:
        return "Good evening"
    return "Hello"


def _fmt_time(dt: datetime.datetime) -> str:
    """12-hour clock, no leading zero, TTS-friendly (e.g. '9 AM', '12:30 PM')."""
    hour = dt.hour % 12 or 12
    minute = dt.minute
    suffix = "AM" if dt.hour < 12 else "PM"
    if minute:
        return "%d:%02d %s" % (hour, minute, suffix)
    return "%d %s" % (hour, suffix)


def _parse_iso_utc(value: Any) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# Per-source collectors. Each returns a section dict:
#   {"status": <STATUS_*>, "summary": <str>, ...source-specific fields}
# and NEVER raises -- a source failure becomes STATUS_UNAVAILABLE.
# ---------------------------------------------------------------------------


async def _collect_weather(city: str) -> dict[str, Any]:
    try:
        from intent.tools.weather_tool import weather_handler

        result = await weather_handler(city)
    except Exception:
        logger.exception("daily_briefing: weather collection failed")
        return {"status": STATUS_UNAVAILABLE, "summary": "Weather is unavailable right now."}

    if not result.ok or not isinstance(result.data, dict):
        return {"status": STATUS_UNAVAILABLE, "summary": "Weather is unavailable right now."}

    data = result.data
    message = str(data.get("message") or "").strip()
    return {
        "status": STATUS_OK,
        "summary": message or "Weather is available.",
        "location": data.get("location"),
        "temperature_f": data.get("temperature_f"),
        "condition": data.get("condition"),
        "forecast_high_f": data.get("forecast_high_f"),
        "forecast_low_f": data.get("forecast_low_f"),
    }


async def _collect_calendar(
    user_id: str,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
) -> dict[str, Any]:
    try:
        from intent.tools.calendar_tools import calendar_list_events_handler

        result = await calendar_list_events_handler(
            user_id=user_id,
            start_date=day_start.isoformat(),
            end_date=day_end.isoformat(),
            max_results=25,
            provider="all",
        )
    except Exception:
        logger.exception("daily_briefing: calendar collection failed")
        return {"status": STATUS_UNAVAILABLE, "summary": "Your calendar is unavailable right now.", "events": []}

    if not result.ok or not isinstance(result.data, dict):
        return {"status": STATUS_UNAVAILABLE, "summary": "Your calendar is unavailable right now.", "events": []}

    payload = result.data
    if not payload.get("calendars_connected"):
        return {
            "status": STATUS_UNAVAILABLE,
            "summary": "No calendar is connected.",
            "events": [],
            "calendars_connected": False,
        }

    tz = day_start.tzinfo or _display_timezone()
    raw_events = [item for item in payload.get("events", []) if isinstance(item, dict)]
    events: list[dict[str, Any]] = []
    for item in raw_events:
        title = str(item.get("title") or item.get("summary") or "Untitled event").strip() or "Untitled event"
        all_day = bool(item.get("all_day"))
        start_dt = _parse_iso_utc(item.get("start_time") or item.get("start"))
        when = "all day" if all_day else (_fmt_time(start_dt.astimezone(tz)) if start_dt else "")
        entry: dict[str, Any] = {"title": title, "all_day": all_day, "when": when}
        location = str(item.get("location") or "").strip()
        if location:
            entry["location"] = location
        if start_dt is not None:
            entry["start"] = start_dt.isoformat()
        events.append(entry)

    events.sort(key=lambda e: (not e.get("all_day"), str(e.get("start") or "")))

    if not events:
        return {"status": STATUS_EMPTY, "summary": "Nothing on your calendar today.", "events": [], "count": 0}

    count = len(events)
    lead = "You have %d event%s today: " % (count, "" if count == 1 else "s")
    parts: list[str] = []
    for entry in events:
        seg = entry["title"]
        when = entry.get("when")
        if when == "all day":
            seg += " (all day)"
        elif when:
            seg += " at %s" % when
        parts.append(seg)
    summary = lead + _join_natural(parts) + "."
    return {"status": STATUS_OK, "summary": summary, "events": events, "count": count}


async def _collect_tasks(
    user_id: str,
    day_start: datetime.datetime,
    day_end: datetime.datetime,
) -> dict[str, Any]:
    try:
        from services.scheduler.service import get_scheduler_service

        svc = get_scheduler_service()
        schedules = await svc.list_schedules_async(user_id, enabled_only=True)
    except Exception:
        logger.exception("daily_briefing: task/schedule collection failed")
        return {"status": STATUS_UNAVAILABLE, "summary": "Your tasks are unavailable right now.", "tasks": []}

    tz = day_start.tzinfo or _display_timezone()
    start_utc = day_start.astimezone(datetime.UTC)
    end_utc = day_end.astimezone(datetime.UTC)

    active_count = 0
    today_tasks: list[dict[str, Any]] = []
    for sched in schedules or []:
        active_count += 1
        next_dt = _parse_iso_utc(getattr(sched, "next_run_at", None))
        if next_dt is None or not (start_utc <= next_dt <= end_utc):
            continue
        label = str(getattr(sched, "label", "") or getattr(sched, "action", "") or "Reminder").strip() or "Reminder"
        today_tasks.append(
            {
                "label": label,
                "when": _fmt_time(next_dt.astimezone(tz)),
                "next_run": next_dt.isoformat(),
                "recurring": bool(getattr(sched, "cron_expr", None)),
            }
        )

    today_tasks.sort(key=lambda t: str(t.get("next_run") or ""))

    if today_tasks:
        count = len(today_tasks)
        lead = "You have %d task%s due today: " % (count, "" if count == 1 else "s")
        parts = ["%s at %s" % (t["label"], t["when"]) for t in today_tasks]
        summary = lead + _join_natural(parts) + "."
        return {
            "status": STATUS_OK,
            "summary": summary,
            "tasks": today_tasks,
            "count": count,
            "active_total": active_count,
        }

    if active_count:
        return {
            "status": STATUS_EMPTY,
            "summary": "No tasks are due today.",
            "tasks": [],
            "count": 0,
            "active_total": active_count,
        }
    return {
        "status": STATUS_EMPTY,
        "summary": "You have no scheduled tasks.",
        "tasks": [],
        "count": 0,
        "active_total": 0,
    }


def _join_natural(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return "%s and %s" % (parts[0], parts[1])
    return "%s, and %s" % (", ".join(parts[:-1]), parts[-1])


def _compose_message(
    greeting: str,
    date_label: str,
    weather: dict[str, Any],
    calendar: dict[str, Any],
    tasks: dict[str, Any],
) -> str:
    lines = ["%s. Here's your briefing for %s." % (greeting, date_label)]

    # Weather -- lead with it when we have it; note the gap when we don't.
    if weather["status"] == STATUS_OK:
        lines.append(weather["summary"])
    else:
        lines.append("I couldn't get the weather right now.")

    # Calendar
    if calendar["status"] == STATUS_OK:
        lines.append(calendar["summary"])
    elif calendar["status"] == STATUS_EMPTY:
        lines.append("Nothing on your calendar today.")
    else:
        lines.append(calendar.get("summary") or "Your calendar is unavailable right now.")

    # Tasks
    if tasks["status"] == STATUS_OK:
        lines.append(tasks["summary"])
    elif tasks["status"] == STATUS_EMPTY:
        lines.append(tasks.get("summary") or "No tasks due today.")
    else:
        lines.append("Your tasks are unavailable right now.")

    return " ".join(line for line in lines if line)


async def daily_briefing_handler(
    user_id: str,
    city: str = "",
    *,
    now: datetime.datetime | None = None,
) -> ToolResult:
    """Aggregate weather, today's calendar, and today's tasks into one briefing.

    Args:
        user_id: Authenticated owner (required for calendar + task isolation).
        city: Optional city override for weather; empty uses the saved location.
        now: Injectable current time (tests only); production leaves it None.

    Returns:
        A ToolResult whose ``data`` carries a spoken ``message`` plus a
        ``sections`` map with an honest per-source status. ``ok`` is False only
        when every source is unavailable.
    """
    resolved_user_id = (user_id or "").strip()
    if not resolved_user_id:
        raise ValueError("user_id is required for the daily briefing (multi-user isolation)")

    tz = _display_timezone()
    local_now = _local_now(now, tz)
    day_start = local_now
    day_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    # Build "Friday, July 17" portably (%-d / %#d differ by platform).
    date_label = "%s, %s %d" % (local_now.strftime("%A"), local_now.strftime("%B"), local_now.day)

    weather, calendar, tasks = await asyncio.gather(
        _collect_weather(city),
        _collect_calendar(resolved_user_id, day_start, day_end),
        _collect_tasks(resolved_user_id, day_start, day_end),
    )

    sections = {"weather": weather, "calendar": calendar, "tasks": tasks}
    available = [name for name, sec in sections.items() if sec["status"] != STATUS_UNAVAILABLE]
    unavailable = [name for name, sec in sections.items() if sec["status"] == STATUS_UNAVAILABLE]

    greeting = _greeting(local_now.hour)

    # Every source failed -> honest failure, not a hollow "success".
    if not available:
        return ToolResult(
            ok=False,
            data={
                "message": "I couldn't put together your briefing right now. Weather, calendar, and tasks are all unavailable.",
                "date": local_now.date().isoformat(),
                "sections": sections,
                "available_sections": [],
                "unavailable_sections": unavailable,
            },
            error="daily_briefing: all sources unavailable (weather, calendar, tasks)",
        )

    message = _compose_message(greeting, date_label, weather, calendar, tasks)
    return ToolResult(
        ok=True,
        data={
            "message": message,
            "date": local_now.date().isoformat(),
            "greeting": greeting,
            "sections": sections,
            "available_sections": available,
            "unavailable_sections": unavailable,
        },
    )
