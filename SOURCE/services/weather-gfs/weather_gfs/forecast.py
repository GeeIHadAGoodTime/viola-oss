from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from typing import Any

import numpy as np

from weather_gfs.derive import (
    apparent_temperature_f,
    condition_from_fields,
    kelvin_to_fahrenheit,
    meters_per_second_to_mph,
    meters_to_miles,
    mm_to_inches,
    pascal_to_hpa,
    precipitation_chance_pct,
    relative_humidity_from_dewpoint,
    sunrise_sunset,
    uv_index_from_gfs,
    wind_direction_cardinal,
    wind_direction_degrees,
)
from weather_gfs.localtime import timezone_for, zone_label, zone_offset_seconds
from weather_gfs.storage import ForecastStore


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _round(value: float | None, digits: int = 0) -> int | float | None:
    if value is None:
        return None
    if digits <= 0:
        return round(value)
    return round(value, digits)


def _celsius_from_fahrenheit(value: float | None) -> int | None:
    if value is None:
        return None
    return round((value - 32.0) * 5.0 / 9.0)


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _hour_payload(
    *,
    latitude: float,
    longitude: float,
    timestamp: dt.datetime,
    values: dict[str, float | None],
) -> dict[str, Any]:
    temp_f = kelvin_to_fahrenheit(values.get("t2m"))
    dew_f = kelvin_to_fahrenheit(values.get("d2m"))
    humidity = values.get("r2")
    if humidity is None:
        humidity = relative_humidity_from_dewpoint(temp_f, dew_f)
    wind_mph = meters_per_second_to_mph(
        math.hypot(values.get("u10") or 0.0, values.get("v10") or 0.0)
        if values.get("u10") is not None and values.get("v10") is not None
        else None
    )
    wind_direction = wind_direction_degrees(values.get("u10"), values.get("v10"))
    gust_mph = meters_per_second_to_mph(values.get("gust"))
    pressure = pascal_to_hpa(values.get("prmsl"))
    visibility = meters_to_miles(values.get("vis"))
    precip_inches = mm_to_inches(values.get("tp"))
    precip_chance = precipitation_chance_pct(
        precip_inches=precip_inches,
        prate_kg_m2_s=values.get("prate"),
        relative_humidity=humidity,
        cloud_cover_pct=values.get("tcc"),
    )
    uv_index = uv_index_from_gfs(
        latitude=latitude,
        longitude=longitude,
        when=timestamp,
        downward_shortwave_wm2=values.get("sdswrf"),
        cloud_cover_pct=values.get("tcc"),
    )
    condition = condition_from_fields(
        cloud_cover_pct=values.get("tcc"),
        precip_inches=precip_inches,
        precip_chance=precip_chance,
    )
    feels_like = apparent_temperature_f(temp_f, humidity, wind_mph)
    payload = {
        "time": timestamp.isoformat(),
        "date": timestamp.date().isoformat(),
        "hour": timestamp.strftime("%H:%M"),
        "temperature": _round(temp_f),
        "temperature_f": _round(temp_f),
        "temperature_c": _celsius_from_fahrenheit(temp_f),
        "feels_like": _round(feels_like),
        "feels_like_f": _round(feels_like),
        "condition": condition["condition"],
        "condition_code": condition["condition_code"],
        "is_daytime": 6 <= timestamp.hour < 20,
        "humidity": _round(humidity),
        "dew_point": _round(dew_f),
        "dew_point_f": _round(dew_f),
        "precipitation_probability": _round(precip_chance),
        "precip_chance": _round(precip_chance),
        "precipitation": _round(precip_inches, 2),
        "precipitation_in": _round(precip_inches, 2),
        "wind_speed": _round(wind_mph, 1),
        "wind_speed_mph": _round(wind_mph, 1),
        "wind_direction": _round(wind_direction),
        "wind_direction_cardinal": wind_direction_cardinal(wind_direction),
        "wind_gust": _round(gust_mph, 1),
        "wind_gust_mph": _round(gust_mph, 1),
        "pressure": _round(pressure, 1),
        "pressure_hpa": _round(pressure, 1),
        "visibility": _round(visibility, 1),
        "visibility_miles": _round(visibility, 1),
        "cloud_cover": _round(values.get("tcc")),
        "uv_index": _round(uv_index, 1),
    }
    return _clean(payload)


