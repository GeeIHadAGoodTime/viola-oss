"""
Continuous Chunk Stamper for multi-room audio streaming.

Maintains an uninterrupted stream of timestamped 20ms PCM chunks regardless
of whether the audio capture source is producing data.  When real audio is
captured, it is stamped and forwarded.  When no audio arrives within a 20ms
window, a silence chunk (zeros) is stamped and forwarded instead.

This guarantees that spoke jitter buffers always receive a steady stream of
chunks and never drain due to gaps between songs, during pauses, or during
silence on the hub.

Threading model:
    - A dedicated daemon thread runs a 20ms tick loop.
    - The capture callback pushes PCM data into a thread-safe queue.
    - Each tick, the stamper drains the queue and emits one stamped chunk
      (real audio or silence) per 20ms slot.
"""

from __future__ import annotations

import collections
import math
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from core.logging_config import get_logger

from .chunk_protocol import (
    CHUNK_DURATION_MS,
    CHUNK_SIZE_BYTES,
    CHUNK_SIZE_BYTES_24,
    FLAG_SILENCE,
    FORMAT_VERSION_16BIT,
    FORMAT_VERSION_24BIT,
    HEADER_SIZE,
    SILENCE_PCM,
    SILENCE_PCM_24,
    PCMChunk,
    PCMChunkHeader,
    serialize,
)

logger = get_logger(__name__)

# Ring buffer holds recent serialized frames for late-joining spokes.
# 2.5 seconds ≈ 125 chunks at 50/s.
RING_BUFFER_SIZE: int = 125

# PCM recording ring buffer: 10 seconds at 50 chunks/sec = 500 chunks.
# Used by debug endpoint for time-domain fidelity comparison.
PCM_RING_DEFAULT_CHUNKS: int = 500

# Maximum capture buffer size: 1 second of audio = 50 chunks.
# Safety valve for clock drift between ProcTap WASAPI (input HW clock) and
# the stamper tick thread (time.monotonic).  At 250ppm drift this trims
# ~1 chunk every ~80s — inaudible, discards already-stale data.
MAX_CAPTURE_BUF_BYTES: int = CHUNK_SIZE_BYTES * 50  # 192,000 bytes = 1s

# Bounded burst catch-up: when the OS scheduler parks the tick thread past
# whole 20ms slots, the missed slots' PCM is already sitting in _capture_buf.
# The loop bursts those backlogged chunks out immediately (spoke jitter
# buffers exist to absorb exactly such bursts) instead of deleting the slots.
# The burst is capped: past MAX_CATCHUP_CHUNKS slots (~500ms) the stall is
# pathological and the excess slots are forgiven.
MAX_CATCHUP_CHUNKS: int = 25

# Mach thread_policy_set constants (mach/thread_policy.h).
_DARWIN_THREAD_STANDARD_POLICY = 1  # THREAD_STANDARD_POLICY
_DARWIN_THREAD_TIME_CONSTRAINT_POLICY = 2  # THREAD_TIME_CONSTRAINT_POLICY
_DARWIN_TIME_CONSTRAINT_POLICY_COUNT = 4  # 4 x uint32 fields
_DARWIN_STANDARD_POLICY_COUNT = 0  # THREAD_STANDARD_POLICY_COUNT
# Real-time contract for the 20ms tick thread: every 20ms period we need
# ~2ms of computation, finished within a 10ms constraint, preemptible.
_DARWIN_RT_PERIOD_NS = 20_000_000
_DARWIN_RT_COMPUTATION_NS = 2_000_000
_DARWIN_RT_CONSTRAINT_NS = 10_000_000


