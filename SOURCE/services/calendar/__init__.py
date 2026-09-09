"""
services/calendar - Calendar service module.

Provides the canonical provider-agnostic calendar facade for the
always-on local primary calendar plus Google Calendar, Microsoft Graph,
and CalDAV sync targets.
"""

from __future__ import annotations

from .manager import CalendarEvent, CalendarManager, get_calendar_manager

__all__ = ["CalendarEvent", "CalendarManager", "get_calendar_manager"]
