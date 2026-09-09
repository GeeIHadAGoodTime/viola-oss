"""
Aggressive and Complete AI Question/Response Debugging System
=============================================================

Provides thorough, aggressive, and complete ability to debug AI questions and responses.

Features:
- End-to-end tracking from user input to UI display
- Captures data at every stage of the pipeline
- Modular and plugin-friendly architecture
- File-based logging for offline analysis
- Real-time event bus integration
- Zero performance impact when disabled

Usage:
    from utils.ai_debug_tracer import get_ai_debug_tracer

    tracer = get_ai_debug_tracer()
    with tracer.trace_question("What's the weather?"):
        # Your code here
        result = await process_question(text)
        tracer.log_response(result)
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from config import env
from core.json_types import JSONObject, JSONValue, to_json_object, to_json_value
from core.logging_config import get_logger
from core.platform import get_logs_dir

logger = get_logger(__name__)

# Context variable for trace ID propagation across async/thread boundaries
trace_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("ai_trace_id", default=None)

_QT_EVENT_BUS_CLS: type[object] | None = None
try:  # pragma: no cover - optional dependency
    from PySide6.QtCore import QObject, Signal

    QT_AVAILABLE = True

    class _AIEventBusQObject(QObject):
        question_received = Signal(str, dict)  # question, metadata
        question_sent_to_api = Signal(str, dict)  # question, request_data
        response_received = Signal(str, dict)  # question, response_data
        response_processed = Signal(str, dict)  # question, processed_data
        response_extracted = Signal(str, str)  # question, extracted_message
        response_displayed = Signal(str, str)  # question, displayed_message
        error_occurred = Signal(str, str, dict)  # question, error, context

        stage_entered = Signal(str, str, dict)  # stage_name, trace_id, data
        stage_exited = Signal(str, str, dict)  # stage_name, trace_id, result
        data_captured = Signal(str, str, dict)  # stage_name, trace_id, data

    _QT_EVENT_BUS_CLS = _AIEventBusQObject

except ImportError:  # pragma: no cover - optional dependency
    QT_AVAILABLE = False


class _Signal(Protocol):
    def emit(self, *args: object) -> None: ...


class _QtAIEventBus(Protocol):
    question_received: _Signal
    question_sent_to_api: _Signal
    response_received: _Signal
    response_processed: _Signal
    response_extracted: _Signal
    response_displayed: _Signal
    error_occurred: _Signal
    stage_entered: _Signal
    stage_exited: _Signal
    data_captured: _Signal


class _NoopSignal:
    def emit(self, *args: object) -> None:
        return None


class AIEventBus:
    """Qt-backed event bus when available, otherwise no-op signals."""

    question_received: _Signal
    question_sent_to_api: _Signal
    response_received: _Signal
    response_processed: _Signal
    response_extracted: _Signal
    response_displayed: _Signal
    error_occurred: _Signal
    stage_entered: _Signal
    stage_exited: _Signal
    data_captured: _Signal

    def __init__(self) -> None:
        if QT_AVAILABLE and _QT_EVENT_BUS_CLS is not None:
            qt = cast(_QtAIEventBus, _QT_EVENT_BUS_CLS())
            self._qt: object | None = qt
            self.question_received = qt.question_received
            self.question_sent_to_api = qt.question_sent_to_api
            self.response_received = qt.response_received
            self.response_processed = qt.response_processed
            self.response_extracted = qt.response_extracted
            self.response_displayed = qt.response_displayed
            self.error_occurred = qt.error_occurred
            self.stage_entered = qt.stage_entered
            self.stage_exited = qt.stage_exited
            self.data_captured = qt.data_captured
            return

        noop = _NoopSignal()
        self._qt = None
        self.question_received = noop
        self.question_sent_to_api = noop
        self.response_received = noop
        self.response_processed = noop
        self.response_extracted = noop
        self.response_displayed = noop
        self.error_occurred = noop
        self.stage_entered = noop
        self.stage_exited = noop
        self.data_captured = noop


class DebugLevel(Enum):
    """Debug detail levels"""

    NONE = "none"  # Disabled
    MINIMAL = "minimal"  # Only errors and critical events
    STANDARD = "standard"  # Standard debugging (default)
    VERBOSE = "verbose"  # Everything including data dumps
    AGGRESSIVE = "aggressive"  # Maximum detail, data dumps, timing


@dataclass
class QuestionTrace:
    """Complete trace of a single AI question/response"""

    trace_id: str
    timestamp: str
    question: str

    # Stage 1: User Input
    input_stage: JSONObject = field(default_factory=dict)

    # Stage 2: API Client
    api_client_stage: JSONObject = field(default_factory=dict)

    # Stage 3: Server/Backend
    server_stage: JSONObject = field(default_factory=dict)

    # Stage 4: Intent Pipeline
    intent_stage: JSONObject = field(default_factory=dict)

    # Stage 5: GPT Handler / AI Controller
    ai_stage: JSONObject = field(default_factory=dict)

    # Stage 6: Response Processing
    response_stage: JSONObject = field(default_factory=dict)

    # Stage 7: UI Display
    ui_stage: JSONObject = field(default_factory=dict)

    # Final response
    final_response: str | None = None
    response_displayed: bool = False
    error: str | None = None

    # Timing
    timings: dict[str, float] = field(default_factory=dict)
    total_duration_ms: float | None = None


def _as_str_key_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        out[key] = item
    return out


def _extract_message_from_response(response: object) -> str | None:
    if isinstance(response, str):
        return response

    response_dict = _as_str_key_dict(response)
    if response_dict is None:
        return None

    direct_message = response_dict.get("message")
    if isinstance(direct_message, str):
        return direct_message

    for key in ("response", "answer"):
        value = response_dict.get(key)
        if isinstance(value, str):
            return value

    data = response_dict.get("data")
    data_dict = _as_str_key_dict(data)
    if data_dict is not None:
        nested_message = data_dict.get("message")
        if isinstance(nested_message, str):
            return nested_message
        return str(data_dict)

    if data is not None:
        return str(data)

    return None


class AIDebugTracer:
    """
    Aggressive AI question/response debug tracer.

    Tracks every stage of AI question processing from user input to UI display.
    Modular, plugin-friendly, zero overhead when disabled.
    """

    def __init__(
        self,
        enabled: bool | None = None,
        debug_level: DebugLevel | None = None,
        log_dir: Path | None = None,
        max_traces: int = 100,
        enable_file_logging: bool = True,
        enable_event_bus: bool = True,
    ):
        """
        Initialize AI debug tracer.

        Args:
            enabled: Override enabled state (default: check env vars)
            debug_level: Debug detail level (default: from env)
            log_dir: Directory for trace files (default: logs/ai_debug/)
            max_traces: Maximum traces to keep in memory
            enable_file_logging: Whether to write trace files
            enable_event_bus: Whether to emit Qt signals (if available)
        """
        # Determine enabled state
        # NOTE: Using env.get() here is acceptable per CLAUDE.md - these are temporary
        # debug flags for AI tracing diagnostics, not core application settings.
        # See CLAUDE.md section 2: "Direct env.get() should only be used for temporary debug flags"
        if enabled is None:
            self.enabled = env.get("VIOLA_AI_DEBUG", "false").lower() in (
                "true",
                "1",
                "yes",
                "on",
            )
        else:
            self.enabled = enabled

        # Determine debug level
        if debug_level is None:
            level_str = env.get("VIOLA_AI_DEBUG_LEVEL", "standard").lower()
            self.debug_level = (
                DebugLevel(level_str) if level_str in [e.value for e in DebugLevel] else DebugLevel.STANDARD
            )
        else:
            self.debug_level = debug_level

        # Only enable if explicitly enabled
        if not self.enabled:
            self.debug_level = DebugLevel.NONE

        self.log_dir = log_dir or get_logs_dir() / "ai_debug"
        self.max_traces = max_traces
        self.enable_file_logging = enable_file_logging and self.enabled
        self.enable_event_bus = enable_event_bus and self.enabled and QT_AVAILABLE

        # In-memory trace storage
        self._traces: dict[str, QuestionTrace] = {}
        self._lock = threading.RLock()

        # Event bus (optional)
        self._event_bus: AIEventBus | None = None
        if self.enable_event_bus:
            try:
                self._event_bus = AIEventBus()
                logger.info("AI Debug Event Bus initialized (Qt signals enabled)")
            except Exception as e:
                logger.warning("Failed to initialize AI Debug Event Bus: %s", e)
                self._event_bus = None

        # Setup file logging
        if self.enable_file_logging:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            logger.info("AI Debug file logging enabled: %s", self.log_dir)

        if self.enabled:
            logger.info("AI Debug Tracer ENABLED (level: %s)", self.debug_level.value)
        else:
            logger.debug("AI Debug Tracer disabled (set VIOLA_AI_DEBUG=true to enable)")

    @contextmanager
    def trace_question(self, question: str, metadata: Mapping[str, object] | None = None) -> Iterator[TraceContext]:
        """
        Context manager to trace a complete AI question/response cycle.

        Usage:
            with tracer.trace_question("What's the weather?") as trace:
                result = await process_question(question)
                trace.log_response(result)
        """
        trace_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        trace = QuestionTrace(trace_id=trace_id, timestamp=datetime.now().isoformat(), question=question)

        if metadata:
            trace.input_stage.update(to_json_object(metadata))

        try:
            # Store trace
            with self._lock:
                self._traces[trace_id] = trace
                # Trim old traces
                if len(self._traces) > self.max_traces:
                    oldest_id = min(self._traces.keys(), key=lambda k: self._traces[k].timestamp)
                    del self._traces[oldest_id]

            # Log question received
            if self.debug_level != DebugLevel.NONE:
                logger.debug(
                    "[AI-DEBUG] Question received: '%s...' (trace_id: %s)",
                    question[:50],
                    trace_id,
                )

            # Emit event
            if self._event_bus:
                try:
                    self._event_bus.question_received.emit(question, {"trace_id": trace_id, "metadata": metadata or {}})
                except Exception as e:
                    logger.debug("Failed to emit question_received event: %s", e, exc_info=True)
                    pass

            # Set context variable for trace ID propagation
            trace_id_token = trace_id_ctx.set(trace_id)

            try:
                # Create trace context
                trace_context = TraceContext(self, trace, start_time)

                yield trace_context
            finally:
                # Restore previous context
                trace_id_ctx.reset(trace_id_token)

            # Calculate total duration
            trace.total_duration_ms = (time.time() - start_time) * 1000

            # Save trace file if enabled
            if self.enable_file_logging:
                self._save_trace_file(trace)

            # Final log
            if self.debug_level != DebugLevel.NONE:
                if trace.error:
                    logger.error(
                        "[AI-DEBUG] Question failed: '%s...' (trace_id: %s, duration: %.1fms)",
                        question[:50],
                        trace_id,
                        trace.total_duration_ms,
                    )
                elif trace.response_displayed:
                    logger.debug(
                        "[AI-DEBUG] Question completed: '%s...' -> '%s...' (trace_id: %s, duration: %.1fms)",
                        question[:50],
                        (trace.final_response[:50] if trace.final_response else "NO RESPONSE"),
                        trace_id,
                        trace.total_duration_ms,
                    )
                else:
                    logger.warning(
                        "[AI-DEBUG] Question processed but response NOT displayed: '%s...' (trace_id: %s, duration: %.1fms)",
                        question[:50],
                        trace_id,
                        trace.total_duration_ms,
                    )

        except Exception as e:
            trace.error = str(e)
            logger.exception("[AI-DEBUG] Trace context error: %s", e)
            raise

    def log_stage(
        self,
        trace_id: str,
        stage_name: str,
        data: Mapping[str, object],
        timing_ms: float | None = None,
    ) -> None:
        """
        Log data for a specific stage in the pipeline.

        Args:
            trace_id: Trace ID from trace_question()
            stage_name: Stage name (e.g., "api_client", "server", "gpt_handler")
            data: Stage-specific data to log
            timing_ms: Optional timing for this stage
        """
        if not self.enabled:
            return

        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                logger.warning("[AI-DEBUG] Unknown trace_id: %s", trace_id)
                return

            # Store stage data
            stage_dict: JSONObject = {
                "timestamp": datetime.now().isoformat(),
                "data": to_json_object(data),
            }

            if timing_ms is not None:
                stage_dict["timing_ms"] = timing_ms
                trace.timings[stage_name] = timing_ms

            # Map stage name to trace field
            stage_field_map = {
                "input": "input_stage",
                "api_client": "api_client_stage",
                "server": "server_stage",
                "intent": "intent_stage",
                "ai": "ai_stage",
                "gpt_handler": "ai_stage",
                "ai_controller": "ai_stage",
                "response": "response_stage",
                "ui": "ui_stage",
            }

            field_name = stage_field_map.get(stage_name.lower(), f"{stage_name}_stage")
            if hasattr(trace, field_name):
                stage_field = getattr(trace, field_name)
                if isinstance(stage_field, dict):
                    stage_field.update(stage_dict)

            # Log based on debug level
            if self.debug_level in [DebugLevel.VERBOSE, DebugLevel.AGGRESSIVE]:
                logger.debug(
                    "[AI-DEBUG] [%s] trace_id=%s data=%s",
                    stage_name.upper(),
                    trace_id,
                    json.dumps(to_json_object(data), default=str)[:200],
                )

            # Emit event
            if self._event_bus:
                try:
                    stage_payload = to_json_object(data)
                    self._event_bus.stage_entered.emit(stage_name, trace_id, stage_payload)
                    self._event_bus.data_captured.emit(stage_name, trace_id, stage_payload)
                except Exception as e:
                    logger.debug("Failed to emit stage events: %s", e, exc_info=True)
                    return

    def log_response(self, trace_id: str, response: object, displayed: bool = False) -> None:
        """
        Log the final response.

        Args:
            trace_id: Trace ID from trace_question()
            response: Response data (dict, string, or any)
            displayed: Whether response was displayed in UI
        """
        if not self.enabled:
            return

        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                return

            message = _extract_message_from_response(response)

            trace.final_response = message
            trace.response_displayed = displayed
            trace.response_stage.update(
                {
                    "response": to_json_value(response if isinstance(response, dict) else {"message": message}),
                    "displayed": displayed,
                    "timestamp": datetime.now().isoformat(),
                }
            )

            # Log
            if self.debug_level != DebugLevel.NONE:
                if displayed:
                    logger.debug(
                        "[AI-DEBUG] Response displayed: '%s...' (trace_id: %s)",
                        message[:100] if message else "NO MESSAGE",
                        trace_id,
                    )
                else:
                    logger.warning(
                        "[AI-DEBUG] Response NOT displayed: '%s...' (trace_id: %s)",
                        message[:100] if message else "NO MESSAGE",
                        trace_id,
                    )

            # Emit events
            if self._event_bus:
                try:
                    self._event_bus.response_received.emit(
                        trace.question,
                        {"trace_id": trace_id, "response": to_json_value(response)},
                    )
                    self._event_bus.response_extracted.emit(trace.question, message or "NO MESSAGE")
                    if displayed:
                        self._event_bus.response_displayed.emit(trace.question, message or "NO MESSAGE")
                except Exception as e:
                    logger.debug("Failed to emit response events: %s", e, exc_info=True)
                    return

    def log_error(self, trace_id: str, error: str, context: Mapping[str, object] | None = None) -> None:
        """
        Log an error during processing.

        Args:
            trace_id: Trace ID from trace_question()
            error: Error message
            context: Optional error context
        """
        if not self.enabled:
            return

        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                return

            trace.error = error
            if context:
                trace.response_stage["error_context"] = to_json_object(context)

            logger.error("[AI-DEBUG] Error in trace_id=%s: %s", trace_id, error)

            if self._event_bus:
                try:
                    self._event_bus.error_occurred.emit(trace.question, error, to_json_object(context or {}))
                except Exception as e:
                    logger.debug("Failed to emit error_occurred event: %s", e, exc_info=True)
                    return

    def get_trace(self, trace_id: str) -> QuestionTrace | None:
        """Get a trace by ID."""
        with self._lock:
            return self._traces.get(trace_id)

    def get_recent_traces(self, limit: int = 10) -> list[QuestionTrace]:
        """Get recent traces."""
        with self._lock:
            traces = sorted(self._traces.values(), key=lambda t: t.timestamp, reverse=True)
            return traces[:limit]

    def get_failed_traces(self) -> list[QuestionTrace]:
        """Get traces that failed or didn't display responses."""
        with self._lock:
            return [trace for trace in self._traces.values() if trace.error or not trace.response_displayed]

    def _save_trace_file(self, trace: QuestionTrace) -> None:
        """Save trace to JSON file."""
        if not self.enable_file_logging:
            return

        try:
            filename = f"ai_trace_{trace.trace_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            filepath = self.log_dir / filename

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(asdict(trace), f, indent=2, default=str)

            if self.debug_level == DebugLevel.AGGRESSIVE:
                logger.debug("[AI-DEBUG] Trace saved: %s", filepath)
        except Exception as e:
            logger.warning("Failed to save trace file: %s", e)

    def get_event_bus(self) -> AIEventBus | None:
        """Get the event bus (for Qt signal subscriptions)."""
        return self._event_bus

    def enable(self) -> None:
        """Enable tracing (if disabled)."""
        self.enabled = True
        if self.debug_level == DebugLevel.NONE:
            self.debug_level = DebugLevel.STANDARD
        logger.info("AI Debug Tracer enabled")

    def disable(self) -> None:
        """Disable tracing."""
        self.enabled = False
        logger.info("AI Debug Tracer disabled")


