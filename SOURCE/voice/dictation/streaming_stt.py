"""
Streaming speech-to-text engines for live dictation.

Provides two backends:
- **LocalStreamingSTT**: faster-whisper + Silero VAD for fully offline transcription
- **DeepgramStreamingSTT**: Deepgram Nova-2 cloud WebSocket for low-latency streaming

A factory function selects the best available backend at runtime.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

from config.settings import settings
from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)


# ======================================================================== #
# Abstract base                                                            #
# ======================================================================== #


class StreamingSTTEngine(ABC):
    """Base contract for streaming speech-to-text engines."""

    @abstractmethod
    def start(self) -> None:
        """Start the engine and prepare for audio ingestion."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the engine and release resources."""

    @abstractmethod
    def feed_audio(self, chunk: np.ndarray) -> None:
        """Feed a chunk of 16 kHz mono int16 audio into the engine.

        Args:
            chunk: numpy int16 array of audio samples at 16 kHz mono.
        """

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """Return True if all required dependencies are installed."""


# ======================================================================== #
# Local (faster-whisper + Silero VAD)                                      #
# ======================================================================== #


class LocalStreamingSTT(StreamingSTTEngine):
    """Offline streaming STT using faster-whisper with Silero VAD endpoint detection.

    Audio chunks are pushed via :meth:`feed_audio`, buffered internally, and
    processed in a dedicated daemon thread.  Silero VAD detects speech
    boundaries; once a speech segment ends the accumulated audio is
    transcribed with faster-whisper and the ``on_final`` callback fires.
    During active speech, partial transcriptions are emitted via
    ``on_partial``.

    Args:
        model_size: faster-whisper model identifier (e.g. ``"base.en"``).
        on_partial: Called with interim text while the user is still speaking.
        on_final: Called with the completed transcription of a speech segment.
        language: Language code for transcription (default ``"en"``).
    """

    # Silero VAD configuration
    _VAD_THRESHOLD: float = 0.5
    _SPEECH_PAD_MS: int = 300
    _MIN_SPEECH_MS: int = 250
    _MIN_SILENCE_MS: int = 700

    # Hard utterance cap. Protects against continuous noise (TV, HVAC, etc.)
    # that Silero flags as speech: after 30s we force an endpoint so the
    # segment gets transcribed and the buffer drained instead of growing
    # unboundedly in memory.
    _MAX_UTTERANCE_SECONDS: float = 30.0

    def __init__(
        self,
        model_size: str = "base.en",
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        language: str = "en",
    ) -> None:
        self._model_size = model_size
        self._on_partial = on_partial
        self._on_final = on_final
        self._language = language

        self._audio_queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._model: object | None = None
        self._vad_model: object | None = None
        # S10-VOICE-002: when True, _process_loop flushes its in-progress
        # speech buffer to ``on_final`` before exiting so the user's last
        # phrase isn't lost when stop() races VAD endpointing.
        self._stopping = False

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        """Initialise models and start the processing thread."""
        if self._running:
            logger.warning("LocalStreamingSTT already running")
            return

        self._load_models()
        self._running = True
        self._thread = threading.Thread(
            target=self._process_loop,
            daemon=True,
            name="local-streaming-stt",
        )
        self._thread.start()
        logger.info("LocalStreamingSTT started (model=%s)", self._model_size)

    def stop(self) -> None:
        """Signal the processing thread to exit and wait for it.

        Sets ``_stopping`` so :meth:`_process_loop` can flush any
        in-progress speech buffer through ``on_final`` before the
        thread exits (S10-VOICE-002 parity for the local backend).
        """
        if not self._running:
            return
        self._stopping = True
        self._running = False
        # Sentinel value to unblock the queue
        self._audio_queue.put(None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._model = None
        self._vad_model = None
        self._stopping = False
        logger.info("LocalStreamingSTT stopped")

    def feed_audio(self, chunk: np.ndarray) -> None:
        """Enqueue an audio chunk for processing.

        Args:
            chunk: int16 numpy array at 16 kHz mono.
        """
        if self._running:
            self._audio_queue.put(chunk)

    @staticmethod
    def is_available() -> bool:
        """Check whether faster-whisper and onnxruntime (for Silero VAD) are installed."""
        try:
            import faster_whisper as _faster_whisper
            import onnxruntime as _onnxruntime

            return _faster_whisper is not None and _onnxruntime is not None
        except ImportError:
            return False

    # -- internals --------------------------------------------------------- #

    def _load_models(self) -> None:
        """Lazily load faster-whisper and Silero VAD models."""

        try:
            from faster_whisper import WhisperModel

            from voice.transcription.whisper_model_cache import resolve_model_load

            # Pin the model cache to a writable/baked location so a read-only
            # rootfs (cloud) does not crash the load — see whisper_model_cache.
            self._model = WhisperModel(
                self._model_size,
                device="cpu",
                compute_type="int8",
                **resolve_model_load(self._model_size),
            )
            logger.info(
                "faster-whisper model loaded: %s",
                self._model_size,
            )
        except Exception:
            logger.exception("Failed to load faster-whisper model")
            raise

        try:
            from voice.vad.silero_onnx import create_silero_vad

            model = create_silero_vad()
            if model is None:
                raise RuntimeError("Silero VAD ONNX model not available")
            self._vad_model = model
            logger.info("Silero VAD ONNX model loaded")
        except Exception:
            logger.exception("Failed to load Silero VAD model")
            raise

    def _process_loop(self) -> None:
        """Background thread: accumulate speech, detect endpoints, transcribe."""
        import numpy as np

        speech_buffer: list[np.ndarray] = []
        is_speaking = False
        silence_frames = 0
        frames_per_chunk = int(SAMPLE_RATE_16K * 0.032)  # 32ms windows for VAD
        min_silence_chunks = int(self._MIN_SILENCE_MS / 32)
        min_speech_chunks = int(self._MIN_SPEECH_MS / 32)
        # One chunk == 32 ms; cap at MAX_UTTERANCE_SECONDS * 1000 / 32 chunks.
        max_utterance_chunks = int(self._MAX_UTTERANCE_SECONDS * 1000 / 32)
        speech_chunks = 0

        # Run until we see the stop-sentinel (None). We deliberately do NOT
        # gate this on ``self._running`` alone: that would race stop()
        # because the flag flips before the sentinel is enqueued, and
        # the loop could exit on the next iteration *without* ever
        # processing the sentinel — leaving the in-progress speech buffer
        # un-flushed (S10-VOICE-002).
        while True:
            try:
                chunk = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                if not self._running:
                    # No sentinel queued but stop() never ran — defensive
                    # exit. The buffer is intentionally NOT flushed here
                    # because we have no signal that the user finished
                    # speaking; this branch fires when the engine is
                    # garbage-collected without a clean stop().
                    break
                continue

            if chunk is None:
                # S10-VOICE-002: sentinel = stop() requested. Flush any
                # in-progress speech buffer so the user's last phrase
                # isn't silently dropped when they release the PTT key
                # mid-utterance.
                if is_speaking and speech_chunks >= min_speech_chunks and speech_buffer:
                    logger.info(
                        "LocalStreamingSTT flushing %d-chunk in-progress speech buffer on stop",
                        speech_chunks,
                    )
                    self._transcribe_segment(speech_buffer)
                speech_buffer.clear()
                break

            # Convert to float32 for Silero VAD
            audio_f32 = chunk.astype(np.float32) / 32768.0

            # Process in 32ms windows
            offset = 0
            while offset + frames_per_chunk <= len(audio_f32):
                window = audio_f32[offset : offset + frames_per_chunk]
                offset += frames_per_chunk

                try:
                    confidence = self._vad_model(window, SAMPLE_RATE_16K)
                except Exception:
                    logger.debug("VAD inference failed for window", exc_info=True)
                    confidence = 0.0

                if confidence >= self._VAD_THRESHOLD:
                    if not is_speaking:
                        is_speaking = True
                        silence_frames = 0
                        speech_chunks = 0
                        logger.debug("VAD: speech onset detected")
                    silence_frames = 0
                    speech_chunks += 1
                    speech_buffer.append(window)
                elif is_speaking:
                    silence_frames += 1
                    speech_buffer.append(window)

                    if silence_frames >= min_silence_chunks:
                        # Speech segment ended
                        if speech_chunks >= min_speech_chunks:
                            self._transcribe_segment(speech_buffer)
                        else:
                            logger.debug(
                                "VAD: speech segment too short (%d chunks), discarding",
                                speech_chunks,
                            )
                        speech_buffer.clear()
                        is_speaking = False
                        silence_frames = 0
                        speech_chunks = 0

                # Hard cap: force an endpoint at _MAX_UTTERANCE_SECONDS so a
                # VAD mis-classification cannot grow the speech buffer without
                # bound (OOM), and the user still gets a transcript.
                if is_speaking and speech_chunks >= max_utterance_chunks:
                    logger.warning(
                        "VAD: forced endpoint at %.1fs (max-utterance cap)",
                        self._MAX_UTTERANCE_SECONDS,
                    )
                    self._transcribe_segment(speech_buffer)
                    speech_buffer.clear()
                    is_speaking = False
                    silence_frames = 0
                    speech_chunks = 0

            # Emit partial transcription during ongoing speech
            if is_speaking and speech_chunks > 0 and speech_chunks % 15 == 0:
                self._transcribe_partial(speech_buffer)

    def _transcribe_segment(self, buffers: list[np.ndarray]) -> None:
        """Transcribe a completed speech segment."""
        import numpy as np

        if not buffers or self._model is None:
            return

        audio = np.concatenate(buffers)
        audio_f32 = audio.astype(np.float32) if audio.dtype != np.float32 else audio

        try:
            segments, _info = self._model.transcribe(
                audio_f32,
                language=self._language,
                beam_size=5,
                vad_filter=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text and self._on_final:
                logger.debug("Local STT final length=%d", len(text))
                self._on_final(text)
        except Exception:
            logger.exception("faster-whisper transcription failed")

    def _transcribe_partial(self, buffers: list[np.ndarray]) -> None:
        """Emit an interim transcription for the ongoing speech segment."""
        import numpy as np

        if not buffers or self._model is None:
            return

        audio = np.concatenate(buffers)
        audio_f32 = audio.astype(np.float32) if audio.dtype != np.float32 else audio

        try:
            segments, _info = self._model.transcribe(
                audio_f32,
                language=self._language,
                beam_size=1,
                vad_filter=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text and self._on_partial:
                self._on_partial(text)
        except Exception:
            logger.debug("Partial transcription failed", exc_info=True)


# ======================================================================== #
# Deepgram (cloud WebSocket)                                               #
# ======================================================================== #


# ======================================================================== #
# Finalization timers (parity with Claude voiceStreamSTT.ts)               #
# ======================================================================== #
#
# When ``stop()`` is called, Deepgram needs:
#   1. A grace window for any queued audio to flush to the wire BEFORE we
#      tell the server to stop accepting audio.
#   2. After CloseStream is sent, a window in which Deepgram emits its
#      final ``is_final``+``speech_final`` message — this is where the
#      last interim becomes a final.
#   3. A safety cap if the server never responds.
#
# Mirrors Claude's ``FINALIZE_TIMEOUTS_MS = {safety: 5_000, noData: 1_500}``
# at src/services/voiceStreamSTT.ts:44-47.
FINALIZE_TIMEOUTS_S: dict[str, float] = {
    # Time we wait *before* sending CloseStream so the send queue can
    # drain any frames the mic callback already enqueued.
    "audio_flush": 0.15,
    # No further server messages after CloseStream → we assume Deepgram
    # has nothing more to say and resolve.
    "no_data": 1.5,
    # Absolute upper bound on the whole finalize cycle.
    "safety": 5.0,
}


class DeepgramStreamingSTT(StreamingSTTEngine):
    """Cloud streaming STT via Deepgram Nova-2 WebSocket API.

    Audio is sent as raw PCM int16 frames over a persistent WebSocket.
    Deepgram returns interim and final transcript messages which are
    dispatched to the registered callbacks.

    The engine handles automatic reconnection on disconnect.

    Finalization (``stop``) is staged to match Claude Code's voice_stream
    behaviour: drain audio → send CloseStream → wait for the final
    ``is_final`` → promote any lingering interim transcript to final.
    This closes S10-VOICE-002 where the abrupt sentinel-and-stop loop
    could drop the last segment.

    Args:
        api_key: Deepgram API key. Falls back to ``settings.deepgram_api_key``.
        on_interim: Called with interim (partial) transcription text.
        on_final: Called with the final transcription of a speech utterance.
        language: BCP-47 language code (default ``"en-US"``).
        model: Deepgram model name (default ``"nova-2"``).
        keyterms: Optional list of words/phrases passed to Deepgram as
            ``keyterm=`` (Nova-3) or ``keywords=`` (Nova-2) query params
            for accuracy boosting. When ``None`` (default) the engine
            falls back to :func:`voice.dictation.keyterms.get_voice_keyterms`
            at connect time. Pass ``[]`` to disable enrichment entirely.
    """

    _WS_URL = "wss://api.deepgram.com/v1/listen"
    _MAX_RECONNECTS = 5
    _RECONNECT_DELAY_S = 2.0

    def __init__(
        self,
        api_key: str | None = None,
        on_interim: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        language: str = "en-US",
        model: str = "nova-2",
        keyterms: list[str] | None = None,
    ) -> None:
        self._api_key = api_key or getattr(settings, "deepgram_api_key", "")
        self._on_interim = on_interim
        self._on_final = on_final
        self._language = language
        self._model = model
        # ``None`` means "auto-populate at connect"; explicit ``[]`` disables.
        self._keyterms: list[str] | None = list(keyterms) if keyterms is not None else None

        self._ws: object | None = None  # websockets connection
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._reconnect_count = 0
        self._send_queue: asyncio.Queue[bytes | None] | None = None

        # Finalization state. ``_last_interim_text`` tracks the most
        # recent interim string seen since the last final; if the WS
        # closes (or finalize() times out) with text still here, we
        # promote it to a final so the caller isn't silently dropped.
        self._last_interim_text: str = ""
        # Event that fires when Deepgram delivers its post-CloseStream final
        # (or speech_final after silence). Used by stop() to block until
        # the server has had its chance to flush.
        self._finalize_event: threading.Event = threading.Event()
        # True once stop() has begun finalization — gates message
        # handling so a stray late chunk doesn't re-arm us.
        self._finalizing: bool = False

    # -- public interface (sync wrappers) ---------------------------------- #

    def start(self) -> None:
        """Start the WebSocket connection in a background thread."""
        if self._running:
            logger.warning("DeepgramStreamingSTT already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_event_loop,
            daemon=True,
            name="deepgram-streaming-stt",
        )
        self._thread.start()
        logger.info("DeepgramStreamingSTT started (model=%s)", self._model)

    def stop(self) -> None:
        """Finalize the WebSocket session and stop the event loop.

        Finalization (S10-VOICE-002 fix) is staged to avoid losing the
        last interim transcript on close:

        1. ``_finalizing`` flips True; ``feed_audio`` becomes a no-op.
        2. The send loop is notified via the sentinel that it should
           (a) wait a short ``audio_flush`` window for buffered audio
           to drain, (b) send ``CloseStream``, (c) wait for the no-data
           or safety timer.
        3. After the finalize event fires (or times out), the receive
           loop's WS-close handler promotes any ``_last_interim_text``
           still in flight to a final transcript via ``on_final``.
        4. The loop is stopped and the thread is joined.

        Without these steps, the previous implementation (sentinel +
        immediate ``loop.stop``) would race the WebSocket teardown and
        drop the final ``is_final`` message whenever the user released
        push-to-talk milliseconds before Deepgram's endpointer fired.
        """
        if not self._running:
            return
        # Phase 1: stop accepting new audio, but keep the loop running
        # so the sender can flush + send CloseStream and the receiver
        # can pick up the final transcript.
        self._running = False
        self._finalizing = True
        self._finalize_event.clear()

        if self._send_queue is not None and self._loop is not None:
            # Sentinel triggers the staged-shutdown branch in _send_loop.
            try:
                self._loop.call_soon_threadsafe(self._send_queue.put_nowait, None)
            except RuntimeError:
                pass

        # Phase 2: wait for the finalize event up to the safety cap.
        # The receive loop sets the event on:
        #   - a post-CloseStream message arriving (or no_data timeout)
        #   - the WebSocket closing
        # If neither happens within safety, we fall through and tear
        # down anyway; any pending ``_last_interim_text`` will still
        # be promoted by the close handler when it eventually runs.
        finalize_deadline = FINALIZE_TIMEOUTS_S["safety"]
        if not self._finalize_event.wait(timeout=finalize_deadline):
            logger.warning(
                "DeepgramStreamingSTT finalize timed out after %.1fs — promoting "
                "any unreported interim transcript via safety path",
                finalize_deadline,
            )
            self._promote_last_interim_if_any(reason="safety_timeout")

        # Phase 3: stop the loop and join the worker thread.
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._loop = None
        self._ws = None
        self._finalizing = False
        logger.info("DeepgramStreamingSTT stopped")

    def _promote_last_interim_if_any(self, *, reason: str) -> None:
        """Promote a lingering interim transcript to final (S10-VOICE-002).

        Called from the close handler and the stop() safety-timeout
        path. Idempotent: clears ``_last_interim_text`` so a follow-up
        call does nothing.
        """
        text = self._last_interim_text
        if not text:
            return
        self._last_interim_text = ""
        logger.info(
            "Promoting unreported interim to final on %s length=%d",
            reason,
            len(text),
        )
        if self._on_final is not None:
            try:
                self._on_final(text)
            except Exception:
                logger.exception("on_final callback raised during interim promotion")

    def feed_audio(self, chunk: np.ndarray) -> None:
        """Convert chunk to bytes and enqueue for sending.

        Args:
            chunk: int16 numpy array at 16 kHz mono.
        """
        if not self._running or self._finalizing:
            # During finalization the send loop is in its drain-and-close
            # phase; new audio after CloseStream is rejected by Deepgram
            # as a protocol error, so drop it (parity with Claude
            # voiceStreamSTT.ts:220-227).
            return
        if self._send_queue is None or self._loop is None:
            return
        raw = chunk.tobytes()
        try:
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, raw)
        except RuntimeError:
            # Event loop already closed
            pass

    @staticmethod
    def is_available() -> bool:
        """Check whether the websockets library is installed and an API key is configured."""
        try:
            import websockets as _websockets

            key = getattr(settings, "deepgram_api_key", "")
            return _websockets is not None and bool(key)
        except ImportError:
            return False

    # -- async internals --------------------------------------------------- #

    def _run_event_loop(self) -> None:
        """Entry point for the background thread: create and run an asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._send_queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._session_loop())
        except Exception:
            logger.exception("Deepgram event loop crashed")
        finally:
            self._loop.close()
            self._loop = None

    async def _session_loop(self) -> None:
        """Maintain a WebSocket session with auto-reconnection."""
        while self._running and self._reconnect_count < self._MAX_RECONNECTS:
            try:
                await self._connect_and_stream()
            except Exception:
                self._reconnect_count += 1
                if self._running:
                    logger.warning(
                        "Deepgram connection lost, reconnecting (%d/%d)",
                        self._reconnect_count,
                        self._MAX_RECONNECTS,
                    )
                    await asyncio.sleep(self._RECONNECT_DELAY_S)
        if self._reconnect_count >= self._MAX_RECONNECTS:
            logger.error(
                "Deepgram max reconnects (%d) exhausted",
                self._MAX_RECONNECTS,
            )

    def _resolve_keyterms(self) -> list[str]:
        """Return the keyterms to attach to this connection.

        - Explicit list (incl. empty) from the constructor wins.
        - Otherwise, ask :mod:`voice.dictation.keyterms` for the
          curated defaults plus session context. Best-effort — a
          broken settings store must not stop dictation from starting.
        """
        if self._keyterms is not None:
            return list(self._keyterms)
        try:
            from voice.dictation.keyterms import get_voice_keyterms

            return get_voice_keyterms()
        except Exception:
            logger.debug("Keyterm enrichment failed, continuing without", exc_info=True)
            return []

    async def _connect_and_stream(self) -> None:
        """Open the WebSocket, start sender/receiver tasks."""
        import urllib.parse

        import websockets

        # ``keyterm`` is the Nova-3 spelling; older Nova-2 deployments
        # used ``keywords``. We send both — Deepgram silently ignores the
        # spelling its current model doesn't understand, which keeps the
        # request working across model rolls without a feature flag.
        # See Claude's voiceStreamSTT.ts:144-173 for the same approach.
        keyterm_list = self._resolve_keyterms()
        base_params: list[tuple[str, str]] = [
            ("model", self._model),
            ("language", self._language),
            ("encoding", "linear16"),
            ("sample_rate", str(SAMPLE_RATE_16K)),
            ("channels", "1"),
            ("interim_results", "true"),
            ("punctuate", "true"),
            ("endpointing", "300"),
        ]
        for term in keyterm_list:
            base_params.append(("keyterm", term))
            base_params.append(("keywords", term))

        if keyterm_list:
            logger.debug(
                "Deepgram connect with %d keyterm(s) (e.g. %s)",
                len(keyterm_list),
                keyterm_list[:3],
            )

        url = self._WS_URL + "?" + urllib.parse.urlencode(base_params)
        headers = {"Authorization": f"Token {self._api_key}"}

        async with websockets.connect(url, extra_headers=headers) as ws:
            self._ws = ws
            self._reconnect_count = 0
            logger.info("Deepgram WebSocket connected")

            sender = asyncio.create_task(self._send_loop(ws))
            receiver = asyncio.create_task(self._receive_loop(ws))

            _done, pending = await asyncio.wait(
                [sender, receiver],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            self._ws = None

    async def _send_loop(self, ws: object) -> None:
        """Forward queued audio bytes to the WebSocket.

        On sentinel (``None``): runs the **staged finalization sequence**
        (S10-VOICE-002) instead of slamming the connection shut. We
        briefly wait for any queued audio chunks to flush, then send
        ``CloseStream``, then wait for either the no-data timeout or
        the receive loop to signal that the final transcript arrived.
        """
        assert self._send_queue is not None
        while True:
            try:
                data = await self._send_queue.get()
            except asyncio.CancelledError:
                break

            if data is None:
                # Staged-shutdown: drain whatever's already queued, then
                # CloseStream, then arm the no-data timer.
                await self._drain_and_close(ws)
                break

            # Normal audio frame. If we're already finalizing (race —
            # a chunk was enqueued just before stop() flipped the flag),
            # drop it: Deepgram will protocol-error if it arrives after
            # CloseStream.
            if self._finalizing:
                continue
            try:
                await ws.send(data)  # type: ignore[union-attr]
            except Exception:
                logger.debug("Deepgram send failed", exc_info=True)
                break

    async def _drain_and_close(self, ws: object) -> None:
        """Flush queued audio, send CloseStream, wait for final or timeout.

        Mirrors Claude voiceStreamSTT.ts:239-304:
        - Defer ``CloseStream`` so any audio callbacks queued by the
          native recording layer get flushed to the wire first
          (``audio_flush`` window).
        - Send ``CloseStream``.
        - Wait for Deepgram's post-CloseStream final (set via
          ``_finalize_event`` from the receive loop) **or** the
          ``no_data`` timeout, whichever comes first.
        - In either case, ``_promote_last_interim_if_any`` runs from
          the close handler to guarantee the last segment isn't lost.
        """
        assert self._send_queue is not None

        # Step 1: ~150 ms grace for in-flight audio to drain. We poll
        # the queue rather than time.sleep so any chunks the mic layer
        # enqueued just before stop() still make it to Deepgram.
        flush_deadline = asyncio.get_event_loop().time() + FINALIZE_TIMEOUTS_S["audio_flush"]
        while asyncio.get_event_loop().time() < flush_deadline:
            try:
                data = self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)
                continue
            if data is None:
                continue
            try:
                await ws.send(data)  # type: ignore[union-attr]
            except Exception:
                logger.debug("Deepgram drain send failed", exc_info=True)
                break

        # Step 2: send CloseStream. After this Deepgram emits its
        # final ``is_final``/``speech_final`` and then closes the WS.
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))  # type: ignore[union-attr]
            logger.debug("Sent Deepgram CloseStream (finalize)")
        except Exception:
            logger.debug("Deepgram CloseStream send failed", exc_info=True)
            self._finalize_event.set()
            return

        # Step 3: wait for either the receive loop to flag the final or
        # the no-data timeout to elapse. ``_finalize_event.set()`` is
        # called from the receive-loop final-message handler and from
        # the WS close handler.
        try:
            await asyncio.wait_for(
                self._wait_finalize_event_async(),
                timeout=FINALIZE_TIMEOUTS_S["no_data"],
            )
        except TimeoutError:
            logger.debug(
                "Deepgram post-CloseStream no_data timeout (%.1fs) — finalizing anyway",
                FINALIZE_TIMEOUTS_S["no_data"],
            )
            # Promote any interim still in flight before signalling the
            # main thread; the receive loop may not run again before
            # stop() joins.
            self._promote_last_interim_if_any(reason="no_data_timeout")
            self._finalize_event.set()

    async def _wait_finalize_event_async(self) -> None:
        """Bridge threading.Event into the asyncio loop without busy-waiting."""
        while not self._finalize_event.is_set():
            await asyncio.sleep(0.02)

    async def _receive_loop(self, ws: object) -> None:
        """Read transcript messages from the WebSocket.

        Tracks ``_last_interim_text`` so the close path (S10-VOICE-002)
        can promote a lingering interim to final if Deepgram never
        sends an explicit ``is_final`` after CloseStream. Also sets
        ``_finalize_event`` on the first final received post-CloseStream
        so the send loop can unblock immediately instead of waiting
        the full no-data timer.
        """
        try:
            async for raw_msg in ws:  # type: ignore[union-attr]
                # During finalization we still process messages — that's
                # exactly when Deepgram delivers its post-CloseStream
                # final. Only bail when both ``_running`` AND
                # ``_finalizing`` are False (i.e. fully torn down).
                if not self._running and not self._finalizing:
                    break
                try:
                    msg = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError):
                    continue

                channel = msg.get("channel", {})
                alternatives = channel.get("alternatives", [])
                if not alternatives:
                    continue

                transcript = alternatives[0].get("transcript", "").strip()
                if not transcript:
                    # Deepgram occasionally emits speech_final=True with
                    # an empty transcript to signal endpointing. Use it
                    # to unblock the finalize wait.
                    if msg.get("speech_final") and self._finalizing:
                        self._promote_last_interim_if_any(reason="speech_final_empty")
                        self._finalize_event.set()
                    continue

                is_final = msg.get("is_final", False)
                if is_final:
                    # Reset interim tracking — this segment is committed.
                    self._last_interim_text = ""
                    logger.debug("Deepgram final length=%d", len(transcript))
                    if self._on_final:
                        try:
                            self._on_final(transcript)
                        except Exception:
                            logger.exception("on_final callback raised")
                    # During finalization, a final means "Deepgram has
                    # nothing more to say" — signal the send loop.
                    if self._finalizing:
                        self._finalize_event.set()
                else:
                    self._last_interim_text = transcript
                    if self._on_interim:
                        try:
                            self._on_interim(transcript)
                        except Exception:
                            logger.exception("on_interim callback raised")
        except Exception:
            logger.debug("Deepgram receive loop ended", exc_info=True)
        finally:
            # WS closed — promote any unreported interim and unblock
            # finalize() waiters (parity with Claude voiceStreamSTT.ts:463-496).
            self._promote_last_interim_if_any(reason="ws_close")
            self._finalize_event.set()


# ======================================================================== #
# Factory                                                                   #
# ======================================================================== #


class StreamingSTTFactory:
    """Factory for creating the best available streaming STT engine.

    Args:
        provider: One of ``"auto"``, ``"local"``, ``"deepgram"``.
            When ``"auto"`` the factory prefers local, then falls back to
            Deepgram if an API key is present.
    """

    @staticmethod
    def create(
        provider: str = "auto",
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        keyterms: list[str] | None = None,
    ) -> StreamingSTTEngine:
        """Create a streaming STT engine instance.

        Args:
            provider: Backend selection (``"auto"``, ``"local"``, ``"deepgram"``).
            on_partial: Callback for interim transcriptions.
            on_final: Callback for finalised transcriptions.
            keyterms: Optional keyterm list passed to Deepgram for
                accuracy boosting. Local Whisper ignores this. ``None``
                (default) means "let the engine auto-populate from
                :mod:`voice.dictation.keyterms`"; pass ``[]`` to disable.

        Returns:
            A ready-to-start :class:`StreamingSTTEngine` instance.

        Raises:
            RuntimeError: If the requested provider is unavailable.
        """
        if provider == "local":
            if not LocalStreamingSTT.is_available():
                raise RuntimeError("Local STT unavailable: install faster-whisper and torch")
            return LocalStreamingSTT(
                on_partial=on_partial,
                on_final=on_final,
            )

        if provider == "deepgram":
            if not DeepgramStreamingSTT.is_available():
                raise RuntimeError("Deepgram STT unavailable: install websockets and set DEEPGRAM_API_KEY")
            return DeepgramStreamingSTT(
                on_interim=on_partial,
                on_final=on_final,
                keyterms=keyterms,
            )

        # Auto-selection: prefer local, then cloud
        if provider == "auto":
            if LocalStreamingSTT.is_available():
                logger.info("Auto-selected local streaming STT")
                return LocalStreamingSTT(
                    on_partial=on_partial,
                    on_final=on_final,
                )
            if DeepgramStreamingSTT.is_available():
                logger.info("Auto-selected Deepgram streaming STT")
                return DeepgramStreamingSTT(
                    on_interim=on_partial,
                    on_final=on_final,
                    keyterms=keyterms,
                )
            raise RuntimeError(
                "No streaming STT backend available. "
                "Install faster-whisper+torch for local, "
                "or install websockets and set DEEPGRAM_API_KEY for cloud."
            )

        raise ValueError("Unknown STT provider: %s" % provider)


__all__ = [
    "DeepgramStreamingSTT",
    "LocalStreamingSTT",
    "StreamingSTTEngine",
    "StreamingSTTFactory",
]
