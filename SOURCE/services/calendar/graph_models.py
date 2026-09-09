"""
Microsoft Graph calendar payload builders and normalizers.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from services.calendar.datetime_utils import CalendarDateTimeUtils

GraphDateTimeInput = str | dt.date | dt.datetime | Mapping[str, str]


def serialize_query_datetime(value: GraphDateTimeInput) -> str:
    """Serialize a datetime-like value for Graph query parameters."""
    if isinstance(value, Mapping):
        date_time = value.get("dateTime")
        if isinstance(date_time, str) and date_time:
            return date_time
        raise ValueError("dateTime mapping must include a non-empty 'dateTime' value")

    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.isoformat()

    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC).isoformat()

    parsed = CalendarDateTimeUtils.parse_iso_datetime(value)
    if parsed is not None:
        return parsed.isoformat()
    return value


def build_date_time_timezone(
    value: GraphDateTimeInput,
    *,
    timezone: str,
    all_day: bool = False,
    is_end: bool = False,
) -> dict[str, str]:
    """Build a Graph dateTimeTimeZone payload from a datetime-like value."""
    if isinstance(value, Mapping):
        date_time = value.get("dateTime")
        time_zone = value.get("timeZone") or timezone
        if not isinstance(date_time, str) or not date_time:
            raise ValueError("dateTime mapping must include a non-empty 'dateTime' value")
        return {
            "dateTime": date_time,
            "timeZone": str(time_zone),
        }

    target_tz = CalendarDateTimeUtils.get_timezone(timezone)
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime.combine(value, dt.time.min)
    else:
        parsed = CalendarDateTimeUtils.parse_iso_datetime(value)
        if parsed is not None:
            moment = parsed
        else:
            try:
                moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Unsupported datetime value for Graph payload") from exc

    if all_day:
        base_date = moment.date()
        if is_end and moment.time() == dt.time.min:
            base_date = base_date + dt.timedelta(days=1)
        moment = dt.datetime.combine(base_date, dt.time.min)
    elif moment.tzinfo is not None and target_tz is not None:
        moment = moment.astimezone(target_tz).replace(tzinfo=None)
    elif moment.tzinfo is not None:
        moment = moment.astimezone(dt.UTC).replace(tzinfo=None)

    return {
        "dateTime": moment.isoformat(timespec="seconds"),
        "timeZone": timezone,
    }


def build_attendees(attendees: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize a generic attendee list into Graph attendee objects."""
    normalized: list[dict[str, Any]] = []
    if not attendees:
        return normalized

    for attendee in attendees:
        if not isinstance(attendee, Mapping):
            continue

        email_address = attendee.get("emailAddress")
        if isinstance(email_address, Mapping):
            address = email_address.get("address")
            name = email_address.get("name")
        else:
            address = attendee.get("email") or attendee.get("address")
            name = attendee.get("name")

        if not isinstance(address, str) or not address:
            continue

        payload = {
            "emailAddress": {
                "address": address,
                "name": name if isinstance(name, str) and name else address,
            },
            "type": str(attendee.get("type") or attendee.get("attendeeType") or "required"),
        }
        normalized.append(payload)

    return normalized


