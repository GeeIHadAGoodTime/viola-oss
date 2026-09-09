"""Liveness for the Qt GUI event loop, coupled to the loop's own dispatch.

Why this module exists (#4650). On 2026-08-01 22:17:39 local the desktop app
took a native access violation on its MAIN thread while it was inside
``app.exec()`` -- proven by the faulthandler dump whose ``Current thread``
traceback is exactly ``viola_qt.py:1620 in main`` (which was
``_exit_code = app.exec()`` in the tree of that day). The Qt message loop
stopped dispatching, the window froze mid-frame, and Windows put up a modal
hard-error dialog that suspended the process. The HTTP server thread was
untouched, so for the entire outage:

    - ``GET /health``      answered ``{"status": "ok"}``
    - ``GET /health/live`` answered ``{"status": "live"}`` -- a hardcoded
      constant that could not have said anything else
    - ``viola_control.py health`` answered ``{"status": "ok", "running": true}``
      because it asks ``/health/live`` and believes a 200

Nothing paged, because nothing in the process was coupled to the thing that
had died. That is the defect this module closes: the desktop app's liveness
must be answered by the message loop itself, not by the web server that
happens to share its process.

**Why marking from a QTimer is honest here.** :mod:`services.liveness` warns
that a timer standing in for a work loop is exactly how false-green liveness
gets shipped -- "a timer thread standing in for a detection loop, so the beat
continued after the loop it represented had exited". That warning is about a
timer on a DIFFERENT thread reporting on someone else's behalf. This is the
opposite arrangement: the unit of work being measured *is* Qt event dispatch,
and a ``QTimer`` timeout slot is a queued event that only ever runs because
the GUI thread's event loop dequeued and dispatched it. The loop is marking
itself from inside its own body. If dispatch stops for any reason -- the
thread crashed, the process is suspended behind a hard-error dialog, a slot is
blocking, the loop exited -- the mark stops with it, which is precisely the
coupling :mod:`services.liveness` asks for.

**Headless is not a fault.** ``viola_qt.py --headless`` runs a daemon with no
QApplication at all. A process that never declared a GUI reports
``monitored: false`` and is never called stalled; a process that DID declare
one owes a fresh beat, and its silence is a fault. Declaring happens at
QApplication construction, so a GUI process cannot quietly opt out of being
watched by failing to install the probe -- that state reads as stalled, not as
healthy.
"""

from __future__ import annotations

import threading
from typing import Any

from core.logging_config import get_logger
from services.liveness import Health, WorkSignal

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_STALL_AFTER_S",
    "declare_gui_process",
    "gui_process_declared",
    "install_event_loop_probe",
    "probe",
    "reset_for_tests",
    "snapshot",
]

# One beat per second: frequent enough that a stall is obvious quickly, cheap
# enough to be invisible (a lock, an increment and a clock read -- see
# WorkSignal.mark).
DEFAULT_INTERVAL_S = 1.0

# A GUI that has not dispatched a single queued event in 30 seconds is not
# "busy", it is broken. The threshold is deliberately far above the beat
# interval: this box has run load averages in the 40s during parallel test
# batteries, and ordinary scheduler starvation must never be reported as a
# dead UI. The failure this exists to catch is permanent, so a generous
# threshold costs nothing in detection.
DEFAULT_STALL_AFTER_S = 30.0

# Cold start can legitimately keep the GUI thread busy: window construction,
# first paint, WebEngine bring-up. This is a bounded allowance, not an
# exemption -- a loop that never produces a first beat still goes stalled once
# it expires, which is what catches a GUI that died during startup.
DEFAULT_STARTUP_GRACE_S = 120.0

_LOCK = threading.Lock()
_gui_declared = False
_signal: WorkSignal | None = None
_timer: Any = None  # QTimer, held so it is not garbage-collected.


def declare_gui_process() -> None:
    """Record that this process runs a Qt GUI, so a dead loop is a fault.

    Call once, at QApplication construction. Separating the declaration from
    :func:`install_event_loop_probe` is what makes the check fail closed: a GUI
    process that never reaches the install call has declared but never beats,
    which reads as stalled rather than as unmonitored.
    """
    global _gui_declared
    with _LOCK:
        _gui_declared = True


def gui_process_declared() -> bool:
    with _LOCK:
        return _gui_declared


