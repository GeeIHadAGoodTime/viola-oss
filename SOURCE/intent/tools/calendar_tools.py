"""Calendar tools for the agent executor."""

from __future__ import annotations

import datetime
import re
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

from core.logging_config import get_logger
from intent.tool_types import ToolResult
from services.calendar.datetime_utils import CalendarDateTimeUtils
from services.user_timezone import user_timezone_scope

logger = get_logger(__name__)

_CENTRAL_TZ = ZoneInfo("America/Chicago")
_DATETIME_UTILS = CalendarDateTimeUtils()


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


def _require_user_id(user_id: str | None) -> str:
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


# ---------------------------------------------------------------------------
# Reminder-step disclosure
#
# Every calendar mutation also drives the pre-event reminder scheduler, inside
# a try/except that swallowed the exception. The event itself really was
# created/updated/deleted, so flipping the whole result to a failure would be
# wrong -- and worse than wrong, it invites a retry that duplicates the event.
# But saying nothing at all meant "event added" read to the model, and then to
# the user, as "event added and you will be reminded", with no way to tell that
# the reminder half never happened. These notes name which half is established.
#
# The outcome is deliberately three-valued, because the reminder functions
# differ in what they report back. ``sync_event_reminder`` propagates its
# failures, so a clean return really is a completed step. ``cancel_event_reminder``,
# ``sync_event_reminders`` and ``reconcile_reminders_for_window`` catch and log
# their own per-item errors (services/calendar/reminders.py), so a clean return
# from them proves the step was driven and nothing more -- calling that
# "completed" would be the same optimistic guess one level further down.
# ---------------------------------------------------------------------------
_REMINDER_SYNC_FIELD = "reminder_sync"
_REMINDER_COMPLETED = "completed"
_REMINDER_REQUESTED = "requested"
_REMINDER_FAILED = "failed"


def _reminder_note(step: str, outcome: str, error: BaseException | None = None) -> dict[str, Any]:
    note: dict[str, Any] = {"step": step, "outcome": outcome}
    if error is not None:
        note["error"] = "%s: %s" % (type(error).__name__, error)
    return note


def _display_timezone() -> datetime.tzinfo:
    """The zone a bare wall-clock time means, for the user of this turn.

    #3557: this used to be ``settings.calendar_timezone``, a per-process value.
    On the cloud that is the container's zone (UTC), so "2pm" from a user in
    America/Chicago was stored as 14:00Z and shown back to them as 9:00 AM.
    """
    return _DATETIME_UTILS.get_user_display_timezone()


def _normalize_parsed_datetime(
    parsed: datetime.datetime,
    timezone_hint: datetime.tzinfo | None = None,
) -> datetime.datetime:
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(datetime.UTC)
    return parsed.replace(tzinfo=timezone_hint or _display_timezone()).astimezone(datetime.UTC)


def _strip_timezone_hint(value: str) -> tuple[str, datetime.tzinfo | None]:
    central_pattern = r"\b(?:central(?:\s+(?:standard|daylight))?\s+time|cst|cdt|ct)\b"
    stripped, count = re.subn(central_pattern, " ", value, flags=re.IGNORECASE)
    if count:
        return re.sub(r"\s+", " ", stripped).strip(), _CENTRAL_TZ
    return value, None


def _user_now_naive() -> datetime.datetime:
    """Current wall-clock time in the user's own zone, as a naive datetime.

    "Tomorrow" has to be resolved against the user's calendar day. Using the
    server's clock made "tomorrow" mean the wrong DAY for anyone whose local
    date differs from the container's UTC date (#3557).
    """
    return datetime.datetime.now(tz=_display_timezone()).replace(tzinfo=None)


def _relative_default(value: str, now: datetime.datetime | None) -> tuple[str, datetime.datetime | None]:
    base = now or _user_now_naive()
    base = base.replace(hour=0, minute=0, second=0, microsecond=0)
    replacements = {
        "today": 0,
        "tomorrow": 1,
    }
    cleaned = value
    for word, days in replacements.items():
        pattern = r"\b%s\b" % word
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\bat\b", " ", cleaned, flags=re.IGNORECASE)
            return re.sub(r"\s+", " ", cleaned).strip(), base + datetime.timedelta(days=days)
    return value, None


