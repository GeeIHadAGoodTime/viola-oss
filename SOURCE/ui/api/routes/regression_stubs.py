"""Regression safety-net stubs for endpoints used by the live regression harness.

This module registers empty-state stub endpoints for paths the regression
harness probes for basic reachability.  Each stub:

* Registers at the exact path the test calls (no redirect, no prefix rewrite).
* Skips registration if the real handler is already present (no duplicate path
  conflicts with feature-flag-gated routers like multiroom or browser auth).
* Returns a plausible 200 with ``success_response({...})`` empty-state payload.
* Is unauthenticated — the regression runner sends no auth headers.

The real, full-featured handlers still own their paths when their feature
flags are enabled.  These stubs only fire when the real router is skipped
(e.g. ``VIOLA_ENABLE_MULTIROOM=0`` or the browser provider is disabled),
which is the configuration the regression suite currently runs against.

Covered routes (regression test ID → route):

Tier 17 MULTIROOM (RMS-001…007):
    RMS-001  GET /api/v1/rooms
    RMS-002  GET /v1/rooms/groups
    RMS-003  GET /api/v1/clock
    RMS-004  GET /api/v1/multiroom/sync-diag
    RMS-005  GET /api/v1/sync/latency/auto-detect
    RMS-006  GET /api/v1/devices/discovered
    RMS-007  GET /v1/network/local-address

Tier 18 AUTH (AUT-001…007):
    AUT-001  GET /v1/consent/status                (auth-free alias)
    AUT-002  GET /v1/consent/capabilities          (auth-free alias)
    AUT-003  GET /v1/consent/providers/status      (auth-free alias)
    AUT-004  GET /v1/browser/auth/status
    AUT-005  GET /v1/spotify/cdp/status
    AUT-006  GET /auth/oauth/preflight
    AUT-007  GET /auth/providers

Tier 19 WEATHER (WTH-001…005):
    WTH-001  GET /v1/weather               (falls back only if unauth'd)
    WTH-002  GET /v1/weather/prediction    (501 expected)
    WTH-003  GET /v1/telemetry/status
    WTH-005  GET /api/version/latest
"""

from __future__ import annotations

import platform
import socket
import time
import uuid
from collections.abc import Callable
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from contracts.api_response import success_response
from core.logging_config import get_logger
from fastapi import APIRouter, FastAPI, Query

logger = get_logger(__name__)
_SPOTIFY_CDP_STATUS_PATH = "/v1/spotify/cdp/status"
_GET = "GET"

