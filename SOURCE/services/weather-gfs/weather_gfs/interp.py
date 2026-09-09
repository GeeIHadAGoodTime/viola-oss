from __future__ import annotations

import numpy as np


def normalize_longitude(lon: float, grid_longitudes: np.ndarray) -> float:
    minimum = float(np.nanmin(grid_longitudes))
    maximum = float(np.nanmax(grid_longitudes))
    if minimum >= 0.0 and lon < 0.0:
        return lon + 360.0
    if maximum <= 180.0 and lon > 180.0:
        return lon - 360.0
    return lon


def bracket_indices(values: np.ndarray, target: float) -> tuple[int, int, float]:
    if values.ndim != 1:
        raise ValueError("values must be one-dimensional")
    if values.size < 2:
        return 0, 0, 0.0

    ascending = values[0] <= values[-1]
    ordered = values if ascending else values[::-1]
    clipped = float(np.clip(target, ordered[0], ordered[-1]))
    upper = int(np.searchsorted(ordered, clipped, side="left"))
    if upper <= 0:
        low_ordered = high_ordered = 0
    elif upper >= ordered.size:
        low_ordered = high_ordered = ordered.size - 1
    elif ordered[upper] == clipped:
        low_ordered = high_ordered = upper
    else:
        low_ordered = upper - 1
        high_ordered = upper

    if low_ordered == high_ordered:
        weight = 0.0
    else:
        span = float(ordered[high_ordered] - ordered[low_ordered])
        weight = 0.0 if span == 0.0 else float((clipped - ordered[low_ordered]) / span)

    if ascending:
        return low_ordered, high_ordered, weight
    size = values.size
    return size - 1 - low_ordered, size - 1 - high_ordered, weight


def bilinear(
    grid: np.ndarray,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    lat: float,
    lon: float,
) -> float:
    if grid.ndim != 2:
        raise ValueError("grid must be two-dimensional")
    normalized_lon = normalize_longitude(lon, longitudes)
    lat0, lat1, lat_weight = bracket_indices(latitudes, lat)
    lon0, lon1, lon_weight = bracket_indices(longitudes, normalized_lon)
    q00 = float(grid[lat0, lon0])
    q01 = float(grid[lat0, lon1])
    q10 = float(grid[lat1, lon0])
    q11 = float(grid[lat1, lon1])
    top = q00 * (1.0 - lon_weight) + q01 * lon_weight
    bottom = q10 * (1.0 - lon_weight) + q11 * lon_weight
    return top * (1.0 - lat_weight) + bottom * lat_weight


def bilinear_series(
    array: np.ndarray,
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    lat: float,
    lon: float,
) -> np.ndarray:
    if array.ndim != 3:
        raise ValueError("array must have shape (time, latitude, longitude)")
    normalized_lon = normalize_longitude(lon, longitudes)
    lat0, lat1, lat_weight = bracket_indices(latitudes, lat)
    lon0, lon1, lon_weight = bracket_indices(longitudes, normalized_lon)
    q00 = array[:, lat0, lon0].astype("f8")
    q01 = array[:, lat0, lon1].astype("f8")
    q10 = array[:, lat1, lon0].astype("f8")
    q11 = array[:, lat1, lon1].astype("f8")
    top = q00 * (1.0 - lon_weight) + q01 * lon_weight
    bottom = q10 * (1.0 - lon_weight) + q11 * lon_weight
    return top * (1.0 - lat_weight) + bottom * lat_weight
