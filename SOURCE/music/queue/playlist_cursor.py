"""ConsolidatedState-aware playlist cursor and queue controller."""

from __future__ import annotations

import random
from collections import deque
from typing import TYPE_CHECKING

from diagnostics.queue_history import (
    QueueEventType,
    check_invariant,
    get_queue_history,
    log_queue_event,
)
from models.player import QueueItem
from models.state_manager import ConsolidatedState

if TYPE_CHECKING:
    pass


class PlaylistCursor:
    """
    Cursor-based playlist with explicit current, upcoming, and history tracking.

    When provided with a ConsolidatedState instance, queue mutations keep the
    canonical queue/history data in sync automatically.
    """

    def __init__(
        self,
        history_size: int = 10,
        state_manager: ConsolidatedState | None = None,
    ) -> None:
        self._state_manager = state_manager
        self._history_maxlen = history_size
        self._current: QueueItem | None = None
        if state_manager is not None:
            state_manager.queue.history_max_size = history_size
            self._upcoming = state_manager.queue.items
            self._history: deque[QueueItem] = deque(state_manager.queue.history, maxlen=history_size)
            state_manager.queue.history = list(self._history)
        else:
            self._upcoming = []
            self._history = deque(maxlen=history_size)
        self.pending = False
        self.version = 0
        self._pre_shuffle_order: list[QueueItem] | None = None

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def upcoming_ref(self) -> list[QueueItem]:
        return self._upcoming

    def reset(self, item: QueueItem | None) -> None:
        self._current = item
        self._upcoming.clear()
        self._history.clear()
        self.pending = item is not None
        self.version += 1
        self._sync_state()

    def current(self) -> QueueItem | None:
        return self._current

    def current_id(self) -> str | None:
        return self._current.id if self._current else None

    def schedule_current(self) -> bool:
        if self._current is not None:
            self.pending = True
            self._sync_state()
            return True
        if not self._upcoming:
            self.pending = False
            self._sync_state()
            return False
        self._current = self._upcoming.pop(0)
        self.pending = True
        self.version += 1
        self._sync_state()
        return True

    def mark_playing(self) -> QueueItem | None:
        self.pending = False
        self._sync_state()
        return self._current

    def upcoming(self) -> list[QueueItem]:
        return list(self._upcoming)

    def upcoming_count(self) -> int:
        return len(self._upcoming)

    def append(self, item: QueueItem) -> None:
        self._upcoming.append(item)
        self.version += 1
        self._sync_state()
        log_queue_event(
            QueueEventType.ADD,
            track_id=item.id,
            title=getattr(item, "title", None),
            queue_pos=len(self._upcoming) - 1,
            current_id=self._current.id if self._current else None,
            queue_length=len(self._upcoming),
            reason="append",
        )

    def insert_next(self, item: QueueItem) -> None:
        self._upcoming.insert(0, item)
        self.version += 1
        self._sync_state()
        log_queue_event(
            QueueEventType.ADD,
            track_id=item.id,
            title=getattr(item, "title", None),
            queue_pos=0,
            current_id=self._current.id if self._current else None,
            queue_length=len(self._upcoming),
            reason="insert_next",
        )

    def clear_upcoming(self) -> None:
        if self._upcoming:
            prev_len = len(self._upcoming)
            self._upcoming.clear()
            self.version += 1
            self._sync_state()
            log_queue_event(
                QueueEventType.CLEAR,
                current_id=self._current.id if self._current else None,
                queue_length=0,
                reason="clear_upcoming",
                extra={"previous_length": prev_len},
            )

    def remove_upcoming(self, item_id: str) -> bool:
        for idx, item in enumerate(self._upcoming):
            if item.id == item_id:
                removed = self._upcoming.pop(idx)
                self.version += 1
                self._sync_state()
                log_queue_event(
                    QueueEventType.REMOVE,
                    track_id=item_id,
                    title=getattr(removed, "title", None),
                    queue_pos=idx,
                    current_id=self._current.id if self._current else None,
                    queue_length=len(self._upcoming),
                    reason="remove_upcoming",
                )
                return True
        return False

    def reorder_upcoming(self, from_index: int, to_index: int) -> None:
        queue_len = len(self._upcoming)
        if from_index < 0 or from_index >= queue_len:
            raise ValueError(f"from_index {from_index} out of range")
        if to_index < 0 or to_index >= queue_len:
            raise ValueError(f"to_index {to_index} out of range")
        if from_index == to_index:
            return
        item = self._upcoming.pop(from_index)
        self._upcoming.insert(to_index, item)
        self.version += 1
        self._sync_state()

    def move_to_next(self, item_id: str) -> bool:
        if self._current is None:
            for idx, item in enumerate(self._upcoming):
                if item.id == item_id:
                    self._current = self._upcoming.pop(idx)
                    self.pending = True
                    self.version += 1
                    self._sync_state()
                    return True
            return False

        if self._current.id == item_id:
            self.pending = True
            self._sync_state()
            return True

        for idx, item in enumerate(self._upcoming):
            if item.id == item_id:
                entry = self._upcoming.pop(idx)
                self._upcoming.insert(0, entry)
                self.pending = True
                self.version += 1
                self._sync_state()
                return True
        return False

    def rewind(self) -> QueueItem | None:
        if not self._history:
            return None
        prev = self._history.pop()
        if self._current is not None:
            self._upcoming.insert(0, self._current)
        self._current = prev
        self.pending = True
        self.version += 1
        self._sync_state()
        return prev

    def complete_current(self, success: bool) -> QueueItem | None:
        finished = self._current
        from_id = finished.id if finished else None

        # INVARIANT: Log if completing with no current (should not happen in normal flow)
        if finished is None:
            check_invariant(
                False,
                "COMPLETE_NO_CURRENT",
                expected="current track to exist",
                actual="None",
                context={"queue_length": len(self._upcoming), "pending": self.pending},
            )
            log_queue_event(
                QueueEventType.INVARIANT_NO_CURRENT,
                reason="complete_current called with no current track",
                queue_length=len(self._upcoming),
            )
            self.pending = False
            self._sync_state()
            return None

        # INVARIANT: Check for double completion of same track
        history = get_queue_history()
        if history.check_double_complete(finished.id):
            # Double completion detected - return finished but don't advance again
            # This is idempotent protection
            log_queue_event(
                QueueEventType.INVARIANT_DOUBLE_COMPLETE,
                track_id=finished.id,
                title=getattr(finished, "title", None),
                reason="double_complete_prevented",
                outcome="ignored",
            )
            return finished

        if success:
            self._history.append(finished)

        if self._upcoming:
            self._current = self._upcoming.pop(0)
            self.pending = True
        else:
            self._current = None
            self.pending = False

        self.version += 1
        self._sync_state()

        # Log the completion and advance
        log_queue_event(
            QueueEventType.COMPLETE,
            track_id=finished.id,
            title=getattr(finished, "title", None),
            from_id=from_id,
            to_id=self._current.id if self._current else None,
            current_id=self._current.id if self._current else None,
            next_id=self._upcoming[0].id if self._upcoming else None,
            outcome="success" if success else "fail",
            queue_length=len(self._upcoming),
            extra={"added_to_history": success},
        )

        # Log the advance separately for timeline clarity
        if from_id != (self._current.id if self._current else None):
            log_queue_event(
                QueueEventType.ADVANCE,
                from_id=from_id,
                to_id=self._current.id if self._current else None,
                current_id=self._current.id if self._current else None,
                queue_length=len(self._upcoming),
                reason="complete_current",
            )

        return finished

    def snapshot(self) -> tuple[int, str | None]:
        return (self.version, self._current.id if self._current else None)

    def matches(self, token: tuple[int, str | None], item_id: str) -> bool:
        current = self._current
        if current is None:
            return False
        return current.id == item_id and token[1] == item_id

    # ------------------------------------------------------------------ #
    # Shuffle support
    # ------------------------------------------------------------------ #

    def shuffle_upcoming(self) -> int:
        """Shuffle the upcoming queue, preserving the original order for restore.

        Returns the number of tracks shuffled.
        """
        if not self._upcoming:
            return 0
        self._pre_shuffle_order = list(self._upcoming)
        random.shuffle(self._upcoming)
        self.version += 1
        self._sync_state()
        return len(self._upcoming)

    def unshuffle_upcoming(self) -> int:
        """Restore the original (pre-shuffle) queue order.

        Tracks that have been played since the shuffle was activated are
        excluded from the restored list.  The order reflects the original
        sequence starting from the first track that hasn't been played yet.

        Tracks queued *after* the shuffle are not in the saved order, so they
        keep their current relative order and follow the restored ones. Before
        #4214 they were dropped outright: the restored list was built purely
        from `_pre_shuffle_order`, so `_upcoming.clear()` deleted every track
        the user had added while shuffle was on. Turning shuffle off silently
        emptied part of the queue -- a worse failure than the wrong order it
        was fixing.

        The reordering rule itself lives in `music.shuffle_control`, shared
        with the cloud session service so both surfaces restore identically.

        Returns the number of tracks in the restored queue, or 0 if no saved
        order exists.
        """
        if self._pre_shuffle_order is None:
            return 0

        from music.shuffle_control import restore_saved_order

        restored = restore_saved_order([item.id for item in self._pre_shuffle_order], self._upcoming)

        self._upcoming.clear()
        self._upcoming.extend(restored)
        self._pre_shuffle_order = None
        self.version += 1
        self._sync_state()
        return len(self._upcoming)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _sync_state(self) -> None:
        if not self._state_manager:
            return
        # Upcoming list is shared with ConsolidatedState by reference.
        self._state_manager.queue.history = list(self._history)
        self._state_manager.playback.now_playing = self._current


