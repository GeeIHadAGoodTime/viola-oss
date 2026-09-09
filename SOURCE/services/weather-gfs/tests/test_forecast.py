from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pytest

pytest.importorskip("astral")
pytest.importorskip("zarr")
from weather_gfs.forecast import build_forecast_payload
from weather_gfs.storage import VARIABLES, CycleMetadata, ForecastStore


def _values(hours: list[int], latitudes: np.ndarray, longitudes: np.ndarray) -> dict[str, np.ndarray]:
    values: dict[str, np.ndarray] = {}
    for variable in VARIABLES:
        array = np.zeros((len(hours), len(latitudes), len(longitudes)), dtype="f4")
        for index, _hour in enumerate(hours):
            array[index, :, :] = 280.0 + index
        values[variable] = array
    return values


def test_build_forecast_payload_prefers_now_or_future_rows(tmp_path) -> None:
    base_time = dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
    now = dt.datetime(2026, 5, 14, tzinfo=dt.UTC)
    hours = [0, 23, 24, 25]
    latitudes = np.array([44.0, 43.0], dtype="f4")
    longitudes = np.array([-88.0, -87.0], dtype="f4")
    store = ForecastStore(tmp_path)
    store.write_cycle_from_arrays(
        metadata=CycleMetadata(
            cycle_id="cycle-a",
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=base_time.isoformat(),
            source="test",
            variable_mapping={},
        ),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_values(hours, latitudes, longitudes),
    )

    payload = build_forecast_payload(store=store, latitude=43.5, longitude=-87.5, now=now)

    # The same instant as ``now``, stated on the clock at 43.5N 87.5W
    # (America/Chicago, on daylight time in May), because that is the clock
    # every date and hour in this payload is read off (#4216).
    local_now = now.astimezone(ZoneInfo("America/Chicago"))
    assert local_now.isoformat() == "2026-05-13T19:00:00-05:00"
    assert payload["current"]["time"] == local_now.isoformat()
    assert payload["hourly_forecast"][0]["time"] == local_now.isoformat()
    assert payload["hourly"][0]["time"] == local_now.isoformat()
    # #4216 regression record: this file used to assert "2026-05-14" here, the
    # UTC date. 2026-05-14T00:00Z is 19:00 on the 13th in Wisconsin, so the old
    # expectation was itself an instance of the bug -- someone looking out the
    # window on Wednesday evening was handed Thursday's forecast day.
    assert payload["daily_forecast"][0]["date"] == "2026-05-13"
    assert payload["forecast"][0]["date"] == "2026-05-13"
    assert payload["stale"] is False


def test_build_forecast_payload_marks_all_past_rows_stale(tmp_path) -> None:
    base_time = dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
    now = dt.datetime(2026, 5, 14, tzinfo=dt.UTC)
    hours = [0, 1, 2]
    latitudes = np.array([44.0, 43.0], dtype="f4")
    longitudes = np.array([-88.0, -87.0], dtype="f4")
    store = ForecastStore(tmp_path)
    store.write_cycle_from_arrays(
        metadata=CycleMetadata(
            cycle_id="cycle-a",
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=base_time.isoformat(),
            source="test",
            variable_mapping={},
        ),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_values(hours, latitudes, longitudes),
    )

    payload = build_forecast_payload(store=store, latitude=43.5, longitude=-87.5, now=now)

    assert payload["stale"] is True


def _overcast_dry_values(hours: list[int], latitudes: np.ndarray, longitudes: np.ndarray) -> dict[str, np.ndarray]:
    """A solidly overcast, rain-free day: cloud cover 95%, no precipitation."""
    per_variable = {
        "t2m": 288.0,
        "d2m": 283.0,
        "r2": 70.0,
        "u10": 2.0,
        "v10": 1.0,
        "tcc": 95.0,
        "prate": 0.0,
        "tp": 0.0,
        "prmsl": 101000.0,
        "gust": 4.0,
        "vis": 20000.0,
        "sdswrf": 120.0,
    }
    values: dict[str, np.ndarray] = {}
    for variable in VARIABLES:
        array = np.zeros((len(hours), len(latitudes), len(longitudes)), dtype="f4")
        array[:, :, :] = per_variable[variable]
        values[variable] = array
    return values