def _darwin_promote_thread_to_realtime() -> tuple[Any, int] | None:
    """Promote the calling thread to Mach real-time scheduling (macOS).

    On macOS the scheduler parks a default-priority tick thread for
    30-111ms every ~10-20s on idle machines. QoS USER_INTERACTIVE and
    busy-spinning do NOT prevent the parks (measured 2026-07-04);
    THREAD_TIME_CONSTRAINT_POLICY does (0 stalls over 120s, worst-case
    0.1ms). Fail-open: on any failure, log and return None — never crash
    audio for a scheduling nicety.

    Returns:
        (libsystem, mach_thread_port) on success, for the symmetric
        revert in :func:`_darwin_revert_thread_realtime`; None on failure.
    """
    try:
        import ctypes

        libsystem = ctypes.CDLL(None, use_errno=True)

        class _MachTimebaseInfo(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        class _ThreadTimeConstraintPolicy(ctypes.Structure):
            _fields_ = [
                ("period", ctypes.c_uint32),
                ("computation", ctypes.c_uint32),
                ("constraint", ctypes.c_uint32),
                ("preemptible", ctypes.c_uint32),
            ]

        libsystem.mach_timebase_info.argtypes = [ctypes.POINTER(_MachTimebaseInfo)]
        libsystem.mach_timebase_info.restype = ctypes.c_int
        libsystem.mach_thread_self.argtypes = []
        libsystem.mach_thread_self.restype = ctypes.c_uint32
        # thread_policy_t is passed as void* so the revert can reuse the
        # same function pointer with a different policy struct.
        libsystem.thread_policy_set.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        libsystem.thread_policy_set.restype = ctypes.c_int

        timebase = _MachTimebaseInfo()
        if libsystem.mach_timebase_info(ctypes.byref(timebase)) != 0 or timebase.numer == 0:
            logger.warning("[SYNC] mach_timebase_info failed; stamper tick thread stays default priority")
            return None

        def _ns_to_abs(ns: int) -> int:
            # mach_timebase_info: elapsed_ns = abs_units * numer / denom,
            # so abs_units = ns * denom / numer.
            return ns * timebase.denom // timebase.numer

        policy = _ThreadTimeConstraintPolicy(
            period=_ns_to_abs(_DARWIN_RT_PERIOD_NS),
            computation=_ns_to_abs(_DARWIN_RT_COMPUTATION_NS),
            constraint=_ns_to_abs(_DARWIN_RT_CONSTRAINT_NS),
            preemptible=1,
        )
        thread_port = libsystem.mach_thread_self()
        kern_return = libsystem.thread_policy_set(
            thread_port,
            _DARWIN_THREAD_TIME_CONSTRAINT_POLICY,
            ctypes.cast(ctypes.byref(policy), ctypes.c_void_p),
            _DARWIN_TIME_CONSTRAINT_POLICY_COUNT,
        )
        if kern_return != 0:
            logger.warning(
                "[SYNC] thread_policy_set(TIME_CONSTRAINT) failed (kern_return=%d); "
                "stamper tick thread stays default priority",
                kern_return,
            )
            return None
        logger.info("[SYNC] Stamper tick thread promoted to Mach real-time (period=20ms comp=2ms constraint=10ms)")
        return (libsystem, int(thread_port))
    except (AttributeError, OSError, TypeError, ValueError):
        logger.warning("[SYNC] Mach real-time promotion unavailable; stamper tick thread stays default priority")
        return None


def _darwin_revert_thread_realtime(token: tuple[Any, int] | None) -> None:
    """Symmetric revert of the Mach real-time promotion (fail-open)."""
    if token is None:
        return
    libsystem, thread_port = token
    try:
        import ctypes

        standard = ctypes.c_uint32(0)  # thread_standard_policy_data_t.no_data
        kern_return = libsystem.thread_policy_set(
            thread_port,
            _DARWIN_THREAD_STANDARD_POLICY,
            ctypes.cast(ctypes.byref(standard), ctypes.c_void_p),
            _DARWIN_STANDARD_POLICY_COUNT,
        )
        if kern_return != 0:
            logger.debug("thread_policy_set(STANDARD) revert failed (kern_return=%d)", kern_return)
        # mach_thread_self() takes a +1 port reference; release it.
        task_self = ctypes.c_uint32.in_dll(libsystem, "mach_task_self_")
        libsystem.mach_port_deallocate(task_self.value, thread_port)
    except (AttributeError, OSError, TypeError, ValueError):
        logger.debug("Mach real-time revert failed during stamper cleanup")


class ChunkStamper:
    """
    Continuous 20ms chunk stamper with silence padding.

    Receives raw PCM from a capture source via :meth:`on_pcm_data` and
    emits a continuous stream of timestamped, serialized binary frames via
    registered output callbacks.

    If the capture source does not deliver a full chunk within a 20ms tick,
    a silence chunk is emitted so the stream never stalls.

    Usage::

        stamper = ChunkStamper()
        stamper.add_output(binary_ws_broadcast)
        capture_provider.set_callback(stamper.on_capture_data)
        stamper.start()
        # ...
        stamper.stop()
    """

    def __init__(self, bit_depth: int = 16) -> None:
        # Bit depth configuration (16 or 24)
        if bit_depth == 24:
            self._bytes_per_sample = 3
            self._chunk_size = CHUNK_SIZE_BYTES_24
            self._format_version = FORMAT_VERSION_24BIT
            self._silence_pcm = SILENCE_PCM_24
        else:
            self._bytes_per_sample = 2
            self._chunk_size = CHUNK_SIZE_BYTES
            self._format_version = FORMAT_VERSION_16BIT
            self._silence_pcm = SILENCE_PCM
        self._bit_depth = bit_depth
        # Max capture buffer: 1 second of audio at the configured bit depth
        self._max_capture_buf = self._chunk_size * 50

        # Incoming PCM buffer from capture callback
        self._capture_buf = bytearray()
        self._capture_lock = threading.Lock()

        # Output callbacks receive serialized binary frames (bytes)
        self._outputs: list[Callable[[bytes], None]] = []
        self._outputs_lock = threading.Lock()

        # Ring buffer of recent frames for late-join burst
        self._ring: collections.deque[bytes] = collections.deque(
            maxlen=RING_BUFFER_SIZE,
        )
        self._ring_lock = threading.Lock()

        # PCM recording ring buffer — stores raw PCM bytes (no header)
        # for debug/fidelity comparison. Default capacity: 10 seconds
        # at 50 chunks/sec = 500 chunks × 3840 bytes = 1.92 MB.
        self._pcm_ring: collections.deque[bytes] = collections.deque(
            maxlen=PCM_RING_DEFAULT_CHUNKS,
        )
        self._pcm_ring_lock = threading.Lock()

        # Sequence counter (monotonic, wraps at uint32 max)
        self._sequence: int = 0

        # Tick thread
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

        # Metrics
        self._metrics_lock = threading.Lock()
        self._audio_chunks: int = 0
        self._silence_chunks: int = 0
        self._total_chunks: int = 0
        self._last_rms: float = 0.0
        self._capture_trims: int = 0
        self._last_capture_trim_log: float = 0.0
        self._last_broadcast_time: float = 0.0  # monotonic time of last broadcast

        # Tick jitter tracking (rolling window of 50 samples)
        self._tick_jitters: collections.deque[float] = collections.deque(maxlen=50)
        # Capture trim alignment diagnostic: counts how many times the OLD
        # unaligned trim code would have produced a mid-sample cut.
        self._capture_trim_unaligned_count: int = 0

        # Gain / clipping metrics (updated by pipeline_wiring._capture_fanout)
        self._gain_raw_peak: float = 0.0  # most recent pre-gain peak
        self._gain_gained_peak: float = 0.0  # most recent post-gain peak (before normalize)
        self._gain_clip_count: int = 0  # samples exceeding 32767 in most recent chunk
        self._gain_normalizer_scale: float = 1.0  # most recent normalizer scale (1.0 = no scaling)
        self._gain_chunks_measured: int = 0  # total chunks with gain applied
        self._gain_chunks_clipped: int = 0  # chunks where normalizer kicked in
        self._gain_total_clipped_samples: int = 0  # cumulative clipped samples
        self._gain_max_gained_peak: float = 0.0  # worst-case peak seen
        self._gain_max_clip_count: int = 0  # worst-case clip count in a single chunk

    # ------------------------------------------------------------------ #
    # Capture source callback                                             #
    # ------------------------------------------------------------------ #

    def on_capture_data(self, data: bytes, _sr: int, _ch: int, _sw: int) -> None:
        """
        Callback for the AudioCaptureProvider.

        Appends raw PCM bytes to an internal buffer.  The tick thread drains
        this buffer every 20ms.

        Args:
            data: Raw PCM bytes from capture.
            _sr, _ch, _sw: Sample rate, channels, sample width (ignored;
                the capture provider already outputs the correct format).
        """
        if not data:
            return
        with self._capture_lock:
            self._capture_buf.extend(data)
            buf_len = len(self._capture_buf)
            max_buf = self._max_capture_buf
            if buf_len > max_buf:
                excess = buf_len - max_buf
                # Align trim to chunk boundaries to prevent mid-sample
                # corruption.  The old code deleted raw `excess` bytes which
                # could leave the buffer starting mid-sample (e.g. 100 bytes
                # deleted from a 6-byte-aligned 24-bit stereo buffer).
                aligned_excess = (excess // self._chunk_size) * self._chunk_size
                # Track how often the old code would have produced an
                # unaligned trim (diagnostic — shows bug frequency).
                if excess != aligned_excess:
                    self._capture_trim_unaligned_count += 1
                if aligned_excess > 0:
                    del self._capture_buf[:aligned_excess]
                    trim_needed = aligned_excess
                else:
                    trim_needed = 0
            else:
                trim_needed = 0
        if trim_needed:
            self._capture_trims += 1
            now = time.monotonic()
            if now - self._last_capture_trim_log >= 1.0:
                self._last_capture_trim_log = now
                logger.warning(
                    "[SYNC] Capture buffer overflow: trimmed %d bytes (clock drift)",
                    trim_needed,
                )

    def update_gain_metrics(
        self,
        raw_peak: float,
        gained_peak: float,
        clip_count: int,
        normalizer_scale: float,
    ) -> None:
        """Record gain/clipping metrics from the capture fanout stage.

        Called by pipeline_wiring._capture_fanout on every gained chunk.
        """
        with self._metrics_lock:
            self._gain_raw_peak = raw_peak
            self._gain_gained_peak = gained_peak
            self._gain_clip_count = clip_count
            self._gain_normalizer_scale = normalizer_scale
            self._gain_chunks_measured += 1
            if clip_count > 0:
                self._gain_chunks_clipped += 1
            self._gain_total_clipped_samples += clip_count
            if gained_peak > self._gain_max_gained_peak:
                self._gain_max_gained_peak = gained_peak
            if clip_count > self._gain_max_clip_count:
                self._gain_max_clip_count = clip_count

    # ------------------------------------------------------------------ #
    # Output management                                                   #
    # ------------------------------------------------------------------ #

    def add_output(self, callback: Callable[[bytes], None], *, first: bool = False) -> None:
        """Register a callback to receive serialized binary frames.

        Args:
            callback: Function to call with each serialized frame.
            first: If True, insert at position 0 so this callback fires
                before any previously registered outputs.
        """
        with self._outputs_lock:
            if callback not in self._outputs:
                if first:
                    self._outputs.insert(0, callback)
                else:
                    self._outputs.append(callback)

    def remove_output(self, callback: Callable[[bytes], None]) -> None:
        """Unregister an output callback."""
        with self._outputs_lock:
            if callback in self._outputs:
                self._outputs.remove(callback)

    def get_ring_snapshot(self) -> list[bytes]:
        """
        Return a snapshot of the ring buffer for late-joining spokes.

        Returns:
            List of serialized binary frames (oldest first), up to
            RING_BUFFER_SIZE entries (~2.5 seconds of audio).
        """
        with self._ring_lock:
            return list(self._ring)

    def clear_runtime_buffers(self) -> None:
        """Clear buffered PCM and late-join frames after demand-driven stop."""
        with self._capture_lock:
            self._capture_buf.clear()
        with self._ring_lock:
            self._ring.clear()
        with self._pcm_ring_lock:
            self._pcm_ring.clear()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Start the 20ms tick thread."""
        if self._running:
            return
        self._stop_event.clear()
        self._sequence = 0
        self._running = True
        self._thread = threading.Thread(
            target=self._tick_loop,
            name="chunk-stamper",
            daemon=True,
        )
        self._thread.start()
        logger.info("ChunkStamper started")

    def stop(self) -> None:
        """Stop the tick thread and release resources."""
        if not self._running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._running = False
        with self._capture_lock:
            self._capture_buf.clear()
        logger.info(
            "ChunkStamper stopped: total=%d audio=%d silence=%d",
            self._total_chunks,
            self._audio_chunks,
            self._silence_chunks,
        )

    def _wait_until(self, deadline: float, *, clock: Callable[[], float] = time.perf_counter) -> None:
        """Wait until a high-resolution deadline using short cancellable slices."""
        max_slice = 0.001 if sys.platform == "win32" else 0.002
        spin_window = 0.002 if sys.platform == "win32" else 0.001
        busy_spin_window = spin_window if sys.platform == "win32" else 0.00025
        yield_between_slices = sys.platform != "win32"
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return
            if remaining <= spin_window:
                if self._stop_event.is_set():
                    return
                if remaining <= busy_spin_window:
                    continue
                if yield_between_slices:
                    time.sleep(0)
                continue
            if self._stop_event.wait(min(remaining, max_slice)):
                return
            if yield_between_slices:
                time.sleep(0)

    # ------------------------------------------------------------------ #
    # Core tick loop                                                      #
    # ------------------------------------------------------------------ #

    def _tick_loop(self) -> None:
        """
        20ms tick loop.

        Each tick:
        1. Drain up to _chunk_size bytes from the capture buffer.
        2. If enough data → stamp as audio chunk.
        3. If not enough → stamp as silence chunk.
        4. Serialize and broadcast to all outputs.
        5. Push serialized frame into ring buffer.
        """
        # Windows default timer resolution is 15.625ms, which causes
        # Event.wait(19ms) to actually sleep 31ms.  Set 1ms resolution
        # for the duration of this loop.  Confirmed by measurement:
        # sleep_req alternates 25ms/10ms and subprocess sees 31ms GAPs.
        _timer_set = False
        _avrt = None
        _mmcss_handle = None
        _priority_thread = None
        _previous_priority = None
        _previous_switch_interval = None
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                ctypes.windll.winmm.timeBeginPeriod(1)
                _timer_set = True
                kernel32 = ctypes.windll.kernel32
                _priority_thread = kernel32.GetCurrentThread()
                _previous_priority = kernel32.GetThreadPriority(_priority_thread)
                # Keep the 20 ms audio stamper from being preempted by GUI
                # browser load. THREAD_PRIORITY_HIGHEST is below real-time
                # priority but high enough for audio scheduling.
                kernel32.SetThreadPriority(_priority_thread, 2)
                _avrt = ctypes.WinDLL("avrt")
                _avrt.AvSetMmThreadCharacteristicsW.argtypes = [
                    wintypes.LPCWSTR,
                    ctypes.POINTER(wintypes.DWORD),
                ]
                _avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
                _avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
                _avrt.AvSetMmThreadPriority.restype = wintypes.BOOL
                _avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
                _avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
                _task_index = wintypes.DWORD(0)
                _mmcss_handle = _avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(_task_index))
                if _mmcss_handle:
                    # AVRT_PRIORITY_CRITICAL keeps the 20ms audio stamper off
                    # ordinary browser/GUI scheduling paths without raising the
                    # whole process to real-time priority.
                    _avrt.AvSetMmThreadPriority(_mmcss_handle, 2)
            except (AttributeError, OSError, TypeError, ValueError):
                logger.debug("Windows high-resolution stamper timing setup unavailable")
        # macOS: the scheduler parks a default-priority thread 30-111ms
        # every ~10-20s on idle machines, which stalls the 20ms tick loop.
        # Promote to Mach real-time (the only measured fix — see
        # _darwin_promote_thread_to_realtime). Fail-open on error.
        _darwin_rt_token = None
        if sys.platform == "darwin":
            _darwin_rt_token = _darwin_promote_thread_to_realtime()
        _previous_switch_interval = sys.getswitchinterval()
        if _previous_switch_interval > 0.0015:
            sys.setswitchinterval(0.001)

        interval = CHUNK_DURATION_MS / 1000.0  # 0.020s
        # Use perf_counter for tick timing — monotonic uses GetTickCount64
        # on Windows (15.625ms resolution), perf_counter uses QPC (100ns).
        _pc = time.perf_counter
        next_tick = _pc()
        chunk_size = self._chunk_size
        silence_pcm = self._silence_pcm
        fmt_version = self._format_version

        try:
            while not self._stop_event.is_set():
                now = _pc()
                # Measure tick jitter: deviation from scheduled time
                tick_jitter_ms = abs(now - next_tick) * 1000.0
                self._tick_jitters.append(tick_jitter_ms)

                # Drain capture buffer
                with self._capture_lock:
                    if len(self._capture_buf) >= chunk_size:
                        pcm = bytes(self._capture_buf[:chunk_size])
                        del self._capture_buf[:chunk_size]
                        is_silence = False
                    else:
                        pcm = silence_pcm
                        is_silence = True

                # Stamp
                flags = FLAG_SILENCE if is_silence else 0
                header = PCMChunkHeader(
                    play_at=now,
                    sequence=self._sequence & 0xFFFFFFFF,
                    flags=flags,
                )
                chunk = PCMChunk(header=header, pcm_data=pcm)
                frame = serialize(chunk, format_version=fmt_version)

                self._sequence += 1

                # Metrics
                with self._metrics_lock:
                    self._total_chunks += 1
                    self._last_broadcast_time = time.monotonic()
                    if is_silence:
                        self._silence_chunks += 1
                    else:
                        self._audio_chunks += 1
                        # Compute RMS every 50th audio chunk (~1/sec)
                        if self._audio_chunks % 50 == 0:
                            if self._bit_depth == 24:
                                self._last_rms = _compute_rms_int24(pcm)
                            else:
                                self._last_rms = _compute_rms_int16(pcm)

                # Broadcast to outputs
                with self._outputs_lock:
                    outputs = list(self._outputs)

                for cb in outputs:
                    try:
                        cb(frame)
                    except Exception:
                        logger.exception("ChunkStamper output callback failed")

                # Push into ring buffer
                with self._ring_lock:
                    self._ring.append(frame)

                # Push raw PCM into recording ring (non-silence only)
                if not is_silence:
                    with self._pcm_ring_lock:
                        self._pcm_ring.append(pcm)

                # Sleep until next tick (compensate for processing time)
                next_tick += interval
                now_after = _pc()
                sleep_time = next_tick - now_after
                if sleep_time > 0:
                    self._wait_until(next_tick, clock=_pc)
                elif sleep_time < -interval:
                    # The tick thread was stalled past at least one whole
                    # slot (OS scheduler park). The missed slots' PCM is
                    # already in _capture_buf, so do NOT delete the slots —
                    # the old `next_tick = _pc()` reset permanently erased
                    # them, eroding every spoke's jitter buffer by the stall
                    # duration (the browser drift corrector's 2000ppm clamp
                    # can never win it back) and backlogging the capture
                    # buffer into content-destroying trims. Keep next_tick
                    # in the past so the loop bursts the backlog out
                    # immediately; spoke jitter buffers absorb the burst.
                    # Bound the burst: beyond MAX_CATCHUP_CHUNKS slots
                    # (~500ms) the stall is pathological and the excess
                    # slots are forgiven.
                    max_lag = MAX_CATCHUP_CHUNKS * interval
                    if -sleep_time > max_lag:
                        next_tick = now_after - max_lag
        finally:
            if _previous_switch_interval is not None:
                sys.setswitchinterval(_previous_switch_interval)
            if _darwin_rt_token is not None:
                _darwin_revert_thread_realtime(_darwin_rt_token)
            if _mmcss_handle is not None and _avrt is not None:
                try:
                    _avrt.AvRevertMmThreadCharacteristics(_mmcss_handle)
                except (AttributeError, OSError, TypeError, ValueError):
                    logger.debug("MMCSS stamper cleanup failed")
            if _priority_thread is not None and _previous_priority is not None:
                try:
                    import ctypes

                    ctypes.windll.kernel32.SetThreadPriority(_priority_thread, _previous_priority)
                except (AttributeError, OSError, TypeError, ValueError):
                    logger.debug("SetThreadPriority restore failed during stamper cleanup")
            if _timer_set:
                try:
                    import ctypes

                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    logger.debug("timeEndPeriod(1) failed during stamper cleanup")

    # ------------------------------------------------------------------ #
    # PCM recording (debug / fidelity comparison)                        #
    # ------------------------------------------------------------------ #

    def get_pcm_capture(self, seconds: float = 5.0) -> bytes:
        """Return the last *seconds* of raw int16 PCM audio data.

        Non-silence chunks only. Returns concatenated raw PCM bytes
        (stereo, 48 kHz, int16 LE). Useful for time-domain fidelity
        comparison against spoke recordings.

        Args:
            seconds: How many seconds of audio to return (max 10).

        Returns:
            Raw PCM bytes, up to the requested duration.
        """
        chunks_wanted = int(seconds / (CHUNK_DURATION_MS / 1000.0))
        with self._pcm_ring_lock:
            # Take the most recent N chunks
            available = list(self._pcm_ring)
        recent = available[-chunks_wanted:] if chunks_wanted < len(available) else available
        return b"".join(recent)

    # ------------------------------------------------------------------ #
    # Metrics                                                             #
    # ------------------------------------------------------------------ #

    def get_metrics(self) -> dict[str, Any]:
        """Return stamper metrics for monitoring."""
        with self._metrics_lock:
            clip_pct = 0.0
            if self._gain_chunks_measured > 0:
                clip_pct = self._gain_chunks_clipped / self._gain_chunks_measured * 100.0
            return {
                "running": self._running,
                "bit_depth": self._bit_depth,
                "frame_size": HEADER_SIZE + self._chunk_size,
                "chunk_size_bytes": self._chunk_size,
                "sequence": self._sequence,
                "total_chunks": self._total_chunks,
                "audio_chunks": self._audio_chunks,
                "silence_chunks": self._silence_chunks,
                "ring_buffer_size": len(self._ring),
                "capture_buffer_bytes": len(self._capture_buf),
                "capture_trims": self._capture_trims,
                "capture_trim_unaligned_count": self._capture_trim_unaligned_count,
                "rms": self._last_rms,
                "last_broadcast_time": self._last_broadcast_time,
                # Gain / clipping tracking
                "gain_raw_peak": round(self._gain_raw_peak, 1),
                "gain_gained_peak": round(self._gain_gained_peak, 1),
                "gain_clip_count": self._gain_clip_count,
                "gain_normalizer_scale": round(self._gain_normalizer_scale, 4),
                "gain_chunks_measured": self._gain_chunks_measured,
                "gain_chunks_clipped": self._gain_chunks_clipped,
                "gain_clip_pct": round(clip_pct, 2),
                "gain_total_clipped_samples": self._gain_total_clipped_samples,
                "gain_max_gained_peak": round(self._gain_max_gained_peak, 1),
                "gain_max_clip_count": self._gain_max_clip_count,
                # Tick jitter (thread scheduling accuracy)
                "tick_jitter_avg_ms": (
                    round(sum(self._tick_jitters) / len(self._tick_jitters), 3) if self._tick_jitters else 0.0
                ),
                "tick_jitter_max_ms": (round(max(self._tick_jitters), 3) if self._tick_jitters else 0.0),
            }


def _compute_rms_int16(pcm: bytes) -> float:
    """Compute RMS of int16 PCM data, normalized to [0, 1]."""
    n_samples = len(pcm) // 2
    if n_samples == 0:
        return 0.0
    fmt = "<%dh" % n_samples
    samples = struct.unpack(fmt, pcm[: n_samples * 2])
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / n_samples) / 32767.0


def _compute_rms_int24(pcm: bytes) -> float:
    """Compute RMS of int24 PCM data, normalized to [0, 1]."""
    from .chunk_protocol import unpack_int24_to_float32

    floats = unpack_int24_to_float32(pcm)
    if len(floats) == 0:
        return 0.0
    import numpy as np

    return float(np.sqrt(np.mean(floats * floats)))


__all__ = [
    "MAX_CAPTURE_BUF_BYTES",
    "MAX_CATCHUP_CHUNKS",
    "RING_BUFFER_SIZE",
    "ChunkStamper",
]
