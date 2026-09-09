from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from config.settings import settings
from models.player import QueueItem
from music.backends.base import BaseBackend
from music.compliance.ytm_policy import ComplianceContext
from music.exceptions import BackendError
from music.playback.backend_selector import BackendSelector
from music.playback.routing import PlaybackRoute, PlaybackRouter
from music.player.playback_state import PlaybackPhase
from music.runtime.backend_manager import BackendLifecycleManager
from music.runtime.embedded_playback import play_external_browser

if TYPE_CHECKING:
    from music.player import MusicPlayer


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        _logger.debug("State machine transition to %s failed (non-critical)", phase)


_logger = logging.getLogger(__name__)


class _EngineManager(Protocol):
    def identify_provider(self, item: QueueItem) -> str | None: ...

    def attach_backend(
        self,
        item: QueueItem,
        *,
        upcoming: list[QueueItem],
        default_backend: BaseBackend | None,
    ) -> BaseBackend | None: ...


def _get_engine_manager(player: MusicPlayer) -> _EngineManager | None:
    candidate = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
    if candidate is None:
        return None
    identify_provider = getattr(candidate, "identify_provider", None)
    attach_backend = getattr(candidate, "attach_backend", None)
    if callable(identify_provider) and callable(attach_backend):
        return cast(_EngineManager, candidate)
    return None


@dataclass
class BackendSelectionResult:
    """Result of backend selection process."""

    success: bool
    backend: BaseBackend | None
    backend_name: str
    selection_method: str
    error: str | None = None


