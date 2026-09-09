from __future__ import annotations

import datetime as dt
import logging
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import cfgrib
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config

from weather_gfs.config import ServiceConfig
from weather_gfs.storage import VARIABLES, CycleMetadata, ForecastStore

LOGGER = logging.getLogger(__name__)

GRIB_TO_ZARR: dict[str, str] = {
    "TMP:2 m above ground": "t2m",
    "DPT:2 m above ground": "d2m",
    "RH:2 m above ground": "r2",
    "UGRD:10 m above ground": "u10",
    "VGRD:10 m above ground": "v10",
    "TCDC:entire atmosphere": "tcc",
    "PRATE:surface": "prate",
    "APCP:surface": "tp",
    "PRMSL:mean sea level": "prmsl",
    "GUST:surface": "gust",
    "VIS:surface": "vis",
    "DSWRF:surface": "sdswrf",
}

CFGRIB_VARIABLES: dict[str, str] = {
    "t2m": "t2m",
    "d2m": "d2m",
    "r2": "r2",
    "u10": "u10",
    "v10": "v10",
    "tcc": "tcc",
    "prate": "prate",
    "tp": "tp",
    "prmsl": "prmsl",
    "gust": "gust",
    "vis": "vis",
    "sdswrf": "sdswrf",
}


@dataclass(frozen=True)
class IndexRow:
    offset: int
    short_name: str
    level: str
    step: str
    raw: str


@dataclass(frozen=True)
class SelectedRange:
    variable: str
    start: int
    end: int | None
    raw: str


def target_forecast_hours(max_hour: int) -> list[int]:
    hourly_until = min(72, max_hour)
    hours = list(range(0, hourly_until + 1))
    next_three_hour = hourly_until + (3 - hourly_until % 3 if hourly_until % 3 else 3)
    hours.extend(range(next_three_hour, max_hour + 1, 3))
    return hours


def source_forecast_hours(max_hour: int, step_hours: int) -> list[int]:
    return list(range(0, max_hour + 1, step_hours))


def parse_idx(body: str) -> list[IndexRow]:
    rows: list[IndexRow] = []
    for line in body.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        rows.append(
            IndexRow(
                offset=int(parts[1]),
                short_name=parts[3],
                level=parts[4],
                step=parts[5],
                raw=line,
            )
        )
    return rows


def _candidate_score(row: IndexRow) -> int | None:
    key = "%s:%s" % (row.short_name, row.level)
    if key not in GRIB_TO_ZARR:
        return None
    if row.short_name in {"TMP", "DPT", "RH"} and row.level != "2 m above ground":
        return None
    if row.short_name in {"UGRD", "VGRD"} and row.level != "10 m above ground":
        return None
    if row.short_name == "TCDC" and row.level == "entire atmosphere":
        return 0 if "ave" not in row.step else 1
    if row.short_name == "PRATE":
        return 0 if row.step.endswith("fcst") and "ave" not in row.step else 1
    if row.short_name == "APCP":
        return 0 if "acc" in row.step else 1
    if row.short_name == "DSWRF":
        return 0 if "ave" in row.step else 1
    return 0


def select_variable_ranges(rows: list[IndexRow], *, object_size: int) -> list[SelectedRange]:
    chosen: dict[str, tuple[int, IndexRow]] = {}
    for row in rows:
        score = _candidate_score(row)
        if score is None:
            continue
        variable = GRIB_TO_ZARR["%s:%s" % (row.short_name, row.level)]
        existing = chosen.get(variable)
        if existing is None or score < existing[0]:
            chosen[variable] = (score, row)

    ordered_rows = sorted(rows, key=lambda item: item.offset)
    next_offsets = {row.offset: ordered_rows[index + 1].offset for index, row in enumerate(ordered_rows[:-1])}
    selected: list[SelectedRange] = []
    for variable in VARIABLES:
        row_info = chosen.get(variable)
        if row_info is None:
            continue
        row = row_info[1]
        selected.append(
            SelectedRange(
                variable=variable,
                start=row.offset,
                end=next_offsets.get(row.offset, object_size) - 1,
                raw=row.raw,
            )
        )
    return selected


