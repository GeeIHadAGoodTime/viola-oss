from __future__ import annotations

import datetime as dt

from weather_gfs.derive import apparent_temperature_f, condition_from_fields, uv_index_from_gfs


def test_uv_index_cloudless_noon_equator_is_high() -> None:
    value = uv_index_from_gfs(
        latitude=0.0,
        longitude=0.0,
        when=dt.datetime(2026, 3, 20, 12, tzinfo=dt.UTC),
        downward_shortwave_wm2=1000.0,
        cloud_cover_pct=0.0,
    )

    assert 11.0 <= value <= 12.5


def test_uv_index_cloudy_high_latitude_winter_is_near_zero() -> None:
    value = uv_index_from_gfs(
        latitude=67.0,
        longitude=0.0,
        when=dt.datetime(2026, 12, 20, 12, tzinfo=dt.UTC),
        downward_shortwave_wm2=40.0,
        cloud_cover_pct=95.0,
    )

    assert value < 0.2


def test_apparent_temperature_uses_heat_index_for_hot_humid_weather() -> None:
    feels_like = apparent_temperature_f(92.0, 70.0, 5.0)

    assert feels_like is not None
    assert round(feels_like) >= 105


def test_apparent_temperature_uses_wind_chill_for_cold_windy_weather() -> None:
    feels_like = apparent_temperature_f(30.0, 45.0, 20.0)

    assert feels_like is not None
    assert round(feels_like) <= 18


def test_apparent_temperature_returns_air_temp_for_neutral_weather() -> None:
    assert apparent_temperature_f(68.0, 50.0, 6.0) == 68.0


def test_condition_reports_unknown_when_cloud_cover_is_missing() -> None:
    """No cloud cover means no sky claim, rather than a default partly cloudy."""
    result = condition_from_fields(cloud_cover_pct=None, precip_inches=0.0, precip_chance=0.0)

    assert result["condition"] is None
    assert result["condition_code"] == "unknown"


def test_condition_still_reports_rain_without_cloud_cover() -> None:
    """Precipitation is its own evidence and does not need cloud cover."""
    result = condition_from_fields(cloud_cover_pct=None, precip_inches=0.3, precip_chance=0.0)

    assert result["condition"] == "Rain"
    assert result["condition_code"] == "rain"


def test_condition_reads_the_sky_when_cloud_cover_is_present() -> None:
    assert condition_from_fields(cloud_cover_pct=5.0, precip_inches=0.0, precip_chance=0.0) == {
        "condition": "Clear",
        "condition_code": "clear",
    }
    assert condition_from_fields(cloud_cover_pct=40.0, precip_inches=0.0, precip_chance=0.0) == {
        "condition": "Partly cloudy",
        "condition_code": "partly_cloudy",
    }
    assert condition_from_fields(cloud_cover_pct=95.0, precip_inches=0.0, precip_chance=0.0) == {
        "condition": "Overcast",
        "condition_code": "overcast",
    }
