"""Uptime and crash monitoring for Viola.

Tracks application uptime and records crash events for debugging.

NOTE: This module is intentionally device-scoped, not per-user.
Uptime, crash counts, and clean shutdown tracking are properties of
the physical device / process, not individual user sessions.
"""

from __future__ import annotations

import atexit
import time
from dataclasses import dataclass, field

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)


@dataclass
class UptimeMetrics:
    """Uptime tracking data."""

    start_time: float = field(default_factory=time.time)
    crash_count: int = 0
    last_crash: float | None = None
    clean_shutdowns: int = 0

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def uptime_hours(self) -> float:
        return self.uptime_seconds / 3600


_metrics = UptimeMetrics()
_state_file = get_data_dir() / "uptime_state.json"


def get_uptime_metrics() -> UptimeMetrics:
    """Get current uptime metrics."""
    return _metrics


def record_crash() -> None:
    """Record a crash event."""
    _metrics.crash_count += 1
    _metrics.last_crash = time.time()
    _save_state()
    logger.error("Crash recorded (total: %d)", _metrics.crash_count)


def record_clean_shutdown(*, emit_log: bool = True) -> None:
    """Record a clean shutdown."""
    _metrics.clean_shutdowns += 1
    _save_state()
    if emit_log:
        logger.info("Clean shutdown recorded (uptime: %.1f hours)", _metrics.uptime_hours)


def _record_clean_shutdown_at_exit() -> None:
    """Persist shutdown state without logging during interpreter teardown."""
    record_clean_shutdown(emit_log=False)


def _save_state() -> None:
    """Persist state to disk."""
    import json

    _state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(_state_file, "w") as f:
        json.dump(
            {
                "crash_count": _metrics.crash_count,
                "clean_shutdowns": _metrics.clean_shutdowns,
                "last_crash": _metrics.last_crash,
            },
            f,
        )


def _load_state() -> None:
    """Load state from disk."""
    import json

    if _state_file.exists():
        try:
            with open(_state_file) as f:
                data = json.load(f)
            _metrics.crash_count = data.get("crash_count", 0)
            _metrics.clean_shutdowns = data.get("clean_shutdowns", 0)
            _metrics.last_crash = data.get("last_crash")
        except Exception as e:
            logger.warning("Could not load uptime state: %s", e)


# Initialize on import
_load_state()
atexit.register(_record_clean_shutdown_at_exit)
