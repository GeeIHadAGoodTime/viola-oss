"""Compatibility facade for timer services.

Desktop builds use the Qt adapter. Headless/cloud builds fall back to the
pure-Python core implementation.
"""

from __future__ import annotations

from services.timer_core import Timer, TimerEventListener, parse_duration

try:
    from services.timer_qt import TimerService
except ImportError:
    from services.timer_core import TimerService


def get_timer_service() -> TimerService:
    """Get the global TimerService instance."""
    return TimerService.get_instance()


__all__ = [
    "Timer",
    "TimerEventListener",
    "TimerService",
    "get_timer_service",
    "parse_duration",
]
