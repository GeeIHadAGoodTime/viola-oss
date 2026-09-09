"""Hold detection and pipeline muting during phone holds.

Uses LLM-based hold entry with periodic STT listen windows for return detection.
Muting STT stops the entire downstream chain (no transcription -> no SmartTurn ->
no LLM invocations -> no TTS). Single mechanism, full pipeline silence.
"""

from __future__ import annotations

import asyncio
import time

from core.logging_config import get_logger

logger = get_logger(__name__)


class HoldModeHandler:
    def __init__(
        self,
        llm,
        listen_interval_secs: float = 30.0,
        window_duration_secs: float = 5.0,
        max_hold_secs: float = 300.0,
        min_speech_words: int = 4,
    ):
        self._llm = llm
        self._listen_interval = listen_interval_secs
        self._window_duration = window_duration_secs
        self._max_hold = max_hold_secs
        self._min_speech_words = min_speech_words
        self._state = "NORMAL"
        self._hold_start: float | None = None
        self._timer_task: asyncio.Task | None = None
        self._windows_opened = 0

    async def enter_hold(self, params) -> None:
        """LLM function handler for enter_hold_mode tool.

        Uses new FunctionCallParams API (single param).
        """
        from pipecat.frames.frames import STTMuteFrame

        if self._state != "NORMAL":
            await params.result_callback({"status": "already_in_hold_mode"})
            return

        logger.info("Hold mode: ENTERING — muting STT")
        self._state = "HOLD_ACTIVE"
        self._hold_start = time.time()
        self._windows_opened = 0

        # Mute STT — stops entire downstream chain
        await params.llm.push_frame(STTMuteFrame(mute=True))

        # mt-ok: hold handler instance is per-call (per-user); bg task inherits via self
        self._timer_task = asyncio.create_task(self._hold_timer_loop())

        await params.result_callback({"status": "hold_mode_entered"})

    async def _hold_timer_loop(self):
        try:
            while self._state == "HOLD_ACTIVE":
                elapsed = time.time() - self._hold_start
                if elapsed >= self._max_hold:
                    logger.info("Hold mode: TIMEOUT after %.0fs", elapsed)
                    await self._exit_hold(source="hold_timeout")
                    return

                await asyncio.sleep(self._listen_interval)

                if self._state != "HOLD_ACTIVE":
                    return

                await self._open_listen_window()
        except asyncio.CancelledError:
            pass

    async def _open_listen_window(self):
        from pipecat.frames.frames import STTMuteFrame

        self._state = "LISTEN_WINDOW"
        self._windows_opened += 1
        logger.info("Hold mode: listen window #%d open", self._windows_opened)

        await self._llm.push_frame(STTMuteFrame(mute=False))
        await asyncio.sleep(self._window_duration)

        # If still in LISTEN_WINDOW (on_transcription didn't transition us), close it
        if self._state == "LISTEN_WINDOW":
            logger.info("Hold mode: listen window closed — no speech detected")
            await self._llm.push_frame(STTMuteFrame(mute=True))
            self._state = "HOLD_ACTIVE"

    async def on_transcription(self, text: str):
        """Called when STT produces transcription during a listen window."""
        if self._state != "LISTEN_WINDOW":
            return

        words = text.strip().split()
        if len(words) < self._min_speech_words:
            return  # Garbage from hold music

        logger.info("Hold mode: human detected — '%s'", text[:80])
        await self._exit_hold(source="recipient_returned", transcription=text)

    async def _exit_hold(self, *, source: str, transcription: str = ""):
        from pipecat.frames.frames import LLMMessagesAppendFrame, STTMuteFrame

        self._state = "NORMAL"
        hold_duration = time.time() - self._hold_start if self._hold_start else 0

        # Do NOT cancel the timer task if we ARE the timer task. On a hold
        # timeout, _hold_timer_loop calls _exit_hold, so self._timer_task is the
        # currently-running task; cancelling it here schedules a CancelledError
        # that fires at the very next await (the STTMuteFrame unmute below),
        # leaving STT muted and never delivering the hold_returned context — the
        # call wedges in dead air until the max-duration hangup, still billing.
        # The loop returns right after this call, so the task ends cleanly on its
        # own; only the recipient_returned path (a different task) needs the
        # explicit cancel.
        current_task = asyncio.current_task()
        if self._timer_task and not self._timer_task.done() and self._timer_task is not current_task:
            self._timer_task.cancel()

        # Ensure STT is unmuted
        await self._llm.push_frame(STTMuteFrame(mute=False))

        context_lines = [
            "phone_event: hold_returned",
            "source: %s" % source,
            "hold_duration_seconds: %.0f" % hold_duration,
            "recipient_audio_observed: %s" % ("true" if transcription else "false"),
        ]
        if transcription:
            context_lines.append("recipient_transcript: %s" % transcription)

        # Attach raw call state so the model can decide the next move.
        await self._llm.push_frame(
            LLMMessagesAppendFrame(messages=[{"role": "system", "content": "\n".join(context_lines)}])
        )

        logger.info(
            "Hold mode: EXITED after %.0fs, %d listen windows",
            hold_duration,
            self._windows_opened,
        )

    @property
    def is_on_hold(self) -> bool:
        return self._state in ("HOLD_ACTIVE", "LISTEN_WINDOW")

    @property
    def hold_duration(self) -> float:
        if self._hold_start and self.is_on_hold:
            return time.time() - self._hold_start
        return 0.0

    @property
    def windows_opened(self) -> int:
        return self._windows_opened

    async def cleanup(self):
        """Cancel all tasks. Call in finally block of _run_call()."""
        self._state = "NORMAL"
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
