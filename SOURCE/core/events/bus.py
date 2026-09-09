"""
Event bus implementations shared across the application.

The LocalEventBus provides a thread-safe in-process dispatcher used by
headless services and tests. The SignalBus adapts the same contract to
Qt, ensuring handlers execute on the GUI thread via typed signals.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import (
    TYPE_CHECKING,
    Generic,
    Protocol,
    TypeAlias,
    TypeVar,
    cast,
    runtime_checkable,
)

from core.logging_config import get_logger

from .types import BaseEvent

logger = get_logger(__name__)

_QT_AVAILABLE: bool = False

try:
    from PySide6.QtCore import QObject, Signal

    _QT_AVAILABLE = True
except Exception:  # pragma: no cover - Qt optional
    logger.debug("PyQt6 not available, Qt event bus disabled")

if TYPE_CHECKING:
    from PySide6.QtCore import QObject as _QObjectType
else:
    _QObjectType = object


T_Event = TypeVar("T_Event", bound=BaseEvent)
Callback: TypeAlias = Callable[[T_Event], None]


class _QtBoundSignal(Protocol[T_Event]):
    def connect(self, slot: Callable[[T_Event], None]) -> None: ...

    def emit(self, event: T_Event) -> None: ...


@runtime_checkable
class _SignalAnchorProtocol(Protocol):
    """Protocol defining the interface for SignalAnchor implementations."""

    payload: _QtBoundSignal[BaseEvent] | None

    def __init__(self, parent: _QObjectType | None = None) -> None: ...


@dataclass(slots=True)
class Subscription:
    """Handle returned to allow callers to unsubscribe from an event."""

    event_type: type[BaseEvent]
    callback: Callable[[BaseEvent], None]
    cancel: Callable[[], None]

    def dispose(self) -> None:
        """Detach the callback from the event bus."""
        self.cancel()


class EventBus:
    """Protocol-like base class documenting the public API."""

    def publish(self, event: BaseEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def subscribe(
        self,
        event_type: type[T_Event],
        callback: Callback[T_Event],
        user_id: str | None = None,
    ) -> Subscription:  # pragma: no cover - interface
        raise NotImplementedError

    def unsubscribe(self, subscription: Subscription) -> None:
        """Detach a subscription returned by subscribe()."""
        subscription.dispose()


class LocalEventBus(EventBus):
    """Thread-safe in-process event bus used for tests or headless flows."""

    def __init__(self) -> None:
        self._subscribers: defaultdict[type[BaseEvent], list[Callable[[BaseEvent], None]]] = defaultdict(list)
        self._lock = Lock()

    def publish(self, event: BaseEvent) -> None:
        if not isinstance(event, BaseEvent):
            raise TypeError("Event must derive from BaseEvent")

        with self._lock:
            callbacks = tuple(self._subscribers.get(type(event), ()))

        for callback in callbacks:
            try:
                callback(event)
            except Exception as e:
                # Subscribers should not crash the publisher; diagnostics are emitted separately.
                logger.debug(
                    "Event subscriber callback failed (non-critical): %s",
                    e,
                    exc_info=True,
                )
                continue

    def subscribe(
        self,
        event_type: type[T_Event],
        callback: Callback[T_Event],
        user_id: str | None = None,
    ) -> Subscription:
        if not issubclass(event_type, BaseEvent):
            raise TypeError("event_type must be a subclass of BaseEvent")

        def _wrapped(event: BaseEvent) -> None:
            if isinstance(event, event_type) and (user_id is None or event.user_id == user_id):
                callback(event)

        with self._lock:
            self._subscribers[event_type].append(_wrapped)

        def _cancel() -> None:
            with self._lock:
                callbacks = self._subscribers.get(event_type)
                if not callbacks:
                    return
                if _wrapped in callbacks:
                    callbacks.remove(_wrapped)
                if not callbacks:
                    self._subscribers.pop(event_type, None)

        return Subscription(event_type=event_type, callback=_wrapped, cancel=_cancel)


class TypedSignal(Generic[T_Event]):
    """
    Light wrapper around a Qt signal enforcing event typing and GUI thread delivery.

    When Qt is unavailable, the signal degrades to a LocalEventBus-backed dispatcher
    so headless tests keep functioning.
    """

    def __init__(self, event_type: type[T_Event], *, parent: _QObjectType | None = None) -> None:
        self._event_type = event_type
        self._parent = parent
        self._fallback_bus: LocalEventBus | None = None

        if not _QT_AVAILABLE:
            self._fallback_bus = LocalEventBus()
            self._anchor = None
        else:
            self._anchor = _SignalAnchor(parent)

    def emit(self, event: T_Event) -> None:
        if not isinstance(event, self._event_type):
            raise TypeError(f"Signal event must be instance of {self._event_type.__name__}")

        if self._fallback_bus is not None:
            self._fallback_bus.publish(event)
            return

        if self._anchor is not None and self._anchor.payload is not None:
            self._anchor.payload.emit(event)

    def connect(self, callback: Callback[T_Event]) -> None:
        if self._fallback_bus is not None:
            self._fallback_bus.subscribe(self._event_type, callback)
            return

        if self._anchor is not None and self._anchor.payload is not None:

            def _wrapped(payload: BaseEvent) -> None:
                if isinstance(payload, self._event_type):
                    callback(payload)

            self._anchor.payload.connect(_wrapped)


class SignalBus(LocalEventBus):
    """
    Qt-aware event bus that guarantees handlers execute on the Qt GUI thread.
    Falls back to LocalEventBus semantics outside of Qt contexts (e.g. tests).
    """

    def __init__(self, parent: _QObjectType | None = None) -> None:
        super().__init__()
        self._parent = parent
        self._anchor: TypedSignal[BaseEvent] | None
        if not _QT_AVAILABLE:
            self._anchor = None
        else:
            self._anchor = TypedSignal(BaseEvent, parent=parent)
            self._anchor.connect(self._dispatch)

    def publish(self, event: BaseEvent) -> None:
        if not isinstance(event, BaseEvent):
            raise TypeError("Event must derive from BaseEvent")

        with self._lock:
            callbacks = tuple(self._subscribers.get(type(event), ()))

        if not callbacks:
            return

        if self._anchor is None:
            for callback in callbacks:
                try:
                    callback(event)
                except Exception as e:
                    logger.debug("Signal callback failed (non-critical): %s", e, exc_info=True)
                    continue
            return

        self._anchor.emit(event)

    def _dispatch(self, payload: BaseEvent) -> None:
        callbacks: tuple[Callable[[BaseEvent], None], ...]
        with self._lock:
            callbacks = tuple(self._subscribers.get(type(payload), ()))

        for callback in callbacks:
            try:
                callback(payload)
            except Exception as e:
                logger.debug(
                    "Event dispatcher callback failed (non-critical): %s",
                    e,
                    exc_info=True,
                )
                continue


_default_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the process-local typed event bus."""
    global _default_event_bus
    if _default_event_bus is None:
        _default_event_bus = SignalBus()
    return _default_event_bus


