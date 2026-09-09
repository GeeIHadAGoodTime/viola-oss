"""
Native Mic Streamer — captures microphone audio and streams to hub.

Speaks the same WebSocket protocol as the browser voice_stream endpoint:
  - Text: ``{"type": "start_listening"}`` to begin wake detection
  - Binary: raw 16 kHz mono int16 PCM frames
  - Receives: ``{"type": "wake_detected", ...}`` and ``{"type": "command_result", ...}``
  - Receives: binary ``TTS\\x00`` + PCM bytes for TTS playback

This replaces the browser microphone capture with native sounddevice,
enabling headless Pi Zero operation without a browser.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import numpy as np

from core.constants import LOCALHOST, LOCALHOST_NAME, SAMPLE_RATE_16K
from core.logging_config import get_logger

if TYPE_CHECKING:
    from spoke.tts_receiver import TTSReceiver

logger = get_logger(__name__)

# Capture parameters
FRAME_DURATION_MS = 80  # match browser spoke chunk size
FRAME_SAMPLES = int(SAMPLE_RATE_16K * FRAME_DURATION_MS / 1000)  # 1280
RECONNECT_DELAY_S = 3.0
MAX_RECONNECT_DELAY_S = 30.0

# Spoke-credential handshake header. Must equal
# ``ui.security.spoke_credentials.SPOKE_TOKEN_HEADER_NAME`` (the hub's
# verifier). Defined locally so the slim spoke does not import the heavy
# ``ui.security`` package (FastAPI/auth) — see viola_spoke.py import budget.
SPOKE_TOKEN_HEADER_NAME = "X-Spoke-Token"  # nosec B105

# Loopback hosts where plaintext ``ws://`` is acceptable: the audio never
# leaves the machine, so there is no on-wire eavesdropper. Every other host
# (the real LAN spoke→hub case) MUST use encrypted ``wss://`` (SEC-048).
_LOOPBACK_HOSTS = frozenset({LOCALHOST, LOCALHOST_NAME, "::1"})


class MicStreamer:
    """Captures mic audio via sounddevice and streams to hub over WebSocket.

    Transport security (SEC-048, 2026-06-09 sweep — FL-SPOKE-AUDIO):
        The stream carries live microphone PCM and the spoke credential, so it
        runs over encrypted ``wss://`` to every non-loopback hub. The
        credential is sent in the ``X-Spoke-Token`` handshake header (the same
        header the hub already validates and the music downlink already uses),
        never in the URL query string — query strings leak into server,
        reverse-proxy, and tunnel access logs.

    Args:
        hub_host: Hub hostname or IP.
        hub_port: Hub API port (WebSocket at ``/ws/voice-stream``).
        room: Room name for this spoke.
        tts_receiver: TTSReceiver instance for playing hub TTS responses.
        input_device: sounddevice input device index or name.
        spoke_token: Optional VIOLA_SPOKE_TOKEN for authentication; sent as
            the ``X-Spoke-Token`` header on the handshake.
        allow_insecure_loopback: Permit plaintext ``ws://`` when the hub is a
            loopback address (no on-wire exposure). Off by default; a non-
            loopback hub always uses ``wss://`` regardless of this flag.
    """

    def __init__(
        self,
        hub_host: str,
        hub_port: int,
        room: str,
        tts_receiver: TTSReceiver,
        *,
        input_device: int | str | None = None,
        spoke_token: str | None = None,
        allow_insecure_loopback: bool = True,
    ) -> None:
        self._hub_host = hub_host
        self._hub_port = hub_port
        self._room = room
        self._tts = tts_receiver
        self._input_device = input_device
        self._spoke_token = spoke_token
        self._allow_insecure_loopback = allow_insecure_loopback
        self._running = False
        self._ws: Any = None

    def _is_loopback_hub(self) -> bool:
        return self._hub_host.strip().lower() in _LOOPBACK_HOSTS

    @property
    def _use_tls(self) -> bool:
        """Encrypted transport unless the hub is loopback and insecure is opted-in."""
        if self._is_loopback_hub() and self._allow_insecure_loopback:
            return False
        return True

    @property
    def ws_url(self) -> str:
        # Credential rides in the X-Spoke-Token header (see auth_headers),
        # NOT the query string — query strings are logged by proxies/tunnels.
        scheme = "wss" if self._use_tls else "ws"
        params = {"room": self._room}
        return f"{scheme}://{self._hub_host}:{self._hub_port}/ws/voice-stream?{urlencode(params)}"

    @property
    def auth_headers(self) -> list[tuple[str, str]] | None:
        """Handshake headers carrying the spoke credential, if present."""
        if self._spoke_token:
            return [(SPOKE_TOKEN_HEADER_NAME, self._spoke_token)]
        return None

    async def run(self) -> None:
        """Main loop: connect → stream → reconnect on failure."""
        import websockets

        self._running = True
        delay = RECONNECT_DELAY_S

        while self._running:
            try:
                logger.info(
                    "Connecting to hub voice stream: %s (authed=%s, tls=%s)",
                    self.ws_url,
                    bool(self.auth_headers),
                    self._use_tls,
                )
                async with websockets.connect(self.ws_url, additional_headers=self.auth_headers) as ws:
                    self._ws = ws
                    delay = RECONNECT_DELAY_S  # reset backoff on success
                    logger.info("Connected to hub voice stream (room=%s)", self._room)

                    # Tell hub to start wake detection on our stream
                    await ws.send(
                        json.dumps(
                            {
                                "type": "start_listening",
                                "sample_rate": SAMPLE_RATE_16K,
                            }
                        )
                    )

                    # Run send + receive concurrently
                    send_task = asyncio.create_task(self._send_loop(ws))
                    recv_task = asyncio.create_task(self._receive_loop(ws))

                    done, pending = await asyncio.wait(
                        {send_task, recv_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    # Re-raise if a task failed
                    for t in done:
                        task_exc = t.exception()
                        if task_exc is not None:
                            raise task_exc

            except asyncio.CancelledError:
                logger.info("MicStreamer cancelled")
                break
            except Exception:
                logger.exception("MicStreamer connection error, reconnecting in %.0fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, MAX_RECONNECT_DELAY_S)

        self._running = False
        logger.info("MicStreamer stopped")

    async def _send_loop(self, ws: Any) -> None:
        """Capture mic audio and send PCM frames to hub."""
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)

        def _audio_callback(
            indata: np.ndarray,
            frames: int,
            time_info: Any,
            status: Any,
        ) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            # indata shape: (frames, channels) as int16
            pcm = indata[:, 0].tobytes()
            try:
                loop.call_soon_threadsafe(q.put_nowait, pcm)
            except asyncio.QueueFull:
                # Drop the frame under backpressure (the hub is slower than
                # capture); log at debug so the drop is observable.
                logger.debug("Mic frame dropped: send queue full (backpressure)")

        stream = sd.InputStream(
            samplerate=SAMPLE_RATE_16K,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=self._input_device,
            callback=_audio_callback,
        )

        with stream:
            logger.info(
                "Mic capture started: %d Hz, %d ms frames, device=%s",
                SAMPLE_RATE_16K,
                FRAME_DURATION_MS,
                self._input_device or "default",
            )
            while self._running:
                try:
                    pcm = await asyncio.wait_for(q.get(), timeout=2.0)
                    await ws.send(pcm)
                except TimeoutError:
                    continue  # no audio, keep alive

    async def _receive_loop(self, ws: Any) -> None:
        """Receive hub responses: command results, wake events, TTS audio."""
        from spoke.tts_receiver import TTSReceiver

        async for msg in ws:
            if isinstance(msg, bytes):
                # Binary message — check for TTS prefix
                if TTSReceiver.is_tts_message(msg):
                    pcm, sample_rate = TTSReceiver.decode_frame(msg)
                    self._tts.play(pcm, sample_rate=sample_rate)
                else:
                    logger.debug("Received unknown binary message (%d bytes)", len(msg))
                continue

            # Text message — JSON
            try:
                data = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = data.get("type", "")
            if msg_type == "wake_detected":
                logger.info(
                    "Wake word detected by hub (score=%.3f, room=%s)",
                    data.get("score", 0),
                    self._room,
                )
            elif msg_type == "command_result":
                transcript = data.get("transcript", "")
                response = data.get("response", "")
                logger.info(
                    "Command result: transcript=%r response=%r",
                    transcript,
                    response[:80] if response else "",
                )
            elif msg_type == "listening_status":
                logger.info("Listening status: active=%s", data.get("active"))
            elif msg_type == "error":
                logger.warning("Hub error: %s", data.get("message", ""))
            else:
                logger.debug("Unhandled message type: %s", msg_type)

    def stop(self) -> None:
        """Signal the streamer to stop."""
        self._running = False
