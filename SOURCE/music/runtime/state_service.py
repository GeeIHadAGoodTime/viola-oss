from __future__ import annotations

import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from core.json_types import to_json_value
from models.player import PlayerState, QueueItem
from models.state_manager import ConsolidatedState
from music.runtime import StateService

if TYPE_CHECKING:
    from services.persistence.state_store import PersistentStateStore


class QueueSource(Protocol):
    """Protocol for canonical queue source (PlaylistQueueEngine or similar)."""

    def upcoming(self) -> list[QueueItem]: ...

    def current(self) -> QueueItem | None: ...

    def clear_upcoming(self) -> None: ...


class PlayerStateService(StateService):
    """
    Owns the canonical `PlayerState` object plus persistence hooks.

    QUEUE ARCHITECTURE (2026-01-12):
        Queue is NOT stored in this service. All queue reads come from the
        canonical source (PlaylistCursor._upcoming via PlaylistQueueEngine).
        This eliminates sync calls and ensures a single source of truth.

    The legacy `MusicPlayer` historically mutated `PlayerState` directly and
    handled persistence inline. This service centralises those concerns so the
    queue/worker/controller layers can evolve independently.

    Idle Timeout (Product Decision 2026-01-11):
        Queue automatically clears after 1 hour of inactivity to prevent
        stale context from affecting AI autoplay recommendations.
    """

    # Queue clears after 1 hour of inactivity (Product Decision 2026-01-11)
    IDLE_TIMEOUT_SECONDS: float = 3600.0  # 1 hour

    def __init__(
        self,
        *,
        logger,
        default_volume: int,
        test_mode: bool,
        state_store: Any,  # PersistentStateStore | None
        queue_source: QueueSource | None = None,  # Canonical queue source
    ) -> None:
        self._logger = logger
        self._state_store = state_store
        self._test_mode = test_mode
        self._default_volume = default_volume
        self._queue_source = queue_source  # Canonical queue - NO local copy
        self._state = PlayerState()
        self._state.is_playing = False
        self._state.now_playing = None
        self._state.queue = []  # Ignored - queue comes from canonical source
        self._state.volume = default_volume
        self._state.metadata = {}
        self._consolidated = ConsolidatedState()
        self._last_snapshot_ts = 0.0
        self._last_snapshot_cache: PlayerState | None = None
        # Track last activity for idle timeout (Product Decision 2026-01-11)
        self._last_activity_ts: float = time.time()
        # Callback to reset autoplay context on idle timeout (set by MusicPlayer)
        self._on_idle_timeout_callback: callable | None = None

    def set_queue_source(self, source: QueueSource) -> None:
        """Set the canonical queue source. Called after PlaylistQueueEngine is created."""
        self._queue_source = source

    @staticmethod
    def _resolve_user_id() -> str:
        try:
            from core.user_context import get_current_user_id
        except ImportError as exc:
            raise LookupError("user_id is required for music state persistence") from exc

        try:
            user_id = get_current_user_id()
        except LookupError as exc:
            raise LookupError("user_id is required for music state persistence") from exc
        if not user_id:
            raise LookupError("user_id is required for music state persistence")
        return user_id

    def _get_canonical_queue(self) -> list[QueueItem]:
        """Get queue from canonical source. Returns empty list if source not set."""
        if self._queue_source is None:
            return []
        return list(self._queue_source.upcoming())

    # ------------------------------------------------------------------ #
    # Properties                                                         #
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> PlayerState:
        return self._state

    @property
    def consolidated(self) -> ConsolidatedState:
        return self._consolidated

    # ------------------------------------------------------------------ #
    # Idle timeout (Product Decision 2026-01-11)                         #
    # ------------------------------------------------------------------ #

    def touch_activity(self) -> None:
        """Update last activity timestamp. Called when queue/playback changes."""
        self._last_activity_ts = time.time()

    def set_idle_timeout_callback(self, callback: callable) -> None:
        """Set callback to be called when idle timeout clears queue."""
        self._on_idle_timeout_callback = callback

    def check_idle_timeout(self) -> bool:
        """
        Check if queue should be cleared due to inactivity.

        Returns True if queue was cleared, False otherwise.
        Also resets autoplay context via callback to prevent stale recommendations.

        QUEUE ARCHITECTURE (2026-01-12):
            Clears the canonical queue source directly, not a local copy.
        """
        if self._test_mode:
            return False  # Don't clear in tests

        # Check canonical queue, not local state
        canonical_queue = self._get_canonical_queue()
        elapsed = time.time() - self._last_activity_ts
        if elapsed > self.IDLE_TIMEOUT_SECONDS and (canonical_queue or self._state.now_playing):
            self._logger.info(
                "Clearing queue due to idle timeout (%.0f min idle, threshold=%.0f min)",
                elapsed / 60,
                self.IDLE_TIMEOUT_SECONDS / 60,
            )
            # Clear canonical queue
            if self._queue_source is not None:
                self._queue_source.clear_upcoming()
            self._state.now_playing = None
            self._state.is_playing = False
            self.invalidate_snapshot()
            self._last_activity_ts = time.time()  # Reset after clear

            # Reset autoplay context to prevent stale recommendations
            if self._on_idle_timeout_callback is not None:
                try:
                    self._on_idle_timeout_callback()
                except Exception as exc:
                    self._logger.debug("Idle timeout callback failed: %s", exc)

            return True
        return False

    # ------------------------------------------------------------------ #
    # Snapshot helpers                                                   #
    # ------------------------------------------------------------------ #

    def snapshot(self) -> PlayerState:
        """
        Return a copy of the current PlayerState suitable for external callers.

        QUEUE ARCHITECTURE (2026-01-12):
            Queue is read directly from the canonical source (PlaylistCursor._upcoming).
            No sync calls needed - the canonical source is always authoritative.

        Cloning avoids exposing internal references that callers might mutate.
        The copy is cached briefly to reduce churn when the UI polls rapidly.

        Also checks idle timeout and clears queue if inactive for 1+ hour.
        """
        # Check idle timeout before returning state
        self.check_idle_timeout()

        now = time.time()
        cached = self._last_snapshot_cache
        if cached is not None and (now - self._last_snapshot_ts) < 0.05:
            return cached

        # Read queue from canonical source - NO local copy
        queue = self._get_canonical_queue()

        clone = PlayerState(
            is_playing=self._state.is_playing,
            now_playing=self._state.now_playing,
            queue=queue,  # From canonical source
            volume=self._state.volume,
            position=self._state.position,
            duration=self._state.duration,
            position_percentage=self._state.position_percentage,
            backend=self._state.backend,
            backend_display_name=self._state.backend_display_name,
            backend_capabilities=dict(self._state.backend_capabilities),
            playback_capabilities=dict(self._state.playback_capabilities),
            playback_mode=self._state.playback_mode,
            resolver_info=dict(self._state.resolver_info),
            playback_errors=list(self._state.playback_errors),
            metadata=dict(self._state.metadata),
        )
        self._last_snapshot_cache = clone
        self._last_snapshot_ts = now
        return clone

    def invalidate_snapshot(self) -> None:
        """Force the next snapshot() call to refresh the cached copy."""
        self._last_snapshot_cache = None

    # ------------------------------------------------------------------ #
    # Queue/state mutation helpers                                       #
    # ------------------------------------------------------------------ #

    def set_now_playing(self, item: QueueItem | None, *, force_clear: bool = False) -> None:
        """
        Update now_playing with the same guardrails the legacy player enforced.

        Args:
            item: Queue entry to publish.
            force_clear: When True, bypasses the embedded webview guard and allows
                clearing now_playing even in test mode. Used when the engine
                reports completion.
        """
        if not force_clear and self._test_mode and item is None and self._state.now_playing is not None:
            current_mode = getattr(self._state.now_playing, "playback_mode", None)
            if current_mode in ("embedded_webview", "embedded_iframe_webview"):
                self._logger.warning("Attempted to clear now_playing for embedded webview in test mode")
                return
        if not force_clear and self._test_mode and item is None and self._state.now_playing is not None:
            self._logger.warning(
                "Clearing now_playing in test mode. Previous=%s",
                getattr(self._state.now_playing, "id", None),
            )
        self._state.now_playing = item
        if item is None:
            self._state.playback_mode = None
        else:
            item_mode = getattr(item, "playback_mode", None)
            if item_mode:
                self._state.playback_mode = item_mode
        self.touch_activity()  # Track activity for idle timeout
        self.invalidate_snapshot()

    def set_is_playing(self, value: bool) -> None:
        self._state.is_playing = bool(value)
        # Touch activity on ANY state change (play or pause) to prevent
        # idle timeout from firing while user has simply paused playback
        self.touch_activity()
        self.invalidate_snapshot()

    def apply_playback_state(
        self,
        *,
        now_playing: QueueItem | None = None,
        is_playing: bool | None = None,
        backend_name: str | None = None,
        backend_capabilities: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PlayerState:
        """
        Update playback state fields (NOT queue - queue is canonical).

        QUEUE ARCHITECTURE (2026-01-12):
            Queue is NOT updated here. Queue reads come from canonical source.
            This method only updates playback-related fields.
        """
        if is_playing is not None:
            self._state.is_playing = bool(is_playing)
        if now_playing is not None:
            self._state.now_playing = now_playing

        if backend_name is not None:
            self._state.backend = backend_name
            self._state.backend_display_name = backend_name
        if backend_capabilities is not None:
            backend_value = to_json_value(backend_capabilities)
            if isinstance(backend_value, dict):
                self._state.backend_capabilities = backend_value
        if metadata is not None:
            metadata_value = to_json_value(metadata)
            if isinstance(metadata_value, dict):
                self._state.metadata = metadata_value

        self.touch_activity()  # Track activity for idle timeout
        self.invalidate_snapshot()
        return self._state

    # DEPRECATED: Use apply_playback_state() instead
    # Kept for backward compatibility during migration
    def apply_queue_state(
        self,
        *,
        queue: Sequence[QueueItem] | None = None,  # IGNORED - queue is canonical
        now_playing: QueueItem | None = None,
        is_playing: bool = False,
        backend_name: str | None = None,
        backend_capabilities: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PlayerState:
        """
        DEPRECATED: Queue parameter is ignored. Use apply_playback_state().

        Queue is read from canonical source, not stored locally.
        """
        # Queue parameter is ignored - queue comes from canonical source
        return self.apply_playback_state(
            now_playing=now_playing,
            is_playing=is_playing,
            backend_name=backend_name,
            backend_capabilities=backend_capabilities,
            metadata=metadata,
        )

    def set_volume(self, volume: int) -> PlayerState:
        self._state.volume = max(0, min(100, int(volume)))
        self.invalidate_snapshot()
        return self._state

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def restore_full_state(self) -> None:
        """
        Restore full persisted state including queue and now_playing.
        (PRD Exit Criteria: Queue survives restart)

        Hardening notes:
        - Validates that restored items have required fields (id, url)
        - Tracks and logs skipped items for debugging
        - If now_playing has no URL, it's cleared (prevents playback trap)
        - Logs restore statistics for observability
        """
        if self._state_store is None:
            self._state.volume = self._default_volume
            self._state.now_playing = None
            self._state.is_playing = False
            self._state.queue = []
            self.invalidate_snapshot()
            return

        skipped_count = 0
        invalid_reasons: list[str] = []

        try:
            payload = self._state_store.load_music_state(self._resolve_user_id())

            # Restore volume
            volume_value = payload.get("volume", self._default_volume)
            try:
                volume_int = max(0, min(100, int(volume_value)))
            except (TypeError, ValueError):
                volume_int = self._default_volume
            self._state.volume = volume_int

            # Restore queue with validation
            queue_data = payload.get("queue", [])
            restored_queue: list[QueueItem] = []
            for idx, entry in enumerate(queue_data):
                try:
                    if isinstance(entry, dict):
                        item = QueueItem.model_validate(entry)
                        # HARDENING: Validate required playback fields
                        if not item.id:
                            skipped_count += 1
                            invalid_reasons.append(f"queue[{idx}]: missing id")
                            continue
                        # Note: url can be None for embedded playback (uses video_id)
                        # But if neither url nor video_id exists, skip
                        if not getattr(item, "url", None) and not getattr(item, "video_id", None):
                            skipped_count += 1
                            invalid_reasons.append(f"queue[{idx}] ({item.id}): missing url and video_id")
                            continue
                        restored_queue.append(item)
                except (ValidationError, TypeError, ValueError) as exc:
                    skipped_count += 1
                    invalid_reasons.append(f"queue[{idx}]: {exc}")
                    self._logger.debug("Failed to restore queue item: %r", exc)
            self._state.queue = restored_queue

            # Restore now_playing with validation
            now_playing_data = payload.get("now_playing")
            if now_playing_data is not None:
                try:
                    if isinstance(now_playing_data, dict):
                        item = QueueItem.model_validate(now_playing_data)
                        # HARDENING: Validate now_playing has playable content
                        if not item.id:
                            self._logger.warning("Restored now_playing has no id, clearing to prevent trap")
                            self._state.now_playing = None
                        elif not getattr(item, "url", None) and not getattr(item, "video_id", None):
                            self._logger.warning(
                                "Restored now_playing %s has no url/video_id, clearing to prevent playback trap",
                                item.id,
                            )
                            self._state.now_playing = None
                        else:
                            self._state.now_playing = item
                    else:
                        self._state.now_playing = None
                except (ValidationError, TypeError, ValueError) as exc:
                    self._logger.debug("Failed to restore now_playing: %r", exc)
                    self._state.now_playing = None
            else:
                self._state.now_playing = None

            # Don't auto-resume playback - user should explicitly resume
            self._state.is_playing = False

            # OBSERVABILITY: Log restore statistics
            self._logger.info(
                "Restored full state: volume=%d, queue_size=%d (skipped=%d), now_playing=%s",
                self._state.volume,
                len(self._state.queue),
                skipped_count,
                getattr(self._state.now_playing, "id", None),
            )

            # Log skipped items for debugging (if any)
            if skipped_count > 0:
                self._logger.warning(
                    "Skipped %d invalid queue items during restore: %s",
                    skipped_count,
                    "; ".join(invalid_reasons[:5]),  # Limit to first 5 for brevity
                )

        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("Failed to load persisted music state: %r", exc)
            self._state.volume = self._default_volume
            self._state.now_playing = None
            self._state.is_playing = False
            self._state.queue = []

        self.invalidate_snapshot()

    def restore_volume_only(self) -> None:
        """
        Restore persisted volume preference (queue is intentionally not restored).
        """
        if self._state_store is None:
            self._state.volume = self._default_volume
            return

        try:
            payload = self._state_store.load_music_state(self._resolve_user_id())
            volume_value = payload.get("volume", self._default_volume)
            try:
                volume_int = max(0, min(100, int(volume_value)))
            except (TypeError, ValueError):
                volume_int = self._default_volume
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("Failed to load persisted music state: %r", exc)
            volume_int = self._default_volume

        self._state.volume = volume_int
        self._state.now_playing = None
        self._state.is_playing = False
        self._state.queue = []
        self.invalidate_snapshot()

    def persist(self, snapshot: PlayerState | None = None) -> None:
        if self._state_store is None:
            return

        target = snapshot or self.snapshot()

        queue_payload: list[dict[str, Any]] = []
        for entry in target.queue:
            if isinstance(entry, QueueItem):
                queue_payload.append(entry.model_dump())
                continue
            try:
                queue_payload.append(QueueItem.model_validate(entry).model_dump())
            except (ValidationError, TypeError, ValueError):
                queue_payload.append(
                    {
                        "id": getattr(entry, "id", None),
                        "title": getattr(entry, "title", None),
                    }
                )

        now_playing_payload: dict[str, Any] | None = None
        if target.now_playing is not None:
            if isinstance(target.now_playing, QueueItem):
                now_playing_payload = target.now_playing.model_dump()
            else:
                try:
                    now_playing_payload = QueueItem.model_validate(target.now_playing).model_dump()
                except (ValidationError, TypeError, ValueError):
                    now_playing_payload = {
                        "id": getattr(target.now_playing, "id", None),
                        "title": getattr(target.now_playing, "title", None),
                    }

        payload = {
            "volume": target.volume,
            "queue": queue_payload,
            "now_playing": now_playing_payload,
            "is_playing": target.is_playing,
        }

        try:
            self._state_store.save_music_state(
                user_id=self._resolve_user_id(),
                **payload,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("Failed to persist music state: %r", exc)


# Late import to avoid a hard dependency when persistence is unavailable.
_PersistentStateStore: type | None = None

try:  # pragma: no cover - optional persistence
    from services.persistence.state_store import PersistentStateStore

    _PersistentStateStore = PersistentStateStore
except Exception:  # pragma: no cover - optional in constrained envs
    pass