def test_daily_condition_label_and_code_describe_the_same_sky(tmp_path) -> None:
    """A day's words and its icon code come from the same hours.

    The code used to be re-derived from precipitation alone (with cloud cover
    deliberately passed as None), so an overcast day carried a partly-cloudy
    code and the icon contradicted the label printed next to it.
    """
    base_time = dt.datetime(2026, 5, 13, tzinfo=dt.UTC)
    now = dt.datetime(2026, 5, 14, tzinfo=dt.UTC)
    hours = [24, 25, 26]
    latitudes = np.array([44.0, 43.0], dtype="f4")
    longitudes = np.array([-88.0, -87.0], dtype="f4")
    store = ForecastStore(tmp_path)
    store.write_cycle_from_arrays(
        metadata=CycleMetadata(
            cycle_id="cycle-a",
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=base_time.isoformat(),
            source="test",
            variable_mapping={},
        ),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_overcast_dry_values(hours, latitudes, longitudes),
    )

    payload = build_forecast_payload(store=store, latitude=43.5, longitude=-87.5, now=now)

    day = payload["daily_forecast"][0]
    day_hours = [row for row in payload["hourly"] if row["date"] == day["date"]]
    assert day_hours
    assert {row["condition"] for row in day_hours} == {"Overcast"}
    assert day["condition"] == "Overcast"
    assert day["condition_code"] == "overcast"


# ---------------------------------------------------------------------------
# A forecast day is a day where the user is standing (#4216)
# ---------------------------------------------------------------------------


def _store_at(tmp_path, *, base_time: dt.datetime, hours: list[int], latitude: float, longitude: float):
    """A single-cycle store whose grid brackets ``(latitude, longitude)``."""
    latitudes = np.array([latitude + 0.5, latitude - 0.5], dtype="f4")
    longitudes = np.array([longitude - 0.5, longitude + 0.5], dtype="f4")
    store = ForecastStore(tmp_path)
    store.write_cycle_from_arrays(
        metadata=CycleMetadata(
            cycle_id="cycle-a",
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=base_time.isoformat(),
            source="test",
            variable_mapping={},
        ),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_values(hours, latitudes, longitudes),
    )
    return store


# 35.68N 139.69E is Tokyo (Asia/Tokyo, UTC+9 year round); 41.88N 87.63W is
# Chicago (America/Chicago, UTC-5 in July). One instant, two calendars.
_TOKYO = (35.68, 139.69)
_CHICAGO = (41.88, -87.63)


@pytest.mark.parametrize(
    ("latitude", "longitude", "expected_zone", "expected_day"),
    [
        (*_TOKYO, "Asia/Tokyo", "2026-08-01"),
        (*_CHICAGO, "America/Chicago", "2026-07-31"),
    ],
)
def test_daily_buckets_are_keyed_on_the_locations_own_calendar_day(
    tmp_path, latitude, longitude, expected_zone, expected_day
) -> None:
    """The same instant is a different calendar day in Tokyo and in Chicago.

    2026-07-31T20:00Z is 05:00 on August 1st in Tokyo and 15:00 on July 31st in
    Chicago. Before #4216 the service answered "2026-07-31" for both, because
    the bucket key came off the UTC timestamp -- so a Tokyo user asking at
    breakfast got yesterday's day labelled as their day.
    """
    instant = dt.datetime(2026, 7, 31, 20, tzinfo=dt.UTC)
    store = _store_at(tmp_path, base_time=instant, hours=[0, 1, 2], latitude=latitude, longitude=longitude)

    payload = build_forecast_payload(store=store, latitude=latitude, longitude=longitude, now=instant)

    assert payload["timezone"] == expected_zone
    assert payload["daily_forecast"][0]["date"] == expected_day
    # The weekday printed next to the day has to name the same day the key does.
    assert payload["daily_forecast"][0]["day"] == dt.date.fromisoformat(expected_day).strftime("%A")
    # ...and every hour that fed that bucket agrees it belongs there.
    assert {row["date"] for row in payload["hourly"]} == {expected_day}


