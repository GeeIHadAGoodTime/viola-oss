from __future__ import annotations

import asyncio

from models.player import QueueItem
from music.player.playback_state import PlaybackPhase
from music.resolution.provider_router import Source


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        pass


class QueueCommandService:
    """Handles enqueue/play_next flows."""

    def __init__(
        self,
        *,
        player: MusicPlayer,
        controller,
        resolver,
        playlist,
        max_queue_size: int,
        logger,
    ) -> None:
        self._player = player
        self._controller = controller
        self._resolver = resolver
        self._playlist = playlist
        self._max_queue_size = max_queue_size
        self._logger = logger.getChild("queue_command")

    def enqueue(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        if not self._can_accept_more(query):
            return None

        item = self._resolve_queue_item(query, source, metadata)
        if item is None:
            return None

        player = self._player
        with player._cv:
            self._playlist.append(item)
            # Queue mutations auto-invalidate - no sync needed
            self._playlist.clear_backpressure()
            player._user_paused = False
            player._paused_current = None
            if not self._playlist.current():
                if self._playlist.schedule_current():
                    current = self._playlist.current()
                    if current is not None:
                        player._set_now_playing_locked(current)
                        player._state.is_playing = False
                        _sm_transition(player, PlaybackPhase.LOADING)
            player._cv.notify_all()

        if emit:
            player._emit()
        return item

    async def enqueue_async(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        return await asyncio.to_thread(self.enqueue, query, source, emit=emit, metadata=metadata)

    def play_next(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> QueueItem | None:
        if not self._can_accept_more(query):
            return None

        item = self._resolve_queue_item(query, source, metadata)
        if item is None:
            return None

        player = self._player
        with player._cv:
            self._controller.insert_next(item)
            # Queue mutations auto-invalidate - no sync needed
            if not self._playlist.current():
                self._playlist.schedule_current()
                player._set_now_playing_locked(self._playlist.current())
            player._cv.notify_all()

        if emit:
            player._emit()
        return item

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _can_accept_more(self, query: str) -> bool:
        player = self._player
        with player._cv:
            queue_len = self._playlist.queue_size()
            if queue_len >= self._max_queue_size:
                self._playlist.set_backpressure(origin="user", reason="capacity")
                self._logger.warning(
                    "Queue at maximum capacity (%s), not adding: %s",
                    self._max_queue_size,
                    query[:50],
                )
                return False
            return True

    def _resolve_queue_item(
        self,
        query: str,
        source: Source | None,
        metadata: dict[str, Any] | None,
    ) -> QueueItem | None:
        player = self._player
        engine_manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
        return self._resolver.resolve(
            query,
            source,
            metadata,
            engine_manager=engine_manager,
        )


from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from music.player import MusicPlayer
