"""
TTS Receiver — plays PCM audio bytes received from hub via sounddevice.

The hub synthesises TTS and sends raw PCM bytes prefixed with ``b"TTS\\x00"``
over the voice-stream WebSocket. Newer hub builds (post-stradivari) also
include an optional sample-rate header (``b"SRAT" + uint32 LE``) immediately
after the prefix so the spoke can play at whatever rate Kokoro emitted
(24 kHz native), rather than forcing a 16 kHz resample at the hub. Frames
without the SRAT header are treated as legacy 16 kHz PCM.

This module wraps the canonical decoder in ``voice.synthesis.tts_wire`` so
both sides of the wire agree on the format.
"""

from __future__ import annotations

import threading
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# TTS wire prefix: 4 bytes identifying a TTS binary message
TTS_PREFIX = b"TTS\x00"
TTS_PREFIX_LEN = len(TTS_PREFIX)

# Default sample rate for legacy frames without an explicit rate header
TTS_SAMPLE_RATE = 16000
TTS_CHANNELS = 1
TTS_DTYPE = "int16"


class TTSReceiver:
    """Plays TTS PCM bytes through the local sounddevice output."""

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._lock = threading.Lock()
        self._sd: Any = None

    def _ensure_sd(self) -> Any:
        """Lazy-import sounddevice to keep module import lightweight."""
        if self._sd is None:
            import sounddevice as sd

            self._sd = sd
        return self._sd

    def play(self, pcm_bytes: bytes, sample_rate: int | None = None) -> None:
        """Play raw int16 mono PCM bytes through speakers.

        Called from the mic_streamer receive loop when a TTS binary message
        arrives. Runs in the caller's thread — sounddevice play() is
        non-blocking by default.

        ``sample_rate`` defaults to ``TTS_SAMPLE_RATE`` (16 kHz) when the
        caller did not parse one from a SRAT frame header.
        """
        if not pcm_bytes:
            return

        rate = int(sample_rate) if sample_rate else TTS_SAMPLE_RATE

        import numpy as np

        sd = self._ensure_sd()
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)

        with self._lock:
            try:
                sd.play(
                    samples,
                    samplerate=rate,
                    device=self._device,
                    blocking=False,
                )
                logger.debug(
                    "TTS playback started: %d samples @ %d Hz (%.1fs)",
                    len(samples),
                    rate,
                    len(samples) / rate,
                )
            except Exception:
                logger.exception("TTS playback failed")

    @staticmethod
    def is_tts_message(data: bytes) -> bool:
        """Check whether a binary WebSocket message is a TTS payload."""
        return len(data) > TTS_PREFIX_LEN and data[:TTS_PREFIX_LEN] == TTS_PREFIX

    @staticmethod
    def decode_frame(data: bytes) -> tuple[bytes, int]:
        """Decode a TTS-prefixed frame into ``(pcm_bytes, sample_rate)``.

        Frames with the new ``SRAT`` header carry an explicit sample rate.
        Frames without it are treated as legacy 16 kHz PCM, matching the
        pre-stradivari wire contract.
        """
        from voice.synthesis.tts_wire import decode_tts_frame

        decoded = decode_tts_frame(data)
        if decoded is None:
            # Not a TTS-prefixed payload; preserve callers' assumption that
            # `is_tts_message` is checked first and this is unreachable.
            return b"", TTS_SAMPLE_RATE
        return decoded

    @staticmethod
    def strip_prefix(data: bytes) -> bytes:
        """Strip TTS prefix (and optional SRAT header), returning raw PCM bytes.

        Kept for legacy callers that don't care about the embedded sample
        rate. Prefer :meth:`decode_frame` if you need to play at the
        hub-reported rate (the Python spoke pipeline does).
        """
        pcm, _rate = TTSReceiver.decode_frame(data)
        return pcm
