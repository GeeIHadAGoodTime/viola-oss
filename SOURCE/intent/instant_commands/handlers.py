"""Composed InstantCommandHandlers class built from domain mixins."""

from __future__ import annotations

from ._base import BaseInstantCommandHandlersMixin
from .calendar import CalendarHandlersMixin
from .info import InfoHandlersMixin
from .music import MusicHandlersMixin
from .smart_home import SmartHomeHandlersMixin
from .system import SystemHandlersMixin
from .timers import TimersHandlersMixin
from .weather import WeatherHandlersMixin


class InstantCommandHandlers(
    SmartHomeHandlersMixin,
    InfoHandlersMixin,
    CalendarHandlersMixin,
    WeatherHandlersMixin,
    TimersHandlersMixin,
    SystemHandlersMixin,
    MusicHandlersMixin,
    BaseInstantCommandHandlersMixin,
):
    """Domain-composed instant command handler implementation."""

    pass
