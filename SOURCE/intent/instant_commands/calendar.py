"""Calendar command handlers for Viola instant commands."""

from __future__ import annotations

import re

from ._base import log


class CalendarHandlersMixin:
    """Calendar command handlers."""

    def _extract_calendar_create_details(self, params: dict[str, object]) -> tuple[str, str]:
        """Extract a calendar event title and time from instant-command params."""
        title = str(params.get("title") or params.get("event") or "").strip()
        time_str = str(params.get("time") or params.get("time_str") or "").strip()
        if title and time_str:
            return title, time_str

        original = str(params.get("_original_text", "")).strip()
        called_named_m = re.match(
            r"^(?:add|create|put|schedule)\s+(?:a\s+|an\s+)?(?:calendar\s+)?event\s+"
            r"(?:to\s+my\s+calendar\s+|on\s+my\s+calendar\s+)?at\s+(.+?)\s+(?:called|named)\s+(.+?)\.?$",
            original,
            re.I,
        )
        if called_named_m:
            return called_named_m.group(2).strip(" ."), called_named_m.group(1).strip(" .")

        patterns = [
            r"^(?:add|create|put|make|set\s+up)\s+(?:a|an)\s+(?:new\s+)?(.+?)\s+(?:to|on|in)\s+(?:my\s+|the\s+)?(?:calendar|schedule)\s+(?:for|at|on)\s+(.+?)\.?$",
            r"^add\s+(.+?)\s+to\s+my\s+calendar\s+at\s+(.+?)\.?$",
            r"^create(?:\s+a)?\s+calendar\s+event\s+(.+?)\s+at\s+(.+?)\.?$",
            r"^schedule\s+(.+?)\s+for\s+(.+?)\.?$",
            r"^put\s+(.+?)\s+on\s+my\s+calendar\s+at\s+(.+?)\.?$",
        ]
        for pattern in patterns:
            match = re.match(pattern, original, re.I)
            if match:
                extracted_title = match.group(1).strip(" .")
                extracted_time = match.group(2).strip(" .")
                context_m = re.match(
                    r"(.+?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+(?:with|about|regarding|for|to discuss)\b(.*)",
                    extracted_time,
                    re.I,
                )
                if context_m:
                    extracted_time = context_m.group(1).strip()
                    context = context_m.group(2).strip(" .")
                    if context:
                        context_clean = re.sub(
                            r"^(?:with|about|regarding|for|to discuss)\s+",
                            "",
                            context,
                            flags=re.I,
                        )
                        if context_clean:
                            extracted_title = "%s - %s" % (
                                extracted_title,
                                context_clean,
                            )
                return extracted_title, extracted_time

        return title, time_str

    def _extract_calendar_delete_details(self, params: dict[str, object]) -> tuple[str, str]:
        """Extract a calendar event title or event_id from instant-command params."""
        title = str(params.get("title") or params.get("event") or "").strip()
        event_id = str(params.get("event_id") or "").strip()
        if title or event_id:
            return title, event_id

        original = str(params.get("_original_text", "")).strip()
        if re.match(
            r"^(?:delete|remove|cancel)\s+my\s+next\s+(?:meeting|event|appointment)\.?$",
            original,
            re.I,
        ):
            return "next event", ""

        patterns = [
            r"^(?:delete|remove)\s+(.+?)\s+from\s+my\s+calendar\.?$",
            r"^cancel\s+my\s+(.+?)\.?$",
        ]
        for pattern in patterns:
            match = re.match(pattern, original, re.I)
            if match:
                return match.group(1).strip(" ."), event_id

        return title, event_id

    async def get_calendar_today(self, params: dict[str, object]) -> dict[str, object]:
        """Get today's calendar events."""
        try:
            from services.calendar.assistant import get_events_today_response

            result = await get_events_today_response()
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'get_calendar_today' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }

    async def get_calendar_tomorrow(self, params: dict[str, object]) -> dict[str, object]:
        """Get tomorrow's calendar events."""
        try:
            from services.calendar.assistant import get_events_tomorrow_response

            result = await get_events_tomorrow_response()
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'get_calendar_tomorrow' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }

    async def get_calendar_week(self, params: dict[str, object]) -> dict[str, object]:
        """Get this week's calendar events."""
        try:
            from services.calendar.assistant import get_events_week_response

            result = await get_events_week_response()
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'get_calendar_week' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }

    async def get_next_event(self, params: dict[str, object]) -> dict[str, object]:
        """Get the next upcoming calendar event."""
        try:
            from services.calendar.assistant import get_next_event_response

            result = await get_next_event_response()
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'get_next_event' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }

    async def create_calendar_event(self, params: dict[str, object]) -> dict[str, object]:
        """Create a calendar event."""
        try:
            from services.calendar.assistant import add_event_response

            title, time_str = self._extract_calendar_create_details(params)
            result = await add_event_response(title=title, time_str=time_str)
            if result.get("error") is None:
                await self._broadcast_calendar_updated("created", result.get("display", {}) or {})
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'create_calendar_event' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }

    async def delete_calendar_event(self, params: dict[str, object]) -> dict[str, object]:
        """Delete a calendar event."""
        try:
            from services.calendar.assistant import delete_event_response

            title, event_id = self._extract_calendar_delete_details(params)
            result = await delete_event_response(title=title, event_id=event_id)
            if result.get("error") is None:
                await self._broadcast_calendar_updated("deleted", result.get("display", {}) or {})
            return {
                "ok": result.get("error") is None,
                "message": result.get("speech", ""),
                "data": result.get("display", {}) or {},
            }
        except Exception:
            log.exception("Command 'delete_calendar_event' failed")
            return {
                "ok": False,
                "message": "The calendar service is not responding. Try again.",
                "data": {},
                "error": "calendar_unavailable",
            }
