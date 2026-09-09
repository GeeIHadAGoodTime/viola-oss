"""Thin calendar plugin shim over the shared calendar assistant flows."""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from plugins.api import PluginResponse
from services.calendar.assistant import (
    add_event_response,
    delete_event_response,
    get_events_today_response,
    get_events_tomorrow_response,
    get_events_week_response,
    get_next_event_response,
    parse_natural_time as _parse_natural_time,
)

logger = get_logger(__name__)
_PLUGIN_HANDLER_ERRORS = (ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError)


def _get_manager():
    from services.calendar import get_calendar_manager

    return get_calendar_manager()


def _run_async(target: Any, *args: Any, **kwargs: Any) -> Any:
    import asyncio
    import concurrent.futures

    coro = target(*args, **kwargs) if callable(target) else target
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=15)
    return asyncio.run(coro)


def _to_plugin_response(payload: dict[str, Any]) -> PluginResponse:
    return PluginResponse(
        speech=str(payload.get("speech") or ""),
        display=payload.get("display") if isinstance(payload.get("display"), dict) else {},
        error=payload.get("error"),
    )


def _compat_today_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(payload.get("ok"))
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    count = int(payload.get("count") or len(events))

    if not ok:
        return {
            "speech": "I couldn't complete that calendar request right now.",
            "display": payload if isinstance(payload, dict) else {},
            "error": payload.get("error", "unknown"),
        }

    if "event_id" in payload and count == 0:
        title = payload.get("title") or "that event"
        return {
            "speech": "Added '%s' to your calendar." % title,
            "display": payload if isinstance(payload, dict) else {},
            "error": None,
        }

    if not events:
        return {
            "speech": "You have no events on your calendar today.",
            "display": {"events": []},
            "error": None,
        }

    lead = "You have %d event%s today." % (count, "" if count == 1 else "s")
    details = []
    for event in events[:3]:
        title = str(event.get("title") or "Untitled")
        time_text = str(event.get("time") or "").strip()
        if time_text:
            details.append("%s at %s" % (title, time_text))
        else:
            details.append(title)
    return {
        "speech": "%s %s" % (lead, ". ".join(details)),
        "display": {"events": events},
        "error": None,
    }


def _compat_next_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ok = bool(payload.get("ok"))
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if not ok:
        return {
            "speech": "I couldn't complete that calendar request right now.",
            "display": payload if isinstance(payload, dict) else {},
            "error": payload.get("error", "unknown"),
        }
    if not events:
        return {
            "speech": "You have no upcoming events in the next week.",
            "display": {"next_event": None},
            "error": None,
        }

    event = events[0]
    title = str(event.get("title") or "Untitled")
    time_text = str(event.get("time") or "").strip()
    return {
        "speech": "Your next event is %s today%s."
        % (
            title,
            (" at %s" % time_text) if time_text else "",
        ),
        "display": {"next_event": event},
        "error": None,
    }


def _compat_add_payload(payload: dict[str, Any], *, title: str) -> dict[str, Any]:
    if not bool(payload.get("ok")):
        return {
            "speech": "I couldn't add that event to your calendar.",
            "display": payload if isinstance(payload, dict) else {},
            "error": payload.get("error", "unknown"),
        }
    return {
        "speech": "Added '%s' to your calendar." % title,
        "display": payload if isinstance(payload, dict) else {},
        "error": None,
    }


def get_events_today() -> PluginResponse:
    try:
        payload = _run_async(get_events_today_response)
        if "speech" not in payload and "display" not in payload:
            payload = _compat_today_payload(payload)
        return _to_plugin_response(payload)
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin today error: %s", exc)
        return PluginResponse(
            speech="The calendar service returned an error (%s). Check your calendar settings are configured, or try again."
            % type(exc).__name__,
            error=str(exc),
        )


def get_events_tomorrow() -> PluginResponse:
    try:
        return _to_plugin_response(_run_async(get_events_tomorrow_response))
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin tomorrow error: %s", exc)
        return PluginResponse(
            speech="The calendar service returned an error (%s). Check your calendar settings are configured, or try again."
            % type(exc).__name__,
            error=str(exc),
        )


def get_events_week() -> PluginResponse:
    try:
        return _to_plugin_response(_run_async(get_events_week_response))
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin week error: %s", exc)
        return PluginResponse(
            speech="The calendar service returned an error (%s). Check your calendar settings are configured, or try again."
            % type(exc).__name__,
            error=str(exc),
        )


def get_next_event() -> PluginResponse:
    try:
        payload = _run_async(get_next_event_response)
        if "speech" not in payload and "display" not in payload:
            payload = _compat_next_payload(payload)
        return _to_plugin_response(payload)
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin next error: %s", exc)
        return PluginResponse(
            speech="The calendar service returned an error (%s). Check your calendar settings are configured, or try again."
            % type(exc).__name__,
            error=str(exc),
        )


def add_event(title: str, time_str: str) -> PluginResponse:
    if not title.strip():
        return PluginResponse(speech="I need a title for the event.", error="missing_title")
    if not time_str.strip():
        return PluginResponse(speech="I need a time for the event.", error="missing_time")
    try:
        payload = _run_async(add_event_response, title=title, time_str=time_str)
        if "speech" not in payload and "display" not in payload:
            payload = _compat_add_payload(payload, title=title or "that event")
        return _to_plugin_response(payload)
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin add error: %s", exc)
        return PluginResponse(
            speech="Failed to add the event (%s). Check your calendar settings are configured and try again."
            % type(exc).__name__,
            error=str(exc),
        )


def delete_event(title: str = "", event_id: str = "") -> PluginResponse:
    try:
        return _to_plugin_response(_run_async(delete_event_response, title=title, event_id=event_id))
    except _PLUGIN_HANDLER_ERRORS as exc:
        logger.warning("Calendar plugin delete error: %s", exc)
        return PluginResponse(
            speech="Failed to delete the event (%s). Check your calendar settings are configured and try again."
            % type(exc).__name__,
            error=str(exc),
        )


async def api_events_today(request: Any = None) -> dict[str, Any]:
    result = get_events_today()
    return {"speech": result.speech, "display": result.display, "error": result.error}


async def api_next_event(request: Any = None) -> dict[str, Any]:
    result = get_next_event()
    return {"speech": result.speech, "display": result.display, "error": result.error}


async def api_add_event(request: Any = None) -> dict[str, Any]:
    if request is not None:
        try:
            body = await request.json()
            title = body.get("title", "")
            time_str = body.get("time", "")
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Calendar plugin API add request body unavailable: %s", exc)
            title = ""
            time_str = ""
    else:
        title = ""
        time_str = ""

    result = add_event(title, time_str)
    return {"speech": result.speech, "display": result.display, "error": result.error}


__all__ = [
    "_parse_natural_time",
    "add_event",
    "api_add_event",
    "api_events_today",
    "api_next_event",
    "delete_event",
    "get_events_today",
    "get_events_tomorrow",
    "get_events_week",
    "get_next_event",
]
