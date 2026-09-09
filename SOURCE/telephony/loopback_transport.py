"""Loopback transport for Pipecat — in-memory audio bridge for testing.

Replaces TelnyxTransport with asyncio.Queue-based audio I/O so two
Pipecat pipelines can talk to each other without any network layer.

Audio format mirrors production telephony EXACTLY: 16-bit PCM, mono, at the
native inbound telephone rate (8 kHz). The rate is SOURCED from the production
constant ``telephony.call_manager.PHONE_INBOUND_SAMPLE_RATE``, never a local
literal — so the rig exercises the same 8 kHz inbound stream a real cellular
call delivers, and the same 8k->16k whisper upsample / 16 kHz SmartTurn path
that historically garbled real calls. A local 16 kHz literal here is what let
the "Viola can't hear / garbles the recipient" class be physically impossible to
reproduce offline (audit 2026-06-25). Frame types match production exactly
(InputAudioRawFrame / OutputAudioRawFrame).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from core.logging_config import get_logger

# Source the inbound rate from the ONE production constant so the rig can never
# drift to an easier (wideband) audio world than a real call. PHONE_INBOUND_SAMPLE_RATE
# is the native Telnyx PCMU rate (8 kHz); flipping it here without sourcing it was
# the highest-leverage oracle lie in the 2026-06-25 audit.
from telephony.call_manager import PHONE_INBOUND_SAMPLE_RATE

logger = get_logger(__name__)

# Match production telephony settings — rate sourced from the prod constant.
LOOPBACK_SAMPLE_RATE = PHONE_INBOUND_SAMPLE_RATE
LOOPBACK_CHANNELS = 1
# 20ms chunks at the inbound rate, mono, 16-bit. At 8 kHz this is 320 bytes;
# the math derives from LOOPBACK_SAMPLE_RATE so it stays correct if prod ever
# changes the inbound rate.
CHUNK_DURATION_MS = 20
CHUNK_BYTES = LOOPBACK_SAMPLE_RATE * 2 * LOOPBACK_CHANNELS * CHUNK_DURATION_MS // 1000


class LoopbackInputTransport(BaseInputTransport):
    """Reads audio frames from an asyncio.Queue and pushes them into the pipeline.

    Paces delivery at ~20ms per chunk to simulate real telephony timing.
    """

    def __init__(
        self,
        input_queue: asyncio.Queue,
        params: TransportParams,
        name: str = "loopback-in",
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._input_queue = input_queue
        self._reader_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._name = name

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if not self._reader_task:
            self._reader_task = self.create_task(self._read_loop())
        await self.set_transport_ready(frame)
        logger.debug("LoopbackInput[%s] started", self._name)

    async def stop(self, frame: EndFrame) -> None:
        self._stop_event.set()
        if self._reader_task:
            await self._reader_task
            self._reader_task = None
        await super().stop(frame)
        logger.debug("LoopbackInput[%s] stopped", self._name)

    async def cancel(self, frame: CancelFrame) -> None:
        self._stop_event.set()
        if self._reader_task:
            await self.cancel_task(self._reader_task)
            self._reader_task = None
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await super().cleanup()

    async def _read_loop(self) -> None:
        """Emit continuous audio at ~20ms intervals: real audio or silence.

        VAD requires a continuous audio stream to detect speech start/stop
        transitions. If we only push frames when TTS produces audio, VAD
        gets stuck in "speaking" because it never sees silence after speech.
        """
        pace_seconds = CHUNK_DURATION_MS / 1000.0
        silence_chunk = b"\x00" * CHUNK_BYTES

        while not self._stop_event.is_set():
            try:
                audio_bytes: bytes = self._input_queue.get_nowait()
            except asyncio.QueueEmpty:
                audio_bytes = silence_chunk

            if audio_bytes is None:
                # Poison pill — other side ended
                logger.debug("LoopbackInput[%s] received end signal", self._name)
                break

            frame = InputAudioRawFrame(
                audio=audio_bytes,
                sample_rate=LOOPBACK_SAMPLE_RATE,
                num_channels=LOOPBACK_CHANNELS,
            )
            await self.push_audio_frame(frame)

            # Pace at real-time rate to maintain proper VAD timing
            await asyncio.sleep(pace_seconds)


class LoopbackOutputTransport(BaseOutputTransport):
    """Receives audio frames from the pipeline and writes them to an asyncio.Queue."""

    def __init__(
        self,
        output_queue: asyncio.Queue,
        params: TransportParams,
        name: str = "loopback-out",
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._output_queue = output_queue
        self._name = name

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        logger.debug("LoopbackOutput[%s] started", self._name)

    async def stop(self, frame: EndFrame) -> None:
        # Send poison pill so the other side's input knows we're done
        try:
            self._output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        await super().stop(frame)
        logger.debug("LoopbackOutput[%s] stopped", self._name)

    async def cancel(self, frame: CancelFrame) -> None:
        try:
            self._output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        await super().cancel(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Write TTS output audio to the queue for the other pipeline to consume."""
        try:
            self._output_queue.put_nowait(frame.audio)
            return True
        except asyncio.QueueFull:
            logger.warning("LoopbackOutput[%s] queue full, dropping frame", self._name)
            return False


class LoopbackTransport(BaseTransport):
    """Pipecat transport that sends/receives audio via asyncio.Queues.

    Drop-in replacement for TelnyxTransport in test scenarios.

    Args:
        input_queue: Queue to READ audio from (other side's TTS output).
        output_queue: Queue to WRITE audio to (this side's TTS output).
        name: Label for logging (e.g. "viola" or "business").
    """

    def __init__(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
        name: str = "loopback",
    ) -> None:
        super().__init__()
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=LOOPBACK_SAMPLE_RATE,
            audio_in_channels=LOOPBACK_CHANNELS,
            audio_out_enabled=True,
            audio_out_sample_rate=LOOPBACK_SAMPLE_RATE,
            audio_out_channels=LOOPBACK_CHANNELS,
        )
        self._input_transport = LoopbackInputTransport(
            input_queue=input_queue,
            params=params,
            name="%s-in" % name,
        )
        self._output_transport = LoopbackOutputTransport(
            output_queue=output_queue,
            params=params,
            name="%s-out" % name,
        )

    def input(self) -> LoopbackInputTransport:
        return self._input_transport

    def output(self) -> LoopbackOutputTransport:
        return self._output_transport
