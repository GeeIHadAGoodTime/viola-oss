from __future__ import annotations

from typing import Callable, Protocol, cast

from contracts.api_response import (
    ResponseContractError,
    ResponseEnvelope,
    ensure_envelope,
    success_response,
)
from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from core.user_context import get_current_or_device_user_id, user_id_or_none
from diagnostics.bus import get_diagnostics_bus
from services.liveness import Health, WorkSignal
from services.supervisor import HeartbeatSupervisor
from utils.timeout_manager import TimeoutManager, get_timeout_manager

from .config import IntentBridgeConfig, load_config
from .dependencies import DependencyResolver
from .dispatch import CommandDispatcher, _MusicAdapter, handle_dispatch_error
from .interpreter import (
    InterpreterRuntimeError,
    InterpreterStrategy,
    build_strategy,
)
from .models import IntentPayload, InterpreterResult, SkillResult
from .skills import SkillOrchestrator

logger = get_logger(__name__)


def _normalize_bridge_user_id(value: object, *, source: str) -> str:
    normalized = user_id_or_none(value)
    if normalized is not None:
        return normalized
    raise ValueError(f"IntentBridge requires explicit user_id ({source} did not provide one)")


def _bridge_user_id_or_none(value: object) -> str | None:
    return user_id_or_none(value)


def _resolve_bridge_user_id(*, user_id: str | None, state: object | None) -> str:
    candidates: list[tuple[str, object]] = [("user_id", user_id)]
    if state is not None:
        candidates.append(("state.user_id", getattr(state, "user_id", "")))

    for _source, candidate in candidates:
        resolved = _bridge_user_id_or_none(candidate)
        if resolved is not None:
            return resolved

    try:
        return _normalize_bridge_user_id(get_current_or_device_user_id(), source="current_or_device_user_id")
    except LookupError as exc:
        raise ValueError("IntentBridge requires explicit user_id (no current request context)") from exc


class _Skills(Protocol):
    def available(self) -> bool: ...

    async def process(self, text: str) -> SkillResult | None: ...


settings: object
try:  # pragma: no cover - settings unavailable in some tests
    from config import settings as _settings

    settings = _settings
except Exception as exc:  # pragma: no cover

    class _SettingsFallback:
        openai_api_key: str | None = None

    settings = _SettingsFallback()
    logger.warning("config.settings unavailable, using defaults: %s", exc)


