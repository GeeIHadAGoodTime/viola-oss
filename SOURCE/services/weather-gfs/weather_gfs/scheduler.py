from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from weather_gfs.config import ServiceConfig
from weather_gfs.ingestion import GfsIngestor

LOGGER = logging.getLogger(__name__)


@dataclass
class Metrics:
    cache_hits: int = 0
    cache_misses: int = 0
    last_ingestion_duration_seconds: float | None = None
    ingestion_success_count: int = 0
    ingestion_error_count: int = 0
    last_error: str | None = None

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        if total <= 0:
            return 0.0
        return round(self.cache_hits / total, 4)

    def as_dict(self) -> dict[str, float | int | str | None]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": self.cache_hit_rate,
            "last_ingestion_duration_seconds": self.last_ingestion_duration_seconds,
            "ingestion_success_count": self.ingestion_success_count,
            "ingestion_error_count": self.ingestion_error_count,
            "last_error": self.last_error,
        }


class IngestionScheduler:
    def __init__(self, *, config: ServiceConfig, ingestor: GfsIngestor, metrics: Metrics) -> None:
        self.config = config
        self.ingestor = ingestor
        self.metrics = metrics
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self.config.fixture_mode or self.config.fixture_bootstrap:
            await self.run_once(fixture=True)
        elif self.config.ingest_on_start:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_once(self, *, fixture: bool = False) -> None:
        started = asyncio.get_running_loop().time()
        try:
            if fixture:
                await asyncio.to_thread(self.ingestor.ingest_fixture)
            else:
                await asyncio.to_thread(self.ingestor.ingest_latest)
            self.metrics.ingestion_success_count += 1
            self.metrics.last_error = None
        except Exception as exc:
            self.metrics.ingestion_error_count += 1
            self.metrics.last_error = str(exc)
            LOGGER.exception("GFS ingestion failed")
        finally:
            self.metrics.last_ingestion_duration_seconds = round(asyncio.get_running_loop().time() - started, 3)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(60.0, self.config.ingestion_interval_hours * 3600.0),
                )
            except TimeoutError:
                continue
