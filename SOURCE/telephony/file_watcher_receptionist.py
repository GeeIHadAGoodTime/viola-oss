"""File-IO LLM seam for receptionist pipelines.

This module provides two Pipecat FrameProcessors that let an external operator
play the receptionist on a phone call without an LLM service in the pipeline:

- ``TranscriptWriter`` intercepts finalized ``TranscriptionFrame`` (whatever
  Whisper produces from the inbound audio) and writes them as JSON lines to a
  file.
- ``FileSpeechInjector`` tails a file for new lines and emits ``TTSSpeakFrame``
  for each, which feeds Kokoro TTS without going through any LLM.

Together these replace the LLM seam in a phone receptionist pipeline. The
operator (e.g. Claude in a chat session) reads transcribed Viola turns from
the incoming file and writes responses to the outgoing file. The receptionist
pipeline runs real Whisper STT + real Kokoro TTS on real audio in both
directions; the only thing missing is the LLM, which the operator supplies.

Used by:
- ``LoopbackPhoneCallSession`` with ``receptionist_mode='file_watcher_bot'``
  for in-process smoke testing.
- ``tools/devbench/phone_receptionist_pipeline.py`` for standalone Telnyx-bound runs (DEV BENCH ONLY).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from pipecat.frames.frames import ClientConnectedFrame, StartFrame, TranscriptionFrame, TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False
    FrameProcessor = object  # type: ignore[misc, assignment]
    ClientConnectedFrame = None  # type: ignore[misc, assignment]
    StartFrame = None  # type: ignore[misc, assignment]


END_SENTINEL = "__END_CALL__"


def _is_session_reset_frame(frame: Any) -> bool:
    return (StartFrame is not None and isinstance(frame, StartFrame)) or (
        ClientConnectedFrame is not None and isinstance(frame, ClientConnectedFrame)
    )


class TranscriptWriter(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Writes finalized TranscriptionFrame text as JSON lines.

    One line per finalized transcription. Schema::

        {"turn": <int>, "ts": "<iso8601 utc>", "text": "<transcription>"}

    The receptionist operator tails this file to see what Viola said.
    """

    def __init__(self, output_path: Path, *, name: str = "transcript_writer") -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name=name)
        self._output_path = Path(output_path)
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate so each session starts clean.
        self._output_path.write_text("", encoding="utf-8")
        self._turn_idx = 0

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if PIPECAT_AVAILABLE:
            await super().process_frame(frame, direction)
            if _is_session_reset_frame(frame):
                self._reset_session_state()
            self._capture_frame(frame)
            await self.push_frame(frame, direction)

    def _reset_session_state(self) -> None:
        self._turn_idx = 0
        try:
            self._output_path.write_text("", encoding="utf-8")
        except Exception:
            logger.exception("TranscriptWriter failed to truncate %s on session start", self._output_path)

    def _capture_frame(self, frame: Any) -> None:
        if not isinstance(frame, TranscriptionFrame):
            return
        text = (getattr(frame, "text", "") or "").strip()
        if not text:
            return
        if getattr(frame, "finalized", True) is False:
            return
        self._turn_idx += 1
        record = {
            "turn": self._turn_idx,
            "ts": datetime.now(tz=UTC).isoformat(),
            "text": text,
        }
        try:
            with self._output_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()
            logger.info("TranscriptWriter: turn %d <- %s", self._turn_idx, text[:120])
        except Exception:
            logger.exception("TranscriptWriter failed to persist turn")


class FileSpeechInjector(FrameProcessor if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Tails a file and emits TTSSpeakFrame for each new non-empty line.

    The watcher runs as a background asyncio task that starts when the first
    frame is processed (so the FrameProcessor's event loop and queue are
    ready). Lines equal to ``END_SENTINEL`` trigger graceful shutdown.

    The injector pushes each line downstream as a TTSSpeakFrame which the
    Kokoro TTS service speaks directly without any LLM context aggregation.
    """

    def __init__(
        self,
        input_path: Path,
        *,
        name: str = "file_speech_injector",
        poll_interval_s: float = 0.2,
    ) -> None:
        if PIPECAT_AVAILABLE:
            super().__init__(name=name)
        self._input_path = Path(input_path)
        self._input_path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate so each session starts clean.
        self._input_path.write_text("", encoding="utf-8")
        self._poll_interval_s = poll_interval_s
        self._last_position = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = False
        self._lines_spoken = 0

    @property
    def lines_spoken(self) -> int:
        return self._lines_spoken

    async def process_frame(self, frame: Any, direction: Any) -> None:
        if PIPECAT_AVAILABLE:
            await super().process_frame(frame, direction)
            if _is_session_reset_frame(frame):
                await self._reset_session_state()
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._watch_loop())
            await self.push_frame(frame, direction)

    async def _reset_session_state(self) -> None:
        await self._cancel_watch_task()
        self._stop = False
        self._last_position = 0
        self._lines_spoken = 0
        try:
            self._input_path.write_text("", encoding="utf-8")
        except Exception:
            logger.exception("FileSpeechInjector failed to truncate %s on session start", self._input_path)

    async def _watch_loop(self) -> None:
        try:
            while not self._stop:
                new_data = await asyncio.to_thread(self._read_new_data)
                if new_data:
                    for raw_line in new_data.splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line == END_SENTINEL:
                            logger.info("FileSpeechInjector: end sentinel received")
                            self._stop = True
                            break
                        await self._speak_line(line)
                await asyncio.sleep(self._poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("FileSpeechInjector watch loop failed")

    def _read_new_data(self) -> str:
        try:
            with self._input_path.open(encoding="utf-8") as f:
                f.seek(self._last_position)
                new_data = f.read()
                self._last_position = f.tell()
            return new_data
        except FileNotFoundError:
            return ""

    async def _speak_line(self, line: str) -> None:
        logger.info("FileSpeechInjector: speaking: %s", line[:120])
        self._lines_spoken += 1
        try:
            await self.push_frame(TTSSpeakFrame(text=line))
        except Exception:
            logger.exception("FileSpeechInjector failed to push TTSSpeakFrame")

    async def stop_watching(self) -> None:
        self._stop = True
        await self._cancel_watch_task()

    async def _cancel_watch_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
