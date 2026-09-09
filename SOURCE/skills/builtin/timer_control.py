"""
Timer Control Skill

Handles timer commands (set, cancel, status).
"""

from __future__ import annotations

from core.logging_config import get_logger

from ..base import Intent, Response, Skill

logger = get_logger(__name__)


class TimerControlSkill(Skill):
    """Skill for managing timers."""

    name = "timer_control"
    description = "Set, cancel, and check timers"
    priority = 90  # High priority - timers are instant commands

    def __init__(self, context=None):
        super().__init__(context)
        self._logger = logger.bind(skill=self.name)

    def patterns(self) -> list[str]:
        return [
            # Set timer: "set timer for 5 minutes", "timer 10 mins", "5 minute timer"
            r"^(?:set\s+)?(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*(?:m(?:in(?:ute)?s?)?|h(?:ours?)?|s(?:ec(?:ond)?s?)?)$",
            r"^(\d+)\s*(?:m(?:in(?:ute)?s?)?|h(?:ours?)?|s(?:ec(?:ond)?s?)?)\s+timer$",
            # Timer with hours and minutes: "timer 1 hour 30 minutes"
            r"^(?:set\s+)?(?:a\s+)?timer\s+(?:for\s+)?(\d+)\s*h(?:ours?)?\s*(?:and\s+)?(\d+)\s*m(?:in(?:ute)?s?)?$",
            # Cancel timer
            r"^(?:cancel|stop|clear|delete)\s+timer$",
            # Cancel all timers
            r"^(?:cancel|clear|stop)\s+all\s+timers$",
            # Timer status
            r"^(?:how much time (?:is )?left|timer status|check timer|time left|show timers?)$",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Execute timer command."""
        text = intent.text.lower().strip()

        # Cancel all timers
        if "all" in text and ("cancel" in text or "clear" in text or "stop" in text) and "timer" in text:
            return self._cancel_all_timers()

        # Cancel timer
        if (
            ("cancel" in text or "stop" in text or "clear" in text or "delete" in text)
            and "timer" in text
            and "all" not in text
        ):
            return self._cancel_timer()

        # Timer status
        if any(
            phrase in text
            for phrase in [
                "status",
                "time left",
                "how much time",
                "check timer",
                "show timer",
            ]
        ):
            return self._timer_status()

        # Set timer (must be last - catches the rest)
        return self._set_timer(text)

    def _set_timer(self, text: str) -> Response:
        """Set a timer by parsing duration from text."""
        try:
            from services.timer_service import get_timer_service, parse_duration

            duration_seconds = parse_duration(text)

            if duration_seconds is None or duration_seconds <= 0:
                return Response(
                    message="I couldn't understand the timer duration. Try 'timer 5 minutes'.",
                    success=False,
                )

            service = get_timer_service()
            timer_id = service.add_timer(duration_seconds)

            time_str = self._format_duration(duration_seconds)
            return Response(
                message=f"Timer set for {time_str}",
                data={"timer_id": timer_id, "duration_seconds": duration_seconds},
            )
        except Exception as e:
            self._logger.error("Failed to set timer: %s", e)
            return Response(message="Failed to set timer", success=False)

    def _cancel_timer(self) -> Response:
        """Cancel the most recent timer."""
        try:
            from services.timer_service import get_timer_service

            service = get_timer_service()

            if service.get_timer_count() == 0:
                return Response(message="No active timers to cancel")

            if service.cancel_most_recent():
                return Response(message="Timer cancelled")
            return Response(message="Couldn't cancel timer", success=False)
        except Exception as e:
            self._logger.error("Failed to cancel timer: %s", e)
            return Response(message="Failed to cancel timer", success=False)

    def _cancel_all_timers(self) -> Response:
        """Cancel all active timers."""
        try:
            from services.timer_service import get_timer_service

            service = get_timer_service()
            count = service.cancel_all()

            if count == 0:
                return Response(
                    message="No active timers to cancel",
                    data={"cancelled_count": 0},
                )
            return Response(
                message=f"Cancelled {count} timer{'s' if count != 1 else ''}",
                data={"cancelled_count": count},
            )
        except Exception as e:
            self._logger.error("Failed to cancel timers: %s", e)
            return Response(message="Failed to cancel timers", success=False)

    def _timer_status(self) -> Response:
        """Get status of active timers."""
        try:
            from services.timer_service import get_timer_service

            service = get_timer_service()
            timers = service.get_all_timers()

            if not timers:
                return Response(
                    message="No active timers",
                    data={"timers": []},
                )

            status_lines = []
            for timer in timers:
                remaining = timer.remaining_seconds
                if remaining <= 0:
                    time_str = "done!"
                else:
                    time_str = f"{self._format_duration(int(remaining))} left"
                status_lines.append(f"{timer.label}: {time_str}")

            message = "\n".join(status_lines)
            return Response(
                message=message,
                data={"timers": [t.to_dict() for t in timers], "count": len(timers)},
            )
        except Exception as e:
            self._logger.error("Failed to get timer status: %s", e)
            return Response(message="Failed to check timers", success=False)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds into a human-readable duration string."""
        if seconds >= 3600:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            time_str = f"{hours} hour{'s' if hours != 1 else ''}"
            if mins > 0:
                time_str += f" {mins} minute{'s' if mins != 1 else ''}"
        elif seconds >= 60:
            mins = seconds // 60
            secs = seconds % 60
            time_str = f"{mins} minute{'s' if mins != 1 else ''}"
            if secs > 0:
                time_str += f" {secs} second{'s' if secs != 1 else ''}"
        else:
            time_str = f"{seconds} second{'s' if seconds != 1 else ''}"
        return time_str

    async def validate(self) -> bool:
        """Timer skill is always available (no external dependencies)."""
        return True