def normalize_calendar(calendar: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Graph calendar object."""
    owner_payload = calendar.get("owner")
    email_payload = owner_payload.get("emailAddress") if isinstance(owner_payload, Mapping) else None
    owner = {
        "name": email_payload.get("name") if isinstance(email_payload, Mapping) else None,
        "address": email_payload.get("address") if isinstance(email_payload, Mapping) else None,
    }

    return {
        "id": calendar.get("id"),
        "name": calendar.get("name"),
        "is_default_calendar": bool(calendar.get("isDefaultCalendar")),
        "can_edit": bool(calendar.get("canEdit")),
        "can_share": bool(calendar.get("canShare")),
        "can_view_private_items": bool(calendar.get("canViewPrivateItems")),
        "hex_color": calendar.get("hexColor"),
        "change_key": calendar.get("changeKey"),
        "owner": owner,
        "allowed_online_meeting_providers": list(calendar.get("allowedOnlineMeetingProviders") or ()),
        "default_online_meeting_provider": calendar.get("defaultOnlineMeetingProvider"),
        "provider": "microsoft_calendar",
        "source": "microsoft_graph",
        "raw": dict(calendar),
    }


def normalize_response_status(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Graph responseStatus payload."""
    if not isinstance(value, Mapping):
        return None

    time_value = value.get("time")
    normalized_time = None
    if isinstance(time_value, str):
        normalized_time = CalendarDateTimeUtils.parse_iso_datetime(time_value)

    return {
        "response": value.get("response"),
        "time": time_value,
        "time_iso": normalized_time.isoformat() if normalized_time else None,
    }


def normalize_datetime_payload(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Graph dateTimeTimeZone payload."""
    if not isinstance(value, Mapping):
        return None

    date_time = value.get("dateTime")
    time_zone = value.get("timeZone")
    if not isinstance(date_time, str) or not date_time:
        return None

    parsed: dt.datetime | None = None
    if isinstance(time_zone, str) and time_zone.upper() == "UTC" and not date_time.endswith(("Z", "z")):
        try:
            parsed = dt.datetime.fromisoformat(date_time).replace(tzinfo=dt.UTC)
        except ValueError:
            parsed = None
    if parsed is None:
        parsed = CalendarDateTimeUtils.parse_iso_datetime(date_time)
        if parsed is None:
            try:
                parsed = dt.datetime.fromisoformat(date_time)
            except ValueError:
                parsed = None

    iso_value = None
    if parsed is not None:
        if parsed.tzinfo is None:
            tz_name = time_zone if isinstance(time_zone, str) else ""
            if tz_name.upper() == "UTC":
                parsed = parsed.replace(tzinfo=dt.UTC)
            else:
                resolved_tz = CalendarDateTimeUtils.get_timezone(tz_name)
                if resolved_tz is not None:
                    parsed = parsed.replace(tzinfo=resolved_tz)
        if parsed.tzinfo is not None:
            iso_value = parsed.isoformat()

    return {
        "date_time": date_time,
        "time_zone": time_zone,
        "iso": iso_value,
    }


def normalize_event(event: Mapping[str, Any], *, calendar_id: str | None = None) -> dict[str, Any]:
    """Normalize a Graph event object into a stable provider shape."""
    body_payload = event.get("body")
    location_payload = event.get("location")
    organizer_payload = event.get("organizer")
    organizer_email = organizer_payload.get("emailAddress") if isinstance(organizer_payload, Mapping) else None
    online_meeting = event.get("onlineMeeting")
    response_status = normalize_response_status(
        event.get("responseStatus") if isinstance(event.get("responseStatus"), Mapping) else None
    )
    start = normalize_datetime_payload(event.get("start") if isinstance(event.get("start"), Mapping) else None)
    end = normalize_datetime_payload(event.get("end") if isinstance(event.get("end"), Mapping) else None)

    attendees: list[dict[str, Any]] = []
    for attendee in event.get("attendees") or ():
        if not isinstance(attendee, Mapping):
            continue
        email_address = attendee.get("emailAddress")
        attendee_status = attendee.get("status")
        attendees.append(
            {
                "name": email_address.get("name") if isinstance(email_address, Mapping) else None,
                "address": email_address.get("address") if isinstance(email_address, Mapping) else None,
                "type": attendee.get("type"),
                "status": normalize_response_status(attendee_status if isinstance(attendee_status, Mapping) else None),
            }
        )

    location_name = None
    if isinstance(location_payload, Mapping):
        location_name = location_payload.get("displayName")
    elif isinstance(location_payload, str):
        location_name = location_payload

    description = None
    if isinstance(body_payload, Mapping):
        description = body_payload.get("content")
    if not description:
        description = event.get("bodyPreview")

    return {
        "id": event.get("id"),
        "calendar_id": calendar_id,
        "subject": event.get("subject"),
        "title": event.get("subject"),
        "description": description,
        "body_preview": event.get("bodyPreview"),
        "body_content_type": body_payload.get("contentType") if isinstance(body_payload, Mapping) else None,
        "location": location_name,
        "location_details": dict(location_payload) if isinstance(location_payload, Mapping) else None,
        "start": start,
        "end": end,
        "start_time": start.get("iso") if isinstance(start, Mapping) else None,
        "end_time": end.get("iso") if isinstance(end, Mapping) else None,
        "is_all_day": bool(event.get("isAllDay")),
        "organizer": {
            "name": organizer_email.get("name") if isinstance(organizer_email, Mapping) else None,
            "address": organizer_email.get("address") if isinstance(organizer_email, Mapping) else None,
        },
        "attendees": attendees,
        "response_status": response_status,
        "categories": list(event.get("categories") or ()),
        "importance": event.get("importance"),
        "sensitivity": event.get("sensitivity"),
        "show_as": event.get("showAs"),
        "is_cancelled": bool(event.get("isCancelled")),
        "allow_new_time_proposals": event.get("allowNewTimeProposals"),
        "response_requested": event.get("responseRequested"),
        "web_link": event.get("webLink"),
        "online_meeting_url": (online_meeting.get("joinUrl") if isinstance(online_meeting, Mapping) else None),
        "provider": "microsoft_calendar",
        "source": "microsoft_graph",
        "raw": dict(event),
    }
