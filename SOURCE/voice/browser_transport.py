"""Browser WebSocket Transport for Pipecat — bridges browser mic/speaker with Pipecat frames.

A browser client sends raw 16-bit signed PCM audio (16 kHz mono) as binary
WebSocket messages.  This transport converts those to Pipecat
InputAudioRawFrame and converts Pipecat OutputAudioRawFrame back to binary
WebSocket messages for browser playback.

Wire format (browser -> server):
    Binary: Raw Int16 PCM bytes (16 kHz, mono, little-endian)

Wire format (server -> browser):
    Binary: Raw Int16 PCM bytes (16 kHz, mono, little-endian)
    Text/JSON: State updates ({"type": "state", "state": "listening"})

Unlike the Telnyx transport (JSON + base64), this transport uses raw binary
for minimal latency.  The transport does NOT start its own WebSocket server;
it receives an already-accepted FastAPI WebSocket from the endpoint handler.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Pipecat imports (guarded)
# ---------------------------------------------------------------------------

try:
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

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BROWSER_SAMPLE_RATE = 16000
BROWSER_CHANNELS = 1
BYTES_PER_SAMPLE = 2  # 16-bit PCM
# 20 ms chunks at 16 kHz mono 16-bit = 640 bytes
CHUNK_DURATION_MS = 20
SILENCE_CHUNK_BYTES = BROWSER_SAMPLE_RATE * BYTES_PER_SAMPLE * BROWSER_CHANNELS * CHUNK_DURATION_MS // 1000


# ---------------------------------------------------------------------------
# Input transport — browser WebSocket -> Pipecat frames
# ---------------------------------------------------------------------------


class BrowserInputTransport(BaseInputTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Reads raw PCM from a FastAPI WebSocket and emits Pipecat InputAudioRawFrames."""

    def __init__(
        self,
        websocket: Any,
        params: TransportParams,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._websocket = websocket
        self._reader_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected = False

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if not self._reader_task:
            self._reader_task = self.create_task(self._read_loop())
        self._connected = True
        await self.set_transport_ready(frame)
        logger.info("BrowserInputTransport started")

    async def stop(self, frame: EndFrame) -> None:
        self._stop_event.set()
        if self._reader_task:
            await self._reader_task
            self._reader_task = None
        self._connected = False
        await super().stop(frame)
        logger.info("BrowserInputTransport stopped")

    async def cancel(self, frame: CancelFrame) -> None:
        self._stop_event.set()
        if self._reader_task:
            await self.cancel_task(self._reader_task)
            self._reader_task = None
        self._connected = False
        await super().cancel(frame)

    async def cleanup(self) -> None:
        await super().cleanup()

    async def _read_loop(self) -> None:
        """Read binary PCM from the browser WebSocket and push as Pipecat frames.

        Also emits silence chunks at 20 ms intervals when no data arrives,
        so VAD gets a continuous stream and can detect speech boundaries.
        """
        silence_chunk = b"\x00" * SILENCE_CHUNK_BYTES
        pace_seconds = CHUNK_DURATION_MS / 1000.0

        while not self._stop_event.is_set():
            try:
                # Use a short timeout so we emit silence if browser is quiet
                raw = await asyncio.wait_for(
                    self._websocket.receive(),
                    timeout=pace_seconds,
                )

                if raw.get("type") == "websocket.disconnect":
                    logger.info("Browser WebSocket disconnected")
                    break

                # Binary data = PCM audio
                audio_bytes = raw.get("bytes")
                if audio_bytes:
                    frame = InputAudioRawFrame(
                        audio=audio_bytes,
                        sample_rate=BROWSER_SAMPLE_RATE,
                        num_channels=BROWSER_CHANNELS,
                    )
                    await self.push_audio_frame(frame)

                # Text messages are control messages (handled by endpoint)
                # We just skip them here

            except TimeoutError:
                # No data from browser — emit silence so VAD stays active
                frame = InputAudioRawFrame(
                    audio=silence_chunk,
                    sample_rate=BROWSER_SAMPLE_RATE,
                    num_channels=BROWSER_CHANNELS,
                )
                await self.push_audio_frame(frame)

            except Exception:
                if not self._stop_event.is_set():
                    logger.exception("BrowserInputTransport read error")
                break

        logger.info("BrowserInputTransport read loop ended")


# ---------------------------------------------------------------------------
# Output transport — Pipecat frames -> browser WebSocket
# ---------------------------------------------------------------------------


class BrowserOutputTransport(BaseOutputTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Sends Pipecat OutputAudioRawFrames as raw binary PCM to the browser."""

    def __init__(
        self,
        websocket: Any,
        params: TransportParams,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._websocket = websocket
        self._initialized = False

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self.set_transport_ready(frame)
        logger.info("BrowserOutputTransport started")

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        logger.info("BrowserOutputTransport stopped")

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send TTS audio back to the browser as raw binary PCM.

        Overrides BaseOutputTransport.write_audio_frame so Pipecat's
        _audio_task_handler delivers audio to the browser instead of
        discarding it (base class returns False).
        """
        try:
            await self._websocket.send_bytes(frame.audio)
            return True
        except Exception:
            logger.warning("Failed to send audio to browser WebSocket")
            return False

    async def send_state(self, state: str) -> None:
        """Send a state update to the browser (listening, thinking, speaking)."""
        import json

        try:
            await self._websocket.send_text(json.dumps({"type": "state", "state": state}))
        except Exception:
            logger.warning("Failed to send state to browser: %s", state)


# ---------------------------------------------------------------------------
# Combined transport
# ---------------------------------------------------------------------------


class WebSocketVoiceTransport(BaseTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Pipecat transport that bridges browser WebSocket audio.

    Drop-in replacement for TelnyxTransport in the Pipecat pipeline.
    Instead of starting a WebSocket server and waiting for Telnyx to connect,
    this transport receives an already-accepted FastAPI WebSocket.

    Usage::

        transport = WebSocketVoiceTransport(websocket)

        pipeline = Pipeline([
            transport.input(),   # receives mic audio from browser
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),  # sends TTS audio to browser
            context_aggregator.assistant(),
        ])
    """

    def __init__(self, websocket: Any) -> None:
        if not PIPECAT_AVAILABLE:
            raise ImportError("pipecat-ai is required: pip install pipecat-ai")

        super().__init__()

        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=BROWSER_SAMPLE_RATE,
            audio_in_channels=BROWSER_CHANNELS,
            audio_out_enabled=True,
            audio_out_sample_rate=BROWSER_SAMPLE_RATE,
            audio_out_channels=BROWSER_CHANNELS,
        )

        self._input_transport = BrowserInputTransport(
            websocket=websocket,
            params=params,
        )
        self._output_transport = BrowserOutputTransport(
            websocket=websocket,
            params=params,
        )

    def input(self) -> BrowserInputTransport:
        """Return the input transport for the Pipecat pipeline."""
        return self._input_transport

    def output(self) -> BrowserOutputTransport:
        """Return the output transport for the Pipecat pipeline."""
        return self._output_transport
