from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StartupState(str, Enum):
    """
    Canonical startup phases for the Qt application lifecycle.

    The values are human friendly (lowercase strings) so they can be logged,
    emitted via debug events, and asserted in UI tests without additional
    conversion.
    """

    IDLE = "idle"
    STARTING_BACKEND = "starting_backend"
    WAITING_BACKEND = "waiting_backend"
    WAITING_READY = "waiting_ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(slots=True)
class HealthStatus:
    """
    Normalised payload returned from backend health probes.

    ``ready`` communicates readiness (True when backend fully initialised),
    ``status`` mirrors the textual status returned by the backend
    (e.g. "ok", "starting", "degraded"), and ``details`` carries the raw
    backend payload for diagnostics/telemetry.
    """

    ready: bool
    status: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200
    latency_ms: float | None = None

    def coerce_status(self) -> str:
        """Return a human friendly status token."""
        if self.ready:
            return "ok"
        status = (self.status or "").strip().lower()
        return status or ("http_" + str(self.http_status))

    def as_summary(self) -> dict[str, Any]:
        """Flatten important fields for logging/debug events."""
        summary = {
            "ready": self.ready,
            "status": self.coerce_status(),
            "http_status": self.http_status,
        }
        if self.latency_ms is not None and not math.isnan(self.latency_ms):
            summary["latency_ms"] = round(float(self.latency_ms), 2)
        if self.details:
            summary["details"] = self.details
        return summary


@dataclass(slots=True)
class StartupPolicy:
    """
    Tunable knobs that govern startup orchestration.

    Values are intentionally conservative to remain compatible with
    Raspberry Pi class hardware yet fast enough on desktop machines.
    """

    backend_start_grace_s: float = 2.0
    backend_start_timeout_s: float = 35.0
    # Headroom for heavy first-run init (model loading) before uvicorn binds; a
    # too-tight window painted "Backend Failed" on slow starts (2026-05-29 P0).
    readiness_timeout_s: float = 120.0
    poll_interval_ms: int = 600
    initial_probe_delay_ms: int = 500
    # Capped so probes stay frequent within the readiness window. The old
    # (…16000, 30000) schedule left a 30s probe-gap: a backend that bound ~43s in
    # (post heavy init) went healthy at ~44s but the next probe wasn't until the
    # 60s deadline, so READY was never observed and the UI bricked on a terminal
    # "Backend Failed" page. Keep max step <= 5000ms (see
    # scripts/check_startup_readiness_backoff_bounded.py).
    readiness_backoff_ms: tuple[int, ...] = field(default_factory=lambda: (500, 1000, 1500, 2000, 2000))
    passive_poll_interval_ms: int = 30000
    health_details_endpoint: str = "/health/details"
    health_ready_endpoint: str = "/health/ready"
    health_live_endpoint: str = "/health/live"
    log_on_each_attempt: bool = True


__all__ = [
    "HealthStatus",
    "StartupPolicy",
    "StartupState",
]
