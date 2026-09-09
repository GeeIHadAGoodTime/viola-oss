from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import ParamSpec, TypeVar
from urllib.parse import quote

from contracts.fastapi_helpers import SafeJSONResponse
from core.constants import TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from fastapi import Depends, Query, Request
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from utils.text_encoding import repair_mojibake_deep
from utils.weather_location import (
    WEATHER_LOCATION_REQUIRED_MESSAGE,
    normalize_weather_request_location,
    resolve_weather_request_location,
)
from utils.weather_normalization import normalize_weather_data
from utils.weather_payload import build_weather_payload

log = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


async def _resolve_cloud_user_location(request: Request) -> str | None:
    """Resolve the signed-in user's OWN saved weather location on cloud.

    ``resolve_weather_request_location``'s no-city fallback reads the
    process-global ``SettingsManager`` — safe on desktop (one user per
    install) but tenant-unsafe on a shared cloud server (see
    ``utils.weather_location.get_configured_weather_location``, which
    already returns ``None`` on cloud). This is the cloud-safe replacement:
    it resolves the AUTHENTICATED caller's own Tier-2 cloud setting, never
    another tenant's and never a shared default.

    Returns ``None`` on any non-cloud surface, any unauthenticated caller,
    a user without cloud-sync consent, or a user with no saved location —
    every one of those falls through to the existing per-request IP
    geolocation step in the caller, which stays tenant-safe because it is
    scoped to this one request's own IP.
    """
    from services.computer_use.cloud_guard import is_cloud_surface

    if not is_cloud_surface():
        return None

    try:
        user_id = await get_current_user_id(request)
    except Exception:  # noqa: BLE001, RUF100 - best-effort: unauth falls to per-request IP geolocation
        return None

    try:
        from services.cloud_settings import get_cloud_settings_service

        service = get_cloud_settings_service()
        if not await service.has_cloud_sync_consent(user_id):
            return None
        location = await service.get_setting(user_id, "weather_location", None)
    except Exception:  # noqa: BLE001, RUF100 - best-effort: lookup failure falls to IP geolocation
        log.debug("Cloud user weather_location lookup failed", exc_info=True)
        return None

    return normalize_weather_request_location(location) if isinstance(location, str) else None


def _no_rate_limit(_limit: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def _decorator(func: Callable[P, R]) -> Callable[P, R]:
        return func

    return _decorator


def _validate_location_param(location: str) -> str | None:
    """Validate and sanitize location parameter to prevent SSRF.

    Returns sanitized location or None if invalid.
    """
    if not location:
        return None

    # Max length check
    if len(location) > 100:
        return None

    # Block numeric-only input (potential decimal IP like 2130706433 = 127.0.0.1)
    # But allow coordinates which have both comma AND decimal point
    stripped = location.replace(" ", "")
    if stripped.replace(",", "").replace(".", "").replace("-", "").isdigit():
        # Pure digits (possibly with separators) - could be decimal IP
        # Allow only if it looks like coordinates (has comma AND decimal)
        if not ("," in stripped and "." in stripped):
            return None

    # Allow Unicode word chars, spaces, commas, periods, hyphens (for coordinates and city names)
    # Pattern: "New York", "München", "東京", "São Paulo", "40.7128,-74.0060"
    if not re.match(r"^[\w\s,.\-]+$", location):
        return None

    # Block obvious injection attempts
    dangerous_patterns = [
        "://",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "@",
        "\\",
    ]  # nosec B104
    location_lower = location.lower()
    for pattern in dangerous_patterns:
        if pattern in location_lower:
            return None

    # URL-encode the result
    return quote(location, safe="")


def _weather_error_response(*, status_code: int, code: str, message: str) -> SafeJSONResponse:
    return SafeJSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "status": "error",
            "error": {
                "code": code,
                "message": message,
            },
            "data": None,
        },
    )


