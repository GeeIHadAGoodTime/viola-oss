from __future__ import annotations

import logging
from collections import deque
from collections.abc import MutableSequence, Sequence

from models.player import QueueItem

from .queue_engine import PlaylistQueueEngine
from .state_service import PlayerStateService


class PlayerStatePipeline:
    """
    Glue layer between the queue engine and PlayerState persistence.

    The legacy MusicPlayer historically mutated PlayerState, queue mirrors, and
    history structures inline. This service now owns those responsibilities so
    higher-level orchestration can stay small.
    """

    def __init__(
        self,
        *,
        state_service: PlayerStateService,
        playlist: PlaylistQueueEngine,
        logger: logging.Logger,
        test_mode: bool,
    ) -> None:
        self._state_service = state_service
        self._playlist = playlist
        self._logger = logger.getChild("state_pipeline")
        self._test_mode = test_mode
        self._public_queue_view: Sequence[QueueItem] = playlist.upcoming_ref
        self._history_ref: deque[QueueItem] = playlist.history_ref
        self._pending_skip_tokens = 0

    # ------------------------------------------------------------------ #
    # Public surfaces                                                    #
    # ------------------------------------------------------------------ #

    @property
    def queue_view(self) -> Sequence[QueueItem]:
        return self._public_queue_view

    @property
    def history(self) -> deque[QueueItem]:
        return self._history_ref

    @property
    def pending_skip_tokens(self) -> int:
        return self._pending_skip_tokens

    def set_pending_skip_tokens(self, value: int) -> None:
        self._pending_skip_tokens = max(0, int(value))

    def add_pending_skip(self, count: int = 1) -> int:
        if count <= 0:
            return self._pending_skip_tokens
        self._pending_skip_tokens += count
        return self._pending_skip_tokens

    def consume_pending_skip(self) -> bool:
        if not self._pending_skip_tokens:
            return False
        self._pending_skip_tokens -= 1
        return True

    def consume_pending_skip_for_token(
        self,
        token: tuple[int, str | None] | None,
        item_id: str | None,
    ) -> bool:
        if (
            not self._pending_skip_tokens
            or token is None
            or item_id is None
            or not self._playlist.matches(token, item_id)
        ):
            return False
        self._pending_skip_tokens -= 1
        return True

    # ------------------------------------------------------------------ #
    # Queue / state synchronization                                      #
    # ------------------------------------------------------------------ #

    def set_now_playing(
        self,
        item: QueueItem | None,
        *,
        force_clear: bool = False,
    ) -> None:
        self._state_service.set_now_playing(item, force_clear=force_clear)

    def sync_queue_state(self) -> Sequence[QueueItem]:
        """
        DEPRECATED: Queue sync is no longer needed.

        QUEUE ARCHITECTURE (2026-01-12):
            Queue is read from canonical source on every snapshot() call.
            This method is kept for backward compatibility but does minimal work.
            All callers should be migrated to not depend on this.
        """
        # Just invalidate snapshot cache so next read gets fresh data
        self._state_service.invalidate_snapshot()

        if self._test_mode:
            # Test harness expects a copy that includes now_playing as index 0.
            state = self._state_service.state
            now_playing = state.now_playing
            if now_playing is not None:
                self._public_queue_view = [now_playing] + list(self._playlist.upcoming())
            else:
                self._public_queue_view = list(self._playlist.upcoming())
        else:
            self._public_queue_view = self._playlist.upcoming_ref
        return self._public_queue_view

    def promote_current(
        self,
        *,
        set_is_playing: bool | None = True,
        mark_playing: bool = False,
    ) -> tuple[int, str | None] | None:
        """
        Promote the current playlist item to now_playing.

        QUEUE ARCHITECTURE (2026-01-12): No sync_queue_state() call needed.
        Queue is read from canonical source on snapshot().
        """
        current = self._playlist.current()
        if current is None:
            return None
        token = self._playlist.snapshot()
        self.set_now_playing(current)
        # No sync_queue_state() - queue is canonical
        if set_is_playing is not None:
            self._state_service.set_is_playing(bool(set_is_playing))
        if mark_playing:
            try:
                self._playlist.mark_playing()
            except Exception as e:
                self._logger.exception("playlist.mark_playing failed (non-critical): %s", e)
        return token

    def handle_engine_finished(self) -> QueueItem | None:
        """
        Clear now_playing + playlist current when transport reports completion.

        QUEUE ARCHITECTURE (2026-01-12): No sync_queue_state() call needed.
        Queue is read from canonical source on snapshot().
        """
        current = self._playlist.current()
        if current is None:
            return None

        self.append_history(current)
        try:
            self._playlist.force_current(None, pending=False)
        except Exception as e:
            self._logger.exception("playlist.force_current(None) failed (non-critical): %s", e)

        self._state_service.set_is_playing(False)
        self.set_now_playing(None, force_clear=True)
        # No sync_queue_state() - queue is canonical
        return current

    # ------------------------------------------------------------------ #
    # History helpers                                                    #
    # ------------------------------------------------------------------ #

    def append_history(self, item: QueueItem) -> None:
        try:
            self._history_ref.append(item)
        except Exception as e:
            self._logger.exception("Failed to append history item (non-critical): %s", e)

    def replace_history(self, items: Sequence[QueueItem]) -> deque[QueueItem]:
        new_history = deque(items, maxlen=self._playlist.history_ref.maxlen)
        self._playlist.replace_history(new_history)
        self._history_ref = self._playlist.history_ref
        return self._history_ref

    def refresh_history_reference(self) -> deque[QueueItem]:
        self._history_ref = self._playlist.history_ref
        return self._history_ref

    # ------------------------------------------------------------------ #
    # Test-mode helpers                                                  #
    # ------------------------------------------------------------------ #

    def override_queue_view(self, queue_view: MutableSequence[QueueItem]) -> Sequence[QueueItem]:
        self._public_queue_view = queue_view
        return self._public_queue_view
