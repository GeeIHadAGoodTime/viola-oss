from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from cache.weather_cache import WeatherCache
from fastapi import APIRouter, FastAPI
from ui.core.bindings import Bindings
from ui.core.metrics import Counter
from ui.core.security import SecurityContext
from ui.security import ErrorSanitizer, InputValidator, ResourceLimits
from ui.ux_manager import UXEnhancementManager
from ui.websocket.event_hub import EventHub

P = ParamSpec("P")
R = TypeVar("R")
RateLimitDecorator = Callable[[str], Callable[[Callable[P, R]], Callable[P, R]]]


@dataclass
class ApiContext:
    app: FastAPI
    router: APIRouter
    bindings: Bindings
    hub: EventHub
    security: SecurityContext
    weather_cache: WeatherCache
    resource_limits: ResourceLimits
    error_sanitizer: ErrorSanitizer
    input_validator: InputValidator
    rate_limit: RateLimitDecorator
    commands_total: Counter
    play_events_total: Counter
    http_requests_total: Counter
    ux_manager: UXEnhancementManager | None
    monitoring_router: APIRouter
    rate_limit_default: str | None
    rate_limiter_enabled: bool
    command_service: Any = None
