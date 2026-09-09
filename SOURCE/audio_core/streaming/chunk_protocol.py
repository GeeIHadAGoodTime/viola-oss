"""
PCM Chunk Protocol for multi-room audio streaming.

Defines the binary format for timestamped PCM audio chunks transmitted between
Hub (audio source) and Spoke (playback devices) for synchronized multi-room playback.

Wire Format:
    Header (16 bytes):
        - play_at: float64 (8 bytes) - Hub monotonic timestamp when chunk was captured
        - sequence: uint32 (4 bytes) - monotonically increasing sequence number
        - flags: uint16 (2 bytes)    - bit 0: FLAG_SILENCE (padding chunk, no real audio)
        - reserved: uint16 (2 bytes) - reserved for future use (always 0)

    Body:
        - pcm_data: bytes (CHUNK_SIZE_BYTES) - raw PCM audio data

Total frame size: 16 + 3840 = 3856 bytes
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from core.constants import AUDIO_CHANNELS_STEREO, SAMPLE_RATE_48K

from .exceptions import ChunkDeserializationError

# Audio format constants
CHUNK_DURATION_MS: int = 20  # 20ms chunks for low-latency streaming
SAMPLE_RATE: int = SAMPLE_RATE_48K  # 48kHz for high quality audio
CHANNELS: int = AUDIO_CHANNELS_STEREO  # Stereo audio
BYTES_PER_SAMPLE: int = 2  # 16-bit PCM = 2 bytes per sample
BYTES_PER_SAMPLE_24: int = 3  # 24-bit PCM = 3 bytes per sample

# Calculated chunk size: 20ms * 48000 Hz * 2 channels * 2 bytes = 3840 bytes
SAMPLES_PER_CHUNK: int = (CHUNK_DURATION_MS * SAMPLE_RATE) // 1000  # 960
CHUNK_SIZE_BYTES: int = SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE
CHUNK_SIZE_BYTES_24: int = SAMPLES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE_24  # 5760

# Header format: float64 play_at, uint32 sequence, uint16 flags, uint16 reserved
# '<' = little-endian
HEADER_FORMAT: str = "<dIHH"
HEADER_SIZE: int = struct.calcsize(HEADER_FORMAT)  # 16 bytes

# Total binary frame size (header + PCM payload)
FRAME_SIZE: int = HEADER_SIZE + CHUNK_SIZE_BYTES  # 3856 bytes
FRAME_SIZE_24: int = HEADER_SIZE + CHUNK_SIZE_BYTES_24  # 5776 bytes

# Format version (stored in the ``reserved`` uint16 header field)
FORMAT_VERSION_16BIT: int = 0  # Legacy 16-bit PCM
FORMAT_VERSION_24BIT: int = 1  # 24-bit PCM

# Flag bits
FLAG_SILENCE: int = 0x0001  # Chunk is silence padding (no captured audio)


@dataclass(frozen=True, slots=True)
class PCMChunkHeader:
    """
    Header for a PCM audio chunk.

    Attributes:
        play_at: Hub monotonic timestamp when this chunk was captured
        sequence: Monotonically increasing sequence number for ordering/gap detection
        flags: Bit flags (FLAG_SILENCE = 0x0001)
    """

    play_at: float  # float64 - Hub monotonic time
    sequence: int  # uint32
    flags: int = 0  # uint16

    @property
    def is_silence(self) -> bool:
        """Return True if this is a silence padding chunk."""
        return bool(self.flags & FLAG_SILENCE)


@dataclass(frozen=True, slots=True)
class PCMChunk:
    """
    A timestamped PCM audio chunk for multi-room sync.

    Attributes:
        header: Chunk metadata including timing and sequence info
        pcm_data: Raw PCM audio data (16-bit stereo at 48kHz)
    """

    header: PCMChunkHeader
    pcm_data: bytes


def serialize(chunk: PCMChunk, format_version: int = FORMAT_VERSION_16BIT) -> bytes:
    """
    Serialize a PCMChunk to binary wire format.

    Args:
        chunk: PCMChunk to serialize.
        format_version: ``FORMAT_VERSION_16BIT`` (0) or ``FORMAT_VERSION_24BIT`` (1).
            Written into the ``reserved`` header field so receivers can detect bit depth.

    Returns:
        Binary data: header (16 bytes) + pcm_data.
    """
    header_bytes = struct.pack(
        HEADER_FORMAT,
        chunk.header.play_at,
        chunk.header.sequence,
        chunk.header.flags,
        format_version,  # reserved field carries format version
    )
    return header_bytes + chunk.pcm_data


def deserialize(data: bytes) -> PCMChunk:
    """
    Deserialize binary wire format to a PCMChunk.

    Accepts both 16-bit (3856 bytes) and 24-bit (5776 bytes) frames.
    The ``reserved`` header field carries the format version
    (0 = 16-bit, 1 = 24-bit).

    Args:
        data: Binary data in wire format (header + pcm_data)

    Returns:
        Deserialized PCMChunk

    Raises:
        ChunkDeserializationError: If data is malformed or too short
    """
    min_size = HEADER_SIZE
    if len(data) < min_size:
        raise ChunkDeserializationError(
            reason="Data too short for header",
            received_bytes=len(data),
            expected_bytes=min_size,
        )

    try:
        play_at, sequence, flags, format_version = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    except struct.error as e:
        raise ChunkDeserializationError(
            reason="Header unpack failed: %s" % e,
            received_bytes=len(data),
            expected_bytes=HEADER_SIZE,
        ) from e

    pcm_data = data[HEADER_SIZE:]

    # Determine expected PCM size from format version
    if format_version == FORMAT_VERSION_24BIT:
        expected_pcm = CHUNK_SIZE_BYTES_24
    else:
        expected_pcm = CHUNK_SIZE_BYTES

    if len(pcm_data) != expected_pcm:
        raise ChunkDeserializationError(
            reason="PCM data size mismatch: got %d, expected %d (format_version=%d)"
            % (len(pcm_data), expected_pcm, format_version),
            received_bytes=len(pcm_data),
            expected_bytes=expected_pcm,
        )

    header = PCMChunkHeader(
        play_at=play_at,
        sequence=sequence,
        flags=flags,
    )

    return PCMChunk(header=header, pcm_data=pcm_data)


def chunk_duration_seconds() -> float:
    """Return the duration of a single chunk in seconds."""
    return CHUNK_DURATION_MS / 1000.0


def bytes_to_duration_ms(num_bytes: int) -> float:
    """Convert byte count to duration in milliseconds."""
    samples = num_bytes // (CHANNELS * BYTES_PER_SAMPLE)
    return (samples / SAMPLE_RATE) * 1000.0


def duration_ms_to_bytes(duration_ms: float) -> int:
    """Convert duration in milliseconds to byte count."""
    samples = int((duration_ms / 1000.0) * SAMPLE_RATE)
    return samples * CHANNELS * BYTES_PER_SAMPLE


# Pre-allocated silence chunks
SILENCE_PCM: bytes = b"\x00" * CHUNK_SIZE_BYTES
SILENCE_PCM_24: bytes = b"\x00" * CHUNK_SIZE_BYTES_24


def pack_float32_to_int24(samples: numpy.ndarray) -> bytes:
    """Convert a float32 numpy array to packed 24-bit little-endian PCM bytes.

    Clipping is applied: values outside [-1, 1) are clamped.
    The output has 3 bytes per sample (little-endian signed int24).

    Args:
        samples: float32 ndarray (flat, interleaved stereo).

    Returns:
        ``bytes`` of length ``len(samples) * 3``.
    """
    import numpy as np

    # Clip to [-1, 1) and scale to 24-bit range
    clamped = np.clip(samples, -1.0, 1.0 - 1.0 / 8388608.0)
    int32_vals = (clamped * 8388607.0).astype(np.int32)

    # Pack each int32 into 3 bytes (little-endian)
    # byte0 = bits[0:8], byte1 = bits[8:16], byte2 = bits[16:24]
    raw = int32_vals.view(np.uint8).reshape(-1, 4)
    packed = raw[:, :3].tobytes()
    return packed


def unpack_int24_to_float32(data: bytes) -> numpy.ndarray:
    """Convert packed 24-bit little-endian PCM bytes to a float32 numpy array.

    Args:
        data: Raw PCM bytes (3 bytes per sample, little-endian signed int24).

    Returns:
        float32 ndarray, normalised to [-1.0, 1.0].
    """
    import numpy as np

    n_samples = len(data) // 3
    # Pad each 3-byte value to 4 bytes (add a zero high byte)
    raw = np.frombuffer(data, dtype=np.uint8).reshape(n_samples, 3)
    padded = np.zeros((n_samples, 4), dtype=np.uint8)
    padded[:, :3] = raw
    int32_vals = padded.view(np.int32).reshape(n_samples)
    # Sign-extend: shift left 8 then arithmetic shift right 8
    int32_vals = (int32_vals << 8).astype(np.int32) >> 8
    return int32_vals.astype(np.float32) / 8388607.0


__all__ = [
    "BYTES_PER_SAMPLE",
    "BYTES_PER_SAMPLE_24",
    "CHANNELS",
    "CHUNK_DURATION_MS",
    "CHUNK_SIZE_BYTES",
    "CHUNK_SIZE_BYTES_24",
    "FLAG_SILENCE",
    "FORMAT_VERSION_16BIT",
    "FORMAT_VERSION_24BIT",
    "FRAME_SIZE",
    "FRAME_SIZE_24",
    "HEADER_FORMAT",
    "HEADER_SIZE",
    "SAMPLES_PER_CHUNK",
    "SAMPLE_RATE",
    "SILENCE_PCM",
    "SILENCE_PCM_24",
    "PCMChunk",
    "PCMChunkHeader",
    "bytes_to_duration_ms",
    "chunk_duration_seconds",
    "deserialize",
    "duration_ms_to_bytes",
    "pack_float32_to_int24",
    "serialize",
    "unpack_int24_to_float32",
]
