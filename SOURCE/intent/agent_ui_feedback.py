"""Agent UI feedback -- TTS speech, overlay management, progress updates."""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_TOOL_PROGRESS_PHRASES: dict[str, str] = {
    "file_read": "Reading files...",
    "file_write": "Writing files...",
    "run_command": "Running a command...",
    "web_search": "Searching the web...",
    "web_read": "Reading that page...",
    "system_info": "Checking your system...",
    "send_email": "Sending the email...",
    "gmail_inbox": "Checking your Gmail inbox...",
    "gmail_search": "Searching your emails...",
    "gmail_read": "Reading that email...",
    "gmail_draft_reply": "Drafting a reply...",
    "gmail_send": "Sending the email...",
    "gmail_daily_summary": "Getting your email summary...",
    "browser_navigate": "Opening that website...",
    "browser_screenshot": "Taking a screenshot...",
    "browser_run_script": "Running browser automation...",
    "browser_interact": "Clicking that element...",
    "browser_fill_form": "Filling in the form...",
    "browser_snapshot": "Reading the page...",
    "memory": "Accessing memory...",
    "schedule": "Managing schedule...",
    "phone": "Handling phone call...",
    "computer": "Using the desktop...",
    "self_manage": "Managing system...",
    "timer": "Managing timer...",
    "calendar": "Managing calendar...",
    "payment": "Processing payment...",
    "playlist": "Managing playlist...",
    "api_credential": "Managing credentials...",
    "spawn_subtask": "Delegating a subtask...",
}

_PROGRESS_THRESHOLD = 3.0


class AgentUIFeedback:
    """UI interaction methods for the agent executor."""

    def __init__(self, executor: Any) -> None:
        self._exec = executor

    async def speak(self, text: str) -> None:
        """Send progress text through the channel (preferred) or TTS fallback."""
        # Prefer channel if available
        if self._exec._channel is not None:
            try:
                await self._exec._channel.send(text)
                return
            except Exception as exc:
                logger.debug("Agent channel send failed: %s", exc)
        # Fallback to TTS
        if self._exec._tts is None:
            return
        try:
            speak_fn = getattr(self._exec._tts, "speak", None)
            if callable(speak_fn):
                await speak_fn(text)
        except Exception as exc:
            logger.debug("Agent TTS failed: %s", exc)

    def hide_overlay(self, *, outcome: str = "") -> None:
        """Hide the agentic overlay if it was shown.

        Parameters
        ----------
        outcome : str
            Forwarded to the overlay controller so the frontend can show
            a completion toast (``"done"``, ``"error"``, ``"cancelled"``).
        """
        if self._exec._overlay is None or not self._exec._overlay_shown:
            return
        try:
            self._exec._overlay.hide(outcome=outcome)
        except Exception as exc:
            logger.debug("Overlay hide failed: %s", exc)
        self._exec._overlay_shown = False

    def update_overlay_status(self, status: str) -> None:
        """Update the agentic overlay status without hiding it."""
        if self._exec._overlay is None or not self._exec._overlay_shown:
            return
        try:
            self._exec._overlay.update_agentic_status(status=status)
        except Exception as exc:
            logger.debug("Overlay status update failed: %s", exc)

    def update_overlay_phase(self, phase: str) -> None:
        """Update the overlay phase (acting/thinking/user_input)."""
        if self._exec._overlay is None or not self._exec._overlay_shown:
            return
        try:
            self._exec._overlay.update_agentic_status(phase=phase)
        except Exception as exc:
            logger.debug("Overlay phase update failed: %s", exc)

    async def delayed_progress(self, tool_name: str, delay: float = _PROGRESS_THRESHOLD) -> None:
        """Keep the delayed-progress hook silent for chat/TTS surfaces."""
        del tool_name, delay