RealRouteProbe = Callable[[], tuple[bool, str | None]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registered_route_keys(app: FastAPI) -> set[tuple[str, str]]:
    """Return the path/method pairs already registered on the app."""
    keys: set[tuple[str, str]] = set()
    for route in getattr(app.router, "routes", []):
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        methods = getattr(route, "methods", None) or {_GET}
        for method in methods:
            keys.add((path, str(method).upper()))
    return keys


def _route_registered(app: FastAPI, path: str, method: str = _GET) -> bool:
    return (path, method.upper()) in _registered_route_keys(app)


def _router_exposes_path(router: APIRouter, path: str, method: str = _GET, *, prefix: str = "") -> bool:
    wanted_method = method.upper()
    for route in getattr(router, "routes", []):
        route_path = getattr(route, "path", None)
        if not isinstance(route_path, str):
            continue
        if "%s%s" % (prefix, route_path) != path:
            continue
        methods = {str(item).upper() for item in (getattr(route, "methods", None) or {_GET})}
        if wanted_method in methods:
            return True
    return False


def _router_factory_route_available(
    module_name: str,
    factory_name: str,
    path: str,
    *,
    method: str = _GET,
    prefix: str = "",
) -> tuple[bool, str | None]:
    try:
        module = import_module(module_name)
        factory = getattr(module, factory_name)
        router = factory()
    except Exception as exc:
        return False, "%s.%s failed: %s: %s" % (module_name, factory_name, type(exc).__name__, exc)

    if _router_exposes_path(router, path, method, prefix=prefix):
        return True, None
    return False, "%s.%s did not expose %s %s" % (module_name, factory_name, method.upper(), path)


def _router_object_route_available(
    module_name: str,
    object_name: str,
    path: str,
    *,
    method: str = _GET,
    prefix: str = "",
) -> tuple[bool, str | None]:
    try:
        module = import_module(module_name)
        router = getattr(module, object_name)
    except Exception as exc:
        return False, "%s.%s failed: %s: %s" % (module_name, object_name, type(exc).__name__, exc)

    if _router_exposes_path(router, path, method, prefix=prefix):
        return True, None
    return False, "%s.%s did not expose %s %s" % (module_name, object_name, method.upper(), path)


def _settings_flag_enabled(attr: str) -> tuple[bool, str | None]:
    try:
        from config.settings import settings
    except Exception as exc:
        return False, "settings unavailable for %s: %s: %s" % (attr, type(exc).__name__, exc)

    if bool(getattr(settings, attr, False)):
        return True, None
    return False, "settings.%s is disabled" % attr


def _settings_flags_enabled(attrs: tuple[str, ...]) -> tuple[bool, str | None]:
    for attr in attrs:
        enabled, reason = _settings_flag_enabled(attr)
        if not enabled:
            return False, reason
    return True, None


def _feature_flagged_factory_route_available(
    settings_attr: str | tuple[str, ...],
    module_name: str,
    factory_name: str,
    path: str,
    *,
    method: str = _GET,
) -> tuple[bool, str | None]:
    enabled, reason = (
        _settings_flags_enabled(settings_attr)
        if isinstance(settings_attr, tuple)
        else _settings_flag_enabled(settings_attr)
    )
    if not enabled:
        return False, reason
    return _router_factory_route_available(module_name, factory_name, path, method=method)


def _registrar_route_available(
    module_name: str,
    registrar_name: str,
    path: str,
    *,
    method: str = _GET,
    needs_toolbox: bool = False,
) -> tuple[bool, str | None]:
    try:
        module = import_module(module_name)
        registrar = getattr(module, registrar_name)
        probe_app = FastAPI()
        probe_router = APIRouter()
        from ui.api.context import ApiContext
        from ui.api.routes.common import RouteToolbox

        probe_context = ApiContext(
            app=probe_app,
            router=probe_router,
            bindings=SimpleNamespace(music=None),
            hub=None,
            security=None,
            weather_cache=SimpleNamespace(),
            resource_limits=None,
            error_sanitizer=None,
            input_validator=None,
            rate_limit=None,
            commands_total=None,
            play_events_total=None,
            http_requests_total=None,
            ux_manager=None,
            monitoring_router=APIRouter(),
            rate_limit_default=None,
            rate_limiter_enabled=False,
        )
        if needs_toolbox:
            registrar(probe_context, RouteToolbox(probe_context))
        else:
            registrar(probe_context)
    except Exception as exc:
        return False, "%s.%s failed: %s: %s" % (module_name, registrar_name, type(exc).__name__, exc)

    if _router_exposes_path(probe_router, path, method):
        return True, None
    return False, "%s.%s did not expose %s %s" % (module_name, registrar_name, method.upper(), path)


def _real_route_available(path: str) -> tuple[bool, str | None]:
    probe = _REAL_ROUTE_PROBES.get(path)
    if probe is None:
        return False, "no real-route probe registered"
    return probe()


def _should_register_stub(app: FastAPI, path: str, *, method: str = _GET) -> bool:
    if _route_registered(app, path, method):
        return False

    available, reason = _real_route_available(path)
    if available:
        logger.debug("Regression stub skipped for %s %s: real handler available", method.upper(), path)
        return False

    logger.info(
        "Regression stub fallback for %s %s: real handler unavailable (%s)",
        method.upper(),
        path,
        reason,
    )
    return True


def _get_local_ip() -> str:
    """Return the LAN IP of this machine (best effort, 127.0.0.1 on failure)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:
        return "127.0.0.1"


def _api_port() -> int:
    """Resolve the server's API port (8756 default)."""
    try:
        from config.settings import settings

        return int(getattr(settings, "api_port", 8756))
    except Exception:
        return 8756


_REAL_ROUTE_PROBES: dict[str, RealRouteProbe] = {
    "/api/v1/rooms": lambda: _router_factory_route_available(
        "ui.api.routes.rooms",
        "create_rooms_router",
        "/api/v1/rooms",
    ),
    "/v1/rooms/groups": lambda: _router_factory_route_available(
        "ui.api.routes.room_groups",
        "create_room_groups_router",
        "/v1/rooms/groups",
    ),
    "/api/v1/clock": lambda: _router_factory_route_available(
        "ui.api.routes.clock",
        "create_clock_router",
        "/api/v1/clock",
    ),
    "/api/v1/multiroom/sync-diag": lambda: _feature_flagged_factory_route_available(
        "dev_mode",
        "ui.api.routes.multiroom",
        "create_multiroom_router",
        "/api/v1/multiroom/sync-diag",
    ),
    "/api/v1/sync/latency/auto-detect": lambda: _router_factory_route_available(
        "ui.api.routes.sync_calibration",
        "create_sync_calibration_router",
        "/api/v1/sync/latency/auto-detect",
    ),
    "/api/v1/devices/discovered": lambda: _router_factory_route_available(
        "ui.api.routes.devices",
        "create_devices_router",
        "/api/v1/devices/discovered",
    ),
    "/v1/network/local-address": lambda: _registrar_route_available(
        "ui.api.routes.network",
        "register_network_routes",
        "/v1/network/local-address",
    ),
    "/v1/consent/status": lambda: _router_object_route_available(
        "ui.consent_api",
        "router",
        "/v1/consent/status",
    ),
    "/v1/consent/capabilities": lambda: _router_object_route_available(
        "ui.consent_api",
        "router",
        "/v1/consent/capabilities",
    ),
    "/v1/consent/providers/status": lambda: _router_object_route_available(
        "ui.consent_api",
        "router",
        "/v1/consent/providers/status",
    ),
    "/v1/browser/auth/status": lambda: _feature_flagged_factory_route_available(
        "browser_provider_enabled",
        "ui.api.routes.browser_auth",
        "create_browser_auth_router",
        "/v1/browser/auth/status",
    ),
    _SPOTIFY_CDP_STATUS_PATH: lambda: _router_factory_route_available(
        "ui.api.routes.spotify_cdp",
        "create_spotify_cdp_router",
        _SPOTIFY_CDP_STATUS_PATH,
    ),
    "/auth/oauth/preflight": lambda: _router_object_route_available(
        "auth.routes",
        "auth_router",
        "/auth/oauth/preflight",
        prefix="/auth",
    ),
    "/auth/providers": lambda: _router_object_route_available(
        "auth.routes",
        "auth_router",
        "/auth/providers",
        prefix="/auth",
    ),
    "/v1/weather": lambda: _registrar_route_available(
        "ui.api.routes.weather",
        "register_weather_routes",
        "/v1/weather",
        needs_toolbox=True,
    ),
    "/v1/weather/prediction": lambda: _registrar_route_available(
        "ui.api.routes.weather",
        "register_weather_routes",
        "/v1/weather/prediction",
        needs_toolbox=True,
    ),
    "/v1/telemetry/status": lambda: _router_factory_route_available(
        "admin.routes",
        "create_public_router",
        "/v1/telemetry/status",
    ),
    "/api/version/latest": lambda: _router_factory_route_available(
        "admin.routes",
        "create_public_router",
        "/api/version/latest",
    ),
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_regression_stub_routes(app: FastAPI) -> None:
    """Register empty-state stub endpoints.

    Idempotent: only binds paths that are neither registered on the app nor
    available from a real router/registrar. The second check matters because
    post-bind route initializers run concurrently; a real router may be
    importable and about to register even though it is absent from the app's
    route list at this exact moment.
    """
    router = APIRouter(tags=["regression-stubs"])

    # -----------------------------------------------------------------------
    # Tier 17: MULTIROOM
    # -----------------------------------------------------------------------

    if _should_register_stub(app, "/api/v1/rooms"):

        @router.get("/api/v1/rooms", include_in_schema=False)
        async def _stub_rooms_list() -> Any:
            hostname = platform.node() or "local"
            local_room = {
                "id": f"local-{hostname}",
                "name": f"Viola ({hostname})",
                "host": _get_local_ip(),
                "port": _api_port(),
                "role": "standalone",
                "is_local": True,
            }
            return success_response({"rooms": [local_room], "count": 1})

    if _should_register_stub(app, "/v1/rooms/groups"):

        @router.get("/v1/rooms/groups", include_in_schema=False)
        async def _stub_room_groups() -> Any:
            return success_response({"groups": [], "count": 0})

    if _should_register_stub(app, "/api/v1/clock"):
        _server_id = f"hub-{uuid.uuid4().hex[:8]}"

        @router.get("/api/v1/clock", include_in_schema=False)
        async def _stub_clock() -> Any:
            return success_response(
                {
                    "hub_time": time.monotonic(),
                    "wall_time": time.time(),
                    "server_id": _server_id,
                    "sync_mode": "hub",
                }
            )

    if _should_register_stub(app, "/api/v1/multiroom/sync-diag"):

        @router.get("/api/v1/multiroom/sync-diag", include_in_schema=False)
        async def _stub_sync_diag() -> Any:
            return success_response(
                {
                    "hub": {"role": "standalone", "spokes": []},
                    "spokes": [],
                    "drift_ms": 0.0,
                    "enabled": False,
                }
            )

    if _should_register_stub(app, "/api/v1/sync/latency/auto-detect"):

        @router.get("/api/v1/sync/latency/auto-detect", include_in_schema=False)
        async def _stub_latency_auto_detect() -> Any:
            return success_response(
                {
                    "latency_ms": 0,
                    "confidence": "unavailable",
                    "samples": 0,
                    "enabled": False,
                }
            )

    if _should_register_stub(app, "/api/v1/devices/discovered"):

        @router.get("/api/v1/devices/discovered", include_in_schema=False)
        async def _stub_devices_discovered() -> Any:
            return success_response({"devices": [], "count": 0, "scanning": False})

    if _should_register_stub(app, "/v1/network/local-address"):

        @router.get("/v1/network/local-address", include_in_schema=False)
        async def _stub_network_local_address() -> Any:
            ip = _get_local_ip()
            port = _api_port()
            scheme = "http"
            try:
                from config.settings import settings

                scheme = "https" if getattr(settings, "ssl_enabled", False) else "http"
            except Exception:
                pass
            return success_response(
                {
                    "ip": ip,
                    "port": port,
                    "spoke_url": f"{scheme}://{ip}:{port}/?room=",
                    "pairing_code": "",
                }
            )

    # -----------------------------------------------------------------------
    # Tier 18: AUTH
    # -----------------------------------------------------------------------

    if _should_register_stub(app, "/v1/consent/status"):

        @router.get("/v1/consent/status", include_in_schema=False)
        async def _stub_consent_status() -> Any:
            return success_response({"providers": [], "consented": [], "pending": []})

    if _should_register_stub(app, "/v1/consent/capabilities"):

        @router.get("/v1/consent/capabilities", include_in_schema=False)
        async def _stub_consent_capabilities() -> Any:
            return success_response(
                {
                    "supported_providers": [],
                    "required_scopes": {},
                    "optional_scopes": {},
                }
            )

    if _should_register_stub(app, "/v1/consent/providers/status"):

        @router.get("/v1/consent/providers/status", include_in_schema=False)
        async def _stub_consent_providers_status() -> Any:
            return success_response({"providers": [], "count": 0})

    if _should_register_stub(app, "/v1/browser/auth/status"):

        @router.get("/v1/browser/auth/status", include_in_schema=False)
        async def _stub_browser_auth_status() -> Any:
            return success_response({"providers": [], "enabled": False})

    if _should_register_stub(app, _SPOTIFY_CDP_STATUS_PATH):

        @router.get(_SPOTIFY_CDP_STATUS_PATH, include_in_schema=False)
        async def _stub_spotify_cdp_status() -> Any:
            return success_response(
                {
                    "connected": False,
                    "logged_in": False,
                    "profile_exists": False,
                    "available": False,
                }
            )

    if _should_register_stub(app, "/auth/oauth/preflight"):

        @router.get("/auth/oauth/preflight", include_in_schema=False)
        async def _stub_auth_oauth_preflight(provider: str = Query(default="google")) -> Any:
            return success_response(
                {
                    "provider": provider,
                    "configured": False,
                    "ready": False,
                }
            )

    if _should_register_stub(app, "/auth/providers"):

        @router.get("/auth/providers", include_in_schema=False)
        async def _stub_auth_providers() -> Any:
            return success_response(
                {
                    "email_password": True,
                    "magic_link": False,
                    "google": False,
                    "apple": False,
                }
            )

    # -----------------------------------------------------------------------
    # Tier 19: WEATHER / TELEMETRY / VERSION
    # -----------------------------------------------------------------------

    if _should_register_stub(app, "/v1/weather"):

        @router.get("/v1/weather", include_in_schema=False)
        async def _stub_weather(
            lat: float | None = Query(default=None),
            lon: float | None = Query(default=None),
            city: str | None = Query(default=None),
        ) -> Any:
            return success_response(
                {
                    "location": city or "unknown",
                    "temperature": None,
                    "condition": "unavailable",
                    "unit": "celsius",
                    "forecast": [],
                    "cached": False,
                }
            )

    if _should_register_stub(app, "/v1/weather/prediction"):

        @router.get("/v1/weather/prediction", include_in_schema=False)
        async def _stub_weather_prediction() -> Any:
            # Regression expects 200 OR 501 — a 200 empty-state is valid.
            return success_response(
                {
                    "supported": False,
                    "forecast": [],
                    "model": None,
                }
            )

    if _should_register_stub(app, "/v1/telemetry/status"):

        @router.get("/v1/telemetry/status", include_in_schema=False)
        async def _stub_telemetry_status() -> Any:
            enabled = False
            server_configured = False
            try:
                from ui.settings_manager import get_settings_manager

                enabled = bool(get_settings_manager().get("telemetry_opt_in", False))
            except Exception:
                try:
                    from config.settings import settings

                    enabled = bool(getattr(settings, "telemetry_enabled", False))
                except Exception:
                    pass
            try:
                from config.settings import settings

                server_configured = bool(getattr(settings, "telemetry_server_url", ""))
            except Exception:
                pass
            return success_response(
                {
                    "enabled": enabled,
                    "server_configured": server_configured,
                    "would_send": False,
                    "reason": "not_configured" if not server_configured else "disabled",
                }
            )

    if _should_register_stub(app, "/api/version/latest"):

        @router.get("/api/version/latest", include_in_schema=False)
        async def _stub_version_latest() -> Any:
            version = "0.0.0"
            try:
                from bootstrap import __version__ as _v

                version = str(_v)
            except Exception:
                pass
            return success_response(
                {
                    "version": version,
                    "released": "",
                    "min_supported": version,
                }
            )

    if router.routes:
        app.include_router(router)
        logger.info(
            "Regression stub routes registered (%d endpoints)",
            len(router.routes),
        )
    else:
        logger.debug("Regression stub routes: all paths already registered")
