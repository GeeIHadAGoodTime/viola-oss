"""
Monitoring and health check API routes
Exposes metrics and health status via HTTP endpoints.

Security:
    - Read-only endpoints (metrics, health, stats) are accessible from loopback only.
    - Destructive endpoints (reset) require loopback access.
    - If /monitoring should also be covered by API-key auth, add "/monitoring/"
      to SecurityConfig.auth_required_endpoints in ui/security/config.py.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from core.constants import LOCALHOST, LOCALHOST_NAME
from core.logging_config import get_logger
from diagnostics.runtime_metrics import get_runtime_metrics
from fastapi import APIRouter, HTTPException, Request, Response, status as http_status
from routing.health import register_health_routes
from ui.security.config import get_security_config
from utils.monitoring import get_health_checker, get_metrics_collector

logger = get_logger(__name__)

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _require_loopback(request: Request) -> None:
    """Reject requests that do not originate from the loopback interface.

    Monitoring endpoints expose internal metrics and operational controls.
    Restricting them to loopback prevents external network access while
    keeping them available for local dashboards and admin tools.

    Raises:
        HTTPException 403 if the request does not come from localhost.
    """
    client_host = request.client.host if request.client else None
    # Accept common loopback representations (IPv4, IPv6, hostname)
    loopback_addrs = {LOCALHOST, LOCALHOST_NAME, "::1", "0.0.0.0"}  # nosec B104
    if client_host not in loopback_addrs:
        logger.warning(
            "Blocked non-loopback access to monitoring endpoint %s from %s",
            request.url.path,
            client_host,
        )
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Monitoring endpoints are only accessible from localhost",
        )


async def _require_loopback_operator(request: Request) -> None:
    """Require localhost plus an operator credential for destructive actions."""
    _require_loopback(request)
    config = get_security_config()
    api_key = request.headers.get("X-API-Key")
    if api_key and config.auth_api_key and hmac.compare_digest(api_key, config.auth_api_key):
        return
    if getattr(request.state, "user", None) is not None:
        return
    raise HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="Operator authentication required",
        headers={"WWW-Authenticate": "ApiKey"},
    )


@router.get("/metrics")
async def get_metrics(request: Request, format: str = "json"):
    """
    Get application metrics (loopback only).

    Query params:
        format: 'json' or 'prometheus' (default: json)

    Returns metrics in requested format
    """
    _require_loopback(request)
    runtime_metrics = get_runtime_metrics()
    collector = get_metrics_collector()

    if format == "prometheus":
        payload, content_type = runtime_metrics.generate_prometheus()
        return Response(content=payload, media_type=content_type)

    snapshot = runtime_metrics.snapshot()
    collector_metrics = collector.get_all_metrics()
    if collector_metrics:
        snapshot["collector_metrics"] = {name: metric.to_dict() for name, metric in collector_metrics.items()}
    return snapshot


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """
    Kubernetes liveness probe endpoint

    Returns:
        Simple status response for liveness checks
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz():
    """
    Kubernetes readiness probe endpoint

    Returns:
        Readiness status with detailed check results
        503 status if not ready
    """
    health = get_health_checker()
    checks = health.check_all()

    # Check if overall status is healthy
    all_healthy = checks.get("status") == "healthy"

    if all_healthy:
        return {"status": "ready", "checks": checks}

    # Return 503 if not ready
    return Response(
        content=json.dumps({"status": "not_ready", "checks": checks}),
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        media_type="application/json",
    )


@router.get("/health/{check_name}")
async def specific_health_check(request: Request, check_name: str) -> dict[str, Any]:
    """
    Check specific health check (loopback only).

    Args:
        check_name: Name of health check to run

    Returns:
        Health check result
    """
    _require_loopback(request)
    health = get_health_checker()
    return health.check_one(check_name)


@router.get("/stats")
async def get_stats(request: Request) -> dict[str, Any]:
    """
    Get application statistics (loopback only).

    Returns:
        Summary statistics about the application
    """
    _require_loopback(request)
    collector = get_metrics_collector()
    metrics = collector.get_all_metrics()

    # Calculate summary stats
    stats: dict[str, Any] = {
        "total_metrics": len(metrics),
        "metrics_by_type": {},
        "top_metrics": [],
    }

    # Group by type
    for metric in metrics.values():
        metric_type = metric.type.value
        metrics_by_type = stats["metrics_by_type"]
        assert isinstance(metrics_by_type, dict)
        metrics_by_type[metric_type] = metrics_by_type.get(metric_type, 0) + 1

    # Top metrics by value
    sorted_metrics = sorted(metrics.values(), key=lambda m: m.value, reverse=True)[:10]

    stats["top_metrics"] = [{"name": m.name, "value": m.value, "type": m.type.value} for m in sorted_metrics]

    return stats


