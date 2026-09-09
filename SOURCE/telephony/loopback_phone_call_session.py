"""Pipecat phone-call bench using LoopbackTransport.

This module runs the production phone-call pipeline shape in process, replacing
Telnyx media with ``telephony.loopback_transport.LoopbackTransport``. It keeps
the production prompt builder, phone tool surface, hold/language/voicemail
handlers, transcript frame collectors, audio tee processors, event-bus
transcript broadcasts, and call-record lifecycle state. No Telnyx network,
carrier webhook, browser WebSocket, microphone, or speaker is required.

Documented limits:
- Whisper STT is bypassed in ``text_inject`` mode. Accent handling, real
  utterance batching, VAD edge cases, and Whisper transcription accuracy are
  not exercised.
- Kokoro TTS audio quality is not exercised. ``capture`` mode emits synthetic
  PCM bytes so downstream recording/listen-in code sees audio frames, but the
  bytes are not spoken audio.
- ``real_whisper`` and ``real_kokoro`` modes exercise the local production
  faster-whisper and Kokoro processors while still using LoopbackTransport.
- Telnyx call_control events are replaced by direct pipeline lifecycle calls.
  DIAL_INITIATED, RINGING, AMD webhook ordering, and carrier hangup sequences
  are not simulated.
- Network behavior such as jitter, packet loss, websocket reconnects, and
  remote listener delivery is not exercised.
"""

from __future__ import annotations

import asyncio
import audioop
import contextvars
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.events.bus import EventBus, Subscription, get_event_bus, set_event_bus
from core.events.types import BaseEvent, CallLifecycleEvent, CallTranscriptDelta
from core.logging_config import get_logger
from telephony.answer_settle import AnswerSettleObserver
from telephony.audio_bridge import AudioBridge
from telephony.audio_tee_processor import AudioTeeProcessor
from telephony.call_context import (
    build_phone_system_instruction,
    build_phone_volatile_context,
)
from telephony.call_manager import (
    PHONE_INBOUND_SAMPLE_RATE,
    PHONE_VAD_CONFIDENCE,
    CallManager,
    CallRecord,
    CallStatus,
    TranscriptCollector,
    _create_phone_voicemail_classifier_llm,
    _guard_phone_function_handler,
    _maybe_build_phone_codex_client,
    _wrap_openai_client_for_phone_spend_accounting,
    build_phone_llm_tool_surface,
    queue_outbound_opening_after_answer_settle,
)
from telephony.config import TelnyxConfig, default_phone_tts_voice
from telephony.loopback_transport import (
    CHUNK_BYTES,
    CHUNK_DURATION_MS,
    LOOPBACK_CHANNELS,
    LOOPBACK_SAMPLE_RATE,
)
from telephony.phone_latency_trace import (
    PhoneLatencyTraceProcessor,
    PhoneLatencyTraceRecorder,
    PhoneLatencyTraceWriter,
)
from telephony.phone_simulator import PhoneEvent, PhoneScenario

logger = get_logger(__name__)

_DEFAULT_CALL_MAX_SECONDS = 600
_TURN_WAIT_TIMEOUT_SECONDS = 10.0
_TEE_DRAIN_SECONDS = 0.05
_PARTIAL_TRANSCRIPTION_GAP_SECONDS = 0.1
_SAMPLE_WIDTH_BYTES = 2
# Mirror production's audio_out_sample_rate (call_manager.py:5052 / :4455). Prod
# pins outbound to 16 kHz while inbound stays at the native 8 kHz
# PHONE_INBOUND_SAMPLE_RATE; the rig pins the same so the StartFrame the pipeline
# propagates matches the carrier exactly.
_PHONE_AUDIO_OUT_SAMPLE_RATE = 16000
_SINE_HZ = 440.0
_SINE_AMPLITUDE = 16000
_DEFAULT_USER_ID = "loopback-user"
_VALID_STT_MODES = {"text_inject", "real_whisper"}
_VALID_TTS_MODES = {"capture", "real_kokoro"}
_VALID_RECEPTIONIST_MODES = {"text_inject", "tts_bot", "file_watcher_bot"}
_RECEPTIONIST_TTS_VOICE = "am_adam"
_RECEPTIONIST_SILENCE_MS = 1500
_RECEPTIONIST_RMS_THRESHOLD = 250.0
_RECEPTIONIST_INITIAL_DELAY_SECONDS = 0.2
_TTS_RECEPTIONIST_ANSWER_OBSERVATION_SECONDS = 2.0
_RECEPTIONIST_WAIT_TIMEOUT_SECONDS = 90.0
_RECEPTIONIST_ASSISTANT_TURN_TIMEOUT_SECONDS = 45.0
_RECEPTIONIST_INPUT_VAD_STOP_SECS = 1.0
_TERMINAL_CALL_STATUSES = {
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
    CallStatus.TIMEOUT,
    CallStatus.VOICEMAIL,
}


ToolOverride = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


def _parse_scenario_current_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid phone scenario current_time: %s" % raw) from exc


