"""Agent tool handler for weather lookup.

Provides a single ``weather_handler`` so the AI agent can answer weather
queries with Viola's canonical weather fetcher, falling back to the local
``/v1/weather`` endpoint for saved-location resolution.
"""

from __future__ import annotations

import datetime
import os
import re
from collections.abc import Mapping
from typing import Any

from core.constants import TIMEOUT_EXTENDED
from core.logging_config import get_logger
from intent.tool_types import ToolResult

_log = get_logger(__name__)

_MISSING_CONDITIONS = {"", "unknown", "unavailable", "not available", "n/a", "none"}
_TRAILING_DATE_SUFFIXES = (
    re.compile(r"\s+(?:today|tonight|tomorrow)\s*$", re.IGNORECASE),
    re.compile(
        r"\s+(?:(?:on|for)\s+)?(?:(?:this|next)\s+)?"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s*$",
        re.IGNORECASE,
    ),
)


def _weather_base_url() -> str:
    env_base_url = os.environ.get("VIOLA_BASE_URL", "").strip()
    if env_base_url:
        return env_base_url.rstrip("/")

    for key in ("VIOLA_API_PORT", "VIOLA_PORT"):
        raw_port = os.environ.get(key, "").strip()
        if not raw_port:
            continue
        try:
            port = int(raw_port)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            try:
                from config.settings import settings

                scheme = "https" if getattr(settings, "ssl_enabled", False) else "http"
            except (AttributeError, ImportError, RuntimeError, ValueError):
                scheme = "http"
            return "%s://127.0.0.1:%s" % (scheme, port)

    from config.settings import get_runtime_base_url

    return get_runtime_base_url().rstrip("/")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload_temperature(payload: Mapping[str, Any]) -> Any:
    current = _as_mapping(payload.get("current"))
    for key in ("temperature_f", "temp_f", "temperature"):
        value = payload.get(key)
        if value is not None:
            return value
    for key in ("temperature_f", "temp_f", "temperature", "temp"):
        value = current.get(key)
        if value is not None:
            return value
    return None


def _payload_condition(payload: Mapping[str, Any]) -> str:
    current = _as_mapping(payload.get("current"))
    value = payload.get("condition") or payload.get("condition_text") or current.get("condition") or ""
    return str(value)


def _payload_has_weather(payload: Mapping[str, Any]) -> bool:
    condition = _payload_condition(payload).strip().lower()
    return _payload_temperature(payload) is not None or condition not in _MISSING_CONDITIONS


def _normalize_weather_city_argument(city: str) -> str:
    normalized = " ".join(city.strip().split())
    if not normalized:
        return ""
    for pattern in _TRAILING_DATE_SUFFIXES:
        candidate = pattern.sub("", normalized).rstrip(" ,")
        if candidate and candidate != normalized:
            return candidate
    return normalized


async def _fetch_direct_weather_payload(city: str) -> Mapping[str, Any] | None:
    """Fetch explicit-city weather from the canonical backend without local HTTP."""
    if not city.strip():
        return None

    from backend.weather_fetch import fetch_weather

    for force_refresh in (False, True):
        payload = await fetch_weather(city=city.strip(), force_refresh=force_refresh)
        if isinstance(payload, Mapping) and _payload_has_weather(payload):
            return payload
    return None


async def _fetch_endpoint_weather_payload(
    city: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    import httpx

    from ui.security.bootstrap import load_bootstrap_api_key

    url = "%s/v1/weather" % _weather_base_url()
    params = {}
    if city.strip():
        params["city"] = city.strip()
    headers = {}
    api_key = load_bootstrap_api_key()
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_EXTENDED) as client:
            resp = await client.get(url, params=params, headers=headers)
            data = resp.json()
    except httpx.HTTPError as exc:
        return None, "Weather request failed: %s" % exc
    except ValueError as exc:
        return None, "Weather response was not valid JSON: %s" % exc

    if resp.status_code != 200 or not data.get("ok"):
        error_msg = data.get("error") or data.get("message") or "Weather request failed"
        return None, str(error_msg)

    payload = data.get("data") or {}
    if not isinstance(payload, Mapping):
        return None, "Weather response missing data"
    return payload, None


