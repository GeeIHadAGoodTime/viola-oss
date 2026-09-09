from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from models.player import QueueItem
from music.exceptions import BackendError
from music.player.playback_state import PlaybackPhase

if TYPE_CHECKING:  # pragma: no cover
    from music.player import MusicPlayer

_logger = get_logger(__name__)


def _sm_transition(player, phase: PlaybackPhase, *, user_initiated: bool = False) -> None:
    """Safely transition the state machine alongside legacy flags (dual-write phase)."""
    sm = getattr(player, "_playback_sm", None)
    if sm is None:
        return
    try:
        sm.transition(phase, user_initiated=user_initiated, force=True)
    except Exception:
        pass


def _wire_browser_engine_webview(player: MusicPlayer, backend_or_engine: Any) -> bool:
    """Wire the Qt QWebEngineView to the browser engine backend or engine.

    The QWebEngineView reference is stored on the player by viola_qt.py
    during bootstrap.  This function connects it to the BrowserPlaybackEngine
    (via the EngineBackedBackend adapter) so the engine can navigate to URLs.
    """
    webview = getattr(player, "_browser_webview_ref", None)
    if webview is None:
        _logger.warning(
            "WIRE_BROWSER_WEBVIEW: player._browser_webview_ref is None — "
            "QWebEngineView not stored on player (viola_qt.py may not have run _wire_browser_webview)"
        )
        return False

    _logger.info(
        "WIRE_BROWSER_WEBVIEW: Found webview ref type=%s",
        type(webview).__name__,
    )

    # Playback normally passes an EngineBackedBackend. Bootstrap passes the
    # eagerly registered BrowserPlaybackEngine before any backend exists.
    engine = getattr(backend_or_engine, "_engine", None)
    if engine is None:
        if (
            hasattr(backend_or_engine, "_ensure_controller")
            or getattr(backend_or_engine, "provider_id", None) == "browser"
        ):
            engine = backend_or_engine
        else:
            _logger.warning(
                "WIRE_BROWSER_WEBVIEW: backend._engine is None — backend type=%s",
                type(backend_or_engine).__name__,
            )
            return False

    _logger.info(
        "WIRE_BROWSER_WEBVIEW: Found engine type=%s",
        type(engine).__name__,
    )

    shared_controller = getattr(player, "_browser_controller_ref", None)
    if shared_controller is not None and hasattr(engine, "set_controller"):
        engine.set_controller(shared_controller)
        controller = shared_controller
        _logger.info(
            "WIRE_BROWSER_WEBVIEW: Reused shared controller type=%s",
            type(controller).__name__,
        )
    else:
        controller = getattr(engine, "_controller", None)

    if controller is not None and hasattr(controller, "set_webview_controller"):
        controller.set_webview_controller(webview)
        _logger.info(
            "WIRE_BROWSER_WEBVIEW: Wired webview to existing controller type=%s",
            type(controller).__name__,
        )
        player._browser_controller_ref = controller
        return _wire_auth_manager_to_controller(controller)

    # Controller not created yet — force creation and wire it.
    if hasattr(engine, "_ensure_controller"):
        try:
            ctrl = engine._ensure_controller()
            if hasattr(ctrl, "set_webview_controller"):
                ctrl.set_webview_controller(webview)
                _logger.info(
                    "WIRE_BROWSER_WEBVIEW: Created and wired controller type=%s",
                    type(ctrl).__name__,
                )
                player._browser_controller_ref = ctrl
                return _wire_auth_manager_to_controller(ctrl)
            else:
                _logger.warning(
                    "WIRE_BROWSER_WEBVIEW: Controller %s has no set_webview_controller method",
                    type(ctrl).__name__,
                )
        except Exception:
            _logger.exception("WIRE_BROWSER_WEBVIEW: Failed to create/wire controller")
            return False
    else:
        _logger.warning(
            "WIRE_BROWSER_WEBVIEW: Engine %s has no _ensure_controller method and no controller",
            type(engine).__name__,
        )
    return False