def _parse_with_dateutil(
    value: str,
    *,
    default: datetime.datetime | None = None,
    timezone_hint: datetime.tzinfo | None = None,
) -> datetime.datetime | None:
    if not value.strip():
        if default is None:
            return None
        parsed = default
    else:
        try:
            parsed = dateutil_parser.parse(value, default=default)
        except (TypeError, ValueError, OverflowError):
            return None

    return _normalize_parsed_datetime(parsed, timezone_hint)


def _parse_datetime(value: str, *, now: datetime.datetime | None = None) -> datetime.datetime | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    natural_value, timezone_hint = _strip_timezone_hint(value)
    relative_value, relative_default = _relative_default(natural_value, now)
    if relative_default is not None:
        return _parse_with_dateutil(relative_value, default=relative_default, timezone_hint=timezone_hint)

    if "T" in value:
        iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.datetime.fromisoformat(iso_value)
        except ValueError:
            pass
        else:
            return _normalize_parsed_datetime(parsed, timezone_hint)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return _normalize_parsed_datetime(datetime.datetime.strptime(natural_value, fmt), timezone_hint)
        except ValueError:
            continue
    parsed = _parse_with_dateutil(natural_value, timezone_hint=timezone_hint)
    if parsed is not None:
        return parsed
    return None


async def calendar_add_event_handler(
    user_id: str,
    title: str,
    start_time: str,
    end_time: str = "",
    description: str = "",
    location: str = "",
    all_day: bool = False,
    provider: str = "auto",
    calendar_id: str = "",
    attendees: list[str] | None = None,
) -> ToolResult:
    # Bind this user's own timezone for the whole call: a bare wall-clock
    # time means their local time, not the server's (#3557).
    async with user_timezone_scope(user_id):
        if not title or not title.strip():
            return ToolResult(ok=False, data=None, error="Event title is required.")

        start_dt = _parse_datetime(start_time)
        if start_dt is None:
            return ToolResult(
                ok=False,
                data=None,
                error="Could not parse start_time. Use ISO-8601 format like '2026-03-22T14:00'.",
            )

        end_dt = None
        if end_time:
            end_dt = _parse_datetime(end_time)
            if end_dt is None:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Could not parse end_time. Use ISO-8601 format like '2026-03-22T15:00'.",
                )
        from services.calendar.manager import get_calendar_manager

        mgr = get_calendar_manager()
        result = await mgr.add_event(
            title=title.strip(),
            start_time=start_dt,
            end_time=end_dt,
            description=description.strip() if description else None,
            location=location.strip() if location else None,
            calendar=provider or "auto",
            calendar_id=calendar_id or None,
            all_day=all_day,
            attendees=attendees,
            user_id=_require_user_id(user_id),
        )
        if not result.get("ok"):
            return ToolResult(
                ok=False, data=result, error=str(result.get("message", "Failed to create calendar event."))
            )

        data = dict(result)
        event = result.get("event")
        if isinstance(event, dict):
            try:
                from services.calendar.reminders import sync_event_reminder

                await sync_event_reminder(_require_user_id(user_id), event)
            except Exception as exc:
                logger.exception("Failed to schedule reminder for new calendar event")
                data[_REMINDER_SYNC_FIELD] = _reminder_note("schedule", _REMINDER_FAILED, exc)
            else:
                data[_REMINDER_SYNC_FIELD] = _reminder_note("schedule", _REMINDER_COMPLETED)

        return ToolResult(ok=True, data=data)


