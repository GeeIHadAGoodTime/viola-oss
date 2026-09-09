from __future__ import annotations

import datetime
import os
import platform
import sys

from fastapi.responses import FileResponse, JSONResponse

from config.settings import settings as app_settings
from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Body, Depends, HTTPException
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.core.player_state import to_player_state

log = get_logger(__name__)


from ui.api.routes._guards import require_dev_mode as _require_dev_mode


def register_diagnostics_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    bindings = context.bindings
    music = bindings.music
    state = bindings.state
    app = context.app

    @router.get("/v1/diagnostics", dependencies=[Depends(require_auth)])
    async def diagnostics():
        async def _inner():
            try:
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()

                # Get backend from player (music is MusicControllerAdapter wrapper)
                player = getattr(music, "player", music)
                backend = getattr(player, "_backend", None)
                recent_failures: list[dict[str, str]] = []
                music_diag = {
                    "backend_type": (type(backend).__name__ if backend is not None else "unknown"),
                    "backend_available": backend is not None,
                }
                if backend is None:
                    recent_failures.append(
                        {
                            "subsystem": "music",
                            "message": "Playback backend is unavailable.",
                        }
                    )

                try:
                    hub_authority = getattr(app.state, "hub_state_authority", None)
                    player_state_model = to_player_state(music, state, hub_authority=hub_authority)
                    music_diag.update(
                        {
                            "is_playing": player_state_model.is_playing,
                            "has_now_playing": player_state_model.now_playing is not None,
                            "queue_size": len(player_state_model.queue),
                            "volume": player_state_model.volume,
                            "position": player_state_model.position,
                            "duration": player_state_model.duration,
                            "position_percentage": player_state_model.position_percentage,
                        }
                    )
                except Exception:
                    log.exception("Player state diagnostics failed")
                    recent_failures.append(
                        {
                            "subsystem": "music",
                            "message": "Player state diagnostics failed.",
                        }
                    )
                    music_diag["state_message"] = "Player state is temporarily unavailable."

                autoplay_diag = {
                    "autoplay_enabled": settings_mgr.get("autoplay_enabled", False),
                    "autoplay_min_queue": settings_mgr.get("autoplay_min_queue", 7),
                    "autoplay_backend": "youtube_music_radio",  # Now uses YTM Radio instead of AI
                }

                try:
                    from music.autoplay import get_ytmusic_radio

                    radio = get_ytmusic_radio()
                    autoplay_diag.update(
                        {
                            "radio_available": radio.is_available,
                            "radio_is_fetching": radio._is_fetching,
                        }
                    )
                except Exception:
                    log.exception("Autoplay diagnostics failed")
                    recent_failures.append(
                        {
                            "subsystem": "autoplay",
                            "message": "Autoplay diagnostics failed.",
                        }
                    )
                    autoplay_diag["message"] = "Autoplay diagnostics are temporarily unavailable."

                settings_diag = {
                    "voice_mode": settings_mgr.get("voice_mode", "unknown"),
                    "stt_engine": settings_mgr.get("stt_engine", "unknown"),
                    "whisper_model": settings_mgr.get("whisper_model", "unknown"),
                    "tts_enabled": settings_mgr.get("tts_enabled", False),
                    "llm_model": settings_mgr.get("llm_model", "unknown"),
                    "enable_gpt": settings_mgr.get("enable_gpt", False),
                }

                # Add resolved wake config from settings (single source of truth)
                settings_diag["resolved_wake_config"] = {
                    "wake_enabled": getattr(app_settings, "wake_enabled", False),
                    "wake_engine": getattr(app_settings, "wake_engine", "none"),
                    "wake_keyword_path": getattr(app_settings, "wake_keyword_path", None),
                }

                system_diag = {
                    "python_version": sys.version,
                    "platform": platform.platform(),
                    "cwd": os.getcwd(),
                    "runtime_profile": getattr(state, "runtime_profile", "unknown"),
                    "runtime_capabilities": getattr(state, "runtime_capabilities", {}),
                }

                # Wake word metrics
                wake_diag = {}
                try:
                    from diagnostics.wake_metrics import get_wake_metrics

                    wake_metrics = get_wake_metrics()
                    wake_diag = wake_metrics.get_metrics_dict()
                except Exception:
                    log.exception("Wake metrics diagnostics failed")
                    recent_failures.append(
                        {
                            "subsystem": "wake_word",
                            "message": "Wake metrics diagnostics failed.",
                        }
                    )
                    wake_diag = {
                        "message": "Wake metrics are temporarily unavailable.",
                    }

                # AEC diagnostics
                aec_diag = {}
                try:
                    from voice.wake_detector.facade import WakeDetectorFacade

                    facade = WakeDetectorFacade.get_instance()
                    if facade:
                        # ViolaWakeListener stored directly or wrapped in WakeDetectorAdapter
                        detector = getattr(facade, "_raw_listener", facade)
                        proc = getattr(detector, "_aec_processor", None)
                        if proc is not None:
                            aec_diag = proc.get_diagnostics()
                            aec_diag["backend"] = getattr(proc, "name", "unknown")
                        else:
                            aec_diag = {"backend": "none", "status": "no_processor"}
                    else:
                        aec_diag = {"status": "no_listener"}
                except Exception:
                    log.exception("AEC diagnostics unavailable")
                    recent_failures.append(
                        {
                            "subsystem": "aec",
                            "message": "AEC diagnostics are unavailable.",
                        }
                    )
                    aec_diag = {"message": "AEC diagnostics are temporarily unavailable."}

                overall_status = "degraded" if recent_failures else "ok"
                from diagnostics.voice_status import get_voice_status

                return success_response(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "overall_status": overall_status,
                        "recent_failures": recent_failures,
                        "music": music_diag,
                        "autoplay": autoplay_diag,
                        "settings": settings_diag,
                        "system": system_diag,
                        "voice_status": get_voice_status(),
                        "wake_word": wake_diag,
                        "aec": aec_diag,
                        "websocket_clients": (len(context.hub._clients) if hasattr(context.hub, "_clients") else 0),
                    }
                )
            except Exception:
                log.exception("Diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "diagnostics_failed",
                        "Couldn't gather diagnostics right now. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics", method="GET")

    @router.get("/v1/diagnostics/summary", dependencies=[Depends(require_auth)])
    async def diagnostics_summary(user_id: str = Depends(get_current_user_id)):
        """Lightweight diagnostics summary (platform, runtime, memory, websocket clients)."""

        async def _inner():
            try:
                system_summary = {
                    "timestamp": datetime.datetime.now().isoformat(),
                    "python_version": sys.version.split()[0],
                    "platform": platform.platform(),
                    "runtime_profile": getattr(state, "runtime_profile", "unknown"),
                }

                # Memory count
                try:
                    from services.memory.store import get_memory_store

                    store = get_memory_store()
                    system_summary["memory_count"] = store.count_active(user_id)
                except Exception:
                    system_summary["memory_count"] = 0

                # Websocket clients
                system_summary["websocket_clients"] = (
                    len(context.hub._clients) if hasattr(context.hub, "_clients") else 0
                )

                # Wake engine
                system_summary["wake_engine"] = getattr(app_settings, "wake_engine", "none")

                return success_response(system_summary)
            except Exception:
                log.exception("Diagnostics summary failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "diagnostics_summary_failed",
                        "Couldn't gather diagnostics summary. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/summary", method="GET")

    @router.get("/v1/diagnostics/cache", dependencies=[Depends(require_auth)])
    async def cache_diagnostics():
        async def _inner():
            try:
                try:
                    from music.providers.youtube_core import get_shared_cache_stats
                except ImportError:
                    return JSONResponse(
                        status_code=500,
                        content=failure_response(
                            "cache_diagnostics_unavailable",
                            "YouTube cache diagnostics are temporarily unavailable.",
                        ),
                    )

                return success_response(get_shared_cache_stats())
            except Exception:
                log.exception("Cache diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "cache_diagnostics_failed",
                        "Couldn't load cache diagnostics. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/cache", method="GET")

    @router.post("/v1/debug/trigger-autoplay", dependencies=[Depends(require_auth), Depends(_require_dev_mode)])
    async def trigger_autoplay(body: dict = Body(default={})):
        async def _inner():
            try:
                from ui.core.player_state import to_player_state
                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()

                log.info("🔧 DEBUG: Manual autoplay trigger requested")

                hub_authority = getattr(app.state, "hub_state_authority", None)
                player_state = to_player_state(music, state, hub_authority=hub_authority)

                log.info("🔧 DEBUG: Current queue size: %d", len(player_state.queue))
                log.info("🔧 DEBUG: Now playing: %s", player_state.now_playing)
                log.info(
                    "🔧 DEBUG: Autoplay enabled: %s",
                    settings_mgr.get("autoplay_enabled", False),
                )

                # Trigger autoplay via controller if available
                autoplay_controller = getattr(music, "autoplay_controller", None)
                if autoplay_controller:
                    added = await autoplay_controller.ensure_buffer(
                        current_track=player_state.now_playing,
                    )
                    log.info("🔧 DEBUG: Autoplay added %d tracks", added)
                else:
                    log.warning("🔧 DEBUG: No autoplay controller available")

                return success_response(
                    {
                        "message": "Autoplay triggered manually (YouTube Music Radio)",
                        "new_queue_size": len(to_player_state(music, state, hub_authority=hub_authority).queue),
                    }
                )
            except HTTPException:
                raise
            except Exception:
                log.exception("Failed to trigger autoplay")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("autoplay_failed", "Couldn't trigger autoplay. Please try again."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/debug/trigger-autoplay", method="POST")

    @router.get("/v1/diagnostics/wake-word", dependencies=[Depends(require_auth)])
    async def wake_word_diagnostics():
        """Get wake word diagnostics metrics."""

        async def _inner():
            try:
                from diagnostics.wake_metrics import get_wake_metrics

                wake_metrics = get_wake_metrics()

                # Get policy diagnostics (critical for debugging false positives)
                policy_diag = {}
                try:
                    from voice.wake_detector.wake_decision_policy import get_wake_policy

                    policy = get_wake_policy()
                    base_diag = policy.get_diagnostics()

                    # Add presence-based config info
                    policy_diag = {
                        **base_diag,
                        "presence_threshold": policy._config.playback_presence_rms_threshold,
                        "echo_veto_threshold": policy._config.echo_veto_rms_threshold,
                    }
                except Exception as policy_exc:
                    log.debug("Policy diagnostics unavailable: %s", policy_exc)
                    policy_diag = {"error": "policy_unavailable"}

                from ui.settings_manager import get_settings_manager

                settings_mgr = get_settings_manager()

                # Contributor mode info
                contributor_mode_diag = {
                    "enabled": settings_mgr.get("contributor_mode_enabled", False),
                    "samples_pending": 0,
                }

                # Try to get sample count from contributor mode manager
                try:
                    from voice.wake_detector.contributor_mode import (
                        get_contributor_manager,
                    )

                    manager = get_contributor_manager()
                    contributor_mode_diag["is_active"] = manager.is_active
                    contributor_mode_diag["samples_pending"] = manager.get_pending_upload_count()
                    if manager.is_active:
                        contributor_mode_diag["time_remaining_seconds"] = manager.time_remaining_seconds
                except Exception as cm_exc:
                    log.debug("Contributor mode info unavailable: %s", cm_exc)

                return success_response(
                    {
                        "timestamp": datetime.datetime.now().isoformat(),
                        "metrics": wake_metrics.get_metrics_dict(),
                        "policy": policy_diag,
                        "contributor_mode": contributor_mode_diag,
                        "config": {
                            "vad_gate_enabled": policy_diag.get("config", {}).get("vad_gate_enabled", False),
                            "echo_veto_enabled": policy_diag.get("config", {}).get("echo_veto_enabled", False),
                            "confirmation_enabled": policy_diag.get("config", {}).get("confirmation_enabled", False),
                            "threshold_boost_factor": policy_diag.get("config", {}).get("threshold_boost_factor", 1.0),
                        },
                    }
                )
            except Exception:
                log.exception(
                    "Wake word diagnostics failed",
                    route="/v1/diagnostics/wake-word",
                    method="GET",
                )
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "wake_diagnostics_failed", "Couldn't load wake word diagnostics. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/wake-word", method="GET")

    @router.get("/v1/diagnostics/ai-debug", dependencies=[Depends(require_auth)])
    async def ai_debug_diagnostics(user_id: str = Depends(get_current_user_id)):
        """Get AI-friendly diagnostic payload for Claude Code debugging.

        This endpoint provides structured diagnostic information optimized for
        AI analysis. Use this when debugging with Claude Code to get:
        - Subsystem health with severity levels and recommendations
        - Recent command history with execution results
        - Playback state
        - Actionable recommendations

        Query params:
            include_raw: Include raw metrics snapshot (verbose, default false)
        """

        async def _inner(include_raw: bool = False):
            try:
                from diagnostics.ai_debugger import collect_diagnostics

                # Create a state holder with music reference
                class AppState:
                    def __init__(self, music_instance: object) -> None:
                        self.music = music_instance

                app_state = AppState(music)

                diagnostics = collect_diagnostics(
                    app_state=app_state,
                    include_raw_metrics=include_raw,
                    user_id=user_id,
                )
                return success_response(diagnostics.to_dict())
            except Exception:
                log.exception("AI diagnostics collection failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "ai_diagnostics_failed", "Couldn't load AI diagnostics. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/ai-debug", method="GET")

    @router.get("/v1/diagnostics/youtube", dependencies=[Depends(require_auth)])
    async def youtube_health():
        """Check YouTube embed health.

        Probes YouTube's embed endpoint to detect issues before users report them.
        Returns:
        - status: healthy, degraded, blocked, quota_exceeded, network_error, unknown
        - latency_ms: Response time in milliseconds
        - error: Error message if any

        Use this to proactively detect:
        - YouTube API quota issues
        - Embed blocking (region, age-restricted)
        - Network connectivity problems
        """

        async def _inner():
            try:
                from diagnostics.youtube_health import (
                    YouTubeHealthStatus,
                    check_youtube_embed_health,
                )

                result = await check_youtube_embed_health()

                if result.status == YouTubeHealthStatus.HEALTHY:
                    return success_response(result.to_dict())
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "youtube_degraded",
                        "YouTube health check found playback issues.",
                        data=result.to_dict(),
                    ),
                )
            except Exception:
                log.exception("YouTube health check failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "youtube_health_failed", "Couldn't check YouTube connection health. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/youtube", method="GET")

    @router.get("/v1/diagnostics/export-bundle", dependencies=[Depends(require_auth)])
    async def export_diagnostics_bundle():
        """Export a diagnostics bundle as a ZIP file.

        Creates a comprehensive diagnostics bundle containing:
        - Recent log files (tailed)
        - Runtime metrics snapshot
        - Failure data and queue state
        - Sanitized settings
        - Hardware/system information

        Returns the ZIP file for download. Use this for support requests
        or debugging complex issues.
        """

        async def _inner():
            try:
                from diagnostics.bundle_exporter import DiagnosticsBundleExporter

                exporter = DiagnosticsBundleExporter()
                zip_path = exporter.export()

                return FileResponse(
                    path=str(zip_path),
                    media_type="application/zip",
                    filename=zip_path.name,
                )
            except Exception:
                log.exception("Diagnostics bundle export failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "bundle_export_failed", "Couldn't export the diagnostics bundle. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/export-bundle", method="GET")

    # Alias: /v1/diagnostics/export -> same handler as /v1/diagnostics/export-bundle
    @router.get("/v1/diagnostics/export", dependencies=[Depends(require_auth)])
    async def export_diagnostics_bundle_alias():
        """Alias for /v1/diagnostics/export-bundle (shorter path used by docs and tooling)."""
        return await export_diagnostics_bundle()

    # Contributor Mode endpoints are registered in ui/api/routes/training.py

    @router.get("/v1/diagnostics/aec-snapshot", dependencies=[Depends(require_auth)])
    async def aec_snapshot_diagnostics():
        """Get comprehensive AEC diagnostic snapshot for AI tuning.

        Returns a single JSON payload with everything an AI needs to
        evaluate and tune the echo canceller: filter parameters,
        instantaneous state, 60-second rolling stats, delay calibration,
        frame history, and a pre-computed assessment.

        Use this endpoint (instead of piecing together /v1/diagnostics
        and log files) when debugging AEC behavior or tuning parameters.
        """

        async def _inner():
            try:
                from voice.wake_detector.facade import WakeDetectorFacade

                facade = WakeDetectorFacade.get_instance()
                if not facade:
                    return success_response(
                        {
                            "aec_snapshot": None,
                            "message": "Wake detection is not running.",
                        }
                    )

                # Reach the ViolaWakeListener through the facade
                detector = getattr(facade, "_raw_listener", facade)
                snapshot_fn = getattr(detector, "get_aec_snapshot", None)

                if snapshot_fn is None or not callable(snapshot_fn):
                    return success_response(
                        {
                            "aec_snapshot": None,
                            "message": ("AEC snapshot is unavailable for the current wake detector."),
                        }
                    )

                snapshot = snapshot_fn()
                return success_response({"aec_snapshot": snapshot})

            except Exception:
                log.exception("AEC snapshot diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "aec_snapshot_failed",
                        "Couldn't capture AEC diagnostics. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/aec-snapshot", method="GET")

    @router.get("/v1/diagnostics/playback-stack", dependencies=[Depends(require_auth)])
    async def playback_stack_diagnostics():
        """Get backend playback state diagnostics.

        Returns the Python backend's view of playback state:
        - is_playing, is_paused flags
        - Current position and duration
        - Now playing track info
        - Backend type and availability
        - Internal player flags

        Use this to understand what the backend believes about playback state.
        """

        async def _inner():
            try:
                from diagnostics.playback_stack import collect_backend_diagnostics

                backend_diag = collect_backend_diagnostics(music, state)
                return success_response(backend_diag.to_dict())
            except Exception:
                log.exception("Playback stack diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "playback_stack_failed", "Couldn't load playback diagnostics. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/playback-stack", method="GET")

    @router.get("/v1/diagnostics/full-playback-state", dependencies=[Depends(require_auth)])
    async def full_playback_state_diagnostics():
        """Get comprehensive playback state from ALL layers.

        Combines diagnostics from:
        - Backend: Python player state, internal flags, errors
        - Frontend: YouTube IFrame API state, player instance, iframe visibility
        - Consistency: Checks for mismatches between layers

        Returns:
        {
            "backend": {"is_playing": true, "position_ms": 45000, ...},
            "frontend": {"yt_api_loaded": true, "player_state": 1, ...},
            "consistency": {"all_layers_agree": true, "issues": []}
        }

        Use this as the primary diagnostic tool for playback issues.
        If frontend is null, call /v1/diagnostics/request-frontend-state first.
        """

        async def _inner():
            try:
                from diagnostics.playback_stack import collect_full_playback_state

                full_state = collect_full_playback_state(music, state)
                return success_response(full_state.to_dict())
            except Exception:
                log.exception("Full playback state diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "full_playback_state_failed", "Couldn't load full playback state. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/full-playback-state", method="GET")

    @router.post("/v1/diagnostics/request-frontend-state", dependencies=[Depends(require_auth)])
    async def request_frontend_state(user_id: str = Depends(get_current_user_id)):
        """Request the frontend to send its diagnostic state.

        Broadcasts a request to all WebSocket clients asking them to
        collect and send their YouTube player state. The response will
        be stored and available via /v1/diagnostics/full-playback-state.

        This is useful when you need fresh frontend state before checking
        consistency.
        """

        async def _inner():
            try:
                # Broadcast request to all WebSocket clients
                await context.hub.broadcast(
                    "diagnostic_request",
                    {
                        "request_type": "playback_state",
                        "timestamp": datetime.datetime.now().isoformat(),
                    },
                    user_id=user_id,
                    force=True,
                )
                return success_response(
                    {
                        "message": "Frontend diagnostic request broadcast. Check /v1/diagnostics/full-playback-state after ~1 second.",
                    }
                )
            except Exception:
                log.exception("Failed to request frontend state")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "request_failed", "Couldn't complete the diagnostics request. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/request-frontend-state", method="POST")

    @router.get("/v1/diagnostics/browser-webview", dependencies=[Depends(require_auth)])
    async def browser_webview_diagnostics():
        """Get browser-native playback chain diagnostics.

        Walks the full object chain from ViolaWebViewWindow through to
        BrowserPlaybackController and reports the actual runtime state of
        each link.  Use this to diagnose silent playback failures when the
        browser provider reports is_playing=true but no audio is captured.
        """

        async def _inner():
            try:
                result: dict = {
                    "browser_provider_enabled": getattr(app_settings, "browser_provider_enabled", False),
                    "browser_webview_created": False,
                    "browser_webview_url": None,
                    "browser_webview_message": None,
                    "player_has_webview_ref": False,
                    "controller_has_webview": False,
                    "controller_last_navigate_url": None,
                    "controller_last_navigate_error": None,
                    "controller_is_playing": False,
                    "controller_playback_state": "unknown",
                    "stack_index_current": None,
                    "qtwebengineprocess_pids": [],
                    "qtwebengineprocess_message": None,
                }

                # Walk the runtime object chain
                player = getattr(music, "player", music)

                # 1. Check player._browser_webview_ref
                webview_ref = getattr(player, "_browser_webview_ref", None)
                result["player_has_webview_ref"] = webview_ref is not None

                if webview_ref is not None:
                    result["browser_webview_created"] = True
                    # Try to get the currently loaded URL
                    try:
                        page = webview_ref.page() if callable(getattr(webview_ref, "page", None)) else None
                        if page is not None:
                            url_obj = page.url()
                            result["browser_webview_url"] = (
                                url_obj.toString() if hasattr(url_obj, "toString") else str(url_obj)
                            )
                        elif hasattr(webview_ref, "url"):
                            url_obj = webview_ref.url()
                            result["browser_webview_url"] = (
                                url_obj.toString() if hasattr(url_obj, "toString") else str(url_obj)
                            )
                    except Exception:
                        log.exception("Browser webview URL inspection failed")
                        result["browser_webview_message"] = "Browser preview details are temporarily unavailable."

                # 2. Walk backend → engine → controller
                backend = getattr(player, "_backend", None)
                engine = getattr(backend, "_engine", None) if backend is not None else None
                controller = getattr(engine, "_controller", None) if engine is not None else None

                if controller is not None:
                    ctrl_webview = getattr(controller, "_webview_controller", None)
                    result["controller_has_webview"] = ctrl_webview is not None
                    result["controller_last_navigate_url"] = getattr(controller, "_current_url", None)
                    result["controller_is_playing"] = getattr(controller, "_is_playing", False)

                    try:
                        result["controller_playback_state"] = controller.get_playback_state()
                    except Exception:
                        log.exception("Browser controller playback state check failed")
                        result["controller_playback_state"] = "unavailable"

                # 3. Get stack index from ViolaWebViewWindow (if accessible)
                try:
                    from PySide6.QtWidgets import QApplication

                    from ui.qt_native.webview_window import ViolaWebViewWindow

                    app_instance = QApplication.instance()
                    if app_instance is not None:
                        for widget in app_instance.topLevelWidgets():
                            if isinstance(widget, ViolaWebViewWindow):
                                stack = getattr(widget, "stack", None)
                                if stack is not None and hasattr(stack, "currentIndex"):
                                    result["stack_index_current"] = stack.currentIndex()
                                # Also check if browser_webview attr exists on window
                                if not result["browser_webview_created"]:
                                    bw = getattr(widget, "browser_webview", None)
                                    result["browser_webview_created"] = bw is not None
                                break
                except Exception as exc:
                    log.debug("Stack index check failed: %s", exc)

                # 4. Find QtWebEngineProcess PIDs
                try:
                    import psutil

                    pids = []
                    for proc in psutil.process_iter(["pid", "name"]):
                        try:
                            if "QtWebEngineProcess" in (proc.info["name"] or ""):
                                pids.append(proc.info["pid"])
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    result["qtwebengineprocess_pids"] = pids
                except ImportError:
                    result["qtwebengineprocess_message"] = "Process inspection is unavailable on this system."
                except Exception:
                    log.exception("Qt WebEngine process inspection failed")
                    result["qtwebengineprocess_message"] = "Qt WebEngine process details are temporarily unavailable."

                return success_response(result)
            except Exception:
                log.exception("Browser webview diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "browser_webview_diagnostics_failed",
                        "Couldn't load browser diagnostics. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/browser-webview", method="GET")

    # ------------------------------------------------------------------
    # Self-check endpoint (real subsystem health)
    # ------------------------------------------------------------------

    @router.get("/v1/diagnostics/self-check", dependencies=[Depends(require_auth)])
    async def self_check():
        """Run a real health check against all core subsystems.

        Returns structured per-subsystem status with an overall roll-up.
        Nothing is hardcoded — each check probes live runtime state.

        Response shape::

            {
                "ok": true,
                "checks": {
                    "music_backend": {"status": "ok", ...},
                    "wake_detector": {"status": "ok", ...},
                    "llm_provider":  {"status": "ok", ...},
                    "database":      {"status": "ok"},
                    "audio_devices": {"status": "ok", "input": N, "output": N}
                },
                "overall": "ok" | "degraded" | "error",
                "timestamp": "..."
            }
        """

        async def _inner():
            try:
                from ui.api.routes.health import (
                    _check_audio_device_health,
                    _check_database_health,
                    _check_llm_health,
                    _check_music_backend_health,
                    _check_wake_detector_health,
                )

                checks: dict = {}

                # --- music backend ---
                music_result = _check_music_backend_health(music)
                music_check: dict = {"status": music_result.get("status", "error")}
                if "backend_type" in music_result:
                    music_check["detail"] = music_result["backend_type"]
                elif "message" in music_result:
                    music_check["detail"] = music_result["message"]
                elif "note" in music_result:
                    music_check["detail"] = music_result["note"]
                checks["music_backend"] = music_check

                # --- wake detector ---
                wake_result = _check_wake_detector_health()
                wake_check: dict = {"status": wake_result.get("status", "error")}
                if wake_result.get("is_running"):
                    wake_check["detail"] = "active"
                elif "reason" in wake_result:
                    wake_check["detail"] = wake_result["reason"]
                elif "message" in wake_result:
                    wake_check["detail"] = wake_result["message"]
                checks["wake_detector"] = wake_check

                # --- LLM provider ---
                llm_result = _check_llm_health()
                llm_check: dict = {"status": llm_result.get("status", "error")}
                if "provider" in llm_result:
                    llm_check["detail"] = llm_result["provider"]
                elif "message" in llm_result:
                    llm_check["detail"] = llm_result["message"]
                checks["llm_provider"] = llm_check

                # --- database ---
                db_result = _check_database_health()
                db_check: dict = {"status": db_result.get("status", "error")}
                if "message" in db_result:
                    db_check["detail"] = db_result["message"]
                checks["database"] = db_check

                # --- audio devices ---
                audio_result = _check_audio_device_health()
                audio_check: dict = {"status": audio_result.get("status", "error")}
                if "input_devices" in audio_result:
                    audio_check["input"] = audio_result["input_devices"]
                if "output_devices" in audio_result:
                    audio_check["output"] = audio_result["output_devices"]
                if "message" in audio_result:
                    audio_check["detail"] = audio_result["message"]
                checks["audio_devices"] = audio_check

                # --- overall roll-up ---
                statuses = [c.get("status", "error") for c in checks.values()]
                if "error" in statuses:
                    overall = "error"
                elif "degraded" in statuses:
                    overall = "degraded"
                else:
                    overall = "ok"

                return success_response(
                    {
                        "ok": overall == "ok",
                        "checks": checks,
                        "overall": overall,
                        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                    }
                )
            except Exception:
                log.exception("Self-check failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "self_check_failed",
                        "Couldn't run the self-check right now. Try again in a moment.",
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/self-check", method="GET")

    # ------------------------------------------------------------------
    # Bug ticket endpoints (self-diagnosis persistence)
    # ------------------------------------------------------------------

    @router.get("/v1/diagnostics/bug-tickets", dependencies=[Depends(require_auth)])
    async def list_bug_tickets(
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        user_id: str = Depends(get_current_user_id),
    ):
        async def _inner():
            try:
                from services.agent.bug_tickets import BugTicketStore

                store = BugTicketStore()
                tickets = store.list_tickets(user_id=user_id, status=status, limit=min(limit, 100), offset=offset)
                return success_response([t.to_dict() for t in tickets])
            except Exception:
                log.exception("Failed to list bug tickets")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "bug_tickets_list_failed", "Couldn't load the issues list. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/bug-tickets", method="GET")

    @router.get("/v1/diagnostics/bug-tickets/stats", dependencies=[Depends(require_auth)])
    async def bug_ticket_stats(user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                from services.agent.bug_tickets import BugTicketStore

                store = BugTicketStore()
                stats = store.get_stats(user_id=user_id)
                return success_response(stats)
            except Exception:
                log.exception("Failed to get bug ticket stats")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "bug_tickets_stats_failed", "Couldn't load issue statistics. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/bug-tickets/stats", method="GET")

    @router.get("/v1/diagnostics/bug-tickets/{ticket_id}", dependencies=[Depends(require_auth)])
    async def get_bug_ticket(ticket_id: int, user_id: str = Depends(get_current_user_id)):
        async def _inner():
            try:
                from services.agent.bug_tickets import BugTicketStore

                store = BugTicketStore()
                ticket = store.get_ticket(ticket_id, user_id=user_id)
                if ticket is None:
                    return JSONResponse(
                        status_code=404,
                        content=failure_response("ticket_not_found", "Ticket not found"),
                    )
                return success_response(ticket.to_dict())
            except Exception:
                log.exception("Failed to get bug ticket")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "bug_ticket_get_failed", "Couldn't load the issue details. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/bug-tickets/{id}", method="GET")

    @router.patch("/v1/diagnostics/bug-tickets/{ticket_id}", dependencies=[Depends(require_auth)])
    async def update_bug_ticket(
        ticket_id: int,
        body: dict = Body(...),
        user_id: str = Depends(get_current_user_id),
    ):
        async def _inner():
            try:
                from services.agent.bug_tickets import BugTicketStore

                new_status = body.get("status")
                if not new_status:
                    return JSONResponse(
                        status_code=400,
                        content=failure_response("missing_status", "Request body must include 'status'"),
                    )

                store = BugTicketStore()
                success = store.update_status(ticket_id, new_status, user_id=user_id)
                if not success:
                    return JSONResponse(
                        status_code=404,
                        content=failure_response("update_failed", "Ticket not found or invalid status"),
                    )
                return success_response({"id": ticket_id, "status": new_status})
            except Exception:
                log.exception("Failed to update bug ticket")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "bug_ticket_update_failed", "Couldn't update the issue. Please try again."
                    ),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/bug-tickets/{id}", method="PATCH")

    # ------------------------------------------------------------------
    # Anonymized diagnostic minimum: cloud ingest relay + consent panel
    # ------------------------------------------------------------------

    # POST /v1/diagnostics/ingest lives in diagnostics.diagnostic_ingest_handler,
    # shared verbatim with the cloud carve-out registration
    # (ui.api.routes.cloud_diagnostics_ingest) so the two can never drift --
    # see that module's docstring for why this one route needed its own cloud
    # manifest entry (#1389/#3897: the rest of this file stays LOCAL_ONLY).
    from services.company_service_boundary import shared_company_service_available

    if shared_company_service_available("diagnostics.diagnostic_ingest_handler"):
        from diagnostics.diagnostic_ingest_handler import register_diagnostic_ingest_route

        register_diagnostic_ingest_route(context, toolbox)

    # POST /v1/diagnostics/ui-error: where an error thrown in the desktop's
    # React UI lands. Same-origin on purpose -- the browser reporting straight
    # to an ingest host is what CSP silently refused for 62 days (see that
    # module's docstring). Deliberately unauthenticated and desktop-local: it
    # runs before/independently of sign-in, and it is the UI's own crash path,
    # so requiring a session would put the errors we most need behind the
    # failure that produced them. The handler treats the body as hostile and
    # rebuilds it through the allowlist sanitizer.
    from diagnostics.ui_error_handler import register_ui_error_route

    register_ui_error_route(context, toolbox)

    @router.get("/v1/diagnostics/consent", dependencies=[Depends(require_auth)])
    async def get_diagnostics_consent():
        """Read the diagnostics consent state for the Settings > Privacy panel."""

        async def _inner():
            try:
                from diagnostics import diagnostic_consent, diagnostic_spool
                from diagnostics.diagnostic_minimum import _ALLOWED_TOP_LEVEL_KEYS, APP_STATE_FIELD_SPEC

                return success_response(
                    {
                        "consent": diagnostic_consent.consent_snapshot(),
                        "minimum_fields": sorted(_ALLOWED_TOP_LEVEL_KEYS),
                        "app_state_fields": sorted(APP_STATE_FIELD_SPEC),
                        "queued_reports": diagnostic_spool.pending_count(),
                    }
                )
            except Exception:  # noqa: BLE001, RUF100 - handler fail-closed; never 500 the caller
                log.exception("Diagnostics consent read failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("diagnostics_consent_failed", "Couldn't load diagnostics settings."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/consent", method="GET")

    @router.post("/v1/diagnostics/consent", dependencies=[Depends(require_auth)])
    async def set_diagnostics_consent(body: dict = Body(...)):
        """Update the diagnostics consent toggles (opt-out baseline / opt-in extra).

        Recognized keys: ``baseline_opted_out`` (bool), ``identifiable_extra``
        (bool), ``disclosure_shown`` (bool, set-once acknowledgement).
        """

        async def _inner():
            try:
                from diagnostics import diagnostic_consent
                from ui.settings_manager import get_settings_manager

                mgr = get_settings_manager()
                if "baseline_opted_out" in body:
                    mgr.set(diagnostic_consent.SETTING_BASELINE_OPTED_OUT, bool(body["baseline_opted_out"]))
                if "identifiable_extra" in body:
                    mgr.set(diagnostic_consent.SETTING_IDENTIFIABLE_CONSENT, bool(body["identifiable_extra"]))
                if body.get("disclosure_shown"):
                    diagnostic_consent.mark_disclosure_shown()
                    # Flush anything queued before the card was acknowledged.
                    try:
                        from diagnostics.diagnostic_dispatch import flush_spool

                        flush_spool()
                    except Exception:  # noqa: BLE001, RUF100 - handler fail-closed; never 500 the caller
                        log.debug("Spool flush after disclosure failed", exc_info=True)
                return success_response({"consent": diagnostic_consent.consent_snapshot()})
            except Exception:  # noqa: BLE001, RUF100 - handler fail-closed; never 500 the caller
                log.exception("Diagnostics consent update failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("diagnostics_consent_set_failed", "Couldn't update diagnostics settings."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/consent", method="POST")

    @router.get("/v1/diagnostics/preview", dependencies=[Depends(require_auth)])
    async def preview_diagnostic_minimum():
        """Preview the exact anonymized payload that would be/was sent.

        Powers the "preview my last report" button: builds the minimum from the
        current runtime the same way a real bug-report attach would, so the user
        sees precisely what leaves the device -- never a mock.
        """

        async def _inner():
            try:
                from diagnostics.diagnostic_minimum import build_diagnostic_minimum
                from ui.settings_manager import get_settings_manager

                mgr = get_settings_manager()
                app_state = {
                    "surface": "desktop_qt",
                    "ai_source": str(mgr.get("ai_source", "managed") or "managed"),
                    "is_authenticated": True,
                    "network_online": True,
                }
                payload = build_diagnostic_minimum(
                    report_kind="bug_report",
                    surface="desktop_qt",
                    app_state=app_state,
                )
                return success_response({"preview": payload})
            except Exception:  # noqa: BLE001, RUF100 - handler fail-closed; never 500 the caller
                log.exception("Diagnostics preview failed")
                return JSONResponse(
                    status_code=500,
                    content=failure_response("diagnostics_preview_failed", "Couldn't build the diagnostics preview."),
                )

        return await toolbox.record_and_call(_inner, route="/v1/diagnostics/preview", method="GET")

    log.info("Diagnostics routes registered")
