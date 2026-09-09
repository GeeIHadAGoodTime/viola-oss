from __future__ import annotations

import datetime as dt
import json
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import zarr

from weather_gfs.interp import bilinear_series

VARIABLES: tuple[str, ...] = (
    "t2m",
    "d2m",
    "r2",
    "u10",
    "v10",
    "tcc",
    "prate",
    "tp",
    "prmsl",
    "gust",
    "vis",
    "sdswrf",
)


@dataclass(frozen=True)
class CycleMetadata:
    cycle_id: str
    base_time: str
    forecast_hours: list[int]
    created_at: str
    source: str
    variable_mapping: dict[str, str]


@dataclass(frozen=True)
class HealthSnapshot:
    status: str
    last_ingestion_at: str | None
    cycle_age_hours: float | None
    ready: bool


class CycleWriter:
    def __init__(
        self,
        *,
        store: ForecastStore,
        tmp_path: Path,
        metadata: CycleMetadata,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
    ) -> None:
        self._store = store
        self.tmp_path = tmp_path
        self.metadata = metadata
        self.forecast_hours = metadata.forecast_hours
        self._hour_index = {hour: idx for idx, hour in enumerate(self.forecast_hours)}
        self.group = zarr.open_group(str(tmp_path / "data.zarr"), mode="w")
        self.group.create_array("latitudes", data=latitudes.astype("f4"))
        self.group.create_array("longitudes", data=longitudes.astype("f4"))
        shape = (len(self.forecast_hours), len(latitudes), len(longitudes))
        chunks = (1, min(181, len(latitudes)), min(360, len(longitudes)))
        for variable in VARIABLES:
            self.group.create_array(variable, shape=shape, chunks=chunks, dtype="f4", fill_value=np.nan)
        (tmp_path / "metadata.json").write_text(json.dumps(asdict(metadata), indent=2), encoding="utf-8")

    def write_hour(self, forecast_hour: int, values: Mapping[str, np.ndarray]) -> None:
        index = self._hour_index.get(forecast_hour)
        if index is None:
            return
        for variable in VARIABLES:
            value = values.get(variable)
            if value is not None:
                self.group[variable][index, :, :] = value.astype("f4")

    def interpolate_missing_hours(self, *, max_hour: int = 72) -> None:
        for hour in self.forecast_hours:
            if hour > max_hour or hour % 3 == 0:
                continue
            lower = hour - hour % 3
            upper = lower + 3
            if lower not in self._hour_index or upper not in self._hour_index:
                continue
            target_index = self._hour_index[hour]
            lower_index = self._hour_index[lower]
            upper_index = self._hour_index[upper]
            weight = (hour - lower) / 3.0
            for variable in VARIABLES:
                low = self.group[variable][lower_index, :, :]
                high = self.group[variable][upper_index, :, :]
                self.group[variable][target_index, :, :] = low * (1.0 - weight) + high * weight

    def commit(self) -> Path:
        return self._store.commit_writer(self)


