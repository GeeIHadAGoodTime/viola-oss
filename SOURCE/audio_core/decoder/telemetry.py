"""
Telemetry instrumentation for the audio decode pipeline.

The telemetry module emits structured JSON logs and publishes TelemetryEvent
messages on the shared event bus. Metrics are surfaced through the
TelemetryMetric dataclass so scrapers or in-process observers can subscribe
without parsing log output.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from core.events.bus import EventBus, LocalEventBus
from core.events.types import TelemetryEvent
from core.logging_config import get_logger


class MetricObserver(Protocol):
    """Callable signature for metric observers."""

    def __call__(self, metric: TelemetryMetric) -> None:  # pragma: no cover - protocol
        ...


@dataclass(slots=True)
class TelemetryMetric:
    """Simple metric structure for downstream scrapers/actions."""

    name: str
    value: float
    tags: dict[str, str]
    timestamp: float


class DecoderTelemetry:
    """
    Emit structured telemetry for decoder components.

    - Logs JSON payloads to the configured logger.
    - Publishes TelemetryEvent to the provided EventBus.
    - Invokes registered metric observers for each metric emission.
    """

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        logger: logging.Logger | None = None,
        metric_observer: MetricObserver | None = None,
        source: str = "audio_core.decoder",
    ) -> None:
        self._event_bus = event_bus or LocalEventBus()
        self._logger = logger or get_logger(source)
        self._metric_observer = metric_observer
        self._source = source

    # ------------------------------------------------------------------ #
    # Generic emitters
    # ------------------------------------------------------------------ #

    def emit_event(self, name: str, payload: dict[str, object]) -> None:
        """Emit a structured telemetry event."""
        event_payload = dict(payload)
        event_payload.setdefault("emitted_at", time.time())

        log_record = {
            "event": name,
            "source": self._source,
            "payload": event_payload,
        }
        self._logger.info(json.dumps(log_record, sort_keys=True))

        try:
            self._event_bus.publish(TelemetryEvent(name=name, payload=event_payload, source=self._source))
        except Exception:  # pragma: no cover - defensive
            self._logger.exception("Failed to publish telemetry event")

    def emit_metric(self, name: str, value: float, *, tags: dict[str, str] | None = None) -> None:
        """Emit a numeric metric suitable for scraping."""
        metric = TelemetryMetric(
            name=name,
            value=float(value),
            tags=dict(tags or {}),
            timestamp=time.time(),
        )

        payload = {
            "metric": metric.name,
            "value": metric.value,
            "tags": metric.tags,
            "timestamp": metric.timestamp,
        }
        self.emit_event("audio.decode.metric", payload)

        if self._metric_observer:
            try:
                self._metric_observer(metric)
            except Exception:  # pragma: no cover
                self._logger.exception("Metric observer failed")

    # ------------------------------------------------------------------ #
    # Specialised helpers for decoder pipeline
    # ------------------------------------------------------------------ #

    def emit_decode_start(self, *, source_uri: str, codec: str, sample_rate: int, channels: int) -> None:
        self.emit_event(
            "audio.decode.start",
            {
                "source_uri": source_uri,
                "codec": codec,
                "sample_rate": sample_rate,
                "channels": channels,
            },
        )

    def emit_decode_stop(self, *, source_uri: str, reason: str, elapsed_sec: float) -> None:
        self.emit_event(
            "audio.decode.stop",
            {
                "source_uri": source_uri,
                "reason": reason,
                "elapsed_sec": round(elapsed_sec, 3),
            },
        )

    def emit_decode_error(self, *, source_uri: str, error_code: str, message: str) -> None:
        self.emit_event(
            "audio.decode.error",
            {
                "source_uri": source_uri,
                "error_code": error_code,
                "message": message,
            },
        )
        self.emit_metric(
            "audio_decode_errors_total",
            1.0,
            tags={"error_code": error_code, "source": self._source},
        )

    def emit_buffer_metrics(self, metrics) -> None:
        """
        Emit buffer metrics as part of decoder telemetry.

        `metrics` is expected to implement the BufferMetrics protocol defined in
        `buffer_manager.py`. Lazy import avoided to keep coupling minimal.
        """
        payload = {
            "capacity_bytes": metrics.capacity_bytes,
            "occupied_bytes": metrics.occupied_bytes,
            "level_percent": round(metrics.level_percent, 2),
            "underrun_count": metrics.underrun_count,
            "overflow_count": metrics.overflow_count,
            "last_write_timestamp": metrics.last_write_timestamp,
            "last_read_timestamp": metrics.last_read_timestamp,
        }
        self.emit_event("audio.decode.buffer_metrics", payload)
        self.emit_metric(
            "audio_decode_underflows_total",
            float(metrics.underrun_count),
            tags={"source": self._source},
        )
        self.emit_metric(
            "audio_decode_overflows_total",
            float(metrics.overflow_count),
            tags={"source": self._source},
        )

    def emit_buffer_level(self, metrics) -> None:
        """Emit instantaneous buffer level metric."""
        self.emit_metric(
            "audio_decode_buffer_level",
            metrics.level_percent,
            tags={"source": self._source},
        )

    def emit_decode_latency(self, *, stage: str, duration_sec: float) -> None:
        """Emit latency measurement for a decode stage."""
        self.emit_metric(
            "audio_decode_stage_latency_sec",
            duration_sec,
            tags={"stage": stage, "source": self._source},
        )


__all__ = ["DecoderTelemetry", "TelemetryMetric"]
