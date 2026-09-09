from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from core.logging_config import get_logger

# NOTE: ``Request`` MUST stay importable at module scope. This module uses
# ``from __future__ import annotations``, so route parameter annotations are
# strings that FastAPI resolves against this module's globals. When ``Request``
# was only imported inside ``_register_utility_endpoints``, FastAPI could not
# resolve the ``request: Request`` annotation on ``_ws_auth`` and silently
# registered ``request`` as a REQUIRED QUERY PARAMETER — every POST
# /v1/ws/auth then failed with 422 (requal-M2: dead UI event WebSocket on the
# installed 1.0.1 respin).
from fastapi import APIRouter, FastAPI, Request

if TYPE_CHECKING:
    from ui.api.context import ApiContext

log = get_logger(__name__)


def _register_utility_endpoints(app: FastAPI) -> None:
    """Register lightweight utility endpoints (version, timers, WS auth)."""
    from typing import Any

    from fastapi import Depends

    from contracts.api_response import success_response
    from ui.api.routes.auth_dependencies import require_auth

    @app.get("/v1/version", dependencies=[Depends(require_auth)])
    async def _version() -> dict[str, Any]:
        from bootstrap import __version__

        return success_response({"version": __version__})

    @app.get("/v1/timers", dependencies=[Depends(require_auth)])
    async def _timers() -> dict[str, Any]:
        from core.state_selectors import get_active_timers

        timers = get_active_timers()
        return success_response(
            {
                "ok": True,
                "timers": timers,
                "count": len(timers),
            }
        )

    @app.post("/v1/ws/auth", dependencies=[Depends(require_auth)])
    async def _ws_auth(request: Request) -> dict[str, Any]:
        """Exchange an authenticated request for a short-lived WS auth token.

        The returned token should be passed as ``?token=<value>`` when
        opening a WebSocket connection.  It expires in 30 seconds.  Callers
        that need EventSource reconnect support may pass
        ``?purpose=sse&stream_id=<id>`` to receive a reusable token bound to
        that stream id.

        This eliminates the need to pass the API key in query params,
        which leaks the key to server logs, proxy logs, and browser
        history.
        """
        from ui.security.auth import AuthenticationPlugin
        from ui.security.config import get_security_config

        config = get_security_config()
        auth_plugin = AuthenticationPlugin(config)
        user = getattr(request.state, "user", None)
        session = getattr(request.state, "session", None)
        purpose = (request.query_params.get("purpose") or "").strip().lower()
        if purpose in {"sse", "stream", "eventsource"}:
            from fastapi import HTTPException, status
            from services.llm.stream_bus import get_stream_owner, normalize_stream_id

            raw_stream_id = (request.query_params.get("stream_id") or "").strip()
            if not raw_stream_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="stream_id required for SSE tokens",
                )
            try:
                stream_id = normalize_stream_id(raw_stream_id)
            except ValueError:
                stream_id = None
            if stream_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid stream_id",
                )

            # SEC-030 (2026-06-09 sweep): never mint a stream-bound token for
            # a stream another principal owns. The read path enforces
            # ownership too (ui/api/routes/llm_stream.py), but the mint must
            # not hand out a credential scoped to someone else's stream in
            # the first place. Resolve the caller with the SAME principal
            # resolution the read path uses so mint and read can never
            # disagree. Fail closed: an owned stream with an unresolvable or
            # mismatched caller is denied.
            owner = get_stream_owner(stream_id)
            if owner:
                from ui.api.routes.llm_stream import _resolve_request_user_id

                caller_id = await _resolve_request_user_id(request)
                if not caller_id or caller_id != owner:
                    log.warning(
                        "Refused SSE token mint for stream %s: owner=%s caller=%s",
                        stream_id,
                        owner,
                        caller_id or "<none>",
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Stream access denied",
                    )

            token = auth_plugin.generate_stream_auth_token(
                stream_id=stream_id,
                user_id=str(getattr(user, "id", "") or "") or None,
                session_id=str(getattr(session, "id", "") or "") or None,
            )
            expires_in = 300
        else:
            token = auth_plugin.generate_ws_auth_token(
                user_id=str(getattr(user, "id", "") or "") or None,
                session_id=str(getattr(session, "id", "") or "") or None,
            )
            expires_in = 30
        if not token:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Token secret not configured",
            )
        # The response contract enforcer expects the envelope shape
        # {ok, data, error}; without the `data` wrapper this 200 was
        # being rewritten to a 500 response_contract_violation.
        return {
            "ok": True,
            "data": {"token": token, "expires_in": expires_in},
            "error": None,
        }

    log.debug("Public telemetry/version routes registered")


