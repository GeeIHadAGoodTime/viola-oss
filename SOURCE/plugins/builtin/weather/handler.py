"""Weather plugin handler — delegates to existing weather fetch infrastructure."""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from plugins.api import PluginResponse

logger = get_logger(__name__)
_WEATHER_PLUGIN_ERRORS = (ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError)


def get_current_weather(city: str = "") -> PluginResponse:
    """Get current weather conditions.

    .. deprecated::
        Phase 1 instant commands (``intent/instant_commands/weather.py:get_weather``)
        intercept all voice weather queries before the plugin system runs.  This
        function is only reachable via ``WeatherPlugin.handle()`` which is itself
        dead code for voice input.  The REST wrappers ``api_current`` / ``api_forecast``
        below still call it for the ``/v1/plugins/weather/`` endpoints.
    """
    try:
        import asyncio

        from backend.weather_fetch import fetch_weather

        # Run async fetch in sync context
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already in async context — use thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, fetch_weather(city=city or None))
                data = future.result(timeout=15)
        else:
            data = asyncio.run(fetch_weather(city=city or None))

        if data is None:
            return PluginResponse(
                speech="I couldn't get the weather right now. Try again later.",
                error="Weather fetch returned None",
            )

        temp = data.get("temperature", "?")
        condition = data.get("condition", "unknown")
        location = data.get("location", city or "your area")

        speech = "It's currently %s degrees and %s in %s." % (temp, condition, location)

        return PluginResponse(
            speech=speech,
            display={
                "temperature": temp,
                "condition": condition,
                "location": location,
                "temperature_c": data.get("temperature_c"),
                "unit": data.get("unit", "F"),
            },
        )
    except _WEATHER_PLUGIN_ERRORS as exc:
        logger.warning("Weather plugin error: %s", exc)
        return PluginResponse(
            speech="The weather service is not responding (%s). You can try again, or say 'search weather for [location]' to look it up online."
            % type(exc).__name__,
            error=str(exc),
        )


def get_forecast(city: str = "") -> PluginResponse:
    """Get weather forecast.

    .. deprecated::
        Phase 1 instant commands handle forecast queries before the plugin system.
        See ``get_current_weather`` deprecation note for details.
    """
    try:
        import asyncio

        from backend.weather_fetch import fetch_weather

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, fetch_weather(city=city or None))
                data = future.result(timeout=15)
        else:
            data = asyncio.run(fetch_weather(city=city or None))

        if data is None:
            return PluginResponse(
                speech="I couldn't get the forecast right now.",
                error="Weather fetch returned None",
            )

        location = data.get("location", city or "your area")
        forecast = data.get("forecast", [])

        if not forecast:
            temp = data.get("temperature", "?")
            condition = data.get("condition", "unknown")
            return PluginResponse(
                speech="Right now it's %s degrees and %s in %s. No extended forecast available."
                % (temp, condition, location),
            )

        parts = ["Here's the forecast for %s." % location]
        for day in forecast[:3]:
            date = day.get("date", "")
            temp = day.get("temperature", "?")
            cond = day.get("condition", "?")
            parts.append("%s: %s degrees, %s." % (date, temp, cond))

        return PluginResponse(
            speech=" ".join(parts),
            display={"location": location, "forecast": forecast[:3]},
        )
    except _WEATHER_PLUGIN_ERRORS as exc:
        logger.warning("Weather forecast plugin error: %s", exc)
        return PluginResponse(
            speech="The forecast service is not responding (%s). You can try again, or say 'search weather forecast for [location]' to look it up online."
            % type(exc).__name__,
            error=str(exc),
        )


# --- REST API handlers (mounted as /v1/plugins/weather/...) ---


async def api_current(request: Any = None) -> dict[str, Any]:
    """API handler for GET /v1/plugins/weather/current"""
    result = get_current_weather()
    return {"speech": result.speech, "display": result.display, "error": result.error}


async def api_forecast(request: Any = None) -> dict[str, Any]:
    """API handler for GET /v1/plugins/weather/forecast"""
    result = get_forecast()
    return {"speech": result.speech, "display": result.display, "error": result.error}
