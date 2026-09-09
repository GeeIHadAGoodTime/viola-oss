from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from core.logging_config import get_logger

logger = get_logger(__name__)


class _APIClient(Protocol):
    base_url: str
    session: requests.Session


@dataclass(slots=True)
class MusicPlayerViewModel:
    is_playing: bool = False


class MusicPlayerWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.is_playing = False

    def apply(self, model: MusicPlayerViewModel) -> None:
        self.is_playing = model.is_playing


class ChatWidget(QWidget):
    def __init__(self, *, send_command: CommandSender, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chat_input = QLineEdit(self)
        self._send_command = send_command

        layout = QVBoxLayout(self)
        layout.addWidget(self.chat_input)
        self.setLayout(layout)

    def _on_submit(self) -> None:
        text = self.chat_input.text().strip()
        if not text:
            return
        self._send_command(text)
        self.chat_input.clear()


class CommandSender(Protocol):
    def __call__(self, text: str) -> None: ...


class WindowCore(QWidget):
    def __init__(self, *, api_client: _APIClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api_client = api_client

        self.music_player = MusicPlayerWidget(self)
        self.chat_widget = ChatWidget(send_command=self._send_command, parent=self)

        layout = QVBoxLayout(self)
        layout.addWidget(self.music_player)
        layout.addWidget(self.chat_widget)
        self.setLayout(layout)

        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self.poll_state)
        self._state_timer.start(750)
        self.poll_state()

    def _send_command(self, text: str) -> None:
        url = f"{self.api_client.base_url.rstrip('/')}/v1/command"
        try:
            resp = self.api_client.session.post(url, json={"text": text}, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            logger.exception("Command failed: %s", exc)
            return
        self.poll_state()

    def poll_state(self) -> None:
        url = f"{self.api_client.base_url.rstrip('/')}/v1/state"
        try:
            resp = self.api_client.session.get(url, timeout=5)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.exception("State poll failed: %s", exc)
            return

        state = payload.get("data") if isinstance(payload, dict) else None
        if state is None:
            state = payload

        if not isinstance(state, dict):
            return

        is_playing = state.get("is_playing")
        model = MusicPlayerViewModel(is_playing=bool(is_playing))
        self.music_player.apply(model)


__all__ = ["ChatWidget", "MusicPlayerViewModel", "MusicPlayerWidget", "WindowCore"]
