"""
Test Mode Control Stub.

Minimal control surface for test mode when full dependencies are unavailable.
Extracted from music/player/core.py to reduce file size.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

from core.json_types import JsonDict, JsonObject, to_json_value
from music.player.playback_state import PlaybackPhase

if TYPE_CHECKING:
    import logging

    from models.player import QueueItem


class _CV(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...

    def notify_all(self) -> None: ...


class _Playlist(Protocol):
    def upcoming(self) -> list[QueueItem]: ...

    def append(self, item: QueueItem) -> None: ...

    def insert_next(self, item: QueueItem) -> None: ...

    def schedule_current(self) -> None: ...

    def clear_upcoming(self) -> None: ...

    def remove_upcoming(self, item_id: str) -> bool: ...

    def replace_upcoming(self, items: list[QueueItem]) -> None: ...

    def set_backpressure(self, *, origin: str, reason: str) -> None: ...

    def autoplay_additions(self) -> int: ...

    def increment_autoplay_counter(self) -> None: ...

    def has_recent_failure(self, item: QueueItem, ttl_seconds: int) -> bool: ...

    def __iter__(self) -> Iterator[QueueItem]: ...


class _QueueConfig(Protocol):
    failure_ttl_sec: int

    def can_add_autoplay_song(self, queue_size: int, autoplay_additions: int) -> bool: ...


class _PlayerState(Protocol):
    now_playing: QueueItem | None
    is_playing: bool


class _Player(Protocol):
    _cv: _CV
    _playlist: _Playlist
    _state: _PlayerState
    _user_paused: bool
    _paused_current: object | None
    _queue_config: _QueueConfig
    _backend: object

    def state(self) -> object: ...

    def status(self) -> object: ...

    def _emit(self) -> None: ...


class TestModeControlStub:
    """Minimal control surface for test mode when full dependencies unavailable."""

    def __init__(self, player: _Player, logger: logging.Logger) -> None:
        self._player = player
        self._logger = logger

    def enqueue(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonObject | None = None,
    ) -> QueueItem | None:
        from models.player import QueueItem

        item = QueueItem(
            id=f"test-{query}",
            title=query,
            url=f"https://test.example/{query}",
            source=str(source) if source else "test",
        )
        with self._player._cv:
            # Check backpressure - queue capacity check
            current_size = len(self._player._playlist.upcoming())
            # Add 1 for current if exists
            if self._player._state.now_playing is not None:
                current_size += 1
            max_size = getattr(self._player, "_max_queue_size", 10000)

            if current_size >= max_size:
                # Record backpressure notice
                self._player._playlist.set_backpressure(origin="user", reason="capacity")
                return None

            # Clear user pause state when enqueueing (resume playback intent)
            self._player._user_paused = False
            self._player._paused_current = None

            self._player._playlist.append(item)
            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()
        return item

    async def enqueue_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonObject | None = None,
    ) -> QueueItem | None:
        return self.enqueue(query, source, emit=emit, metadata=metadata)

    def play_next(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonObject | None = None,
    ) -> QueueItem:
        from models.player import QueueItem

        item = QueueItem(
            id=f"test-{query}",
            title=query,
            url=f"https://test.example/{query}",
            source=str(source) if source else "test",
        )
        with self._player._cv:
            self._player._playlist.insert_next(item)
            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()
        return item

    def play(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonObject | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        """Play a track immediately, scheduling it as current."""
        from models.player import QueueItem

        item = QueueItem(
            id=f"test-{query}",
            title=query,
            url=f"https://test.example/{query}",
            source=str(source) if source else "test",
        )
        with self._player._cv:
            # Clear user pause state when starting new playback
            self._player._user_paused = False
            self._player._paused_current = None
            self._player._paused = False

            # Insert at front of queue and schedule as current
            self._player._playlist.insert_next(item)
            self._player._playlist.schedule_current()

            # Set now_playing in state
            self._player._state.now_playing = item
            self._player._state.is_playing = True
            # Also set the canonical _is_playing flag used by state()
            self._player._is_playing = True
            # State machine dual-write
            sm = getattr(self._player, "_playback_sm", None)
            if sm is not None:
                try:
                    sm.transition(PlaybackPhase.PLAYING, force=True)
                except Exception:
                    pass

            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()
        return item

    async def play_async(
        self,
        query: str,
        source: str | None = None,
        *,
        emit: bool = True,
        metadata: JsonObject | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        return self.play(query, source, emit=emit, metadata=metadata)

    def enqueue_autoplay(self, query: str, *, metadata: JsonObject | None = None) -> QueueItem | None:
        """Enqueue autoplay track with failure and backpressure checks."""
        from models.player import QueueItem

        # Check if backpressure prevents adding autoplay
        queue_size = len(self._player._playlist.upcoming())

        # Use the queue_config's can_add_autoplay_song method if available
        can_add_method = getattr(self._player._queue_config, "can_add_autoplay_song", None)
        if can_add_method and callable(can_add_method):
            if not can_add_method(queue_size, self._player._playlist.autoplay_additions()):
                # Record backpressure notice
                self._player._playlist.set_backpressure(origin="autoplay", reason="Max autoplay depth reached")
                return None

        # Create candidate item to check for recent failures
        # Use video_id to match the _failure_key logic (video_id is checked first)
        item = QueueItem(
            id=f"test-{query}",
            title=query,
            url=f"https://test.example/{query}",
            source="test",
            video_id=f"vid-{query}",  # Match the video_id format used by _install_resolver
        )

        # Check for recent failures (use 900 second TTL from queue config)
        failure_ttl = getattr(self._player._queue_config, "failure_ttl_sec", 900)
        if self._player._playlist.has_recent_failure(item, failure_ttl):
            self._logger.debug("Skipping autoplay for %s (recent failure)", query)
            return None

        # Enqueue if checks pass
        with self._player._cv:
            self._player._playlist.append(item)
            self._player._playlist.increment_autoplay_counter()
            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()
        return item

    def queue(self) -> list[QueueItem]:
        return list(self._player._playlist.upcoming())

    def clear_queue(self) -> None:
        with self._player._cv:
            self._player._playlist.clear_upcoming()
            # Queue mutations auto-invalidate - no sync needed

    def remove_from_queue(self, item_id: str) -> None:
        """Remove item from queue. Raises if trying to remove currently playing."""
        with self._player._cv:
            # Check if trying to remove currently playing item
            now_playing = self._player._state.now_playing
            if now_playing is not None and now_playing.id == item_id:
                raise ValueError(f"Cannot remove currently playing item: {item_id}")

            self._player._playlist.remove_upcoming(item_id)
            # Queue mutations auto-invalidate - no sync needed

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        """Reorder queue by moving item from from_index to to_index."""
        with self._player._cv:
            queue = list(self._player._playlist.upcoming())
            if from_index < 0 or from_index >= len(queue):
                raise ValueError(f"Invalid from_index {from_index}, queue size is {len(queue)}")
            if to_index < 0 or to_index >= len(queue):
                raise ValueError(f"Invalid to_index {to_index}, queue size is {len(queue)}")

            # Perform the reorder
            item = queue.pop(from_index)
            queue.insert(to_index, item)

            # Update playlist with reordered queue
            self._player._playlist.replace_upcoming(queue)
            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()

    def play_item_now(self, item_id: str) -> None:
        pass  # No-op for test mode

    @staticmethod
    def _track_payload(item: QueueItem | None) -> JsonDict | None:
        if item is None:
            return None
        return {
            "id": item.id,
            "title": item.title,
            "artist": item.artist,
            "provider": item.provider,
            "video_id": item.video_id,
            "url": item.url,
        }

    def skip(self) -> JsonDict:
        """Advance the test-mode queue without raising on an empty queue."""
        with self._player._cv:
            previous = self._player._state.now_playing
            upcoming = self._player._playlist.upcoming()
            next_item = upcoming[0] if upcoming else None
            if next_item is not None:
                self._player._playlist.remove_upcoming(next_item.id)
                self._player._state.now_playing = next_item
                self._player._state.is_playing = True
                self._player._is_playing = True
            elif previous is None:
                self._player._state.is_playing = False
                self._player._is_playing = False
            self._player._cv.notify_all()

        if next_item is not None:
            self._player._emit()
        return {
            "ok": True,
            "track_id": previous.id if previous is not None else None,
            "next_track_id": next_item.id if next_item is not None else None,
            "mode": "test",
            "now_playing": self._track_payload(next_item or self._player._state.now_playing),
            "is_playing": bool(self._player._state.is_playing),
            "queue_size": len(self._player._playlist.upcoming()),
            "autoplay": {"triggered": False, "added": 0, "available": False},
        }

    def next(self) -> JsonDict:
        return self.skip()

    def allow_test_stream_playback(self, enabled: bool = True) -> None:
        pass  # No-op for test mode

    def use_test_backend(self, backend: object) -> None:
        self._player._backend = backend

    def enqueue_resolved_item(self, item: QueueItem, *, play_immediately: bool = False, emit: bool = True) -> QueueItem:
        with self._player._cv:
            if play_immediately:
                # Match real PlayerControlService: reset cursor to this item
                # so it becomes the current track
                self._player._playlist.reset(item)
                self._player._state.now_playing = item
            else:
                self._player._playlist.append(item)
            # Queue mutations auto-invalidate - no sync needed
            self._player._cv.notify_all()
        return item

    def state(self) -> object:
        return self._player.state()

    def status(self) -> JsonDict:
        status_value = to_json_value(self._player.status())
        return status_value if isinstance(status_value, dict) else {}

    def emit_state_change(self) -> None:
        self._player._emit()
