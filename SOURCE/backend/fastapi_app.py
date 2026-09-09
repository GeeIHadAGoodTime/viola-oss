from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol

from fastapi.responses import JSONResponse

from contracts.api_response import (
    ResponseEnvelope,
    failure_response,
    success_response,
)
from contracts.fastapi import attach_response_contract
from contracts.fastapi_helpers import SafeJSONResponse
from core.hub_state_authority import HubStateAuthority
from core.logging_config import get_logger
from core.sentry_integration import sentry_initialized
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from services.command.idempotency import IdempotencyLedger
from services.sentry_init import init_sentry
from services.supervisor import ensure_supervisor
from ui.api.routes.auth_dependencies import (
    get_current_user_id as get_auth_user_id,
    require_auth,
)

from .app_builder import _try_import_create_app
from .player_state_contract import ensure_player_state_contract

logger = get_logger(__name__)
if not sentry_initialized():
    init_sentry("backend.fastapi_app")


def _check_multipart_available() -> bool:
    """Check if python-multipart is available for form data handling."""
    try:
        import multipart

        return True
    except ImportError:
        return False


if not _check_multipart_available():  # pragma: no cover - optional dependency
    logger.warning("python-multipart not installed; multipart form uploads will be unavailable.")


class IntentBridgeProtocol(Protocol):
    async def interpret(self, text: str) -> dict[str, Any] | ResponseEnvelope: ...

    async def dispatch(self, payload: dict[str, Any]) -> dict[str, Any] | ResponseEnvelope: ...


_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "data" / "runtime"
_COMMAND_LEDGER_PATH = _RUNTIME_DIR / "command_ledger.json"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "y", "on"})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_ENV_VALUES


