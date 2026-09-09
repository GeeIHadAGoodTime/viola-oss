"""
Microsoft Graph calendar provider.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from config.settings import settings
from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger
from services.calendar.datetime_utils import CalendarDateTimeUtils
from services.calendar.graph_auth import (
    MicrosoftGraphAuthError,
    MicrosoftGraphTokenResolver,
)
from services.calendar.graph_models import (
    GraphDateTimeInput,
    build_attendees,
    build_date_time_timezone,
    normalize_calendar as normalize_graph_calendar,
    normalize_event as normalize_graph_event,
    serialize_query_datetime,
)
from services.calendar.providers.base import (
    normalize_calendar as normalize_provider_calendar,
    normalize_event as normalize_provider_event,
)

logger = get_logger(__name__)

GRAPH_API_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_CALENDAR_SELECT = ",".join(
    (
        "id",
        "name",
        "isDefaultCalendar",
        "canEdit",
        "canShare",
        "canViewPrivateItems",
        "hexColor",
        "changeKey",
        "owner",
        "allowedOnlineMeetingProviders",
        "defaultOnlineMeetingProvider",
    )
)
GRAPH_EVENT_SELECT = ",".join(
    (
        "id",
        "subject",
        "body",
        "bodyPreview",
        "start",
        "end",
        "isAllDay",
        "location",
        "organizer",
        "attendees",
        "responseStatus",
        "categories",
        "importance",
        "sensitivity",
        "showAs",
        "isCancelled",
        "allowNewTimeProposals",
        "responseRequested",
        "webLink",
        "onlineMeeting",
    )
)
_UNSET = object()


class MicrosoftGraphApiError(RuntimeError):
    """Raised when the Microsoft Graph API returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MicrosoftGraphCalendarProvider:
    """Calendar CRUD client for Microsoft Graph."""

    def __init__(
        self,
        *,
        user_id: str,
        auth_resolver: MicrosoftGraphTokenResolver | None = None,
        http_client: httpx.AsyncClient | None = None,
        default_timezone: str | None = None,
        api_base_url: str = GRAPH_API_BASE_URL,
    ) -> None:
        if not user_id:
            raise ValueError("user_id is required")
        self._user_id = user_id
        self._auth_resolver = auth_resolver or MicrosoftGraphTokenResolver(user_id=user_id)
        self._http_client = http_client
        self._default_timezone = default_timezone or getattr(settings, "calendar_timezone", "UTC") or "UTC"
        self._api_base_url = api_base_url.rstrip("/")

    @property
    def _effective_timezone(self) -> str:
        """The zone Graph requests and renders in, for the current user.

        ``_default_timezone`` is a deployment/process value (UTC in a cloud
        container), which is the wrong answer for any user who does not live
        there -- #3557. The per-request user zone wins whenever one is bound.
        """
        from services.user_timezone import active_timezone_name

        return active_timezone_name() or self._default_timezone

    async def close(self) -> None:
        """Close the underlying HTTP client if one exists."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def list_calendars(self, *, max_results: int = 100) -> list[dict[str, Any]]:
        payloads = await self._collect_pages(
            "/me/calendars",
            params={
                "$top": max_results,
                "$select": GRAPH_CALENDAR_SELECT,
            },
            max_results=max_results,
        )
        return [normalize_graph_calendar(calendar) for calendar in payloads]

    async def list_events(
        self,
        *,
        calendar_id: str | None = None,
        start: GraphDateTimeInput | None = None,
        end: GraphDateTimeInput | None = None,
        max_results: int = 100,
        timezone: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_timezone = timezone or self._effective_timezone
        params: dict[str, Any] = {
            "$top": max_results,
            "$select": GRAPH_EVENT_SELECT,
        }

        if (start is None) != (end is None):
            raise ValueError("start and end must either both be provided or both be omitted")

        if start is not None and end is not None:
            params["startDateTime"] = serialize_query_datetime(start)
            params["endDateTime"] = serialize_query_datetime(end)
            path = self._calendar_view_path(calendar_id)
        else:
            path = self._events_collection_path(calendar_id)

        payloads = await self._collect_pages(
            path,
            params=params,
            max_results=max_results,
            timezone=effective_timezone,
        )
        events = [normalize_graph_event(event, calendar_id=calendar_id) for event in payloads]
        events.sort(key=self._event_sort_key)
        return events

    async def get_event(
        self,
        event_id: str,
        *,
        calendar_id: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            self._event_path(event_id, calendar_id=calendar_id),
            params={"$select": GRAPH_EVENT_SELECT},
            timezone=timezone or self._effective_timezone,
        )
        return normalize_graph_event(response.json(), calendar_id=calendar_id)

    async def create_event(
        self,
        *,
        subject: str,
        start: GraphDateTimeInput,
        end: GraphDateTimeInput,
        calendar_id: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: Sequence[Mapping[str, Any]] | None = None,
        all_day: bool = False,
        timezone: str | None = None,
        allow_new_time_proposals: bool | None = None,
        response_requested: bool | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_timezone = timezone or self._effective_timezone
        payload = self._build_event_payload(
            subject=subject,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            all_day=all_day,
            timezone=effective_timezone,
            allow_new_time_proposals=allow_new_time_proposals,
            response_requested=response_requested,
            extra_fields=extra_fields,
        )
        response = await self._request(
            "POST",
            self._events_collection_path(calendar_id),
            json_body=payload,
            timezone=effective_timezone,
            expected_status=(200, 201),
        )
        return normalize_graph_event(response.json(), calendar_id=calendar_id)

    async def update_event(
        self,
        event_id: str,
        *,
        calendar_id: str | None = None,
        subject: str | None | object = _UNSET,
        start: GraphDateTimeInput | None | object = _UNSET,
        end: GraphDateTimeInput | None | object = _UNSET,
        description: str | None | object = _UNSET,
        location: str | None | object = _UNSET,
        attendees: Sequence[Mapping[str, Any]] | None | object = _UNSET,
        all_day: bool | object = _UNSET,
        timezone: str | None = None,
        allow_new_time_proposals: bool | None | object = _UNSET,
        response_requested: bool | None | object = _UNSET,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_timezone = timezone or self._effective_timezone
        payload: dict[str, Any] = {}

        if subject is not _UNSET:
            payload["subject"] = subject
        if description is not _UNSET:
            payload["body"] = {
                "contentType": "text",
                "content": description or "",
            }
        if location is not _UNSET:
            payload["location"] = {
                "displayName": location or "",
            }
        if attendees is not _UNSET:
            payload["attendees"] = build_attendees(attendees)
        if allow_new_time_proposals is not _UNSET:
            payload["allowNewTimeProposals"] = allow_new_time_proposals
        if response_requested is not _UNSET:
            payload["responseRequested"] = response_requested

        resolved_all_day = bool(all_day) if all_day is not _UNSET else False
        if all_day is not _UNSET:
            payload["isAllDay"] = resolved_all_day

        if start is not _UNSET:
            if start is None:
                raise ValueError("start cannot be None when updating a Graph event")
            payload["start"] = build_date_time_timezone(
                start,
                timezone=effective_timezone,
                all_day=resolved_all_day,
            )
        if end is not _UNSET:
            if end is None:
                raise ValueError("end cannot be None when updating a Graph event")
            payload["end"] = build_date_time_timezone(
                end,
                timezone=effective_timezone,
                all_day=resolved_all_day,
                is_end=True,
            )

        if extra_fields:
            payload.update(extra_fields)

        response = await self._request(
            "PATCH",
            self._event_path(event_id, calendar_id=calendar_id),
            json_body=payload,
            timezone=effective_timezone,
        )
        return normalize_graph_event(response.json(), calendar_id=calendar_id)

    async def delete_event(
        self,
        event_id: str,
        *,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        await self._request(
            "DELETE",
            self._event_path(event_id, calendar_id=calendar_id),
            expected_status=(204,),
        )
        return {
            "ok": True,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "provider": "microsoft_calendar",
        }

    async def respond_to_event(
        self,
        event_id: str,
        *,
        response: str,
        comment: str | None = None,
        send_response: bool = True,
    ) -> dict[str, Any]:
        action = self._normalize_response_action(response)
        await self._request(
            "POST",
            "%s/%s" % (self._event_path(event_id), action),
            json_body={
                "comment": comment or "",
                "sendResponse": send_response,
            },
            expected_status=(202,),
        )
        return {
            "ok": True,
            "event_id": event_id,
            "action": action,
            "comment": comment,
            "send_response": send_response,
            "provider": "microsoft_calendar",
        }

    async def _collect_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        max_results: int | None = None,
        timezone: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_url = path
        next_params: Mapping[str, Any] | None = params

        while next_url:
            response = await self._request(
                "GET",
                next_url,
                params=next_params,
                timezone=timezone,
            )
            payload = response.json()
            page_items = payload.get("value", [])
            if not isinstance(page_items, list):
                raise MicrosoftGraphApiError("Microsoft Graph returned a non-list page payload")

            for item in page_items:
                if isinstance(item, Mapping):
                    items.append(dict(item))
                    if max_results is not None and len(items) >= max_results:
                        return items[:max_results]

            next_link = payload.get("@odata.nextLink")
            next_url = next_link if isinstance(next_link, str) and next_link else ""
            next_params = None

        return items

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timezone: str | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        response = await self._send(
            method,
            path,
            params=params,
            json_body=json_body,
            timezone=timezone,
            force_refresh=False,
        )
        if response.status_code == 401:
            logger.info(
                "Microsoft Graph request unauthorized for user %s; retrying after token refresh",
                self._user_id,
            )
            response = await self._send(
                method,
                path,
                params=params,
                json_body=json_body,
                timezone=timezone,
                force_refresh=True,
            )

        if response.status_code not in expected_status:
            raise MicrosoftGraphApiError(
                self._error_message(response),
                status_code=response.status_code,
            )
        return response

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        timezone: str | None,
        force_refresh: bool,
    ) -> httpx.Response:
        access_token = await self._auth_resolver.resolve_access_token(force_refresh=force_refresh)
        client = await self._get_client()
        url = path if path.startswith("http") else "%s%s" % (self._api_base_url, path)
        headers = {
            "Authorization": "Bearer %s" % access_token,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if timezone:
            headers["Prefer"] = 'outlook.timezone="%s"' % timezone

        return await client.request(
            method,
            url,
            params=self._clean_mapping(params),
            json=dict(json_body) if json_body is not None else None,
            headers=headers,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=TIMEOUT_EXTENDED)
        return self._http_client

    def _build_event_payload(
        self,
        *,
        subject: str,
        start: GraphDateTimeInput,
        end: GraphDateTimeInput,
        description: str | None,
        location: str | None,
        attendees: Sequence[Mapping[str, Any]] | None,
        all_day: bool,
        timezone: str,
        allow_new_time_proposals: bool | None,
        response_requested: bool | None,
        extra_fields: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": subject,
            "start": build_date_time_timezone(
                start,
                timezone=timezone,
                all_day=all_day,
            ),
            "end": build_date_time_timezone(
                end,
                timezone=timezone,
                all_day=all_day,
                is_end=True,
            ),
            "isAllDay": all_day,
        }
        if description is not None:
            payload["body"] = {
                "contentType": "text",
                "content": description,
            }
        if location is not None:
            payload["location"] = {
                "displayName": location,
            }
        normalized_attendees = build_attendees(attendees)
        if normalized_attendees:
            payload["attendees"] = normalized_attendees
        if allow_new_time_proposals is not None:
            payload["allowNewTimeProposals"] = allow_new_time_proposals
        if response_requested is not None:
            payload["responseRequested"] = response_requested
        if extra_fields:
            payload.update(extra_fields)
        return payload

    @staticmethod
    def _clean_mapping(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {key: item for key, item in value.items() if item is not None}

    @staticmethod
    def _event_sort_key(event: Mapping[str, Any]) -> str:
        start_time = event.get("start_time")
        if isinstance(start_time, str) and start_time:
            return start_time
        raw_start = event.get("start")
        if isinstance(raw_start, Mapping):
            date_time = raw_start.get("date_time")
            if isinstance(date_time, str):
                return date_time
        return ""

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, Mapping):
            error_payload = payload.get("error")
            if isinstance(error_payload, Mapping):
                message = error_payload.get("message") or error_payload.get("code")
                if isinstance(message, str) and message:
                    return "Microsoft Graph API error (%d): %s" % (
                        response.status_code,
                        message,
                    )

        return "Microsoft Graph API error (%d): %s" % (
            response.status_code,
            response.text[:400],
        )

    @staticmethod
    def _normalize_response_action(value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        action_map = {
            "accept": "accept",
            "decline": "decline",
            "tentative": "tentativelyAccept",
            "tentatively_accept": "tentativelyAccept",
            "tentativelyaccept": "tentativelyAccept",
        }
        action = action_map.get(normalized)
        if action is None:
            raise ValueError("response must be one of: accept, decline, tentative")
        return action

    @staticmethod
    def _quote_id(value: str) -> str:
        return quote(value, safe="")

    def _events_collection_path(self, calendar_id: str | None) -> str:
        if calendar_id:
            return "/me/calendars/%s/events" % self._quote_id(calendar_id)
        return "/me/events"

    def _calendar_view_path(self, calendar_id: str | None) -> str:
        if calendar_id:
            return "/me/calendars/%s/calendarView" % self._quote_id(calendar_id)
        return "/me/calendarView"

    def _event_path(self, event_id: str, calendar_id: str | None = None) -> str:
        quoted_event_id = self._quote_id(event_id)
        if calendar_id:
            return "/me/calendars/%s/events/%s" % (
                self._quote_id(calendar_id),
                quoted_event_id,
            )
        return "/me/events/%s" % quoted_event_id


class GraphCalendarProvider:
    """Provider-contract wrapper around the standalone Microsoft Graph client."""

    provider_id = "graph"

    def __init__(
        self,
        *,
        datetime_utils: CalendarDateTimeUtils | None = None,
        default_timezone: str | None = None,
    ) -> None:
        self._datetime_utils = datetime_utils or CalendarDateTimeUtils()
        self._default_timezone = default_timezone or getattr(settings, "calendar_timezone", "UTC") or "UTC"
        self._clients: dict[str, MicrosoftGraphCalendarProvider] = {}

    async def is_configured(self, user_id: str) -> bool:
        if not user_id:
            return False
        try:
            client = self._get_client(user_id)
            await client._auth_resolver.resolve_access_token()
            return True
        except MicrosoftGraphAuthError:
            return False
        except Exception:
            logger.exception("graph is_configured failed for user=%s", user_id)
            return False

    async def list_calendars(self, user_id: str) -> list[dict[str, Any]]:
        try:
            calendars = await self._get_client(user_id).list_calendars()
        except MicrosoftGraphAuthError:
            return []
        except Exception:
            logger.exception("graph list_calendars failed for user=%s", user_id)
            return []

        return [self._normalize_calendar(item) for item in calendars]

    async def list_events(
        self,
        user_id: str,
        *,
        start_date,
        end_date,
        max_results: int,
        calendar_id: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            events = await self._get_client(user_id).list_events(
                calendar_id=calendar_id,
                start=start_date,
                end=end_date,
                max_results=max_results,
                timezone=self._effective_timezone,
            )
        except MicrosoftGraphAuthError:
            return []
        except Exception:
            logger.exception("graph list_events failed for user=%s", user_id)
            return []

        return [self._normalize_event(item, calendar_id=calendar_id) for item in events]

    async def get_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            event = await self._get_client(user_id).get_event(
                event_id,
                calendar_id=calendar_id,
                timezone=self._effective_timezone,
            )
        except MicrosoftGraphAuthError:
            return None
        except Exception:
            logger.exception("graph get_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        return self._normalize_event(event, calendar_id=calendar_id) if event else None

    async def create_event(
        self,
        user_id: str,
        *,
        title: str,
        start_time,
        end_time,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        attendees: list[str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            event = await self._get_client(user_id).create_event(
                subject=title,
                start=start_time,
                end=end_time,
                calendar_id=calendar_id,
                description=description,
                location=location,
                attendees=self._build_attendees(attendees),
                all_day=all_day,
                timezone=self._effective_timezone,
            )
        except MicrosoftGraphAuthError:
            return None
        except Exception:
            logger.exception("graph create_event failed for user=%s title=%s", user_id, title)
            return None
        return self._normalize_event(event, calendar_id=calendar_id) if event else None

    async def update_event(
        self,
        user_id: str,
        *,
        event_id: str,
        title: str | None = None,
        start_time=None,
        end_time=None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
    ) -> dict[str, Any] | None:
        try:
            event = await self._get_client(user_id).update_event(
                event_id,
                calendar_id=calendar_id,
                subject=title,
                start=start_time,
                end=end_time,
                description=description,
                location=location,
                all_day=all_day,
                timezone=self._effective_timezone,
            )
        except MicrosoftGraphAuthError:
            return None
        except Exception:
            logger.exception("graph update_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        return self._normalize_event(event, calendar_id=calendar_id) if event else None

    async def delete_event(
        self,
        user_id: str,
        *,
        event_id: str,
        calendar_id: str | None = None,
    ) -> bool:
        try:
            result = await self._get_client(user_id).delete_event(
                event_id,
                calendar_id=calendar_id,
            )
        except MicrosoftGraphAuthError:
            return False
        except Exception:
            logger.exception("graph delete_event failed for user=%s event_id=%s", user_id, event_id)
            return False
        return bool(result and result.get("ok"))

    async def respond_to_event(
        self,
        user_id: str,
        *,
        event_id: str,
        response_status: str,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            result = await self._get_client(user_id).respond_to_event(
                event_id,
                response=response_status,
            )
        except MicrosoftGraphAuthError:
            return None
        except Exception:
            logger.exception("graph respond_to_event failed for user=%s event_id=%s", user_id, event_id)
            return None
        if not result or not result.get("ok"):
            return None
        return {
            "provider": self.provider_id,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "status": response_status,
            "raw": result,
        }

    async def find_free_time(
        self,
        user_id: str,
        *,
        attendees: list[str],
        start_date,
        end_date,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> dict[str, Any] | None:
        _ = attendees
        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)
        slot = self._first_free_slot(
            await self.list_events(
                user_id,
                start_date=start_dt,
                end_date=end_dt,
                max_results=100,
                calendar_id=calendar_id,
            ),
            start_dt=start_dt,
            end_dt=end_dt,
            duration_minutes=duration_minutes,
        )
        if slot is None:
            return None
        return {
            "provider": self.provider_id,
            "start_time": slot[0],
            "end_time": slot[1],
            "busy_events": [],
        }

    def _get_client(self, user_id: str) -> MicrosoftGraphCalendarProvider:
        client = self._clients.get(user_id)
        if client is None:
            client = MicrosoftGraphCalendarProvider(
                user_id=user_id,
                default_timezone=self._default_timezone,
            )
            self._clients[user_id] = client
        return client

    def _normalize_calendar(self, item: Mapping[str, Any]) -> dict[str, Any]:
        calendar_id = str(item.get("id") or "")
        name = str(item.get("name") or "Untitled Calendar")
        description = ""
        owner = item.get("owner")
        if isinstance(owner, Mapping):
            address = owner.get("address")
            if isinstance(address, str):
                description = address
        return normalize_provider_calendar(
            provider=self.provider_id,
            calendar_id=calendar_id,
            name=name,
            description=description,
            primary=bool(item.get("is_default_calendar")),
            writable=bool(item.get("can_edit", True)),
            timezone=self._effective_timezone,
            raw=dict(item),
        )

    def _normalize_event(
        self,
        item: Mapping[str, Any],
        *,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        start_time = self._parse_graph_datetime(item.get("start_time"))
        end_time = self._parse_graph_datetime(item.get("end_time"))
        response_status = item.get("response_status")
        return normalize_provider_event(
            provider=self.provider_id,
            calendar_id=str(item.get("calendar_id") or calendar_id or ""),
            event_id=str(item.get("id") or item.get("event_id") or ""),
            title=str(item.get("title") or item.get("subject") or "Untitled Event"),
            description=self._optional_str(item.get("description")),
            location=self._optional_str(item.get("location")),
            start_time=start_time,
            end_time=end_time,
            url=self._optional_str(item.get("web_link")),
            all_day=bool(item.get("is_all_day")),
            attendees=item.get("attendees") if isinstance(item.get("attendees"), list) else None,
            status=response_status.get("response") if isinstance(response_status, Mapping) else None,
            display_timezone=self._datetime_utils.get_user_display_timezone(),
            raw=dict(item),
        )

    def _parse_graph_datetime(self, value: Any):
        if not isinstance(value, str) or not value:
            return None
        parsed = self._datetime_utils.parse_iso_datetime(value)
        if parsed is None:
            return None
        return self._datetime_utils.normalise_datetime(parsed)

    def _first_free_slot(
        self,
        events: list[dict[str, Any]],
        *,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        duration_minutes: int,
    ) -> tuple[datetime.datetime, datetime.datetime] | None:
        cursor = start_dt
        duration = datetime.timedelta(minutes=max(duration_minutes, 1))
        for event in sorted(
            events,
            key=lambda event: event.get("start_time") or datetime.datetime.max.replace(tzinfo=datetime.UTC),
        ):
            event_start = event.get("start_time")
            event_end = event.get("end_time")
            if not isinstance(event_start, datetime.datetime):
                continue
            if cursor + duration <= event_start:
                return cursor, cursor + duration
            if isinstance(event_end, datetime.datetime) and event_end > cursor:
                cursor = event_end

        if cursor + duration <= end_dt:
            return cursor, cursor + duration
        return None

    @staticmethod
    def _build_attendees(attendees: Sequence[str] | None) -> list[dict[str, str]]:
        if not attendees:
            return []
        built: list[dict[str, str]] = []
        for attendee in attendees:
            text = str(attendee or "").strip()
            if text:
                built.append({"email": text, "name": text})
        return built

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
