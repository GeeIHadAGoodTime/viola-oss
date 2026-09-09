"""Telnyx Transport for Pipecat — bridges Telnyx media streaming with Pipecat frames.

Telnyx Call Control dials a number and streams bidirectional audio over WebSocket.
This transport:
    1. Starts a local WebSocket server
    2. Tells Telnyx to stream call audio to it (via stream_url in the dial command)
    3. Bridges Telnyx JSON+base64 messages ↔ Pipecat InputAudioRawFrame / OutputAudioRawFrame

Wire format (Telnyx → us):
    {"event": "connected", "stream_id": "..."}
    {"event": "start",     "stream_id": "...", "start": {"media_format": {...}}}
    {"event": "media",     "stream_id": "...", "media": {"track": "inbound", "payload": "<b64>"}}
    {"event": "stop",      "stream_id": "..."}

Wire format (us → Telnyx):
    {"event": "media",     "stream_id": "...", "media": {"payload": "<b64>"}}

Codec: L16 (raw 16-bit signed PCM, little-endian) at 16 kHz mono.

Modes:
    - **Local mode**: Transport starts its own ``websockets`` server on a local port.
      Telnyx connects via a Cloudflare tunnel or direct WS URL.
    - **Cloud mode**: A FastAPI WebSocket endpoint (``cloud_routes.phone_media_ws``)
      accepts the Telnyx connection and injects it into the transport via
      ``inject_cloud_websocket()``. No local server is started.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Pipecat frame types
try:
    from pipecat.frames.frames import (
        CancelFrame,
        ClientConnectedFrame,
        EndFrame,
        InputAudioRawFrame,
        InputDTMFFrame,
        InterruptionFrame,
        OutputAudioRawFrame,
        StartFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.serializers.telnyx import TelnyxFrameSerializer
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams

    PIPECAT_AVAILABLE = True
except ImportError:
    PIPECAT_AVAILABLE = False

try:
    from websockets.asyncio.server import serve as websocket_serve

    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Peer-disconnect exception set
# ---------------------------------------------------------------------------
# Writing audio to a media WebSocket whose far end already went away is an
# ordinary end-of-call event, not an error -- but the exception type raised for
# it depends on which WebSocket stack is under the socket we were handed, and
# neither of the two is a builtin:
#
#   cloud mode -> a FastAPI/Starlette WebSocket. uvicorn turns the closed
#       connection into ``ClientDisconnected`` (an OSError), then Starlette
#       catches that and re-raises ``WebSocketDisconnect(code=1006)``, which is
#       NOT an OSError and whose ``str()`` is empty.
#   local mode -> a ``websockets`` server connection, which raises
#       ``ConnectionClosed`` -- also not an OSError.
#
# Before this set existed the send paths caught only builtin error types, so a
# hangup mid-utterance escaped ``write_audio_frame`` into Pipecat's audio task,
# which logged it at ERROR (``base_output.py`` ``_audio_task_handler``) with an
# empty exception text. That turned a routine end-of-call race into a
# production error alert nobody could read (GlitchTip issue 115, 2026-08-07).
_PEER_DISCONNECT_ERRORS: tuple[type[BaseException], ...] = ()

try:
    from starlette.websockets import WebSocketDisconnect as _StarletteWebSocketDisconnect

    _PEER_DISCONNECT_ERRORS += (_StarletteWebSocketDisconnect,)
except ImportError:
    pass

try:
    from websockets.exceptions import ConnectionClosed as _WebSocketsConnectionClosed

    _PEER_DISCONNECT_ERRORS += (_WebSocketsConnectionClosed,)
except ImportError:
    pass

# Every failure shape the WebSocket send paths treat as "this frame did not go
# out" rather than propagating: the builtin errors a broken socket object can
# raise, plus the peer-disconnect types above.
_WS_SEND_ERRORS: tuple[type[BaseException], ...] = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
) + _PEER_DISCONNECT_ERRORS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Telnyx streams telephone audio as 8 kHz PCMU (mu-law). We keep the INBOUND
# Pipecat pipeline at that native 8 kHz rate instead of upsampling to 16 kHz.
#
# Why this matters (2026-06-24 "Viola deaf to recipient" incident): Silero VAD
# does NOT reliably detect speech in 8 kHz telephone-band audio that has been
# upsampled to 16 kHz. A band-limited 0-3.4 kHz telephone signal carried in a
# 16 kHz container reads as near-silence to the model -- measured on the real
# failing-call recording, max Silero confidence stayed ~0.12 on genuine
# recipient speech and never crossed the 0.7 start threshold, so the user
# aggregator's VAD never emitted UserStartedSpeaking, SegmentedSTTService never
# ran run_stt, and Viola never heard or responded to the recipient. The SAME
# audio analyzed at its native 8 kHz crosses the threshold normally.
# faster-whisper handles 8 kHz input natively, so STT is unaffected. The
# OUTBOUND leg is independent (audio_out_sample_rate stays 16 kHz to drive
# Kokoro); the serializer downsamples Viola's TTS to 8 kHz PCMU for the wire as
# before.
TELNYX_SAMPLE_RATE = 8000
TELNYX_WIRE_SAMPLE_RATE = 8000
TELNYX_CHANNELS = 1
TELNYX_CODEC = "PCMU"
TELNYX_STOP_LOCAL_SHUTDOWN_GRACE_SECONDS = 1.0


def _new_telnyx_frame_serializer() -> TelnyxFrameSerializer:
    serializer = TelnyxFrameSerializer(
        stream_id="",
        outbound_encoding=TELNYX_CODEC,
        inbound_encoding=TELNYX_CODEC,
        params=TelnyxFrameSerializer.InputParams(
            telnyx_sample_rate=TELNYX_WIRE_SAMPLE_RATE,
            sample_rate=TELNYX_SAMPLE_RATE,
            outbound_encoding=TELNYX_CODEC,
            inbound_encoding=TELNYX_CODEC,
            auto_hang_up=False,
        ),
    )
    # Replace the OUTBOUND resampler (pipeline-rate -> 8kHz wire) with a
    # clear-resistant one. pipecat's default stream resampler auto-clears its
    # SoX filter delay-line after >0.2s idle, and that clear DROPS audio: the
    # chunks fed right after a clear emit fewer samples while the filter
    # re-primes (~100ms lost per clear at this 16k->8k stage). Viola speaks in
    # sentence bursts separated by >0.2s pauses, so the default clears once per
    # sentence and clips the start of each one -> the intermittent voice
    # breakup on cloud phone calls. The continuous resampler keeps history
    # across pauses. The INBOUND resampler is left alone: inbound telephone
    # audio is a continuous 8kHz stream from the carrier with no >0.2s feed
    # gaps, so its clear never fires destructively, and recipient STT is a
    # separate concern. See telephony/continuous_stream_resampler.py.
    from telephony.continuous_stream_resampler import create_continuous_stream_resampler

    serializer._output_resampler = create_continuous_stream_resampler()
    return serializer


def _telnyx_start_call_control_id(message: dict[str, Any]) -> str:
    """Return the Telnyx call_control_id from a media stream start event."""
    start = message.get("start")
    if not isinstance(start, dict):
        return ""
    value = start.get("call_control_id")
    if not value:
        return ""
    return str(value).strip()


def _telnyx_media_format_mismatch_reason(media_format: Any) -> str:
    """Return a human-readable mismatch reason for the serializer wire format."""
    if not isinstance(media_format, dict):
        return "missing media_format"

    encoding = str(media_format.get("encoding") or "").upper()
    try:
        sample_rate = int(media_format.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    try:
        channels = int(media_format.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0

    if encoding != TELNYX_CODEC or sample_rate != TELNYX_WIRE_SAMPLE_RATE or channels != TELNYX_CHANNELS:
        return "expected %s/%dHz/%dch, got %s/%sHz/%sch" % (
            TELNYX_CODEC,
            TELNYX_WIRE_SAMPLE_RATE,
            TELNYX_CHANNELS,
            encoding or "<missing>",
            sample_rate or "<missing>",
            channels or "<missing>",
        )
    return ""


# ---------------------------------------------------------------------------
# FastAPI WebSocket adapter — normalises API to match ``websockets`` library
# ---------------------------------------------------------------------------


class FastAPIWebSocketAdapter:
    """Wraps a FastAPI ``WebSocket`` so it looks like a ``websockets`` connection.

    The existing ``TelnyxInputTransport._handle_connection`` and
    ``TelnyxOutputTransport.write_audio_frame`` use the ``websockets`` library
    API (``send()``, ``async for msg in ws``).  FastAPI WebSocket uses
    ``send_text()`` / ``receive_text()``.

    This adapter bridges the two so the transport code does not need to know
    which WebSocket implementation is in use.
    """

    def __init__(self, fastapi_ws: Any, initial_messages: list[str] | None = None) -> None:
        self._ws = fastapi_ws
        self._initial_messages = list(initial_messages or [])
        # Provide a best-effort remote_address for logging
        client = getattr(fastapi_ws, "client", None)
        self.remote_address: tuple[str, int] | str = (client.host, client.port) if client else "cloud"

    async def send(self, data: str) -> None:
        """Send a text message (matches ``websockets.WebSocketServerProtocol.send``)."""
        await self._ws.send_text(data)

    async def recv(self) -> str:
        """Receive a text message (matches ``websockets`` recv)."""
        if self._initial_messages:
            return self._initial_messages.pop(0)
        return await self._ws.receive_text()

    def __aiter__(self) -> FastAPIWebSocketAdapter:
        return self

    async def __anext__(self) -> str:
        """Iterate over incoming messages until the connection closes."""
        try:
            if self._initial_messages:
                return self._initial_messages.pop(0)
            return await self._ws.receive_text()
        except Exception:
            # FastAPI raises WebSocketDisconnect; websockets raises
            # ConnectionClosed — both mean "stop iterating".
            raise StopAsyncIteration

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the underlying FastAPI WebSocket."""
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass  # Already closed