class PlaybackExecutor:
    """
    Handles routing, backend selection, compliance, and playback monitoring.
    """

    def __init__(
        self,
        *,
        player: MusicPlayer,
        backend_manager: BackendLifecycleManager,
        logger: logging.Logger,
    ) -> None:
        self._player = player
        self._backend_manager = backend_manager
        self._logger = logger.getChild("playback_executor")

    # -------------------------------------------------------------------------
    # Main Entry Point
    # -------------------------------------------------------------------------

    def start_and_monitor_playback(
        self,
        item: QueueItem,
        token: tuple[int, str | None],
    ) -> float | bool:
        """Start playback and monitor until complete or skipped."""
        player = self._player

        # Phase 1: Validate and compliance check
        if not self._validate_item_and_compliance(item):
            return False

        # Phase 2: Test stream playback (special mode)
        if self._is_test_stream_mode(item):
            return self._handle_test_stream_playback(item)

        # Phase 3: Resolve route
        playback_mode = item.playback_mode or "vlc_stream"
        self._log_engine_play(item, playback_mode)

        # Telemetry: record music provider and playback feature
        try:
            from admin.instrumentation import record_music_provider

            record_music_provider(item.provider or item.source or "unknown")
        except Exception:
            _logger.debug("Music provider telemetry recording failed (non-critical)")

        route = self._resolve_route(item, playback_mode)
        if route is None:
            return False

        # Phase 4: Handle embedded/external routes
        if route.route_type in ("embedded_iframe_webview", "embedded_webview"):
            result = self._handle_embedded_route(item, route, playback_mode)
            if result is not None:
                return result
            # result is None → engine-managed provider (e.g. Spotify CDP)
            # already started a backend.  Skip phases 5-6 (backend
            # selection/configuration) and go straight to monitoring.
            backend = player._backend
            if backend is not None:
                self._logger.info(
                    "ENGINE_MANAGED_MONITOR: Monitoring pre-started backend=%s for %s",
                    type(backend).__name__,
                    item.title or "Unknown",
                )
                return self._monitor_playback_loop(item, token, backend)
            # Backend not set — fall through to normal phases 5-7.
            self._logger.warning(
                "ENGINE_MANAGED_FALLTHROUGH: _handle_embedded_route returned None "
                "but no backend was set; falling through to stream playback."
            )

        if route.route_type == "external_browser":
            return play_external_browser(player, self._logger, item)

        # Phase 5: Select backend for stream playback
        selection = self._select_backend_for_stream(item, route)
        if not selection.success:
            return False

        backend = selection.backend
        if backend is None:
            return False
        backend_name = selection.backend_name

        # Phase 6: Configure and start playback
        self._configure_backend(item, route, backend, backend_name)

        if not self._execute_backend_playback(item, backend, backend_name, playback_mode, route):
            try:
                from admin.instrumentation import record_playback_failure

                record_playback_failure()
            except Exception:
                _logger.debug("Playback failure telemetry recording failed (non-critical)")
            return False

        # Phase 7: Monitor playback
        return self._monitor_playback_loop(item, token, backend)

    # -------------------------------------------------------------------------
    # Phase 1: Validation
    # -------------------------------------------------------------------------

    def _validate_item_and_compliance(self, item: QueueItem) -> bool:
        """Validate item has URL and passes compliance check."""
        player = self._player

        if not item.url:
            self._logger.error("Queue item missing URL: %s", item)
            player._record_queue_failure(item, stage="missing_url", metadata={})
            return False

        compliance_result = player._compliance_service.evaluate(
            item,
            context=ComplianceContext(
                allow_test_override=self._backend_manager.test_stream_playback_allowed,
                auto_fix_playback_mode=True,
            ),
        )
        if not compliance_result.is_compliant:
            return False

        return True

    # -------------------------------------------------------------------------
    # Phase 2: Test Stream Mode
    # -------------------------------------------------------------------------

    def _is_test_stream_mode(self, item: QueueItem) -> bool:
        """Check if test stream playback mode is active."""
        return (
            self._backend_manager.test_stream_playback_allowed
            and item.playback_mode == "vlc_stream"
            and item.provider == "youtube_music"
        )

    def _handle_test_stream_playback(self, item: QueueItem) -> bool:
        """Handle test stream playback mode."""
        player = self._player
        backend = self._backend_manager.manual_override_backend or self._backend_manager.backend

        if backend is None:
            self._logger.warning("Test stream playback requested but no backend available")
            return False

        self._logger.info(
            "TEST_STREAM_PLAYBACK provider=%s url=%s backend=%s video_id=%s",
            item.provider or "unknown",
            item.url[:80] if item.url else "none",
            type(backend).__name__,
            getattr(item, "video_id", None) or "none",
        )

        self._backend_manager.prepare_backend_for_track(item)

        try:
            backend.play_url(item.url)
        except Exception as exc:
            self._logger.exception("Test stream playback failed: %s", exc)
            return False

        with player._cv:
            player._backend = backend
            player._backend_name = type(backend).__name__
            self._backend_manager.configure_backend_state(backend)
            player._set_now_playing_locked(item)
            _sm_transition(player, PlaybackPhase.PLAYING)
            # Queue mutations auto-invalidate - no sync needed
            player._cv.notify_all()
        player._emit()
        return True

    # -------------------------------------------------------------------------
    # Phase 3: Route Resolution
    # -------------------------------------------------------------------------

    def _log_engine_play(self, item: QueueItem, playback_mode: str) -> None:
        """Log engine play event."""
        current_backend = getattr(self._player._state, "backend", None) or "unknown"
        self._logger.info(
            "ENGINE_PLAY provider=%s playback_mode=%s url=%s backend=%s video_id=%s",
            item.provider or "unknown",
            playback_mode,
            item.url[:80] if item.url else "none",
            current_backend,
            getattr(item, "video_id", None) or "none",
        )

    def _resolve_route(self, item: QueueItem, playback_mode: str) -> PlaybackRoute | None:
        """Resolve playback route for item."""
        player = self._player
        router = PlaybackRouter(logger=self._logger)

        try:
            return router.route(item)
        except ValueError as exc:
            self._logger.error(
                "ROUTING_ERROR: %s item_id=%s provider=%s",
                str(exc),
                item.id,
                item.provider or "unknown",
            )
            player._record_queue_failure(
                item,
                stage="routing_error",
                metadata={
                    "error": str(exc),
                    "provider": item.provider,
                    "playback_mode": playback_mode,
                },
            )
            return None

    # -------------------------------------------------------------------------
    # Phase 4: Embedded/External Routes
    # -------------------------------------------------------------------------

    def _handle_embedded_route(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        playback_mode: str,
    ) -> bool | None:
        """Handle embedded webview routes.

        For browser-native provider: routes through engine manager to
        BrowserPlaybackEngine which navigates a dedicated QWebEngineView.

        For all other providers (youtube_iframe etc.): defers to frontend
        iframe via EMBEDDED_DEFER -- sets minimal player state so React
        renders the YouTube iframe.
        """
        player = self._player

        # Browser-native path.
        # Browser provider uses BrowserPlaybackEngine + dedicated QWebEngineView,
        # NOT the React iframe defer path.
        if getattr(item, "provider", None) == "browser" and route.route_type == "embedded_webview":
            return self._handle_browser_native_route(item, route, playback_mode)

        # Engine-managed providers (Spotify CDP, etc.).
        if playback_mode == "spotify_cdp" or route.metadata.get("playback_mode") == "spotify_cdp":
            # Store player ref so the CDP poll loop can push position/duration
            # updates via _broadcast_state() -> _get_player_for_emit().
            from playback.engine_manager import _set_player_for_emit

            _set_player_for_emit(player)

            manager = _get_engine_manager(player)
            if manager is not None:
                result = self._try_engine_manager_playback(item, manager, playback_mode)
                if result is True:
                    # Engine started successfully. Return None so
                    # start_and_monitor_playback falls through to the monitor
                    # loop, which will block until the CDP handle finishes.
                    # NOTE: Do NOT call set_embedded_source(True) here.
                    # Spotify CDP is an external Chrome process, not an embedded
                    # source like YouTube IFrame. Leaving embedded_source=False
                    # allows the ProcTap path to manage the external browser.
                    return None  # signal: backend ready, caller should monitor
                if result is not None:
                    # Engine returned explicit failure (False)
                    return result
                self._logger.warning("SPOTIFY_CDP_ROUTE: Engine manager returned None, falling through")

        video_id = getattr(item, "video_id", None)
        effective_mode = route.route_type or playback_mode
        metadata_mode = route.metadata.get("playback_mode")
        if isinstance(metadata_mode, str) and metadata_mode:
            effective_mode = metadata_mode

        # ProcTap path: YouTube IFrame plays directly through Chromium to the
        # default speaker. ProcTap captures the same browser process only for
        # multiroom spokes; standalone desktop audio does not go through a
        # virtual cable or hidden source browser.
        try:
            from audio_core.streaming.pipeline_wiring import set_embedded_source

            set_embedded_source(True)
            self._logger.info("EMBEDDED_DEFER: embedded source flag set (ProcTap path)")
        except (ImportError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
            self._logger.warning("EMBEDDED_DEFER set_embedded_source failed: %s", exc)

        with player._cv:
            player._state.playback_mode = effective_mode
            player._position_ms = 0
            player._duration_ms = 0
            player._state.position = 0
            if hasattr(player._state, "position_ms"):
                player._state.position_ms = 0
            player._state.duration = 0
            player._state.is_playing = False
            if video_id:
                try:
                    embed_url = f"/static/webviews/youtube_iframe.html?autoplay=1&video={video_id}"
                    object.__setattr__(item, "embed_url", embed_url)
                except (AttributeError, TypeError) as exc:
                    self._logger.debug("EMBEDDED_DEFER: could not set item embed_url: %s", exc)
            player._set_now_playing_locked(item)
            _sm_transition(player, PlaybackPhase.LOADING)
            player._cv.notify_all()
        player._emit()

        self._logger.info(
            "EMBEDDED_DEFER: React will render iframe. video_id=%s mode=%s",
            video_id,
            effective_mode,
        )

        # Tell ProcTap to target self-PID immediately. ProcTap will use its
        # extended silence timeout for notified PIDs, so it waits until YouTube
        # starts rendering audio instead of failing early.
        try:
            from audio_core.streaming.pipeline_wiring import notify_proctap_pid

            notify_proctap_pid(os.getpid())
            self._logger.info("EMBEDDED_DEFER: notified ProcTap to target self-PID %d", os.getpid())
        except (ImportError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
            self._logger.warning("EMBEDDED_DEFER: notify_proctap_pid failed: %s", exc)

        return True

    def _stop_existing_backend_for_new_playback_source(self) -> None:
        # Stopped OUTSIDE player._lock, matching MusicRuntimeControlSurface.stop.
        # The backend's stop() joins its playback thread, and that thread calls
        # back into MusicPlayerStateManager.handle_backend_progress, which takes
        # player._lock -- so holding the lock here deadlocked every new-track
        # play against its own progress callback until the join timed out.
        # Nothing on the player is mutated here, so the lock protected nothing.
        stop_backend = getattr(self._backend_manager, "stop_backend_locked", None)
        if not callable(stop_backend):
            return
        stop_backend()

    def _handle_browser_native_route(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        playback_mode: str,
    ) -> bool | None:
        """Route browser-native items through BrowserPlaybackEngine.

        1. Gets the engine manager
        2. Attaches backend (EngineBackedBackend wrapping BrowserPlaybackEngine)
        3. Wires the QWebEngineView to the controller  **before** play
        4. Calls backend.play_url() which navigates and auto-plays
        """
        player = self._player
        manager = _get_engine_manager(player)

        if manager is None:
            self._logger.error("BROWSER_NATIVE_ROUTE: No engine manager available, cannot play browser provider")
            return None  # Fall through to Phase 5

        self._logger.info(
            "BROWSER_NATIVE_ROUTE: Routing provider=browser through engine manager url=%s",
            (item.url or "")[:120],
        )

        # --- Step 1: attach backend (creates EngineBackedBackend) ---
        try:
            provider_id = manager.identify_provider(item)
            if not provider_id:
                self._logger.warning("BROWSER_NATIVE_ROUTE: Could not identify provider")
                return None

            with player._cv:
                upcoming = list(player._playlist.upcoming_ref)

            selected_backend = manager.attach_backend(
                item,
                upcoming=upcoming,
                default_backend=None,
            )

            if selected_backend is None:
                self._logger.warning("BROWSER_NATIVE_ROUTE: attach_backend returned None")
                return None

        except Exception as exc:
            self._logger.warning("BROWSER_NATIVE_ROUTE: Engine manager attach failed: %r", exc)
            return None

        # --- Step 2: wire webview BEFORE play ---
        from music.runtime.embedded_playback import _wire_browser_engine_webview

        _wire_browser_engine_webview(player, selected_backend)

        # Store player ref so browser engine can emit state after async
        # video_id extraction (callback fires later on Qt main thread).
        from playback.engine_manager import _set_player_for_emit

        _set_player_for_emit(player)

        # --- Step 3: start playback ---
        result = self._start_engine_manager_backend(item, selected_backend, provider_id, playback_mode)

        if result is True:
            self._logger.info("BROWSER_NATIVE_ROUTE: Search started via BrowserPlaybackEngine")
            return True

        if result is not None:
            return result

        self._logger.warning("BROWSER_NATIVE_ROUTE: Engine manager returned None, falling through")
        return None

    def _try_engine_manager_playback(
        self,
        item: QueueItem,
        manager: _EngineManager,
        playback_mode: str,
    ) -> bool | None:
        """Try to use engine manager for playback. Returns result or None."""
        player = self._player

        try:
            provider_id = manager.identify_provider(item)
            if not provider_id:
                return None

            with player._cv:
                upcoming = list(player._playlist.upcoming_ref)

            selected_backend = manager.attach_backend(
                item,
                upcoming=upcoming,
                default_backend=None,
            )

            if selected_backend is None:
                return None

            return self._start_engine_manager_backend(item, selected_backend, provider_id, playback_mode)

        except Exception as exc:
            self._logger.warning(
                "Engine manager check failed for embedded_webview item, falling back: %r",
                exc,
            )
            return None

    def _start_engine_manager_backend(
        self,
        item: QueueItem,
        backend: BaseBackend,
        provider_id: str,
        playback_mode: str,
    ) -> bool:
        """Start playback via engine manager backend."""
        player = self._player
        backend_name = type(backend).__name__

        self._stop_existing_backend_for_new_playback_source()

        with player._cv:
            player._backend = backend
            player._backend_name = backend_name
            player._state.playback_mode = playback_mode
            player._is_playing = False
            player._paused = False
            player._state.is_playing = False
            self._backend_manager.configure_backend_state(backend)
            player._set_now_playing_locked(item)
            _sm_transition(player, PlaybackPhase.IDLE)
            # Queue mutations auto-invalidate - no sync needed
            player._cv.notify_all()

        try:
            backend.play_url(item.url)
            with player._cv:
                player._is_playing = True
                player._paused = False
                player._state.is_playing = True
                _sm_transition(player, PlaybackPhase.PLAYING)
                player._cv.notify_all()
            self._logger.info(
                "ENGINE_PLAY_STARTED provider=%s backend=%s video_id=%s title=%s",
                provider_id,
                backend_name,
                item.video_id or "none",
                item.title or "Unknown",
            )
            player._emit()
            return True
        except Exception as exc:
            self._logger.exception(
                "Engine manager backend play failed for %s: %r",
                item.title,
                exc,
            )
            with player._cv:
                player._is_playing = False
                player._state.is_playing = False
                player._cv.notify_all()
            player._record_queue_failure(
                item,
                stage="engine_backend_play_failed",
                metadata={
                    "error": str(exc),
                    "provider": provider_id,
                },
            )
            return False

    # -------------------------------------------------------------------------
    # Phase 5: Backend Selection
    # -------------------------------------------------------------------------

    def _select_backend_for_stream(
        self,
        item: QueueItem,
        route: PlaybackRoute,
    ) -> BackendSelectionResult:
        """Select appropriate backend for stream playback."""
        player = self._player
        manager = _get_engine_manager(player)

        with player._cv:
            upcoming = list(player._playlist.upcoming_ref)

        if self._backend_manager.manual_override_active:
            return self._select_manual_override_backend()

        if self._backend_manager.backend is None:
            self._backend_manager.init_backend()

        configured_backend = self._backend_manager.backend
        configured_backend_name = getattr(player, "_backend_name", None)
        if (
            configured_backend is not None
            and isinstance(configured_backend_name, str)
            and configured_backend_name
            and configured_backend_name != "vlc"
        ):
            return BackendSelectionResult(
                success=True,
                backend=configured_backend,
                backend_name=configured_backend_name,
                selection_method="configured_backend",
            )

        return self._select_backend_via_selector(item, route, upcoming, manager)

    def _select_manual_override_backend(self) -> BackendSelectionResult:
        """Use manually overridden backend."""
        backend = self._backend_manager.manual_override_backend
        return BackendSelectionResult(
            success=backend is not None,
            backend=backend,
            backend_name=type(backend).__name__ if backend else "None",
            selection_method="manual_override",
            error=None if backend else "Manual override backend is None",
        )

    def _select_backend_via_selector(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        upcoming: list[QueueItem],
        manager: _EngineManager | None,
    ) -> BackendSelectionResult:
        """Select backend using BackendSelector."""
        player = self._player
        selector = BackendSelector(
            logger=self._logger,
            engine_manager=manager,
            existing_backend=self._backend_manager.backend,
        )
        selection_result = selector.select_backend(item, route, upcoming_items=upcoming)

        if not selection_result.success:
            self._logger.error(
                "Backend selection failed: %s item_id=%s route_type=%s",
                selection_result.error,
                item.id,
                route.route_type,
            )
            player._record_queue_failure(
                item,
                stage="backend_selection_failed",
                metadata={
                    "error": selection_result.error or "unknown",
                    "route_type": route.route_type,
                    "provider": route.provider_id,
                    "selection_method": selection_result.selection_method,
                },
            )

        backend = selection_result.backend
        backend_name = selection_result.backend_name or (type(backend).__name__ if backend else "None")

        return BackendSelectionResult(
            success=selection_result.success,
            backend=backend,
            backend_name=backend_name,
            selection_method=selection_result.selection_method,
            error=selection_result.error,
        )

    # -------------------------------------------------------------------------
    # Phase 6: Backend Configuration
    # -------------------------------------------------------------------------

    def _configure_backend(
        self,
        item: QueueItem,
        route: PlaybackRoute,
        backend: BaseBackend,
        backend_name: str,
    ) -> None:
        """Configure backend state and log playback start."""
        player = self._player
        manager = _get_engine_manager(player)

        self._logger.info(
            "selected backend=%s (route_type=%s, selection_method=%s, provider=%s)",
            backend_name,
            route.route_type,
            ("manual_override" if self._backend_manager.manual_override_active else "selector"),
            route.provider_id or "unknown",
        )

        provider_id = self._resolve_provider_id(item, route, manager)

        if backend is not player._backend:
            player._backend = backend
            player._backend_name = backend_name

        self._log_playback_start(item, backend, provider_id)

    def _resolve_provider_id(self, item: QueueItem, route: PlaybackRoute, manager: _EngineManager | None) -> str:
        """Resolve provider ID from route or manager."""
        provider_id = route.provider_id or item.provider or "unknown"

        if manager is not None:
            try:
                identified_provider = manager.identify_provider(item)
                if identified_provider:
                    provider_id = identified_provider
            except Exception as exc:
                self._logger.debug("Provider identification failed: %r", exc)

        return provider_id

    def _log_playback_start(self, item: QueueItem, backend: BaseBackend, provider_id: str) -> None:
        """Log playback start with backend capabilities."""
        try:
            backend_caps = self._backend_manager.extract_backend_capabilities(backend)
            embedded_only = settings.embedded_only

            self._logger.info(
                "PLAYBACK_START provider=%s backend=%s video_id=%s title=%s url=%s capabilities=%s embedded_only=%s",
                provider_id,
                type(backend).__name__,
                item.video_id or "none",
                item.title or "Unknown",
                item.url[:80] if item.url else "none",
                backend_caps,
                embedded_only,
            )

            self._backend_manager.configure_backend_state(backend)
        except Exception as exc:
            self._logger.warning("Failed to configure backend state: %r", exc)

    # -------------------------------------------------------------------------
    # Phase 7: Execute Playback
    # -------------------------------------------------------------------------

    def _execute_backend_playback(
        self,
        item: QueueItem,
        backend: BaseBackend,
        backend_name: str,
        playback_mode: str,
        route: PlaybackRoute,
    ) -> bool:
        """Execute playback on the backend."""

        effective_backend_name = backend_name or type(backend).__name__
        provider_id = route.provider_id or item.provider or "unknown"

        self._logger.info(
            "ENGINE_START_PLAYBACK provider=%s backend=%s playback_mode=%s requires_embedded=%s url=%s",
            provider_id,
            effective_backend_name,
            playback_mode,
            route.requires_embedded,
            item.url[:80] if item.url else "none",
        )

        try:
            return self._start_and_verify_playback(item, backend, playback_mode)
        except BackendError as exc:
            return self._handle_backend_error(item, backend_name, exc)
        except Exception as exc:
            return self._handle_playback_exception(item, exc)

    def _start_and_verify_playback(
        self,
        item: QueueItem,
        backend: BaseBackend,
        playback_mode: str,
    ) -> bool:
        """Start playback and verify it begins."""
        player = self._player

        self._backend_manager.prepare_backend_for_track(item)

        with player._cv:
            player._state.playback_mode = playback_mode
            player._set_now_playing_locked(item)

        backend.play_url(item.url)

        player._emit()
        return True

    def _handle_backend_error(
        self,
        item: QueueItem,
        backend_name: str,
        exc: BackendError,
    ) -> bool:
        """Handle BackendError during playback."""
        player = self._player

        self._logger.exception(
            "Backend play failed (BackendError) for %s: %r",
            item.title,
            exc,
        )

        with player._cv:
            _sm_transition(player, PlaybackPhase.ERROR)
            # Queue mutations auto-invalidate - no sync needed
            player._cv.notify_all()

        error_entry = {
            "error": "backend_error",
            "message": str(exc),
            "backend": backend_name or "unknown",
            "stage": "play_start",
        }

        with player._cv:
            if not hasattr(player._state, "playback_errors"):
                player._state.playback_errors = []
            from core.json_types import to_json_value

            error_value = to_json_value(error_entry)
            if isinstance(error_value, dict):
                player._state.playback_errors.append(error_value)
            if len(player._state.playback_errors) > 10:
                player._state.playback_errors = player._state.playback_errors[-10:]

        player._record_queue_failure(
            item,
            stage="backend_error",
            metadata={
                "error": "backend_error",
                "error_message": str(exc),
                "backend": backend_name or "unknown",
            },
        )
        player._emit()
        return False

    def _handle_playback_exception(self, item: QueueItem, exc: Exception) -> bool:
        """Handle generic exception during playback."""
        player = self._player

        self._logger.exception("Backend play failed for %s: %r", item.title, exc)

        from playback.engines.base import PlaybackError as EnginePlaybackError

        if isinstance(exc, EnginePlaybackError):
            return self._handle_engine_playback_error(item, exc)

        player._record_queue_failure(
            item,
            stage="play_start_failure",
            metadata={"error": repr(exc)},
        )
        return False

    def _handle_engine_playback_error(self, item: QueueItem, exc: Exception) -> bool:
        """Handle EnginePlaybackError specifically."""
        player = self._player
        error_msg = str(exc)

        if "YouTube playback failed" in error_msg:
            parts = error_msg.split(":", 1)
            user_message = (
                parts[1].strip()
                if len(parts) > 1
                else "YouTube playback failed. Please check that the video is available."
            )
        else:
            user_message = "Couldn't play that from YouTube. The video may be unavailable or region-locked."

        self._logger.error(
            "YTM_ENGINE_ERROR stage=playback_start video_id=%s error=%s",
            item.video_id or "none",
            error_msg[:100],
        )

        player._record_queue_failure(
            item,
            stage="play_start_failure",
            metadata={
                "error": "playback_failed",
                "error_message": user_message,
                "provider": getattr(item, "provider", None),
            },
        )
        item.unavailable = True
        item.unavailable_reason = user_message
        return False

    # -------------------------------------------------------------------------
    # Phase 8: Playback Monitoring
    # -------------------------------------------------------------------------

    def _monitor_playback_loop(
        self,
        item: QueueItem,
        token: tuple[int, str | None],
        backend: BaseBackend,
    ) -> float | bool:
        """Monitor playback until complete or skipped."""
        start_time = time.time()

        # Wait for playback to actually start
        if not self._wait_for_playback_start(item, backend):
            return False

        # Monitor until playback ends
        skip_detected = self._monitor_until_complete(item, token, backend)

        elapsed = time.time() - start_time
        if skip_detected:
            # Return elapsed (truthy float) so the completion handler treats
            # this as a successful playback that ended via skip, NOT a failure.
            # Returning False here previously caused PLAY_ERROR + DOUBLE_COMPLETE
            # because the handler would call complete_current(success=False).
            return elapsed
        return elapsed

    def _wait_for_playback_start(self, item: QueueItem, backend: BaseBackend) -> bool:
        """Wait for playback to start, with timeout."""
        player = self._player
        playback_start_wait_time = 0.5
        playback_start_timeout = time.time() + playback_start_wait_time

        while True:
            if player._worker_manager.should_stop():
                backend.stop()
                return False

            try:
                is_playing = backend.is_playing()
            except Exception as e:
                self._logger.debug("is_playing check failed (non-critical): %s", e)
                is_playing = False

            if is_playing:
                self._backend_manager.mark_track_started_playing()
                with player._cv:
                    player._is_playing = True
                    player._paused = False
                    player._state.is_playing = True
                    _sm_transition(player, PlaybackPhase.PLAYING)
                    # Queue mutations auto-invalidate - no sync needed
                    player._cv.notify_all()
                player._emit()
                return True

            if time.time() < playback_start_timeout:
                time.sleep(0.1)
                continue

            # Timeout reached - check backend health
            return self._handle_playback_start_timeout(item, backend, playback_start_wait_time)

    def _handle_playback_start_timeout(
        self,
        item: QueueItem,
        backend: BaseBackend,
        timeout_seconds: float,
    ) -> bool:
        """Handle timeout waiting for playback to start."""
        player = self._player

        self._logger.warning(
            "Playback did not start within %s seconds for %s - checking backend health",
            timeout_seconds,
            item.title,
        )

        health_error = self._backend_manager.evaluate_backend_health(backend)
        if health_error is not None:
            self._logger.error(
                "Backend health check failed while waiting for playback: %s",
                health_error,
            )
            player._record_queue_failure(item, stage="backend_health_error", metadata=health_error)
            backend.stop()
            return False

        # Give it one more chance after health check
        time.sleep(0.2)
        try:
            is_playing = backend.is_playing()
        except Exception as e:
            self._logger.debug("is_playing check after health check failed (non-critical): %s", e)
            is_playing = False

        if is_playing:
            return True

        self._logger.error(
            "Playback failed to start for %s - backend reports not playing",
            item.title,
        )
        player._record_queue_failure(
            item,
            stage="playback_start_timeout",
            metadata={
                "timeout_seconds": timeout_seconds,
                "backend_healthy": True,
            },
        )
        backend.stop()
        return False

    def _monitor_until_complete(
        self,
        item: QueueItem,
        token: tuple[int, str | None],
        backend: BaseBackend,
    ) -> bool:
        """Monitor playback until it completes. Returns True if skip detected."""
        player = self._player
        prebuffer_triggered = False  # Track if we've already triggered prebuffering

        while True:
            if player._worker_manager.should_stop():
                backend.stop()
                backend.cancel_preload()  # Cancel any pending preload
                return False

            # Check for skip request
            with player._cv:
                if player._pending_skip_tokens and player._playlist.matches(token, item.id):
                    player._pending_skip_tokens -= 1
                    backend.stop()
                    backend.cancel_preload()  # Cancel preload on skip
                    return True  # Skip detected

            try:
                is_playing = backend.is_playing()
            except Exception as e:
                self._logger.debug("is_playing check during monitoring failed (non-critical): %s", e)
                is_playing = False

            # Gapless playback: Trigger prebuffering when approaching end of track
            if is_playing and not prebuffer_triggered:
                prebuffer_triggered = self._try_trigger_prebuffering(backend, player)

            # Check backend health
            health_error = self._backend_manager.evaluate_backend_health(backend)
            if health_error is not None:
                self._logger.warning("Backend health check failed: %s", health_error)
                player._record_queue_failure(item, stage="backend_health_error", metadata=health_error)
                backend.stop()
                backend.cancel_preload()
                return False

            if not is_playing:
                # CB-1 Layer 3 FIX: Only check backend._paused to detect pause.
                # SimpleBackend.is_playing() returns False when paused,
                # but the track is still alive and can be resumed.
                # DO NOT check player._user_paused here — stop() sets it True,
                # which would trap this loop thinking a stopped track is paused.
                paused = getattr(backend, "_paused", False)

                if paused:
                    time.sleep(0.1)
                    continue  # Track is paused, not finished — keep monitoring
                break  # Genuine completion

            time.sleep(0.1)

        return False  # Normal completion

    def _try_trigger_prebuffering(self, backend: BaseBackend, player: MusicPlayer) -> bool:
        """
        Check if we should trigger prebuffering for gapless playback.

        Args:
            backend: Current playback backend
            player: Music player instance

        Returns:
            True if prebuffering was triggered (or already complete)
        """
        # Check position and duration to determine if we should prebuffer
        position_ms = backend.current_position_ms()
        duration_ms = backend.current_duration_ms()

        if position_ms is None or duration_ms is None or duration_ms <= 0:
            return False

        remaining_ms = duration_ms - position_ms

        # Trigger prebuffering when 5-10 seconds remain
        # This gives enough time to buffer the next track
        PREBUFFER_THRESHOLD_MS = 10000  # Start prebuffering at 10s remaining
        PREBUFFER_MIN_MS = 5000  # Don't prebuffer if less than 5s remaining

        if remaining_ms > PREBUFFER_THRESHOLD_MS or remaining_ms < PREBUFFER_MIN_MS:
            return False

        # Get the next track in the queue
        next_track = self._get_next_track_for_prebuffer(player)
        if next_track is None:
            return True  # No next track, consider prebuffering "done"

        # Resolve the URL for the next track
        next_url = self._resolve_next_track_url(next_track)
        if next_url is None:
            self._logger.debug("Could not resolve URL for next track, skipping prebuffer")
            return True  # Can't prebuffer, mark as done

        # Trigger prebuffering
        success = backend.preload_next_track(next_url)
        if success:
            self._logger.info(
                "Prebuffering next track: %s (remaining: %dms)",
                next_track.title or "Unknown",
                remaining_ms,
            )
        else:
            self._logger.debug("Backend does not support prebuffering")

        return True  # Mark as triggered regardless of success

    def _get_next_track_for_prebuffer(self, player: MusicPlayer) -> QueueItem | None:
        """Get the next track from the queue for prebuffering."""
        try:
            with player._cv:
                upcoming = list(player._playlist.upcoming_ref)
                if upcoming:
                    return upcoming[0]
        except Exception as exc:
            self._logger.debug("Could not get next track for prebuffer: %s", exc)
        return None

    def _resolve_next_track_url(self, track: QueueItem) -> str | None:
        """Resolve the playback URL for a track."""
        if track.url:
            return track.url
        # For items without a resolved URL, we'd need to trigger resolution
        # For now, return None if URL isn't already available
        return None
