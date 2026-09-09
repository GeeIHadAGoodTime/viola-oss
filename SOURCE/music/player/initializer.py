"""
Music Player Initializer.

Handles the complex initialization logic for MusicPlayer.
Extracted from music/player/core.py to reduce file size.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from core.constants import TIMEOUT_VERY_LONG
from core.logging_config import get_logger

if TYPE_CHECKING:
    import logging

    from config.player_runtime import PlayerRuntimeConfig
    from music.backends.base import BaseBackend
    from music.player_config import PlayerConfigManager
    from music.resolution.provider_router import AsyncBackgroundResolver


def _initial_volume(cfg: object | None) -> int:
    """Volume the player starts at, taken from the user's own setting.

    ``default_music_volume`` is what the Settings UI writes and what Viola's
    agent sets when a user says "start music at 40". It lives in
    SettingsManager, which is the runtime truth for user preferences —
    AppConfig's ``default_volume`` is only the seed used before any user
    preference exists (see config/settings.py's note on the field).

    This used to read ``cfg.default_volume`` off AppConfig, which is never
    re-synced from SettingsManager, so the user's saved value was written,
    confirmed, and then ignored for the life of the process.
    """
    seed = getattr(cfg, "default_volume", 50) if cfg else 50
    try:
        from ui.settings_manager import get_settings_manager

        stored = get_settings_manager().get("default_music_volume", seed)
    except Exception:  # noqa: BLE001, RUF100 - a volume seed must never block player startup
        get_logger("music.player").debug(
            "Falling back to seed volume; SettingsManager unavailable",
            exc_info=True,
        )
        return int(seed)

    try:
        return max(0, min(100, int(stored)))
    except (TypeError, ValueError):
        return int(seed)


class MusicPlayerInitializer:
    """Handles the complex initialization logic for MusicPlayer."""

    def __init__(self, player_instance):
        """Initialize the initializer with a reference to the player."""
        self.player = player_instance

    def initialize_player(
        self,
        backend: Literal["vlc", "simple"],
        logger: logging.Logger | None,
        backend_factory: Callable[[], BaseBackend] | None,
        integrity_monitor_enabled: bool | None,
        background_resolver_enabled: bool | None,
        autoplay_enabled: bool | None,
        **kwargs: object,
    ) -> None:
        """Initialize the music player with all its complex setup logic."""

        # Extract basic configuration
        backend_factory_override = kwargs.pop("backend_factory", None)
        if callable(backend_factory_override):
            backend_factory = cast("Callable[[], BaseBackend]", backend_factory_override)
        cfg = cast("PlayerConfigManager | None", kwargs.get("config"))
        settings_obj = cast("PlayerConfigManager | None", kwargs.get("settings"))

        # Setup basic attributes
        self._setup_basic_attributes(cfg, settings_obj, logger, kwargs)

        # Setup test mode detection
        self._setup_test_mode_detection(cfg, settings_obj, kwargs)

        # Build runtime configuration
        runtime_config = self._build_runtime_config(
            test_mode=self.player._test_mode,
            autoplay_enabled=autoplay_enabled,
            background_resolver_enabled=background_resolver_enabled,
            integrity_monitor_enabled=integrity_monitor_enabled,
        )

        # Extract runtime toggles
        toggles = runtime_config.toggles
        autoplay_enabled = toggles.autoplay_enabled
        background_resolver_enabled = toggles.background_resolver_enabled
        integrity_monitor_enabled = toggles.integrity_monitor_enabled

        # Setup state persistence
        state_store = self._setup_state_persistence()

        # Setup configuration objects
        self._setup_config_objects(runtime_config, cfg, settings_obj)

        # Setup backend infrastructure
        self._setup_backend_infrastructure(backend_factory)

        # Setup monitoring and metrics
        self._setup_monitoring_and_metrics()

        # Setup core services
        self._setup_core_services(runtime_config, state_store)

        # Setup playlist and queue
        self._setup_playlist_and_queue()

        # Setup state pipeline
        self._setup_state_pipeline()

        # Setup backend management
        self._setup_backend_management(backend)

        # Setup engine manager (provider-based playback routing)
        self._setup_engine_manager()

        # Setup worker infrastructure
        self._setup_worker_infrastructure()

        # Setup control services
        self._setup_control_services(runtime_config)

        # Setup autoplay
        self._setup_autoplay(autoplay_enabled, runtime_config)

        # Setup PlaybackSessionController (playlists work regardless of autoplay)
        self._setup_session_controller()

        # Wire up idle timeout callback to reset autoplay context (Product Decision 2026-01-11)
        if hasattr(self.player, "autoplay_controller") and self.player.autoplay_controller is not None:
            self.player._state_service.set_idle_timeout_callback(self.player.autoplay_controller.reset_context)

        # Setup resolution infrastructure
        self._setup_resolution_infrastructure()

        # Setup integrity monitoring
        self._setup_integrity_monitoring(integrity_monitor_enabled)

        # Initialize managers (extracted components)
        self._initialize_extracted_managers()

        # Initialize fallback handler
        from .fallback import MusicPlayerFallback

        self.player._fallback_handler = MusicPlayerFallback(self.player)

        # Initialize artwork handler
        from .state import MusicPlayerArtworkHandler

        self.player._artwork_handler = MusicPlayerArtworkHandler(self.player)

        # Setup legacy attribute aliases for backward compatibility
        self._setup_legacy_attribute_aliases()

        # Setup backend assignment ready flag
        self.player._backend_assign_ready = True

        # Eagerly initialize the backend so health checks report "ok"
        # instead of "no_backend" before first playback.
        try:
            blm = getattr(self.player, "_backend_lifecycle_manager", None)
            if blm is not None and hasattr(blm, "init_backend"):
                blm.init_backend()
        except Exception as exc:
            self.player._logger.debug("Eager backend init skipped: %s", exc)

    def _setup_basic_attributes(
        self,
        cfg: PlayerConfigManager | None,
        settings_obj: PlayerConfigManager | None,
        logger: logging.Logger | None,
        kwargs: dict[str, object],
    ) -> None:
        """Setup basic player attributes."""
        self.player._settings = settings_obj or cfg
        self.player._cv = threading.Condition()
        self.player._lock = threading.RLock()  # Create lock early for telemetry service
        self.player._logger = logger or get_logger("music.player")
        self.player._backend = None
        resolver = cast("AsyncBackgroundResolver | None", kwargs.get("background_resolver"))
        self.player._background_resolver = resolver if kwargs.get("background_resolver_enabled") else None
        self.player._resolver = kwargs.get("resolver")

        # Playback state machine — single source of truth for playback phase.
        # The _is_playing, _paused, _user_paused attributes are now @property
        # wrappers on MusicPlayer that delegate to this state machine.
        from music.player.playback_state import PlaybackStateMachine

        self.player._playback_sm = PlaybackStateMachine()

        # _user_paused is a sticky flag (True after user pause/stop, cleared on resume/play).
        # It remains a plain instance attribute — not derived from the state machine.
        self.player._user_paused = False
        self.player._paused_current = None
        self.player._current_url = None  # URL of currently playing item
        # Note: _queue is now a computed property reading from PlaylistCursor._upcoming
        # No initialization needed - the property returns [] before _playlist exists
        self.player._queue_index = 0
        self.player._volume = _initial_volume(cfg)
        self.player._position_ms = 0
        self.player._duration_ms = 0  # Track duration (updated via WebSocket from YouTube iframe)
        # Use deque with maxlen to automatically limit history size to 10
        self.player._history = deque(maxlen=10)
        self.player._telemetry = None  # Set properly in _setup_telemetry_service
        self.player._recent_queue_failures = []

    def _setup_test_mode_detection(
        self,
        cfg: PlayerConfigManager | None,
        settings_obj: PlayerConfigManager | None,
        kwargs: dict[str, object],
    ) -> None:
        """Setup test mode detection logic."""
        from config.settings import settings

        test_mode_hint = any(
            [
                bool(kwargs.get("test_mode")),
                settings.pytest_in_progress,
                "pytest" in sys.modules,
            ]
        )
        self.player._test_mode = test_mode_hint

    def _build_runtime_config(
        self,
        *,
        test_mode: bool,
        autoplay_enabled: bool | None,
        background_resolver_enabled: bool | None,
        integrity_monitor_enabled: bool | None,
    ) -> PlayerRuntimeConfig:
        """Build runtime configuration."""
        from config.player_runtime import build_runtime_config

        return build_runtime_config(
            test_mode=test_mode,
            autoplay_enabled=autoplay_enabled,
            background_resolver_enabled=background_resolver_enabled,
            integrity_monitor_enabled=integrity_monitor_enabled,
        )

    def _setup_state_persistence(self):
        """Setup state persistence infrastructure."""
        if self.player._test_mode:
            return None

        try:
            from services.persistence.state_store import get_state_store
        except Exception:  # pragma: no cover - optional dependency
            return None

        try:
            return get_state_store()
        except Exception as exc:
            self.player._logger.debug("Persistent state store unavailable: %r", exc)
            return None

    def _setup_config_objects(
        self,
        runtime_config: PlayerRuntimeConfig,
        cfg: PlayerConfigManager | None,
        settings_obj: PlayerConfigManager | None,
    ) -> None:
        """Setup configuration objects."""
        self.player._runtime_config = runtime_config
        self.player._config = runtime_config.player_config
        self.player._playback_cfg = self.player._config.playback
        self.player._backend_settings = self.player._config.backend
        self.player._queue_config = runtime_config.queue_config
        self.player._max_queue_size = 10000

    def _setup_backend_infrastructure(self, backend_factory: Callable[[], BaseBackend] | None) -> None:
        """Setup backend infrastructure."""
        from music.backends.loader import BackendStrategyLoader

        self.player._backend_factory = backend_factory
        self.player._backend_loader = BackendStrategyLoader(
            logger=self.player._logger,
            backend_settings=self.player._backend_settings,
        )

    def _setup_monitoring_and_metrics(self) -> None:
        """Setup monitoring and metrics."""
        from diagnostics.playback_metrics import PlaybackMetricsRecorder
        from diagnostics.runtime_metrics import get_runtime_metrics

        self.player._metrics = get_runtime_metrics()
        self.player._metrics_recorder = PlaybackMetricsRecorder()

        # Initialize telemetry service (required for test mode too)
        # Note: RuntimeTelemetryService will be fully initialized after queue_engine exists
        self.player._telemetry = None

    def _setup_core_services(self, runtime_config: PlayerRuntimeConfig, state_store: object) -> None:
        """Setup core services."""
        from config.settings import settings
        from music.runtime.state_service import PlayerStateService

        default_volume = int(getattr(self.player, "_volume", 50))

        self.player._state_service = PlayerStateService(
            logger=self.player._logger,
            default_volume=default_volume,
            test_mode=self.player._test_mode,
            state_store=state_store,
        )

        # Restore state from persistence (PRD Exit Criteria: Queue survives restart)
        if settings.restore_queue_on_startup:
            self.player._state_service.restore_full_state()
        else:
            self.player._state_service.restore_volume_only()
            # Product Decision (2026-01-13): Clear stale queue data when not restoring
            # This prevents stale context from accumulating across sessions
            if state_store is not None and hasattr(state_store, "clear_stale_queue"):
                try:
                    try:
                        from core.user_context import get_current_user_id

                        resolved_user_id = get_current_user_id()
                    except LookupError:
                        from core.user_context import get_device_user_id

                        # mt-ok: canonical desktop pre-auth fallback — device id
                        # is the single-listener boundary on a fresh machine.
                        resolved_user_id = get_device_user_id()
                    state_store.clear_stale_queue(resolved_user_id)
                except Exception as e:
                    self.player._logger.debug("clear_stale_queue failed (non-critical): %s", e)

        self.player._state = self.player._state_service.state

    def _setup_playlist_and_queue(self) -> None:
        """Setup playlist and queue infrastructure."""
        from music.runtime.queue_engine import PlaylistQueueEngine

        self.player._playlist = PlaylistQueueEngine(
            state_service=self.player._state_service,
            queue_config=self.player._queue_config,
            history_size=10,
            logger=self.player._logger,
        )
        self.player._queue_engine = self.player._playlist

        # QUEUE ARCHITECTURE (2026-01-12): Wire up canonical queue source
        # PlayerStateService now reads queue directly from PlaylistQueueEngine
        self.player._state_service.set_queue_source(self.player._playlist)

    def _setup_state_pipeline(self) -> None:
        """Setup state pipeline."""
        from music.runtime.state_pipeline import PlayerStatePipeline

        self.player._state_pipeline = PlayerStatePipeline(
            state_service=self.player._state_service,
            playlist=self.player._playlist,
            logger=self.player._logger,
            test_mode=self.player._test_mode,
        )

        # Now that we have playlist/queue_engine, initialize telemetry service
        self._setup_telemetry_service()

    def _setup_telemetry_service(self) -> None:
        """Setup telemetry service with all dependencies."""
        from typing import Any

        from music.runtime.telemetry_service import RuntimeTelemetryService

        debug_emitter: Callable[[str, dict[str, Any], str], None] | None = None
        try:
            from ui.qt_native.debug_events import emit_debug_event

            # Wrap emit_debug_event to match expected signature: (name, payload, source)
            def adapted_emit(name: str, payload: dict[str, Any], source: str) -> None:
                emit_debug_event(name, payload, source=source)

            debug_emitter = adapted_emit
        except Exception:
            pass

        self.player._telemetry = RuntimeTelemetryService(
            state_service=self.player._state_service,
            queue_engine=self.player._playlist,
            lock=self.player._lock,
            logger=self.player._logger,
            emit_debug_event=debug_emitter,
            runtime_metrics=self.player._metrics,
            playback_metrics=self.player._metrics_recorder,
        )

    def _setup_backend_management(self, backend: Literal["vlc", "simple", "qt_media"]) -> None:
        """Setup backend management."""
        from config.settings import settings

        from .backends import MusicPlayerBackendManager

        self.player._backend_manager = MusicPlayerBackendManager(self.player)
        self.player._backend_assignments = {}
        self.player._backend_assignment_lock = threading.RLock()
        # Prefer settings.player_backend (supports VIOLA_PLAYER_BACKEND env var)
        effective_backend = getattr(settings, "player_backend", None) or backend
        self.player._backend_name = effective_backend
        self.player._backend_restart_count = 0  # Initialize restart count

    def _setup_engine_manager(self) -> None:
        """Create PlaybackEngineManager for provider-based playback routing.

        This wires up the engine manager that BrowserPlaybackEngine (and other
        provider engines) need.  Without it, ``_get_engine_manager()`` in the
        playback executor returns ``None`` and browser-native playback cannot
        start.
        """
        try:
            from playback.engine_manager import PlaybackEngineManager
            from playback.feature_flags import PlaybackFeatureFlags

            flags = PlaybackFeatureFlags.from_settings()
            manager = PlaybackEngineManager(
                feature_flags=flags,
                controllers={},
            )
            self.player._engine_manager = manager
            self.player._playback_manager = manager  # alias used by some code paths
            self.player._logger.info("PlaybackEngineManager initialised (engines=%s)", list(manager._engines))
        except Exception as exc:
            self.player._logger.warning("PlaybackEngineManager unavailable: %r", exc)
            self.player._engine_manager = None
            self.player._playback_manager = None

    def _setup_worker_infrastructure(self) -> None:
        """Setup worker infrastructure."""
        from music.controller.workers import BackgroundResolverWorker
        from music.runtime.backend_manager import BackendLifecycleManager
        from music.runtime.playback_executor import PlaybackExecutor
        from music.runtime.worker_manager import RuntimeWorkerManager

        queue_failure_ttl = getattr(self.player, "_queue_config", None)
        if queue_failure_ttl:
            queue_failure_ttl = getattr(queue_failure_ttl, "failure_ttl_sec", 900)
        else:
            queue_failure_ttl = 900

        self.player._worker_manager = RuntimeWorkerManager(
            player=self.player,
            logger=self.player._logger,
            queue_failure_ttl=queue_failure_ttl,
        )

        # Create BackendLifecycleManager for PlaybackExecutor (it needs the full lifecycle manager)
        backend_loader = getattr(self.player, "_backend_loader", None)
        # BackendLifecycleManager expects RuntimeMetrics (not RuntimeTelemetryService)
        # for methods like record_music_heartbeat_miss, record_music_backend_restart, etc.
        telemetry = getattr(self.player, "_metrics", None)
        self.player._backend_lifecycle_manager = BackendLifecycleManager(
            player=self.player,
            backend_loader=backend_loader,
            telemetry=telemetry,
            logger=self.player._logger,
            pending_start_timeout=TIMEOUT_VERY_LONG,
        )

        # Create playback executor - required for worker to start playback
        self.player._playback_executor = PlaybackExecutor(
            player=self.player,
            backend_manager=self.player._backend_lifecycle_manager,
            logger=self.player._logger,
        )

        resolver = getattr(self.player, "_resolver", None)
        if resolver:
            self.player._background_worker = BackgroundResolverWorker(resolver)
        else:
            self.player._background_worker = None

        self.player._worker_heartbeat = 0
        self.player._last_worker_check = time.time()

        # Start the worker manager - this enables the playback worker thread that
        # triggers PlaybackExecutor.start_and_monitor_playback for actual playback.
        # Critical fix: Without this call, backend playback never starts.
        playback_enabled = not self.player._test_mode
        integrity_enabled = False  # Will be configured later via _setup_integrity_monitoring
        self.player._worker_manager.start(
            playback_enabled=playback_enabled,
            integrity_enabled=integrity_enabled,
        )
        self.player._logger.info(
            "Worker manager started: playback_enabled=%s integrity_enabled=%s",
            playback_enabled,
            integrity_enabled,
        )

    def _setup_control_services(self, runtime_config: PlayerRuntimeConfig) -> None:
        """Setup control services."""
        from music.controller.playback_controller import MusicPlaybackController

        self._setup_provider_infrastructure()

        provider_router = getattr(self.player, "_provider_router", None)
        compliance_policy = getattr(self.player, "_compliance_policy", None)
        backend_loader = getattr(self.player, "_backend_loader", None)
        metrics = getattr(self.player, "_metrics", None)

        if provider_router and compliance_policy and backend_loader:
            self.player._controller = MusicPlaybackController(
                logger=self.player._logger,
                playlist=self.player._playlist,
                provider_router=provider_router,
                compliance_policy=compliance_policy,
                backend_loader=backend_loader,
                metrics=metrics,
            )
        else:
            self.player._controller = None

        self._setup_player_control_service(runtime_config)

    def _setup_provider_infrastructure(self) -> None:
        """Setup provider router and compliance policy."""
        from music.compliance.ytm_policy import YouTubeCompliancePolicy
        from music.resolution.error_handler import ResolutionErrorHandler
        from music.resolution.provider_router import ProviderRouter

        logger = self.player._logger
        error_handler = ResolutionErrorHandler(logger=logger)

        def record_resolution_failure(query: str, source, exc: Exception) -> None:
            if hasattr(self.player, "_resolution_failures"):
                self.player._resolution_failures.append(
                    {
                        "query": query,
                        "source": str(source),
                        "error": str(exc),
                        "timestamp": time.time(),
                    }
                )

        resolver = getattr(self.player, "_resolver", None)
        background_resolver = getattr(self.player, "_background_resolver", None)
        background_worker = getattr(self.player, "_background_worker", None)
        background_available = background_resolver is not None

        self.player._provider_router = ProviderRouter(
            logger=logger,
            resolver=resolver,
            background_resolver=background_resolver,
            background_available=background_available,
            error_handler=error_handler,
            record_resolution_failure=record_resolution_failure,
            background_worker=background_worker,
        )

        def violation_callback(item, violation_type, details) -> None:
            logger.warning(
                "Compliance violation: item_id=%s type=%s details=%s",
                getattr(item, "id", "unknown"),
                violation_type,
                details,
            )

        self.player._compliance_policy = YouTubeCompliancePolicy(
            logger=logger,
            violation_callback=violation_callback,
        )
        self.player._compliance_service = self.player._compliance_policy

    def _setup_player_control_service(self, runtime_config: PlayerRuntimeConfig) -> None:
        """Setup the PlayerControlService with all dependencies."""
        from music.runtime.play_command import PlayCommandService
        from music.runtime.queue_command import QueueCommandService
        from music.runtime.queue_resolver import QueueItemResolver

        player = self.player
        logger = player._logger

        controller = player._controller
        resolver = (
            QueueItemResolver(
                controller=controller,
                logger=logger,
            )
            if controller
            else None
        )

        play_command = (
            PlayCommandService(
                player=player,
                logger=logger,
                pending_start_timeout=TIMEOUT_VERY_LONG,
                resolver=resolver,
            )
            if resolver
            else None
        )

        queue_command = (
            QueueCommandService(
                player=player,
                controller=controller,
                resolver=resolver,
                playlist=player._playlist,
                max_queue_size=player._max_queue_size,
                logger=logger,
            )
            if resolver and controller
            else None
        )

        if play_command and queue_command and not player._test_mode:
            try:
                from music.runtime.autoplay_service import AutoplayService
                from music.runtime.backend_manager import BackendLifecycleManager
                from music.runtime.control_surface import PlayerControlService

                telemetry = getattr(player, "_metrics", None)

                backend_manager = BackendLifecycleManager(
                    player=player,
                    backend_loader=player._backend_loader,
                    telemetry=telemetry,
                    logger=logger,
                    pending_start_timeout=TIMEOUT_VERY_LONG,
                )

                queue_config = getattr(player._queue_config, "limits", None)
                failure_ttl: float = float(getattr(player._queue_config, "failure_ttl_sec", 900.0))

                if resolver is None:
                    raise RuntimeError("Cannot create AutoplayService without resolver")
                autoplay_service = AutoplayService(
                    player=player,
                    controller=controller,
                    resolver=resolver,
                    playlist=player._playlist,
                    queue_config=queue_config,
                    failure_ttl=failure_ttl,
                    logger=logger,
                )

                player._control = PlayerControlService(
                    owner=player,
                    play_command=play_command,
                    queue_command=queue_command,
                    autoplay_service=autoplay_service,
                    backend_manager=backend_manager,
                    queue_engine=player._playlist,
                    state_service=player._state_service,
                    condition=player._cv,
                    emit_callback=lambda: player._emit(),
                    logger=logger,
                    test_mode=player._test_mode,
                )
            except Exception as exc:
                from .test_stub import TestModeControlStub

                logger.warning("Failed to create full PlayerControlService: %r", exc)
                player._control = TestModeControlStub(player, logger)
        else:
            from .test_stub import TestModeControlStub

            player._control = TestModeControlStub(player, logger)

    def _setup_autoplay(self, autoplay_enabled: bool | None, runtime_config: PlayerRuntimeConfig) -> None:
        """Setup autoplay functionality."""
        if autoplay_enabled:
            try:
                from music.autoplay_controller import AutoplayController

                self.player.autoplay_controller = AutoplayController(
                    music_player=self.player,
                    settings_manager=self.player._settings,
                )

                # Reset autoplay context on startup (Product Decision 2026-01-11)
                # Prevents stale context from affecting AI recommendations
                self.player.autoplay_controller.reset_context()

            except ImportError:
                self.player._logger.warning("Autoplay controller not available")
                self.player.autoplay_controller = None
        else:
            self.player.autoplay_controller = None

    def _setup_session_controller(self) -> None:
        """Setup PlaybackSessionController with playlist and state dependencies.

        Runs unconditionally — playlists must work even when autoplay is off.
        """
        try:
            from models.state_manager import ConsolidatedState
            from music.playback_session import initialize_playback_session_controller
            from music.playlist_manager import get_playlist_manager

            playlist_mgr = get_playlist_manager()
            state_mgr = getattr(self.player, "_consolidated_state", None)
            if state_mgr is None:
                state_mgr = ConsolidatedState()
                self.player._consolidated_state = state_mgr

            autoplay = getattr(self.player, "autoplay_controller", None)

            session_controller = initialize_playback_session_controller(
                autoplay_controller=autoplay,
                playlist_manager=playlist_mgr,
                state_manager=state_mgr,
            )

            if autoplay is not None:
                autoplay.set_session_controller(session_controller)

            self.player._logger.info("PlaybackSessionController initialized")
        except Exception as exc:
            self.player._logger.warning("PlaybackSessionController not available: %s", exc)

    def _setup_resolution_infrastructure(self) -> None:
        """Setup resolution infrastructure."""
        self.player._resolution_failures = []
        self.player._resolution_lock = threading.RLock()
        # Note: self.player._lock is already created in _setup_basic_attributes

    def _setup_integrity_monitoring(self, integrity_monitor_enabled: bool | None) -> None:
        """Setup integrity monitoring.

        Note: Integrity monitoring is currently disabled. The IntegrityMonitorWorker
        class exists in music/controller/workers.py but requires an actual integrity
        monitor implementation before it can be enabled. Setting to None disables
        the integrity monitoring thread in the playback worker.
        """
        # Integrity monitoring is not yet implemented - worker infrastructure exists
        # but requires actual monitoring logic before activation
        self.player._integrity_monitor = None

    def _initialize_extracted_managers(self) -> None:
        """Initialize the extracted manager components."""
        from .playback import MusicPlayerEmbeddedPlayback, MusicPlayerPlaybackController
        from .state import MusicPlayerQueueManager, MusicPlayerStateManager

        self.player._playback_controller = MusicPlayerPlaybackController(self.player)
        self.player._state_manager = MusicPlayerStateManager(self.player)
        self.player._queue_mgr = MusicPlayerQueueManager(self.player)
        self.player._embedded_mgr = MusicPlayerEmbeddedPlayback(self.player)

    def _setup_legacy_attribute_aliases(self) -> None:
        """Setup legacy attribute aliases for backward compatibility."""
        if hasattr(self.player, "_background_worker"):
            self.player._worker = self.player._background_worker
        else:
            self.player._worker = None
        autoplay_controller = getattr(self.player, "autoplay_controller", None) or getattr(
            self.player, "_autoplay_controller", None
        )
        self.player._logger.info("Legacy aliases: autoplay_controller=%s", autoplay_controller is not None)
        if autoplay_controller is not None:
            self.player._autoplay_controller = autoplay_controller
            self.player._autoplay_monitor = autoplay_controller
            self.player.autoplay = autoplay_controller
            self.player._logger.info("player.autoplay set successfully")
        else:
            self.player._autoplay_monitor = None
            self.player.autoplay = None
            self.player._logger.warning("player.autoplay set to None (no autoplay_controller)")