# ---------------------------------------------------------------------------
# Transport params
# ---------------------------------------------------------------------------


class TelnyxTransportParams(TransportParams if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Config for the Telnyx WebSocket transport.

    Attributes:
        host: Local WS server bind address.
        port: Local WS server bind port.
        session_timeout: Max seconds before auto-hangup.
    """

    host: str = "0.0.0.0"  # nosec B104 — telephony server needs all-interface binding
    port: int = 8769
    session_timeout: int | None = None
    stream_shared_secret: str = ""


# ---------------------------------------------------------------------------
# Input transport — Telnyx WS → Pipecat frames
# ---------------------------------------------------------------------------


class TelnyxInputTransport(BaseInputTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Receives audio from Telnyx media stream and emits Pipecat frames.

    Supports two modes:

    **Local mode** (default):
        Starts a ``websockets`` server on ``host:port``.  Telnyx connects
        to it directly (or via a tunnel).

    **Cloud mode** (``inject_cloud_websocket``):
        An external caller (``cloud_routes.phone_media_ws``) provides an
        already-connected WebSocket.  No local server is started.
    """

    def __init__(
        self,
        transport: BaseTransport,
        params: TelnyxTransportParams,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._transport = transport
        self._params = params
        self._websocket: Any = None
        self._stream_id: str = ""
        self._call_control_id: str = ""
        self._call_control_id_callback: Callable[[str], None] | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._cloud_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._server_ready_event = asyncio.Event()
        self._unexpected_disconnect_event = asyncio.Event()
        self._media_disconnect_reason = ""
        self._initialized = False
        self._cloud_mode = False
        self._stream_stop_seen = False
        self._serializer = _new_telnyx_frame_serializer()

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def is_connected(self) -> bool:
        return self._websocket is not None and not self._stop_event.is_set()

    @property
    def media_disconnect_reason(self) -> str:
        return self._media_disconnect_reason

    @property
    def call_control_id(self) -> str:
        return self._call_control_id

    @property
    def ws_url(self) -> str:
        """URL that Telnyx should connect to (local mode only)."""
        return "ws://%s:%d" % (self._params.host, self._params.port)

    def set_call_control_id_callback(self, callback: Callable[[str], None] | None) -> None:
        """Register a callback for the recipient-leg call_control_id."""
        self._call_control_id_callback = callback

    def _set_call_control_id(self, call_control_id: str) -> None:
        call_control_id = str(call_control_id or "").strip()
        if not call_control_id or call_control_id == self._call_control_id:
            return
        self._call_control_id = call_control_id
        if self._call_control_id_callback is None:
            return
        try:
            self._call_control_id_callback(call_control_id)
        except Exception:
            logger.debug("Telnyx call_control_id callback failed", exc_info=True)

    async def wait_for_connection(self, timeout: float = 30.0) -> bool:
        """Wait until Telnyx connects its media stream."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def wait_for_unexpected_disconnect(self, timeout: float | None = None) -> bool:
        """Wait for a media stream end before Viola intentionally stopped it.

        Kept for compatibility with older call-manager tests. Telnyx ``stop``
        is not a healthy in-call state for outbound audio: once it arrives,
        there is no media stream left for TTS frames to reach the callee.
        """
        return await self.wait_for_media_disconnect(timeout)

    async def wait_for_media_disconnect(self, timeout: float | None = None) -> bool:
        """Wait for a Telnyx media stream end before local pipeline shutdown."""
        try:
            if timeout is None:
                await self._unexpected_disconnect_event.wait()
            else:
                await asyncio.wait_for(self._unexpected_disconnect_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _media_ended_before_local_stop(self) -> bool:
        """Return true only when carrier media ended outside local shutdown."""
        if self._stop_event.is_set():
            return False
        if self._media_disconnect_reason != "telnyx_stop":
            return True
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                TELNYX_STOP_LOCAL_SHUTDOWN_GRACE_SECONDS,
            )
            return False
        except TimeoutError:
            return True

    # ---- Cloud mode injection ----

    async def inject_cloud_websocket(self, websocket: Any, stream_id: str = "") -> None:
        """Inject an externally-managed WebSocket for cloud mode.

        Called by ``cloud_routes.phone_media_ws`` after it receives the
        Telnyx connection and resolves which transport to route it to.

        The websocket should be a ``FastAPIWebSocketAdapter`` (or any
        object that implements ``send()``, ``recv()``, and async iteration).

        This method blocks until the WebSocket disconnects or the transport
        is stopped -- the caller should ``await`` it in a task.

        Args:
            websocket: Adapter-wrapped FastAPI WebSocket from the cloud bridge.
            stream_id: The Telnyx stream_id (already extracted by the bridge
                from the "connected" event that was consumed before injection).
        """
        self._cloud_mode = True
        if stream_id:
            self._stream_id = stream_id
        logger.info(
            "Cloud mode: WebSocket injected into TelnyxInputTransport (stream_id=%s)",
            self._stream_id,
        )
        await self._handle_connection(websocket)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self._serializer.setup(frame)
        if self._initialized:
            return
        self._initialized = True
        # In cloud mode, skip local server — WebSocket arrives via inject_cloud_websocket
        if not self._cloud_mode and not self._server_task:
            self._server_task = self.create_task(self._run_server())
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        self._stop_event.set()
        if self._server_task:
            await self._server_task
            self._server_task = None
        if self._cloud_task:
            self._cloud_task.cancel()
            self._cloud_task = None

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        self._stop_event.set()
        if self._server_task:
            await self.cancel_task(self._server_task)
            self._server_task = None
        if self._cloud_task:
            self._cloud_task.cancel()
            self._cloud_task = None

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._transport.cleanup()

    # ---- WebSocket server (local mode) ----

    async def _run_server(self) -> None:
        logger.info(
            "Telnyx transport: WS server starting on %s:%d",
            self._params.host,
            self._params.port,
        )
        async with websocket_serve(
            self._handle_connection,
            self._params.host,
            self._params.port,
        ):
            self._server_ready_event.set()
            await self._stop_event.wait()

    # ---- Connection handler (shared by local and cloud modes) ----

    async def _handle_connection(self, websocket: Any) -> None:
        """Process Telnyx media messages from *any* WebSocket.

        Works with both ``websockets`` library connections (local mode)
        and ``FastAPIWebSocketAdapter`` instances (cloud mode).
        """
        if not self._cloud_mode:
            import hmac
            from urllib.parse import parse_qs, urlparse

            request = getattr(websocket, "request", None)
            path = getattr(request, "path", "") or getattr(websocket, "path", "")
            tokens = parse_qs(urlparse(path).query).get("token", [])
            secret = self._params.stream_shared_secret
            if not secret or len(tokens) != 1 or not hmac.compare_digest(tokens[0], secret):
                await websocket.close(code=1008, reason="Media authentication required")
                return
            if self._websocket is not None:
                await websocket.close(code=1008, reason="Media stream already connected")
                return
        remote = getattr(websocket, "remote_address", "unknown")
        logger.info("Telnyx media stream connected from %s", remote)
        self._websocket = websocket
        self._stream_stop_seen = False
        self._media_disconnect_reason = ""
        self._unexpected_disconnect_event.clear()
        # Expose socket to output transport via parent transport
        if hasattr(self._transport, "_on_ws_connected"):
            await self._transport._on_ws_connected(websocket)
        await self.push_frame(ClientConnectedFrame())

        try:
            async for raw_message in websocket:
                if self._stop_event.is_set():
                    break

                try:
                    msg = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    continue

                event = msg.get("event", "")

                if event == "connected":
                    self._stream_id = msg.get("stream_id", "")
                    logger.info("Telnyx stream connected: stream_id=%s", self._stream_id)

                elif event == "start":
                    stream_id = msg.get("stream_id", "")
                    if stream_id:
                        self._stream_id = str(stream_id)
                        if hasattr(self._transport, "_on_ws_connected"):
                            await self._transport._on_ws_connected(websocket)

                    call_control_id = _telnyx_start_call_control_id(msg)
                    if call_control_id:
                        self._set_call_control_id(call_control_id)
                        logger.info(
                            "Telnyx stream start includes call_control_id=%s",
                            call_control_id[:16],
                        )
                    logger.info(
                        "Telnyx stream started: %s",
                        msg.get("start", {}).get("media_format", {}),
                    )
                    mismatch_reason = _telnyx_media_format_mismatch_reason(msg.get("start", {}).get("media_format", {}))
                    if mismatch_reason:
                        logger.warning(
                            "Telnyx stream media format does not match Pipecat serializer transport: %s",
                            mismatch_reason,
                        )
                    self._connected_event.set()

                elif event == "media":
                    frame = await self._serializer.deserialize(raw_message)
                    if isinstance(frame, InputAudioRawFrame):
                        await self.push_audio_frame(frame)
                    elif frame is not None:
                        await self.push_frame(frame)

                elif event == "dtmf":
                    frame = await self._serializer.deserialize(raw_message)
                    if isinstance(frame, InputDTMFFrame):
                        await self.push_frame(frame)

                elif event == "mark":
                    mark = msg.get("mark", {}) or {}
                    mark_name = str(mark.get("name") or "").strip()
                    if mark_name and hasattr(self._transport, "_on_mark_received"):
                        self._transport._on_mark_received(mark_name)

                elif event == "error":
                    payload = msg.get("payload", {}) or msg.get("error", {}) or {}
                    code = str(payload.get("code") or "").strip()
                    title = str(payload.get("title") or "media_error").strip()
                    detail = str(payload.get("detail") or "").strip()
                    self._media_disconnect_reason = "telnyx_error:%s" % (code or title)
                    logger.warning(
                        "Telnyx media stream error: code=%s title=%s detail=%s stream_id=%s",
                        code or "<unknown>",
                        title or "<unknown>",
                        detail or "<none>",
                        self._stream_id or msg.get("stream_id", "") or "<unknown>",
                    )
                    break

                elif event == "stop":
                    logger.info("Telnyx stream stopped")
                    self._stream_stop_seen = True
                    self._media_disconnect_reason = "telnyx_stop"
                    break

        except Exception:
            self._media_disconnect_reason = "transport_error"
            logger.exception("Telnyx input transport error")
        finally:
            media_ended_before_local_stop = await self._media_ended_before_local_stop()
            if media_ended_before_local_stop and not self._media_disconnect_reason:
                self._media_disconnect_reason = "websocket_closed"
            self._websocket = None
            if hasattr(self._transport, "_on_ws_disconnected"):
                await self._transport._on_ws_disconnected(websocket)
            if media_ended_before_local_stop:
                self._unexpected_disconnect_event.set()
                try:
                    await self.push_frame(EndFrame(reason="Telnyx media stream disconnected"))
                except Exception as exc:
                    logger.debug(
                        "Telnyx input transport failed to push EndFrame on disconnect: %s",
                        exc,
                    )
            logger.info("Telnyx media stream disconnected")


# ---------------------------------------------------------------------------
# Output transport — Pipecat frames → Telnyx WS
# ---------------------------------------------------------------------------


class TelnyxOutputTransport(BaseOutputTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Sends Pipecat audio frames to Telnyx through the Pipecat serializer."""

    def __init__(
        self,
        transport: BaseTransport,
        params: TelnyxTransportParams,
        **kwargs: Any,
    ) -> None:
        super().__init__(params, **kwargs)
        self._transport = transport
        self._params = params
        self._websocket: Any = None
        self._stream_id: str = ""
        self._send_interval: float = 0.0
        self._next_send_time: float = 0.0
        self._initialized = False
        self._audio_frames_attempted = 0
        self._audio_frames_sent = 0
        self._audio_bytes_sent = 0
        self._audio_frames_dropped_after_disconnect = 0
        self._serializer = _new_telnyx_frame_serializer()

    async def set_ws_connection(self, websocket: Any, stream_id: str = "") -> None:
        """Called by parent transport when Telnyx connects."""
        self._websocket = websocket
        self._stream_id = stream_id

    async def clear_ws_connection(self, websocket: Any | None = None) -> None:
        """Clear the output socket after the matching Telnyx WS disconnects."""
        if websocket is not None and self._websocket is not websocket:
            return
        self._websocket = None
        self._stream_id = ""

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self._serializer.setup(frame)
        if self._initialized:
            return
        self._initialized = True
        self._send_interval = (self.audio_chunk_size / self.sample_rate) / 2
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            await self._send_serialized_frame(frame, warn_on_drop=False)
        await super().process_frame(frame, direction)

    async def _send_serialized_frame(self, frame: Any, *, warn_on_drop: bool) -> bool:
        if not self._websocket:
            if warn_on_drop:
                logger.warning("Telnyx output frame dropped before WebSocket was connected")
            return False

        msg = await self._serializer.serialize(frame)
        if not msg:
            return False
        try:
            await self._websocket.send(msg)
            return True
        except _WS_SEND_ERRORS:
            logger.warning("Failed to send serialized Telnyx frame")
            return False

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio to Telnyx over WebSocket.

        Overrides BaseOutputTransport.write_audio_frame — the method Pipecat
        calls from _audio_task_handler. Without this override, the base class
        returns False and silently discards all outbound audio.
        """
        self._audio_frames_attempted += 1
        if not self._websocket:
            if self._audio_frames_sent:
                self._audio_frames_dropped_after_disconnect += 1
                if self._audio_frames_dropped_after_disconnect == 1:
                    logger.warning("Telnyx output audio frame dropped after media stream disconnected")
            elif self._audio_frames_attempted == 1:
                logger.warning("Telnyx output audio frame dropped before WebSocket was connected")
            return False

        msg = await self._serializer.serialize(frame)
        if not msg:
            return True

        try:
            await self._websocket.send(msg)
            self._audio_frames_sent += 1
            self._audio_bytes_sent += len(frame.audio or b"")
            if self._audio_frames_sent == 1:
                logger.info(
                    "Telnyx output sent first audio frame: bytes=%d sample_rate=%d channels=%d stream_id=%s",
                    len(frame.audio or b""),
                    getattr(frame, "sample_rate", 0),
                    getattr(frame, "num_channels", 0),
                    self._stream_id or "<unknown>",
                )
            return True
        except _WS_SEND_ERRORS:
            logger.warning("Failed to send audio to Telnyx WS")
            return False

    async def send_mark(self, name: str) -> bool:
        """Send a Telnyx media mark over the bidirectional stream."""
        if not self._websocket:
            logger.warning("Telnyx output mark dropped before WebSocket was connected")
            return False
        mark_name = str(name or "").strip()
        if not mark_name:
            return False
        msg = json.dumps(
            {
                "event": "mark",
                "mark": {"name": mark_name},
            }
        )
        try:
            await self._websocket.send(msg)
            logger.debug("Telnyx output mark sent: %s", mark_name)
            return True
        except _WS_SEND_ERRORS:
            logger.warning("Failed to send mark to Telnyx WS")
            return False


# ---------------------------------------------------------------------------
# Combined transport
# ---------------------------------------------------------------------------


class TelnyxTransport(BaseTransport if PIPECAT_AVAILABLE else object):  # type: ignore[misc]
    """Pipecat transport that bridges Telnyx media streaming.

    Supports two modes:

    **Local mode** (default)::

        transport = TelnyxTransport(TelnyxTransportParams(port=8769))
        # Starts its own WS server; Telnyx connects via tunnel.

    **Cloud mode**::

        transport = TelnyxTransport(TelnyxTransportParams())
        transport.set_cloud_mode()
        # No WS server.  Call inject_cloud_websocket() later.

    Pipeline wiring is identical in both modes::

        pipeline = Pipeline([
            transport.input(),   # receives audio from phone call
            stt,
            context_aggregator.user(),
            llm,
            tts,
            transport.output(),  # sends audio back to phone call
            context_aggregator.assistant(),
        ])
    """

    def __init__(self, params: TelnyxTransportParams | None = None) -> None:
        if not PIPECAT_AVAILABLE:
            raise ImportError("pipecat-ai is required: pip install pipecat-ai")
        if not WS_AVAILABLE:
            raise ImportError("websockets is required: pip install websockets")

        super().__init__()
        self._params = params or TelnyxTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=TELNYX_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=TELNYX_SAMPLE_RATE,
            vad_enabled=True,
        )
        # Ensure audio is enabled
        self._params.audio_in_enabled = True
        self._params.audio_out_enabled = True
        if not self._params.audio_in_sample_rate:
            self._params.audio_in_sample_rate = TELNYX_SAMPLE_RATE
        if not self._params.audio_out_sample_rate:
            self._params.audio_out_sample_rate = TELNYX_SAMPLE_RATE

        self._input = TelnyxInputTransport(self, self._params)
        self._output = TelnyxOutputTransport(self, self._params)
        self._mark_waiters: dict[str, asyncio.Future[None]] = {}

    def set_cloud_mode(self) -> None:
        """Switch to cloud mode before pipeline start.

        In cloud mode the transport does NOT start a local WebSocket
        server.  Instead, call ``inject_cloud_websocket()`` once the
        FastAPI endpoint has a Telnyx connection ready.
        """
        self._input._cloud_mode = True
        logger.info("TelnyxTransport set to cloud mode (no local WS server)")

    async def inject_cloud_websocket(
        self,
        fastapi_ws: Any,
        stream_id: str = "",
        initial_messages: list[str] | None = None,
    ) -> None:
        """Inject a FastAPI WebSocket into the transport for cloud mode.

        Wraps the FastAPI WebSocket in a ``FastAPIWebSocketAdapter`` so
        the existing message-processing code works unchanged, then
        delegates to the input transport.

        This method blocks until the WebSocket disconnects or the
        transport is stopped.  The caller (``cloud_routes``) should
        run this in a task.

        Args:
            fastapi_ws: A raw FastAPI ``WebSocket`` instance (already accepted).
            stream_id: The Telnyx stream_id (already extracted by the bridge
                from the "connected" event).
            initial_messages: Telnyx messages already consumed by the cloud
                bridge while resolving the stream. They are replayed into the
                input transport so media-start metadata is not lost.
        """
        adapter = FastAPIWebSocketAdapter(fastapi_ws, initial_messages=initial_messages)
        await self._input.inject_cloud_websocket(adapter, stream_id=stream_id)

    @property
    def stream_url(self) -> str:
        """WebSocket URL to pass to Telnyx dial command."""
        return self._input.ws_url

    @property
    def stream_id(self) -> str:
        return self._input.stream_id

    @property
    def is_connected(self) -> bool:
        return self._input.is_connected

    @property
    def call_control_id(self) -> str:
        return self._input.call_control_id

    def set_call_control_id_callback(self, callback: Callable[[str], None] | None) -> None:
        """Register a callback for the Telnyx recipient-leg call_control_id."""
        self._input.set_call_control_id_callback(callback)

    async def wait_for_connection(self, timeout: float = 30.0) -> bool:
        """Wait until Telnyx connects its media stream."""
        return await self._input.wait_for_connection(timeout)

    async def wait_for_server_ready(self, timeout: float = 10.0) -> bool:
        """Prove this call's own listener bound before issuing a billable dial."""
        try:
            await asyncio.wait_for(self._input._server_ready_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def wait_for_unexpected_disconnect(self, timeout: float | None = None) -> bool:
        """Wait for a media stream end before Viola intentionally stopped it."""
        return await self._input.wait_for_unexpected_disconnect(timeout)

    async def wait_for_media_disconnect(self, timeout: float | None = None) -> bool:
        """Wait for a media stream end before Viola intentionally stopped it."""
        return await self._input.wait_for_media_disconnect(timeout)

    async def wait_for_output_mark(self, name: str, timeout: float = 2.0) -> bool:
        """Send a Telnyx mark and wait for its echo after preceding media plays."""
        mark_name = str(name or "").strip()
        if not mark_name:
            return False
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        old_future = self._mark_waiters.pop(mark_name, None)
        if old_future is not None and not old_future.done():
            old_future.set_exception(RuntimeError("superseded by a newer Telnyx mark waiter"))
        self._mark_waiters[mark_name] = future
        try:
            sent = await self._output.send_mark(mark_name)
            if not sent:
                return False
            await asyncio.wait_for(future, timeout=timeout)
            return True
        except (ConnectionError, RuntimeError, OSError, TimeoutError):
            return False
        finally:
            self._mark_waiters.pop(mark_name, None)

    @property
    def media_disconnect_reason(self) -> str:
        return self._input.media_disconnect_reason

    def input(self) -> TelnyxInputTransport:
        return self._input

    def output(self) -> TelnyxOutputTransport:
        return self._output

    async def _on_ws_connected(self, websocket: Any) -> None:
        """Called by input transport when Telnyx connects."""
        await self._output.set_ws_connection(websocket, self._input.stream_id)

    async def _on_ws_disconnected(self, websocket: Any) -> None:
        """Called by input transport when Telnyx disconnects."""
        await self._output.clear_ws_connection(websocket)
        waiters = list(self._mark_waiters.values())
        self._mark_waiters.clear()
        for future in waiters:
            if not future.done():
                future.set_exception(ConnectionError("Telnyx media stream disconnected before mark echo"))

    def _on_mark_received(self, name: str) -> None:
        """Resolve a wait_for_output_mark waiter when Telnyx echoes a mark."""
        mark_name = str(name or "").strip()
        future = self._mark_waiters.pop(mark_name, None)
        if future is None:
            logger.debug("Telnyx mark received with no waiter: %s", mark_name)
            return
        if not future.done():
            future.set_result(None)

    async def cleanup(self) -> None:
        pass
