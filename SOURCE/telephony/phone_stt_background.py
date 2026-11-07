"""Transcribe a phone clip without stopping the audio behind it.

Pipecat awaits ``run_stt`` inside the STT processor, and VAD runs further down the same
line in the user aggregator. So every clip being transcribed held all later audio in
the STT queue: on call 3745134a no VAD result appeared from 22:51:38 to 22:51:46, then
six "stopped speaking" events landed in two seconds. Turn-taking was blind for exactly
as long as transcription took.

With this, ``run_stt`` returns at once and the transcript is pushed when it is ready.
Clips stay in order: one is transcribed at a time, first come first served.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class OffPathTranscriber:
    """Runs one call's transcriptions in order, away from the frame path."""

    def __init__(
        self,
        transcribe: Callable[[bytes], Awaitable[str]],
        frames_for: Callable[[str], AsyncIterator[Any]],
        push_frame: Callable[[Any], Awaitable[None]],
    ) -> None:
        self._transcribe = transcribe
        self._frames_for = frames_for
        self._push_frame = push_frame
        self._turn = asyncio.Lock()
        self._running: set[asyncio.Task[None]] = set()

    def submit(self, audio: bytes) -> None:
        task = asyncio.ensure_future(self._run(audio))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, audio: bytes) -> None:
        async with self._turn:
            try:
                text = await self._transcribe(audio)
                async for frame in self._frames_for(text):
                    await self._push_frame(frame)
            except asyncio.CancelledError:
                raise
            # One bad clip must not stop the clips behind it.
            except Exception:  # noqa: BLE001, RUF100
                logger.exception("Phone transcription failed for one clip")

    async def finish_pending(self) -> None:
        """Wait for every clip submitted so far."""
        while self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    def cancel(self) -> None:
        for task in list(self._running):
            task.cancel()
