"""Canonical weather fetch logic â€” single source of truth for weather API access.

Every code path that needs to fetch weather data MUST call ``fetch_weather()``
from this module.  The function handles:

- Provider selection: self-hosted NOAA GFS primary, NWS US overlay, wttr.in fallback
- NOAA GFS: internal weather-gfs service for global 10-day forecasts
- NWS: api.weather.gov current observations + short-range forecast overlay (US only)
- wttr.in: last-resort global fallback
- Response parsing (current conditions, location, forecast)
- Condition classification and normalization
- Location auto-detection fallback

Callers:
    - ``ui/api/routes/weather.py``  â€” GET /v1/weather endpoint
    - ``ui/server.py``              â€” startup prefetch
"""

from __future__ import annotations

import asyncio
import datetime
import math
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from config.settings import settings
from core.constants import TIMEOUT_10_MINUTES, TIMEOUT_HOUR, TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from services.api_cache import get_api_cache, public_cache_key
from utils.weather_normalization import (
    UNKNOWN_CONDITION_CODE,
    classify_condition,
    normalize_condition_text,
    normalize_weather_data,
    payload_local_today,
    payload_timezone,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# NWS constants
# ---------------------------------------------------------------------------

_NWS_USER_AGENT = "Viola/1.0 (github.com/user/viola)"
_NWS_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SELF_GFS_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_GEOCODE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_AIRNOW_REPORTING_AREA_URL = "https://files.airnowtech.org/airnow/today/reportingarea.dat"
_AIRNOW_REPORTING_AREA_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
_AIRNOW_REPORTING_AREA_CACHE_KEY = "weather:airnow:reportingarea:v1"
_NWS_HOURLY_LIMIT = 72
_NWS_DAILY_LIMIT = 7
_GEOCODE_CACHE_TTL_SECONDS = int(TIMEOUT_HOUR * 24 * 7)
_GEOCODE_NEGATIVE_CACHE_TTL_SECONDS = int(TIMEOUT_HOUR)
_WEATHER_FETCH_CACHE_TTL_SECONDS = int(TIMEOUT_10_MINUTES)
_GEOCODE_NEGATIVE_HIT = object()
_NOMINATIM_APP_LIMIT_SCOPE = "weather.nominatim"
_NOMINATIM_APP_LIMIT_IDENTIFIER = "public"
_NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
_NOMINATIM_THROTTLE_LOCK: asyncio.Lock | None = None
_NOMINATIM_LAST_REQUEST_AT = 0.0

_US_STATES_2LETTER: frozenset[str] = frozenset(
    {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "PR",
        "VI",
        "GU",
        "AS",
        "MP",
    }
)

_US_STATE_NAMES: frozenset[str] = frozenset(
    {
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "georgia",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
        "district of columbia",
        "puerto rico",
    }
)

# ---------------------------------------------------------------------------
# US detection helpers
# ---------------------------------------------------------------------------


def _is_likely_us(city: str) -> bool:
    """Heuristic: does *city* look like a US location?

    False negatives just use wttr.in (still works).  False positives hit NWS
    which returns an error, then we fall back to wttr.in.
    """
    if not city:
        return False

    text = city.strip()

    # US zip code (5-digit or 5+4)
    if re.match(r"^\d{5}(-\d{4})?$", text):
        return True

    lower = text.lower()
    if "usa" in lower or "united states" in lower:
        return True

    # "City, ST" or "City, State Name"
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 2:
        last = parts[-1].strip()
        if last.upper() in _US_STATES_2LETTER:
            return True
        if last.lower() in _US_STATE_NAMES:
            return True

    return False


def _is_us_coordinates(lat: float, lon: float) -> bool:
    """Check if coordinates fall within US bounds (continental + AK/HI/PR)."""
    # Continental US
    if 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0:
        return True
    # Alaska
    if 51.0 <= lat <= 72.0 and -180.0 <= lon <= -129.0:
        return True
    # Hawaii
    if 18.0 <= lat <= 23.0 and -161.0 <= lon <= -154.0:
        return True
    # Puerto Rico / USVI
    if 17.5 <= lat <= 18.6 and -68.0 <= lon <= -64.0:
        return True
    return False


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_value(value: Any, digits: int = 0) -> int | float | None:
    number = _coerce_float(value)
    if number is None:
        return None
    if digits <= 0:
        return round(number)
    return round(number, digits)


def _air_quality_category_us_aqi(value: Any) -> str | None:
    aqi = _coerce_float(value)
    if aqi is None:
        return None
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for sensitive groups"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very unhealthy"
    return "Hazardous"


def _distance_miles(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Great-circle distance between two WGS84 coordinate pairs."""
    radius_miles = 3958.8
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    delta_phi = math.radians(lat_b - lat_a)
    delta_lambda = math.radians(lon_b - lon_a)
    haversine = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
    return radius_miles * 2.0 * math.atan2(math.sqrt(haversine), math.sqrt(1.0 - haversine))


def _parse_airnow_reporting_area(text: str) -> list[dict[str, Any]]:
    """Parse AirNow's public reportingarea.dat file into hourly observation rows."""
    rows: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 17:
            continue

        data_type = parts[5].strip().upper()
        if data_type != "O":
            continue

        state_code = parts[8].strip().upper()
        if state_code not in _US_STATES_2LETTER:
            continue

        lat = _coerce_float(parts[9].strip())
        lon = _coerce_float(parts[10].strip())
        aqi = _round_value(parts[12].strip())
        if lat is None or lon is None or aqi is None:
            continue

        rows.append(
            {
                "issue_date": parts[0].strip(),
                "valid_date": parts[1].strip(),
                "valid_time": parts[2].strip(),
                "time_zone": parts[3].strip(),
                "record_sequence": parts[4].strip(),
                "data_type": data_type,
                "primary": parts[6].strip().upper() == "Y",
                "reporting_area": parts[7].strip(),
                "state_code": state_code,
                "latitude": lat,
                "longitude": lon,
                "pollutant": parts[11].strip(),
                "aqi": aqi,
                "category": parts[13].strip() or _air_quality_category_us_aqi(aqi),
                "action_day": parts[14].strip(),
                "discussion": parts[15].strip(),
                "source": parts[16].strip(),
            }
        )
    return rows


def _airnow_group_key(row: Mapping[str, Any]) -> tuple[str, str, float, float]:
    return (
        str(row.get("reporting_area") or ""),
        str(row.get("state_code") or ""),
        float(row.get("latitude") or 0.0),
        float(row.get("longitude") or 0.0),
    )


def _select_airnow_observation(rows: list[dict[str, Any]], lat: float, lon: float) -> dict[str, Any] | None:
    max_distance = _coerce_float(getattr(settings, "weather_air_quality_airnow_max_distance_miles", 100.0)) or 100.0
    nearby: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        row_lat = _coerce_float(row.get("latitude"))
        row_lon = _coerce_float(row.get("longitude"))
        if row_lat is None or row_lon is None:
            continue
        distance = _distance_miles(lat, lon, row_lat, row_lon)
        if distance <= max_distance:
            nearby.append((distance, row))

    if not nearby:
        return None

    distance, nearest = min(nearby, key=lambda item: item[0])
    nearest_key = _airnow_group_key(nearest)
    group_rows = [row for row in rows if _airnow_group_key(row) == nearest_key]
    primary_rows = [row for row in group_rows if row.get("primary")]
    selected = max(primary_rows or group_rows, key=lambda row: _coerce_float(row.get("aqi")) or -1.0)

    pollutants = []
    for row in sorted(group_rows, key=lambda item: (not bool(item.get("primary")), -(int(item.get("aqi") or 0)))):
        pollutants.append(
            {
                "name": row.get("pollutant"),
                "aqi": row.get("aqi"),
                "category": row.get("category"),
                "primary": bool(row.get("primary")),
            }
        )

    us_aqi = selected.get("aqi")
    result = {
        "aqi": us_aqi,
        "us_aqi": us_aqi,
        "category": selected.get("category") or _air_quality_category_us_aqi(us_aqi),
        "primary_pollutant": selected.get("pollutant"),
        "pollutants": pollutants,
        "reporting_area": selected.get("reporting_area"),
        "state_code": selected.get("state_code"),
        "valid_date": selected.get("valid_date"),
        "valid_time": selected.get("valid_time"),
        "time_zone": selected.get("time_zone"),
        "source": selected.get("source"),
        "distance_miles": round(distance, 1),
        "provider": "EPA AirNow",
        "attribution": "EPA AirNow",
        "preliminary": True,
    }
    return {key: value for key, value in result.items() if value not in (None, "", [])}


async def _fetch_airnow_air_quality(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch U.S. AQI from AirNow's public reporting-area file.

    This downloads a public nationwide file and resolves the nearest reporting
    area locally, so Viola does not send per-user coordinates to AirNow.
    """
    if not getattr(settings, "weather_air_quality_airnow_enabled", True):
        return None
    if not _is_us_coordinates(lat, lon):
        return None

    try:
        cache = get_api_cache()
        rows = cache.get_public(_AIRNOW_REPORTING_AREA_CACHE_KEY)
        if rows is None:
            url = str(
                getattr(settings, "weather_air_quality_airnow_reporting_area_url", "") or _AIRNOW_REPORTING_AREA_URL
            )
            async with httpx.AsyncClient(timeout=_AIRNOW_REPORTING_AREA_TIMEOUT) as client:
                response = await client.get(url)
            if response.status_code != 200:
                log.debug("AirNow reporting-area file returned status %s", response.status_code)
                return None
            rows = _parse_airnow_reporting_area(response.text)
            if not rows:
                return None
            ttl_seconds = int(
                _coerce_float(getattr(settings, "weather_air_quality_airnow_cache_ttl_seconds", 1800)) or 1800
            )
            cache.set_public(_AIRNOW_REPORTING_AREA_CACHE_KEY, rows, ttl_seconds=ttl_seconds)
        if not isinstance(rows, list):
            return None
        return _select_airnow_observation(rows, lat, lon)
    except Exception as exc:
        log.debug("AirNow air quality fetch failed: %s", exc)
        return None


def _attach_air_quality(weather: dict[str, Any], air_quality: dict[str, Any] | None) -> dict[str, Any]:
    if not air_quality:
        return weather

    enriched = dict(weather)
    enriched["air_quality"] = air_quality
    enriched["air_quality_provider"] = air_quality.get("provider")
    if air_quality.get("us_aqi") is not None:
        enriched["us_aqi"] = air_quality["us_aqi"]
        enriched["aqi"] = air_quality["us_aqi"]

    current = enriched.get("current")
    current_payload = dict(current) if isinstance(current, Mapping) else {}
    current_payload["air_quality"] = air_quality
    if air_quality.get("us_aqi") is not None:
        current_payload["us_aqi"] = air_quality["us_aqi"]
        current_payload["aqi"] = air_quality["us_aqi"]
    enriched["current"] = current_payload
    return enriched


async def _enrich_with_air_quality(weather: dict[str, Any] | None, lat: float, lon: float) -> dict[str, Any] | None:
    if weather is None:
        return None
    return _attach_air_quality(weather, await _fetch_airnow_air_quality(lat, lon))


def _fahrenheit_to_celsius(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return round((number - 32.0) * 5.0 / 9.0)


def _celsius_to_fahrenheit(value: Any) -> int | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return round(number * 9.0 / 5.0 + 32.0)


def _meters_to_miles(value: Any) -> float | None:
    number = _coerce_float(value)
    if number is None:
        return None
    return round(number / 1609.344, 1)


def _wind_direction_cardinal(degrees: Any) -> str | None:
    value = _coerce_float(degrees)
    if value is None:
        return None
    directions = (
        "N",
        "NNE",
        "NE",
        "ENE",
        "E",
        "ESE",
        "SE",
        "SSE",
        "S",
        "SSW",
        "SW",
        "WSW",
        "W",
        "WNW",
        "NW",
        "NNW",
    )
    index = int((value % 360) / 22.5 + 0.5) % 16
    return directions[index]


def _series_value(series: Mapping[str, list[Any]], key: str, index: int) -> Any | None:
    values = series.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


# ---------------------------------------------------------------------------
# Geocoding (Nominatim / OpenStreetMap â€” free, no API key, handles city names)
# ---------------------------------------------------------------------------


async def _geocode_us_city(city: str) -> tuple[float, float] | None:
    """Geocode a US city string to (lat, lon) via Nominatim.

    Returns ``None`` on any failure â€” caller falls through to wttr.in.
    """
    return await _geocode_city(city, country_code="us")


def _geocode_cache_key(city: str, country_code: str | None) -> str:
    return public_cache_key(
        "weather:geocode:v1",
        {
            "city": city.strip().casefold(),
            "country_code": (country_code or "").strip().casefold(),
        },
    )


def _read_cached_geocode(cache_key: str) -> tuple[float, float] | object | None:
    cached = get_api_cache().get_public(cache_key)
    if not isinstance(cached, Mapping):
        return None
    if cached.get("found") is False:
        return _GEOCODE_NEGATIVE_HIT
    lat = _coerce_float(cached.get("lat"))
    lon = _coerce_float(cached.get("lon"))
    if lat is None or lon is None:
        return None
    return (lat, lon)


def _write_cached_geocode(cache_key: str, coords: tuple[float, float] | None) -> None:
    if coords is None:
        get_api_cache().set_public(
            cache_key,
            {"found": False},
            ttl_seconds=_GEOCODE_NEGATIVE_CACHE_TTL_SECONDS,
        )
        return
    get_api_cache().set_public(
        cache_key,
        {
            "found": True,
            "lat": coords[0],
            "lon": coords[1],
        },
        ttl_seconds=_GEOCODE_CACHE_TTL_SECONDS,
    )


def _get_nominatim_throttle_lock() -> asyncio.Lock:
    global _NOMINATIM_THROTTLE_LOCK
    if _NOMINATIM_THROTTLE_LOCK is None:
        _NOMINATIM_THROTTLE_LOCK = asyncio.Lock()
    return _NOMINATIM_THROTTLE_LOCK


async def _nominatim_redis_limit_allows_request() -> bool:
    try:
        from services.cache.rate_limit import check_redis_sliding_window, redis_rate_limit_enabled
        from services.cache.redis_backend import get_redis

        if not redis_rate_limit_enabled():
            return True

        decision = await check_redis_sliding_window(
            await get_redis(),
            scope=_NOMINATIM_APP_LIMIT_SCOPE,
            identifier=_NOMINATIM_APP_LIMIT_IDENTIFIER,
            limit=1,
            window_seconds=1,
        )
        if not decision.allowed:
            log.debug("Nominatim geocoding app limit blocked request; retry_after=%s", decision.retry_after)
        return decision.allowed
    except (AttributeError, ImportError, RuntimeError, TypeError) as exc:
        log.debug("Nominatim app limiter check failed: %s", exc)
        return True


async def _reserve_nominatim_request_slot() -> bool:
    """Enforce Nominatim's app-level public-service request ceiling."""
    global _NOMINATIM_LAST_REQUEST_AT

    if not await _nominatim_redis_limit_allows_request():
        return False

    async with _get_nominatim_throttle_lock():
        now = time.monotonic()
        wait_seconds = _NOMINATIM_MIN_INTERVAL_SECONDS - (now - _NOMINATIM_LAST_REQUEST_AT)
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        _NOMINATIM_LAST_REQUEST_AT = time.monotonic()
        return True


async def _geocode_city(city: str, *, country_code: str | None = None) -> tuple[float, float] | None:
    """Geocode a city name to (lat, lon) via Nominatim.

    If *country_code* is set (e.g. ``"us"``), restrict results to that country.
    Returns ``None`` on any failure.
    """
    cache_key = _geocode_cache_key(city, country_code)
    cached_coords = _read_cached_geocode(cache_key)
    if cached_coords is _GEOCODE_NEGATIVE_HIT:
        return None
    if isinstance(cached_coords, tuple):
        return cached_coords

    try:
        if not await _reserve_nominatim_request_slot():
            return None

        url = "https://nominatim.openstreetmap.org/search"
        params: dict[str, str] = {
            "q": city,
            "format": "json",
            "limit": "1",
        }
        if country_code:
            params["countrycodes"] = country_code
        headers = {"User-Agent": _NWS_USER_AGENT}
        async with httpx.AsyncClient(timeout=_GEOCODE_TIMEOUT, headers=headers) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                log.debug("Nominatim returned %s for %r", resp.status_code, city)
                _write_cached_geocode(cache_key, None)
                return None

            results = resp.json()

        if not results:
            log.debug("Nominatim: no results for %r", city)
            _write_cached_geocode(cache_key, None)
            return None

        lat = float(results[0].get("lat", 0))
        lon = float(results[0].get("lon", 0))
        if lat == 0.0 and lon == 0.0:
            _write_cached_geocode(cache_key, None)
            return None

        coords = (lat, lon)
        _write_cached_geocode(cache_key, coords)
        return coords
    except Exception as exc:
        log.debug("Nominatim geocoding failed for %r: %s", city, exc)
        return None


# ---------------------------------------------------------------------------
# NWS provider (api.weather.gov â€” free, no API key, US-only)
# ---------------------------------------------------------------------------


async def _fetch_self_gfs(
    lat: float,
    lon: float,
    *,
    display_location: str | None = None,
) -> dict[str, Any] | None:
    """Fetch forecast data from the internal self-hosted NOAA GFS service."""
    base_url = str(getattr(settings, "weather_gfs_url", "") or "http://weather-gfs:8080").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_SELF_GFS_TIMEOUT) as client:
            resp = await client.get(
                "%s/forecast" % base_url,
                params={"lat": "%.4f" % lat, "lon": "%.4f" % lon},
            )
        if resp.status_code != 200:
            log.debug("weather-gfs returned %s for (%.4f, %.4f)", resp.status_code, lat, lon)
            return None
        data = resp.json()
    except Exception as exc:
        log.debug("weather-gfs fetch failed for (%.4f, %.4f): %s", lat, lon, exc)
        return None

    if not isinstance(data, Mapping):
        return None
    payload = dict(data)
    if payload.get("stale") is True:
        log.debug("weather-gfs returned stale payload for (%.4f, %.4f)", lat, lon)
        return None
    hourly_rows = payload.get("hourly") if isinstance(payload.get("hourly"), list) else payload.get("hourly_forecast")
    if isinstance(hourly_rows, list) and not _gfs_has_current_or_future_hours(
        hourly_rows,
        datetime.datetime.now(datetime.UTC),
    ):
        log.debug("weather-gfs returned only stale hourly rows for (%.4f, %.4f)", lat, lon)
        return None
    if display_location:
        payload["location"] = display_location
    normalized = normalize_weather_data(
        {key: value for key, value in payload.items() if value is not None},
        source="noaa-gfs",
        cached=False,
        stale=False,
        provider=str(payload.get("provider") or "NOAA GFS"),
    )
    return dict(normalized)


def _parse_nws_time(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _gfs_has_current_or_future_hours(rows: list[Any], now_utc: datetime.datetime) -> bool:
    if not rows:
        return False
    saw_parseable_time = False
    threshold = now_utc - datetime.timedelta(minutes=30)
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        parsed = _parse_nws_time(item.get("time"))
        if parsed is None:
            continue
        saw_parseable_time = True
        if parsed.astimezone(datetime.UTC) >= threshold:
            return True
    return not saw_parseable_time


def _parse_nws_wind_speed(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        return None
    return _round_value(match.group(0), 1)


def _nws_probability(period: Mapping[str, Any]) -> int | None:
    probability = period.get("probabilityOfPrecipitation")
    if isinstance(probability, Mapping):
        return _round_value(probability.get("value"))
    return None


def _build_nws_hourly(periods: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    hourly: list[dict[str, Any]] = []
    for period in periods[:_NWS_HOURLY_LIMIT]:
        start = _parse_nws_time(period.get("startTime"))
        if start is None:
            continue
        # A period with no shortForecast tells us nothing about the sky. Emit no
        # condition at all (the entry filter below drops the None) and let the
        # "unknown" code carry that fact, instead of shipping a placeholder
        # string a renderer would happily pick an icon for.
        condition = normalize_condition_text(period.get("shortForecast"))
        classification = classify_condition(condition or "", start)
        wind_speed = _parse_nws_wind_speed(period.get("windSpeed"))
        precip = _nws_probability(period)
        temp = _round_value(period.get("temperature"))
        entry = {
            "time": start.isoformat(),
            "date": start.date().isoformat(),
            "hour": start.strftime("%H:%M"),
            "temperature": temp,
            "temperature_f": temp,
            "temperature_c": _fahrenheit_to_celsius(temp),
            "condition": condition,
            "condition_code": classification["condition_code"],
            "is_daytime": (
                period.get("isDaytime") if period.get("isDaytime") is not None else classification["is_daytime"]
            ),
            "precipitation_probability": precip,
            "precip_chance": precip,
            "wind_speed": wind_speed,
            "wind_speed_mph": wind_speed,
            "wind_direction_cardinal": period.get("windDirection"),
        }
        hourly.append({key: value for key, value in entry.items() if value is not None})
    return hourly


def _condition_from_hourly_rows(
    rows: list[Mapping[str, Any]],
    now_utc: datetime.datetime,
) -> str | None:
    """Return the forecast condition for the hour that contains ``now_utc``.

    NWS station observations frequently omit ``textDescription`` (plenty of
    automated stations report temperature and wind but no present-weather
    text), while the SAME NWS point still publishes an hourly forecast with a
    ``shortForecast`` for that hour. Reaching for it keeps the current
    condition on NWS data instead of dropping straight to unknown.
    """
    best: tuple[int, float, str] | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        condition = normalize_condition_text(row.get("condition"))
        if condition is None:
            continue
        parsed = _parse_nws_time(row.get("time"))
        if parsed is None:
            continue
        # NWS hourly periods start on the hour and cover the hour that follows,
        # so the period CONTAINING now wins outright; the hour about to start is
        # only a fallback for the last half hour before it.
        delta_hours = (now_utc - parsed.astimezone(datetime.UTC)).total_seconds() / 3600.0
        if 0.0 <= delta_hours < 1.0:
            rank = 0
        elif -0.5 <= delta_hours < 0.0:
            rank = 1
        else:
            continue
        candidate = (rank, abs(delta_hours), condition)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best else None


def _build_nws_daily(periods: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for period in periods:
        start = _parse_nws_time(period.get("startTime"))
        if start is None:
            continue
        grouped.setdefault(start.date().isoformat(), []).append(period)

    daily: list[dict[str, Any]] = []
    for date_text, day_periods in list(grouped.items())[:_NWS_DAILY_LIMIT]:
        temps = [_round_value(period.get("temperature")) for period in day_periods]
        valid_temps = [temp for temp in temps if temp is not None]
        if not valid_temps:
            continue
        daytime_period = next((period for period in day_periods if period.get("isDaytime")), day_periods[0])
        condition = normalize_condition_text(daytime_period.get("shortForecast"))
        if condition is None:
            condition = next(
                (
                    text
                    for text in (normalize_condition_text(period.get("shortForecast")) for period in day_periods)
                    if text is not None
                ),
                None,
            )
        parsed_date = datetime.date.fromisoformat(date_text)
        classification = classify_condition(
            condition or "", datetime.datetime.combine(parsed_date, datetime.time(12), tzinfo=datetime.UTC)
        )
        precip_values = [_nws_probability(period) for period in day_periods]
        wind_values = [_parse_nws_wind_speed(period.get("windSpeed")) for period in day_periods]
        entry = {
            "date": date_text,
            "day": parsed_date.strftime("%A"),
            "temperature": round(sum(valid_temps) / len(valid_temps)),
            "high": max(valid_temps),
            "low": min(valid_temps),
            "high_f": max(valid_temps),
            "low_f": min(valid_temps),
            "condition": condition,
            "condition_code": classification["condition_code"],
            "precipitation_probability": max((value for value in precip_values if value is not None), default=None),
            "precip_chance": max((value for value in precip_values if value is not None), default=None),
            "wind_speed": max((value for value in wind_values if value is not None), default=None),
            "wind_speed_mph": max((value for value in wind_values if value is not None), default=None),
        }
        daily.append({key: value for key, value in entry.items() if value is not None})
    return daily


def _overlay_defined(target: dict[str, Any], source: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        value = source.get(key)
        if value is not None:
            target[key] = value


def _is_unknown_condition(value: Any) -> bool:
    """True when *value* is NWS's "no data" sentinel, not a real condition.

    ``_build_nws_hourly``/``_build_nws_daily``/the current-observation fetch all
    default to the literal string ``"Unknown"`` when NWS's own
    ``shortForecast``/``textDescription`` field is blank — a known gap in NWS's
    API for some automated stations and periods (C-110), not a signal that no
    condition exists at all. Treating that sentinel as a real, defined value
    during merge overlays it onto (and hides) a genuine GFS-derived condition
    for the same hour/day.
    """
    return value is None or str(value).strip().lower() in ("", "unknown")


def _overlay_condition_aware(target: dict[str, Any], source: Mapping[str, Any], keys: tuple[str, ...]) -> None:
    """Like ``_overlay_defined``, but never lets an "Unknown" NWS condition win.

    *keys* is expected to include ``condition``/``condition_code`` among other
    fields to overlay; if *source*'s condition is the "Unknown" sentinel, those
    two keys are dropped from the overlay so *target*'s existing (GFS-derived)
    condition survives instead of being overwritten by "no data".
    """
    if _is_unknown_condition(source.get("condition")):
        source = {key: value for key, value in source.items() if key not in ("condition", "condition_code")}
    _overlay_defined(target, source, keys)


def _filter_future_hourly_rows(
    rows: list[Any],
    now_utc: datetime.datetime,
) -> list[Any]:
    future_rows = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        parsed = _parse_nws_time(item.get("time"))
        if parsed is not None and parsed.astimezone(datetime.UTC) >= now_utc:
            future_rows.append(item)
    return future_rows


def _filter_future_daily_rows(rows: list[Any], today_local: datetime.date) -> list[Any]:
    """Drop daily rows that already ended, judged on the forecast location's calendar.

    ``today_local`` must be today *where the forecast is for*, not today in UTC
    (#4216). Both providers date their daily rows on the location's own
    calendar, so comparing them against the UTC date discards the user's
    current day for every location east of Greenwich after local midnight and
    for the Americas after local evening -- 18:00 PDT is already tomorrow in
    UTC, and the row for that user's actual today would be dropped before the
    NWS overlay ever ran.
    """
    future_rows = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        date_text = str(item.get("date", ""))[:10]
        try:
            row_date = datetime.date.fromisoformat(date_text)
        except ValueError:
            continue
        if row_date >= today_local:
            future_rows.append(item)
    return future_rows


def _hour_bucket_key(value: Any) -> str:
    """Return a UTC-normalized hour-bucket key for an hourly row's ``time`` field.

    NWS and the self-hosted GFS service format ``time`` with different UTC
    offsets for the same real instant (NWS periods carry the station's local
    offset, e.g. ``-04:00``; GFS rows are ``+00:00``). Keying the merge by a
    naive string prefix of the raw value treats those as two different hours,
    so the same instant survives into the client payload twice under two
    offsets (C-111). Parsing with ``_parse_nws_time`` and converting to UTC
    before truncating to the hour makes the key offset-independent.
    """
    parsed = _parse_nws_time(value)
    if parsed is None:
        return str(value or "")[:13]
    return parsed.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H")


def _normalize_row_time(row: dict[str, Any], tz: datetime.tzinfo) -> None:
    """Rewrite an hourly row's ``time`` (and ``date``/``hour`` if present) to one offset, in place.

    Deduping by UTC hour (``_hour_bucket_key``) stops the same instant from
    appearing twice, but a surviving row can still carry its source's raw
    offset. Without this, the array a client renders can still mix
    ``+00:00``/``-04:00`` formatting across rows (the visible half of C-111)
    even once duplicates are gone. A row that also carries separate ``date``/
    ``hour`` fields (``_build_nws_hourly`` always sets them) gets those
    restated too, so they cannot disagree with the now-corrected ``time`` --
    e.g. a row shifted across a day boundary keeping its pre-shift date.

    The single offset the rows are restated on is the *forecast location's*
    (#4216). C-111 needed one offset, not specifically UTC, and UTC was the
    wrong one to pick: the hourly rows are grouped against the daily rows by
    their ``date`` string (the per-day expand in ``WeatherForecast.jsx`` keys
    off exactly that), and the daily rows are the location's calendar days.
    Restating hours on the UTC calendar left a Tokyo evening filed under
    tomorrow and an American evening filed under a day the daily list did not
    contain at all.
    """
    parsed = _parse_nws_time(row.get("time"))
    if parsed is None:
        return
    localized = parsed.astimezone(tz)
    row["time"] = localized.isoformat()
    if "date" in row:
        row["date"] = localized.date().isoformat()
    if "hour" in row:
        row["hour"] = localized.strftime("%H:%M")


def _hourly_sort_key(item: Mapping[str, Any]) -> float:
    parsed = _parse_nws_time(item.get("time"))
    if parsed is None:
        return float("inf")
    return parsed.astimezone(datetime.UTC).timestamp()


def _daily_sort_key(item: Mapping[str, Any]) -> int:
    date_text = str(item.get("date", ""))[:10]
    try:
        return datetime.date.fromisoformat(date_text).toordinal()
    except ValueError:
        return 99999999


def _merge_us_weather(
    nws: dict[str, Any] | None,
    gfs: dict[str, Any] | None,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any] | None:
    """Overlay the NWS station reading onto the GFS model payload.

    ``now`` names the instant "which day is it" is answered at; it defaults to
    the real clock and exists so a caller (a test) can stand the merge at a
    chosen day boundary, the same way ``build_forecast_payload`` takes one.
    """
    if gfs is None:
        return nws
    if nws is None:
        return gfs

    now_utc = now.astimezone(datetime.UTC) if now is not None else datetime.datetime.now(datetime.UTC)
    # Both providers date their rows on the forecast location's calendar, so
    # every day-level comparison below is made on that same clock (#4216). The
    # GFS payload names the zone; a payload that does not carries the offset on
    # its own timestamps, which ``payload_timezone`` reads back.
    local_zone = payload_timezone(gfs) if gfs.get("timezone") else payload_timezone(nws)
    today_local = now_utc.astimezone(local_zone).date()
    merged = dict(gfs)
    gfs_current = gfs.get("current") if isinstance(gfs.get("current"), Mapping) else {}
    nws_current = nws.get("current") if isinstance(nws.get("current"), Mapping) else {}
    # NWS observations own the current block: mixing a modelled GFS value into
    # an observed reading produces incoherent pairs (an observed 43F next to a
    # modelled 62F feels-like). Only fields GFS alone provides are added.
    current = dict(nws_current)
    _overlay_defined(
        current,
        nws_current,
        (
            "temperature",
            "temperature_f",
            "temperature_c",
            "is_daytime",
            "humidity",
            "dew_point",
            "dew_point_f",
            "wind_speed",
            "wind_speed_mph",
            "wind_direction",
            "wind_direction_cardinal",
            "pressure",
            "pressure_hpa",
            "visibility",
            "visibility_miles",
            "updated",
            "feels_like",
            "feels_like_f",
        ),
    )
    current.setdefault("uv_index", gfs_current.get("uv_index"))
    current.setdefault("sunrise", gfs_current.get("sunrise"))
    current.setdefault("sunset", gfs_current.get("sunset"))
    # C-110: NWS's own API leaves textDescription/shortForecast blank for some
    # automated stations/periods, and the fetch code defaults that to the
    # literal string "Unknown" (a "no data" sentinel, not a real condition).
    # `current` already carries NWS's condition as-is from the `dict(nws_current)`
    # copy above; only override it when NWS's value is that sentinel AND GFS
    # actually has a real classification to offer. If GFS has nothing better
    # either, "Unknown" is the honest answer for THIS function -- the trailing
    # normalize_weather_data() call at the end of this function still turns it
    # into an absent `condition` + explicit `condition_code: "unknown"` before
    # returning, same as every other read path (live fetch, cache reads).
    if _is_unknown_condition(current.get("condition")) and not _is_unknown_condition(gfs_current.get("condition")):
        current["condition"] = gfs_current.get("condition")
        current["condition_code"] = gfs_current.get("condition_code", current.get("condition_code"))

    raw_nws_hourly = nws.get("hourly_forecast") if isinstance(nws.get("hourly_forecast"), list) else []
    raw_gfs_hourly = gfs.get("hourly") if isinstance(gfs.get("hourly"), list) else gfs.get("hourly_forecast")
    raw_gfs_hourly = raw_gfs_hourly if isinstance(raw_gfs_hourly, list) else []
    gfs_future_hourly = _filter_future_hourly_rows(raw_gfs_hourly, now_utc)
    nws_future_hourly = _filter_future_hourly_rows(raw_nws_hourly, now_utc)
    if gfs_future_hourly or nws_future_hourly:
        gfs_hourly = gfs_future_hourly
        nws_hourly = nws_future_hourly
    else:
        gfs_hourly = raw_gfs_hourly
        nws_hourly = raw_nws_hourly
    # Keyed by UTC hour, not a raw string prefix (C-111): NWS periods and GFS
    # rows can format the same instant under different UTC offsets, and a
    # naive prefix key would treat those as two separate hours instead of one.
    nws_by_hour = {_hour_bucket_key(item.get("time")): item for item in nws_hourly if isinstance(item, Mapping)}
    seen_hours: set[str] = set()
    hourly: list[dict[str, Any]] = []
    for item in gfs_hourly:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        hour_key = _hour_bucket_key(row.get("time"))
        seen_hours.add(hour_key)
        nws_row = nws_by_hour.get(hour_key)
        if nws_row:
            _overlay_condition_aware(
                row,
                nws_row,
                (
                    "temperature",
                    "temperature_f",
                    "temperature_c",
                    "condition",
                    "condition_code",
                    "is_daytime",
                    "precipitation_probability",
                    "precip_chance",
                    "wind_speed",
                    "wind_speed_mph",
                    "wind_direction_cardinal",
                ),
            )
        _normalize_row_time(row, local_zone)
        hourly.append(row)
    for item in nws_hourly:
        if not isinstance(item, Mapping):
            continue
        hour_key = _hour_bucket_key(item.get("time"))
        if hour_key not in seen_hours:
            row = dict(item)
            _normalize_row_time(row, local_zone)
            hourly.append(row)
    hourly.sort(key=_hourly_sort_key)

    raw_nws_daily = nws.get("daily_forecast") if isinstance(nws.get("daily_forecast"), list) else []
    raw_gfs_daily = gfs.get("daily_forecast") if isinstance(gfs.get("daily_forecast"), list) else []
    gfs_future_daily = _filter_future_daily_rows(raw_gfs_daily, today_local)
    nws_future_daily = _filter_future_daily_rows(raw_nws_daily, today_local)
    if gfs_future_daily or nws_future_daily:
        gfs_daily = gfs_future_daily
        nws_daily = nws_future_daily
    else:
        gfs_daily = raw_gfs_daily
        nws_daily = raw_nws_daily
    nws_by_date = {str(item.get("date", ""))[:10]: item for item in nws_daily if isinstance(item, Mapping)}
    seen_dates: set[str] = set()
    daily: list[dict[str, Any]] = []
    for item in gfs_daily:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        date_key = str(row.get("date", ""))[:10]
        seen_dates.add(date_key)
        nws_row = nws_by_date.get(date_key)
        if nws_row:
            _overlay_condition_aware(
                row,
                nws_row,
                (
                    "temperature",
                    "high",
                    "low",
                    "high_f",
                    "low_f",
                    "condition",
                    "condition_code",
                    "precipitation_probability",
                    "precip_chance",
                    "wind_speed",
                    "wind_speed_mph",
                ),
            )
        daily.append(row)
    for item in nws_daily:
        if not isinstance(item, Mapping):
            continue
        date_key = str(item.get("date", ""))[:10]
        if date_key not in seen_dates:
            daily.append(dict(item))
    daily.sort(key=_daily_sort_key)

    first_day = daily[0] if daily else {}
    merged.update(
        {
            "current": {key: value for key, value in current.items() if value is not None},
            "hourly": hourly,
            "hourly_forecast": hourly[:24],
            "daily": daily,
            "daily_forecast": daily,
            "forecast": daily,
            "source": "nws+noaa-gfs",
            "provider": "NWS + NOAA GFS",
            "forecast_provider": "NWS + NOAA GFS",
            # State the clock the merged rows are dated on, so a consumer
            # deciding which row is "today" does not have to re-derive it.
            "timezone_offset_seconds": int(
                (now_utc.astimezone(local_zone).utcoffset() or datetime.timedelta(0)).total_seconds()
            ),
            "location": nws.get("location") or gfs.get("location"),
            "temperature": current.get("temperature_f"),
            "temperature_f": current.get("temperature_f"),
            "temp_f": current.get("temperature_f"),
            "temperature_c": current.get("temperature_c"),
            "condition": current.get("condition"),
            "description": current.get("condition"),
            "condition_code": current.get("condition_code"),
            "is_daytime": current.get("is_daytime"),
            "updated": current.get("updated") or gfs.get("updated"),
            "forecast_high_f": first_day.get("high_f"),
            "forecast_low_f": first_day.get("low_f"),
            "high_f": first_day.get("high_f"),
            "low_f": first_day.get("low_f"),
        }
    )
    for key in (
        "feels_like",
        "feels_like_f",
        "humidity",
        "pressure",
        "pressure_hpa",
        "dew_point",
        "dew_point_f",
        "visibility",
        "visibility_miles",
        "wind_speed",
        "wind_speed_mph",
        "wind_direction",
        "wind_direction_cardinal",
        "wind_gust",
        "wind_gust_mph",
        "precipitation",
        "precipitation_in",
        "uv_index",
        "cloud_cover",
        "sunrise",
        "sunset",
    ):
        if current.get(key) is not None:
            merged[key] = current[key]
    normalized = normalize_weather_data(
        {key: value for key, value in merged.items() if value is not None},
        source="nws+noaa-gfs",
        cached=False,
        stale=False,
        provider="NWS + NOAA GFS",
    )
    return dict(normalized)


async def _fetch_nws(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch current weather + forecast from NWS for US coordinates.

    Returns a normalized dict matching the same schema as ``_fetch_wttr``,
    or ``None`` on any failure (non-US, network error, parse error, etc.).
    """
    try:
        headers = {
            "User-Agent": _NWS_USER_AGENT,
            "Accept": "application/geo+json",
        }
        async with httpx.AsyncClient(timeout=_NWS_TIMEOUT, headers=headers) as client:
            # Step 1: Point metadata â€” gives us station + forecast URLs
            points_url = "https://api.weather.gov/points/%.4f,%.4f" % (lat, lon)
            resp = await client.get(points_url)
            if resp.status_code != 200:
                log.debug(
                    "NWS /points returned %s for (%.4f, %.4f)",
                    resp.status_code,
                    lat,
                    lon,
                )
                return None

            props = resp.json().get("properties", {})

            stations_url = props.get("observationStations")
            forecast_url = props.get("forecast")
            forecast_hourly_url = props.get("forecastHourly")
            rel_loc = props.get("relativeLocation", {}).get("properties", {})
            city_name = rel_loc.get("city", "")
            state_name = rel_loc.get("state", "")
            display_location = "%s, %s" % (city_name, state_name) if city_name else "Local"

            if not stations_url:
                log.debug("NWS: no observationStations URL for (%.4f, %.4f)", lat, lon)
                return None

            # Step 2: Nearest observation station
            resp = await client.get(stations_url)
            if resp.status_code != 200:
                return None

            features = resp.json().get("features", [])
            if not features:
                return None

            station_id = features[0].get("properties", {}).get("stationIdentifier")
            if not station_id:
                return None

            # Step 3: Latest observation
            obs_url = "https://api.weather.gov/stations/%s/observations/latest" % station_id
            resp = await client.get(obs_url)
            if resp.status_code != 200:
                return None

            obs = resp.json().get("properties", {})

            # Temperature (NWS gives Celsius)
            temp_c_raw = obs.get("temperature", {}).get("value")
            if temp_c_raw is None:
                log.debug("NWS: null temperature from station %s", station_id)
                return None

            temp_c = round(temp_c_raw)
            temp_f = round(temp_c_raw * 9.0 / 5.0 + 32.0)

            observed_condition = normalize_condition_text(obs.get("textDescription"))

            observation_timestamp = datetime.datetime.now(datetime.UTC)
            humidity = _round_value(obs.get("relativeHumidity", {}).get("value"))
            dew_point_f = _celsius_to_fahrenheit(obs.get("dewpoint", {}).get("value"))
            heat_index_f = _celsius_to_fahrenheit(obs.get("heatIndex", {}).get("value"))
            wind_chill_f = _celsius_to_fahrenheit(obs.get("windChill", {}).get("value"))
            feels_like_f = heat_index_f if heat_index_f is not None else wind_chill_f
            wind_kmh = _coerce_float(obs.get("windSpeed", {}).get("value"))
            wind_mph = round(wind_kmh * 0.621371, 1) if wind_kmh is not None else None
            wind_direction = _round_value(obs.get("windDirection", {}).get("value"))
            pressure_pa = _coerce_float(obs.get("barometricPressure", {}).get("value"))
            pressure_hpa = round(pressure_pa / 100.0, 1) if pressure_pa is not None else None
            visibility_miles = _meters_to_miles(obs.get("visibility", {}).get("value"))

            # Step 4: Forecast overlays (nice-to-have; don't fail current conditions)
            hourly_forecast: list[dict[str, Any]] = []
            daily_forecast: list[dict[str, Any]] = []
            if forecast_hourly_url:
                try:
                    resp = await client.get(forecast_hourly_url)
                    if resp.status_code == 200:
                        periods = resp.json().get("properties", {}).get("periods", [])
                        if isinstance(periods, list):
                            hourly_forecast = _build_nws_hourly(periods)
                except Exception as exc:
                    log.debug("NWS hourly forecast fetch failed: %s", exc)
            if forecast_url:
                try:
                    resp = await client.get(forecast_url)
                    if resp.status_code == 200:
                        periods = resp.json().get("properties", {}).get("periods", [])
                        if isinstance(periods, list):
                            daily_forecast = _build_nws_daily(periods)
                except Exception as exc:
                    log.debug("NWS daily forecast fetch failed: %s", exc)

        # Observation text first (that is what a person outside would see), then
        # this point's own hourly forecast for the current hour. If neither has
        # it we say so: no condition field, condition_code "unknown".
        condition = observed_condition or _condition_from_hourly_rows(hourly_forecast, observation_timestamp)
        classification = classify_condition(condition or "", observation_timestamp)
        if condition is None:
            log.info(
                "NWS observation for %s carries no condition text and no current-hour forecast; reporting unknown",
                station_id,
            )

        weather_data: dict[str, Any] = {
            "temperature": temp_f,
            "temperature_c": temp_c,
            "unit": "F",
            "measurement_system": "imperial",
            "condition": condition,
            "condition_code": classification["condition_code"],
            "is_daytime": classification["is_daytime"],
            "location": display_location,
            "updated": observation_timestamp.isoformat(),
            "source": "nws",
            "provider": "NWS",
            "cached": False,
            "stale": False,
            "description": condition,
            "temperature_f": temp_f,
            "temp_f": temp_f,
            "feels_like": feels_like_f,
            "feels_like_f": feels_like_f,
            "humidity": humidity,
            "dew_point": dew_point_f,
            "dew_point_f": dew_point_f,
            "wind_speed": wind_mph,
            "wind_speed_mph": wind_mph,
            "wind_direction": wind_direction,
            "wind_direction_cardinal": _wind_direction_cardinal(wind_direction),
            "pressure": pressure_hpa,
            "pressure_hpa": pressure_hpa,
            "visibility": visibility_miles,
            "visibility_miles": visibility_miles,
            "current": {
                key: value
                for key, value in {
                    "temperature": temp_f,
                    "temperature_f": temp_f,
                    "temperature_c": temp_c,
                    "feels_like": feels_like_f,
                    "feels_like_f": feels_like_f,
                    "condition": condition,
                    "condition_code": classification["condition_code"],
                    "is_daytime": classification["is_daytime"],
                    "humidity": humidity,
                    "dew_point": dew_point_f,
                    "dew_point_f": dew_point_f,
                    "wind_speed": wind_mph,
                    "wind_speed_mph": wind_mph,
                    "wind_direction": wind_direction,
                    "wind_direction_cardinal": _wind_direction_cardinal(wind_direction),
                    "pressure": pressure_hpa,
                    "pressure_hpa": pressure_hpa,
                    "visibility": visibility_miles,
                    "visibility_miles": visibility_miles,
                    "updated": observation_timestamp.isoformat(),
                }.items()
                if value is not None
            },
            "hourly": hourly_forecast,
            "hourly_forecast": hourly_forecast[:24],
            "daily": daily_forecast,
            "daily_forecast": daily_forecast,
            "forecast": daily_forecast,
            "forecast_high_f": daily_forecast[0].get("high_f") if daily_forecast else None,
            "forecast_low_f": daily_forecast[0].get("low_f") if daily_forecast else None,
            "high_f": daily_forecast[0].get("high_f") if daily_forecast else None,
            "low_f": daily_forecast[0].get("low_f") if daily_forecast else None,
        }

        normalized = normalize_weather_data(
            {key: value for key, value in weather_data.items() if value is not None},
            source="nws",
            cached=False,
            stale=False,
            provider=weather_data.get("provider", "NWS"),
        )
        log.info(
            "NWS weather for %s: %sÂ°F, %s",
            display_location,
            temp_f,
            condition or UNKNOWN_CONDITION_CODE,
        )
        return dict(normalized)

    except Exception as exc:
        log.debug("NWS fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# wttr.in provider (global fallback)
# ---------------------------------------------------------------------------


async def _fetch_wttr(
    *,
    lat: float | None = None,
    lon: float | None = None,
    city: str | None = None,
    location_url_encoded: str | None = None,
) -> dict[str, Any] | None:
    """Fetch weather from wttr.in and return a normalized dict, or ``None``."""
    location = ""
    if lat is not None and lon is not None:
        location = "%s,%s" % (lat, lon)
    elif location_url_encoded:
        location = location_url_encoded
    elif city:
        location = city

    base_url = settings.weather_api_base or "https://wttr.in"
    url = "%s/%s?format=j1" % (base_url, location) if location else "%s/?format=j1" % base_url

    timeout = httpx.Timeout(TIMEOUT_VERY_LONG, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        if response.status_code != 200:
            log.warning("wttr.in returned non-200 status: %s", response.status_code)
            return None

        data = response.json()

    current = data.get("current_condition", [{}])[0]
    temp_f = int(current.get("temp_F", 72))
    temp_c = int(current.get("temp_C", 22))
    condition = normalize_condition_text(current.get("weatherDesc", [{}])[0].get("value"))

    observation_timestamp = datetime.datetime.now(datetime.UTC)
    classification = classify_condition(condition or "", observation_timestamp)

    # When the caller provided a city name, prefer it over wttr.in's nearest_area
    # which can show micro-locations (e.g., "St James" instead of "London").
    if city:
        display_location = city.strip()
    else:
        display_location = data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", "Local")

    feels_like_f = _round_value(current.get("FeelsLikeF"))
    humidity = _round_value(current.get("humidity"))
    pressure_hpa = _round_value(current.get("pressure"), 1)
    wind_speed_mph = _round_value(current.get("windspeedMiles"), 1)
    wind_direction = _round_value(current.get("winddirDegree"))
    precipitation_in = _round_value(current.get("precipInches"), 2)
    visibility_miles = _round_value(current.get("visibilityMiles") or current.get("visibility"), 1)
    uv_index = _round_value(current.get("uvIndex"), 1)

    # Parse wttr.in's 3-day forecast. It is shorter than NOAA GFS/NWS but
    # already contains rich hourly/current fields, so keep them for fallback.
    forecast_data: list[dict[str, Any]] = []
    hourly_forecast: list[dict[str, Any]] = []
    for day_forecast in data.get("weather", [])[:3]:
        try:
            day = day_forecast.get("date", "")
            hourly = day_forecast.get("hourly", [])
            if not hourly:
                continue

            temps = [int(h.get("tempF", temp_f)) for h in hourly if "tempF" in h]
            avg_temp = sum(temps) // len(temps) if temps else temp_f

            conditions = [
                text
                for text in (
                    normalize_condition_text(h.get("weatherDesc", [{}])[0].get("value"))
                    for h in hourly
                    if "weatherDesc" in h
                )
                if text is not None
            ]
            common_condition = max(set(conditions), key=conditions.count) if conditions else condition
            day_classification = classify_condition(common_condition or "", observation_timestamp)
            high = _round_value(day_forecast.get("maxtempF"))
            low = _round_value(day_forecast.get("mintempF"))
            astronomy = (day_forecast.get("astronomy") or [{}])[0]
            day_precip = _round_value(day_forecast.get("totalSnow_cm"), 2)

            forecast_data.append(
                {
                    "date": day,
                    "temperature": avg_temp,
                    "high": high,
                    "low": low,
                    "high_f": high,
                    "low_f": low,
                    "condition": common_condition,
                    "condition_code": day_classification["condition_code"],
                    "precipitation_sum": day_precip,
                    "snowfall_cm": day_precip,
                    "precipitation_probability": max(
                        (int(h.get("chanceofrain", 0)) for h in hourly if str(h.get("chanceofrain", "")).isdigit()),
                        default=0,
                    ),
                    "precip_chance": max(
                        (int(h.get("chanceofrain", 0)) for h in hourly if str(h.get("chanceofrain", "")).isdigit()),
                        default=0,
                    ),
                    "wind_speed": max(
                        (
                            float(h.get("windspeedMiles", 0))
                            for h in hourly
                            if _coerce_float(h.get("windspeedMiles")) is not None
                        ),
                        default=0,
                    ),
                    "wind_speed_mph": max(
                        (
                            float(h.get("windspeedMiles", 0))
                            for h in hourly
                            if _coerce_float(h.get("windspeedMiles")) is not None
                        ),
                        default=0,
                    ),
                    "uv_index": _round_value(day_forecast.get("uvIndex"), 1),
                    "sunrise": astronomy.get("sunrise"),
                    "sunset": astronomy.get("sunset"),
                }
            )

            for hour in hourly:
                raw_hour = str(hour.get("time", "0")).zfill(4)
                hour_label = "%s:%s" % (raw_hour[:-2].zfill(2), raw_hour[-2:])
                time_value = "%sT%s" % (day, hour_label)
                hour_condition = (
                    normalize_condition_text(hour.get("weatherDesc", [{}])[0].get("value")) or common_condition
                )
                hour_classification = classify_condition(hour_condition or "", observation_timestamp)
                hour_wind_direction = _round_value(hour.get("winddirDegree"))
                hour_precip = _round_value(hour.get("precipInches"), 2)
                hourly_forecast.append(
                    {
                        "time": time_value,
                        "date": day,
                        "hour": hour_label,
                        "temperature": _round_value(hour.get("tempF")),
                        "temperature_f": _round_value(hour.get("tempF")),
                        "feels_like": _round_value(hour.get("FeelsLikeF")),
                        "feels_like_f": _round_value(hour.get("FeelsLikeF")),
                        "condition": hour_condition,
                        "condition_code": hour_classification["condition_code"],
                        "humidity": _round_value(hour.get("humidity")),
                        "precipitation_probability": _round_value(hour.get("chanceofrain")),
                        "precip_chance": _round_value(hour.get("chanceofrain")),
                        "precipitation": hour_precip,
                        "precipitation_in": hour_precip,
                        "wind_speed": _round_value(hour.get("windspeedMiles"), 1),
                        "wind_speed_mph": _round_value(hour.get("windspeedMiles"), 1),
                        "wind_direction": hour_wind_direction,
                        "wind_direction_cardinal": hour.get("winddir16Point")
                        or _wind_direction_cardinal(hour_wind_direction),
                        "pressure": _round_value(hour.get("pressure"), 1),
                        "pressure_hpa": _round_value(hour.get("pressure"), 1),
                        "visibility": _round_value(hour.get("visibilityMiles") or hour.get("visibility"), 1),
                        "visibility_miles": _round_value(hour.get("visibilityMiles") or hour.get("visibility"), 1),
                        "cloud_cover": _round_value(hour.get("cloudcover")),
                        "uv_index": _round_value(hour.get("uvIndex"), 1),
                    }
                )
        except Exception as exc:  # pragma: no cover
            log.debug("Failed to parse forecast day: %s", exc)

    first_day = forecast_data[0] if forecast_data else {}
    current_payload = {
        "temperature": temp_f,
        "temperature_f": temp_f,
        "temperature_c": temp_c,
        "feels_like": feels_like_f,
        "feels_like_f": feels_like_f,
        "condition": condition,
        "condition_code": classification["condition_code"],
        "is_daytime": classification["is_daytime"],
        "humidity": humidity,
        "pressure": pressure_hpa,
        "pressure_hpa": pressure_hpa,
        "wind_speed": wind_speed_mph,
        "wind_speed_mph": wind_speed_mph,
        "wind_direction": wind_direction,
        "wind_direction_cardinal": current.get("winddir16Point") or _wind_direction_cardinal(wind_direction),
        "precipitation": precipitation_in,
        "precipitation_in": precipitation_in,
        "visibility": visibility_miles,
        "visibility_miles": visibility_miles,
        "uv_index": uv_index,
        "updated": observation_timestamp.isoformat(),
        "sunrise": first_day.get("sunrise"),
        "sunset": first_day.get("sunset"),
    }

    weather_data: dict[str, Any] = {
        "temperature": temp_f,
        "temperature_c": temp_c,
        "unit": "F",
        "measurement_system": "imperial",
        "condition": condition,
        "description": condition,
        "condition_code": classification["condition_code"],
        "is_daytime": classification["is_daytime"],
        "location": display_location,
        "updated": observation_timestamp.isoformat(),
        "source": "wttr.in",
        "provider": "wttr.in",
        "cached": False,
        "stale": False,
        "temperature_f": temp_f,
        "temp_f": temp_f,
        "feels_like": feels_like_f,
        "feels_like_f": feels_like_f,
        "humidity": humidity,
        "pressure": pressure_hpa,
        "pressure_hpa": pressure_hpa,
        "wind_speed": wind_speed_mph,
        "wind_speed_mph": wind_speed_mph,
        "wind_direction": wind_direction,
        "wind_direction_cardinal": current.get("winddir16Point") or _wind_direction_cardinal(wind_direction),
        "precipitation": precipitation_in,
        "precipitation_in": precipitation_in,
        "visibility": visibility_miles,
        "visibility_miles": visibility_miles,
        "uv_index": uv_index,
        "current": {key: value for key, value in current_payload.items() if value is not None},
        "hourly": hourly_forecast,
        "hourly_forecast": hourly_forecast[:24],
        "daily": forecast_data,
        "daily_forecast": forecast_data,
        "forecast": forecast_data,
        "forecast_high_f": first_day.get("high_f"),
        "forecast_low_f": first_day.get("low_f"),
        "high_f": first_day.get("high_f"),
        "low_f": first_day.get("low_f"),
    }

    normalized = normalize_weather_data(
        {key: value for key, value in weather_data.items() if value is not None},
        source="wttr.in",
        cached=False,
        stale=False,
        provider="wttr.in",
    )
    return dict(normalized)


# ---------------------------------------------------------------------------
# Public API â€” sole entry point
# ---------------------------------------------------------------------------


def _weather_fetch_cache_key(
    *,
    lat: float | None,
    lon: float | None,
    city: str | None,
    location_url_encoded: str | None,
) -> str:
    return public_cache_key(
        "weather:forecast:v1",
        {
            "lat": round(lat, 4) if lat is not None else None,
            "lon": round(lon, 4) if lon is not None else None,
            "city": city.strip().casefold() if city else None,
            "location_url_encoded": location_url_encoded or None,
        },
    )


def _read_cached_weather(cache_key: str) -> dict[str, Any] | None:
    cached = get_api_cache().get_public(cache_key)
    if not isinstance(cached, Mapping):
        return None
    normalized = normalize_weather_data(
        dict(cached),
        source=str(cached.get("source") or "api_cache"),
        cached=True,
        stale=False,
        provider=str(cached.get("provider") or ""),
    )
    return dict(normalized)


def _cache_weather_result(cache_key: str, weather: dict[str, Any] | None) -> dict[str, Any] | None:
    if weather is None:
        return None
    payload = dict(weather)
    payload.setdefault("cached", False)
    payload.setdefault("stale", False)
    get_api_cache().set_public(
        cache_key,
        payload,
        ttl_seconds=_WEATHER_FETCH_CACHE_TTL_SECONDS,
    )
    return payload


async def fetch_weather(
    *,
    lat: float | None = None,
    lon: float | None = None,
    city: str | None = None,
    location_url_encoded: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Fetch weather data via NOAA GFS, NWS US overlay, and wttr.in fallback.

    Parameters
    ----------
    lat, lon : float | None
        Coordinates.  Both must be provided together.
    city : str | None
        Human-readable city name (not URL-encoded).
    location_url_encoded : str | None
        Pre-validated, URL-encoded location string (from route SSRF validation).
        Takes precedence over *city* when set.
    force_refresh : bool
        Bypass the shared public weather cache for this request.

    Returns
    -------
    dict | None
        Normalized weather data ready for caching/display, or ``None`` on any error.
    """
    try:
        from admin.instrumentation import record_feature_used

        record_feature_used("weather")
    except Exception:
        pass

    cache_key = _weather_fetch_cache_key(
        lat=lat,
        lon=lon,
        city=city,
        location_url_encoded=location_url_encoded,
    )
    if not force_refresh:
        cached_weather = _read_cached_weather(cache_key)
        if cached_weather is not None:
            return cached_weather

    try:
        # --- Coordinate requests can go straight to provider selection. ---
        geocoded_coords: tuple[float, float] | None = None

        if lat is not None and lon is not None:
            if _is_us_coordinates(lat, lon):
                nws_result = await _fetch_nws(lat, lon)
                gfs_result = await _fetch_self_gfs(lat, lon, display_location=city)
                merged = _merge_us_weather(nws_result, gfs_result)
                if merged is not None:
                    return _cache_weather_result(cache_key, await _enrich_with_air_quality(merged, lat, lon))
            else:
                gfs_result = await _fetch_self_gfs(lat, lon, display_location=city)
                if gfs_result is not None:
                    return _cache_weather_result(cache_key, await _enrich_with_air_quality(gfs_result, lat, lon))

        elif city:
            # Geocode city names for accuracy.
            # Strategy: global geocoding first (so "tokyo" â†’ Japan, not Tokyo TX),
            # then check if the result is in the US for NWS routing.
            if _is_likely_us(city):
                # Explicit US format like "Chicago, IL" â€” use US-only geocoding
                geocoded_coords = await _geocode_us_city(city)
            else:
                # Bare city name â€” use global geocoding to avoid false US matches
                # (e.g., "tokyo" â†’ Tokyo, Japan, not some US hamlet named Tokyo)
                geocoded_coords = await _geocode_city(city)

            if geocoded_coords and _is_us_coordinates(geocoded_coords[0], geocoded_coords[1]):
                nws_result = await _fetch_nws(geocoded_coords[0], geocoded_coords[1])
                gfs_result = await _fetch_self_gfs(
                    geocoded_coords[0],
                    geocoded_coords[1],
                    display_location=city.strip(),
                )
                merged = _merge_us_weather(nws_result, gfs_result)
                if merged is not None:
                    return _cache_weather_result(
                        cache_key,
                        await _enrich_with_air_quality(merged, geocoded_coords[0], geocoded_coords[1]),
                    )
            elif geocoded_coords:
                gfs_result = await _fetch_self_gfs(
                    geocoded_coords[0],
                    geocoded_coords[1],
                    display_location=city.strip(),
                )
                if gfs_result is not None:
                    return _cache_weather_result(
                        cache_key,
                        await _enrich_with_air_quality(gfs_result, geocoded_coords[0], geocoded_coords[1]),
                    )
                return _cache_weather_result(
                    cache_key,
                    await _enrich_with_air_quality(
                        await _fetch_wttr(city=city),
                        geocoded_coords[0],
                        geocoded_coords[1],
                    ),
                )

        if geocoded_coords:
            gfs_result = await _fetch_self_gfs(
                geocoded_coords[0],
                geocoded_coords[1],
                display_location=city.strip() if city else None,
            )
            if gfs_result is not None:
                return _cache_weather_result(
                    cache_key,
                    await _enrich_with_air_quality(gfs_result, geocoded_coords[0], geocoded_coords[1]),
                )

        # --- Fallback: wttr.in ---
        # If Nominatim geocoded the city successfully, pass coordinates to
        # wttr.in instead of the bare city name.  Coordinates bypass wttr.in's
        # own ambiguous city resolution (e.g., "chicago" â†’ Mccormickville).
        if geocoded_coords:
            return _cache_weather_result(
                cache_key,
                await _enrich_with_air_quality(
                    await _fetch_wttr(
                        lat=geocoded_coords[0],
                        lon=geocoded_coords[1],
                        city=city,
                    ),
                    geocoded_coords[0],
                    geocoded_coords[1],
                ),
            )

        fallback = await _fetch_wttr(
            lat=lat,
            lon=lon,
            city=city,
            location_url_encoded=location_url_encoded,
        )
        if lat is not None and lon is not None:
            return _cache_weather_result(cache_key, await _enrich_with_air_quality(fallback, lat, lon))
        return _cache_weather_result(cache_key, fallback)

    except Exception as exc:
        log.warning("Weather fetch failed: %s", exc)
        return None
