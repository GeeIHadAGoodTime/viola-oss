"""
Null sink for automated testing and environments without audio hardware.

The null sink consumes PCM frames from the decoder buffer while maintaining
timing guarantees. It is useful for headless CI runs and latency benchmarking
without real speakers.
"""

from __future__ import annotations

import logging
import threading
import time

from audio_core.decoder.buffer_manager import BufferUnderrunError
from core.constants import TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

from .sink_interface import (
    AudioSink,
    AudioSinkState,
    PCMSource,
    SinkConfiguration,
    SinkTelemetry,
)


class NullSink(AudioSink):
    """Sink that discards audio while simulating playback timing."""

    def __init__(
        self,
        *,
        telemetry: SinkTelemetry | None = None,
        logger: logging.Logger | None = None,
        chunk_frames: int = 1024,
    ) -> None:
        super().__init__(sink_id="null", telemetry=telemetry)
        self._logger = logger or get_logger("audio_core.sinks.null")
        self._chunk_frames = max(1, chunk_frames)
        self._config: SinkConfiguration | None = None
        self._source: PCMSource | None = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._underruns = 0

    def configure(self, config: SinkConfiguration) -> None:
        self._config = config
        self._update_state(AudioSinkState.CONFIGURED)

    def start(self, source: PCMSource) -> None:
        if self._config is None:
            raise RuntimeError("NullSink.start called before configure")

        if self._worker and self._worker.is_alive():
            self.stop()

        self._source = source
        self._stop_event.clear()
        self._pause_event.set()

        self._worker = threading.Thread(target=self._run_loop, name="null-sink", daemon=True)
        self._worker.start()
        self._update_state(AudioSinkState.STARTING)

    def pause(self) -> None:
        self._pause_event.clear()
        self._update_state(AudioSinkState.PAUSED)

    def resume(self) -> None:
        self._pause_event.set()
        self._update_state(AudioSinkState.PLAYING)

    def stop(self, *, drain: bool = False) -> None:
        if self._worker and self._worker.is_alive():
            self._stop_event.set()
            self._pause_event.set()
            self._worker.join(timeout=TIMEOUT_SHUTDOWN)
        self._worker = None
        self._source = None
        self._update_state(AudioSinkState.STOPPED)

    def teardown(self) -> None:
        self.stop()
        self._update_state(AudioSinkState.IDLE)

    # ------------------------------------------------------------------ #
    # Internal worker
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        assert self._config is not None and self._source is not None
        bytes_per_second = self._config.format.bytes_per_second
        chunk_bytes = self._chunk_frames * self._config.format.frame_size_bytes

        self._update_state(AudioSinkState.PLAYING)

        while not self._stop_event.is_set():
            if not self._pause_event.is_set():
                time.sleep(0.01)
                continue

            try:
                payload = self._source.read(chunk_bytes, block=True)
            except BufferUnderrunError:
                self._underruns += 1
                self._update_state(AudioSinkState.UNDERFLOW)
                if self._telemetry:
                    try:
                        self._telemetry.emit_underrun(sink_id=self.sink_id, underrun_count=self._underruns)
                    except Exception:  # pragma: no cover
                        self._logger.exception("Null sink telemetry underrun emit failed")
                time.sleep(self._config.latency_target_ms / 1000.0)
                continue

            if not payload:
                time.sleep(self._config.latency_target_ms / 1000.0)
                continue

            if self.state != AudioSinkState.PLAYING:
                self._update_state(AudioSinkState.PLAYING)

            duration = len(payload) / bytes_per_second if bytes_per_second else 0.0
            if duration > 0:
                time.sleep(duration)

        self._update_state(AudioSinkState.DRAINING)


__all__ = ["NullSink"]