# ============================================================================
# Queue Coherence Assertion
# ============================================================================


def assert_queue_coherent(
    canonical_queue: list[QueueItem],
    presented_queue: list[QueueItem],
    *,
    context: str = "",
) -> None:
    """
    Safety net assertion - fires if queue state ever diverges.

    This assertion verifies that the queue shown to users (API responses,
    WebSocket events) matches the canonical source (PlaylistCursor._upcoming).

    After the 2026-01-12 consolidation, this should NEVER fire. If it does,
    there's a bug in the queue synchronization logic.

    Args:
        canonical_queue: The authoritative queue from PlaylistCursor.upcoming()
        presented_queue: The queue being shown to users (from PlayerState, API, etc.)
        context: Optional context string for debugging (e.g., "GET /v1/state")

    Raises:
        AssertionError: If queues don't match
    """
    if len(canonical_queue) != len(presented_queue):
        raise AssertionError(
            f"QUEUE DESYNC{f' ({context})' if context else ''}: "
            f"canonical has {len(canonical_queue)} items, "
            f"presented has {len(presented_queue)} items"
        )

    for i, (canonical, presented) in enumerate(zip(canonical_queue, presented_queue)):
        if canonical.id != presented.id:
            raise AssertionError(
                f"QUEUE DESYNC{f' ({context})' if context else ''} at index {i}: "
                f"canonical={canonical.id}, presented={presented.id}"
            )
