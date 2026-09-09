"""
Windows WASAPI sink implementation built on sounddevice/PortAudio bindings.

The sink operates in shared mode and honours the latency target supplied in
`SinkConfiguration`. Exclusive mode is rejected during configuration because
only shared mode is implemented.

AEC Support:
    This sink includes an optional AEC reference buffer that captures
    playback audio for use in Acoustic Echo Cancellation. Enable by
    passing enable_aec_reference=True to __init__.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from audio_core.decoder.buffer_manager import BufferUnderrunError
from core.constants import TIMEOUT_GRACE
from core.logging_config import get_logger

from .sink_interface import (
    AudioSink,
    AudioSinkState,
    PCMFormat,
    PCMSource,
    SinkConfiguration,
    SinkConfigurationError,
    SinkIOError,
    SinkTelemetry,
)

if TYPE_CHECKING:
    import numpy as np

    from .aec_reference_buffer import AECReferenceBuffer


class WASAPISink(AudioSink):
    """Shared-mode WASAPI sink using sounddevice's raw stream interface."""

    def __init__(
        self,
        *,
        sink_id: str,
        telemetry: SinkTelemetry | None = None,
        logger: logging.Logger | None = None,
        chunk_frames: int = 2048,
        enable_aec_reference: bool = True,
        aec_buffer_duration_ms: int = 500,
        aec_delay_ms: int = 50,
    ) -> None:
        super().__init__(sink_id=sink_id, telemetry=telemetry)
        self._logger = logger or get_logger("audio_core.sinks.wasapi")
        self._chunk_frames = max(256, chunk_frames)

        self._config: SinkConfiguration | None = None
        self._source: PCMSource | None = None
        self._sd = None  # sounddevice module loaded lazily
        self._stream = None
        self._dtype: str | None = None
        self._device_ref = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._underruns = 0
        self._latency_samples: list[float] = []
        self._stream_lock = threading.Lock()

        # AEC reference buffer (initialized on configure when sample rate is known)
        self._enable_aec_reference = enable_aec_reference
        self._aec_buffer_duration_ms = aec_buffer_duration_ms
        self._aec_delay_ms = aec_delay_ms
        self._aec_reference_buffer: AECReferenceBuffer | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def configure(self, config: SinkConfiguration) -> None:
        self._config = config
        self._sd = self._import_sounddevice()
        self._dtype = self._resolve_dtype(config.format)
        self._device_ref = self._resolve_device(config.device_id)

        if config.exclusive_mode:
            raise SinkConfigurationError("WASAPISink currently supports shared mode only; set exclusive_mode=False")

        blocksize = self._determine_blocksize(config)
        self._chunk_frames = blocksize // config.format.frame_size_bytes
        self._ensure_settings(config, blocksize)

        # Initialize AEC reference buffer if enabled
        if self._enable_aec_reference:
            from .aec_reference_buffer import AECReferenceBuffer

            self._aec_reference_buffer = AECReferenceBuffer(
                buffer_duration_ms=self._aec_buffer_duration_ms,
                sample_rate=config.format.sample_rate_hz,
                delay_ms=self._aec_delay_ms,
            )
            self._logger.debug(
                "AEC reference buffer initialized for sink %s (rate=%dHz)",
                self.sink_id,
                config.format.sample_rate_hz,
            )

        self._update_state(AudioSinkState.CONFIGURED)

    def start(self, source: PCMSource) -> None:
        if self._config is None or self._sd is None or self._dtype is None:
            raise RuntimeError("WASAPISink.start called before configure")

        if self._worker and self._worker.is_alive():
            self.stop()

        self._source = source
        self._stop_event.clear()
        self._pause_event.set()
        self._underruns = 0
        self._latency_samples.clear()

        try:
            self._stream = self._create_stream(self._config)
        except Exception as exc:  # pragma: no cover - sounddevice specific
            raise SinkIOError(f"Failed to open WASAPI stream: {exc}") from exc

        self._worker = threading.Thread(target=self._run_loop, name="wasapi-sink", daemon=True)
        self._worker.start()
        self._update_state(AudioSinkState.STARTING)

    def pause(self) -> None:
        if self._stream:
            with self._stream_lock:
                try:
                    self._stream.stop()
                except Exception:  # pragma: no cover
                    self._logger.warning("WASAPI pause stop failed")
        self._pause_event.clear()
        self._update_state(AudioSinkState.PAUSED)

    def resume(self) -> None:
        self._pause_event.set()
        if self._stream:
            with self._stream_lock:
                try:
                    self._stream.start()
                except Exception as exc:  # pragma: no cover
                    raise SinkIOError(f"Failed to resume WASAPI stream: {exc}") from exc
        self._update_state(AudioSinkState.PLAYING)

    def stop(self, *, drain: bool = False) -> None:
        self._stop_event.set()
        self._pause_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=TIMEOUT_GRACE)
        self._worker = None
        if self._stream:
            with self._stream_lock:
                try:
                    if drain:
                        self._stream.stop()
                    self._stream.close()
                except Exception:  # pragma: no cover
                    self._logger.warning("Failed to close WASAPI stream")
            self._stream = None
        self._update_state(AudioSinkState.STOPPED)

    def teardown(self) -> None:
        self.stop()
        self._sd = None
        self._update_state(AudioSinkState.IDLE)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _determine_blocksize(self, config: SinkConfiguration) -> int:
        if config.buffer_frames:
            return int(max(config.buffer_frames * config.format.frame_size_bytes, 256))
        bytes_per_frame = config.format.frame_size_bytes
        frames = max(
            256,
            int(config.format.sample_rate_hz * (config.latency_target_ms / 1000.0)),
        )
        return int(frames * bytes_per_frame)

    def _resolve_dtype(self, fmt: PCMFormat) -> str:
        if fmt.is_float:
            return "float32"
        mapping = {8: "int8", 16: "int16", 24: "int24", 32: "int32"}
        dtype = mapping.get(fmt.bits_per_sample)
        if dtype is None:
            raise SinkConfigurationError(f"Unsupported bit depth: {fmt.bits_per_sample}")
        if dtype == "int24":
            # PortAudio expects packed 3-byte data; the decoder emits this layout
            return "int24"
        return dtype

    def _import_sounddevice(self):
        try:
            import sounddevice as sd

            return sd
        except ImportError as exc:
            raise SinkConfigurationError(
                "sounddevice package is required for WASAPI playback. Install via 'pip install sounddevice'."
            ) from exc

    def _resolve_device(self, device_id: str | None):
        if device_id is None:
            return None
        # _sd is set in configure(), but check defensively
        if self._sd is None:
            return None

        if device_id.isdigit():
            return int(device_id)

        try:
            index_component = device_id.rsplit("_", 1)[-1]
            return int(index_component)
        except (ValueError, AttributeError):
            pass

        from audio_core.portaudio_guard import sounddevice_guard

        with sounddevice_guard():
            devices = self._sd.query_devices()
        for index, info in enumerate(devices):
            if isinstance(info, dict) and info.get("name") == device_id:
                return index
        return None

    def _ensure_settings(self, config: SinkConfiguration, blocksize_bytes: int) -> None:
        # These should be set by configure(), but check defensively
        if self._sd is None or self._dtype is None:
            return

        try:
            self._sd.check_output_settings(
                samplerate=config.format.sample_rate_hz,
                channels=config.format.channels,
                dtype=self._dtype,
                device=self._device_ref,
            )
        except Exception as exc:  # pragma: no cover
            raise SinkConfigurationError(f"WASAPI settings rejected: {exc}") from exc

    def _create_stream(self, config: SinkConfiguration):
        assert self._sd is not None
        assert self._dtype is not None

        extra_settings = None
        if hasattr(self._sd, "WasapiSettings"):
            extra_settings = self._sd.WasapiSettings(
                exclusive=False,  # Shared mode
            )
        elif config.latency_target_ms < 80:
            self._logger.warning(
                "Low latency target requested but sounddevice lacks WasapiSettings; falling back to default latency."
            )

        stream = self._sd.RawOutputStream(
            samplerate=config.format.sample_rate_hz,
            channels=config.format.channels,
            dtype=self._dtype,
            device=self._device_ref,
            blocksize=self._chunk_frames,
            finished_callback=self._on_stream_finished,
            extra_settings=extra_settings,
        )
        stream.start()
        return stream

    def _on_stream_finished(self) -> None:
        self._logger.debug("WASAPI stream finished callback invoked")

    # ------------------------------------------------------------------ #
    # Worker loop
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        assert self._config is not None
        assert self._source is not None
        assert self._stream is not None
        stream = self._stream

        bytes_per_second = self._config.format.bytes_per_second
        chunk_bytes = self._chunk_frames * self._config.format.frame_size_bytes

        self._update_state(AudioSinkState.PLAYING)

        while not self._stop_event.is_set():
            self._handle_pause_state()
            if not self._process_audio_chunk(stream, chunk_bytes, bytes_per_second):
                break

        self._update_state(AudioSinkState.DRAINING)

    def _handle_pause_state(self) -> None:
        """Handle pause state by sleeping briefly."""
        if not self._pause_event.is_set():
            time.sleep(0.01)

    def _process_audio_chunk(self, stream, chunk_bytes: int, bytes_per_second: float) -> bool:
        """Process one audio chunk. Returns False if loop should exit."""
        assert self._source is not None
        if self._stop_event.is_set():
            return False

        try:
            payload = self._source.read(chunk_bytes, block=True)
        except BufferUnderrunError:
            self._handle_underrun()
            return True

        if not payload:
            return True

        self._write_audio_chunk(stream, payload)
        self._handle_playback_timing(payload, bytes_per_second)
        return True

    def _handle_underrun(self) -> None:
        """Handle buffer underrun situation."""
        assert self._config is not None
        self._underruns += 1
        self._update_state(AudioSinkState.UNDERFLOW)
        if self._telemetry:
            try:
                self._telemetry.emit_underrun(sink_id=self.sink_id, underrun_count=self._underruns)
            except Exception:  # pragma: no cover
                self._logger.exception("WASAPI underrun telemetry failed")
        time.sleep(self._config.latency_target_ms / 1000.0)

    def _write_audio_chunk(self, stream, payload: bytes) -> None:
        """Write audio chunk to stream and track latency."""
        try:
            # Record to AEC reference buffer before writing to speakers
            if self._aec_reference_buffer is not None and self._config is not None:
                self._aec_reference_buffer.write(
                    payload,
                    channels=self._config.format.channels,
                    bits_per_sample=self._config.format.bits_per_sample,
                    is_float=self._config.format.is_float,
                )

            write_start = time.perf_counter()
            stream.write(payload)
            duration = time.perf_counter() - write_start
            self._track_latency(duration)
        except Exception as exc:  # pragma: no cover - PortAudio runtime failures
            self._logger.exception("WASAPI stream write failed")
            self._update_state(AudioSinkState.ERROR)
            raise SinkIOError(f"WASAPI stream write failed: {exc}") from exc

    def _track_latency(self, duration: float) -> None:
        """Track latency measurements and emit telemetry."""
        self._latency_samples.append(duration)
        if len(self._latency_samples) > 120:
            self._latency_samples = self._latency_samples[-120:]

        if self._telemetry and len(self._latency_samples) % 10 == 0:
            try:
                average_latency_ms = sum(self._latency_samples[-10:]) / 10.0 * 1000.0
                self._telemetry.emit_latency(sink_id=self.sink_id, latency_ms=average_latency_ms)
            except Exception:  # pragma: no cover
                self._logger.exception("WASAPI latency telemetry failed")

    def _handle_playback_timing(self, payload: bytes, bytes_per_second: float) -> None:
        """Handle playback timing by sleeping for the appropriate duration."""
        playback_time = len(payload) / bytes_per_second if bytes_per_second else 0.0
        if playback_time > 0:
            time.sleep(playback_time)

    # ------------------------------------------------------------------ #
    # AEC Reference API                                                   #
    # ------------------------------------------------------------------ #

    def get_aec_reference_frame(
        self,
        frame_samples: int,
        target_rate: int | None = None,
    ) -> np.ndarray | None:
        """
        Get a frame of playback audio for AEC reference.

        Args:
            frame_samples: Number of samples to return (at target_rate)
            target_rate: Target sample rate for resampling (None = buffer's native rate)

        Returns:
            Audio frame as int16 numpy array, or None if AEC not enabled
        """
        if self._aec_reference_buffer is None:
            return None
        return self._aec_reference_buffer.get_frame(frame_samples, target_rate)

    def has_aec_reference(self) -> bool:
        """Check if AEC reference buffer is available."""
        return self._aec_reference_buffer is not None

    def set_aec_delay(self, delay_ms: int) -> None:
        """Update the AEC delay compensation."""
        if self._aec_reference_buffer is not None:
            self._aec_reference_buffer.set_delay(delay_ms)
            self._aec_delay_ms = delay_ms


__all__ = ["WASAPISink"]
