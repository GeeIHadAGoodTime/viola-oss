from __future__ import annotations

from typing import Any


class Counter:
    """Lightweight in-memory counter used for telemetry snapshots."""

    __slots__ = ("_values", "labels", "name")

    def __init__(self, name: str, labels: list[str] | None = None) -> None:
        self.name = name
        self.labels = labels or []
        self._values: dict[tuple[tuple[str, Any], ...], int] = {}

    def inc(self, **label_values: Any) -> None:
        key = tuple((key, label_values.get(key)) for key in self.labels)
        self._values[key] = self._values.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        return {str(key): value for key, value in self._values.items()}


http_requests_total = Counter("viola_http_requests_total", labels=["route", "code", "method"])
commands_total = Counter("viola_commands_total", labels=["channel"])
play_events_total = Counter("viola_play_events_total", labels=["channel"])

# Import runtime metrics from core (canonical location)
from core.metrics import runtime_capability_toggle_total, runtime_profile_selected_total

__all__ = [
    "Counter",
    "commands_total",
    "http_requests_total",
    "play_events_total",
    "runtime_capability_toggle_total",
    "runtime_profile_selected_total",
]
