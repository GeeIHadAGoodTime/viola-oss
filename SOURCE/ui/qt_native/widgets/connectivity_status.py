from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget


class ConnectivityState(str, Enum):
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class ConnectivityStatusIndicator(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.state = ConnectivityState.CONNECTING
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._sync_label()

    def set_state(self, state: ConnectivityState) -> None:
        self.state = state
        self._sync_label()

    def _sync_label(self) -> None:
        self._label.setText(f"Connectivity: {self.state.value}")
        self._label.adjustSize()

    def attach_to(self, parent: QWidget) -> None:
        self.setParent(parent)
        self.show()


__all__ = ["ConnectivityState", "ConnectivityStatusIndicator"]
