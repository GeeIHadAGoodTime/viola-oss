"""
music/backends/simple_av.py

In-process PyAV decode session for SimpleBackend.

Shaped like the tiny subset of the ``subprocess.Popen`` surface SimpleBackend
uses for its ffmpeg CLI pipeline (``.stdout.read()/.close()``, ``.stderr``,
``.terminate()``, ``.wait()``, ``.kill()``), so the existing playback loop,
pause-drain, AudioTee and direct-injection logic run unchanged on top of it.

Why this exists (requal-M1): the frozen desktop bundle ships PyAV
(``_internal/av`` + ``_internal/av.libs``, ffmpeg's own shared libraries,
pulled in by faster-whisper) but no ``ffmpeg.exe``.  On a clean machine
``shutil.which("ffmpeg")`` returns ``None`` and music playback died at
construction with ``RuntimeError: ffmpeg binary not found`` →
``BackendError: No functional backend available``.  PyAV decodes through the
same ffmpeg libraries in-process, so the frozen app produces real playback
with zero system dependencies.

Design notes:
- A dedicated producer thread owns the ``av`` container exclusively (libav
  objects are not safely shared across threads) and pushes interleaved
  s16le PCM chunks into a bounded queue — the bounded queue replicates the
  natural back-pressure of a full OS pipe in the CLI pipeline.
- The consumer (SimpleBackend's playback thread) reads via the file-like
  ``stdout`` adapter; ``read()`` returns ``b""`` at end-of-stream or after
  close/terminate, exactly like a drained pipe of a dead ffmpeg process.
- Decode errors end the stream (EOF) rather than raising into the playback
  loop — identical observable behavior to an ffmpeg process exiting.

THE DECODE PATH MUST NOT CREATE NATIVE THREADS (requal-M1 freeze):
``av.AudioResampler`` builds an avfilter graph whose configuration calls
``avpriv_slicethread_create`` — spawning worker threads while the producer
holds the GIL.  In the frozen app this deadlocks the entire process: the new
native thread blocks on the Windows loader lock (DllMain THREAD_ATTACH),
a boot-time import thread holds the loader lock while waiting for the GIL,
and the producer holds the GIL while waiting for the thread to start.
py-spy native dump (2026-06-10, frozen dist): ``_produce → resampler.pyd →
avfilter graph → avpriv_slicethread_create → SleepConditionVariableSRW``
with MainThread stuck in ``PyGILState_Ensure``.  Therefore: decode with
``thread_count = 1`` and resample with the already-bundled ``soxr``
(streaming API, in-thread, no thread creation) + numpy format/layout
conversion.  Do not reintroduce ``AudioResampler`` here — the ratchet gate
(scripts/check_frozen_music_decoder.py) fails the build if it reappears.
"""

from __future__ import annotations

import queue
import subprocess
import threading
from functools import lru_cache

from core.logging_config import get_logger

# ~64 chunks of typical resampled frame size (~4 KiB) ≈ 0.25-1.5 s of audio
# buffered ahead — comparable to the OS pipe buffer the CLI pipeline had.
_QUEUE_MAX_CHUNKS = 64
_AV_TIME_BASE = 1_000_000  # libav AV_TIME_BASE: container timestamps in µs


@lru_cache(maxsize=1)
def av_available() -> bool:
    """True when the PyAV package (and its bundled ffmpeg libs) imports."""
    try:
        import av
    except (ModuleNotFoundError, OSError):
        return False
    return True


def probe_duration_seconds(source: str) -> float | None:
    """Duration of `source` in seconds via PyAV, or None when unknown."""
    if not av_available():
        return None
    import av

    try:
        with av.open(source, metadata_errors="ignore") as container:
            if container.duration is not None:
                return float(container.duration) / _AV_TIME_BASE
            for stream in container.streams:
                if stream.type == "audio" and stream.duration is not None and stream.time_base is not None:
                    return float(stream.duration * stream.time_base)
    except (OSError, av.error.FFmpegError, ValueError, TypeError):
        return None
    return None


class _AVStdout:
    """File-like PCM reader over the decode queue (Popen.stdout shape)."""

    def __init__(self, session: AVDecodeSession) -> None:
        self._session = session
        self._buffer = bytearray()
        self._eof = False

    def read(self, size: int = -1) -> bytes:
        session = self._session
        while not self._eof and (size < 0 or len(self._buffer) < size):
            if session._stop_event.is_set():
                break
            try:
                chunk = session._queue.get(timeout=0.1)
            except queue.Empty:
                if session._producer_done.is_set() and session._queue.empty():
                    self._eof = True
                continue
            if not chunk:  # EOF sentinel
                self._eof = True
                break
            self._buffer.extend(chunk)

        if size < 0 or len(self._buffer) <= size:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def close(self) -> None:
        # Popen.stdout.close() in _stop_locked() signals teardown: stop the
        # producer so it never blocks on a full queue nobody drains.
        self._session._stop_event.set()

    @property
    def closed(self) -> bool:
        return self._session._stop_event.is_set()


