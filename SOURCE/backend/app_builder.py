"""
FastAPI app building utilities.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from core.logging_config import get_logger
from fastapi import APIRouter, FastAPI

logger = get_logger(__name__)

CreateAppType = Callable[[Any, Any, Any], Any]


class _NoopCounter:
    def inc(self, **_labels: object) -> None:
        return None


def _noop_rate_limit(_limit: str):
    def _decorator(func):
        return func

    return _decorator


def _make_minimal_api_context(
    *,
    app: FastAPI,
    router: APIRouter,
    state: Any,
    music: Any,
    intent: Any,
    command_service: Any,
):
    from ui.api.context import ApiContext
    from ui.core.bindings import Bindings
    from ui.security import get_error_sanitizer, get_input_validator, get_resource_limits

    hub = getattr(app.state, "event_hub", None)
    bindings = Bindings(state=state, music=music, intent=intent, hub=hub)
    counter = _NoopCounter()
    return ApiContext(
        app=app,
        router=router,
        bindings=bindings,
        hub=hub,
        security=None,
        weather_cache=None,
        resource_limits=get_resource_limits(),
        error_sanitizer=get_error_sanitizer(),
        input_validator=get_input_validator(),
        rate_limit=_noop_rate_limit,
        commands_total=counter,
        play_events_total=counter,
        http_requests_total=counter,
        ux_manager=None,
        monitoring_router=APIRouter(),
        rate_limit_default=None,
        rate_limiter_enabled=False,
        command_service=command_service,
    )


def register_canonical_minimal_routes(
    app: FastAPI,
    *,
    state: Any,
    music: Any,
    intent: Any,
    command_service: Any,
) -> None:
    """Mount canonical route implementations on the fallback/minimal app."""
    from ui.api.routes import _register_utility_endpoints
    from ui.api.routes.capabilities import router as capabilities_router
    from ui.api.routes.command import register_command_routes
    from ui.api.routes.common import RouteToolbox
    from ui.api.routes.diagnostics import register_diagnostics_routes
    from ui.api.routes.messaging import register_messaging_routes
    from ui.api.routes.onboarding import register_onboarding_routes
    from ui.api.routes.queue import register_queue_routes
    from ui.api.routes.review import register_review_routes
    from ui.api.routes.runtime import register_runtime_routes
    from ui.api.routes.state import register_state_routes
    from ui.api.routes.suggestions import register_suggestions_routes
    from ui.api.routes.system_info import register_system_info_routes
    from ui.api.routes.ux import register_ux_routes
    from ui.api.routes.ux_preferences import register_ux_preferences_routes
    from ui.settings_api import create_settings_router

    router = APIRouter(tags=["api"])
    context = _make_minimal_api_context(
        app=app,
        router=router,
        state=state,
        music=music,
        intent=intent,
        command_service=command_service,
    )
    toolbox = RouteToolbox(context)

    register_command_routes(context)
    register_runtime_routes(context)
    register_state_routes(context, toolbox)
    register_diagnostics_routes(context, toolbox)
    register_queue_routes(context, toolbox)
    register_onboarding_routes(context, toolbox)
    register_ux_routes(context, toolbox)
    register_ux_preferences_routes(context, toolbox)
    register_suggestions_routes(context, toolbox)
    register_system_info_routes(context, toolbox)
    register_messaging_routes(context, toolbox)
    register_review_routes(context, toolbox)
    app.include_router(capabilities_router)
    app.include_router(create_settings_router(music_service=music))
    app.include_router(router)
    _register_utility_endpoints(app)


def register_canonical_core_routes(
    app: FastAPI,
    *,
    state: Any,
    music: Any,
    intent: Any,
    command_service: Any,
) -> None:
    """Mount only the canonical core routes used by build_fastapi_app repair."""
    from ui.api.routes.command import register_command_routes
    from ui.api.routes.runtime import register_runtime_routes

    router = APIRouter(tags=["api"])
    context = _make_minimal_api_context(
        app=app,
        router=router,
        state=state,
        music=music,
        intent=intent,
        command_service=command_service,
    )
    register_command_routes(context)
    register_runtime_routes(context)
    app.include_router(router)


def build_minimal_app(
    *,
    state: Any,
    music: Any,
    intent: Any,
    command_service: Any,
) -> FastAPI:
    """
    Build a minimal FastAPI app with core functionality.

    This creates a basic app suitable for headless or constrained environments
    that don't have full UI dependencies available.
    """
    from backend.instrumentation import add_correlation_id_middleware, instrument_app
    from backend.observability_routes import register_observability_routes
    from backend.player_state_contract import ensure_player_state_contract
    from backend.security import configure_backend_security
    from backend.static_assets import setup_static_routes
    from contracts.fastapi_helpers import SafeJSONResponse
    from diagnostics.runtime_metrics import get_runtime_metrics
    from ui.api.routes.agents import register_agent_routes

    app = FastAPI(
        title="Viola Hub API",
        description="Minimal Viola Hub API for core functionality",
        version="1.0.0",
        default_response_class=SafeJSONResponse,
    )

    # Register the request-handler loop as the canonical main loop for
    # sync->async bridges. Without this, post-task evaluator + user_model
    # writes fall through to a worker loop while the asyncpg pool was
    # initialized on this loop, raising "PostgresAuthDatabase pool is bound
    # to a different event loop". Cloud surfaces do this in their lifespan
    # (backend/cloud_app.py:996); the minimal desktop app needs it too.
    @app.on_event("startup")
    async def _register_main_loop_for_async_bridges() -> None:
        try:
            import asyncio as _asyncio

            from core.asyncio_safe import set_main_loop

            set_main_loop(_asyncio.get_running_loop())
            logger.info("asyncio_safe main loop registered for desktop minimal app")
        except Exception:
            logger.exception("Failed to register main loop for asyncio_safe bridges")

    # Add correlation ID middleware (first, so all requests get traced)
    add_correlation_id_middleware(app)

    # Add request tracing enhancement
    try:
        from utils.enhancements.request_tracing import enhance_with_tracing

        enhance_with_tracing(app)
    except ImportError:
        logger.debug("Request tracing enhancement not available")

    # Match the full desktop app path: API-key security middleware verifies the
    # transport, while AuthMiddleware enriches trusted loopback desktop requests
    # with the explicit user principal named by the caller.
    try:
        from auth.middleware import AuthMiddleware
        from services.sentry_init import SentryUserContextMiddleware

        app.add_middleware(SentryUserContextMiddleware)
        app.add_middleware(AuthMiddleware)
        logger.info("Auth middleware added to minimal app")
    except Exception as exc:
        logger.warning("Could not add auth middleware to minimal app: %s", exc)

    # Configure security
    configure_backend_security(app)

    # Setup static assets
    setup_static_routes(app)

    # Register routes through the canonical UI/API modules. The fallback app
    # stays minimal as an app shell, but it does not carry alternate handler
    # implementations that can drift from the full router.
    register_canonical_minimal_routes(
        app,
        state=state,
        music=music,
        intent=intent,
        command_service=command_service,
    )
    register_agent_routes(app, intent=intent)

    # Register observability routes
    register_observability_routes(app, state=state)

    # Setup sync engine (core audio sync, always enabled)
    try:
        from audio_core.sync_engine import SyncEngine
        from core.events.bus import LocalEventBus

        sync_engine = SyncEngine(
            hub_clock_url=None,  # Hub mode: use local clock as authority
            event_bus=LocalEventBus(),
            sync_pulse_interval=1.0,
        )
        app.state.sync_engine = sync_engine

        @app.on_event("startup")
        async def _start_sync_engine() -> None:
            try:
                sync_engine.start()
                logger.info("Sync engine started (Hub mode)")
            except Exception as exc:
                logger.warning("Failed to start sync engine: %s", exc)

        @app.on_event("shutdown")
        async def _stop_sync_engine() -> None:
            try:
                sync_engine.stop()
                logger.info("Sync engine stopped")
            except Exception as exc:
                logger.warning("Failed to stop sync engine: %s", exc)

    except Exception as exc:
        logger.warning("Failed to setup sync engine: %s", exc)

    # Set up the always-on multiroom node registry.
    try:
        from experimental.phase2_multiroom.node_registry_setup import (
            setup_node_registry,
        )

        setup_node_registry(app, state)
        logger.info("Multi-room node registry enabled")
    except ImportError as exc:
        logger.warning("Failed to import multi-room setup: %s", exc)
    except Exception as exc:
        logger.warning("Failed to setup multi-room node registry: %s", exc)

    # Instrument app
    runtime_metrics = get_runtime_metrics()
    runtime_metrics.start_resource_sampler(interval=5.0)
    app.state.runtime_metrics = runtime_metrics
    instrument_app(app, runtime_metrics)

    # Ensure player state contract
    ensure_player_state_contract(app)

    # Add /health/auth diagnostic endpoint for minimal app
    @app.get("/health/auth")
    async def auth_health():
        auth_routes = [r.path for r in app.routes if hasattr(r, "path") and "/auth" in r.path]
        return {
            "auth_available": len(auth_routes) > 0,
            "auth_routes_count": len(auth_routes),
            "auth_routes": auth_routes,
            "app_type": "minimal",
        }

    return app


def _try_import_create_app() -> CreateAppType | None:
    """Try to import the full-featured app factory from ui.server."""
    try:
        from ui.server import create_app

        return cast(CreateAppType, create_app)
    except ImportError:
        return None
