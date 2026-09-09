"""Audio sink implementations and interfaces.

The `audio_core.sinks` package exposes platform-neutral contracts plus concrete
adapters (e.g. WASAPI) that connect the decoder buffer to physical devices.

See `audio_core.sinks.sink_interface` for the canonical sink contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .null_sink import NullSink
from .sink_interface import (
    AudioDeviceDescriptor,
    AudioSink,
    AudioSinkError,
    AudioSinkState,
    PCMFormat,
    PCMSource,
    SinkConfiguration,
    SinkConfigurationError,
    SinkIOError,
    SinkObserver,
    SinkTelemetry,
)

if TYPE_CHECKING:
    from .aec_reference_buffer import AECReferenceBuffer
    from .wasapi_sink import WASAPISink


def __getattr__(name: str) -> object:
    if name == "AECReferenceBuffer":
        from .aec_reference_buffer import AECReferenceBuffer

        return AECReferenceBuffer
    if name == "WASAPISink":
        from .wasapi_sink import WASAPISink

        return WASAPISink
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AECReferenceBuffer",
    "AudioDeviceDescriptor",
    "AudioSink",
    "AudioSinkError",
    "AudioSinkState",
    "NullSink",
    "PCMFormat",
    "PCMSource",
    "SinkConfiguration",
    "SinkConfigurationError",
    "SinkIOError",
    "SinkObserver",
    "SinkTelemetry",
    "WASAPISink",
]
