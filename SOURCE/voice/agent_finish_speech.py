"""Say once that a background task the user is no longer waiting on has ended.

1.0.5 build-11 laptop (item 14): a background essay agent completed, its card
opened, and nothing was said; the next spoken line was the reply to the user's
next command. The registry (``services.agent_runtime.registry``) only logged the
end, and nothing else ever announced it.

The line is fixed, with no model call and nothing of the task in it:

- completed: "Your background task is done. The result is on the screen."
- failed: "Your background task stopped before it finished."

Who is told about (one line per job the user started, never per helper):

- a job the executor marked ``announce_when_done`` (a top-level turn's
  ``start_agent`` or resumed agent; never a scheduler run, which speaks its own
  result, and never a helper of another agent);
- that no turn is still waiting on (``turn_waiting`` is False: started in the
  background, or a foreground helper the turn stopped waiting on);
- that ended ``completed`` or ``failed`` (``killed`` is the user's own cancel).

When it is spoken: only on the desktop surface, never with speech off or the
voice muted, not in quiet hours, not while a call this device placed is live. A
job asked for by voice is announced; a typed one (or one whose origin is not
known) only when "Speak Responses Aloud" is on, the same rule as a typed reply
(``core.reply_speech_settings``).

How: through the TTS engine's own ``speak`` (the path that records her words in
``audio_core.own_playback``, so the wake loop and the voice turn know the words
as hers). Lines wait for any speech in progress to end and are said one at a
time, so two jobs ending together give two lines, one after the other.

Every decision writes one log line: agent id, status, spoken, and the reason
when not spoken. Never the task or its result.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

DONE_LINE = "Your background task is done. The result is on the screen."
FAILED_LINE = "Your background task stopped before it finished."

# How long a line waits for speech already in progress before it is said anyway
# (the engine still queues it behind the current reply). A stuck "speaking" flag
# must not hold the line forever.
_MAX_WAIT_FOR_QUIET_S = 120.0
_QUIET_POLL_S = 0.25
# Her voice counts as still going this long after the last word.
_SPEECH_TAIL_MS = 500.0


def _viola_is_speaking() -> bool:
    from voice.wake_detector.wake_decision_policy import viola_self_speech_active

    return viola_self_speech_active(tail_ms=_SPEECH_TAIL_MS)


def _on_desktop() -> bool:
    from ui.core.security import is_desktop_surface

    return is_desktop_surface()


def _in_quiet_hours(user_id: str) -> bool:
    from core.quiet_hours import is_quiet_hours

    return is_quiet_hours(user_id=user_id)


def _call_is_live() -> bool:
    from core.live_call_state import any_call_live

    return any_call_live()


def _speech_settings(user_id: str) -> Any:
    from core.reply_speech_settings import read_reply_speech_settings

    return read_reply_speech_settings(user_id)


class AgentFinishSpeech:
    """The registry's finish listener on a machine that can speak."""

    def __init__(
        self,
        tts: Any,
        *,
        is_speaking: Callable[[], bool] = _viola_is_speaking,
        on_desktop: Callable[[], bool] = _on_desktop,
        in_quiet_hours: Callable[[str], bool] = _in_quiet_hours,
        call_is_live: Callable[[], bool] = _call_is_live,
        speech_settings: Callable[[str], Any] = _speech_settings,
        max_wait_for_quiet_s: float = _MAX_WAIT_FOR_QUIET_S,
        quiet_poll_s: float = _QUIET_POLL_S,
    ) -> None:
        self._tts = tts
        self._is_speaking = is_speaking
        self._on_desktop = on_desktop
        self._in_quiet_hours = in_quiet_hours
        self._call_is_live = call_is_live
        self._speech_settings = speech_settings
        self._max_wait_for_quiet_s = max_wait_for_quiet_s
        self._quiet_poll_s = quiet_poll_s
        self._line_lock: asyncio.Lock | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._announced: set[str] = set()

    # -- registry listener ---------------------------------------------------
    def __call__(self, task: Any) -> None:
        agent_id = str(getattr(task, "agent_id", "") or "")
        status = str(getattr(task, "status", "") or "")
        reason = self._job_reason(task)
        if reason:
            self._log(agent_id, status, spoken=False, reason=reason)
            return
        if agent_id in self._announced:
            self._log(agent_id, status, spoken=False, reason="already_announced")
            return
        self._announced.add(agent_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._log(agent_id, status, spoken=False, reason="no_event_loop")
            return
        line = DONE_LINE if status == "completed" else FAILED_LINE
        work = loop.create_task(self._announce(task, agent_id, status, line))
        self._tasks.add(work)
        work.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for every line already queued (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- decisions -----------------------------------------------------------
    @staticmethod
    def _job_reason(task: Any) -> str:
        """Why this job's end is not the user's to hear about, or "" when it is."""
        status = str(getattr(task, "status", "") or "")
        if status == "killed":
            return "cancelled"
        if status not in {"completed", "failed"}:
            return "not_finished"
        if not getattr(task, "announce_when_done", False):
            # A scheduler run (it speaks its own result) or a helper of another agent.
            return "not_a_user_job"
        if getattr(task, "turn_waiting", False):
            # The turn that waited on it replies with its answer.
            return "turn_waited"
        return ""

    def _speech_reason(self, task: Any) -> str:
        """Why the line may not be spoken right now, or "" when it may."""
        user_id = str(getattr(task, "user_id", "") or "")
        if not self._on_desktop():
            return "not_desktop"
        settings = self._speech_settings(user_id)
        if not settings.tts_enabled:
            return "tts_off"
        if settings.voice_muted:
            return "voice_muted"
        origin = str(getattr(task, "origin", "") or "").lower()
        if origin != "voice" and not settings.speak_all_replies:
            return "typed_and_speak_responses_aloud_off"
        if self._in_quiet_hours(user_id):
            return "quiet_hours"
        if self._call_is_live():
            return "phone_call_live"
        return ""

    # -- speaking ------------------------------------------------------------
    async def _wait_for_quiet(self) -> None:
        deadline = time.monotonic() + self._max_wait_for_quiet_s
        while time.monotonic() < deadline:
            try:
                if not self._is_speaking():
                    return
            # A state read that fails must not hold the line.
            except Exception:  # noqa: BLE001, RUF100
                return
            await asyncio.sleep(self._quiet_poll_s)

    async def _announce(self, task: Any, agent_id: str, status: str, line: str) -> None:
        if self._line_lock is None:
            self._line_lock = asyncio.Lock()
        async with self._line_lock:
            await self._wait_for_quiet()
            try:
                reason = self._speech_reason(task)
            # Settings that cannot be read: say nothing rather than guess.
            except Exception:  # noqa: BLE001, RUF100
                logger.debug("Background task line: settings unreadable", exc_info=True)
                reason = "settings_unreadable"
            if reason:
                self._log(agent_id, status, spoken=False, reason=reason)
                return
            speak = getattr(self._tts, "speak", None)
            if not callable(speak):
                self._log(agent_id, status, spoken=False, reason="no_tts")
                return
            try:
                spoken = speak(line)
                if asyncio.iscoroutine(spoken):
                    await spoken
            # An audio device can fail any way at all; the card still holds the result.
            except Exception as exc:  # noqa: BLE001, RUF100
                self._log(agent_id, status, spoken=False, reason="tts_failed:%s" % type(exc).__name__)
                return
            self._log(agent_id, status, spoken=True, reason="")

    @staticmethod
    def _log(agent_id: str, status: str, *, spoken: bool, reason: str) -> None:
        if spoken:
            logger.info("Background task line: agent_id=%s status=%s spoken=true", agent_id, status)
        else:
            logger.info(
                "Background task line: agent_id=%s status=%s spoken=false reason=%s",
                agent_id,
                status,
                reason,
            )


def install_agent_finish_speech(tts: Any, *, registry: Any | None = None) -> AgentFinishSpeech | None:
    """Make the desktop say when a background task ends. Returns the listener."""
    if tts is None:
        return None
    if registry is None:
        from services.agent_runtime.registry import agent_registry as registry
    listener = AgentFinishSpeech(tts)
    registry.set_finish_listener(listener)
    logger.info("Background task line installed")
    return listener
