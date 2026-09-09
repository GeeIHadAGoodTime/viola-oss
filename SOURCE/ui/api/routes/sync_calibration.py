"""
Sync Calibration HTTP Endpoints for Multi-Room Audio.

Provides endpoints for managing device latency calibration settings
used in multi-room audio synchronization.

Endpoints:
    GET  /api/v1/sync/latency       - Get current latency for device
    POST /api/v1/sync/latency       - Set manual latency override
    POST /api/v1/sync/latency/reset - Reset to default latency
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends
from ui.api.routes.auth_dependencies import require_auth

logger = get_logger(__name__)


# Request/Response Models


class LatencyRequest(BaseModel):
    """Request model for setting latency."""

    device_id: str = Field(..., description="Device identifier (fingerprint)")
    latency_ms: float = Field(
        ...,
        ge=0.0,
        le=500.0,
        description="Latency in milliseconds (0-500ms)",
    )


class LatencyResetRequest(BaseModel):
    """Request model for resetting latency."""

    device_id: str | None = Field(
        None,
        description="Device identifier (fingerprint). Defaults to local device if omitted.",
    )


class LatencyData(BaseModel):
    """Response data for latency queries."""

    device_id: str
    latency_ms: float
    is_override: bool = Field(
        default=False,
        description="True if this is a manual override, False if from calibration",
    )
    source: Literal["override", "calibration", "default"] = Field(
        default="calibration",
        description="Source of latency value: 'override', 'calibration', or 'default'",
    )


class ErrorData(BaseModel):
    """Error detail structure."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class LatencySuccessResponse(BaseModel):
    """Success response envelope for latency endpoints."""

    ok: Literal[True] = True
    error: None = None
    data: LatencyData


class LatencyErrorResponse(BaseModel):
    """Error response envelope for latency endpoints."""

    ok: Literal[False] = False
    error: ErrorData
    data: None = None


# Keep backward compat alias
LatencyResponse = LatencyData


# Latency validation constants
LATENCY_MIN_MS = 0.0
LATENCY_MAX_MS = 500.0


def _latency_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=failure_response(code, message, details=details),
    )


def _get_latency_provider() -> Any:
    """Get the latency provider instance, creating if needed."""
    try:
        from audio_core.latency_provider import DeviceProfileLatencyProvider

        # Get or create singleton
        if not hasattr(_get_latency_provider, "_instance"):
            _get_latency_provider._instance = DeviceProfileLatencyProvider()
        return _get_latency_provider._instance
    except Exception as e:
        logger.warning("Failed to get latency provider: %s", e)
        return None


def _get_current_device_id() -> str | None:
    """Get the current device fingerprint."""
    try:
        from voice.wake_detector.device_profile_manager import get_device_fingerprint

        return get_device_fingerprint()
    except Exception as e:
        logger.debug("Failed to get device fingerprint: %s", e)
        return None