def install_event_loop_probe(
    app: Any,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    stall_after_s: float = DEFAULT_STALL_AFTER_S,
    startup_grace_s: float = DEFAULT_STARTUP_GRACE_S,
) -> WorkSignal:
    """Attach the beat to ``app``'s event loop and return the signal.

    Must be called on the GUI thread, before ``app.exec()``. The timer is
    parented to ``app`` and also held in a module global so neither Qt nor
    Python can collect it out from under the loop.

    Args:
        app: The ``QApplication``. Passed in rather than imported so this
            module never drags PySide6 into a headless or test process.
    """
    global _signal, _timer

    from PySide6.QtCore import QTimer

    signal = WorkSignal(
        "qt_event_loop",
        stall_after=stall_after_s,
        startup_grace=startup_grace_s,
    )

    timer = QTimer(app)
    timer.setInterval(int(interval_s * 1000))
    # A precise timer would ask Windows for a higher-resolution clock for a
    # beat that does not need one; coarse is right for a 1s heartbeat.
    timer.setTimerType(_coarse_timer_type())
    # mark() runs on the GUI thread because the timeout is dispatched by the
    # GUI thread's event loop -- that dispatch IS the work being measured.
    timer.timeout.connect(signal.mark)
    timer.start()

    with _LOCK:
        _signal = signal
        _timer = timer
        declared = _gui_declared
    if not declared:
        # Installing without declaring would leave the probe reporting
        # "not monitored" forever. Treat the install itself as a declaration
        # rather than silently running blind.
        declare_gui_process()
        logger.debug("Qt loop probe installed before declare_gui_process(); declaring now")

    logger.info(
        "Qt event-loop liveness probe installed (beat=%.1fs, stall_after=%.1fs)",
        interval_s,
        stall_after_s,
    )
    return signal


def _coarse_timer_type() -> Any:
    from PySide6.QtCore import Qt as _Qt

    return _Qt.TimerType.CoarseTimer


def probe() -> Health:
    """Health of this process's Qt event loop.

    ``UNAVAILABLE`` means "no GUI in this process" (a headless daemon), which
    :attr:`Health.is_healthy` already treats as not-a-fault for a component
    that is absent. Callers that need to distinguish absent-by-design from
    dead should read :func:`snapshot`, which says so explicitly.
    """
    with _LOCK:
        declared = _gui_declared
        signal = _signal
    if not declared:
        return Health.UNAVAILABLE
    if signal is None:
        # Declared a GUI but never installed the beat: the loop cannot be
        # vouched for, and the whole point of this module is that an
        # unvouched-for GUI loop is not reported as healthy.
        return Health.STALLED
    return signal.health(present=True)


def snapshot() -> dict[str, Any]:
    """Diagnostic block for the health payload.

    ``status`` follows the convention the rest of ``/health/details`` uses
    (``ok`` / ``degraded`` / ``error``) so the aggregate status calculation
    picks a stalled loop up without special-casing.
    """
    with _LOCK:
        declared = _gui_declared
        signal = _signal

    if not declared:
        return {
            "status": "ok",
            "monitored": False,
            "reason": "no Qt GUI in this process (headless daemon)",
        }
    if signal is None:
        return {
            "status": "error",
            "monitored": True,
            "reason": "GUI process declared but the event-loop probe was never installed",
            "alive": False,
        }

    snap = signal.snapshot()
    alive = bool(snap["fresh"])
    block: dict[str, Any] = {
        "status": "ok" if alive else "error",
        "monitored": True,
        "alive": alive,
        "beats": snap["work_units"],
        "seconds_since_beat": snap["seconds_since_work"],
        "stall_after_s": snap["stall_after"],
        "ever_beat": snap["ever_worked"],
    }
    if not alive:
        block["reason"] = (
            "the Qt message loop has not dispatched an event in "
            f"{snap['seconds_since_work']}s; the desktop UI is frozen or dead"
        )
    return block


def is_alive() -> bool:
    """True unless this process has a Qt GUI whose loop has stopped beating."""
    return probe() is not Health.STALLED


def reset_for_tests() -> None:
    """Drop all probe state. Tests only."""
    global _gui_declared, _signal, _timer
    with _LOCK:
        _gui_declared = False
        _signal = None
        _timer = None
