"""ui/server.py
FastAPI app factory & UI API for Viola.

Implements the WORK ORDER contract: create_app(state, music, intent) -> FastAPI

New/Changed in this version
---------------------------
- Adds POST /v1/command with normalized response mapping to Interpreter.
- Normalizes responses across endpoints to include {"ok", "error"} (non-breaking: original keys remain).
- Broadcasts PlayerState over WS after state-changing commands (play/pause/resume/stop/skip/volume.*).

Existing Endpoints
------------------
GET  /health
POST /v1/command                {"text": "..."} -> {"ok": true, "intent": "...", "data": {...}, "error": null}
POST /v1/play                   {"query": "...", "source": "ytsearch1|url|local"} -> {"ok": true, "enqueued": QueueItem, "error": null}
POST /v1/pause|/v1/resume|/v1/stop|/v1/skip    -> {"ok": true, "error": null}
POST /v1/volume                 {"level": 0-100} -> {"ok": true, "volume": <int>, "error": null}
GET  /v1/state                  -> {"ok": true, <PlayerState fields...>, "error": null}
GET  /v1/queue                  -> {"ok": true, "queue": [QueueItem, ...], "error": null}
WS   /ws/events                 -> {"type": "state", "payload": PlayerState}
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from config.constants import CACHE_DIR
from contracts.api_response import failure_response, success_response
from contracts.fastapi import attach_response_contract
from core.json_types import to_json_value
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, FastAPI, Query

log = get_logger(__name__)

# Module-level app registry so internal components can retrieve the running app
# without circular imports.  Set by _set_current_app() from backend/fastapi_app.py.
_current_app: FastAPI | None = None


def get_app() -> FastAPI | None:
    """Return the running FastAPI app instance, or None if not yet created."""
    return _current_app


def _set_current_app(app: FastAPI) -> None:
    """Register the running app instance (called by the app builder after creation)."""
    global _current_app
    _current_app = app


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        log.error("Background task failed: %s", exc)


def weather_prefetch_refresh_interval_seconds(
    cache_ttl_seconds: float,
    *,
    safety_margin_seconds: float = 90.0,
    floor_seconds: float = 60.0,
) -> float:
    """Compute the weather keep-warm refresh cadence (issue #2094).

    Re-warms the saved-location forecast just under the shared public
    weather cache TTL (``backend.weather_fetch._WEATHER_FETCH_CACHE_TTL_SECONDS``)
    so a live query for the saved location almost never lands on a cold
    cache. ``safety_margin_seconds`` absorbs fetch latency/clock drift
    between refreshes; ``floor_seconds`` keeps the cadence sane if the TTL
    is ever configured very low.
    """
    return max(cache_ttl_seconds - safety_margin_seconds, floor_seconds)


async def weather_prefetch_loop(
    do_prefetch,
    refresh_interval_seconds: float,
    *,
    initial_delay_seconds: float = 5.0,
) -> None:
    """Run *do_prefetch* once after *initial_delay_seconds*, then forever
    every *refresh_interval_seconds* until the task is cancelled.

    A module-level coroutine (not an inline closure) so the keep-warm
    behavior is directly unit-testable without booting the full FastAPI app.
    """
    await asyncio.sleep(initial_delay_seconds)
    while True:
        await do_prefetch()
        await asyncio.sleep(refresh_interval_seconds)


class _LazyWeatherCache:
    """Defer weather cache import/open until weather is actually requested."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._real: Any | None = None

    def _ensure(self) -> Any:
        if self._real is not None:
            return self._real
        with self._lock:
            if self._real is not None:
                return self._real
            from diagnostics.startup_telemetry import subsystem_timer

            with subsystem_timer("weather_cache"):
                from cache.weather_cache import WeatherCache

                self._real = WeatherCache(self._cache_dir)
            return self._real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ensure(), name)


try:  # pragma: no cover - ensure PendingDeprecation is silenced by eager import
    from importlib import import_module

    _MULTIPART_EAGER_IMPORT = import_module("multipart")
except ImportError:  # pragma: no cover - optional dependency
    log.warning("python-multipart not installed; multipart form uploads will be unavailable.")

# Heavy imports deferred to create_app() for faster module load (Phase 2C)


