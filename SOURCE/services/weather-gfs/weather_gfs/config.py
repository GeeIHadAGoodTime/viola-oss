from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ServiceConfig:
    data_dir: Path
    tmp_dir: Path
    bucket: str
    forecast_max_hour: int
    forecast_source_step_hours: int
    cache_ttl_seconds: int
    ingestion_interval_hours: float
    fixture_mode: bool
    fixture_bootstrap: bool
    ingest_on_start: bool
    keep_cycles: int


def load_config() -> ServiceConfig:
    return ServiceConfig(
        data_dir=Path(os.environ.get("WEATHER_GFS_DATA_DIR", "/data")),
        tmp_dir=Path(os.environ.get("WEATHER_GFS_TMP_DIR", str(Path(tempfile.gettempdir()) / "weather-gfs"))),
        bucket=os.environ.get("WEATHER_GFS_BUCKET", "noaa-gfs-bdp-pds"),
        forecast_max_hour=_int_env("WEATHER_GFS_MAX_FORECAST_HOUR", 240),
        forecast_source_step_hours=_int_env("WEATHER_GFS_SOURCE_STEP_HOURS", 3),
        cache_ttl_seconds=_int_env("WEATHER_GFS_CACHE_TTL_SECONDS", 3600),
        ingestion_interval_hours=_float_env("WEATHER_GFS_INGESTION_INTERVAL_HOURS", 6.0),
        fixture_mode=_bool_env("WEATHER_GFS_FIXTURE_MODE", False),
        fixture_bootstrap=_bool_env("WEATHER_GFS_FIXTURE_BOOTSTRAP", False),
        ingest_on_start=_bool_env("WEATHER_GFS_INGEST_ON_START", True),
        keep_cycles=_int_env("WEATHER_GFS_KEEP_CYCLES", 2),
    )