def wire_browser_auth_controller_at_bootstrap(player: MusicPlayer) -> bool:
    """Eagerly attach BrowserAuthManager after Qt stores the browser webview."""
    webview = getattr(player, "_browser_webview_ref", None)
    if webview is None:
        _logger.debug("WIRE_BROWSER_BOOTSTRAP: no browser webview ref on player")
        return False

    manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
    if manager is None or not hasattr(manager, "get_engine"):
        _logger.debug("WIRE_BROWSER_BOOTSTRAP: no playback engine manager available")
        return False

    try:
        engine = manager.get_engine("browser")
    except Exception:
        _logger.exception("WIRE_BROWSER_BOOTSTRAP: failed to register browser engine")
        return False

    if engine is None:
        _logger.debug("WIRE_BROWSER_BOOTSTRAP: browser engine unavailable")
        return False

    wired = _wire_browser_engine_webview(player, engine)
    if wired:
        _logger.info("WIRE_BROWSER_BOOTSTRAP: browser auth controller wired eagerly")
    return wired


def _wire_auth_manager_to_controller(controller) -> bool:
    """Wire the BrowserAuthManager singleton to the BrowserPlaybackController.

    Called after the controller is created so the auth manager can inject
    JS for login detection and navigate the webview for login flows.
    """
    try:
        from music.providers.browser.auth_manager import get_browser_auth_manager

        auth_mgr = get_browser_auth_manager()
        auth_mgr.set_controller(controller)
        _logger.info("WIRE_AUTH_MANAGER: Wired auth manager to controller")
        return True
    except Exception:
        _logger.exception("WIRE_AUTH_MANAGER: Failed to wire auth manager")
        return False


def get_browser_backend_display_name(item: QueueItem) -> str:
    provider_id = getattr(item, "provider", None)
    if provider_id == "youtube_music":
        return "YouTube Music (Browser)"
    return "Browser"


def play_external_browser(
    player: MusicPlayer,
    logger,
    item: QueueItem,
) -> bool:
    url = getattr(item, "url", None)
    if not url:
        logger.error("external_browser playback requested but item.url is missing: %r", item)
        player._record_queue_failure(item, stage="missing_url", metadata={})
        return False

    logger.info(
        "PLAYBACK_BROWSER_MODE: Opening URL in browser video_id=%s title=%s url=%s",
        item.video_id or "none",
        item.title or "Unknown",
        url[:80],
    )

    try:
        webbrowser.open(url)

        with player._cv:
            player._state.backend = "browser"
            player._state.backend_display_name = get_browser_backend_display_name(item)
            player._state.backend_capabilities = {
                "play": True,
                "stop": True,
                "can_seek": False,
                "supports_volume": False,
                "can_skip_next": True,
                "can_skip_previous": True,
            }
            player._state.playback_mode = "external_browser"

            player._set_now_playing_locked(item)
            player._state.is_playing = True
            _sm_transition(player, PlaybackPhase.PLAYING)
            # Queue mutations auto-invalidate - no sync needed
            player._cv.notify_all()

        logger.info(
            "ENGINE_PLAY_BROWSER provider=%s url=%s backend=%s",
            item.provider or "unknown",
            url[:80],
            player._state.backend,
        )
        player._emit()
        return True
    except Exception as exc:
        logger.error("Failed to open browser URL %s: %r", url, exc)
        player._record_queue_failure(item, stage="browser_open_failed", metadata={"error": str(exc)})
        return False


