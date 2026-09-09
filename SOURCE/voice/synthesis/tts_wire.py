"""Wire helpers for browser TTS PCM frames."""

from __future__ import annotations

import struct

from core.constants import SAMPLE_RATE_16K

TTS_PREFIX = b"TTS\x00"
TTS_SAMPLE_RATE_MAGIC = b"SRAT"
_TTS_SAMPLE_RATE_HEADER = TTS_SAMPLE_RATE_MAGIC + struct.pack("<I", 0)
_TTS_SAMPLE_RATE_HEADER_LEN = len(_TTS_SAMPLE_RATE_HEADER)


def encode_tts_frame(pcm_mono_int16: bytes, sample_rate: int) -> bytes:
    """Return a TTS-prefixed binary frame with explicit sample-rate metadata."""
    return TTS_PREFIX + TTS_SAMPLE_RATE_MAGIC + struct.pack("<I", int(sample_rate)) + bytes(pcm_mono_int16)


def decode_tts_frame(frame: bytes) -> tuple[bytes, int] | None:
    """Decode a TTS wire frame for tests and server-side consumers.

    Legacy frames with only ``TTS_PREFIX`` are still accepted as 16 kHz PCM.
    """
    if not frame.startswith(TTS_PREFIX):
        return None
    payload = frame[len(TTS_PREFIX) :]
    if payload.startswith(TTS_SAMPLE_RATE_MAGIC) and len(payload) >= _TTS_SAMPLE_RATE_HEADER_LEN:
        sample_rate = struct.unpack("<I", payload[len(TTS_SAMPLE_RATE_MAGIC) : _TTS_SAMPLE_RATE_HEADER_LEN])[0]
        return payload[_TTS_SAMPLE_RATE_HEADER_LEN:], int(sample_rate)
    return payload, SAMPLE_RATE_16K


__all__ = [
    "TTS_PREFIX",
    "TTS_SAMPLE_RATE_MAGIC",
    "decode_tts_frame",
    "encode_tts_frame",
]
