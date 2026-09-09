"""
Audio decoder toolkit providing FFmpeg-backed decoding, buffering, and telemetry.

This package exposes the primary components required to assemble the decode
pipeline:

- FFmpegDecoder: subprocess-managed decode-only wrapper around FFmpeg.
- BufferManager: lock-aware ring buffer abstraction with watermark callbacks.
- DecoderTelemetry: structured logging/metrics publisher for observability.

The modules are intentionally decoupled so that downstream integrations (e.g.,
device sinks, queue managers) can compose them without inheriting transport or
state assumptions.
"""

from __future__ import annotations

from .buffer_manager import (
    BufferConfiguration,
    BufferManager,
    BufferMetrics,
    BufferOverflowError,
    BufferUnderrunError,
)
from .ffmpeg_decoder import (
    CodecNotSupportedError,
    DecoderConfiguration,
    DecoderIOError,
    DecoderStartupError,
    FFmpegDecoder,
    ProbeResult,
)
from .telemetry import DecoderTelemetry, TelemetryMetric

__all__ = [
    "BufferConfiguration",
    "BufferManager",
    "BufferMetrics",
    "BufferOverflowError",
    "BufferUnderrunError",
    "CodecNotSupportedError",
    "DecoderConfiguration",
    "DecoderIOError",
    "DecoderStartupError",
    "DecoderTelemetry",
    "FFmpegDecoder",
    "ProbeResult",
    "TelemetryMetric",
]