def register_weather_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    weather_cache = context.weather_cache
    rate_limit = context.rate_limit or _no_rate_limit

    @router.get("/v1/weather", dependencies=[Depends(require_auth)])
    @rate_limit("30/minute")
    async def get_weather(
        request: Request,
        lat: float | None = Query(default=None),
        lon: float | None = Query(default=None),
        city: str | None = Query(default=None),
        force_refresh: bool = Query(default=False),
    ):
        log.info(
            "Weather endpoint called (lat=%s, lon=%s, city=%s, force_refresh=%s)",
            lat,
            lon,
            city,
            force_refresh,
        )

        async def _inner():
            # Resolve city from settings if no query params provided
            resolved_city = city
            auto_detected = False
            from_settings = False

            if lat is None and lon is None and not city:
                resolved_city = await _resolve_cloud_user_location(request)
                if resolved_city:
                    from_settings = True
                    log.info("Using cloud user's saved weather_location")
                else:
                    user_ip = (
                        request.headers.get("cf-connecting-ip")
                        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                        or (request.client.host if request.client else None)
                    )
                    resolved_city = resolve_weather_request_location(None, user_ip=user_ip)
                    if resolved_city:
                        from_settings = True
                        log.info("Using weather_location from settings: %s", resolved_city)
                    else:
                        return _weather_error_response(
                            status_code=400,
                            code="weather_location_required",
                            message=WEATHER_LOCATION_REQUIRED_MESSAGE,
                        )

            # Validate location input before any cache read, write, or force
            # refresh clear touches filesystem-backed cache entries.
            location_url_encoded: str | None = None
            if (lat is None) != (lon is None):
                return _weather_error_response(
                    status_code=400,
                    code="invalid_coordinates",
                    message="Invalid coordinates: latitude and longitude must be provided together.",
                )
            if lat is not None and lon is not None:
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    return _weather_error_response(
                        status_code=400,
                        code="invalid_coordinates",
                        message="Invalid coordinates: latitude must be -90 to 90 and longitude must be -180 to 180.",
                    )
            elif resolved_city:
                location_url_encoded = _validate_location_param(resolved_city)
                if location_url_encoded is None:
                    return _weather_error_response(
                        status_code=400,
                        code="invalid_location",
                        message="Invalid location parameter.",
                    )

            # Force refresh bypasses only the requested location cache key. Do
            # not clear the shared weather cache directory for every user.
            if force_refresh:
                log.info("Force refresh requested for weather location")
                clear_entry = getattr(weather_cache, "clear_entry", None)
                if callable(clear_entry):
                    clear_entry(lat=lat, lon=lon, city=resolved_city)
                cached_weather = None
            else:
                cached_weather = weather_cache.get(lat=lat, lon=lon, city=resolved_city)
            if cached_weather:
                normalized_cache = normalize_weather_data(
                    cached_weather,
                    source="cache",
                    cached=True,
                    stale=bool(cached_weather.get("stale", False)),
                )
                log.info(
                    "Serving cached weather (age: %.1fh)",
                    normalized_cache.get("cache_age_hours", 0.0),
                )
                weather_payload = repair_mojibake_deep(build_weather_payload(normalized_cache))
                return {
                    "ok": True,
                    "status": "ok",
                    "data": weather_payload,
                }

            # Initialize error tracking variables
            error_message: str | None = None
            error_code: str | None = None

            try:
                from backend.weather_fetch import fetch_weather

                weather_data = await asyncio.wait_for(
                    fetch_weather(
                        lat=lat,
                        lon=lon,
                        city=resolved_city,
                        location_url_encoded=location_url_encoded,
                        force_refresh=force_refresh,
                    ),
                    timeout=TIMEOUT_VERY_LONG,
                )
                if weather_data:
                    # Report auto-detected location (but don't save it)
                    display_location = weather_data.get("location", "")
                    if not resolved_city and display_location and display_location != "Local":
                        auto_detected = True
                        log.info(
                            "Auto-detected weather location: %s",
                            display_location,
                        )

                    weather_cache.set(
                        weather_data,
                        lat=lat,
                        lon=lon,
                        city=resolved_city,
                        forecast_data=weather_data.get("forecast"),
                    )

                    weather_payload = repair_mojibake_deep(build_weather_payload(weather_data))
                    if auto_detected:
                        weather_payload["auto_detected_location"] = display_location
                    elif from_settings:
                        weather_payload["auto_detected_location"] = resolved_city
                    return {
                        "ok": True,
                        "status": "ok",
                        "data": weather_payload,
                    }
                else:
                    error_code = "weather_fetch_failed"
                    error_message = "Weather service unavailable"
            except TimeoutError:
                error_code = "weather_timeout"
                error_message = "Weather request timed out"
                log.warning("Weather fetch timeout after %s seconds", TIMEOUT_VERY_LONG)
            except Exception:
                error_code = "weather_error"
                error_message = "Weather fetch failed"
                log.exception("Weather fetch error")

            # Try to use stale cache as fallback
            if cached_weather is None:
                get_stale = getattr(weather_cache, "get_stale", None)
                stale_data = get_stale(lat=lat, lon=lon, city=resolved_city) if callable(get_stale) else None
                if stale_data:
                    normalized_stale = normalize_weather_data(
                        stale_data,
                        source="cache",
                        cached=True,
                        stale=True,
                    )
                    normalized_stale.setdefault("note", "served from cache due to offline fallback")
                    log.info("Using stale cached weather (offline fallback)")
                    weather_payload = repair_mojibake_deep(build_weather_payload(normalized_stale))
                    return {
                        "ok": True,
                        "status": "degraded",
                        "data": weather_payload,
                    }

            # No cache available — return honest 503
            log.warning(
                "Weather unavailable: code=%s message=%s",
                error_code or "weather_unavailable",
                error_message or "Weather temporarily unavailable",
            )
            return _weather_error_response(
                status_code=503,
                code=error_code or "weather_unavailable",
                message=error_message or "Weather temporarily unavailable",
            )

        try:
            return await toolbox.record_and_call(_inner, route="/v1/weather", method="GET")
        except Exception:
            log.exception("Unhandled weather route failure")
            return _weather_error_response(
                status_code=503,
                code="weather_unavailable",
                message="Weather service is temporarily unavailable. Try again in a moment.",
            )

    @router.get("/v1/weather/trends", dependencies=[Depends(require_auth)])
    async def get_weather_trends(
        lat: float | None = Query(default=None),
        lon: float | None = Query(default=None),
        city: str | None = Query(default=None),
    ):
        async def _inner():
            try:
                trends = weather_cache.get_trends(lat=lat, lon=lon, city=city)
                return {"ok": True, "trends": trends}
            except NotImplementedError:
                return _weather_error_response(
                    status_code=501,
                    code="not_implemented",
                    message="Weather trends analysis is not yet available.",
                )
            except Exception:
                log.exception("Failed to get weather trends")
                return _weather_error_response(
                    status_code=500,
                    code="weather_trends_unavailable",
                    message="Weather trends are temporarily unavailable right now.",
                )

        try:
            return await toolbox.record_and_call(_inner, route="/v1/weather/trends", method="GET")
        except Exception:
            log.exception("Unhandled weather trends route failure")
            return _weather_error_response(
                status_code=500,
                code="weather_trends_unavailable",
                message="Weather trends are temporarily unavailable right now.",
            )

    @router.get("/v1/weather/prediction", dependencies=[Depends(require_auth)])
    async def get_weather_prediction(
        lat: float | None = Query(default=None),
        lon: float | None = Query(default=None),
        city: str | None = Query(default=None),
    ):
        async def _inner():
            try:
                prediction = weather_cache.get_prediction(lat=lat, lon=lon, city=city)
                return {"ok": True, "prediction": prediction}
            except NotImplementedError:
                return _weather_error_response(
                    status_code=501,
                    code="not_implemented",
                    message="Weather prediction is not yet available.",
                )
            except Exception:
                log.exception("Failed to get weather prediction")
                return _weather_error_response(
                    status_code=500,
                    code="weather_prediction_unavailable",
                    message="Weather prediction is temporarily unavailable right now.",
                )

        try:
            return await toolbox.record_and_call(_inner, route="/v1/weather/prediction", method="GET")
        except Exception:
            log.exception("Unhandled weather prediction route failure")
            return _weather_error_response(
                status_code=500,
                code="weather_prediction_unavailable",
                message="Weather prediction is temporarily unavailable right now.",
            )

    log.info("Weather routes registered")
