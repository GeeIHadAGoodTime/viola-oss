"""Scheduler service — persistent cron-based automation with sub-agent execution.

Runs an asyncio background loop that checks for due schedules every 30 seconds.
When a schedule fires, it spawns a sub-agent to execute the action through the
normal intent pipeline.

Circuit breaker logic prevents runaway API calls when the LLM is down:
- Schedules auto-disable after 3 consecutive failures
- Per-schedule error backoff (5 minutes after error/timeout)
- Global LLM health check skips LLM-dependent schedules when primary model is down

No Qt dependency — pure asyncio. Thread-safe singleton via :func:`get_scheduler_service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from core.constants import TIMEOUT_5_MINUTES
from core.exceptions import ErrorContext, ServiceError
from core.logging_config import get_logger
from core.user_context import user_scope
from services.scheduler.actions import decode_tool_action
from services.scheduler.store import (
    MAX_ENABLED_SCHEDULES,
    Schedule,
    ScheduleStore,
    get_schedule_store,
)

logger = get_logger(__name__)

_SERVICE_LOCK = threading.Lock()
_SERVICE_SINGLETON: SchedulerService | None = None

_CHECK_INTERVAL = 30  # seconds between due-schedule checks
_MAX_CONCURRENT_SUBAGENTS = 3
_SUBAGENT_TIMEOUT = 60.0  # seconds per sub-agent execution
_SHUTDOWN_GRACE_PERIOD = 5.0  # seconds to wait for in-flight sub-agents
_ERROR_BACKOFF_SECONDS = TIMEOUT_5_MINUTES  # 300s backoff after error/timeout
MIN_SCHEDULE_INTERVAL_SECONDS = TIMEOUT_5_MINUTES
_CRON_FREQUENCY_SAMPLE_COUNT = 64
_DANGEROUS_FREQUENCY_PATTERNS = frozenset(
    {
        "* * * * *",
    }
)

# Actions that match these patterns are instant commands — they do NOT
# need LLM processing and can fire even when the LLM is unhealthy.
_INSTANT_COMMAND_PATTERNS = re.compile(
    r"^("
    r"stop|pause|resume|play|skip|next|previous|mute|unmute"
    r"|volume\s+(up|down|\d+)"
    r"|what('?s| is) playing"
    r"|show (queue|playlist)"
    r"|shuffle|repeat"
    r")\s*$",
    re.IGNORECASE,
)

# Tools that should NOT run unattended via scheduled sub-agents.
# These are destructive or interactive tools that require human presence.
_SUBAGENT_DENY_LIST = frozenset(
    {
        "file_write",
        "run_command",
        "send_email",
        "computer",
        "self_manage",
    }
)
_SCHEDULED_ACTION_SAFETY_PREFIX = "SAFETY: scheduled actions cannot run unattended tool"
_SCHEDULED_ACTION_DENIED_TOOLS: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "scheduled_action_denied_tools",
    default=None,
)


def current_scheduled_action_denied_tools() -> frozenset[str]:
    """Return tools blocked for the currently running scheduled action."""

    return _SCHEDULED_ACTION_DENIED_TOOLS.get() or frozenset()


@contextlib.contextmanager
def _scheduled_action_tool_guard():
    token = _SCHEDULED_ACTION_DENIED_TOOLS.set(_SUBAGENT_DENY_LIST)
    try:
        yield
    finally:
        _SCHEDULED_ACTION_DENIED_TOOLS.reset(token)


def compute_next_run(cron_expr: str, after: datetime | None = None, timezone_name: str = "UTC") -> str:
    """Compute the next run time from a cron expression.

    Args:
        cron_expr: Standard 5-field cron expression.
        after: Base datetime (defaults to now UTC).

    Returns:
        ISO 8601 string of the next fire time.

    Raises:
        ValueError: If the cron expression is invalid.
    """
    normalized = _normalize_cron_expr(cron_expr)
    detail = validate_cron_expr_detail(normalized)
    if detail is not None:
        raise ValueError("Invalid cron expression %r: %s" % (cron_expr, detail))

    timezone = _resolve_scheduler_timezone(timezone_name)
    base = _coerce_cron_base(after or datetime.now(UTC), timezone)
    try:
        cron = croniter(normalized, base)
        next_dt = cron.get_next(datetime)
        if next_dt.tzinfo is None or next_dt.utcoffset() is None:
            next_dt = next_dt.replace(tzinfo=timezone)
        return next_dt.astimezone(UTC).isoformat()
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Invalid cron expression %r: %s" % (cron_expr, exc)) from exc


def _resolve_scheduler_timezone(timezone_name: str | None) -> datetime.tzinfo:
    name = (timezone_name or "UTC").strip()
    if not name or name.lower() == "auto":
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid scheduler timezone %r; falling back to UTC", timezone_name)
        return UTC


def _configured_scheduler_timezone_name() -> str:
    try:
        from config.settings import settings

        value = getattr(settings, "calendar_timezone", "UTC")
        return value if isinstance(value, str) else "UTC"
    except Exception as exc:
        logger.debug("Scheduler timezone lookup failed: %s", exc)
        return "UTC"


def _coerce_cron_base(value: datetime, timezone: datetime.tzinfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _normalise_one_shot_at(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=_resolve_scheduler_timezone(_configured_scheduler_timezone_name()))
    return dt.astimezone(UTC).isoformat()


def validate_cron_expr(cron_expr: str) -> bool:
    """Check if a string is a valid cron expression with a safe cadence."""
    return validate_cron_expr_detail(cron_expr) is None


def validate_cron_expr_detail(cron_expr: str) -> str | None:
    """Validate a cron expression and return the error message if invalid or unsafe.

    Returns:
        ``None`` if the expression is valid, otherwise a human-readable
        error string from croniter or the scheduler cadence floor.
    """
    normalized = _normalize_cron_expr(cron_expr)
    if normalized in _DANGEROUS_FREQUENCY_PATTERNS:
        return _format_min_interval_error(60.0)

    try:
        croniter(normalized)
        return _validate_cron_frequency(normalized)
    except (ValueError, KeyError, TypeError) as exc:
        return str(exc)


def _normalize_cron_expr(cron_expr: str) -> str:
    """Collapse whitespace so dangerous-pattern checks are deterministic."""
    return " ".join(str(cron_expr).strip().split())


def _validate_cron_frequency(cron_expr: str) -> str | None:
    """Reject cron expressions that can fire faster than the configured floor."""
    base = datetime.now(UTC).replace(second=0, microsecond=0)
    cron = croniter(cron_expr, base)
    previous = cron.get_next(datetime)

    for _ in range(_CRON_FREQUENCY_SAMPLE_COUNT):
        current = cron.get_next(datetime)
        gap_seconds = (current - previous).total_seconds()
        if gap_seconds < MIN_SCHEDULE_INTERVAL_SECONDS:
            return _format_min_interval_error(gap_seconds)
        previous = current

    return None


def _format_min_interval_error(actual_seconds: float) -> str:
    actual_minutes = actual_seconds / 60
    min_minutes = MIN_SCHEDULE_INTERVAL_SECONDS // 60
    return "runs every %.0f seconds (%.1f minutes); minimum interval is %d seconds (%d minutes)" % (
        actual_seconds,
        actual_minutes,
        MIN_SCHEDULE_INTERVAL_SECONDS,
        min_minutes,
    )


class SchedulerService:
    """Manages persistent scheduled actions with sub-agent execution.

    Lifecycle:
        start()  -> begins the check loop (called during bootstrap)
        stop()   -> cancels the loop and waits for in-flight sub-agents

    CRUD is delegated to :class:`ScheduleStore` for persistence.
    """

    def __init__(self, store: ScheduleStore | None = None) -> None:
        self._store = store or get_schedule_store()
        self._running = False
        self._check_task: asyncio.Task[None] | None = None
        # Lazy init (b2/HEAD pattern): supports constructing the service without
        # an active event loop (e.g. during early bootstrap or unit-test fixture
        # creation). start() and stop() handle the lifecycle. b1's eager init
        # would tie SchedulerService construction to an existing loop, breaking
        # the cloud-lifespan ordering where the loop is established after
        # service registration.
        self._stop_event: asyncio.Event | None = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUBAGENTS)
        self._inflight_tasks: set[asyncio.Task[None]] = set()
        # F-015: per-schedule in-flight guard so a slow subagent does not
        # cause the next poll tick to re-fire the same due row before
        # ``record_execution`` advances ``next_run_at``.
        self._inflight_ids: set[int] = set()
        self._lock = asyncio.Lock()

        # Intent pipeline reference (set externally after bootstrap)
        self._intent_pipeline: Any = None
        # MCP hub reference (set externally after bootstrap)
        self._mcp_hub: Any = None

    # ------------------------------------------------------------------ CRUD

    @staticmethod
    def _compute_create_next_run(cron_expr: str | None, one_shot_at: str | None) -> str:
        if cron_expr:
            detail = validate_cron_expr_detail(cron_expr)
            if detail is not None:
                raise ValueError(
                    "Invalid cron expression %r: %s. "
                    "Expected 5-field cron (minute hour day month weekday), "
                    "e.g. '0 7 * * 1-5' for 7:00 AM on weekdays." % (cron_expr, detail)
                )
            return compute_next_run(cron_expr, timezone_name=_configured_scheduler_timezone_name())
        if one_shot_at:
            # Validate the datetime is parseable and in the future
            try:
                dt = datetime.fromisoformat(one_shot_at)
                if dt.tzinfo is None:
                    # Naive datetimes represent user's local time, not UTC
                    dt = dt.astimezone()
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid one_shot_at datetime: %s" % exc) from exc
            return dt.isoformat()
        raise ValueError("Either cron_expr or one_shot_at must be provided")

    @staticmethod
    def _record_timer_feature() -> None:
        try:
            from admin.instrumentation import record_feature_used

            record_feature_used("timer")
        except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
            logger.debug("Telemetry record_feature_used (timer) failed, continuing")

    def add(
        self,
        user_id: str,
        label: str,
        action: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        """Create a new schedule. Returns the schedule ID.

        Args:
            label: Human-readable name.
            action: Natural language command (e.g. "play jazz playlist").
            cron_expr: Standard 5-field cron expression.
            one_shot_at: ISO 8601 datetime for one-shot execution.

        Raises:
            ValueError: If inputs are invalid, cron_expr is malformed,
                or the maximum number of enabled schedules has been reached.
        """
        # Max schedule limit is enforced in store.add() via MAX_ENABLED_SCHEDULES
        if cron_expr:
            detail = validate_cron_expr_detail(cron_expr)
            if detail is not None:
                raise ValueError(
                    "Invalid cron expression %r: %s. "
                    "Expected 5-field cron (minute hour day month weekday), "
                    "e.g. '0 7 * * 1-5' for 7:00 AM on weekdays." % (cron_expr, detail)
                )
            next_run = compute_next_run(cron_expr, timezone_name=_configured_scheduler_timezone_name())
        elif one_shot_at:
            # Validate the datetime is parseable and in the future
            try:
                next_run = _normalise_one_shot_at(one_shot_at)
                one_shot_at = next_run
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid one_shot_at datetime: %s" % exc) from exc
        else:
            raise ValueError("Either cron_expr or one_shot_at must be provided")

        schedule_id = self._store.add(
            user_id=user_id,
            label=label,
            action=action,
            next_run_at=next_run,
            cron_expr=cron_expr,
            one_shot_at=one_shot_at,
        )

        self._record_timer_feature()
        return schedule_id

    async def add_async(
        self,
        user_id: str,
        label: str,
        action: str,
        cron_expr: str | None = None,
        one_shot_at: str | None = None,
    ) -> int:
        """Create a new schedule from an async runtime path."""
        next_run = self._compute_create_next_run(cron_expr, one_shot_at)
        schedule_id = await self._store.add_async(
            user_id=user_id,
            label=label,
            action=action,
            next_run_at=next_run,
            cron_expr=cron_expr,
            one_shot_at=one_shot_at,
        )
        self._record_timer_feature()
        return schedule_id

    def list_schedules(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        """List all schedules."""
        return self._store.list_schedules(user_id, enabled_only=enabled_only)

    async def list_schedules_async(self, user_id: str, enabled_only: bool = True) -> list[Schedule]:
        """List all schedules from an async runtime path."""
        return await self._store.list_schedules_async(user_id, enabled_only=enabled_only)

    def get(self, user_id: str, schedule_id: int) -> Schedule | None:
        """Get a schedule by ID."""
        return self._store.get(user_id, schedule_id)

    async def get_async(self, user_id: str, schedule_id: int) -> Schedule | None:
        """Get a schedule by ID from an async runtime path."""
        return await self._store.get_async(user_id, schedule_id)

    async def get_by_label_async(self, user_id: str, label: str) -> Schedule | None:
        """Look up an enabled schedule by its exact label (case-insensitive).

        Used by callers that need an idempotent "find-or-create" schedule
        keyed off a deterministic label (e.g. calendar-event reminders keyed
        as ``calendar-reminder:<event_id>``) instead of tracking a numeric
        schedule id separately.
        """
        return await self._store.get_by_label_async(user_id, label)

    def update(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Update fields of an existing schedule.

        If cron_expr is changed, next_run_at is recomputed.
        """
        next_run_at: str | None = None
        if cron_expr is not None:
            detail = validate_cron_expr_detail(cron_expr)
            if detail is not None:
                raise ValueError("Invalid cron expression %r: %s" % (cron_expr, detail))
            next_run_at = compute_next_run(cron_expr, timezone_name=_configured_scheduler_timezone_name())

        return self._store.update(
            user_id,
            schedule_id,
            label=label,
            action=action,
            cron_expr=cron_expr,
            enabled=enabled,
            next_run_at=next_run_at,
        )

    async def update_async(
        self,
        user_id: str,
        schedule_id: int,
        *,
        label: str | None = None,
        action: str | None = None,
        cron_expr: str | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Update fields of an existing schedule from an async runtime path."""
        next_run_at: str | None = None
        if cron_expr is not None:
            detail = validate_cron_expr_detail(cron_expr)
            if detail is not None:
                raise ValueError("Invalid cron expression %r: %s" % (cron_expr, detail))
            next_run_at = compute_next_run(cron_expr, timezone_name=_configured_scheduler_timezone_name())

        return await self._store.update_async(
            user_id,
            schedule_id,
            label=label,
            action=action,
            cron_expr=cron_expr,
            enabled=enabled,
            next_run_at=next_run_at,
        )

    def delete(self, user_id: str, schedule_id: int) -> bool:
        """Delete a schedule permanently."""
        return self._store.delete(user_id, schedule_id)

    async def delete_async(self, user_id: str, schedule_id: int) -> bool:
        """Delete a schedule permanently from an async runtime path."""
        return await self._store.delete_async(user_id, schedule_id)

    def enable(self, user_id: str, schedule_id: int) -> bool:
        """Enable a disabled schedule, recompute next_run_at, and reset failure counter.

        When a user manually re-enables a schedule (especially one that was
        auto-disabled by the circuit breaker), the consecutive failure counter
        is reset so it gets a fresh start.
        """
        schedule = self._store.get(user_id, schedule_id)
        if schedule is None:
            return False
        next_run_at: str | None = None
        if schedule.cron_expr:
            detail = validate_cron_expr_detail(schedule.cron_expr)
            if detail is not None:
                raise ValueError("Invalid cron expression %r: %s" % (schedule.cron_expr, detail))
            next_run_at = compute_next_run(schedule.cron_expr, timezone_name=_configured_scheduler_timezone_name())
        # Reset consecutive failures when re-enabling (circuit breaker reset)
        self._store.reset_consecutive_failures(user_id, schedule_id)
        return self._store.update(
            user_id,
            schedule_id,
            enabled=True,
            next_run_at=next_run_at,
        )

    def disable(self, user_id: str, schedule_id: int) -> bool:
        """Disable a schedule (it won't fire until re-enabled)."""
        return self._store.update(user_id, schedule_id, enabled=False)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Begin the background check loop."""
        if self._running:
            logger.debug("Scheduler already running")
            return

        count = await self._store.count_enabled_all_async()
        self._running = True
        # Lazy-init pattern (b2/HEAD): create a fresh Event each start so the
        # service can be restarted across event-loop boundaries (e.g. in tests
        # that tear down and recreate the loop between cases).
        self._stop_event = asyncio.Event()
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("Scheduler started with %d enabled schedule(s)", count)

    async def stop(self) -> None:
        """Cancel the check loop and wait for in-flight sub-agents."""
        self._running = False
        # Defensive: stop() may run before start() in error paths, so guard
        # against None stop_event (matches the lazy-init __init__ choice).
        if self._stop_event is not None:
            self._stop_event.set()

        if self._check_task is not None:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            self._check_task = None
        self._stop_event = None

        # Wait for in-flight sub-agents with grace period
        if self._inflight_tasks:
            logger.info(
                "Waiting for %d in-flight sub-agent(s) (grace=%ss)...",
                len(self._inflight_tasks),
                _SHUTDOWN_GRACE_PERIOD,
            )
            _done, pending = await asyncio.wait(
                self._inflight_tasks,
                timeout=_SHUTDOWN_GRACE_PERIOD,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._inflight_tasks.clear()

        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------ check loop

    async def _check_loop(self) -> None:
        """Background loop that checks for due schedules every 30 seconds."""
        while self._running:
            try:
                await self._check_and_fire()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Scheduler check loop error: %s", exc)

            await self._wait_for_next_tick()

    async def _wait_for_next_tick(self) -> None:
        """Stop-aware sleep between check loops.

        Returns either when ``_stop_event`` is set (allowing immediate shutdown)
        or after _CHECK_INTERVAL elapses. Defensive None-check supports the
        lazy-init pattern in __init__.
        """
        stop_event = self._stop_event
        if stop_event is None:
            await asyncio.sleep(_CHECK_INTERVAL)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_CHECK_INTERVAL)
        except TimeoutError:
            return

    async def _check_and_fire(self) -> None:
        """Check for due schedules and spawn sub-agents.

        Circuit breaker checks applied before execution:
        1. Global LLM health — skip LLM-dependent schedules when primary model is down.
        2. Per-schedule error backoff — skip schedules that failed recently (5-min cooldown).
        """
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        due = await self._store.get_due_schedules_for_all_users_async(now_iso)
        if not due:
            return

        logger.info("Found %d due schedule(s)", len(due))

        # Check global LLM health
        llm_healthy = self._is_llm_healthy()

        for schedule in due:
            # --- F-015: in-flight guard ---
            # If a previous tick already spawned this schedule and the
            # sub-agent is still running, do NOT spawn another. The store
            # only advances ``next_run_at`` after ``record_execution``, so
            # without this guard a 60s sub-agent on a 30s poll cadence
            # would fire twice for the same due row.
            if schedule.id in self._inflight_ids:
                logger.debug(
                    "Schedule id=%d skipped (already in-flight)",
                    schedule.id,
                )
                continue

            # --- Circuit breaker: per-schedule error backoff ---
            if self._should_backoff(schedule):
                logger.info(
                    "Schedule id=%d skipped (error backoff, last_status=%s)",
                    schedule.id,
                    schedule.last_status,
                )
                continue

            # --- Circuit breaker: global LLM health check ---
            if not llm_healthy and not self._is_instant_command(schedule.action):
                logger.warning(
                    "Schedule id=%d skipped (LLM unhealthy, action=%r requires LLM)",
                    schedule.id,
                    schedule.action,
                )
                continue

            self._inflight_ids.add(schedule.id)
            task = asyncio.create_task(
                self._guarded_execute(schedule),
                name="scheduler-%d" % schedule.id,
            )
            self._inflight_tasks.add(task)
            task.add_done_callback(self._inflight_tasks.discard)

    async def _guarded_execute(self, schedule: Schedule) -> None:
        """Acquire semaphore, execute schedule, release.

        F-049: best-effort registration into ``AgentRegistry`` so a
        scheduled sub-agent shows up alongside foreground agents and is
        inspectable / cancellable through the normal TaskOutput / stop
        APIs. Registration failures (e.g. per-user active limit) MUST
        NOT abort the schedule — the scheduler is the system-level
        cron, not a user-initiated agent dispatch, and silent demotion
        to "ran without a record" is preferable to skipping a critical
        scheduled action.
        """
        try:
            async with self._semaphore:
                async with self._with_registered_agent(schedule):
                    await self._execute_schedule(schedule)
        finally:
            # F-015: clear in-flight marker so the next tick can fire
            # the schedule's next due time (cron) or skip it (one-shot).
            self._inflight_ids.discard(schedule.id)

    @contextlib.asynccontextmanager
    async def _with_registered_agent(self, schedule: Schedule):
        """F-049: register the scheduled run as a background agent record.

        Yields the registered ``agent_id`` (or ``None`` if registration
        failed). The record is marked completed/errored on exit so the
        registry's TaskOutput / cancel surfaces see the lifecycle.
        """
        agent_id: str | None = None
        registry = None
        try:
            from services.agent_runtime.registry import agent_registry as _registry

            registry = _registry
        except ImportError:
            logger.debug("AgentRegistry unavailable; scheduled run will not be registered")

        if registry is not None:
            try:
                from services.agent_runtime.registry import AgentRegistryError

                agent_id, _cancel_event, _background_signal = await registry.register_foreground(
                    user_id=schedule.user_id,
                    task=schedule.action,
                    reason="scheduler:%d" % schedule.id,
                    subagent_type="scheduler",
                    name=schedule.label or None,
                )
            except (AgentRegistryError, ValueError, RuntimeError):
                logger.debug(
                    "Could not register scheduled run for schedule id=%d in agent registry; "
                    "execution will proceed unregistered.",
                    schedule.id,
                    exc_info=True,
                )
                agent_id = None

        success = False
        error_msg: str | None = None
        try:
            yield agent_id
        except Exception as exc:
            error_msg = str(exc) or exc.__class__.__name__
            raise
        else:
            success = True
        finally:
            if registry is not None and agent_id is not None:
                from services.agent_runtime.registry import AgentRegistryError

                try:
                    if success:
                        await registry.mark_complete_explicit(schedule.user_id, agent_id, "schedule completed")
                    else:
                        await registry.mark_failed_explicit(
                            schedule.user_id,
                            agent_id,
                            error_msg or "schedule failed",
                        )
                except (AgentRegistryError, ValueError, RuntimeError):
                    logger.debug(
                        "Could not finalize scheduled-run agent %s in registry",
                        agent_id,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------ circuit breaker

    @staticmethod
    def _is_llm_healthy() -> bool:
        """Check if the primary LLM model is available.

        Returns True if the primary model is operational (no fallback active).
        Returns True if the fallback tracker cannot be imported (fail-open).
        """
        try:
            from services.llm.model_fallback import get_fallback_tracker

            tracker = get_fallback_tracker()
            if tracker.is_fallback_active:
                logger.warning(
                    "LLM primary model is down (fallback active, %d consecutive failures). "
                    "LLM-dependent schedules will be skipped.",
                    tracker.consecutive_failures,
                )
                return False
            return True
        except Exception:
            # Fail-open: if we can't check, assume healthy
            logger.debug("Could not check LLM fallback tracker, assuming healthy")
            return True

    @staticmethod
    def _is_instant_command(action: str) -> bool:
        """Check if an action is an instant command that doesn't need LLM processing.

        Instant commands (stop, pause, resume, volume, etc.) can fire even
        when the LLM is unhealthy because they don't make API calls.

        Args:
            action: The schedule's action string.

        Returns:
            True if the action matches an instant command pattern.
        """
        if decode_tool_action(action) is not None:
            return True
        return bool(_INSTANT_COMMAND_PATTERNS.match(action.strip()))

    @staticmethod
    def _should_backoff(schedule: Schedule) -> bool:
        """Check if a schedule should be skipped due to recent errors.

        If the last execution resulted in error or timeout, enforce a
        5-minute backoff before retrying. This prevents rapid-fire retries
        of schedules that are likely to fail again.

        Args:
            schedule: The schedule to check.

        Returns:
            True if the schedule should be skipped (still in backoff period).
        """
        if schedule.last_status not in ("error", "timeout"):
            return False

        if not schedule.last_run_at:
            return False

        try:
            last_run = datetime.fromisoformat(schedule.last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            elapsed = (now - last_run).total_seconds()
            if elapsed < _ERROR_BACKOFF_SECONDS:
                return True
        except (ValueError, TypeError):
            # If we can't parse the last_run_at, don't backoff
            logger.debug(
                "Could not parse last_run_at for schedule %d: %s",
                schedule.id,
                schedule.last_run_at,
            )
        return False

    # ------------------------------------------------------------------ execution

    async def _execute_schedule(self, schedule: Schedule) -> None:
        """Execute a single schedule by spawning a sub-agent.

        Updates the schedule with execution results regardless of outcome.
        """
        start = time.monotonic()
        logger.info(
            "Executing schedule id=%d label=%r action=%r",
            schedule.id,
            schedule.label,
            schedule.action,
        )

        # Pre-compute next_run_at for recurring schedules
        next_run_at: str | None = None
        if schedule.cron_expr:
            try:
                next_run_at = compute_next_run(
                    schedule.cron_expr,
                    timezone_name=_configured_scheduler_timezone_name(),
                )
            except ValueError as exc:
                logger.error(
                    "Failed to compute next run for schedule %d: %s",
                    schedule.id,
                    exc,
                )
                await self._store.record_execution_async(
                    schedule.user_id,
                    schedule.id,
                    status="error",
                    next_run_at=None,
                    error="Schedule could not run: the recurrence pattern is invalid.",
                )
                return

        try:
            await asyncio.wait_for(
                self._run_subagent(schedule),
                timeout=_SUBAGENT_TIMEOUT,
            )
            elapsed = time.monotonic() - start
            logger.info(
                "Schedule %d completed successfully in %.1fs",
                schedule.id,
                elapsed,
            )
            await self._store.record_execution_async(
                schedule.user_id,
                schedule.id,
                status="ok",
                next_run_at=next_run_at,
            )
        except TimeoutError:
            elapsed = time.monotonic() - start
            logger.warning(
                "Schedule %d timed out after %.1fs",
                schedule.id,
                elapsed,
            )
            await self._store.record_execution_async(
                schedule.user_id,
                schedule.id,
                status="timeout",
                next_run_at=next_run_at,
                error="Sub-agent execution timed out after %.0fs" % _SUBAGENT_TIMEOUT,
            )
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.exception(
                "Schedule %d failed after %.1fs: %s",
                schedule.id,
                elapsed,
                exc,
            )
            await self._store.record_execution_async(
                schedule.user_id,
                schedule.id,
                status="error",
                next_run_at=next_run_at,
                # Store generic user-facing message; technical details are in logs
                error=str(exc) if str(exc).startswith("Scheduled action") else "Scheduled action failed unexpectedly",
            )

    async def _run_subagent(self, schedule: Schedule) -> None:
        """Dispatch the schedule's action through the intent pipeline.

        The sub-agent processes the action as if a user spoke it, but:
        - No conversation history (fire-and-forget)
        - Dangerous tools are auto-denied
        - User memory context is injected
        """
        if self._intent_pipeline is None:
            logger.warning(
                "No intent pipeline wired for scheduler — skipping schedule %d",
                schedule.id,
            )
            raise RuntimeError("Scheduled action could not be processed (service unavailable)")

        try:
            result = await self._dispatch_schedule_action(schedule)
            ok = result.get("ok", False) if isinstance(result, dict) else False
            if not ok:
                error_msg = ""
                if isinstance(result, dict):
                    data = result.get("data", {})
                    data_error = data.get("error", "") if isinstance(data, dict) else ""
                    error_msg = str(result.get("error") or data_error or result.get("message", "") or "")
                logger.warning(
                    "Sub-agent returned not-ok for schedule %d: %s",
                    schedule.id,
                    error_msg,
                )
                if error_msg.startswith(_SCHEDULED_ACTION_SAFETY_PREFIX):
                    raise ServiceError(
                        "Scheduled action blocked by safety policy: %s" % error_msg,
                        ErrorContext(
                            component="services.scheduler",
                            operation="run_schedule",
                            params={"schedule_id": schedule.id},
                            user_message="The scheduled action was blocked by safety policy.",
                            recovery_hint="Run this action interactively if you want to use that tool.",
                        ),
                    )
                raise ServiceError(
                    "Scheduled action did not complete successfully",
                    ErrorContext(
                        component="services.scheduler",
                        operation="run_schedule",
                        params={"schedule_id": schedule.id},
                        user_message="The scheduled action could not be completed.",
                        recovery_hint="Check the schedule configuration and sub-agent availability.",
                    ),
                )
            logger.info(
                "Sub-agent completed for schedule %d: %s",
                schedule.id,
                result.get("message", "ok") if isinstance(result, dict) else "ok",
            )
        except ServiceError:
            raise
        except Exception as exc:
            logger.exception(
                "Sub-agent dispatch failed for schedule %d: %s",
                schedule.id,
                exc,
            )
            raise ServiceError(
                "Scheduled action failed due to an unexpected error",
                ErrorContext(
                    component="services.scheduler",
                    operation="run_schedule",
                    params={"schedule_id": schedule.id},
                    user_message="The scheduled action encountered an unexpected error.",
                    recovery_hint="Check the logs for details and retry the scheduled action.",
                ),
            ) from exc

    async def _dispatch_schedule_action(self, schedule: Schedule) -> Any:
        """Dispatch a scheduled action while preserving the owning user context.

        F-016: identity is scoped via ``user_scope`` and torn down in a
        ``finally`` so a slow/erroring sub-agent cannot leak the schedule
        owner's ContextVar onto whatever runs next on the same task.
        """
        with user_scope(schedule.user_id):
            structured_action = decode_tool_action(schedule.action)
            if structured_action is not None:
                return await self._dispatch_structured_action(schedule, structured_action)

            process = getattr(self._intent_pipeline, "process", None)
            if callable(process):
                with _scheduled_action_tool_guard():
                    return await process(schedule.action, user_key=schedule.user_id)

            raise RuntimeError("Scheduled action could not be processed (pipeline unavailable)")

    async def _dispatch_structured_action(self, schedule: Schedule, action: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool-owned scheduler action without LLM or instant routing."""
        tool = str(action.get("tool") or "").strip().lower()
        raw_args = action.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}

        if tool == "playback" and args.get("action") == "stop":
            return await self._dispatch_playback_stop(schedule, args)

        if tool == "alarm" and args.get("action") == "sound":
            from intent.tools.alarm_tools import play_alarm_sound_handler

            tts = getattr(self._intent_pipeline, "tts", None)
            result = await play_alarm_sound_handler(
                label=str(args.get("label") or schedule.label or "Alarm"),
                tts=tts,
                user_id=schedule.user_id,
            )
            return _tool_result_to_schedule_result(result)

        if tool == "notify":
            from intent.tools.notification_tools import deliver_notification_handler

            message = str(args.get("message") or schedule.label)
            spoken = False
            tts = getattr(self._intent_pipeline, "tts", None)
            speak = getattr(tts, "speak", None)
            if callable(speak):
                try:
                    await speak(message)
                    spoken = True
                except (RuntimeError, OSError, TypeError, ValueError) as exc:
                    logger.warning("Scheduled reminder TTS failed for schedule=%s: %s", schedule.id, exc)

            # user_requested: this row exists because the user asked to be
            # reminded at this moment (#4790), so it is delivered now rather
            # than parked in the push queue, and quiet hours does not swallow
            # it. Without that the reminder reached the user through nothing at
            # all on a desktop-only install.
            result = await deliver_notification_handler(
                message=message,
                channel=str(args.get("channel") or "") or None,
                user_id=schedule.user_id,
                metadata={"schedule_id": schedule.id, "spoken": spoken},
                user_requested=True,
            )
            return _tool_result_to_schedule_result(result)

        raise RuntimeError("Scheduled action has unsupported structured tool: %s" % (tool or "unknown"))

    async def _dispatch_playback_stop(self, schedule: Schedule, args: dict[str, Any]) -> dict[str, Any]:
        """Stop playback for a structured scheduler action."""
        executor = getattr(self._intent_pipeline, "_command_executor", None)
        execute = getattr(executor, "execute_command", None)
        if callable(execute):
            result = await execute("stop", {})
            ok = bool(result.get("success")) if isinstance(result, dict) else False
            return {
                "ok": ok,
                "message": result.get("message", "Playback stop completed") if isinstance(result, dict) else "",
                "data": {
                    "schedule_id": schedule.id,
                    "tool": "playback",
                    "action": "stop",
                    "reason": args.get("reason"),
                    "result": result,
                },
            }

        music = getattr(self._intent_pipeline, "music", None)
        stop = getattr(music, "stop", None)
        if not callable(stop):
            raise RuntimeError("Scheduled playback stop could not run (music controller unavailable)")
        result = stop()
        if inspect.isawaitable(result):
            result = await result
        return {
            "ok": True,
            "message": "Playback stop completed",
            "data": {
                "schedule_id": schedule.id,
                "tool": "playback",
                "action": "stop",
                "reason": args.get("reason"),
                "result": result,
            },
        }


def _tool_result_to_schedule_result(result: Any) -> dict[str, Any]:
    """Convert a ToolResult-like object to the scheduler's result shape."""
    ok = bool(getattr(result, "ok", False))
    data = getattr(result, "data", None)
    error = getattr(result, "error", None)
    return {
        "ok": ok,
        "message": str(error or "") if not ok else "ok",
        "data": data if isinstance(data, dict) else {"result": data},
    }


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


def get_scheduler_service(store: ScheduleStore | None = None) -> SchedulerService:
    """Return the process-wide SchedulerService singleton."""
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        with _SERVICE_LOCK:
            if _SERVICE_SINGLETON is None:
                _SERVICE_SINGLETON = SchedulerService(store=store)
    return _SERVICE_SINGLETON


def reset_scheduler_service_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _SERVICE_SINGLETON
    with _SERVICE_LOCK:
        _SERVICE_SINGLETON = None