def _hub_pcm_pipeline_skip_reason() -> str | None:
    """Return why native hub PCM capture should not start in this process."""
    if _env_truthy("VIOLA_DISABLE_HUB_PCM_PIPELINE"):
        return "VIOLA_DISABLE_HUB_PCM_PIPELINE is set"
    if _env_truthy("VIOLA_ENABLE_HUB_PCM_PIPELINE"):
        return None
    if _env_truthy("GITHUB_ACTIONS") or _env_truthy("CI"):
        return "CI environment requires VIOLA_ENABLE_HUB_PCM_PIPELINE=1"

    try:
        from config import settings

        if bool(getattr(settings, "test_mode", False)) or bool(getattr(settings, "pytest_in_progress", False)):
            return "test mode requires VIOLA_ENABLE_HUB_PCM_PIPELINE=1"
    except (AttributeError, ImportError, LookupError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Unable to inspect settings before hub PCM setup: %s", exc)

    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return "pytest environment requires VIOLA_ENABLE_HUB_PCM_PIPELINE=1"
    return None


def _setup_sync_engine(app: FastAPI) -> None:
    """
    Initialize and configure sync engine for Hub-side multi-room synchronization.

    Sets up SyncEngine in Hub mode (hub_clock_url=None) to serve as monotonic clock authority.
    Starts sync pulse emission on app startup and stops on shutdown.
    """
    try:
        from audio_core.sync_engine import SyncEngine
        from core.events.bus import LocalEventBus

        # Initialize sync engine in Hub mode (no hub_clock_url = Hub is authority)
        sync_engine = SyncEngine(
            hub_clock_url=None,  # Hub mode: use local clock as authority
            event_bus=LocalEventBus(),
            sync_pulse_interval=1.0,
        )

        # Store in app state for access by endpoints
        app.state.sync_engine = sync_engine

        @app.on_event("startup")
        async def _start_sync_engine() -> None:
            """Start sync engine on app startup to begin emitting sync pulses."""
            try:
                sync_engine.start()
                logger.info("✅ Sync engine started (Hub mode)")
            except Exception as exc:
                logger.warning("Failed to start sync engine: %s", exc)

        @app.on_event("shutdown")
        async def _stop_sync_engine() -> None:
            """Stop sync engine on app shutdown."""
            try:
                sync_engine.stop()
                logger.info("🛑 Sync engine stopped")
            except Exception as exc:
                logger.warning("Failed to stop sync engine: %s", exc)

    except Exception as exc:
        logger.warning("Failed to setup sync engine: %s", exc)


def _setup_companion_client(app: FastAPI) -> None:
    """Wire the desktop companion client into the app lifecycle.

    The companion client pairs this desktop as a companion device of the
    user's Viola cloud account, so the cloud (browser) service can use the
    desktop's local features (on-disk music library, LAN smart home). It is
    a no-op on the cloud surface and starts idle when the user has not
    enabled the feature / is not signed in.
    """
    try:
        from services.companion_client import setup_companion_client

        setup_companion_client(app)
    except Exception as exc:
        logger.warning("Failed to setup companion client: %s", exc)


def _setup_scheduler(app: Any, intent: Any) -> None:
    """Initialize and configure the scheduler service (startup handled by lifespan)."""
    try:
        from services.scheduler.service import get_scheduler_service

        scheduler = get_scheduler_service()
        scheduler._intent_pipeline = intent
        app.state.scheduler = scheduler

    except Exception as exc:
        logger.warning("Failed to setup scheduler: %s", exc)


def _setup_calendar_reminders(app: Any, intent: Any) -> None:
    """Initialize the calendar reminder sweep service (startup handled by lifespan)."""
    try:
        from services.calendar.reminders import get_calendar_reminder_service

        app.state.calendar_reminders = get_calendar_reminder_service()

    except Exception as exc:  # noqa: BLE001, RUF100 -- optional service wiring must fail open
        logger.warning("Failed to setup calendar reminders: %s", exc)


def _setup_health_watchdog(app: Any, intent: Any) -> None:
    """Initialize and configure the health watchdog (startup handled by lifespan)."""
    try:
        from services.health_watchdog import HealthWatchdog

        watchdog = HealthWatchdog(intent=intent)
        app.state.health_watchdog = watchdog

    except Exception as exc:
        logger.warning("Failed to setup health watchdog: %s", exc)


def _register_multiroom_router(app: FastAPI) -> None:
    """Register multiroom router synchronously (G1 race fix).

    Split from _setup_multiroom_if_enabled so the router is registered
    before uvicorn serves, while mDNS discovery is deferred.
    """
    from config.settings import settings

    if not settings.enable_multiroom:
        return

    try:
        from ui.api.routes.multiroom import router as multiroom_router

        app.include_router(multiroom_router)
        logger.info("Multiroom role endpoint registered")
    except ImportError as exc:
        logger.debug("Multiroom router not available: %s", exc)


def _defer_multiroom_discovery(app: FastAPI, state: Any) -> None:
    """Schedule multiroom mDNS discovery on a 3 s timer, cancellable on shutdown."""
    timer = threading.Timer(3.0, _setup_multiroom_if_enabled, args=(app, state))
    timer.daemon = True
    app.state._multiroom_timer = timer
    timer.start()

    @app.on_event("shutdown")
    async def _cancel_multiroom_timer() -> None:
        timer.cancel()


def _setup_multiroom_if_enabled(app: FastAPI, state: Any) -> None:
    """
    Setup multi-room node registry and mDNS discovery if VIOLA_ENABLE_MULTIROOM is set.

    Multiroom features are disabled by default.
    To enable, set VIOLA_ENABLE_MULTIROOM=1 in your environment.

    NOTE: Router registration is handled separately by _register_multiroom_router()
    which runs synchronously before uvicorn serves (G1 race fix).
    """
    from config.settings import settings

    if not settings.enable_multiroom:
        logger.debug("Multi-room features disabled (set VIOLA_ENABLE_MULTIROOM=1 to enable)")
        return

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

    # Start the production DiscoveryService singleton so route handlers
    # can access discovered devices via get_discovery_service().
    try:
        from core.room_registry import get_room_registry
        from services.multiroom.discovery import get_discovery_service

        registry = get_room_registry()

        # Register the local device as a room so it appears in the RoomRegistry
        # (the source of truth). Previously, rooms.py synthesized this on the fly.
        if not registry.has_room("local"):
            import platform

            hostname = platform.node()
            registry.register_local_room(f"Viola ({hostname})")

        # Resolve the canonical device_id from settings (same logic as context_sync.py)
        # so that the production DiscoveryService and the experimental DeviceDiscovery
        # share the same ID. Without this, each discovers the other's mDNS announcement
        # and treats it as a remote device, causing self-sync noise.
        resolved_device_id = None
        try:
            if hasattr(state, "settings_manager") and state.settings_manager:
                resolved_device_id = state.settings_manager.get("device_id")
                if not resolved_device_id:
                    import uuid as _uuid

                    resolved_device_id = str(_uuid.uuid4())
                    state.settings_manager.set("device_id", resolved_device_id, save_immediately=False)
        except Exception as exc:
            logger.debug("Failed to resolve device_id from settings: %s", exc)

        # NOTE: Do NOT pass room_registry here. The DiscoveryService should only
        # discover devices and maintain its internal list. Registration as rooms
        # must happen explicitly via POST /api/v1/devices/{id}/connect so users
        # retain control over which devices are added to their room list.
        discovery = get_discovery_service(device_id=resolved_device_id)
        started = discovery.start()
        if started:
            logger.info("DiscoveryService started for multi-room")
        else:
            logger.debug("DiscoveryService did not start (disabled or unavailable)")
    except ImportError as exc:
        logger.warning("Failed to import DiscoveryService: %s", exc)
    except Exception as exc:
        logger.warning("Failed to start DiscoveryService: %s", exc)

    # NOTE: Multiroom router registration moved to _register_multiroom_router()
    # which runs synchronously before uvicorn serves (G1 race fix).


def _setup_hub_pcm_pipeline(app: FastAPI) -> None:
    """Wire the hub-side PCM streaming pipeline for runtime processes.

    This creates the AudioTee, ChunkStamper, capture provider
    (ProcTap/WASAPI), and optionally HubLocalPlayback.  The pipeline
    runs regardless of the multiroom feature flag because it
    is the core audio path for both hub local playback and spoke
    streaming.  CI and pytest startup skip native capture unless an
    explicit opt-in is set, because some providers can crash the
    process while probing host audio devices.
    """
    skip_reason = _hub_pcm_pipeline_skip_reason()
    if skip_reason is not None:
        logger.info("PCM streaming pipeline skipped: %s", skip_reason)
        return

    try:
        from audio_core.streaming.pipeline_wiring import (
            setup_source_pipeline,
            teardown_pipeline,
        )

        try:
            from core.user_context import get_device_user_id

            pipeline_user_id = get_device_user_id()  # mt-ok: required desktop owner; fail closed below.
        except (
            AttributeError,
            ImportError,
            LookupError,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.warning(
                "PCM streaming pipeline owner unavailable; not wiring hub PCM pipeline: %s",
                exc,
            )
            return

        if not isinstance(pipeline_user_id, str) or not pipeline_user_id.strip():
            logger.warning("PCM streaming pipeline owner is empty; not wiring hub PCM pipeline")
            return

        broadcaster = setup_source_pipeline(app, room_id="local", user_id=pipeline_user_id.strip())
        if broadcaster:
            logger.info("PCM streaming pipeline wired for hub mode")

            # Register teardown on app shutdown
            @app.on_event("shutdown")
            async def _teardown_streaming_pipeline() -> None:
                teardown_pipeline(app)

    except Exception as exc:
        logger.debug("PCM streaming pipeline not available: %s", exc)


def _register_quota_exceeded_handler(app: FastAPI) -> None:
    """Register a global exception handler for LLMQuotaExceededError.

    This is a safety net: the intent pipeline normally catches the error
    and returns a PipelineResult with policy_flags=['rate_limited'].
    If the exception escapes, this handler ensures clients still receive
    a proper HTTP 429 with Retry-After.
    """
    from core.exceptions import LLMQuotaExceededError

    @app.exception_handler(LLMQuotaExceededError)
    async def _handle_quota_exceeded(
        request: Request,
        exc: LLMQuotaExceededError,
    ) -> JSONResponse:
        from services.command.http_responses import seconds_until_midnight_utc

        retry_after = seconds_until_midnight_utc()
        logger.warning(
            "LLMQuotaExceededError escaped to global handler for user %s: %s",
            exc.user_id,
            exc,
        )
        envelope = failure_response(
            "rate_limited",
            "Daily usage limit reached. Upgrade to Pro for higher limits.",
            data={
                "limit_type": exc.limit_type,
                "current": exc.current,
                "limit": exc.limit,
                "reset_at": exc.reset_at,
            },
        )
        return SafeJSONResponse(
            status_code=HTTPStatus.TOO_MANY_REQUESTS,
            content=envelope,
            headers={"Retry-After": str(retry_after)},
        )


def _register_viola_error_handler(app: FastAPI) -> None:
    """Register a global exception handler for all ViolaError subclasses.

    Auto-extracts user_friendly_message() from ViolaError exceptions so that
    every route handler benefits without per-handler boilerplate.  More
    specific handlers (e.g. LLMQuotaExceededError) registered elsewhere take
    precedence because FastAPI resolves the most-specific handler first.
    """
    from core.exceptions import (
        ConfigurationError,
        ServiceTimeoutError,
        ServiceUnavailableError,
        ViolaError,
    )

    def _status_for_error(exc: ViolaError) -> int:
        """Map ViolaError subclass to an appropriate HTTP status code."""
        if isinstance(exc, ServiceTimeoutError):
            return HTTPStatus.GATEWAY_TIMEOUT  # 504
        if isinstance(exc, ServiceUnavailableError):
            return HTTPStatus.SERVICE_UNAVAILABLE  # 503
        if isinstance(exc, ConfigurationError):
            return HTTPStatus.INTERNAL_SERVER_ERROR  # 500
        return HTTPStatus.INTERNAL_SERVER_ERROR  # 500 default

    @app.exception_handler(ViolaError)
    async def _handle_viola_error(
        request: Request,
        exc: ViolaError,
    ) -> JSONResponse:
        user_msg = exc.user_friendly_message()
        error_code = type(exc).__name__
        status = _status_for_error(exc)

        logger.exception(
            "ViolaError escaped to global handler: %s (code=%s, status=%d, path=%s)",
            exc,
            error_code,
            status,
            request.url.path,
        )

        # Build details from ErrorContext if available
        details = None
        if exc.context is not None:
            try:
                details = exc.context.to_details()
            except Exception:
                pass

        envelope = failure_response(
            error_code,
            user_msg,
            details=details,
        )
        return SafeJSONResponse(status_code=status, content=envelope)


_LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


async def _require_loopback_client(request: Request) -> None:
    """Refuse any non-loopback caller on an ``/auth/internal/*`` route.

    Both Google Workspace bridge routes spend Viola's OAuth ``client_secret``
    on caller-supplied input, so they are internal-only no matter what the
    desktop API happens to be bound to (``VIOLA_ENABLE_MULTIROOM=1`` promotes
    ``api_host`` off loopback).  Applied as a router dependency so the guard
    runs before any handler body and cannot be forgotten on a new route the
    way it was on ``GET /auth/internal/google`` (issue #4798).

    ``request.client.host`` is the TCP peer address and is used exclusively:
    ``X-Forwarded-For`` is deliberately ignored because the desktop app does
    not run behind a trusted reverse proxy, so honouring it would let any LAN
    client claim loopback (same reasoning as
    ``ui/api/routes/payment_cards.py``).  ``testclient`` is Starlette's
    synthetic in-process peer name and can never appear on a real socket —
    the same allowance ``backend/observability_routes.py`` and
    ``ui/core/security.py`` already make.
    """
    client_ip = (request.client.host if request.client else "").strip().lower()
    if client_ip not in _LOOPBACK_CLIENT_HOSTS:
        logger.warning(
            "Blocked non-loopback internal Google bridge request from %s (path=%s)",
            client_ip or "<unknown>",
            request.url.path,
        )
        raise HTTPException(status_code=403, detail="Forbidden")


def _include_optional_routers(app: FastAPI) -> None:
    """Include optional routers if their modules are available."""
    # Google Workspace MCP token refresh proxy.
    # The Node.js MCP server calls POST /auth/internal/google/refreshToken
    # to refresh OAuth tokens using Viola's client_secret.  Without this
    # route the MCP server falls back to interactive browser auth (5-min
    # timeout) on every call, causing 121s+ tool timeouts.
    try:
        from services.oauth.google import is_google_restricted_features_enabled

        if not is_google_restricted_features_enabled():
            logger.debug("Google Workspace refresh proxy disabled by restricted Google launch gate")
            raise ImportError("Google Workspace restricted features disabled")

        from services.oauth.workspace_bridge import handle_refresh_token as _ws_refresh

        # C5 / #4798: every route under /auth/internal/ is loopback-only.  The
        # guard is a router-level dependency rather than a per-handler check so
        # a route added here cannot silently ship unguarded.
        _gws_router = APIRouter(prefix="/auth", dependencies=[Depends(_require_loopback_client)])

        @_gws_router.post("/internal/google/refreshToken", dependencies=[Depends(_require_loopback_client)])
        async def _google_refresh_proxy(request: Request) -> Any:
            body = await request.json()
            refresh_token = body.get("refresh_token")
            if not refresh_token:
                raise HTTPException(status_code=400, detail="refresh_token required")
            try:
                result = await _ws_refresh(refresh_token)
                return JSONResponse(content=result)
            except Exception as exc:
                logger.exception("Google token refresh proxy failed")
                raise HTTPException(status_code=502, detail="Token refresh failed") from exc

        @_gws_router.get("/internal/google", dependencies=[Depends(_require_loopback_client)])
        async def _google_oauth_callback_proxy(request: Request) -> Any:
            """Local OAuth callback proxy for the Google Workspace MCP server.

            The MCP server sets WORKSPACE_CLOUD_FUNCTION_URL as both its
            redirect_uri (for OAuth) and its refresh endpoint base.  When
            the MCP server falls back to browser-based OAuth (e.g. tokens
            expired and refresh failed), Google redirects here with an
            authorization code.  This handler exchanges the code for tokens
            using Viola's client_secret and redirects the browser back to
            the MCP server's local /oauth2callback with the tokens — exactly
            mirroring the behavior of Google's cloud function.

            Loopback-only (``_require_loopback_client``): the exchange spends
            Viola's ``client_secret`` on a caller-supplied ``code``, so an
            off-box caller must never reach it (issue #4798).  Google's own
            redirect arrives as a top-level navigation in the user's browser
            on this machine, so the guard does not disturb the real flow.
            """
            import base64
            import json as _json_mod
            from urllib.parse import urlencode

            import aiohttp
            from fastapi.responses import HTMLResponse, RedirectResponse

            from config.settings import get_settings

            code = request.query_params.get("code")
            state = request.query_params.get("state")

            if not code:
                raise HTTPException(status_code=400, detail="Missing authorization code")

            settings = get_settings()
            client_id = getattr(settings, "google_client_id", "")
            client_secret = getattr(settings, "google_client_secret", "")
            api_port = getattr(settings, "api_port", 8756)
            redirect_uri = f"http://127.0.0.1:{api_port}/auth/internal/google"

            if not client_id or not client_secret:
                raise HTTPException(
                    status_code=500,
                    detail="Google OAuth client credentials not configured",
                )

            # Exchange authorization code for tokens
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                    },
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(
                            "Google OAuth token exchange failed (%d): %s",
                            resp.status,
                            text,
                        )
                        # Google's body echoes the caller-supplied code and our
                        # client_id back; log it, never return it (#4798).
                        raise HTTPException(
                            status_code=502,
                            detail="Token exchange failed",
                        )
                    data = await resp.json()

            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            expires_in = data.get("expires_in", 3600)
            scope = data.get("scope", "")
            token_type = data.get("token_type", "Bearer")
            import time as _time_mod

            expiry_date = int(_time_mod.time() * 1000) + expires_in * 1000

            # Decode state to find the MCP server's local redirect URI
            if state:
                try:
                    if len(state) > 4096:
                        raise ValueError("State parameter exceeds size limit")
                    payload = _json_mod.loads(base64.b64decode(state).decode("utf-8"))

                    # If not manual mode and a URI is present, redirect to it
                    if payload and payload.get("manual") is False and payload.get("uri"):
                        from urllib.parse import urlparse

                        parsed = urlparse(payload["uri"])
                        if parsed.hostname not in ("localhost", "127.0.0.1"):
                            raise ValueError("Invalid redirect hostname: %s" % parsed.hostname)

                        # Build redirect URL with tokens as query params
                        params = {
                            "access_token": access_token,
                            "scope": scope,
                            "token_type": token_type,
                            "expiry_date": str(expiry_date),
                        }
                        if refresh_token:
                            params["refresh_token"] = refresh_token
                        if payload.get("csrf"):
                            params["state"] = payload["csrf"]

                        final_url = payload["uri"]
                        separator = "&" if "?" in final_url else "?"
                        final_url = final_url + separator + urlencode(params)
                        return RedirectResponse(url=final_url, status_code=302)
                except Exception:
                    logger.exception("Error processing OAuth state, falling back to manual page")

            # Fallback: show tokens on page (same as cloud function)
            return HTMLResponse(
                content=(
                    "<html><body><h2>Google Workspace OAuth Complete</h2>"
                    "<p>Authentication succeeded. You can close this tab.</p>"
                    "</body></html>"
                )
            )

        app.include_router(_gws_router)
        logger.info("Google Workspace token refresh proxy registered")
    except (ImportError, Exception) as exc:
        logger.debug("Google Workspace refresh proxy not available: %s", exc)

    _include_consent_router(app)

    try:
        from ui.api.routes.about import router as about_router

        app.include_router(about_router)
    except (ImportError, Exception) as exc:
        logger.debug("About router not available: %s", exc)

    try:
        from ui.api.routes.browser_stream import router as stream_router

        app.include_router(stream_router)
    except (ImportError, Exception) as exc:
        logger.debug("Browser stream router not available: %s", exc)

    try:
        from ui.api.routes.preferences import router as preferences_router

        app.include_router(preferences_router)
    except (ImportError, Exception) as exc:
        logger.debug("Preferences router not available: %s", exc)

    # Shutdown hook for confirmation manager cleanup
    @app.on_event("shutdown")
    async def _shutdown_confirmation_manager() -> None:
        try:
            from services.payments.confirmation import get_confirmation_manager

            await get_confirmation_manager().shutdown()
        except Exception as exc:
            logger.debug("Confirmation manager shutdown: %s", exc)


def iter_effective_routes(routes: Any) -> Iterator[Any]:
    """Yield routes with resolved ``.path``/``.methods``, recursing through
    FastAPI's lazy include-router tree.

    FastAPI >= 0.137 stopped flattening ``app.include_router(...)`` children
    into ``app.router.routes``; each call instead adds one opaque tree node
    (``fastapi.routing._IncludedRouter``) whose own ``path``/``methods`` are
    None. A flat scan over ``app.router.routes`` therefore reports a mounted
    route as absent even though it dispatches correctly at runtime — this is
    the same "route-tree" class already documented in backend/cloud_app.py's
    ``_is_tier3_cloud_route_absence_scope`` (2026-07-02 incident: POST
    /auth/internal/google/refreshToken answered 401 instead of a fail-closed
    404 because a flat-scan removal missed a blocklisted route hiding inside
    a lazy include node) and independently rediscovered/worked around in
    ``tests/integration/test_settings_user_isolation.py``'s
    ``_iter_effective_routes`` and
    ``tests/canary/test_browser_spa_route_wiring.py`` — four separate
    occurrences of the same FastAPI internal-version workaround before this
    one, which is why this landed as the one shared, importable
    implementation instead of a fifth local copy.

    ``_IncludedRouter.effective_candidates()`` is the exact lazy-resolution
    FastAPI itself calls to match a real request (including prefix/dependency
    -override combination via its ``include_context``), so recursing through
    it mirrors real dispatch instead of re-deriving path/prefix logic by
    walking ``original_router.routes`` directly (which would silently return
    the wrong, unprefixed path for any router mounted with
    ``include_router(..., prefix=...)``). Ducks on ``effective_candidates``
    rather than importing the private ``_IncludedRouter`` class so this keeps
    working across FastAPI versions that do and do not do the lazy wrapping
    (desktop pins ``fastapi==0.133.1``, pre-router-tree-change; other
    environments resolve ``fastapi>=0.139`` per requirements-cloud.lock.txt) —
    on a version with no lazy wrapping, every route already has real
    ``.path``/``.methods`` and no route exposes a callable
    ``effective_candidates``, so this degrades to a plain flat yield.
    """
    for route in routes or []:
        effective_candidates = getattr(route, "effective_candidates", None)
        if callable(effective_candidates):
            yield from iter_effective_routes(effective_candidates())
        else:
            yield route


def _has_route(app: FastAPI, path: str, method: str) -> bool:
    method = method.upper()
    for route in iter_effective_routes(getattr(app.router, "routes", [])):
        if getattr(route, "path", None) != path:
            continue
        route_methods = getattr(route, "methods", set()) or set()
        if method in route_methods:
            return True
    return False


def _include_consent_router(app: FastAPI) -> None:
    """Mount the canonical consent router when it is not already present."""
    if _has_route(app, "/v1/consent/callback", "GET"):
        return

    try:
        from ui.consent_api import router as consent_router

        app.include_router(consent_router)
        logger.info("Consent router registered for desktop API")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Consent router not available: %s", exc)


def _include_desktop_gotrue_auth_proxy(app: FastAPI) -> None:
    """Mount the desktop same-origin GoTrue relay when absent.

    Identity is safety-core (#3365 / #3312): a swallowed import/mount failure
    here is the same "healthy container, missing API surface" class that
    ``.claude/rules/cloud-app.md`` forbids for backend/cloud_app.py's core
    routes. Unlike the desktop app's other optional/best-effort route mounts
    (consent, bug-report), the GoTrue proxy is the ONLY path desktop
    login/signup/refresh/logout can ever reach, so a failure here must never
    be swallowed at debug level — it must be loud (full traceback) and it
    must abort app construction, not ship a desktop build where every
    /auth/v1/* request silently 404s with zero trace. The billing checkout,
    portal-session, and webhook mounts, and the payment-cards and
    payment-confirm mounts, below carry the identical fail-closed contract
    for the same reason on the Payments & Billing safety-core surface
    (C-043 / #3365 sibling fix).
    """
    from services.company_service_boundary import shared_company_service_available

    if not shared_company_service_available("auth.desktop_gotrue_proxy"):
        return
    if _has_route(app, "/auth/v1/token", "POST"):
        return

    try:
        from auth.desktop_gotrue_proxy import desktop_gotrue_proxy_router

        app.include_router(desktop_gotrue_proxy_router)
        logger.info("Desktop GoTrue auth proxy registered")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop GoTrue auth proxy failed to mount; every /auth/v1/* route "
            "would silently 404 with no trace if this failure were swallowed"
        )
        raise


def _include_desktop_oauth_callback(app: FastAPI) -> None:
    """Mount the RFC 8252 loopback receiver for provider sign-in when absent.

    Same fail-closed contract as the GoTrue relay above, and for the same
    reason: this is the ONLY endpoint that can hand the browser leg of a
    Google sign-in back to the process holding the PKCE verifier. A swallowed
    mount failure would ship a build whose "Continue with Google" button waits
    out its deadline and reports a timeout to every user, forever, with no
    trace of why.
    """
    from services.company_service_boundary import shared_company_service_available

    if not shared_company_service_available("auth.desktop_oauth_callback"):
        return
    if _has_route(app, "/auth/callback", "GET"):
        return

    try:
        from auth.desktop_oauth_callback import desktop_oauth_callback_router

        app.include_router(desktop_oauth_callback_router)
        logger.info("Desktop OAuth loopback callback registered")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop OAuth loopback callback failed to mount; provider sign-in could never complete on this build"
        )
        raise


def _ensure_auth_router(app: FastAPI) -> None:
    """Mount ``auth.routes.auth_router`` (prefix ``/auth``) when absent.

    Identity & Access is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix above): this is the desktop app's ONLY mount of ``auth_router``
    (``backend/cloud_app.py`` mounts the separate cloud app), so a swallowed
    import/mount failure here is the same "healthy container, missing API
    surface" class ``.claude/rules/cloud-app.md`` forbids for
    backend/cloud_app.py's core routes. Several routes on this router
    (``/login``, ``/register``, ``/logout``, ``/refresh``) are AUTH-SWAP-1
    retired stubs that point callers at the GoTrue proxy's ``/auth/v1/*``
    mounted above, but the rest is live and safety/compliance-critical:
    ``/gdpr/export``, ``/gdpr/delete``, session management (``/me``,
    ``/sessions``), ``/password/change``, and the OAuth completion routes.
    A broken mount must abort app construction loudly, not ship a desktop
    build where every remaining ``/auth/*`` request silently 404s.

    This was previously registered post-bind
    (``ui/server.py:_register_auth_routes_after_bind`` via
    ``register_post_bind_initializer``); moved here because
    ``diagnostics/startup_telemetry.py``'s ``run_in_background()`` runs
    post-bind initializers on a daemon thread inside its own
    ``except Exception``, which would swallow a bare re-raise one layer up.
    Running from the synchronous pre-bind path here — and being
    re-attempted, unwrapped, on the minimal-app fallback path below — is
    exactly what makes the billing/GoTrue mounts in this module fail closed.
    """
    if _has_route(app, "/auth/login", "POST"):
        return

    try:
        from auth.routes import auth_router
        from ui.security.rate_limit_utils import configure_router_rate_limits

        configure_router_rate_limits(
            auth_router,
            default_limit=getattr(app.state, "default_rate_limit", None),
            limiter_enabled=getattr(app.state, "rate_limiter_enabled", False),
        )
        app.include_router(auth_router, prefix="/auth")
        logger.info("Desktop auth router registered")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop auth router failed to mount; every remaining /auth/* "
            "request (including the GDPR export/delete endpoints) would "
            "silently 404 with no trace if this failure were swallowed"
        )
        raise


def _ensure_billing_webhook_routes(app: FastAPI) -> None:
    """Register payment-provider webhook endpoints on the desktop API surface.

    Payments & Billing is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix): a swallowed import/mount failure here is the same "healthy
    container, missing API surface" class ``.claude/rules/cloud-app.md``
    forbids for backend/cloud_app.py's core routes. A broken Stripe/BTCPay
    webhook mount must never be swallowed at warning level with no
    traceback — it must abort app construction loudly, not ship a desktop
    build where every /billing/webhook/* request silently 404s.
    """
    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    wanted = {
        "/billing/webhook/stripe",
        "/billing/webhook/btcpay",
    }
    if wanted.issubset(paths):
        return

    try:
        from billing.routes import btcpay_webhook, stripe_webhook

        router = APIRouter()
        if "/billing/webhook/stripe" not in paths:
            router.add_api_route(
                "/billing/webhook/stripe",
                stripe_webhook,
                methods=["POST"],
                name="desktop_stripe_webhook",
            )
        if "/billing/webhook/btcpay" not in paths:
            router.add_api_route(
                "/billing/webhook/btcpay",
                btcpay_webhook,
                methods=["POST"],
                name="desktop_btcpay_webhook",
            )
        app.include_router(router)
        logger.info("Billing webhook routes registered for desktop API")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop billing webhook routes failed to mount; every "
            "/billing/webhook/* request would silently 404 with no trace "
            "if this failure were swallowed"
        )
        raise


def _ensure_billing_checkout_route(app: FastAPI) -> None:
    """Register the checkout JSON endpoint on the desktop API surface.

    Payments & Billing is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix): see ``_ensure_billing_webhook_routes`` above for the full
    rationale. A broken checkout mount must abort app construction loudly,
    not ship a desktop build where POST /billing/checkout silently 404s.
    """
    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    if "/billing/checkout" in paths:
        return

    try:
        from billing.routes import CheckoutResponse, create_checkout

        router = APIRouter()
        router.add_api_route(
            "/billing/checkout",
            create_checkout,
            methods=["POST"],
            response_model=CheckoutResponse,
            name="desktop_billing_checkout",
        )
        app.include_router(router)
        logger.info("Billing checkout route registered for desktop API")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop billing checkout route failed to mount; POST "
            "/billing/checkout would silently 404 with no trace if this "
            "failure were swallowed"
        )
        raise


def _ensure_billing_extra_usage_routes(app: FastAPI) -> None:
    """Register the extra-usage top-up endpoints on the desktop API surface.

    Payments & Billing is safety-core (C-043, sibling of the checkout/portal
    mounts above): the Terms name an extra-usage top-up as the primary option
    once a managed allowance is reached (#4215), so the desktop build must carry
    a reachable purchase surface, not a silent 404. A broken mount must abort
    app construction loudly.

    Two routes, both proxying to cloud (the desktop hub holds no Stripe
    credentials): POST ``/billing/extra-usage/checkout`` (the purchase) and GET
    ``/billing/extra-usage`` (the offer the UI renders its availability from).
    The POST keeps the ``paid_checkout`` kill switch so a desktop build is
    fail-closed exactly like the cloud route — that dependency lives on the
    cloud route's decorator, which ``add_api_route`` does not carry over, so it
    is re-attached here explicitly rather than silently dropped.
    """
    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    wanted = {"/billing/extra-usage/checkout", "/billing/extra-usage"}
    if wanted.issubset(paths):
        return

    try:
        from backend.launch_kill_switches import require_subsystem
        from billing.routes import (
            ExtraUsageCheckoutResponse,
            ExtraUsageOfferResponse,
            create_extra_usage_checkout,
            get_extra_usage_offer,
        )

        router = APIRouter()
        if "/billing/extra-usage/checkout" not in paths:
            router.add_api_route(
                "/billing/extra-usage/checkout",
                create_extra_usage_checkout,
                methods=["POST"],
                response_model=ExtraUsageCheckoutResponse,
                dependencies=[Depends(require_subsystem("paid_checkout"))],
                name="desktop_billing_extra_usage_checkout",
            )
        if "/billing/extra-usage" not in paths:
            router.add_api_route(
                "/billing/extra-usage",
                get_extra_usage_offer,
                methods=["GET"],
                response_model=ExtraUsageOfferResponse,
                name="desktop_billing_extra_usage_offer",
            )
        app.include_router(router)
        logger.info("Billing extra-usage routes registered for desktop API")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop billing extra-usage routes failed to mount; the "
            "Terms-promised top-up (POST /billing/extra-usage/checkout) would "
            "silently 404 with no trace if this failure were swallowed"
        )
        raise


def _ensure_billing_portal_session_route(app: FastAPI) -> None:
    """Register the Stripe portal-session endpoint on the desktop API surface.

    Payments & Billing is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix): see ``_ensure_billing_webhook_routes`` above for the full
    rationale. A broken portal-session mount must abort app construction
    loudly, not ship a desktop build where POST /v1/billing/portal/session
    silently 404s.
    """
    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    if "/v1/billing/portal/session" in paths:
        return

    try:
        from billing.routes import (
            BillingPortalSessionResponse,
            create_billing_portal_session,
        )

        router = APIRouter()
        router.add_api_route(
            "/v1/billing/portal/session",
            create_billing_portal_session,
            methods=["POST"],
            response_model=BillingPortalSessionResponse,
            name="desktop_billing_portal_session",
        )
        app.include_router(router)
        logger.info("Billing portal session route registered for desktop API")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop billing portal-session route failed to mount; POST "
            "/v1/billing/portal/session would silently 404 with no trace "
            "if this failure were swallowed"
        )
        raise


def _ensure_payment_cards_route(app: FastAPI) -> None:
    """Register the payment-card vault + spend-ceiling API on the desktop surface.

    Payments & Billing is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix and this PR's own billing checkout/portal/webhook fixes): a
    swallowed import/mount failure here is the same "healthy container,
    missing API surface" class ``.claude/rules/cloud-app.md`` forbids for
    backend/cloud_app.py's core routes. This mounts the PAY-11 Tier-3
    encrypted card vault AND the per-card spend-ceiling control
    (PUT/PATCH /api/payments/cards/{label}/ceiling) — losing the ceiling
    endpoint silently is a spend-control regression, not a cosmetic one. A
     A broken mount must abort app construction loudly when the payment module
     is part of the edition. A source-only edition may omit the private module;
     in that case the route is unavailable and the app continues without it.
    """
    if _has_route(app, "/api/payments/cards", "GET"):
        return

    try:
        from ui.api.routes.payment_cards import router as payment_cards_router

        app.include_router(payment_cards_router)
        logger.info("Payment cards router registered for desktop API")
    except ModuleNotFoundError as exc:
        if exc.name == "ui.api.routes.payment_cards":
            logger.info("Payment cards router unavailable in this edition")
            return
        raise
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop payment cards router failed to mount; every "
            "/api/payments/cards* request (including the spend-ceiling "
            "control) would silently 404 with no trace if this failure "
            "were swallowed"
        )
        raise


def _ensure_payment_confirm_route(app: FastAPI) -> None:
    """Register the human-in-the-loop purchase-approval flow when available.

    Payments & Billing is safety-core (C-043, sibling of the #3365 GoTrue
    proxy fix and this PR's own billing checkout/portal/webhook fixes): see
    ``_ensure_payment_cards_route`` above for the full rationale. This
    mounts the entire /confirm/{token}* purchase-approval surface (approve,
    reject, status, cards, one-shot-card, mark-reentry-*). A broken mount
     must abort app construction loudly when the payment module is part of the
     edition. A source-only edition may omit the private module; in that case
     purchase confirmation is unavailable.
    """
    if _has_route(app, "/confirm/{token}", "GET"):
        return

    try:
        from ui.api.routes.payment_confirm import router as payment_confirm_router

        app.include_router(payment_confirm_router)
        logger.info("Payment confirm router registered for desktop API")
    except ModuleNotFoundError as exc:
        if exc.name == "ui.api.routes.payment_confirm":
            logger.info("Payment confirmation router unavailable in this edition")
            return
        raise
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.exception(
            "Desktop payment confirm router failed to mount; every "
            "/confirm/* purchase-approval request would silently 404 with "
            "no trace if this failure were swallowed"
        )
        raise


def _ensure_bug_report_route(app: FastAPI) -> None:
    """Expose the bug report endpoint even when the full route tree is unavailable."""
    try:
        from ui.api.routes.feedback import (
            BUG_REPORT_ROUTE,
            BugReportSubmitRequest,
            handle_feedback_submission,
        )
    except Exception as exc:
        logger.debug("Bug report route unavailable: %s", exc)
        return

    # Expose to module globals so FastAPI can resolve the deferred annotation
    # (from __future__ import annotations turns it into a string evaluated via
    # get_type_hints, which only searches module globals).
    globals()["BugReportSubmitRequest"] = BugReportSubmitRequest

    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    if BUG_REPORT_ROUTE in paths:
        return

    router = APIRouter()

    @router.post(BUG_REPORT_ROUTE, dependencies=[Depends(require_auth)])
    async def _submit_bug_report(body: BugReportSubmitRequest, user_id: str = Depends(get_auth_user_id)) -> Any:
        return await handle_feedback_submission(
            user_id=user_id,
            feedback_type="bug",
            message=body.message,
            context=body.context,
            route=BUG_REPORT_ROUTE,
        )

    app.include_router(router)
    logger.info("Bug report route registered at %s", BUG_REPORT_ROUTE)


def _ensure_support_endpoints(app: FastAPI, *, state: Any, music: Any) -> None:
    """Register support endpoints that may be missing from the main route tree.

    Adds /v1/monitoring/metrics, /v1/scheduler/jobs, and
    /v1/diagnostics/health-detailed.  Idempotent: skips any path already
    registered.
    """
    paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
    router = APIRouter()

    # --- /v1/monitoring/metrics ---
    if "/v1/monitoring/metrics" not in paths:
        # SEC-025 class (2026-06-09 sweep): metrics surfaces are internal, not
        # public probes — require auth like every other observability route.

        @router.get("/v1/monitoring/metrics", dependencies=[Depends(require_auth)])
        async def _v1_monitoring_metrics() -> Any:
            from utils.monitoring import get_metrics_collector

            collector = get_metrics_collector()
            metrics_list = [m.to_dict() for m in collector.get_all_metrics().values()]
            return success_response({"metrics": metrics_list, "count": len(metrics_list)})

    # --- /v1/scheduler/jobs ---
    if "/v1/scheduler/jobs" not in paths:

        @router.get("/v1/scheduler/jobs", dependencies=[Depends(require_auth)])
        async def _v1_scheduler_jobs(user_id: str = Depends(get_auth_user_id)) -> Any:
            scheduler = getattr(app.state, "scheduler", None)
            if scheduler is None:
                # F-051: distinguish "no scheduler wired" from "empty job list".
                # The caller (UI / health probe) needs to surface this as a
                # 503-class failure instead of misreporting zero jobs.
                return JSONResponse(
                    status_code=503,
                    content=failure_response(
                        "scheduler_unavailable",
                        "The scheduler service is not initialized on this surface.",
                    ),
                )
            try:
                all_jobs = scheduler.list_schedules(user_id, enabled_only=False)
            except Exception:
                # F-051: a store failure must surface to the caller, not be
                # papered over as a successful empty result.
                logger.exception("Failed to list scheduler jobs for user=%s", user_id)
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "scheduler_list_failed",
                        "Could not load scheduled jobs. Please retry.",
                    ),
                )

            enabled = [j for j in all_jobs if j.enabled]
            jobs_out = []
            for j in all_jobs:
                jobs_out.append(
                    {
                        "id": j.id,
                        "label": j.label,
                        "action": j.action,
                        "cron_expr": j.cron_expr,
                        "one_shot_at": j.one_shot_at,
                        "enabled": j.enabled,
                        "next_run_at": j.next_run_at,
                        "run_count": j.run_count,
                        "last_status": j.last_status,
                    }
                )
            return success_response(
                {
                    "jobs": jobs_out,
                    "summary": {
                        "total": len(all_jobs),
                        "enabled": len(enabled),
                        "disabled": len(all_jobs) - len(enabled),
                    },
                }
            )

    # --- /v1/diagnostics/health-detailed ---
    if "/v1/diagnostics/health-detailed" not in paths:

        @router.get("/v1/diagnostics/health-detailed")
        async def _v1_health_detailed() -> Any:
            import time as _time

            ready = True
            ready_attr = getattr(state, "is_ready", None)
            if callable(ready_attr):
                try:
                    ready = ready_attr()
                except Exception:
                    ready = True

            payload: dict[str, Any] = {
                "status": "ok" if ready else "starting",
                "ready": ready,
                "timestamp": _time.time(),
                "dependencies": {},
            }

            start_time = getattr(state, "start_time", None)
            if start_time is not None:
                try:
                    uptime = max(0.0, _time.time() - float(start_time))
                    payload["uptime_s"] = round(uptime, 3)
                except Exception:
                    pass

            return success_response(payload)

    if router.routes:
        app.include_router(router)
        logger.info("Support endpoints registered")


def _register_regression_stubs(app: FastAPI) -> None:
    """Register empty-state regression safety-net stubs.

    The stub registrar must only fill genuinely absent route surfaces. It
    probes real route factories/registrars as well as the app route table so
    concurrent post-bind route initializers cannot let stubs shadow real
    handlers that are still registering.
    """
    from ui.api.routes.regression_stubs import register_regression_stub_routes

    register_regression_stub_routes(app)


def _register_synchronous_route_definitions(app: FastAPI, *, state: Any, music: Any) -> None:
    """Register route definitions that must exist before the first request."""
    if getattr(app.state, "_synchronous_route_definitions_registered", False):
        return

    _include_consent_router(app)
    _include_desktop_gotrue_auth_proxy(app)
    _include_desktop_oauth_callback(app)
    _ensure_auth_router(app)
    from telephony.local_webhook import router as local_phone_webhook_router

    app.include_router(local_phone_webhook_router)
    from services.company_service_boundary import company_service_module_available

    if company_service_module_available(
        "billing.routes",
        component="Desktop company billing routes",
    ):
        _ensure_billing_checkout_route(app)
        _ensure_billing_extra_usage_routes(app)
        _ensure_billing_portal_session_route(app)
        _ensure_billing_webhook_routes(app)
    _ensure_payment_cards_route(app)
    _ensure_payment_confirm_route(app)
    _ensure_bug_report_route(app)
    _ensure_support_endpoints(app, state=state, music=music)
    _register_regression_stubs(app)
    app.state._synchronous_route_definitions_registered = True
    logger.info("Synchronous backend route definitions registered")


CreateAppType = Callable[[Any, Any | None, IntentBridgeProtocol], Any]


def build_fastapi_app(state: Any, music: Any | None, intent: IntentBridgeProtocol) -> Any:
    """
    Build FastAPI application with Hub State Authority integration.

    All provider state updates flow through Hub State Authority before being applied.
    The implementation mirrors the legacy ``viola_main`` behaviour: prefer the
    full-featured app factory in ``ui.server`` and fall back to a minimal API
    when the optional dependency graph is unavailable (e.g. constrained test
    environments).
    """
    supervisor = ensure_supervisor(state)
    # Skip supervisor attach if intent is a lazy proxy -- it will be attached
    # when the proxy materializes and attach_supervisor is called later.
    try:
        from bootstrap.lazy_intent import LazyIntentBridge

        if not isinstance(intent, LazyIntentBridge):
            attach = getattr(intent, "attach_supervisor", None)
            if callable(attach):
                attach(supervisor)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("IntentBridge supervisor attach failed: %s", exc)

    # Initialize Hub State Authority
    hub_state_authority = HubStateAuthority(
        initial_canonical_state=None,  # Start with empty state, will be populated by first provider update
        on_refresh_scheduled=lambda provider_id: logger.info("Coarse refresh scheduled for provider: %s", provider_id),
    )

    create_app = _try_import_create_app()
    if create_app is not None:
        try:
            app = create_app(state, music, intent)

            # Register app instance so ui.server.get_app() works for internal
            # components (e.g. Telegram notification router in ai_controller.py).
            try:
                from ui.server import _set_current_app

                _set_current_app(app)
            except Exception:
                pass  # Non-fatal: messaging notifications will silently no-op

            # Store Hub State Authority in app state
            app.state.hub_state_authority = hub_state_authority

            # Wire hub state authority to music player for direct state updates
            if music is not None and hasattr(music, "set_hub_state_authority"):
                music.set_hub_state_authority(hub_state_authority)
                logger.info("✅ Hub state authority connected to music player")
            else:
                logger.warning(
                    "⚠️ Music player does not have set_hub_state_authority method (type=%s)",
                    type(music).__name__,
                )

            # Register the envelope-shaped HTTPException handler BEFORE
            # attaching the contract middleware. FastAPI's default handler
            # emits ``{"detail": ...}`` on raise HTTPException(...), which
            # the strict pure-ASGI envelope middleware (commit ff966465)
            # correctly rejects as non-envelope — the old BaseHTTPMiddleware
            # version had a body-read-error fallback that masked this. Auth
            # routes raise ``HTTPException(detail=failure_response(...))``
            # so the inner detail IS already an envelope; the handler at
            # ``contracts/fastapi_helpers.py:EnvelopeExceptionHandler``
            # unwraps it to top-level ``{ok: false, error: ...}`` and emits
            # canonical envelopes for plain string/legacy detail too.
            from contracts.fastapi_helpers import EnvelopeExceptionHandler

            EnvelopeExceptionHandler.register(app)

            # Latency decomposition root span (no-op unless
            # VIOLA_LATENCY_SPANS_DIR is set; see diagnostics/latency_spans.py).
            from diagnostics import latency_spans

            if latency_spans.enabled:

                @app.middleware("http")
                async def _latency_server_root(request, call_next):
                    if request.url.path != "/v1/command":
                        return await call_next(request)
                    latency_spans.start_event_loop_heartbeat()
                    turn_id = latency_spans.new_turn()
                    with latency_spans.span("SERVER_ROOT", turn_id=turn_id):
                        return await call_next(request)

            attach_response_contract(
                app,
                skip_paths=(
                    "/metrics",
                    "/docs",
                    "/openapi.json",
                    "/v1/command",
                    "/stream",
                    "/billing/checkout",
                    "/billing/checkout/status",
                    "/billing/extra-usage/checkout",
                    "/billing/extra-usage",
                ),
            )

            # Register missing core routes if the full app didn't include them.
            # The canonical implementations live in ui/api/routes/, so this
            # repair path mounts the same handlers as the full router.
            paths = {getattr(route, "path", None) for route in getattr(app.router, "routes", [])}
            missing_core_routes = {
                "/v1/command",
                "/v1/system/profile",
                "/config",
            } - paths
            if missing_core_routes:
                from backend.app_builder import register_canonical_core_routes
                from services.command import CommandServiceContext
                from services.command.service import CommandService

                _ledger = IdempotencyLedger(_COMMAND_LEDGER_PATH)
                _ledger.reconcile()
                _command_service = CommandService(
                    context=CommandServiceContext(
                        state=state,
                        music=music,
                        intent=intent,
                        idempotency_ledger=_ledger,
                    )
                )
                register_canonical_core_routes(
                    app,
                    state=state,
                    music=music,
                    intent=intent,
                    command_service=_command_service,
                )
                logger.info(
                    "Registered missing core routes via canonical routers: %s",
                    missing_core_routes,
                )

            from ui.api.routes.agents import register_agent_routes

            register_agent_routes(app, intent=intent)

            ensure_player_state_contract(app)
            _setup_sync_engine(app)
            _setup_companion_client(app)
            # G1 fix: register multiroom router synchronously (route defs only).
            # mDNS discovery deferred to 3s timer (the real perf win).
            _register_multiroom_router(app)
            _defer_multiroom_discovery(app, state)
            _register_synchronous_route_definitions(app, state=state, music=music)

            # #1016: wire the lifespan-started services (scheduler, calendar
            # reminder sweep, health watchdog) SYNCHRONOUSLY here, before the
            # server binds and before the desktop lifespan runs. They MUST NOT be
            # deferred into the post-bind initializer below. The desktop lifespan
            # (ui/api/routes/lifecycle.py) starts app.state.scheduler /
            # app.state.calendar_reminders / app.state.health_watchdog during
            # startup, but uvicorn completes lifespan startup (sets
            # server.started=True) BEFORE core/server_factory.py's
            # run_post_bind_initializers() fires. When these were wired post-bind,
            # app.state.* was still None at lifespan-start time, so their .start()
            # branches were skipped, the due-check loops never ran, and EVERY
            # scheduled feature (calendar reminders #348, notify schedules,
            # health watchdog) was silently dead on the shipped desktop app. The
            # minimal fallback path below already wires them synchronously for
            # this exact reason.
            _setup_scheduler(app, intent)
            _setup_calendar_reminders(app, intent)
            _setup_health_watchdog(app, intent)

            def _register_backend_optional_after_bind() -> None:
                from backend.observability_routes import register_observability_routes

                register_observability_routes(app, state=state)
                _include_optional_routers(app)
                _setup_hub_pcm_pipeline(app)

            from diagnostics.startup_telemetry import register_post_bind_initializer

            register_post_bind_initializer(
                app,
                "backend_optional_routes",
                _register_backend_optional_after_bind,
                registers_routes=True,
            )

            return app
        except Exception:
            logger.exception("create_app failed during startup; falling back to minimal app")

    from backend.app_builder import build_minimal_app
    from services.command import CommandServiceContext
    from services.command.service import CommandService

    command_service = CommandService(
        context=CommandServiceContext(
            state=state,
            music=music,
            intent=intent,
        )
    )

    app = build_minimal_app(
        state=state,
        music=music,
        intent=intent,
        command_service=command_service,
    )

    # Store Hub State Authority in app state
    app.state.hub_state_authority = hub_state_authority

    # Wire hub state authority to music player for direct state updates
    if music is not None and hasattr(music, "set_hub_state_authority"):
        music.set_hub_state_authority(hub_state_authority)
        logger.info("✅ Hub state authority connected to music player (fallback)")
    else:
        logger.warning(
            "⚠️ Music player does not have set_hub_state_authority method (fallback, type=%s)",
            type(music).__name__,
        )

    _register_viola_error_handler(app)
    ensure_player_state_contract(app)
    _setup_sync_engine(app)
    _setup_companion_client(app)
    # G1 fix: register multiroom router synchronously (route defs only).
    # mDNS discovery deferred to 3s timer (the real perf win).
    _register_multiroom_router(app)
    _defer_multiroom_discovery(app, state)
    _register_synchronous_route_definitions(app, state=state, music=music)
    _include_optional_routers(app)
    _setup_hub_pcm_pipeline(app)
    try:
        from backend.observability_routes import register_observability_routes

        register_observability_routes(app, state=state)
    except Exception as exc:
        logger.warning("Failed to register observability routes in fallback: %s", exc)
    _setup_scheduler(app, intent)
    _setup_calendar_reminders(app, intent)
    _setup_health_watchdog(app, intent)
    return app
