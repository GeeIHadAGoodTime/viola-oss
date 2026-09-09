"""Runtime voice status for UI and diagnostic surfaces.

The published status snapshot (``set_voice_status``) reflects what bootstrap
*intended* (settings + registration). The live wake detector can still fail
after that point — e.g. the 1.0.1 frozen build where ``create_wake_listener``
died on a broken pyaec, leaving the facade's ``_impl`` as None while /health
kept reporting ``wake_enabled: true`` (lane-4 false-green, L4-5). To keep the
surface honest, the live facade registers a probe (its ``is_available``) and
``get_voice_status`` reconciles the snapshot against it: a claimed-enabled
wake that is not live is reported degraded, never green.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from threading import RLock
from typing import Any

_LOCK = RLock()

# Live wake detector probe — registered by voice.wake_detector.facade.WakeDetector.
# Returns True only when a concrete detector implementation is alive.
_LIVE_WAKE_PROBE: Callable[[], bool] | None = None

_DEFAULT_STATUS: dict[str, Any] = {
    "status": "ok",
    "degraded": False,
    "reason": None,
    # What the install asks for before bootstrap resolves anything: the shipped
    # default. `active_` stays push_to_talk on purpose -- it is the value a
    # degraded wake path falls back to, so it is correct as the pre-resolution
    # answer for "what is actually running".
    "requested_voice_mode": "wake_word",
    "active_voice_mode": "push_to_talk",
    "wake_enabled": False,
    "wake_engine": "none",
    "missing_dependencies": [],
    "details": {},
}

_STATUS: dict[str, Any] = deepcopy(_DEFAULT_STATUS)


def make_voice_status(
    *,
    requested_voice_mode: str,
    active_voice_mode: str,
    wake_enabled: bool,
    wake_engine: str,
    degraded: bool = False,
    reason: str | None = None,
    missing_dependencies: list[str] | tuple[str, ...] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured, machine-readable voice status payload."""
    return {
        "status": "degraded" if degraded else "ok",
        "degraded": bool(degraded),
        "reason": reason,
        "requested_voice_mode": requested_voice_mode,
        "active_voice_mode": active_voice_mode,
        "wake_enabled": bool(wake_enabled),
        "wake_engine": wake_engine,
        "missing_dependencies": list(missing_dependencies or ()),
        "details": dict(details or {}),
    }


def set_voice_status(status: dict[str, Any]) -> dict[str, Any]:
    """Publish the current runtime voice status."""
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(deepcopy(status))
        return deepcopy(_STATUS)


def register_live_wake_probe(probe: Callable[[], bool] | None) -> None:
    """Register the live wake detector availability probe.

    Called by the wake facade with its ``is_available`` bound method (or
    ``None`` to unregister, e.g. in tests). ``get_voice_status`` uses it to
    reconcile the published snapshot with reality.
    """
    global _LIVE_WAKE_PROBE
    with _LOCK:
        _LIVE_WAKE_PROBE = probe


def get_voice_status() -> dict[str, Any]:
    """Return the current runtime voice status, reconciled with the live detector.

    If the snapshot claims ``wake_enabled`` but the live facade reports no
    detector implementation, the returned status is degraded — never a
    false-green (1.0.1 lane-4 finding L4-5).
    """
    with _LOCK:
        status = deepcopy(_STATUS)
        probe = _LIVE_WAKE_PROBE

    if probe is not None:
        try:
            live = bool(probe())
        except (AttributeError, RuntimeError, TypeError, ValueError, OSError):
            # A crashing probe is "not live" — health must stay honest even
            # when the registered callable itself misbehaves.
            live = False
        status.setdefault("details", {})["live_wake_available"] = live
        if status.get("wake_enabled") and not live:
            status["wake_enabled"] = False
            status["degraded"] = True
            status["status"] = "degraded"
            status["reason"] = status.get("reason") or "wake_detector_not_live"
        elif live and status.get("reason") == "wake_detector_not_live":
            # Symmetric reconcile for OUR sentinel reason only: a reconciled
            # (degraded) read may legitimately be republished via
            # set_voice_status (e.g. wake_training's model-reload refresh);
            # once the detector is live again, that stale not-live marker must
            # not stick. Bootstrap-published degradations (other reasons) are
            # never overridden here.
            status["degraded"] = False
            status["status"] = "ok"
            status["reason"] = None
    return status


def reset_voice_status() -> None:
    """Reset status to the conservative default. Intended for tests."""
    global _LIVE_WAKE_PROBE
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(deepcopy(_DEFAULT_STATUS))
        _LIVE_WAKE_PROBE = None


__all__ = [
    "get_voice_status",
    "make_voice_status",
    "register_live_wake_probe",
    "reset_voice_status",
    "set_voice_status",
]
