"""
Canonical TypedDict definitions for structured return types.

This module provides proper type definitions for common structured data
instead of using weak `dict[str, Any]` types.

Usage:
    from core.types import CommandResult, PlayerStateDict

    def execute_command() -> CommandResult:
        return {"success": True, "intent": "play", "data": {"track": "..."}}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict

# ========================================================================= #
# Command/Response Types
# ========================================================================= #


class CommandResult(TypedDict):
    """Result of executing a command (play, pause, etc.)."""

    success: bool
    intent: str
    message: NotRequired[str]
    error: NotRequired[str]
    data: NotRequired[dict[str, str | int | bool | None]]


class APIResponse(TypedDict):
    """Standard API response envelope."""

    ok: bool
    data: NotRequired[dict[str, str | int | bool | list[str] | None]]
    error: NotRequired[str]
    code: NotRequired[str]
    message: NotRequired[str]


# ========================================================================= #
# Player/Playback State Types
# ========================================================================= #


class PlayerStateDict(TypedDict):
    """Player state snapshot."""

    is_playing: bool
    is_paused: bool
    position: float
    duration: NotRequired[float]
    volume: int
    track: NotRequired[str]
    artist: NotRequired[str]
    title: NotRequired[str]


class QueueItemDict(TypedDict):
    """Single item in the play queue."""

    id: str
    title: str
    artist: NotRequired[str]
    duration: NotRequired[float]
    url: NotRequired[str]


class QueueStateDict(TypedDict):
    """Queue state snapshot."""

    items: list[QueueItemDict]
    current_index: int
    total: int


# ========================================================================= #
# Metrics/Diagnostics Types
# ========================================================================= #


class MetricsDict(TypedDict):
    """Generic metrics container."""

    count: NotRequired[int]
    total: NotRequired[int]
    rate: NotRequired[float]
    latency_ms: NotRequired[float]
    success_rate: NotRequired[float]
    error_count: NotRequired[int]


class CacheStatsDict(TypedDict):
    """Cache statistics."""

    hits: int
    misses: int
    size: int
    max_size: int
    hit_rate: NotRequired[float]


class DiagnosticsDict(TypedDict):
    """Standard diagnostics return type."""

    status: str
    timestamp: float
    metrics: NotRequired[MetricsDict]
    errors: NotRequired[list[str]]
    warnings: NotRequired[list[str]]


class HealthCheckDict(TypedDict):
    """Health check result."""

    status: str
    component: str
    message: NotRequired[str]
    latency_ms: NotRequired[float]
    details: NotRequired[dict[str, str | int | bool | None]]


# ========================================================================= #
# Auth Types
# ========================================================================= #


class AuthStatusDict(TypedDict):
    """Authentication status."""

    authenticated: bool
    user_id: NotRequired[str]
    provider: NotRequired[str]
    expires_at: NotRequired[float]


class TokenDict(TypedDict):
    """OAuth token data."""

    access_token: str
    refresh_token: NotRequired[str]
    token_type: str
    expires_in: NotRequired[int]
    scope: NotRequired[str]


@dataclass(frozen=True, slots=True)
class UserContext:
    """Authenticated request context."""

    user_id: str
    session_id: str | None = None
    device_id: str | None = None
    request_id: str | None = None


# ========================================================================= #
# Weather Types
# ========================================================================= #


class WeatherConditionDict(TypedDict):
    """Weather condition data."""

    temperature: float
    feels_like: NotRequired[float]
    humidity: NotRequired[int]
    description: str
    icon: NotRequired[str]


class WeatherDict(TypedDict):
    """Weather API response."""

    city: str
    country: NotRequired[str]
    current: WeatherConditionDict
    forecast: NotRequired[list[WeatherConditionDict]]
    last_updated: float


# ========================================================================= #
# Voice/Wake Types
# ========================================================================= #


class TranscriptionDict(TypedDict):
    """Speech-to-text result."""

    text: str
    confidence: float
    language: NotRequired[str]
    duration_ms: NotRequired[float]


class WakeEventDict(TypedDict):
    """Wake word detection event."""

    detected: bool
    confidence: float
    keyword: NotRequired[str]
    timestamp: float


# ========================================================================= #
# Plugin Types
# ========================================================================= #


class PluginResponseDict(TypedDict):
    """Plugin handler response."""

    speech: str
    display: NotRequired[dict[str, str | int | bool | list[str] | None]]
    state_updates: NotRequired[list[dict[str, str | int | bool | None]]]
    error: NotRequired[str]


class PluginInfoDict(TypedDict):
    """Plugin metadata."""

    name: str
    version: str
    description: str
    author: str
    enabled: bool
    capabilities: list[str]


class RegistryEntryDict(TypedDict):
    """Plugin registry entry."""

    name: str
    description: str
    author: str
    repo_url: str
    version: str
    min_viola_version: NotRequired[str]
    tags: NotRequired[list[str]]
    intent_keywords: NotRequired[list[str]]


__all__ = [
    "APIResponse",
    "AuthStatusDict",
    "CacheStatsDict",
    "CommandResult",
    "DiagnosticsDict",
    "HealthCheckDict",
    "MetricsDict",
    "PlayerStateDict",
    # Plugin types
    "PluginInfoDict",
    "PluginResponseDict",
    "QueueItemDict",
    "QueueStateDict",
    "RegistryEntryDict",
    "TokenDict",
    "TranscriptionDict",
    "UserContext",
    "WakeEventDict",
    "WeatherConditionDict",
    "WeatherDict",
]