class _SignalAnchorFallback:
    """Fallback stub when Qt is not available."""

    payload: _QtBoundSignal[BaseEvent] | None = None

    def __init__(self, parent: _QObjectType | None = None) -> None:
        del parent


# Create the appropriate anchor class based on Qt availability
_SignalAnchor: type[_SignalAnchorProtocol]

if _QT_AVAILABLE:
    # Qt is available - create a proper QObject subclass
    class _QtSignalAnchor(QObject):
        """Qt QObject hosting a dynamic signal to bridge event dispatch."""

        payload = cast(_QtBoundSignal[BaseEvent], Signal(BaseEvent))

        def __init__(self, parent: _QObjectType | None = None) -> None:
            super().__init__(parent)

    _SignalAnchor = _QtSignalAnchor

else:
    # Qt not available - use the fallback stub class
    _SignalAnchor = _SignalAnchorFallback


__all__ = [
    "Callback",
    "EventBus",
    "LocalEventBus",
    "SignalBus",
    "Subscription",
    "TypedSignal",
    "get_event_bus",
    "set_event_bus",
]


def set_event_bus(bus: EventBus) -> EventBus:
    """Replace the process-wide event bus and return the previous instance.

    Tests (notably the loopback Pipecat bench) use this to isolate subscriptions
    without serving the real WebSocket layer. Runtime code should call
    ``get_event_bus()`` and accept whatever the active bus returns.
    """
    if not isinstance(bus, EventBus):
        raise TypeError("bus must implement EventBus")
    global _default_event_bus
    previous = _default_event_bus if _default_event_bus is not None else SignalBus()
    _default_event_bus = bus
    return previous
