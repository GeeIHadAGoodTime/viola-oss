"""Timer, alarm, and sleep-timer instant command handlers."""

from __future__ import annotations

from ._base import *


def _params_user_id(params: dict[str, object]) -> str | None:
    """Extract the request user_id from instant-command params (F-050).

    ``intent.pipeline_processors`` injects ``params["_user_id"]`` before
    dispatch. We require it here so timer/alarm instant commands cannot
    mutate the wrong tenant's state when the dispatch path forgot to
    set it.
    """
    raw = params.get("_user_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


class TimersHandlersMixin:
    """Timer, alarm, and sleep-timer instant command handlers."""

    async def set_timer(self, params: dict[str, object]) -> dict[str, object]:
        """Set a timer from command with duration string."""
        try:
            from services.timer_service import get_timer_service, parse_duration

            uid = _params_user_id(params)
            if uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose timer to set.",
                    "data": {},
                    "error": "missing_user_id",
                }

            # Get the original command text to extract duration
            original_text_obj = params.get("_original_text", "")
            original_text = original_text_obj if isinstance(original_text_obj, str) else ""

            # Try to parse duration from the text
            duration_seconds = parse_duration(original_text)

            if duration_seconds is None or duration_seconds <= 0:
                return {
                    "ok": False,
                    "message": "I couldn't understand the timer duration. Try 'timer 5 minutes'.",
                    "data": {},
                    "error": "invalid_duration",
                }

            # Create the timer scoped to the authenticated caller.
            service = get_timer_service()
            timer_id = service.add_timer(duration_seconds, user_id=uid)

            # Format response
            if duration_seconds >= 3600:
                hours = duration_seconds // 3600
                mins = (duration_seconds % 3600) // 60
                time_str = f"{hours} hour{'s' if hours != 1 else ''}"
                if mins > 0:
                    time_str += f" {mins} minute{'s' if mins != 1 else ''}"
            elif duration_seconds >= 60:
                mins = duration_seconds // 60
                secs = duration_seconds % 60
                time_str = f"{mins} minute{'s' if mins != 1 else ''}"
                if secs > 0:
                    time_str += f" {secs} second{'s' if secs != 1 else ''}"
            else:
                time_str = f"{duration_seconds} second{'s' if duration_seconds != 1 else ''}"

            return {
                "ok": True,
                "message": f"Timer set for {time_str}",
                "data": {"timer_id": timer_id, "duration_seconds": duration_seconds},
            }

        except Exception as e:
            log.exception("Failed to set timer: %s", e)
            return {
                "ok": False,
                "message": "Couldn't set that timer. Try specifying the duration again?",
                "data": {},
                "error": "timer_set_failed",
            }

    async def set_timer_hm(self, params: dict[str, object]) -> dict[str, object]:
        """Set a timer with hours and minutes (e.g., '1 hour 30 minutes')."""
        try:
            from services.timer_service import get_timer_service, parse_duration

            uid = _params_user_id(params)
            if uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose timer to set.",
                    "data": {},
                    "error": "missing_user_id",
                }

            # Get the original command text
            original_text_obj = params.get("_original_text", "")
            original_text = original_text_obj if isinstance(original_text_obj, str) else ""
            duration_seconds = parse_duration(original_text)

            if duration_seconds is None or duration_seconds <= 0:
                return {
                    "ok": False,
                    "message": "I couldn't understand the timer duration.",
                    "data": {},
                    "error": "invalid_duration",
                }

            service = get_timer_service()
            timer_id = service.add_timer(duration_seconds, user_id=uid)

            hours = duration_seconds // 3600
            mins = (duration_seconds % 3600) // 60
            time_str = f"{hours} hour{'s' if hours != 1 else ''} {mins} minute{'s' if mins != 1 else ''}"

            return {
                "ok": True,
                "message": f"Timer set for {time_str}",
                "data": {"timer_id": timer_id, "duration_seconds": duration_seconds},
            }

        except Exception as e:
            log.exception("Failed to set timer: %s", e)
            return {
                "ok": False,
                "message": "Couldn't set that timer. Try specifying the duration again?",
                "data": {},
                "error": "timer_set_failed",
            }

    async def cancel_timer(self, params: dict[str, object]) -> dict[str, object]:
        """Cancel the most recent timer."""
        try:
            from services.timer_service import get_timer_service

            uid = _params_user_id(params)
            if uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose timer to cancel.",
                    "data": {},
                    "error": "missing_user_id",
                }

            service = get_timer_service()

            if service.get_timer_count(user_id=uid) == 0:
                return {
                    "ok": True,
                    "message": "No active timers to cancel",
                    "data": {},
                }

            if service.cancel_most_recent(user_id=uid):
                return {
                    "ok": True,
                    "message": "Timer cancelled",
                    "data": {},
                }
            else:
                return {
                    "ok": False,
                    "message": "Couldn't cancel timer",
                    "data": {},
                    "error": "cancel_failed",
                }

        except Exception as e:
            log.exception("Failed to cancel timer: %s", e)
            return {
                "ok": False,
                "message": "Couldn't cancel the timer right now. Try again?",
                "data": {},
                "error": "timer_cancel_failed",
            }

    async def cancel_all_timers(self, params: dict[str, object]) -> dict[str, object]:
        """Cancel all active timers."""
        try:
            from services.timer_service import get_timer_service

            uid = _params_user_id(params)
            if uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose timers to cancel.",
                    "data": {},
                    "error": "missing_user_id",
                }

            service = get_timer_service()
            count = service.cancel_all(user_id=uid)

            if count == 0:
                return {
                    "ok": True,
                    "message": "No active timers to cancel",
                    "data": {"cancelled_count": 0},
                }

            return {
                "ok": True,
                "message": f"Cancelled {count} timer{'s' if count != 1 else ''}",
                "data": {"cancelled_count": count},
            }

        except Exception as e:
            log.exception("Failed to cancel timers: %s", e)
            return {
                "ok": False,
                "message": "Couldn't cancel the timers right now. Try again?",
                "data": {},
                "error": "timer_cancel_all_failed",
            }

    async def timer_status(self, params: dict[str, object]) -> dict[str, object]:
        """Get status of active timers."""
        try:
            from services.timer_service import get_timer_service

            uid = _params_user_id(params)
            if uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose timers to check.",
                    "data": {},
                    "error": "missing_user_id",
                }

            service = get_timer_service()
            timers = service.get_all_timers(user_id=uid)

            if not timers:
                return {
                    "ok": True,
                    "message": "No active timers",
                    "data": {"timers": []},
                }

            # Format timer info
            status_lines = []
            for timer in timers:
                remaining = timer.remaining_seconds
                if remaining <= 0:
                    time_str = "done!"
                elif remaining >= 3600:
                    hours = int(remaining // 3600)
                    mins = int((remaining % 3600) // 60)
                    time_str = f"{hours}h {mins}m left"
                elif remaining >= 60:
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    time_str = f"{mins}m {secs}s left"
                else:
                    time_str = f"{int(remaining)}s left"

                status_lines.append(f"{timer.label}: {time_str}")

            message = "\n".join(status_lines)

            return {
                "ok": True,
                "message": message,
                "data": {"timers": [t.to_dict() for t in timers], "count": len(timers)},
            }

        except Exception as e:
            log.exception("Failed to get timer status: %s", e)
            return {
                "ok": False,
                "message": "Couldn't check your timers right now. Try again?",
                "data": {},
                "error": "timer_status_failed",
            }

    def _parse_alarm_time(self, text: str) -> datetime.datetime | None:
        """Extract an alarm datetime from natural language text.

        Supports: '7 am', '6:30 pm', '14:00', 'noon', 'midnight',
        'tomorrow', 'tomorrow morning'.
        """
        from datetime import timedelta

        now = datetime.datetime.now().astimezone()

        # "tomorrow" variants
        if re.search(r"\btomorrow\b", text, re.I):
            base = now + timedelta(days=1)
            # Try to extract a specific time from the text
            m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
            if m:
                hour = int(m.group(1))
                minute = int(m.group(2)) if m.group(2) else 0
                period = m.group(3).lower()
                if period == "pm" and hour != 12:
                    hour += 12
                elif period == "am" and hour == 12:
                    hour = 0
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return base.replace(hour=8, minute=0, second=0, microsecond=0)

        # "noon" / "midnight"
        if re.search(r"\bnoon\b", text, re.I):
            alarm_dt = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)
            return alarm_dt
        if re.search(r"\bmidnight\b", text, re.I):
            alarm_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)
            return alarm_dt

        # "7 am", "6:30 pm"
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.group(2) else 0
            period = m.group(3).lower()
            if period == "pm" and hour != 12:
                hour += 12
            elif period == "am" and hour == 12:
                hour = 0
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            alarm_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)
            return alarm_dt

        # 24-hour format "14:00", "6:30"
        m = re.search(r"(\d{1,2}):(\d{2})", text, re.I)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                alarm_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if alarm_dt <= now:
                    alarm_dt += timedelta(days=1)
                return alarm_dt

        return None

    async def set_alarm(self, params: dict[str, object]) -> dict[str, object]:
        """Set an alarm for a specific time using the scheduler service."""
        try:
            from datetime import UTC

            from services.scheduler.service import get_scheduler_service

            _uid = _params_user_id(params)
            if _uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose alarm to set.",
                    "data": {},
                    "error": "missing_user_id",
                }
            original = str(params.get("_original_text", ""))
            alarm_dt = self._parse_alarm_time(original)
            if alarm_dt is None:
                return {
                    "ok": False,
                    "message": "I couldn't understand the alarm time. Try 'set alarm for 7 AM'.",
                    "data": {},
                    "error": "invalid_alarm_time",
                }

            scheduler = get_scheduler_service()
            alarm_utc = alarm_dt.astimezone(UTC)
            alarm_iso = alarm_utc.isoformat()
            schedule_id = scheduler.add(
                _uid,
                label="Alarm",
                action="play alarm sound",
                one_shot_at=alarm_iso,
            )

            time_str = alarm_dt.strftime("%#I:%M %p") if os.name == "nt" else alarm_dt.strftime("%-I:%M %p")
            return {
                "ok": True,
                "message": "Alarm set for %s" % time_str,
                "data": {"schedule_id": schedule_id, "alarm_time": alarm_iso},
            }
        except Exception as e:
            log.exception("Failed to set alarm: %s", e)
            return {
                "ok": False,
                "message": "Couldn't set that alarm. Try again?",
                "data": {},
                "error": "alarm_set_failed",
            }

    async def cancel_alarm(self, params: dict[str, object]) -> dict[str, object]:
        """Cancel active alarm(s)."""
        try:
            from services.scheduler.service import get_scheduler_service

            _uid = _params_user_id(params)
            if _uid is None:
                return {
                    "ok": False,
                    "message": "I couldn't tell whose alarm to cancel.",
                    "data": {},
                    "error": "missing_user_id",
                }
            scheduler = get_scheduler_service()
            schedules = scheduler.list_schedules(_uid, enabled_only=True)
            alarms = [s for s in schedules if "alarm" in s.label.lower()]

            if not alarms:
                return {
                    "ok": True,
                    "message": "No active alarms to cancel.",
                    "data": {"cancelled_count": 0},
                }

            cancelled = 0
            for alarm in alarms:
                scheduler.disable(_uid, alarm.id)
                cancelled += 1

            return {
                "ok": True,
                "message": "Alarm cancelled." if cancelled == 1 else "%d alarms cancelled." % cancelled,
                "data": {"cancelled_count": cancelled},
            }
        except Exception as e:
            log.exception("Failed to cancel alarm: %s", e)
            return {
                "ok": False,
                "message": "Couldn't cancel that alarm. Try again?",
                "data": {},
                "error": "alarm_cancel_failed",
            }

    async def set_sleep_timer(self, params: dict[str, object]) -> dict[str, object]:
        """Set a timer that stops music playback after a delay."""
        try:
            original = str(params.get("_original_text", ""))

            # Parse duration from text
            duration_seconds = self._parse_sleep_duration(original)

            if duration_seconds is None or duration_seconds <= 0:
                return {
                    "ok": False,
                    "message": "I couldn't understand the duration. Try 'sleep timer 30 minutes'.",
                    "data": {},
                    "error": "invalid_duration",
                }

            # Cancel any existing sleep timer
            existing = getattr(self, "_sleep_timer", None)
            if existing is not None:
                existing.cancel()

            # Define callback to stop music
            controller = self.controller

            def _stop_music() -> None:
                try:
                    if hasattr(controller.music, "stop"):
                        import asyncio as _asyncio

                        loop = _asyncio.get_event_loop()
                        if loop.is_running():
                            _asyncio.run_coroutine_threadsafe(_call_maybe_async(controller.music, "stop"), loop)
                        else:
                            loop.run_until_complete(_call_maybe_async(controller.music, "stop"))
                except Exception:
                    log.exception("Sleep timer stop callback failed")

            timer = threading.Timer(duration_seconds, _stop_music)
            timer.daemon = True
            timer.start()
            self._sleep_timer = timer

            # Format friendly duration
            time_str = self._format_duration(duration_seconds)

            log.info("Sleep timer set for %d seconds", duration_seconds)
            return {
                "ok": True,
                "message": "I'll stop the music in %s" % time_str,
                "data": {"duration_seconds": duration_seconds},
            }

        except Exception:
            log.exception("Command 'set_sleep_timer' failed")
            return {
                "ok": False,
                "message": "Couldn't set the sleep timer. Try specifying the duration again?",
                "data": {},
                "error": "sleep_timer_failed",
            }

    def _parse_sleep_duration(self, text: str) -> int | None:
        """Parse a duration string into seconds."""
        total = 0
        found = False

        # Match hours
        h_match = re.search(r"(\d+)\s*h(?:ours?)?", text, re.I)
        if h_match:
            total += int(h_match.group(1)) * 3600
            found = True

        # Match minutes
        m_match = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)?", text, re.I)
        if m_match:
            total += int(m_match.group(1)) * 60
            found = True

        # Match seconds
        s_match = re.search(r"(\d+)\s*s(?:ec(?:ond)?s?)?", text, re.I)
        if s_match:
            total += int(s_match.group(1))
            found = True

        # If no unit specified, try bare number and assume minutes
        if not found:
            bare_match = re.search(r"(\d+)", text)
            if bare_match:
                total = int(bare_match.group(1)) * 60
                found = True

        return total if found else None

    async def play_alarm_sound(self, params: dict[str, object]) -> dict[str, object]:
        """Handle the 'play alarm sound' action dispatched by the scheduler.

        When a user sets an alarm via ``set_alarm``, the scheduler stores
        ``action="play alarm sound"``.  When the alarm fires, the scheduler
        dispatches that string through the intent pipeline.  This handler
        catches it and speaks the notification via TTS.

        The scheduler calls ``pipeline.process()`` directly — there is no
        voice command handler in the loop to speak the result.  So we must
        drive TTS ourselves here.
        """
        notification = "Your alarm is going off!"
        log.info("Alarm triggered — speaking notification via TTS")

        # Speak directly via TTS — the scheduler path has no voice handler.
        # The pipeline injects a TTS reference on the InstantCommandHandler
        # (self.controller.tts) so we can speak directly.
        tts = getattr(self.controller, "tts", None)
        if tts is not None:
            try:
                speak = getattr(tts, "speak", None)
                if callable(speak):
                    coro = speak(notification)
                    if asyncio.iscoroutine(coro):
                        await coro
            except Exception as tts_err:
                log.warning("Alarm TTS notification failed: %s", tts_err)
        else:
            log.warning("Alarm fired but TTS engine not available")

        # Also broadcast via messaging hub if available
        try:
            from messaging.hub import get_messaging_hub

            hub = get_messaging_hub()
            if hub is not None:
                await hub.broadcast(notification)  # mt-ok: MessagingHub — device-scoped channels
        except Exception as msg_err:
            log.debug("Alarm messaging notification skipped: %s", msg_err)

        return {
            "ok": True,
            "message": notification,
            "data": {"alarm_fired": True},
        }

    def _format_duration(seconds: int) -> str:
        """Format seconds into a human-readable duration string."""
        if seconds >= 3600:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            parts = ["%d hour%s" % (hours, "s" if hours != 1 else "")]
            if mins > 0:
                parts.append("%d minute%s" % (mins, "s" if mins != 1 else ""))
            return " ".join(parts)
        elif seconds >= 60:
            mins = seconds // 60
            secs = seconds % 60
            parts = ["%d minute%s" % (mins, "s" if mins != 1 else "")]
            if secs > 0:
                parts.append("%d second%s" % (secs, "s" if secs != 1 else ""))
            return " ".join(parts)
        else:
            return "%d second%s" % (seconds, "s" if seconds != 1 else "")
