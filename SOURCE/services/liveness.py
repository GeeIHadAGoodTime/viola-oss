"""
Work-coupled liveness signals.

A liveness signal is only worth its coupling to the work it claims to measure.
Every false-green this runtime has shipped came from the same shape: something
adjacent to the work published "I am fine" on the work's behalf.

    - A heartbeat emitted only by the restart path, so an idle component was
      declared dead, rebuilt, beat once because it was rebuilt, went idle, and
      was rebuilt again -- forever (``voice/pipeline.py``, STT rebuilt every
      ~6 minutes on an idle install).
    - A timer thread standing in for a detection loop, so the beat continued
      after the loop it represented had exited or parked
      (``voice/wake_detector/facade.py``, the deleted ``_start_heartbeat_pump``).
    - An ``is_running()`` that asked whether a thread object was alive, which
      is true for a thread parked forever in a blocking device read.
    - An availability probe that asked whether an object had been constructed
      (``_impl is not None``), which stays true long after its loop has died.

The rule this module exists to enforce: **the only thing allowed to say the
work is happening is the work itself.**

``WorkSignal`` is a counter that only the inside of a work loop can advance.
It cannot be advanced by a timer, by a restart hook, or by anything holding a
reference to the component -- ``mark()`` is called from inside the loop body,
once per real unit of work, or the count does not move. Every consumer that
wants to know whether that work is happening reads the same signal, so a
health endpoint, a supervisor, and a UI badge cannot disagree, and none of them
can be satisfied by a proxy.

``Health`` distinguishes the two shapes of component this runtime actually has,
which is the distinction the old supervisor lacked:

    - **Continuous** work (the wake-detection loop) must keep advancing. Silence
      means it stopped, and stopping is a fault worth restarting.
    - **On-demand** work (transcription) advances only when a user speaks. Silence
      is the normal resting state and must never be read as death. Health for
      these is answered by asking the component whether it is ready, not by
      watching for a beat that a healthy idle system will never send.

Conflating those two is what rebuilt the transcriber every six minutes forever.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

__all__ = [
    "Health",
    "WorkSignal",
    "continuous_probe",
    "on_demand_probe",
]


class Health(str, Enum):
    """The answer a probe gives about a monitored component."""

    WORKING = "working"
    """The component's real work advanced since the last poll."""

    IDLE_OK = "idle_ok"
    """No work happened, and that is correct -- an on-demand component at rest.

    Never a fault. This is the state that must not be mistaken for death.
    """

    STALLED = "stalled"
    """Work that is supposed to be continuous has stopped advancing.

    The carrier thread may still exist and may even be alive -- parked forever
    in a blocking read counts as stalled, which is precisely the case a
    thread-liveness check reports as healthy.
    """

    STOPPED = "stopped"
    """Not running, deliberately, and must be left alone.

    A user who mutes the microphone or leaves wake-word mode has asked for the
    detector to be off. Without this state a supervisor reads the resulting
    silence as death and "recovers" the component, undoing the user's choice --
    so the distinction between *stopped* and *died* is load-bearing, not
    cosmetic.
    """

    UNAVAILABLE = "unavailable"
    """The component is not there at all (never built, or torn down)."""

    @property
    def is_healthy(self) -> bool:
        """True for states that need no intervention.

        ``STOPPED`` counts as healthy: nothing is wrong, the component was
        asked not to run.
        """
        return self in (Health.WORKING, Health.IDLE_OK, Health.STOPPED)


