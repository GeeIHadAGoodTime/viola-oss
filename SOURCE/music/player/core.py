"""
Music Player Core.

This module contains the main MusicPlayer facade class.

Extracted modules:
- MusicPlayerInitializer: music/player/initializer.py (initialization logic)
- TestModeControlStub: music/player/test_stub.py (test mode stub)
- MusicPlayerFallback: music/player/fallback.py (autoplay fallback handling)
- wait_for_condition: utils/async_helpers.py (utility function)
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

from core.constants import LOCAL_ROOM_ID
from core.json_types import to_json_value
from core.logging_config import get_logger
from diagnostics.playback_metrics import PlaybackMetricsRecorder
from models.player import PlayerState, QueueItem
from music.backends.loader import BackendStrategyLoader
from music.controller.playback_controller import MusicPlaybackController
from music.player.playback_state import PlaybackPhase
from music.queue_config import QueueConfig
from music.resolution.provider_router import Source

from .initializer import MusicPlayerInitializer
from .playback import MusicPlayerEmbeddedPlayback, MusicPlayerPlaybackController
from .state import MusicPlayerQueueManager, MusicPlayerStateManager

if TYPE_CHECKING:
    from music.autoplay_controller import AutoplayController
    from music.backends.base import BackendProgress
    from music.controller.workers import BackgroundResolverWorker
    from music.player_config import PlayerConfigManager
    from music.resolution.provider_router import AsyncBackgroundResolver, CustomResolver
    from music.runtime import PlayerStatePipeline
    from music.runtime.control_surface import PlayerControlService
    from music.runtime.playback_executor import PlaybackExecutor
    from music.runtime.queue_engine import PlaylistQueueEngine
    from music.runtime.state_service import PlayerStateService
    from music.runtime.telemetry_service import RuntimeTelemetryService
    from music.runtime.worker_manager import RuntimeWorkerManager

if TYPE_CHECKING:
    from services.persistence.state_store import PersistentStateStore, get_state_store
else:  # pragma: no cover - persistence is optional in constrained envs
    try:  # Optional dependency: persistence is disabled when unavailable
        from services.persistence.state_store import (
            PersistentStateStore,
            get_state_store,
        )
    except Exception:
        PersistentStateStore = None
        get_state_store = None

# Import extracted modules
from music.backends.base import (
    BaseBackend,
)  # Use canonical BaseBackend for type compatibility
from utils.async_helpers import wait_for_condition

_POSITION_CACHE_TTL = 0.1  # seconds
_QUEUE_FAILURE_TTL_SEC = 900
_QUEUE_BACKPRESSURE_TTL_SEC = 120
_PENDING_START_TIMEOUT_SEC = 2.0

_ROUTER_PUBLIC_MEMBERS = frozenset(
    {
        "allow_test_stream_playback",
        "autoplay",
        "autoplay_controller",
        "clear_queue",
        "enqueue",
        "enqueue_async",
        "enqueue_autoplay",
        "last_queue_error",
        "last_resolution_error",
        "pause",
        "play",
        "play_async",
        "play_item_now",
        "play_next",
        "previous",
        "queue",
        "queue_backpressure_notice",
        "queue_size",
        "remove_from_queue",
        "reorder_queue",
        "resolution_failure_history",
        "resume",
        "run_integrity_check",
        "seek",
        "set_volume",
        "skip",
        "state",
        "state_pipeline",
        "status",
        "stop",
        "use_test_backend",
        "wait_for_condition",
        "wait_for_worker_heartbeat",
        "worker_heartbeat",
    }
)
_ROUTER_INTERNAL_NAMES = frozenset(
    {
        "_create_user_state",
        "_get_or_create_user_state",
        "_hub_state_authority",
        "_is_multi_user_cloud_mode",
        "_logger",
        "_multi_user_router",
        "_resolve_user_id",
        "_router_session_params",
        "_router_shutdown_all",
        "_router_user_lock",
        "_session_mode",
        "_session_user_id",
        "_user_states",
        "set_hub_state_authority",
        "shutdown",
    }
)


def _background_resolver_available() -> bool:
    try:
        importlib.import_module("utils.background_resolver")
    except Exception:
        return False
    return True


BACKGROUND_RESOLVER_AVAILABLE = _background_resolver_available()


class _MusicPlayerFallback(Protocol):
    def trigger_empty_queue_fallback(self, current: QueueItem | None = None) -> None: ...


class _MusicPlayerArtworkHandler(Protocol):
    def on_provider_artwork(self, *args: object, **kwargs: object) -> None: ...


class _MusicPlayerBackendManager(Protocol):
    backend: BaseBackend | None

    def handle_backend_assignment(self, backend: BaseBackend | None) -> None: ...

    def stop_backend_locked(self) -> None: ...

    def restart_backend(self, reason: str) -> None: ...

    def clear_pending_start(self) -> None: ...

    def schedule_pending_start(self, item_id: str | None, *, timeout: float | None = None) -> None: ...

    def configure_backend_state(self, backend: BaseBackend) -> None: ...

    def extract_backend_capabilities(self, backend: BaseBackend) -> dict[str, object]: ...

    def prepare_backend_for_track(self, item: QueueItem) -> None: ...

    def evaluate_backend_health(self, backend: BaseBackend) -> dict[str, object] | None: ...

    def init_backend(self) -> None: ...

    def attach_backend_listeners(self, backend: BaseBackend) -> None: ...


class _ComplianceResult(Protocol):
    """Protocol for compliance check results."""

    is_compliant: bool


class _MusicPlayerComplianceService(Protocol):
    """Protocol for compliance service."""

    def evaluate(self, item: QueueItem, context: object | None = None) -> _ComplianceResult: ...


# ============================================================================
# MusicPlayer Main Class
# ============================================================================


class MusicPlayer:
    """
    Unified MusicPlayer facade with backend selection (VLC or Simple/ffplay).

    Features:
    - Thread-safe queue + worker
    - Typed exceptions
    - Optional on_state_change callback
    - YouTube Music playback via embedded player (control-layer only, no scraping)

    STATE OWNERSHIP (Single Source of Truth):

    Playback phase is tracked by a single PlaybackStateMachine (_playback_sm).
    Legacy boolean attributes (_is_playing, _paused, _user_paused) are thin
    property wrappers that delegate to the state machine.

    State flow:
      Backend event → _sm_transition() or property setter
      → state() reads from _playback_sm → broadcast to UI
    """

    on_state_change: Callable[[PlayerState], None] | None = None

    # ------------------------------------------------------------------ #
    # Playback state properties — delegate to PlaybackStateMachine       #
    # ------------------------------------------------------------------ #

    @property
    def _is_playing(self) -> bool:
        sm = getattr(self, "_playback_sm", None)
        return sm.is_playing if sm is not None else False

    @_is_playing.setter
    def _is_playing(self, value: bool) -> None:
        sm = getattr(self, "_playback_sm", None)
        if sm is None:
            return
        try:
            if value:
                sm.transition(PlaybackPhase.PLAYING, force=True)
            elif sm.phase in (PlaybackPhase.PLAYING, PlaybackPhase.PAUSED):
                sm.transition(PlaybackPhase.STOPPED, force=True)
        except Exception:
            self._logger.debug("State machine transition failed in _is_playing setter (non-critical)")

    @property
    def _paused(self) -> bool:
        sm = getattr(self, "_playback_sm", None)
        return sm.is_paused if sm is not None else False

    @_paused.setter
    def _paused(self, value: bool) -> None:
        sm = getattr(self, "_playback_sm", None)
        if sm is None:
            return
        try:
            if value:
                sm.transition(PlaybackPhase.PAUSED, force=True)
        except Exception:
            self._logger.debug("State machine transition failed in _paused setter (non-critical)")

    # _user_paused remains a plain instance attribute (initialized in initializer.py).
    # It has "sticky" semantics: True after user pause/stop, False on resume/new play.
    # This is distinct from the state machine's phase-tied user_initiated_pause.

    # Core state and configuration attributes
    _settings: PlayerConfigManager | None
    _background_resolver: AsyncBackgroundResolver | None
    _resolver: CustomResolver | None
    _test_mode: bool
    _runtime_config: object
    _config: PlayerConfigManager | None
    _playback_cfg: object
    _backend_settings: object
    _queue_config: QueueConfig
    _max_queue_size: int

    # Backend and infrastructure
    _backend_factory: Callable[[], BaseBackend] | None
    _backend_loader: BackendStrategyLoader
    _backend: BaseBackend | None
    _metrics: object
    _metrics_recorder: PlaybackMetricsRecorder
    _logger: logging.Logger

    # State management
    _state_service: PlayerStateService
    _state: PlayerState
    _cv: threading.Condition
    _playlist: PlaylistQueueEngine
    _queue_engine: PlaylistQueueEngine
    _state_pipeline: PlayerStatePipeline
    _backend_manager: _MusicPlayerBackendManager
    _history: list[QueueItem]
    _backend_assignments: dict[str, object]
    _backend_assignment_lock: threading.RLock

    # Worker and control
    _worker_manager: RuntimeWorkerManager
    _worker_heartbeat: int
    _last_worker_check: float
    _controller: MusicPlaybackController
    _control: PlayerControlService

    # Autoplay
    _autoplay_controller: AutoplayController | None
    autoplay: AutoplayController | None

    # Resolution
    _resolution_failures: list[dict[str, object]]
    _resolution_lock: threading.RLock

    # Integrity monitoring
    _integrity_monitor: object | None

    # Extracted managers (from refactored components)
    _playback_controller: MusicPlayerPlaybackController
    _state_manager: MusicPlayerStateManager
    _queue_mgr: MusicPlayerQueueManager
    _embedded_mgr: MusicPlayerEmbeddedPlayback
    _playback_executor: PlaybackExecutor
    _fallback_handler: _MusicPlayerFallback
    _artwork_handler: _MusicPlayerArtworkHandler

    # Telemetry and monitoring
    _telemetry: RuntimeTelemetryService | None
    _engine_manager: object
    _compliance_service: _MusicPlayerComplianceService

    # Threading
    _lock: threading.RLock

    # Backend state
    _backend_assign_ready: bool
    _backend_name: str
    _backend_restart_count: int
    _user_paused: bool

    # Background worker
    _background_worker: BackgroundResolverWorker | None
    _worker: BackgroundResolverWorker | None  # Legacy alias for _background_worker
    _autoplay_monitor: AutoplayController | None  # Legacy alias for _autoplay_controller

    @property
    def state_pipeline(self) -> PlayerStatePipeline:
        return self._state_pipeline

    @property
    def _queue(self) -> list[QueueItem]:
        """
        Queue accessor - reads directly from canonical source.

        IMPORTANT: This is a computed property, not a stored attribute.
        All queue reads come from PlaylistCursor._upcoming (the canonical source).
        This eliminates the need for sync calls between multiple queue copies.

        During bootstrapping (before _playlist exists), returns an empty list.
        """
        playlist = getattr(self, "_playlist", None)
        if playlist is None:
            return []
        return playlist.upcoming_ref

    @property
    def _pending_skip_tokens(self) -> int:
        return self._state_pipeline.pending_skip_tokens

    @_pending_skip_tokens.setter
    def _pending_skip_tokens(self, value: int) -> None:
        self._state_pipeline.set_pending_skip_tokens(value)

    @staticmethod
    def _is_multi_user_cloud_mode() -> bool:
        """Return True when the singleton should isolate player state per user.

        Keyed on the RUNTIME SURFACE only (cloud deployment). Desktop is
        one-user-per-install (Multi-Tenant Rule): the bootstrap device
        principal IS the user, so the desktop player must never run as the
        cloud multi-tenant router. Keying this on ``phone_mode`` (a phone
        SaaS transport setting, default "cloud" since PHONE-16) or on bare
        ``auth_enabled`` flipped every fresh desktop install into router
        mode, whose ``_resolve_user_id`` categorically rejects the device
        principal — music dead end-to-end while reporting success
        (lane-3 MF-A, 2026-06-09). Cloud surfaces keep per-user isolation
        untouched.
        """
        try:
            from config.settings import settings
        except Exception:
            return False

        deployment = str(
            getattr(settings, "deployment_mode", None) or getattr(settings, "app_surface", "desktop") or "desktop"
        )
        return deployment.strip().lower() == "cloud"

    def _resolve_user_id(self, user_id: str | None = None) -> str:
        from core.user_context import (
            get_current_or_device_user_id,
            get_current_user_id,
            is_desktop_local_principal,
            user_id_or_none,
        )

        multi_user_router = object.__getattribute__(self, "__dict__").get("_multi_user_router", False)

        candidate = user_id_or_none(user_id)
        if candidate is not None:
            if multi_user_router and is_desktop_local_principal(candidate):
                raise LookupError("user_id is required for multi-user music player state")
            return candidate

        session_user_id = object.__getattribute__(self, "__dict__").get("_session_user_id")
        session_user_id = user_id_or_none(session_user_id)
        if session_user_id is not None:
            if multi_user_router and is_desktop_local_principal(session_user_id):
                raise LookupError("user_id is required for multi-user music player state")
            return session_user_id

        if multi_user_router:
            try:
                current_user_id = user_id_or_none(get_current_user_id())
            except LookupError as exc:
                raise LookupError("user_id is required for multi-user music player state") from exc
            if current_user_id is None or is_desktop_local_principal(current_user_id):
                raise LookupError("user_id is required for multi-user music player state")
            return current_user_id

        try:
            resolved = user_id_or_none(get_current_or_device_user_id())
        except LookupError as exc:
            raise LookupError("user_id is required for multi-user music player state") from exc
        if not resolved:
            raise LookupError("user_id is required for multi-user music player state")
        return resolved

    def _create_user_state(self, user_id: str) -> MusicPlayer:
        backend, logger, backend_factory, init_kwargs = object.__getattribute__(
            self,
            "_router_session_params",
        )
        session_kwargs = dict(init_kwargs)
        session_kwargs["_session_mode"] = True
        session_kwargs["_forced_user_id"] = user_id

        try:
            from core.user_context import user_scope
        except Exception:
            user_scope = None

        if user_scope is None:
            session = type(self)(
                backend=backend,
                logger=logger,
                backend_factory=backend_factory,
                **session_kwargs,
            )
        else:
            with user_scope(user_id):
                session = type(self)(
                    backend=backend,
                    logger=logger,
                    backend_factory=backend_factory,
                    **session_kwargs,
                )

        hub_authority = object.__getattribute__(self, "__dict__").get("_hub_state_authority")
        if hub_authority is not None:
            session.set_hub_state_authority(hub_authority)

        callback = object.__getattribute__(self, "__dict__").get("on_state_change")
        if callable(callback):
            session.on_state_change = callback

        return session

    def _get_or_create_user_state(self, user_id: str | None = None) -> MusicPlayer:
        if not object.__getattribute__(self, "__dict__").get("_multi_user_router", False):
            return self

        uid = self._resolve_user_id(user_id)
        states = object.__getattribute__(self, "_user_states")
        existing = states.get(uid)
        if existing is not None:
            return existing

        lock = object.__getattribute__(self, "_router_user_lock")
        with lock:
            existing = states.get(uid)
            if existing is not None:
                return existing
            created = self._create_user_state(uid)
            states[uid] = created
            return created

    def _router_shutdown_all(self, *, join_timeout: float) -> None:
        if not object.__getattribute__(self, "__dict__").get("_multi_user_router", False):
            return

        states = object.__getattribute__(self, "_user_states")
        for session in list(states.values()):
            try:
                session.shutdown(join_timeout=join_timeout)
            except Exception as exc:
                object.__getattribute__(self, "_logger").exception(
                    "Per-user player shutdown failed: %s",
                    exc,
                )
        states.clear()

    def __getattribute__(self, name: str) -> object:
        if name.startswith("__"):
            return object.__getattribute__(self, name)

        internal_dict = object.__getattribute__(self, "__dict__")
        if internal_dict.get("_multi_user_router", False):
            if name in _ROUTER_PUBLIC_MEMBERS:
                session = object.__getattribute__(self, "_get_or_create_user_state")()
                return getattr(session, name)
            if name.startswith("_") and name not in _ROUTER_INTERNAL_NAMES:
                session = object.__getattribute__(self, "_get_or_create_user_state")()
                return getattr(session, name)

        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        if not name.startswith("__") and object.__getattribute__(self, "__dict__").get("_multi_user_router", False):
            if name == "on_state_change":
                object.__setattr__(self, name, value)
                for session in object.__getattribute__(self, "_user_states").values():
                    session.on_state_change = value if callable(value) else None
                return
            if name.startswith("_") and name not in _ROUTER_INTERNAL_NAMES:
                session = object.__getattribute__(self, "_get_or_create_user_state")()
                setattr(session, name, value)
                return

        if name == "_backend" and getattr(self, "_backend_assign_ready", False):
            object.__setattr__(self, name, value)
            manager = getattr(self, "_backend_manager", None)
            if manager is not None and (isinstance(value, BaseBackend) or value is None):
                manager.handle_backend_assignment(value)
            return
        object.__setattr__(self, name, value)

    def __init__(
        self,
        backend: Literal["vlc", "simple"] = "simple",
        logger: logging.Logger | None = None,
        backend_factory: Callable[[], BaseBackend] | None = None,
        *,
        integrity_monitor_enabled: bool | None = None,
        background_resolver_enabled: bool | None = None,
        autoplay_enabled: bool | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the music player using the initialization module."""
        session_mode = bool(kwargs.pop("_session_mode", False))
        forced_user_id = kwargs.pop("_forced_user_id", None)
        multi_user_enabled = bool(kwargs.pop("multi_user_enabled", False))

        object.__setattr__(self, "_session_mode", session_mode)
        object.__setattr__(self, "_session_user_id", forced_user_id)
        object.__setattr__(self, "_multi_user_router", False)
        object.__setattr__(self, "_router_user_lock", threading.RLock())
        object.__setattr__(self, "_user_states", {})
        object.__setattr__(self, "_hub_state_authority", None)

        router_enabled = not session_mode and (multi_user_enabled or self._is_multi_user_cloud_mode())
        if router_enabled:
            effective_logger = logger or get_logger("music.player")
            object.__setattr__(self, "_logger", effective_logger)
            object.__setattr__(self, "_multi_user_router", True)
            object.__setattr__(
                self,
                "_router_session_params",
                (
                    backend,
                    logger,
                    backend_factory,
                    {
                        "integrity_monitor_enabled": integrity_monitor_enabled,
                        "background_resolver_enabled": background_resolver_enabled,
                        "autoplay_enabled": autoplay_enabled,
                        **kwargs,
                    },
                ),
            )
            return

        initializer = MusicPlayerInitializer(self)
        initializer.initialize_player(
            backend=backend,
            logger=logger,
            backend_factory=backend_factory,
            integrity_monitor_enabled=integrity_monitor_enabled,
            background_resolver_enabled=background_resolver_enabled,
            autoplay_enabled=autoplay_enabled,
            **kwargs,
        )
        try:
            initial_user_id = self._resolve_user_id()
        except LookupError:
            initial_user_id = ""
        if initial_user_id:
            self._user_states[initial_user_id] = self

    def state(self) -> PlayerState:
        """Get state using state manager."""
        state = self._state_manager.state()
        if not isinstance(state, PlayerState):
            raise TypeError(f"MusicPlayerStateManager.state() returned unexpected type: {type(state).__name__}")
        return state

    def status(self) -> dict[str, object]:
        """Get status using state manager."""
        return self._state_manager.status()

    def wait_for_condition(
        self,
        predicate: Callable[[], bool],
        timeout: float = 5.0,
        *,
        poll_interval: float = 0.05,
    ) -> bool:
        """Wait for condition using utility function."""
        return wait_for_condition(predicate, timeout, poll_interval=poll_interval)

    def worker_heartbeat(self) -> int:
        """Return the current worker heartbeat counter."""
        return self._worker_manager.heartbeat()

    def wait_for_worker_heartbeat(self, last_seen: int, timeout: float = 0.5) -> bool:
        """Wait for the worker heartbeat counter to advance beyond ``last_seen``."""
        return self._worker_manager.wait_for_heartbeat(last_seen, timeout=timeout)

    def _process_next_track_for_tests(self) -> bool:
        """Expose deterministic worker iteration for integration tests."""
        return self._worker_manager.process_next_track_for_tests()

    def last_resolution_error(self) -> dict[str, object] | None:
        """Get last resolution error using state manager."""
        return self._state_manager.last_resolution_error()

    def resolution_failure_history(self) -> list[dict[str, object]]:
        """Get resolution failure history."""
        return self._state_manager.resolution_failure_history()

    def play(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        return self._control.play(
            query,
            source,
            emit=emit,
            metadata=metadata,
            interrupt=interrupt,
        )

    async def play_async(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
        interrupt: bool = True,
    ) -> QueueItem:
        return await self._control.play_async(
            query,
            source,
            emit=emit,
            metadata=metadata,
            interrupt=interrupt,
        )

    def enqueue(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> QueueItem | None:
        return self._control.enqueue(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    async def enqueue_async(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> QueueItem | None:
        return await self._control.enqueue_async(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    def play_next(
        self,
        query: str,
        source: Source | None = None,
        *,
        emit: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> QueueItem | None:
        return self._control.play_next(
            query,
            source,
            emit=emit,
            metadata=metadata,
        )

    def enqueue_autoplay(self, query: str, *, metadata: dict[str, object] | None = None) -> QueueItem | None:
        return self._control.enqueue_autoplay(query, metadata=metadata)

    def queue(self) -> list[QueueItem]:
        return self._control.queue()

    def queue_size(self) -> int:
        """Get queue size."""
        return self._queue_mgr.get_queue_size()

    def allow_test_stream_playback(self, enabled: bool = True) -> None:
        """Allow direct stream URLs to bypass TOS enforcement in test mode."""
        self._control.allow_test_stream_playback(enabled)

    def use_test_backend(self, backend: BaseBackend) -> None:
        """Inject a deterministic backend for integration testing."""
        self._control.use_test_backend(backend)

    def clear_queue(self) -> None:
        self._control.clear_queue()

    def remove_from_queue(self, item_id: str) -> None:
        self._control.remove_from_queue(item_id)

    def reorder_queue(self, from_index: int, to_index: int) -> None:
        self._control.reorder_queue(from_index, to_index)

    def play_item_now(self, item_id: str) -> None:
        self._control.play_item_now(item_id)

    def _enqueue_resolved_item(
        self,
        item: QueueItem,
        *,
        play_immediately: bool = False,
        emit: bool = True,
    ) -> QueueItem:
        return self._control.enqueue_resolved_item(
            item,
            play_immediately=play_immediately,
            emit=emit,
        )

    def set_volume(self, level: int) -> int:
        """Set volume using playback controller."""
        return self._playback_controller.set_playback_volume(level)

    def set_hub_state_authority(self, hub_authority: object) -> None:
        """Set reference to hub state authority for direct state updates."""
        if object.__getattribute__(self, "__dict__").get("_multi_user_router", False):
            object.__setattr__(self, "_hub_state_authority", hub_authority)
            for session in object.__getattribute__(self, "_user_states").values():
                session.set_hub_state_authority(hub_authority)
            self._logger.debug("Hub state authority connected to multi-user music player")
            return

        self._hub_state_authority = hub_authority
        self._logger.debug("Hub state authority connected to music player")

    def pause(self) -> None:
        """Pause playback using playback controller."""
        self._playback_controller.pause_playback()

    def resume(self) -> None:
        """Resume playback using playback controller."""
        self._playback_controller.resume()

    def stop(self) -> None:
        """Stop playback using playback controller."""
        self._playback_controller.stop_playback()

    def _trigger_empty_queue_fallback(self, current: QueueItem | None = None) -> None:
        """Trigger empty queue fallback using fallback handler."""
        self._fallback_handler.trigger_empty_queue_fallback(current)

    def skip(self) -> dict[str, object]:
        """Skip to next track using the active control surface."""
        return self._control.skip()

    def previous(self) -> None:
        """Skip to previous track using playback controller."""
        self._playback_controller.previous_track()

    def seek(self, position_ms: int) -> None:
        """Seek to a position in the current track.

        Args:
            position_ms: Target position in milliseconds.
        """
        self._playback_controller.seek(position_ms)

    def shutdown(self, *, join_timeout: float = 1.0) -> None:
        if object.__getattribute__(self, "__dict__").get("_multi_user_router", False):
            self._router_shutdown_all(join_timeout=join_timeout)
            return

        self._worker_manager.shutdown(join_timeout=join_timeout)
        if self._background_worker:
            self._background_worker.stop()
        self._backend_manager.stop_backend_locked()

    def last_queue_error(self) -> dict[str, object] | None:
        last_error = self._telemetry.last_queue_error()
        if last_error is None:
            return None
        return {k: v for k, v in last_error.items()}

    def queue_backpressure_notice(self) -> dict[str, object] | None:
        with self._lock:
            notice = self._playlist.backpressure_notice()
            if not notice:
                return None
            if time.time() - notice["timestamp"] > _QUEUE_BACKPRESSURE_TTL_SEC:
                self._playlist.clear_backpressure()
                return None
            return notice

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _emit(self) -> None:
        # Guard against uninitialized or cleared state (e.g., during stop)
        if self._playlist is None or self._backend_manager is None:
            self._logger.debug("_emit: skipped - player not initialized")
            return  # Silent OK - player not fully initialized or being torn down
        snapshot = self.state()
        self._dispatch_state_hub_update(snapshot)
        self._logger.debug(
            "_emit: volume=%s is_playing=%s has_callback=%s has_telemetry=%s",
            snapshot.volume,
            snapshot.is_playing,
            self.on_state_change is not None,
            self._telemetry is not None,
        )
        with self._lock:
            try:
                playlist_current_id = self._playlist.current_id()
                upcoming_count = self._playlist.upcoming_count()
                backend = self._backend_manager.backend
            except AttributeError:
                return  # Silent OK - playlist/backend manager in invalid state
        if self._telemetry:
            self._telemetry.emit_player_state(
                snapshot=snapshot,
                playlist_current_id=playlist_current_id,
                upcoming_count=upcoming_count,
                backend=backend,
                backend_name=self._backend_name,
                on_state_change=self.on_state_change,
            )
        elif self.on_state_change:
            try:
                self.on_state_change(snapshot)
            except Exception as e:
                self._logger.exception("State change callback failed: %s", e)

    def _dispatch_state_hub_update(self, snapshot: PlayerState) -> None:
        """Mirror the local player snapshot into StateHub for selectors."""
        try:
            from core.state_hub import UpdatePlayerState, get_state_hub

            hub = get_state_hub()
            if not hub.is_running:
                self._logger.debug("_dispatch_state_hub_update: skipped - StateHub not running")
                return
            hub.dispatch(
                UpdatePlayerState(
                    room_id=LOCAL_ROOM_ID,
                    player_state=snapshot,
                )
            )
        except Exception as exc:
            self._logger.exception("StateHub player update failed: %s", exc)

    def run_integrity_check(self) -> None:
        """Run a single integrity pass. Exposed for soak/integration tests."""
        self._worker_manager.run_integrity_check()

    def _restart_backend(self, *, reason: str) -> None:
        """Restart backend using backend manager."""
        self._backend_manager.restart_backend(reason)

    def _set_now_playing_locked(self, item: QueueItem | None) -> None:
        """Delegate now_playing mutations to the state pipeline."""
        self._state_pipeline.set_now_playing(item)

        # Also update hub state authority if available (for authoritative changes)
        hub = getattr(self, "_hub_state_authority", None)
        if hub is not None:
            try:
                hub.update_canonical_now_playing(item)
            except Exception as e:
                self._logger.exception("Hub state update failed (non-critical): %s", e)
                # Don't let hub issues block playback

    def _update_state_queue_locked(self) -> None:
        """
        DEPRECATED (2026-01-12): No longer needed.

        QUEUE ARCHITECTURE: Queue mutations automatically invalidate the
        snapshot cache via PlaylistQueueEngine._invalidate(). This method
        is kept for backward compatibility but does nothing.

        All callers should be removed - queue sync is automatic now.
        """
        pass  # No-op - queue mutations auto-invalidate

    def _promote_current_locked(
        self,
        *,
        set_is_playing: bool = True,
        mark_playing: bool = False,
    ) -> tuple[int, str | None] | None:
        """Promote playlist.current via the state pipeline.

        Note: _queue is now a computed property, no sync assignment needed.
        """
        result = self._state_pipeline.promote_current(
            set_is_playing=set_is_playing,
            mark_playing=mark_playing,
        )
        # Dual-write: transition state machine to match set_is_playing
        if set_is_playing is not None:
            sm = getattr(self, "_playback_sm", None)
            if sm is not None:
                try:
                    phase = PlaybackPhase.PLAYING if set_is_playing else PlaybackPhase.LOADING
                    sm.transition(phase, force=True)
                except Exception:
                    self._logger.debug("State machine transition failed (non-critical)")
        return result

    def _clear_pending_start_locked(self) -> None:
        """Reset pending start tracking when worker has claimed the track."""
        self._backend_manager.clear_pending_start()

    def _ensure_playlist_coherent_locked(self) -> None:
        """Ensure playlist coherence using state manager."""
        self._state_manager.ensure_playlist_coherent_locked()

    def _configure_backend_state(self, backend: BaseBackend) -> None:
        self._backend_manager.configure_backend_state(backend)

    def _extract_backend_capabilities(self, backend: BaseBackend) -> dict[str, object]:
        return self._backend_manager.extract_backend_capabilities(backend)

    def _prepare_backend_for_track(self, item: QueueItem) -> None:
        self._backend_manager.prepare_backend_for_track(item)

    def _stop_backend_locked(self) -> None:
        self._backend_manager.stop_backend_locked()

    def _record_resolution_failure(self, query: str, source: Source, exc: Exception) -> None:
        if self._telemetry is not None:
            self._telemetry.record_resolution_failure(query=query, source=source, exc=exc)

    def _record_queue_failure(self, item: QueueItem, *, stage: str, metadata: dict[str, object] | None = None) -> None:
        if self._telemetry is not None:
            metadata_payload = {str(k): to_json_value(v) for k, v in metadata.items()} if metadata is not None else None
            self._telemetry.record_queue_failure(item, stage=stage, metadata=metadata_payload)

    def _record_resolution_event(
        self,
        *,
        query: str,
        source: str,
        success: bool,
        duration_ms: float,
        item: QueueItem | None = None,
        error: str | None = None,
    ) -> None:
        """Record resolution telemetry and audit events."""
        provider = "legacy"
        if item is not None and hasattr(item, "provider") and item.provider:
            provider = item.provider

        # Record telemetry
        if self._telemetry is not None and hasattr(self._telemetry, "record_resolution"):
            self._telemetry.record_resolution(provider, duration_ms, success, source)

        # Record error if failed
        if not success and error and self._telemetry is not None and hasattr(self._telemetry, "record_error"):
            self._telemetry.record_error(provider, error)

        # Record audit
        if hasattr(self, "_audit") and self._audit is not None and hasattr(self._audit, "record_resolution"):
            self._audit.record_resolution(
                provider=provider,
                query=query,
                status="success" if success else "failed",
                latency_ms=duration_ms,
                source=source,
                error=error,
            )

    def _record_playback_event(
        self,
        *,
        item: QueueItem,
        duration_ms: float,
        success: bool,
        error: str | None = None,
    ) -> None:
        """Record playback telemetry and audit events."""
        provider = "legacy"
        if hasattr(item, "provider") and item.provider:
            provider = item.provider

        # Record telemetry
        if self._telemetry is not None and hasattr(self._telemetry, "record_playback"):
            self._telemetry.record_playback(provider, duration_ms, success)

        # Record error if failed
        if not success and error and self._telemetry is not None and hasattr(self._telemetry, "record_error"):
            self._telemetry.record_error(provider, error)

        # Record audit
        if hasattr(self, "_audit") and self._audit is not None and hasattr(self._audit, "record_playback"):
            self._audit.record_playback(
                provider=provider,
                track_id=item.video_id if hasattr(item, "video_id") else None,
                status="success" if success else "failed",
                gap_ms=duration_ms,
                error=error,
            )

    def _on_backend_progress(self, progress: BackendProgress) -> None:
        """Handle backend progress updates using state manager."""
        self._state_manager.handle_backend_progress(progress)

    def _worker_loop(self) -> None:
        raise RuntimeError("Worker loop is managed by RuntimeWorkerManager")

    def _evaluate_backend_health(self, backend: BaseBackend) -> dict[str, object] | None:
        """Evaluate backend health using backend manager."""
        return self._backend_manager.evaluate_backend_health(backend)

    def _get_browser_backend_display_name(self, item: QueueItem) -> str:
        """Get display name for browser backend."""
        return self._embedded_mgr.get_browser_backend_display_name(item)

    def _play_embedded_webview(self, item: QueueItem) -> bool:
        """Play item using embedded webview."""
        return self._embedded_mgr.play_embedded_webview(item)

    def _play_embedded_iframe_webview(self, item: QueueItem) -> bool:
        """Play item using embedded iframe webview."""
        return self._embedded_mgr.play_embedded_iframe_webview(item)

    def _play_external_browser(self, item: QueueItem) -> bool:
        """Play item using external browser."""
        return self._embedded_mgr.play_external_browser(item)

    def _start_and_monitor_playback(
        self,
        item: QueueItem,
        token: tuple[int, str | None],
    ) -> float | bool:
        return self._playback_executor.start_and_monitor_playback(item, token)

    def _init_backend(self) -> None:
        self._backend_manager.init_backend()

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.shutdown()
        except Exception as e:
            # Can't use self._logger in __del__ as it may be already cleaned up
            try:
                get_logger(__name__).exception("Shutdown in destructor failed: %s", e)
            except Exception as log_err:
                # Can't log if logging itself fails during cleanup

                logger = get_logger(__name__)
                logger.exception("Destructor cleanup failed: %s", log_err)

    def _attach_backend_listeners(self, backend: BaseBackend) -> None:
        """Attach backend listeners using backend manager."""
        self._backend_manager.attach_backend_listeners(backend)

    def _on_provider_artwork(self, *args, **_kwargs) -> None:
        """Handle provider artwork updates using artwork handler."""
        self._artwork_handler.on_provider_artwork(*args, **_kwargs)

    def _on_engine_finished(self, source: str = "engine") -> None:
        """Handle engine finished event.

        Note: _queue is now a computed property, no sync assignment needed.
        """
        with self._cv:
            self._state_pipeline.handle_engine_finished()
            # Dual-write: engine finished → IDLE
            sm = getattr(self, "_playback_sm", None)
            if sm is not None:
                try:
                    sm.transition(PlaybackPhase.IDLE, force=True)
                except Exception:
                    self._logger.debug("State machine transition to IDLE failed (non-critical)")
            self._cv.notify_all()
