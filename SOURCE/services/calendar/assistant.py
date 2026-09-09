"""Shared calendar assistant flows for plugin and instant-command surfaces."""

from __future__ import annotations

import datetime
import re
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


def _calendar_connection_fields(result: dict[str, Any]) -> dict[str, object]:
    providers = result.get("connected_providers")
    connected_providers = [str(provider) for provider in providers] if isinstance(providers, list) else []
    if not connected_providers:
        calendars = result.get("calendars")
        if isinstance(calendars, list):
            connected_providers = sorted(
                {
                    str(calendar.get("provider"))
                    for calendar in calendars
                    if isinstance(calendar, dict) and calendar.get("provider")
                }
            )
    if "calendars_connected" in result:
        calendars_connected = bool(result.get("calendars_connected"))
    else:
        calendars_connected = bool(connected_providers or result.get("calendars") or result.get("events"))
    return {
        "calendars_connected": calendars_connected,
        "connected_providers": connected_providers,
    }


def _current_user_id(explicit_user_id: str | None = None) -> str:
    if explicit_user_id:
        return explicit_user_id
    from core.user_context import get_current_user_id

    return get_current_user_id()


async def get_events_today_response(*, user_id: str | None = None) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    now = datetime.datetime.now()
    end_of_day = now.replace(hour=23, minute=59, second=59)
    return await _events_summary_response(
        user_id=uid,
        start_date=now,
        end_date=end_of_day,
        max_results=10,
        empty_speech="You have no events on your calendar today.",
        lead_template="You have %d event%s today.",
        display_key="date",
        display_value=now.strftime("%B %d"),
    )


async def get_events_tomorrow_response(*, user_id: str | None = None) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    now = datetime.datetime.now()
    tomorrow = now.date() + datetime.timedelta(days=1)
    start = datetime.datetime.combine(tomorrow, datetime.time(0, 0))
    end = datetime.datetime.combine(tomorrow, datetime.time(23, 59, 59))
    return await _events_summary_response(
        user_id=uid,
        start_date=start,
        end_date=end,
        max_results=10,
        empty_speech="You have no events on your calendar tomorrow.",
        lead_template="You have %d event%s tomorrow.",
        display_key="date",
        display_value=start.strftime("%B %d"),
    )


async def get_events_week_response(*, user_id: str | None = None) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    now = datetime.datetime.now()
    days_until_sunday = 6 - now.weekday()
    end_date = now.date() + datetime.timedelta(days=days_until_sunday)
    end = datetime.datetime.combine(end_date, datetime.time(23, 59, 59))
    return await _events_summary_response(
        user_id=uid,
        start_date=now,
        end_date=end,
        max_results=20,
        empty_speech="You have no events on your calendar this week.",
        lead_template="You have %d event%s this week.",
        display_key="week_end",
        display_value=end_date.strftime("%B %d"),
    )


async def get_next_event_response(*, user_id: str | None = None) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    now = datetime.datetime.now()
    end = now + datetime.timedelta(days=7)
    manager = _get_manager()
    result = await manager.get_events(
        start_date=now,
        end_date=end,
        max_results=1,
        calendar="all",
        user_id=uid,
    )
    connection_fields = _calendar_connection_fields(result)

    if not result.get("ok"):
        return {
            "speech": "I couldn't check your calendar right now.",
            "display": {"events": [], **connection_fields},
            "error": str(result.get("error", "unknown")),
        }

    events = result.get("events", [])
    if not events:
        return {
            "speech": "You have no upcoming events in the next week.",
            "display": {"events": [], "next_event": None, **connection_fields},
            "error": None,
        }

    event = events[0]
    title = str(event.get("title") or "Untitled")
    time_str = str(event.get("time") or "")
    start_time = event.get("start_time")

    if isinstance(start_time, datetime.datetime):
        day = start_time.strftime("%A")
        if start_time.date() == now.date():
            day = "today"
        elif start_time.date() == (now + datetime.timedelta(days=1)).date():
            day = "tomorrow"
        if time_str and time_str != "All day":
            speech = "Your next event is %s %s at %s." % (title, day, time_str)
        else:
            speech = "Your next event is %s %s." % (title, day)
    elif time_str:
        speech = "Your next event is %s at %s." % (title, time_str)
    else:
        speech = "Your next event is %s." % title

    return {
        "speech": speech,
        "display": {"next_event": event, **connection_fields},
        "error": None,
    }


