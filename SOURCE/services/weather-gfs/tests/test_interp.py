from __future__ import annotations

import numpy as np
from weather_gfs.interp import bilinear


def test_bilinear_reproduces_known_grid_point() -> None:
    latitudes = np.array([1.0, 0.0])
    longitudes = np.array([10.0, 11.0])
    grid = np.array([[5.0, 7.0], [9.0, 11.0]])

    assert bilinear(grid, latitudes, longitudes, 1.0, 10.0) == 5.0
    assert bilinear(grid, latitudes, longitudes, 0.0, 11.0) == 11.0


def test_bilinear_interpolates_between_four_corners() -> None:
    latitudes = np.array([1.0, 0.0])
    longitudes = np.array([10.0, 11.0])
    grid = np.array([[10.0, 20.0], [30.0, 40.0]])

    assert bilinear(grid, latitudes, longitudes, 0.5, 10.5) == 25.0


def test_bilinear_normalizes_negative_longitude_for_gfs_grid() -> None:
    latitudes = np.array([1.0, 0.0])
    longitudes = np.array([270.0, 271.0])
    grid = np.array([[10.0, 20.0], [30.0, 40.0]])

    assert bilinear(grid, latitudes, longitudes, 0.5, -89.5) == 25.0
