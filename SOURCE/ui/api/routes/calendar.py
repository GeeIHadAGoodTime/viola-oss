from __future__ import annotations

import datetime

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, from_legacy, success_response
from core.logging_config import get_logger
from fastapi import Body, Depends, HTTPException, Query, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth, require_operator_auth
from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)


class _CalendarDateRangeError(ValueError):
    """Raised when a supplied calendar date filter is malformed."""


# Well-known iCloud CalDAV principal + the python-caldav quirk-mode flag
# (services/calendar/providers/caldav.py forwards ``features`` straight to
# caldav.DAVClient(features=...); "icloud" is the library's iCloud-specific
# compatibility flag, exercised by tests/unit/services/test_caldav_provider.py).
# Apple mandates 2FA + an app-specific password for third-party CalDAV access —
# there is no OAuth path here, unlike Google/Microsoft, so this is a username +
# app-specific-password form, not a redirect flow.
_ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
_ICLOUD_CALDAV_FEATURES = "icloud"


def _get_user_id(request: Request) -> str:
    """Extract user_id from the authenticated request."""
    user_context = getattr(request.state, "user_context", None)
    if user_context and getattr(user_context, "user_id", None):
        return user_context.user_id

    session = getattr(request.state, "session", None)
    if session and getattr(session, "user_id", None):
        return session.user_id

    raise HTTPException(status_code=401, detail="Not authenticated")


def _resolve_date_range(
    date_param: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """Resolve date range from query parameters.

    Supports:
    - date=today → start of today to end of today
    - date=YYYY-MM-DD → start of that date to end of that date
    - start_date/end_date ISO strings → explicit range
    """
    if date_param:
        if date_param.lower() == "today":
            now = datetime.datetime.now()
            start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            return start_dt, end_dt
        try:
            parsed = datetime.datetime.strptime(date_param, "%Y-%m-%d")
            start_dt = parsed.replace(hour=0, minute=0, second=0)
            end_dt = parsed.replace(hour=23, minute=59, second=59)
            return start_dt, end_dt
        except ValueError as exc:
            log.debug("Invalid date param: %s", date_param)
            raise _CalendarDateRangeError("Invalid date format. Use 'today' or YYYY-MM-DD.") from exc

    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = datetime.datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except ValueError as exc:
            log.debug("Invalid start_date format '%s': %s", start_date, exc)
            raise _CalendarDateRangeError("Invalid start_date format. Use ISO-8601.") from exc

    if end_date:
        try:
            end_dt = datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError as exc:
            log.debug("Invalid end_date format '%s': %s", end_date, exc)
            raise _CalendarDateRangeError("Invalid end_date format. Use ISO-8601.") from exc

    return start_dt, end_dt


def _parse_body_datetime(raw_value: object, *, field_name: str, all_day: bool) -> datetime.datetime | JSONResponse:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_format", "Invalid %s format" % field_name),
        )

    value = raw_value.strip()
    # For all-day events the frontend sends a date-only string ("YYYY-MM-DD").
    # Normalise it to a full ISO datetime so fromisoformat() can parse it.
    if all_day and "T" not in value:
        value = value + "T00:00:00"

    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        log.debug("Invalid %s format '%s': %s", field_name, value, exc)
        return JSONResponse(
            status_code=400,
            content=failure_response("invalid_format", "Invalid %s format" % field_name),
        )


def _serialize_events(events: list[dict]) -> list[dict]:
    """Serialize event dicts for JSON response (convert datetime objects)."""
    serialized = []
    for event in events:
        e = dict(event)
        for key in ("start_time", "end_time"):
            val = e.get(key)
            if isinstance(val, datetime.datetime):
                e[key] = val.isoformat()
        serialized.append(e)
    return serialized


