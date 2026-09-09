"""
services/calendar/manager.py

Canonical calendar facade for Viola.

This module exposes one provider-agnostic manager with an always-on local
primary calendar. Google, Microsoft Graph, and CalDAV remain optional sync
targets without changing the public calendar surface.
"""

from __future__ import annotations

import datetime
import importlib
import threading
from dataclasses import dataclass
from typing import Any, Protocol, cast

from config.settings import settings
from core.logging_config import get_logger
from services.calendar.datetime_utils import CalendarDateTimeUtils
from services.calendar.fallback import CalendarFallbackStorage
from services.calendar.providers import GoogleCalendarProvider, LocalCalendarProvider
from services.calendar.providers.base import CalendarProvider, normalize_event


class _DebugEventEmitter(Protocol):
    def __call__(self, name: str, payload: dict[str, object], *, source: str) -> None: ...


try:
    from ui.qt_native.debug_events import (
        emit_debug_event as _emit_calendar_debug_event_raw,
    )
except Exception:  # pragma: no cover - backend without Qt debug bus
    _emit_calendar_debug_event: _DebugEventEmitter | None = None
else:
    _emit_calendar_debug_event = cast(_DebugEventEmitter, _emit_calendar_debug_event_raw)


log = get_logger("viola.services.calendar")

_DEFAULT_PROVIDER_ORDER = ("local", "google", "graph", "caldav")


def _event_start_sort_key(event: dict[str, object]) -> datetime.datetime:
    value = event.get("start_time")
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
    return datetime.datetime.max.replace(tzinfo=datetime.UTC)


def _emit_backend_debug_event(name: str, payload: dict[str, object]) -> None:
    if _emit_calendar_debug_event is None:
        return
    try:
        _emit_calendar_debug_event(name, payload, source="backend")
    except Exception:
        log.exception("Debug event emit failed for %s", name)


def _format_display_datetime(
    datetime_utils: CalendarDateTimeUtils,
    value: datetime.datetime,
    timezone_name: str | None,
) -> str:
    display_value = datetime_utils.to_display_timezone(value, timezone_name)
    date_part = display_value.strftime("%B %d, %Y").replace(" 0", " ")
    time_part = display_value.strftime("%I:%M %p").lstrip("0")
    timezone_part = display_value.strftime("%Z")
    suffix = " %s" % timezone_part if timezone_part else ""
    return "%s at %s%s" % (date_part, time_part, suffix)


def _explicit_remote_write_provider(selector: str) -> str | None:
    normalized = (selector or "").strip().lower()
    if normalized and normalized not in {"auto", "all", "local"} and normalized in _DEFAULT_PROVIDER_ORDER:
        return normalized
    return None


@dataclass
class CalendarEvent:
    """Represents a calendar event."""

    title: str
    start_time: datetime.datetime
    end_time: datetime.datetime | None = None
    description: str | None = None
    location: str | None = None
    event_id: str | None = None
    source: str = "unknown"


