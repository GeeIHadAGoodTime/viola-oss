"""Diagnostics sink implementations."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TextIO, cast

from core.console import console
from core.logging_config import get_logger

from .bus import DiagnosticsRecord, DiagnosticsSink

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

# Qt optional support
# At runtime, we check QT_AVAILABLE before using Qt classes
QT_AVAILABLE = False


class _SignalProtocol(Protocol):
    """Protocol for Qt-like signal interface."""

    def connect(self, slot: Callable[[DiagnosticsRecord], None]) -> None: ...

    def emit(self, record: DiagnosticsRecord) -> None: ...


class _QtEmitterProtocol(Protocol):
    record_emitted: _SignalProtocol

    def receivers(self, signal: _SignalProtocol) -> int: ...


# Runtime Qt imports - store actual types for dynamic class creation
_QtQObjectType: type | None = None
_pyqtSignalType: type | None = None

try:
    from PySide6.QtCore import QObject as _QtQObject, Signal as _pyqtSignal

    _QtQObjectType = _QtQObject
    _pyqtSignalType = _pyqtSignal
    QT_AVAILABLE = True
except Exception:  # pragma: no cover - Qt optional
    pass


class StdoutSink:
    """Simple sink writing diagnostics payloads to stdout."""

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream: TextIO = stream if stream is not None else sys.stdout

    def __call__(self, record: DiagnosticsRecord) -> None:
        payload = {**record.context, "severity": record.severity}
        if record.correlation_id:
            payload["correlation_id"] = record.correlation_id
        formatted = f"[{record.severity}] {record.name}: {record.message}"
        if payload:
            formatted += f" {payload}"
        console(formatted, file=self._stream)


@dataclass
class BufferingSink:
    """Diagnostics sink buffering all records (useful for tests)."""

    records: list[DiagnosticsRecord] = field(default_factory=list)

    def __call__(self, record: DiagnosticsRecord) -> None:
        self.records.append(record)

    def clear(self) -> None:
        self.records.clear()


def _create_qt_emitter_class() -> type | None:
    """Create Qt emitter class at runtime when Qt is available.

    This factory function creates the QObject subclass with signal
    only when Qt is actually available, avoiding type errors from
    conditionally-defined base classes.

    Uses type() dynamic class creation to avoid mypy valid-type errors
    that occur when using a variable as a base class in a class statement.
    """
    if not QT_AVAILABLE or _QtQObjectType is None or _pyqtSignalType is None:
        return None

    # Create class dynamically using type() to avoid mypy valid-type error
    # This is equivalent to:
    #   class _QtDiagnosticsEmitter(_QtQObjectType):
    #       record_emitted = _pyqtSignalType(object)
    _QtDiagnosticsEmitter = type(
        "_QtDiagnosticsEmitter",
        (_QtQObjectType,),
        {"record_emitted": _pyqtSignalType(object)},
    )
    return _QtDiagnosticsEmitter


class QtDiagnosticsSink:
    """Qt wrapper emitting diagnostics records on the GUI thread.

    The record_emitted signal is connected to _on_record_emitted which stores
    records in an internal buffer for inspection/forwarding. This ensures the
    signal is never hollow (Pattern 6 compliance).

    When Qt is not available, this falls back to a no-op implementation.

    Note: This class uses composition with Qt rather than inheritance to avoid
    mypy issues with conditionally-defined base classes. The internal _qt_obj
    holds the actual QObject instance when Qt is available.
    """

    _MAX_BUFFER_SIZE = 100

    def __init__(self, parent: QObject | None = None) -> None:
        self._records_buffer: list[DiagnosticsRecord] = []
        self._qt_obj: _QtEmitterProtocol | None = None
        self._record_signal: _SignalProtocol | None = None

        emitter_cls = _create_qt_emitter_class()
        if emitter_cls is not None:
            qt_obj = cast(_QtEmitterProtocol, emitter_cls(parent))
            self._qt_obj = qt_obj
            self._record_signal = qt_obj.record_emitted
            self._record_signal.connect(self._on_record_emitted)

    def _on_record_emitted(self, record: DiagnosticsRecord) -> None:
        """Non-hollow handler for record_emitted signal.

        Stores records in internal buffer and logs at appropriate level.
        This ensures the signal has a real, observable effect.
        """
        self._records_buffer.append(record)
        if len(self._records_buffer) > self._MAX_BUFFER_SIZE:
            self._records_buffer.pop(0)
        get_logger(__name__).debug(
            "QtDiagnosticsSink received: [%s] %s: %s",
            record.severity,
            record.name,
            record.message,
        )

    @property
    def record_emitted(self) -> _SignalProtocol | None:
        """Return the Qt signal for external connections, if available."""
        return self._record_signal

    def receivers(self, signal: _SignalProtocol | None) -> int:
        """Return how many slots are connected to the Qt signal.

        This is a small compatibility shim for tests/diagnostics checks that
        expect `QObject.receivers(signal)` to be available.
        """
        if signal is None or self._qt_obj is None:  # pragma: no cover
            return 0
        try:
            return int(self._qt_obj.receivers(signal))
        except Exception:  # pragma: no cover
            return 0

    def get_recent_records(self) -> list[DiagnosticsRecord]:
        """Return copy of recent diagnostics records from buffer."""
        return list(self._records_buffer)

    def clear_buffer(self) -> None:
        """Clear the internal records buffer."""
        self._records_buffer.clear()

    def __call__(self, record: DiagnosticsRecord) -> None:
        if not QT_AVAILABLE or self._record_signal is None:  # pragma: no cover
            return
        try:
            self._record_signal.emit(record)
        except Exception as exc:
            # EXEMPT(hollow-check): Diagnostics must never crash the UI thread.
            # However, we log the error instead of silently swallowing it.
            get_logger(__name__).warning(
                "QtDiagnosticsSink failed to emit record '%s': %s",
                record.name,
                exc,
            )


def install_sink(bus: object, sink: DiagnosticsSink) -> Callable[[], None]:
    """Convenience helper to register a sink and return disposer."""
    # Type narrowing - bus expected to have add_sink method
    add_sink = getattr(bus, "add_sink", None)
    if add_sink is None:
        raise TypeError("bus must have add_sink method")
    result: Callable[[], None] = add_sink(sink)
    return result


__all__ = [
    "BufferingSink",
    "QtDiagnosticsSink",
    "StdoutSink",
    "install_sink",
]
