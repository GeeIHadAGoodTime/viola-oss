from __future__ import annotations

import datetime as dt
import math
from typing import Any

from astral import LocationInfo
from astral.sun import sun


def kelvin_to_fahrenheit(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return (value - 273.15) * 9.0 / 5.0 + 32.0


def meters_per_second_to_mph(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return value * 2.2369362921


def meters_to_miles(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return value / 1609.344


def pascal_to_hpa(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return value / 100.0


def mm_to_inches(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return value / 25.4


def wind_direction_degrees(u_ms: float | None, v_ms: float | None) -> float | None:
    if u_ms is None or v_ms is None or math.isnan(u_ms) or math.isnan(v_ms):
        return None
    degrees = math.degrees(math.atan2(u_ms, v_ms)) + 180.0
    return degrees % 360.0


def wind_direction_cardinal(degrees: float | None) -> str | None:
    if degrees is None or math.isnan(degrees):
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
    return directions[int((degrees % 360.0) / 22.5 + 0.5) % 16]


def relative_humidity_from_dewpoint(temp_f: float | None, dewpoint_f: float | None) -> float | None:
    if temp_f is None or dewpoint_f is None:
        return None
    temp_c = (temp_f - 32.0) * 5.0 / 9.0
    dew_c = (dewpoint_f - 32.0) * 5.0 / 9.0
    saturation = math.exp((17.625 * temp_c) / (243.04 + temp_c))
    actual = math.exp((17.625 * dew_c) / (243.04 + dew_c))
    return max(0.0, min(100.0, 100.0 * actual / saturation))


def heat_index_f(temp_f: float, relative_humidity: float) -> float:
    return (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * relative_humidity
        - 0.22475541 * temp_f * relative_humidity
        - 0.00683783 * temp_f * temp_f
        - 0.05481717 * relative_humidity * relative_humidity
        + 0.00122874 * temp_f * temp_f * relative_humidity
        + 0.00085282 * temp_f * relative_humidity * relative_humidity
        - 0.00000199 * temp_f * temp_f * relative_humidity * relative_humidity
    )


def wind_chill_f(temp_f: float, wind_mph: float) -> float:
    return 35.74 + 0.6215 * temp_f - 35.75 * (wind_mph**0.16) + 0.4275 * temp_f * (wind_mph**0.16)


def apparent_temperature_f(
    temp_f: float | None,
    relative_humidity: float | None,
    wind_mph: float | None,
) -> float | None:
    if temp_f is None:
        return None
    if relative_humidity is not None and temp_f >= 80.0 and relative_humidity >= 40.0:
        return heat_index_f(temp_f, relative_humidity)
    if wind_mph is not None and temp_f <= 50.0 and wind_mph >= 3.0:
        return wind_chill_f(temp_f, wind_mph)
    return temp_f


def solar_cos_zenith(latitude: float, longitude: float, when: dt.datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    when = when.astimezone(dt.UTC)
    day_of_year = when.timetuple().tm_yday
    fractional_hour = when.hour + when.minute / 60.0 + when.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (fractional_hour - 12.0) / 24.0)
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    true_solar_time = (fractional_hour * 60.0 + equation_of_time + 4.0 * longitude) % 1440.0
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)
    lat_rad = math.radians(latitude)
    cos_zenith = math.sin(lat_rad) * math.sin(declination) + math.cos(lat_rad) * math.cos(declination) * math.cos(
        hour_angle
    )
    return max(0.0, cos_zenith)


def uv_index_from_gfs(
    *,
    latitude: float,
    longitude: float,
    when: dt.datetime,
    downward_shortwave_wm2: float | None,
    cloud_cover_pct: float | None,
) -> float:
    cos_zenith = solar_cos_zenith(latitude, longitude, when)
    if cos_zenith <= 0.0:
        return 0.0
    radiation_factor = 1.0
    if downward_shortwave_wm2 is not None and not math.isnan(downward_shortwave_wm2):
        radiation_factor = max(0.0, min(1.15, downward_shortwave_wm2 / 1000.0))
    cloud_fraction = 0.0
    if cloud_cover_pct is not None and not math.isnan(cloud_cover_pct):
        cloud_fraction = max(0.0, min(1.0, cloud_cover_pct / 100.0))
    clear_sky_uv = 12.5 * cos_zenith * 0.96 * radiation_factor
    cloud_multiplier = 1.0 - 0.75 * cloud_fraction
    return max(0.0, clear_sky_uv * cloud_multiplier)


def sunrise_sunset(
    latitude: float,
    longitude: float,
    date_value: dt.date,
    tz: dt.tzinfo | None = None,
) -> dict[str, str | None]:
    """Return sunrise/sunset for the *local* calendar day ``date_value``.

    ``date_value`` is the day as the location reads it, and the returned ISO
    strings are rendered on that same clock, so a client showing "sunrise 5:12"
    is showing the hour someone standing there would see. The instants are
    unchanged by the choice of ``tz`` -- only how they print.
    """
    zone = tz or dt.UTC
    location = LocationInfo(name="Forecast", region="", timezone="UTC", latitude=latitude, longitude=longitude)
    try:
        values = sun(location.observer, date=date_value, tzinfo=zone)
    except ValueError:
        # Polar day/night: the sun does not cross the horizon on this date.
        return {"sunrise": None, "sunset": None}
    return {
        "sunrise": values["sunrise"].isoformat(),
        "sunset": values["sunset"].isoformat(),
    }


def condition_from_fields(
    *,
    cloud_cover_pct: float | None,
    precip_inches: float | None,
    precip_chance: float | None,
) -> dict[str, Any]:
    precip = precip_inches or 0.0
    chance = precip_chance or 0.0
    cloud = cloud_cover_pct
    if precip >= 0.12 or chance >= 70:
        label = "Rain"
        code = "rain"
    elif precip >= 0.02 or chance >= 35:
        label = "Chance rain"
        code = "rain"
    elif cloud is None:
        # Without cloud cover there is nothing to say about the sky. Saying
        # "Partly cloudy" here (the old default) turned a missing field into a
        # confident claim that a client then drew an icon for.
        return {"condition": None, "condition_code": "unknown"}
    elif cloud >= 88:
        label = "Overcast"
        code = "overcast"
    elif cloud >= 60:
        label = "Cloudy"
        code = "cloudy"
    elif cloud >= 25:
        label = "Partly cloudy"
        code = "partly_cloudy"
    else:
        label = "Clear"
        code = "clear"
    return {"condition": label, "condition_code": code}


def precipitation_chance_pct(
    *,
    precip_inches: float | None,
    prate_kg_m2_s: float | None,
    relative_humidity: float | None,
    cloud_cover_pct: float | None,
) -> float:
    precip = precip_inches or 0.0
    rate = 0.0 if prate_kg_m2_s is None or math.isnan(prate_kg_m2_s) else prate_kg_m2_s
    humidity = relative_humidity or 0.0
    cloud = cloud_cover_pct or 0.0
    if precip >= 0.1 or rate >= 0.00008:
        return 95.0
    if precip >= 0.03 or rate >= 0.00002:
        return 70.0
    if precip >= 0.005 or rate > 0.0:
        return 45.0
    return max(0.0, min(35.0, (humidity - 65.0) * 0.5 + cloud * 0.15))
