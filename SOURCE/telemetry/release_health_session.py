"""Durable desktop release-health session journal.

The marker written here is intentionally tiny and local-only: release version,
install id, session id, timestamps, pid, and crash state. It exists so the next
launch can reconcile a prior process that died before in-memory telemetry or
Sentry had a chance to upload anything.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Stdlib logger only: this module is imported at the very top of viola_qt.py
# startup (before the heavy import wall), so it deliberately avoids pulling in any
# project modules. A default no-op debug logger is exactly right for the benign,
# best-effort telemetry races below.
logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_SESSION_FILE = "release_health_session.json"
_INSTALL_ID_FILE = "release_health_install_id.json"
_INSTALL_ID_PREFIX = "viola-install-"
_TRUTHY = frozenset({"1", "true", "yes", "on", "y"})
_NONFATAL_RELEASE_HEALTH_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_CURRENT: ReleaseHealthSession | None = None
_RECORDED_EXCEPTION_IDS: set[int] = set()


@dataclass(frozen=True)
class ReleaseHealthSession:
    """Current desktop session identity."""

    session_id: str
    install_id: str
    version: str
    path: Path


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _session_root() -> Path:
    configured = os.environ.get("VIOLA_RELEASE_HEALTH_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()

    if getattr(sys, "frozen", False):
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return base / "Viola" / "release-health"
        xdg = os.environ.get("XDG_STATE_HOME") or os.environ.get("XDG_DATA_HOME")
        if xdg:
            return Path(xdg) / "viola" / "release-health"
        return Path.home() / ".local" / "state" / "viola" / "release-health"

    return Path(__file__).resolve().parents[1] / ".viola" / "release-health"


def _session_path() -> Path:
    return _session_root() / _SESSION_FILE


def _install_id_path() -> Path:
    return _session_root() / _INSTALL_ID_FILE


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Best-effort, concurrency-safe atomic JSON write.

    Two Viola processes can launch near-simultaneously on first run and race to
    write the same release-health marker. A single *shared* temp name would collide
    between the racers, and ``os.replace`` onto a destination that another process
    is concurrently replacing raises ``PermissionError`` (Windows ``WinError 32``:
    "the process cannot access the file because it is being used by another
    process"). This marker is a tiny, local-only hint — never worth crashing the
    app for — so we:

      * write to a *process-unique* temp (pid + random token) so temps never
        collide between racing writers, and
      * tolerate the replace race: if another writer wins, its payload is an
        equivalent logical state, so we drop this write instead of raising.

    A telemetry write must never propagate an exception to the caller, so any
    OSError (the race, plus transient filesystem errors) is swallowed after
    cleaning up our temp file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Benign concurrent-writer race (e.g. WinError 32) or a transient fs error.
        logger.debug("release-health atomic write lost a benign concurrent-writer race", exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            logger.debug("release-health temp cleanup after a lost write race failed (harmless)", exc_info=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_install_id(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_INSTALL_ID_PREFIX) and len(value) >= 24


def _get_or_create_install_id() -> str:
    path = _install_id_path()
    current = _read_json(path).get("install_id")
    if _valid_install_id(current):
        return str(current)

    created = _INSTALL_ID_PREFIX + secrets.token_urlsafe(18)
    _atomic_write_json(path, {"schema_version": _SCHEMA_VERSION, "install_id": created})
    return created


def _increment_counter(counter_name: str, count: int = 1) -> None:
    if count <= 0:
        return
    try:
        from telemetry import get_accumulator

        accumulator = get_accumulator()
        if hasattr(accumulator, "increment_n"):
            accumulator.increment_n(counter_name, count)
        else:
            for _ in range(count):
                accumulator.increment(counter_name)
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        return


def _sentry_sessions_enabled() -> bool:
    value = os.environ.get("VIOLA_SENTRY_RELEASE_SESSIONS", "1").strip().lower()
    return value in _TRUTHY


def _start_sentry_session() -> None:
    if not _sentry_sessions_enabled():
        return
    try:
        from core.sentry_integration import start_sentry_release_session

        start_sentry_release_session()
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        return


def _end_sentry_session(status: str) -> None:
    try:
        from core.sentry_integration import end_sentry_release_session

        end_sentry_release_session(status=status)
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        return


def _active_session(payload: dict[str, Any]) -> dict[str, Any]:
    active = payload.get("active")
    return active if isinstance(active, dict) else {}


def _prior_session_was_unclean(active: dict[str, Any]) -> bool:
    if not active:
        return False
    return active.get("state") == "started" and active.get("clean_exit_at") is None


def _reconcile_prior_session(active: dict[str, Any]) -> dict[str, Any] | None:
    if not _prior_session_was_unclean(active):
        return None

    unhandled_count = int(active.get("unhandled_exceptions") or 0)
    _increment_counter("crashes")
    _increment_counter("session_crashes")
    _increment_counter("unhandled_exceptions", unhandled_count)
    return {
        "session_id": active.get("session_id"),
        "version": active.get("version"),
        "install_id": active.get("install_id"),
        "started_at": active.get("started_at"),
        "reconciled_at": _now_iso(),
        "reason": "missing_clean_exit",
        "unhandled_exceptions": unhandled_count,
    }


def start_release_health_session(version: str) -> ReleaseHealthSession | None:
    """Start a durable release-health session and reconcile prior abnormal exit.

    Telemetry must NEVER crash Viola's launch. This runs at module top level in
    ``viola_qt.py``, *before* the single-instance lock, so on first run two
    processes can reach it concurrently. Every filesystem touch below is best-effort
    (see ``_atomic_write_json``), and the whole body is additionally guarded so any
    non-fatal telemetry error degrades to a returned ``None`` (or the existing
    session) rather than propagating to the caller.
    """

    global _CURRENT

    try:
        path = _session_path()
        payload = _read_json(path)
        prior = _reconcile_prior_session(_active_session(payload))
        install_id = _get_or_create_install_id()
        session_id = secrets.token_urlsafe(18)
        now = _now_iso()

        active = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": session_id,
            "install_id": install_id,
            "version": str(version or "unknown"),
            "pid": os.getpid(),
            "state": "started",
            "started_at": now,
            "heartbeat_at": now,
            "monotonic_started": time.monotonic(),
            "unhandled_exceptions": 0,
        }
        next_payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "active": active,
            "last_reconciled_crash": prior,
        }
        _atomic_write_json(path, next_payload)
        _increment_counter("sessions_started")
        _CURRENT = ReleaseHealthSession(session_id=session_id, install_id=install_id, version=str(version), path=path)
        return _CURRENT
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        # A telemetry marker failure must never crash the app's launch.
        return _CURRENT


def start_sentry_session_for_active() -> None:
    """Start Sentry release-health session tracking after Sentry is initialized."""

    if _CURRENT is not None:
        _start_sentry_session()


def record_unhandled_exception(exc: BaseException | None = None) -> None:
    """Record a real unhandled exception in durable and in-memory telemetry."""

    exc_id = id(exc) if exc is not None else 0
    if exc_id and exc_id in _RECORDED_EXCEPTION_IDS:
        return
    if exc_id:
        _RECORDED_EXCEPTION_IDS.add(exc_id)
        if len(_RECORDED_EXCEPTION_IDS) > 2048:
            _RECORDED_EXCEPTION_IDS.clear()

    # This runs from the crash handler; it must never raise a second exception.
    try:
        _increment_counter("crashes")
        _increment_counter("unhandled_exceptions")
        _increment_counter("session_crashes")
        current = _CURRENT
        if current is None:
            _end_sentry_session("crashed")
            return

        payload = _read_json(current.path)
        active = _active_session(payload)
        if active.get("session_id") == current.session_id:
            active["state"] = "started"
            active["crashed_at"] = active.get("crashed_at") or _now_iso()
            active["heartbeat_at"] = _now_iso()
            active["unhandled_exceptions"] = int(active.get("unhandled_exceptions") or 0) + 1
            payload["active"] = active
            _atomic_write_json(current.path, payload)
        _end_sentry_session("crashed")
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        return


def mark_release_health_clean_exit(exit_code: int = 0) -> None:
    """Mark the active session as cleanly exited."""

    current = _CURRENT
    if current is None:
        return
    try:
        payload = _read_json(current.path)
        active = _active_session(payload)
        if active.get("session_id") != current.session_id:
            return
        active["state"] = "clean_exit"
        active["clean_exit_at"] = _now_iso()
        active["exit_code"] = int(exit_code)
        active["heartbeat_at"] = _now_iso()
        payload["active"] = active
        _atomic_write_json(current.path, payload)
        _increment_counter("sessions_clean_exits")
        _end_sentry_session("exited")
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        return


__all__ = [
    "ReleaseHealthSession",
    "mark_release_health_clean_exit",
    "record_unhandled_exception",
    "start_release_health_session",
    "start_sentry_session_for_active",
]
