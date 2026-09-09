"""
Command Service Core Execution Logic.

This module contains the core command execution logic
extracted from the main CommandService class to comply with code constraints.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from core.logging_config import get_logger

# Import types from dedicated types module to avoid circular imports
from .types import CommandRequest, CommandResult

logger = get_logger(__name__)

# Channels whose own per-channel formatters render markdown into rich text
# (Telegram -> HTML, Discord/Slack mrkdwn, email HTML). For every other channel
# the reply text is shown or spoken verbatim, so markdown *syntax* must be
# rendered to plain text at this reply boundary or it reaches the user as
# literal "**", "#", "- " artifacts (issue #1406). Only these rich channels
# keep markdown; everything else (web, http, voice, phone, sms, chat_mode,
# and the voice-first default) is rendered to plain text.
_RICH_MARKDOWN_CHANNELS = frozenset({"telegram", "discord", "slack", "email"})
_USER_FACING_TEXT_KEYS = ("message", "response", "answer")


def channel_key(channel: object) -> str:
    """Best-effort channel identifier from a string/dict/channel object.

    Public because the reply boundary is not the only place that has to know
    which channel a command came from: the desktop command route asks the same
    question before deciding whether the reply is spoken aloud on this machine
    (``ui/api/routes/command.py``), and two implementations of "which channel
    is this" would drift.
    """
    if isinstance(channel, Mapping):
        for key in ("origin_channel", "channel", "type", "channel_type"):
            value = channel.get(key)
            if value:
                return str(value).strip().lower()
        return ""
    if isinstance(channel, str):
        return channel.strip().lower()
    channel_type = getattr(channel, "channel_type", None)
    if channel_type:
        return str(channel_type).strip().lower()
    return ""


def _render_display_text(result: CommandResult, channel: object) -> None:
    """Render markdown in the user-facing reply to plain text, in place.

    A presentation-layer render for plain-text/voice channels -- it removes
    markdown markup only, preserving all content, and is skipped for rich
    channels that render markdown themselves (see ``_RICH_MARKDOWN_CHANNELS``).
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not data:
        return
    if channel_key(channel) in _RICH_MARKDOWN_CHANNELS:
        return
    try:
        from intent.response_cleanup import strip_markdown_for_display
    except ImportError:
        logger.debug("Markdown display render unavailable; leaving reply text as-is")
        return
    for key in _USER_FACING_TEXT_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            data[key] = strip_markdown_for_display(value)
    ai_data = data.get("ai_data")
    if isinstance(ai_data, dict):
        answer = ai_data.get("answer")
        if isinstance(answer, str) and answer:
            ai_data["answer"] = strip_markdown_for_display(answer)


