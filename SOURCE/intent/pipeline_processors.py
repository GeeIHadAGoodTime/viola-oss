"""
Intent Pipeline Processing Phases.

This module contains the different processing phases (instant, rules, AI)
extracted from the main IntentPipeline class to comply with code constraints.
"""

from __future__ import annotations

import random
import time
from typing import Any

from core.exceptions import CostCircuitBreakerError, LLMQuotaExceededError
from core.logging_config import get_logger
from diagnostics.bus import get_diagnostics_bus
from intent.pipeline_contracts import PipelineResult
from intent.response_cleanup import strip_json_template as _strip_json_template
from intent.types import RoutedCommand
from services.llm.no_result import build_ai_no_result
from services.llm.token_limits import configured_llm_max_tokens_cap

logger = get_logger(__name__)
_diagnostics = get_diagnostics_bus()

# Instant commands that MUST stay final on failure — hardware, playback, and
# system commands where the LLM cannot offer a meaningful alternative.
# Everything NOT in this set falls through to LLM on failure (Viola almost
# never surfaces errors; she tries a different path instead).
#
# Keep this in lock-step with intent/instant_commands_patterns.py. Adding a
# command here without a matching pattern + executor causes user-facing
# errors when the shim returns instant_shim_no_executor — the pipeline then
# returns the failed result instead of falling through (audit 2026-04-28).
_LLM_FINAL_COMMANDS = frozenset(
    {
        # Playback control: smart_stop is the canonical user "stop" intent and
        # the one surviving instant command after the 2026-05-29 boxing audit.
        # pause / resume joined it (#1402, 2026-07-16): both already build a
        # complete, user-facing message on failure (e.g. resume's "I don't
        # have any music to resume"), so falling through would throw that
        # answer away and force a second, context-free model round-trip --
        # exactly the latency gap #1402 fixed for the success path.
        # volume_up / volume_down / play_default_playlist were removed as
        # semantic intent ladders; the model now routes those natively.
        "smart_stop",
        "pause",
        "resume",
    }
)


