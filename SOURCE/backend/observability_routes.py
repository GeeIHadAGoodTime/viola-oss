"""
Observability routes for FastAPI app.

Health and monitoring endpoints return raw JSON for external probes.
Versioned diagnostics endpoints return canonical ResponseEnvelope payloads.

Exposes:
  - GET /health, /health/details — human/test-friendly health
  - GET /monitoring/healthz — Kubernetes-style liveness probe
  - GET /monitoring/readyz — Kubernetes-style readiness probe
  - GET /metrics — Prometheus metrics
  - GET/POST /v1/diagnostics/* — local error-pattern and self-heal endpoints
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any

from fastapi.responses import PlainTextResponse

from contracts.api_response import failure_response, success_response
from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

logger = get_logger(__name__)


async def _require_localhost(request: Request) -> None:
    """Restrict dangerous diagnostic endpoints to localhost only (H9 fix)."""
    client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1", "testclient"):
        logger.warning("Blocked non-localhost diagnostics request from %s", client_ip)
        raise HTTPException(status_code=403, detail="Forbidden")


# Captured once at module import so the same Unix epoch is reported on every
# scrape even if VIOLA_DEPLOYED_AT_SECONDS is not set by the deploy script.
# Without this fallback the gauge collapses to 0 and the Prometheus rule
# (time() - 0) > threshold fires immediately, defeating the staleness signal.
_PROCESS_START_TIME_SECONDS = int(time.time())


def build_deploy_sha_info_line() -> str:
    """Emit viola_deployed_sha_info + viola_deployed_at_timestamp_seconds (Condition 4.12).

    Used by register_observability_routes' desktop /metrics handler.
    scripts/launch/deploy_cloud.ps1 sets VIOLA_DEPLOYED_SHA, VIOLA_IMAGE_TAG,
    and VIOLA_DEPLOYED_AT_SECONDS at swap time. The two gauges together let
    the ViolaDeployStaleness alert fire purely from inside the container —
    no pushgateway / no main-SHA producer required. Emitted unconditionally;
    SHA collapses to ``unknown`` and timestamp falls back to the container's
    process start time when the env vars are absent so the gap is itself
    observable. The cloud surface emits the same gauges via
    ``admin/cloud_metrics.create_cloud_prometheus_metrics_router``
    (anvil/cloud-metrics-deployed-sha-gauge); both paths must stay in sync —
    see ``scripts/check_cloud_metrics_emits_deployed_sha.py`` (SHA pair)
    and ``scripts/check_deploy_staleness_uses_deployed_at_timestamp.py``
    (the F3 timestamp pair + alert-rule contract).
    """
    import os as _os

    sha = (_os.environ.get("VIOLA_DEPLOYED_SHA") or "unknown").strip() or "unknown"
    image_tag = (_os.environ.get("VIOLA_IMAGE_TAG") or "unknown").strip() or "unknown"
    deployed_at_raw = (_os.environ.get("VIOLA_DEPLOYED_AT_SECONDS") or "").strip()
    try:
        deployed_at = int(float(deployed_at_raw)) if deployed_at_raw else _PROCESS_START_TIME_SECONDS
    except ValueError:
        deployed_at = _PROCESS_START_TIME_SECONDS
    return (
        "# HELP viola_deployed_sha_info Image SHA the container was built from (Condition 4.12 drift detector).\n"
        "# TYPE viola_deployed_sha_info gauge\n"
        'viola_deployed_sha_info{sha="%s",image_tag="%s"} 1\n' % (sha, image_tag)
        + "# HELP viola_deployed_at_timestamp_seconds Unix epoch when the container was deployed (set by scripts/launch/deploy_cloud.ps1; falls back to process start).\n"
        + "# TYPE viola_deployed_at_timestamp_seconds gauge\n"
        + "viola_deployed_at_timestamp_seconds %d\n" % deployed_at
    )


def register_observability_routes(app: FastAPI, *, state: Any) -> None:
    """
    Register observability routes for monitoring and diagnostics.

    Health endpoints return raw JSON (not wrapped in ResponseEnvelope).
    Tests expect: {"status": "ok"} at top level, NOT {"ok": true, "data": {...}}
    """
    router = APIRouter()

    @router.get("/health")
    async def _health() -> SafeJSONResponse:
        """Basic health check endpoint - returns raw JSON."""
        # Check readiness from state
        ready = True
        ready_attr = getattr(state, "is_ready", None)
        if callable(ready_attr):
            try:
                ready = ready_attr()
            except Exception as e:
                logger.exception("Failed to call is_ready(): %s", e)
                ready = True  # Default to ready on error

        if not ready:
            return SafeJSONResponse(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                content={"status": "starting"},
                headers={"Retry-After": "3"},
            )

        # Build payload with uptime if available
        payload: dict[str, Any] = {"status": "ok"}
        from diagnostics.voice_status import get_voice_status

        payload["voice_status"] = get_voice_status()
        start_time = getattr(state, "start_time", None)
        if start_time is not None:
            try:
                uptime = max(0.0, time.time() - float(start_time))
                payload["uptime_s"] = round(uptime, 3)
            except Exception as e:
                logger.exception("Operation failed: %s", e)
                pass

        return SafeJSONResponse(content=payload)

    @router.get("/health/details")
    async def _health_details() -> SafeJSONResponse:
        """Detailed health check with ready flag - returns raw JSON."""
        # Check readiness from state
        ready = True
        ready_attr = getattr(state, "is_ready", None)
        if callable(ready_attr):
            try:
                ready = ready_attr()
            except Exception as e:
                logger.exception("Failed to call is_ready(): %s", e)
                ready = True  # Default to ready on error

        # Build detailed payload
        payload: dict[str, Any] = {
            "status": "ok" if ready else "starting",
            "ready": ready,
            "timestamp": time.time(),
        }
        from diagnostics.voice_status import get_voice_status

        payload["voice_status"] = get_voice_status()

        # Add uptime if available
        start_time = getattr(state, "start_time", None)
        if start_time is not None:
            try:
                uptime = max(0.0, time.time() - float(start_time))
                payload["uptime_s"] = round(uptime, 3)
            except Exception as e:
                logger.exception("Operation failed: %s", e)
                pass

        return SafeJSONResponse(content=payload)

    @router.get("/monitoring/healthz")
    async def _liveness_probe() -> SafeJSONResponse:
        """
        Kubernetes-style liveness probe.

        Returns 200 as long as the process is alive and the event loop is
        responsive. Intentionally does NOT check readiness — a failing
        liveness probe should trigger a pod restart, while readiness
        failures should only route traffic away.
        """
        return SafeJSONResponse(content={"status": "ok"})

    @router.get("/monitoring/readyz")
    async def _readiness_probe() -> SafeJSONResponse:
        """
        Kubernetes-style readiness probe.

        Returns 200 when the app reports ready via ``state.is_ready()``.
        Returns 503 with ``Retry-After`` when still warming up so load
        balancers route traffic elsewhere. If ``is_ready`` is missing or
        raises, defaults to ready to preserve existing behavior.
        """
        ready = True
        ready_attr = getattr(state, "is_ready", None)
        if callable(ready_attr):
            try:
                ready = bool(ready_attr())
            except Exception as e:
                logger.exception("Failed to call is_ready() on readyz: %s", e)
                ready = True  # Default to ready on error (match /health)

        payload: dict[str, Any] = {
            "status": "ok" if ready else "starting",
            "ready": ready,
        }

        start_time = getattr(state, "start_time", None)
        if start_time is not None:
            try:
                uptime = max(0.0, time.time() - float(start_time))
                payload["uptime_s"] = round(uptime, 3)
            except Exception as e:
                logger.exception("Operation failed: %s", e)

        if not ready:
            return SafeJSONResponse(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                content=payload,
                headers={"Retry-After": "3"},
            )

        return SafeJSONResponse(content=payload)

    @router.get("/metrics", dependencies=[Depends(_require_localhost)])
    async def _metrics() -> PlainTextResponse:
        """Prometheus-style metrics endpoint."""
        try:
            runtime_metrics = getattr(app.state, "runtime_metrics", None)
            if runtime_metrics and hasattr(runtime_metrics, "generate_prometheus"):
                payload, content_type = runtime_metrics.generate_prometheus()
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                payload = payload + build_deploy_sha_info_line()
                return PlainTextResponse(
                    content=payload,
                    media_type=content_type,
                )

            return PlainTextResponse(
                content="# Viola Runtime Metrics\nviola_up 1\n" + build_deploy_sha_info_line(),
                media_type="text/plain; charset=utf-8",
            )
        except Exception as e:
            logger.error("Metrics endpoint failed: %s", e)
            return PlainTextResponse(
                content=f"# Error getting metrics: {e}\nviola_up 0\n" + build_deploy_sha_info_line(),
                media_type="text/plain; charset=utf-8",
            )

    @router.get("/v1/diagnostics/error-patterns", dependencies=[Depends(_require_localhost)])
    async def _error_patterns() -> SafeJSONResponse:
        """
        Get error patterns detected by the error registry.

        Returns aggregated error patterns with:
        - Pattern counts and frequencies
        - Category breakdown (expected vs unexpected)
        - Component breakdown
        - Actionable recommendations for each pattern

        Use this endpoint for focused error analysis without the full
        diagnostic payload from /v1/diagnostics/ai-debug.
        """
        try:
            from diagnostics.error_registry import get_error_registry

            registry = get_error_registry()
            summary = registry.get_summary()
            return SafeJSONResponse(
                content={
                    "ok": True,
                    "error": None,
                    "data": summary,
                }
            )
        except Exception:
            logger.exception("Error patterns endpoint failed")
            return SafeJSONResponse(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                content={
                    "ok": False,
                    "error": "Error pattern retrieval failed",
                    "data": None,
                },
            )

    @router.post("/v1/diagnostics/self-heal", dependencies=[Depends(_require_localhost)])
    async def _self_heal() -> SafeJSONResponse:
        """
        Trigger self-healing routines:
        - Clear all Python bytecode caches
        - Invalidate problematic modules
        - Report status

        This endpoint allows the AI debugger to automatically fix
        stale cache issues without manual intervention.
        """
        try:
            from bootstrap.cache_cleaner import startup_cache_clean

            result = startup_cache_clean(force=True)
            return SafeJSONResponse(
                content=success_response(
                    {
                        "status": "completed",
                        "action": "cache_cleared",
                        "details": result,
                        "recommendation": "Retry the failing operation",
                    }
                )
            )
        except Exception:
            logger.exception("Self-heal endpoint failed")
            return SafeJSONResponse(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                content=failure_response(
                    "self_heal_failed",
                    "Self-heal operation failed",
                    data={
                        "status": "failed",
                        "error": "Self-heal operation failed",
                        "recommendation": "Manual restart may be required",
                    },
                ),
            )

    @router.post("/v1/diagnostics/invalidate-module", dependencies=[Depends(_require_localhost)])
    async def _invalidate_module(module_name: str) -> SafeJSONResponse:
        """
        Invalidate a specific module's cache and force reimport.

        Use this to fix stale bytecode for a specific module without
        clearing all caches.
        """
        try:
            from bootstrap.cache_cleaner import invalidate_and_reimport

            module = invalidate_and_reimport(module_name)
            success = module is not None
            return SafeJSONResponse(
                content=success_response(
                    {
                        "status": "success" if success else "failed",
                        "module": module_name,
                        "reimported": success,
                    }
                )
            )
        except Exception:
            logger.exception("Module invalidation failed for %s", module_name)
            return SafeJSONResponse(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                content=failure_response(
                    "module_invalidation_failed",
                    "Module invalidation failed",
                    data={
                        "status": "error",
                        "module": module_name,
                        "error": "Module invalidation failed",
                    },
                ),
            )

    app.include_router(router, prefix="", tags=["observability"])