class CommandServiceCoreExecutor:
    """Handles core command execution logic."""

    def __init__(self, service_instance):
        """
        Initialize core executor.

        Args:
            service_instance: The CommandService instance
        """
        self.service = service_instance

    async def execute_command(self, request: CommandRequest) -> CommandResult:
        """
        Execute a command through the service.

        Args:
            request: Command request to execute

        Returns:
            Command result
        """
        # Store current request for access in _speak_via_intent
        self.service._current_request = request
        context = self.service.context
        ledger = context.idempotency_ledger
        request_key = request.request_id or request.trace_id

        # Track execution time for diagnostics
        start_time = time.perf_counter()

        # Check idempotency
        if ledger and request_key:
            cached = ledger.lookup(user_id=request.user_id, request_id=request_key)
            if cached is not None:
                logger.info(
                    "Idempotent replay for user_id=%s request_id=%s",
                    request.user_id,
                    request_key,
                )
                return CommandResult.from_serialized(cached)

        # Create finalizer for idempotency recording and command tracing
        def finalize(result: CommandResult) -> CommandResult:
            # Reply boundary: render markdown in the user-facing text to plain
            # text for plain-text/voice channels before it is recorded,
            # returned, or spoken (issue #1406). Rich channels keep markdown.
            _render_display_text(result, request.channel)
            if ledger and request_key:
                try:
                    ledger.record(
                        user_id=request.user_id,
                        request_id=request_key,
                        result_payload=result.serialize(),
                    )
                except Exception as e:  # pragma: no cover - defensive
                    logger.debug("Failed to persist idempotent result: %s", e, exc_info=True)

            # Record command trace for AI debugging
            try:
                from diagnostics.ai_debugger import record_command_trace

                duration_ms = (time.perf_counter() - start_time) * 1000
                response_summary = ""
                if result.data:
                    msg = result.data.get("message") or result.data.get("response")
                    if msg:
                        response_summary = str(msg)[:200]
                    elif result.ok:
                        response_summary = f"OK: {result.intent}"
                    else:
                        response_summary = result.error or "Unknown error"

                record_command_trace(
                    user_id=request.user_id,
                    command_text=request.text,
                    intent_type=result.intent,
                    success=result.ok,
                    response_summary=response_summary,
                    duration_ms=round(duration_ms, 2),
                    error=result.error,
                )
            except Exception as e:
                # Tracing should never break command execution
                logger.debug("Failed to record command trace: %s", e, exc_info=True)

            return result

        try:
            # Validate and normalize input
            text = self.service.validator.normalize(request.text)
        except Exception as exc:
            from .command_service_validation import CommandValidationError

            if isinstance(exc, CommandValidationError):
                error_msg = exc.message
            else:
                logger.exception("Unexpected error during command validation")
                error_msg = "Something went wrong, please try again."
            return finalize(
                CommandResult(
                    ok=False,
                    intent="unknown",
                    data={},
                    error=error_msg,
                    status_code=400,
                )
            )

        try:
            from intent.input_prefilter import CRISIS_RESPONSE, detect_crisis

            if detect_crisis(text):
                logger.warning("Crisis language detected before command pipeline execution")
                return finalize(
                    CommandResult(
                        ok=True,
                        intent="crisis",
                        data={"message": CRISIS_RESPONSE},
                        error=None,
                        status_code=200,
                        policy_flags=["crisis_detected"],
                    )
                )
        except Exception as exc:
            logger.exception("Crisis prefilter failed: %s", exc)

        # Update statistics
        if context.commands_total is not None:
            # Counter.inc() expects label keyword args, e.g. channel="typed"
            context.commands_total.inc(channel="typed")

        # Execute through pipeline
        return await self._execute_through_pipeline(request, text, finalize)

    async def _execute_through_pipeline(self, request: CommandRequest, text: str, finalize_func) -> CommandResult:
        """
        Execute command through the processing pipeline.

        Args:
            request: Original command request
            text: Normalized command text
            finalize_func: Function to finalize result

        Returns:
            Command result
        """
        try:
            from diagnostics import latency_spans

            with latency_spans.span("PIPELINE", text_len=len(text)):
                result = await self._execute_unified_pipeline(request, text)
            return finalize_func(result)

        except Exception as e:
            logger.exception("Pipeline execution failed: %s", e)
            return finalize_func(
                CommandResult(
                    ok=False,
                    intent="unknown",
                    data={},
                    error="Something went wrong, please try again.",
                    status_code=500,
                )
            )

    async def _execute_unified_pipeline(self, request: CommandRequest, text: str) -> CommandResult:
        """Execute through unified intent pipeline."""
        try:
            # Use the intent interpreter from context (already configured with all dependencies)
            context = self.service.context
            intent_interpreter = context.intent

            if intent_interpreter is None:
                logger.error("No intent interpreter available")
                return CommandResult(
                    ok=False,
                    intent="unknown",
                    data={},
                    error="Intent interpreter not configured",
                    status_code=500,
                )

            # Prefer IntentPipeline.process() over IntentBridge.interpret().
            # The pipeline includes multi-step plan detection and conversation
            # state tracking that IntentBridge.interpret() bypasses.
            pipeline = getattr(intent_interpreter, "_pipeline", None)
            if pipeline is not None and hasattr(pipeline, "process"):
                return await self._execute_via_pipeline(pipeline, request, text)

            # Fallback: interpret through IntentBridge (legacy path)
            interpreted = await intent_interpreter.interpret(text)

            # Check if interpreted is a dict with response data
            # IntentBridge.interpret returns ResponseEnvelope: {"ok": bool, "error": ..., "data": {...}}
            if isinstance(interpreted, dict):
                # Extract envelope-level fields
                ok = interpreted.get("ok", True)
                error = interpreted.get("error")
                data = interpreted.get("data", interpreted)

                # Handle ResponseEnvelope format where data contains the actual result
                # data is {"type": "answer", "params": {"message": "...", "spoken": ...}}
                # or {"type": "command_type", "params": {...}}
                if isinstance(data, dict):
                    # Extract intent type from data (where IntentBridge puts it)
                    intent_type = data.get("type") or interpreted.get("type") or interpreted.get("intent") or "unknown"

                    # Surface original_intent from knowledge resolver results.
                    # When Phase 2.5 handles a query, the pipeline sets
                    # original_intent to "knowledge_direct"/"knowledge_enrich"
                    # inside params.  Promote it so the API response
                    # distinguishes Phase 2.5 from Phase 3.
                    params = data.get("params", {})
                    if isinstance(params, dict):
                        original_intent = params.get("original_intent")
                        if isinstance(original_intent, str) and original_intent.startswith("knowledge_"):
                            intent_type = original_intent
                            data["source"] = "knowledge"
                            data["already_executed"] = True

                    # Extract message from multiple possible locations
                    # Priority: params.message > message > response > answer
                    message = None
                    if isinstance(params, dict):
                        message = params.get("message") or params.get("answer")
                    if not message:
                        message = data.get("message") or data.get("response") or data.get("answer")
                    if not message:
                        message = interpreted.get("message") or interpreted.get("response")

                    # CB-10 defense-in-depth: strip leaked JSON/dict artifacts
                    # from the message before returning to the API layer.
                    if isinstance(message, str) and message:
                        try:
                            from intent.response_cleanup import (
                                strip_json_template as _strip_json_template,
                            )

                            message = _strip_json_template(message)
                        except Exception:
                            logger.debug("JSON template strip failed, using original message")

                    # Ensure message is in data for CommandPipeline to find
                    if message and "message" not in data:
                        data["message"] = message
                    # Also update params.message if it exists (defense-in-depth)
                    if isinstance(message, str) and isinstance(params, dict) and "message" in params:
                        params["message"] = message
                else:
                    intent_type = interpreted.get("type") or interpreted.get("intent") or "unknown"
                    message = None

                return CommandResult(
                    ok=ok if error is None else False,
                    intent=intent_type,
                    data=data if isinstance(data, dict) else {"result": data},
                    error=error,
                    status_code=200 if ok else 400,
                )
            else:
                # Handle non-dict response
                return CommandResult(
                    ok=True,
                    intent="unknown",
                    data={"result": str(interpreted)},
                    error=None,
                    status_code=200,
                )

        except Exception as e:
            logger.exception("Unified pipeline execution failed: %s", e)
            raise

    async def _execute_via_pipeline(self, pipeline: object, request: CommandRequest, text: str) -> CommandResult:
        """Execute through IntentPipeline.process() for full planning support.

        This path preserves multi-step plan detection, conversation state
        tracking, and plan refinement that IntentBridge.interpret() bypasses.
        """
        history = list(request.history) if isinstance(request.history, list) else None
        _uid = str(getattr(request, "user_id", "") or "").strip()
        if not _uid:
            raise ValueError("CommandRequest.user_id is required for pipeline execution")
        channel = self._resolve_pipeline_channel(getattr(request, "channel", None))
        result = await pipeline.process(text, history=history, channel=channel, user_key=_uid)  # type: ignore[union-attr]

        # Map PipelineResult fields to CommandResult
        data: dict[str, object] = dict(result.data) if result.data else {}

        # Surface the message for the API envelope
        message = data.get("message")
        has_structured_failure = any(key in data for key in ("no_result", "error_state", "cap_state"))
        if not message and result.error and not has_structured_failure:
            # Do not expose raw error strings to users - use plain English
            message = "Something went wrong, please try again."
        if isinstance(message, str) and message:
            try:
                from intent.response_cleanup import (
                    strip_json_template as _strip_json_template,
                )

                message = _strip_json_template(message)
                data["message"] = message
            except Exception:
                logger.debug("JSON template strip (fallback path) failed, using original message")

        return CommandResult(
            ok=result.ok,
            intent=result.intent or "unknown",
            data=data,
            error=result.error,
            status_code=200 if result.ok else 400,
            requires_clarification=getattr(result, "requires_clarification", False),
            policy_flags=list(getattr(result, "policy_flags", [])),
        )

    def _resolve_pipeline_channel(self, raw_channel: object | None) -> object | None:
        """Normalize request channel metadata into a real MessageChannel.

        REST callers often pass string channel identifiers (``"http"``,
        ``"voice-stream"``, ``"telegram"``). The pipeline expects a
        MessageChannel-like object so downstream prompt wiring and TTS
        suppression see the correct channel type. The resolution logic is the
        shared ``chat.channel.resolve_rest_channel`` factory so the desktop and
        cloud REST paths never drift on how a channel string maps to an object.
        """
        from chat.channel import resolve_rest_channel

        return resolve_rest_channel(raw_channel)