async def _speak_via_intent(intent: Any, message: str) -> None:
    """Helper to speak message via intent's TTS engine."""
    if not message or not message.strip():
        return

    try:
        # Try to get TTS from intent
        tts_engine = None
        if hasattr(intent, "tts_engine"):
            tts_engine = intent.tts_engine
        elif hasattr(intent, "tts"):
            tts_engine = intent.tts

        if tts_engine:
            if hasattr(tts_engine, "speak"):
                result = tts_engine.speak(message)
                if asyncio.iscoroutine(result):
                    await result
                log.info("🔊 Server spoke: %s", message[:100])
        elif hasattr(intent, "_speak"):
            # Use interpreter's _speak method
            await intent._speak(message)
    except Exception as e:
        log.warning("Failed to speak via intent: %s", e)


def create_app(state: Any, music: Any, intent: Any) -> FastAPI:
    """
    Factory: returns a FastAPI app wired to provided facades.
    No global singletons; safe for tests and embedding.

    Security:
    - Unified security plugin system (modular, pluggable)
    - Authentication (API key/token) - optional
    - Rate limiting (mandatory)
    - Error sanitization (enabled)
    - Resource limits (file size, WebSocket, request size)
    - Input validation
    """
    # Phase 2C: imports deferred from module level for faster cold start
    from contracts.fastapi_helpers import SafeJSONResponse
    from contracts.player_state import ensure_player_state_schema
    from diagnostics.runtime_metrics import get_runtime_metrics
    from diagnostics.startup_telemetry import register_post_bind_initializer
    from ui.api.context import ApiContext
    from ui.api.models import DebugEventIn
    from ui.api.routes import register_api_routes
    from ui.api.routes.auth_dependencies import require_auth
    from ui.api.routes.debug import _require_dev_mode
    from ui.core.bindings import Bindings as _Bindings
    from ui.core.metrics import commands_total, http_requests_total, play_events_total
    from ui.core.security import configure_security
    from ui.media.static import configure_static_assets
    from ui.monitoring_routes import router as monitoring_router
    from ui.security.rate_limit_utils import configure_router_rate_limits
    from ui.websocket.event_hub import EventHub
    from ui.websocket.routes import register_event_socket

    app = FastAPI(default_response_class=SafeJSONResponse)
    # Innermost middleware, so it sees the router's own 404. Post-bind
    # initializers mount much of the API after the port is bound and after
    # readiness flips true; until they finish, a request for a real endpoint
    # (push-to-talk's POST /v1/transcribe is the one a user reaches first)
    # falls off the end of the router. 404 tells the client the endpoint does
    # not exist; the truth is "not mounted yet", which is a 503 + Retry-After.
    # Must be the FIRST add_middleware call on this app - Starlette builds the
    # stack so the earliest-added middleware ends up closest to the router.
    from routing.startup_gate import attach_starting_route_gate

    attach_starting_route_gate(app)
    runtime_metrics = get_runtime_metrics()
    runtime_metrics.start_resource_sampler(interval=5.0)
    app.state.runtime_metrics = runtime_metrics
    app.add_middleware(GZipMiddleware, minimum_size=500)

    # Add CORS middleware for LAN access (phone/tablet browsers)
    from fastapi.middleware.cors import CORSMiddleware

    from config.settings import settings as _settings

    # When binding to all interfaces, accept LAN origins on our port from
    # trusted network addresses only.  The previous regex (https?://[^:/]+:port)
    # matched ANY hostname, enabling DNS-rebinding attacks where an attacker's
    # domain resolves to 127.0.0.1 and the browser sends credentialed requests
    # to the local Viola API.
    #
    # Allowed hosts:
    #   - localhost / 127.0.0.1 / [::1]
    #   - RFC-1918 private networks: 10.x.x.x, 172.16-31.x.x, 192.168.x.x
    _port = _settings.api_port
    _localhost_re = rf"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::{_port})?"
    _private_re = (
        rf"https?://(?:"
        rf"10\.\d{{1,3}}\.\d{{1,3}}\.\d{{1,3}}"
        rf"|172\.(?:1[6-9]|2\d|3[01])\.\d{{1,3}}\.\d{{1,3}}"
        rf"|192\.168\.\d{{1,3}}\.\d{{1,3}}"
        rf"):{_port}"
    )
    _lan_regex = (
        rf"(?:{_localhost_re}|{_private_re})" if _settings.api_host in ("0.0.0.0", "::") else None  # nosec B104
    )
    # Always allow Chrome extension origins (Viola Browser Bridge)
    _ext_regex = r"chrome-extension://[a-z]{32}"
    _cors_regex = rf"(?:{_lan_regex}|{_ext_regex})" if _lan_regex else _ext_regex

    # X-Spoke-Token: spoke devices send this header for spoke-to-hub authentication (M5 fix).
    # Set VIOLA_SPOKE_TOKEN env var on spokes and hub to enforce spoke registration.
    # If unset, LAN access falls back to session-cookie auth (no regression for single-device setups).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origins,
        allow_origin_regex=_cors_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Requested-With",
            "X-Request-ID",
            "Accept",
            "X-Spoke-Token",
            "X-Debug-Auth-Token",
            # Client's IANA zone, so wall-clock times resolve in the user's
            # timezone rather than the server's (#3557).
            "X-Viola-Timezone",
        ],
    )

    # Add correlation ID middleware first for request tracing
    from backend.instrumentation import add_correlation_id_middleware

    add_correlation_id_middleware(app)

    # Add request tracing enhancement
    try:
        from utils.enhancements.request_tracing import enhance_with_tracing

        enhance_with_tracing(app)
    except ImportError:
        log.debug("Request tracing enhancement not available")

    # Add auth middleware for session-based authentication
    try:
        from auth.middleware import AuthMiddleware
        from services.sentry_init import SentryUserContextMiddleware

        app.add_middleware(SentryUserContextMiddleware)
        app.add_middleware(AuthMiddleware)
        log.info("✅ Auth middleware added")
    except Exception as exc:
        log.warning("Could not add auth middleware: %s", exc)

    # SEC-015: Attach the per-IP sliding-window rate limiter so auth spam
    # returns 429 even when a downstream handler throws 500. Limits:
    #   /v1/command  30 req/min, /auth/* + /v1/auth/* 10 req/min, 200/min default.
    # Previously defined in backend/ip_rate_limiter.py but never wired.
    try:
        from backend.ip_rate_limiter import attach_ip_rate_limiter

        attach_ip_rate_limiter(app)
    except Exception as exc:
        log.warning("Could not attach IP rate limiter: %s", exc)

    ensure_player_state_schema()
    # See backend/fastapi_app.py for the rationale: register the envelope
    # exception handler so HTTPException raises emit canonical envelopes
    # instead of FastAPI's default ``{"detail": ...}`` (which the strict
    # pure-ASGI envelope middleware rejects). Health probe paths are
    # exempt inside the middleware itself (commit 4c04cf06).
    from contracts.fastapi_helpers import EnvelopeExceptionHandler

    EnvelopeExceptionHandler.register(app)

    attach_response_contract(
        app,
        skip_paths=(
            "/metrics",
            "/docs",
            "/openapi.json",
            "/v1/state",
            "/v1/player/state",
            "/v1/schema/player_state",
            "/v1/volume",  # Excluded due to middleware body reading issue with JSONResponse
            "/billing/checkout",
            "/billing/checkout/status",
            "/billing/extra-usage/checkout",
        ),
    )
    security_context = configure_security(app)

    # F-003: Global exception handler for LLM quota exceeded (safety net)
    try:
        from backend.fastapi_app import _register_quota_exceeded_handler

        _register_quota_exceeded_handler(app)
    except Exception as _exc:
        log.debug("Could not register LLM quota handler: %s", _exc)

    # Global ViolaError handler: auto-extracts user_friendly_message() for all routes
    try:
        from backend.fastapi_app import _register_viola_error_handler

        _register_viola_error_handler(app)
    except Exception as _exc:
        log.debug("Could not register ViolaError handler: %s", _exc)

    from typing import Any, cast

    configure_router_rate_limits(
        cast(Any, app),  # FastAPI app used as router for rate limiting
        default_limit=security_context.default_rate_limit,
        limiter_enabled=security_context.rate_limiter_enabled,
    )

    app.state.device_discovery = None
    app.state.device_discovery_stop_event = None

    # ========== PLUGIN SYSTEM ==========
    app.state.plugin_manager = None

    def _init_plugin_manager_after_bind() -> None:
        try:
            from plugins.singleton import get_plugin_manager

            app.state.plugin_manager = get_plugin_manager()
        except Exception as exc:
            log.warning("Plugin system not available: %s", exc)
            app.state.plugin_manager = None

    register_post_bind_initializer(app, "plugin_manager", _init_plugin_manager_after_bind)

    # ========== CAPABILITY REGISTRY (Phase 0 — passive observer) ==========
    app.state.capability_registry = None

    def _init_capability_registry_after_bind() -> None:
        try:
            from services.capability_registry import CapabilityRegistry
            from ui.settings_manager import get_settings_manager

            _sm = get_settings_manager()
            app.state.capability_registry = CapabilityRegistry.bootstrap_from_config(_settings, _sm)
        except Exception as exc:
            log.warning("Capability registry not available: %s", exc)
            app.state.capability_registry = None

    register_post_bind_initializer(app, "capability_registry", _init_capability_registry_after_bind)

    @app.post(
        "/debug/events",
        tags=["debug"],
        response_model=None,
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def record_debug_event(event: DebugEventIn) -> JSONResponse:
        try:
            from ui.qt_native.debug_events import emit_debug_event
        except Exception as exc:
            log.debug("Debug bus unavailable (import failed): %s", exc)
            return JSONResponse(
                status_code=501,
                content=failure_response(
                    "debug_bus_unavailable",
                    "Debug bus is unavailable.",
                ),
            )

        payload = dict(event.payload or {})
        emit_debug_event(event.name, payload, source=event.source)
        return JSONResponse(content=success_response({"ack": True}))

    @app.get(
        "/debug/events",
        tags=["debug"],
        response_model=None,
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def read_debug_events(
        source: str | None = Query(default=None, description="Filter by source label"),
    ) -> JSONResponse:
        try:
            from ui.qt_native.debug_events import get_recorded_debug_events
        except Exception as exc:
            log.debug("Debug bus unavailable (import failed): %s", exc)
            return JSONResponse(
                status_code=501,
                content=failure_response(
                    "debug_bus_unavailable",
                    "Debug bus is unavailable.",
                ),
            )

        events = [
            {
                "name": evt.name,
                "payload": to_json_value(evt.payload),
                "source": evt.source,
                "timestamp_ms": evt.timestamp_ms,
            }
            for evt in get_recorded_debug_events(source=source)
        ]
        return JSONResponse(content=success_response(to_json_value({"events": events})))

    @app.delete(
        "/debug/events",
        tags=["debug"],
        response_model=None,
        dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
    )
    async def clear_debug_events(
        source: str | None = Query(default=None, description="Filter by source label"),
    ) -> JSONResponse:
        try:
            from ui.qt_native.debug_events import reset_debug_events
        except Exception as exc:
            log.debug("Debug bus unavailable (import failed): %s", exc)
            return JSONResponse(
                status_code=501,
                content=failure_response(
                    "debug_bus_unavailable",
                    "Debug bus is unavailable.",
                ),
            )

        reset_debug_events(source=source)
        return JSONResponse(content=success_response({"ack": True}))

    router = APIRouter(tags=["api"])  # Add tag for better organization
    configure_router_rate_limits(
        router,
        default_limit=security_context.default_rate_limit,
        limiter_enabled=security_context.rate_limiter_enabled,
    )

    configure_router_rate_limits(
        monitoring_router,
        default_limit=security_context.default_rate_limit,
        limiter_enabled=security_context.rate_limiter_enabled,
    )

    hub = EventHub()
    app.state.event_hub = hub
    # Register as global singleton for cross-module access (voice_command_handler etc.)
    from ui.websocket.event_hub import set_event_hub as _set_global_hub

    _set_global_hub(hub)
    bindings = _Bindings(state=state, music=music, intent=intent, hub=hub)

    # ========== MULTIROOM COORDINATION ==========
    # Inject EventHub into multiroom singletons so they can broadcast
    # to browser-based spoke devices via WebSocket.
    try:
        from services.multiroom.command_forwarder import get_command_forwarder

        get_command_forwarder().set_event_hub(hub)
    except Exception as exc:
        log.debug("Command forwarder EventHub injection skipped: %s", exc)

    # ========== INSTANT COMMAND SEEK BROADCAST ==========
    # Inject EventHub into the intent pipeline's instant handler so that
    # voice-initiated seek commands broadcast to WebSocket clients.
    # Without this, "seek to 2:00" updates backend state but the YouTube
    # iframe never receives the seekTo postMessage.
    try:
        from bootstrap.lazy_intent import LazyIntentBridge

        if not isinstance(intent, LazyIntentBridge):
            instant_handler = getattr(intent, "instant_handler", None)
            if instant_handler is not None and hasattr(instant_handler, "set_event_hub"):
                instant_handler.set_event_hub(hub)
                log.debug("EventHub injected into instant command handler")
    except Exception as exc:
        log.debug("Instant handler EventHub injection skipped: %s", exc)

    # ========== WEATHER CACHE (OFFLINE-FIRST) ==========
    # Initialize persistent weather cache lazily so offline-first support does
    # not block the port-bind readiness gate.
    weather_cache = _LazyWeatherCache(Path(CACHE_DIR))
    bindings.weather_cache = weather_cache  # Make available to routes

    # Get security utilities (for use in endpoints)
    error_sanitizer = security_context.error_sanitizer
    resource_limits = security_context.resource_limits
    input_validator = security_context.input_validator

    ux_manager = None

    api_context = ApiContext(
        app=app,
        router=router,
        bindings=bindings,
        hub=hub,
        security=security_context,
        weather_cache=weather_cache,
        resource_limits=resource_limits,
        error_sanitizer=error_sanitizer,
        input_validator=input_validator,
        rate_limit=security_context.rate_limit,
        commands_total=commands_total,
        play_events_total=play_events_total,
        http_requests_total=http_requests_total,
        ux_manager=ux_manager,
        monitoring_router=monitoring_router,
        rate_limit_default=security_context.default_rate_limit,
        rate_limiter_enabled=security_context.rate_limiter_enabled,
    )
    register_api_routes(api_context)

    configure_static_assets(app)

    # ========== ASYNCIO BRIDGE: register main loop ==========
    # Mirror cloud_app.py:996. Without this, sync->async bridges (post-task
    # evaluator, user_model writes) fall through to a worker loop while the
    # asyncpg pool is bound to this loop — raising "PostgresAuthDatabase pool
    # is bound to a different event loop" (observed trace bc216688884f
    # 2026-05-01).
    @app.on_event("startup")
    async def _register_main_loop_for_async_bridges() -> None:
        try:
            from core.asyncio_safe import set_main_loop

            set_main_loop(asyncio.get_running_loop())
            log.info("asyncio_safe main loop registered for desktop UI server")
        except Exception:
            log.exception("Failed to register main loop for asyncio_safe bridges")

    # ========== WEATHER PREFETCH (FAST STARTUP + KEEP-WARM LOOP) ==========
    # issue #2094: the shared public weather cache (backend.weather_fetch,
    # _WEATHER_FETCH_CACHE_TTL_SECONDS = 10 min) used to be warmed exactly once,
    # 5s after startup. Any saved-location query more than ~10 minutes after
    # boot landed on a cold cache and paid the full geocode+NWS+GFS fetch
    # (~8s, the dominant cost cited in #2094's live trace). Looping the
    # prefetch just under that TTL keeps the saved location perpetually warm
    # so a live query almost never pays the cold-fetch cost on the user's turn.
    @app.on_event("startup")
    async def _prefetch_weather():
        """Keep the saved-location forecast warm in the background so live
        weather queries hit a warm cache instead of paying the cold
        geocode+NWS+GFS fetch on the user's turn."""

        async def _do_prefetch() -> None:
            try:
                from backend.weather_fetch import fetch_weather
                from utils.weather_location import resolve_weather_request_location

                city = resolve_weather_request_location(None)
                if city is None:
                    log.info("Weather prefetch skipped: no configured or geolocated location")
                    return

                weather_data = await fetch_weather(city=city)
                if weather_data is None:
                    log.warning("Weather prefetch: fetch returned None")
                    return

                weather_cache.set(weather_data, city=city)
                log.info(
                    "Weather prefetch complete: %s, %s°F at %s",
                    weather_data.get("condition", "?"),
                    weather_data.get("temperature", "?"),
                    weather_data.get("location", "?"),
                )

            except Exception as exc:
                log.warning("Weather prefetch failed (non-blocking): %s", exc)

        from core.constants import TIMEOUT_10_MINUTES

        refresh_interval_seconds = weather_prefetch_refresh_interval_seconds(TIMEOUT_10_MINUTES)

        # Fire and forget — don't block startup. Cancelled on shutdown below.
        task = asyncio.create_task(weather_prefetch_loop(_do_prefetch, refresh_interval_seconds))
        task.add_done_callback(_log_task_exception)
        app.state.weather_prefetch_task = task

    @app.on_event("shutdown")
    async def _stop_weather_prefetch() -> None:
        """Cancel the recurring weather keep-warm loop on shutdown."""
        task = getattr(app.state, "weather_prefetch_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001, RUF100 - shutdown cleanup must never raise
            log.debug("Weather prefetch task raised during shutdown cancellation")

    # Identity & Access is safety-core (C-043, sibling of the #3365 GoTrue
    # proxy fix and backend/fastapi_app.py's billing/payment-cards/
    # payment-confirm fail-closed mounts): the auth_router mount below used
    # to live here under register_post_bind_initializer, but that runs on a
    # daemon thread inside diagnostics/startup_telemetry.py's own
    # ``except Exception`` (run_in_background()), so a re-raise there would
    # be swallowed one layer up and never fail closed. The mount now lives
    # in backend/fastapi_app.py's ``_ensure_auth_router`` /
    # ``_register_synchronous_route_definitions`` instead, which — like the
    # billing/GoTrue mounts beside it — runs pre-bind AND is re-attempted,
    # unwrapped, on the minimal-app fallback path, so a broken import fails
    # app construction closed instead of booting a healthy container with
    # /auth/* silently missing. Do not re-add an auth_router mount here.
    #
    # Consent, OAuth-provider routes — best-effort, registered post-bind.
    # Admin routes are registered via register_api_routes() → _register_admin_routes()
    # which gates them on the app surface (cloud only for the dashboard).
    def _register_auth_routes_after_bind() -> None:
        try:
            from ui.consent_api import providers_router, router as consent_router

            configure_router_rate_limits(
                consent_router,
                default_limit=security_context.default_rate_limit,
                limiter_enabled=security_context.rate_limiter_enabled,
            )
            app.include_router(consent_router)
            if providers_router:
                app.include_router(providers_router)
            log.info("Consent router included")
        except Exception as exc:
            log.warning("Could not include consent router: %s", exc)

    register_post_bind_initializer(app, "auth_routes", _register_auth_routes_after_bind, registers_routes=True)

    # Initialize auth database on startup (runs in event loop, not blocking create_app)
    try:
        from auth.database import get_auth_db

        auth_db = get_auth_db()

        @app.on_event("startup")
        async def _init_auth_db():
            async def _run() -> None:
                from diagnostics.startup_telemetry import (
                    subsystem_init_end,
                    subsystem_init_start,
                )

                subsystem_init_start("auth_database")
                try:
                    await auth_db.initialize()
                except Exception as exc:
                    subsystem_init_end("auth_database", ok=False, error=f"{type(exc).__name__}: {exc}")
                    log.warning("Auth database initialization failed: %s", exc)
                else:
                    subsystem_init_end("auth_database", ok=True)
                    log.info("Auth database initialized on startup")

            asyncio.create_task(_run())

    except Exception as exc:
        log.warning("Could not initialize auth database on startup: %s", exc)

    # ========== REDIS BACKEND (OPTIONAL, FOR MULTI-INSTANCE SAAS) ==========
    @app.on_event("startup")
    async def _init_redis_backend() -> None:
        """Connect to Redis and inject into auth rate limiters and OAuth nonces.

        Non-blocking: if Redis is not configured or unreachable, components
        silently continue with their in-memory fallbacks.
        """

        async def _run() -> None:
            from diagnostics.startup_telemetry import (
                subsystem_init_end,
                subsystem_init_start,
            )

            subsystem_init_start("redis_backend")
            try:
                from services.cache.redis_backend import get_redis

                r = await get_redis()
                if r is None:
                    subsystem_init_end("redis_backend", ok=True)
                    return
                for import_path, accessor_name in (
                    ("auth.per_user_rate_limit", "get_user_rate_limiter"),
                    ("backend.ip_rate_limiter", "get_ip_rate_limit_store"),
                ):
                    try:
                        module = __import__(import_path, fromlist=[accessor_name])
                        getattr(module, accessor_name)().set_redis(r)
                    except Exception:
                        log.debug(
                            "Could not inject Redis via %s.%s",
                            import_path,
                            accessor_name,
                        )
                try:
                    from auth.routes import (
                        set_nonce_redis,
                        set_registration_limiter_redis,
                    )

                    set_nonce_redis(r)
                    set_registration_limiter_redis(r)
                except Exception:
                    log.debug("Could not inject Redis into auth route limiters")
            except Exception as exc:
                subsystem_init_end("redis_backend", ok=False, error=f"{type(exc).__name__}: {exc}")
                log.debug("Redis backend initialization skipped (non-fatal)")
            else:
                subsystem_init_end("redis_backend", ok=True)

        asyncio.create_task(_run())
        return

    @app.on_event("shutdown")
    async def _close_redis_backend() -> None:
        """Gracefully close Redis connection on app shutdown."""
        try:
            from services.cache.redis_backend import close_redis

            await close_redis()
        except Exception:
            pass

    @app.on_event("shutdown")
    async def _stop_phone_cloud_event_relay() -> None:
        """Tear down the desktop->cloud phone-event relay on app shutdown.

        Safe no-op when the relay was never started (e.g. local phone mode or no
        cloud call this session) — the helper only acts if the singleton exists.
        """
        try:
            from telephony.phone_cloud_event_relay import stop_phone_cloud_event_relay

            await stop_phone_cloud_event_relay()
        except (ImportError, RuntimeError, OSError) as exc:
            log.debug("Phone cloud-event relay shutdown skipped: %s", exc)

    register_event_socket(
        app=app,
        hub=hub,
        bindings=bindings,
        security=security_context,
        resource_limits=resource_limits,
    )

    # Register WebSocket command handlers (youtube_state, etc.)
    def _register_websocket_command_handlers() -> None:
        from ui.websocket.command_handlers import register_command_handlers

        register_command_handlers(
            hub=hub,
            music=bindings.music,
            state=bindings.state,
        )

    register_post_bind_initializer(
        app,
        "websocket_command_handlers",
        _register_websocket_command_handlers,
        registers_routes=True,
    )

    return app


# ========== Module-Level App for Backward Compatibility ==========
# Create a default app instance for tests and simple imports
# In production, use create_app() factory for proper dependency injection


def _create_mock_facades():
    """Create mock facades for testing"""
    from unittest.mock import Mock

    state = Mock()
    state.is_playing = False
    state.now_playing = None
    state.volume = 80
    state.queue = []

    music = Mock()
    music.play = Mock(return_value={"id": "test", "title": "test"})
    music.pause = Mock()
    music.resume = Mock()
    music.stop = Mock()
    music.next = Mock()
    music.previous = Mock()
    music.set_volume = Mock(return_value=80)
    music.get_volume = Mock(return_value=80)
    music.is_playing = Mock(return_value=False)
    music.get_queue = Mock(return_value=[])
    music.queue_size = Mock(return_value=0)

    intent = Mock()
    intent.parse = Mock(return_value={"intent": "test", "entities": {}})

    return state, music, intent


__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    """Lazy re-export for backward compat (PlayerState, QueueItem)."""
    if name == "PlayerState":
        from ui.core.player_state import PlayerState

        return PlayerState
    if name == "QueueItem":
        from models.player import QueueItem

        return QueueItem
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Module-level app placeholder — production always uses create_app() via bootstrap.
# No eager creation; avoids heavy import during module load (Phase 2C).
app = None
