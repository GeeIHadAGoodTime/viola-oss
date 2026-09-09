"""Shared diagnostics infrastructure."""

from __future__ import annotations

from .bus import (
    DiagnosticsBus,
    DiagnosticsRecord,
    DiagnosticsSink,
    SeverityLevel,
    get_diagnostics_bus,
    log_quick_event,
    set_sink,
)
from .debug_events import install_debug_event_mirror
from .observability_logging import configure_observability
from .sinks import BufferingSink, QtDiagnosticsSink, StdoutSink, install_sink

__all__ = [
    "BufferingSink",
    "DiagnosticsBus",
    "DiagnosticsRecord",
    "DiagnosticsSink",
    "QtDiagnosticsSink",
    "SeverityLevel",
    "StdoutSink",
    "configure_observability",
    "get_diagnostics_bus",
    "install_debug_event_mirror",
    "install_sink",
    "log_quick_event",
    "set_sink",
]
