"""
Cross-platform audio sink contracts.

Batch E introduces a production-grade output layer that decouples device-
specific drivers from the core audio pipeline. The definitions in this module
provide:

* Stream format descriptions shared across sinks.
* Lifecycle contracts for pull-based playback adapters.
* Telemetry hooks to keep underruns, latency, and device churn observable.
* Device descriptors used by the capability registry / device manager.

Platform-specific sinks (e.g. WASAPI, CoreAudio, ALSA) must implement the
`AudioSink` abstract base class and honour the timing semantics described
below.

Threading model
---------------
Sinks execute in worker threads and pull PCM frames from a `PCMSource`
implementation (the default provider is `audio_core.decoder.BufferManager`).
Implementations may spawn additional threads to integrate with platform APIs,
but they must never block the caller thread during `start`, `stop`, `pause`,
or `resume` for longer than `SinkConfiguration.control_timeout_sec`.

Buffer contract
---------------
`PCMSource.read` obeys the same semantics as `BufferManager.read`; sinks should
handle `BufferUnderrunError` by emitting telemetry and entering
`AudioSinkState.UNDERFLOW` until data becomes available again.

Telemetry
---------
Metrics/Events surface through the lightweight `SinkTelemetry` protocol so the
audio governance board can audit latency budgets (<150 ms end-to-end) without
tight coupling to the decoder telemetry module.
"""

from __future__ import annotations

import abc
import enum
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from core.logging_config import get_logger

logger = get_logger(__name__)


class AudioSinkError(RuntimeError):
    """Base exception raised by sink implementations."""


class SinkConfigurationError(AudioSinkError):
    """Raised when configuration settings are invalid for the selected sink."""


class SinkIOError(AudioSinkError):
    """Raised when the sink cannot push audio to the device (device fault)."""


class AudioSinkState(enum.Enum):
    """High-level lifecycle states shared by all sinks."""

    IDLE = "idle"
    CONFIGURED = "configured"
    STARTING = "starting"
    PLAYING = "playing"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPED = "stopped"
    UNDERFLOW = "underflow"
    ERROR = "error"