async def add_event_response(
    *,
    title: str,
    time_str: str,
    user_id: str | None = None,
) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    if not title:
        return {"speech": "I need a title for the event.", "display": {}, "error": "missing_title"}
    if not time_str:
        return {"speech": "I need a time for the event.", "display": {}, "error": "missing_time"}

    manager = _get_manager()
    start_time = parse_natural_time(time_str)
    if start_time is None:
        return {
            "speech": "I couldn't understand the time '%s'. Try something like '3pm' or 'tomorrow at noon'." % time_str,
            "display": {},
            "error": "invalid_time",
        }

    result = await manager.add_event(
        title=title,
        start_time=start_time,
        calendar="auto",
        user_id=uid,
    )

    if result.get("ok"):
        formatted = start_time.strftime("%B %d at %I:%M %p")
        if result.get("fallback"):
            speech = "I've saved '%s' for %s and will retry remote calendar sync when available." % (title, formatted)
        else:
            speech = "Added '%s' to your calendar on %s." % (title, formatted)
        return {
            "speech": speech,
            "display": {
                "event_id": result.get("event_id"),
                "title": title,
                "time": formatted,
                "fallback": result.get("fallback", False),
                "provider": result.get("provider"),
            },
            "error": None,
        }

    return {
        "speech": str(result.get("message", "I couldn't add that event to your calendar.")),
        "display": {},
        "error": str(result.get("error", "unknown")),
    }


async def delete_event_response(
    *,
    title: str = "",
    event_id: str = "",
    user_id: str | None = None,
) -> dict[str, Any]:
    uid = _current_user_id(user_id)
    title = title.strip()
    event_id = event_id.strip()

    if not title and not event_id:
        return {"speech": "Which event should I delete?", "display": {}, "error": "missing_event"}

    manager = _get_manager()

    if event_id:
        result = await manager.delete_event(event_id, calendar="auto", user_id=uid)
        if result.get("ok"):
            return {
                "speech": "Deleted that event from your calendar.",
                "display": {"event_id": event_id},
                "error": None,
            }
        return {
            "speech": str(result.get("message", "I couldn't delete that event from your calendar.")),
            "display": {},
            "error": str(result.get("error", "unknown")),
        }

    now = datetime.datetime.now()
    end = now + datetime.timedelta(days=7)
    events_result = await manager.get_events(
        start_date=now,
        end_date=end,
        max_results=25,
        calendar="all",
        user_id=uid,
    )
    if not events_result.get("ok"):
        return {
            "speech": str(events_result.get("message", "I couldn't check your calendar right now.")),
            "display": {},
            "error": str(events_result.get("error", "unknown")),
        }

    events = events_result.get("events", [])
    title_key = title.casefold()
    next_aliases = {
        "next meeting",
        "next event",
        "next appointment",
        "my next meeting",
        "my next event",
        "my next appointment",
    }
    if title_key in next_aliases:
        matches = events[:1]
    else:
        matches = [event for event in events if title_key and title_key in str(event.get("title", "")).casefold()]

    if not matches:
        return {
            "speech": "I couldn't find an upcoming event called '%s'." % title,
            "display": {},
            "error": "event_not_found",
        }
    if len(matches) > 1:
        names = [str(event.get("title", "Untitled")) for event in matches[:3] if event.get("title")]
        suffix = " I found %s." % ", ".join(names) if names else ""
        return {
            "speech": "I found multiple matching events. Please be more specific.%s" % suffix,
            "display": {"matches": matches[:3]},
            "error": "multiple_matches",
        }

    match = matches[0]
    matched_event_id = str(match.get("event_id") or "").strip()
    matched_title = str(match.get("title") or title or "that event")
    if not matched_event_id:
        return {
            "speech": "I found the event, but I couldn't determine which calendar entry to delete.",
            "display": {},
            "error": "missing_event_id",
        }

    result = await manager.delete_event(matched_event_id, calendar="auto", user_id=uid)
    if result.get("ok"):
        return {
            "speech": "Deleted '%s' from your calendar." % matched_title,
            "display": {"event_id": matched_event_id, "title": matched_title},
            "error": None,
        }
    return {
        "speech": str(result.get("message", "I couldn't delete that event from your calendar.")),
        "display": {},
        "error": str(result.get("error", "unknown")),
    }