async def calendar_list_events_handler(
    user_id: str,
    start_date: str = "",
    end_date: str = "",
    max_results: int = 10,
    provider: str = "all",
    calendar_id: str = "",
) -> ToolResult:
    # Bind this user's own timezone for the whole call: a bare wall-clock
    # time means their local time, not the server's (#3557).
    async with user_timezone_scope(user_id):
        start_dt = _parse_datetime(start_date) if start_date else None
        end_dt = _parse_datetime(end_date) if end_date else None
        if start_dt is None:
            start_dt = _user_now_naive()
        if end_dt is None:
            end_dt = start_dt + datetime.timedelta(days=7)

        from services.calendar.manager import get_calendar_manager

        mgr = get_calendar_manager()
        uid = _require_user_id(user_id)
        result = await mgr.get_events(
            start_date=start_dt,
            end_date=end_dt,
            max_results=min(max_results, 50),
            calendar=provider or "all",
            calendar_id=calendar_id or None,
            user_id=uid,
        )
        if not result.get("ok"):
            return ToolResult(
                ok=False, data=result, error=str(result.get("message", "Failed to fetch calendar events."))
            )
        data = dict(result)
        data.setdefault("events", [])
        calendars_probe_error = ""
        if not result.get("events"):
            calendars_result = await mgr.list_calendars(user_id=uid, provider=provider or "all")
            if calendars_result.get("ok"):
                connection_fields = _calendar_connection_fields(calendars_result)
                data.update(connection_fields)
                data["calendars"] = calendars_result.get("calendars", [])
            else:
                calendars_probe_error = str(calendars_result.get("message") or "calendar listing failed")
        data.update(_calendar_connection_fields(data))
        if calendars_probe_error and not data.get("calendars_connected"):
            # An empty listing plus a failed calendar probe is not evidence that no
            # calendar is connected; it is evidence that we could not find out. The
            # probe failure was discarded, so ``calendars_connected: false`` read as
            # an observation, and the model told users their calendar was not
            # connected on the strength of a check that never completed.
            data["calendars_connected_verified"] = False
            data["calendars_probe_error"] = calendars_probe_error

        full_listing = (provider or "all").strip().lower() in ("all", "")
        reminder_step = "reconcile" if full_listing else "sync"
        try:
            from services.calendar.reminders import reconcile_reminders_for_window, sync_event_reminders

            events_list = [item for item in data.get("events", []) if isinstance(item, dict)]
            if full_listing:
                # A full ("all providers") listing is trustworthy enough to also
                # prune reminders for events that disappeared from the window
                # (moved or canceled directly with the provider, not via Viola).
                await reconcile_reminders_for_window(uid, start_dt, end_dt, events_list)
            else:
                # A provider-filtered listing is a partial view of the user's
                # calendar; only create/update reminders for what's present,
                # never prune (a missing event here may just be on another
                # provider, not actually canceled).
                await sync_event_reminders(uid, events_list)
        except Exception as exc:
            logger.exception("Calendar reminder reconciliation failed during list")
            data[_REMINDER_SYNC_FIELD] = _reminder_note(reminder_step, _REMINDER_FAILED, exc)
        else:
            data[_REMINDER_SYNC_FIELD] = _reminder_note(reminder_step, _REMINDER_REQUESTED)

        return ToolResult(ok=True, data=data)


async def calendar_list_calendars_handler(
    user_id: str,
    provider: str = "all",
) -> ToolResult:
    from services.calendar.manager import get_calendar_manager

    mgr = get_calendar_manager()
    result = await mgr.list_calendars(user_id=_require_user_id(user_id), provider=provider or "all")
    if not result.get("ok"):
        return ToolResult(ok=False, data=result, error=str(result.get("message", "Failed to list calendars.")))
    data = dict(result)
    data.setdefault("events", [])
    data.update(_calendar_connection_fields(data))
    return ToolResult(ok=True, data=data)


async def calendar_get_event_handler(
    user_id: str,
    event_id: str,
    provider: str = "auto",
    calendar_id: str = "",
) -> ToolResult:
    # Bind this user's own timezone for the whole call: a bare wall-clock
    # time means their local time, not the server's (#3557).
    async with user_timezone_scope(user_id):
        if not event_id or not event_id.strip():
            return ToolResult(ok=False, data=None, error="Event ID is required.")

        from services.calendar.manager import get_calendar_manager

        mgr = get_calendar_manager()
        result = await mgr.get_event(
            event_id=event_id.strip(),
            calendar=provider or "auto",
            calendar_id=calendar_id or None,
            user_id=_require_user_id(user_id),
        )
        if not result.get("ok"):
            return ToolResult(
                ok=False, data=result, error=str(result.get("message", "Failed to fetch calendar event."))
            )
        return ToolResult(ok=True, data=result)


