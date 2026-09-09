"""
Video overlay widget for local video file playback.

Wraps QVideoWidget with click-to-pause, double-click-fullscreen,
and Esc-to-exit-fullscreen behavior. Lives in the UI layer — the backend
receives the inner QVideoWidget via dependency injection.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from core.logging_config import get_logger

logger = get_logger(__name__)


class VideoOverlayWidget(QWidget):
    """Overlay widget containing a QVideoWidget with interaction handlers.

    Signals:
        play_pause_requested: Emitted on single click (toggle play/pause).
        fullscreen_requested: Emitted on double click (True=enter, False=exit).
    """

    play_pause_requested = Signal()
    fullscreen_requested = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._is_fullscreen = False

        self._video_widget = QVideoWidget(self)
        self._video_widget.setStyleSheet("background-color: black;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._video_widget)

        self.setStyleSheet("background-color: black;")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # A left press starts this single-shot timer instead of emitting
        # play/pause immediately. A double-click (to toggle fullscreen) arrives
        # within the double-click interval and cancels it, so entering/exiting
        # fullscreen no longer ALSO pauses/resumes the video. A genuine single
        # click lets the timer fire and toggles play/pause. Without this, every
        # double-click fired both play_pause_requested (the first press) and
        # fullscreen_requested — the video paused exactly as it went fullscreen.
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.setInterval(QApplication.doubleClickInterval())
        self._single_click_timer.timeout.connect(self.play_pause_requested.emit)

    @property
    def video_widget(self) -> QVideoWidget:
        """Access the inner QVideoWidget for passing to backend."""
        return self._video_widget

    def set_fullscreen_state(self, is_fullscreen: bool) -> None:
        """Synchronize local double-click state with the parent window."""
        self._is_fullscreen = is_fullscreen

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Left click: schedule a play/pause toggle (cancelled by a double click)."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Restart the timer on each press so the second press of a
            # double-click can't leave a stale single-click pending.
            self._single_click_timer.start()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Double click: toggle fullscreen (and cancel the pending play/pause)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.stop()
            self._is_fullscreen = not self._is_fullscreen
            self.fullscreen_requested.emit(self._is_fullscreen)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Esc: exit fullscreen."""
        if event.key() == Qt.Key.Key_Escape and self._is_fullscreen:
            self._is_fullscreen = False
            self.fullscreen_requested.emit(False)
            event.accept()
        else:
            super().keyPressEvent(event)
