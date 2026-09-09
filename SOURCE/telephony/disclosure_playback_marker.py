"""Post-TTS disclosure playback marker for phone calls."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import LLMFullResponseEndFrame, TTSAudioRawFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - Pipecat is optional in some test envs
    PIPECAT_AVAILABLE = False
    FrameDirection = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.
    FrameProcessor = object  # type: ignore[misc, assignment] # PHONE-01: Pipecat is optional in local test environments.
    LLMFullResponseEndFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.
    TTSAudioRawFrame = None  # type: ignore[assignment] # PHONE-01: Pipecat is optional in local test environments.


DisclosureStateCallback = Callable[[], bool]
DisclosureCallback = Callable[[], None | Awaitable[None]]


class DisclosurePlaybackMarker(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc] # PHONE-01: Pipecat fallback base.
    """Mark disclosure as spoken only after the first accepted TTS turn drains."""

    def __init__(
        self,
        *,
        is_disclosure_confirmed: DisclosureStateCallback,
        on_disclosure_spoken: DisclosureCallback,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name="disclosure_playback_marker")
        self._is_disclosure_confirmed = is_disclosure_confirmed
        self._on_disclosure_spoken = on_disclosure_spoken
        self._saw_confirmed_tts_audio = False
        self._marked = False

    @property
    def playback_marked(self) -> bool:
        return self._marked

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if not PIPECAT_AVAILABLE:
            return

        await super().process_frame(frame, direction)

        confirmed = bool(self._is_disclosure_confirmed())
        if (
            not self._marked
            and confirmed
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TTSAudioRawFrame)
        ):
            self._saw_confirmed_tts_audio = True

        await self.push_frame(frame, direction)

        if (
            not self._marked
            and confirmed
            and self._saw_confirmed_tts_audio
            and direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, LLMFullResponseEndFrame)
        ):
            await self._mark_disclosure_spoken()

    async def _mark_disclosure_spoken(self) -> None:
        self._marked = True
        try:
            result = self._on_disclosure_spoken()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("Recording disclosure playback callback failed")
