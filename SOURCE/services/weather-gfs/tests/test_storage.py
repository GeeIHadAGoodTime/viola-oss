from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from weather_gfs.storage import VARIABLES, CycleMetadata, ForecastStore


def _metadata(cycle_id: str, hours: list[int]) -> CycleMetadata:
    return CycleMetadata(
        cycle_id=cycle_id,
        base_time=dt.datetime(2026, 5, 12, tzinfo=dt.UTC).isoformat(),
        forecast_hours=hours,
        created_at=dt.datetime(2026, 5, 12, tzinfo=dt.UTC).isoformat(),
        source="test",
        variable_mapping={},
    )


def _values(hours: list[int], latitudes: np.ndarray, longitudes: np.ndarray, base: float) -> dict[str, np.ndarray]:
    values: dict[str, np.ndarray] = {}
    for offset, variable in enumerate(VARIABLES):
        array = np.zeros((len(hours), len(latitudes), len(longitudes)), dtype="f4")
        for index, _hour in enumerate(hours):
            array[index, :, :] = base + offset + index
        values[variable] = array
    return values


def test_zarr_write_read_roundtrip(tmp_path) -> None:
    store = ForecastStore(tmp_path)
    hours = [0, 1, 2]
    latitudes = np.array([1.0, 0.0], dtype="f4")
    longitudes = np.array([10.0, 11.0], dtype="f4")

    store.write_cycle_from_arrays(
        metadata=_metadata("cycle-a", hours),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_values(hours, latitudes, longitudes, 100.0),
    )

    point = store.point_series(0.5, 10.5)
    assert point["metadata"].cycle_id == "cycle-a"
    assert point["values"]["t2m"].tolist() == [100.0, 101.0, 102.0]


def test_atomic_swap_keeps_previous_cycle_on_partial_write(tmp_path) -> None:
    store = ForecastStore(tmp_path)
    hours = [0, 1]
    latitudes = np.array([1.0, 0.0], dtype="f4")
    longitudes = np.array([10.0, 11.0], dtype="f4")
    store.write_cycle_from_arrays(
        metadata=_metadata("cycle-a", hours),
        latitudes=latitudes,
        longitudes=longitudes,
        values=_values(hours, latitudes, longitudes, 100.0),
    )

    with pytest.raises(RuntimeError, match="simulated write failure"):
        store.write_cycle_from_arrays(
            metadata=_metadata("cycle-b", hours),
            latitudes=latitudes,
            longitudes=longitudes,
            values=_values(hours, latitudes, longitudes, 200.0),
            fail_before_commit=True,
        )

    assert store.latest_metadata().cycle_id == "cycle-a"
    point = store.point_series(0.5, 10.5)
    assert point["values"]["t2m"].tolist() == [100.0, 101.0]