class GfsIngestor:
    def __init__(self, *, config: ServiceConfig, store: ForecastStore) -> None:
        self.config = config
        self.store = store
        self.s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    def latest_cycle_prefix(self, *, now: dt.datetime | None = None) -> str | None:
        current = now or dt.datetime.now(dt.UTC)
        for days_back in range(0, 4):
            date_text = (current - dt.timedelta(days=days_back)).strftime("%Y%m%d")
            for cycle_hour in ("18", "12", "06", "00"):
                prefix = "gfs.%s/%s/atmos/" % (date_text, cycle_hour)
                key = "%sgfs.t%sz.pgrb2.0p25.f000.idx" % (prefix, cycle_hour)
                try:
                    self.s3.head_object(Bucket=self.config.bucket, Key=key)
                    return prefix
                except Exception as exc:
                    LOGGER.debug("GFS cycle probe failed for %s: %s", key, exc)
        return None

    def ingest_latest(self) -> dict[str, Any]:
        if self.config.fixture_mode:
            return self.ingest_fixture()
        prefix = self.latest_cycle_prefix()
        if prefix is None:
            raise RuntimeError("No published GFS cycle found in NOAA S3 bucket")
        return self.ingest_cycle(prefix)

    def ingest_fixture(self) -> dict[str, Any]:
        started = time.monotonic()
        base_time = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        cycle_id = "fixture-%s" % base_time.strftime("%Y%m%d%H")
        hours = target_forecast_hours(self.config.forecast_max_hour)
        latitudes = np.linspace(90.0, -90.0, 37, dtype="f4")
        longitudes = np.linspace(0.0, 355.0, 72, dtype="f4")
        yy, xx = np.meshgrid(latitudes, longitudes, indexing="ij")
        values: dict[str, np.ndarray] = {}
        shape = (len(hours), len(latitudes), len(longitudes))
        for variable in VARIABLES:
            values[variable] = np.empty(shape, dtype="f4")
        for index, hour in enumerate(hours):
            diurnal = np.sin((hour % 24) / 24.0 * 2.0 * np.pi)
            wave = np.cos(np.radians(xx - 270.0)) * 2.0
            temp_c = 14.0 + (45.0 - np.abs(yy)) * 0.25 + diurnal * 4.0 + wave
            values["t2m"][index] = temp_c + 273.15
            values["d2m"][index] = temp_c + 269.15
            values["r2"][index] = np.clip(68.0 + np.sin(np.radians(xx + hour)) * 18.0, 25.0, 98.0)
            values["u10"][index] = 4.0 + np.sin(np.radians(yy + hour))
            values["v10"][index] = 2.5 + np.cos(np.radians(xx - hour))
            values["tcc"][index] = np.clip(35.0 + 30.0 * np.sin(np.radians(xx + hour * 3)), 0.0, 100.0)
            values["prate"][index] = np.maximum(0.0, (values["tcc"][index] - 70.0) / 1000000.0)
            values["tp"][index] = np.maximum(0.0, (values["tcc"][index] - 68.0) / 18.0)
            values["prmsl"][index] = 101325.0 + np.cos(np.radians(yy)) * 600.0
            values["gust"][index] = np.hypot(values["u10"][index], values["v10"][index]) + 2.5
            values["vis"][index] = 18000.0 - values["tcc"][index] * 65.0
            values["sdswrf"][index] = np.maximum(0.0, 820.0 * np.cos(np.radians(yy)) * max(0.0, diurnal + 0.45))
        metadata = CycleMetadata(
            cycle_id=cycle_id,
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            source="fixture",
            variable_mapping=dict(GRIB_TO_ZARR),
        )
        self.store.write_cycle_from_arrays(
            metadata=metadata,
            latitudes=latitudes,
            longitudes=longitudes,
            values=values,
        )
        return {
            "cycle_id": cycle_id,
            "source": "fixture",
            "duration_seconds": round(time.monotonic() - started, 3),
            "forecast_hours": len(hours),
        }

    def ingest_cycle(self, prefix: str) -> dict[str, Any]:
        started = time.monotonic()
        cycle_hour = prefix.rstrip("/").split("/")[-2]
        date_text = prefix.split("/")[0].replace("gfs.", "")
        base_time = dt.datetime.strptime(date_text + cycle_hour, "%Y%m%d%H").replace(tzinfo=dt.UTC)
        cycle_id = "%s%s" % (date_text, cycle_hour)
        hours = target_forecast_hours(self.config.forecast_max_hour)
        source_hours = source_forecast_hours(self.config.forecast_max_hour, self.config.forecast_source_step_hours)

        first_values, latitudes, longitudes = self._read_forecast_hour(prefix=prefix, cycle_hour=cycle_hour, hour=0)
        metadata = CycleMetadata(
            cycle_id=cycle_id,
            base_time=base_time.isoformat(),
            forecast_hours=hours,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            source="noaa-gfs",
            variable_mapping=dict(GRIB_TO_ZARR),
        )
        writer = self.store.begin_cycle(metadata=metadata, latitudes=latitudes, longitudes=longitudes)
        writer.write_hour(0, first_values)
        for hour in source_hours:
            if hour == 0:
                continue
            values, _, _ = self._read_forecast_hour(prefix=prefix, cycle_hour=cycle_hour, hour=hour)
            writer.write_hour(hour, values)
        writer.interpolate_missing_hours(max_hour=min(72, self.config.forecast_max_hour))
        writer.commit()
        return {
            "cycle_id": cycle_id,
            "source": "noaa-gfs",
            "duration_seconds": round(time.monotonic() - started, 3),
            "forecast_hours": len(hours),
        }

    def _read_forecast_hour(
        self,
        *,
        prefix: str,
        cycle_hour: str,
        hour: int,
    ) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
        key = "%sgfs.t%sz.pgrb2.0p25.f%03d" % (prefix, cycle_hour, hour)
        idx_key = "%s.idx" % key
        idx_body = self.s3.get_object(Bucket=self.config.bucket, Key=idx_key)["Body"].read().decode("utf-8")
        rows = parse_idx(idx_body)
        head = self.s3.head_object(Bucket=self.config.bucket, Key=key)
        selected = select_variable_ranges(rows, object_size=int(head["ContentLength"]))
        if not selected:
            raise RuntimeError("No target weather variables found in %s" % idx_key)

        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.config.tmp_dir, suffix=".grib2", delete=False) as handle:
            subset_path = Path(handle.name)
            for item in selected:
                end = "" if item.end is None else str(item.end)
                body = self.s3.get_object(
                    Bucket=self.config.bucket,
                    Key=key,
                    Range="bytes=%d-%s" % (item.start, end),
                )["Body"].read()
                handle.write(body)

        try:
            return self._read_cfgrib_subset(subset_path)
        finally:
            try:
                subset_path.unlink()
            except OSError as exc:
                LOGGER.debug("Failed to remove temporary GRIB subset %s: %s", subset_path, exc)

    def _read_cfgrib_subset(self, subset_path: Path) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
        datasets = cfgrib.open_datasets(str(subset_path), backend_kwargs={"indexpath": ""})
        values: dict[str, np.ndarray] = {}
        latitudes: np.ndarray | None = None
        longitudes: np.ndarray | None = None
        for dataset in datasets:
            if latitudes is None and "latitude" in dataset.coords:
                latitudes = np.asarray(dataset.coords["latitude"].values, dtype="f4")
            if longitudes is None and "longitude" in dataset.coords:
                longitudes = np.asarray(dataset.coords["longitude"].values, dtype="f4")
            for variable, cfgrib_name in CFGRIB_VARIABLES.items():
                if cfgrib_name in dataset.data_vars and variable not in values:
                    array = np.asarray(dataset[cfgrib_name].values, dtype="f4")
                    if array.ndim == 2:
                        values[variable] = array
        if latitudes is None or longitudes is None:
            raise RuntimeError("cfgrib did not expose latitude/longitude coordinates")
        for variable in VARIABLES:
            if variable not in values:
                values[variable] = np.full((len(latitudes), len(longitudes)), np.nan, dtype="f4")
        return values, latitudes, longitudes


def describe_mapping_from_idx(rows: Iterable[IndexRow]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in select_variable_ranges(list(rows), object_size=0):
        mapping[item.variable] = item.raw
    return mapping
