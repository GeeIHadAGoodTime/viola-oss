from __future__ import annotations

"""
DebugEventBus mirroring utilities.

Persists sanitised DebugEvent payloads to rotated JSONL files for auditing
consent and onboarding flows without attaching directly to the Qt runtime.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.logging_config import get_logger


@runtime_checkable
class DebugEventProtocol(Protocol):
    """Protocol defining the DebugEvent interface for type checking."""

    @property
    def name(self) -> str: ...

    @property
    def payload(self) -> dict[str, object]: ...

    @property
    def source(self) -> str: ...

    @property
    def timestamp_ms(self) -> float: ...


# Define fallback dataclass for when Qt is not available
@dataclass
class _FallbackDebugEvent:
    """Fallback DebugEvent class used when Qt is not available."""

    name: str
    payload: dict[str, object]
    source: str
    timestamp_ms: float


# Type alias for the subscribe function signature using Protocol
_SubscribeFnType = Callable[[Callable[[DebugEventProtocol], None]], Callable[[], None]]

# Runtime state - will be set from Qt module if available
_qt_available: bool = False
_debug_event_class: type[DebugEventProtocol] = _FallbackDebugEvent
_subscribe_fn: _SubscribeFnType | None = None

try:
    from ui.qt_native.debug_events import (
        DebugEvent as _QtDebugEvent,
        subscribe_debug_events as _qt_subscribe,
    )

    _qt_available = True
    _debug_event_class = _QtDebugEvent
    _subscribe_fn = _qt_subscribe
except Exception:  # pragma: no cover - Qt optional
    pass

# Public exports
DebugEvent = _debug_event_class
subscribe_debug_events = _subscribe_fn


class _RotatingJsonWriter:
    """Simple rotating JSONL writer used for debug events."""

    def __init__(self, path: Path, *, max_bytes: int, backups: int) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._backups = max(1, backups)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _current_size(self) -> int:
        try:
            return self._path.stat().st_size
        except FileNotFoundError:
            return 0

    def _rotate(self) -> None:
        # Maintain `.1`, `.2`, ... ordering with `.1` being the latest backup.
        oldest = self._path.with_name(f"{self._path.name}.{self._backups}")
        if oldest.exists():
            oldest.unlink()

        for idx in range(self._backups - 1, 0, -1):
            src = self._path.with_name(f"{self._path.name}.{idx}")
            if src.exists():
                dst = self._path.with_name(f"{self._path.name}.{idx + 1}")
                src.rename(dst)

        if self._path.exists():
            self._path.rename(self._path.with_name(f"{self._path.name}.1"))

    def write(self, event: DebugEventProtocol) -> None:
        payload = {
            "name": event.name,
            "payload": event.payload,
            "source": event.source,
            "timestamp_ms": event.timestamp_ms,
            "ingest_ts": time.time(),
        }
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        with self._lock:
            projected = self._current_size() + len(data.encode("utf-8")) + 1
            if projected > self._max_bytes:
                self._rotate()
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(data)
                handle.write("\n")


def install_debug_event_mirror(
    *,
    destination: Path,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 3,
) -> Callable[[], None]:
    """
    Subscribe to DebugEventBus and persist events to a rotating JSONL file.

    Returns:
        Callable to unsubscribe and release resources.
    """

    if subscribe_debug_events is None:
        raise RuntimeError("DebugEventBus unavailable; ensure PyQt runtime is installed.")

    writer = _RotatingJsonWriter(destination, max_bytes=max_bytes, backups=backups)

    def _mirror(event: DebugEventProtocol) -> None:
        writer.write(event)

    unsubscribe = subscribe_debug_events(_mirror)

    def _cleanup() -> None:
        try:
            unsubscribe()
        except Exception as exc:
            # EXEMPT(hollow-check): Cleanup must not crash the app.
            # Log the error instead of silently swallowing it.

            get_logger(__name__).warning("Failed to unsubscribe debug event mirror: %s", exc)

    return _cleanup


__all__ = ["DebugEventProtocol", "install_debug_event_mirror"]
