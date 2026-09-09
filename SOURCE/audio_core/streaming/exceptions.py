"""
Streaming-specific exceptions for multi-room audio sync.

These exceptions inherit from the core ServiceError hierarchy and provide
specific error types for PCM streaming operations.
"""

from __future__ import annotations

from core.exceptions import ErrorContext, ServiceError


class StreamingError(ServiceError):
    """Base exception for PCM streaming errors."""

    def __init__(self, message: str, context: ErrorContext | None = None):
        if context is None:
            context = ErrorContext(
                component="audio_core.streaming",
                operation="stream",
                user_message="Audio streaming encountered an issue.",
                recovery_hint="Check network connectivity and audio settings.",
            )
        super().__init__(message, context)


class BufferUnderrunError(StreamingError):
    """
    Raised when playback buffer is depleted faster than it can be filled.

    This typically indicates network latency issues or insufficient buffering.
    """

    def __init__(
        self,
        buffer_level_ms: float = 0.0,
        target_buffer_ms: float = 0.0,
    ):
        super().__init__(
            f"Buffer underrun: level={buffer_level_ms:.1f}ms, target={target_buffer_ms:.1f}ms",
            ErrorContext(
                component="audio_core.streaming.playback_scheduler",
                operation="read",
                params={
                    "buffer_level_ms": buffer_level_ms,
                    "target_buffer_ms": target_buffer_ms,
                },
                user_message="Audio playback stuttered due to network lag.",
                recovery_hint="Check network connection or increase buffer size.",
            ),
        )
        self.buffer_level_ms = buffer_level_ms
        self.target_buffer_ms = target_buffer_ms


class BufferOverrunError(StreamingError):
    """
    Raised when incoming chunks arrive faster than they can be consumed.

    This typically indicates playback stall or clock drift between hub and spoke.
    """

    def __init__(
        self,
        buffer_level_ms: float = 0.0,
        max_buffer_ms: float = 0.0,
    ):
        super().__init__(
            f"Buffer overrun: level={buffer_level_ms:.1f}ms, max={max_buffer_ms:.1f}ms",
            ErrorContext(
                component="audio_core.streaming.playback_scheduler",
                operation="add_chunk",
                params={
                    "buffer_level_ms": buffer_level_ms,
                    "max_buffer_ms": max_buffer_ms,
                },
                user_message="Audio buffer overflow - playback may be stalled.",
                recovery_hint="Check if audio output device is working.",
            ),
        )
        self.buffer_level_ms = buffer_level_ms
        self.max_buffer_ms = max_buffer_ms


class ChunkDeserializationError(StreamingError):
    """
    Raised when a PCM chunk cannot be deserialized from wire format.

    This typically indicates protocol mismatch or corrupted data.
    """

    def __init__(
        self,
        reason: str = "",
        received_bytes: int = 0,
        expected_bytes: int = 0,
    ):
        msg = "Failed to deserialize PCM chunk"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            ErrorContext(
                component="audio_core.streaming.chunk_protocol",
                operation="deserialize",
                params={
                    "received_bytes": received_bytes,
                    "expected_bytes": expected_bytes,
                },
                user_message="Received invalid audio data from server.",
                recovery_hint="Reconnect to the streaming server.",
            ),
        )
        self.reason = reason
        self.received_bytes = received_bytes
        self.expected_bytes = expected_bytes


__all__ = [
    "BufferOverrunError",
    "BufferUnderrunError",
    "ChunkDeserializationError",
    "StreamingError",
]
