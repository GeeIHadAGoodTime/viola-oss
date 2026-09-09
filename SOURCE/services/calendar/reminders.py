"""Pre-event calendar reminders, scheduled through the existing SchedulerService.

Viola already has a general-purpose one-shot scheduler (``services/scheduler``)
and a "notify" structured action that speaks + pushes a notification at a
given time (respecting quiet hours via ``services/notifications/push_service``).
This module wires calendar events into that existing machinery instead of
building a second scheduling subsystem.

Each reminder is a one-shot scheduler row keyed by a deterministic label
(``calendar-reminder:<event_id>``). That lets every calendar mutation path
(add / update / delete / list) find and reconcile "its" reminder without a
new table: create it if missing, replace it if the event moved, remove it
if the event was canceled or the user turned reminders off.

Per CLAUDE.md's multi-tenant rule, every operation here is scoped by an
explicit ``user_id`` -- there is no global reminder state.
"""

from __future__ import annotations

import asyncio
import datetime
import threading
from typing import Any

from core.logging_config import get_logger
from services.scheduler.actions import encode_tool_action
from services.scheduler.service import get_scheduler_service
from ui.settings_manager import get_settings_manager

logger = get_logger(__name__)

_LABEL_PREFIX = "calendar-reminder:"
_DEFAULT_LEAD_MINUTES = 10
_MIN_LEAD_MINUTES = 1
_MAX_LEAD_MINUTES = 180
# When the ideal reminder moment has already passed but the event itself
# hasn't started yet (e.g. lead time is 10 minutes but the event is only
# 3 minutes away), fire a "starting soon" alert almost immediately instead
# of silently dropping the reminder.
_IMMEDIATE_FIRE_DELAY_SECONDS = 5
_INSTANT_TOLERANCE_SECONDS = 1.0


def _reminder_label(event_id: str) -> str:
    return _LABEL_PREFIX + event_id


def _reminder_enabled(user_id: str) -> bool:
    try:
        return bool(get_settings_manager().get("calendar_reminders_enabled", True, user_id=user_id))
    except Exception:  # noqa: BLE001, RUF100 -- settings read must fail open (default enabled)
        logger.debug("Could not read calendar_reminders_enabled for user=%s; defaulting to enabled", user_id)
        return True


def _reminder_lead_minutes(user_id: str) -> int:
    try:
        raw = get_settings_manager().get("calendar_reminder_lead_minutes", _DEFAULT_LEAD_MINUTES, user_id=user_id)
    except Exception:  # noqa: BLE001, RUF100 -- settings read must fail open (default lead)
        logger.debug("Could not read calendar_reminder_lead_minutes for user=%s; using default", user_id)
        return _DEFAULT_LEAD_MINUTES
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_LEAD_MINUTES
    return max(_MIN_LEAD_MINUTES, min(_MAX_LEAD_MINUTES, value))


def _event_id(event: dict[str, Any]) -> str | None:
    raw = event.get("event_id") or event.get("id")
    text = str(raw).strip() if raw else ""
    return text or None


def _event_start(event: dict[str, Any]) -> datetime.datetime | None:
    value = event.get("start_time") or event.get("start")
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _reminder_message(title: str, lead_minutes: int) -> str:
    unit = "minute" if lead_minutes == 1 else "minutes"
    return "Reminder: '%s' starts in %d %s." % (title, lead_minutes, unit)


def _parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _same_instant(iso_a: str | None, dt_b: datetime.datetime) -> bool:
    dt_a = _parse_iso(iso_a)
    if dt_a is None:
        return False
    return abs((dt_a - dt_b).total_seconds()) < _INSTANT_TOLERANCE_SECONDS


async def _cancel_by_label(user_id: str, label: str) -> None:
    scheduler = get_scheduler_service()
    existing = await scheduler.get_by_label_async(user_id, label)
    if existing is not None:
        await scheduler.delete_async(user_id, existing.id)