class ForecastStore:
    def __init__(self, data_dir: Path, *, keep_cycles: int = 2) -> None:
        self.data_dir = data_dir
        self.keep_cycles = keep_cycles
        self.cycles_dir = self.data_dir / "cycles"
        self.latest_file = self.data_dir / "latest.json"
        self.cycles_dir.mkdir(parents=True, exist_ok=True)

    def begin_cycle(
        self,
        *,
        metadata: CycleMetadata,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
    ) -> CycleWriter:
        tmp_path = self.cycles_dir / ("%s.tmp.%s" % (metadata.cycle_id, uuid.uuid4().hex))
        tmp_path.mkdir(parents=True, exist_ok=False)
        return CycleWriter(
            store=self,
            tmp_path=tmp_path,
            metadata=metadata,
            latitudes=latitudes,
            longitudes=longitudes,
        )

    def commit_writer(self, writer: CycleWriter) -> Path:
        final_path = self.cycles_dir / writer.metadata.cycle_id
        if final_path.exists():
            shutil.rmtree(final_path)
        writer.tmp_path.replace(final_path)
        tmp_latest = self.data_dir / ("latest.%s.json" % uuid.uuid4().hex)
        tmp_latest.write_text(
            json.dumps(
                {
                    "cycle_id": writer.metadata.cycle_id,
                    "last_ingestion_at": dt.datetime.now(dt.UTC).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp_latest.replace(self.latest_file)
        self.prune_old_cycles()
        return final_path

    def prune_old_cycles(self) -> None:
        cycle_paths = [
            path
            for path in self.cycles_dir.iterdir()
            if path.is_dir() and ".tmp." not in path.name and (path / "metadata.json").exists()
        ]
        cycle_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for old_path in cycle_paths[self.keep_cycles :]:
            shutil.rmtree(old_path)

    def latest_cycle_path(self) -> Path | None:
        if self.latest_file.exists():
            try:
                latest = json.loads(self.latest_file.read_text(encoding="utf-8"))
                path = self.cycles_dir / str(latest.get("cycle_id", ""))
                if (path / "metadata.json").exists():
                    return path
            except (OSError, json.JSONDecodeError):
                return None
        candidates = [
            path
            for path in self.cycles_dir.iterdir()
            if path.is_dir() and ".tmp." not in path.name and (path / "metadata.json").exists()
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def latest_metadata(self) -> CycleMetadata | None:
        path = self.latest_cycle_path()
        if path is None:
            return None
        try:
            raw = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return CycleMetadata(
            cycle_id=str(raw["cycle_id"]),
            base_time=str(raw["base_time"]),
            forecast_hours=[int(hour) for hour in raw["forecast_hours"]],
            created_at=str(raw["created_at"]),
            source=str(raw["source"]),
            variable_mapping=dict(raw.get("variable_mapping") or {}),
        )

    def health(self) -> HealthSnapshot:
        metadata = self.latest_metadata()
        if metadata is None:
            return HealthSnapshot(
                status="degraded",
                last_ingestion_at=None,
                cycle_age_hours=None,
                ready=False,
            )
        try:
            base_time = dt.datetime.fromisoformat(metadata.base_time)
        except ValueError:
            base_time = dt.datetime.now(dt.UTC)
        if base_time.tzinfo is None:
            base_time = base_time.replace(tzinfo=dt.UTC)
        age_hours = (dt.datetime.now(dt.UTC) - base_time.astimezone(dt.UTC)).total_seconds() / 3600.0
        last_ingestion_at = None
        if self.latest_file.exists():
            try:
                last_ingestion_at = str(
                    json.loads(self.latest_file.read_text(encoding="utf-8")).get("last_ingestion_at")
                )
            except (OSError, json.JSONDecodeError):
                last_ingestion_at = metadata.created_at
        return HealthSnapshot(
            status="ok" if age_hours <= 18.0 else "degraded",
            last_ingestion_at=last_ingestion_at or metadata.created_at,
            cycle_age_hours=round(age_hours, 2),
            ready=True,
        )

    def point_series(self, lat: float, lon: float) -> dict[str, Any]:
        path = self.latest_cycle_path()
        metadata = self.latest_metadata()
        if path is None or metadata is None:
            raise FileNotFoundError("No ingested GFS cycle is available")
        group = zarr.open_group(str(path / "data.zarr"), mode="r")
        latitudes = np.asarray(group["latitudes"][:], dtype="f8")
        longitudes = np.asarray(group["longitudes"][:], dtype="f8")
        values: dict[str, np.ndarray] = {}
        for variable in VARIABLES:
            values[variable] = bilinear_series(group[variable], latitudes, longitudes, lat, lon)
        return {
            "metadata": metadata,
            "latitudes": latitudes,
            "longitudes": longitudes,
            "values": values,
        }

    def write_cycle_from_arrays(
        self,
        *,
        metadata: CycleMetadata,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        values: Mapping[str, np.ndarray],
        fail_before_commit: bool = False,
    ) -> Path:
        writer = self.begin_cycle(metadata=metadata, latitudes=latitudes, longitudes=longitudes)
        for variable in VARIABLES:
            if variable in values:
                writer.group[variable][:, :, :] = values[variable].astype("f4")
        if fail_before_commit:
            raise RuntimeError("simulated write failure before atomic commit")
        return writer.commit()