class IntentBridge:
    """
    Backwards-compatible facade around the evolving intent stack.

    This bridge preserves the legacy ``interpret``/``dispatch`` surface relied on by
    tests and the FastAPI facade while internally delegating to modular components.
    """

    def __init__(
        self,
        music: object | None,
        tts: object | None = None,
        state: object | None = None,
        *,
        config: IntentBridgeConfig | None = None,
        resolver: DependencyResolver | None = None,
        supervisor: HeartbeatSupervisor | None = None,
        timeout_manager: TimeoutManager | None = None,
        user_id: str | None = None,
    ) -> None:
        self.music = music
        self.tts = tts
        self.state = state
        self.user_id = _resolve_bridge_user_id(user_id=user_id, state=state)

        self.config = config or load_config(settings)
        self._resolver = resolver or DependencyResolver(settings=settings, config=self.config)
        self._timeout_manager = timeout_manager or get_timeout_manager()
        self._supervisor: HeartbeatSupervisor | None = None

        self._kind: str | None = None
        self._interpret_func: Callable[..., object] | None = None
        self._dispatch_func: Callable[..., object] | None = None
        self._interpreter_strategy: InterpreterStrategy | None = None
        self.interp: object | None = None
        self.skill_manager: object | None = None
        self._skills: _Skills | None = None
        self._dispatcher: CommandDispatcher | None = None
        self._diagnostics_bus = get_diagnostics_bus()
        # Advanced only by real intent interpretation (_record_intent_heartbeat).
        # Its silence is never held against the bridge: nobody speaking is the
        # normal resting state of an on-demand component.
        self._work_signal = WorkSignal("intent_bridge", stall_after=180.0)

        self._initialize_components()
        self.attach_supervisor(supervisor)

        # Expose an IntentPipeline so that lifecycle.py's
        # ``getattr(intent, "_pipeline", intent)`` resolves to a real
        # pipeline with a ``.process()`` method instead of falling back
        # to IntentBridge (which lacks ``.process()``).
        from intent.pipeline import IntentPipeline

        self._pipeline = IntentPipeline(
            music=self.music,
            tts=self.tts,
            state=self.state,
            gpt_handler=None,  # pipeline auto-creates LLM router internally
            user_id=self.user_id,
        )

    # ----------------------------------------------------------------- helpers
    def _initialize_components(self) -> None:
        interpreter_artifacts = self._resolver.resolve_interpreter()
        self._kind = interpreter_artifacts.kind
        self._interpret_func = interpreter_artifacts.interpret_func
        self._dispatch_func = interpreter_artifacts.dispatch_func

        self._interpreter_strategy = build_strategy(
            artifacts=interpreter_artifacts,
            resolver=self._resolver,
            config=self.config,
            music=self.music,
            tts=self.tts,
            state=self.state,
            user_id=self.user_id,
        )
        self.interp = getattr(self._interpreter_strategy, "interpreter", None)

        skill_manager = self._resolver.resolve_skill_manager(
            music=self.music,
            tts=self.tts,
            state=self.state,
            enable_skills=self.config.enable_skills,
        )
        self.skill_manager = skill_manager
        self._skills = SkillOrchestrator.from_manager(skill_manager)
        self._dispatcher = CommandDispatcher(music=cast(_MusicAdapter | None, self.music))

    def attach_supervisor(self, supervisor: HeartbeatSupervisor | None) -> None:
        self._supervisor = supervisor
        if supervisor is None:
            return
        grace = getattr(self.config, "intent_heartbeat_grace_seconds", 180.0)
        # Intent interpretation is ON-DEMAND: it runs only when a user says
        # something, so silence is its healthy resting state. Registered on
        # silence alone it had the same endless-rebuild shape as the STT
        # transcriber -- the restart hook (refresh) recorded a heartbeat, so
        # every rebuild emitted the one beat that scheduled the next rebuild,
        # and an idle install re-initialised the whole intent pipeline every
        # ~6 minutes forever. Health is answered by asking whether it could
        # interpret right now; see services/liveness.py.
        supervisor.register_source(
            "intent_bridge",
            description="Intent interpretation and dispatch pipeline",
            grace_period=grace,
            restart=self.refresh,
            probe=self._probe_health,
        )

    def _probe_health(self) -> Health:
        """Readiness of the on-demand intent pipeline. Idle is healthy."""
        if self._interpret_func is None or self._interpreter_strategy is None:
            return Health.UNAVAILABLE
        return Health.WORKING if self._work_signal.worked_recently() else Health.IDLE_OK

    def _record_intent_heartbeat(self) -> None:
        """Record that intent interpretation actually ran.

        Call ONLY from the real work path. Recording this from the restart hook
        is what turned an idle install into an endless rebuild loop.
        """
        self._work_signal.mark()
        if self._supervisor:
            self._supervisor.record_heartbeat("intent_bridge")

    def refresh(self) -> bool:
        try:
            self._initialize_components()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("IntentBridge refresh failed: %s", exc)
            return False
        # Deliberately does NOT record a heartbeat: rebuilding a component is
        # not evidence that it works. Health is answered by _probe_health.
        return True

    # --------------------------------------------------------------------- API
    async def process(
        self,
        text: str,
        history: list[dict[str, object]] | None = None,
        force_ai: bool = False,
        channel: object | None = None,
        user_key: str = "",
    ) -> object:
        """Delegate scheduled/background work to the canonical IntentPipeline."""
        pipeline = getattr(self, "_pipeline", None)
        process = getattr(pipeline, "process", None)
        if not callable(process):
            raise RuntimeError("IntentBridge canonical pipeline is unavailable")
        return await process(text, history=history, force_ai=force_ai, channel=channel, user_key=user_key)

    async def interpret(self, text: str) -> ResponseEnvelope:
        if not text:
            correlation_id = self._diagnostics_bus.new_correlation_id()
            self._diagnostics_bus.emit(
                "intent.interpret.noop",
                severity="DEBUG",
                message="Empty text provided to IntentBridge.interpret",
                correlation_id=correlation_id,
            )
            return success_response({"type": "noop", "params": {}})

        logger.info(
            "IntentBridge.interpret: text='%s...', kind=%s, has_interp=%s",
            text[:50],
            self._kind,
            self.interp is not None,
        )

        self._record_intent_heartbeat()
        correlation_id = self._diagnostics_bus.new_correlation_id()
        self._diagnostics_bus.emit(
            "intent.interpret.start",
            severity="DEBUG",
            message="IntentBridge interpret start",
            correlation_id=correlation_id,
            interpreter_kind=self._kind,
            skills_available=bool(self._skills and self._skills.available()),
            text_preview=text[:80],
        )

        if self._skills and self._skills.available():
            skill_result = await self._process_skills_with_guard(text)
            if skill_result:
                logger.info("✅ Skill handled: %s...", text[:50])
                self._record_intent_heartbeat()
                self._diagnostics_bus.emit(
                    "intent.interpret.skill_success",
                    severity="INFO",
                    message="Skill orchestrator handled interpretation",
                    correlation_id=correlation_id,
                    skill_type=getattr(skill_result, "type", None),
                )
                # Telemetry: skills are rule-matched commands
                try:
                    from admin.instrumentation import record_command_routed

                    record_command_routed("instant")
                except Exception:
                    pass
                return success_response(skill_result.to_legacy())

        interpreter_result = await self._interpret_with_guard(text)

        if interpreter_result and interpreter_result.success:
            if interpreter_result.answer:
                self._record_intent_heartbeat()
                self._diagnostics_bus.emit(
                    "intent.interpret.success",
                    severity="INFO",
                    message="Interpreter returned answer",
                    correlation_id=correlation_id,
                    result_type="answer",
                )
                return success_response(interpreter_result.answer.to_legacy())
            if interpreter_result.payload:
                self._record_intent_heartbeat()
                self._diagnostics_bus.emit(
                    "intent.interpret.success",
                    severity="INFO",
                    message="Interpreter returned payload",
                    correlation_id=correlation_id,
                    result_type="payload",
                    payload_type=getattr(interpreter_result.payload, "type", None),
                )
                return success_response(interpreter_result.payload.to_legacy())

        self._record_intent_heartbeat()
        self._diagnostics_bus.emit(
            "intent.interpret.unknown",
            severity="WARNING",
            message="Interpreter returned unknown intent",
            correlation_id=correlation_id,
        )
        return success_response({"type": "unknown", "params": {"text": text}})

    async def dispatch(self, intent: dict[str, object]) -> ResponseEnvelope:
        intent_type = str(intent.get("type", "unknown"))
        params_raw = intent.get("params", {})
        params: JsonDict = (
            {str(k): to_json_value(v) for k, v in params_raw.items()} if isinstance(params_raw, dict) else {}
        )

        payload = IntentPayload(type=intent_type, params=params)
        correlation_id = self._diagnostics_bus.new_correlation_id()

        # Debug logging to catch message field regressions
        message_obj = params.get("message")
        logger.debug(
            "IntentBridge dispatch input: intent=%s params_keys=%s message_len=%d",
            intent_type,
            list(params.keys()),
            len(message_obj) if isinstance(message_obj, str) else 0,
        )
        self._diagnostics_bus.emit(
            "intent.dispatch.start",
            severity="DEBUG",
            message="IntentBridge dispatch start",
            correlation_id=correlation_id,
            intent_type=intent_type,
        )

        if intent_type == "skill":
            message_value = params.get("message", "Done")
            message = message_value if isinstance(message_value, str) else str(to_json_value(message_value))
            spoken = bool(params.get("spoken", False))
            status_payload: object = {}
            if self.music is not None:
                status_method = getattr(self.music, "status", None)
                if callable(status_method):
                    try:
                        status_payload = status_method()
                    except Exception as exc:
                        logger.debug("IntentBridge status() failed: %s", exc)
            self._diagnostics_bus.emit(
                "intent.dispatch.skill",
                severity="INFO",
                message="Dispatch returning skill payload",
                correlation_id=correlation_id,
            )
            return success_response(
                {
                    "intent": "skill",
                    "message": message,
                    "spoken": spoken,
                    "payload": params.get("data"),
                    "status": to_json_value(status_payload),
                }
            )

        # Handle Q&A, conversational, and already-dispatched intents directly
        # (not through CommandDispatcher).  "delegated" means the AI interpreter
        # already executed the command and spoke the response.
        if intent_type in (
            "answer",
            "qna",
            "factual",
            "greeting",
            "conversational",
            "help",
            "ignore",
            "delegated",
        ):
            self._diagnostics_bus.emit(
                "intent.dispatch.answer",
                severity="INFO",
                message=f"Dispatch returning {intent_type} payload directly",
                correlation_id=correlation_id,
            )
            message_value = params.get("message") or params.get("answer") or params.get("response", "")
            message = message_value if isinstance(message_value, str) else str(to_json_value(message_value))
            resp: JsonDict = {
                "intent": intent_type,
                "message": message,
                "spoken": bool(params.get("spoken", False)),
            }
            if params.get("continue_listening"):
                resp["continue_listening"] = True
            # Propagate content card from interpret() for UI display
            _card = intent.get("card")
            if _card and isinstance(_card, dict):
                resp["card"] = _card
            return success_response(resp)

        # Handle unknown intents gracefully - don't throw errors
        if intent_type == "unknown":
            self._diagnostics_bus.emit(
                "intent.dispatch.unknown",
                severity="WARNING",
                message="Dispatch handling unknown intent gracefully",
                correlation_id=correlation_id,
            )
            # If there's a message or answer in params, return it
            message_value = params.get("message") or params.get("answer") or params.get("text", "")
            message = message_value if isinstance(message_value, str) else str(to_json_value(message_value))
            if message:
                return success_response(
                    {
                        "intent": "unknown",
                        "message": message,
                        "spoken": False,
                    }
                )
            # Otherwise return a user-friendly fallback
            return success_response(
                {
                    "intent": "unknown",
                    "message": "No matching handler found for that request. Could you rephrase?",
                    "spoken": False,
                }
            )

        if self._dispatcher is None:
            raise RuntimeError("Dispatcher not initialized")

        try:
            dispatcher = self._dispatcher
            timeout_mgr = self._timeout_manager
            dispatch_timeout = getattr(self.config, "dispatch_timeout_seconds", None)

            # SP-BUG-6: run_sync_with_timeout uses future.result() which
            # blocks the calling thread.  When dispatch() runs on the
            # event-loop thread (via /v1/command), this starves the loop.
            # Wrap in asyncio.to_thread() so the blocking wait happens in
            # a worker thread instead.
            import asyncio

            def _dispatch_sync():
                return timeout_mgr.run_sync_with_timeout(
                    lambda: dispatcher.dispatch(payload),
                    operation_name="intent.dispatch",
                    timeout=dispatch_timeout,
                    fallback=None,
                    max_attempts=2,
                )

            result = await asyncio.to_thread(_dispatch_sync)
        except Exception as exc:
            self._record_intent_heartbeat()
            self._diagnostics_bus.emit(
                "intent.dispatch.error",
                severity="ERROR",
                message="Intent dispatch raised exception",
                correlation_id=correlation_id,
                error_type=type(exc).__name__,
            )
            return handle_dispatch_error(intent_type, exc)

        if isinstance(result, dict):
            self._record_intent_heartbeat()
            self._diagnostics_bus.emit(
                "intent.dispatch.success",
                severity="INFO",
                message="Dispatch returned dict result",
                correlation_id=correlation_id,
                intent_type=intent_type,
            )
            try:
                envelope = ensure_envelope(result)
            except ResponseContractError:
                fallback_value = to_json_value(result)
                fallback_payload: JsonDict
                if isinstance(fallback_value, dict):
                    fallback_payload = fallback_value
                else:
                    fallback_payload = {"result": fallback_value}
                fallback_payload.setdefault("intent", intent_type)
                return success_response(fallback_payload)
            return envelope

        self._record_intent_heartbeat()
        self._diagnostics_bus.emit(
            "intent.dispatch.success",
            severity="INFO",
            message="Dispatch returned non-dict result",
            correlation_id=correlation_id,
            intent_type=intent_type,
            result_type=type(result).__name__,
        )
        return success_response({"intent": intent_type, "result": to_json_value(result)})

    async def _process_skills_with_guard(self, text: str) -> SkillResult | None:
        if not self._skills:
            return None

        async def _operation() -> SkillResult | None:
            skills = self._skills
            return await skills.process(text)

        try:
            return await self._timeout_manager.run_with_timeout(
                _operation,
                operation_name="intent.skills",
                timeout=getattr(self.config, "skill_timeout_seconds", None),
                fallback=None,
                max_attempts=2,
            )
        except Exception as exc:
            logger.warning("Skill processing failed: %s", exc)
            self._diagnostics_bus.emit(
                "intent.skills.error",
                severity="WARNING",
                message="Skill processing failed",
                error_type=type(exc).__name__,
            )
            return None

    async def _interpret_with_guard(self, text: str) -> InterpreterResult | None:
        if self._interpreter_strategy is None:
            return None

        async def _operation() -> InterpreterResult:
            strategy = self._interpreter_strategy
            if strategy is None:
                raise RuntimeError("InterpreterStrategy missing")
            return await strategy.interpret(text)

        try:
            return await self._timeout_manager.run_with_timeout(
                _operation,
                operation_name="intent.interpret",
                timeout=getattr(self.config, "interpret_timeout_seconds", None),
                fallback=None,
                max_attempts=1,
            )
        except InterpreterRuntimeError:
            logger.warning("Interpreter runtime error; falling back")
            self._diagnostics_bus.emit(
                "intent.interpret.error",
                severity="WARNING",
                message="Interpreter runtime error",
                error_type="InterpreterRuntimeError",
            )
            return None
        except Exception as exc:
            logger.warning("Interpreter failed: %s", exc)
            self._diagnostics_bus.emit(
                "intent.interpret.error",
                severity="WARNING",
                message="Interpreter failed unexpectedly",
                error_type=type(exc).__name__,
            )
            return None


__all__ = ["IntentBridge"]
