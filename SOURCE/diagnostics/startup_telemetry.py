"""Startup timing telemetry for the desktop hub.

This module stays stdlib-only so it can be imported on the startup critical
path without pulling in the larger logging or diagnostics graph.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_PROCESS_STARTED_MONO = time.perf_counter()
_PROCESS_STARTED_EPOCH = time.time()
_PROCESS_START_RECORDED = False
_PORT_BOUND_MONO: float | None = None
_SUBSYSTEMS: dict[str, dict[str, Any]] = {}
_APP_INITIALIZERS_STARTED: set[int] = set()
# Names of post-bind initializers that mount API routes. Until every one of
# them has finished, the app's route table is incomplete, so an unmatched path
# is "not mounted yet", not "does not exist" (see routing/startup_gate.py).
_ROUTE_INITIALIZERS: set[str] = set()
_FINISHED_STATES = ("ready", "failed")
# True once run_post_bind_initializers() has actually launched the initializers.
# Registration alone is NOT enough to call a route initializer "outstanding":
# an app can be built and served without the post-bind launch ever happening
# (a test harness or an embedded app that never runs the server_factory
# sequence, and the real server when server.start() times out before
# core/server_factory.py reaches run_post_bind_initializers). Treating those
# registrations as forever-outstanding armed the startup gate permanently and
# turned every honest 404 in the process into a 503 that never cleared.
_POST_BIND_LAUNCHED = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _log_path() -> Path:
    # Writable logs dir (per-user on frozen installs), never Path.cwd() — a
    # Start-Menu launch has cwd == the (read-only) install dir. Lazy import keeps
    # this stdlib-light module cheap on the startup critical path.
    from core.platform import get_logs_dir

    return get_logs_dir() / "structured" / "viola-qt.log"


def _duration_ms(start: float, end: float | None = None) -> int:
    return round(((end if end is not None else time.perf_counter()) - start) * 1000)


def _uptime_ms(now: float | None = None) -> int:
    current = now if now is not None else time.perf_counter()
    return _duration_ms(_PROCESS_STARTED_MONO, current)


def _write_event(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": _now_iso(),
        "event": event,
        **fields,
    }
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
    except Exception:
        # Startup telemetry must never break startup.
        return


def record_process_start(*, force: bool = False) -> None:
    """Reset the startup clock for the current process."""
    global _PROCESS_STARTED_MONO, _PROCESS_STARTED_EPOCH, _PROCESS_START_RECORDED, _PORT_BOUND_MONO
    global _POST_BIND_LAUNCHED
    with _LOCK:
        if _PROCESS_START_RECORDED and not force:
            return
        _PROCESS_STARTED_MONO = time.perf_counter()
        _PROCESS_STARTED_EPOCH = time.time()
        _PROCESS_START_RECORDED = True
        _PORT_BOUND_MONO = None
        _SUBSYSTEMS.clear()
        _APP_INITIALIZERS_STARTED.clear()
        _ROUTE_INITIALIZERS.clear()
        _POST_BIND_LAUNCHED = False
    _write_event(
        "process_start",
        message="process_start",
        process_started_epoch=_PROCESS_STARTED_EPOCH,
    )


def record_port_bound() -> None:
    """Record that the HTTP server is responding on its bound port."""
    global _PORT_BOUND_MONO
    with _LOCK:
        if _PORT_BOUND_MONO is not None:
            return
        _PORT_BOUND_MONO = time.perf_counter()
        port_bound_ms = _duration_ms(_PROCESS_STARTED_MONO, _PORT_BOUND_MONO)
    _write_event(
        "port_bound",
        message=f"port_bound duration_ms={port_bound_ms}",
        port_bound_ms=port_bound_ms,
    )


def mark_subsystem_pending(name: str) -> None:
    with _LOCK:
        _SUBSYSTEMS.setdefault(
            name,
            {
                "state": "pending",
                "started_ms": None,
                "ended_ms": None,
                "init_ms": None,
                "ok": None,
                "error": None,
            },
        )


def subsystem_init_start(name: str) -> None:
    now = time.perf_counter()
    with _LOCK:
        entry = _SUBSYSTEMS.setdefault(name, {})
        entry.update(
            {
                "state": "initializing",
                "started_ms": _uptime_ms(now),
                "ended_ms": None,
                "init_ms": None,
                "ok": None,
                "error": None,
            }
        )
    _write_event(
        "subsystem_init_start",
        message=f"subsystem_init_start subsystem={name}",
        subsystem=name,
        process_uptime_ms=_uptime_ms(now),
    )


def subsystem_init_end(name: str, *, ok: bool, error: str | None = None) -> None:
    now = time.perf_counter()
    with _LOCK:
        entry = _SUBSYSTEMS.setdefault(name, {})
        started_ms = entry.get("started_ms")
        init_ms = None
        if isinstance(started_ms, int):
            init_ms = max(0, _uptime_ms(now) - started_ms)
        entry.update(
            {
                "state": "ready" if ok else "failed",
                "ended_ms": _uptime_ms(now),
                "init_ms": init_ms,
                "ok": ok,
                "error": error,
            }
        )
    _write_event(
        "subsystem_init_end",
        message=f"subsystem_init_end subsystem={name} duration_ms={init_ms} ok={ok}",
        subsystem=name,
        duration_ms=init_ms,
        ok=ok,
        error=error,
        process_uptime_ms=_uptime_ms(now),
    )


@contextmanager
def subsystem_timer(name: str) -> Iterator[None]:
    subsystem_init_start(name)
    try:
        yield
    except Exception as exc:
        subsystem_init_end(name, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        subsystem_init_end(name, ok=True)


def run_in_background(name: str, func: Callable[[], Any]) -> threading.Thread:
    """Run a subsystem initializer on a daemon thread with timing telemetry."""

    def _runner() -> None:
        subsystem_init_start(name)
        try:
            result = func()
            if inspect.isawaitable(result):
                asyncio.run(result)
        except Exception as exc:
            subsystem_init_end(name, ok=False, error=f"{type(exc).__name__}: {exc}")
            _write_event(
                "subsystem_init_exception",
                message=f"subsystem_init_exception subsystem={name}",
                subsystem=name,
                traceback=traceback.format_exc(limit=20),
            )
        else:
            subsystem_init_end(name, ok=True)

    mark_subsystem_pending(name)
    thread = threading.Thread(target=_runner, name=f"startup-init-{name}", daemon=True)
    thread.start()
    return thread


def register_post_bind_initializer(
    app: Any,
    name: str,
    func: Callable[[], Any],
    *,
    registers_routes: bool = False,
) -> None:
    """Attach a background initializer to an app for server_factory to launch.

    ``registers_routes=True`` marks an initializer that mounts API routes.
    Those run first (see :func:`run_post_bind_initializers`) and gate
    :func:`route_surface_complete`, so the server can answer "still starting"
    instead of "no such endpoint" while the route table is incomplete.
    """
    mark_subsystem_pending(name)
    if registers_routes:
        with _LOCK:
            _ROUTE_INITIALIZERS.add(str(name))
    try:
        initializers = getattr(app.state, "startup_background_initializers", None)
        if initializers is None:
            initializers = []
            app.state.startup_background_initializers = initializers
        initializers.append((name, func))
    except Exception as exc:
        subsystem_init_end(name, ok=False, error=f"{type(exc).__name__}: {exc}")


def _pending_route_names() -> list[str]:
    """Return outstanding route-mounting initializers. Caller must hold _LOCK.

    Nothing is outstanding until the post-bind launch has actually happened.
    Before then no initializer is in flight, so an unmatched path is genuinely
    unmatched and must keep its honest 404 rather than being reported as
    "still mounting" forever (see ``_POST_BIND_LAUNCHED``).
    """
    if not _POST_BIND_LAUNCHED:
        return []
    return sorted(
        name for name in _ROUTE_INITIALIZERS if (_SUBSYSTEMS.get(name) or {}).get("state") not in _FINISHED_STATES
    )


def route_surface_complete() -> bool:
    """Return True once every route-mounting post-bind initializer has finished.

    "Finished" includes ``failed``: an initializer that blew up is never going
    to mount its routes, so continuing to answer "still starting" would be its
    own lie. The failure stays visible in :func:`snapshot` and in
    ``/health/details``.
    """
    with _LOCK:
        return not _pending_route_names()


def pending_route_initializers() -> list[str]:
    """Return the route-mounting initializers that have not finished yet."""
    with _LOCK:
        return _pending_route_names()


def _initializer_launch_order(
    initializers: list[tuple[str, Callable[[], Any]]],
) -> list[tuple[str, Callable[[], Any]]]:
    """Order initializers so route-mounting work starts before everything else.

    The registration order is otherwise incidental (it follows import/wiring
    order), and every initializer competes for the same interpreter the moment
    the port is bound. Starting the route mounters first shortens the window in
    which a user-reachable endpoint does not exist yet.
    """
    with _LOCK:
        route_names = set(_ROUTE_INITIALIZERS)
    routes = [item for item in initializers if str(item[0]) in route_names]
    rest = [item for item in initializers if str(item[0]) not in route_names]
    return routes + rest


def run_post_bind_initializers(app: Any) -> None:
    """Launch app-level initializers after the server readiness probe succeeds."""
    try:
        initializers = list(getattr(app.state, "startup_background_initializers", []) or [])
    except (AttributeError, RuntimeError):
        return
    with _LOCK:
        try:
            if bool(getattr(app.state, "_post_bind_initializers_started", False)):
                return
            app.state._post_bind_initializers_started = True
        except (AttributeError, RuntimeError):
            app_id = id(app)
            if app_id in _APP_INITIALIZERS_STARTED:
                return
            _APP_INITIALIZERS_STARTED.add(app_id)
    # Arm the route-surface signal before the first thread starts, so the gate
    # covers the whole in-flight window rather than racing the first launch.
    global _POST_BIND_LAUNCHED
    with _LOCK:
        _POST_BIND_LAUNCHED = True
    for name, func in _initializer_launch_order(initializers):
        run_in_background(str(name), func)


def snapshot() -> dict[str, Any]:
    now = time.perf_counter()
    with _LOCK:
        port_bound_ms = None if _PORT_BOUND_MONO is None else _duration_ms(_PROCESS_STARTED_MONO, _PORT_BOUND_MONO)
        subsystems: dict[str, dict[str, Any]] = {}
        for name, entry in sorted(_SUBSYSTEMS.items()):
            item = dict(entry)
            if item.get("state") == "initializing" and isinstance(item.get("started_ms"), int):
                item["started_ms_ago"] = max(0, _uptime_ms(now) - int(item["started_ms"]))
            subsystems[name] = item
        pending_routes = _pending_route_names()
        return {
            "process_started_epoch": _PROCESS_STARTED_EPOCH,
            "process_uptime_ms": _uptime_ms(now),
            "port_bound_ms": port_bound_ms,
            "subsystems": subsystems,
            "route_surface_complete": not pending_routes,
            "pending_route_initializers": pending_routes,
        }


__all__ = [
    "mark_subsystem_pending",
    "pending_route_initializers",
    "record_port_bound",
    "record_process_start",
    "register_post_bind_initializer",
    "route_surface_complete",
    "run_in_background",
    "run_post_bind_initializers",
    "snapshot",
    "subsystem_init_end",
    "subsystem_init_start",
    "subsystem_timer",
]