class AVDecodeSession:
    """
    In-process PyAV decode of `source` to interleaved s16le PCM.

    Mimics the Popen surface SimpleBackend/_stop_locked relies on:
    ``stdout`` (read/close), ``stderr`` (None — no process, no stderr pipe;
    ``_drain_stderr`` no-ops on None), ``terminate()``, ``kill()``,
    ``wait(timeout)``, ``poll()``.
    """

    def __init__(
        self,
        source: str,
        *,
        offset: float = 0.0,
        sample_rate: int = 48_000,
        channels: int = 2,
        logger=None,
    ) -> None:
        if not av_available():  # pragma: no cover - guarded by caller
            raise RuntimeError("PyAV (av) is not importable; AVDecodeSession unavailable")
        self._source = source
        self._offset = max(0.0, float(offset))
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._logger = logger or get_logger(__name__)

        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_MAX_CHUNKS)
        self._stop_event = threading.Event()
        self._producer_done = threading.Event()
        self._returncode: int | None = None

        self.stdout = _AVStdout(self)
        self.stderr = None  # in-process: decode errors are logged, not piped

        self._producer = threading.Thread(
            target=self._produce,
            name="AVDecodeSession",
            daemon=True,
        )
        self._producer.start()

    # ---------- Popen-shaped control surface ----------

    def terminate(self) -> None:
        self._stop_event.set()

    def kill(self) -> None:
        self._stop_event.set()

    def wait(self, timeout: float | None = None) -> int:
        self._producer.join(timeout=timeout)
        if self._producer.is_alive():
            raise subprocess.TimeoutExpired(cmd="av-decode", timeout=timeout or 0.0)
        return self._returncode if self._returncode is not None else 0

    def poll(self) -> int | None:
        if self._producer_done.is_set():
            return self._returncode if self._returncode is not None else 0
        return None

    # ---------- producer ----------

    def _put(self, data: bytes) -> bool:
        """Bounded put with stop-aware back-pressure. False = stopping."""
        while not self._stop_event.is_set():
            try:
                self._queue.put(data, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _frame_to_float32(self, frame) -> object:
        """Decoded AVFrame → float32 ndarray shaped (samples, target_channels)."""
        import numpy as np

        arr = frame.to_ndarray()  # planar: (ch, n); packed: (1, n*ch)
        if arr.dtype == np.int16:
            x = arr.astype(np.float32) / 32768.0
        elif arr.dtype == np.int32:
            x = arr.astype(np.float32) / 2147483648.0
        elif arr.dtype == np.uint8:
            x = (arr.astype(np.float32) - 128.0) / 128.0
        else:  # float32 / float64
            x = arr.astype(np.float32, copy=False)

        nch = max(1, len(frame.layout.channels))
        if frame.format.is_planar:
            x = x.T  # (n, ch)
        else:
            x = x.reshape(-1, nch)

        if x.shape[1] == self._channels:
            return x
        if x.shape[1] == 1:
            return np.repeat(x, self._channels, axis=1)
        if x.shape[1] > self._channels:
            # Take the leading channels (front L/R for standard layouts).
            return np.ascontiguousarray(x[:, : self._channels])
        return np.column_stack([x[:, i % x.shape[1]] for i in range(self._channels)])

    def _produce(self) -> None:
        import av
        import numpy as np
        import soxr

        try:
            container = av.open(self._source, metadata_errors="ignore")
            try:
                audio_stream = next(
                    (stream for stream in container.streams if stream.type == "audio"),
                    None,
                )
                if audio_stream is None:
                    self._logger.warning("AVDecodeSession: no audio stream in %s", self._source)
                    self._returncode = 1
                    return
                # No codec worker threads — see module docstring (frozen-app
                # GIL/loader-lock deadlock class).  Audio codecs don't need
                # them; this guarantees zero native thread creation here.
                audio_stream.codec_context.thread_count = 1
                if self._offset > 0:
                    container.seek(int(self._offset * _AV_TIME_BASE))

                resampler = None  # soxr.ResampleStream, built on first frame
                int16_full_scale = 32767.0

                def emit(chunk_f32) -> bool:
                    if chunk_f32.size == 0:
                        return True
                    pcm = np.clip(chunk_f32, -1.0, 1.0)
                    return self._put((pcm * int16_full_scale).astype("<i2").tobytes())

                for frame in container.decode(audio_stream):
                    if self._stop_event.is_set():
                        return
                    x = self._frame_to_float32(frame)
                    if resampler is None:
                        in_rate = int(frame.sample_rate or audio_stream.codec_context.sample_rate)
                        resampler = soxr.ResampleStream(
                            in_rate,
                            self._sample_rate,
                            self._channels,
                            dtype="float32",
                        )
                    if not emit(resampler.resample_chunk(x)):
                        return
                if resampler is not None:
                    tail = resampler.resample_chunk(
                        np.zeros((0, self._channels), dtype=np.float32),
                        last=True,
                    )
                    if not emit(tail):
                        return
                self._returncode = 0
            finally:
                container.close()
        except (TypeError, ValueError, OSError, av.error.FFmpegError):
            # Same observable shape as ffmpeg CLI dying: stream ends (EOF),
            # the playback loop finishes, is_playing flips false.
            self._logger.exception("AVDecodeSession: decode failed for %s", self._source)
            self._returncode = 1
        finally:
            self._producer_done.set()
            try:
                self._queue.put_nowait(b"")  # EOF sentinel
            except queue.Full:
                pass  # reader checks _producer_done on empty-timeout
