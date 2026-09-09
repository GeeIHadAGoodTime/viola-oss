from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from core.json_types import to_json_value
from core.logging_config import get_logger
from intent.pipeline_contracts import IntentTTSPort, MusicPort, StatePort

from .config import IntentBridgeConfig
from .dependencies import DependencyResolver, InterpreterArtifacts
from .models import AnswerPayload, IntentPayload, InterpreterResult

logger = get_logger(__name__)


async def _maybe_await(value: object) -> object:
    """Await awaitable values or return synchronous values unchanged."""
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await cast(Awaitable[object], value)
    return value


class InterpreterRuntimeError(RuntimeError):
    """Raised when an interpreter backend fails unexpectedly."""


class InterpreterStrategy(Protocol):
    async def interpret(self, text: str) -> InterpreterResult: ...


def _require_methods(obj: object, required: tuple[str, ...]) -> None:
    missing = [name for name in required if not callable(getattr(obj, name, None))]
    if missing:
        raise InterpreterRuntimeError(f"Object missing required methods: {', '.join(missing)}")


def _as_music_port(obj: object) -> MusicPort:
    _require_methods(
        obj,
        (
            "play",
            "pause",
            "resume",
            "stop",
            "skip",
            "previous",
            "seek",
            "set_volume",
            "state",
        ),
    )
    return cast(MusicPort, obj)


def _as_tts_port(obj: object) -> IntentTTSPort:
    _require_methods(obj, ("say",))
    return cast(IntentTTSPort, obj)


def _as_state_port(obj: object) -> StatePort:
    if not hasattr(obj, "now_playing") or not hasattr(obj, "is_playing") or not hasattr(obj, "queue"):
        raise InterpreterRuntimeError("State object missing required attributes (now_playing, is_playing, queue)")
    return cast(StatePort, obj)


class _InterpretFunc(Protocol):
    def __call__(self, text: str) -> object: ...


class _InterpreterInstance(Protocol):
    def _async_interpret(self, text: str) -> object: ...


class _InterpreterFactory(Protocol):
    def __call__(
        self,
        *,
        config: object,
        music_player: object | None,
        tts_engine: object | None,
        app_state: object | None,
        gpt_handler: object | None,
    ) -> _InterpreterInstance: ...


class NullInterpreterStrategy:
    async def interpret(self, text: str) -> InterpreterResult:
        return InterpreterResult(error="No interpreter available.")

    @property
    def interpreter(self) -> object | None:
        return None


class FunctionInterpreterStrategy:
    def __init__(self, interpret_func: _InterpretFunc) -> None:
        self._interpret_func = interpret_func

    async def interpret(self, text: str) -> InterpreterResult:
        try:
            result = await _maybe_await(self._interpret_func(text))
        except Exception as exc:
            logger.warning("Legacy function interpreter failed: %s", exc)
            raise InterpreterRuntimeError(str(exc)) from exc

        if isinstance(result, Mapping):
            raw_params_obj = result.get("params")
            raw_params: Mapping[object, object] = raw_params_obj if isinstance(raw_params_obj, Mapping) else {}
            # Ensure params has string keys as expected by IntentPayload
            params = {str(k): to_json_value(v) for k, v in raw_params.items()}
            payload = IntentPayload(
                type=str(result.get("type", "unknown")),
                params=params,
            )
            return InterpreterResult(payload=payload, raw=to_json_value(result))

        payload = IntentPayload(
            type="unknown",
            params={"raw": to_json_value(result)},
        )
        return InterpreterResult(payload=payload, raw=to_json_value(result))

    @property
    def interpreter(self) -> object | None:
        return None