class IntentPipelineProcessors:
    """Handles the different processing phases of the intent pipeline."""

    def __init__(self, pipeline_instance):
        """
        Initialize processors.

        Args:
            pipeline_instance: The IntentPipeline instance
        """
        self.pipeline = pipeline_instance

    async def try_instant(self, text: str, user_key: str = "") -> PipelineResult | None:
        """
        Try instant command processing.

        Args:
            text: Input text
            user_key: Authenticated user identifier from the pipeline

        Returns:
            PipelineResult if instant command matches, None otherwise
        """
        if not self.pipeline.instant_handler:
            return None

        # Check for instant command match
        instant_match = self.pipeline.instant_handler.check_instant_command(text)
        if not instant_match:
            return None

        command, params, description = instant_match

        # Inject user identity so handlers can scope per-user operations
        params["_user_id"] = user_key

        # Set ambient ContextVar so downstream code (PlaylistManager._resolve_user_id,
        # get_current_user_id, etc.) sees the authenticated user — not the device fallback.
        # #1907: capture the reset Token (matching the established convention in
        # auth/middleware.py / voice_stream.py) instead of discarding it -- reset
        # in the finally below so this instant-command execution never leaves the
        # ambient identity bound past its own scope.
        user_context_token = None
        if user_key:
            from core.user_context import set_current_user_id

            user_context_token = set_current_user_id(user_key)

        # Execute instant command
        try:
            result = await self.pipeline.instant_handler.execute_instant_command(command, params)

            # If a non-hardware command failed, let the LLM try a different
            # approach instead of surfacing the error.  Viola almost never
            # errors — she finds another way.
            if not result.get("ok") and command not in _LLM_FINAL_COMMANDS:
                logger.info(
                    "Instant command '%s' failed (error=%s), falling through to LLM",
                    command,
                    result.get("error", "unknown"),
                )
                return None  # Fall through to rules -> AI

            # Check priority if room tracking is enabled
            should_execute, reason, _error_msg = self.pipeline._room_tracker.check_intent_priority(command, "instant")

            if not should_execute:
                return self.pipeline._room_tracker.create_deferred_result(command, reason)

            # Accept both "ok" and "success" keys for backward compatibility
            result_ok = result.get("ok") if "ok" in result else result.get("success", True)
            result_data = {
                **result.get("data", {}),
                "message": result.get("message", ""),
                "description": description,
                "params": params,
            }
            if isinstance(result.get("error_state"), dict):
                result_data["error_state"] = result["error_state"]
            return PipelineResult(
                ok=result_ok,
                intent=command,
                data=result_data,
                error=result.get("error"),
                source="instant",
                requires_clarification=False,
                bypass_ai=True,
                policy_flags=[],
                source_node_id=self.pipeline.node_id,
            )

        except Exception as e:
            logger.exception("Instant command '%s' failed: %s", command, e)

            # Let LLM handle if this wasn't a hardware/system command
            if command not in _LLM_FINAL_COMMANDS:
                logger.info(
                    "Instant command '%s' threw exception, falling through to LLM",
                    command,
                )
                return None

            return PipelineResult(
                ok=False,
                intent=command,
                data={"description": description, "params": params},
                error=str(e),
                source="instant",
                requires_clarification=False,
                bypass_ai=True,
                policy_flags=[],
                source_node_id=self.pipeline.node_id,
            )
        finally:
            if user_context_token is not None:
                from core.user_context import reset_current_user_id

                reset_current_user_id(user_context_token)

    async def try_ai(self, text: str, *, user_key: str = "") -> PipelineResult | None:
        """
        Try AI-based processing.

        Args:
            text: Input text
            user_key: User identifier for per-user history isolation

        Returns:
            PipelineResult from AI processing
        """
        if not self.pipeline.gpt_handler:
            return None

        # F-003: Enforce per-user rate limit before any LLM call
        try:
            from services.llm.rate_limiter import get_rate_limiter

            limiter = get_rate_limiter()
            # Resolve user_id: prefer explicit user_key, then the pipeline's
            # explicit owner, then state.user_id. Do not invent a local/device
            # fallback inside the shared request path.
            user_id = user_key or getattr(self.pipeline, "user_id", "") or ""
            if not user_id and self.pipeline.state is not None:
                state_user_id = getattr(self.pipeline.state, "user_id", None)
                if isinstance(state_user_id, str) and state_user_id:
                    user_id = state_user_id
            from core.user_context import is_placeholder_user_id

            if is_placeholder_user_id(user_id):
                user_id = ""
            if not user_id:
                return PipelineResult(
                    ok=False,
                    intent="missing_user_id",
                    data={"message": "Authentication is required for this AI request."},
                    error="missing_user_id",
                    source="ai",
                    requires_clarification=False,
                    policy_flags=["tenant_required"],
                    source_node_id=self.pipeline.node_id,
                )
            await limiter.reserve(
                user_id=user_id,
                estimated_tokens=configured_llm_max_tokens_cap(),
            )
        except (LLMQuotaExceededError, CostCircuitBreakerError) as quota_err:
            logger.warning("LLM rate limit exceeded for user %s: %s", user_id, quota_err)
            return PipelineResult(
                ok=False,
                intent="rate_limited",
                data={
                    "message": random.choice(
                        [
                            "I've hit my limit for now — try again in a bit.",
                            "I need a breather. Give me a few minutes.",
                            "My brain's a bit full right now. Try me again shortly.",
                        ]
                    ),
                },
                error=str(quota_err),
                source="ai",
                requires_clarification=False,
                policy_flags=["rate_limited"],
                source_node_id=self.pipeline.node_id,
            )

        t0_llm = time.perf_counter()
        user_context_token = None
        try:
            # E3: Set the ambient user_id ContextVar so LLM providers can record
            # it. #1907: capture the reset Token (matching the established
            # convention in auth/middleware.py / voice_stream.py) instead of
            # discarding it -- reset in the finally below so this AI-processing
            # scope never leaves the ambient identity bound past its own request.
            try:
                from core.user_context import set_current_user_id

                user_context_token = set_current_user_id(user_id)
            except Exception:
                pass

            # Use AI controller for processing
            ai_result = await self.pipeline.ai_controller.process_request(
                text,
                {},
                user_key=user_id,
            )

            # Record LLM path latency
            llm_elapsed_ms = (time.perf_counter() - t0_llm) * 1000
            try:
                from admin.instrumentation import record_latency

                record_latency("latency_intent_llm", llm_elapsed_ms)
            except Exception:
                logger.debug("Telemetry record_latency (llm) failed, continuing")

            # Extract executed-command metadata using RoutedCommand for type safety.
            commands_executed = ai_result.get("commands_executed", [])
            intent_name: str = "ai_processed"
            executed_command: RoutedCommand | None = None
            params: dict[str, Any] = {}
            answer_text: str | None = None

            if commands_executed:
                first_cmd = commands_executed[0]
                if isinstance(first_cmd, dict):
                    try:
                        executed_command = RoutedCommand.from_dict(first_cmd, source="ai")
                        intent_name = executed_command.command
                        params = executed_command.params
                        logger.info(
                            "AI extracted command '%s' with params: %s",
                            intent_name,
                            params,
                        )
                        # Emit success telemetry
                        _diagnostics.emit(
                            "pipeline.ai.command_extracted",
                            severity="INFO",
                            message=f"Pipeline extracted AI command: {intent_name}",
                            intent=intent_name,
                            params=params,
                            text_preview=text[:50],
                        )
                    except ValueError as e:
                        # RoutedCommand validation failed - log and use fallback
                        logger.warning(
                            "AI result has malformed command structure: %s. "
                            "Falling back to 'ai_processed'. Raw data: %s",
                            e,
                            first_cmd,
                        )
                        intent_name = "ai_processed"
                        # Emit fallback telemetry for monitoring
                        _diagnostics.emit(
                            "pipeline.ai.fallback_malformed",
                            severity="WARNING",
                            message="Pipeline fell back to ai_processed due to malformed command",
                            error=str(e),
                            raw_data=str(first_cmd)[:200],
                            text_preview=text[:50],
                        )
                else:
                    # commands_executed[0] is not a dict - unexpected structure
                    logger.warning(
                        "AI commands_executed[0] is not a dict (type=%s, value=%r). "
                        "This indicates a data structure mismatch in the AI pipeline. "
                        "Falling back to 'ai_processed'.",
                        type(first_cmd).__name__,
                        first_cmd,
                    )
                    intent_name = "ai_processed"
                    # Emit telemetry for data structure mismatch
                    _diagnostics.emit(
                        "pipeline.ai.fallback_type_mismatch",
                        severity="WARNING",
                        message="Pipeline fell back to ai_processed due to type mismatch",
                        expected_type="dict",
                        actual_type=type(first_cmd).__name__,
                        raw_value=str(first_cmd)[:100],
                        text_preview=text[:50],
                    )
            else:
                # No commands executed - might be an answer or empty result
                answer_raw = ai_result.get("message") or ai_result.get("data", {}).get("answer")
                if isinstance(answer_raw, str) and answer_raw.strip():
                    intent_name = "answer"
                    # CB-10: Strip any leaked JSON response template from
                    # the answer body (e.g. trailing {{"type":"answer",...}}).
                    answer_text = _strip_json_template(answer_raw.strip())
                    logger.debug("AI returned an answer, not a command")
                else:
                    logger.debug("AI returned no commands_executed and no answer. Using 'ai_processed' as fallback.")

            # E2: Detect agent-handled results so metrics can distinguish
            # single-call asks from multi-step agent chains.
            _is_agent_result = ai_result.get("intent") == "agent"

            # (Removed 2026-05-29 boxing audit) The CB-6 post-LLM false-stop
            # guard used to regex-parse the user text against SMART_STOP_REGEX
            # and OVERRIDE the model's own stop-class decision to a
            # conversational answer when the regex disagreed. That is exactly
            # the boxing CLAUDE.md forbids: second-guessing the model's
            # structured intent with a runtime keyword check. The model has the
            # conversation context and decides whether "stop worrying about it"
            # is a playback-stop; the playback tool/prompt owns the distinction.

            # Check priority if room tracking is enabled
            should_execute, reason, _error_msg = self.pipeline._room_tracker.check_intent_priority(intent_name, "ai")

            if not should_execute:
                return self.pipeline._room_tracker.create_deferred_result(intent_name, reason)

            if intent_name == "answer" and answer_text is not None:
                # Do NOT speak here — ai_interpreter._handle_gpt_answer() is the
                # single TTS speaker for AI-path answers.  Calling speak() here and
                # then letting the interpreter also call _speak() produces two
                # identical TTS utterances for every question/answer exchange.
                _answer_data: dict[str, Any] = {
                    "message": answer_text,
                    "question": text,
                    "ai_data": ai_result.get("data", {}),
                }
                _cl = ai_result.get("continue_listening")
                if _cl is not None:
                    _answer_data["continue_listening"] = _cl
                _tc = ai_result.get("task_category")
                if _tc:
                    _answer_data["task_category"] = _tc
                # Propagate content card for UI display
                _card = ai_result.get("card")
                if _card and isinstance(_card, dict):
                    _answer_data["card"] = _card
                requires_followup = bool(_cl) if _cl is not None else False
                return PipelineResult(
                    ok=True,
                    intent="answer",
                    data=_answer_data,
                    error=None,
                    source="ai",
                    requires_clarification=requires_followup,
                    policy_flags=["ai_agent"] if _is_agent_result else ["ai_answer"],
                    source_node_id=self.pipeline.node_id,
                )

            # CB-10: Clean any leaked JSON template from the message field
            _ai_message = ai_result.get("message", "")
            if isinstance(_ai_message, str):
                _ai_message = _strip_json_template(_ai_message)

            # If the LLM path returned no useful output, preserve that as
            # structured state instead of inventing a user-facing line here.
            if not _ai_message.strip() and not commands_executed:
                ai_data = ai_result.get("data", {})
                upstream_no_result = ai_data.get("no_result") if isinstance(ai_data, dict) else None
                if not isinstance(upstream_no_result, dict):
                    no_result_payload = build_ai_no_result("empty_ai_response")
                    upstream_no_result = no_result_payload["no_result"]
                    upstream_error_state = no_result_payload["error_state"]
                else:
                    upstream_error_state = ai_data.get("error_state") if isinstance(ai_data, dict) else None
                if not isinstance(upstream_error_state, dict):
                    upstream_error_state = {
                        "type": "ai_no_result",
                        **upstream_no_result,
                    }
                return PipelineResult(
                    ok=False,
                    intent="ai_no_result",
                    data={
                        "ai_data": ai_data if isinstance(ai_data, dict) else {},
                        "commands_executed": [],
                        "params": params,
                        "no_result": upstream_no_result,
                        "error_state": upstream_error_state,
                    },
                    error="ai_no_result",
                    source="ai",
                    requires_clarification=True,
                    policy_flags=["ai_no_result"],
                    source_node_id=self.pipeline.node_id,
                )

            _generic_data: dict[str, Any] = {
                "message": _ai_message,
                "ai_data": ai_result.get("data", {}),
                "commands_executed": commands_executed,
                "params": params,
            }
            _cl_generic = ai_result.get("continue_listening")
            if _cl_generic is not None:
                _generic_data["continue_listening"] = _cl_generic
            _tc_generic = ai_result.get("task_category")
            if _tc_generic:
                _generic_data["task_category"] = _tc_generic
            requires_followup = bool(_cl_generic) if _cl_generic is not None else False
            return PipelineResult(
                ok=bool(ai_result.get("ok", False)),
                intent=intent_name,
                data=_generic_data,
                error=ai_result.get("error"),
                source="ai",
                requires_clarification=requires_followup,
                policy_flags=["ai_processed"],
                source_node_id=self.pipeline.node_id,
            )

        except Exception as e:
            logger.exception("AI processing failed for '%s': %s", text, e)

            # COST-1: settle reservation on error — the provider may not have
            # reached its internal _do_settle() if the exception was thrown
            # before or during the API call.  Settle with actual=0 so the
            # full estimated reservation is refunded.
            try:
                from services.llm.rate_limiter import get_rate_limiter

                await get_rate_limiter().settle(
                    user_id,
                    configured_llm_max_tokens_cap(),
                    0,
                )
            except Exception:
                # Settle-on-error-path failure leaks the full reservation —
                # if it recurs the user gets rate-limited out silently.
                # Promote DEBUG -> ERROR via logger.exception.
                logger.exception(
                    "rate-limiter settle failed in try_ai error path (reservation leaked) user_id=%s",
                    user_id,
                )

            # Detect timeout / connectivity errors and return a helpful message
            # instead of an empty data dict that leaves the user confused.
            # Branch on typed exception classes ONLY -- never on regex/substring
            # of the error text (see scripts/check_no_runtime_error_text_classifier.py
            # for the class of anti-pattern this avoids).  If a transport layer
            # raises something untyped, the generic message path handles it.
            from core.exceptions import (
                CircuitOpenError,
                LLMTimeoutError,
                ProviderUnavailableError,
            )

            _is_connectivity = isinstance(
                e,
                (
                    TimeoutError,
                    LLMTimeoutError,
                    ProviderUnavailableError,
                    CircuitOpenError,
                ),
            )

            if _is_connectivity:
                _user_msg = random.choice(
                    [
                        "I can't reach my AI provider right now — try again in a sec, or check your internet.",
                        "My AI backend isn't responding — give it a moment and try again?",
                        "My backend's down for a sec — try again shortly, or ask me something simpler in the meantime.",
                    ]
                )
                _flags = ["ai_error", "llm_unreachable"]
            else:
                _user_msg = "Something tripped me up on that one — try again?"
                _flags = ["ai_error"]

            return PipelineResult(
                ok=False,
                intent="ai_failed",
                data={"message": _user_msg},
                error=f"AI processing failed: {e!s}",
                source="ai",
                requires_clarification=False,
                policy_flags=_flags,
                source_node_id=self.pipeline.node_id,
            )
        finally:
            if user_context_token is not None:
                from core.user_context import reset_current_user_id

                reset_current_user_id(user_context_token)
