"""
diagnostics/runtime_metrics.py
================================

Centralised runtime metrics and heartbeat registry backing the Batch A
observability work. Threads and async tasks can record subsystem metrics in a
type-safe fashion while optionally exposing Prometheus collectors for scraping.

Key responsibilities:
    • Capture rolling latency samples (wake detection, STT, FastAPI routes)
    • Maintain gauges for resource usage, mic levels, queue depth, VLC health
    • Track per-subsystem heartbeat metadata for enriched health responses
    • Produce structured snapshots for /health/details and baseline scripts
    • Generate Prometheus /metrics payloads when prometheus_client is available
"""

from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from statistics import fmean
from typing import Protocol, cast

from core.constants import TIMEOUT_DEFAULT
from core.logging_config import get_logger
from diagnostics.bus import get_diagnostics_bus

logger = get_logger(__name__)

try:  # Optional dependency – Batch A instrumentation must degrade gracefully
    import prometheus_client as _prometheus_client

    _PROMETHEUS_AVAILABLE = True
except Exception:  # pragma: no cover - prometheus_client not installed
    _prometheus_client = None
    _PROMETHEUS_AVAILABLE = False


_MIC_LEVEL_HISTORY = 32
_DEFAULT_SERIES_SIZE = 256
_HEARTBEAT_STALE_SECONDS = 30.0

# Maximum number of distinct route/label keys in per-route metrics dicts.
# Prevents unbounded growth from path-parameter cardinality explosions
# (e.g. /users/{id} generating a unique key per user ID).
_MAX_ROUTE_KEYS = 500
_MAX_GENERAL_COUNTER_KEYS = 1000


class _GaugeLike(Protocol):
    def set(self, value: float) -> None: ...


