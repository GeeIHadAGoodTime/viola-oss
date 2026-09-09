"""Audio tee processor — forks call audio to WebSocket listeners.

A Pipecat FrameProcessor that copies audio frames to registered WebSocket
listeners for listen-in functionality. Non-blocking, best-effort delivery:
dead listeners are removed, slow listeners are skipped.

Two instances per call: inbound tee (after transport.input()) and outbound
tee (before transport.output()).
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# Direction byte prefixed to binary audio messages
_DIR_INBOUND = b"\x00"
_DIR_OUTBOUND = b"\x01"
_RECORDING_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024


class AudioTeeProcessor:
    """Forks audio frames to WebSocket listeners.

    Must be created via create_processor() which returns a proper
    Pipecat FrameProcessor for pipeline insertion.
    """

    def __init__(self, direction: str, *, payment_sensitive_controller: Any | None = None) -> None:
        self._direction = direction
        self._payment_sensitive_controller = payment_sensitive_controller
        self._listeners: list[Any] = []  # WebSocket objects
        self._lock = asyncio.Lock()
        self._direction_byte = _DIR_INBOUND if direction == "inbound" else _DIR_OUTBOUND
        self._frame_processor = None
        self._takeover_active = False
        self._recording_enabled = False
        self._recording_buffer = bytearray()
        self._recording_cap_reached = False

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    @property
    def takeover_active(self) -> bool:
        return self._takeover_active

    @takeover_active.setter
    def takeover_active(self, value: bool) -> None:
        self._takeover_active = value

    def enable_recording(self) -> None:
        """Enable in-memory PCM recording for this tee direction."""
        self._recording_enabled = True

    def disable_recording(self, *, clear_buffer: bool = False) -> None:
        """Stop adding PCM to the recording buffer."""
        self._recording_enabled = False
        if clear_buffer:
            self._recording_buffer.clear()
            self._recording_cap_reached = False

    def set_payment_sensitive_controller(self, controller: Any | None) -> None:
        self._payment_sensitive_controller = controller

    def flush(self, direction: str) -> bytes:
        """Return and clear the captured PCM bytes for a direction."""
        if direction != self._direction:
            raise ValueError("Cannot flush %s audio from %s tee" % (direction, self._direction))
        audio = bytes(self._recording_buffer)
        self._recording_buffer.clear()
        self._recording_cap_reached = False
        return audio

    def _append_recording(self, pcm_bytes: bytes) -> None:
        controller = self._payment_sensitive_controller
        if controller is not None and bool(getattr(controller, "active", False)):
            return
        if not self._recording_enabled or self._recording_cap_reached or not pcm_bytes:
            return

        remaining = _RECORDING_BUFFER_LIMIT_BYTES - len(self._recording_buffer)
        if remaining <= 0:
            self._recording_cap_reached = True
            logger.warning(
                "AudioTee[%s]: recording buffer reached %d bytes; dropping remaining audio",
                self._direction,
                _RECORDING_BUFFER_LIMIT_BYTES,
            )
            return

        if len(pcm_bytes) > remaining:
            self._recording_buffer.extend(pcm_bytes[:remaining])
            self._recording_cap_reached = True
            logger.warning(
                "AudioTee[%s]: recording buffer reached %d bytes; dropping remaining audio",
                self._direction,
                _RECORDING_BUFFER_LIMIT_BYTES,
            )
            return

        self._recording_buffer.extend(pcm_bytes)

    async def add_listener(self, ws: Any) -> None:
        """Register a WebSocket as a listener."""
        async with self._lock:
            if ws not in self._listeners:
                self._listeners.append(ws)
                logger.info(
                    "AudioTee[%s]: listener added (total=%d)",
                    self._direction,
                    len(self._listeners),
                )

    async def remove_listener(self, ws: Any) -> None:
        """Unregister a WebSocket listener."""
        async with self._lock:
            try:
                self._listeners.remove(ws)
                logger.info(
                    "AudioTee[%s]: listener removed (total=%d)",
                    self._direction,
                    len(self._listeners),
                )
            except ValueError:
                logger.debug("Audio tee listener was already removed")

    async def _send_to_listeners(self, pcm_bytes: bytes) -> None:
        """Send audio to all listeners (best-effort, non-blocking)."""
        if not self._listeners:
            return

        message = self._direction_byte + pcm_bytes
        dead: list[Any] = []

        async with self._lock:
            for ws in self._listeners:
                try:
                    await asyncio.wait_for(ws.send_bytes(message), timeout=0.1)
                except Exception:
                    dead.append(ws)

            for ws in dead:
                try:
                    self._listeners.remove(ws)
                except ValueError:
                    logger.debug("Audio tee listener was already removed")

        if dead:
            logger.debug(
                "AudioTee[%s]: removed %d dead listeners",
                self._direction,
                len(dead),
            )

    def create_processor(self):
        """Create and return a Pipecat FrameProcessor wrapper."""
        from pipecat.frames.frames import (
            InputAudioRawFrame,
            OutputAudioRawFrame,
            TTSAudioRawFrame,
        )
        from pipecat.processors.frame_processor import FrameProcessor

        from telephony.payment_transmit_frame import PaymentTransmitEndFrame

        tee = self
        direction_label = self._direction

        class _TeeProcessor(FrameProcessor):
            async def process_frame(self, frame, direction):
                # CRITICAL: must call super().process_frame() so the base
                # FrameProcessor handles StartFrame and marks this processor
                # as started. Without this, _check_started rejects every
                # subsequent frame (including downstream StartFrame propagation
                # to STT/LLM/TTS) and the entire phone pipeline silently drops
                # all audio.
                await super().process_frame(frame, direction)

                if isinstance(frame, PaymentTransmitEndFrame):
                    controller = tee._payment_sensitive_controller
                    if controller is not None:
                        controller.end_payment_segment(str(getattr(frame, "viola_payment_segment_id", "") or ""))
                    return

                # Fork audio to listeners (always — operators monitoring the
                # call still need to hear what's flowing, even during takeover)
                if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame, TTSAudioRawFrame)):
                    if hasattr(frame, "audio") and frame.audio:
                        tee._append_recording(frame.audio)
                        if tee._listeners:
                            asyncio.create_task(tee._send_to_listeners(frame.audio))

                # Takeover suppression — added 2026-05-11.
                # The takeover_active flag previously had no effect: every
                # frame passed through unconditionally, so Viola's TTS kept
                # going to Telnyx even while the operator was mic-talking.
                # Now:
                #   - INBOUND tee drops InputAudioRawFrame so STT doesn't
                #     transcribe what the recipient says back to the
                #     operator (Viola wouldn't act on it anyway, but skipping
                #     STT/LLM saves cost + avoids ghost responses queued for
                #     after release).
                #   - OUTBOUND tee drops TTSAudioRawFrame and the
                #     pipecat-emitted OutputAudioRawFrame *unless* it came
                #     from _inject_takeover_audio() (which calls
                #     task.queue_frame, bypassing this processor entirely).
                if tee._takeover_active:
                    if direction_label == "inbound" and isinstance(frame, InputAudioRawFrame):
                        return
                    if direction_label == "outbound" and isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
                        return

                # Always pass non-audio frames through (control frames, etc.)
                await self.push_frame(frame, direction)

        self._frame_processor = _TeeProcessor(name="tee_%s" % self._direction)
        return self._frame_processor
