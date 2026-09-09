"""Post-call automation: extraction, receipts, calendar events, reminders.

After a phone call completes, extracts structured data from the transcript,
formats a receipt/summary, delivers it via messaging channels, generates
calendar events for appointments, and sets delivery reminders.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any

from config.defaults import DEFAULT_PHONE_MODEL
from core.asyncio_safe import run_async_synchronously
from core.logging_config import get_logger
from intent.tools.calendar_tools import calendar_add_event_handler
from telephony.call_analysis import CallExtraction

logger = get_logger(__name__)

_MID_CALL_DURABLE_ACTION_TYPES = frozenset({"calendar_event", "owner_notification", "callback_note"})


async def extract_call_data(transcript: str, task: str, api_key: str, *, user_id: str = "") -> CallExtraction:
    """Extract structured data from a completed call transcript.

    One LLM call using the canonical phone model.
    """
    prompt = (
        "Extract structured data from this phone call transcript.\n"
        "The caller's task was: %s\n\n"
        "Transcript:\n%s\n\n"
        "Return ONLY a JSON object. Include only fields that have values from the call.\n"
        'Valid fields: items_ordered (list of strings), total_price (string like "$14.99"),\n'
        "appointment_date, appointment_time, party_size (integer), confirmation_number,\n"
        "estimated_wait, business_name, delivery_address, payment_method,\n"
        "needs_callback (boolean), callback_reason.\n\n"
        "Return ONLY valid JSON, nothing else."
    ) % (task, transcript)

    from telephony.call_manager import create_accounted_openai_chat_completion

    data = await create_accounted_openai_chat_completion(
        api_key=api_key,
        model=DEFAULT_PHONE_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_output_tokens=500,
        user_id=user_id,
        operation="phone_post_call_extract",
    )

    text = data["choices"][0]["message"]["content"].strip()
    # Strip markdown code fences if present
    text = text.strip("`").removeprefix("json").strip()
    parsed = json.loads(text)
    return CallExtraction(**{k: v for k, v in parsed.items() if hasattr(CallExtraction, k)})


class PostCallActionRunner:
    """Executes automated actions based on extracted call data."""

    async def run(self, extraction: CallExtraction, record, user_id: str) -> list[str]:
        actions_taken = []
        owner_user_id = getattr(record, "user_id", "") or user_id

        # 1. Always: format and send summary/receipt
        summary = self._format_summary(extraction, record)
        await self._deliver_summary(summary, owner_user_id)
        actions_taken.append("summary_sent")

        # 2. Calendar event if appointment/reservation
        if extraction.appointment_date and extraction.appointment_time:
            actions_taken.append(await self._add_calendar_event(extraction, record, owner_user_id))

        # 3. Delivery/wait reminder if estimated_wait
        if extraction.estimated_wait:
            actions_taken.append("reminder_noted")
            logger.info("Post-call: delivery reminder noted: %s", extraction.estimated_wait)

        # 4. Callback reminder if needed
        if extraction.needs_callback:
            actions_taken.append("callback_reminder_noted")
            logger.info("Post-call: callback needed: %s", extraction.callback_reason)

        return actions_taken

    async def _add_calendar_event(self, extraction: CallExtraction, record, user_id: str) -> str:
        start_time = self._combined_appointment_start(extraction)
        if not start_time:
            logger.warning("Post-call: appointment date/time could not be combined for calendar add")
            return "calendar_event_failed"

        title = self._calendar_title(extraction)
        description = self._calendar_description(extraction, record)
        location = extraction.business_name or ""

        try:
            result = await calendar_add_event_handler(
                user_id=user_id,
                title=title,
                start_time=start_time,
                description=description,
                location=location,
            )
        except Exception as exc:
            logger.warning("Post-call calendar add failed: %s", exc)
            return "calendar_event_failed"

        if result.ok:
            logger.info(
                "Post-call: calendar event created for %s at %s",
                extraction.appointment_date,
                extraction.appointment_time,
            )
            return "calendar_event_created"

        if self._calendar_unavailable(result):
            ics = self._generate_ics(extraction, record)
            logger.info("Post-call: calendar unavailable; generated ICS fallback (%d bytes)", len(ics))
            return "calendar_ics_generated"

        logger.warning("Post-call calendar add rejected: %s", result.error)
        return "calendar_event_failed"

    def _combined_appointment_start(self, extraction: CallExtraction) -> str:
        date = (extraction.appointment_date or "").strip()
        time = (extraction.appointment_time or "").strip()
        if not date or not time:
            return ""
        if "T" in date:
            return date
        if time.startswith("T"):
            return date + time
        if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?", time):
            return "%sT%s" % (date, time)
        return "%s %s" % (date, time)

    def _calendar_title(self, extraction: CallExtraction) -> str:
        if extraction.business_name:
            if extraction.party_size:
                return "Reservation at %s" % extraction.business_name
            return "Appointment with %s" % extraction.business_name
        return "Appointment booked by Viola"

    def _calendar_description(self, extraction: CallExtraction, record) -> str:
        lines = ["Booked via Viola phone call."]
        if getattr(record, "task", ""):
            lines.append("Task: %s" % record.task)
        if extraction.confirmation_number:
            lines.append("Confirmation: %s" % extraction.confirmation_number)
        return "\n".join(lines)

    def _calendar_unavailable(self, result) -> bool:
        if result.error and ("calendar_unavailable" in result.error or "not_configured" in result.error):
            return True
        data = result.data if isinstance(result.data, dict) else {}
        return data.get("error") in {"calendar_unavailable", "not_configured"} or data.get("source") == "not_configured"

    def _format_summary(self, extraction: CallExtraction, record) -> str:
        lines = ["Call Summary — %s" % (extraction.business_name or record.phone_number)]
        lines.append("Duration: %.0fs | Cost: $%.4f" % (record.duration_seconds, record.estimated_cost_usd))
        lines.append("")

        if extraction.items_ordered:
            lines.append("Ordered:")
            for item in extraction.items_ordered:
                lines.append("  - %s" % item)
            if extraction.total_price:
                lines.append("  Total: %s" % extraction.total_price)

        if extraction.appointment_date:
            lines.append("%s at %s" % (extraction.appointment_date, extraction.appointment_time or ""))
            if extraction.party_size:
                lines.append("   Party of %d" % extraction.party_size)

        if extraction.estimated_wait:
            lines.append("Estimated: %s" % extraction.estimated_wait)

        if extraction.confirmation_number:
            lines.append("Confirmation: %s" % extraction.confirmation_number)

        if extraction.payment_method:
            lines.append("Payment: %s" % extraction.payment_method)

        if extraction.needs_callback:
            lines.append("Callback needed: %s" % extraction.callback_reason)

        return "\n".join(lines)

    async def _deliver_summary(self, summary: str, user_id: str):
        # WebSocket (UI)
        try:
            from ui.websocket.event_hub import get_event_hub

            hub = get_event_hub()
            if hub:
                await hub.broadcast("call_summary", {"text": summary}, user_id=user_id)
        except Exception as exc:
            logger.warning("WS summary delivery failed: %s", exc)

        # Messaging channels
        try:
            from messaging.hub import get_messaging_hub

            messaging_hub = get_messaging_hub()
            if messaging_hub is None:
                logger.debug("No messaging hub available for post-call summary")
                return
            delivered = await messaging_hub.broadcast(summary)
            if delivered == 0:
                logger.debug("No active messaging channels for post-call summary")
        except ImportError:
            logger.debug("Messaging hub unavailable for post-call summary")
        except Exception as exc:
            logger.warning("Messaging summary delivery failed: %s", exc)

    def _generate_ics(self, extraction: CallExtraction, record) -> str:
        uid = "%s@viola" % record.call_id
        summary_text = extraction.business_name or "Appointment"
        if extraction.items_ordered:
            summary_text = "%s: %s" % (
                extraction.business_name or "Order",
                ", ".join(extraction.items_ordered),
            )

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Viola//Phone Call//EN",
            "BEGIN:VEVENT",
            "UID:%s" % uid,
            "SUMMARY:%s" % summary_text,
            "DESCRIPTION:Booked via Viola phone call",
        ]
        if extraction.business_name:
            lines.append("LOCATION:%s" % extraction.business_name)
        lines.extend(["END:VEVENT", "END:VCALENDAR"])
        return "\n".join(lines)

    async def run_mid_call_action(self, action: dict[str, Any], record, user_id: str) -> str:
        action_type = str(action.get("action_type") or "").strip()
        if action_type not in _MID_CALL_DURABLE_ACTION_TYPES:
            raise ValueError("Unsupported durable call action_type: %s" % (action_type or "<missing>"))

        owner_user_id = getattr(record, "user_id", "") or user_id
        if not owner_user_id:
            raise ValueError("Authenticated user_id is required for durable call actions.")

        if action_type == "calendar_event":
            extraction = CallExtraction(
                appointment_date=str(action.get("appointment_date") or "").strip(),
                appointment_time=str(action.get("appointment_time") or "").strip(),
                business_name=str(action.get("business_name") or action.get("title") or "").strip(),
                confirmation_number=str(action.get("confirmation_number") or "").strip(),
            )
            party_size = action.get("party_size")
            if isinstance(party_size, int):
                extraction.party_size = party_size
            return await self._add_calendar_event(extraction, record, owner_user_id)

        message = str(action.get("message") or action.get("callback_reason") or "").strip()
        if not message:
            title = str(action.get("title") or "Phone call update").strip()
            message = "%s captured during call %s." % (title, getattr(record, "call_id", ""))
        if action_type == "callback_note":
            message = "Callback needed: %s" % message

        await self._deliver_summary(message, owner_user_id)
        return "%s_sent" % action_type


async def run_mid_call_durable_action(action: dict[str, Any], record, user_id: str) -> str:
    runner = PostCallActionRunner()
    return await runner.run_mid_call_action(action, record, user_id)


def _run_mid_call_durable_action_in_worker(action: dict[str, Any], record, user_id: str) -> str:
    result = run_async_synchronously(run_mid_call_durable_action(action, record, user_id))
    if not isinstance(result, str):
        raise RuntimeError("Mid-call durable action returned an invalid result")
    return result


def schedule_mid_call_durable_action(action: dict[str, Any], record, user_id: str) -> asyncio.Task[str]:
    action_type = str(action.get("action_type") or "").strip()
    if action_type not in _MID_CALL_DURABLE_ACTION_TYPES:
        raise ValueError("Unsupported durable call action_type: %s" % (action_type or "<missing>"))

    async def _run() -> str:
        try:
            result = await asyncio.to_thread(_run_mid_call_durable_action_in_worker, action, record, user_id)
            logger.info(
                "Mid-call durable action completed: call=%s action_type=%s result=%s",
                getattr(record, "call_id", ""),
                action_type,
                result,
            )
            return result
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning(
                "Mid-call durable action failed: call=%s action_type=%s error=%s",
                getattr(record, "call_id", ""),
                action_type,
                exc,
            )
            return "mid_call_action_failed"

    task = asyncio.create_task(_run(), name="phone-mid-call-durable-%s" % getattr(record, "call_id", "unknown"))
    tasks = getattr(record, "_mid_call_durable_tasks", None)
    if not isinstance(tasks, list):
        tasks = []
        with suppress(Exception):
            record._mid_call_durable_tasks = tasks
    tasks.append(task)

    def _discard(done_task: asyncio.Task[str]) -> None:
        with suppress(ValueError):
            tasks.remove(done_task)

    task.add_done_callback(_discard)
    return task