def play_embedded_iframe_webview(
    player: MusicPlayer,
    logger,
    item: QueueItem,
) -> bool:
    url = getattr(item, "url", None)
    video_id = getattr(item, "video_id", None)
    if not (video_id or url):
        logger.error(
            "embedded_iframe_webview requested but neither video_id nor url present: %r",
            item,
        )
        player._record_queue_failure(item, stage="missing_video_id", metadata={})
        return False

    try:
        from music.backends.youtube_iframe_backend import YouTubeIFrameBackend
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "IFRAME_WEBVIEW_IMPORT_FAILED code=iframe_import_failed error=%r",
            exc,
        )
        player._record_queue_failure(
            item,
            stage="iframe_import_failed",
            metadata={"error": str(exc)},
        )
        return False

    try:
        backend = None
        if player._backend is not None and isinstance(player._backend, YouTubeIFrameBackend):
            backend = player._backend
        else:
            backend = YouTubeIFrameBackend(logger=logger, webview_controller=None)
            player._backend = backend
            player._backend_name = "youtube_iframe"

        with player._cv:
            player._state.backend = "youtube_iframe"
            player._state.backend_display_name = "YouTube (IFrame)"
            player._state.backend_capabilities = {
                "play": True,
                "stop": True,
                "can_seek": False,
                "supports_volume": False,
                "can_skip_next": True,
                "can_skip_previous": True,
            }
            player._state.playback_mode = "embedded_iframe_webview"
            try:
                asset_base = "/static/webviews/youtube_iframe.html"
                if video_id:
                    embed_url = f"{asset_base}?autoplay=1&video={video_id}"
                else:
                    embed_url = asset_base
                object.__setattr__(item, "embed_url", embed_url)  # QueueItem has extra="allow"
            except Exception as e:
                logger.exception("Embed URL creation failed: %s", e)
                pass  # Silent OK: embed URL creation fallback
            player._set_now_playing_locked(item)
            player._state.is_playing = False
            _sm_transition(player, PlaybackPhase.IDLE)
            # Queue mutations auto-invalidate - no sync needed
            player._cv.notify_all()

        # Trigger ProcTap rescan so it catches any new QtWebEngine process
        # within ~200ms instead of waiting for the next normal poll cycle.
        try:
            from audio_core.streaming.pipeline_wiring import (
                request_proctap_rescan,
            )

            request_proctap_rescan()
        except Exception:
            pass  # Multiroom may not be active

        # Check if we have a webview controller (Qt native mode with direct webview control)
        # If not, we're in React mode where React UI renders the iframe based on state
        has_webview_controller = backend._webview_controller is not None

        if has_webview_controller:
            # Qt native mode: backend controls webview directly
            try:
                if video_id:
                    backend.play(item)
                else:
                    backend.play(url or "")
            except BackendError as exc:
                logger.exception(
                    "Backend play failed (BackendError) in _play_embedded_iframe_webview: %r",
                    exc,
                )
                with player._cv:
                    player._state.is_playing = False
                    _sm_transition(player, PlaybackPhase.ERROR)
                    if not hasattr(player._state, "playback_errors"):
                        player._state.playback_errors = []
                    player._state.playback_errors.append(
                        {
                            "error": "backend_error",
                            "message": str(exc),
                            "backend": "youtube_iframe",
                            "stage": "play_embedded_iframe_webview",
                        }
                    )
                    if len(player._state.playback_errors) > 10:
                        player._state.playback_errors = player._state.playback_errors[-10:]
                    player._cv.notify_all()
                player._record_queue_failure(
                    item,
                    stage="backend_error",
                    metadata={"error": str(exc), "backend": "youtube_iframe"},
                )
                player._emit()
                return False
        else:
            # React mode: no webview controller attached
            # React UI will render the iframe based on player state (now_playing, is_playing)
            # State was already set above (now_playing, backend, playback_mode)
            logger.info(
                "YTI_REACT_MODE no webview controller - React UI will render iframe via state video_id=%s",
                video_id or "none",
            )

        logger.info(
            "ENGINE_PLAY_IFRAME_EMBED provider=%s video_id=%s url=%s backend=%s",
            item.provider or "unknown",
            video_id or "none",
            (url or "")[:80],
            player._state.backend,
        )
        # Set is_playing=True AFTER successful backend.play() to signal wake policy
        with player._cv:
            player._state.is_playing = True
            _sm_transition(player, PlaybackPhase.PLAYING)
            player._cv.notify_all()
        player._emit()
        return True
    except Exception as exc:
        logger.exception(
            "IFRAME_EMBEDDED_WEBVIEW_FAILED code=iframe_webview_failed id=%s url=%s error=%r",
            video_id or "none",
            (url or "")[:80],
            exc,
        )
        with player._cv:
            player._state.is_playing = False
            _sm_transition(player, PlaybackPhase.ERROR)
            if isinstance(exc, BackendError):
                if not hasattr(player._state, "playback_errors"):
                    player._state.playback_errors = []
                player._state.playback_errors.append(
                    {
                        "error": "backend_error",
                        "message": str(exc),
                        "backend": "youtube_iframe",
                        "stage": "play_embedded_iframe_webview",
                    }
                )
                if len(player._state.playback_errors) > 10:
                    player._state.playback_errors = player._state.playback_errors[-10:]
            player._record_queue_failure(
                item,
                stage="embedded_iframe_webview_failed",
                metadata={
                    "error": "iframe_embedded_webview_failed",
                    "reason": str(exc),
                    "backend": "youtube_iframe",
                },
            )
        player._emit()
        return False


