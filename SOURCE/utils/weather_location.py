from __future__ import annotations

import ipaddress
import time
from collections import OrderedDict

import httpx

from core.constants import TIMEOUT_HOUR
from core.logging_config import get_logger
from services.api_cache import get_api_cache, public_cache_key

logger = get_logger(__name__)

WEATHER_LOCATION_REQUIRED_MESSAGE = (
    "I need to know where you are to check the weather. " "What city are you in? I'll remember it for next time."
)

_IP_GEOLOCATION_TTL_SECONDS = float(TIMEOUT_HOUR)
_IP_GEOLOCATION_CACHE_MAX = 1000
_ip_geolocation_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
_IP_GEOLOCATION_CACHE_NAMESPACE = "weather:ip_geolocation:v1"
_IP_GEOLOCATION_NEGATIVE_HIT = object()


def _normalize_location(value: object) -> str | None:
    if value is None:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = value.strip()
    return value or None


def _public_ip_or_none(user_ip: str | None) -> str | None:
    if not user_ip:
        return None
    candidate = user_ip.strip()
    if not candidate:
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved or parsed.is_multicast:
        return None
    return candidate


def _cache_get(ip_value: str) -> str | None:
    cached = _ip_geolocation_cache.get(ip_value)
    if cached is None:
        return None
    expires_at, location = cached
    if expires_at <= time.monotonic():
        _ip_geolocation_cache.pop(ip_value, None)
        return None
    _ip_geolocation_cache.move_to_end(ip_value)
    return location


def _cache_set(ip_value: str, location: str | None) -> None:
    _ip_geolocation_cache[ip_value] = (time.monotonic() + _IP_GEOLOCATION_TTL_SECONDS, location)
    _ip_geolocation_cache.move_to_end(ip_value)
    while len(_ip_geolocation_cache) > _IP_GEOLOCATION_CACHE_MAX:
        _ip_geolocation_cache.popitem(last=False)


def _ip_geolocation_cache_key(public_ip: str | None) -> str:
    return public_cache_key(
        _IP_GEOLOCATION_CACHE_NAMESPACE,
        {"public_ip": public_ip or "__self__"},
    )


def _get_shared_ip_geolocation(cache_key: str) -> str | object | None:
    cached = get_api_cache().get_public(cache_key)
    if not isinstance(cached, dict):
        return None
    if cached.get("found") is False:
        return _IP_GEOLOCATION_NEGATIVE_HIT
    location = _normalize_location(cached.get("location"))
    return location


def _set_shared_ip_geolocation(cache_key: str, location: str | None) -> None:
    get_api_cache().set_public(
        cache_key,
        {
            "found": location is not None,
            "location": location,
        },
        ttl_seconds=int(_IP_GEOLOCATION_TTL_SECONDS),
    )


def _try_ip_geolocation(user_ip: str | None = None) -> str | None:
    """Best-effort IP geolocation; returns city name or None."""
    public_ip = _public_ip_or_none(user_ip)
    shared_cache_key = _ip_geolocation_cache_key(public_ip)
    shared_cached = _get_shared_ip_geolocation(shared_cache_key)
    if shared_cached is _IP_GEOLOCATION_NEGATIVE_HIT:
        return None
    if isinstance(shared_cached, str):
        return shared_cached

    if public_ip:
        cached = _cache_get(public_ip)
        if cached is not None:
            return cached
    try:
        url = (
            "http://ip-api.com/json/%s?fields=city,regionName" % public_ip
            if public_ip
            else "http://ip-api.com/json/?fields=city,regionName"
        )
        resp = httpx.get(url, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json()
            city = data.get("city", "")
            region = data.get("regionName", "")
            if city:
                location = "%s, %s" % (city, region) if region else city
                if public_ip:
                    _cache_set(public_ip, location)
                _set_shared_ip_geolocation(shared_cache_key, location)
                logger.info("IP geolocation resolved to %s", location)
                return location
    except Exception:
        logger.debug("IP geolocation failed; no fallback location")
    if public_ip:
        _cache_set(public_ip, None)
    _set_shared_ip_geolocation(shared_cache_key, None)
    return None


def get_configured_weather_location() -> str | None:
    """Return the saved weather location, normalized to ``None`` when unset.

    Desktop-only. The cloud surface must never read this: ``SettingsManager``
    and ``config.settings.weather_location`` are both process-global to the
    container, so returning either here would hand one tenant's (or an
    operator default's) location to every other tenant's anonymous/no-city
    weather request on a shared server (tenant-safety review 2026-07-04,
    `backend/cloud_route_manifest.py` "weather" group). On cloud, callers
    resolve the SIGNED-IN user's own saved location per-request instead
    (`ui/api/routes/weather.py::_resolve_cloud_user_location`), and this
    function returns ``None`` so ``resolve_weather_request_location`` falls
    through to the (per-request, tenant-safe) IP geolocation step.
    """
    from services.computer_use.cloud_guard import is_cloud_surface

    if is_cloud_surface():
        return None

    try:
        from ui.settings_manager import get_settings_manager

        settings_mgr = get_settings_manager()
        saved_location = _normalize_location(settings_mgr.get("weather_location", ""))
        if saved_location:
            return saved_location
    except Exception:
        pass

    try:
        from config.settings import settings as app_settings
    except Exception:
        return None

    return _normalize_location(getattr(app_settings, "weather_location", ""))


def normalize_weather_request_location(city: str) -> str | None:
    """Normalize a city name extracted from user input.

    Strips whitespace and trailing punctuation, then title-cases the result.
    Returns ``None`` when the input is empty or whitespace-only.
    """
    return _normalize_location(city)


def resolve_weather_request_location(city: str | None, *, user_ip: str | None = None) -> str | None:
    """Return the best available location for a weather request.

    Resolution order:
    1. Explicit city from user input
    2. Configured default from SettingsManager / AppConfig
    3. Best-effort IP geolocation
    4. ``None`` so callers can ask the user to set a location
    """
    if city:
        return city
    configured = get_configured_weather_location()
    if configured:
        return configured
    return _try_ip_geolocation(user_ip)
