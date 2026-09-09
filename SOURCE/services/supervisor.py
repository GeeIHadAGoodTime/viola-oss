from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from config.settings import settings
from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger
from services.liveness import Health

logger = get_logger(__name__)

# Restart backoff. A component that is genuinely broken -- a speech model whose
# files are corrupt, a microphone that no longer exists -- reports unhealthy on
# every single poll, so recovery has to slow down or the supervisor becomes the
# thing hammering the machine. Grows from RESTART_BACKOFF_BASE and caps at
# RESTART_BACKOFF_MAX.
RESTART_BACKOFF_BASE = 5.0
RESTART_BACKOFF_MAX = 300.0

try:
    from backend.app_state import AppState

    _HAS_APP_STATE = True
except Exception:  # pragma: no cover - fallback for isolated tests
    _HAS_APP_STATE = False

if TYPE_CHECKING:
    from backend.app_state import AppState

from diagnostics.runtime_metrics import get_runtime_metrics


class _DebugEventEmitter(Protocol):
    """Protocol for debug event emitter function."""

    def __call__(
        self,
        name: str,
        payload: dict[str, object] | None = None,
        *,
        source: str = "qt",
    ) -> None: ...


_emit_debug_event: _DebugEventEmitter | None = None
try:
    from ui.qt_native.debug_events import emit_debug_event as _imported_emit

    _emit_debug_event = _imported_emit
except Exception:  # pragma: no cover - optional in headless mode
    logger.debug("Debug event emitter unavailable, running in headless mode")


RestartCallable = Callable[[], Any]


@dataclass
class _HeartbeatEntry:
    """Internal bookkeeping for a monitored subsystem."""

    name: str
    description: str
    grace_period: float
    restart: RestartCallable
    last_seen: float = field(default_factory=time.time)
    restart_attempts: int = 0
    missed_lapses: int = 0
    active: bool = True
    probe: Callable[[], Health] | None = None
    last_health: Health | None = None
    next_restart_after: float = 0.0
    consecutive_restarts: int = 0
    """Failed-recovery streak, reset the moment the source reports healthy.

    Drives the backoff. Distinct from ``restart_attempts``, which stays a
    lifetime total for diagnostics.
    """


