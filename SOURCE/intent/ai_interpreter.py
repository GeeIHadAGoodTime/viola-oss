from __future__ import annotations

import asyncio
import time
from typing import Protocol, cast

from core.logging_config import get_logger
from models.player import PlayerState, QueueItem

from .command_executor import CommandExecutor
from .pipeline_contracts import MusicPort
from .types import Intent

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


class _AppState(Protocol):
    last_gpt_response: str | None


def _record_intent_operation(
    success: bool,
    intent_type: str | None,
    start_time: float,
    *,
    source: str = "unknown",
    has_answer: bool = False,
    error: str | None = None,
) -> None:
    """Record intent parsing operation for AI debugging diagnostics."""
    try:
        from diagnostics.operation_trace import OperationType, record_operation

        duration_ms = (time.perf_counter() - start_time) * 1000
        record_operation(
            OperationType.SKILL,
            "intent_parsed",
            success=success,
            details={
                "intent_type": intent_type or "unknown",
                "source": source,
                "has_answer": has_answer,
            },
            duration_ms=round(duration_ms, 2),
            error=error,
        )
    except ImportError:
        pass
    except Exception as e:
        logger.debug("Failed to record intent operation trace: %s", e)


class IntentInterpreter:
    """
    High-level intent interpreter that combines rule-based parsing with GPT fallback.

    NOTE: This is a LEGACY interface maintained for backward compatibility.
    For new code, prefer using IntentPipeline from intent.pipeline which provides
    a unified, canonical path for intent processing.

    This class is expected by viola_main.py and other legacy code paths.
    """

    # Set of intents that are safe to execute deterministically when GPT declines.
    _DETERMINISTIC_INTENTS: set[str] = {
        "play",
        "pause",
        "resume",
        "stop",
        "next",
        "previous",
        "seek",
        "volume.set",
        "volume.up",
        "volume.down",
        "status",
        "help",
        "say",
        "play_next",
        "add_to_queue",
        "shuffle_on",
        "shuffle_off",
        "shuffle",
    }

    def __init__(
        self,
        config: object,
        music_player: MusicPort | None = None,
        tts_engine: object | None = None,
        app_state: object | None = None,
        gpt_handler: object | None = None,
        *,
        rule_interpreter: object | None = None,
    ) -> None:
        """Initialize IntentInterpreter."""
        self.config = config
        self.music_player = music_player
        self.tts_engine = tts_engine
        self.app_state = app_state
        self.gpt_handler = gpt_handler

        # Rule interpreter removed — pipeline handles all routing now.
        # Keep attribute for backward compatibility with callers.
        self.rule_interpreter = rule_interpreter
        self.command_executor = CommandExecutor(self.music_player or _NoopMusic(), self.tts_engine)

        logger.info(
            "IntentInterpreter initialized with GPT handler: %s",
            gpt_handler is not None,
        )

    def parse(self, text: str) -> dict[str, object]:
        """Parse text into intent result dict (for backward compatibility with tests)."""
        if self.rule_interpreter is None:
            return {"intent": None, "action": None, "args": {}}
        try:
            intent = self.rule_interpreter.parse(text)
            return {"intent": intent.name, "action": intent.name, "args": intent.args}
        except Exception as e:
            logger.debug(
                "Rule parser did not recognize input (expected for questions): %s - %s",
                text[:50] if text else "<empty>",
                e,
            )
            return {"intent": None, "action": None, "args": {}}

    def maybe_parse_deterministic(self, text: str) -> Intent | None:
        """Check if the utterance maps cleanly onto a deterministic command."""
        if not text or self.rule_interpreter is None:
            return None

        try:
            intent = self.rule_interpreter.parse(text)
        except Exception as e:
            logger.debug(
                "Deterministic parse failed (expected for non-command inputs): %s - %s",
                text[:50] if text else "<empty>",
                e,
            )
            return None

        if intent.name in self._DETERMINISTIC_INTENTS:
            return intent

        return None

    # -------------------------------------------------------------------------
    # Main Entry Point
    # -------------------------------------------------------------------------

    async def _async_interpret(
        self,
        text: str,
        history: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """
        AI-FIRST routing: ALL requests go through GPT which decides if it's a command or answer.

        Room targeting is the LLM's job: it sees the phrase and emits a tool
        call with ``target_room`` as a parameter. No regex pre-processor runs
        here. The previous ``intent.room_target_extractor`` was a hard-coded
        classifier that produced false-positives (e.g. "turn on the living
        room lights" → command_text="turn") and was removed 2026-04-27 per
        founder direction "it should just be the LLM understanding the phrase
        and taking action" (memory antipattern #3 / project_no_tool_classifier.md).
        """
        start_time = time.perf_counter()

        if not text or not text.strip():
            _record_intent_operation(False, None, start_time, error="empty_input")
            return {"success": False}

        self._pending_target_room: str | None = None
        effective_text = text

        try:
            # Route through GPT first if available
            if self.gpt_handler:
                result = await self._route_through_gpt(effective_text, history, start_time)
                if result is not None:
                    return self._attach_target_room_to_result(result)

            # Fallback to rule-based parsing
            result = await self._fallback_to_rule_based(effective_text, start_time)
            return self._attach_target_room_to_result(result)
        finally:
            self._pending_target_room = None

    def _attach_target_room_to_result(self, result: dict[str, object]) -> dict[str, object]:
        """Attach target_room metadata to the result dict (for informational purposes)."""
        if self._pending_target_room:
            result["target_room"] = self._pending_target_room
        return result

    # -------------------------------------------------------------------------
    # GPT Routing
    # -------------------------------------------------------------------------

    async def _route_through_gpt(
        self,
        text: str,
        history: list[dict[str, object]] | None,
        start_time: float,
    ) -> dict[str, object] | None:
        """Route request through GPT. Returns result or None to fallback."""
        if self.gpt_handler is None:
            return None
        try:
            logger.info(
                "AI-FIRST routing: sending request to GPT via canonical prompt frames (ignored legacy msgs=%d): %s",
                len(history) if history else 0,
                text[:100],
            )
            route_command = getattr(self.gpt_handler, "route_command", None)
            if not callable(route_command):
                logger.debug("GPT handler missing route_command; falling back to rule-based parsing")
                return None

            if history:
                # REMOVE AFTER USERS: callers may still provide role/content
                # turns here, but provider input must come from prompt frames.
                logger.warning(
                    "Ignoring legacy AIInterpreter history payload (%d messages)",
                    len(history),
                )
            gpt_result_obj = route_command(text)
            if asyncio.iscoroutine(gpt_result_obj):
                gpt_result_obj = await gpt_result_obj

            if not isinstance(gpt_result_obj, dict):
                logger.warning("GPT handler returned non-dict result: %s", type(gpt_result_obj))
                return None

            gpt_result = cast(dict[str, object], gpt_result_obj)
            result_type_obj = gpt_result.get("type")
            result_type = result_type_obj if isinstance(result_type_obj, str) else None

            logger.debug(
                "[TRACE] IntentInterpreter: received result_type=%s, keys=%s",
                result_type,
                list(gpt_result.keys()),
            )

            return await self._handle_gpt_result(text, gpt_result, result_type, start_time)

        except Exception as e:
            logger.exception("AI-first routing failed: %s", e)
            return None

    async def _handle_gpt_result(
        self,
        text: str,
        gpt_result: dict[str, object],
        result_type: str | None,
        start_time: float,
    ) -> dict[str, object] | None:
        """Handle GPT result based on type."""
        if result_type == "ignore":
            return self._handle_gpt_ignore(gpt_result, start_time)

        if result_type == "answer":
            return await self._handle_gpt_answer(text, gpt_result, start_time)

        if result_type == "command":
            return await self._handle_gpt_command(text, gpt_result, start_time)

        if result_type == "error":
            self._log_gpt_error(gpt_result)
            return None  # Fall through to rule-based

        logger.warning(
            "Unknown GPT result type: %s (full result: %s)",
            result_type,
            list(gpt_result.keys()),
        )
        return {"success": False, "message": "I got confused. Could you try again?"}

    async def _handle_gpt_answer(
        self,
        text: str,
        gpt_result: dict[str, object],
        start_time: float,
    ) -> dict[str, object]:
        """Handle GPT answer type result."""
        # Skip deterministic override if the pipeline already executed the
        # command (instant/rule match).  Without this guard, commands like
        # "play my playlist" get double-executed: first correctly via the
        # instant command (play_default_playlist), then incorrectly via the
        # rule interpreter which searches for "my playlist" as a query.
        deterministic_intent = None
        if not gpt_result.get("already_executed"):
            deterministic_intent = self.maybe_parse_deterministic(text)
        if deterministic_intent:
            logger.info(
                "GPT returned answer, but deterministic command '%s' detected - executing locally.",
                deterministic_intent.name,
            )
            override_payload = await self._run_deterministic_override(text, deterministic_intent)
            if override_payload:
                return override_payload
            logger.warning("Deterministic override failed; falling back to GPT answer.")

        answer_obj = gpt_result.get("answer")
        answer = answer_obj if isinstance(answer_obj, str) else "I'm not sure what to say."
        logger.info("GPT answered question: %s", answer[:100])

        # Guard against double-response: when the pipeline already executed the
        # command and spoke the response (instant/rule/plugin/knowledge/state/
        # cache paths), ``already_executed`` is set to True.  Speaking here as
        # well would produce two identical TTS utterances.
        if not gpt_result.get("already_executed"):
            # Use voice_summary if the LLM provided one (for card/long/URL responses)
            voice_summary = gpt_result.get("voice_summary")
            has_card = bool(gpt_result.get("card"))
            has_urls = "http://" in answer or "https://" in answer
            is_long = len(answer) > 200

            if voice_summary:
                # Layer 1: LLM provided a brief spoken version
                await self._speak(voice_summary)
            elif has_card or is_long:
                # Layer 2: Auto-generate brief spoken version
                card_data = gpt_result.get("card", {})
                card_title = card_data.get("title", "") if isinstance(card_data, dict) else ""
                if card_title:
                    await self._speak("Here are the details on %s. Check the card." % card_title)
                else:
                    await self._speak("I have the details on your screen.")
            elif has_urls:
                # Layer 2b: Has URLs but not long enough for card — brief spoken version
                await self._speak("I found what you need. The link is on your screen.")
            else:
                # Short response, no URLs — speak normally
                await self._speak(answer)

        if self.app_state is not None:
            app_state = cast(_AppState, self.app_state)
            app_state.last_gpt_response = answer

        # Preserve the pipeline source/intent so callers can distinguish
        # Phase 2.5 (knowledge resolver) from Phase 3 (LLM agent).
        pipeline_source = gpt_result.get("source")
        original_intent = gpt_result.get("original_intent")
        intent_name = original_intent if isinstance(original_intent, str) else "answer"
        record_source = pipeline_source if isinstance(pipeline_source, str) else "gpt"

        _record_intent_operation(True, intent_name, start_time, source=record_source, has_answer=True)
        result_dict: dict[str, object] = {
            "success": True,
            "intent": intent_name,
            "message": answer,
            "spoken": True,
        }
        if gpt_result.get("already_executed"):
            result_dict["already_executed"] = True
        if pipeline_source:
            result_dict["source"] = pipeline_source
        if gpt_result.get("continue_listening"):
            result_dict["continue_listening"] = True
        # Pass card through for IntentBridge → WebSocket broadcast
        card_val = gpt_result.get("card")
        if card_val and isinstance(card_val, dict):
            result_dict["card"] = card_val
        return result_dict

    def _handle_gpt_ignore(
        self,
        gpt_result: dict[str, object],
        start_time: float,
    ) -> dict[str, object]:
        """Handle GPT ignore type result — ambient/non-directed speech."""
        reason_obj = gpt_result.get("reason")
        reason = reason_obj if isinstance(reason_obj, str) else "non-directed speech"
        logger.info("GPT classified as ignore (ambient speech): %s", reason)

        _record_intent_operation(True, "ignore", start_time, source="gpt", has_answer=False)
        return {
            "success": True,
            "intent": "ignore",
            "message": "",
            "spoken": True,
        }

    async def _handle_gpt_command(
        self,
        text: str,
        gpt_result: dict[str, object],
        start_time: float,
    ) -> dict[str, object]:
        """Handle GPT command type result."""
        command = gpt_result.get("command")
        params = gpt_result.get("params", {})
        recommendation_obj = gpt_result.get("answer")
        recommendation_msg = recommendation_obj if isinstance(recommendation_obj, str) else None

        # Validate command
        if not command or not isinstance(command, str):
            return await self._handle_invalid_command(command)

        if not isinstance(params, dict):
            logger.warning("Invalid params from GPT, using empty dict: %s", type(params))
            params = {}

        logger.info("GPT identified command: %s with params: %s", command, params)

        # Check if command was already executed by the pipeline (rule interpreter)
        # This is indicated by having an answer/message but empty params for rule-based commands
        # Rule interpreter commands: play, pause, resume, stop, next, previous, seek, volume.set, etc.
        rule_based_commands = {
            # Short form commands (from rule interpreter)
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
            "help",
            "say",
            "play_next",
            "add_to_queue",
            "shuffle",
            "shuffle_on",
            "shuffle_off",
            # Long form commands (from IntentPipeline/LLM router)
            "play_music",
            "pause_music",
            "resume_music",
            "stop_music",
            "next_track",
            "previous_track",
            "skip_track",
            "set_volume",
            "volume_up",
            "volume_down",
        }
        if command in rule_based_commands and recommendation_msg:
            # Command was already executed by rule interpreter, just return the result
            logger.info(
                "Command '%s' already executed by rule interpreter, returning result",
                command,
            )
            if recommendation_msg:
                await self._speak(recommendation_msg)
            _record_intent_operation(True, command, start_time, source="rule_pipeline", has_answer=True)
            return {
                "success": True,
                "intent": command,
                "message": recommendation_msg,
                "spoken": True,
                "params": params,  # Preserve original params (query, etc.)
            }

        # Inject target_room into params if we extracted one from the voice command
        if self._pending_target_room:
            params["target_room"] = self._pending_target_room

        # Execute the command
        return await self._execute_gpt_command(command, params, recommendation_msg, start_time)

    async def _handle_invalid_command(self, command: object) -> dict[str, object]:
        """Handle invalid command from GPT."""
        logger.error("Invalid command from GPT: %s", command)
        error_msg = "I understood you want me to do something, but I'm not sure what."
        await self._speak(error_msg)
        return {
            "success": False,
            "intent": "unknown",
            "message": error_msg,
            "spoken": True,
        }

    async def _execute_gpt_command(
        self,
        command: str,
        params: dict,
        recommendation_msg: str | None,
        start_time: float,
    ) -> dict[str, object]:
        """Execute a GPT-identified command."""
        if recommendation_msg:
            logger.info("GPT provided recommendation: %s", recommendation_msg[:100])
            await self._speak(recommendation_msg)

        result = await self.command_executor.execute_command(command, params)
        success = result["success"]
        response_msg = result["message"]

        if not recommendation_msg and response_msg:
            await self._speak(response_msg)

        final_message = recommendation_msg or response_msg or "Done"
        _record_intent_operation(
            success,
            command,
            start_time,
            source="gpt",
            has_answer=bool(recommendation_msg),
        )

        return {
            "success": success,
            "intent": command,
            "message": final_message,
            "data": result.get("data", {}),
            "spoken": True,
            "params": params,  # Preserve original params (query, etc.)
        }

    def _log_gpt_error(self, gpt_result: dict[str, object]) -> None:
        """Log GPT error result."""
        error_msg_obj = gpt_result.get("message") or gpt_result.get("error") or "AI response unavailable"
        error_msg = error_msg_obj if isinstance(error_msg_obj, str) else str(error_msg_obj)
        error_source_obj = gpt_result.get("source", "unknown")
        error_source = error_source_obj if isinstance(error_source_obj, str) else str(error_source_obj)
        init_error_obj = gpt_result.get("init_error", "none")
        init_error = init_error_obj if isinstance(init_error_obj, str) else str(init_error_obj)
        logger.warning(
            "Pipeline returned error: %s (source=%s, init_error=%s)",
            error_msg[:50] if error_msg else "none",
            error_source,
            init_error,
        )
        logger.debug("[TRACE] Error type received, falling through to rule-based parsing")

    # -------------------------------------------------------------------------
    # Fallback
    # -------------------------------------------------------------------------

    async def _fallback_to_rule_based(
        self,
        text: str,
        start_time: float,
    ) -> dict[str, object]:
        """Fallback to rule-based parsing."""
        if self.rule_interpreter is None:
            return await self._handle_all_methods_failed(text, start_time)

        logger.info("Falling back to rule-based parsing")

        # If we have a target_room, parse only (don't dispatch locally)
        # and route through CommandExecutor which handles room forwarding
        if self._pending_target_room:
            return await self._rule_based_with_room_target(text, start_time)

        try:
            result = self.rule_interpreter.interpret(text)
            if result.ok:
                logger.info("Rule-based interpretation succeeded: %s", result.intent.name)
                if result.message:
                    await self._speak(result.message)
                _record_intent_operation(True, result.intent.name, start_time, source="rule_based")
                return {
                    "success": True,
                    "intent": result.intent.name,
                    "message": result.message,
                    "spoken": True,
                    "params": dict(result.intent.args),  # Preserve original params (query, etc.)
                }
            # Check if this was a dispatch failure (command recognized but execution failed)
            # vs. parse failure (command not understood)
            # Parse failures return intent.name == "help", dispatch failures return actual intent
            if result.intent.name != "help":
                # Dispatch failed - command was recognized and attempted but failed
                logger.warning(
                    "Rule-based dispatch failed for intent '%s': %s",
                    result.intent.name,
                    result.message,
                )
                _record_intent_operation(
                    False,
                    result.intent.name,
                    start_time,
                    source="rule_based",
                    error="dispatch_failed",
                )
                return {
                    "success": False,
                    "intent": result.intent.name,
                    "message": result.message or f"Failed to execute {result.intent.name}",
                    "spoken": False,
                    "params": dict(result.intent.args),  # Preserve original params for retry
                }
        except Exception as e:
            logger.warning("Rule-based interpretation also failed: %s", e)

        return await self._handle_all_methods_failed(text, start_time)

    async def _rule_based_with_room_target(
        self,
        text: str,
        start_time: float,
    ) -> dict[str, object]:
        """
        Parse the command via rule interpreter (no dispatch) then route
        through CommandExecutor with target_room for room-aware forwarding.
        """
        try:
            intent = self.rule_interpreter.parse(text)
        except Exception as e:
            logger.debug("Rule parse failed for room-targeted command: %s", e)
            return await self._handle_all_methods_failed(text, start_time)

        logger.info(
            "Rule-parsed room-targeted command: intent=%s, target_room=%s",
            intent.name,
            self._pending_target_room,
        )

        # Build params with target_room
        params = dict(intent.args)
        if self._pending_target_room:
            params["target_room"] = self._pending_target_room

        # Execute through CommandExecutor which handles room routing
        result = await self.command_executor.execute_command(intent.name, params)
        success = result["success"]
        response_msg = result["message"]

        if response_msg:
            await self._speak(response_msg)

        _record_intent_operation(
            success,
            intent.name,
            start_time,
            source="rule_based_room_routed",
        )

        return {
            "success": success,
            "intent": intent.name,
            "message": response_msg or "Done",
            "data": result.get("data", {}),
            "spoken": True,
            "params": params,
        }

    async def _handle_all_methods_failed(
        self,
        text: str,
        start_time: float,
    ) -> dict[str, object]:
        """Handle case when all interpretation methods failed."""
        logger.warning("All interpretation methods failed for: %s", text[:50])

        if not self.gpt_handler:
            error_msg = "I can handle music commands right now, but for questions I need an AI API key in the .env file. Check useviola.com/setup for instructions."
            logger.warning("GPT not configured, user tried non-music command")
        else:
            error_msg = "No matching command or handler found."

        await self._speak(error_msg)
        _record_intent_operation(False, None, start_time, error="all_methods_failed")
        return {"success": False, "message": error_msg, "spoken": True}

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    async def _speak(self, text: str) -> None:
        """Helper to speak text via TTS."""
        if not text or not self.tts_engine:
            if not self.tts_engine and text:
                logger.warning("No TTS engine available to speak: %s", text[:50])
            return

        try:
            if hasattr(self.tts_engine, "speak"):
                result = self.tts_engine.speak(text)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    task.add_done_callback(_log_task_exception)
                logger.info("TTS queued: %s", text[:100])
            else:
                logger.warning("TTS engine has no speak method")
        except Exception as e:
            logger.error("Failed to queue TTS: %s", e)

    async def _run_deterministic_override(self, text: str, intent: Intent) -> dict[str, object] | None:
        """Execute rule-based intent when AI declines to provide a command."""
        try:
            rule_result = self.rule_interpreter.interpret(text)
        except Exception as rule_err:
            logger.warning(
                "Deterministic override execution failed (%s) for text='%s'",
                rule_err,
                text,
            )
            return None

        if not rule_result.ok:
            logger.debug(
                "Deterministic override produced non-ok result (%s) for text='%s'",
                rule_result.intent.name,
                text,
            )
            return None

        message = rule_result.message
        data = rule_result.data or {}
        if data.get("intent") != rule_result.intent.name:
            data = {**data, "intent": rule_result.intent.name}
        source = intent.args.get("source")
        if source and data.get("source") != source:
            data = {**data, "source": source}

        if message:
            await self._speak(message)

        return {
            "success": True,
            "intent": rule_result.intent.name,
            "message": message,
            "spoken": bool(message),
            "data": data,
            "_already_spoken": bool(message),
            "params": dict(rule_result.intent.args),  # Preserve original params (query, etc.)
        }

    async def _handle_gpt_intent(self, intent_data: dict[str, object]) -> bool:
        """Handle intents recognized by GPT."""
        intent_obj = intent_data.get("intent")
        intent_name = intent_obj if isinstance(intent_obj, str) else None
        entities_obj = intent_data.get("entities", {})
        entities = entities_obj if isinstance(entities_obj, dict) else {}

        if not self.music_player:
            logger.warning("No music player available for GPT intent: %s", intent_name)
            return False

        return self._execute_gpt_intent_action(intent_name, entities)

    def _execute_gpt_intent_action(self, intent_name: str | None, entities: dict) -> bool:
        """Execute action for a GPT intent."""
        if not self.music_player:
            return False

        try:
            return self._dispatch_gpt_intent(intent_name, entities)
        except Exception as e:
            logger.error("Error handling GPT intent %s: %s", intent_name, e)
            return False

    def _dispatch_gpt_intent(self, intent_name: str | None, entities: dict) -> bool:
        """Dispatch GPT intent to appropriate handler."""
        handlers = {
            "play_music": lambda: self._play_music_intent(entities),
            "pause_music": lambda: self.music_player.pause() or True,
            "resume_music": lambda: self.music_player.resume() or True,
            "next_track": lambda: self.music_player.skip() or True,
            "volume_up": lambda: self._adjust_volume(10),
            "volume_down": lambda: self._adjust_volume(-10),
            "get_time": lambda: self._speak_current_time(),
        }

        handler = handlers.get(intent_name)
        if handler:
            return handler()
        return False

    def _play_music_intent(self, entities: dict) -> bool:
        """Handle play music intent."""
        query = entities.get("query", "")
        if query:
            self.music_player.play(query)
            return True
        return False

    def _adjust_volume(self, delta: int) -> bool:
        """Adjust volume by delta."""
        current_state = self.music_player.state()
        current_vol = getattr(current_state, "volume", 50)
        new_vol = max(0, min(100, current_vol + delta))
        self.music_player.set_volume(new_vol)
        return True

    def _speak_current_time(self) -> bool:
        """Speak the current time."""
        import datetime

        current_time = datetime.datetime.now().strftime("%I:%M %p")
        if self.tts_engine and hasattr(self.tts_engine, "speak"):
            self.tts_engine.speak(f"The current time is {current_time}")
        else:
            logger.info("Current time: %s", current_time)
        return True


class _NoopMusic(MusicPort):
    """
    No-op music port implementation (Null Object Pattern).

    INTENTIONAL STUBS: Methods use `...` (ellipsis) because they are intentionally
    empty - this is a null-object pattern.
    """

    def play(self, query: str, source: str | None = None) -> QueueItem | None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def skip(self) -> None: ...

    def previous(self) -> None: ...

    def set_volume(self, level: int) -> int:
        return 0

    def change_volume(self, delta: int) -> None: ...

    def seek(self, seconds: int) -> None: ...

    def state(self) -> PlayerState:
        """Return empty state (indicates no music player available)."""
        return PlayerState()