class WorkSignal:
    """A monotonic counter that only the real work can advance.

    Create one next to the loop it measures and call :meth:`mark` from inside
    the loop body, once per unit of real work. Readers ask :meth:`health` or
    :meth:`snapshot`; nothing but ``mark()`` moves the count, so a reader cannot
    be fooled by a component that exists but has stopped working.

    ``mark()`` is called at audio-frame rate (about every 80 ms in the wake
    loop), so it stays a lock, an increment, and a clock read -- nothing that
    allocates or logs.

    Thread-safe. All timing uses :func:`time.monotonic`, so a wall-clock
    adjustment cannot make a stalled component look fresh.
    """

    __slots__ = (
        "__weakref__",
        "_count",
        "_last",
        "_lock",
        "_name",
        "_stall_after",
        "_started_at",
        "_startup_grace",
    )

    def __init__(self, name: str, *, stall_after: float, startup_grace: float | None = None) -> None:
        """
        Args:
            name: Identifier used in diagnostics.
            stall_after: Seconds of no ``mark()`` after which continuous work is
                considered stalled. Set it well above the loop's natural period
                so ordinary jitter cannot trip it.
            startup_grace: Seconds a loop may take to produce its FIRST unit of
                work before it counts as stalled. Loading a speech model and
                opening an audio device is legitimately slow, and a cold start
                must not look like a death. Defaults to ``stall_after``.

                This is a bounded allowance, not an exemption: a loop that never
                produces a first unit of work still goes stalled once the grace
                expires, which is what catches a detector that died during
                initialisation.
        """
        if stall_after <= 0:
            raise ValueError("stall_after must be positive")
        if startup_grace is not None and startup_grace <= 0:
            raise ValueError("startup_grace must be positive")
        self._name = name
        self._stall_after = float(stall_after)
        self._startup_grace = float(startup_grace) if startup_grace is not None else float(stall_after)
        self._lock = threading.Lock()
        self._count = 0
        self._started_at = time.monotonic()
        self._last: float | None = None

    def restart_window(self) -> None:
        """Reset the startup allowance, e.g. when the loop is (re)started.

        Does not touch the work count -- history is preserved -- it only says
        "a fresh attempt begins now", so a detector restarted after a device
        failure gets the same honest cold-start allowance as the first one.
        """
        with self._lock:
            self._started_at = time.monotonic()
            self._last = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def stall_after(self) -> float:
        return self._stall_after

    def mark(self) -> None:
        """Record one unit of real work. Call ONLY from inside the work itself.

        Calling this from a timer, a restart hook, a health endpoint, or any
        path that has not actually done the work reintroduces exactly the class
        of defect this module exists to prevent.
        """
        with self._lock:
            self._count += 1
            self._last = time.monotonic()

    @property
    def count(self) -> int:
        """Units of real work completed since this signal was created."""
        with self._lock:
            return self._count

    def since_last_work(self) -> float:
        """Seconds since work last advanced.

        Before the first ``mark()`` this measures from the start of the current
        attempt, so a loop that never produced a single unit of work is not
        mistaken for a fresh one.
        """
        with self._lock:
            reference = self._last if self._last is not None else self._started_at
            return time.monotonic() - reference

    def has_ever_worked(self) -> bool:
        with self._lock:
            return self._last is not None

    def worked_recently(self) -> bool:
        """True only if real work has happened AND happened recently.

        Distinct from :meth:`is_fresh`, which grants a not-yet-started loop its
        cold-start allowance. Use this one to answer "has this component done
        anything lately", where a component that has never worked at all must
        answer no rather than borrow the startup grace.
        """
        with self._lock:
            if self._last is None:
                return False
            return (time.monotonic() - self._last) <= self._stall_after

    def is_fresh(self) -> bool:
        """True when work advanced recently enough to count as alive.

        Uses ``startup_grace`` until the first unit of work lands, then
        ``stall_after`` for every unit after it.
        """
        with self._lock:
            if self._last is None:
                return (time.monotonic() - self._started_at) <= self._startup_grace
            return (time.monotonic() - self._last) <= self._stall_after

    def health(self, *, present: bool = True) -> Health:
        """Health of a CONTINUOUS worker measured by this signal.

        Args:
            present: Whether the component still exists at all. False short
                circuits to :attr:`Health.UNAVAILABLE`.
        """
        if not present:
            return Health.UNAVAILABLE
        return Health.WORKING if self.is_fresh() else Health.STALLED

    def snapshot(self) -> dict[str, Any]:
        """Diagnostic view. Safe to expose on a health endpoint."""
        with self._lock:
            last = self._last
            count = self._count
            reference = last if last is not None else self._started_at
            idle = time.monotonic() - reference
            limit = self._stall_after if last is not None else self._startup_grace
        return {
            "name": self._name,
            "work_units": count,
            "seconds_since_work": round(idle, 3),
            "stall_after": self._stall_after,
            "startup_grace": self._startup_grace,
            "fresh": idle <= limit,
            "ever_worked": last is not None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        snap = self.snapshot()
        return f"<WorkSignal {snap['name']} units={snap['work_units']} idle={snap['seconds_since_work']}s fresh={snap['fresh']}>"


def continuous_probe(
    signal: WorkSignal,
    presence: Callable[[], bool],
) -> Callable[[], Health]:
    """Build a probe for work that must never stop.

    Reports :attr:`Health.WORKING` only while ``signal`` keeps advancing, so a
    loop that exited, or that is parked forever inside a blocking read, reports
    :attr:`Health.STALLED` instead of the healthy answer a thread-liveness check
    would give.

    Args:
        signal: The signal the loop marks from inside its body.
        presence: Cheap check that the component still exists (not that it is
            working -- that is the signal's job).
    """

    def _probe() -> Health:
        try:
            present = bool(presence())
        except Exception:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
            logger.debug("Presence check for '%s' raised; treating as unavailable", signal.name, exc_info=True)
            return Health.UNAVAILABLE
        return signal.health(present=present)

    return _probe


def on_demand_probe(
    readiness: Callable[[], bool],
    signal: WorkSignal | None = None,
) -> Callable[[], Health]:
    """Build a probe for work that only runs when a user asks for it.

    Idleness is never a fault here: a component that is ready but has nothing to
    do reports :attr:`Health.IDLE_OK`. This is the case the grace-period model
    got wrong -- it read a healthy resting transcriber as a dead one and rebuilt
    it on a timer, forever.

    Args:
        readiness: Answers "could you do the work right now if asked?"
        signal: Optional signal marked by the real work, used only to report
            :attr:`Health.WORKING` when work genuinely happened recently. Its
            silence is never held against the component.
    """

    def _probe() -> Health:
        try:
            ready = bool(readiness())
        except Exception:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
            logger.debug("Readiness check raised; treating as unavailable", exc_info=True)
            return Health.UNAVAILABLE
        if not ready:
            return Health.UNAVAILABLE
        if signal is not None and signal.worked_recently():
            return Health.WORKING
        return Health.IDLE_OK

    return _probe