@router.post("/metrics/reset")
async def reset_metrics(request: Request) -> dict[str, str]:
    """
    Reset all metrics (loopback plus operator credential, destructive operation).

    Returns:
        Success message
    """
    await _require_loopback_operator(request)
    collector = get_metrics_collector()
    collector.reset()
    logger.info(
        "Metrics reset by loopback request from %s",
        request.client.host if request.client else "unknown",
    )

    return {"status": "success", "message": "All metrics reset"}


def _monitoring_health_ready() -> bool:
    """
    Determine readiness for monitoring health endpoint.
    Returns True only when all health checks report healthy.
    """
    try:
        checks = get_health_checker().check_all()
        return checks.get("status") == "healthy"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Monitoring readiness check failed: %s", exc)
        return False


async def _monitoring_health_payload(_: Request | None = None) -> dict[str, Any]:
    """Return monitoring health payload (same as canonical health schema)."""
    health = get_health_checker()
    return health.check_all()


# Register canonical health routes for monitoring namespace
register_health_routes(
    router,
    ready_check=_monitoring_health_ready,
    status_provider=_monitoring_health_payload,
    diag_redirect=False,
    not_ready_status="unhealthy",
)


# ---------------------------------------------------------------------------
# Default health checks
#
# These gate ``/monitoring/health`` and ``/monitoring/health/ready`` through
# ``_monitoring_health_ready`` above, so a check that cannot fail turns the
# readiness endpoint into a constant 200. Each check below must therefore have
# a reachable false: it exercises the real dependency rather than asserting
# that a freshly constructed object is not None.
# ---------------------------------------------------------------------------


def _check_media_backend() -> bool:
    """True when the native libVLC library this install plays audio through loads.

    Scope, stated plainly: this is a READINESS check for on-demand work, not a
    liveness check. Playback is something the user asks for, so silence is the
    healthy resting state and there is no work signal to watch -- the honest
    question is "could this process play audio if asked right now?", which is
    answered by whether the binding can resolve the native library.

    It deliberately does NOT construct a ``vlc.Instance()``. Doing so measured
    the wrong thing (a brand-new throwaway instance says nothing about the
    player the app is actually using) and spun up a fresh libVLC instance,
    with its threads and file handles, on every poll of a monitoring endpoint.

    A missing or unloadable libVLC is a real, shipped Windows failure mode
    (the native DLL not landing next to the frozen binary), which is exactly
    what this now catches.
    """
    try:
        import vlc

        # find_lib() is what the binding itself calls to locate libvlc; it
        # returns a falsy handle when the native library is absent.
        find_lib = getattr(vlc, "find_lib", None)
        if callable(find_lib):
            result = find_lib()
            dll = result[0] if isinstance(result, tuple) else result
            return dll is not None
        # Older/newer bindings without find_lib: the module-level handle the
        # binding builds at import time serves the same purpose.
        return getattr(vlc, "dll", None) is not None
    except Exception as e:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
        logger.debug("Media backend health check failed: %s", e)
        return False


def _check_settings() -> bool:
    """True when user settings can actually be read, not merely constructed.

    The previous form built a ``SettingsFacade`` and asserted it was not None,
    which is true of any successful constructor call and so could never fail.
    This instead requires that the underlying SettingsManager is reachable and
    that a real read returns a value, which is what breaks when settings.json
    is corrupt, locked, or unreadable.
    """
    try:
        from config.facade import SettingsFacade

        facade = SettingsFacade()
        manager = facade.user_settings
        if manager is None:
            logger.debug("Settings health check: settings manager unavailable")
            return False
        # Exercise the read path itself; a manager that raises or hands back
        # nothing at all is not a working settings store.
        return facade.get("default_volume") is not None
    except Exception as e:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
        logger.debug("Settings health check failed: %s", e)
        return False


def _check_filesystem() -> bool:
    """True when Viola's real data directory is present AND writable.

    ``os.path.exists(".")`` -- the previous check -- is true for any process
    that has a current directory, which is every process, and it named a path
    the application does not store anything in. The data directory is the one
    the app actually depends on, and a full disk, a revoked ACL, or a
    disconnected volume are the ways it genuinely fails, so the check writes
    and reads back a probe file rather than asking whether a path exists.
    """
    try:
        from core.platform import get_data_dir

        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".health_probe"
        payload = str(time.time_ns())
        probe.write_text(payload, encoding="utf-8")
        try:
            return probe.read_text(encoding="utf-8") == payload
        finally:
            probe.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001, RUF100 - probe reports unhealthy, never raises
        logger.debug("Filesystem health check failed: %s", e)
        return False


def register_default_health_checks() -> None:
    """Register the default health checks on the global health checker."""
    health = get_health_checker()
    health.register_check("media_backend", _check_media_backend)
    health.register_check("settings", _check_settings)
    health.register_check("filesystem", _check_filesystem)


# Auto-register on import
register_default_health_checks()