async def sync_event_reminder(user_id: str, event: dict[str, Any]) -> None:
    """Create, update, or cancel the pre-event reminder for one calendar event.

    Safe to call on every add/update/list of a calendar event: it is a
    no-op if reminders are off, the event has no id, the event is all-day
    (no single pre-event moment applies), or the event has already started.
    """
    if not user_id or not isinstance(event, dict):
        return

    event_id = _event_id(event)
    if event_id is None:
        logger.debug("Skipping reminder sync for event without an id: %r", event.get("title"))
        return

    label = _reminder_label(event_id)

    if not _reminder_enabled(user_id):
        await _cancel_by_label(user_id, label)
        return

    if bool(event.get("all_day")):
        # All-day events have no specific pre-event moment to alert ahead
        # of; skip rather than guess a time. Known gap -- see reminders
        # module docstring / PR self-audit.
        await _cancel_by_label(user_id, label)
        return

    start_dt = _event_start(event)
    if start_dt is None:
        return

    now = datetime.datetime.now(datetime.UTC)
    if start_dt <= now:
        # Event already started or is in the past -- nothing to remind about.
        await _cancel_by_label(user_id, label)
        return

    lead_minutes = _reminder_lead_minutes(user_id)
    fire_at = start_dt - datetime.timedelta(minutes=lead_minutes)
    if fire_at <= now:
        fire_at = now + datetime.timedelta(seconds=_IMMEDIATE_FIRE_DELAY_SECONDS)

    title = str(event.get("title") or "Untitled event").strip() or "Untitled event"
    action = encode_tool_action("notify", {"message": _reminder_message(title, lead_minutes)})

    scheduler = get_scheduler_service()
    existing = await scheduler.get_by_label_async(user_id, label)
    if existing is not None:
        if existing.action == action and _same_instant(existing.one_shot_at, fire_at):
            return
        await scheduler.delete_async(user_id, existing.id)

    await scheduler.add_async(
        user_id=user_id,
        label=label,
        action=action,
        one_shot_at=fire_at.isoformat(),
    )


async def sync_event_reminders(user_id: str, events: list[dict[str, Any]]) -> None:
    """Reconcile reminders for a batch of events (e.g. a calendar listing)."""
    for event in events:
        try:
            await sync_event_reminder(user_id, event)
        except Exception:
            logger.exception(
                "Calendar reminder sync failed for event_id=%r",
                event.get("event_id") if isinstance(event, dict) else None,
            )


async def cancel_event_reminder(user_id: str, event_id: str) -> None:
    """Cancel the reminder for a deleted/canceled event, if one was scheduled."""
    if not user_id or not event_id:
        return
    try:
        await _cancel_by_label(user_id, _reminder_label(str(event_id)))
    except Exception:
        logger.exception("Failed to cancel calendar reminder for event_id=%s", event_id)


async def reconcile_reminders_for_window(
    user_id: str,
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    events: list[dict[str, Any]],
) -> None:
    """Sync reminders for *events* and prune reminders orphaned by an
    out-of-band change (event moved or deleted directly with the calendar
    provider, not through Viola's own update/delete tools).

    Only prunes reminders whose fire time (event start - lead) falls
    inside [window_start, window_end]; a reminder for an event outside the
    queried window is left untouched, so a narrow query can never cancel
    an unrelated reminder.
    """
    if not user_id:
        return

    present_ids: set[str] = set()
    for event in events:
        if isinstance(event, dict):
            event_id = _event_id(event)
            if event_id is not None:
                present_ids.add(event_id)
        try:
            await sync_event_reminder(user_id, event)
        except Exception:
            logger.exception("Calendar reminder sync failed during window reconciliation")

    try:
        scheduler = get_scheduler_service()
        schedules = await scheduler.list_schedules_async(user_id, enabled_only=True)
    except Exception:
        logger.exception("Could not list schedules for calendar reminder pruning, user=%s", user_id)
        return

    if window_start.tzinfo is None or window_start.utcoffset() is None:
        window_start = window_start.replace(tzinfo=datetime.UTC)
    if window_end.tzinfo is None or window_end.utcoffset() is None:
        window_end = window_end.replace(tzinfo=datetime.UTC)
    window_start = window_start.astimezone(datetime.UTC)
    window_end = window_end.astimezone(datetime.UTC)

    for schedule in schedules:
        if not schedule.label.startswith(_LABEL_PREFIX):
            continue
        event_id = schedule.label[len(_LABEL_PREFIX) :]
        if event_id in present_ids:
            continue
        fire_at = _parse_iso(schedule.one_shot_at or schedule.next_run_at)
        if fire_at is None:
            continue
        # Widen the window by the max lead so an event whose reminder was
        # scheduled with a larger lead time isn't mistaken for "outside".
        widened_start = window_start - datetime.timedelta(minutes=_MAX_LEAD_MINUTES)
        if widened_start <= fire_at <= window_end:
            try:
                await scheduler.delete_async(user_id, schedule.id)
                logger.info(
                    "Pruned orphaned calendar reminder schedule_id=%d event_id=%s (no longer in listing)",
                    schedule.id,
                    event_id,
                )
            except Exception:
                logger.exception("Failed to prune orphaned calendar reminder schedule_id=%d", schedule.id)


