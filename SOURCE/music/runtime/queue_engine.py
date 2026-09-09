from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from models.player import QueueItem
from music.queue.playlist_cursor import PlaylistCursor
from music.queue_config import QueueConfig
from music.runtime import QueueEngine, StateService


class PlaylistQueueEngine:
    """
    Thin wrapper around `PlaylistCursor` that centralises queue bookkeeping.

    This layer is the single owner of ConsolidatedState mutations. The legacy
    `MusicPlayer` interacts with it instead of touching `PlaylistCursor`
    directly, which allows us to migrate queue logic incrementally.

    Conformance to the `QueueEngine` protocol is STRUCTURAL and deliberately not
    by inheritance. `QueueEngine` is a `Protocol` whose members have `...`
    bodies, so subclassing it makes every one of those a real inherited method
    that returns `None`. A member missing from this class would then answer the
    call silently instead of raising `AttributeError` -- e.g.
    `loop_from_history()` would return `None`, the Repeat-All caller would read
    that as "no history to loop", and the feature would be quietly dead with
    nothing raised and nothing logged. That is the #2757 failure mode arriving
    by a second route, and inheriting the protocol is the only thing that
    creates it. Keep the protocol as the typed contract, not as a base class;
    `tests/unit/music/runtime/test_playlist_queue_engine_shuffle.py` asserts the
    structural `isinstance` check, which is only meaningful while this class
    does not inherit.
    """

    def __init__(
        self,
        *,
        state_service: StateService,
        queue_config: QueueConfig,
        history_size: int = 10,
        logger=None,
    ) -> None:
        self._logger = logger.getChild("queue_engine") if logger else None
        self._state_service = state_service
        self._queue_config = queue_config
        self._cursor = PlaylistCursor(
            history_size=history_size,
            state_manager=state_service.consolidated,
        )
        self._queue_ref = self._cursor.upcoming_ref
        self._history_ref: deque[QueueItem] = self._cursor._history
        # No artificial queue limit - users can curate freely (10000 = effectively unlimited)
        self._max_queue_size = 10000
        self._backpressure_notice: dict[str, Any] | None = None
        self._recent_queue_failures: dict[str, dict[str, Any]] = {}
        self._autoplay_additions = 0

    # ------------------------------------------------------------------ #
    # Core playlist operations                                            #
    # ------------------------------------------------------------------ #

    def current(self) -> QueueItem | None:
        return self._cursor.current()

    def current_id(self) -> str | None:
        return self._cursor.current_id()

    def schedule_current(self) -> bool:
        return self._cursor.schedule_current()

    def mark_playing(self) -> QueueItem | None:
        return self._cursor.mark_playing()

    def upcoming(self) -> list[QueueItem]:
        return self._cursor.upcoming()

    def upcoming_count(self) -> int:
        return self._cursor.upcoming_count()

    @property
    def upcoming_ref(self) -> list[QueueItem]:
        return self._queue_ref

    @property
    def history_ref(self) -> deque[QueueItem]:
        return self._history_ref

    # ------------------------------------------------------------------ #
    # Queue mutations - automatically invalidate snapshot cache            #
    # QUEUE ARCHITECTURE (2026-01-12): Mutations trigger cache invalidation#
    # so callers don't need to call _update_state_queue_locked()          #
    # ------------------------------------------------------------------ #

    def _invalidate(self) -> None:
        """Invalidate snapshot cache after queue mutation."""
        self._state_service.invalidate_snapshot()

    def append(self, item: QueueItem) -> None:
        self._cursor.append(item)
        self._invalidate()

    def insert_next(self, item: QueueItem) -> None:
        self._cursor.insert_next(item)
        self._invalidate()

    def clear_upcoming(self) -> None:
        self._cursor.clear_upcoming()
        self._invalidate()

    def remove_upcoming(self, item_id: str) -> bool:
        result = self._cursor.remove_upcoming(item_id)
        self._invalidate()
        return result

    def reorder_upcoming(self, from_index: int, to_index: int) -> None:
        self._cursor.reorder_upcoming(from_index, to_index)
        self._invalidate()

    def move_to_next(self, item_id: str) -> bool:
        result = self._cursor.move_to_next(item_id)
        self._invalidate()
        return result

    def rewind(self) -> QueueItem | None:
        result = self._cursor.rewind()
        self._invalidate()
        return result

    def complete_current(self, success: bool) -> QueueItem | None:
        result = self._cursor.complete_current(success)
        self._invalidate()
        return result

    def snapshot(self) -> tuple[int, str | None]:
        return self._cursor.snapshot()

    def matches(self, token: tuple[int, str | None], item_id: str) -> bool:
        return self._cursor.matches(token, item_id)

    def reset(self, item: QueueItem | None) -> None:
        self._cursor.reset(item)
        self._invalidate()

    def advance_embedded_cursor(self) -> QueueItem | None:
        """Advance the cursor to the next upcoming track for embedded-mode
        playback (e.g. the YouTube iframe), where completion is reported
        externally rather than driven through `complete_current()`.

        Mutates the real `PlaylistCursor` state (not a stray attribute on
        this wrapper) and invalidates the snapshot cache, so the
        queue/now-playing broadcast to clients reflects the advance instead
        of lagging behind what is actually about to play (#2757).

        Returns the new current track, or None if the queue was empty (in
        which case the cursor's current pointer is left untouched).
        """
        cursor = self._cursor
        if not cursor._upcoming:
            return None
        next_track = cursor._upcoming.pop(0)
        cursor._current = next_track
        cursor.version += 1
        cursor._sync_state()
        self._invalidate()
        return next_track

    def loop_from_history(self) -> QueueItem | None:
        """Repeat-All: when upcoming is exhausted, move history back into
        upcoming and advance to the first looped track.

        Goes through the cursor + invalidation path (unlike poking
        `_upcoming`/`_current`/`_history` directly on this wrapper, which
        are not real attributes here and silently fail to update the
        canonical cursor state or the broadcast snapshot) (#2757).

        Returns the new current track, or None if there was no history to
        loop from.
        """
        cursor = self._cursor
        if not cursor._history:
            return None
        looped = list(cursor._history)
        cursor._history.clear()
        cursor._upcoming.extend(looped)
        next_track = cursor._upcoming.pop(0)
        cursor._current = next_track
        cursor.pending = True
        cursor.version += 1
        cursor._sync_state()
        self._invalidate()
        return next_track

    def shuffle_upcoming(self) -> int:
        """Shuffle the upcoming queue, preserving the original order for restore.

        Returns the number of tracks shuffled (0 when nothing is queued).

        Callers hold a `PlaylistQueueEngine` - that is what `player._playlist`
        is - never the raw cursor, so this mutator has to exist here or the
        call is an `AttributeError`. The wrapper defines no `__getattr__`, so a
        cursor-only name reached through it does not fall through to the
        cursor, it simply explodes (#2757 bug class). The `_invalidate()` is
        the second half: reordering the queue without dropping the snapshot
        cache would leave every client rendering the pre-shuffle order.
        """
        count = self._cursor.shuffle_upcoming()
        self._invalidate()
        return count

    def unshuffle_upcoming(self) -> int:
        """Restore the original (pre-shuffle) upcoming order.

        Returns the number of tracks restored, or 0 when no saved order
        exists. Mirrors `shuffle_upcoming`: cursor mutation plus snapshot
        invalidation, so the restored order is what clients actually see.
        """
        count = self._cursor.unshuffle_upcoming()
        self._invalidate()
        return count

    # ------------------------------------------------------------------ #
    # Derived metadata                                                    #
    # ------------------------------------------------------------------ #

    @property
    def pending(self) -> bool:
        return self._cursor.pending

    @pending.setter
    def pending(self, value: bool) -> None:
        self._cursor.pending = bool(value)

    @property
    def version(self) -> int:
        return self._cursor.version

    @version.setter
    def version(self, value: int) -> None:
        self._cursor.version = int(value)

    # ------------------------------------------------------------------ #
    # Backpressure/failure handling                                      #
    # ------------------------------------------------------------------ #

    def queue_size(self) -> int:
        return len(self._queue_ref)

    def at_capacity(self) -> bool:
        return self.queue_size() >= self._max_queue_size

    def capacity(self) -> int:
        return self._max_queue_size

    def set_backpressure(self, origin: str, reason: str) -> dict[str, Any]:
        notice = {
            "origin": origin,
            "reason": reason,
            "timestamp": time.time(),
            "queue_size": self.queue_size(),
            "max": self._max_queue_size,
        }
        self._backpressure_notice = notice
        return notice

    def clear_backpressure(self) -> None:
        self._backpressure_notice = None

    def backpressure_notice(self) -> dict[str, Any] | None:
        if not self._backpressure_notice:
            return None
        return dict(self._backpressure_notice)

    def reset_autoplay_counter(self) -> None:
        self._autoplay_additions = 0

    def increment_autoplay_counter(self) -> int:
        self._autoplay_additions += 1
        return self._autoplay_additions

    def autoplay_additions(self) -> int:
        return self._autoplay_additions

    def record_failure(self, item: QueueItem, info: dict[str, Any]) -> None:
        self._recent_queue_failures[self._failure_key(item)] = info

    def has_recent_failure(self, item: QueueItem, ttl_seconds: float) -> bool:
        self.expire_failures(ttl_seconds)
        return self._failure_key(item) in self._recent_queue_failures

    def expire_failures(self, ttl_seconds: float) -> None:
        now = time.time()
        expired = [key for key, meta in self._recent_queue_failures.items() if now - meta["timestamp"] > ttl_seconds]
        for key in expired:
            self._recent_queue_failures.pop(key, None)

    def last_failures(self) -> Iterable[dict[str, Any]]:
        return list(self._recent_queue_failures.values())

    def _failure_key(self, item: QueueItem) -> str:
        return getattr(item, "video_id", None) or getattr(item, "url", None) or item.id

    # ------------------------------------------------------------------ #
    # Test-mode helpers                                                  #
    # ------------------------------------------------------------------ #

    def force_current(self, item: QueueItem | None, *, pending: bool | None = None) -> None:
        self._cursor._current = item
        if pending is not None:
            self._cursor.pending = bool(pending)
        self._cursor._sync_state()

    def replace_history(self, items: Sequence[QueueItem]) -> None:
        self._history_ref = deque(items, maxlen=self._history_ref.maxlen)
        self._cursor._history = self._history_ref
        self._cursor._sync_state()

    def replace_upcoming(self, items: Sequence[QueueItem]) -> None:
        self._queue_ref[:] = list(items)
        self._cursor._sync_state()


if TYPE_CHECKING:  # pragma: no cover - static conformance only

    def _assert_satisfies_queue_engine(engine: PlaylistQueueEngine) -> QueueEngine:
        """Keep the protocol conformance a type-checker error, not a base class.

        Inheriting `QueueEngine` would give every protocol member a silent
        `None`-returning fallback (see the class docstring). This assignment
        gets the same "did you drop a member?" signal at type-check time while
        a genuinely missing member stays a loud `AttributeError` at runtime.
        """
        return engine
