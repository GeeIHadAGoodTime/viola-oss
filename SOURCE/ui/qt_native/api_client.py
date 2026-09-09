"""
Viola API Client - Main facade maintaining backward compatibility.
Delegates to specialized client modules for different operations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from .api_base import BaseAPIClient, PlayerState, QueueResponse, StatePollResult
from .monitor_client import MonitorClient
from .playback_client import PlaybackClient
from .queue_client import QueueClient
from .rating_client import RatingClient
from .startup_types import HealthStatus
from .state_client import StateClient


class _HasBaseUrl(Protocol):
    base_url: str


class ViolaAPIClient(BaseAPIClient):
    """
    Simple, reliable API client for Viola backend
    Uses HTTP polling instead of WebSocket for maximum reliability
    """

    def __init__(self, base_url: str | None = None, *, run_initial_probe: bool = False):
        BaseAPIClient.__init__(self, base_url)

        # Initialize specialized clients
        self._playback = PlaybackClient(self.session)
        self._queue = QueueClient(self.session)
        self._rating = RatingClient(self.session)
        self._monitor = MonitorClient(self.session)
        self._state = StateClient(self.session, self)

        # Set base URLs on all clients
        for client in (
            self._playback,
            self._queue,
            self._rating,
            self._monitor,
            self._state,
        ):
            cast(_HasBaseUrl, client).base_url = self.base_url

        # Initialize monitoring state
        self.backend_ready = False
        if run_initial_probe:
            self.backend_ready = self._monitor._initial_health_probe()

    # Playback controls
    def play_pause(self) -> bool:
        return self._playback.play_pause()

    def pause(self) -> bool:
        return self._playback.pause()

    def resume(self) -> bool:
        return self._playback.resume()

    def next_track(self) -> bool:
        return self._playback.next_track()

    def previous_track(self) -> bool:
        return self._playback.previous_track()

    def set_volume(self, level: int) -> bool:
        return self._playback.set_volume(level)

    def seek(self, position: int) -> bool:
        return self._playback.seek(position)

    # Queue management
    def get_queue(self) -> QueueResponse | None:
        raw = self._queue.get_queue()
        if raw is None:
            return None
        if not isinstance(raw, dict):
            return None
        return cast(QueueResponse, raw)

    def clear_queue(self) -> bool:
        return self._queue.clear_queue()

    def reorder_queue(self, from_index: int, to_index: int) -> bool:
        return self._queue.reorder_queue(from_index, to_index)

    def remove_from_queue(self, item_id: str) -> bool:
        return self._queue.remove_from_queue(item_id)

    # Rating system
    def thumbs_up(self, video_id: str, title: str, artist: str | None = None) -> bool:
        return self._rating.thumbs_up(video_id, title, artist)

    def thumbs_down(self, video_id: str, title: str, artist: str | None = None) -> bool:
        return self._rating.thumbs_down(video_id, title, artist)

    def get_rating_status(self, video_id: str) -> str | None:
        return self._rating.get_rating_status(video_id)

    def remove_rating(self, video_id: str) -> bool:
        return self._rating.remove_rating(video_id)

    # State management
    def get_state(self, *, notify: bool = True) -> PlayerState | None:
        return self._state.get_state(notify=notify)

    def poll_state_async(
        self,
        *,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> object | None:
        return self._state.poll_state_async(on_result=on_result, on_error=on_error)

    # Monitoring
    def health_check(self) -> bool:
        return self._monitor.health_check()

    def health_check_async(
        self,
        *,
        on_result: Callable[[bool], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> object | None:
        return self._monitor.health_check_async(on_result=on_result, on_error=on_error)

    def get_health_status(self, *, endpoint: str = "/health/details", timeout: float = 1.5) -> HealthStatus:
        return self._monitor.get_health_status(endpoint=endpoint, timeout=timeout)

    def get_health_details(self) -> HealthStatus:
        return self._monitor.get_health_details()

    def get_weather(self) -> dict[str, object] | None:
        payload = self._monitor.get_weather()
        if payload is None:
            return None
        if isinstance(payload, dict):
            return cast(dict[str, object], payload)
        return None

    def get_weather_async(
        self,
        *,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> object | None:
        return self._monitor.get_weather_async(on_result=on_result, on_error=on_error)

    def get_public_config(
        self,
        *,
        timeout: float = 2.0,
        use_cache: bool = True,
        max_age: float | None = 30.0,
    ) -> dict[str, object] | None:
        payload = self._monitor.get_public_config(timeout=timeout, use_cache=use_cache, max_age=max_age)
        if payload is None:
            return None
        if isinstance(payload, dict):
            return cast(dict[str, object], payload)
        return None


__all__ = ["StatePollResult", "ViolaAPIClient"]
