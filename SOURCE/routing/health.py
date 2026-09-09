"""routing/health.py - Health Endpoint Handlers

Provides a central implementation for the canonical ``/health`` route
with optional legacy redirect support and readiness gating.

Note: This is a ROUTING concern (endpoint registration), not diagnostics.
For health metrics data collection, see:
- diagnostics/runtime_metrics.py
- diagnostics/error_registry.py
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from fastapi.responses import JSONResponse, RedirectResponse

from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger
from fastapi import APIRouter, Request

logger = get_logger(__name__)


class _StartTimeCarrier(Protocol):
    start_time: float | int | str


HealthPayload = Mapping[str, JsonValue]
HealthStatusProvider = Callable[..., Awaitable[HealthPayload] | HealthPayload]
ReadyCheck = Callable[[], bool]

DEFAULT_NOT_READY_STATUS = "starting"

#: Status reported when the HTTP thread is answering but the Qt message loop
#: that owns the user-visible app has stopped dispatching (#4650). Distinct
#: from ``error`` so a watchdog can tell "the UI is dead" apart from "a
#: dependency is unhappy" without parsing prose.
UI_STALLED_STATUS = "ui_stalled"


def public_runtime_descriptor() -> JsonDict:
    """Identify the configured UI surface without granting access or exposing secrets."""
    from config.settings import settings

    return {"app_surface": settings.app_surface, "build_profile": settings.build_profile}


async def _run_provider_off_loop(
    provider: Callable[..., Any],
    request: Request | None,
    accepts_request: bool,
) -> Any:
    """Run a synchronous health provider in a worker thread.

    Falls back to an inline call only when no thread offload is available at
    all (no anyio, no running loop) - a health probe must still answer.
    """
    import functools

    call = functools.partial(provider, request) if accepts_request else provider
    try:
        import anyio.to_thread
    except ImportError:  # pragma: no cover - anyio ships with starlette
        return call()
    try:
        return await anyio.to_thread.run_sync(call)
    except RuntimeError:  # pragma: no cover - no async worker available
        logger.debug("Health provider thread offload unavailable; running inline", exc_info=True)
        return call()


def qt_event_loop_block() -> JsonDict:
    """The Qt message loop's own liveness, for every health payload (#4650).

    Read directly here rather than injected by each caller, on purpose. The
    defect this closes was a health surface that answered for a process
    without being coupled to the part of it that had died, and an optional
    wiring parameter is a wiring a future caller can omit -- reopening exactly
    that hole. The call is safe in every process because
    :func:`services.qt_loop_liveness.snapshot` reports ``monitored: false`` for
    anything that never declared a GUI (the headless daemon, the cloud app,
    tests), so it can only go red where a real GUI stopped dispatching.
    """
    try:
        from services.qt_loop_liveness import snapshot as _qt_snapshot

        return cast(JsonDict, _qt_snapshot())
    except Exception:  # noqa: BLE001, RUF100 - a health probe must answer, never raise
        logger.debug("Qt event-loop liveness snapshot failed", exc_info=True)
        return {"status": "ok", "monitored": False, "reason": "liveness probe unavailable"}


def qt_loop_stalled(block: Mapping[str, JsonValue]) -> bool:
    """True only when a declared GUI process has a loop that stopped beating."""
    return bool(block.get("monitored")) and block.get("alive") is not True


def build_basic_health_payload(state: _StartTimeCarrier | None = None, extra: JsonDict | None = None) -> JsonDict:
    """Return a minimal health payload with optional uptime metadata."""
    payload: JsonDict = {"status": "ok"}
    if state is not None and getattr(state, "start_time", None):
        try:
            uptime = max(0.0, time.time() - float(state.start_time))
            payload["uptime_s"] = round(uptime, 3)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Unable to compute uptime for health payload", exc_info=True)
    if extra:
        payload.update(extra)
    return payload


def _wrap_status_provider(
    status_provider: HealthStatusProvider | None,
) -> Callable[[Request | None], Awaitable[JsonDict]]:
    if status_provider is None:

        async def _default(_: Request | None = None) -> JsonDict:
            return {"status": "ok"}

        return _default

    sig = inspect.signature(status_provider)
    accepts_request = bool(sig.parameters)
    is_coroutine = inspect.iscoroutinefunction(status_provider)

    async def _call(request: Request | None = None) -> JsonDict:
        try:
            result_obj: HealthPayload | JsonValue
            if is_coroutine:
                async_provider = cast(Callable[..., Awaitable[HealthPayload]], status_provider)
                result_obj = await async_provider(request) if accepts_request else await async_provider()
            else:
                # A sync provider MUST NOT run inline on the event loop. The
                # desktop details provider probes real subsystems - a PortAudio
                # device enumeration under the process-wide lock, a state-store
                # read, LLM provider selection - and any one of them blocking
                # freezes every other request in the process, not just this one.
                # The Qt client re-polls /health/details every 30s forever, so
                # inline execution is a recurring stall, not a one-time boot
                # cost. anyio's worker thread carries the caller's contextvars,
                # which the per-user scoping inside the checks depends on.
                sync_provider = status_provider
                maybe_result = await _run_provider_off_loop(sync_provider, request, accepts_request)
                if inspect.isawaitable(maybe_result):
                    result_obj = await maybe_result
                else:
                    result_obj = maybe_result
            payload_value = to_json_value(result_obj)
            if isinstance(payload_value, dict):
                return payload_value
            logger.warning(
                "Health status provider returned non-object payload (%s); coercing to minimal payload.",
                type(result_obj),
            )
            return {"status": "ok"}
        except Exception:
            logger.exception("Health status provider failed")
            return {"status": "error", "detail": "health_status_unavailable"}

    return _call


def _safe_ready(ready_check: ReadyCheck) -> bool:
    try:
        return bool(ready_check())
    except Exception:
        logger.exception("Health readiness check failed")
        return False


def register_health_routes(
    router: APIRouter,
    *,
    ready_check: ReadyCheck,
    status_provider: HealthStatusProvider | None = None,
    diag_redirect: bool = True,
    diag_path: str = "/diag/health",
    not_ready_status: str = DEFAULT_NOT_READY_STATUS,
    include_liveness: bool = True,
    include_readiness: bool = True,
    include_details: bool = True,
    details_provider: HealthStatusProvider | None = None,
) -> None:
    """Register health endpoints on the provided router."""

    _status_provider = _wrap_status_provider(status_provider)
    _details_provider = _wrap_status_provider(details_provider) if details_provider is not None else _status_provider

    async def _health_endpoint(request: Request) -> JSONResponse:
        ready = _safe_ready(ready_check)
        if not ready:
            return JSONResponse(
                status_code=503,
                content={"status": not_ready_status, **public_runtime_descriptor()},
                headers={"Retry-After": "3"},
            )

        payload = await _status_provider(request)
        # The server's configuration is authoritative, including when a status
        # provider returns unrelated metadata with the same names. The browser
        # still has to authenticate before accessing any protected capability.
        payload.update(public_runtime_descriptor())
        if "status" not in payload:
            payload["status"] = "ok"
        loop = qt_event_loop_block()
        payload["qt_event_loop"] = loop
        if qt_loop_stalled(loop):
            # This thread answering proves only that this thread is alive.
            # When the message loop that owns the window has stopped, the
            # product is down for the person using it, and saying "ok" here is
            # the exact false-green that let #4650 run unnoticed through an
            # entire filming session.
            payload["status"] = UI_STALLED_STATUS
            return JSONResponse(status_code=503, content=payload, headers={"Retry-After": "5"})
        return JSONResponse(content=payload)

    router.add_api_route(
        "/health",
        _health_endpoint,
        name="health",
        methods=["GET"],
        include_in_schema=False,
    )

    # /v1/health alias for clients that include the version prefix
    router.add_api_route(
        "/v1/health",
        _health_endpoint,
        name="v1_health",
        methods=["GET"],
        include_in_schema=False,
    )

    # /api/v1/health alias for cloud-style clients and installer smoke probes.
    router.add_api_route(
        "/api/v1/health",
        _health_endpoint,
        name="api_v1_health",
        methods=["GET"],
        include_in_schema=False,
    )

    if include_liveness:

        async def _live_endpoint() -> JSONResponse:
            # Was a hardcoded ``{"status": "live"}``. It answered "live" from a
            # frozen app for the whole of the 2026-08-01 outage, and it is what
            # ``viola_control.py health`` believes, so the runner reported a
            # dead UI as healthy too (#4650 / candidate C-701). Liveness now
            # comes from the message loop's own beat.
            loop = qt_event_loop_block()
            if qt_loop_stalled(loop):
                return JSONResponse(
                    status_code=503,
                    content={"status": UI_STALLED_STATUS, "qt_event_loop": loop},
                    headers={"Retry-After": "5"},
                )
            return JSONResponse(content={"status": "live", "qt_event_loop": loop}, status_code=200)

        router.add_api_route(
            "/health/live",
            _live_endpoint,
            name="health_live",
            methods=["GET"],
            include_in_schema=False,
        )

    if include_readiness:

        async def _ready_endpoint(request: Request) -> JSONResponse:
            ready = _safe_ready(ready_check)
            if ready:
                payload = await _status_provider(request)
                payload.setdefault("status", "ok")
                payload["ready"] = True
                loop = qt_event_loop_block()
                payload["qt_event_loop"] = loop
                if qt_loop_stalled(loop):
                    # A frozen UI is not ready to serve anybody, whatever the
                    # route table says.
                    payload["status"] = UI_STALLED_STATUS
                    payload["ready"] = False
                    return JSONResponse(status_code=503, content=payload, headers={"Retry-After": "5"})
                return JSONResponse(content=payload, status_code=200)
            return JSONResponse(
                status_code=503,
                content={"status": not_ready_status, "ready": False},
                headers={"Retry-After": "3"},
            )

        router.add_api_route(
            "/health/ready",
            _ready_endpoint,
            name="health_ready",
            methods=["GET"],
            include_in_schema=False,
        )

    async def _startup_endpoint() -> JSONResponse:
        from diagnostics.startup_telemetry import snapshot

        return JSONResponse(content=snapshot(), status_code=200)

    router.add_api_route(
        "/health/startup",
        _startup_endpoint,
        name="health_startup",
        methods=["GET"],
        include_in_schema=False,
    )
    router.add_api_route(
        "/v1/health/startup",
        _startup_endpoint,
        name="v1_health_startup",
        methods=["GET"],
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/v1/health/startup",
        _startup_endpoint,
        name="api_v1_health_startup",
        methods=["GET"],
        include_in_schema=False,
    )

    if include_details:

        async def _details_endpoint(request: Request) -> JSONResponse:
            ready = _safe_ready(ready_check)
            payload = await _details_provider(request)
            payload.setdefault("status", "ok" if ready else not_ready_status)
            payload["ready"] = ready
            return JSONResponse(content=payload, status_code=200)

        router.add_api_route(
            "/health/details",
            _details_endpoint,
            name="health_details",
            methods=["GET"],
            include_in_schema=False,
        )

        # /v1/health/details alias
        router.add_api_route(
            "/v1/health/details",
            _details_endpoint,
            name="v1_health_details",
            methods=["GET"],
            include_in_schema=False,
        )

        # /api/v1/health/details alias
        router.add_api_route(
            "/api/v1/health/details",
            _details_endpoint,
            name="api_v1_health_details",
            methods=["GET"],
            include_in_schema=False,
        )

    if not diag_redirect or not diag_path:
        return

    async def _diag_health_endpoint() -> RedirectResponse:
        return RedirectResponse(url="/health", status_code=301)

    router.add_api_route(
        diag_path,
        _diag_health_endpoint,
        name="diag_health",
        methods=["GET"],
        include_in_schema=False,
    )