async def calendar_update_event_handler(
    user_id: str,
    event_id: str,
    title: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    location: str = "",
    all_day: bool = False,
    provider: str = "auto",
    calendar_id: str = "",
) -> ToolResult:
    # Bind this user's own timezone for the whole call: a bare wall-clock
    # time means their local time, not the server's (#3557).
    async with user_timezone_scope(user_id):
        if not event_id or not event_id.strip():
            return ToolResult(ok=False, data=None, error="Event ID is required.")

        start_dt = None
        if start_time:
            start_dt = _parse_datetime(start_time)
            if start_dt is None:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Could not parse start_time. Use ISO-8601 format like '2026-03-22T14:00'.",
                )
        end_dt = None
        if end_time:
            end_dt = _parse_datetime(end_time)
            if end_dt is None:
                return ToolResult(
                    ok=False,
                    data=None,
                    error="Could not parse end_time. Use ISO-8601 format like '2026-03-22T15:00'.",
                )

        from services.calendar.manager import get_calendar_manager

        mgr = get_calendar_manager()
        result = await mgr.update_event(
            event_id=event_id.strip(),
            title=title.strip() or None,
            start_time=start_dt,
            end_time=end_dt,
            description=description.strip() if description else None,
            location=location.strip() if location else None,
            calendar=provider or "auto",
            calendar_id=calendar_id or None,
            all_day=all_day,
            user_id=_require_user_id(user_id),
        )
        if not result.get("ok"):
            return ToolResult(
                ok=False, data=result, error=str(result.get("message", "Failed to update calendar event."))
            )

        data = dict(result)
        event = result.get("event")
        if isinstance(event, dict):
            try:
                from services.calendar.reminders import sync_event_reminder

                # Providers don't reliably echo event_id back on update; force it
                # so the reminder's deterministic label always matches this event.
                reminder_event = dict(event)
                reminder_event["event_id"] = event_id.strip()
                await sync_event_reminder(_require_user_id(user_id), reminder_event)
            except Exception as exc:
                logger.exception("Failed to reconcile reminder for updated calendar event id=%s", event_id)
                data[_REMINDER_SYNC_FIELD] = _reminder_note("reschedule", _REMINDER_FAILED, exc)
            else:
                data[_REMINDER_SYNC_FIELD] = _reminder_note("reschedule", _REMINDER_COMPLETED)

        return ToolResult(ok=True, data=data)


async def calendar_delete_event_handler(
    user_id: str,
    event_id: str,
    provider: str = "auto",
    calendar_id: str = "",
) -> ToolResult:
    if not event_id or not event_id.strip():
        return ToolResult(ok=False, data=None, error="Event ID is required.")

    from services.calendar.manager import get_calendar_manager

    mgr = get_calendar_manager()
    result: dict[str, Any] = {}
    try:
        result = await mgr.delete_event(
            event_id.strip(),
            calendar=provider or "auto",
            calendar_id=calendar_id or None,
            user_id=_require_user_id(user_id),
        )
    except Exception:
        logger.exception("calendar_delete_event failed for id=%s", event_id)
        return ToolResult(ok=False, data=None, error="Failed to delete calendar event.")

    if not result.get("ok"):
        return ToolResult(ok=False, data=result, error=str(result.get("message", "Failed to delete calendar event.")))

    data = dict(result)
    try:
        from services.calendar.reminders import cancel_event_reminder

        await cancel_event_reminder(_require_user_id(user_id), event_id.strip())
    except Exception as exc:
        logger.exception("Failed to cancel reminder for deleted calendar event id=%s", event_id)
        data[_REMINDER_SYNC_FIELD] = _reminder_note("cancel", _REMINDER_FAILED, exc)
    else:
        data[_REMINDER_SYNC_FIELD] = _reminder_note("cancel", _REMINDER_REQUESTED)

    return ToolResult(ok=True, data=data)


