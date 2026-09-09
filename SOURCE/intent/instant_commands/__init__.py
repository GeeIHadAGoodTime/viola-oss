"""Instant command package exports."""

from __future__ import annotations

from ..instant_commands_patterns import INSTANT_PATTERNS
from .handlers import InstantCommandHandlers
from .runtime import InstantCommandHandler

__all__ = [
    "INSTANT_PATTERNS",
    "InstantCommandHandler",
    "InstantCommandHandlers",
]
