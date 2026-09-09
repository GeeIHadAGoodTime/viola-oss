"""Standalone CalDAV provider for calendar CRUD operations.

This module intentionally does not wire itself into the shared calendar manager
yet. It provides a provider-scoped implementation that can be adopted by shared
calendar surfaces later without forcing CalDAV concerns into the Google path.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from core.logging_config import get_logger
from services.calendar.datetime_utils import CalendarDateTimeUtils
from services.calendar.providers.base import normalize_calendar, normalize_event

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

logger = get_logger(__name__)

_CREDENTIAL_SERVICE = "calendar_caldav"
_DEFAULT_EVENT_DURATION = timedelta(hours=1)
_DEFAULT_ALL_DAY_DURATION = timedelta(days=1)
_PRODID = "-//NOVVIOLA//CalDAV Calendar Provider//EN"


class CalDAVProviderError(RuntimeError):
    """Base error for CalDAV provider failures."""


class CalDAVDependencyError(CalDAVProviderError):
    """Raised when the CalDAV dependency is unavailable."""


class CalDAVCredentialsError(CalDAVProviderError):
    """Raised when CalDAV credentials are missing or invalid."""


class CalDAVEventNotFoundError(CalDAVProviderError):
    """Raised when an event cannot be found."""


class _CredentialRepository(Protocol):
    async def get_credential(self, user_id: str, service: str) -> str | None: ...

    async def set_credential(self, user_id: str, service: str, plaintext: str) -> None: ...

    async def delete_credential(self, user_id: str, service: str) -> bool: ...


@dataclass(slots=True)
class CalDAVCredentials:
    """Per-user CalDAV connection settings."""

    url: str
    username: str
    password: str
    calendar_id: str | None = None
    calendar_name: str | None = None
    verify_ssl: bool = True
    timeout_seconds: int | None = None
    features: str | None = None

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "calendar_id": self.calendar_id,
            "calendar_name": self.calendar_name,
            "verify_ssl": self.verify_ssl,
            "timeout_seconds": self.timeout_seconds,
            "features": self.features,
        }

    @classmethod
    def from_storage_dict(cls, payload: dict[str, object]) -> CalDAVCredentials:
        url = str(payload.get("url") or "").strip()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not url or not username or not password:
            raise CalDAVCredentialsError("Stored CalDAV credentials are incomplete.")
        return cls(
            url=url,
            username=username,
            password=password,
            calendar_id=_optional_str(payload.get("calendar_id") or payload.get("calendar_url")),
            calendar_name=_optional_str(payload.get("calendar_name")),
            verify_ssl=bool(payload.get("verify_ssl", True)),
            timeout_seconds=_optional_int(payload.get("timeout_seconds")),
            features=_optional_str(payload.get("features")),
        )


class CalDAVCalendarProvider:
    """Provider-scoped CalDAV implementation with per-user credential storage."""

    provider_id = "caldav"

    def __init__(
        self,
        *,
        datetime_utils: CalendarDateTimeUtils | None = None,
        auth_db_factory: Callable[[], Any] | None = None,
        client_factory: Callable[[CalDAVCredentials], Any] | None = None,
    ) -> None:
        self._datetime_utils = datetime_utils or CalendarDateTimeUtils()
        self._auth_db_factory = auth_db_factory
        self._client_factory = client_factory

    async def is_configured(self, user_id: str) -> bool:
        """Return True when a user has valid CalDAV credentials and at least one calendar."""
        try:
            credentials = await self.load_credentials(user_id)
            if credentials is None:
                return False
            calendars = await asyncio.to_thread(self._list_calendars_sync, credentials)
            return bool(calendars)
        except CalDAVProviderError:
            logger.exception("caldav is_configured failed for user=%s", user_id)
            return False

    async def load_credentials(self, user_id: str, *, provision: bool = True) -> CalDAVCredentials | None:
        """Load CalDAV credentials for a user from the encrypted auth DB.

        ``provision=False`` is the read-only mode for GDPR export/erasure and
        status checks: it must NEVER create an account as a side effect.
        """
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        raw = await repository.get_credential(uid, _CREDENTIAL_SERVICE)
        if not raw:
            if not provision:
                return None
            # Cloud deployments hosting their own CalDAV backend provision a
            # per-user account lazily on first calendar use. Returns None on
            # deployments without a hosted backend (desktop unchanged).
            from services.company_service_boundary import shared_company_service_available

            if not shared_company_service_available("services.calendar.cloud_caldav"):
                return None
            from services.calendar.cloud_caldav import ensure_cloud_caldav_credentials

            return await ensure_cloud_caldav_credentials(uid, repository)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalDAVCredentialsError("Stored CalDAV credentials are not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise CalDAVCredentialsError("Stored CalDAV credentials must be a JSON object.")
        return CalDAVCredentials.from_storage_dict(payload)

    async def store_credentials(self, user_id: str, credentials: CalDAVCredentials) -> None:
        """Persist CalDAV credentials for a user in the encrypted auth DB."""
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        await repository.set_credential(uid, _CREDENTIAL_SERVICE, json.dumps(credentials.to_storage_dict()))

    async def delete_credentials(self, user_id: str) -> bool:
        """Delete stored CalDAV credentials for a user."""
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        return await repository.delete_credential(uid, _CREDENTIAL_SERVICE)

    async def list_calendars(self, user_id: str) -> list[dict[str, object]]:
        """Return normalized calendar metadata for the user's CalDAV account."""
        try:
            credentials = await self._require_credentials(user_id)
            return await asyncio.to_thread(self._list_calendars_sync, credentials)
        except CalDAVCredentialsError:
            return []
        except CalDAVProviderError:
            logger.exception("caldav list_calendars failed for user=%s", user_id)
            return []

    async def list_events(
        self,
        user_id: str,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        calendar_id: str | None = None,
        max_results: int = 50,
    ) -> list[dict[str, object]]:
        """List normalized events across one or more CalDAV calendars."""
        try:
            credentials = await self._require_credentials(user_id)
            return await asyncio.to_thread(
                self._list_events_sync,
                credentials,
                start_date,
                end_date,
                calendar_id,
                max_results,
            )
        except CalDAVCredentialsError:
            return []
        except CalDAVProviderError:
            logger.exception("caldav list_events failed for user=%s", user_id)
            return []

    async def get_event(
        self,
        user_id: str,
        event_id: str,
        *,
        calendar_id: str | None = None,
    ) -> dict[str, object] | None:
        """Fetch a single event by UID."""
        try:
            credentials = await self._require_credentials(user_id)
            uid = _require_text(event_id, "event_id")
            return await asyncio.to_thread(self._get_event_sync, credentials, uid, calendar_id)
        except CalDAVCredentialsError:
            return None
        except CalDAVEventNotFoundError:
            return None
        except CalDAVProviderError:
            logger.exception("caldav get_event failed for user=%s event_id=%s", user_id, event_id)
            return None

    async def create_event(
        self,
        user_id: str,
        *,
        title: str,
        start_time: datetime,
        end_time: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool = False,
        attendees: Sequence[str] | None = None,
    ) -> dict[str, object] | None:
        """Create an event in the selected CalDAV calendar."""
        try:
            credentials = await self._require_credentials(user_id)
            return await asyncio.to_thread(
                self._create_event_sync,
                credentials,
                _require_text(title, "title"),
                start_time,
                end_time,
                description,
                location,
                calendar_id,
                all_day,
                list(attendees) if attendees else None,
            )
        except CalDAVCredentialsError:
            return None
        except CalDAVProviderError:
            logger.exception("caldav create_event failed for user=%s title=%s", user_id, title)
            return None

    async def update_event(
        self,
        user_id: str,
        event_id: str,
        *,
        title: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str | None = None,
        all_day: bool | None = None,
    ) -> dict[str, object] | None:
        """Update an existing CalDAV event by UID."""
        try:
            credentials = await self._require_credentials(user_id)
            uid = _require_text(event_id, "event_id")
            return await asyncio.to_thread(
                self._update_event_sync,
                credentials,
                uid,
                title,
                start_time,
                end_time,
                description,
                location,
                calendar_id,
                all_day,
            )
        except CalDAVCredentialsError:
            return None
        except CalDAVProviderError:
            logger.exception("caldav update_event failed for user=%s event_id=%s", user_id, event_id)
            return None

    async def delete_event(
        self,
        user_id: str,
        event_id: str,
        *,
        calendar_id: str | None = None,
    ) -> bool:
        """Delete an event by UID."""
        try:
            credentials = await self._require_credentials(user_id)
            uid = _require_text(event_id, "event_id")
            return await asyncio.to_thread(self._delete_event_sync, credentials, uid, calendar_id)
        except CalDAVCredentialsError:
            return False
        except CalDAVProviderError:
            logger.exception("caldav delete_event failed for user=%s event_id=%s", user_id, event_id)
            return False

    async def respond_to_event(
        self,
        user_id: str,
        *,
        event_id: str,
        response_status: str,
        calendar_id: str | None = None,
    ) -> dict[str, object] | None:
        _ = (user_id, event_id, response_status, calendar_id)
        return None

    async def find_free_time(
        self,
        user_id: str,
        *,
        attendees: list[str],
        start_date: datetime,
        end_date: datetime,
        duration_minutes: int,
        calendar_id: str | None = None,
    ) -> dict[str, object] | None:
        _ = attendees
        events = await self.list_events(
            user_id,
            start_date=start_date,
            end_date=end_date,
            calendar_id=calendar_id,
            max_results=200,
        )
        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)
        slot = self._first_free_slot(
            events,
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

    @staticmethod
    def _require_user_id(user_id: str | None) -> str:
        if not user_id:
            raise CalDAVCredentialsError("user_id is required")
        return user_id

    async def _require_credentials(self, user_id: str) -> CalDAVCredentials:
        credentials = await self.load_credentials(user_id)
        if credentials is None:
            raise CalDAVCredentialsError("No CalDAV credentials stored for user.")
        return credentials

    async def _get_credential_repository(self) -> _CredentialRepository:
        if self._auth_db_factory is None:
            from auth.database import get_auth_db

            db = get_auth_db()
        else:
            db = self._auth_db_factory()

        if not getattr(db, "_initialized", True):
            await db.initialize()

        if hasattr(db, "_pool"):
            # Cloud Postgres: Viola-issued CalDAV credentials live in the
            # RLS-scoped calendar_service_credentials table (migration 064) —
            # NOT the retired Tier-3 user_credentials table (migration 041).
            # These are OUR app passwords for OUR hosted Radicale, not the
            # user's third-party secrets.
            from services.calendar.cloud_caldav import PgCalendarCredentialsRepository

            return PgCalendarCredentialsRepository(db)

        repository = getattr(db, "user_credentials", None)
        if repository is None:
            raise CalDAVProviderError("Auth database does not expose user_credentials.")
        return repository

    def _list_calendars_sync(self, credentials: CalDAVCredentials) -> list[dict[str, object]]:
        with self._open_client(credentials) as client:
            calendars = self._get_calendars(client)
            default_target = credentials.calendar_id or credentials.calendar_name
            normalized: list[dict[str, object]] = []
            for index, calendar in enumerate(calendars):
                calendar_identifier = self._calendar_identifier(calendar, index)
                item = normalize_calendar(
                    provider=self.provider_id,
                    calendar_id=calendar_identifier,
                    name=self._calendar_name(calendar, calendar_identifier),
                    primary=bool(self._calendar_matches(calendar, default_target) if default_target else index == 0),
                    raw={"url": calendar_identifier},
                )
                item["id"] = calendar_identifier
                item["url"] = calendar_identifier
                item["is_default"] = item["primary"]
                item["source"] = self.provider_id
                normalized.append(item)
            return normalized

    def _list_events_sync(
        self,
        credentials: CalDAVCredentials,
        start_date: datetime | None,
        end_date: datetime | None,
        calendar_id: str | None,
        max_results: int,
    ) -> list[dict[str, object]]:
        with self._open_client(credentials) as client:
            calendars = self._resolve_calendars(client, credentials, calendar_id, prefer_default=False)
            events: list[dict[str, object]] = []
            for calendar in calendars:
                for event in self._search_calendar_events(calendar, start_date, end_date):
                    events.append(self._normalize_event(event, calendar))

            events.sort(key=self._event_sort_key)
            if max_results > 0:
                return events[:max_results]
            return events

    def _get_event_sync(
        self,
        credentials: CalDAVCredentials,
        event_id: str,
        calendar_id: str | None,
    ) -> dict[str, object]:
        with self._open_client(credentials) as client:
            event, calendar = self._find_event(client, credentials, event_id, calendar_id)
            return self._normalize_event(event, calendar)

    def _create_event_sync(
        self,
        credentials: CalDAVCredentials,
        title: str,
        start_time: datetime,
        end_time: datetime | None,
        description: str | None,
        location: str | None,
        calendar_id: str | None,
        all_day: bool,
        attendees: list[str] | None = None,
    ) -> dict[str, object]:
        with self._open_client(credentials) as client:
            calendar = self._resolve_calendar(client, credentials, calendar_id)
            event_id = str(uuid.uuid4())
            ical = self._build_icalendar_payload(
                event_id=event_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                description=description,
                location=location,
                all_day=all_day,
                attendees=attendees,
            )
            saved_event = self._save_event(calendar, ical)
            if saved_event is None:
                saved_event = self._fetch_event_from_calendar(calendar, event_id)
            if saved_event is None:
                return {
                    "provider": self.provider_id,
                    "event_id": event_id,
                    "id": event_id,
                    "calendar_id": self._calendar_identifier(calendar),
                    "calendar_name": self._calendar_name(calendar),
                    "title": title,
                    "description": description,
                    "location": location,
                    "start_time": self._normalize_datetime_value(start_time),
                    "end_time": self._normalize_datetime_value(
                        end_time if end_time is not None else self._default_end_time(start_time, all_day)
                    ),
                    "all_day": all_day,
                    "source": self.provider_id,
                    "time": "All day" if all_day else self._normalize_datetime_value(start_time).strftime("%I:%M %p"),
                    "raw": {},
                }
            return self._normalize_event(saved_event, calendar)

    def _update_event_sync(
        self,
        credentials: CalDAVCredentials,
        event_id: str,
        title: str | None,
        start_time: datetime | None,
        end_time: datetime | None,
        description: str | None,
        location: str | None,
        calendar_id: str | None,
        all_day: bool | None,
    ) -> dict[str, object]:
        with self._open_client(credentials) as client:
            event, calendar = self._find_event(client, credentials, event_id, calendar_id)
            with self._edit_event_component(event) as component:
                current_start = self._decode_component_value(component, "DTSTART")
                current_end = self._decode_component_value(component, "DTEND")
                is_all_day = all_day if all_day is not None else self._is_all_day_value(current_start)

                if title is not None:
                    self._write_text_property(component, "SUMMARY", _require_text(title, "title"))
                if description is not None:
                    self._write_text_property(component, "DESCRIPTION", description)
                if location is not None:
                    self._write_text_property(component, "LOCATION", location)

                if start_time is not None or end_time is not None or all_day is not None:
                    resolved_start = start_time if start_time is not None else current_start
                    resolved_end = end_time if end_time is not None else current_end
                    self._write_temporal_properties(component, resolved_start, resolved_end, is_all_day)

            saved = event.save()
            return self._normalize_event(saved, calendar)

    def _delete_event_sync(
        self,
        credentials: CalDAVCredentials,
        event_id: str,
        calendar_id: str | None,
    ) -> bool:
        with self._open_client(credentials) as client:
            event, _calendar = self._find_event(client, credentials, event_id, calendar_id)
            event.delete()
            return True

    @contextmanager
    def _open_client(self, credentials: CalDAVCredentials) -> Iterator[Any]:
        client = self._make_client(credentials)
        if hasattr(client, "__enter__") and hasattr(client, "__exit__"):
            with client as active_client:
                yield active_client
            return

        try:
            yield client
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _make_client(self, credentials: CalDAVCredentials) -> Any:
        if self._client_factory is not None:
            return self._client_factory(credentials)

        try:
            caldav_module = importlib.import_module("caldav")
        except ImportError as exc:
            raise CalDAVDependencyError(
                "CalDAV dependency missing. Install the 'caldav' package to enable CalDAV calendars."
            ) from exc

        dav_client = getattr(caldav_module, "DAVClient", None)
        if dav_client is None:
            davclient_module = importlib.import_module("caldav.davclient")
            dav_client = getattr(davclient_module, "DAVClient", None)
        if dav_client is None:
            raise CalDAVDependencyError("Could not resolve caldav.DAVClient.")

        kwargs: dict[str, object] = {
            "url": credentials.url,
            "username": credentials.username,
            "password": credentials.password,
            "ssl_verify_cert": credentials.verify_ssl,
        }
        if credentials.timeout_seconds is not None:
            kwargs["timeout"] = credentials.timeout_seconds
        if credentials.features:
            kwargs["features"] = credentials.features
        return dav_client(**kwargs)

    def _get_principal(self, client: Any) -> Any:
        if hasattr(client, "get_principal"):
            return client.get_principal()
        if hasattr(client, "principal"):
            return client.principal()
        raise CalDAVProviderError("CalDAV client does not expose a principal getter.")

    def _get_calendars(self, client: Any) -> list[Any]:
        principal = self._get_principal(client)
        if hasattr(principal, "get_calendars"):
            calendars = principal.get_calendars()
        elif hasattr(principal, "calendars"):
            calendars = principal.calendars()
        else:
            raise CalDAVProviderError("CalDAV principal does not expose calendars.")
        return list(calendars or [])

    def _resolve_calendar(self, client: Any, credentials: CalDAVCredentials, calendar_id: str | None) -> Any:
        calendars = self._resolve_calendars(client, credentials, calendar_id, prefer_default=True)
        if not calendars:
            raise CalDAVProviderError("No CalDAV calendars available.")
        return calendars[0]

    def _resolve_calendars(
        self,
        client: Any,
        credentials: CalDAVCredentials,
        calendar_id: str | None,
        *,
        prefer_default: bool,
    ) -> list[Any]:
        calendars = self._get_calendars(client)
        if not calendars:
            return []

        target = calendar_id or credentials.calendar_id or credentials.calendar_name
        if target:
            matched = [calendar for calendar in calendars if self._calendar_matches(calendar, target)]
            if matched:
                return matched
            if calendar_id:
                raise CalDAVProviderError("CalDAV calendar '%s' was not found." % calendar_id)

        return [calendars[0]] if prefer_default else calendars

    def _calendar_matches(self, calendar: Any, target: str | None) -> bool:
        if not target:
            return False
        normalized_target = target.strip()
        if not normalized_target:
            return False
        return normalized_target in {
            self._calendar_identifier(calendar),
            self._calendar_name(calendar),
        }

    def _calendar_identifier(self, calendar: Any, fallback_index: int | None = None) -> str:
        url = getattr(calendar, "url", None)
        if url is not None:
            return str(url)
        if fallback_index is not None:
            return "caldav-calendar-%d" % fallback_index
        return "caldav-calendar"

    def _calendar_name(self, calendar: Any, default: str | None = None) -> str:
        if hasattr(calendar, "get_display_name"):
            name = calendar.get_display_name()
            if name:
                return str(name)
        name = getattr(calendar, "name", None)
        if name:
            return str(name)
        return default or self._calendar_identifier(calendar)

    def _search_calendar_events(
        self,
        calendar: Any,
        start_date: datetime | None,
        end_date: datetime | None,
    ) -> list[Any]:
        if hasattr(calendar, "search"):
            kwargs: dict[str, object] = {"event": True}
            if start_date is not None:
                kwargs["start"] = self._normalize_datetime_value(start_date)
            if end_date is not None:
                kwargs["end"] = self._normalize_datetime_value(end_date)
            if start_date is not None and end_date is not None:
                kwargs["expand"] = True
            return list(calendar.search(**kwargs) or [])

        if hasattr(calendar, "date_search"):
            start = self._normalize_datetime_value(start_date or (datetime.now(UTC) - timedelta(days=3650)))
            end = self._normalize_datetime_value(end_date) if end_date is not None else None
            return list(calendar.date_search(start=start, end=end, expand=bool(end)) or [])

        raise CalDAVProviderError("CalDAV calendar does not support event search.")

    def _find_event(
        self,
        client: Any,
        credentials: CalDAVCredentials,
        event_id: str,
        calendar_id: str | None,
    ) -> tuple[Any, Any]:
        calendars = self._resolve_calendars(client, credentials, calendar_id, prefer_default=False)
        for calendar in calendars:
            found = self._fetch_event_from_calendar(calendar, event_id)
            if found is not None:
                return found, calendar
        raise CalDAVEventNotFoundError("CalDAV event '%s' was not found." % event_id)

    def _fetch_event_from_calendar(self, calendar: Any, event_id: str) -> Any | None:
        if hasattr(calendar, "get_object_by_uid"):
            try:
                return calendar.get_object_by_uid(event_id)
            except Exception as exc:
                if not self._is_not_found_error(exc):
                    raise

        if hasattr(calendar, "search"):
            matches = list(calendar.search(uid=event_id, event=True, post_filter=True) or [])
            for event in matches:
                if self._event_identifier(event) == event_id:
                    return event
            if matches:
                return matches[0]
        return None

    def _save_event(self, calendar: Any, ical_payload: str) -> Any | None:
        if hasattr(calendar, "save_event"):
            return calendar.save_event(ical_payload)
        if hasattr(calendar, "add_event"):
            return calendar.add_event(ical_payload)
        raise CalDAVProviderError("CalDAV calendar does not support event creation.")

    @contextmanager
    def _edit_event_component(self, event: Any) -> Iterator[Any]:
        if hasattr(event, "edit_icalendar_instance"):
            with event.edit_icalendar_instance() as calendar_data:
                yield self._select_vevent_component(calendar_data)
            return
        yield self._event_component(event)

    def _normalize_event(self, event: Any, calendar: Any) -> dict[str, object]:
        component = self._event_component(event)
        start_raw = self._decode_component_value(component, "DTSTART")
        end_raw = self._decode_component_value(component, "DTEND")
        event_id = self._event_identifier(event)
        normalized = normalize_event(
            provider=self.provider_id,
            event_id=event_id,
            title=_text_value(component.get("SUMMARY")) or "Untitled Event",
            description=_text_value(component.get("DESCRIPTION")),
            location=_text_value(component.get("LOCATION")),
            start_time=self._normalize_datetime_value(start_raw),
            end_time=self._normalize_datetime_value(end_raw),
            calendar_id=self._calendar_identifier(calendar),
            url=_optional_str(getattr(event, "url", None)),
            all_day=self._is_all_day_value(start_raw),
            display_timezone=self._datetime_utils.get_user_display_timezone(),
            raw={"calendar_name": self._calendar_name(calendar)},
        )
        normalized["id"] = event_id
        normalized["calendar_name"] = self._calendar_name(calendar)
        normalized["source"] = self.provider_id
        return normalized

    def _event_component(self, event: Any) -> Any:
        component = getattr(event, "component", None)
        if component is not None:
            return component
        if hasattr(event, "get_icalendar_instance"):
            return self._select_vevent_component(event.get_icalendar_instance())
        raise CalDAVProviderError("CalDAV event does not expose an iCalendar component.")

    def _event_identifier(self, event: Any) -> str:
        event_id = getattr(event, "id", None)
        if event_id:
            return str(event_id)

        component = self._event_component(event)
        uid = component.get("UID")
        if uid:
            return str(uid)
        raise CalDAVProviderError("CalDAV event is missing a UID.")

    @staticmethod
    def _select_vevent_component(calendar_data: Any) -> Any:
        for component in getattr(calendar_data, "subcomponents", []):
            if getattr(component, "name", "").upper() == "VEVENT":
                return component
        raise CalDAVProviderError("iCalendar payload does not contain a VEVENT.")

    @staticmethod
    def _is_not_found_error(exc: Exception) -> bool:
        return exc.__class__.__name__ == "NotFoundError"

    def _build_icalendar_payload(
        self,
        *,
        event_id: str,
        title: str,
        start_time: datetime,
        end_time: datetime | None,
        description: str | None,
        location: str | None,
        all_day: bool,
        attendees: list[str] | None = None,
    ) -> str:
        dtstamp = datetime.now(UTC)
        start_value, end_value = self._resolve_temporal_values(start_time, end_time, all_day)
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:%s" % _PRODID,
            "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            "UID:%s" % event_id,
            "DTSTAMP:%s" % self._format_datetime_utc(dtstamp),
            "SUMMARY:%s" % _escape_ical_text(title),
        ]

        if all_day:
            lines.append("DTSTART;VALUE=DATE:%s" % start_value.strftime("%Y%m%d"))
            lines.append("DTEND;VALUE=DATE:%s" % end_value.strftime("%Y%m%d"))
        else:
            lines.append("DTSTART:%s" % self._format_datetime_utc(start_value))
            lines.append("DTEND:%s" % self._format_datetime_utc(end_value))

        if description:
            lines.append("DESCRIPTION:%s" % _escape_ical_text(description))
        if location:
            lines.append("LOCATION:%s" % _escape_ical_text(location))
        for attendee in attendees or []:
            value = str(attendee or "").strip()
            if not value:
                continue
            if "@" in value and ":" not in value:
                value = "mailto:%s" % value
            lines.append("ATTENDEE:%s" % _escape_ical_text(value))

        lines.extend(["END:VEVENT", "END:VCALENDAR"])
        return "\r\n".join(lines) + "\r\n"

    def _write_temporal_properties(
        self,
        component: Any,
        start_time: datetime | date | None,
        end_time: datetime | date | None,
        all_day: bool,
    ) -> None:
        if start_time is None:
            raise CalDAVProviderError("CalDAV event update requires a start time.")

        start_value, end_value = self._resolve_temporal_values(start_time, end_time, all_day)
        self._delete_property(component, "DTSTART")
        self._delete_property(component, "DTEND")
        component.add("dtstart", start_value)
        component.add("dtend", end_value)

    @staticmethod
    def _write_text_property(component: Any, key: str, value: str) -> None:
        cleaned = value.strip()
        if not cleaned:
            CalDAVCalendarProvider._delete_property(component, key)
            return
        component[key] = cleaned

    @staticmethod
    def _delete_property(component: Any, key: str) -> None:
        try:
            del component[key]
        except Exception:
            try:
                component.pop(key, None)
            except Exception:
                pass

    def _decode_component_value(self, component: Any, key: str) -> datetime | date | None:
        if hasattr(component, "decoded"):
            try:
                return component.decoded(key)
            except Exception:
                return None
        return component.get(key)

    def _resolve_temporal_values(
        self,
        start_time: datetime | date,
        end_time: datetime | date | None,
        all_day: bool,
    ) -> tuple[datetime | date, datetime | date]:
        if all_day:
            start_date = self._coerce_date_value(start_time)
            end_date = (
                self._coerce_date_value(end_time) if end_time is not None else start_date + _DEFAULT_ALL_DAY_DURATION
            )
            if end_date <= start_date:
                end_date = start_date + _DEFAULT_ALL_DAY_DURATION
            return start_date, end_date

        start_dt = self._coerce_datetime_value(start_time)
        end_dt = self._coerce_datetime_value(end_time) if end_time is not None else start_dt + _DEFAULT_EVENT_DURATION
        if end_dt < start_dt:
            raise CalDAVProviderError("CalDAV event end time must be after start time.")
        return start_dt, end_dt

    def _coerce_datetime_value(self, value: datetime | date) -> datetime:
        if isinstance(value, datetime):
            return self._normalize_datetime_value(value)
        return datetime.combine(value, time.min, tzinfo=UTC)

    @staticmethod
    def _coerce_date_value(value: datetime | date) -> date:
        if isinstance(value, datetime):
            return value.date()
        return value

    def _normalize_datetime_value(self, value: datetime | date | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return self._datetime_utils.normalise_datetime(value)
        return datetime.combine(value, time.min, tzinfo=UTC)

    @staticmethod
    def _is_all_day_value(value: object) -> bool:
        return isinstance(value, date) and not isinstance(value, datetime)

    @staticmethod
    def _format_datetime_utc(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _event_sort_key(event: dict[str, object]) -> datetime:
        start_time = event.get("start_time")
        if isinstance(start_time, datetime):
            return start_time
        return datetime.max.replace(tzinfo=UTC)

    def _first_free_slot(
        self,
        events: list[dict[str, object]],
        *,
        start_dt: datetime,
        end_dt: datetime,
        duration_minutes: int,
    ) -> tuple[datetime, datetime] | None:
        cursor = start_dt
        duration = timedelta(minutes=max(duration_minutes, 1))
        for event in sorted(events, key=self._event_sort_key):
            event_start = event.get("start_time")
            event_end = event.get("end_time")
            if not isinstance(event_start, datetime):
                continue
            if cursor + duration <= event_start:
                return cursor, cursor + duration
            if isinstance(event_end, datetime) and event_end > cursor:
                cursor = event_end
        if cursor + duration <= end_dt:
            return cursor, cursor + duration
        return None

    @staticmethod
    def _default_end_time(start_time: datetime, all_day: bool) -> datetime:
        if all_day:
            return CalendarDateTimeUtils.normalise_datetime(start_time) + _DEFAULT_ALL_DAY_DURATION
        return CalendarDateTimeUtils.normalise_datetime(start_time) + _DEFAULT_EVENT_DURATION


def _escape_ical_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("\r\n", "\n").replace("\r", "\n")
    escaped = escaped.replace("\n", "\\n")
    escaped = escaped.replace(";", "\\;").replace(",", "\\,")
    return escaped


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CalDAVProviderError("%s is required." % field_name)
    return text


def _text_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