def register_calendar_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    hub = context.hub

    async def _broadcast_calendar_updated(payload: dict[str, object], *, user_id: str) -> None:
        if hub is None:
            return
        try:
            await hub.broadcast("calendar_updated", payload, user_id=user_id, force=True)
        except Exception as exc:
            log.debug("Failed to broadcast calendar update: %s", exc)

    @router.get("/v1/calendar/status", dependencies=[Depends(require_auth)])
    async def get_calendar_status(request: Request):
        """Check configured calendar providers for the current user."""

        async def _inner():
            user_id = _get_user_id(request)
            try:
                from services.calendar import get_calendar_manager

                calendar_mgr = get_calendar_manager()
                providers = await calendar_mgr.list_providers(user_id)
                connected_providers = [
                    str(item.get("provider"))
                    for item in providers
                    if bool(item.get("configured")) and item.get("provider")
                ]
                local_connected = "local" in connected_providers
                remote_connected = any(provider != "local" for provider in connected_providers)
                return success_response(
                    {
                        "connected": local_connected or remote_connected,
                        "local_connected": local_connected,
                        "remote_connected": remote_connected,
                        "connected_providers": connected_providers,
                        "providers": providers,
                    }
                )
            except Exception as exc:
                log.error("Calendar status check failed: %s", exc)
                return success_response(
                    {
                        "connected": True,
                        "local_connected": True,
                        "remote_connected": False,
                        "connected_providers": ["local"],
                        "providers": [{"provider": "local", "configured": True}],
                        "reason": "service_unavailable",
                    }
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/status", method="GET")

    @router.get("/v1/calendar/events/next", dependencies=[Depends(require_auth)])
    async def get_next_event(request: Request):
        """Get the next upcoming calendar event."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                calendar_mgr = get_calendar_manager()
                now = datetime.datetime.now()
                end = now + datetime.timedelta(days=7)

                result = await calendar_mgr.get_events(
                    start_date=now,
                    end_date=end,
                    max_results=1,
                    calendar="all",
                    user_id=user_id,
                )

                if result.get("ok") and result.get("events"):
                    events = _serialize_events(result["events"][:1])
                    return success_response({"event": events[0]})

                return success_response({"event": None})
            except Exception as exc:
                log.error("Failed to get next event: %s", exc)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "next_event_failed", "Couldn't load your next calendar event. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/events/next", method="GET")

    @router.get("/v1/calendar/calendars", dependencies=[Depends(require_auth)])
    async def list_calendar_calendars(request: Request, provider: str = "all"):
        """List calendars across configured providers."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.list_calendars(user_id=user_id, provider=provider)
                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to list calendars")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "list_calendars_failed", "Couldn't load your calendars. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/calendars", method="GET")

    @router.get("/v1/calendar/next", dependencies=[Depends(require_auth)])
    async def get_calendar_next(request: Request):
        """Get upcoming calendar events (next 5)."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.get_events(max_results=5, user_id=user_id)

                if result.get("ok"):
                    payload = {
                        "events": _serialize_events(result.get("events", [])),
                        "source": result.get("source", "unknown"),
                        "calendars_connected": bool(result.get("calendars_connected")),
                        "connected_providers": list(result.get("connected_providers") or []),
                    }
                    if result.get("events") or result.get("calendars_connected"):
                        return success_response(payload)
                    return success_response(payload)

                if result.get("events"):
                    return success_response(
                        {
                            "events": _serialize_events(result["events"]),
                            "source": result.get("source", "unknown"),
                        }
                    )
            except Exception as exc:
                log.error("Calendar error: %s", exc)

            return success_response(
                {
                    "events": [],
                    "source": "local",
                    "calendars_connected": True,
                    "connected_providers": ["local"],
                    "error": "calendar_unavailable",
                }
            )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/next", method="GET")

    @router.post("/v1/calendar/events", dependencies=[Depends(require_operator_auth)])
    async def add_calendar_event(request: Request, body: dict = Body(...)):
        """Create a new calendar event."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                title = body.get("title")
                if not title:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("missing_field", "title is required"),
                    )

                start_str = body.get("start_time")
                if not start_str:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("missing_field", "start_time is required"),
                    )

                all_day = bool(body.get("all_day", False))
                start_time = _parse_body_datetime(start_str, field_name="start_time", all_day=all_day)
                if isinstance(start_time, JSONResponse):
                    return start_time

                end_time = None
                end_str = body.get("end_time")
                if end_str:
                    parsed_end = _parse_body_datetime(end_str, field_name="end_time", all_day=all_day)
                    if isinstance(parsed_end, JSONResponse):
                        return parsed_end
                    end_time = parsed_end

                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.add_event(
                    title=title,
                    start_time=start_time,
                    end_time=end_time,
                    description=body.get("description"),
                    location=body.get("location"),
                    calendar=body.get("provider") or body.get("calendar", "auto"),
                    calendar_id=body.get("calendar_id"),
                    attendees=body.get("attendees"),
                    user_id=user_id,
                    all_day=all_day,
                )

                if isinstance(result, dict) and result.get("ok"):
                    await _broadcast_calendar_updated(
                        {
                            "action": "created",
                            "event_id": result.get("event_id"),
                            "title": title,
                        },
                        user_id=user_id,
                    )

                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to add calendar event")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("add_event_failed", "Failed to add event"),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/events", method="POST")

    # Keep legacy endpoint for backward compatibility
    @router.post("/v1/calendar/event", dependencies=[Depends(require_operator_auth)])
    async def add_calendar_event_legacy(request: Request, body: dict = Body(...)):
        return await add_calendar_event(request, body)

    @router.get("/v1/calendar/events", dependencies=[Depends(require_auth)])
    async def get_calendar_events(
        request: Request,
        date: str | None = Query(default=None),
        start_date: str | None = Query(default=None),
        end_date: str | None = Query(default=None),
        max_results: int = Query(default=10),
        calendar: str = Query(default="all"),
        calendar_id: str | None = Query(default=None),
    ):
        """Get calendar events. Supports ?date=today or ?date=YYYY-MM-DD."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                try:
                    start_dt, end_dt = _resolve_date_range(date, start_date, end_date)
                except _CalendarDateRangeError as exc:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("invalid_format", str(exc)),
                    )
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.get_events(
                    start_date=start_dt,
                    end_date=end_dt,
                    max_results=max_results,
                    calendar=calendar,
                    calendar_id=calendar_id,
                    user_id=user_id,
                )
                if isinstance(result, dict) and "ok" in result:
                    # Serialize datetime objects in events
                    events = result.get("events", [])
                    result["events"] = _serialize_events(events)
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to get calendar events")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "get_events_failed", "Couldn't load your calendar events. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/events", method="GET")

    @router.delete("/v1/calendar/events/{event_id}", dependencies=[Depends(require_operator_auth)])
    async def delete_calendar_event(
        request: Request,
        event_id: str,
        calendar: str = "auto",
        calendar_id: str | None = None,
    ):
        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.delete_event(
                    event_id,
                    calendar=calendar,
                    calendar_id=calendar_id,
                    user_id=user_id,
                )
                if isinstance(result, dict) and result.get("ok"):
                    await _broadcast_calendar_updated(
                        {
                            "action": "deleted",
                            "event_id": event_id,
                            "calendar": calendar,
                        },
                        user_id=user_id,
                    )
                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to delete calendar event")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("delete_event_failed", "Couldn't delete the event. Please try again."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/events/%s" % event_id, method="DELETE")

    # Keep legacy endpoint
    @router.delete("/v1/calendar/event/{event_id}", dependencies=[Depends(require_operator_auth)])
    async def delete_calendar_event_legacy(request: Request, event_id: str, calendar: str = "auto"):
        return await delete_calendar_event(request, event_id, calendar)

    @router.put("/v1/calendar/events/{event_id}", dependencies=[Depends(require_operator_auth)])
    async def update_calendar_event(request: Request, event_id: str, body: dict = Body(...)):
        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                start_time = None
                end_time = None
                all_day = bool(body.get("all_day", False))

                if body.get("start_time"):
                    parsed_start = _parse_body_datetime(body["start_time"], field_name="start_time", all_day=all_day)
                    if isinstance(parsed_start, JSONResponse):
                        return parsed_start
                    start_time = parsed_start

                if body.get("end_time"):
                    parsed_end = _parse_body_datetime(body["end_time"], field_name="end_time", all_day=all_day)
                    if isinstance(parsed_end, JSONResponse):
                        return parsed_end
                    end_time = parsed_end

                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.update_event(
                    event_id=event_id,
                    title=body.get("title"),
                    start_time=start_time,
                    end_time=end_time,
                    description=body.get("description"),
                    location=body.get("location"),
                    calendar=body.get("provider") or body.get("calendar", "auto"),
                    calendar_id=body.get("calendar_id"),
                    user_id=user_id,
                    all_day=all_day,
                )

                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to update calendar event")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("update_event_failed", "Couldn't update the event. Please try again."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/events/%s" % event_id, method="PUT")

    # Keep legacy endpoint
    @router.put("/v1/calendar/event/{event_id}", dependencies=[Depends(require_operator_auth)])
    async def update_calendar_event_legacy(request: Request, event_id: str, body: dict = Body(...)):
        return await update_calendar_event(request, event_id, body)

    @router.post("/v1/calendar/caldav/connect", dependencies=[Depends(require_operator_auth)])
    async def connect_caldav_calendar(request: Request, body: dict = Body(...)):
        """Connect a user-owned external CalDAV account (iCloud, Fastmail, etc.).

        Distinct from Google/Microsoft calendar linking: CalDAV has no OAuth
        redirect, so the user supplies a server URL + username + password
        (an iCloud app-specific password, not the Apple ID password) directly.
        """

        async def _inner():
            from services.calendar import get_calendar_manager
            from services.calendar.cloud_caldav import cloud_caldav_configured

            if cloud_caldav_configured():
                # This deployment IS the cloud, self-hosted-Radicale surface —
                # storing a user's own third-party secret (an iCloud
                # app-specific password) here would put an external-account
                # credential in cloud storage, which the Tier-3 rule in
                # CLAUDE.md forbids (OAuth/app-password style credentials for
                # a user's external accounts are desktop-only, never cloud).
                return JSONResponse(
                    status_code=400,
                    content=failure_response(
                        "caldav_connect_desktop_only",
                        "Connecting an external CalDAV calendar (like iCloud) is available in the desktop app.",
                    ),
                )

            user_id = _get_user_id(request)
            username = body.get("username")
            password = body.get("password")
            if not username or not password:
                return JSONResponse(
                    status_code=400,
                    content=failure_response("missing_field", "username and password are required"),
                )

            icloud = bool(body.get("icloud"))
            url = body.get("url") or (_ICLOUD_CALDAV_URL if icloud else None)
            if not url:
                return JSONResponse(
                    status_code=400,
                    content=failure_response("missing_field", "url is required (or set icloud: true)"),
                )
            features = body.get("features") or (_ICLOUD_CALDAV_FEATURES if icloud else None)

            try:
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.connect_caldav_account(
                    user_id=user_id,
                    url=str(url),
                    username=str(username),
                    password=str(password),
                    calendar_id=body.get("calendar_id"),
                    calendar_name=body.get("calendar_name"),
                    verify_ssl=bool(body.get("verify_ssl", True)),
                    features=features,
                )
                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to connect CalDAV account")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("caldav_connect_failed", "Couldn't connect that calendar account."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/caldav/connect", method="POST")

    @router.post("/v1/calendar/caldav/disconnect", dependencies=[Depends(require_operator_auth)])
    async def disconnect_caldav_calendar(request: Request):
        """Remove a previously connected external CalDAV account."""

        async def _inner():
            from services.calendar import get_calendar_manager

            user_id = _get_user_id(request)
            try:
                calendar_mgr = get_calendar_manager()
                result = await calendar_mgr.disconnect_caldav_account(user_id=user_id)
                if isinstance(result, dict) and "ok" in result:
                    return from_legacy(result)
                return success_response(result)
            except Exception:
                log.exception("Failed to disconnect CalDAV account")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("caldav_disconnect_failed", "Couldn't disconnect that calendar account."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/calendar/caldav/disconnect", method="POST")

    log.info("Calendar routes registered")


__all__ = ["register_calendar_routes"]
