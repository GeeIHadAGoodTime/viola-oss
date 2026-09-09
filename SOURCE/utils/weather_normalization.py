"""
Helpers for normalising weather payloads across backends and UI layers.

The functions in this module provide a consistent representation of weather
conditions so that different entry points (FastAPI, Qt bridge, web API) can
share behaviour without duplicating classification logic.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Optional zoneinfo (absent from some stripped Python builds); a payload's own
# UTC offset still answers the day question when it is missing.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo, ZoneInfoNotFoundError as _ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - stdlib on every supported build
    _ZoneInfo = None
    _ZoneInfoNotFoundError = Exception

UNKNOWN_CONDITION_CODE = "unknown"

# Strings a provider (or an older cached payload) may hand us in the
# ``condition`` slot that carry no sky information at all. They must never
# reach a client as if they were a real condition: a renderer that treats
# "Unknown" as a condition string ends up picking an icon for it, which turns
# missing data into a confident wrong claim.
NON_CONDITION_TEXTS: frozenset[str] = frozenset(
    {
        "unknown",
        "unknown condition",
        "n/a",
        "na",
        "none",
        "null",
        "not available",
        "unavailable",
        "--",
        "-",
    }
)

# Row collections that carry their own per-entry condition fields.
_CONDITION_ROW_KEYS: tuple[str, ...] = (
    "hourly",
    "hourly_forecast",
    "daily",
    "daily_forecast",
    "forecast",
)

CONDITION_CODE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("thunder", "thunderstorm"),
    ("storm", "storm"),
    ("blizzard", "snow"),
    ("snow", "snow"),
    ("sleet", "sleet"),
    ("hail", "hail"),
    ("rain", "rain"),
    ("drizzle", "rain"),
    ("shower", "rain"),
    ("fog", "fog"),
    ("mist", "fog"),
    ("haze", "fog"),
    ("smoke", "smoke"),
    ("dust", "dust"),
    ("partly", "partly_cloudy"),
    ("cloud", "cloudy"),
    ("overcast", "overcast"),
    ("wind", "wind"),
    ("clear", "clear"),
    ("sunny", "clear"),
)


def parse_timestamp(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(normalized)
    except Exception as e:
        logger.debug("Failed to parse timestamp '%s': %s", value, e, exc_info=True)
        return None


def _timestamp_offset_zone(payload: Mapping[str, Any]) -> datetime.tzinfo | None:
    """Recover a payload's clock from the UTC offset its own timestamps carry.

    Providers that never name a zone still date their rows on the location's
    wall clock (NWS periods arrive as ``...T06:00:00-04:00``). Reading that
    offset back is enough to answer "what day is it there", and it keeps this
    helper useful for payloads written before the zone field existed.
    """
    current = payload.get("current")
    candidates: list[Any] = []
    if isinstance(current, Mapping):
        candidates.extend([current.get("time"), current.get("updated")])
    candidates.append(payload.get("updated"))
    for rows_key in ("hourly", "hourly_forecast"):
        rows = payload.get(rows_key)
        if isinstance(rows, list):
            candidates.extend(row.get("time") for row in rows[:1] if isinstance(row, Mapping))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed = parse_timestamp(candidate)
        if parsed is not None and parsed.tzinfo is not None:
            offset = parsed.utcoffset()
            if offset is not None:
                return datetime.timezone(offset)
    return None


def payload_timezone(payload: Mapping[str, Any]) -> datetime.tzinfo:
    """Return the civil timezone a weather payload's dates are expressed in.

    Forecast rows are bucketed by the calendar day at the *forecast location*,
    so anything asking "is this row today?" has to ask it on that location's
    clock. Falls back to UTC only when the payload carries no clock at all.
    """
    name = payload.get("timezone")
    if isinstance(name, str) and name.strip() and _ZoneInfo is not None:
        try:
            return _ZoneInfo(name.strip())
        except (_ZoneInfoNotFoundError, ValueError, KeyError, OSError) as exc:
            # Not an IANA key (a fixed-offset label like "UTC+09:00", or a zone
            # this machine's tz database does not carry) -- try the next source.
            logger.debug("Weather payload timezone %r is not an IANA zone: %s", name, exc)
    offset_seconds = payload.get("timezone_offset_seconds")
    if isinstance(offset_seconds, (int, float)) and not isinstance(offset_seconds, bool):
        try:
            return datetime.timezone(datetime.timedelta(seconds=int(offset_seconds)))
        except ValueError as exc:
            logger.debug("Weather payload offset %r is out of range: %s", offset_seconds, exc)
    return _timestamp_offset_zone(payload) or datetime.UTC


def payload_local_today(
    payload: Mapping[str, Any],
    *,
    now: datetime.datetime | None = None,
) -> datetime.date:
    """Return today's date at the forecast location, not at UTC."""
    moment = now or datetime.datetime.now(datetime.UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return moment.astimezone(payload_timezone(payload)).date()


def normalize_condition_text(value: Any) -> str | None:
    """Return a real, human-meaningful condition string, or ``None``.

    ``None`` means "the provider did not tell us the sky state". Callers must
    then omit the ``condition`` field entirely rather than substituting a
    placeholder, so downstream renderers can show an honest unknown state.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.casefold() in NON_CONDITION_TEXTS:
        return None
    return text


def normalize_condition_code(value: Any) -> str | None:
    """Return a usable ``condition_code``, or ``None`` when it carries nothing."""
    if not isinstance(value, str):
        return None
    code = value.strip().lower().replace("-", "_")
    if not code or code in {UNKNOWN_CONDITION_CODE, "n/a", "na", "none", "null"}:
        return None
    return code


def apply_condition_honesty(
    entry: dict[str, Any],
    *,
    timestamp: datetime.datetime | None = None,
    set_is_daytime: bool = False,
    mirror_description: bool = False,
) -> dict[str, Any]:
    """Make one weather mapping's condition fields honest, in place.

    Guarantees, for every payload level (top level, ``current``, and each
    hourly/daily row):

    - ``condition``/``description`` are absent when the provider gave us no
      real condition (rather than carrying ``"Unknown"`` or ``""``).
    - ``condition_code`` is ALWAYS present, and is ``"unknown"`` exactly when
      the condition is not known. That single field is what a renderer keys
      its icon off, so "we don't know" is expressible instead of guessable.
    """
    condition_text = normalize_condition_text(entry.get("condition"))
    description_text = normalize_condition_text(entry.get("description"))

    if condition_text is None:
        entry.pop("condition", None)
    else:
        entry["condition"] = condition_text

    if description_text is None:
        entry.pop("description", None)
        # Only the top level mirrors the condition into ``description`` (legacy
        # clients read it there); rows keep the shape they already had.
        if mirror_description and condition_text is not None:
            entry["description"] = condition_text
    else:
        entry["description"] = description_text

    code = normalize_condition_code(entry.get("condition_code"))
    if code is None:
        classified = classify_condition(condition_text or description_text or "", timestamp)
        code = normalize_condition_code(classified["condition_code"])
        if set_is_daytime and entry.get("is_daytime") is None and classified["is_daytime"] is not None:
            entry["is_daytime"] = classified["is_daytime"]
    entry["condition_code"] = code or UNKNOWN_CONDITION_CODE
    return entry


def classify_condition(condition: str, timestamp: datetime.datetime | None) -> dict[str, Any | None]:
    condition_lower = (condition or "").lower()
    code = "unknown"
    for needle, mapped in CONDITION_CODE_PATTERNS:
        if needle in condition_lower:
            code = mapped
            break

    is_daytime: bool | None = None
    if timestamp is not None:
        is_daytime = 6 <= timestamp.hour < 20
    elif "night" in condition_lower:
        is_daytime = False
    elif "day" in condition_lower:
        is_daytime = True

    return {"condition_code": code, "is_daytime": is_daytime}


def normalize_weather_data(
    raw: Mapping[str, Any],
    *,
    source: str,
    cached: bool,
    stale: bool,
    provider: str | None = None,
) -> dict[str, Any]:
    """
    Return a copy of ``raw`` guaranteed to expose the canonical weather schema.

    Args:
        raw: Arbitrary mapping of weather data (from cache, network, etc).
        source: Machine-readable source identifier ("cache", "wttr.in", ...).
        cached: Whether the payload comes from a cache layer.
        stale: Whether the payload may be outdated (e.g., fallback cache).
        provider: Human-friendly provider attribution. Defaults to ``source``.
    """

    normalized: dict[str, Any] = dict(raw)
    normalized["source"] = source
    normalized["cached"] = cached
    normalized["stale"] = stale
    normalized["provider"] = provider or normalized.get("provider") or source
    normalized.setdefault("measurement_system", "imperial")

    updated_value = normalized.get("updated") or normalized.get("updated_at")
    timestamp: datetime.datetime | None
    if isinstance(updated_value, datetime.datetime):
        timestamp = updated_value
        normalized["updated"] = updated_value.isoformat()
    elif isinstance(updated_value, str):
        timestamp = parse_timestamp(updated_value)
        if timestamp is not None:
            normalized["updated"] = timestamp.isoformat()
    else:
        timestamp = None

    # Condition honesty runs at EVERY level and on EVERY path through this
    # function (live fetch, provider merge, and cache reads), because a cached
    # payload written before this rule existed still carries the literal
    # "Unknown" string and is replayed to clients verbatim.
    apply_condition_honesty(normalized, timestamp=timestamp, set_is_daytime=True, mirror_description=True)

    current_mapping = normalized.get("current")
    if isinstance(current_mapping, Mapping):
        normalized["current"] = apply_condition_honesty(dict(current_mapping), timestamp=timestamp)

    for rows_key in _CONDITION_ROW_KEYS:
        rows = normalized.get(rows_key)
        if not isinstance(rows, list):
            continue
        normalized[rows_key] = [
            (
                apply_condition_honesty(dict(row), timestamp=parse_timestamp(row.get("time")))
                if isinstance(row, Mapping)
                else row
            )
            for row in rows
        ]

    normalized.setdefault("unit", normalized.get("unit") or "F")
    normalized.setdefault("temperature_c", normalized.get("temperature_c"))
    if normalized.get("temperature") is not None:
        normalized.setdefault("temperature_f", normalized.get("temperature"))
        normalized.setdefault("temp_f", normalized.get("temperature"))
    current = normalized.get("current")
    if isinstance(current, Mapping):
        for source_key, aliases in (
            ("feels_like_f", ("feels_like", "apparent_f")),
            ("wind_speed_mph", ("wind_speed", "wind_mph")),
            ("humidity", ("humidity",)),
        ):
            value = current.get(source_key)
            if value is None and source_key == "feels_like_f":
                value = current.get("feels_like")
            if value is None and source_key == "wind_speed_mph":
                value = current.get("wind_speed")
            if value is not None:
                for alias in aliases:
                    normalized.setdefault(alias, value)

    forecast = normalized.get("daily_forecast") or normalized.get("daily") or normalized.get("forecast")
    if isinstance(forecast, list) and forecast and isinstance(forecast[0], Mapping):
        today = forecast[0]
        high = today.get("high_f") if today.get("high_f") is not None else today.get("high")
        low = today.get("low_f") if today.get("low_f") is not None else today.get("low")
        if high is not None:
            normalized.setdefault("forecast_high_f", high)
            normalized.setdefault("high_f", high)
        if low is not None:
            normalized.setdefault("forecast_low_f", low)
            normalized.setdefault("low_f", low)

    return normalized


__all__ = [
    "NON_CONDITION_TEXTS",
    "UNKNOWN_CONDITION_CODE",
    "apply_condition_honesty",
    "classify_condition",
    "normalize_condition_code",
    "normalize_condition_text",
    "normalize_weather_data",
    "parse_timestamp",
    "payload_local_today",
    "payload_timezone",
]
