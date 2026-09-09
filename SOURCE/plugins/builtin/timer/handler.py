"""Timer handler — manages named countdown timers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from core.logging_config import get_logger
from plugins.api import PluginResponse

logger = get_logger(__name__)


def _parse_seconds(duration_str: str, unit_str: str) -> int:
    """Convert duration + unit to seconds."""
    try:
        value = int(duration_str)
    except (ValueError, TypeError):
        return 0

    unit = unit_str.lower().rstrip("s") if unit_str else "m"
    if unit.startswith("h"):
        return value * 3600
    if unit.startswith("m"):
        return value * 60
    return value  # seconds


def _format_duration(seconds: int) -> str:
    """Human-friendly duration string."""
    if seconds >= 3600:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        if mins:
            return "%d hour%s and %d minute%s" % (
                hours,
                "s" if hours != 1 else "",
                mins,
                "s" if mins != 1 else "",
            )
        return "%d hour%s" % (hours, "s" if hours != 1 else "")
    if seconds >= 60:
        mins = seconds // 60
        secs = seconds % 60
        if secs:
            return "%d minute%s and %d second%s" % (
                mins,
                "s" if mins != 1 else "",
                secs,
                "s" if secs != 1 else "",
            )
        return "%d minute%s" % (mins, "s" if mins != 1 else "")
    return "%d second%s" % (seconds, "s" if seconds != 1 else "")


class TimerHandler:
    """Manages named countdown timers."""

    def __init__(self, tts_callback: Callable[[str], None] | None = None):
        self._timers: dict[str, dict] = {}  # name -> {thread, end_time, seconds}
        self._lock = threading.Lock()
        self._counter = 0
        self._tts_callback = tts_callback

    def set_timer(self, slots: dict[str, str]) -> PluginResponse:
        duration_str = slots.get("duration", "0")
        unit_str = slots.get("unit", "m")
        name = slots.get("name", "")

        seconds = _parse_seconds(duration_str, unit_str)
        if seconds <= 0:
            return PluginResponse(speech="I need a valid duration for the timer.")

        if not name:
            self._counter += 1
            name = "timer_%d" % self._counter

        with self._lock:
            # Cancel existing timer with same name
            if name in self._timers:
                existing = self._timers[name]
                existing["thread"].cancel()

            end_time = time.time() + seconds
            t = threading.Timer(seconds, self._on_expire, args=[name])
            t.daemon = True
            t.start()

            self._timers[name] = {
                "thread": t,
                "end_time": end_time,
                "seconds": seconds,
            }

        friendly = _format_duration(seconds)
        label = name if not name.startswith("timer_") else ""
        if label:
            speech = "Okay, I've set a %s timer for %s." % (label, friendly)
        else:
            speech = "Timer set for %s." % friendly

        return PluginResponse(
            speech=speech,
            display={"name": name, "seconds": seconds, "end_time": end_time},
        )

    def cancel_timer(self, slots: dict[str, str]) -> PluginResponse:
        name = slots.get("name", "")

        with self._lock:
            if name and name in self._timers:
                self._timers[name]["thread"].cancel()
                del self._timers[name]
                return PluginResponse(speech="Cancelled the %s timer." % name)

            if not name and self._timers:
                # Cancel most recent
                last_name = list(self._timers.keys())[-1]
                self._timers[last_name]["thread"].cancel()
                del self._timers[last_name]
                return PluginResponse(speech="Timer cancelled.")

        return PluginResponse(speech="There's no active timer to cancel.")

    def timer_status(self) -> PluginResponse:
        with self._lock:
            if not self._timers:
                return PluginResponse(speech="You don't have any active timers.")

            parts = []
            now = time.time()
            for name, info in self._timers.items():
                remaining = max(0, int(info["end_time"] - now))
                label = name if not name.startswith("timer_") else "Timer"
                parts.append("%s: %s remaining" % (label, _format_duration(remaining)))

            return PluginResponse(
                speech=". ".join(parts) + ".",
                display={"active_timers": len(self._timers)},
            )

    def cancel_all(self) -> None:
        """Cancel all timers (called on plugin cleanup)."""
        with self._lock:
            for info in self._timers.values():
                info["thread"].cancel()
            self._timers.clear()

    def _on_expire(self, name: str) -> None:
        """Called when a timer expires (from a threading.Timer thread)."""
        with self._lock:
            self._timers.pop(name, None)

        label = name if not name.startswith("timer_") else "Your"
        announcement = "%s timer is done!" % label
        logger.info("Timer expired: %s", name)

        if self._tts_callback is not None:
            try:
                self._tts_callback(announcement)
            except Exception:
                logger.exception("TTS callback failed for timer %s", name)
        else:
            logger.info("Timer announcement (no TTS wired): %s", announcement)
