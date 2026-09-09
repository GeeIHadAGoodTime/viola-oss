"""Qt adapter for the pure-Python timer service."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from services.timer_core import Timer, TimerEventListener, TimerService as TimerCoreService


class _QtSignalBridge(TimerEventListener):
    def __init__(self, owner: TimerService) -> None:
        self._owner = owner

    def timer_added(self, user_id: str, timer: Timer) -> None:
        self._owner.timer_added.emit(timer.timer_id, timer.label, timer.duration_seconds)

    def timer_cancelled(self, user_id: str, timer_id: str, timer: Timer | None) -> None:
        self._owner.timer_cancelled.emit(timer_id)

    def timer_completed(self, user_id: str, timer_id: str, label: str) -> None:
        self._owner.timer_completed.emit(timer_id, label)

    def timer_updated(self, user_id: str, timer: Timer) -> None:
        self._owner.timer_updated.emit(timer.timer_id)

    def timers_changed(self, user_id: str | None = None) -> None:
        self._owner.timers_changed.emit()


class TimerService(QObject, TimerCoreService):
    """Qt signal wrapper over the shared timer core."""

    timer_added = Signal(str, str, int)
    timer_cancelled = Signal(str)
    timer_completed = Signal(str, str)
    timer_updated = Signal(str)
    timers_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        QObject.__init__(self, parent)
        TimerCoreService.__init__(self)
        self._qt_signal_bridge = _QtSignalBridge(self)
        self.add_listener(self._qt_signal_bridge)


__all__ = ["TimerService"]
