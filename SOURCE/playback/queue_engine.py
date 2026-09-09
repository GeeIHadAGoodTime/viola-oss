"""
Reducer-driven playback queue engine and provider coordination helpers.

This module consolidates playback queue management, state transitions, and
transport orchestration into a single, thread-safe component.  It composes the
audio-core queue manager with the playback state machine and exposes a
reducer-style API that keeps the `models.player.PlayerState` object as the
canonical source of truth.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable

from audio_core.queue_controller import QueueManager
from audio_core.queue_types import QueueOperationResult
from audio_core.state_machine import PlaybackStateMachine, StateTransitionError
from core.logging_config import StructuredLogger, get_logger
from models.player import PlayerState, QueueItem
from playback.engine_manager import PlaybackEngineManager
from playback.feature_flags import PlaybackFeatureFlags

QueueItemLike: TypeAlias = QueueItem | Mapping[str, object]


@runtime_checkable
class PlaybackTransport(Protocol):
    """Protocol describing the transport surface required by the queue engine."""

    transport_id: str
    transport_display_name: str
    transport_capabilities: Mapping[str, object]

    def play_item(
        self,
        item: QueueItem,
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> None: ...

    def stop(self) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def set_volume(self, level: int) -> int: ...

    def seek(self, position_seconds: float) -> None: ...

    def current_position(self) -> float | None: ...

    def current_duration(self) -> float | None: ...


class PlaybackQueueAction(Enum):
    """Reducer actions supported by :class:`PlaybackQueueEngine`."""

    ENQUEUE = "enqueue"
    INSERT_NEXT = "insert_next"
    PLAY_NOW = "play_now"
    PLAY_NEXT = "play_next"
    FINISH_CURRENT = "finish_current"
    REMOVE = "remove"
    CLEAR = "clear"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SET_VOLUME = "set_volume"
    SEEK = "seek"
    UPDATE_POSITION = "update_position"
    SYNC_QUEUE = "sync_queue"
    LOAD_SNAPSHOT = "load_snapshot"


@dataclass(frozen=True, slots=True)
class PlaybackErrorRecord:
    """Structured playback error record persisted in `PlayerState.playback_errors`."""

    code: str
    message: str
    timestamp: float
    item_id: str | None = None
    context: dict[str, object] | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "timestamp": self.timestamp,
        }
        if self.item_id:
            payload["item_id"] = self.item_id
        if self.context:
            payload["context"] = self.context
        return payload


class PlaybackQueueEngine:
    """
    Single source of truth for playback queue and player state.

    The engine exposes reducer-style actions that mutate the queue and playback
    state atomically, delegating media control to a transport implementation.

    Thread-safety
    -------------
    * All state mutations are protected by an internal re-entrant lock.
    * Subscribers receive snapshots after each successful reducer invocation.

    Transport contract
    -------------------
    The supplied transport must implement :class:`PlaybackTransport`.  The
    engine automatically wires the transport's ``on_finished`` callback so that
    natural track completion feeds back into the reducer pipeline.
    """

    _MAX_ERROR_RECORDS = 20

    def __init__(
        self,
        transport: PlaybackTransport,
        *,
        max_queue_size: int = 100,
        initial_queue: Sequence[QueueItemLike] | None = None,
        initial_state: PlayerState | None = None,
        logger: logging.Logger | StructuredLogger | None = None,
    ) -> None:
        if logger is None:
            self._logger = get_logger("viola.playback.queue")._logger
        elif isinstance(logger, StructuredLogger):
            self._logger = logger._logger
        else:
            self._logger = logger
        self._transport = transport
        self._lock = threading.RLock()
        self._queue_manager = QueueManager(
            max_size=max_queue_size,
            logger=self._logger.getChild("queue_manager"),
        )
        self._fsm = PlaybackStateMachine(
            logger=self._logger.getChild("state_machine"),
        )
        self._subscribers: list[Callable[[PlayerState], None]] = []
        self._history: list[QueueItem] = []
        self._errors: list[PlaybackErrorRecord] = []

        self._transport_id = getattr(transport, "transport_id", "unknown")
        self._transport_name = getattr(transport, "transport_display_name", self._transport_id.title())
        capabilities = getattr(transport, "transport_capabilities", None) or {}
        self._transport_capabilities = dict(capabilities)

        base_state = initial_state.model_copy(deep=True) if initial_state else PlayerState()
        self._state: PlayerState = base_state
        self._now_playing: QueueItem | None = (
            base_state.now_playing.model_copy(deep=True) if base_state.now_playing is not None else None
        )
        self._volume = int(base_state.volume) if isinstance(base_state.volume, (int, float)) else 0
        self._position = int(base_state.position) if isinstance(base_state.position, (int, float)) else 0
        self._duration = int(base_state.duration) if isinstance(base_state.duration, (int, float)) else 0

        if base_state.queue:
            self._replace_queue(base_state.queue, notify=False)
        elif initial_queue:
            self._replace_queue(initial_queue, notify=False)

        if self._now_playing is not None:
            # Align FSM with provided state.
            if base_state.is_playing:
                self._safe_transition(self._fsm.play, track_id=self._now_playing.id)
                self._safe_transition(self._fsm.loaded)
            else:
                self._safe_transition(self._fsm.play, track_id=self._now_playing.id)

        self._refresh_state(propagate=False)

    # ---------------------------------------------------------------------#
    # Public API
    # ---------------------------------------------------------------------#
    def dispatch(
        self,
        action: PlaybackQueueAction,
        *,
        trigger: str = "",
        **payload: object,
    ) -> PlayerState:
        """
        Apply a reducer action and return the updated player state.

        Args:
            action: Reducer action to apply.
            trigger: Optional diagnostic label for logging.
            **payload: Action-specific payload.
        """
        with self._lock:
            handler = self._action_handlers().get(action)
            if handler is None:
                raise ValueError(f"Unsupported queue action: {action}")
            state = handler(trigger=trigger or action.value, **payload)
            return state

    def subscribe(self, callback: Callable[[PlayerState], None]) -> Callable[[], None]:
        """
        Subscribe to state updates.

        Returns an unsubscribe callable.
        """
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsubscribe

    def get_state(self) -> PlayerState:
        """Return a deep copy of the current player state."""
        with self._lock:
            return self._state.model_copy(deep=True)

    # Convenience wrappers ------------------------------------------------#
    def enqueue(self, item: QueueItemLike) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.ENQUEUE, item=item)

    def insert_next(self, item: QueueItemLike) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.INSERT_NEXT, item=item)

    def play_now(self, item: QueueItemLike, *, remove_from_queue: bool = True) -> PlayerState:
        return self.dispatch(
            PlaybackQueueAction.PLAY_NOW,
            item=item,
            remove_from_queue=remove_from_queue,
        )

    def play_next(self) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.PLAY_NEXT)

    def finish_current(self) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.FINISH_CURRENT)

    def remove(self, item_id: str) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.REMOVE, item_id=item_id)

    def clear(self, *, reason: str = "user_request") -> PlayerState:
        return self.dispatch(PlaybackQueueAction.CLEAR, reason=reason)

    def pause(self) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.PAUSE)

    def resume(self) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.RESUME)

    def stop(self) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.STOP)

    def set_volume(self, level: int) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.SET_VOLUME, level=level)

    def seek(self, position_seconds: float) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.SEEK, position_seconds=position_seconds)

    def update_position(self, position: float, duration: float | None = None) -> PlayerState:
        return self.dispatch(
            PlaybackQueueAction.UPDATE_POSITION,
            position=position,
            duration=duration,
        )

    def sync_queue(self, items: Sequence[QueueItemLike]) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.SYNC_QUEUE, items=items)

    def load_snapshot(self, snapshot: PlayerState) -> PlayerState:
        return self.dispatch(PlaybackQueueAction.LOAD_SNAPSHOT, snapshot=snapshot)

    def handle_transport_finished(self, *, source: str = "transport") -> None:
        """
        Route an external transport completion event into the reducer.
        """
        self.dispatch(
            PlaybackQueueAction.FINISH_CURRENT,
            trigger=f"{source}_finished",
        )

    # ---------------------------------------------------------------------#
    # Reducer handlers
    # ---------------------------------------------------------------------#
    def _action_handlers(self) -> dict[PlaybackQueueAction, Callable[..., PlayerState]]:
        return {
            PlaybackQueueAction.ENQUEUE: self._handle_enqueue,
            PlaybackQueueAction.INSERT_NEXT: self._handle_insert_next,
            PlaybackQueueAction.PLAY_NOW: self._handle_play_now,
            PlaybackQueueAction.PLAY_NEXT: self._handle_play_next,
            PlaybackQueueAction.FINISH_CURRENT: self._handle_finish_current,
            PlaybackQueueAction.REMOVE: self._handle_remove,
            PlaybackQueueAction.CLEAR: self._handle_clear,
            PlaybackQueueAction.PAUSE: self._handle_pause,
            PlaybackQueueAction.RESUME: self._handle_resume,
            PlaybackQueueAction.STOP: self._handle_stop,
            PlaybackQueueAction.SET_VOLUME: self._handle_set_volume,
            PlaybackQueueAction.SEEK: self._handle_seek,
            PlaybackQueueAction.UPDATE_POSITION: self._handle_update_position,
            PlaybackQueueAction.SYNC_QUEUE: self._handle_sync_queue,
            PlaybackQueueAction.LOAD_SNAPSHOT: self._handle_load_snapshot,
        }

    def _handle_enqueue(self, *, item: QueueItemLike, trigger: str, **_: object) -> PlayerState:
        queue_item = self._coerce_item(item)
        result, message = self._queue_manager.add(queue_item)
        if result != QueueOperationResult.SUCCESS:
            self._record_queue_error(
                "enqueue_failed",
                message or result.value,
                item_id=queue_item.id,
                context={"trigger": trigger},
            )
        return self._refresh_state()

    def _handle_insert_next(self, *, item: QueueItemLike, trigger: str, **_: object) -> PlayerState:
        queue_item = self._coerce_item(item)
        result, message = self._queue_manager.add(queue_item, position=0)
        if result != QueueOperationResult.SUCCESS:
            self._record_queue_error(
                "insert_next_failed",
                message or result.value,
                item_id=queue_item.id,
                context={"trigger": trigger},
            )
        return self._refresh_state()

    def _handle_play_now(
        self,
        *,
        item: QueueItemLike,
        remove_from_queue: bool,
        trigger: str,
        **_: object,
    ) -> PlayerState:
        queue_item = self._coerce_item(item)
        if remove_from_queue:
            self._queue_manager.remove(queue_item.id)

        if self._now_playing is not None and self._now_playing.id != queue_item.id:
            self._history.append(self._now_playing)
            if len(self._history) > 25:
                self._history.pop(0)

        return self._start_playback(queue_item, trigger=trigger)

    def _handle_play_next(self, *, trigger: str, **_: object) -> PlayerState:
        next_item = self._queue_manager.pop_next()
        if next_item is None:
            # Nothing left to play; stop current track if any.
            return self._handle_stop(trigger=trigger)
        return self._start_playback(next_item, trigger=trigger)

    def _handle_finish_current(self, *, trigger: str, **_: object) -> PlayerState:
        if self._now_playing is not None:
            self._history.append(self._now_playing)
            if len(self._history) > 25:
                self._history.pop(0)

        self._safe_transition(self._fsm.end)
        next_item = self._queue_manager.pop_next()
        if next_item is None:
            self._stop_transport()
            return self._refresh_state()
        return self._start_playback(next_item, trigger=trigger)

    def _handle_remove(self, *, item_id: str, trigger: str, **_: object) -> PlayerState:
        result, message = self._queue_manager.remove(item_id)
        if result != QueueOperationResult.SUCCESS:
            self._record_queue_error(
                "remove_failed",
                message or result.value,
                item_id=item_id,
                context={"trigger": trigger},
            )
        return self._refresh_state()

    def _handle_clear(self, *, reason: str, **_: object) -> PlayerState:
        self._queue_manager.clear(reason=reason)
        return self._refresh_state()

    def _handle_pause(self, *, trigger: str, **_: object) -> PlayerState:
        try:
            self._transport.pause()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_transport_error("pause_failed", exc)
        self._safe_transition(self._fsm.pause)
        return self._refresh_state()

    def _handle_resume(self, *, trigger: str, **_: object) -> PlayerState:
        try:
            self._transport.resume()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_transport_error("resume_failed", exc)
        self._safe_transition(self._fsm.resume)
        return self._refresh_state()

    def _handle_stop(self, *, trigger: str, **_: object) -> PlayerState:
        self._stop_transport()
        self._safe_transition(self._fsm.stop)
        self._now_playing = None
        self._position = 0
        self._duration = 0
        return self._refresh_state()

    def _handle_set_volume(self, *, level: int, **_: object) -> PlayerState:
        clamped = max(0, min(100, int(level)))
        try:
            volume_result = self._transport.set_volume(clamped)
            self._volume = int(volume_result) if isinstance(volume_result, (int, float)) else clamped
        except Exception as exc:  # pragma: no cover - defensive
            self._record_transport_error("set_volume_failed", exc)
            self._volume = clamped
        return self._refresh_state()

    def _handle_seek(self, *, position_seconds: float, trigger: str, **_: object) -> PlayerState:
        try:
            self._transport.seek(position_seconds)
        except Exception as exc:  # pragma: no cover - defensive
            self._record_transport_error("seek_failed", exc)
        else:
            self._position = max(0, int(position_seconds))
        return self._refresh_state()

    def _handle_update_position(
        self,
        *,
        position: float,
        duration: float | None,
        **_: object,
    ) -> PlayerState:
        self._position = max(0, int(position))
        if duration is not None:
            self._duration = max(0, int(duration))
        return self._refresh_state(propagate=False)

    def _handle_sync_queue(self, *, items: Sequence[QueueItemLike], **_: object) -> PlayerState:
        self._replace_queue(items, notify=False)
        return self._refresh_state()

    def _handle_load_snapshot(self, *, snapshot: PlayerState, **_: object) -> PlayerState:
        snapshot_copy = snapshot.model_copy(deep=True)
        self._state = snapshot_copy
        self._now_playing = snapshot_copy.now_playing.model_copy(deep=True) if snapshot_copy.now_playing else None
        self._volume = int(snapshot_copy.volume)
        self._position = int(snapshot_copy.position)
        self._duration = int(snapshot_copy.duration)
        self._replace_queue(snapshot_copy.queue, notify=False)
        if snapshot_copy.is_playing and self._now_playing is not None:
            self._safe_transition(self._fsm.play, track_id=self._now_playing.id)
            self._safe_transition(self._fsm.loaded)
        else:
            self._safe_transition(self._fsm.stop)
        return self._refresh_state()

    # ---------------------------------------------------------------------#
    # Internal helpers
    # ---------------------------------------------------------------------#
    def _start_playback(self, item: QueueItem, *, trigger: str) -> PlayerState:
        self._now_playing = item.model_copy(deep=True)
        self._safe_transition(self._fsm.play, track_id=item.id)
        try:
            self._transport.play_item(
                item,
                on_finished=self._handle_transport_finished,
            )
        except Exception as exc:
            self._record_transport_error("play_failed", exc, item_id=item.id)
            self._safe_transition(
                self._fsm.error,
                error_message=str(exc),
                error_code="transport_play_failed",
            )
            self._now_playing = None
            return self._refresh_state()
        else:
            self._safe_transition(self._fsm.loaded)
            # Refresh cached position from transport after playback kicks off.
            self._update_position_from_transport()
            return self._refresh_state()

    def _stop_transport(self) -> None:
        try:
            self._transport.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_transport_error("stop_failed", exc)

    def _handle_transport_finished(self) -> None:
        """
        Handle transport completion callback.

        This is called from the transport thread when playback ends.
        We hold the lock through the entire operation to prevent races
        with concurrent stop() calls. The lock is reentrant (RLock) so
        dispatch() can safely re-acquire it.
        """
        with self._lock:
            # Guard: Only process if we're actually playing something
            # This prevents spurious callbacks from affecting state
            if self._now_playing is None:
                self._logger.debug("Ignoring transport_finished callback: no track currently playing")
                return
            # Keep lock held to ensure atomicity with the guard check
            self.handle_transport_finished(source=self._transport_id)

    def _replace_queue(self, items: Sequence[QueueItemLike], *, notify: bool) -> None:
        self._queue_manager.clear(reason="replace")
        for entry in items:
            queue_item = self._coerce_item(entry)
            self._queue_manager.add(queue_item)
        if notify:
            self._refresh_state()

    def _refresh_state(self, *, propagate: bool = True) -> PlayerState:
        self._update_position_from_transport()
        queue_snapshot = [
            item.model_copy(deep=True) if hasattr(item, "model_copy") else item
            for item in self._queue_manager.get_items()
        ]

        state_payload = {
            "is_playing": self._fsm.is_playing(),
            "now_playing": (self._now_playing.model_copy(deep=True) if self._now_playing else None),
            "queue": queue_snapshot,
            "volume": self._volume,
            "position": self._position,
            "duration": self._duration,
            "position_percentage": self._position_percentage(),
            "backend": self._transport_id,
            "backend_display_name": self._transport_name,
            "backend_capabilities": dict(self._transport_capabilities),
            "playback_capabilities": dict(self._transport_capabilities),
            "playback_errors": [record.to_payload() for record in self._errors],
        }
        self._state = self._state.model_copy(update=state_payload)
        if propagate:
            self._notify_subscribers()
        return self._state

    def _notify_subscribers(self) -> None:
        if not self._subscribers:
            return
        snapshot = self._state.model_copy(deep=True)
        for callback in list(self._subscribers):
            try:
                callback(snapshot)
            except Exception as exc:  # pragma: no cover - observer errors
                self._logger.debug("State subscriber failed: %s", exc)

    def _update_position_from_transport(self) -> None:
        get_position = getattr(self._transport, "current_position", None)
        if callable(get_position):
            try:
                position = get_position()
            except Exception:  # pragma: no cover - defensive
                position = None
            if position is not None and isinstance(position, (int, float)):
                self._position = max(0, int(position))

        get_duration = getattr(self._transport, "current_duration", None)
        if callable(get_duration):
            try:
                duration = get_duration()
            except Exception:  # pragma: no cover - defensive
                duration = None
            if duration is not None and isinstance(duration, (int, float)):
                self._duration = max(0, int(duration))

    def _position_percentage(self) -> float:
        if self._duration <= 0 or self._position < 0:
            return 0.0
        return min(1.0, self._position / max(self._duration, 1))

    def _safe_transition(self, transition: Callable[..., object], *args: object, **kwargs: object) -> None:
        try:
            transition(*args, **kwargs)
        except StateTransitionError as exc:
            self._logger.debug("Ignored invalid state transition: %s", exc)

    def _coerce_item(self, item: QueueItemLike) -> QueueItem:
        if isinstance(item, QueueItem):
            return item
        return QueueItem.model_validate(item)

    def _record_queue_error(
        self,
        code: str,
        message: str,
        *,
        item_id: str | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        record = PlaybackErrorRecord(
            code=code,
            message=message,
            item_id=item_id,
            context=context,
            timestamp=time.time(),
        )
        self._errors.append(record)
        if len(self._errors) > self._MAX_ERROR_RECORDS:
            self._errors.pop(0)
        self._logger.debug(
            "Queue error recorded: code=%s, message=%s, context=%s",
            code,
            message,
            context,
        )

    def _record_transport_error(self, code: str, exc: Exception, *, item_id: str | None = None) -> None:
        self._record_queue_error(
            code,
            str(exc),
            item_id=item_id,
            context={"transport": self._transport_id},
        )


class PlaybackQueueCoordinator:
    """
    Provider prefetch coordinator used by the audio engine manager.
    """

    def __init__(
        self,
        manager: PlaybackEngineManager,
        feature_flags: PlaybackFeatureFlags,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._manager = manager
        self._feature_flags = feature_flags
        self._logger = logger or get_logger("viola.playback.queue.prefetch")

    def prepare_upcoming(self, queue_items: Iterable[QueueItem]) -> None:
        if not self._feature_flags.hot_buffer_enabled:
            return

        from collections import defaultdict

        grouped: dict[str, list[QueueItem]] = defaultdict(list)
        for item in queue_items:
            provider = self._manager.identify_provider(item)
            if provider:
                grouped[provider].append(item)

        for provider_id, items in grouped.items():
            engine = self._manager.get_engine(provider_id)
            if not engine or not engine.is_available():
                continue
            if engine.delegates_to_legacy_backend():
                continue
            try:
                from playback.engines.base import QueueContext

                engine.prefetch(
                    items,
                    queue_context=QueueContext(
                        upcoming_items=items,
                        feature_flags=self._feature_flags.to_dict(),
                    ),
                )
            except Exception as exc:  # pragma: no cover
                self._logger.debug("Prefetch failed for provider %s: %s", provider_id, exc)


__all__ = [
    "PlaybackQueueAction",
    "PlaybackQueueCoordinator",
    "PlaybackQueueEngine",
    "PlaybackTransport",
]
