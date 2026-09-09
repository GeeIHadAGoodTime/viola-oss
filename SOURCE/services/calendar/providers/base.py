"""Shared calendar provider contract and normalization helpers."""

from __future__ import annotations

import datetime
from typing import Any, Protocol


class CalendarProvider(Protocol):
    """Provider interface for calendar backends."""

    provider_id: str

    async def is_configured(self, user_id: str) -> bool: ...

    async def list_calendars(self, user_id: str) -> list[dict[str, Any]]: ...

    async def list_events(
        self,
        user_id: str,
        *,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        max_results: int,
        calendar_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def create_event(
        self,
        user_id: str,
        *,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        attendees: list[str] | None = None,
    ) -> dict[str, Any] | None: ...

    async def update_event(
        self,
        user_id: str,
        *,
        event_id: str,
        title: str | None = None,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
    ) -> dict[str, Any] | None: ...

    async def delete_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> bool: ...

    async def respond_to_event(
        self,
        user_id: str,
        *,
        event_id: str,
        response_status: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None: ...

    async def find_free_time(
        self,
        user_id: str,
        *,
        attendees: list[str],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None: ...


def normalize_calendar(
    *,
    provider: str,
    calendar_id: str,
    name: str,
    description: str = "",
    primary: bool = False,
    writable: bool = True,
    timezone: str | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a provider-agnostic calendar descriptor."""

    return {
        "provider": provider,
        "calendar_id": calendar_id,
        "name": name,
        "description": description,
        "primary": primary,
        "writable": writable,
        "timezone": timezone,
        "raw": raw or {},
    }


def _display_time(value: datetime.datetime) -> str:
    return value.strftime("%I:%M %p")


def _display_date(value: datetime.datetime) -> str:
    return value.strftime("%B %d, %Y").replace(" 0", " ")


def _display_local_datetime(
    value: datetime.datetime | None,
    *,
    all_day: bool,
    display_timezone: datetime.tzinfo | None,
) -> str:
    if value is None:
        return "Unknown"

    display_value = value
    if display_value.tzinfo is None or display_value.utcoffset() is None:
        display_value = display_value.replace(tzinfo=datetime.UTC)
    if display_timezone is not None:
        display_value = display_value.astimezone(display_timezone)

    if all_day:
        return _display_date(display_value)

    time_part = _display_time(display_value).lstrip("0")
    timezone_part = display_value.strftime("%Z")
    suffix = " %s" % timezone_part if timezone_part else ""
    return "%s at %s%s" % (_display_date(display_value), time_part, suffix)


def _stored_iso_utc(value: datetime.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).isoformat()


def normalize_event(
    *,
    provider: str,
    event_id: str,
    title: str,
    start_time: datetime.datetime | None,
    end_time: datetime.datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    calendar_id: str | None = None,
    url: str | None = None,
    all_day: bool | None = None,
    attendees: list[dict[str, Any]] | None = None,
    status: str | None = None,
    display_timezone: datetime.tzinfo | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a provider-agnostic event record."""

    if all_day is None and start_time is not None:
        all_day = (
            start_time.hour == 0
            and start_time.minute == 0
            and start_time.second == 0
            and end_time is not None
            and end_time.hour == 0
            and end_time.minute == 0
            and end_time.second == 0
        )

    display_local = _display_local_datetime(start_time, all_day=bool(all_day), display_timezone=display_timezone)
    stored_iso_utc = _stored_iso_utc(start_time)

    if start_time:
        if all_day:
            time_str = "All day"
        else:
            display_start = start_time
            if display_timezone is not None and start_time.tzinfo is not None and start_time.utcoffset() is not None:
                display_start = start_time.astimezone(display_timezone)
            time_str = display_start.strftime("%I:%M %p")
    else:
        time_str = "Unknown"

    return {
        "provider": provider,
        "calendar_id": calendar_id,
        "event_id": event_id,
        "title": title or "Untitled Event",
        "description": description or "",
        "location": location or "",
        "start_time": start_time,
        "end_time": end_time,
        "stored_iso_utc": stored_iso_utc,
        "display_local": display_local,
        "time": time_str,
        "url": url,
        "all_day": bool(all_day),
        "attendees": attendees or [],
        "status": status,
        "raw": raw or {},
    }
