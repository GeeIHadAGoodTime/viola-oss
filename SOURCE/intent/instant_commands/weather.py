"""Weather instant command handlers."""

from __future__ import annotations

from ._base import _format_forecast_message, log


class WeatherHandlersMixin:
    """Weather instant command handlers."""

    async def get_weather(self, params: dict[str, object]) -> dict[str, object]:
        """Get current weather by calling the weather fetch backend directly."""
        try:
            import re as _re

            from backend.weather_fetch import fetch_weather
            from utils.weather_location import (
                WEATHER_LOCATION_REQUIRED_MESSAGE,
                resolve_weather_request_location,
            )

            # Extract explicit city from "weather in <city>" / "forecast for <city>"
            original_text_raw = params.get("_original_text")
            original_text_str = original_text_raw if isinstance(original_text_raw, str) else ""
            city_match = _re.search(r"\b(?:in|for|at|near)\s+(.+?)\.?$", original_text_str, _re.I)
            explicit_city = city_match.group(1).strip() if city_match else None
            # Strip time phrases from city: "Milwaukee right now" → "Milwaukee"
            # Also handles standalone time words: "tomorrow" → None
            if explicit_city:
                explicit_city = (
                    _re.sub(
                        r"(?:^|\s+)(?:right\s+now|now|today|tonight|tomorrow|currently|this\s+(?:morning|afternoon|evening|weekend|week))$",
                        "",
                        explicit_city,
                        flags=_re.I,
                    ).strip()
                    or None
                )

            # Resolve: explicit city → settings → IP geolocation
            city = resolve_weather_request_location(explicit_city)
            if city is None:
                return {
                    "ok": False,
                    "message": WEATHER_LOCATION_REQUIRED_MESSAGE,
                    "data": {},
                    "error": "weather_location_required",
                }

            weather_data = await fetch_weather(city=city)
            if not weather_data:
                return {
                    "ok": False,
                    "message": "Weather data isn't available right now. Try again in a moment.",
                    "data": {},
                    "error": "weather_unavailable",
                }

            forecast_message = _format_forecast_message(weather_data, original_text_str)
            if forecast_message:
                return {"ok": True, "message": forecast_message, "data": weather_data}

            # Build a human-readable response
            temp = weather_data.get("temperature")
            condition = weather_data.get("condition", "")
            location = weather_data.get("location", "your area")
            humidity = weather_data.get("humidity")

            parts = []
            if temp is not None and condition:
                parts.append("It's currently %s and %s\u00b0F in %s" % (condition.lower(), temp, location))
            elif temp is not None:
                parts.append("It's %s\u00b0F in %s" % (temp, location))
            elif condition:
                parts.append("It's %s in %s" % (condition.lower(), location))

            if humidity is not None:
                parts.append("with %s%% humidity" % humidity)

            message = " ".join(parts) + "." if parts else "Weather data unavailable."
            return {"ok": True, "message": message, "data": weather_data}

        except Exception:
            log.exception("Command 'get_weather' failed")
            return {
                "ok": False,
                "message": "Couldn't get the weather right now. Check your internet connection.",
                "data": {},
                "error": "weather_failed",
            }