@dataclass(slots=True)
class PCMFormat:
    """PCM stream characteristics supplied by the decoder pipeline."""

    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    is_float: bool = False
    channel_layout: str = "stereo"

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if self.bits_per_sample not in (8, 16, 24, 32):
            raise ValueError("bits_per_sample must be one of {8,16,24,32}")
        if self.is_float and self.bits_per_sample != 32:
            raise ValueError("floating PCM must use 32 bits per sample")

    @property
    def frame_size_bytes(self) -> int:
        """Number of bytes per frame (all channels for a single sample)."""
        return (self.bits_per_sample // 8) * self.channels

    @property
    def bytes_per_second(self) -> int:
        """Throughput in bytes per second."""
        return self.frame_size_bytes * self.sample_rate_hz


@dataclass(slots=True)
class SinkConfiguration:
    """Run-time configuration passed to sinks prior to start."""

    format: PCMFormat
    device_id: str | None = None
    latency_target_ms: int = 120
    buffer_frames: int | None = None
    shared_mode: bool = True
    exclusive_mode: bool = False
    telemetry_device_label: str | None = None
    control_timeout_sec: float = 3.0

    def __post_init__(self) -> None:
        if self.latency_target_ms <= 0:
            raise ValueError("latency_target_ms must be positive")
        if self.control_timeout_sec <= 0:
            raise ValueError("control_timeout_sec must be positive")
        if self.shared_mode and self.exclusive_mode:
            raise ValueError("shared_mode and exclusive_mode cannot both be True")


@dataclass(slots=True)
class AudioDeviceDescriptor:
    """Metadata about an output device exposed by the device manager."""

    id: str
    name: str
    api: str
    is_default: bool
    sample_rates_hz: Sequence[int]
    max_channels: int
    default_format: PCMFormat
    latency_ms: int = 80
    is_plugged: bool = True
    capabilities: Sequence[str] = field(default_factory=tuple)

    def supports_format(self, fmt: PCMFormat) -> bool:
        """Return True when the device can handle the requested format."""
        if fmt.sample_rate_hz not in self.sample_rates_hz:
            return False
        if fmt.channels > self.max_channels:
            return False
        if fmt.bits_per_sample > self.default_format.bits_per_sample:
            return False
        return True


class PCMSource(Protocol):
    """Protocol implemented by the decoder buffer or test doubles."""

    def read(self, num_bytes: int, *, block: bool = True) -> bytes: ...

    def available_read(self) -> int: ...


class SinkTelemetry(Protocol):
    """Telemetry hooks supplied by the audio governance pipeline."""

    def emit_state_change(self, *, sink_id: str, state: AudioSinkState) -> None: ...

    def emit_latency(self, *, sink_id: str, latency_ms: float) -> None: ...

    def emit_underrun(self, *, sink_id: str, underrun_count: int) -> None: ...

    def emit_device_event(self, *, sink_id: str, event: str, details: dict) -> None: ...


class SinkObserver(Protocol):
    """Observer invoked when sink state transitions occur."""

    def __call__(self, new_state: AudioSinkState) -> None: ...


class AudioSink(abc.ABC):
    """
    Abstract base class for platform-specific PCM sinks.

    Implementations must be thread-safe. Public methods are invoked by the
    playback orchestrator while `run` loops or callbacks execute in
    background threads owned by the sink.
    """

    def __init__(
        self,
        *,
        sink_id: str,
        telemetry: SinkTelemetry | None = None,
    ) -> None:
        self._sink_id = sink_id
        self._telemetry = telemetry
        self._state = AudioSinkState.IDLE
        self._state_lock = threading.RLock()
        self._observers: list[SinkObserver] = []

    # --------------------------------------------------------------------- #
    # Observable state helpers
    # --------------------------------------------------------------------- #

    @property
    def sink_id(self) -> str:
        """Stable identifier derived from device id + backend."""
        return self._sink_id

    @property
    def state(self) -> AudioSinkState:
        """Thread-safe accessor for the current sink state."""
        with self._state_lock:
            return self._state

    def register_observer(self, observer: SinkObserver) -> None:
        """Register a callback executed on state transitions."""
        with self._state_lock:
            if observer not in self._observers:
                self._observers.append(observer)

    def unregister_observer(self, observer: SinkObserver) -> None:
        """Remove a previously registered observer."""
        with self._state_lock:
            if observer in self._observers:
                self._observers.remove(observer)

    def _update_state(self, new_state: AudioSinkState) -> None:
        with self._state_lock:
            if self._state == new_state:
                return
            self._state = new_state
            observers = list(self._observers)
        if self._telemetry:
            try:
                self._telemetry.emit_state_change(sink_id=self._sink_id, state=new_state)
            except Exception as e:  # pragma: no cover - telemetry failures must not crash sink
                logger.debug("Telemetry state change emission failed: %s", e, exc_info=True)
                pass
        for observer in observers:
            try:
                observer(new_state)
            except Exception as e:  # pragma: no cover - observer isolation
                logger.debug("Observer notification failed: %s", e, exc_info=True)
                pass

    # --------------------------------------------------------------------- #
    # Lifecycle hooks
    # --------------------------------------------------------------------- #

    @abc.abstractmethod
    def configure(self, config: SinkConfiguration) -> None:
        """
        Prepare the sink for playback.

        Called once per playback session before `start`. Implementations should
        validate that the requested format and latency can be satisfied and
        raise `SinkConfigurationError` if constraints cannot be met.
        """

    @abc.abstractmethod
    def start(self, source: PCMSource) -> None:
        """
        Begin playback by consuming PCM frames from `source`.

        Implementations must spawn their own worker loop and return before the
        control timeout expires. Errors during startup should raise
        `SinkIOError` and transition the sink into `ERROR`.
        """

    @abc.abstractmethod
    def pause(self) -> None:
        """Pause playback while retaining buffers."""

    @abc.abstractmethod
    def resume(self) -> None:
        """Resume playback from paused state."""

    @abc.abstractmethod
    def stop(self, *, drain: bool = False) -> None:
        """
        Stop playback and optionally drain remaining frames.

        This call must return once the device is silent. Implementations should
        tolerate redundant `stop` invocations.
        """

    @abc.abstractmethod
    def teardown(self) -> None:
        """
        Release underlying device handles and worker threads.

        Called when the sink is being disposed of. Implementations must be
        idempotent.
        """

    # ------------------------------------------------------------------ #
    # Optional helpers that sinks may override
    # ------------------------------------------------------------------ #

    def supported_formats(self) -> Iterable[PCMFormat]:
        """
        Return formats supported without reconfiguration.

        Implementations may override to expose pre-computed capabilities.
        """
        return ()


__all__ = [
    "AudioDeviceDescriptor",
    "AudioSink",
    "AudioSinkError",
    "AudioSinkState",
    "PCMFormat",
    "PCMSource",
    "SinkConfiguration",
    "SinkConfigurationError",
    "SinkIOError",
    "SinkObserver",
    "SinkTelemetry",
]
