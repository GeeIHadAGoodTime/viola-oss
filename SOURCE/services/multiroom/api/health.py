"""
Multi-Room Sync Health Endpoint

Provides a FastAPI router with health check endpoints for multi-room
synchronization, including component status and metrics.

Usage:
    from services.multiroom.api.health import router

    app.include_router(router, prefix="/v1")
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from contracts.api_response import success_response
from core.constants import Status
from core.logging_config import get_logger
from fastapi import APIRouter

from ..degradation import DegradationLevel, get_degradation_manager
from ..metrics import get_component_registry, get_multiroom_metrics

logger = get_logger(__name__)


# Response Models


class ComponentStatus(BaseModel):
    """Status of a single component."""

    status: str = Field(..., description="Component status: ok or error")
    node_count: int | None = Field(default=None, description="Number of nodes (for node_registry)")
    devices_found: int | None = Field(default=None, description="Number of devices (for device_discovery)")
    last_beat_ms: float | None = Field(default=None, description="Last heartbeat time (for heartbeat)")
    active_protocols: list[str] | None = Field(default=None, description="Active protocols (for heartbeat)")
    heartbeat_status: str | None = Field(default=None, description="Heartbeat status string")
    buffer_level: float | None = Field(default=None, description="Buffer level (for audio_streaming)")
    active_schedulers: int | None = Field(default=None, description="Active schedulers (for audio_streaming)")
    underruns: int | None = Field(default=None, description="Underrun count (for audio_streaming)")
    overruns: int | None = Field(default=None, description="Overrun count (for audio_streaming)")
    streaming_status: str | None = Field(default=None, description="Streaming status string")


class HealthMetrics(BaseModel):
    """Metrics summary for health endpoint."""

    sync_latency_p50_ms: float | None = Field(default=None, description="P50 sync latency in milliseconds")
    sync_latency_p95_ms: float | None = Field(default=None, description="P95 sync latency in milliseconds")
    sync_latency_p99_ms: float | None = Field(default=None, description="P99 sync latency in milliseconds")
    connected_nodes: int = Field(..., description="Number of connected nodes")
    failovers_total: int = Field(..., description="Total number of hub failovers")
    reconnections_total: int = Field(..., description="Total number of reconnections")
    dropped_chunks_total: int = Field(..., description="Total number of dropped audio chunks")
    sync_success_rate: float = Field(..., description="Sync success rate (0.0-1.0)")


class HealthData(BaseModel):
    """Health check response data."""

    status: str = Field(..., description="Overall status: ok, degraded, or error")
    degradation_level: str = Field(..., description="Current degradation level")
    components: dict[str, Any] = Field(..., description="Component health status")
    metrics: HealthMetrics = Field(..., description="Sync metrics summary")
    available_features: list[str] = Field(..., description="List of available features at current degradation level")


class SimpleComponentStatus(BaseModel):
    """Simple component status without extra fields."""

    status: str = Field(..., description="Component status: ok or error")


class ComponentHealthData(BaseModel):
    """Component health response data."""

    components: dict[str, SimpleComponentStatus] = Field(..., description="Component health status")
    failed_count: int = Field(..., description="Number of failed components")
    total_count: int = Field(..., description="Total number of components")


class HealthSuccessResponse(BaseModel):
    """Success response for health endpoint."""

    ok: Literal[True] = True
    error: None = None
    data: HealthData


class ComponentHealthSuccessResponse(BaseModel):
    """Success response for component health endpoint."""

    ok: Literal[True] = True
    error: None = None
    data: ComponentHealthData


class MetricsSuccessResponse(BaseModel):
    """Success response for metrics endpoint."""

    ok: Literal[True] = True
    error: None = None
    data: dict[str, Any] = Field(..., description="Full metrics dictionary")


router = APIRouter(prefix="/multiroom", tags=["multiroom"])


def _get_component_status(component: str, failed_components: set[str]) -> dict[str, Any]:
    """
    Generate status dict for a component.

    Args:
        component: Component name
        failed_components: Set of currently failed component names

    Returns:
        Status dictionary for the component
    """
    is_ok = component not in failed_components
    return {"status": Status.OK if is_ok else Status.ERROR}


def _level_to_status(level: DegradationLevel) -> str:
    """
    Map degradation level to health status string.

    Args:
        level: Current degradation level

    Returns:
        Status string: "ok", "degraded", or "error"
    """
    if level == DegradationLevel.FULL:
        return Status.OK
    if level == DegradationLevel.SINGLE_DEVICE:
        return Status.ERROR
    return Status.DEGRADED


@router.get(
    "/health",
    response_model=HealthSuccessResponse,
    responses={
        200: {
            "model": HealthSuccessResponse,
            "description": "Multi-room health status",
        },
    },
)
async def get_multiroom_health() -> HealthSuccessResponse:
    """
    Get multi-room sync health status.

    Returns comprehensive health information including:
    - Overall status (ok/degraded/error)
    - Current degradation level
    - Component health status
    - Sync metrics (latency percentiles, connected nodes, etc.)

    Returns:
        ResponseEnvelope with health data
    """
    degradation = get_degradation_manager()
    metrics = get_multiroom_metrics()
    registry = get_component_registry()

    level = degradation.current_level
    failed_components = degradation.failed_components
    percentiles = metrics.get_latency_percentiles()

    # Build component status
    components: dict[str, dict[str, Any]] = {}

    # Node registry status
    node_status = _get_component_status("node_registry", failed_components)
    node_status["node_count"] = metrics.connected_nodes
    components["node_registry"] = node_status

    # Device discovery status
    discovery_status = _get_component_status("device_discovery", failed_components)
    discovery_status["devices_found"] = metrics.connected_nodes
    components["device_discovery"] = discovery_status

    # Heartbeat status - get actual metrics from component registry
    heartbeat_status = _get_component_status("heartbeat", failed_components)
    heartbeat_info = registry.get_heartbeat_status()
    heartbeat_status["last_beat_ms"] = heartbeat_info["last_beat_ms"]
    heartbeat_status["active_protocols"] = heartbeat_info["active_protocols"]
    heartbeat_status["heartbeat_status"] = heartbeat_info["status"]
    components["heartbeat"] = heartbeat_status

    # Audio streaming status - get actual metrics from component registry
    streaming_status = _get_component_status("audio_streaming", failed_components)
    streaming_info = registry.get_streaming_status()
    streaming_status["buffer_level"] = streaming_info["buffer_level_ms"]
    streaming_status["active_schedulers"] = streaming_info["active_schedulers"]
    streaming_status["underruns"] = streaming_info["underruns"]
    streaming_status["overruns"] = streaming_info["overruns"]
    streaming_status["streaming_status"] = streaming_info["status"]
    components["audio_streaming"] = streaming_status

    # Cloud relay status
    cloud_status = _get_component_status("cloud_relay", failed_components)
    components["cloud_relay"] = cloud_status

    # Build metrics summary
    metrics_summary = {
        "sync_latency_p50_ms": percentiles.get("p50"),
        "sync_latency_p95_ms": percentiles.get("p95"),
        "sync_latency_p99_ms": percentiles.get("p99"),
        "connected_nodes": metrics.connected_nodes,
        "failovers_total": metrics.hub_failovers,
        "reconnections_total": metrics.reconnections,
        "dropped_chunks_total": metrics.dropped_chunks,
        "sync_success_rate": round(metrics.get_success_rate(), 4),
    }

    health_data: dict[str, Any] = {
        "status": _level_to_status(level),
        "degradation_level": level.value,
        "components": components,
        "metrics": metrics_summary,
        "available_features": list(degradation.get_available_features(level)),
    }

    logger.debug(
        "Health check: status=%s, level=%s, nodes=%d",
        health_data["status"],
        level.value,
        metrics.connected_nodes,
    )

    return success_response(health_data)


@router.get(
    "/health/components",
    response_model=ComponentHealthSuccessResponse,
    responses={
        200: {
            "model": ComponentHealthSuccessResponse,
            "description": "Component health status",
        },
    },
)
async def get_component_health() -> ComponentHealthSuccessResponse:
    """
    Get detailed component health status.

    Returns individual component status without aggregated metrics.

    Returns:
        ResponseEnvelope with component health data
    """
    degradation = get_degradation_manager()
    failed_components = degradation.failed_components

    components = {
        "node_registry": _get_component_status("node_registry", failed_components),
        "device_discovery": _get_component_status("device_discovery", failed_components),
        "heartbeat": _get_component_status("heartbeat", failed_components),
        "audio_streaming": _get_component_status("audio_streaming", failed_components),
        "cloud_relay": _get_component_status("cloud_relay", failed_components),
    }

    return success_response(
        {
            "components": components,
            "failed_count": len(failed_components),
            "total_count": len(components),
        }
    )


@router.get(
    "/health/metrics",
    response_model=MetricsSuccessResponse,
    responses={
        200: {"model": MetricsSuccessResponse, "description": "Full sync metrics"},
    },
)
async def get_sync_metrics() -> MetricsSuccessResponse:
    """
    Get detailed sync metrics.

    Returns all collected metrics for monitoring and debugging.

    Returns:
        ResponseEnvelope with full metrics data
    """
    metrics = get_multiroom_metrics()
    return success_response(metrics.to_dict())


__all__ = [
    "ComponentHealthData",
    "ComponentHealthSuccessResponse",
    "ComponentStatus",
    "HealthData",
    "HealthMetrics",
    "HealthSuccessResponse",
    "MetricsSuccessResponse",
    "SimpleComponentStatus",
    "router",
]