# ---------------------------------------------------------------------------
# Background sweep -- coverage for events created outside the agent tool path
# ---------------------------------------------------------------------------
#
# The per-tool hooks above only run when an event flows through the agent's
# calendar tool (add/update/delete/list). Real desktop users create events
# through the Qt calendar panel, which posts to ``/v1/calendar/events``
# (calendar_mgr.add_event, no tool hook), and external events arrive via
# Google/Graph/CalDAV sync. Without a background reconcile those events would
# never get a reminder. This service periodically lists each owner's upcoming
# events and drives the SAME ``reconcile_reminders_for_window`` used by the
# list tool, so every creation path is covered while reusing one mechanism.

# Poll cadence. A newly-created event gets its reminder scheduled within one
# tick; the one-shot scheduler then fires it at the exact lead time, so a
# coarse sweep does not blunt reminder timing.
_SWEEP_INTERVAL_SECONDS = 60
# How far ahead to list events each sweep. Reminders are scheduled in advance
# and held by the one-shot scheduler until their fire time, so a wide horizon
# is cheap (it changes result count, not poll frequency).
_SWEEP_HORIZON_HOURS = 26
_SWEEP_MAX_EVENTS = 100

_service_lock = threading.Lock()
_service_singleton: CalendarReminderService | None = None


def _resolve_reminder_owners() -> list[str]:
    """Owners whose calendars the periodic sweep should reconcile.

    Desktop (one user per install): the device user id. Cloud does not
    enumerate owners here and returns none unless explicitly configured via
    ``settings.calendar_reminder_user_ids`` -- the sweep never runs for an
    anonymous / ``"default"`` user.
    """
    try:
        from config.settings import settings

        configured = getattr(settings, "calendar_reminder_user_ids", None) or []
        owners = [str(uid).strip() for uid in configured if str(uid).strip()]
        if owners:
            return owners
    except (ImportError, AttributeError, TypeError):
        logger.debug("Could not read calendar_reminder_user_ids from settings", exc_info=True)

    try:
        from config.settings import settings as _settings

        app_surface = str(getattr(_settings, "app_surface", "desktop")).lower()
    except (ImportError, AttributeError, TypeError):
        app_surface = "desktop"

    if app_surface == "desktop":
        try:
            from core.user_context import get_current_or_device_user_id

            return [get_current_or_device_user_id()]
        except (ImportError, LookupError):
            logger.debug("Could not resolve desktop device user id for calendar reminders", exc_info=True)

    return []


class CalendarReminderService:
    """Periodically reconcile reminders for every upcoming event, any source.

    This is a pure scheduler driver: it lists calendar events and calls
    ``reconcile_reminders_for_window``. It never classifies user input, parses
    model output, or injects hints -- the one agent loop is untouched.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Begin the reconcile sweep loop (no-op if globally disabled)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Calendar reminder sweep service started")

    async def stop(self) -> None:
        """Cancel the sweep loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Calendar reminder sweep service stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Calendar reminder sweep error")
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    async def sweep_once(self) -> int:
        """Reconcile reminders for all resolved owners once.

        Returns the number of owners successfully reconciled. Exposed for
        manual/test invocation so the behavior can be driven without the loop.
        """
        owners = _resolve_reminder_owners()
        reconciled = 0
        for uid in owners:
            try:
                await self._sweep_owner(uid)
                reconciled += 1
            except Exception:
                logger.exception("Calendar reminder sweep failed for user=%s", uid)
        return reconciled

    @staticmethod
    async def _sweep_owner(user_id: str) -> None:
        uid = (user_id or "").strip()
        if not uid:
            return

        from core.user_context import user_scope
        from services.calendar.manager import get_calendar_manager

        now = datetime.datetime.now(datetime.UTC)
        horizon = now + datetime.timedelta(hours=_SWEEP_HORIZON_HOURS)
        mgr = get_calendar_manager()
        with user_scope(uid):
            result = await mgr.get_events(
                start_date=now,
                end_date=horizon,
                max_results=_SWEEP_MAX_EVENTS,
                calendar="all",
                user_id=uid,
            )
        if not isinstance(result, dict) or not result.get("ok"):
            return
        events = [item for item in result.get("events", []) if isinstance(item, dict)]
        await reconcile_reminders_for_window(uid, now, horizon, events)


def get_calendar_reminder_service() -> CalendarReminderService:
    """Return the process-wide CalendarReminderService singleton."""
    global _service_singleton
    if _service_singleton is None:
        with _service_lock:
            if _service_singleton is None:
                _service_singleton = CalendarReminderService()
    return _service_singleton


def reset_calendar_reminder_service_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _service_singleton
    with _service_lock:
        _service_singleton = None


__all__ = [
    "CalendarReminderService",
    "cancel_event_reminder",
    "get_calendar_reminder_service",
    "reconcile_reminders_for_window",
    "reset_calendar_reminder_service_for_tests",
    "sync_event_reminder",
    "sync_event_reminders",
]