def play_embedded_webview(
    player: MusicPlayer,
    logger,
    item: QueueItem,
) -> bool:
    from config.settings import settings

    strategy = settings.ytm_fallback_strategy.strip().lower()
    url = getattr(item, "url", None)
    if not url:
        logger.error("embedded_webview playback requested but item.url is missing: %r", item)
        player._record_queue_failure(item, stage="missing_url", metadata={})
        return False

    manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
    if manager is not None:
        try:
            provider_id = manager.identify_provider(item)
            if provider_id:
                with player._cv:
                    upcoming = list(player._playlist.upcoming_ref)
                selected_backend = manager.attach_backend(
                    item,
                    upcoming=upcoming,
                    default_backend=None,
                )
                if selected_backend is not None:
                    backend = selected_backend
                    backend_name = type(backend).__name__
                    with player._cv:
                        player._backend = backend
                        player._backend_name = backend_name
                        playback_mode = getattr(item, "playback_mode", None) or "embedded_webview"
                        player._state.playback_mode = playback_mode
                        player._backend_manager.configure_backend_state(backend)
                        player._set_now_playing_locked(item)
                        player._state.is_playing = False
                        _sm_transition(player, PlaybackPhase.LOADING)
                        # Queue mutations auto-invalidate - no sync needed
                        player._cv.notify_all()
                    # Wire browser webview if this is the browser provider
                    if provider_id == "browser":
                        _wire_browser_engine_webview(player, backend)

                    try:
                        backend.play_url(item.url)
                        logger.info(
                            "ENGINE_PLAY_EMBEDDED_WEBVIEW provider=%s url=%s using_engine_manager=True",
                            provider_id,
                            item.url[:80] if item.url else "none",
                        )
                        # Set is_playing=True AFTER successful backend.play_url() to signal wake policy
                        with player._cv:
                            player._state.is_playing = True
                            _sm_transition(player, PlaybackPhase.PLAYING)
                            player._cv.notify_all()
                        player._emit()
                        return True
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.exception(
                            "ENGINE_BACKEND_PLAY_FAILED code=engine_backend_play_failed provider=%s title=%s error=%r",
                            provider_id,
                            item.title,
                            exc,
                        )
                        player._record_queue_failure(
                            item,
                            stage="engine_backend_play_failed",
                            metadata={"error": str(exc), "provider": provider_id},
                        )
                        return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ENGINE_ATTACH_UNAVAILABLE code=engine_attach_unavailable reason=%r",
                exc,
            )

    if strategy == "external_browser":
        logger.warning(
            "YTM_FALLBACK_EXTERNAL_BROWSER reason=engine_unavailable provider=%s url=%s",
            item.provider or "youtube_music",
            url[:80],
        )
        return play_external_browser(player, logger, item)

    if strategy == "legacy_webview":
        logger.warning(
            "YTM_FALLBACK_LEGACY_WEBVIEW_ENABLED reason=explicit_opt_in provider=%s url=%s",
            item.provider or "youtube_music",
            url[:80],
        )
        try:
            from music.backends.youtube_web_backend import YouTubeWebBackend
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "LEGACY_WEBVIEW_IMPORT_FAILED code=legacy_import_failed error=%r",
                exc,
            )
            player._record_queue_failure(
                item,
                stage="legacy_webview_import_failed",
                metadata={"error": str(exc)},
            )
            return False

        try:
            youtube_web_backend = None
            if player._backend is not None and isinstance(player._backend, YouTubeWebBackend):
                youtube_web_backend = player._backend
            else:
                youtube_web_backend = YouTubeWebBackend(
                    logger=logger,
                    webview_controller=None,
                )
                player._backend = youtube_web_backend
                player._backend_name = "youtube_web"

            with player._cv:
                player._state.backend = "youtube_web"
                player._state.backend_display_name = "YouTube Music (Embedded)"
                player._state.backend_capabilities = {
                    "play": True,
                    "stop": True,
                    "can_seek": False,
                    "supports_volume": False,
                    "can_skip_next": False,
                    "can_skip_previous": False,
                }
                player._state.playback_mode = "embedded_webview"
                player._set_now_playing_locked(item)
                player._state.is_playing = False
                _sm_transition(player, PlaybackPhase.LOADING)
                # Queue mutations auto-invalidate - no sync needed
                player._cv.notify_all()

            try:
                youtube_web_backend.play_url(url)
            except BackendError as exc:
                logger.exception(
                    "Backend play failed (BackendError) in _play_embedded_webview: %r",
                    exc,
                )
                with player._cv:
                    player._state.is_playing = False
                    _sm_transition(player, PlaybackPhase.ERROR)
                    if not hasattr(player._state, "playback_errors"):
                        player._state.playback_errors = []
                    player._state.playback_errors.append(
                        {
                            "error": "backend_error",
                            "message": str(exc),
                            "backend": "youtube_web",
                            "stage": "play_embedded_webview",
                        }
                    )
                    if len(player._state.playback_errors) > 10:
                        player._state.playback_errors = player._state.playback_errors[-10:]
                    player._cv.notify_all()
                player._record_queue_failure(
                    item,
                    stage="backend_error",
                    metadata={"error": str(exc), "backend": "youtube_web"},
                )
                player._emit()
                return False

            logger.info(
                "ENGINE_PLAY_EMBEDDED_WEBVIEW provider=%s url=%s backend=%s",
                item.provider or "unknown",
                url[:80],
                player._state.backend,
            )
            # Set is_playing=True AFTER successful backend.play_url() to signal wake policy
            with player._cv:
                player._state.is_playing = True
                _sm_transition(player, PlaybackPhase.PLAYING)
                player._cv.notify_all()
            player._emit()
            return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "LEGACY_WEBVIEW_FAILED code=legacy_webview_failed error=%r",
                exc,
            )
            with player._cv:
                player._state.is_playing = False
                _sm_transition(player, PlaybackPhase.ERROR)
                player._cv.notify_all()
            player._record_queue_failure(
                item,
                stage="legacy_webview_failed",
                metadata={"error": str(exc)},
            )
            return False

    logger.error(
        "YTM_EMBEDDED_PLAYBACK_FAILED reason=no_strategy provider=%s url=%s",
        item.provider or "youtube_music",
        url[:80],
    )
    player._record_queue_failure(item, stage="ytm_embedded_playback_failed", metadata={"strategy": strategy})
    return False


__all__ = [
    "get_browser_backend_display_name",
    "play_embedded_iframe_webview",
    "play_embedded_webview",
    "play_external_browser",
]
