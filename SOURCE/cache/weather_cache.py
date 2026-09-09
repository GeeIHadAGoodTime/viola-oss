"""
Weather Cache Module

Simple file-based cache for weather data.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from core.constants import WEATHER_CACHE_THRESHOLD
from core.logging_config import get_logger

logger = get_logger(__name__)


class WeatherCache:
    """File-based cache for weather data with TTL support."""

    def __init__(self, cache_dir: Path, ttl_hours: float = 0.333):
        """
        Initialize weather cache.

        Args:
            cache_dir: Directory to store cache files
            ttl_hours: Time-to-live for cache entries in hours
        """
        self._cache_dir = cache_dir
        self._ttl_seconds = ttl_hours * 3600
        self._lock = threading.Lock()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(lat: float | None, lon: float | None, city: str | None) -> str:
        """Generate cache key from location parameters."""
        if lat is not None and lon is not None:
            return f"{lat:.4f},{lon:.4f}"
        if city:
            normalized = " ".join(city.strip().lower().split())
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
            return f"city_{digest}"
        return "__auto__"

    def _cache_path(self, key: str) -> Path:
        """Get cache file path for a key."""
        path = (self._cache_dir / f"weather_{key}.json").resolve()
        path.relative_to(self._cache_dir.resolve())
        return path

    def get(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
        allow_nearby: bool = True,
        max_distance_km: float = 50.0,
    ) -> dict[str, Any] | None:
        """
        Get cached weather data.

        Args:
            lat: Latitude
            lon: Longitude
            city: City name
            allow_nearby: Allow nearby location matches (not implemented)
            max_distance_km: Max distance for nearby matches (not implemented)

        Returns:
            Cached weather data or None if not found/expired
        """
        key = self._key(lat, lon, city)
        cache_path = self._cache_path(key)

        with self._lock:
            if not cache_path.exists():
                return None

            try:
                with open(cache_path, encoding="utf-8") as f:
                    data = json.load(f)

                # Check TTL
                cached_time = data.get("cached_at", 0)
                if time.time() - cached_time > self._ttl_seconds:
                    # Expired, remove file
                    cache_path.unlink(missing_ok=True)
                    return None

                # Mark as stale if close to expiry
                age_hours = (time.time() - cached_time) / 3600
                if age_hours > (self._ttl_seconds / 3600 * WEATHER_CACHE_THRESHOLD):
                    data["stale"] = True

                data["cache_age_hours"] = age_hours
                return data

            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read weather cache for %s: %s", key, e)
                cache_path.unlink(missing_ok=True)
                return None

    def set(
        self,
        weather_data: dict[str, Any],
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
        forecast_data: Any | None = None,
    ) -> None:
        """
        Store weather data in cache.

        Args:
            weather_data: Weather data to cache
            lat: Latitude
            lon: Longitude
            city: City name
            forecast_data: Forecast data (ignored)
        """
        key = self._key(lat, lon, city)
        cache_path = self._cache_path(key)

        with self._lock:
            data = dict(weather_data)
            data["cached_at"] = time.time()
            data["stale"] = False

            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError as e:
                logger.warning("Failed to write weather cache for %s: %s", key, e)

    def get_stale(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a cached entry even when expired.

        This is used only as an offline fallback after a live provider fetch
        fails. Normal cache reads still enforce TTL through ``get``.
        """
        key = self._key(lat, lon, city)
        cache_path = self._cache_path(key)
        with self._lock:
            if not cache_path.exists():
                return None
            try:
                with open(cache_path, encoding="utf-8") as f:
                    data = json.load(f)
                cached_time = float(data.get("cached_at", 0) or 0)
                data["cache_age_hours"] = (time.time() - cached_time) / 3600 if cached_time else 0.0
                data["stale"] = True
                return data
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
                logger.warning("Failed to read stale weather cache for %s: %s", key, e)
                return None

    def get_trends(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get weather trends — not yet implemented."""
        raise NotImplementedError("Weather trends analysis is not yet available.")

    def get_prediction(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
    ) -> dict[str, Any] | None:
        """Get weather prediction — not yet implemented."""
        raise NotImplementedError("Weather prediction is not yet available.")

    def clear(self) -> None:
        """
        Clear all cached weather data.

        Removes all cached weather entries from the cache directory.
        """
        with self._lock:
            for cache_file in self._cache_dir.glob("weather_*.json"):
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to remove cache file %s: %s", cache_file, e)

    def clear_entry(
        self,
        lat: float | None = None,
        lon: float | None = None,
        city: str | None = None,
    ) -> None:
        """Clear one weather cache entry instead of every user's cache."""
        key = self._key(lat, lon, city)
        cache_path = self._cache_path(key)
        with self._lock:
            try:
                cache_path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to remove cache file %s: %s", cache_path, e)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """
        Get snapshot of all cached data.

        Returns:
            Dictionary of cache keys to cached data
        """
        result = {}
        with self._lock:
            for cache_file in self._cache_dir.glob("weather_*.json"):
                try:
                    with open(cache_file, encoding="utf-8") as f:
                        data = json.load(f)
                    key = cache_file.stem.replace("weather_", "")
                    result[key] = data
                except (json.JSONDecodeError, OSError):
                    continue
        return result