async def calendar_respond_event_handler(
    user_id: str,
    event_id: str,
    response_status: str,
    provider: str = "auto",
    calendar_id: str = "",
) -> ToolResult:
    if not event_id or not response_status:
        return ToolResult(ok=False, data=None, error="event_id and response_status are required.")

    from services.calendar.manager import get_calendar_manager

    mgr = get_calendar_manager()
    result = await mgr.respond_to_event(
        event_id=event_id.strip(),
        response_status=response_status.strip(),
        calendar=provider or "auto",
        calendar_id=calendar_id or None,
        user_id=_require_user_id(user_id),
    )
    if not result.get("ok"):
        return ToolResult(ok=False, data=result, error=str(result.get("message", "Failed to respond to event.")))
    return ToolResult(ok=True, data=result)


async def calendar_icloud_status_handler(user_id: str) -> ToolResult:
    """Report whether the user has an iCloud (or other external CalDAV) calendar connected.

    Read-only by design: this never accepts or collects Apple ID / app-specific
    password credentials through the conversational path. Apple requires an
    app-specific password for third-party CalDAV access (no OAuth exists for
    it), and a spoken or agent-relayed password is both a bad security
    practice and error-prone to transcribe -- so the connect flow lives only
    in the desktop Settings > Calendar UI (services/calendar/manager.py's
    connect_caldav_account(), fronted by POST /v1/calendar/caldav/connect and
    the "Connect iCloud Calendar" settings card, #3281). This tool exists so
    the agent can accurately answer "is my iCloud calendar connected?" and
    point the user to that form instead of guessing or asking for the
    password itself.
    """
    from services.calendar.manager import get_calendar_manager

    mgr = get_calendar_manager()
    uid = _require_user_id(user_id)
    providers = await mgr.list_providers(uid)
    caldav_provider = next((item for item in providers if item.get("provider") == "caldav"), None)
    connected = bool(caldav_provider and caldav_provider.get("configured"))

    if connected:
        # ``list_providers`` reports each provider's ``is_configured`` and
        # nothing else, so credentials being present is the whole observation.
        # "connected and syncing" asserted an ongoing sync that no call here
        # looked at -- a user whose stored app-specific password had stopped
        # working was told their calendar was syncing.
        return ToolResult(
            ok=True,
            data={
                "connected": True,
                "message": "Your iCloud (CalDAV) calendar is connected.",
            },
        )

    return ToolResult(
        ok=True,
        data={
            "connected": False,
            "message": (
                "No iCloud calendar is connected yet. Open Settings > Calendar in the Viola "
                "desktop app and use 'Connect' under iCloud Calendar with your Apple ID and an "
                "app-specific password (generate one at appleid.apple.com under Sign-In and "
                "Security > App-Specific Passwords, which requires two-factor authentication on "
                "the Apple ID). Viola can't collect that password through conversation -- it has "
                "to be entered directly in the settings form."
            ),
        },
    )


async def calendar_find_free_time_handler(
    user_id: str,
    attendees: list[str] | None = None,
    start_date: str = "",
    end_date: str = "",
    duration_minutes: int = 30,
    provider: str = "auto",
    calendar_id: str = "",
) -> ToolResult:
    # Bind this user's own timezone for the whole call: a bare wall-clock
    # time means their local time, not the server's (#3557).
    async with user_timezone_scope(user_id):
        if not start_date or not end_date:
            return ToolResult(ok=False, data=None, error="start_date and end_date are required.")

        start_dt = _parse_datetime(start_date)
        end_dt = _parse_datetime(end_date)
        if start_dt is None or end_dt is None:
            return ToolResult(ok=False, data=None, error="Could not parse start_date/end_date.")

        from services.calendar.manager import get_calendar_manager

        mgr = get_calendar_manager()
        result = await mgr.find_free_time(
            attendees=attendees or [],
            start_date=start_dt,
            end_date=end_dt,
            duration_minutes=duration_minutes,
            calendar=provider or "auto",
            calendar_id=calendar_id or None,
            user_id=_require_user_id(user_id),
        )
        if not result.get("ok"):
            return ToolResult(ok=False, data=result, error=str(result.get("message", "Failed to check availability.")))
        return ToolResult(ok=True, data=result)
