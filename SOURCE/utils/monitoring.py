"""
Monitoring and observability utilities
Provides metrics collection, health checks, and performance monitoring.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Literal, ParamSpec, TypeVar

from core.logging_config import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class MetricType(Enum):
    """Metric types"""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


@dataclass
class Metric:
    """A single metric"""

    name: str
    type: MetricType
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        return {
            "name": self.name,
            "type": self.type.value,
            "value": self.value,
            "labels": self.labels,
            "timestamp": self.timestamp,
        }


class MetricsCollector:
    """
    Collects application metrics

    Usage:
        collector = MetricsCollector()
        collector.increment("songs_played", labels={"source": "youtube"})
        collector.gauge("queue_size", 5)

        with collector.timer("resolution_time"):
            # ... do work ...
    """

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def increment(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        """Increment a counter"""
        with self._lock:
            key = self._make_key(name, labels)
            self._counters[key] += value
            self._metrics[key] = Metric(
                name=name,
                type=MetricType.COUNTER,
                value=self._counters[key],
                labels=labels or {},
            )

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge value"""
        with self._lock:
            key = self._make_key(name, labels)
            self._gauges[key] = value
            self._metrics[key] = Metric(name=name, type=MetricType.GAUGE, value=value, labels=labels or {})

    def histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram value"""
        with self._lock:
            key = self._make_key(name, labels)
            self._histograms[key].append(value)

            # Keep last 1000 values
            if len(self._histograms[key]) > 1000:
                self._histograms[key] = self._histograms[key][-1000:]

            # Store avg as metric
            avg = sum(self._histograms[key]) / len(self._histograms[key])
            self._metrics[key] = Metric(name=name, type=MetricType.HISTOGRAM, value=avg, labels=labels or {})

    def timer(self, name: str, labels: dict[str, str] | None = None) -> TimerContext:
        """
        Context manager for timing operations

        Usage:
            with collector.timer("operation_name"):
                # ... do work ...
        """
        return TimerContext(self, name, labels)

    def get_metric(self, name: str, labels: dict[str, str] | None = None) -> Metric | None:
        """Get a specific metric"""
        key = self._make_key(name, labels)
        with self._lock:
            return self._metrics.get(key)

    def get_all_metrics(self) -> dict[str, Metric]:
        """Get all metrics"""
        with self._lock:
            return dict(self._metrics)

    def to_prometheus_format(self) -> str:
        """Export metrics in Prometheus format"""
        lines: list[str] = []

        with self._lock:
            for metric in self._metrics.values():
                # Metric help/type
                lines.append(f"# HELP {metric.name} {metric.name}")
                lines.append(f"# TYPE {metric.name} {metric.type.value}")

                # Metric value with labels
                if metric.labels:
                    labels_str = ",".join(f'{k}="{v}"' for k, v in metric.labels.items())
                    lines.append(f"{metric.name}{{{labels_str}}} {metric.value}")
                else:
                    lines.append(f"{metric.name} {metric.value}")

        return "\n".join(lines)

    def to_json(self) -> str:
        """Export metrics as JSON"""
        with self._lock:
            metrics = [m.to_dict() for m in self._metrics.values()]
            return json.dumps(metrics, indent=2)

    def reset(self) -> None:
        """Reset all metrics"""
        with self._lock:
            self._metrics.clear()
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    def _make_key(self, name: str, labels: dict[str, str] | None) -> str:
        """Create unique key for metric"""
        if not labels:
            return name
        labels_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{labels_str}}}"


class TimerContext:
    """Context manager for timing operations"""

    def __init__(
        self,
        collector: MetricsCollector,
        name: str,
        labels: dict[str, str] | None = None,
    ):
        self.collector = collector
        self.name = name
        self.labels = labels
        self.start_time: float | None = None

    def __enter__(self) -> TimerContext:
        self.start_time = time.time()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any | None,
    ) -> Literal[False]:
        if self.start_time is None:
            return False
        duration = time.time() - self.start_time
        self.collector.histogram(f"{self.name}_duration_seconds", duration, self.labels)
        return False


class HealthChecker:
    """
    Health check system

    Usage:
        health = HealthChecker()
        health.register_check("database", check_db_connection)
        health.register_check("api", check_api_availability)

        status = health.check_all()
    """

    def __init__(self):
        self._checks: dict[str, Callable[[], bool]] = {}
        self._results: dict[str, tuple[bool, str]] = {}
        self._lock = threading.Lock()

    def register_check(self, name: str, check_fn: Callable[[], bool]) -> None:
        """Register a health check"""
        with self._lock:
            self._checks[name] = check_fn

    def check_all(self) -> dict[str, Any]:
        """Run all health checks"""
        results: dict[str, dict[str, Any]] = {}
        all_healthy = True

        with self._lock:
            for name, check_fn in self._checks.items():
                try:
                    is_healthy = check_fn()
                    results[name] = {
                        "status": "healthy" if is_healthy else "unhealthy",
                        "message": "OK" if is_healthy else "Check failed",
                    }
                    if not is_healthy:
                        all_healthy = False
                except Exception as e:
                    results[name] = {"status": "error", "message": str(e)}
                    all_healthy = False

        return {
            "status": "healthy" if all_healthy else "unhealthy",
            "checks": results,
            "timestamp": time.time(),
        }

    def check_one(self, name: str) -> dict[str, Any]:
        """Run a specific health check"""
        with self._lock:
            if name not in self._checks:
                return {"status": "error", "message": f"Check '{name}' not found"}

            try:
                is_healthy = self._checks[name]()
                return {
                    "status": "healthy" if is_healthy else "unhealthy",
                    "message": "OK" if is_healthy else "Check failed",
                }
            except Exception as e:
                logger.debug("Health check '%s' failed: %s", name, e, exc_info=True)
                return {"status": "error", "message": str(e)}


# Global metrics collector instance
_metrics_collector: MetricsCollector | None = None
_health_checker: HealthChecker | None = None


def get_metrics_collector() -> MetricsCollector:
    """Get global metrics collector"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


def get_health_checker() -> HealthChecker:
    """Get global health checker"""
    global _health_checker
    if _health_checker is None:
        _health_checker = HealthChecker()
    return _health_checker


# Convenience decorators
def track_calls(
    name: str | None = None, labels: dict[str, str] | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator to track function calls

    Usage:
        @track_calls("play_song", labels={"source": "youtube"})
        def play_song(song):
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        metric_name = name or f"{func.__module__}.{func.__name__}_calls"

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            collector = get_metrics_collector()
            collector.increment(metric_name, labels=labels)

            with collector.timer(f"{metric_name}_duration", labels=labels):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def track_errors(
    name: str | None = None, labels: dict[str, str] | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator to track function errors

    Usage:
        @track_errors("play_song_errors")
        def play_song(song):
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        metric_name = name or f"{func.__module__}.{func.__name__}_errors"

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                collector = get_metrics_collector()
                error_labels = {**(labels or {}), "error_type": type(e).__name__}
                collector.increment(metric_name, labels=error_labels)
                raise

        return wrapper

    return decorator
