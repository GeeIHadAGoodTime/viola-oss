"""
Core System Metrics

Lightweight in-memory counters for system telemetry.
These metrics are used across the application for observability.
"""

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


# Runtime profile and capability metrics
runtime_profile_selected_total = Counter("viola_runtime_profile_selected_total", labels=["profile", "override"])
runtime_capability_toggle_total = Counter("viola_runtime_capability_toggle_total", labels=["capability", "value"])

__all__ = [
    "Counter",
    "runtime_capability_toggle_total",
    "runtime_profile_selected_total",
]
