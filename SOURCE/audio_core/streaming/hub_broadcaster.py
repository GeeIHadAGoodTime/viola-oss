"""
Hub Audio Broadcaster - Create and broadcast timestamped PCM chunks.

The HubAudioBroadcaster sits on the Hub (audio source) side and receives
raw PCM data from the audio decoder. It creates timestamped chunks with
Hub monotonic time and broadcasts them to all connected Spoke devices
via WebSocket through the EventHub.

Design:
    - Receives raw PCM via on_pcm_data() callback (from AudioTee)
    - Buffers partial chunks until CHUNK_SIZE_BYTES is reached
    - Creates PCMChunk with play_at = current_hub_time + LEAD_TIME_MS
    - Broadcasts serialized chunks to room via EventHub
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

from .chunk_protocol import (
    CHUNK_SIZE_BYTES,
    PCMChunk,
    PCMChunkHeader,
    serialize,
)

if TYPE_CHECKING:
    from ui.websocket.event_hub import EventHub

logger = get_logger(__name__)


def _normalize_user_id(user_id: str | None) -> str | None:
    if not isinstance(user_id, str):
        return None
    normalized = user_id.strip()
    return normalized or None


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


# Lead time allows for network transit before playback is due
LEAD_TIME_MS: int = 100


class HubAudioBroadcaster:
    """
    Creates timestamped PCM chunks and broadcasts them to Spoke devices.

    The broadcaster buffers incoming PCM data until a full chunk is available,
    then timestamps it with Hub monotonic time plus lead time for network transit.

    Usage:
        broadcaster = HubAudioBroadcaster(event_hub, room_id="living_room")
        audio_tee.add_output(broadcaster.on_pcm_data)
        broadcaster.start()
    """

    def __init__(
        self,
        event_hub: EventHub,
        room_id: str,
        *,
        lead_time_ms: int = LEAD_TIME_MS,
        user_id: str | None = None,
    ) -> None:
        """
        Initialize the hub broadcaster.

        Args:
            event_hub: EventHub for WebSocket broadcasting
            room_id: Room identifier for scoped broadcasts
            lead_time_ms: Lead time in ms before play_at timestamp
            user_id: Optional owner for EventHub's user-scoped room bucket
        """
        self._event_hub = event_hub
        self._room_id = room_id
        self._lead_time_ms = lead_time_ms
        self._user_id = _normalize_user_id(user_id)

        # State
        self._running = False
        self._sequence: int = 0
        self._buffer = bytearray()
        self._lock = threading.Lock()

        # Async event loop reference for sync-to-async bridging
        self._loop: asyncio.AbstractEventLoop | None = None
        self._no_loop_logged: bool = False
        self._missing_user_logged: bool = False

        # Metrics
        self._chunks_broadcast: int = 0
        self._bytes_received: int = 0
        self._broadcast_failures: int = 0
        self._last_sent_count: int = 0

    def start(self) -> None:
        """Start the broadcaster."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._sequence = 0
            self._buffer.clear()
            logger.info(
                "Hub broadcaster started for room: %s, lead_time_ms=%d",
                self._room_id,
                self._lead_time_ms,
            )

    def stop(self) -> None:
        """Stop the broadcaster and clear state."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._buffer.clear()
            logger.info(
                "Hub broadcaster stopped for room: %s, chunks_broadcast=%d",
                self._room_id,
                self._chunks_broadcast,
            )

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the async event loop for sync-to-async bridging."""
        self._loop = loop
        self._no_loop_logged = False

    def set_user_id(self, user_id: str | None) -> None:
        """Set the owner used for user-scoped room broadcasts."""
        self._user_id = _normalize_user_id(user_id)
        self._missing_user_logged = False

    def on_pcm_data(self, pcm_data: bytes) -> None:
        """
        Callback for receiving PCM data from AudioTee.

        Buffers data and emits chunks when full. This is called from the
        audio decode thread, so it bridges to async for broadcasting.

        Args:
            pcm_data: Raw PCM audio data
        """
        if not pcm_data:
            return

        with self._lock:
            if not self._running:
                return

            self._bytes_received += len(pcm_data)
            self._buffer.extend(pcm_data)

            # Process complete chunks
            while len(self._buffer) >= CHUNK_SIZE_BYTES:
                chunk_data = bytes(self._buffer[:CHUNK_SIZE_BYTES])
                del self._buffer[:CHUNK_SIZE_BYTES]

                chunk = self._create_chunk(chunk_data)
                self._schedule_broadcast(chunk)

    def _create_chunk(self, pcm_data: bytes) -> PCMChunk:
        """
        Create a timestamped PCM chunk.

        Args:
            pcm_data: Raw PCM data for the chunk

        Returns:
            PCMChunk with header containing sequence and play_at timestamp
        """
        # play_at is hub monotonic time + lead time
        # Lead time gives network transit buffer before playback is due
        hub_time = time.monotonic()
        play_at = hub_time + (self._lead_time_ms / 1000.0)

        header = PCMChunkHeader(
            play_at=play_at,
            sequence=self._sequence,
        )
        self._sequence += 1

        return PCMChunk(header=header, pcm_data=pcm_data)

    def _schedule_broadcast(self, chunk: PCMChunk) -> None:
        """
        Schedule async broadcast from sync context.

        Args:
            chunk: PCMChunk to broadcast
        """
        if self._loop is None:
            # Log once, then silently drop until a loop is set
            if not self._no_loop_logged:
                logger.warning(
                    "Hub broadcaster has no event loop; " "chunks will be dropped until set_event_loop() is called"
                )
                self._no_loop_logged = True
            return

        def _do_broadcast():
            task = asyncio.create_task(self.broadcast_chunk(chunk))
            task.add_done_callback(_log_task_exception)

        try:
            self._loop.call_soon_threadsafe(_do_broadcast)
        except RuntimeError:
            # Loop closed — clear reference so set_event_loop can re-set it
            self._loop = None
            logger.debug("Event loop closed, cannot broadcast")

    async def broadcast_chunk(self, chunk: PCMChunk) -> None:
        """
        Broadcast a PCM chunk to all devices in the room.

        Args:
            chunk: PCMChunk to broadcast
        """
        # Serialize chunk to binary
        chunk_bytes = serialize(chunk)

        # Broadcast via EventHub to room
        # Using base64 encoding for JSON transport
        import base64

        payload: dict[str, Any] = {
            "chunk": base64.b64encode(chunk_bytes).decode("ascii"),
            "sequence": chunk.header.sequence,
            "play_at": chunk.header.play_at,
        }

        if self._user_id is None:
            self._last_sent_count = 0
            if not self._missing_user_logged:
                logger.warning(
                    "Hub broadcaster skipped PCM room broadcast for %s because user_id is missing",
                    self._room_id,
                )
                self._missing_user_logged = True
            return

        try:
            sent = await self._event_hub.broadcast_to_room(
                self._room_id,
                "pcm_chunk",
                payload,
                user_id=self._user_id,
            )
            self._last_sent_count = sent
            if sent > 0:
                self._chunks_broadcast += 1
                logger.debug(
                    "Broadcast chunk seq=%d to %d clients in room %s",
                    chunk.header.sequence,
                    sent,
                    self._room_id,
                )
        except Exception:
            self._broadcast_failures += 1
            self._last_sent_count = 0
            logger.exception(
                "Failed to broadcast chunk seq=%d to room %s",
                chunk.header.sequence,
                self._room_id,
            )

    def get_metrics(self) -> dict[str, Any]:
        """
        Get broadcaster metrics for monitoring.

        Returns:
            Dictionary with chunks_broadcast, bytes_received, etc.
        """
        with self._lock:
            return {
                "room_id": self._room_id,
                "running": self._running,
                "sequence": self._sequence,
                "chunks_broadcast": self._chunks_broadcast,
                "bytes_received": self._bytes_received,
                "buffer_size": len(self._buffer),
                "lead_time_ms": self._lead_time_ms,
                "user_id": self._user_id,
                "broadcast_failures": self._broadcast_failures,
                "last_sent_count": self._last_sent_count,
            }

    def reset_metrics(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._chunks_broadcast = 0
            self._bytes_received = 0
            self._broadcast_failures = 0
            self._last_sent_count = 0


__all__ = [
    "LEAD_TIME_MS",
    "HubAudioBroadcaster",
]
