from __future__ import annotations

"""Structured diagnostics bus shared across runtime surfaces."""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings
from core.asyncio_safe import is_event_loop_closed_error
from core.logging_config import get_logger

logger = get_logger(__name__)

SeverityLevel = str

# Map severity levels to logging levels
_SEVERITY_TO_LEVEL = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

DiagnosticsSink = Callable[["DiagnosticsRecord"], None]


@dataclass(frozen=True, slots=True)
class DiagnosticsRecord:
    """Immutable diagnostics payload emitted by the bus."""

    name: str
    severity: SeverityLevel
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    correlation_id: str | None = None


class DiagnosticsBus:
    """
    Publish/subscribe diagnostics hub intended for runtime instrumentation.

    Thread-safe. Consumers add/remove sinks dynamically (e.g. Qt GUI surfaces,
    CLI sinks, test buffers). Emitted records mirror structured logging fields
    so downstream tooling can aggregate without parsing log text.
    """

    def __init__(self) -> None:
        self._subscribers: list[DiagnosticsSink] = []
        self._lock = threading.Lock()

    def emit(
        self,
        name: str,
        *,
        severity: SeverityLevel = "INFO",
        message: str = "",
        correlation_id: str | None = None,
        **context: Any,
    ) -> DiagnosticsRecord:
        if settings.diagnostics_disabled:
            return DiagnosticsRecord(
                name=name,
                severity=severity,
                message=message,
                context=dict(context),
                correlation_id=correlation_id,
            )

        record = DiagnosticsRecord(
            name=name,
            severity=severity,
            message=message,
            context=dict(context),
            correlation_id=correlation_id,
        )

        with self._lock:
            subscribers: Iterable[DiagnosticsSink] = tuple(self._subscribers)

        for sink in subscribers:
            try:
                sink(record)
            except Exception as exc:  # pragma: no cover - sinks shouldn't raise
                if is_event_loop_closed_error(exc):
                    continue
                # EXEMPT(hollow-check): Diagnostics must never crash the app.
                # Log at WARNING level so errors are visible but don't propagate.
                logger.warning("Diagnostics sink failed for %s: %s", name, exc)

        try:
            logger.bind(
                component="diagnostics",
                diagnostics_event=name,
                severity=severity,
                context=record.context,
            ).log(
                _SEVERITY_TO_LEVEL.get(severity, logging.INFO),
                message or name,
            )
        except Exception as exc:  # pragma: no cover - defensive logging guard
            if not is_event_loop_closed_error(exc):
                raise
        return record

    def add_sink(self, sink: DiagnosticsSink) -> Callable[[], None]:
        """Register a sink; returns a callable to remove it."""
        with self._lock:
            self._subscribers.append(sink)

        def _remove() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(sink)
                except ValueError:
                    # EXEMPT(hollow-check): Removing a non-existent sink is idempotent
                    # but should be logged at DEBUG level for debugging.
                    logger.debug("Attempted to remove sink that was already removed")

        return _remove

    def remove_sink(self, sink: DiagnosticsSink) -> None:
        """Remove a sink from the bus."""
        with self._lock:
            try:
                self._subscribers.remove(sink)
            except ValueError:
                # EXEMPT(hollow-check): Removing a non-existent sink is idempotent
                # but should be logged at DEBUG level for debugging.
                logger.debug("Attempted to remove sink that was already removed")

    def new_correlation_id(self) -> str:
        """Return a random correlation token for multi-step workflows."""
        return uuid.uuid4().hex


_GLOBAL_BUS = DiagnosticsBus()


def get_diagnostics_bus() -> DiagnosticsBus:
    """Return the singleton diagnostics bus."""
    return _GLOBAL_BUS


def set_sink(
    sink: Callable[[str, dict[str, Any]], None] | None,
) -> Callable[[], None] | None:
    """
    Backwards-compatible helper used by legacy tests.

    When provided, replaces subscribers with a wrapper that preserves the old
    `(event, payload)` callback signature.
    """
    if sink is None:
        return None

    def _wrapper(record: DiagnosticsRecord) -> None:
        payload = dict(record.context)
        payload.setdefault("message", record.message)
        payload.setdefault("severity", record.severity)
        payload.setdefault("timestamp", record.timestamp)
        if record.correlation_id:
            payload.setdefault("correlation_id", record.correlation_id)
        sink(record.name, payload)

    remover = _GLOBAL_BUS.add_sink(_wrapper)
    return remover


def log_quick_event(event: str, **payload: Any) -> None:
    """
    Emit a lightweight diagnostics event for rapid instrumentation.

    Payload fields become part of the diagnostics context; a timestamp is added
    automatically so tests can assert ordering without relying on wall-clock logs.
    """
    if settings.diagnostics_disable_quick_events:
        return
    message = str(payload.pop("message", event))
    _GLOBAL_BUS.emit(
        event,
        severity="DEBUG",
        message=message,
        ts=time.time(),
        **payload,
    )


__all__ = [
    "DiagnosticsBus",
    "DiagnosticsRecord",
    "DiagnosticsSink",
    "SeverityLevel",
    "get_diagnostics_bus",
    "log_quick_event",
    "set_sink",
]