def _round_number(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _forecast_reference_date(
    payload: Mapping[str, Any],
    *,
    now: datetime.datetime | None = None,
) -> datetime.date | None:
    """Today's date at the forecast location -- the day a row must match to be "Today".

    Asked on the location's own clock, never on UTC and never on this machine's
    (#4216). A user in Tokyo asking at 08:00 local is asking about a day that
    is still yesterday in UTC, and the payload's rows are dated on their
    calendar; matching them against the UTC date labels tomorrow's row "Today".

    It is also asked about *now*, not about when the payload was written. A
    cached payload from two days ago has no "Today" in it, and calling its
    first row Today would tell the user that a stale high is this afternoon's.

    ``now`` defaults to the real clock and exists so a caller can pin the
    instant, which is what makes a day-boundary assertion reproducible.
    """
    try:
        from utils.weather_normalization import payload_local_today

        return payload_local_today(payload, now=now)
    except (ImportError, TypeError, ValueError, OSError) as exc:
        # The payload named no usable clock; fall back to the timestamps it
        # does carry rather than answering with this machine's date.
        _log.debug("Weather payload carried no resolvable timezone: %s", exc)
    for key in ("updated", "as_of", "observed_at"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.date.fromisoformat(raw.strip()[:10])
            except ValueError:
                continue
    return None


def _forecast_day_label(row: Mapping[str, Any], ref_date: datetime.date | None, index: int) -> str:
    """Relative/weekday label for a forecast row. Derived from the row's own date, never the query."""
    row_date: datetime.date | None = None
    raw_date = str(row.get("date") or "")[:10]
    if raw_date:
        try:
            row_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            row_date = None
    if ref_date is not None and row_date is not None:
        delta = (row_date - ref_date).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Tomorrow"
    weekday = row.get("day")
    if isinstance(weekday, str) and weekday.strip():
        return weekday.strip()
    if row_date is not None:
        return row_date.strftime("%A")
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    return "Day %d" % (index + 1)


def _normalize_daily_forecast(
    forecast: Any,
    payload: Mapping[str, Any],
    *,
    limit: int = 7,
    now: datetime.datetime | None = None,
) -> list[dict[str, Any]]:
    """Flatten the fetched multi-day forecast into a compact, model-usable list.

    The canonical weather payload already carries a multi-day ``daily_forecast``
    (NOAA GFS / NWS). Surfacing it lets the model answer tomorrow / this-weekend /
    named-day questions from this tool alone instead of falling through to search.

    ``now`` is passed straight through to :func:`_forecast_reference_date`.
    """
    if not isinstance(forecast, list):
        return []
    ref_date = _forecast_reference_date(payload, now=now)
    days: list[dict[str, Any]] = []
    for index, row in enumerate(forecast[:limit]):
        if not isinstance(row, Mapping):
            continue
        high = _round_number(row.get("high_f") if row.get("high_f") is not None else row.get("high"))
        low = _round_number(row.get("low_f") if row.get("low_f") is not None else row.get("low"))
        condition_raw = row.get("condition") or row.get("summary") or ""
        condition = str(condition_raw).strip()
        precip = row.get("precip_chance")
        if precip is None:
            precip = row.get("precipitation_probability")
        precip = _round_number(precip)
        entry = {
            "label": _forecast_day_label(row, ref_date, index),
            "date": (str(row.get("date") or "")[:10] or None),
            "high_f": high,
            "low_f": low,
            "condition": condition or None,
            "precip_chance": precip,
        }
        days.append({key: value for key, value in entry.items() if value is not None})
    return days


def _format_forecast_summary(days: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for day in days:
        segment = str(day.get("label") or "Upcoming")
        high = day.get("high_f")
        low = day.get("low_f")
        if high is not None and low is not None:
            segment += " high %s degrees F, low %s degrees F" % (high, low)
        elif high is not None:
            segment += " high %s degrees F" % high
        elif low is not None:
            segment += " low %s degrees F" % low
        condition = day.get("condition")
        if condition:
            segment += ", %s" % condition.lower()
        precip = day.get("precip_chance")
        if precip is not None and precip >= 20:
            segment += " (%s%% chance of rain)" % precip
        parts.append(segment)
    return "; ".join(parts)


def _result_from_weather_payload(payload: Mapping[str, Any], city: str) -> ToolResult:
    location = payload.get("location") or payload.get("city") or city or "your location"
    temp_f = _payload_temperature(payload)
    condition = _payload_condition(payload)
    if not _payload_has_weather(payload):
        return ToolResult(ok=False, data=None, error="Weather data unavailable for %s." % location)

    current = _as_mapping(payload.get("current"))
    feels_like = payload.get("feels_like_f") or payload.get("apparent_f") or current.get("feels_like_f")
    wind = payload.get("wind_mph") or payload.get("wind_speed_mph") or current.get("wind_speed_mph")
    humidity = payload.get("humidity")
    forecast_high = payload.get("forecast_high_f") or payload.get("high_f")
    forecast_low = payload.get("forecast_low_f") or payload.get("low_f")
    forecast = payload.get("daily_forecast") or payload.get("forecast") or []
    if (forecast_high is None or forecast_low is None) and forecast and isinstance(forecast[0], dict):
        today = forecast[0]
        forecast_high = forecast_high if forecast_high is not None else today.get("high_f") or today.get("high")
        forecast_low = forecast_low if forecast_low is not None else today.get("low_f") or today.get("low")

    daily = _normalize_daily_forecast(forecast, payload)

    parts = []
    if temp_f is not None and condition:
        parts.append("%s is %s degrees F and %s" % (location, round(float(temp_f)), condition.lower()))
    elif temp_f is not None:
        parts.append("%s is %s degrees F" % (location, round(float(temp_f))))
    elif condition:
        parts.append("%s - %s" % (location, condition))

    details = []
    if feels_like is not None:
        details.append("feels like %s degrees F" % round(float(feels_like)))
    if wind is not None:
        details.append("%s mph wind" % round(float(wind)))
    if humidity is not None:
        details.append("%s%% humidity" % int(humidity))

    if details:
        parts.append(", ".join(details) + ".")
    elif parts:
        parts[-1] = parts[-1] + "."

    if daily:
        parts.append("Forecast: " + _format_forecast_summary(daily) + ".")
    elif forecast_high is not None and forecast_low is not None:
        parts.append(
            "Today: high %s degrees F, low %s degrees F." % (round(float(forecast_high)), round(float(forecast_low)))
        )

    message = " ".join(parts) if parts else "Weather data unavailable for %s." % location

    return ToolResult(
        ok=True,
        data={
            "message": message,
            "location": location,
            "temperature_f": temp_f,
            "condition": condition,
            "feels_like_f": feels_like,
            "wind_mph": wind,
            "humidity": humidity,
            "forecast_high_f": forecast_high,
            "forecast_low_f": forecast_low,
            "daily_forecast": daily,
        },
    )


async def weather_handler(city: str = "") -> ToolResult:
    """Return the current weather for a city or the user's saved location."""
    requested_city = _normalize_weather_city_argument(city)
    try:
        if requested_city:
            direct_payload = await _fetch_direct_weather_payload(requested_city)
            if direct_payload is not None:
                return _result_from_weather_payload(direct_payload, requested_city)

        payload, error = await _fetch_endpoint_weather_payload(requested_city)
    except (ImportError, RuntimeError, ValueError, OSError) as exc:
        return ToolResult(ok=False, data=None, error="Failed to fetch weather: %s" % exc)

    if error:
        return ToolResult(ok=False, data=None, error=error)
    return _result_from_weather_payload(payload or {}, requested_city)
