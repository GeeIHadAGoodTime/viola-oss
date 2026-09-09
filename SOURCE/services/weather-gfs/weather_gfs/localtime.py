"""Resolve the civil timezone a forecast coordinate actually lives in.

GFS is a global model published on the UTC clock, but "Thursday's high" is a
statement about a *calendar day where the user is standing*, not about a UTC
day. Bucketing the model's hours on ``timestamp.date()`` while ``timestamp`` is
UTC therefore mislabels every location whose civil offset is not zero: Tokyo's
early morning (UTC+9) belongs to the previous UTC date, and a US evening
(UTC-4..-10) belongs to the next one. The daily minimum in particular lands on
the wrong day, because it happens near dawn -- exactly where the UTC boundary
cuts for Asia.

Everything in this module exists so a forecast row can be keyed on the day the
user would call it. ``timezone_for`` answers "which civil timezone is this
coordinate in"; ``local_day`` answers "which calendar day is this instant, over
there".

The coordinate lookup is delegated to ``tzfpy`` (the published Python binding
for ``tzf-rs``, which carries the timezone-boundary-builder polygons); the
offset arithmetic including daylight saving comes from the standard library's
``zoneinfo``. Neither is reimplemented here.
"""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Nautical time: at sea there is no civil timezone, and the convention is a
# whole-hour offset per 15 degrees of longitude. UTC-12..UTC+14 is the range
# real civil offsets span, so clamping there keeps a bad coordinate from
# producing an offset no calendar uses.
_MIN_OFFSET_HOURS = -12
_MAX_OFFSET_HOURS = 14


def local_day(timestamp: dt.datetime, tz: dt.tzinfo) -> dt.date:
    """Return the calendar date *timestamp* falls on, read off ``tz``'s wall clock.

    A naive ``timestamp`` is treated as UTC, matching how the GFS cycle base
    time is stored.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.UTC)
    return timestamp.astimezone(tz).date()


def nautical_zone(longitude: float) -> dt.tzinfo:
    """Return the whole-hour nominal zone for *longitude*.

    This is the answer for a point with no civil timezone at all (open ocean),
    and the fallback when the boundary lookup cannot resolve one. It is a real
    convention rather than a placeholder: UTC would claim a Pacific coordinate's
    day starts at local noon, whereas the nominal zone puts it near local
    midnight.
    """
    offset_hours = round(longitude / 15.0)
    offset_hours = max(_MIN_OFFSET_HOURS, min(_MAX_OFFSET_HOURS, offset_hours))
    return dt.timezone(dt.timedelta(hours=offset_hours))


@lru_cache(maxsize=4096)
def timezone_for(latitude: float, longitude: float) -> dt.tzinfo:
    """Return the civil timezone covering ``(latitude, longitude)``.

    Falls back to :func:`nautical_zone` when the coordinate is not inside any
    timezone polygon (at sea) or the lookup is unavailable. It never falls back
    to UTC for a non-zero longitude, because that is the failure this module
    exists to prevent.
    """
    try:
        # Imported lazily: it is a compiled extension carrying the boundary
        # data, and the pure date arithmetic above must stay importable (and
        # testable) without it.
        import tzfpy
    except ImportError:
        logger.warning("tzfpy is unavailable; falling back to nominal longitude zones")
        return nautical_zone(longitude)

    try:
        name = tzfpy.get_tz(longitude, latitude)
    except (ValueError, TypeError, OverflowError) as exc:
        logger.debug("timezone lookup failed for (%.4f, %.4f): %s", latitude, longitude, exc)
        name = None
    if not name:
        return nautical_zone(longitude)
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.debug("no tz database entry for %r: %s", name, exc)
        return nautical_zone(longitude)


def zone_label(tz: dt.tzinfo, *, at: dt.datetime | None = None) -> str:
    """Return the name a consumer can rebuild *tz* from.

    An IANA key when there is one (``"Asia/Tokyo"``), so a consumer gets the
    daylight-saving rules too; otherwise a fixed-offset label (``"UTC+09:00"``)
    that says plainly what the offset is.
    """
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    total_minutes = zone_offset_seconds(tz, at=at) // 60
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return "UTC%s%02d:%02d" % (sign, total_minutes // 60, total_minutes % 60)


def zone_offset_seconds(tz: dt.tzinfo, *, at: dt.datetime | None = None) -> int:
    """Return *tz*'s UTC offset in seconds at *at* (default: now)."""
    moment = at or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    offset = moment.astimezone(tz).utcoffset() or dt.timedelta(0)
    return int(offset.total_seconds())


__all__ = [
    "local_day",
    "nautical_zone",
    "timezone_for",
    "zone_label",
    "zone_offset_seconds",
]