class TraceContext:
    """Context object returned by trace_question()."""

    def __init__(self, tracer: AIDebugTracer, trace: QuestionTrace, start_time: float):
        self.tracer = tracer
        self.trace = trace
        self.start_time = start_time

    def log_stage(
        self,
        stage_name: str,
        data: Mapping[str, object],
        timing_ms: float | None = None,
    ) -> None:
        """Log stage data (convenience method)."""
        self.tracer.log_stage(self.trace.trace_id, stage_name, data, timing_ms)

    def log_response(self, response: object, displayed: bool = False) -> None:
        """Log response (convenience method)."""
        self.tracer.log_response(self.trace.trace_id, response, displayed)

    def log_error(self, error: str, context: Mapping[str, object] | None = None) -> None:
        """Log error (convenience method)."""
        self.tracer.log_error(self.trace.trace_id, error, context)

    @property
    def trace_id(self) -> str:
        """Get trace ID."""
        return self.trace.trace_id


# Global singleton instance
_tracer_instance: AIDebugTracer | None = None
_tracer_lock = threading.RLock()


def get_ai_debug_tracer() -> AIDebugTracer:
    """
    Get the global AI debug tracer instance (singleton).

    Usage:
        from utils.ai_debug_tracer import get_ai_debug_tracer

        tracer = get_ai_debug_tracer()
        with tracer.trace_question("What's the weather?") as trace:
            result = await process_question(question)
            trace.log_response(result)
    """
    global _tracer_instance

    with _tracer_lock:
        if _tracer_instance is None:
            _tracer_instance = AIDebugTracer()
        return _tracer_instance


