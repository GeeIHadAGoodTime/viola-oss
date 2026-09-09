from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QResizeEvent
from PySide6.QtWidgets import QLabel, QWidget


class TrackLabel(QLabel):
    """
    Multi-line track title label that preserves the full title in a tooltip and
    shows a clipped display (wrap + ellipsis) in the visible label.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        object_name: str | None = None,
        max_lines: int = 1,
    ) -> None:
        super().__init__(parent)
        if object_name is not None:
            self.setObjectName(object_name)
        self._max_lines = max(1, int(max_lines))
        self._full_text = ""
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._apply_layout()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._full_text:
            self._apply_layout()

    def _apply_layout(self) -> None:
        if not self._full_text:
            self.setText("")
            return

        metrics = QFontMetrics(self.font())
        width = max(self.contentsRect().width(), 1)
        words = self._full_text.split()
        if not words:
            self.setText(self._full_text)
            return

        lines: list[str] = []
        current = ""
        consumed = 0
        for idx, word in enumerate(words):
            candidate = (current + " " + word).strip()
            if not current or metrics.horizontalAdvance(candidate) <= width:
                current = candidate
                consumed = idx + 1
                continue

            lines.append(current)
            if len(lines) >= self._max_lines:
                break
            current = word
            consumed = idx + 1

        if len(lines) < self._max_lines and current:
            lines.append(current)

        truncated = consumed < len(words)
        if truncated and lines:
            lines[-1] = metrics.elidedText(lines[-1], Qt.TextElideMode.ElideRight, width)

        self.setText("\n".join(lines))


__all__ = ["TrackLabel"]