def parse_natural_time(text: str) -> datetime.datetime | None:
    """Parse a small set of natural-language time expressions."""
    text = text.strip().lower()
    reversed_match = re.match(
        r"^(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(tomorrow|(?:next|this)\s+\w+|\w+day)$",
        text,
        re.I,
    )
    if reversed_match:
        text = reversed_match.group(2).strip() + " " + reversed_match.group(1).strip()

    now = datetime.datetime.now()
    today = now.date()
    base_date = today
    day_names = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
        "mon": 0,
        "tue": 1,
        "tues": 1,
        "wed": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }
    consumed = False

    if text.startswith("tomorrow"):
        base_date = today + datetime.timedelta(days=1)
        text = re.sub(r"^tomorrow\s*", "", text)
        text = re.sub(r"^at\s+", "", text).strip()
        consumed = True

    if not consumed:
        match = re.match(r"^next\s+(\w+)(.*)", text)
        if match and match.group(1) in day_names:
            target_wd = day_names[match.group(1)]
            days_to_next_monday = 7 - today.weekday()
            base_date = today + datetime.timedelta(days=days_to_next_monday + target_wd)
            text = re.sub(r"^at\s+", "", match.group(2).strip())
            consumed = True

    if not consumed:
        match = re.match(r"^this\s+(\w+)(.*)", text)
        if match and match.group(1) in day_names:
            target_wd = day_names[match.group(1)]
            days_ahead = (target_wd - today.weekday()) % 7
            base_date = today + datetime.timedelta(days=days_ahead)
            text = re.sub(r"^at\s+", "", match.group(2).strip())
            consumed = True

    if not consumed:
        match = re.match(r"^(\w+)(.*)", text)
        if match and match.group(1) in day_names:
            target_wd = day_names[match.group(1)]
            days_ahead = (target_wd - today.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            base_date = today + datetime.timedelta(days=days_ahead)
            text = re.sub(r"^at\s+", "", match.group(2).strip())

    text = re.split(r"\s+(?:to|for|about|and|then)\s+", text, maxsplit=1)[0].strip()
    if text in ("noon", "12 noon"):
        return datetime.datetime.combine(base_date, datetime.time(12, 0))
    if text == "midnight":
        return datetime.datetime.combine(base_date + datetime.timedelta(days=1), datetime.time(0, 0))

    match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    period = match.group(3)
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    elif not period and hour < 8:
        hour += 12

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return datetime.datetime.combine(base_date, datetime.time(hour, minute))
    return None


async def _events_summary_response(
    *,
    user_id: str,
    start_date: datetime.datetime,
    end_date: datetime.datetime,
    max_results: int,
    empty_speech: str,
    lead_template: str,
    display_key: str,
    display_value: str,
) -> dict[str, Any]:
    manager = _get_manager()
    calendars_result = await manager.list_calendars(user_id=user_id, provider="all")
    connection_fields = _calendar_connection_fields(calendars_result)

    result = await manager.get_events(
        start_date=start_date,
        end_date=end_date,
        max_results=max_results,
        calendar="all",
        user_id=user_id,
    )
    if not result.get("ok"):
        return {
            "speech": "I couldn't check your calendar right now.",
            "display": {"events": [], display_key: display_value, **connection_fields},
            "error": str(result.get("error", "unknown")),
        }

    connection_fields.update(_calendar_connection_fields(result))
    events = result.get("events", [])
    if not events:
        return {
            "speech": empty_speech,
            "display": {"events": [], display_key: display_value, **connection_fields},
            "error": None,
        }

    count = len(events)
    parts = [lead_template % (count, "s" if count != 1 else "")]
    for event in events[:5]:
        title = str(event.get("title") or "Untitled")
        time_str = str(event.get("time") or "")
        if time_str:
            parts.append("%s at %s." % (title, time_str))
        else:
            parts.append("%s." % title)
    return {
        "speech": " ".join(parts),
        "display": {
            "events": events[:max_results],
            display_key: display_value,
            **connection_fields,
        },
        "error": None,
    }


def _get_manager() -> Any:
    from services.calendar import get_calendar_manager

    return get_calendar_manager()