class ClassInterpreterStrategy:
    def __init__(
        self,
        artifacts: InterpreterArtifacts,
        resolver: DependencyResolver,
        config: IntentBridgeConfig,
        music: object | None,
        tts: object | None,
        state: object | None,
        user_id: str,
    ) -> None:
        if artifacts.interpreter_cls is None:
            raise InterpreterRuntimeError("Interpreter class missing.")

        self._resolver = resolver
        self._config = config
        self._music = music
        self._tts = tts
        self._state = state
        self._user_id = user_id

        self._interpreter = self._build_interpreter(cast(_InterpreterFactory, artifacts.interpreter_cls))

    def _build_interpreter(self, interpreter_cls: _InterpreterFactory) -> _InterpreterInstance:
        # music IS MusicPlayer directly (no adapter wrapping)
        music_player = self._music

        # Canonical AI path: Use IntentPipeline with LLM router instead of direct GptHandler
        gpt_handler: object | None = None
        try:
            from intent.pipeline import create_pipeline

            # Create IntentPipeline as the canonical AI engine
            if music_player is not None:
                music_port = _as_music_port(music_player)
                # TTS port is optional — pipeline works without TTS (headless, no-kokoro)
                tts_port: IntentTTSPort | None = None
                if self._tts is not None:
                    try:
                        tts_port = _as_tts_port(self._tts)
                    except InterpreterRuntimeError:
                        logger.debug("TTS object lacks 'say' method, pipeline will run without TTS")
                # State port is optional — pipeline works without state (headless)
                state_port: StatePort | None = None
                if self._state is not None:
                    try:
                        state_port = _as_state_port(self._state)
                    except InterpreterRuntimeError:
                        logger.debug("State object missing required attributes, pipeline will run without state")
                intent_pipeline = create_pipeline(
                    music=music_port,
                    tts=tts_port,
                    state=state_port,
                    config_or_settings=self._resolver.settings,
                    use_llm_router=True,  # Explicitly use LLM router
                    user_id=self._user_id,
                )
                # For backwards compatibility, provide pipeline as gpt_handler to interpreter
                gpt_handler = intent_pipeline
            else:
                logger.error("No music player available for canonical IntentPipeline")
                raise RuntimeError("No music player available for canonical IntentPipeline")

        except Exception as exc:
            logger.exception(
                "IntentPipeline unavailable; startup must fail loudly: %s",
                exc,
            )
            raise InterpreterRuntimeError(str(exc)) from exc

        try:
            interpreter = interpreter_cls(
                config=self._resolver.settings,
                music_player=music_player,
                tts_engine=self._tts,
                app_state=self._state,
                gpt_handler=gpt_handler if self._config.enable_gpt else None,
            )
        except Exception as exc:
            logger.exception("Failed to instantiate IntentInterpreter: %s", exc)
            raise InterpreterRuntimeError(str(exc)) from exc

        logger.info(
            "IntentInterpreter initialized with canonical AI: IntentPipeline + LLM router",
        )

        return interpreter

    async def interpret(self, text: str) -> InterpreterResult:
        try:
            result = await _maybe_await(self._interpreter._async_interpret(text))
        except Exception as exc:
            logger.exception("IntentInterpreter failed: %s", exc)
            raise InterpreterRuntimeError(str(exc)) from exc

        return self._normalize(text, result)

    @staticmethod
    def _normalize(text: str, result: object) -> InterpreterResult:
        if isinstance(result, Mapping):
            if result.get("success") or result.get("ok"):
                intent_name = result.get("intent", "delegated")
                message = result.get("message", "")
                spoken = bool(result.get("spoken", False))
                if intent_name == "ignore":
                    # "ignore" is NOT an answer — it means "don't respond at all".
                    # Route as IntentPayload so dispatch preserves type="ignore"
                    # and voice_command_handler can detect it for FP marking.
                    return InterpreterResult(
                        payload=IntentPayload(
                            type="ignore",
                            params={"message": message, "spoken": spoken},
                        ),
                        raw=to_json_value(result),
                    )
                if intent_name in ("answer", "knowledge_direct", "knowledge_enrich"):
                    # Extract card from result if LLM provided one
                    _card_raw = result.get("card")
                    _card = _card_raw if isinstance(_card_raw, dict) else None
                    return InterpreterResult(
                        answer=AnswerPayload(
                            message=message,
                            spoken=spoken,
                            original_intent=intent_name,
                            continue_listening=bool(result.get("continue_listening", False)),
                            card=_card,
                        ),
                        raw=to_json_value(result),
                    )

                # Preserve original params from AI result (contains query, etc.)
                # FIX: Without this, play commands lose the query parameter when
                # skills don't handle the command (cold start, skill timeout, etc.)
                original_params = result.get("params", {})
                if isinstance(original_params, Mapping):
                    original_params_clean = {str(k): to_json_value(v) for k, v in original_params.items()}
                else:
                    original_params_clean = {}

                params = {
                    **original_params_clean,  # Original params first (query, etc.)
                    "message": message,
                    "spoken": spoken,
                    "original_intent": intent_name,
                }
                return InterpreterResult(
                    payload=IntentPayload(type="delegated", params=params),
                    raw=to_json_value(result),
                )

            # When success=False, preserve dispatch errors that already reached a command handler.
            # Parse errors stay errors so the bridge can return unknown with the original text.
            intent_name = result.get("intent", "help")
            error_msg = result.get("message", "No matching handler found for that request.")

            # Commands that might have been executed (dispatch failure vs parse failure)
            # If intent_name != "help", the command was parsed and dispatch was attempted
            executed_commands = {
                "play",
                "pause",
                "resume",
                "stop",
                "next",
                "previous",
                "skip",
                "seek",
                "volume.set",
                "volume.up",
                "volume.down",
                "status",
                "play_next",
                "add_to_queue",
                "say",
            }

            if intent_name in executed_commands:
                # Dispatch was attempted - return as delegated with failure info
                # Preserve the failed executed command instead of re-dispatching it.
                params = {
                    "message": error_msg,
                    "spoken": False,
                    "original_intent": intent_name,
                    "executed": True,
                    "failed": True,
                }
                return InterpreterResult(
                    payload=IntentPayload(type="delegated", params=params),
                    raw=to_json_value(result),
                )

            # Parse/validation failure - return error so the bridge can mark it unknown.
            return InterpreterResult(error=error_msg, raw=to_json_value(result))

        if result:
            return InterpreterResult(
                payload=IntentPayload(type="delegated", params={"result": to_json_value(result)}),
                raw=to_json_value(result),
            )

        return InterpreterResult(
            payload=IntentPayload(type="unknown", params={"text": text}),
            raw=to_json_value(result),
        )

    @property
    def interpreter(self) -> object:
        return self._interpreter


def build_strategy(
    artifacts: InterpreterArtifacts,
    resolver: DependencyResolver,
    config: IntentBridgeConfig,
    music: object | None,
    tts: object | None,
    state: object | None,
    user_id: str,
) -> InterpreterStrategy:
    if artifacts.kind == "funcs" and artifacts.interpret_func:
        return FunctionInterpreterStrategy(cast(_InterpretFunc, artifacts.interpret_func))

    if artifacts.kind == "class":
        return ClassInterpreterStrategy(
            artifacts=artifacts,
            resolver=resolver,
            config=config,
            music=music,
            tts=tts,
            state=state,
            user_id=user_id,
        )

    logger.warning("No interpreter artifacts resolved; using null strategy.")
    return NullInterpreterStrategy()


__all__ = [
    "ClassInterpreterStrategy",
    "FunctionInterpreterStrategy",
    "InterpreterRuntimeError",
    "InterpreterStrategy",
    "NullInterpreterStrategy",
    "build_strategy",
]