def _register_feature_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Import and register FEATURE routes.

    Creates a separate feature_router so CORE routes (already included on the
    app) are not affected.  After all FEATURE routes are registered, the
    feature_router is included on the app.

    G1 fix: runs synchronously before uvicorn serves (was background thread).
    Heavy transitive imports remain function-level inside route modules.
    """
    from ui.security.rate_limit_utils import configure_router_rate_limits
    from ui.ux_manager import get_ux_manager

    # FEATURE route modules — heavy import chain (~2.3s on cold start)
    from .calendar import register_calendar_routes
    from .chat_mode import register_chat_mode_routes
    from .codex_auth import register_codex_auth_routes
    from .connectors import register_connector_routes
    from .context_sync import register_context_sync
    from .debug import register_debug_routes
    from .diagnostics import register_diagnostics_routes
    from .extensions import register_extensions_routes
    from .feedback import register_feedback_routes
    from .local_library import register_local_library_routes
    from .local_media import register_local_media_routes
    from .messaging import register_messaging_routes
    from .music_sources import register_music_source_routes
    from .network import register_network_routes
    from .smarthome import register_smarthome_routes
    from .cost import register_cost_routes
    from .weekly_review import register_weekly_review_routes
    from .intent_market import register_intent_market_routes
    from .wake_training import register_wake_training_routes
    from .playlists import register_playlist_routes
    from .onboarding import register_onboarding_routes
    from .plugins import register_plugin_routes
    from .about import router as about_router
    from .capabilities import router as capabilities_router
    from .rating import register_rating_routes
    from .review import register_review_routes
    from .skills import register_skill_routes
    from .track_rating import register_track_rating_routes
    from .transcription import register_transcription_routes
    from .ux import register_ux_routes
    from .ux_preferences import register_ux_preferences_routes
    from .suggestions import register_suggestions_routes
    from .system_info import register_system_info_routes
    from .wake_diagnostics import register_wake_diagnostics_routes

    _register_weather_routes = None
    try:
        from .weather import register_weather_routes as _weather_routes_impl

        _register_weather_routes = _weather_routes_impl
    except (SyntaxError, ImportError) as exc:
        log.warning("Weather routes unavailable: %s", exc)
    # UX Manager init (needed by some feature routes)
    if context.ux_manager is None:
        try:
            from ui.settings_manager import get_settings_manager

            settings_mgr = get_settings_manager()
            context.ux_manager = get_ux_manager(
                settings_manager=settings_mgr,
                music_player=context.bindings.music,
                websocket_hub=context.hub,
            )
            log.info("UX Enhancement Manager initialized")
        except Exception as exc:
            context.ux_manager = None
            log.warning("UX Enhancement Manager unavailable: %s", exc)
    # Create a feature router so routes don't need the main router
    app: FastAPI = context.app
    feature_router = APIRouter(tags=["api"])
    feat_ctx = replace(context, router=feature_router)

    register_transcription_routes(feat_ctx)
    register_debug_routes(feat_ctx)
    register_diagnostics_routes(feat_ctx, toolbox)
    register_wake_diagnostics_routes(feat_ctx, toolbox)
    register_onboarding_routes(feat_ctx, toolbox)
    if _register_weather_routes is not None:
        _register_weather_routes(feat_ctx, toolbox)
    else:
        log.warning("Weather routes skipped due to import error")
    register_calendar_routes(feat_ctx, toolbox)
    register_chat_mode_routes(feat_ctx, toolbox)
    register_codex_auth_routes(feat_ctx)
    register_connector_routes(feat_ctx)
    register_rating_routes(feat_ctx, toolbox)
    register_ux_routes(feat_ctx, toolbox)
    register_skill_routes(feat_ctx, toolbox)
    register_local_library_routes(feat_ctx, toolbox)
    register_local_media_routes(feat_ctx)
    register_review_routes(feat_ctx, toolbox)
    register_track_rating_routes(feat_ctx, toolbox)
    register_feedback_routes(feat_ctx, toolbox)
    register_ux_preferences_routes(feat_ctx, toolbox)
    register_plugin_routes(feat_ctx, toolbox)
    register_extensions_routes(feat_ctx)
    register_messaging_routes(feat_ctx, toolbox)
    register_music_source_routes(feat_ctx)
    register_network_routes(feat_ctx)
    register_smarthome_routes(feat_ctx)
    register_weekly_review_routes(feat_ctx)
    register_cost_routes(feat_ctx)
    register_intent_market_routes(feat_ctx)
    register_wake_training_routes(feat_ctx)
    register_playlist_routes(feat_ctx, toolbox)
    register_context_sync(feat_ctx)
    register_suggestions_routes(feat_ctx, toolbox)
    register_system_info_routes(feat_ctx, toolbox)

    # Memory CRUD routes (A6)
    try:
        from .memories import register_memory_routes

        register_memory_routes(feat_ctx, toolbox)
    except Exception as exc:
        log.warning("Failed to register memory routes: %s", exc)

    # Workbench file routes
    try:
        from .workbench import register_workbench_routes

        register_workbench_routes(feat_ctx, toolbox)
    except Exception as exc:
        log.warning("Failed to register workbench routes: %s", exc)

    # Knowledge compatibility routes (legacy /v1/knowledge over Workbench)
    try:
        from .knowledge import register_knowledge_routes

        register_knowledge_routes(feat_ctx, toolbox)
    except Exception as exc:
        log.warning("Failed to register knowledge routes: %s", exc)

    # LLM streaming routes (B6)
    try:
        from .llm_stream import llm_stream_router

        app.include_router(llm_stream_router)
    except Exception as exc:
        log.warning("Failed to register LLM stream routes: %s", exc)

    app.include_router(about_router)
    app.include_router(capabilities_router)
    # public_stats serves the useviola.com marketing-page efficiency widget
    # and reads from the cloud operator metrics DB. Desktop installs must
    # not expose this endpoint or instantiate MetricsDB to satisfy it.
    from config.settings import settings as _public_stats_settings

    if (getattr(_public_stats_settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud":
        from .public_stats import router as public_stats_router

        app.include_router(public_stats_router)
    app.include_router(feature_router)

    # Voice, multiroom, browser auth, spotify CDP, telephony, admin routes (include directly on app)
    _register_voice_routes(app, context)
    _register_voice_stream_ws(app)
    _register_voice_session_ws(app)
    _register_multiroom_routes(app, context)
    _register_browser_auth_routes(app, context)
    _register_spotify_cdp_routes(app, context)
    _register_ha_conversation_routes(app, context)
    _register_telephony_routes(app, context)
    _register_admin_routes(app)
    _register_aec_telemetry_routes(app)

    # Regression safety-net stubs are registered by `_register_regression_stubs`
    # in backend/fastapi_app.py. The registrar probes real router factories and
    # registrars before binding a stub, so it remains safe even when post-bind
    # route initializers are still racing to include their routers.

    log.info("FEATURE routes registered")


def register_api_routes(context: ApiContext) -> None:
    """Register all API routes synchronously before uvicorn serves.

    CORE routes (health, control, queue, state, public, lifecycle, settings,
    monitoring) are registered first. FEATURE routes (19 modules) follow.
    All registration completes before the app is handed to uvicorn.

    Heavy transitive imports remain function-level inside route modules
    for fast module load (~30ms total for FEATURE registration).
    """
    from ui.security.rate_limit_utils import configure_router_rate_limits
    from diagnostics.startup_telemetry import register_post_bind_initializer

    from .command import register_command_routes
    from .common import RouteToolbox
    from .health import register_health
    from .lifecycle import configure_lifecycle
    from .runtime import register_runtime_routes
    from .state import register_state_routes

    app: FastAPI = context.app
    router = context.router

    configure_router_rate_limits(
        router,
        default_limit=context.rate_limit_default,
        limiter_enabled=context.rate_limiter_enabled,
    )

    # === CORE routes (immediate — needed for first UI paint) ===
    register_health(context)
    configure_lifecycle(context)
    register_runtime_routes(context)
    register_command_routes(context)
    register_state_routes(context, RouteToolbox(context))

    def _register_core_routes() -> None:
        from .common import RouteToolbox
        from .control import register_control_routes
        from .queue import register_queue_routes

        deferred_router = APIRouter(tags=["api"])
        deferred_context = replace(context, router=deferred_router)
        deferred_toolbox = RouteToolbox(deferred_context)
        configure_router_rate_limits(
            deferred_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        register_control_routes(deferred_context, deferred_toolbox)
        register_queue_routes(deferred_context, deferred_toolbox)
        from services.company_service_boundary import company_service_module_available

        if company_service_module_available(
            "admin.auth_middleware",
            component="Public website intake routes",
        ):
            from .public import register_public_routes

            register_public_routes(deferred_context)
        app.include_router(deferred_router)
        _register_utility_endpoints(app)

    _register_core_routes()

    # Settings UI routes import the broader settings graph. They are mounted
    # after bind so the readiness gate only depends on health/auth/core routes.
    def _register_settings_router_after_bind() -> None:
        from ui.settings_api import create_settings_router

        settings_router = create_settings_router(
            music_service=context.bindings.music,
        )
        configure_router_rate_limits(
            settings_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(settings_router)

    register_post_bind_initializer(
        app,
        "settings_router",
        _register_settings_router_after_bind,
        registers_routes=True,
    )

    # Monitoring router
    configure_router_rate_limits(
        context.monitoring_router,
        default_limit=context.rate_limit_default,
        limiter_enabled=context.rate_limiter_enabled,
    )
    app.include_router(context.monitoring_router)
    app.include_router(router)

    # === FEATURE routes (synchronous — G1 race fix) ===
    # Previously deferred to a background thread, but include_router() after
    # uvicorn serves is a data race (Starlette routes list is unsynchronized).
    # Heavy transitive imports remain function-level inside route modules (~30ms).
    try:
        from diagnostics.startup_telemetry import register_post_bind_initializer

        def _register_deferred_feature_routes() -> None:
            from .common import RouteToolbox

            _register_feature_routes(context, RouteToolbox(context))

        # POST /v1/transcribe (push-to-talk) lands here. Marking it as a
        # route mounter both launches it ahead of the non-route initializers
        # and lets the server answer 503 "still starting" instead of 404 for
        # the seconds before it completes.
        register_post_bind_initializer(
            app,
            "api_feature_routes",
            _register_deferred_feature_routes,
            registers_routes=True,
        )
    except Exception as exc:
        log.warning(
            "Feature route post-bind registration unavailable, registering synchronously: %s",
            exc,
        )
        from .common import RouteToolbox

        _register_feature_routes(context, RouteToolbox(context))


def _register_voice_routes(app: FastAPI, context: ApiContext) -> None:
    """Register voice API routes (spoke wake-trigger endpoint)."""
    from ui.security.rate_limit_utils import configure_router_rate_limits

    try:
        from .voice import create_voice_router

        voice_router = create_voice_router()
        configure_router_rate_limits(
            voice_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(voice_router)
        log.debug("Registered voice routes")
    except Exception as exc:
        log.warning("Failed to register voice routes: %s", exc)


def _register_voice_stream_ws(app: FastAPI) -> None:
    """Register WebSocket endpoint for spoke voice streaming with wake detection."""
    try:
        from .voice_stream import register_voice_stream_ws

        register_voice_stream_ws(app)
        log.debug("Registered voice stream WebSocket at /ws/voice-stream")
    except Exception as exc:
        log.warning("Failed to register voice stream WebSocket: %s", exc)

    # Attach the in-process ASR seam (same one cloud_app.py wires) so
    # /ws/voice-stream command turns transcribe in-process on the desktop
    # surface too. Without it the handler falls back to an HTTP loopback to
    # /v1/transcribe, which the auth middleware rejects (401) for turns that
    # authenticated via spoke token instead of a session cookie — the turn
    # then dies as a silent empty transcript ("Could not understand speech").
    # Models lazy-load on first use, so this costs nothing at startup.
    try:
        if getattr(app.state, "asr", None) is None:
            from config.settings import settings as app_settings
            from voice.transcription.factory import ASRFactory

            app.state.asr = ASRFactory(app_settings).create_local()
            log.debug("Attached in-process ASR for /ws/voice-stream command turns")
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        log.warning("Failed to attach in-process ASR for voice stream: %s", exc)


def _register_voice_session_ws(app: FastAPI) -> None:
    """Register WebSocket endpoint for browser bidirectional voice sessions."""
    try:
        from .voice_session import register_voice_session_ws

        register_voice_session_ws(app)
        log.debug("Registered voice session WebSocket at /ws/voice-session")
    except Exception as exc:
        log.warning("Failed to register voice session WebSocket: %s", exc)


def _register_admin_routes(app: FastAPI) -> None:
    """Register admin routes based on the configured app surface.

    - Public endpoints (telemetry ingest, version check) are ALWAYS registered.
    - Admin dashboard + admin API endpoints are only mounted on the cloud surface.
    - On the desktop surface, /admin/ returns 404 as if it never existed.
    """
    from config.settings import settings
    from services.company_service_boundary import company_service_module_available

    if not company_service_module_available("admin.routes", component="Company telemetry and admin routes"):
        return

    # Public endpoints remain available on every distribution that includes the
    # company service package. Standalone personal builds omit that package.
    from admin.routes import create_public_router

    public_router = create_public_router()
    app.include_router(public_router)
    log.debug("Public telemetry/version routes registered")

    # Admin dashboard + API: only on the cloud surface.
    if getattr(settings, "app_surface", "desktop") == "cloud":
        from admin.routes import create_admin_router

        admin_router = create_admin_router()
        app.include_router(admin_router)
        log.info("Admin dashboard routes registered (cloud surface)")
    else:
        log.debug("Admin dashboard disabled (desktop surface)")


def _register_aec_telemetry_routes(app: FastAPI) -> None:
    """Register AEC ERLE telemetry route (admin-authenticated)."""
    from services.company_service_boundary import company_service_module_available

    if not company_service_module_available("admin.auth_middleware", component="Admin-authenticated AEC telemetry"):
        return

    from .aec_telemetry import create_aec_telemetry_router

    aec_router = create_aec_telemetry_router()
    app.include_router(aec_router)
    log.debug("Registered AEC telemetry route")


def _register_multiroom_routes(app: FastAPI, context: ApiContext) -> None:
    """Register multi-room API routes."""
    from ui.security.rate_limit_utils import configure_router_rate_limits

    try:
        from .clock import create_clock_router
        from .devices import create_devices_router
        from .multiroom import create_multiroom_router
        from .room_groups import create_room_groups_router
        from .rooms import create_rooms_router
        from .sync_calibration import create_sync_calibration_router

        for factory, name in [
            (create_rooms_router, "rooms"),
            (create_room_groups_router, "room_groups"),
            (create_clock_router, "clock"),
            (create_sync_calibration_router, "sync_calibration"),
            (create_devices_router, "devices"),
            (create_multiroom_router, "multiroom"),
        ]:
            router = factory()
            configure_router_rate_limits(
                router,
                default_limit=context.rate_limit_default,
                limiter_enabled=context.rate_limiter_enabled,
            )
            app.include_router(router)
            log.debug("Registered multi-room route: %s", name)

        log.info("Multi-room API routes enabled")
    except Exception as exc:
        log.warning("Failed to register multi-room routes: %s", exc)


def _register_spotify_cdp_routes(app: FastAPI, context: ApiContext) -> None:
    """Register Spotify CDP authentication routes."""
    from ui.security.rate_limit_utils import configure_router_rate_limits

    try:
        from .spotify_cdp import create_spotify_cdp_router

        spotify_cdp_router = create_spotify_cdp_router()
        configure_router_rate_limits(
            spotify_cdp_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(spotify_cdp_router)
        log.info("Spotify CDP status route: real handler registered")
        log.debug("Registered Spotify CDP routes")
    except Exception as exc:
        log.warning("Failed to register Spotify CDP routes: %s", exc)


def _register_ha_conversation_routes(app: FastAPI, context: ApiContext) -> None:
    """Register Home Assistant conversation agent routes."""
    from ui.security.rate_limit_utils import configure_router_rate_limits

    try:
        from .ha_conversation import create_ha_conversation_router

        ha_router = create_ha_conversation_router()
        configure_router_rate_limits(
            ha_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(ha_router)
        log.debug("Registered HA conversation routes")
    except Exception as exc:
        log.warning("Failed to register HA conversation routes: %s", exc)


def _register_telephony_routes(app: FastAPI, context: ApiContext) -> None:
    """Register telephony routes (call history, phone ToS, dev cost tracking)."""
    from ui.security.rate_limit_utils import configure_router_rate_limits
    from services.company_service_boundary import shared_company_service_available

    # Local owner-scoped history/control remains available when the optional
    # hosted phone service is not part of the source distribution.
    company_phone_available = shared_company_service_available("telephony.cloud_routes")

    try:
        from telephony.listen_ws import ws_call_listen
        from telephony.routes import dev_router, phone_calls_router, router, tos_router

        for tel_router, name in [
            (router, "calls"),
            (tos_router, "phone-tos"),
            (phone_calls_router, "phone-calls"),
            (dev_router, "dev-calls"),
        ]:
            configure_router_rate_limits(
                tel_router,
                default_limit=context.rate_limit_default,
                limiter_enabled=context.rate_limiter_enabled,
            )
            app.include_router(tel_router)
            log.debug("Registered telephony route: %s", name)

        app.add_api_websocket_route("/ws/call-listen/{call_id}", ws_call_listen)
        log.info("Telephony routes registered")
    except Exception as exc:
        log.warning("Failed to register telephony routes: %s", exc)

    if company_phone_available:
        from telephony.cloud_routes import router as cloud_phone_router

        configure_router_rate_limits(
            cloud_phone_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(cloud_phone_router)


def _register_browser_auth_routes(app: FastAPI, context: ApiContext) -> None:
    """Register browser authentication routes if browser provider is enabled."""
    from config.settings import settings
    from ui.security.rate_limit_utils import configure_router_rate_limits

    if not settings.browser_provider_enabled:
        log.debug("Browser auth routes disabled (browser_provider_enabled=false)")
        return

    try:
        from .browser_auth import create_browser_auth_router

        browser_auth_router = create_browser_auth_router()
        configure_router_rate_limits(
            browser_auth_router,
            default_limit=context.rate_limit_default,
            limiter_enabled=context.rate_limiter_enabled,
        )
        app.include_router(browser_auth_router)
        log.debug("Registered browser auth routes")
    except Exception as exc:
        log.warning("Failed to register browser auth routes: %s", exc)


__all__ = ["register_api_routes"]
