"""Google Calendar provider implementation."""

from __future__ import annotations

import datetime
from typing import Any

from core.logging_config import get_logger
from services.calendar.integration_state import is_google_calendar_enabled
from services.calendar.providers.base import normalize_calendar, normalize_event

logger = get_logger(__name__)


class GoogleCalendarProvider:
    """Provider wrapper around the Google Calendar API."""

    provider_id = "google"

    def __init__(self, service_builder, datetime_utils):
        self._service_builder = service_builder
        self._datetime_utils = datetime_utils
        self._services: dict[str, Any] = {}

    async def is_configured(self, user_id: str) -> bool:
        service = await self._get_service(user_id)
        return service is not None

    async def list_calendars(self, user_id: str) -> list[dict[str, Any]]:
        service = await self._get_service(user_id)
        if service is None:
            return []

        try:
            response = service.calendarList().list().execute()
            items = response.get("items", [])
        except Exception:
            logger.exception("google list_calendars failed for user=%s", user_id)
            self._services.pop(user_id, None)
            return []

        calendars: list[dict[str, Any]] = []
        for item in items:
            calendars.append(
                normalize_calendar(
                    provider=self.provider_id,
                    calendar_id=str(item.get("id", "")),
                    name=str(item.get("summaryOverride") or item.get("summary") or "Untitled Calendar"),
                    description=str(item.get("description") or ""),
                    primary=bool(item.get("primary")),
                    writable=(str(item.get("accessRole") or "").lower() in {"owner", "writer"}),
                    timezone=item.get("timeZone"),
                    raw=item,
                )
            )
        return calendars

    async def list_events(
        self,
        user_id: str,
        *,
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        max_results: int,
        calendar_id: str | None = None,
    ) -> list[dict[str, Any]]:
        service = await self._get_service(user_id)
        if service is None:
            return []

        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)
        target_calendar = calendar_id or "primary"

        try:
            response = (
                service.events()
                .list(
                    calendarId=target_calendar,
                    timeMin=start_dt.isoformat(),
                    timeMax=end_dt.isoformat(),
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            items = response.get("items", [])
        except Exception:
            logger.exception("google list_events failed for user=%s", user_id)
            self._services.pop(user_id, None)
            return []

        return [self._normalize_google_event(item, calendar_id=target_calendar) for item in items]

    async def get_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        service = await self._get_service(user_id)
        if service is None:
            return None

        target_calendar = calendar_id or "primary"
        try:
            item = service.events().get(calendarId=target_calendar, eventId=event_id).execute()
        except Exception:
            logger.exception("google get_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        return self._normalize_google_event(item, calendar_id=target_calendar)

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
    ) -> dict[str, Any] | None:
        service = await self._get_service(user_id)
        if service is None:
            return None

        body = self._build_event_body(
            title=title,
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=location,
            all_day=all_day,
            attendees=attendees,
        )
        target_calendar = calendar_id or "primary"

        try:
            item = service.events().insert(calendarId=target_calendar, body=body).execute()
        except Exception:
            logger.exception("google create_event failed for user=%s title=%s", user_id, title)
            return None
        return self._normalize_google_event(item, calendar_id=target_calendar)

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
    ) -> dict[str, Any] | None:
        service = await self._get_service(user_id)
        if service is None:
            return None

        target_calendar = calendar_id or "primary"
        try:
            body = service.events().get(calendarId=target_calendar, eventId=event_id).execute()
            if title is not None:
                body["summary"] = title
            if description is not None:
                body["description"] = description
            if location is not None:
                body["location"] = location
            if start_time is not None:
                if all_day:
                    body["start"] = {"date": start_time.strftime("%Y-%m-%d")}
                else:
                    body["start"] = self._timed_payload(start_time)
            if end_time is not None:
                if all_day:
                    body["end"] = {"date": end_time.strftime("%Y-%m-%d")}
                else:
                    body["end"] = self._timed_payload(end_time)
            item = service.events().update(calendarId=target_calendar, eventId=event_id, body=body).execute()
        except Exception:
            logger.exception("google update_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        return self._normalize_google_event(item, calendar_id=target_calendar)

    async def delete_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> bool:
        service = await self._get_service(user_id)
        if service is None:
            return False

        target_calendar = calendar_id or "primary"
        try:
            service.events().delete(calendarId=target_calendar, eventId=event_id).execute()
            return True
        except Exception:
            logger.exception("google delete_event failed for user=%s event_id=%s", user_id, event_id)
            return False

    async def respond_to_event(
        self,
        user_id: str,
        *,
        event_id: str,
        response_status: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        service = await self._get_service(user_id)
        if service is None:
            return None

        target_calendar = calendar_id or "primary"
        try:
            body = service.events().get(calendarId=target_calendar, eventId=event_id).execute()
            attendees = list(body.get("attendees") or [])
            self_attendee = None
            for attendee in attendees:
                if attendee.get("self"):
                    self_attendee = attendee
                    break
            if self_attendee is None:
                self_attendee = {"self": True}
                attendees.append(self_attendee)
            self_attendee["responseStatus"] = response_status
            body["attendees"] = attendees
            item = service.events().update(calendarId=target_calendar, eventId=event_id, body=body).execute()
        except Exception:
            logger.exception("google respond_to_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        return self._normalize_google_event(item, calendar_id=target_calendar)

    async def find_free_time(
        self,
        user_id: str,
        *,
        attendees: list[str],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        service = await self._get_service(user_id)
        if service is None:
            return None

        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)
        items = [{"id": attendee} for attendee in attendees if attendee]
        if not items:
            items = [{"id": calendar_id or "primary"}]

        try:
            response = (
                service.freebusy()
                .query(
                    body={
                        "timeMin": start_dt.isoformat(),
                        "timeMax": end_dt.isoformat(),
                        "items": items,
                    }
                )
                .execute()
            )
        except Exception:
            logger.exception("google find_free_time failed for user=%s", user_id)
            return None

        return {
            "provider": self.provider_id,
            "duration_minutes": duration_minutes,
            "start_time": start_dt,
            "end_time": end_dt,
            "busy": response.get("calendars", {}),
            "raw": response,
        }

    async def _get_service(self, user_id: str):
        if not is_google_calendar_enabled(user_id):
            self._services.pop(user_id, None)
            return None

        service = self._services.get(user_id)
        if service is not None:
            return service
        service = await self._service_builder(user_id)
        if service is not None:
            self._services[user_id] = service
        return service

    def _normalize_google_event(self, item: dict[str, Any], *, calendar_id: str) -> dict[str, Any]:
        start_dt, end_dt, all_day = self._parse_google_times(item.get("start", {}), item.get("end", {}))
        attendees = [
            {
                "email": attendee.get("email"),
                "name": attendee.get("displayName"),
                "response_status": attendee.get("responseStatus"),
                "self": bool(attendee.get("self")),
            }
            for attendee in (item.get("attendees") or [])
            if isinstance(attendee, dict)
        ]
        return normalize_event(
            provider=self.provider_id,
            calendar_id=calendar_id,
            event_id=str(item.get("id", "")),
            title=str(item.get("summary") or "Untitled Event"),
            description=item.get("description"),
            location=item.get("location"),
            start_time=start_dt,
            end_time=end_dt,
            url=item.get("htmlLink"),
            all_day=all_day,
            attendees=attendees,
            status=item.get("status"),
            display_timezone=self._datetime_utils.get_user_display_timezone(),
            raw=item,
        )

    def _parse_google_times(
        self,
        start_payload: dict[str, Any],
        end_payload: dict[str, Any],
    ) -> tuple[datetime.datetime | None, datetime.datetime | None, bool]:
        all_day = False
        start_dt = None
        end_dt = None

        if "dateTime" in start_payload:
            start_dt = datetime.datetime.fromisoformat(str(start_payload["dateTime"]).replace("Z", "+00:00"))
        elif "date" in start_payload:
            start_dt = datetime.datetime.fromisoformat(str(start_payload["date"]))
            all_day = True

        if "dateTime" in end_payload:
            end_dt = datetime.datetime.fromisoformat(str(end_payload["dateTime"]).replace("Z", "+00:00"))
        elif "date" in end_payload:
            end_dt = datetime.datetime.fromisoformat(str(end_payload["date"]))
            all_day = True

        return start_dt, end_dt, all_day

    def _build_event_body(
        self,
        *,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        description: str | None,
        location: str | None,
        all_day: bool,
        attendees: list[str] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": title,
            "description": description,
            "location": location,
        }
        if all_day:
            body["start"] = {"date": start_time.strftime("%Y-%m-%d")}
            body["end"] = {"date": end_time.strftime("%Y-%m-%d")}
        else:
            body["start"] = self._timed_payload(start_time)
            body["end"] = self._timed_payload(end_time)
        if attendees:
            body["attendees"] = [{"email": attendee} for attendee in attendees if attendee]
        return body

    def _timed_payload(self, value: datetime.datetime) -> dict[str, Any]:
        normalized = self._datetime_utils.normalise_datetime(value)
        return {
            "dateTime": normalized.isoformat(),
            "timeZone": normalized.tzname() or "UTC",
        }