class HeartbeatSupervisor:
    """
    Cooperative watchdog that monitors subsystem heartbeats.

    Each registered source must periodically call :meth:`record_heartbeat`.
    When a source becomes silent past its grace period, the supervisor will
    invoke the supplied restart hook, increment the persistent restart counter
    on ``AppState``, and append a breadcrumb for UI/telemetry visibility.
    """

    def __init__(self, state: AppState, *, poll_interval: float = 2.0) -> None:
        self._state = state
        self._poll_interval = max(0.5, poll_interval)
        self._entries: dict[str, _HeartbeatEntry] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="HeartbeatSupervisor",
            daemon=True,
        )
        self._metrics = get_runtime_metrics()

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        """Start the supervisor loop if not already running."""

        # Do not start background thread under pytest/test mode
        if settings.test_mode or settings.pytest_in_progress:
            logger.debug("HeartbeatSupervisor disabled in test mode")
            return
        if self._thread.is_alive():
            return
        logger.debug("Starting HeartbeatSupervisor loop")
        self._thread.start()

    def stop(self) -> None:
        """Stop the supervisor loop."""

        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=TIMEOUT_SHUTDOWN)

    def register_source(
        self,
        name: str,
        *,
        description: str,
        grace_period: float,
        restart: RestartCallable,
        probe: Callable[[], Health] | None = None,
    ) -> None:
        """
        Register a monitored heartbeat source.

        Args:
            name: Identifier used for counters/breadcrumbs.
            description: Human-readable description for breadcrumb context.
            grace_period: Maximum allowed seconds between heartbeats before
                attempting recovery. Ignored when ``probe`` is supplied.
            restart: Callable invoked when the source is judged unhealthy. It may
                return a boolean or an awaitable resolving to a boolean
                indicating whether the restart succeeded.
            probe: Preferred health source. When supplied, the supervisor ASKS
                this callable each poll instead of inferring death from silence,
                and the probe's verdict is the only thing that can trigger a
                restart.

                Silence is a bad proxy in both directions, and this runtime has
                shipped both failures. An on-demand component (transcription)
                is silent whenever nobody is speaking, so silence-means-death
                rebuilt it on a timer forever. A continuous component can keep
                a push-heartbeat flowing from a thread that is merely adjacent
                to the work, so a beat is not proof the work is happening. A
                probe built on a ``WorkSignal`` the work itself advances closes
                both holes -- see ``services/liveness.py``.
        """

        entry = _HeartbeatEntry(
            name=name,
            description=description,
            grace_period=max(1.0, grace_period),
            restart=restart,
            probe=probe,
        )
        with self._lock:
            self._entries[name] = entry
        self.start()
        logger.debug(
            "Registered heartbeat source '%s' (grace_period=%ss)",
            name,
            entry.grace_period,
        )

    def record_heartbeat(self, name: str) -> None:
        """Record a heartbeat for the given source."""

        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                logger.debug("Ignoring heartbeat for unknown source '%s'", name)
                return
            now = time.time()
            entry.last_seen = now
            entry.active = True
            entry.missed_lapses = 0
            grace = entry.grace_period
        self._metrics.heartbeat(
            f"supervisor.{name}",
            status="ok",
            grace_period=grace,
            last_seen=now,
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of active entries (useful for diagnostics/tests)."""

        with self._lock:
            return {
                name: {
                    "description": entry.description,
                    "last_seen": entry.last_seen,
                    "grace_period": entry.grace_period,
                    "restart_attempts": entry.restart_attempts,
                    "missed_lapses": entry.missed_lapses,
                    "active": entry.active,
                    "probed": entry.probe is not None,
                    "health": entry.last_health.value if entry.last_health is not None else None,
                }
                for (name, entry) in self._entries.items()
            }

    # ----------------------------------------------------------------- internal
    def _run(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self._poll_interval)
            self.poll_once()

    def poll_once(self) -> None:
        """Run a single supervision pass.

        Split out of the loop so the recovery decision is directly testable --
        the endless-rebuild defect this supervisor once caused was invisible to
        tests precisely because the decision only ran inside a sleeping daemon
        thread that pytest never starts.
        """
        now = time.time()
        entries: dict[str, _HeartbeatEntry]
        with self._lock:
            entries = dict(self._entries)

        for name, entry in entries.items():
            if not entry.active:
                continue
            elapsed = now - entry.last_seen
            reason = self._assess(entry, now, elapsed)
            if reason is None:
                continue
            if now < entry.next_restart_after:
                # Unhealthy, but a recent recovery attempt has not had time to
                # take effect. Backing off here is what stops a permanently
                # broken component from being rebuilt on every poll forever --
                # the same endless-rebuild shape this supervisor used to cause,
                # only faster.
                logger.debug(
                    "Recovery for '%s' deferred %.0fs by backoff: %s",
                    name,
                    entry.next_restart_after - now,
                    reason,
                )
                continue
            logger.warning("Recovery needed for '%s': %s", name, reason)
            entry.missed_lapses += 1
            self._metrics.heartbeat(
                f"supervisor.{name}",
                status="degraded",
                silence_seconds=elapsed,
                grace_period=entry.grace_period,
                missed_lapses=entry.missed_lapses,
            )
            if entry.missed_lapses == 1:
                logger.warning(
                    "Source '%s' looked unhealthy once; deferring restart one poll to absorb startup and transients.",
                    name,
                )
                entry.last_seen = time.time()
                continue
            if _emit_debug_event is not None:
                _emit_debug_event(
                    "self_healing_action",
                    {
                        "source": name,
                        "status": "triggered",
                        "silence_seconds": elapsed,
                    },
                    source="heartbeat_supervisor",
                )
            success = self._attempt_restart(entry)
            status = "recovered" if success else "restart_failed"
            restart_count = self._state.increment_restart_counter(name)
            breadcrumb = f"[{status}] {entry.description} (#{restart_count}) after {elapsed:.1f}s silence"
            self._state.append_breadcrumb(breadcrumb)
            entry.last_seen = time.time()
            entry.restart_attempts += 1
            entry.consecutive_restarts += 1
            entry.missed_lapses = 0
            entry.next_restart_after = entry.last_seen + min(
                RESTART_BACKOFF_MAX,
                RESTART_BACKOFF_BASE * (2 ** min(entry.consecutive_restarts, 16)),
            )
            if not success:
                entry.active = False
            self._metrics.heartbeat(
                f"supervisor.{name}",
                status="ok" if success else "error",
                restart_attempts=entry.restart_attempts,
                silence_seconds=elapsed,
            )
            if _emit_debug_event is not None:
                _emit_debug_event(
                    "self_healing_action",
                    {
                        "source": name,
                        "status": status,
                        "restart_attempts": entry.restart_attempts,
                    },
                    source="heartbeat_supervisor",
                )

    def _assess(self, entry: _HeartbeatEntry, now: float, elapsed: float) -> str | None:
        """Decide whether ``entry`` needs recovery. Returns None when healthy.

        A probed source is ASKED. Only a probe that reports an unhealthy state
        can trigger recovery, so an on-demand component at rest is never
        mistaken for a dead one, and a component whose real work has stopped
        cannot hide behind a beat emitted by something adjacent to it.

        A source registered without a probe keeps the original behaviour:
        silence past its grace period is read as death.
        """
        if entry.probe is not None:
            try:
                health = entry.probe()
            except Exception:
                logger.exception("Health probe for '%s' raised; treating as unhealthy", entry.name)
                entry.last_health = Health.UNAVAILABLE
                return "probe raised"
            entry.last_health = health
            if health.is_healthy:
                entry.last_seen = now
                entry.missed_lapses = 0
                # A genuine recovery clears the backoff, so a component that
                # comes back and later fails again gets a prompt first retry
                # rather than inheriting an old penalty.
                entry.consecutive_restarts = 0
                entry.next_restart_after = 0.0
                self._metrics.heartbeat(
                    f"supervisor.{entry.name}",
                    status="ok",
                    health=health.value,
                    last_seen=now,
                )
                return None
            return f"probe reported {health.value}"

        if elapsed <= entry.grace_period:
            return None
        return f"heartbeat lapsed ({elapsed:.1f}s > {entry.grace_period:.1f}s)"

    def _attempt_restart(self, entry: _HeartbeatEntry) -> bool:
        try:
            outcome = entry.restart()
            if inspect.iscoroutine(outcome) or isinstance(outcome, Awaitable):
                return asyncio.run(self._await_restart(outcome))
            return bool(outcome) if outcome is not None else True
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Restart hook for '%s' failed: %s", entry.name, exc)
            return False

    async def _await_restart(self, coro: Awaitable[Any]) -> bool:
        try:
            result = await coro
            return bool(result) if result is not None else True
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Async restart hook failed: %s", exc)
            return False


_SUPERVISOR_ATTR = "_heartbeat_supervisor"
_SUPERVISOR_LOCK = threading.Lock()


def ensure_supervisor(state: AppState) -> HeartbeatSupervisor:
    """
    Return a singleton supervisor bound to the provided ``AppState``.

    Attaches the supervisor instance directly onto the ``state`` instance so
    that distinct test harnesses or embedded runtimes can maintain separate
    watchdog loops without global cross-talk.
    """

    with _SUPERVISOR_LOCK:
        supervisor = getattr(state, _SUPERVISOR_ATTR, None)
        if supervisor is None:
            supervisor = HeartbeatSupervisor(state)
            setattr(state, _SUPERVISOR_ATTR, supervisor)
        return supervisor


__all__ = ["HeartbeatSupervisor", "ensure_supervisor"]