def get_current_trace_id() -> str | None:
    """
    Get current trace ID from context (for use across async/thread boundaries).

    Usage:
        from utils.ai_debug_tracer import get_current_trace_id
        trace_id = get_current_trace_id()
    """
    return trace_id_ctx.get()


# Convenience decorator
def _extract_question_text(args: tuple[object, ...], kwargs: Mapping[str, object]) -> str:
    if args:
        return str(args[0])

    value = kwargs.get("text") or kwargs.get("question")
    return str(value) if value is not None else "unknown"


def trace_ai_question(func: Callable[..., object]) -> Callable[..., object]:
    """
    Decorator to automatically trace AI questions.

    Usage:
        @trace_ai_question
        async def process_question(text: str):
            ...
    """
    import asyncio
    import functools

    if asyncio.iscoroutinefunction(func):
        async_func = cast(Callable[..., Awaitable[object]], func)

        @functools.wraps(func)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            tracer = get_ai_debug_tracer()
            question = _extract_question_text(args, kwargs)

            with tracer.trace_question(question) as trace:
                start_time = time.time()
                try:
                    result = await async_func(*args, **kwargs)
                except Exception as exc:
                    trace.log_error(str(exc), {"function": func.__name__})
                    raise

                timing_ms = (time.time() - start_time) * 1000
                trace.log_stage("function", {"result": result, "timing_ms": timing_ms})
                trace.log_response(result)
                return result

        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: object, **kwargs: object) -> object:
        tracer = get_ai_debug_tracer()
        question = _extract_question_text(args, kwargs)

        with tracer.trace_question(question) as trace:
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                trace.log_error(str(exc), {"function": func.__name__})
                raise

            timing_ms = (time.time() - start_time) * 1000
            trace.log_stage("function", {"result": result, "timing_ms": timing_ms})
            trace.log_response(result)
            return result

    return sync_wrapper