class _CounterLike(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...

    def labels(self, **labels: str) -> _CounterLike: ...


class _HistogramLike(Protocol):
    def observe(self, value: float) -> None: ...

    def labels(self, **labels: str) -> _HistogramLike: ...


def _now_ts() -> float:
    return time.time()


def _quantiles(samples: deque[float]) -> dict[str, float | int]:
    if not samples:
        return {"count": 0}
    sorted_samples = sorted(samples)
    count = len(sorted_samples)

    def _pct(p: float) -> float:
        if count == 1:
            return sorted_samples[0]
        idx = min(count - 1, max(0, round((p / 100.0) * (count - 1))))
        return sorted_samples[idx]

    return {
        "count": count,
        "avg": fmean(sorted_samples),
        "min": sorted_samples[0],
        "p50": _pct(50.0),
        "p90": _pct(90.0),
        "p99": _pct(99.0),
        "max": sorted_samples[-1],
    }


@dataclass(slots=True)
class _Heartbeat:
    subsystem: str
    status: str
    details: dict[str, object] = field(default_factory=dict)
    ts: float = field(default_factory=_now_ts)
    sequence: int = 0


class RuntimeMetrics:
    """
    Singleton runtime metrics registry.

    Exposes helper methods to record metrics from various subsystems while
    keeping snapshots for health checks and Prometheus scraping aligned.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process_snapshot: dict[str, object] = {}
        self._wake_latency: deque[float] = deque(maxlen=_DEFAULT_SERIES_SIZE)
        self._stt_latency: deque[float] = deque(maxlen=_DEFAULT_SERIES_SIZE)
        self._fastapi_latency: dict[str, deque[float]] = {}
        self._fastapi_counters: dict[str, dict[str, int]] = {}
        self._vlc_status: dict[str, object] = {}
        self._queue_drift: dict[str, object] = {}
        self._music_stats: dict[str, object] = {
            "backend": {"restarts": 0, "reasons": {}},
            "buffer": {"underruns": 0},
            "queue": {"skew_events": 0, "last_drift": 0},
            "heartbeat": {"misses": 0, "sources": {}},
            "failures": {},
        }
        self._mic_levels: dict[str, deque[float]] = {}
        self._stt_idle_seconds: float = 0.0
        self._voice_idle_seconds: float = 0.0
        self._command_queue_depth: int = 0
        self._heartbeats: dict[str, _Heartbeat] = {}
        self._heartbeat_seq = 0

        self._sampler_thread: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._sampler_interval = 5.0

        self._prom_registry: object | None = None
        self._metrics: dict[str, object] = {}
        self._prometheus_initialized = False
        self._failures: deque[dict[str, object]] = deque(maxlen=100)
        self._restart_counter_cache: dict[str, int] = {}

    # --------------------------------------------------------------------- #
    # Prometheus initialisation (deferred to first use)
    # --------------------------------------------------------------------- #
    def _ensure_prometheus(self) -> None:
        """Lazily initialise Prometheus collectors on first use."""
        if self._prometheus_initialized:
            return
        if _PROMETHEUS_AVAILABLE:
            self._init_prometheus_collectors()
        else:
            logger.debug("Prometheus client not available; /metrics will return JSON snapshot")
        self._prometheus_initialized = True

    def _init_prometheus_collectors(self) -> None:
        if not _PROMETHEUS_AVAILABLE or _prometheus_client is None:
            return

        registry = _prometheus_client.CollectorRegistry(auto_describe=False)

        self._metrics = {
            "process_cpu": _prometheus_client.Gauge(
                "viola_process_cpu_percent",
                "CPU utilisation of Viola process",
                registry=registry,
            ),
            "process_mem": _prometheus_client.Gauge(
                "viola_process_memory_mb",
                "Resident memory usage of Viola process (MB)",
                registry=registry,
            ),
            "process_threads": _prometheus_client.Gauge(
                "viola_process_threads",
                "Number of threads in Viola process",
                registry=registry,
            ),
            "process_io_read": _prometheus_client.Gauge(
                "viola_process_io_read_bytes_total",
                "Cumulative IO read bytes",
                registry=registry,
            ),
            "process_io_write": _prometheus_client.Gauge(
                "viola_process_io_write_bytes_total",
                "Cumulative IO write bytes",
                registry=registry,
            ),
            "wake_latency": _prometheus_client.Histogram(
                "viola_wake_detection_latency_seconds",
                "Wake word detection latency",
                buckets=(0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.0, 3.0),
                registry=registry,
            ),
            "stt_latency": _prometheus_client.Histogram(
                "viola_stt_round_trip_seconds",
                "STT round-trip latency",
                buckets=(0.2, 0.4, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0),
                registry=registry,
            ),
            "fastapi_requests": _prometheus_client.Counter(
                "viola_fastapi_request_total",
                "FastAPI request total by route/method/status",
                ("route", "method", "status"),
                registry=registry,
            ),
            "fastapi_latency": _prometheus_client.Histogram(
                "viola_fastapi_request_latency_seconds",
                "FastAPI request latency",
                ("route", "method"),
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
                registry=registry,
            ),
            "vlc_health": _prometheus_client.Gauge(
                "viola_vlc_health_score",
                "VLC health score (0=down, 1=healthy)",
                registry=registry,
            ),
            "queue_drift": _prometheus_client.Gauge(
                "viola_queue_state_drift",
                "Difference between FastAPI queue and VLC now-playing",
                registry=registry,
            ),
            "music_backend_restarts": _prometheus_client.Counter(
                "viola_music_backend_restarts_total",
                "Count of self-healing music backend restarts",
                ("reason",),
                registry=registry,
            ),
            "music_buffer_underruns": _prometheus_client.Counter(
                "viola_music_buffer_underruns_total",
                "Detected buffer underruns or stalled playback events",
                registry=registry,
            ),
            "music_queue_skew": _prometheus_client.Counter(
                "viola_music_queue_skew_total",
                "Queue integrity corrections applied",
                ("direction",),
                registry=registry,
            ),
            "music_heartbeat_miss": _prometheus_client.Counter(
                "viola_music_heartbeat_miss_total",
                "Missed music subsystem heartbeats",
                ("source",),
                registry=registry,
            ),
            "music_queue_failures": _prometheus_client.Counter(
                "viola_music_queue_failures_total",
                "Queue failure events by stage",
                ("stage",),
                registry=registry,
            ),
            "command_queue_depth": _prometheus_client.Gauge(
                "viola_command_queue_depth",
                "Depth of voice command queue",
                registry=registry,
            ),
            "stt_idle": _prometheus_client.Gauge(
                "viola_stt_idle_seconds",
                "Seconds since last STT activity",
                registry=registry,
            ),
            "voice_idle": _prometheus_client.Gauge(
                "viola_voice_idle_seconds",
                "Seconds since last voice activity",
                registry=registry,
            ),
            "restart_counters": _prometheus_client.Counter(
                "viola_restart_counters_total",
                "Monotonic restart counter per monitored component",
                ("component",),
                registry=registry,
            ),
            "failures_total": _prometheus_client.Counter(
                "viola_failures_total",
                "Total failure envelopes emitted",
                ("code", "component"),
                registry=registry,
            ),
        }
        self._prom_registry = registry

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def record_process_usage(self) -> None:
        """
        Capture a snapshot of the Viola process resource usage.

        Safe to call from background threads; errors are logged and ignored.
        """
        try:
            import psutil  # Local import to avoid optional dependency at module load

            process = psutil.Process()
            with self._lock:
                cpu_percent = float(process.cpu_percent(interval=None))
                mem_info = process.memory_info()
                io_counters = process.io_counters() if hasattr(process, "io_counters") else None
                thread_count = int(process.num_threads())
                memory_mb = float(mem_info.rss / (1024 * 1024))
                io_read_bytes = float(getattr(io_counters, "read_bytes", 0))
                io_write_bytes = float(getattr(io_counters, "write_bytes", 0))
                snapshot: dict[str, object] = {
                    "timestamp": _now_ts(),
                    "cpu_percent": cpu_percent,
                    "memory_mb": memory_mb,
                    "threads": thread_count,
                    "io_read_bytes": io_read_bytes,
                    "io_write_bytes": io_write_bytes,
                }
                self._process_snapshot = snapshot
                if self._metrics:
                    cast(_GaugeLike, self._metrics["process_cpu"]).set(cpu_percent)
                    cast(_GaugeLike, self._metrics["process_mem"]).set(memory_mb)
                    cast(_GaugeLike, self._metrics["process_threads"]).set(float(thread_count))
                    cast(_GaugeLike, self._metrics["process_io_read"]).set(io_read_bytes)
                    cast(_GaugeLike, self._metrics["process_io_write"]).set(io_write_bytes)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.debug("Failed to capture process usage: %s", exc)

    def start_resource_sampler(self, interval: float = 5.0) -> None:
        """
        Start a background sampler that periodically updates process usage metrics.

        Idempotent – repeated calls refresh the interval without spawning multiple
        threads.
        """
        self._ensure_prometheus()
        interval = max(1.0, float(interval))
        with self._lock:
            self._sampler_interval = interval
            if self._sampler_thread and self._sampler_thread.is_alive():
                return
            self._sampler_stop.clear()
            thread = threading.Thread(
                target=self._sampler_loop,
                name="runtime-metrics-sampler",
                daemon=True,
            )
            thread.start()
            self._sampler_thread = thread
            logger.debug("RuntimeMetrics sampler started (interval=%ss)", interval)

    def stop_resource_sampler(self) -> None:
        with self._lock:
            if not self._sampler_thread:
                return
            self._sampler_stop.set()
            thread = self._sampler_thread
            self._sampler_thread = None
        thread.join(timeout=TIMEOUT_DEFAULT)

    def _sampler_loop(self) -> None:
        try:
            while not self._sampler_stop.is_set():
                self.record_process_usage()
                if self._sampler_stop.wait(self._sampler_interval):
                    break
        except Exception as exc:  # pragma: no cover - defensive loop guard
            logger.debug("RuntimeMetrics sampler loop terminated unexpectedly: %s", exc)

    def record_wake_latency(self, latency_s: float, *, source: str = "wake") -> None:
        latency_s = max(0.0, float(latency_s))
        with self._lock:
            self._wake_latency.append(latency_s)
            get_diagnostics_bus().emit(
                f"wake.latency.{source}",
                severity="INFO",
                message="Wake detection latency recorded",
                latency_s=latency_s,
            )
            if self._metrics:
                cast(_HistogramLike, self._metrics["wake_latency"]).observe(latency_s)

    def record_stt_round_trip(self, latency_s: float, *, provider: str = "default") -> None:
        latency_s = max(0.0, float(latency_s))
        with self._lock:
            self._stt_latency.append(latency_s)
            get_diagnostics_bus().emit(
                f"stt.latency.{provider}",
                severity="INFO",
                message="STT round trip recorded",
                latency_s=latency_s,
            )
            if self._metrics:
                cast(_HistogramLike, self._metrics["stt_latency"]).observe(latency_s)

    def record_fastapi_request(
        self,
        route: str,
        method: str,
        status_code: int,
        latency_s: float,
    ) -> None:
        norm_route = route or "unknown"
        method = method.upper()
        key = f"{method} {norm_route}"
        latency_s = max(0.0, float(latency_s))
        with self._lock:
            # Enforce max keys to prevent cardinality explosion from
            # path-parameter routes (e.g. /users/{id}).
            if key not in self._fastapi_latency and len(self._fastapi_latency) >= _MAX_ROUTE_KEYS:
                return  # Silently drop to protect memory
            series = self._fastapi_latency.setdefault(key, deque(maxlen=_DEFAULT_SERIES_SIZE))
            series.append(latency_s)
            counters = self._fastapi_counters.setdefault(key, {})
            counters[str(status_code)] = counters.get(str(status_code), 0) + 1
            if self._metrics:
                labels = {"route": norm_route, "method": method}
                cast(_HistogramLike, self._metrics["fastapi_latency"]).labels(**labels).observe(latency_s)
                cast(_CounterLike, self._metrics["fastapi_requests"]).labels(
                    route=norm_route,
                    method=method,
                    status=str(status_code),
                ).inc()

    def record_vlc_status(
        self,
        *,
        healthy: bool,
        track: str | None = None,
        position_seconds: float | None = None,
        queue_position: int | None = None,
    ) -> None:
        with self._lock:
            self._vlc_status = {
                "timestamp": _now_ts(),
                "healthy": bool(healthy),
                "track": track,
                "position_seconds": position_seconds,
                "queue_position": queue_position,
            }
            if self._metrics:
                cast(_GaugeLike, self._metrics["vlc_health"]).set(1.0 if healthy else 0.0)

    def record_queue_drift(
        self,
        *,
        expected_queue_len: int,
        actual_queue_len: int,
        now_playing_track_id: str | None = None,
    ) -> None:
        drift = actual_queue_len - expected_queue_len
        with self._lock:
            self._queue_drift = {
                "timestamp": _now_ts(),
                "expected_queue_len": expected_queue_len,
                "actual_queue_len": actual_queue_len,
                "drift": drift,
                "now_playing_track_id": now_playing_track_id,
            }
            queue_stats = self._music_stats.setdefault("queue", {"skew_events": 0, "last_drift": 0})
            if isinstance(queue_stats, dict):
                queue_stats["last_drift"] = drift
        if drift != 0:
            self.record_music_queue_skew(drift=drift, increment_metrics=False)
            gauge = self._metrics.get("queue_drift") if self._metrics else None
            if gauge is not None:
                cast(_GaugeLike, gauge).set(float(drift))
            if drift != 0 and self._metrics:
                direction = "positive" if drift > 0 else "negative"
                counter = self._metrics.get("music_queue_skew")
                if counter is not None:
                    cast(_CounterLike, counter).labels(direction=direction).inc()

    def record_mic_levels(self, levels: MutableMapping[str, float]) -> None:
        sanitized_levels = {str(k): float(v) for k, v in levels.items()}
        ts = _now_ts()
        with self._lock:
            for channel, level in sanitized_levels.items():
                history = self._mic_levels.setdefault(channel, deque(maxlen=_MIC_LEVEL_HISTORY))
                history.append(level)
            get_diagnostics_bus().emit(
                "mic.levels",
                severity="DEBUG",
                message="Microphone levels sampled",
                timestamp=ts,
                levels=sanitized_levels,
            )

    def record_stt_idle(self, idle_seconds: float) -> None:
        idle_seconds = max(0.0, float(idle_seconds))
        with self._lock:
            self._stt_idle_seconds = idle_seconds
            if self._metrics:
                cast(_GaugeLike, self._metrics["stt_idle"]).set(idle_seconds)

    def record_voice_idle(self, idle_seconds: float) -> None:
        idle_seconds = max(0.0, float(idle_seconds))
        with self._lock:
            self._voice_idle_seconds += idle_seconds
            if self._metrics:
                cast(_GaugeLike, self._metrics["voice_idle"]).set(self._voice_idle_seconds)

    def record_command_queue_depth(self, depth: int) -> None:
        depth = max(0, int(depth))
        with self._lock:
            self._command_queue_depth = depth
            if self._metrics:
                cast(_GaugeLike, self._metrics["command_queue_depth"]).set(float(depth))

    def record_restart_counter(self, component: str, value: int) -> None:
        component = component.lower()
        value = max(0, int(value))
        with self._lock:
            previous = self._restart_counter_cache.get(component, 0)
            self._restart_counter_cache[component] = value
            delta = value - previous
            if self._metrics and delta > 0:
                cast(_CounterLike, self._metrics["restart_counters"]).labels(component=component).inc(float(delta))

    def record_failure(
        self,
        code: str,
        component: str,
        *,
        message: str | None = None,
        correlation_id: str | None = None,
        tier: str | None = None,
        retryable: bool | None = None,
        **context: object,
    ) -> None:
        event = {
            "timestamp": _now_ts(),
            "code": code,
            "component": component,
            "message": message,
            "tier": tier,
            "correlation_id": correlation_id,
            "retryable": retryable,
            "context": self._normalize_context(context),
        }
        with self._lock:
            self._failures.append(event)
            if self._metrics:
                cast(_CounterLike, self._metrics["failures_total"]).labels(code=code, component=component).inc()

    def heartbeat(self, subsystem: str, status: str = "ok", **details: object) -> None:
        subsystem = subsystem.lower()
        with self._lock:
            self._heartbeat_seq += 1
            entry = _Heartbeat(
                subsystem=subsystem,
                status=status,
                details=dict(details),
                ts=_now_ts(),
                sequence=self._heartbeat_seq,
            )
            self._heartbeats[subsystem] = entry
        # Heartbeats are high-frequency - use DEBUG for normal states, WARNING for errors
        # Normal states: ok, idle, active, ready, handled, waiting, listening
        # Error states: error, failed, timeout, stale, degraded
        normal_statuses = {
            "ok",
            "idle",
            "active",
            "ready",
            "handled",
            "waiting",
            "listening",
        }
        severity = "DEBUG" if status.lower() in normal_statuses else "WARNING"
        get_diagnostics_bus().emit(
            f"heartbeat.{subsystem}",
            severity=severity,
            message=f"{subsystem} heartbeat ({status})",
            status=status,
            details=details,
        )

    def increment(self, metric_name: str, value: int = 1, **labels: str) -> None:
        """
        Increment a named counter metric.

        This is a general-purpose increment method for hub state telemetry and
        other callers that need simple counter increments without creating
        dedicated methods for each metric.

        Args:
            metric_name: The metric name (e.g., "hub_state.reconciliations_total")
            value: The amount to increment by (default 1)
            **labels: Optional labels for the metric
        """
        with self._lock:
            # Store in a general counters dict for snapshot
            if not hasattr(self, "_general_counters"):
                self._general_counters: dict[str, int] = {}

            label_key = (
                f"{metric_name}:{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}" if labels else metric_name
            )
            # Enforce max keys to prevent unbounded growth from dynamic labels
            if label_key not in self._general_counters and len(self._general_counters) >= _MAX_GENERAL_COUNTER_KEYS:
                return  # Silently drop to protect memory
            self._general_counters[label_key] = self._general_counters.get(label_key, 0) + value

    def record_music_backend_restart(self, *, reason: str) -> None:
        label = reason or "unknown"
        with self._lock:
            backend_stats = self._music_stats.setdefault("backend", {"restarts": 0, "reasons": {}})
            if isinstance(backend_stats, dict):
                backend_stats["restarts"] = backend_stats.get("restarts", 0) + 1
                reasons = backend_stats.setdefault("reasons", {})
                if isinstance(reasons, dict):
                    reasons[label] = reasons.get(label, 0) + 1
        if self._metrics:
            cast(_CounterLike, self._metrics["music_backend_restarts"]).labels(reason=label).inc()

    def record_music_buffer_underrun(self) -> None:
        with self._lock:
            buffer_stats = self._music_stats.setdefault("buffer", {"underruns": 0})
            if isinstance(buffer_stats, dict):
                buffer_stats["underruns"] = buffer_stats.get("underruns", 0) + 1
        if self._metrics:
            cast(_CounterLike, self._metrics["music_buffer_underruns"]).inc()

    def record_music_queue_skew(self, *, drift: int, increment_metrics: bool = True) -> None:
        direction = "positive" if drift > 0 else "negative"
        with self._lock:
            queue_stats = self._music_stats.setdefault("queue", {"skew_events": 0, "last_drift": 0})
            if isinstance(queue_stats, dict):
                queue_stats["skew_events"] = queue_stats.get("skew_events", 0) + 1
                queue_stats["last_drift"] = drift
        if increment_metrics and self._metrics:
            counter = self._metrics.get("music_queue_skew")
            if counter is not None:
                cast(_CounterLike, counter).labels(direction=direction).inc()

    def record_music_queue_failure(self, stage: str) -> None:
        label = stage or "unknown"
        with self._lock:
            failure_stats = self._music_stats.setdefault("failures", {})
            if isinstance(failure_stats, dict):
                failure_stats[label] = failure_stats.get(label, 0) + 1
        if self._metrics:
            cast(_CounterLike, self._metrics["music_queue_failures"]).labels(stage=label).inc()

    def record_music_heartbeat_miss(self, *, source: str) -> None:
        label = source or "unknown"
        with self._lock:
            heartbeat_stats = self._music_stats.setdefault("heartbeat", {"misses": 0, "sources": {}})
            if isinstance(heartbeat_stats, dict):
                heartbeat_stats["misses"] = heartbeat_stats.get("misses", 0) + 1
                sources = heartbeat_stats.setdefault("sources", {})
                if isinstance(sources, dict):
                    sources[label] = sources.get(label, 0) + 1
        if self._metrics:
            cast(_CounterLike, self._metrics["music_heartbeat_miss"]).labels(source=label).inc()

    # ------------------------------------------------------------------ #
    # Snapshot helpers
    # ------------------------------------------------------------------ #
    def _summarise_fastapi(self) -> dict[str, object]:
        with self._lock:
            summary: dict[str, object] = {}
            for key, series in self._fastapi_latency.items():
                summary[key] = {
                    "latency": _quantiles(series),
                    "counts": dict(self._fastapi_counters.get(key, {})),
                }
            return summary

    def _summarise_mic_levels(self) -> dict[str, object]:
        with self._lock:
            return {
                channel: {
                    "latest": history[-1] if history else None,
                    "avg": fmean(history) if history else None,
                    "samples": len(history),
                }
                for channel, history in self._mic_levels.items()
            }

    def _summarise_latencies(self) -> dict[str, object]:
        with self._lock:
            return {
                "wake_detection": _quantiles(self._wake_latency),
                "stt_round_trip": _quantiles(self._stt_latency),
            }

    def _summarise_heartbeats(self) -> list[dict[str, object]]:
        with self._lock:
            result: list[dict[str, object]] = []
            now = _now_ts()
            for heartbeat in self._heartbeats.values():
                age = now - heartbeat.ts
                result.append(
                    {
                        "subsystem": heartbeat.subsystem,
                        "status": heartbeat.status,
                        "details": heartbeat.details,
                        "last_beat": heartbeat.ts,
                        "age_seconds": age,
                        "stale": age > _HEARTBEAT_STALE_SECONDS,
                    }
                )
            return result

    def _summarise_music(self) -> dict[str, object]:
        with self._lock:
            return copy.deepcopy(self._music_stats)

    def _normalize_context(self, value: object) -> object:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(k): self._normalize_context(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._normalize_context(v) for v in value]
        return str(value)

    def _summarise_failures(self) -> list[dict[str, object]]:
        with self._lock:
            return [copy.deepcopy(event) for event in self._failures]

    def snapshot(self) -> dict[str, object]:
        """
        Produce a structured snapshot used by /health/details and baseline scripts.
        """
        with self._lock:
            process_snapshot = dict(self._process_snapshot)
            vlc_status = dict(self._vlc_status)
            queue_drift = dict(self._queue_drift)
            command_queue_depth = self._command_queue_depth
            stt_idle = self._stt_idle_seconds
            music_stats = copy.deepcopy(self._music_stats)

        snapshot = {
            "generated_at": _now_ts(),
            "process": process_snapshot,
            "latency": self._summarise_latencies(),
            "fastapi": self._summarise_fastapi(),
            "vlc": vlc_status,
            "queue": {"drift": queue_drift, "command_queue_depth": command_queue_depth},
            "voice": {
                "stt_idle_seconds": stt_idle,
                "voice_idle_seconds": self._voice_idle_seconds,
                "mic_levels": self._summarise_mic_levels(),
            },
            "music": music_stats,
            "heartbeats": self._summarise_heartbeats(),
            "failures": self._summarise_failures(),
        }
        return snapshot

    # ------------------------------------------------------------------ #
    # Prometheus export
    # ------------------------------------------------------------------ #
    def generate_prometheus(self) -> tuple[bytes, str]:
        """
        Return (payload, content_type) for Prometheus scrapers.
        """
        self._ensure_prometheus()
        if _PROMETHEUS_AVAILABLE and self._prom_registry and _prometheus_client is not None:
            from prometheus_client import CollectorRegistry as PromCollectorRegistry

            registry = cast(PromCollectorRegistry, self._prom_registry)
            return (
                _prometheus_client.generate_latest(registry),
                "text/plain; version=0.0.4",
            )
        snapshot = json.dumps(self.snapshot(), indent=2).encode("utf-8")
        return snapshot, "application/json"


_RUNTIME_METRICS: RuntimeMetrics | None = None
_METRICS_LOCK = threading.Lock()


def get_runtime_metrics() -> RuntimeMetrics:
    global _RUNTIME_METRICS
    if _RUNTIME_METRICS is None:
        with _METRICS_LOCK:
            if _RUNTIME_METRICS is None:
                _RUNTIME_METRICS = RuntimeMetrics()
    return _RUNTIME_METRICS


__all__ = ["RuntimeMetrics", "get_runtime_metrics"]