def create_sync_calibration_router() -> APIRouter:
    """Create the sync calibration router."""
    router = APIRouter(prefix="/api/v1/sync", tags=["sync"])

    @router.get(
        "/latency",
        response_model=LatencySuccessResponse | LatencyErrorResponse,
        dependencies=[Depends(require_auth)],
        responses={
            200: {
                "model": LatencySuccessResponse,
                "description": "Latency data retrieved successfully",
            },
            400: {
                "model": LatencyErrorResponse,
                "description": "Device not found or provider unavailable",
            },
            500: {"model": LatencyErrorResponse, "description": "Internal error"},
        },
    )
    async def get_latency(
        device_id: str | None = None,
    ) -> LatencySuccessResponse | LatencyErrorResponse:
        """
        Get current latency setting for a device.

        Args:
            device_id: Device identifier (optional, uses current device if not specified)

        Returns:
            ResponseEnvelope with latency data
        """
        # Use current device if not specified
        if device_id is None:
            device_id = _get_current_device_id()
            if device_id is None:
                return _latency_error_response(
                    status_code=400,
                    code="device_id_required",
                    message="A device ID is required for this request.",
                )

        provider = _get_latency_provider()
        if provider is None:
            return _latency_error_response(
                status_code=500,
                code="provider_unavailable",
                message="Latency calibration is temporarily unavailable.",
            )

        try:
            latency_ms = provider.get_latency_ms(device_id)

            # Determine source of latency value
            is_override = device_id in getattr(provider, "_overrides", {})
            if is_override:
                source = "override"
            elif device_id in getattr(provider, "_cache", {}):
                source = "calibration"
            else:
                source = "default"

            logger.debug(
                "Get latency for device %s: %s ms (source=%s)",
                device_id,
                format(latency_ms, ".1f"),
                source,
            )

            return success_response(
                {
                    "device_id": device_id,
                    "latency_ms": latency_ms,
                    "is_override": is_override,
                    "source": source,
                }
            )

        except Exception:
            logger.exception("Failed to get latency for device %s", device_id)
            return _latency_error_response(
                status_code=500,
                code="latency_error",
                message="Try again in a moment",
            )

    @router.post(
        "/latency",
        response_model=LatencySuccessResponse | LatencyErrorResponse,
        dependencies=[Depends(require_auth)],
        responses={
            200: {
                "model": LatencySuccessResponse,
                "description": "Latency set successfully",
            },
            400: {
                "model": LatencyErrorResponse,
                "description": "Invalid latency value or provider unavailable",
            },
            500: {"model": LatencyErrorResponse, "description": "Internal error"},
        },
    )
    async def set_latency(
        request: LatencyRequest,
    ) -> LatencySuccessResponse | LatencyErrorResponse:
        """
        Set manual latency override for a device.

        Args:
            request: LatencyRequest with device_id and latency_ms

        Returns:
            ResponseEnvelope with updated latency data
        """
        provider = _get_latency_provider()
        if provider is None:
            return _latency_error_response(
                status_code=500,
                code="provider_unavailable",
                message="Latency calibration is temporarily unavailable.",
            )

        # Validate latency range
        if not LATENCY_MIN_MS <= request.latency_ms <= LATENCY_MAX_MS:
            return _latency_error_response(
                status_code=400,
                code="invalid_latency",
                message=(f"Latency must be between {LATENCY_MIN_MS} and {LATENCY_MAX_MS} ms."),
                details={"min": LATENCY_MIN_MS, "max": LATENCY_MAX_MS},
            )

        try:
            provider.set_latency_ms(request.device_id, request.latency_ms)

            logger.info(
                "Set latency for device %s: %s ms",
                request.device_id,
                format(request.latency_ms, ".1f"),
            )

            return success_response(
                {
                    "device_id": request.device_id,
                    "latency_ms": request.latency_ms,
                    "is_override": True,
                    "source": "override",
                }
            )

        except ValueError as e:
            logger.warning("Invalid latency value for device %s: %s", request.device_id, e)
            return _latency_error_response(
                status_code=400,
                code="invalid_latency",
                message="Invalid latency value.",
            )
        except Exception:
            logger.exception("Failed to set latency for device %s", request.device_id)
            return _latency_error_response(
                status_code=500,
                code="latency_error",
                message="Try again in a moment",
            )

    @router.post(
        "/latency/reset",
        response_model=LatencySuccessResponse | LatencyErrorResponse,
        dependencies=[Depends(require_auth)],
        responses={
            200: {
                "model": LatencySuccessResponse,
                "description": "Latency reset successfully",
            },
            400: {"model": LatencyErrorResponse, "description": "Provider unavailable"},
            500: {"model": LatencyErrorResponse, "description": "Internal error"},
        },
    )
    async def reset_latency(
        request: LatencyResetRequest | None = None,
    ) -> LatencySuccessResponse | LatencyErrorResponse:
        """
        Reset device latency to default (remove manual override).

        Args:
            request: Optional LatencyResetRequest with device_id.
                     Defaults to local device if omitted or body is empty.

        Returns:
            ResponseEnvelope with new latency data
        """
        # Resolve device_id: from body, or fall back to local device
        device_id = getattr(request, "device_id", None) if request else None
        if not device_id:
            device_id = _get_current_device_id()
            if not device_id:
                return _latency_error_response(
                    status_code=400,
                    code="device_id_required",
                    message="A device ID is required for this request.",
                )

        provider = _get_latency_provider()
        if provider is None:
            return _latency_error_response(
                status_code=500,
                code="provider_unavailable",
                message="Latency calibration is temporarily unavailable.",
            )

        try:
            provider.reset_to_default(device_id)

            # Get the new default latency
            latency_ms = provider.get_latency_ms(device_id)

            logger.info(
                "Reset latency for device %s to default: %s ms",
                device_id,
                format(latency_ms, ".1f"),
            )

            return success_response(
                {
                    "device_id": device_id,
                    "latency_ms": latency_ms,
                    "is_override": False,
                    "source": "default",
                }
            )

        except Exception:
            logger.exception("Failed to reset latency for device %s", device_id)
            return _latency_error_response(
                status_code=500,
                code="latency_error",
                message="Try again in a moment",
            )

    @router.get("/latency/auto-detect", dependencies=[Depends(require_auth)])
    async def auto_detect_latency() -> dict[str, Any]:
        """
        Run Tier A auto-detection of output device latency.

        Queries the OS audio subsystem for the default output device latency
        and applies Bluetooth codec estimates if applicable.

        Returns:
            ResponseEnvelope with auto-detected latency info
        """
        try:
            from audio_core.calibration.device_latency import detect_output_latency

            result = detect_output_latency()
            return success_response(result)
        except Exception:
            logger.exception("Auto-detection failed")
            return _latency_error_response(
                status_code=500,
                code="auto_detect_error",
                message="Latency auto-detection is temporarily unavailable.",
            )

    return router


__all__ = [
    "ErrorData",
    "LatencyData",
    "LatencyErrorResponse",
    "LatencyRequest",
    "LatencyResetRequest",
    "LatencyResponse",
    "LatencySuccessResponse",
    "create_sync_calibration_router",
]