def build_forecast_payload(
    *,
    store: ForecastStore,
    latitude: float,
    longitude: float,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    point = store.point_series(latitude, longitude)
    metadata = point["metadata"]
    series: dict[str, np.ndarray] = point["values"]
    base_time = dt.datetime.fromisoformat(metadata.base_time)
    base_time = _ensure_utc(base_time)
    now_utc = _ensure_utc(now or dt.datetime.now(dt.UTC))
    # GFS runs on the UTC clock, but a forecast day is a day where the user is
    # standing. Every timestamp below is moved onto the location's civil clock
    # before anything reads a date or an hour off it, so the daily buckets,
    # weekday names and is_daytime flags describe the user's calendar rather
    # than Greenwich's.
    local_zone = timezone_for(latitude, longitude)

    hourly_all: list[dict[str, Any]] = []
    for index, forecast_hour in enumerate(metadata.forecast_hours):
        timestamp = (base_time + dt.timedelta(hours=forecast_hour)).astimezone(local_zone)
        hour_values = {variable: _safe_float(values[index]) for variable, values in series.items()}
        hourly_all.append(
            _hour_payload(
                latitude=latitude,
                longitude=longitude,
                timestamp=timestamp,
                values=hour_values,
            )
        )
    future_hourly = [
        item for item in hourly_all if dt.datetime.fromisoformat(str(item["time"])).astimezone(dt.UTC) >= now_utc
    ]
    has_future_hourly = bool(future_hourly)
    displayed_hourly = future_hourly if has_future_hourly else hourly_all

    daily_forecast: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in displayed_hourly:
        by_date.setdefault(str(item.get("date")), []).append(item)
    for date_text, hours in list(by_date.items())[:10]:
        temps = [float(item["temperature_f"]) for item in hours if item.get("temperature_f") is not None]
        if not temps:
            continue
        precip = [float(item.get("precip_chance") or 0.0) for item in hours]
        precip_inches = [float(item.get("precipitation_in") or 0.0) for item in hours]
        uv_values = [float(item.get("uv_index") or 0.0) for item in hours]
        wind_values = [float(item.get("wind_speed_mph") or 0.0) for item in hours]
        # The day's condition comes from the hours we actually computed, label
        # and code together. Deriving the code separately (from precipitation
        # alone) used to let a day read "Clear" while carrying a partly-cloudy
        # code, so the icon contradicted the words next to it.
        known_conditions = [
            (str(item["condition"]), str(item.get("condition_code") or "unknown"))
            for item in hours
            if item.get("condition")
        ]
        parsed_date = dt.date.fromisoformat(date_text)
        sun_values = sunrise_sunset(latitude, longitude, parsed_date, local_zone)
        if known_conditions:
            condition, condition_code = Counter(known_conditions).most_common(1)[0][0]
        else:
            fallback = condition_from_fields(
                cloud_cover_pct=None,
                precip_inches=max(precip_inches) if precip_inches else 0.0,
                precip_chance=max(precip) if precip else 0.0,
            )
            condition = fallback["condition"]
            condition_code = fallback["condition_code"]
        daily_forecast.append(
            _clean(
                {
                    "date": date_text,
                    "day": parsed_date.strftime("%A"),
                    "temperature": _round(sum(temps) / len(temps)),
                    "high": _round(max(temps)),
                    "low": _round(min(temps)),
                    "high_f": _round(max(temps)),
                    "low_f": _round(min(temps)),
                    "condition": condition,
                    "condition_code": condition_code,
                    "precipitation_probability": _round(max(precip) if precip else 0.0),
                    "precip_chance": _round(max(precip) if precip else 0.0),
                    "precipitation_sum": _round(sum(precip_inches), 2),
                    "precipitation_in": _round(sum(precip_inches), 2),
                    "wind_speed": _round(max(wind_values) if wind_values else None, 1),
                    "wind_speed_mph": _round(max(wind_values) if wind_values else None, 1),
                    "uv_index": _round(max(uv_values) if uv_values else 0.0, 1),
                    "sunrise": sun_values["sunrise"],
                    "sunset": sun_values["sunset"],
                }
            )
        )

    current = dict(displayed_hourly[0] if displayed_hourly else {})
    first_day = daily_forecast[0] if daily_forecast else {}
    if first_day:
        current.setdefault("sunrise", first_day.get("sunrise"))
        current.setdefault("sunset", first_day.get("sunset"))
    # Same instant the cycle was published, rendered on the location's clock so
    # a consumer taking the date off it (to answer "is this row today?") reads
    # the local day and not the UTC one.
    updated_local = base_time.astimezone(local_zone).isoformat()
    current["updated"] = updated_local
    location = "%.4f, %.4f" % (latitude, longitude)
    payload = {
        "temperature": current.get("temperature_f"),
        "temperature_f": current.get("temperature_f"),
        "temp_f": current.get("temperature_f"),
        "temperature_c": current.get("temperature_c"),
        "unit": "F",
        "measurement_system": "imperial",
        "condition": current.get("condition"),
        "description": current.get("condition"),
        "condition_code": current.get("condition_code"),
        "is_daytime": current.get("is_daytime"),
        "location": location,
        "updated": updated_local,
        "source": "noaa-gfs",
        "provider": "NOAA GFS",
        "forecast_provider": "NOAA GFS",
        "cached": False,
        "stale": not has_future_hourly,
        # The zone every date/hour in this payload is expressed in. An IANA key
        # where one exists, so a consumer can rebuild the daylight-saving rules
        # and ask "what day is it there right now?"; the offset is published
        # alongside for consumers without a tz database.
        "timezone": zone_label(local_zone, at=now_utc),
        "timezone_offset_seconds": zone_offset_seconds(local_zone, at=now_utc),
        "current": current,
        "hourly": displayed_hourly,
        "hourly_forecast": displayed_hourly[:24],
        "daily": daily_forecast,
        "daily_forecast": daily_forecast,
        "forecast": daily_forecast,
        "forecast_high_f": first_day.get("high_f"),
        "forecast_low_f": first_day.get("low_f"),
        "high_f": first_day.get("high_f"),
        "low_f": first_day.get("low_f"),
    }
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
            payload[key] = current[key]
    return _clean(payload)