class CalendarManager:
    """Provider-agnostic calendar manager."""

    def __init__(self, user_id: str | None = None):
        _ = user_id
        # Deployment default only. ``CalendarManager`` is a process singleton,
        # so a timezone captured here is the SERVER's zone (UTC in a cloud
        # container) and is wrong for every user who does not live in it --
        # #3557. Reads go through the ``calendar_timezone`` property below,
        # which prefers the zone bound for the current request's user.
        self._default_calendar_timezone: str = settings.calendar_timezone
        self._datetime_utils = CalendarDateTimeUtils()
        self._fallback_storage = CalendarFallbackStorage()
        self._fallback_lock = threading.Lock()
        self._fallback_store_path = self._fallback_storage.store_path
        # Legacy compatibility attributes used in some tests / old call sites.
        self.google_service: Any | None = None
        self.icloud_service: object | None = None

        self._providers: dict[str, CalendarProvider] = {}
        self._register_builtin_providers()

    @property
    def calendar_timezone(self) -> str:
        """Display timezone for the user of the current request (#3557)."""
        from services.user_timezone import active_timezone_name

        return active_timezone_name() or self._default_calendar_timezone

    @calendar_timezone.setter
    def calendar_timezone(self, value: str) -> None:
        self._default_calendar_timezone = value

    @staticmethod
    def _require_user_id(user_id: str | None) -> str:
        if not user_id:
            raise ValueError("user_id is required")
        return user_id

    def _register_builtin_providers(self) -> None:
        self._providers["local"] = LocalCalendarProvider(datetime_utils=self._datetime_utils)
        self._providers["google"] = GoogleCalendarProvider(self._build_google_service, self._datetime_utils)
        self._register_optional_provider("services.calendar.providers.graph", "GraphCalendarProvider")
        self._register_optional_provider("services.calendar.providers.caldav", "CalDAVCalendarProvider")

    def _register_optional_provider(self, module_name: str, class_name: str) -> None:
        try:
            module = importlib.import_module(module_name)
            provider_cls = getattr(module, class_name)
        except Exception:
            return

        try:
            init = getattr(provider_cls, "__init__", None)
            kwargs: dict[str, object] = {}
            if init is not None and hasattr(init, "__code__"):
                arg_names = set(init.__code__.co_varnames[: init.__code__.co_argcount])
                if "datetime_utils" in arg_names:
                    kwargs["datetime_utils"] = self._datetime_utils
            provider = provider_cls(**kwargs)
        except Exception:
            log.exception("Failed to initialize optional calendar provider %s", module_name)
            return

        provider_id = getattr(provider, "provider_id", "")
        if isinstance(provider_id, str) and provider_id:
            self._providers[provider_id] = cast(CalendarProvider, provider)

    async def _build_google_service(self, user_id: str) -> Any | None:
        if self.google_service is not None:
            return self.google_service
        try:
            from services.oauth.credentials import get_google_credentials

            creds = await get_google_credentials(user_id)
            if creds is None:
                log.debug("No Google credentials for user %s", user_id)
                return None

            discovery = importlib.import_module("googleapiclient.discovery")
            build = discovery.build
            service = build(
                "calendar",
                "v3",
                credentials=creds,
                static_discovery=True,
            )
            return service
        except ImportError:
            log.warning("Google Calendar libraries not installed: pip install google-api-python-client google-auth")
            return None
        except Exception:
            log.exception("Failed to initialize Google Calendar for user %s", user_id)
            return None

    async def connect_caldav_account(
        self,
        *,
        user_id: str,
        url: str,
        username: str,
        password: str,
        calendar_id: str | None = None,
        calendar_name: str | None = None,
        verify_ssl: bool = True,
        features: str | None = None,
    ) -> dict[str, object]:
        """Store a user-supplied external CalDAV account and verify it connects.

        This is the generic "bring your own CalDAV server" path (iCloud,
        Fastmail, a self-hosted Radicale/Nextcloud, etc.) — distinct from the
        Viola-hosted Radicale auto-provisioning in ``cloud_caldav.py``, which
        never asks the user for credentials because it mints its own. Here the
        credentials are the USER'S OWN third-party secret, so callers must
        refuse this path entirely on deployments where
        ``services.calendar.cloud_caldav.cloud_caldav_configured()`` is true
        (Tier-3 external-account secrets never live in the cloud store — see
        CLAUDE.md's three-tier storage rule); this method itself does not
        re-check that, so it stays reusable from both HTTP and agent-tool
        callers that already gate on it.
        """
        uid = self._require_user_id(user_id)
        provider = self._providers.get("caldav")
        if provider is None:
            return {
                "ok": False,
                "error": "caldav_unavailable",
                "message": "CalDAV support isn't available on this build.",
            }

        from services.calendar.providers.caldav import (
            CalDAVCredentials,
            CalDAVProviderError,
        )

        credentials = CalDAVCredentials(
            url=url,
            username=username,
            password=password,
            calendar_id=calendar_id,
            calendar_name=calendar_name,
            verify_ssl=verify_ssl,
            features=features,
        )
        try:
            await provider.store_credentials(uid, credentials)
        except CalDAVProviderError as exc:
            return {"ok": False, "error": "caldav_credentials_invalid", "message": str(exc)}

        # Verify the account actually connects before reporting success — a
        # stored-but-unreachable credential is worse than no credential at
        # all, since is_configured() would then report "connected" for an
        # account that can never sync (the empty-cart failure mode CLAUDE.md
        # warns about: don't report success on an unverified side effect).
        try:
            calendars = await provider.list_calendars(uid)
        except Exception:
            log.exception("CalDAV connect verification failed for user %s", uid)
            calendars = []

        if not calendars:
            await provider.delete_credentials(uid)
            return {
                "ok": False,
                "error": "caldav_connect_failed",
                "message": (
                    "Couldn't connect to that CalDAV account. Check the server address, "
                    "username, and password (iCloud requires an app-specific password, "
                    "not your Apple ID password)."
                ),
            }

        return {
            "ok": True,
            "provider": "caldav",
            "calendars": calendars,
            "message": "Connected %d calendar(s)." % len(calendars),
        }

    async def disconnect_caldav_account(self, *, user_id: str) -> dict[str, object]:
        """Delete a user's stored external CalDAV credentials, if any."""
        uid = self._require_user_id(user_id)
        provider = self._providers.get("caldav")
        if provider is None:
            return {"ok": False, "error": "caldav_unavailable", "message": "CalDAV support isn't available."}

        deleted = await provider.delete_credentials(uid)
        if deleted:
            return {"ok": True, "message": "CalDAV account disconnected."}
        return {"ok": False, "error": "caldav_not_connected", "message": "No CalDAV account was connected."}

    async def list_providers(self, user_id: str) -> list[dict[str, object]]:
        uid = self._require_user_id(user_id)
        items: list[dict[str, object]] = []
        for provider_id in _DEFAULT_PROVIDER_ORDER:
            provider = self._providers.get(provider_id)
            if provider is None:
                continue
            configured = await provider.is_configured(uid)
            items.append({"provider": provider_id, "configured": configured})
        return items

    async def list_calendars(
        self,
        *,
        user_id: str,
        provider: str = "all",
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)
        connected_providers = await self._resolve_provider_ids(uid, provider, write=False)
        calendars: list[dict[str, object]] = []
        for provider_id in connected_providers:
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            calendars.extend(await backend.list_calendars(uid))
        return {
            "ok": True,
            "calendars": calendars,
            "count": len(calendars),
            "source": provider,
            "calendars_connected": bool(connected_providers),
            "connected_providers": connected_providers,
        }

    async def add_event(
        self,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar: str = "auto",
        user_id: str | None = None,
        all_day: bool = False,
        calendar_id: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)
        await self._replay_fallback_events_if_possible(uid)
        requested_remote_provider = _explicit_remote_write_provider(calendar)

        if end_time is None:
            end_time = start_time if all_day else start_time + datetime.timedelta(hours=1)

        if not all_day and end_time < start_time:
            return {
                "ok": False,
                "error": "invalid_date_range",
                "message": "Event end time must be after start time.",
            }

        start_dt = self._datetime_utils.normalise_datetime(start_time)
        end_dt = self._datetime_utils.normalise_datetime(end_time)

        created_events: list[tuple[str, dict[str, object]]] = []
        failed_providers: list[str] = []
        # SINGLE-PERSIST contract: a create writes to exactly ONE provider — the
        # first configured write target that accepts it. Targets are ordered
        # system-of-record-first (a connected remote) then the always-on local
        # fallback, so a Viola-created event lands in the connected calendar when
        # one exists and in local otherwise. Writing to BOTH local and a remote
        # (the pre-2026-07-11 mirror-write) persisted the same logical event twice
        # with no correlation key, so get_events surfaced it twice — the calendar
        # double-write defect (#1019). Stop at the first successful provider.
        for provider_id in await self._resolve_write_targets(uid, calendar):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            try:
                created = await backend.create_event(
                    uid,
                    title=title,
                    start_time=start_dt,
                    end_time=end_dt,
                    description=description,
                    location=location,
                    calendar_id=calendar_id,
                    all_day=all_day,
                    attendees=attendees,
                )
            except Exception:
                log.exception("Calendar provider %s failed to create event for user %s", provider_id, uid)
                failed_providers.append(provider_id)
                continue
            if created:
                created_events.append((provider_id, created))
                break
            failed_providers.append(provider_id)

        if created_events:
            primary_provider, primary_event = created_events[0]
            synced_providers = [provider_id for provider_id, _event in created_events]
            if requested_remote_provider is not None and requested_remote_provider not in synced_providers:
                if requested_remote_provider not in failed_providers:
                    failed_providers.append(requested_remote_provider)
                retry_record = self._fallback_storage.store_fallback_event(
                    user_id=uid,
                    title=title,
                    start_time=start_dt.isoformat(),
                    end_time=end_dt.isoformat(),
                    description=description,
                    location=location,
                    provider=requested_remote_provider,
                    calendar_id=calendar_id,
                    all_day=all_day,
                )
                display_local = _format_display_datetime(self._datetime_utils, start_dt, self.calendar_timezone)
                stored_iso_utc = start_dt.astimezone(datetime.UTC).isoformat()
                primary_event["display_local"] = display_local
                primary_event["stored_iso_utc"] = stored_iso_utc
                return {
                    "ok": False,
                    "error": {
                        "code": "calendar_remote_sync_failed",
                        "message": (
                            "Saved locally, but couldn't sync to %s. " "Reconnect that calendar provider and retry."
                        )
                        % requested_remote_provider.capitalize(),
                    },
                    "provider": primary_provider,
                    "event_id": primary_event.get("event_id"),
                    "local_event_id": primary_event.get("event_id"),
                    "pending_sync_id": retry_record.get("id"),
                    "stored_iso_utc": stored_iso_utc,
                    "display_local": display_local,
                    "event": primary_event,
                    "synced_providers": synced_providers,
                    "sync_failures": failed_providers,
                    "local_primary": primary_provider == "local",
                    "pending_sync": True,
                }
            display_local = _format_display_datetime(self._datetime_utils, start_dt, self.calendar_timezone)
            stored_iso_utc = start_dt.astimezone(datetime.UTC).isoformat()
            primary_event["display_local"] = display_local
            primary_event["stored_iso_utc"] = stored_iso_utc
            return {
                "ok": True,
                "provider": primary_provider,
                "event_id": primary_event.get("event_id"),
                "message": "Added '%s' to %s on %s"
                % (
                    title,
                    primary_provider.capitalize(),
                    display_local,
                ),
                "stored_iso_utc": stored_iso_utc,
                "display_local": display_local,
                "event": primary_event,
                "synced_providers": synced_providers,
                "sync_failures": failed_providers,
                "local_primary": primary_provider == "local",
            }

        log.warning("All calendar providers failed, using local fallback")
        return await self._add_local_event_fallback(
            uid,
            title,
            start_dt,
            end_dt,
            description,
            location,
        )

    async def get_events(
        self,
        start_date: datetime.datetime | None = None,
        end_date: datetime.datetime | None = None,
        max_results: int = 10,
        calendar: str = "all",
        user_id: str | None = None,
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)
        await self._replay_fallback_events_if_possible(uid)

        if start_date is None:
            start_date = datetime.datetime.now(datetime.UTC)
        if end_date is None:
            end_date = start_date + datetime.timedelta(days=30)

        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)

        connected_providers = await self._resolve_provider_ids(uid, calendar, write=False)

        all_events: list[dict[str, object]] = []
        for provider_id in connected_providers:
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            all_events.extend(
                await backend.list_events(
                    uid,
                    start_date=start_dt,
                    end_date=end_dt,
                    max_results=max_results,
                    calendar_id=calendar_id,
                )
            )

        all_events.extend(self._get_fallback_events_in_range(uid, start_dt, end_dt))
        all_events.sort(key=_event_start_sort_key)

        return {
            "ok": True,
            "events": all_events[:max_results],
            "source": calendar,
            "count": len(all_events),
            "calendars_connected": bool(connected_providers),
            "connected_providers": connected_providers,
        }

    async def get_event(
        self,
        *,
        event_id: str,
        user_id: str | None,
        calendar: str = "auto",
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)

        for provider_id in await self._resolve_provider_ids(uid, calendar, write=False):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            event = await backend.get_event(uid, event_id=event_id, calendar_id=calendar_id)
            if event:
                return {"ok": True, "event": event, "provider": provider_id}

        return {"ok": False, "error": "event_not_found", "message": "Couldn't find that calendar event."}

    async def delete_event(
        self,
        event_id: str,
        calendar: str = "auto",
        user_id: str | None = None,
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)

        for provider_id in await self._resolve_provider_ids(uid, calendar, write=True):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            deleted = await backend.delete_event(uid, event_id=event_id, calendar_id=calendar_id)
            if deleted:
                return {"ok": True, "provider": provider_id, "message": "Event deleted from %s" % provider_id}

        if self._fallback_storage.delete_fallback_event(event_id, user_id=uid):
            return {"ok": True, "provider": "fallback", "message": "Deleted fallback event"}

        return {
            "ok": False,
            "error": "calendar_delete_failed",
            "message": "Failed to delete event. Please try again.",
        }

    async def update_event(
        self,
        event_id: str,
        title: str | None = None,
        start_time: datetime.datetime | None = None,
        end_time: datetime.datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar: str = "auto",
        user_id: str | None = None,
        all_day: bool = False,
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)
        if not all_day and start_time is not None and end_time is not None and end_time < start_time:
            return {
                "ok": False,
                "error": "invalid_date_range",
                "message": "Event end time must be after start time.",
            }

        normalized_start = self._datetime_utils.normalise_datetime(start_time) if start_time else None
        normalized_end = self._datetime_utils.normalise_datetime(end_time) if end_time else None

        for provider_id in await self._resolve_provider_ids(uid, calendar, write=True):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            updated = await backend.update_event(
                uid,
                event_id=event_id,
                title=title,
                start_time=normalized_start,
                end_time=normalized_end,
                description=description,
                location=location,
                calendar_id=calendar_id,
                all_day=all_day,
            )
            if updated:
                return {
                    "ok": True,
                    "provider": provider_id,
                    "event_id": event_id,
                    "message": "Event updated successfully",
                    "event": updated,
                }

        return {
            "ok": False,
            "error": "calendar_update_failed",
            "message": "Failed to update calendar event.",
        }

    async def respond_to_event(
        self,
        *,
        event_id: str,
        response_status: str,
        user_id: str | None,
        calendar: str = "auto",
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)

        for provider_id in await self._resolve_provider_ids(uid, calendar, write=True):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            updated = await backend.respond_to_event(
                uid,
                event_id=event_id,
                response_status=response_status,
                calendar_id=calendar_id,
            )
            if updated:
                return {
                    "ok": True,
                    "provider": provider_id,
                    "event": updated,
                    "message": "Updated event response to %s" % response_status,
                }

        return {
            "ok": False,
            "error": "calendar_respond_failed",
            "message": "Couldn't respond to that event invitation.",
        }

    async def find_free_time(
        self,
        *,
        attendees: list[str],
        start_date: datetime.datetime,
        end_date: datetime.datetime,
        duration_minutes: int,
        user_id: str | None,
        calendar: str = "auto",
        calendar_id: str | None = None,
    ) -> dict[str, object]:
        uid = self._require_user_id(user_id)
        start_dt = self._datetime_utils.normalise_datetime(start_date)
        end_dt = self._datetime_utils.normalise_datetime(end_date)

        for provider_id in await self._resolve_provider_ids(uid, calendar, write=False):
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            result = await backend.find_free_time(
                uid,
                attendees=attendees,
                start_date=start_dt,
                end_date=end_dt,
                duration_minutes=duration_minutes,
                calendar_id=calendar_id,
            )
            if result:
                return {"ok": True, "provider": provider_id, **result}

        return {
            "ok": False,
            "error": "calendar_free_time_failed",
            "message": "Couldn't find availability right now.",
        }

    async def _resolve_provider_ids(self, user_id: str, selector: str, *, write: bool) -> list[str]:
        normalized = (selector or "auto").strip().lower()
        if normalized in {"all"}:
            chosen = []
            for provider_id in _DEFAULT_PROVIDER_ORDER:
                backend = self._providers.get(provider_id)
                if backend and await backend.is_configured(user_id):
                    chosen.append(provider_id)
            return chosen
        if normalized in self._providers:
            backend = self._providers[normalized]
            if write and normalized != "local":
                chosen = []
                local = self._providers.get("local")
                if local and await local.is_configured(user_id):
                    chosen.append("local")
                if await backend.is_configured(user_id):
                    chosen.append(normalized)
                return chosen
            return [normalized] if await backend.is_configured(user_id) else []

        # auto/default ordering
        ordered = []
        for provider_id in _DEFAULT_PROVIDER_ORDER:
            backend = self._providers.get(provider_id)
            if backend is None:
                continue
            if await backend.is_configured(user_id):
                ordered.append(provider_id)
        return ordered

    async def _resolve_write_targets(self, user_id: str, selector: str) -> list[str]:
        """Ordered create targets for the SINGLE-persist write contract.

        A create persists to exactly one provider: the first entry here that
        accepts the write. The list is ordered system-of-record-first — configured
        remotes (google/graph/caldav) ahead of the always-on ``local`` store — so a
        Viola-created event lands in the connected calendar when one exists and in
        local otherwise, and ``local`` is the guaranteed fallback that also catches
        a failed explicit-remote write (which is then queued for retry by the
        caller). This supersedes the earlier local+remote mirror-write: that fan-out
        wrote the same event to two providers with no correlation key and no
        read-side reconciliation, so ``get_events`` surfaced it twice (#1019).
        """
        normalized = (selector or "auto").strip().lower()
        local_id = "local"
        local = self._providers.get(local_id)
        local_ok = bool(local and await local.is_configured(user_id))

        # Explicit single-provider request (not the "all" fan-out selector).
        if normalized in self._providers and normalized != "all":
            if normalized == local_id:
                return [local_id] if local_ok else []
            backend = self._providers[normalized]
            targets: list[str] = []
            if await backend.is_configured(user_id):
                targets.append(normalized)
            if local_ok:
                # Fallback catches a failed remote write; add_event then queues the
                # requested remote for retry via the calendar_remote_sync_failed path.
                targets.append(local_id)
            return targets

        # auto / all / unknown: remotes first (system-of-record), local last (fallback).
        targets = []
        for provider_id in _DEFAULT_PROVIDER_ORDER:
            if provider_id == local_id:
                continue
            backend = self._providers.get(provider_id)
            if backend is not None and await backend.is_configured(user_id):
                targets.append(provider_id)
        if local_ok:
            targets.append(local_id)
        return targets

    async def _add_local_event_fallback(
        self,
        user_id: str,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        description: str | None,
        location: str | None,
    ) -> dict[str, object]:
        log.info("Local calendar fallback: Event '%s' at %s", title, start_time.strftime("%Y-%m-%d %H:%M"))
        log.warning("Calendar providers unavailable - event stored locally")

        record = self._store_fallback_event(
            user_id,
            title,
            start_time,
            end_time,
            description,
            location,
        )
        display_start = self._datetime_utils.normalise_datetime(start_time)
        display_local = _format_display_datetime(self._datetime_utils, display_start, self.calendar_timezone)
        stored_iso_utc = display_start.astimezone(datetime.UTC).isoformat()
        message = "Note: '%s' remembered for %s (local storage - will retry when provider reconnects)" % (
            title,
            display_local,
        )
        return {
            "ok": True,
            "event_id": record["id"],
            "message": message,
            "stored_iso_utc": stored_iso_utc,
            "display_local": display_local,
            "fallback": True,
            "pending_sync": True,
            "storage_path": str(self._fallback_store_path),
        }

    def _load_fallback_events(self, user_id: str) -> list[dict[str, object]]:
        return self._fallback_storage.load_fallback_events(user_id=user_id)

    def _persist_fallback_events(self, user_id: str, events: list[dict[str, object]]) -> None:
        self._fallback_storage.persist_fallback_events(events, user_id=user_id)

    def _normalise_datetime(self, value: datetime.datetime) -> datetime.datetime:
        return self._datetime_utils.normalise_datetime(value)

    def _store_fallback_event(
        self,
        user_id: str,
        title: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        description: str | None,
        location: str | None,
    ) -> dict[str, object]:
        record = self._fallback_storage.store_fallback_event(
            user_id=user_id,
            title=title,
            start_time=self._datetime_utils.normalise_datetime(start_time).isoformat(),
            end_time=self._datetime_utils.normalise_datetime(end_time).isoformat(),
            description=description,
            location=location,
            provider="fallback",
        )
        _emit_backend_debug_event(
            "calendar_fallback_saved",
            {
                "title": title,
                "start_time": record.get("start_time", ""),
                "timezone": self.calendar_timezone,
                "fallback_id": record.get("id", ""),
            },
        )
        return record

    def _parse_iso_datetime(self, value: str) -> datetime.datetime | None:
        return self._datetime_utils.parse_iso_datetime(value)

    def _get_fallback_events_in_range(
        self,
        user_id: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for record in self._load_fallback_events(user_id):
            start_iso = record.get("start_time") or record.get("start")
            end_iso = record.get("end_time") or record.get("end")
            if not isinstance(start_iso, str):
                continue
            record_start = self._parse_iso_datetime(start_iso)
            record_end = self._parse_iso_datetime(end_iso) if isinstance(end_iso, str) else None
            if record_start is None:
                continue
            record_start = self._datetime_utils.normalise_datetime(record_start)
            if record_end is not None:
                record_end = self._datetime_utils.normalise_datetime(record_end)
            if record_start > end_dt or (record_end and record_end < start_dt):
                continue
            events.append(
                normalize_event(
                    provider="fallback",
                    calendar_id="fallback",
                    event_id=str(record.get("id", "")),
                    title=str(record.get("title") or "Untitled Event"),
                    description=record.get("description"),
                    location=record.get("location"),
                    start_time=record_start,
                    end_time=record_end,
                    all_day=bool(record.get("all_day")),
                    display_timezone=self._datetime_utils.get_user_display_timezone(),
                    raw=record,
                )
            )
        return events

    async def _replay_fallback_events_if_possible(self, user_id: str) -> None:
        with self._fallback_lock:
            pending = list(self._load_fallback_events(user_id))

        if not pending:
            return

        succeeded: list[str] = []
        for record in pending:
            start_iso = record.get("start_time") or record.get("start")
            end_iso = record.get("end_time") or record.get("end")
            if not isinstance(start_iso, str) or not isinstance(end_iso, str):
                continue

            start_dt = self._datetime_utils.parse_iso_datetime(start_iso)
            end_dt = self._datetime_utils.parse_iso_datetime(end_iso)
            if not (start_dt and end_dt):
                continue

            target_providers = await self._fallback_replay_provider_ids(user_id, record)
            for provider_id in target_providers:
                backend = self._providers.get(provider_id)
                if backend is None:
                    continue
                calendar_id = record.get("calendar_id")
                created = await backend.create_event(
                    user_id,
                    title=str(record.get("title") or "(untitled)"),
                    start_time=self._datetime_utils.normalise_datetime(start_dt),
                    end_time=self._datetime_utils.normalise_datetime(end_dt),
                    description=str(record.get("description") or "") or None,
                    location=str(record.get("location") or "") or None,
                    calendar_id=calendar_id if isinstance(calendar_id, str) and calendar_id else None,
                    all_day=bool(record.get("all_day")),
                    attendees=None,
                )
                if created:
                    record_id = record.get("id")
                    if isinstance(record_id, str):
                        succeeded.append(record_id)
                    break

        if succeeded:
            remaining = [
                event
                for event in self._fallback_storage.load_fallback_events(user_id=user_id)
                if event.get("id") not in succeeded
            ]
            self._fallback_storage.persist_fallback_events(remaining, user_id=user_id)

        log.info("Replayed %d calendar fallback event(s) for user %s", len(succeeded), user_id)

    async def _fallback_replay_provider_ids(self, user_id: str, record: dict[str, object]) -> list[str]:
        provider_value = str(record.get("provider") or "").strip().lower()
        if provider_value and provider_value not in {"fallback", "local"}:
            backend = self._providers.get(provider_value)
            if backend is not None and await backend.is_configured(user_id):
                return [provider_value]
            return []

        configured = await self._resolve_provider_ids(user_id, "auto", write=True)
        remote = [provider_id for provider_id in configured if provider_id != "local"]
        return remote[:1] or configured[:1]


_calendar_manager: CalendarManager | None = None


def get_calendar_manager() -> CalendarManager:
    global _calendar_manager
    if _calendar_manager is None:
        _calendar_manager = CalendarManager()
    return _calendar_manager
