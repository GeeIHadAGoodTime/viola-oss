"""Plain per-call phone latency traces for the deterministic live grader."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)

# v2 (2026-08-08): `llm_first_token_ms` is renamed to
# `post_transcript_to_first_token_ms`. The old name read like a model metric while
# being anchored at STT-done, and a real grade misread it exactly that way -- see
# `mark_llm_first_token` below for the incident. Consumers that still meet a v1
# artifact should treat the legacy key as the same measurement under the honest name.
PHONE_LATENCY_TRACE_SCHEMA_VERSION = 2
_TRACE_USER_PARTITION = "phone/by_user"
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _hash_user_id(user_id: str | None) -> str:
    raw = (user_id or "unknown-user").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _safe_path_segment(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_PATH_RE.sub("-", str(value or "").strip()).strip(".-")
    return cleaned[:128] if cleaned else fallback


def _date_segment(started_at: str | datetime | None) -> str:
    if isinstance(started_at, datetime):
        return started_at.astimezone(UTC).strftime("%Y%m%d")
    if started_at:
        try:
            return datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).astimezone(UTC).strftime("%Y%m%d")
        except ValueError:
            logger.debug("PhoneLatencyTraceWriter: invalid started_at %r", started_at)
    return datetime.now(tz=UTC).strftime("%Y%m%d")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(child) for child in value]
    return str(value)


@dataclass
class PhoneLatencyTraceWriter:
    """Append-only plain JSONL writer consumed by ``tools/phone_live_grader.py``."""

    call_id: str
    path: Path
    _disabled: bool = field(default=False, init=False, repr=False)

    @classmethod
    def for_call(
        cls,
        *,
        call_id: str,
        user_id: str | None,
        started_at: str | datetime | None = None,
        root_dir: Path | None = None,
    ) -> PhoneLatencyTraceWriter:
        safe_call_id = _safe_path_segment(
            call_id,
            fallback="call-%s" % hashlib.sha256(call_id.encode()).hexdigest()[:8],
        )
        trace_root = root_dir or (get_logs_dir() / "traces")
        path = (
            trace_root
            / _TRACE_USER_PARTITION
            / _hash_user_id(user_id)
            / _date_segment(started_at)
            / ("%s.trace.jsonl" % safe_call_id)
        )
        return cls(call_id=call_id, path=path)

    def append_event(self, event: str, **payload: Any) -> None:
        if self._disabled:
            return
        record = {
            "schema": "phone_latency_trace",
            "schema_version": PHONE_LATENCY_TRACE_SCHEMA_VERSION,
            "ts": _utc_now(),
            "event": event,
            "call_id": self.call_id,
        }
        record.update({key: _jsonable(value) for key, value in payload.items() if value is not None})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        except (OSError, ValueError, TypeError) as exc:
            # Trace writing must never break the live audio path: absorb disk/permission (OSError) and
            # any serialization (ValueError/TypeError) error, self-disable, and let the call continue.
            self._disabled = True
            logger.debug("PhoneLatencyTraceWriter disabled for %s: %s", self.call_id, exc)


@dataclass
class _TurnTiming:
    turn_index: int
    stt_done_perf: float
    stage_ms: dict[str, float] = field(default_factory=dict)
    speech_end_perf: float | None = None
    llm_started_perf: float | None = None
    llm_first_token_perf: float | None = None
    first_audio_perf: float | None = None


class PhoneLatencyTraceRecorder:
    """Stateful turn-latency recorder shared by multiple Pipecat frame taps."""

    def __init__(self, writer: PhoneLatencyTraceWriter, *, source: str) -> None:
        self._writer = writer
        self._source = source
        self._started_perf = time.perf_counter()
        self._turn_index = 0
        self._pending: _TurnTiming | None = None
        self._pending_stage_ms: dict[str, float] = {}
        self._pending_speech_end_perf: float | None = None
        self._completed_turns = 0

    @property
    def path(self) -> Path:
        return self._writer.path

    def start(self, **payload: Any) -> None:
        self._writer.append_event("call_trace_start", source=self._source, **payload)

    def record_media_path(self, *, mode: str, stream_url: str) -> None:
        parsed = urlparse(stream_url or "")
        host = parsed.hostname or ""
        scheme = parsed.scheme or ""
        self._writer.append_event(
            "call_media_path",
            source=self._source,
            mode=mode,
            stream_scheme=scheme,
            stream_host=host,
            production_named_tunnel=(scheme == "wss" and host == "phone.useviola.com"),
            quick_tunnel=host.endswith(".trycloudflare.com"),
        )

    def complete(self, **payload: Any) -> None:
        self.flush_incomplete(reason="call_complete")
        self._writer.append_event(
            "call_trace_complete",
            source=self._source,
            completed_turns=self._completed_turns,
            **payload,
        )

    def mark_user_speech_stopped(
        self,
        *,
        stop_secs: float | None = None,
        speech_end_perf: float | None = None,
    ) -> None:
        clean_stop_secs = max(0.0, float(stop_secs or 0.0))
        end_perf = speech_end_perf if speech_end_perf is not None else time.perf_counter() - clean_stop_secs
        self._pending_speech_end_perf = end_perf
        self._writer.append_event(
            "user_speech_stopped",
            source=self._source,
            stop_secs=clean_stop_secs,
        )

    def mark_user_transcription(self, *, finalized: bool, text: str | None = None) -> None:
        if not finalized:
            return
        self.flush_incomplete(reason="new_user_turn")
        self._turn_index += 1
        stt_done_perf = time.perf_counter()
        speech_end_perf = self._pending_speech_end_perf
        stage_ms = dict(self._pending_stage_ms)
        if speech_end_perf is not None:
            stage_ms["post_speech_to_transcript_ms"] = max(0.0, (stt_done_perf - speech_end_perf) * 1000.0)
        self._pending = _TurnTiming(
            turn_index=self._turn_index,
            stt_done_perf=stt_done_perf,
            stage_ms=stage_ms,
            speech_end_perf=speech_end_perf,
        )
        self._pending_speech_end_perf = None
        self._pending_stage_ms.clear()
        self._writer.append_event(
            "turn_user_transcription",
            source=self._source,
            turn_index=self._turn_index,
            text_chars=len(text or ""),
            **self._pending.stage_ms,
        )

    def mark_llm_context_dispatched(self) -> None:
        """Record when the aggregated LLMContext/Run frame reaches the LLM input.

        This is the ``stt_done -> context-frame-ready-at-LLM`` sub-stage: the
        time the finalized user transcript spends traversing the aggregator +
        turn-stop + upstream processor chain before a context frame is even
        pushed toward the LLM service. It is the FIRST half of the previously
        invisible window between STT-done and the LLM request stages.
        """
        pending = self._pending
        if pending is None or "post_transcript_to_context_ms" in pending.stage_ms:
            return
        value_ms = max(0.0, (time.perf_counter() - pending.stt_done_perf) * 1000.0)
        pending.stage_ms["post_transcript_to_context_ms"] = value_ms
        self._writer.append_event(
            "llm_context_dispatched",
            source=self._source,
            turn_index=pending.turn_index,
            post_transcript_to_context_ms=value_ms,
        )

    def mark_llm_process_context_start(self) -> None:
        """Record when the LLM service actually begins request preparation.

        This is the ``stt_done -> _process_context-entry`` measurement: the
        WHOLE previously-invisible window between STT-done and the point where
        the already-instrumented request stages (adapter_params/response_params/
        trace_enqueue/request_prep) begin. Subtracting
        ``post_transcript_to_context_ms`` isolates the response-gate + LLM
        input-queue portion; the remainder is the aggregator/turn-stop chain.
        """
        pending = self._pending
        if pending is None or "post_transcript_to_llm_dispatch_ms" in pending.stage_ms:
            return
        value_ms = max(0.0, (time.perf_counter() - pending.stt_done_perf) * 1000.0)
        pending.stage_ms["post_transcript_to_llm_dispatch_ms"] = value_ms
        self._writer.append_event(
            "llm_process_context_start",
            source=self._source,
            turn_index=pending.turn_index,
            post_transcript_to_llm_dispatch_ms=value_ms,
        )

    def mark_llm_start(self) -> None:
        pending = self._pending
        if pending is not None and pending.llm_started_perf is None:
            pending.llm_started_perf = time.perf_counter()

    def mark_llm_first_token(self) -> None:
        """Record the first LLM *text* frame leaving the LLM, anchored at STT-done.

        NAMING IS LOAD-BEARING HERE. This was called ``llm_first_token_ms`` and it
        cost a real misdiagnosis. It is measured from ``stt_done_perf`` -- the
        finalized transcript -- not from request-sent, so it contains the whole
        aggregator/turn-stop hold AND the request prep AND the model's own TTFB AND
        the reasoning phase before the first text token. Anything reading it as "how
        long the model took" overstates the model by whatever the pipeline spent in
        front of it.

        That is not hypothetical. On graded call 6f271e68 (2026-08-07) the deterministic
        grader bucketed this field under a stage literally labelled "LLM TTFB" and
        reported "LLM TTFB avg 3986 ms, max 5415 ms" -- which is exactly the mean and
        max of this field across that call's six turns. Pipecat's real per-turn model
        TTFB on the very same call averaged 1636 ms. The report sent an investigation
        after the model; the model was never the dominant term.

        So the anchor now lives in the name, matching its siblings
        ``post_transcript_to_context_ms`` / ``post_transcript_to_llm_dispatch_ms``:
        every ``post_transcript_to_*`` field is measured from the same STT-done origin
        and none of them can be mistaken for a service's own timing.

        ``llm_generation_to_text_ms`` closes the last unnamed hole in the per-turn
        budget: pipecat's ``llm_ttfb_ms`` stops at the first token the API returns,
        which for a reasoning model is a reasoning token, while the pipeline cannot
        speak until the first TEXT frame arrives. On 6f271e68 that stretch ran 435 ms
        to 1940 ms per turn and scaled with the turn's reasoning-token count. It was
        derivable only by subtracting three fields, so in practice nobody derived it
        and the budget never visibly closed. With it, the turn's stages sum to
        ``post_speech_to_first_audio_ms`` exactly.
        """
        pending = self._pending
        if pending is None or pending.llm_first_token_perf is not None:
            return
        now = time.perf_counter()
        pending.llm_first_token_perf = now
        value_ms = (now - pending.stt_done_perf) * 1000.0
        pending.stage_ms.setdefault("post_transcript_to_first_token_ms", value_ms)
        dispatch_ms = pending.stage_ms.get("post_transcript_to_llm_dispatch_ms")
        model_ttfb_ms = pending.stage_ms.get("llm_ttfb_ms")
        if dispatch_ms is not None and model_ttfb_ms is not None:
            pending.stage_ms.setdefault(
                "llm_generation_to_text_ms",
                max(0.0, value_ms - float(dispatch_ms) - float(model_ttfb_ms)),
            )

    def mark_first_audio(self, *, audio_bytes: int | None = None, sample_rate: int | None = None) -> None:
        pending = self._pending
        if pending is None or pending.first_audio_perf is not None:
            return
        now = time.perf_counter()
        pending.first_audio_perf = now
        if pending.llm_first_token_perf is not None:
            pending.stage_ms.setdefault("tts_first_audio_ms", (now - pending.llm_first_token_perf) * 1000.0)
        if pending.speech_end_perf is not None:
            pending.stage_ms["post_speech_to_first_audio_ms"] = max(
                0.0,
                (now - pending.speech_end_perf) * 1000.0,
            )
        pending.stage_ms["e2e_ms"] = (now - pending.stt_done_perf) * 1000.0
        self._emit_turn_latency(
            pending,
            source_event="first_audio",
            audio_bytes=audio_bytes,
            sample_rate=sample_rate,
        )

    def record_user_bot_latency(self, latency_seconds: float) -> None:
        value_ms = max(0.0, float(latency_seconds or 0.0) * 1000.0)
        pending = self._pending
        if pending is None:
            payload = {"user_bot_observer_ms": value_ms}
            if self._completed_turns == 0:
                payload["e2e_ms"] = value_ms
            self._writer.append_event(
                "user_bot_latency",
                source=self._source,
                **payload,
            )
            return
        pending.stage_ms["user_bot_latency_ms"] = value_ms
        pending.stage_ms["e2e_ms"] = value_ms
        self._emit_turn_latency(pending, source_event="user_bot_latency_observer")

    def record_opening_stage(self, stage: str, *, elapsed_ms: float, **payload: Any) -> None:
        """Record one stage of the pre-first-word (opening) window.

        The opening turn is the one the caller judges the product on, and it was the
        ONE turn this recorder could not decompose: ``mark_llm_context_dispatched``
        and ``mark_llm_process_context_start`` both no-op while ``_pending`` is None,
        and ``_pending`` is only created by a finalized user ``TranscriptionFrame``
        (``mark_user_transcription``). Viola's opening is driven by the aggregator's
        own run frame before any Viola-facing user turn exists, so the whole
        answer-settle -> voicemail-classify -> gate-release chain in front of it
        appeared in the trace as a silent multi-second gap between the recipient's STT
        metric and the first ``llm_request_stage``. On the 2026-07-25 cloud calls that
        gap was 6.42s / 6.01s of the 9.76s / 9.55s first-word latency and could not be
        attributed from the trace at all (#2587).

        These stages are emitted from the opening path itself, so the gap is named
        rather than inferred: the answer-settle wait and the one-shot voicemail
        classification that gates the opening context.
        """
        self._writer.append_event(
            "opening_stage",
            source=self._source,
            stage=str(stage),
            elapsed_ms=max(0.0, float(elapsed_ms)),
            since_call_start_ms=max(0.0, (time.perf_counter() - self._started_perf) * 1000.0),
            **payload,
        )

    def record_turn_guard_event(self, event: str, **payload: Any) -> None:
        """Emit a self-echo / turn-guard lifecycle event (observe-only instrumentation).

        Used by ``SelfEchoGuardedMinWordsUserTurnStartStrategy`` to record barge-in
        candidates (with the guard's suppress/pass disposition) and the bot
        started/stopped-speaking transitions. Pure event emission: it inherits the
        writer's automatic ``ts`` + ``call_id`` stamp and never influences the guard's
        suppress/pass decision. ``append_event`` drops any ``None`` payload values.
        """
        self._writer.append_event(event, source=self._source, **payload)

    def record_tool_call(self, tool_name: str) -> None:
        self._writer.append_event(
            "tool_call",
            source=self._source,
            tool_name=tool_name,
            elapsed_seconds=time.perf_counter() - self._started_perf,
        )

    def record_vad_sample(
        self,
        *,
        confidence: float,
        volume: float,
        state: str,
        confidence_threshold: float,
        min_volume: float,
        is_transition: bool,
    ) -> None:
        """Record a live inbound-VAD calibration sample.

        Non-behavioral instrumentation: captures the (confidence, volume, state)
        the base analyzer actually computed, so the live per-turn distribution can
        set ``PHONE_VAD_CONFIDENCE`` / ``min_volume`` / ``start_secs`` from real
        traffic instead of an attenuated recording. Consumed offline; not on any
        latency-critical path.
        """
        self._writer.append_event(
            "vad_calibration",
            source=self._source,
            confidence=confidence,
            volume=volume,
            state=state,
            confidence_threshold=confidence_threshold,
            min_volume=min_volume,
            is_transition=is_transition,
            elapsed_seconds=time.perf_counter() - self._started_perf,
        )

    def record_metric(
        self,
        *,
        processor: str,
        metric_type: str,
        value_ms: float,
        stage: str,
        model: str | None = None,
    ) -> None:
        metric_key = _metric_key(stage=stage, metric_type=metric_type)
        if not metric_key:
            return
        value_ms = max(0.0, float(value_ms))
        target = self._pending.stage_ms if self._pending is not None else self._pending_stage_ms
        target[metric_key] = value_ms
        self._writer.append_event(
            "pipecat_metric",
            source=self._source,
            metric_stage=stage,
            metric_type=metric_type,
            processor=processor,
            model=model,
            **{metric_key: value_ms},
        )

    def record_llm_request_stage(
        self,
        *,
        stage: str,
        duration_ms: float,
        tool_count: int | None = None,
        request_bytes: int | None = None,
        input_items: int | None = None,
        instruction_chars: int | None = None,
        prompt_cache_key: str | None = None,
        prompt_cache_key_set: bool | None = None,
    ) -> None:
        clean_stage = _safe_path_segment(stage, fallback="request").replace("-", "_")
        metric_key = "llm_%s_ms" % clean_stage
        value_ms = max(0.0, float(duration_ms))
        target = self._pending.stage_ms if self._pending is not None else self._pending_stage_ms
        target[metric_key] = value_ms
        self._writer.append_event(
            "llm_request_stage",
            source=self._source,
            stage=clean_stage,
            **{metric_key: value_ms},
            tool_count=tool_count,
            request_bytes=request_bytes,
            input_items=input_items,
            instruction_chars=instruction_chars,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_key_set=prompt_cache_key_set,
        )

    def record_llm_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cached_input_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        self._writer.append_event(
            "llm_usage",
            source=self._source,
            input_tokens=max(0, int(input_tokens or 0)),
            output_tokens=max(0, int(output_tokens or 0)),
            total_tokens=max(0, int(total_tokens or 0)),
            cached_input_tokens=max(0, int(cached_input_tokens or 0)),
            reasoning_tokens=max(0, int(reasoning_tokens or 0)),
        )

    def flush_incomplete(self, *, reason: str) -> None:
        pending = self._pending
        if pending is None:
            return
        self._writer.append_event(
            "turn_latency_incomplete",
            source=self._source,
            turn_index=pending.turn_index,
            reason=reason,
            **pending.stage_ms,
        )
        self._pending = None

    def _emit_turn_latency(self, pending: _TurnTiming, *, source_event: str, **payload: Any) -> None:
        self._completed_turns += 1
        self._writer.append_event(
            "turn_latency",
            source=self._source,
            turn_index=pending.turn_index,
            source_event=source_event,
            **pending.stage_ms,
            **payload,
        )
        self._pending = None


def _metric_key(*, stage: str, metric_type: str) -> str:
    stage_key = stage.lower().replace(" ", "_")
    if stage_key == "llm_ttfb":
        return "llm_ttfb_ms"
    if stage_key == "stt":
        return "stt_ms" if metric_type == "processing" else "stt_%s_ms" % metric_type
    if stage_key == "tts":
        return "tts_ms" if metric_type == "processing" else "tts_%s_ms" % metric_type
    if stage_key == "vad":
        return "vad_ms" if metric_type in {"processing", "turn"} else "vad_%s_ms" % metric_type
    return ""


try:
    from pipecat.frames.frames import (
        FunctionCallsStartedFrame,
        LLMContextFrame,
        LLMFullResponseStartFrame,
        LLMRunFrame,
        LLMTextFrame,
        MetricsFrame,
        OutputAudioRawFrame,
        TextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSSpeakFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.metrics.metrics import (
        ProcessingMetricsData,
        SmartTurnMetricsData,
        TextAggregationMetricsData,
        TTFBMetricsData,
        TurnMetricsData,
    )
    from pipecat.processors.frame_processor import FrameProcessor

    PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard for non-pipecat unit environments.
    PIPECAT_AVAILABLE = False


if PIPECAT_AVAILABLE:

    class PhoneLatencyTraceProcessor(FrameProcessor):
        """Pipecat frame tap that feeds a shared ``PhoneLatencyTraceRecorder``."""

        def __init__(
            self,
            recorder: PhoneLatencyTraceRecorder,
            *,
            stage: Literal["inbound", "llm_inbound", "llm", "outbound"],
        ) -> None:
            super().__init__(name="phone_latency_trace_%s" % stage)
            self._recorder = recorder
            self._stage = stage

        async def process_frame(self, frame: Any, direction: Any) -> None:
            await super().process_frame(frame, direction)
            self._observe_frame(frame)
            await self.push_frame(frame, direction)

        def _observe_frame(self, frame: Any) -> None:
            if self._stage == "inbound":
                self._observe_inbound(frame)
            elif self._stage == "llm_inbound":
                self._observe_llm_inbound(frame)
            elif self._stage == "llm":
                self._observe_llm(frame)
            elif self._stage == "outbound":
                self._observe_outbound(frame)

        def _observe_llm_inbound(self, frame: Any) -> None:
            # Placed immediately BEFORE the LLM service: the aggregated context
            # (or an explicit run) frame passing here is the moment the user
            # turn's context is handed toward the model. Marks the aggregator/
            # turn-stop portion of the STT-done -> LLM-request window.
            if isinstance(frame, (LLMContextFrame, LLMRunFrame)):
                self._recorder.mark_llm_context_dispatched()

        def _observe_inbound(self, frame: Any) -> None:
            if isinstance(frame, VADUserStoppedSpeakingFrame):
                self._recorder.mark_user_speech_stopped(stop_secs=getattr(frame, "stop_secs", None))
                return
            if isinstance(frame, TranscriptionFrame):
                self._recorder.mark_user_transcription(
                    finalized=getattr(frame, "finalized", True) is not False,
                    text=str(getattr(frame, "text", "") or ""),
                )
                return
            if isinstance(frame, MetricsFrame):
                self._record_metrics(frame, allowed_stages={"STT", "VAD"})

        def _observe_llm(self, frame: Any) -> None:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._recorder.mark_llm_start()
                return
            if isinstance(frame, (LLMTextFrame, FunctionCallsStartedFrame)):
                self._recorder.mark_llm_first_token()
                return
            if isinstance(frame, MetricsFrame):
                self._record_metrics(frame, allowed_stages={"LLM TTFB"})

        def _observe_outbound(self, frame: Any) -> None:
            if isinstance(frame, (OutputAudioRawFrame, TTSAudioRawFrame)):
                self._recorder.mark_first_audio(
                    audio_bytes=len(getattr(frame, "audio", b"") or b""),
                    sample_rate=getattr(frame, "sample_rate", None),
                )
                return
            if isinstance(frame, (TextFrame, TTSSpeakFrame)):
                self._recorder.mark_llm_first_token()
                return
            if isinstance(frame, MetricsFrame):
                self._record_metrics(frame, allowed_stages={"TTS"})

        def _record_metrics(self, frame: Any, *, allowed_stages: set[str]) -> None:
            for entry in frame.data or ():
                metric = _classify_metric(entry)
                if metric is None:
                    continue
                stage, metric_type, value_ms = metric
                if stage not in allowed_stages:
                    continue
                self._recorder.record_metric(
                    processor=str(getattr(entry, "processor", "") or ""),
                    metric_type=metric_type,
                    value_ms=value_ms,
                    stage=stage,
                    model=getattr(entry, "model", None),
                )


def _classify_metric(entry: Any) -> tuple[str, str, float] | None:
    processor = str(getattr(entry, "processor", "") or "").lower()
    if PIPECAT_AVAILABLE and isinstance(entry, TTFBMetricsData):
        if any(marker in processor for marker in ("tts", "kokoro", "eleven", "synth")):
            return "TTS", "ttfb", float(entry.value or 0.0) * 1000.0
        return "LLM TTFB", "ttfb", float(entry.value or 0.0) * 1000.0
    if PIPECAT_AVAILABLE and isinstance(entry, ProcessingMetricsData):
        if any(marker in processor for marker in ("stt", "whisper", "deepgram", "transcrib")):
            return "STT", "processing", float(entry.value or 0.0) * 1000.0
        if any(marker in processor for marker in ("tts", "kokoro", "eleven", "synth")):
            return "TTS", "processing", float(entry.value or 0.0) * 1000.0
        if any(marker in processor for marker in ("vad", "smart", "turn")):
            return "VAD", "processing", float(entry.value or 0.0) * 1000.0
    if PIPECAT_AVAILABLE and isinstance(entry, (SmartTurnMetricsData, TurnMetricsData)):
        return (
            "VAD",
            "turn",
            float(getattr(entry, "e2e_processing_time_ms", 0.0) or 0.0),
        )
    if PIPECAT_AVAILABLE and isinstance(entry, TextAggregationMetricsData):
        return "LLM TTFB", "text_aggregation", float(entry.value or 0.0) * 1000.0
    return None


if not PIPECAT_AVAILABLE:

    class PhoneLatencyTraceProcessor:  # pragma: no cover - only used when Pipecat is unavailable.
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("PhoneLatencyTraceProcessor requires Pipecat")
