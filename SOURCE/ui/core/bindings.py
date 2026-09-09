from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from cache.weather_cache import WeatherCache


@dataclass
class Bindings:
    state: Any
    music: Any
    intent: Any
    hub: Any  # Avoid tight coupling to WebSocket implementations
    weather_cache: WeatherCache | None = None


@dataclass
class BroadcasterTaskState:
    task: asyncio.Task[Any] | None = None
    should_stop: bool = False


__all__ = ["Bindings", "BroadcasterTaskState"]
