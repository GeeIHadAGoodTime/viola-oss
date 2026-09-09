from __future__ import annotations

import datetime as dt
import time
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from weather_gfs.config import load_config
from weather_gfs.forecast import build_forecast_payload
from weather_gfs.ingestion import GfsIngestor
from weather_gfs.scheduler import IngestionScheduler, Metrics
from weather_gfs.storage import ForecastStore

app = FastAPI(title="Viola NOAA GFS Weather", version="0.1.0")
config = load_config()
store = ForecastStore(config.data_dir, keep_cycles=config.keep_cycles)
metrics = Metrics()
ingestor = GfsIngestor(config=config, store=store)
scheduler = IngestionScheduler(config=config, ingestor=ingestor, metrics=metrics)
_forecast_cache: OrderedDict[tuple[float, float], tuple[float, dict[str, Any]]] = OrderedDict()


@app.on_event("startup")
async def _startup() -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    await scheduler.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop()


def _cache_get(key: tuple[float, float]) -> dict[str, Any] | None:
    item = _forecast_cache.get(key)
    if item is None:
        metrics.cache_misses += 1
        return None
    expires_at, payload = item
    if expires_at <= time.time():
        _forecast_cache.pop(key, None)
        metrics.cache_misses += 1
        return None
    _forecast_cache.move_to_end(key)
    metrics.cache_hits += 1
    cached = dict(payload)
    cached["cached"] = True
    return cached


def _cache_set(key: tuple[float, float], payload: dict[str, Any]) -> None:
    _forecast_cache[key] = (time.time() + config.cache_ttl_seconds, payload)
    _forecast_cache.move_to_end(key)
    while len(_forecast_cache) > 4096:
        _forecast_cache.popitem(last=False)


@app.get("/health")
async def health() -> dict[str, Any]:
    snapshot = store.health()
    return {
        "status": snapshot.status,
        "last_ingestion_at": snapshot.last_ingestion_at,
        "cycle_age_hours": snapshot.cycle_age_hours,
        "ready": snapshot.ready,
    }


@app.get("/metrics")
async def metrics_endpoint() -> dict[str, Any]:
    payload = metrics.as_dict()
    payload["cache_entries"] = len(_forecast_cache)
    payload["reported_at"] = dt.datetime.now(dt.UTC).isoformat()
    return payload


@app.post("/ingest")
async def ingest_now() -> dict[str, Any]:
    await scheduler.run_once()
    return {"ok": True, "metrics": metrics.as_dict()}


@app.get("/forecast")
async def forecast(
    lat: float = Query(..., ge=-90.0, le=90.0),
    lon: float = Query(..., ge=-180.0, le=180.0),
) -> dict[str, Any]:
    if not store.health().ready:
        raise HTTPException(status_code=503, detail="GFS store is not ready")
    cache_key = (round(lat, 1), round(lon, 1))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    payload = build_forecast_payload(store=store, latitude=lat, longitude=lon)
    _cache_set(cache_key, payload)
    return payload
