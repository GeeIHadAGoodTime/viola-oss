from __future__ import annotations

import statistics
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Protocol, TypedDict

from core.logging_config import get_logger

logger = get_logger(__name__)


class _CounterChild(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...


class _HistogramChild(Protocol):
    def observe(self, amount: float) -> None: ...


class _CounterMetric(Protocol):
    def labels(self, **labels: str) -> _CounterChild: ...


class _HistogramMetric(Protocol):
    def labels(self, **labels: str) -> _HistogramChild: ...


class _PrometheusHandles(TypedDict, total=False):
    resolution_total: _CounterMetric
    resolution_latency: _HistogramMetric
    playback_total: _CounterMetric
    gap_duration: _HistogramMetric
    errors: _CounterMetric


@dataclass
class TelemetrySnapshot:
    """Snapshot of provider telemetry suitable for SLA evaluation."""

    provider: str
    resolution_count: int
    resolution_failures: int
    mean_resolution_ms: float | None
    p95_resolution_ms: float | None
    playback_count: int
    playback_failures: int
    mean_gap_ms: float | None
    error_counts: dict[str, int]

    @property
    def failure_rate(self) -> float:
        total = self.resolution_count or 0
        return (self.resolution_failures / total) if total else 0.0


@dataclass
class _ProviderMetrics:
    resolution_latencies: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    resolution_failures: int = 0
    resolution_total: int = 0
    playback_gaps: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    playback_failures: int = 0
    playback_total: int = 0
    error_types: Counter[str] = field(default_factory=lambda: Counter[str]())


class ProviderTelemetryRegistry:
    """
    Aggregate telemetry per provider with optional Prometheus export.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, _ProviderMetrics] = {}
        self._lock = threading.Lock()
        self._prometheus: _PrometheusHandles = self._build_prometheus_handles()

    # ------------------------------------------------------------------ #
    # Recording helpers
    # ------------------------------------------------------------------ #
    def record_resolution(self, provider: str, duration_ms: float, success: bool, source: str | None) -> None:
        normalized = provider.lower() if provider else "unknown"
        with self._lock:
            metrics = self._metrics.setdefault(normalized, _ProviderMetrics())
            metrics.resolution_total += 1
            metrics.resolution_latencies.append(duration_ms)
            if not success:
                metrics.resolution_failures += 1
        self._record_prometheus_resolution(normalized, duration_ms, success, source)

    def record_playback(self, provider: str, gap_ms: float, success: bool) -> None:
        normalized = provider.lower() if provider else "unknown"
        with self._lock:
            metrics = self._metrics.setdefault(normalized, _ProviderMetrics())
            metrics.playback_total += 1
            metrics.playback_gaps.append(gap_ms)
            if not success:
                metrics.playback_failures += 1
        self._record_prometheus_playback(normalized, gap_ms, success)

    def record_error(self, provider: str, error_type: str) -> None:
        normalized = provider.lower() if provider else "unknown"
        with self._lock:
            metrics = self._metrics.setdefault(normalized, _ProviderMetrics())
            metrics.error_types[error_type] += 1
        errors = self._prometheus.get("errors")
        if errors is not None:
            errors.labels(provider=normalized, error_type=error_type).inc()

    # ------------------------------------------------------------------ #
    # Snapshot helpers
    # ------------------------------------------------------------------ #
    def snapshot(self, provider: str) -> TelemetrySnapshot:
        normalized = provider.lower() if provider else "unknown"
        with self._lock:
            metrics = self._metrics.setdefault(normalized, _ProviderMetrics())
            latencies = list(metrics.resolution_latencies)
            gaps = list(metrics.playback_gaps)
            return TelemetrySnapshot(
                provider=normalized,
                resolution_count=metrics.resolution_total,
                resolution_failures=metrics.resolution_failures,
                mean_resolution_ms=self._safe_mean(latencies),
                p95_resolution_ms=self._percentile(latencies, 95),
                playback_count=metrics.playback_total,
                playback_failures=metrics.playback_failures,
                mean_gap_ms=self._safe_mean(gaps),
                error_counts=dict(metrics.error_types),
            )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_mean(values: list[float]) -> float | None:
        if not values:
            return None
        try:
            return round(statistics.mean(values), 2)
        except statistics.StatisticsError:
            return None

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float | None:
        if not values:
            return None
        if len(values) == 1:
            return round(values[0], 2)
        try:
            sorted_values = sorted(values)
            index = round((percentile / 100) * (len(sorted_values) - 1))
            return round(sorted_values[index], 2)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("Percentile calculation failed: %s", e)
            return None

    # ------------------------------------------------------------------ #
    # Prometheus integration
    # ------------------------------------------------------------------ #
    def _build_prometheus_handles(self) -> _PrometheusHandles:
        try:  # pragma: no cover - optional dependency
            from prometheus_client import Counter as PromCounter, Histogram
        except ImportError:  # pragma: no cover
            return {}

        try:
            return {
                "resolution_total": PromCounter(
                    "music_resolution_requests_total",
                    "Resolution attempts per provider",
                    ["provider", "status", "source"],
                ),
                "resolution_latency": Histogram(
                    "music_resolution_latency_ms",
                    "Resolution latency in ms",
                    ["provider"],
                    buckets=(50, 100, 200, 400, 800, 1600, 3200),
                ),
                "playback_total": PromCounter(
                    "music_playback_events_total",
                    "Playback outcomes per provider",
                    ["provider", "status"],
                ),
                "gap_duration": Histogram(
                    "music_playback_gap_ms",
                    "Gapless playback metric in ms",
                    ["provider"],
                    buckets=(0, 10, 25, 50, 100, 200, 500),
                ),
                "errors": PromCounter(
                    "music_provider_errors_total",
                    "Errors per provider and type",
                    ["provider", "error_type"],
                ),
            }
        except ValueError:
            logger.debug("Prometheus metrics already registered; skipping re-registration")
            return {}
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to initialize Prometheus metrics: %s", exc)
            return {}

    def _record_prometheus_resolution(
        self, provider: str, duration_ms: float, success: bool, source: str | None
    ) -> None:
        if not self._prometheus or "resolution_total" not in self._prometheus:
            return
        status = "success" if success else "failure"
        self._prometheus["resolution_total"].labels(provider=provider, status=status, source=source or "unknown").inc()
        self._prometheus["resolution_latency"].labels(provider=provider).observe(duration_ms / 1000)

    def _record_prometheus_playback(self, provider: str, gap_ms: float, success: bool) -> None:
        if not self._prometheus or "playback_total" not in self._prometheus:
            return
        status = "success" if success else "failure"
        self._prometheus["playback_total"].labels(provider=provider, status=status).inc()
        self._prometheus["gap_duration"].labels(provider=provider).observe(gap_ms / 1000)