def test_hourly_rows_join_the_daily_rows_on_the_date_key(tmp_path) -> None:
    """``WeatherForecast.jsx`` groups each day's expand by matching these two
    ``date`` strings, so a day whose hours are filed under a different key
    renders an empty expand. Both sides must be the location's calendar day.
    """
    latitude, longitude = _TOKYO
    # 30 hours from 20:00Z crosses two Tokyo midnights, so the join has to hold
    # across a day boundary rather than trivially inside one day.
    instant = dt.datetime(2026, 7, 31, 20, tzinfo=dt.UTC)
    store = _store_at(tmp_path, base_time=instant, hours=list(range(0, 30)), latitude=latitude, longitude=longitude)

    payload = build_forecast_payload(store=store, latitude=latitude, longitude=longitude, now=instant)

    daily_dates = [day["date"] for day in payload["daily_forecast"]]
    assert daily_dates == ["2026-08-01", "2026-08-02"]
    for day in payload["daily_forecast"]:
        matching_hours = [row for row in payload["hourly"] if row["date"] == day["date"]]
        assert matching_hours, "day %s has no hourly rows to expand" % day["date"]
    # No hourly row is orphaned under a date the daily list does not carry.
    assert {row["date"] for row in payload["hourly"]} <= set(daily_dates)


def test_is_daytime_follows_the_local_clock_not_the_utc_one(tmp_path) -> None:
    """1 PM in Tokyo is daytime and 1 AM is not, whatever UTC says.

    Read off UTC these two hours answer backwards: Tokyo's 01:00 is 16:00Z
    (called day) and Tokyo's 13:00 is 04:00Z (also called day, but the pair is
    what the client renders a moon against). ``WeatherForecast.jsx`` carries a
    client-side workaround for exactly this symptom.
    """
    latitude, longitude = _TOKYO
    # 2026-07-31T15:00Z is midnight on August 1st in Tokyo.
    midnight_local = dt.datetime(2026, 7, 31, 15, tzinfo=dt.UTC)
    store = _store_at(tmp_path, base_time=midnight_local, hours=[1, 13], latitude=latitude, longitude=longitude)

    payload = build_forecast_payload(store=store, latitude=latitude, longitude=longitude, now=midnight_local)

    by_hour = {row["hour"]: row for row in payload["hourly"]}
    assert set(by_hour) == {"01:00", "13:00"}
    assert by_hour["01:00"]["is_daytime"] is False
    assert by_hour["13:00"]["is_daytime"] is True


def test_payload_publishes_the_clock_its_dates_are_expressed_in(tmp_path) -> None:
    """A consumer must not have to guess which calendar the rows are on.

    The payload used to declare ``"timezone": "UTC"`` while its rows were UTC
    dates; now it names the zone it actually used and states the offset beside
    it, so a client with no tz database can still work out the local day.
    """
    latitude, longitude = _CHICAGO
    instant = dt.datetime(2026, 7, 31, 20, tzinfo=dt.UTC)
    store = _store_at(tmp_path, base_time=instant, hours=[0, 1], latitude=latitude, longitude=longitude)

    payload = build_forecast_payload(store=store, latitude=latitude, longitude=longitude, now=instant)

    assert payload["timezone"] == "America/Chicago"
    assert payload["timezone_offset_seconds"] == -5 * 3600
    # ``updated`` is the cycle instant on that same clock, so a consumer slicing
    # a date off it reads the local day rather than Greenwich's.
    assert payload["updated"] == instant.astimezone(ZoneInfo("America/Chicago")).isoformat()


def test_open_ocean_falls_back_to_the_nominal_longitude_zone(tmp_path) -> None:
    """A point with no civil timezone still gets a day that starts near local
    midnight, not one that starts at local noon (which is what UTC would give a
    mid-Pacific coordinate).
    """
    from weather_gfs.localtime import nautical_zone, timezone_for

    # 0N 180E: mid-Pacific, as far from a land timezone as a coordinate gets.
    zone = timezone_for(0.0, 180.0)
    reference = nautical_zone(180.0)
    moment = dt.datetime(2026, 7, 31, 20, tzinfo=dt.UTC)
    assert moment.astimezone(zone).utcoffset() == moment.astimezone(reference).utcoffset()
    # Never silently UTC for a coordinate half a world away from Greenwich.
    assert moment.astimezone(zone).utcoffset() != dt.timedelta(0)
