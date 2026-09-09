"""
Calendar DateTime Utilities

Extracted from calendar.py to reduce file size.
Handles date/time normalization and parsing.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

from core.logging_config import get_logger

logger = get_logger(__name__)

_ZoneInfoCtor = Callable[[str], datetime.tzinfo]
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except Exception:  # pragma: no cover - optional Windows fallback
    _zone_info: _ZoneInfoCtor | None = None
else:
    _zone_info = _ZoneInfo


class CalendarDateTimeUtils:
    """Utilities for calendar date/time operations."""

    @staticmethod
    def normalise_datetime(value: datetime.datetime) -> datetime.datetime:
        """
        Normalise datetime to UTC with timezone info.

        Args:
            value: Input datetime

        Returns:
            Normalised datetime in UTC
        """
        if value.tzinfo is None:
            # User-entered calendar times without an offset are local wall-clock
            # times in THIS USER's timezone, not UTC and not the server's zone
            # (#3557: a cloud container is UTC, so "2pm" was stored as 14:00Z
            # for a user in America/Chicago and rendered back as 9:00 AM).
            local_tz = CalendarDateTimeUtils.get_user_display_timezone()
            return value.replace(tzinfo=local_tz).astimezone(datetime.UTC)
        elif hasattr(value, "utcoffset") and value.utcoffset() is not None:
            # Has timezone, convert to UTC
            return value.astimezone(datetime.UTC)
        else:
            # Already UTC or timezone-naive, ensure UTC
            return value.replace(tzinfo=datetime.UTC)

    @staticmethod
    def parse_iso_datetime(value: str) -> datetime.datetime | None:
        """
        Parse ISO datetime string to datetime object.

        Args:
            value: ISO datetime string

        Returns:
            Parsed datetime or None if invalid
        """
        if not value:
            return None

        try:
            # Handle various ISO formats
            if "T" in value:
                # ISO 8601 format
                if value.endswith("Z"):
                    return datetime.datetime.fromisoformat(value[:-1]).replace(tzinfo=datetime.UTC)
                elif "+" in value or "-" in value[-6:]:
                    # Has timezone
                    return datetime.datetime.fromisoformat(value)
                else:
                    # No timezone, interpret as configured local calendar time.
                    return CalendarDateTimeUtils.normalise_datetime(datetime.datetime.fromisoformat(value))
            else:
                # Date only, assume start of day in configured local calendar time.
                date_obj = datetime.datetime.strptime(value, "%Y-%m-%d")
                return CalendarDateTimeUtils.normalise_datetime(date_obj)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def format_datetime_for_storage(dt: datetime.datetime) -> str:
        """
        Format datetime for storage in ISO format.

        Args:
            dt: Datetime to format

        Returns:
            ISO formatted string
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt.isoformat()

    @staticmethod
    def get_timezone(tz_name: str) -> datetime.tzinfo | None:
        """
        Get timezone object from name.

        Args:
            tz_name: Timezone name

        Returns:
            Timezone object or None
        """
        if _zone_info is None:
            return None
        try:
            return _zone_info(tz_name)
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return None

    @classmethod
    def get_display_timezone(cls, tz_name: str | None) -> datetime.tzinfo:
        """Resolve a user-visible calendar timezone."""

        name = (tz_name or "").strip()
        if name and name.lower() != "auto":
            tz = cls.get_timezone(name)
            if tz is not None:
                return tz
        try:
            local_tz = datetime.datetime.now().astimezone().tzinfo
            if local_tz is not None:
                return local_tz
        except Exception as e:
            logger.debug("Local timezone detection failed: %s", e, exc_info=True)
        return datetime.UTC

    @classmethod
    def get_user_display_timezone(cls) -> datetime.tzinfo:
        """Resolve the display timezone for the user of the current request.

        Prefers the per-user zone bound by ``services.user_timezone`` and falls
        back to the deployment's configured calendar timezone, which is what
        every call site used unconditionally before #3557.
        """

        from services.user_timezone import active_timezone

        return active_timezone()

    @classmethod
    def to_display_timezone(
        cls,
        value: datetime.datetime,
        tz_name: str | None,
    ) -> datetime.datetime:
        """Convert a datetime to the configured display timezone."""

        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.astimezone(cls.get_display_timezone(tz_name))

    @classmethod
    def to_user_display_timezone(cls, value: datetime.datetime) -> datetime.datetime:
        """Convert a datetime into the current user's display timezone."""

        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=datetime.UTC)
        return value.astimezone(cls.get_user_display_timezone())
