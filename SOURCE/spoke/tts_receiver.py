"""
TTS Receiver — plays PCM audio bytes received from hub via sounddevice.

The hub synthesises TTS and sends raw PCM bytes prefixed with ``b"TTS\\x00"``
over the voice-stream WebSocket. Newer hub builds (post-stradivari) also
include an optional sample-rate header (``b"SRAT" + uint32 LE``) immediately
after the prefix so the spoke can play at whatever rate Kokoro emitted
(24 kHz native), rather than forcing a 16 kHz resample at the hub. Frames
without the SRAT header are treated as legacy 16 kHz PCM.

This module wraps the canonical decoder in ``voice.synthesis.tts_wire`` so
both sides of the wire agree on the format.

One reply can arrive as several frames (one per sentence, while the hub's model
is still writing). ``sd.play`` cuts off whatever it is playing, so frames are
queued: a frame that arrives while one is playing starts when that one ends.
``stop()`` (barge-in) silences the current frame and drops the queue.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# TTS wire prefix: 4 bytes identifying a TTS binary message
TTS_PREFIX = b"TTS\x00"
TTS_PREFIX_LEN = len(TTS_PREFIX)

# Default sample rate for legacy frames without an explicit rate header
TTS_SAMPLE_RATE = 16000
TTS_CHANNELS = 1
TTS_DTYPE = "int16"


class TTSReceiver:
    """Plays TTS PCM bytes through the local sounddevice output."""

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._lock = threading.Lock()
        self._sd: Any = None
        # Playback queue: frames waiting for the current one to end.
        self._cond = threading.Condition(self._lock)
        self._queue: deque[tuple[Any, int]] = deque()
        self._busy = False
        self._clip_seconds = 0.0
        self._clip_started = 0.0
        # Bumped by stop(): the queue worker abandons the clip it waited on.
        self._generation = 0
        self._worker: threading.Thread | None = None

    def _ensure_sd(self) -> Any:
        """Lazy-import sounddevice to keep module import lightweight."""
        if self._sd is None:
            import sounddevice as sd

            self._sd = sd
        return self._sd

    def play(self, pcm_bytes: bytes, sample_rate: int | None = None) -> None:
        """Play raw int16 mono PCM bytes through speakers.

        Called from the mic_streamer receive loop when a TTS binary message
        arrives. Never blocks: when nothing is playing the clip starts now (in
        the caller's thread; sounddevice play() is non-blocking), otherwise it
        is queued and starts as soon as the clips before it have finished.

        ``sample_rate`` defaults to ``TTS_SAMPLE_RATE`` (16 kHz) when the
        caller did not parse one from a SRAT frame header.
        """
        if not pcm_bytes:
            return

        rate = int(sample_rate) if sample_rate else TTS_SAMPLE_RATE

        import numpy as np

        self._ensure_sd()
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)

        with self._cond:
            if self._busy:
                self._queue.append((samples, rate))
                return
            self._start_locked(samples, rate)
            self._ensure_worker_locked()
            self._cond.notify_all()

    def stop(self) -> None:
        """Barge-in: silence the current clip and drop every queued one."""
        with self._cond:
            dropped = len(self._queue)
            self._queue.clear()
            was_busy = self._busy
            self._busy = False
            self._generation += 1
            self._cond.notify_all()
            if not was_busy:
                return
            # Under the lock, so a clip started right after this call is not
            # the one silenced.
            sd = self._sd
            stop = getattr(sd, "stop", None) if sd is not None else None
            if callable(stop):
                try:
                    stop()
                except Exception:
                    logger.debug("TTS stop failed", exc_info=True)
        logger.debug("TTS playback stopped (%d queued clip(s) dropped)", dropped)

    @property
    def queued(self) -> int:
        """Clips waiting behind the one playing now."""
        with self._lock:
            return len(self._queue)

    def _start_locked(self, samples: Any, rate: int) -> None:
        """Start one clip; caller holds the lock."""
        sd = self._ensure_sd()
        self._busy = True
        self._clip_seconds = len(samples) / rate
        self._clip_started = time.monotonic()
        try:
            sd.play(
                samples,
                samplerate=rate,
                device=self._device,
                blocking=False,
            )
            logger.debug(
                "TTS playback started: %d samples @ %d Hz (%.1fs)",
                len(samples),
                rate,
                self._clip_seconds,
            )
        except Exception:
            logger.exception("TTS playback failed")
            self._clip_seconds = 0.0

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run_queue, name="spoke-tts-queue", daemon=True)
            self._worker.start()

    def _run_queue(self) -> None:
        """Start each queued clip when the one before it has finished."""
        while True:
            with self._cond:
                while not self._busy:
                    self._cond.wait()
                generation = self._generation
            self._wait_clip_end(generation)
            with self._cond:
                if generation != self._generation:
                    continue  # stop() ran; nothing of that turn plays on
                if not self._queue:
                    self._busy = False
                    continue
                samples, rate = self._queue.popleft()
                self._start_locked(samples, rate)

    def _wait_clip_end(self, generation: int) -> None:
        """Block until the playing clip ends (or stop() abandons it)."""
        sd = self._sd
        wait = getattr(sd, "wait", None) if sd is not None else None
        if callable(wait):
            # sounddevice's own end-of-playback signal; stop() releases it.
            try:
                wait()
            except Exception:
                logger.debug("TTS playback wait failed", exc_info=True)
        # Timer backstop (and the only signal for an engine without wait()):
        # never start the next clip before this one's duration has elapsed.
        with self._cond:
            end = self._clip_started + self._clip_seconds
            while generation == self._generation:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

    @staticmethod
    def is_tts_message(data: bytes) -> bool:
        """Check whether a binary WebSocket message is a TTS payload."""
        return len(data) > TTS_PREFIX_LEN and data[:TTS_PREFIX_LEN] == TTS_PREFIX

    @staticmethod
    def decode_frame(data: bytes) -> tuple[bytes, int]:
        """Decode a TTS-prefixed frame into ``(pcm_bytes, sample_rate)``.

        Frames with the new ``SRAT`` header carry an explicit sample rate.
        Frames without it are treated as legacy 16 kHz PCM, matching the
        pre-stradivari wire contract.
        """
        from voice.synthesis.tts_wire import decode_tts_frame

        decoded = decode_tts_frame(data)
        if decoded is None:
            # Not a TTS-prefixed payload; preserve callers' assumption that
            # `is_tts_message` is checked first and this is unreachable.
            return b"", TTS_SAMPLE_RATE
        return decoded

    @staticmethod
    def strip_prefix(data: bytes) -> bytes:
        """Strip TTS prefix (and optional SRAT header), returning raw PCM bytes.

        Kept for legacy callers that don't care about the embedded sample
        rate. Prefer :meth:`decode_frame` if you need to play at the
        hub-reported rate (the Python spoke pipeline does).
        """
        pcm, _rate = TTSReceiver.decode_frame(data)
        return pcm