@dataclass(slots=True)
class ScriptedAssistantTurn:
    """One scripted assistant turn for ``ScriptedLoopbackLLM``."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LoopbackCallManager(CallManager):
    """CallManager variant whose hangup path drains the loopback pipeline."""

    async def end_call(
        self,
        call_id: str,
        *,
        wait_for_cleanup: bool = True,
        user_id: str | None = None,
    ) -> bool:
        del wait_for_cleanup, user_id
        record = self._active_calls.get(call_id) or self._call_records.get(call_id)
        if record is None:
            return False

        pipeline_task = getattr(record, "_pipeline_task", None)
        if pipeline_task is not None:
            try:
                await asyncio.wait_for(
                    pipeline_task.stop_when_done(),
                    timeout=_TURN_WAIT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                with suppress(asyncio.CancelledError, RuntimeError):
                    await pipeline_task.cancel()

        if record.status not in _TERMINAL_CALL_STATUSES:
            record.status = CallStatus.COMPLETED
        self._active_calls.pop(call_id, None)
        self._call_tasks.pop(call_id, None)
        return True


class _LoopbackFunctionCallParams:
    def __init__(
        self,
        *,
        function_name: str,
        arguments: dict[str, Any],
        llm: Any,
        assistant_text: str = "",
    ) -> None:
        self.function_name = function_name
        self.arguments = arguments
        self.llm = llm
        self.assistant_text = assistant_text
        self.context = None
        self.result: Any = None
        self.result_properties: Any = None
        # Pipecat parity: once the pipeline times the function call out, the
        # call is "not running" and any LATE result_callback is discarded
        # (production log: "FunctionCallResultFrame ... is not running").
        self.timed_out = False
        self._late_result_sink: list[dict[str, Any]] | None = None

    def mark_timed_out(self, late_result_sink: list[dict[str, Any]]) -> None:
        self.timed_out = True
        self._late_result_sink = late_result_sink

    async def result_callback(self, result: Any, *, properties: Any | None = None) -> None:
        if self.timed_out:
            # Mirror pipecat LLMService: the function call already timed out,
            # so this late result is dropped — it never reaches the context
            # aggregator and never triggers an LLM re-run.
            if self._late_result_sink is not None:
                self._late_result_sink.append(
                    {
                        "function_name": self.function_name,
                        "arguments": dict(self.arguments or {}),
                        "result": result,
                    }
                )
            return
        self.result = result
        self.result_properties = properties


class ScriptedLoopbackLLM:
    """No-network Pipecat LLM service used by integration tests.

    The class mimics the small subset of ``OpenAILLMService`` that the phone
    pipeline uses: it is a FrameProcessor, accepts registered function
    handlers, consumes ``LLMContextFrame``, emits LLM text frames, and invokes
    the registered tool handlers with Pipecat-like params.
    """

    def __init__(
        self,
        turns: list[ScriptedAssistantTurn | dict[str, Any]],
        *,
        honor_run_llm: bool = False,
        function_call_timeout_secs: float = 10.0,
    ) -> None:
        from pipecat.processors.frame_processor import FrameProcessor

        owner = self

        class _Processor(FrameProcessor):
            async def process_frame(self, frame: Any, direction: Any) -> None:
                await super().process_frame(frame, direction)
                await owner._handle_frame(frame, direction)

        self._processor = _Processor(name="scripted_loopback_llm")
        self._turns = [_coerce_scripted_turn(turn) for turn in turns]
        self._index = 0
        self._functions: dict[str, Any] = {}
        self._function_timeouts: dict[str, float | None] = {}
        self._event_handlers: dict[str, list[Any]] = {}
        self.invocations: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        # Pipecat parity: production LLMService enforces a per-function-call
        # timeout (constructor default 10.0s, per-registration timeout_secs
        # override). When it fires, a None result is delivered and the
        # handler's late real result is DISCARDED with no LLM re-run — the
        # exact mechanism that dead-aired capstone call 67105503. The loopback
        # mirrors it so registration-vs-internal-wait ordering bugs are
        # reproducible offline instead of physically impossible.
        self._function_call_timeout_secs = float(function_call_timeout_secs)
        self.discarded_tool_results: list[dict[str, Any]] = []
        # Mirror the production Responses service: a function result whose
        # FunctionCallResultProperties carries run_llm=True triggers a fresh LLM
        # run (production re-runs after e.g. the PHONE-15 closing_speech_missing
        # refusal, producing the recovery goodbye — real trace 18080c61). When
        # enabled, the scripted service consumes its NEXT scripted turn on such a
        # result instead of waiting for recipient speech. Opt-in so existing
        # one-turn-per-trigger scripts keep their semantics.
        self._honor_run_llm = honor_run_llm

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)

    def register_function(
        self,
        function_name: str,
        handler: Any,
        *,
        timeout_secs: float | None = None,
        **_kwargs: Any,
    ) -> None:
        self._functions[function_name] = handler
        self._function_timeouts[function_name] = timeout_secs

    def event_handler(self, event_name: str) -> Any:
        def _decorator(handler: Any) -> Any:
            self._event_handlers.setdefault(event_name, []).append(handler)
            return handler

        return _decorator

    async def process_frame(self, frame: Any, direction: Any) -> None:
        await self._processor.process_frame(frame, direction)

    async def run_inference(
        self,
        context: Any = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> str:
        """One-shot, out-of-band inference mirroring ``OpenAIResponsesLLMService.run_inference``.

        The phone voicemail one-shot classifier
        (``telephony.voicemail_detection.PipecatVoicemailDetectionHandler.classify_opening``)
        calls ``run_inference`` on the *classifier* LLM rather than driving it as
        a pipeline processor. The scripted classifier returns its next scripted
        turn's text so a forced VOICEMAIL / CONVERSATION verdict exercises the
        real detection path instead of silently failing safe to human.
        """

        del max_tokens, system_instruction
        turn = self._next_turn()
        self.invocations.append(
            {
                "messages": list(getattr(context, "messages", []) or []),
                "turn": turn,
                "inference": True,
            }
        )
        return turn.text

    async def _call_event_handlers(self, event_name: str, *args: Any) -> None:
        for handler in list(self._event_handlers.get(event_name, [])):
            result = handler(self, *args)
            if asyncio.iscoroutine(result):
                await result

    async def _handle_frame(self, frame: Any, direction: Any) -> None:
        from pipecat.frames.frames import (
            LLMContextFrame,
            LLMMessagesAppendFrame,
        )

        if isinstance(frame, LLMMessagesAppendFrame):
            if not frame.run_llm:
                await self._processor.push_frame(frame, direction)
                return
            await self._emit_turn(
                messages=list(frame.messages or []),
                direction=direction,
                context=None,
            )
            return

        if not isinstance(frame, LLMContextFrame):
            await self._processor.push_frame(frame, direction)
            return

        await self._emit_turn(
            messages=list(getattr(frame.context, "messages", []) or []),
            direction=direction,
            context=frame.context,
        )

    async def _emit_turn(self, *, messages: list[dict[str, Any]], direction: Any, context: Any) -> None:
        from pipecat.frames.frames import (
            FunctionCallFromLLM,
            FunctionCallsStartedFrame,
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
        )

        turn = self._next_turn()
        self.invocations.append(
            {
                "messages": messages,
                "turn": turn,
            }
        )
        await self._processor.push_frame(LLMFullResponseStartFrame(), direction)
        await self._emit_text(turn.text, direction)
        function_calls = [
            FunctionCallFromLLM(
                function_name=str(tool_call.get("name") or tool_call.get("function", {}).get("name") or "").strip(),
                tool_call_id="scripted-loopback-%d-%d" % (self._index, index),
                arguments=_tool_call_arguments(tool_call),
                context=context,
            )
            for index, tool_call in enumerate(turn.tool_calls, start=1)
            if str(tool_call.get("name") or tool_call.get("function", {}).get("name") or "").strip()
        ]
        if function_calls:
            await self._call_event_handlers("on_function_calls_started", function_calls)
            await self._processor.push_frame(FunctionCallsStartedFrame(function_calls), direction)
        rerun_requested = False
        for tool_call in turn.tool_calls:
            requested = await self._invoke_tool(tool_call, assistant_text=turn.text)
            rerun_requested = rerun_requested or requested
        await self._processor.push_frame(LLMFullResponseEndFrame(), direction)
        # Production parity (opt-in): a run_llm=True function result re-runs the
        # LLM after the current response completes. Only while unconsumed
        # scripted turns remain, so an exhausted script can never loop forever.
        if self._honor_run_llm and rerun_requested and self._index < len(self._turns):
            await self._emit_turn(messages=messages, direction=direction, context=context)

    async def cleanup(self) -> None:
        await self._processor.cleanup()

    async def setup(self, task: Any) -> None:
        await self._processor.setup(task)

    async def start(self, frame: Any) -> None:
        await self._processor.start(frame)

    async def stop(self, frame: Any) -> None:
        await self._processor.stop(frame)

    async def cancel(self, frame: Any) -> None:
        await self._processor.cancel(frame)

    async def _emit_text(self, text: str, direction: Any) -> None:
        from pipecat.frames.frames import LLMTextFrame

        if not text:
            return
        midpoint = max(1, len(text) // 2)
        chunks = [text[:midpoint], text[midpoint:]]
        for chunk in chunks:
            if chunk:
                await self._processor.push_frame(LLMTextFrame(chunk), direction)

    async def _invoke_tool(self, tool_call: dict[str, Any], *, assistant_text: str = "") -> bool:
        """Invoke one registered tool handler. Returns True iff the handler's
        result properties requested an LLM re-run (FunctionCallResultProperties
        run_llm=True), mirroring the production Responses service contract.

        Pipecat-parity timeout semantics (LLMService._run_function_call): the
        per-registration ``timeout_secs`` (or the service default when the
        registration omitted it) bounds the wait for the handler's
        result_callback. On timeout the pipeline delivers a None result, the
        handler keeps running to completion, and its LATE result_callback is
        discarded — never delivered, never re-running the LLM. This is the
        production discard mechanism that dead-aired capstone call 67105503.

        Implementation note: the handler is awaited INLINE (same task) — the
        existing loopback tests are load-bearing on the deterministic frame
        ordering that gives (a detached create_task adds event-loop yields
        that reorder frames around the voicemail delay gate). The timeout is a
        loop.call_later watchdog, exactly like pipecat's timeout task: it
        fires while the handler is still awaiting, records the None result,
        and flips the params to discard-late mode; the handler then finishes
        on its own time with its outcome stranded.
        """
        name = str(tool_call.get("name") or tool_call.get("function", {}).get("name") or "").strip()
        if not name:
            return False
        arguments = _tool_call_arguments(tool_call)
        handler = self._functions.get(name)
        params = _LoopbackFunctionCallParams(
            function_name=name,
            arguments=arguments,
            llm=self,
            assistant_text=assistant_text,
        )
        if handler is None:
            await params.result_callback({"ok": False, "error": "unregistered tool: %s" % name})
            self.tool_results.append({"name": name, "arguments": arguments, "result": params.result})
            return bool(getattr(params.result_properties, "run_llm", False))

        effective_timeout = self._function_timeouts.get(name)
        if effective_timeout is None:
            effective_timeout = self._function_call_timeout_secs

        def _watchdog_fired() -> None:
            logger.warning(
                "Loopback function call [%s] timed out after %s seconds; late result will be discarded",
                name,
                effective_timeout,
            )
            params.mark_timed_out(self.discarded_tool_results)
            self.tool_results.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "result": None,
                    "timed_out": True,
                    "timeout_secs": effective_timeout,
                }
            )

        watchdog = asyncio.get_running_loop().call_later(effective_timeout, _watchdog_fired)
        try:
            await handler(params)
        finally:
            watchdog.cancel()
        if params.timed_out:
            # The pipeline already delivered the None result; the handler's
            # own late outcome was recorded to discarded_tool_results by
            # result_callback and never triggers an LLM re-run.
            return False
        self.tool_results.append({"name": name, "arguments": arguments, "result": params.result})
        return bool(getattr(params.result_properties, "run_llm", False))

    def _next_turn(self) -> ScriptedAssistantTurn:
        if not self._turns:
            return ScriptedAssistantTurn(text="Simulated assistant response.")
        turn = self._turns[min(self._index, len(self._turns) - 1)]
        self._index += 1
        return turn

    @classmethod
    def from_scenario(cls, scenario: PhoneScenario) -> ScriptedLoopbackLLM:
        events = scenario.events or [PhoneEvent(type="utterance", text=text) for text in scenario.turns]
        turns: list[ScriptedAssistantTurn] = [ScriptedAssistantTurn(text=_scripted_opening_text(scenario))]
        for index, event in enumerate(events, start=1):
            tool_calls = [_normalize_expected_tool_call(call) for call in event.expected_tool_calls]
            turns.append(
                ScriptedAssistantTurn(
                    text=_scripted_assistant_text(event, index, tool_calls),
                    tool_calls=tool_calls,
                )
            )
        return cls(turns)


class LoopbackPhoneCallSession:
    """In-process phone call harness using the production Pipecat components."""

    def __init__(
        self,
        *,
        scenario: PhoneScenario,
        llm_provider: Any | None = None,
        voicemail_classifier_provider: Any | None = None,
        stt_mode: str = "text_inject",
        tts_mode: str = "capture",
        receptionist_mode: str = "text_inject",
        include_handlers: bool = True,
        event_bus: EventBus | None = None,
        tool_overrides: dict[str, ToolOverride | dict[str, Any]] | None = None,
        record_phone_calls: bool = False,
        config: TelnyxConfig | None = None,
        task_trace_writer: Any | None = None,
        receptionist_control_dir: Path | str | None = None,
    ) -> None:
        self.scenario = scenario
        self.llm_provider = llm_provider
        self.voicemail_classifier_provider = voicemail_classifier_provider
        self.stt_mode = stt_mode
        self.tts_mode = tts_mode
        self.receptionist_mode = receptionist_mode
        self.receptionist_control_dir = Path(receptionist_control_dir) if receptionist_control_dir is not None else None
        self.include_handlers = include_handlers
        self.event_bus = event_bus or get_event_bus()
        self.tool_overrides = dict(tool_overrides or {})
        self.record_phone_calls = record_phone_calls
        self.config = config or _default_loopback_config(record_phone_calls=record_phone_calls)
        self.call_manager = LoopbackCallManager(self.config)
        self.bridge = AudioBridge()
        self.transport = None
        self.recipient_transport = None
        self.record: CallRecord | None = None
        self.assistant_turns: list[dict[str, Any]] = []
        self.oracle_events: list[dict[str, Any]] = []
        self.initial_greeting_text = ""
        self.first_bot_speech_latency_seconds: float | None = None
        self.transcript_events: list[CallTranscriptDelta] = []
        self.lifecycle_events: list[BaseEvent] = []
        self.tool_calls: list[dict[str, Any]] = []
        self._recorded_audio = bytearray()
        self._subscriptions: list[Subscription] = []
        self._pipeline_task: Any | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._opening_settle_task: asyncio.Task[str] | None = None
        self._text_injector: _TextInjectionProcessor | None = None
        self._receptionist_bot: _TTSBotReceptionist | None = None
        self._turn_condition = asyncio.Condition()
        self._turn_cursor = 0
        self._pending_turn_tool_calls: list[dict[str, Any]] = []
        self._hold_handler: Any | None = None
        self._voicemail_handler: Any | None = None
        self._recording_storage_paths: list[str] = []
        self._task_trace = task_trace_writer
        self._task_trace_owned = task_trace_writer is None  # we'll construct one if not provided
        self._trace_frame_chunk_index = 0
        self._phone_latency_trace: PhoneLatencyTraceRecorder | None = None
        # Production self-echo guard state (capstone 49c7106f), built via the shared
        # phone_self_echo_guard.build_phone_user_turn_strategies in _context_aggregators so
        # the rig runs the SAME turn-start guard as the cloud pipeline. The reference is fed
        # by the create_bot_speech_echo_tap wired after TTS; the floor is driven by the
        # attach_generation_floor_handlers wired on the user aggregator.
        self._bot_speech_echo_reference: Any | None = None
        self._bot_turn_floor_state: Any | None = None
        self._payment_gate_fired = False
        self._last_payment_gate_ctx: dict[str, Any] | None = None
        self._receptionist_runner: Any | None = None
        self._receptionist_pipeline_task: Any | None = None
        self._receptionist_runner_task: asyncio.Task[None] | None = None
        self._receptionist_speech_injector: Any | None = None
        self._previous_event_bus: EventBus | None = None
        self._scenario_now = _parse_scenario_current_time(self.scenario.current_time)
        # PHONE-15 path-drop recovery: records each time a latched (text-empty)
        # end_call was completed by a subsequent spoken turn. Production wires the
        # identical fire in CallManager._run_call_pipeline via
        # TranscriptFrameCollector(on_assistant_complete=...); the loopback wires it
        # too so the fix is exercised end-to-end offline. Each entry:
        # {"text": <spoken closing line>, "fired": <bool from fire_latched...>}.
        self.latched_end_call_fires: list[dict[str, Any]] = []
        # One fake Telnyx client shared by the end_call tool handler AND the
        # media-drain hangup processor (mirrors production, where both hold the
        # same telnyx.AsyncTelnyx). Every dispatched hangup is recorded here so
        # tests can assert the terminal effect is single-shot.
        self._fake_telnyx = _FakeTelnyxClient(self)
        self.telnyx_hangups: list[dict[str, Any]] = []

    @property
    def task_trace_path(self):
        """Path to the trace v2 JSONL file if a TaskTraceWriter is active."""
        return getattr(self._task_trace, "path", None) if self._task_trace is not None else None

    @property
    def phone_latency_trace_path(self) -> Path | None:
        """Path to the plain phone latency trace consumed by phone_live_grader."""
        return self._phone_latency_trace.path if self._phone_latency_trace is not None else None

    @property
    def recorded_audio(self) -> bytes:
        return bytes(self._recorded_audio)

    @property
    def recording_storage_paths(self) -> list[str]:
        return list(self._recording_storage_paths)

    def _dispose_event_subscriptions(self) -> None:
        while self._subscriptions:
            subscription = self._subscriptions.pop()
            with suppress(Exception):
                subscription.dispose()

    def _restore_event_bus(self) -> None:
        previous = self._previous_event_bus
        if previous is not None:
            set_event_bus(previous)
            self._previous_event_bus = None

    def _append_trace_frame_chunk(self, chunk_kind: str, delta: dict[str, Any]) -> None:
        if self._task_trace is None:
            return
        self._trace_frame_chunk_index += 1
        with suppress(Exception):
            self._task_trace.append_llm_stream_chunk(
                ts=datetime.now(tz=UTC).isoformat(),
                attempt_id="%s:frames" % self._task_trace.task_id,
                chunk_index=self._trace_frame_chunk_index,
                chunk_kind=chunk_kind,
                delta=delta,
            )

    def _append_trace_start(self, *, system_prompt: str) -> None:
        if self._task_trace is None or self.record is None:
            return
        append_start = getattr(self._task_trace, "append_start", None)
        if not callable(append_start):
            return
        with suppress(Exception):
            append_start(
                started_at=(
                    self.record.started_at.isoformat() if self.record.started_at else datetime.now(tz=UTC).isoformat()
                ),
                request={
                    "scenario": self.scenario.name,
                    "task": self.scenario.task,
                    "caller_name": self.scenario.caller_name,
                    "phone_number": self.scenario.phone_number,
                    "mode": self.scenario.mode,
                    "system_prompt": system_prompt,
                },
                runtime={
                    "harness": "loopback_phone_call_session",
                    "stt_mode": self.stt_mode,
                    "tts_mode": self.tts_mode,
                    "receptionist_mode": self.receptionist_mode,
                    "llm_provider_class": (
                        type(self.llm_provider).__name__ if self.llm_provider is not None else "production"
                    ),
                    "llm_model": self.config.llm_model,
                    "record_phone_calls": self.record_phone_calls,
                },
                continuity={},
            )

    def _append_phone_trace_event(self, kind: str, **payload: Any) -> None:
        if self._task_trace is None:
            return
        append_event = getattr(self._task_trace, "append_event", None)
        if not callable(append_event):
            return
        record = self.record
        event_payload = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "kind": kind,
            "scenario": self.scenario.name,
            "call_id": record.call_id if record is not None else "",
        }
        event_payload.update(payload)
        with suppress(Exception):
            append_event("trace_phone_oracle_event", event_payload)

    def _append_trace_tool_execution(
        self,
        *,
        tool_name: str,
        ok: bool,
        result: Any = None,
        error: Any = None,
    ) -> None:
        if self._task_trace is None:
            return
        append_tool_execution = getattr(self._task_trace, "append_tool_execution", None)
        if not callable(append_tool_execution):
            return
        with suppress(Exception):
            append_tool_execution(
                ts=datetime.now(tz=UTC).isoformat(),
                step=None,
                tool_name=tool_name,
                ok=ok,
                duration_ms=None,
                result=result,
                error=error,
            )

    def _append_trace_complete(self) -> None:
        if self._task_trace is None or self.record is None:
            return
        append_complete = getattr(self._task_trace, "append_complete", None)
        if not callable(append_complete):
            return
        final_answer = ""
        if self.assistant_turns:
            final_answer = str(self.assistant_turns[-1].get("text") or "")
        with suppress(Exception):
            append_complete(
                completed_at=datetime.now(tz=UTC).isoformat(),
                outcome=self.record.status.value,
                final_answer=final_answer,
                total_steps=len(self.assistant_turns),
                total_duration_s=float(self.record.duration_seconds or 0.0),
                final_context_usage_pct=0.0,
                continuity={},
                gate_state={
                    "recording_enabled": self.record.recording_enabled,
                    "disclosure_spoken": self.record.disclosure_spoken,
                    "recording_started_after_disclosure": self.record.recording_started_after_disclosure,
                    "recording_paths": dict(self.record.recording_paths),
                },
            )

    async def start(self) -> None:
        if self._pipeline_task is not None:
            raise RuntimeError("LoopbackPhoneCallSession is already started")
        if self.stt_mode not in _VALID_STT_MODES:
            raise NotImplementedError("Unsupported stt_mode=%r" % self.stt_mode)
        if self.tts_mode not in _VALID_TTS_MODES:
            raise NotImplementedError("Unsupported tts_mode=%r" % self.tts_mode)
        if self.receptionist_mode not in _VALID_RECEPTIONIST_MODES:
            raise NotImplementedError("Unsupported receptionist_mode=%r" % self.receptionist_mode)

        # Swap the process-wide bus to ours so production code paths that publish
        # via core.events.bus.get_event_bus() (e.g. TranscriptFrameCollector from
        # PR #225) are observable by this session's subscriptions. Restored on stop().
        self._previous_event_bus = set_event_bus(self.event_bus)

        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineParams, PipelineTask

        call_id = "loopback-%s" % uuid.uuid4().hex[:8]
        record = CallRecord(
            call_id=call_id,
            phone_number=self.scenario.phone_number,
            task=self.scenario.task,
            caller_name=self.scenario.caller_name,
            user_id=self.scenario.user_id or _DEFAULT_USER_ID,
            info_manifest=self.scenario.info_manifest or {"have": [], "dont_have": []},
            issuer_channel=_ScriptedIssuerChannel(self.scenario.consult_responses),
            issuer_channel_info={"channel_type": "loopback", "scripted": True},
        )
        record.recording_enabled = bool(self.record_phone_calls)
        record.status = CallStatus.ACTIVE
        record.started_at = datetime.now(tz=UTC)
        self.record = record
        self.call_manager._active_calls[record.call_id] = record
        self.call_manager._call_records[record.call_id] = record
        self._phone_latency_trace = PhoneLatencyTraceRecorder(
            PhoneLatencyTraceWriter.for_call(
                call_id=record.call_id,
                user_id=record.user_id,
                started_at=record.started_at,
            ),
            source="loopback_phone_call_session",
        )
        record._phone_latency_trace = self._phone_latency_trace
        self._phone_latency_trace.start(
            runtime="loopback_phone_call_session",
            stt_mode=self.stt_mode,
            tts_mode=self.tts_mode,
            receptionist_mode=self.receptionist_mode,
            llm_model=self.config.llm_model,
        )

        self._subscribe_to_events(record.call_id)
        self._publish_lifecycle("active")

        self.transport = self.bridge.get_transport_a(name="viola-loopback")
        self.recipient_transport = self.bridge.get_transport_b(name="recipient-script")
        if self.stt_mode == "real_whisper":
            self._configure_loopback_input_vad(self.transport)

        transcript_collector = TranscriptCollector(record)
        system_prompt = build_phone_system_instruction(
            caller_name=self.scenario.caller_name,
            task=self.scenario.task,
            extra_context=self.scenario.extra_context,
            recording_disclosure=self.scenario.recording_disclosure,
            ai_disclosure=self.scenario.ai_disclosure,
            info_manifest=self.scenario.info_manifest,
            mode=self.scenario.mode,
            session_id="phone:%s" % record.call_id,
            include_volatile_context=False,
        )
        phone_tool_surface = build_phone_llm_tool_surface(list(self.scenario.mcp_tools or []))
        # Construct a TaskTraceWriter for this call so the LLM service can emit
        # trace v2 events (every chat-completion request + stream chunks). Same
        # pattern as intent/agent_executor.py:3973.
        if self._task_trace is None and self._task_trace_owned:
            try:
                from intent.task_trace import TaskTraceWriter

                self._task_trace = TaskTraceWriter.for_task(
                    user_id=record.user_id,
                    task_id=record.call_id,
                    started_at=(record.started_at.isoformat() if record.started_at else None),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "LoopbackPhoneCallSession: failed to construct TaskTraceWriter: %s",
                    exc,
                )
                self._task_trace = None
        self._append_trace_start(system_prompt=system_prompt)
        llm = self._build_llm(system_prompt, phone_tool_surface.tools_schema)

        inbound_tee = AudioTeeProcessor("inbound")
        outbound_tee = AudioTeeProcessor("outbound")
        capture_listener = _AudioCaptureListener(self)
        await outbound_tee.add_listener(capture_listener)
        record._inbound_tee = inbound_tee
        record._outbound_tee = outbound_tee
        user_aggregator, assistant_aggregator = self._context_aggregators(phone_tool_surface.tools_schema, record)
        # Drive the generation-window floor from Pipecat's own turn lifecycle, exactly as
        # production does (call_manager.py on_user_turn_stopped/started handlers): recipient
        # yields -> floor set (min_words gate covers generation), new user turn -> floor cleared.
        from telephony.phone_self_echo_guard import (
            attach_generation_floor_handlers,
            create_bot_speech_echo_tap,
        )

        attach_generation_floor_handlers(user_aggregator, self._bot_turn_floor_state)
        bot_speech_echo_tap = create_bot_speech_echo_tap(self._bot_speech_echo_reference)
        stt_processor = self._build_stt_processor()
        tts_processor = self._build_tts_processor()
        if self.llm_provider is None:
            from pipecat.processors.aggregators.llm_context import LLMContext

            from telephony.call_manager import _PHONE_RING_LLM_WARMUP_TIMEOUT_SECS

            warmup_started = time.perf_counter()
            await self.call_manager._prewarm_call_runtime_during_ring(
                record.call_id,
                stt=stt_processor,
                llm=llm,
                tts=tts_processor,
                llm_context=LLMContext(tools=phone_tool_surface.tools_schema),
            )
            self._append_phone_trace_event(
                "ring_warmup_complete",
                elapsed_ms=round((time.perf_counter() - warmup_started) * 1000.0, 3),
                llm_timeout_seconds=_PHONE_RING_LLM_WARMUP_TIMEOUT_SECS,
                tts_provider=self.config.tts_provider,
            )
        self._hold_handler, language_handler, self._voicemail_handler = self._build_handlers(llm, record, tts_processor)
        self._register_phone_functions(llm, record, phone_tool_surface.mcp_name_by_llm_name)
        if self._voicemail_handler is not None:
            self._voicemail_handler.set_context_frame_target(user_aggregator)
            record._voicemail_handler = self._voicemail_handler
        answer_settle_observer = AnswerSettleObserver()

        pipeline_processors: list[Any] = []
        # answer_settle_observer sits DOWNSTREAM of stt_processor to mirror production
        # call_manager.py exactly (both sides moved together on 2026-08-06, #4796,
        # closing the #2587 defect) — a rig on the other side of STT would resolve
        # answer-settle differently from the real call and stop being a usable oracle
        # for opening latency or voicemail. The rationale and the measured cost of the
        # old side are documented at the production wiring in telephony/call_manager.py;
        # both sides always move together.
        if self.stt_mode == "real_whisper":
            pipeline_processors.extend(
                [
                    self.transport.input(),
                    _started_tee_processor(inbound_tee),
                    stt_processor,
                    answer_settle_observer,
                    PhoneLatencyTraceProcessor(self._phone_latency_trace, stage="inbound"),
                ]
            )
            self._text_injector = _TextInjectionProcessor() if self.receptionist_mode == "text_inject" else None
            if self._text_injector is not None:
                pipeline_processors.append(self._text_injector)
        else:
            self._text_injector = _TextInjectionProcessor()
            pipeline_processors.extend(
                [
                    self._text_injector,
                    _started_tee_processor(inbound_tee),
                    stt_processor,
                    answer_settle_observer,
                    PhoneLatencyTraceProcessor(self._phone_latency_trace, stage="inbound"),
                ]
            )

        pipeline_processors.extend(
            [
                _TraceFrameCapture(self, name="loopback_trace_inbound_frames"),
                self._transcription_observer(self._hold_handler, language_handler),
                self._transcript_collector(transcript_collector, capture_assistant=False),
                # One-shot voicemail (2026-06-27): no persistent classifier branch; the
                # opening greeting is classified once at answer_settle (shared
                # queue_outbound_opening_after_answer_settle -> classify_opening).
                user_aggregator,
                (
                    self._voicemail_handler.response_gate()
                    if self._voicemail_handler is not None
                    else _PassthroughProcessor("voicemail_response_gate_disabled")
                ),
                # PHONE-LATENCY: pre-LLM tap (mirrors production call_manager.py) so
                # the STT-done -> LLM-request window decomposes into
                # post_transcript_to_context_ms here vs
                # post_transcript_to_llm_dispatch_ms at _process_context entry.
                PhoneLatencyTraceProcessor(self._phone_latency_trace, stage="llm_inbound"),
                llm,
                PhoneLatencyTraceProcessor(self._phone_latency_trace, stage="llm"),
                self._cost_metrics_collector(record),
                _AssistantTurnCapture(self),
                self._transcript_collector(
                    transcript_collector,
                    capture_user=False,
                    on_assistant_complete=self._make_loopback_assistant_complete(record, llm),
                ),
                tts_processor,
                # Self-echo tap: feed Viola's live TTS text into the echo reference so the
                # guarded turn-start strategy upstream recognises her own words coming back
                # and does NOT treat them as a barge-in — identical placement to production
                # (call_manager.py: tap immediately after tts). Populated only when the TTS
                # service emits TTSTextFrame (push_text_frames=True); see start-strategy note.
                bot_speech_echo_tap,
                # (one-shot voicemail: persistent TTS gate removed; opening is classified
                # before Viola speaks, so no TTS buffering is needed)
                PhoneLatencyTraceProcessor(self._phone_latency_trace, stage="outbound"),
                _TraceFrameCapture(self, name="loopback_trace_outbound_frames"),
                _started_tee_processor(outbound_tee),
                self.transport.output(),
                # Mirror production (call_manager.py: end_call_hangup sits right
                # after transport.output()): the REAL drain processor consumes
                # EndCallHangupFrame, awaits the transport's output-mark drain
                # (tests may stub transport.wait_for_output_mark — the loopback
                # transport has none, so the processor hangs up fail-closed),
                # then dispatches the Telnyx hangup.
                _make_end_call_hangup_processor(self, record),
                assistant_aggregator,
            ]
        )
        pipeline = Pipeline(pipeline_processors)
        self._pipeline_task = PipelineTask(
            pipeline,
            # Mirror production telephony rates EXACTLY (call_manager.py:5051-5052):
            # inbound is the native 8 kHz PHONE_INBOUND_SAMPLE_RATE, output 16 kHz.
            # PipelineParams defaults audio_in_sample_rate to 16000 (pipecat
            # task.py), which would make the StartFrame lie to Viola's STT — it
            # would skip the 8k->16k upsample in phone_pcm_to_whisper_float and
            # garble the recipient (the exact prod bug, 2026-06-24). Pinning the
            # inbound rate here is what makes the rig exercise the real resample
            # path the carrier exercises, so the garble class is reproducible
            # offline instead of physically impossible.
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                audio_in_sample_rate=PHONE_INBOUND_SAMPLE_RATE,
                audio_out_sample_rate=_PHONE_AUDIO_OUT_SAMPLE_RATE,
            ),
            check_dangling_tasks=False,
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
        )
        record._pipeline_task = self._pipeline_task

        # Post-``start()`` contract: the pipeline's ``StartFrame`` has propagated
        # to every processor before this method returns. Without this wait,
        # frames pushed directly into the pipeline immediately after ``start()``
        # (e.g. a Telnyx AMD voicemail context, which a test fires the instant the
        # call is up) race the StartFrame and are dropped by aggregators that
        # raise "StartFrame not received yet" — so the voicemail message turn
        # never fires. ``on_pipeline_started`` fires when StartFrame reaches the
        # pipeline sink (pipecat task.py), i.e. the pipeline is ready.
        pipeline_started = asyncio.Event()

        @self._pipeline_task.event_handler("on_pipeline_started")
        async def _on_pipeline_started(_task: Any, _frame: Any) -> None:
            pipeline_started.set()

        runner = PipelineRunner(handle_sigint=False)
        self._runner_task = asyncio.create_task(
            runner.run(self._pipeline_task),
            context=contextvars.copy_context(),
        )
        self.call_manager._call_tasks[record.call_id] = self._runner_task
        await asyncio.sleep(0)
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(pipeline_started.wait(), timeout=_TURN_WAIT_TIMEOUT_SECONDS)
        if self.receptionist_mode == "tts_bot":
            self._receptionist_bot = _TTSBotReceptionist(self, self.scenario)
            await outbound_tee.add_listener(self._receptionist_bot)
            await self._receptionist_bot.start()
        elif self.receptionist_mode == "file_watcher_bot":
            await self._start_file_watcher_receptionist()
        self._opening_settle_task = asyncio.create_task(
            queue_outbound_opening_after_answer_settle(
                record,
                self._pipeline_task,
                answer_settle_observer,
                silent_answer_fallback_seconds=(
                    _TTS_RECEPTIONIST_ANSWER_OBSERVATION_SECONDS if self.receptionist_mode == "tts_bot" else 0.2
                ),
            ),
            name="loopback-opening-settle-%s" % record.call_id,
            context=contextvars.copy_context(),
        )

    async def stop(self) -> None:
        if self.record is None:
            # Even when start() never produced a record, restore any bus we swapped.
            self._dispose_event_subscriptions()
            self._restore_event_bus()
            return
        try:
            if self._receptionist_bot is not None:
                await self._receptionist_bot.stop()
                self._receptionist_bot = None
            if self._receptionist_speech_injector is not None:
                with suppress(Exception):
                    await self._receptionist_speech_injector.stop_watching()
                self._receptionist_speech_injector = None
            if self._receptionist_pipeline_task is not None:
                with suppress(Exception):
                    await self._receptionist_pipeline_task.stop_when_done()
            if self._receptionist_runner_task is not None:
                with suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(
                        self._receptionist_runner_task,
                        timeout=_TURN_WAIT_TIMEOUT_SECONDS,
                    )
                self._receptionist_runner_task = None
            if self._opening_settle_task is not None:
                if not self._opening_settle_task.done():
                    self._opening_settle_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._opening_settle_task
                self._opening_settle_task = None
            await self.call_manager.end_call(self.record.call_id, user_id=self.record.user_id)
            if self._pipeline_task is not None:
                with suppress(Exception):
                    await self._pipeline_task.stop_when_done()
            if self._runner_task is not None:
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(self._runner_task, timeout=_TURN_WAIT_TIMEOUT_SECONDS)
            await asyncio.sleep(_TEE_DRAIN_SECONDS)
        except Exception:
            self._dispose_event_subscriptions()
            self._restore_event_bus()
            raise

        # Restore the process-wide bus before any post-stop assertions/cleanup.
        self._dispose_event_subscriptions()
        self._restore_event_bus()

        if self.record.status not in _TERMINAL_CALL_STATUSES:
            self.record.status = CallStatus.COMPLETED
        self.record.ended_at = datetime.now(tz=UTC)
        if self.record.started_at is not None:
            self.record.duration_seconds = (self.record.ended_at - self.record.started_at).total_seconds()
        if self._phone_latency_trace is not None:
            self._phone_latency_trace.complete(
                status=self.record.status.value,
                duration_seconds=self.record.duration_seconds,
            )

        self._mark_loopback_disclosure_if_spoken()
        if self.record.recording_enabled and self.record.disclosure_spoken and self.recorded_audio:
            path = await self.call_manager._recording_storage.save(
                self.record.call_id,
                "loopback",
                self.recorded_audio,
            )
            self._recording_storage_paths.append(path)
            if path:
                self.record.recording_paths["loopback"] = path

        self._append_phone_trace_event(
            "recording_state",
            recording_enabled=self.record.recording_enabled,
            disclosure_spoken=self.record.disclosure_spoken,
            disclosure_text_confirmed=self.record.disclosure_text_confirmed,
            recording_started_after_disclosure=self.record.recording_started_after_disclosure,
            recording_paths=dict(self.record.recording_paths),
            recorded_audio_bytes=len(self.recorded_audio),
        )
        if self._hold_handler is not None:
            with suppress(Exception):
                await self._hold_handler.cleanup()
        if self._voicemail_handler is not None and hasattr(self._voicemail_handler, "cleanup"):
            with suppress(Exception):
                await self._voicemail_handler.cleanup()
        self._append_trace_complete()
        if self._task_trace is not None:
            with suppress(Exception):
                self._task_trace.flush()
        self._publish_lifecycle("completed")
        for subscription in self._subscriptions:
            subscription.dispose()
        self._subscriptions = []

    async def simulate_recipient_speech(self, text: str, *, duration_seconds: float = 0.0) -> None:
        if self._pipeline_task is None:
            raise RuntimeError("start() must be called before simulating speech")
        if self._call_has_ended():
            return
        from pipecat.frames.frames import (
            TranscriptionFrame,
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        # NOTE: Real Whisper STT emits interim (finalized=False) partials before
        # the final. We can't faithfully reproduce that in text-injection mode —
        # the user_aggregator without real audio metadata treats partial+final
        # as TWO distinct user messages, duplicating every receptionist line in
        # the LLM context. That made the model interpret the call as a transcript
        # replay and fire end_call early. Verified via trace v2 inspection on
        # 2026-05-05 (loopback-858a7341 attempt 0004). Stick with single final.
        final_frame = TranscriptionFrame(
            text=text,
            user_id="recipient",
            timestamp=datetime.now(tz=UTC).isoformat(),
            language=_english_language(),
            finalized=True,
        )
        self._append_phone_trace_event("recipient_speech", text=text)
        self.oracle_events.append(
            {
                "kind": "recipient_speech",
                "text": text,
                "ts": datetime.now(tz=UTC).isoformat(),
            }
        )
        await self._emit_transcription_frame(UserStartedSpeakingFrame())
        if duration_seconds > 0:
            await asyncio.sleep(duration_seconds)
        await self._emit_transcription_frame(UserStoppedSpeakingFrame())
        await self._emit_transcription_frame(final_frame)
        self._append_phone_trace_event("recipient_speech_complete", text=text)

    async def simulate_media_stream_stop_after_first_frame(
        self,
        *,
        reason: str = "telnyx_stop",
        timeout: float = _TURN_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        """Simulate the carrier ending the media stream after Viola starts audio.

        LoopbackTransport has no real Telnyx WebSocket, so this helper waits for
        the first captured outbound audio frame and then drives the production
        CallManager media-disconnect watchdog with a fake disconnected
        transport.
        """
        if self.record is None or self._pipeline_task is None:
            raise RuntimeError("start() must be called before simulating a media stream stop")

        deadline = time.monotonic() + timeout
        while not self._recorded_audio:
            if self._call_has_ended():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for first outbound loopback audio frame")
            await asyncio.sleep(0.01)

        await self.call_manager._watch_telnyx_ws_disconnect(
            self.record,
            _LoopbackStoppedMediaTransport(reason=reason),
            grace_seconds=0.0,
        )

    def _mark_loopback_disclosure_if_spoken(self) -> bool:
        if self.record is None:
            return False
        from telephony.disclosure_text import expected_disclosure_sentence

        disclosure = expected_disclosure_sentence(
            self.record.recording_enabled,
            self.record.transcript_retention_enabled,
            self.record.caller_name,
        )
        if not disclosure:
            return False

        spoken = any(event.role == "viola" and disclosure in event.text for event in self.transcript_events) or any(
            item.get("role") == "viola" and disclosure in item.get("text", "") for item in self.record.transcript
        )
        if not spoken:
            return False

        self.record.disclosure_text_confirmed = True
        self.record.disclosure_spoken = True
        if self.record.recording_enabled:
            self.record.recording_started_after_disclosure = True
        return True

    async def _emit_transcription_frame(self, frame: Any) -> None:
        if self._call_has_ended():
            return
        if self._pipeline_task is None:
            raise RuntimeError("start() must be called before simulating speech")
        if self._text_injector is None:
            await self._pipeline_task.queue_frame(frame)
            return
        await self._text_injector.emit_frame(frame)

    def _call_has_ended(self) -> bool:
        if self.record is not None and self.record.status in _TERMINAL_CALL_STATUSES:
            return True
        return self._runner_task is not None and self._runner_task.done()

    async def simulate_dtmf_received(self, digit: str) -> None:
        if self._pipeline_task is None:
            raise RuntimeError("start() must be called before simulating DTMF")
        from pipecat.audio.dtmf.types import KeypadEntry
        from pipecat.frames.frames import InputDTMFFrame

        frame = InputDTMFFrame(button=KeypadEntry(str(digit)))
        self._append_phone_trace_event("recipient_dtmf", digit=str(digit))
        if self._text_injector is not None:
            await self._text_injector.emit_frame(frame)
        else:
            await self._pipeline_task.queue_frame(frame)

    async def simulate_silence(self, seconds: float) -> None:
        self._append_phone_trace_event("recipient_silence", duration_seconds=seconds)
        await self._push_pcm_for_seconds(b"\x00" * CHUNK_BYTES, seconds)

    async def simulate_hold_music(self, seconds: float) -> None:
        self._append_phone_trace_event("hold_music", duration_seconds=seconds)
        total_chunks = max(1, int(seconds * 1000 / CHUNK_DURATION_MS))
        for chunk_index in range(total_chunks):
            await self._emit_input_audio(_sine_wave_chunk(chunk_index))
            await asyncio.sleep(CHUNK_DURATION_MS / 1000)

    async def run_scenario(self) -> None:
        events = self.scenario.events or [PhoneEvent(type="utterance", text=text) for text in self.scenario.turns]
        for event in events:
            self._append_phone_trace_event(
                "scenario_event",
                event_type=event.type,
                label=event.label,
                text=event.text,
                duration_seconds=event.duration_seconds,
                triggers_llm=event.triggers_llm,
                expected_tool_calls=list(event.expected_tool_calls),
                expected_assistant=dict(event.expected_assistant),
            )
            if self.receptionist_mode == "tts_bot":
                await self.wait_for_receptionist()
                return
            if event.type == "silence":
                await self.simulate_silence(event.duration_seconds)
                continue
            if event.type == "hold_music":
                await self.simulate_hold_music(event.duration_seconds)
                continue
            if event.text:
                await self.simulate_recipient_speech(event.text, duration_seconds=event.duration_seconds)
                if event.triggers_llm:
                    expected_tool_names = [
                        str(call.get("name") or call.get("function", {}).get("name") or "").strip()
                        for call in event.expected_tool_calls
                    ]
                    expected_tool_names = [name for name in expected_tool_names if name]
                    if expected_tool_names:
                        await self.wait_for_assistant_tools(expected_tool_names)
                    else:
                        await self.wait_for_assistant_turn()

    async def wait_for_assistant_turn(self, timeout: float = _TURN_WAIT_TIMEOUT_SECONDS) -> dict[str, Any]:
        async with self._turn_condition:
            await asyncio.wait_for(
                self._turn_condition.wait_for(lambda: len(self.assistant_turns) > self._turn_cursor),
                timeout=timeout,
            )
            turn = self.assistant_turns[self._turn_cursor]
            self._turn_cursor += 1
            return turn

    async def wait_for_assistant_tools(
        self,
        expected_tool_names: list[str],
        timeout: float = _TURN_WAIT_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        start_cursor = self._turn_cursor

        def _observed_tool_names() -> list[str]:
            names: list[str] = []
            for turn in self.assistant_turns[start_cursor:]:
                for tool_call in turn.get("tool_calls", []) or []:
                    name = str(tool_call.get("name") or "").strip()
                    if name:
                        names.append(name)
            return names

        def _has_expected_tools() -> bool:
            position = 0
            for name in _observed_tool_names():
                if position < len(expected_tool_names) and name == expected_tool_names[position]:
                    position += 1
            return position == len(expected_tool_names)

        async with self._turn_condition:
            await asyncio.wait_for(self._turn_condition.wait_for(_has_expected_tools), timeout=timeout)
            self._turn_cursor = len(self.assistant_turns)
            return list(self.assistant_turns[start_cursor : self._turn_cursor])

    async def wait_for_receptionist(self, timeout: float = _RECEPTIONIST_WAIT_TIMEOUT_SECONDS) -> None:
        if self._receptionist_bot is None:
            return
        await self._receptionist_bot.wait_finished(timeout=timeout)

    async def _push_pcm_for_seconds(self, chunk: bytes, seconds: float) -> None:
        total_chunks = max(1, int(seconds * 1000 / CHUNK_DURATION_MS))
        for _ in range(total_chunks):
            await self._emit_input_audio(chunk)
            await asyncio.sleep(CHUNK_DURATION_MS / 1000)

    async def _emit_input_audio(self, audio: bytes) -> None:
        if self._pipeline_task is None:
            raise RuntimeError("start() must be called before simulating audio")
        from pipecat.frames.frames import InputAudioRawFrame

        frame = InputAudioRawFrame(
            audio=audio,
            sample_rate=LOOPBACK_SAMPLE_RATE,
            num_channels=LOOPBACK_CHANNELS,
        )
        if self._text_injector is not None:
            await self._text_injector.emit_frame(frame)
        elif self.stt_mode == "real_whisper":
            await self._queue_recipient_audio(audio)
        else:
            await self._pipeline_task.queue_frame(frame)

    async def _queue_recipient_audio(self, audio: bytes) -> None:
        if self.transport is None:
            raise RuntimeError("start() must be called before queuing loopback audio")
        for offset in range(0, len(audio), CHUNK_BYTES):
            chunk = audio[offset : offset + CHUNK_BYTES]
            if len(chunk) < CHUNK_BYTES:
                chunk += b"\x00" * (CHUNK_BYTES - len(chunk))
            await self.bridge.b_to_a.put(chunk)

    def _build_llm(self, system_prompt: str, tools_schema: Any) -> Any:
        if self.llm_provider is not None:
            if callable(self.llm_provider) and not hasattr(self.llm_provider, "process_frame"):
                return self.llm_provider(system_prompt=system_prompt, tools_schema=tools_schema, session=self)
            return self.llm_provider

        # Codex subscription path: when ai_source=codex is set, route the LLM
        # through the user's ChatGPT subscription via codex_auth instead of an
        # api_key. Zero per-token cost; same Responses API surface.
        codex_client = None
        try:
            from ui.settings_manager import get_settings_manager

            ai_source = get_settings_manager().get("ai_source", "byok")
            if ai_source == "codex":
                from services.llm.codex_auth import (
                    create_codex_openai_client,
                    is_codex_available,
                )

                if is_codex_available():
                    codex_client = create_codex_openai_client()
                    logger.info("Loopback phone LLM using codex-routed client (ai_source=codex)")
        except Exception:
            logger.exception("codex client setup failed; falling back to api_key transport")
            codex_client = None

        # Real-LLM path: use the Responses API (not Chat Completions) so the
        # phone agent runs with reasoning_effort enabled. Chat Completions on
        # gpt-5.4-mini rejects reasoning_effort when tools are present, which
        # forces reasoning_tokens=0 (verified in trace v2 usage chunks). The
        # Responses API accepts reasoning + tools together.
        try:
            from pipecat.services.openai.responses.llm import OpenAIResponsesLLMService

            from config import defaults as _defaults
            from telephony.traced_openai_responses_llm_service import (
                TracedOpenAIResponsesLLMService,
            )

            # Mirror the production phone reasoning effort (canonical low-latency
            # DEFAULT_PHONE_REASONING_EFFORT) so the loopback oracle exercises the
            # real config — never hard-code a heavier effort here.
            settings = OpenAIResponsesLLMService.Settings(
                model=self.config.llm_model,
                system_instruction=system_prompt,
                # No reasoning.summary, matching production (PHONE-LATENCY-01). The rig
                # must send the same request shape or it stops being an oracle for
                # first-token latency.
                extra={
                    "reasoning": {
                        "effort": _defaults.resolve_reasoning_effort(
                            _defaults.DEFAULT_PHONE_REASONING_EFFORT,
                            self.config.llm_model,
                        ),
                    }
                },
            )
            service = TracedOpenAIResponsesLLMService(
                api_key=self.config.openai_api_key or "codex-subscription",  # pragma: allowlist secret
                settings=settings,
                task_trace_writer=self._task_trace,
                direct_context_commit=True,
                phone_latency_recorder=self._phone_latency_trace,
                phone_volatile_context_builder=lambda: build_phone_volatile_context(
                    call_record=self.record,
                    caller_name=(self.record.caller_name if self.record is not None else self.scenario.caller_name),
                    now=self._scenario_now,
                ),
            )
            if codex_client is not None:
                # Overwrite the api_key-built AsyncOpenAI client with the codex
                # transport-rewritten one. Pipecat reads from self._client.
                service._client = codex_client
            service._client = _wrap_openai_client_for_phone_spend_accounting(
                service._client,
                user_id=(self.record.user_id if self.record is not None else _DEFAULT_USER_ID),
            )
            return service
        except ModuleNotFoundError:
            logger.warning(
                "Pipecat Responses LLM service is unavailable; using traced OpenAILLMService fallback for loopback"
            )
            from pipecat.services.openai.llm import OpenAILLMService

            from telephony.traced_openai_llm_service import TracedOpenAILLMService

            service = TracedOpenAILLMService(
                api_key=self.config.openai_api_key,
                settings=OpenAILLMService.Settings(
                    model=self.config.llm_model,
                    system_instruction=system_prompt,
                ),
                task_trace_writer=self._task_trace,
            )
            service._client = _wrap_openai_client_for_phone_spend_accounting(
                service._client,
                user_id=(self.record.user_id if self.record is not None else _DEFAULT_USER_ID),
            )
            return service

    def _build_voicemail_classifier_llm(self) -> Any:
        if self.voicemail_classifier_provider is not None:
            if callable(self.voicemail_classifier_provider) and not hasattr(
                self.voicemail_classifier_provider,
                "process_frame",
            ):
                return self.voicemail_classifier_provider(session=self)
            return self.voicemail_classifier_provider

        if self.llm_provider is not None:
            return ScriptedLoopbackLLM([{"text": "CONVERSATION"}])

        user_id = self.record.user_id if self.record is not None else _DEFAULT_USER_ID
        return _create_phone_voicemail_classifier_llm(
            self.config,
            openai_client=_maybe_build_phone_codex_client(self.config),
            user_id=user_id,
            task_trace_writer=self._task_trace,
        )

    def _build_stt_processor(self) -> Any:
        if self.stt_mode == "text_inject":
            return _PassthroughProcessor("text_inject_stt")
        return self.call_manager._create_stt()

    def _build_tts_processor(self) -> Any:
        if self.tts_mode == "capture":
            return _SyntheticTTSProcessor(self)
        voice_id = self.config.tts_voice
        if self.config.tts_provider != "local":
            voice_id = default_phone_tts_voice("local")
        return _create_kokoro_tts(
            voice_id=voice_id,
            normalize_text=True,
            suppress_context_text=True,
        )

    @staticmethod
    def _configure_loopback_input_vad(transport: Any) -> None:
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams

        params = getattr(transport.input(), "_params", None)
        if params is None:
            return
        params.vad_analyzer = SileroVADAnalyzer(params=VADParams(stop_secs=_RECEPTIONIST_INPUT_VAD_STOP_SECS))

    async def _start_file_watcher_receptionist(self) -> None:
        """Build a parallel Pipecat pipeline on the recipient transport that
        relays audio through Whisper STT → file IO → Kokoro TTS instead of
        an LLM. The operator (e.g. an external Claude session) reads
        finalized Viola transcripts from ``incoming.jsonl`` and writes
        receptionist responses to ``outgoing.txt`` in the control directory.

        Uses real ``faster-whisper`` and real Kokoro on the recipient side so
        the audio round-trip Viola hears is end-to-end real audio, not text
        injection.
        """
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.frames.frames import StartFrame
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.runner import PipelineRunner
        from pipecat.pipeline.task import PipelineParams, PipelineTask

        from telephony.file_watcher_receptionist import (
            FileSpeechInjector,
            TranscriptWriter,
        )

        if self.recipient_transport is None:
            raise RuntimeError("recipient_transport not initialized; call start() first")

        control_dir = self.receptionist_control_dir
        if control_dir is None:
            raise ValueError("receptionist_mode='file_watcher_bot' requires receptionist_control_dir")
        control_dir = Path(control_dir)
        control_dir.mkdir(parents=True, exist_ok=True)

        # Configure VAD on the recipient inbound side so faster-whisper sees
        # audio framed by silence boundaries.
        params = getattr(self.recipient_transport.input(), "_params", None)
        if params is not None and getattr(params, "vad_analyzer", None) is None:
            params.vad_analyzer = SileroVADAnalyzer(params=VADParams(stop_secs=_RECEPTIONIST_INPUT_VAD_STOP_SECS))

        recipient_stt = self.call_manager._create_stt()
        recipient_tts_voice = "af_heart" if self.config.tts_voice != "af_heart" else "am_adam"
        # Receptionist is role-playing a human. Skip Viola's 3-sentence
        # summarizer so multi-sentence prompts aren't truncated mid-thought
        # with the canned "I'll send the full details in chat." suffix.
        recipient_tts = _create_kokoro_tts(
            voice_id=recipient_tts_voice,
            normalize_text=True,
            suppress_context_text=False,
            summarize=False,
        )

        transcript_writer = TranscriptWriter(control_dir / "incoming.jsonl")
        speech_injector = FileSpeechInjector(control_dir / "outgoing.txt")
        self._receptionist_speech_injector = speech_injector

        pipeline = Pipeline(
            [
                self.recipient_transport.input(),
                recipient_stt,
                transcript_writer,
                speech_injector,
                recipient_tts,
                self.recipient_transport.output(),
            ]
        )
        self._receptionist_pipeline_task = PipelineTask(
            pipeline,
            params=PipelineParams(allow_interruptions=True, enable_metrics=False),
            check_dangling_tasks=False,
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
        )
        runner = PipelineRunner(handle_sigint=False, name="loopback_receptionist_runner")
        self._receptionist_runner_task = asyncio.create_task(
            runner.run(self._receptionist_pipeline_task),
            name="loopback_receptionist_pipeline",
        )
        # Yield so the runner task gets a chance to start before the caller
        # proceeds to send/receive audio.
        await asyncio.sleep(0)
        logger.info(
            "Loopback file-watcher receptionist started: control_dir=%s tts_voice=%s",
            control_dir,
            recipient_tts_voice,
        )

    def _build_handlers(
        self,
        llm: Any,
        record: CallRecord,
        tts: Any | None,
    ) -> tuple[Any | None, Any | None, Any | None]:
        if not self.include_handlers:
            return None, None, None
        from telephony.hold_handler import HoldModeHandler
        from telephony.language_handler import LanguageHandler
        from telephony.voicemail_detection import PipecatVoicemailDetectionHandler

        hold_handler = HoldModeHandler(llm=llm)
        language_handler = LanguageHandler(llm=llm, tts=tts)
        if self.llm_provider is not None and self.voicemail_classifier_provider is None:
            return hold_handler, language_handler, None
        voicemail_handler = PipecatVoicemailDetectionHandler(
            llm=llm,
            classifier_llm=self._build_voicemail_classifier_llm(),
            call_record=record,
        )
        return hold_handler, language_handler, voicemail_handler

    def _register_phone_functions(
        self,
        llm: Any,
        record: CallRecord,
        mcp_name_by_llm_name: dict[str, str],
    ) -> None:
        if not hasattr(llm, "register_function"):
            return
        from telephony.call_tools import (
            consult_user_pipeline_timeout_secs,
            make_consult_user_handler,
            make_end_call_handler,
            make_save_call_result_handler,
            press_button_handler,
        )

        fake_telnyx = self._fake_telnyx

        def register(name: str, handler: Any, *, timeout_secs: float | None = None) -> None:
            wrapped = self._record_tool_call_handler(name, _guard_phone_function_handler(name, handler, record))
            llm.register_function(name, wrapped, cancel_on_interruption=False, timeout_secs=timeout_secs)

        if self._hold_handler is not None:
            register("enter_hold_mode", self._hold_handler.enter_hold)
        else:
            register("enter_hold_mode", _simulated_enter_hold_handler)
        register("press_button", press_button_handler)
        # Mirror production (call_manager.py registers consult_user with the
        # pipeline timeout derived from the consult wait constants — the
        # 67105503 dead-air fix). The phone-function-timeout-budget gate pins
        # call_manager's registration to the same derivation, so the loopback
        # exercises the exact production ordering: pipeline timeout strictly
        # greater than the handler's worst-case internal consult wait.
        register(
            "consult_user",
            make_consult_user_handler(record),
            timeout_secs=consult_user_pipeline_timeout_secs(),
        )
        register("save_call_result", make_save_call_result_handler(record))
        register(
            "end_call",
            make_end_call_handler(
                telnyx_client=fake_telnyx,
                call_control_id="loopback-call-control",
                call_record=record,
                min_duration_seconds=0,
                # Mirror production (call_manager.py registers end_call with
                # hangup_after_output_drain=True): an accepted or latched
                # end_call pushes an EndCallHangupFrame that trails the final
                # TTS media and is consumed by the real
                # EndCallHangupAfterOutputProcessor after transport.output(),
                # so the goodbye-then-hangup ordering is exercised offline.
                hangup_after_output_drain=True,
            ),
        )
        for llm_tool_name in mcp_name_by_llm_name:
            if llm_tool_name in {
                "enter_hold_mode",
                "press_button",
                "consult_user",
                "save_call_result",
                "end_call",
            }:
                continue
            register(llm_tool_name, self._make_mock_mcp_tool_handler(llm_tool_name))

    def _record_tool_call_handler(self, name: str, handler: Any) -> Any:
        async def _wrapped(params: Any) -> None:
            arguments = dict(getattr(params, "arguments", {}) or {})
            entry = {
                "name": name,
                "arguments": arguments,
                "ts": datetime.now(tz=UTC).isoformat(),
            }
            self.tool_calls.append(entry)
            if self._phone_latency_trace is not None:
                self._phone_latency_trace.record_tool_call(name)
            self._append_phone_trace_event(
                "tool_call_start",
                tool_name=name,
                arguments=arguments,
                assistant_text=str(getattr(params, "assistant_text", "") or ""),
            )
            if (
                name == "end_call"
                and self.record is not None
                and str(getattr(params, "assistant_text", "") or "").strip()
            ):
                # Scripted LLM tool handlers run before Pipecat's full-response
                # end frame flips this production guard. Preserve same-turn
                # "say goodbye, then end_call" behavior without blessing
                # tool-only hangups.
                self.record.first_assistant_turn_complete = True
            await handler(params)
            if hasattr(params, "result"):
                entry["result"] = params.result
            self._append_trace_tool_execution(
                tool_name=name,
                ok=not (isinstance(entry.get("result"), dict) and entry["result"].get("ok") is False),
                result=entry.get("result"),
            )
            self.oracle_events.append(
                {
                    "kind": "tool_result",
                    "tool_name": name,
                    "result": entry.get("result"),
                    "ts": datetime.now(tz=UTC).isoformat(),
                }
            )
            self._append_phone_trace_event(
                "tool_result",
                tool_name=name,
                result=entry.get("result"),
            )

        return _wrapped

    def _make_mock_mcp_tool_handler(self, name: str) -> Any:
        async def _handler(params: Any) -> None:
            arguments = dict(getattr(params, "arguments", {}) or {})
            override = self.tool_overrides.get(name)
            if callable(override):
                result = override(arguments)
                if asyncio.iscoroutine(result):
                    result = await result
            elif isinstance(override, dict):
                result = dict(override)
            elif name == "calendar":
                result = {"ok": True, "status": "created", "simulated": True}
            elif name == "payment" and str(arguments.get("action") or "").lower() in {
                "request_review",
                "checkout_ready",
                "handoff",
            }:
                # Mirror the production interceptor at intent.agent_executor:8498 —
                # request_review converts into a PAYMENT_GATE answer + payment_gate flag.
                merchant = str(arguments.get("merchant") or "").strip()
                total = str(arguments.get("total") or "").strip()
                summary = str(arguments.get("order_summary") or arguments.get("notes") or "").strip()
                parts: list[str] = []
                if merchant:
                    parts.append(merchant)
                if summary:
                    parts.append(summary)
                if total:
                    parts.append(total)
                gate_message = "PAYMENT_GATE: " + " — ".join(p for p in parts if p)
                # Trigger Path B confirmation through our loopback hook (mirrors
                # _maybe_start_phone_payment_confirmation in production call_manager.py).
                await self._maybe_trigger_text_payment_gate(gate_message)
                ctx = self._last_payment_gate_ctx or {}
                result = {
                    "ok": True,
                    "simulated": True,
                    "answer": gate_message,
                    "payment_gate": True,
                    "confirmation_url": ctx.get("url", ""),
                }
            else:
                result = {"ok": True, "simulated": True}
            await params.result_callback(result)

        return _handler

    def _subscribe_to_events(self, call_id: str) -> None:
        def on_transcript(event: CallTranscriptDelta) -> None:
            if event.call_id == call_id:
                self.transcript_events.append(event)

        def on_lifecycle(event: CallLifecycleEvent) -> None:
            if event.call_id == call_id:
                self.lifecycle_events.append(event)

        self._subscriptions.append(self.event_bus.subscribe(CallTranscriptDelta, on_transcript))
        self._subscriptions.append(self.event_bus.subscribe(CallLifecycleEvent, on_lifecycle))

    def _record_initial_greeting(self, text: str) -> None:
        self.oracle_events.append(
            {
                "kind": "initial_greeting",
                "text": text,
                "ts": datetime.now(tz=UTC).isoformat(),
            }
        )
        self._append_phone_trace_event("initial_greeting", text=text)

    def _publish_lifecycle(self, status: str) -> None:
        if self.record is None:
            return
        self.event_bus.publish(
            CallLifecycleEvent(
                call_id=self.record.call_id,
                status=status,
                detail="loopback_phone_call_session",
            )
        )

    async def _complete_assistant_turn(self, text: str) -> None:
        await self._maybe_trigger_text_payment_gate(text)
        turn = {
            "text": text,
            "tool_calls": list(self._pending_turn_tool_calls),
            "ts": datetime.now(tz=UTC).isoformat(),
            "payment_gate": (dict(self._last_payment_gate_ctx) if self._last_payment_gate_ctx else None),
        }
        self._append_phone_trace_event(
            "assistant_turn",
            text=text,
            tool_calls=list(self._pending_turn_tool_calls),
            payment_gate=turn["payment_gate"],
        )
        self._pending_turn_tool_calls = []
        async with self._turn_condition:
            self.assistant_turns.append(turn)
            self._turn_condition.notify_all()

    async def _maybe_trigger_text_payment_gate(self, text: str) -> None:
        """Path A: when assistant final-answer text starts with PAYMENT_GATE:, exercise
        the production confirmation pipe (ConfirmationManager.create_session +
        dispatch_confirmation_link). This mirrors what telephony.call_manager wires
        in production via TranscriptFrameCollector(on_assistant_complete=...).
        """
        if not text:
            return
        try:
            from intent.agent_executor import _PAYMENT_GATE_PREFIX
        except Exception:
            return
        if not text.strip().startswith(_PAYMENT_GATE_PREFIX):
            return
        if self._payment_gate_fired:
            return
        try:
            from services.payments.confirmation import get_confirmation_manager
            from services.payments.confirmation_link_dispatch import (
                dispatch_confirmation_link,
            )
        except Exception:
            return
        try:
            order_text = text.strip()[len(_PAYMENT_GATE_PREFIX) :].strip()
            order_summary = {
                "description": order_text,
                "raw_gate_message": text.strip(),
            }
            mgr = get_confirmation_manager()
            user_id = self.record.user_id if self.record else _DEFAULT_USER_ID
            token, url = mgr.create_session(
                order_summary,
                "",
                user_id=user_id,
                task_id=self.record.call_id if self.record else None,
                channel="phone",
                page=None,
                metadata=None,
                executor=None,
            )
            delivery: dict[str, Any] | None = None
            try:
                delivery = await dispatch_confirmation_link(
                    origin_channel="phone",
                    confirmation_url=url,
                    channel=None,
                    user_id=user_id,
                    metadata={
                        "session_id": "phone:%s" % (self.record.call_id if self.record else ""),
                        "task_id": self.record.call_id if self.record else None,
                        "order_summary": order_summary,
                    },
                )
            except Exception:
                logger.exception("Loopback payment gate dispatch failed")
            self._last_payment_gate_ctx = {
                "token": token,
                "url": url,
                "order_summary": order_summary,
                "delivery": delivery,
            }
            self._payment_gate_fired = True
            logger.info("Loopback PAYMENT_GATE triggered: token=%s url=%s", token[:8], url[:120])
        except Exception:
            logger.exception("Loopback PAYMENT_GATE setup failed")

    @staticmethod
    def _transcript_collector(transcript_collector: TranscriptCollector, **kwargs: Any) -> Any:
        from telephony.transcription_observer import TranscriptFrameCollector

        return TranscriptFrameCollector(transcript_collector, **kwargs)

    def _make_loopback_assistant_complete(self, record: CallRecord, llm: Any) -> Callable[[str], Awaitable[None]]:
        """Wire the PHONE-15 latched-end_call completion, mirroring production.

        Production CallManager._run_call_pipeline passes an on_assistant_complete
        callback to the assistant TranscriptFrameCollector that calls
        fire_latched_end_call_if_pending on every non-empty spoken turn. Without
        this, the loopback would REFUSE+LATCH a text-empty end_call (the guard runs)
        but never COMPLETE the hangup on the following spoken close — so the fix
        under test would be half-exercised. This closes that harness gap so rung-1/
        rung-2 exercise the full path-drop -> latch -> hangup sequence.
        """

        async def _on_assistant_complete(text: str) -> None:
            from telephony.call_tools import fire_latched_end_call_if_pending

            fired = await fire_latched_end_call_if_pending(record, push_frame=getattr(llm, "push_frame", None))
            self.latched_end_call_fires.append({"text": text, "fired": bool(fired)})

        return _on_assistant_complete

    @staticmethod
    def _transcription_observer(hold_handler: Any | None, language_handler: Any | None) -> Any:
        from telephony.transcription_observer import TranscriptionObserver

        return TranscriptionObserver(hold_handler=hold_handler, language_handler=language_handler)

    @staticmethod
    def _cost_metrics_collector(record: CallRecord) -> Any:
        from telephony.cost_tracker import CostMetricsCollector, CostTracker

        cost_tracker = CostTracker(
            call_id=record.call_id,
            llm_model="loopback",
            phone_number=record.phone_number,
        )
        record._cost_tracker = cost_tracker
        return CostMetricsCollector(cost_tracker)

    def _context_aggregators(self, tools_schema: Any, record: CallRecord) -> tuple[Any, Any]:
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )

        from telephony.call_manager import PHONE_INTERRUPTION_MIN_WORDS, PHONE_VAD_SILENCE_SECS
        from telephony.phone_self_echo_guard import build_phone_user_turn_strategies
        from telephony.phone_turn_stop import build_phone_user_turn_stop_strategies

        context = LLMContext(tools=tools_schema)
        smart_turn = LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=PHONE_VAD_SILENCE_SECS))
        # Build the EXACT production turn-start strategy via the one shared builder that
        # call_manager's cloud pipeline uses (SelfEchoGuardedMinWordsUserTurnStartStrategy
        # with its echo reference + generation-window floor + min_words + use_interim), so
        # the cheap autonomous rungs 1-2 exercise the real barge-in / self-echo guard rather
        # than the fully-yielding Pipecat default they used to silently run. The echo
        # reference is stashed on self + record and fed by create_bot_speech_echo_tap after
        # TTS; the floor is driven by attach_generation_floor_handlers on the user aggregator
        # — both wired in start(), identical to production.
        turn_strategies, echo_reference, floor_state = build_phone_user_turn_strategies(
            min_words=PHONE_INTERRUPTION_MIN_WORDS,
            # Same shared builder production uses, for the same reason the start
            # strategy goes through one: turn-STOP is where the 800ms-per-turn hold
            # lived, so a rig on the stock pipecat strategy would stop being an oracle
            # for turn latency (telephony/phone_turn_stop.py).
            stop_strategies=build_phone_user_turn_stop_strategies(
                turn_analyzer=smart_turn, latency_trace=self._phone_latency_trace
            ),
            latency_trace=self._phone_latency_trace,
            use_interim=True,
        )
        self._bot_speech_echo_reference = echo_reference
        self._bot_turn_floor_state = floor_state
        record._bot_speech_echo_reference = echo_reference
        record._bot_turn_floor_state = floor_state
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                # Mirror production VAD confidence (call_manager.py:4921). Telephone-band
                # 8 kHz speech yields lower Silero confidence than wideband mic audio, so
                # the library default (0.7) misses real recipient turns. Sourcing
                # PHONE_VAD_CONFIDENCE makes the rig as deaf-prone as the carrier — the
                # "Viola can't hear the recipient" class becomes reproducible offline.
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        confidence=PHONE_VAD_CONFIDENCE,
                        stop_secs=PHONE_VAD_SILENCE_SECS,
                    ),
                ),
                user_turn_strategies=turn_strategies,
            ),
        )
        return user_aggregator, assistant_aggregator


class _PassthroughProcessor:
    def __init__(self, name: str) -> None:
        from pipecat.processors.frame_processor import FrameProcessor

        class _Processor(FrameProcessor):
            async def process_frame(self, frame: Any, direction: Any) -> None:
                await super().process_frame(frame, direction)
                await self.push_frame(frame, direction)

        self._processor = _Processor(name=name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)


class _TextInjectionProcessor:
    def __init__(self) -> None:
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

        owner = self

        class _Processor(FrameProcessor):
            async def process_frame(self, frame: Any, direction: Any) -> None:
                from pipecat.frames.frames import StartFrame

                await super().process_frame(frame, direction)
                if isinstance(frame, StartFrame):
                    owner._ready.set()
                await self.push_frame(frame, direction)

        self._direction = FrameDirection.DOWNSTREAM
        self._processor = _Processor(name="loopback_text_injection")
        self._ready = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)

    async def emit_frame(self, frame: Any) -> None:
        await self._ready.wait()
        await self._processor.push_frame(frame, self._direction)


class _AssistantTurnCapture:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        from pipecat.processors.frame_processor import FrameProcessor

        class _Processor(FrameProcessor):
            def __init__(inner_self) -> None:
                super().__init__(name="loopback_assistant_capture")
                inner_self._chunks: list[str] = []
                inner_self._suppress_turn_capture = False

            async def process_frame(inner_self, frame: Any, direction: Any) -> None:
                from pipecat.frames.frames import (
                    FunctionCallsStartedFrame,
                    LLMFullResponseEndFrame,
                    LLMFullResponseStartFrame,
                    LLMTextFrame,
                    TextFrame,
                    TTSSpeakFrame,
                )

                await super().process_frame(frame, direction)
                if isinstance(frame, LLMFullResponseStartFrame):
                    inner_self._chunks = []
                    inner_self._suppress_turn_capture = False
                elif isinstance(frame, LLMTextFrame):
                    inner_self._chunks.append(frame.text)
                    if session.llm_provider is not None:
                        session._append_trace_frame_chunk(
                            "text_delta",
                            {"content": frame.text, "source": "assistant_capture"},
                        )
                elif isinstance(frame, FunctionCallsStartedFrame):
                    for fc in frame.function_calls or ():
                        session._pending_turn_tool_calls.append(
                            {
                                "name": fc.function_name,
                                "arguments": dict(fc.arguments or {}),
                                "ts": datetime.now(tz=UTC).isoformat(),
                            }
                        )
                    # end_call's task may shut the pipeline before
                    # LLMFullResponseEndFrame propagates — finalize now.
                    text = "".join(inner_self._chunks).strip()
                    if not inner_self._suppress_turn_capture and (text or session._pending_turn_tool_calls):
                        await session._complete_assistant_turn(text)
                    inner_self._chunks = []
                elif isinstance(frame, LLMFullResponseEndFrame):
                    text = "".join(inner_self._chunks).strip()
                    if not inner_self._suppress_turn_capture and (text or session._pending_turn_tool_calls):
                        await session._complete_assistant_turn(text)
                    inner_self._chunks = []
                    inner_self._suppress_turn_capture = False
                elif isinstance(frame, (TextFrame, TTSSpeakFrame)):
                    if session.llm_provider is not None:
                        session._append_trace_frame_chunk(
                            "text_delta",
                            {"content": frame.text, "source": "assistant_capture"},
                        )
                await inner_self.push_frame(frame, direction)

        self._processor = _Processor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)


class _SyntheticTTSProcessor:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        from pipecat.processors.frame_processor import FrameProcessor

        class _Processor(FrameProcessor):
            def __init__(inner_self) -> None:
                super().__init__(name="loopback_synthetic_tts")
                inner_self._chunks: list[str] = []

            async def process_frame(inner_self, frame: Any, direction: Any) -> None:
                from pipecat.frames.frames import (
                    LLMFullResponseEndFrame,
                    LLMFullResponseStartFrame,
                    LLMTextFrame,
                    TextFrame,
                    TTSAudioRawFrame,
                    TTSSpeakFrame,
                )

                await super().process_frame(frame, direction)
                await inner_self.push_frame(frame, direction)
                text = ""
                if isinstance(frame, LLMFullResponseStartFrame):
                    inner_self._chunks = []
                    return
                if isinstance(frame, LLMTextFrame):
                    inner_self._chunks.append(frame.text)
                    return
                if isinstance(frame, LLMFullResponseEndFrame):
                    text = "".join(inner_self._chunks).strip()
                    inner_self._chunks = []
                elif isinstance(frame, (TextFrame, TTSSpeakFrame)):
                    text = frame.text
                if text:
                    audio = _synthetic_tts_audio(text)
                    await inner_self.push_frame(
                        TTSAudioRawFrame(
                            audio=audio,
                            sample_rate=LOOPBACK_SAMPLE_RATE,
                            num_channels=LOOPBACK_CHANNELS,
                        ),
                        direction,
                    )

        self._processor = _Processor()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)


class _AudioCaptureListener:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        self._session = session
        self.messages: list[bytes] = []

    async def send_bytes(self, message: bytes) -> None:
        record = self._session.record
        if record is not None and record.status == CallStatus.FAILED:
            return
        self.messages.append(message)
        if len(message) > 1:
            self._session._recorded_audio.extend(message[1:])


class _LoopbackStoppedMediaTransport:
    is_connected = False

    def __init__(self, *, reason: str) -> None:
        self.media_disconnect_reason = reason

    async def wait_for_media_disconnect(self) -> bool:
        return True

    async def wait_for_unexpected_disconnect(self) -> bool:
        return True


class _TraceFrameCapture:
    def __init__(self, session: LoopbackPhoneCallSession, *, name: str) -> None:
        from pipecat.processors.frame_processor import FrameProcessor

        class _Processor(FrameProcessor):
            async def process_frame(self, frame: Any, direction: Any) -> None:
                from pipecat.frames.frames import (
                    OutputAudioRawFrame,
                    OutputDTMFFrame,
                    TranscriptionFrame,
                )

                await super().process_frame(frame, direction)
                if isinstance(frame, TranscriptionFrame):
                    session._append_trace_frame_chunk(
                        "TranscriptionFrame",
                        {
                            "frame_type": type(frame).__name__,
                            "text": frame.text,
                            "finalized": getattr(frame, "finalized", None),
                            "user_id": getattr(frame, "user_id", None),
                            "language": str(getattr(frame, "language", "") or ""),
                        },
                    )
                elif isinstance(frame, OutputAudioRawFrame):
                    session._append_trace_frame_chunk(
                        "OutputAudioRawFrame",
                        {
                            "frame_type": type(frame).__name__,
                            "audio_bytes": len(frame.audio or b""),
                            "sample_rate": frame.sample_rate,
                            "num_channels": frame.num_channels,
                        },
                    )
                elif isinstance(frame, OutputDTMFFrame):
                    session._append_trace_frame_chunk(
                        "OutputDTMFFrame",
                        {
                            "frame_type": type(frame).__name__,
                            "button": str(getattr(frame, "button", "") or ""),
                        },
                    )
                await self.push_frame(frame, direction)

        self._processor = _Processor(name=name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._processor, name)


class _SilenceDetector:
    def __init__(
        self,
        *,
        sample_rate: int = LOOPBACK_SAMPLE_RATE,
        sample_width: int = _SAMPLE_WIDTH_BYTES,
        threshold_rms: float = _RECEPTIONIST_RMS_THRESHOLD,
        silence_ms: int = _RECEPTIONIST_SILENCE_MS,
    ) -> None:
        self._sample_rate = sample_rate
        self._sample_width = sample_width
        self._threshold_rms = threshold_rms
        self._silence_seconds = silence_ms / 1000.0
        self._window: deque[tuple[float, float]] = deque()
        self.reset()

    def reset(self) -> None:
        now = time.monotonic()
        self._window.clear()
        self._wait_started_at = now
        self._last_voice_ends_at: float | None = None

    def observe(self, audio: bytes) -> None:
        if not audio:
            return
        now = time.monotonic()
        rms = _pcm_rms(audio, sample_width=self._sample_width)
        duration_seconds = len(audio) / (self._sample_rate * self._sample_width)
        self._window.append((now, rms))
        while self._window and now - self._window[0][0] > self._silence_seconds:
            self._window.popleft()
        if rms >= self._threshold_rms:
            self._last_voice_ends_at = now + duration_seconds

    def is_silent(self) -> bool:
        now = time.monotonic()
        if self._last_voice_ends_at is not None:
            return now - self._last_voice_ends_at >= self._silence_seconds
        if now - self._wait_started_at < self._silence_seconds:
            return False
        return not self._window or all(rms < self._threshold_rms for _, rms in self._window)


class _TTSBotReceptionist:
    def __init__(
        self,
        session: LoopbackPhoneCallSession,
        scenario: PhoneScenario,
        *,
        voice_id: str = _RECEPTIONIST_TTS_VOICE,
        silence_ms: int = _RECEPTIONIST_SILENCE_MS,
    ) -> None:
        self._session = session
        self._events = scenario.events or [PhoneEvent(type="utterance", text=text) for text in scenario.turns]
        if voice_id == session.config.tts_voice:
            voice_id = "af_heart" if voice_id != "af_heart" else "am_adam"
        self._tts = _create_kokoro_tts(voice_id=voice_id, normalize_text=False)
        self._silence = _SilenceDetector(silence_ms=silence_ms)
        self._task: asyncio.Task[None] | None = None
        self._finished = asyncio.Event()
        self._stopping = False
        self._error: BaseException | None = None
        self._prerendered_first_audio: bytes | None = None
        self._prerendered_first_text: str = ""

    async def start(self) -> None:
        if self._task is None:
            # Pre-render the FIRST scripted greeting before the run loop reaches it.
            # On a real carrier the answerer's greeting audio is present from the
            # moment of answer; here Kokoro CPU synthesis of a long greeting (an IVR
            # menu, a transfer line) takes several seconds, so without pre-rendering
            # the greeting reaches the line LATER than Viola's silent-answer
            # fallback window (_PHONE_ANSWER_SILENT_FALLBACK_SECS) — Viola correctly
            # concludes "silent answer" and opens, and the grader records a FALSE
            # `spoke_over_recipient_greeting`. Synthesizing it up front (off the
            # answer clock) makes the rig faithful: the greeting is on the line
            # promptly, exactly as production delivers it, so the real talk-over
            # check still fires on a real talk-over but stops false-failing on a
            # synthesis-latency artifact.
            first_text = next(
                (e.text for e in self._events if e.type not in ("silence", "hold_music") and e.text),
                "",
            )
            if first_text:
                self._prerendered_first_audio = await _render_kokoro_pcm(self._tts, first_text)
                self._prerendered_first_text = first_text
            self._task = asyncio.create_task(self._run(), name="loopback_tts_bot_receptionist")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._finished.set()

    async def wait_finished(self, *, timeout: float) -> None:
        await asyncio.wait_for(self._finished.wait(), timeout=timeout)
        if self._error is not None:
            raise self._error

    async def send_bytes(self, message: bytes) -> None:
        if len(message) > 1:
            self._silence.observe(message[1:])

    async def _run(self) -> None:
        try:
            await asyncio.sleep(_RECEPTIONIST_INITIAL_DELAY_SECONDS)
            first_text_event = True
            for event in self._events:
                if self._stopping or self._session._call_has_ended():
                    return
                if event.type == "silence":
                    await asyncio.sleep(max(0.0, event.duration_seconds))
                    continue
                if event.type == "hold_music":
                    await self._emit_hold_music(event.duration_seconds)
                    continue
                if not event.text:
                    continue
                if not first_text_event:
                    await self._wait_for_viola_silence()
                first_text_event = False

                assistant_turn_count = len(self._session.assistant_turns)
                if self._prerendered_first_audio is not None and event.text == self._prerendered_first_text:
                    audio = self._prerendered_first_audio
                    self._prerendered_first_audio = None
                else:
                    audio = await _render_kokoro_pcm(self._tts, event.text)
                await self._emit_pcm(audio)
                if event.triggers_llm:
                    await self._wait_for_assistant_turn(assistant_turn_count)
            await self._wait_for_viola_silence()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
        finally:
            self._finished.set()

    async def _wait_for_assistant_turn(self, previous_count: int) -> None:
        async with self._session._turn_condition:
            await asyncio.wait_for(
                self._session._turn_condition.wait_for(
                    lambda: len(self._session.assistant_turns) > previous_count
                    or self._session._call_has_ended()
                    or self._stopping
                ),
                timeout=_RECEPTIONIST_ASSISTANT_TURN_TIMEOUT_SECONDS,
            )
            # If the just-completed turn fired consult_user, wait for the
            # follow-up turn that incorporates the consult response. Without
            # this, the bot advances during the consult round-trip and the
            # model's post-consult response gets attributed to the next event,
            # cascading 1-turn lag through the rest of the call.
            if not self._session._call_has_ended() and not self._stopping and self._session.assistant_turns:
                latest = self._session.assistant_turns[-1]
                tool_names = [str(tc.get("name") or "") for tc in (latest.get("tool_calls") or [])]
                if "consult_user" in tool_names:
                    # Some models emit a "Give me just a moment" placeholder
                    # turn between the consult tool call and the substantive
                    # response that incorporates the consult result. Wait up
                    # to 3 additional turns for either: a turn with substantive
                    # verbal text (≥30 chars), a turn with non-consult tool
                    # calls (e.g. calendar/end_call directly), or until no
                    # more turns arrive within 15s. Without this, bot
                    # advances on the placeholder and the substantive
                    # response cascades into the next event.
                    initial_count = len(self._session.assistant_turns)
                    while len(self._session.assistant_turns) < initial_count + 3:
                        next_target = len(self._session.assistant_turns) + 1
                        try:
                            await asyncio.wait_for(
                                self._session._turn_condition.wait_for(
                                    lambda: len(self._session.assistant_turns) >= next_target
                                    or self._session._call_has_ended()
                                    or self._stopping
                                ),
                                timeout=15.0,
                            )
                        except TimeoutError:
                            break
                        if self._session._call_has_ended() or self._stopping:
                            break
                        latest_turn = self._session.assistant_turns[-1]
                        text = str(latest_turn.get("text") or "").strip()
                        non_consult_tools = [
                            tc
                            for tc in (latest_turn.get("tool_calls") or [])
                            if str(tc.get("name") or "") != "consult_user"
                        ]
                        if len(text) >= 30 or non_consult_tools:
                            break

    async def _wait_for_viola_silence(self) -> None:
        self._silence.reset()
        while not self._stopping and not self._session._call_has_ended():
            if self._silence.is_silent():
                return
            await asyncio.sleep(0.02)

    async def _emit_pcm(self, audio: bytes) -> None:
        await self._session._queue_recipient_audio(audio)

    async def _emit_hold_music(self, seconds: float) -> None:
        total_chunks = max(1, int(seconds * 1000 / CHUNK_DURATION_MS))
        for chunk_index in range(total_chunks):
            if self._stopping or self._session._call_has_ended():
                return
            await self._session._queue_recipient_audio(_sine_wave_chunk(chunk_index))
            await asyncio.sleep(CHUNK_DURATION_MS / 1000)


def _started_tee_processor(tee: AudioTeeProcessor) -> Any:
    from types import MethodType

    from pipecat.processors.frame_processor import FrameProcessor

    processor = tee.create_processor()
    original_process_frame = processor.process_frame

    async def process_frame(self: Any, frame: Any, direction: Any) -> None:
        await FrameProcessor.process_frame(self, frame, direction)
        await original_process_frame(frame, direction)

    processor.process_frame = MethodType(process_frame, processor)
    return processor


class _ScriptedIssuerChannel:
    channel_type = "loopback"
    active_delivery = True

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.questions: list[str] = []

    async def ask(self, question: str, timeout: float | None = None) -> str:
        del timeout
        self.questions.append(question)
        if self._responses:
            return self._responses.pop(0)
        return "No scripted response available. Use your best judgment."


def _make_end_call_hangup_processor(session: LoopbackPhoneCallSession, record: CallRecord) -> Any:
    """Build the REAL EndCallHangupAfterOutputProcessor for the loopback pipeline.

    Same construction shape as production CallManager._run_call_pipeline: the
    same fake Telnyx client the end_call tool handler holds, the registered
    call_control_id, the live CallRecord, and the session transport (whose
    ``wait_for_output_mark`` — absent on LoopbackTransport unless a test stubs
    it — is the media-drain boundary).
    """
    from telephony.end_call_hangup import EndCallHangupAfterOutputProcessor

    return EndCallHangupAfterOutputProcessor(
        telnyx_client=session._fake_telnyx,
        call_control_id_getter=lambda: "loopback-call-control",
        call_record=record,
        transport=session.transport,
    )


class _FakeTelnyxActions:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        self._session = session

    async def hangup(self, **kwargs: Any) -> None:
        self._session.telnyx_hangups.append(dict(kwargs))
        pipeline_task = self._session._pipeline_task
        if pipeline_task is not None:
            with suppress(Exception):
                await pipeline_task.stop_when_done()
        return None


class _FakeTelnyxCalls:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        self.actions = _FakeTelnyxActions(session)


class _FakeTelnyxClient:
    def __init__(self, session: LoopbackPhoneCallSession) -> None:
        self.calls = _FakeTelnyxCalls(session)


async def _simulated_conference_user_handler(params: Any) -> None:
    await params.result_callback(
        {
            "conference_status": "conference_dialing",
            "conference_name": "loopback-conference",
            "speaker_attribution_note": "Synthetic conference response.",
        }
    )


async def _simulated_enter_hold_handler(params: Any) -> None:
    await params.result_callback({"status": "hold_mode_entered"})


def _default_loopback_config(*, record_phone_calls: bool) -> TelnyxConfig:
    return TelnyxConfig(
        api_key="loopback",  # pragma: allowlist secret
        phone_number="+15550000000",
        sip_connection_id="loopback",
        openai_api_key="",
        mode="local",
        answering_machine_detection=False,
        audio_recording_enabled=record_phone_calls,
    )


def _coerce_scripted_turn(
    raw: ScriptedAssistantTurn | dict[str, Any],
) -> ScriptedAssistantTurn:
    if isinstance(raw, ScriptedAssistantTurn):
        return raw
    return ScriptedAssistantTurn(
        text=str(raw.get("text") or ""),
        tool_calls=[_normalize_expected_tool_call(call) for call in list(raw.get("tool_calls", []) or [])],
    )


def _normalize_expected_tool_call(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("function", {}).get("name") or "").strip()
    arguments = _tool_call_arguments(raw)
    if name == "consult_user":
        arguments.setdefault("question", "Which offered option should I choose?")
        arguments.setdefault("urgency", "medium")
    elif name == "calendar":
        arguments.setdefault("action", "add")
        arguments.setdefault("title", "Dental appointment")
        arguments.setdefault("start", "2026-05-08T14:00:00")
        arguments.setdefault("end", "2026-05-08T15:00:00")
        arguments.setdefault("notes", "Send insurance info to records@drbrownexample.com.")
    elif name == "end_call":
        arguments.setdefault("reason", "task complete")
    return {"name": name, "arguments": arguments}


def _tool_call_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw.get("arguments"), dict):
        return dict(raw["arguments"])
    function = raw.get("function")
    if isinstance(function, dict) and isinstance(function.get("arguments"), dict):
        return dict(function["arguments"])
    return {}


def _scripted_opening_text(scenario: PhoneScenario) -> str:
    caller = str(scenario.caller_name or "Jay").strip() or "Jay"
    disclosure = str(scenario.recording_disclosure or "").strip()
    purpose = str(scenario.task or "complete this call").strip()
    parts = [
        "Hello, this is Viola calling for %s." % caller,
        disclosure,
        "%s would like to %s."
        % (
            caller,
            purpose[:1].lower() + purpose[1:] if purpose else "complete this call",
        ),
    ]
    return " ".join(part for part in parts if part).strip()


def _scripted_assistant_text(event: PhoneEvent, index: int, tool_calls: list[dict[str, Any]]) -> str:
    names = {str(call.get("name") or "") for call in tool_calls}
    if "press_button" in names:
        return ""
    if "enter_hold_mode" in names:
        return "I will hold."
    if "consult_user" in names:
        return "Let me confirm that."
    if "calendar" in names:
        return "Friday at 2 PM works. I will note the insurance follow-up."
    if "end_call" in names:
        return "Thank you. Goodbye."
    if index == 1:
        return "Hi, I am calling for Jay to schedule a dental appointment."
    if "who am I speaking with" in event.text.lower():
        return "This is Viola calling for Jay Shkoukani."
    return "Thanks, I understand."


def _english_language() -> Any:
    try:
        from pipecat.transcriptions.language import Language

        return Language.EN
    except Exception:
        return None


def _create_kokoro_tts(
    *,
    voice_id: str,
    normalize_text: bool,
    suppress_context_text: bool = False,
    summarize: bool = True,
) -> Any:
    from pipecat.services.kokoro.tts import KokoroTTSService

    kwargs: dict[str, Any] = {
        "settings": KokoroTTSService.Settings(voice=voice_id),
        "sample_rate": LOOPBACK_SAMPLE_RATE,
        "stop_frame_timeout_s": 0.5,
    }
    if suppress_context_text:
        kwargs["push_text_frames"] = False
    speech_filter = None
    if normalize_text:
        from telephony.tts_normalizer import SpeechTextFilter

        speech_filter = SpeechTextFilter(summarize=summarize)
        kwargs["text_filters"] = [speech_filter]
    tts = KokoroTTSService(**kwargs)
    if speech_filter is not None:
        # Bind so the corruption guard reads the active TTS language.
        speech_filter.bind_tts(tts)
    return tts


async def _render_kokoro_pcm(tts: Any, text: str) -> bytes:
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

    if getattr(tts, "sample_rate", 0) != LOOPBACK_SAMPLE_RATE:
        # Direct run_tts() calls do not receive Pipecat's StartFrame, so set the
        # same sample rate the loopback transport would have negotiated.
        tts._sample_rate = LOOPBACK_SAMPLE_RATE

    chunks: list[bytes] = []
    async for frame in tts.run_tts(text, context_id="loopback-receptionist-%s" % uuid.uuid4().hex[:8]):
        if isinstance(frame, ErrorFrame):
            raise RuntimeError("Kokoro receptionist TTS failed: %s" % frame.error)
        if isinstance(frame, TTSAudioRawFrame):
            audio = _coerce_loopback_pcm(
                frame.audio,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
            chunks.append(audio)
    return b"".join(chunks)


def _coerce_loopback_pcm(audio: bytes, *, sample_rate: int, num_channels: int) -> bytes:
    if not audio:
        return audio
    if num_channels != LOOPBACK_CHANNELS:
        if num_channels < 1:
            raise ValueError("num_channels must be >= 1")
        if num_channels == 2:
            audio = audioop.tomono(audio, _SAMPLE_WIDTH_BYTES, 0.5, 0.5)
        else:
            audio = _first_channel_pcm(audio, num_channels=num_channels)
        num_channels = LOOPBACK_CHANNELS
    if sample_rate != LOOPBACK_SAMPLE_RATE:
        audio, _ = audioop.ratecv(
            audio,
            _SAMPLE_WIDTH_BYTES,
            num_channels,
            sample_rate,
            LOOPBACK_SAMPLE_RATE,
            None,
        )
    return audio


def _first_channel_pcm(audio: bytes, *, num_channels: int) -> bytes:
    frame_width = _SAMPLE_WIDTH_BYTES * num_channels
    data = bytearray()
    for offset in range(0, len(audio) - frame_width + 1, frame_width):
        data.extend(audio[offset : offset + _SAMPLE_WIDTH_BYTES])
    return bytes(data)


def _pcm_rms(audio: bytes, *, sample_width: int) -> float:
    if not audio:
        return 0.0
    try:
        return float(audioop.rms(audio, sample_width))
    except audioop.error:
        return 0.0


def _synthetic_tts_audio(text: str) -> bytes:
    sample_count = max(320, min(3200, len(text) * 80))
    return _sine_wave(sample_count, phase_offset=0)


def _sine_wave_chunk(chunk_index: int) -> bytes:
    samples_per_chunk = CHUNK_BYTES // _SAMPLE_WIDTH_BYTES
    return _sine_wave(samples_per_chunk, phase_offset=chunk_index * samples_per_chunk)


def _sine_wave(sample_count: int, *, phase_offset: int) -> bytes:
    data = bytearray()
    for sample_index in range(sample_count):
        angle = 2 * math.pi * _SINE_HZ * (sample_index + phase_offset) / LOOPBACK_SAMPLE_RATE
        sample = int(math.sin(angle) * _SINE_AMPLITUDE)
        data.extend(sample.to_bytes(_SAMPLE_WIDTH_BYTES, byteorder="little", signed=True))
    return bytes(data)


__all__ = [
    "LoopbackCallManager",
    "LoopbackPhoneCallSession",
    "ScriptedAssistantTurn",
    "ScriptedLoopbackLLM",
]
