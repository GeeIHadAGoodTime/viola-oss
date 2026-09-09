"""Streaming STT for the phone lane (sherpa-onnx NeMo cache-aware transducer).

Phase 1 of the founder-approved staged rollout
(`_diag/2026-07-05/streaming_stt_bakeoff/DECISION_REPORT.md`). The streaming
engine becomes the phone lane's *first pass*: it produces real interim
transcripts mid-utterance so the turn-taking / interruption stack can see a long
barge-in as it happens, while batch faster-whisper KEEPS final-text authority
(zero accuracy risk). Phase 2 (NOT this module's job) flips final authority to
the streaming engine behind a live A/B.

Why this exists / the latent gap it closes
-------------------------------------------
The phone user aggregator already wires
``SelfEchoGuardedMinWordsUserTurnStartStrategy(use_interim=True)`` — it is *built*
to gate interruptions on interim transcripts. But the batch
``SegmentedSTTService`` (faster-whisper) emits NO interim frames mid-utterance:
it only decodes a whole segment after VAD stop. So today a long interruption
cannot trigger the gate until the batch decode lands. This service emits
``InterimTranscriptionFrame`` as the partial changes (~280 ms partial lag,
measured), making that gate live.

Design (per the decision report, verified against the running code)
-------------------------------------------------------------------
- Subclass pipecat's ``STTService`` DIRECTLY (not ``SegmentedSTTService``): the
  base feeds every ``InputAudioRawFrame`` to ``run_stt`` (continuous), which is
  exactly what a frame-synchronous streaming recognizer wants.
- ONE shared ``sherpa_onnx.OnlineRecognizer`` per process (warm, lazily built),
  ONE ``OnlineStream`` per call, ``num_threads=1``. Decoding runs on a worker
  thread (``asyncio.to_thread``); each instance serializes its own stream ops.
- Reuse the proven prod front-end ``phone_pcm_to_whisper_float`` (8k->16k
  polyphase) so the recognizer sees the same 16 kHz float the bake-off measured.
- Interims flow downstream to the user aggregator's turn strategies. They are
  NEVER written to context: pipecat's aggregator only aggregates final
  ``TranscriptionFrame`` text; ``InterimTranscriptionFrame`` merely sets
  ``_seen_interim_results`` (verified in llm_response.py). So whisper's final
  remains the single authority — no double context writes.
- Fail-open: if sherpa-onnx or the model is unavailable, the phone pipeline
  simply does not construct this service and runs today's whisper-only path.

Two emit modes:
- ``interims`` (Phase 1): emit only ``InterimTranscriptionFrame``. Whisper owns
  the final. Stream is reset at each VAD turn boundary for the next turn.
- ``full`` (Phase 2 scaffold): additionally flush at turn stop and emit a
  finalized ``TranscriptionFrame``. Wiring whisper OUT of the call path is a
  Phase-2 change and is intentionally NOT done here.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.services.stt_service import STTService
from pipecat.utils.time import time_now_iso8601

from core.logging_config import get_logger
from telephony.phone_stt_options import phone_pcm_to_whisper_float
from telephony.streaming_stt_model import (
    STREAMING_STT_MODEL_FILES,
    streaming_stt_model_available,
    streaming_stt_model_dir,
)

logger = get_logger(__name__)

# The recognizer is stateless across streams (all per-utterance state lives in the
# OnlineStream), so one warm instance is shared by every concurrent call. Built
# lazily under a lock so the ~2.7 s load happens once, off the call hot path.
_shared_recognizer: Any = None
_shared_recognizer_lock = threading.Lock()

# Right-context silence padded at flush so the transducer's lookahead can emit the
# final words of the turn (measured flush ~46 ms p50). Only used in "full" mode.
_FLUSH_SILENCE_SECS = 0.5

VALID_STREAMING_MODES = ("off", "interims", "full")


def get_shared_streaming_recognizer() -> Any:
    """Return the process-wide warm ``OnlineRecognizer`` (building it once).

    Raises if sherpa-onnx is not importable or the model files are missing — the
    caller (phone pipeline) checks availability first and fails open, so this only
    raises on a genuinely misconfigured attempt to use streaming STT.
    """
    global _shared_recognizer
    if _shared_recognizer is not None:
        return _shared_recognizer
    with _shared_recognizer_lock:
        if _shared_recognizer is not None:
            return _shared_recognizer
        import sherpa_onnx  # lazy: keeps `import cloud_app` / cloud image clean when unused.

        model_dir = streaming_stt_model_dir()
        paths = {name: str(model_dir / name) for name in STREAMING_STT_MODEL_FILES}
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=paths["tokens.txt"],
            encoder=paths["encoder.onnx"],
            decoder=paths["decoder.onnx"],
            joiner=paths["joiner.onnx"],
            num_threads=1,
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            model_type="nemo",
        )
        _shared_recognizer = recognizer
        logger.info("Streaming phone STT recognizer loaded (%s)", model_dir.name)
        return _shared_recognizer


class SherpaOnnxStreamingSTTService(STTService):
    """Streaming phone STT that emits interim transcripts for turn-taking.

    See module docstring for the full rationale. In ``interims`` mode this emits
    only ``InterimTranscriptionFrame`` and leaves final-text authority to the
    batch whisper service still in the pipeline.
    """

    # 16 kHz stream rate the recognizer is built for; the front-end resamples the
    # 8 kHz telephone PCM up to this before the recognizer sees it.
    _RECOGNIZER_RATE = 16000

    def __init__(
        self,
        *,
        mode: str = "interims",
        sample_rate: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the streaming STT service.

        Args:
            mode: ``interims`` (Phase 1, emit interims only) or ``full`` (Phase 2
                scaffold, also emit a finalized transcript at turn stop).
            sample_rate: The inbound telephone audio rate (pinned by the caller to
                the real 8 kHz rate so the 8k->16k front-end resamples correctly).
            **kwargs: Forwarded to the pipecat ``STTService`` base.
        """
        super().__init__(sample_rate=sample_rate, **kwargs)
        if mode not in ("interims", "full"):
            raise ValueError("streaming STT mode must be 'interims' or 'full', got %r" % mode)
        self._mode = mode
        self._stream: Any = None
        self._recognizer: Any = None
        # Serializes this instance's stream ops (accept/decode/get_result/reset).
        # Uncontended across calls (each has its own stream + lock); the shared
        # recognizer supports concurrent per-stream decode.
        self._stream_lock = threading.Lock()
        self._last_interim = ""

    async def start(self, frame: Any) -> None:
        """Build the shared recognizer + this call's stream at pipeline start.

        Fail-open: if the recognizer cannot be built (e.g. the model was removed
        between the pipeline's availability check and now), the service disables
        itself (run_stt/turn-stop become no-ops) so the call proceeds on batch
        whisper — a streaming-STT problem must never break a live call.
        """
        await super().start(frame)
        try:
            # Build lazily off the event loop; the recognizer is shared/warm after the
            # first call, so this is a cheap create_stream() on subsequent calls.
            self._recognizer = await asyncio.to_thread(get_shared_streaming_recognizer)
            self._stream = self._recognizer.create_stream()
            self._last_interim = ""
        except Exception:
            logger.exception(
                "Streaming phone STT failed to initialize; disabling it for this call "
                "and continuing on batch whisper."
            )
            self._recognizer = None
            self._stream = None

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Feed one audio frame to the recognizer and emit interim partials.

        The base calls this for every ``InputAudioRawFrame``. We resample to 16 kHz,
        push into the online stream, drain the decode, and yield an
        ``InterimTranscriptionFrame`` whenever the running partial changes.
        """
        if not self._stream or not self._recognizer:
            return
        if not audio:
            return

        # Resample 8k -> 16k float32 with the proven prod front-end.
        samples = phone_pcm_to_whisper_float(audio, self.sample_rate)
        if samples.size == 0:
            return

        text = await asyncio.to_thread(self._accept_and_decode, samples)
        if text and text != self._last_interim:
            self._last_interim = text
            yield InterimTranscriptionFrame(
                text,
                self._user_id,
                time_now_iso8601(),
                self._settings.language,
            )

    def _accept_and_decode(self, samples: Any) -> str:
        """Worker-thread: push samples, drain decode, return the running partial."""
        rec, stream = self._recognizer, self._stream
        if rec is None or stream is None:
            return ""
        with self._stream_lock:
            stream.accept_waveform(self._RECOGNIZER_RATE, samples)
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            return (rec.get_result(stream) or "").strip()

    async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame) -> None:
        """At a VAD turn boundary: (full mode) flush a final, then reset the stream.

        Resetting clears the recognizer's per-turn state so the next turn's interims
        start fresh — without it the running partial would accumulate across the whole
        call and permanently exceed the interruption word-count gate.
        """
        await super()._handle_vad_user_stopped_speaking(frame)
        if self._mode == "full":
            final_text = await asyncio.to_thread(self._flush_final)
            if final_text:
                await self.push_frame(
                    TranscriptionFrame(
                        final_text,
                        self._user_id,
                        time_now_iso8601(),
                        self._settings.language,
                    )
                )
        await asyncio.to_thread(self._reset_stream)

    def _flush_final(self) -> str:
        """Worker-thread: pad right-context silence, drain, return the final text."""
        import numpy as np

        rec, stream = self._recognizer, self._stream
        if rec is None or stream is None:
            return ""
        with self._stream_lock:
            pad = np.zeros(int(_FLUSH_SILENCE_SECS * self._RECOGNIZER_RATE), dtype=np.float32)
            stream.accept_waveform(self._RECOGNIZER_RATE, pad)
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            return (rec.get_result(stream) or "").strip()

    def _reset_stream(self) -> None:
        """Worker-thread: reset this call's stream for the next turn."""
        rec, stream = self._recognizer, self._stream
        if rec is None or stream is None:
            return
        with self._stream_lock:
            rec.reset(stream)
        self._last_interim = ""

    async def cleanup(self) -> None:
        """Drop this call's stream (the shared recognizer stays warm)."""
        await super().cleanup()
        self._stream = None
        self._recognizer = None


def streaming_stt_runtime_available() -> bool:
    """True when both the sherpa-onnx runtime and the model files are present.

    The phone pipeline calls this to decide whether to construct the streaming
    service; False means fail open to today's batch-whisper path.
    """
    if not streaming_stt_model_available():
        return False
    import importlib.util

    return importlib.util.find_spec("sherpa_onnx") is not None
