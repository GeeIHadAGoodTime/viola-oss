"""Shared "when" parsing for scheduling tools (alarm, notify).

Both ``alarm_tools.set_alarm_handler`` and ``notification_tools.notify_handler``
need to turn a natural-language ``when`` string into a concrete future UTC
datetime. Before #2774 these were two divergent copies: the alarm parser
understood absolute clock times ("tomorrow at 8am", "9am", "noon", "9:30")
but not durations, while the notify parser understood relative durations
("in 30 minutes") but not clock times -- so "remind me tomorrow at 8am" via
``notify`` failed to parse even though the identical phrase worked for
``alarm``. One parser, shared, understands both.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

__all__ = ["parse_schedule_time"]


def parse_schedule_time(when: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse `when` into an aware UTC datetime in the future, or None if unparseable.

    Understands, in order:
      - ISO-8601 timestamps ("2026-03-22T17:00")
      - relative durations ("in 30 minutes", "2 hours", "45s")
      - absolute clock times ("9am", "9:30", "noon", "midnight"), optionally
        prefixed with "tomorrow"; a bare clock time rolls forward to the next
        occurrence (today if still ahead, else tomorrow)

    A result at or before `now` is never returned (never schedules into the
    past) -- ISO/duration results that land in the past are treated as
    unparseable rather than silently firing immediately.
    """
    text = (when or "").strip()
    if not text:
        return None

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    iso_result = _parse_iso(text, current)
    if iso_result is not None:
        return iso_result

    duration_result = _parse_relative_duration(text, current)
    if duration_result is not None:
        return duration_result

    return _parse_clock_time(text, current)


def _parse_iso(text: str, current: datetime) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    if parsed <= current:
        return None
    return parsed.astimezone(UTC)


def _parse_relative_duration(text: str, current: datetime) -> datetime | None:
    lowered = text.lower()
    if lowered.startswith("in "):
        lowered = lowered[3:].strip()

    try:
        from services.timer_core import parse_duration

        seconds = parse_duration(lowered)
    except (ImportError, ValueError, TypeError, AttributeError):
        seconds = None
    if seconds and seconds > 0:
        return current + timedelta(seconds=seconds)

    match = re.search(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|seconds?|secs?|s)\b", lowered)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit.startswith("h"):
        seconds = int(amount * 3600)
    elif unit.startswith("m"):
        seconds = int(amount * 60)
    else:
        seconds = int(amount)
    return current + timedelta(seconds=seconds) if seconds > 0 else None


def _parse_clock_time(text: str, current: datetime) -> datetime | None:
    lowered = text.lower()
    day_offset = 1 if "tomorrow" in lowered else 0
    base = current + timedelta(days=day_offset)

    if re.search(r"\bnoon\b", lowered):
        local_dt = base.replace(hour=12, minute=0, second=0, microsecond=0)
    elif re.search(r"\bmidnight\b", lowered):
        local_dt = base.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", lowered)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.group(2) else 0
            period = match.group(3)
            if period == "pm" and hour != 12:
                hour += 12
            if period == "am" and hour == 12:
                hour = 0
        else:
            match = re.search(r"\b(\d{1,2}):(\d{2})\b", lowered)
            if not match:
                if day_offset:
                    local_dt = base.replace(hour=8, minute=0, second=0, microsecond=0)
                    return local_dt.astimezone(UTC)
                return None
            hour = int(match.group(1))
            minute = int(match.group(2))

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        local_dt = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if local_dt <= current:
        local_dt += timedelta(days=1)
    return local_dt.astimezone(UTC)
