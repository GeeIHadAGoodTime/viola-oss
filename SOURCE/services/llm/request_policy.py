"""Shared provider request policy for LLM calls.

This module is the single runtime policy layer for provider retries, timeouts,
rate-limit handling, model fallback signalling, and structured failure
envelopes. Provider adapters still translate payloads, but retry semantics live
here.

Claude Code parity: this module implements the same state machine as
``src/services/api/withRetry.ts`` from the Claude Code source. The functions
:func:`execute_with_policy` and :func:`collect_stream_with_policy` are the
Python equivalent of the TypeScript ``withRetry`` async generator. Notable
parity surfaces (file refs below point to the source-of-truth TypeScript):

- ``DEFAULT_MAX_RETRIES = 10`` (withRetry.ts:52)
- ``FLOOR_OUTPUT_TOKENS = 3000`` (withRetry.ts:53)
- ``MAX_529_RETRIES = 3`` (withRetry.ts:54)
- ``BASE_DELAY_MS = 500`` (withRetry.ts:55)
- Foreground 529 retry set (withRetry.ts:62-82)
- ``FallbackTriggeredError`` (withRetry.ts:160-168) — propagates to the agent
  loop so that partial assistant/tool state is cleared and the active model
  is swapped (query.ts:893-950).
- Max-token context-overflow parser (withRetry.ts:550-595) — sets
  ``RetryContext.max_tokens_override`` so the next attempt can fit.
- ``refresh_client`` callback (withRetry.ts:170-251) — fires on 401 / OAuth
  revoke / stale connection.
- Persistent retry heartbeat (withRetry.ts:477-503) — chunked sleeps with
  yielded retry events so the host stays awake during long capacity cascades.
"""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from core.exceptions import CostCircuitBreakerError, LLMQuotaExceededError, RateLimitError
from core.logging_config import get_logger
from services.conversation.context_frames import Frame, FrameKind, system_reminder_frame

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Claude Code parity constants (withRetry.ts:52-55)
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES = 10
"""Claude default. Override per-call via ``ProviderRequestContext.max_retries``."""

MAX_529_RETRIES = 3
"""Consecutive 529 overload errors before triggering model fallback."""

BASE_DELAY_MS = 500
"""Base delay for exponential backoff; jittered up to ``max_delay_s``."""

FLOOR_OUTPUT_TOKENS = 3000
"""Minimum output budget when shrinking ``max_tokens`` after context overflow."""

PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000  # 5 min cap per attempt
PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000  # 6 hour total reset cap
HEARTBEAT_INTERVAL_MS = 30_000  # heartbeat retry-event cadence

ProviderRetryAction = Literal["retry", "fallback", "fail"]
ProviderStatus = Literal[
    "ok",
    "failed",
    "fallback",
    "interrupted",
    "timeout",
    "rate_limited",
    "cost_limited",
    "quota_limited",
]
ProviderErrorCategory = Literal[
    "abort",
    "auth",
    "bad_request",
    "billing",
    "context_length",
    "cost_limit",
    "quota_limit",
    "rate_limit",
    "overloaded",
    "server",
    "timeout",
    "transport",
    "unknown",
]

# Claude Code parity: foreground 529 retry set (withRetry.ts:62-82).
# Includes Claude's repl_main_thread:outputStyle:* variants and auto_mode +
# bash_classifier, plus Viola-specific sources (voice_command, agent_loop,
# route_command, ask). Sources NOT in this set drop 529s immediately to avoid
# capacity-cascade amplification (each retry is 3-10x gateway pressure during
# a real outage).
_FOREGROUND_529_RETRY_SOURCES = frozenset(
    {
        # Claude-shared sources
        "repl_main_thread",
        "repl_main_thread:outputStyle:custom",
        "repl_main_thread:outputStyle:Explanatory",
        "repl_main_thread:outputStyle:Learning",
        "sdk",
        "agent:custom",
        "agent:default",
        "agent:builtin",
        "compact",
        "hook_agent",
        "hook_prompt",
        "verification_agent",
        "side_question",
        "auto_mode",
        "bash_classifier",
        # Viola-specific sources
        "voice_command",
        "agent_loop",
        "route_command",
        "ask",
    }
)


@dataclass
class RetryContext:
    """Per-attempt mutable state shared with the provider call.

    Claude Code parity: ``withRetry.ts:120-125`` ``RetryContext`` interface.
    The provider operation reads ``max_tokens_override`` after a 400 context
    overflow on the previous attempt and replays with the adjusted ceiling.

    ``model`` is duplicated from the request context so a future fallback
    inside the loop (e.g. soft-Claude downgrade) can be observed without
    rebuilding the whole request.
    """

    model: str
    max_tokens_override: int | None = None
    thinking_budget_tokens: int = 0
    fast_mode: bool = False
    # Free-form per-attempt scratchpad — providers may stash anything here
    # (e.g. previous_response_id rotation hints). Keep small.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRequestContext:
    provider: str
    model: str
    session_id: str | None
    request_id: str
    stream: bool
    timeout_s: float
    cost_budget: Mapping[str, Any] | None = None
    fallback_allowed: bool = False
    source: str | None = None
    query_kind: Literal["foreground", "background"] = "foreground"
    abort_signal: Any | None = None
    # Claude default is 10 (withRetry.ts:52). Viola callers historically passed
    # max_retries=2; keep that as the zero-arg default to preserve existing
    # behavior. Callers that want Claude-style 10 should pass max_retries=10
    # explicitly (or DEFAULT_MAX_RETRIES).
    max_retries: int = 2
    rate_limit_retry_budget: int = 2
    overload_retry_budget: int = MAX_529_RETRIES
    base_delay_s: float = BASE_DELAY_MS / 1000.0
    max_delay_s: float = 32.0
    retry_jitter_fraction: float = 0.25
    stream_idle_timeout_s: float | None = 90.0
    nonstreaming_fallback: bool = False
    fallback_model: str | None = None
    initial_overload_errors: int = 0
    on_retry: Callable[[ProviderAttemptContext, BaseException, ProviderRetryDecision], Any] | None = None
    # Claude parity (withRetry.ts:170-251). Fires before the next attempt when
    # the previous error is 401 / OAuth revoke / stale ECONNRESET; providers
    # use this to rebuild their client / refresh auth tokens. Return value is
    # ignored. Signature: ``async refresh_client(error) -> None``.
    refresh_client: Callable[[BaseException | None], Awaitable[None] | None] | None = None
    # Claude parity (withRetry.ts:120-125). The state machine constructs a
    # ``RetryContext`` and passes it to the operation on every attempt. Most
    # callers don't need it; opt-in by setting ``pass_retry_context=True`` and
    # accepting a positional ``retry_context`` argument on the call.
    pass_retry_context: bool = False
    thinking_budget_tokens: int = 0
    fast_mode: bool = False
    # Claude parity: persistent retry mode (withRetry.ts:96-104, 477-503).
    # Used by long-running unattended sessions — 429/529 retries indefinitely
    # with 5-min cap per attempt and HEARTBEAT_INTERVAL_MS retry events to keep
    # the host alive. Off by default.
    persistent_retry: bool = False
    # FallbackTriggeredError propagation (withRetry.ts:160-168, query.ts:893-950).
    # When True and the consecutive 529 budget is exhausted with a configured
    # fallback_model, raise FallbackTriggeredError instead of returning a
    # fallback envelope. The query-loop / router catches it, swaps the active
    # model, clears partial assistant/tool state, and retries.
    raise_fallback_triggered: bool = False


@dataclass(frozen=True)
class ProviderAttemptContext:
    context: ProviderRequestContext
    attempt: int
    model: str
    timeout_s: float


@dataclass(frozen=True)
class ProviderRetryDecision:
    action: ProviderRetryAction
    delay_s: float = 0.0
    reason: str | None = None
    next_provider: str | None = None


@dataclass
class ProviderResponseEnvelope:
    status: ProviderStatus
    frames: list[Frame] | None = None
    usage: Mapping[str, Any] | None = None
    raw_error: BaseException | None = None
    interrupted: bool = False
    value: Any = None
    error_category: ProviderErrorCategory | None = None
    stop_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def raise_for_status(self) -> None:
        if self.ok:
            return
        exc: BaseException = self.raw_error or ProviderRequestPolicyError(self)
        try:
            exc.viola_provider_envelope = self.to_metadata()  # type: ignore[attr-defined]
            exc.viola_provider_stop_reason = self.stop_reason  # type: ignore[attr-defined]
            exc.viola_provider_error_category = self.error_category  # type: ignore[attr-defined]
            if self.frames:
                exc.viola_failure_frames = self.frames  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Could not annotate provider policy exception", exc_info=True)
        raise exc

    def value_or_raise(self) -> Any:
        self.raise_for_status()
        return self.value

    def to_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "interrupted": self.interrupted,
            "error_category": self.error_category,
            "stop_reason": self.stop_reason,
            "usage": dict(self.usage or {}),
            **dict(self.metadata),
        }


class ProviderRequestPolicyError(RuntimeError):
    """Raised when callers need exception semantics for a failed envelope."""

    def __init__(self, envelope: ProviderResponseEnvelope) -> None:
        self.envelope = envelope
        category = envelope.error_category or envelope.status
        super().__init__("Provider request failed: %s" % category)


class ProviderStreamIdleTimeoutError(TimeoutError):
    """Raised when a provider stream stops producing events."""


class FallbackTriggeredError(Exception):
    """Raised when consecutive overload errors exhaust the 529 budget.

    Claude Code parity: ``withRetry.ts:160-168``. The agent loop / provider
    router catches this, swaps the active model to ``fallback_model``,
    clears partial assistant/tool state (``query.ts:893-950``), and retries
    the request. This MUST propagate — swallowing it silently turns the
    fallback into a no-op (Claude Code precedent: ``claude.ts:2598-2605``).
    """

    def __init__(self, original_model: str, fallback_model: str) -> None:
        self.original_model = original_model
        self.fallback_model = fallback_model
        super().__init__("Model fallback triggered: %s -> %s" % (original_model, fallback_model))


class CannotRetryError(Exception):
    """Raised when the retry loop exhausts attempts on a non-retryable class.

    Claude Code parity: ``withRetry.ts:144-158``. Carries the original
    provider error plus the final :class:`RetryContext` so the caller can
    log diagnostics and present a meaningful failure message.
    """

    def __init__(self, original_error: BaseException, retry_context: RetryContext) -> None:
        self.original_error = original_error
        self.retry_context = retry_context
        super().__init__(str(original_error))


def new_request_id(prefix: str = "llm") -> str:
    return "%s_%s" % (prefix, uuid4().hex)


_MAX_TOKENS_OVERFLOW_PATTERN = "input length and `max_tokens` exceed context limit"


def parse_max_tokens_context_overflow_error(
    exc: BaseException,
) -> dict[str, int] | None:
    """Parse the Anthropic 400 context-overflow error shape.

    Claude Code parity: ``withRetry.ts:550-595``. The error message has the
    form ``"input length and ``max_tokens`` exceed context limit: 188059 + 20000 > 200000"``.
    When recognised, returns ``{"input_tokens": ..., "max_tokens": ...,
    "context_limit": ...}``; callers compute a fresh ``max_tokens`` ceiling
    via :func:`compute_max_tokens_after_overflow` and replay the attempt.
    """
    status = _status_code(exc)
    if status != 400:
        return None
    message = str(exc)
    if _MAX_TOKENS_OVERFLOW_PATTERN not in message:
        return None
    # "...context limit: <inputTokens> + <maxTokens> > <contextLimit>"
    import re as _re

    match = _re.search(r"context limit:\s*(\d+)\s*\+\s*(\d+)\s*>\s*(\d+)", message)
    if not match:
        return None
    try:
        return {
            "input_tokens": int(match.group(1)),
            "max_tokens": int(match.group(2)),
            "context_limit": int(match.group(3)),
        }
    except (TypeError, ValueError):
        return None


def compute_max_tokens_after_overflow(
    overflow: Mapping[str, int],
    *,
    thinking_budget_tokens: int = 0,
    safety_buffer: int = 1000,
) -> int | None:
    """Compute the adjusted ``max_tokens`` after a context-overflow 400.

    Claude Code parity: ``withRetry.ts:393-415``. Returns ``None`` when even
    the minimum output budget cannot fit; callers should re-raise the
    original 400 in that case rather than retrying.
    """
    input_tokens = int(overflow.get("input_tokens", 0))
    context_limit = int(overflow.get("context_limit", 0))
    if context_limit <= 0:
        return None
    available_context = max(0, context_limit - input_tokens - safety_buffer)
    if available_context < FLOOR_OUTPUT_TOKENS:
        return None
    min_required = max(0, int(thinking_budget_tokens)) + 1
    return max(FLOOR_OUTPUT_TOKENS, available_context, min_required)


def classify_provider_error(exc: BaseException) -> ProviderErrorCategory:
    if isinstance(exc, asyncio.CancelledError):
        return "abort"
    if isinstance(exc, CostCircuitBreakerError):
        return "cost_limit"
    if isinstance(exc, LLMQuotaExceededError):
        return "quota_limit"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, (ProviderStreamIdleTimeoutError, TimeoutError, asyncio.TimeoutError)):
        return "timeout"

    text = str(exc).lower()
    status = _status_code(exc)
    if status == 400:
        return "bad_request"
    if status in {401, 403}:
        return "auth"
    if status == 402:
        return "billing"
    if status == 408:
        return "timeout"
    if status == 429:
        return "rate_limit"
    if status == 529 or "overloaded" in text or '"type":"overloaded_error"' in text:
        return "overloaded"
    if status is not None and status >= 500:
        return "server"
    if "context length" in text or "context window" in text or "exceed context" in text:
        return "context_length"
    if "quota" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    if any(token in text for token in ("timeout", "timed out")):
        return "timeout"
    if any(token in text for token in ("connect", "connection", "econnreset", "epipe", "transport", "refused")):
        return "transport"
    return "unknown"


async def execute_with_policy(
    call: Callable[..., Awaitable[Any]],
    context: ProviderRequestContext,
) -> ProviderResponseEnvelope:
    """Execute one provider operation under the shared retry policy.

    Claude Code parity: ``withRetry.ts:170-516``. State machine handles:

    - Abort-before-attempt (signal check at top of each loop iteration).
    - Client refresh on 401 / OAuth revoke / stale ECONNRESET (calls
      ``context.refresh_client`` if set).
    - Per-attempt :class:`RetryContext` with ``max_tokens_override``.
    - Fast-mode fallback (left as no-op hook for now; Claude-specific).
    - Foreground vs background 529 retry semantics.
    - Consecutive 529 budget → :class:`FallbackTriggeredError` when
      ``raise_fallback_triggered=True`` and a fallback model is configured.
      Otherwise returns a fallback-status envelope.
    - Context-overflow 400 → recompute ``max_tokens`` and retry without
      counting against the attempt budget.
    - Persistent retry mode with HEARTBEAT_INTERVAL_MS retry events while
      sleeping (kept inline rather than yielded — Python callers don't need
      the async-generator surface Claude's TS code exposes).
    """

    retry_events: list[dict[str, Any]] = []
    last_error: BaseException | None = None
    consecutive_rate_limits = 0
    consecutive_overloads = max(0, int(context.initial_overload_errors))
    max_attempts = max(0, context.max_retries) + 1
    persistent_attempt = 0
    persistent_total_ms = 0

    retry_context = RetryContext(
        model=context.model,
        thinking_budget_tokens=context.thinking_budget_tokens,
        fast_mode=context.fast_mode,
    )

    attempt = 1
    while attempt <= max_attempts:
        if _is_aborted(context.abort_signal):
            return _interrupted_envelope(context, attempt=attempt)

        # Claude parity: client refresh on auth / stale connection on the
        # PREVIOUS error. The callback may rotate keys, refresh OAuth, or
        # disable keep-alive before the next attempt.
        if last_error is not None and context.refresh_client is not None:
            prev_category = classify_provider_error(last_error)
            should_refresh = (
                prev_category == "auth" or _is_oauth_revoked(last_error) or _is_stale_connection(last_error)
            )
            if should_refresh:
                try:
                    refresh_result = context.refresh_client(last_error)
                    if inspect.isawaitable(refresh_result):
                        await refresh_result
                except Exception:
                    logger.exception("refresh_client callback failed; continuing with current client")

        attempt_context = ProviderAttemptContext(
            context=context,
            attempt=attempt,
            model=retry_context.model,
            timeout_s=context.timeout_s,
        )
        try:
            value = await asyncio.wait_for(
                _invoke_call(call, attempt_context, retry_context=retry_context),
                timeout=max(0.001, float(context.timeout_s)),
            )
            if isinstance(value, ProviderResponseEnvelope):
                if value.ok:
                    value.metadata.setdefault("attempts", attempt)
                    value.metadata.setdefault("retry_events", retry_events)
                    return value
                last_error = value.raw_error or ProviderRequestPolicyError(value)
                category = value.error_category or classify_provider_error(last_error)
                raise last_error
            return ProviderResponseEnvelope(
                status="ok",
                value=value,
                usage=_extract_usage(value),
                metadata={
                    "attempts": attempt,
                    "retry_events": retry_events,
                    "provider": context.provider,
                    "model": retry_context.model,
                    "request_id": context.request_id,
                    "stream": context.stream,
                },
            )
        except asyncio.CancelledError as exc:
            return _interrupted_envelope(context, attempt=attempt, raw_error=exc)
        except FallbackTriggeredError:
            # Already raised by inner code or by us in a prior iteration —
            # propagate as-is. The router catches it (query.ts:893-950 parity).
            raise
        except Exception as exc:
            last_error = exc
            category = classify_provider_error(exc)
            if category == "rate_limit":
                consecutive_rate_limits += 1
            elif category == "overloaded":
                consecutive_overloads += 1
            else:
                consecutive_rate_limits = 0
                consecutive_overloads = 0

            # Claude parity (withRetry.ts:388-426): max_tokens context overflow
            # → adjust ``RetryContext.max_tokens_override`` and replay. Does
            # NOT count against the attempt budget (matches Claude — overflow
            # is a recoverable provider-side limit, not a transient failure).
            overflow = parse_max_tokens_context_overflow_error(exc)
            if overflow is not None:
                adjusted = compute_max_tokens_after_overflow(
                    overflow,
                    thinking_budget_tokens=retry_context.thinking_budget_tokens,
                )
                if adjusted is not None:
                    retry_context.max_tokens_override = adjusted
                    retry_events.append(
                        {
                            "attempt": attempt,
                            "action": "max_tokens_overflow_adjust",
                            "delay_seconds": 0.0,
                            "reason": "max_tokens_overflow",
                            "status_code": 400,
                            "error_type": type(exc).__name__,
                            "adjusted_max_tokens": adjusted,
                            "input_tokens": overflow["input_tokens"],
                            "context_limit": overflow["context_limit"],
                        }
                    )
                    logger.info(
                        "max_tokens context overflow adjusted: input=%d limit=%d new_max=%d attempt=%d",
                        overflow["input_tokens"],
                        overflow["context_limit"],
                        adjusted,
                        attempt,
                    )
                    # Replay without consuming an attempt slot.
                    continue
                # Adjustment infeasible — fall through to normal failure path.

            decision = decide_retry(
                exc,
                context=context,
                attempt=attempt,
                max_attempts=max_attempts,
                category=category,
                consecutive_rate_limits=consecutive_rate_limits,
                consecutive_overloads=consecutive_overloads,
            )

            # Claude parity (withRetry.ts:336-351): consecutive 529 budget
            # exhausted with a fallback model → raise FallbackTriggeredError
            # so the agent loop/router clears partial state and swaps model.
            if (
                category == "overloaded"
                and consecutive_overloads >= MAX_529_RETRIES
                and context.raise_fallback_triggered
                and context.fallback_model
            ):
                logger.warning(
                    "529 budget exhausted (%d/%d) — raising FallbackTriggeredError %s -> %s",
                    consecutive_overloads,
                    MAX_529_RETRIES,
                    retry_context.model,
                    context.fallback_model,
                )
                raise FallbackTriggeredError(retry_context.model, context.fallback_model) from exc

            retry_events.append(
                {
                    "attempt": attempt,
                    "action": decision.action,
                    "delay_seconds": decision.delay_s,
                    "reason": decision.reason or category,
                    "status_code": _status_code(exc),
                    "error_type": type(exc).__name__,
                }
            )

            if decision.action == "retry":
                # Claude parity (withRetry.ts:368-372, 433-503): persistent
                # mode bypasses the attempt clamp and uses a separate counter
                # for backoff, with a 6-hour total cap.
                use_persistent = context.persistent_retry and category in {"rate_limit", "overloaded"}
                effective_delay = decision.delay_s
                if use_persistent:
                    persistent_attempt += 1
                    effective_delay = _persistent_retry_delay_s(exc, context, persistent_attempt)
                    persistent_total_ms += int(effective_delay * 1000)
                    if persistent_total_ms > PERSISTENT_RESET_CAP_MS:
                        # Past the 6-hour cap — bail out as if attempts exhausted.
                        logger.warning(
                            "Persistent retry reset cap exceeded (%.0fs > %ds)",
                            persistent_total_ms / 1000.0,
                            PERSISTENT_RESET_CAP_MS // 1000,
                        )
                        # Fall through to normal failure envelope.
                    else:
                        await _run_retry_callback(context.on_retry, attempt_context, exc, decision)
                        # Heartbeat-chunked sleep — yields retry events into
                        # the log every HEARTBEAT_INTERVAL_MS so the host
                        # sees activity.
                        try:
                            await _heartbeat_sleep(
                                effective_delay,
                                context=context,
                                attempt=persistent_attempt,
                                error=exc,
                                retry_events=retry_events,
                            )
                        except asyncio.CancelledError as abort_exc:
                            return _interrupted_envelope(context, attempt=attempt, raw_error=abort_exc)
                        # Persistent mode does NOT advance the attempt counter —
                        # the loop is gated by the cap and the abort signal.
                        if attempt >= max_attempts:
                            attempt = max_attempts
                        continue

                await _run_retry_callback(context.on_retry, attempt_context, exc, decision)
                logger.warning(
                    "Provider request retry provider=%s model=%s attempt=%d/%d reason=%s delay=%.3fs",
                    context.provider,
                    retry_context.model,
                    attempt,
                    max_attempts,
                    decision.reason or category,
                    effective_delay,
                )
                try:
                    await _sleep_with_abort(effective_delay, context.abort_signal)
                except asyncio.CancelledError as abort_exc:
                    return _interrupted_envelope(context, attempt=attempt, raw_error=abort_exc)
                attempt += 1
                continue

            metadata = {
                "attempts": attempt,
                "retry_events": retry_events,
                "provider": context.provider,
                "model": retry_context.model,
                "request_id": context.request_id,
                "stream": context.stream,
                "fallback_model": context.fallback_model,
            }
            if decision.action == "fallback":
                return ProviderResponseEnvelope(
                    status="fallback",
                    raw_error=exc,
                    error_category=category,
                    stop_reason=_stop_reason_for_category(category),
                    frames=_failure_frames(exc, context, category),
                    metadata=metadata,
                )
            return ProviderResponseEnvelope(
                status=_status_for_category(category),
                raw_error=exc,
                error_category=category,
                stop_reason=_stop_reason_for_category(category),
                frames=_failure_frames(exc, context, category),
                metadata=metadata,
            )

    category = classify_provider_error(last_error) if last_error else "unknown"
    return ProviderResponseEnvelope(
        status=_status_for_category(category),
        raw_error=last_error,
        error_category=category,
        stop_reason=_stop_reason_for_category(category),
        frames=_failure_frames(last_error, context, category),
        metadata={
            "attempts": max_attempts,
            "retry_events": retry_events,
            "provider": context.provider,
            "model": retry_context.model,
            "request_id": context.request_id,
            "stream": context.stream,
        },
    )


async def collect_stream_with_policy(
    stream_factory: Callable[..., Awaitable[Any]],
    context: ProviderRequestContext,
) -> ProviderResponseEnvelope:
    """Collect a provider stream only if it completes under policy.

    The caller can yield the collected events after success. If the stream is
    interrupted or idle-times out, partial deltas stay out of durable history.
    """

    async def _collect(attempt_context: ProviderAttemptContext) -> list[Any]:
        stream = await _invoke_call(stream_factory, attempt_context)
        events: list[Any] = []
        iterator = stream.__aiter__()
        while True:
            try:
                timeout_s = context.stream_idle_timeout_s or context.timeout_s
                event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_s)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
                raise ProviderStreamIdleTimeoutError("Provider stream idle timeout after %.3fs" % timeout_s) from exc
            events.append(event)
        if not events:
            raise ProviderStreamIdleTimeoutError("Provider stream completed without events")
        return events

    return await execute_with_policy(_collect, context)


def decide_retry(
    exc: BaseException,
    *,
    context: ProviderRequestContext,
    attempt: int,
    max_attempts: int,
    category: ProviderErrorCategory,
    consecutive_rate_limits: int,
    consecutive_overloads: int,
) -> ProviderRetryDecision:
    # Claude parity (withRetry.ts:773-781): 401 / OAuth-revoked is retryable
    # when a refresh_client callback is provided — the callback rotates the
    # token, then the next attempt runs against the refreshed client.
    # Without a callback, 401 means stop (matches Viola's historical behavior).
    if category == "auth" and context.refresh_client is not None:
        # Fall through to standard retry logic below.
        pass
    elif category in {"abort", "auth", "bad_request", "billing", "context_length", "cost_limit", "quota_limit"}:
        return ProviderRetryDecision(action="fail", reason=category)

    if category == "overloaded" and not _should_retry_overload(context):
        return ProviderRetryDecision(action="fail", reason="background_529")

    # Claude parity (withRetry.ts:702-706): persistent mode bypasses
    # rate_limit/overload budget caps and the attempt clamp — those exist to
    # protect short-lived foreground requests from amplifying capacity cascades;
    # persistent mode is opt-in for unattended sessions that explicitly want
    # to wait out the cascade.
    is_persistent_transient = context.persistent_retry and category in {
        "rate_limit",
        "overloaded",
    }

    if (
        category == "overloaded"
        and consecutive_overloads >= context.overload_retry_budget
        and not is_persistent_transient
    ):
        if context.fallback_allowed or context.fallback_model or context.nonstreaming_fallback:
            return ProviderRetryDecision(
                action="fallback", reason="overload_budget", next_provider=context.fallback_model
            )
        return ProviderRetryDecision(action="fail", reason="overload_budget")

    if (
        category == "rate_limit"
        and consecutive_rate_limits > context.rate_limit_retry_budget
        and not is_persistent_transient
    ):
        if context.fallback_allowed or context.fallback_model or context.nonstreaming_fallback:
            return ProviderRetryDecision(
                action="fallback",
                reason="rate_limit_budget",
                next_provider=context.fallback_model,
            )
        return ProviderRetryDecision(action="fail", reason="rate_limit_budget")

    retryable_categories = {"rate_limit", "overloaded", "server", "timeout", "transport", "unknown"}
    if category == "auth" and context.refresh_client is not None:
        retryable_categories = retryable_categories | {"auth"}
    if category not in retryable_categories:
        return ProviderRetryDecision(action="fail", reason=category)

    if attempt >= max_attempts and not is_persistent_transient:
        if (context.fallback_allowed or context.fallback_model or context.nonstreaming_fallback) and category in {
            "rate_limit",
            "overloaded",
            "server",
            "timeout",
            "transport",
        }:
            return ProviderRetryDecision(
                action="fallback", reason="attempt_budget", next_provider=context.fallback_model
            )
        return ProviderRetryDecision(action="fail", reason="attempt_budget")

    return ProviderRetryDecision(
        action="retry",
        delay_s=_retry_delay_s(exc, context, attempt),
        reason=category,
    )


def _should_retry_overload(context: ProviderRequestContext) -> bool:
    if context.query_kind == "background":
        return False
    if context.source is None:
        return True
    return context.source in _FOREGROUND_529_RETRY_SOURCES


def _status_code(exc: BaseException | None) -> int | None:
    if exc is None:
        return None
    for attr in ("status_code", "status", "code"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw)
            except ValueError:
                continue
    response = getattr(exc, "response", None)
    raw = getattr(response, "status_code", None)
    return raw if isinstance(raw, int) else None


def _retry_after_s(exc: BaseException) -> float | None:
    headers = getattr(exc, "headers", None)
    value: Any = None
    if isinstance(headers, Mapping):
        value = headers.get("retry-after") or headers.get("Retry-After")
    elif headers is not None:
        get = getattr(headers, "get", None)
        if callable(get):
            value = get("retry-after") or get("Retry-After")
    if value is None:
        response = getattr(exc, "response", None)
        response_headers = getattr(response, "headers", None)
        if isinstance(response_headers, Mapping):
            value = response_headers.get("retry-after") or response_headers.get("Retry-After")
        elif response_headers is not None:
            get = getattr(response_headers, "get", None)
            if callable(get):
                value = get("retry-after") or get("Retry-After")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


def _retry_delay_s(exc: BaseException, context: ProviderRequestContext, attempt: int) -> float:
    retry_after = _retry_after_s(exc)
    if retry_after is not None:
        return retry_after
    base = min(context.base_delay_s * (2 ** max(0, attempt - 1)), context.max_delay_s)
    jitter = random.random() * context.retry_jitter_fraction * base if context.retry_jitter_fraction > 0 else 0.0
    return base + jitter


async def _invoke_call(
    call: Callable[..., Awaitable[Any]],
    attempt_context: ProviderAttemptContext,
    *,
    retry_context: RetryContext | None = None,
) -> Any:
    """Invoke the provider call with attempt + retry context awareness.

    Three calling conventions are supported in priority order:

    1. ``call(attempt_context, retry_context=retry_context)`` when the call
       accepts both (provider opted in via ``pass_retry_context=True``).
    2. ``call(attempt_context)`` for the historical Viola convention.
    3. ``call()`` for legacy lambda-free closures.
    """
    pass_retry = retry_context is not None and attempt_context.context.pass_retry_context
    arity = _call_arity(call)
    try:
        if pass_retry and arity.accepts_retry_context:
            result = call(attempt_context, retry_context=retry_context)
        elif arity.accepts_positional:
            result = call(attempt_context)
        else:
            result = call()
    except TypeError:
        # Defensive fallback: signature probe may misfire on bound methods or
        # functools.partial. Retry with no arguments rather than crashing the
        # whole retry loop.
        result = call()
    if inspect.isawaitable(result):
        return await result
    return result


@dataclass(frozen=True)
class _CallArity:
    accepts_positional: bool
    accepts_retry_context: bool


def _call_arity(call: Callable[..., Any]) -> _CallArity:
    try:
        signature = inspect.signature(call)
    except (TypeError, ValueError):
        return _CallArity(accepts_positional=False, accepts_retry_context=False)
    accepts_positional = False
    accepts_retry_context = False
    for parameter in signature.parameters.values():
        if (
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            and parameter.default is inspect.Parameter.empty
        ):
            accepts_positional = True
        if parameter.name == "retry_context" or parameter.kind == inspect.Parameter.VAR_KEYWORD:
            accepts_retry_context = True
    return _CallArity(
        accepts_positional=accepts_positional,
        accepts_retry_context=accepts_retry_context,
    )


def _call_accepts_attempt_context(call: Callable[..., Any]) -> bool:
    """Backward-compat shim; kept for any out-of-tree callers."""
    return _call_arity(call).accepts_positional


def _is_oauth_revoked(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    status = _status_code(exc)
    if status != 403:
        return False
    return "oauth" in str(exc).lower() and "revoked" in str(exc).lower()


def _is_stale_connection(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    text = str(exc).lower()
    return any(code in text for code in ("econnreset", "epipe"))


def _persistent_retry_delay_s(
    exc: BaseException,
    context: ProviderRequestContext,
    persistent_attempt: int,
) -> float:
    """Compute the next delay in persistent-retry mode.

    Claude parity (withRetry.ts:433-447): respect Retry-After if present
    (server directive bypasses the soft max), but cap the total at the 5-min
    per-attempt ceiling. Honoring an extreme Retry-After unbounded was a real
    bug Claude fixed in v2 — we mirror the cap here.
    """
    retry_after = _retry_after_s(exc)
    if retry_after is not None:
        return min(retry_after, PERSISTENT_MAX_BACKOFF_MS / 1000.0)
    base = min(
        context.base_delay_s * (2 ** max(0, persistent_attempt - 1)),
        PERSISTENT_MAX_BACKOFF_MS / 1000.0,
    )
    jitter = random.random() * context.retry_jitter_fraction * base if context.retry_jitter_fraction > 0 else 0.0
    return base + jitter


async def _heartbeat_sleep(
    delay_s: float,
    *,
    context: ProviderRequestContext,
    attempt: int,
    error: BaseException,
    retry_events: list[dict[str, Any]],
) -> None:
    """Chunked sleep that emits a retry event every HEARTBEAT_INTERVAL_MS.

    Claude parity (withRetry.ts:489-503). The events go into ``retry_events``
    so callers logging the policy metadata can see the retry was alive — even
    when the actual wait spans tens of minutes.
    """
    if delay_s <= 0:
        return
    chunk_s = HEARTBEAT_INTERVAL_MS / 1000.0
    remaining = delay_s
    while remaining > 0:
        if _is_aborted(context.abort_signal):
            raise asyncio.CancelledError()
        retry_events.append(
            {
                "attempt": attempt,
                "action": "persistent_heartbeat",
                "delay_seconds": min(remaining, chunk_s),
                "remaining_seconds": remaining,
                "reason": "persistent_retry_wait",
                "status_code": _status_code(error),
                "error_type": type(error).__name__,
                "monotonic_s": time.monotonic(),
            }
        )
        chunk = min(remaining, chunk_s)
        await asyncio.sleep(chunk)
        remaining -= chunk


def _is_aborted(signal: Any | None) -> bool:
    if signal is None:
        return False
    for attr in ("aborted", "cancelled", "done"):
        value = getattr(signal, attr, None)
        if callable(value):
            try:
                if bool(value()):
                    return True
            except Exception:
                continue
        elif value is not None and bool(value):
            return True
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        try:
            return bool(is_set())
        except Exception:
            return False
    return False


def is_abort_signal_set(signal: Any | None) -> bool:
    """Return whether a caller-supplied abort/cancel signal is already set."""

    return _is_aborted(signal)


async def _sleep_with_abort(delay_s: float, signal: Any | None) -> None:
    if delay_s <= 0:
        return
    remaining = delay_s
    while remaining > 0:
        if _is_aborted(signal):
            raise asyncio.CancelledError()
        chunk = min(remaining, 0.5)
        await asyncio.sleep(chunk)
        remaining -= chunk


async def _run_retry_callback(
    callback: Callable[[ProviderAttemptContext, BaseException, ProviderRetryDecision], Any] | None,
    attempt_context: ProviderAttemptContext,
    exc: BaseException,
    decision: ProviderRetryDecision,
) -> None:
    if callback is None:
        return
    result = callback(attempt_context, exc, decision)
    if inspect.isawaitable(result):
        await result


def _interrupted_envelope(
    context: ProviderRequestContext,
    *,
    attempt: int,
    raw_error: BaseException | None = None,
) -> ProviderResponseEnvelope:
    return ProviderResponseEnvelope(
        status="interrupted",
        raw_error=raw_error,
        interrupted=True,
        error_category="abort",
        stop_reason="interrupted",
        frames=[
            system_reminder_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                text="Provider request was interrupted before completion.",
                source_tag="provider_request_interrupted",
                origin="provider_policy",
                session_id=context.session_id,
            )
        ],
        metadata={
            "attempts": attempt,
            "provider": context.provider,
            "model": context.model,
            "request_id": context.request_id,
            "stream": context.stream,
        },
    )


def _status_for_category(category: ProviderErrorCategory) -> ProviderStatus:
    if category == "abort":
        return "interrupted"
    if category == "timeout":
        return "timeout"
    if category == "rate_limit":
        return "rate_limited"
    if category == "cost_limit":
        return "cost_limited"
    if category == "quota_limit":
        return "quota_limited"
    return "failed"


def _stop_reason_for_category(category: ProviderErrorCategory) -> str:
    if category == "rate_limit":
        return "rate_limit"
    if category == "cost_limit":
        return "cost_limit"
    if category == "quota_limit":
        return "quota_limit"
    if category == "timeout":
        return "provider_timeout"
    if category == "abort":
        return "interrupted"
    if category == "overloaded":
        return "provider_overloaded"
    return "provider_error"


def _failure_frames(
    exc: BaseException | None,
    context: ProviderRequestContext,
    category: ProviderErrorCategory,
) -> list[Frame]:
    existing = getattr(exc, "viola_failure_frames", None) if exc is not None else None
    if isinstance(existing, list) and all(isinstance(frame, Frame) for frame in existing):
        return existing
    return [
        system_reminder_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            text=_frame_text_for_category(category, exc),
            source_tag="provider_%s" % category,
            origin="provider_policy",
            session_id=context.session_id,
        )
    ]


def _frame_text_for_category(category: ProviderErrorCategory, exc: BaseException | None) -> str:
    if category == "rate_limit":
        return "Provider request stopped because the model provider returned a rate limit."
    if category == "cost_limit":
        return "Provider request stopped because Viola's cost safety limit was reached."
    if category == "quota_limit":
        return "Provider request stopped because the current account is not allowed to spend more LLM tokens."
    if category == "timeout":
        return "Provider request stopped because the model provider timed out."
    if category == "abort":
        return "Provider request stopped because the user or runtime interrupted it."
    if category == "overloaded":
        return "Provider request stopped because the model provider is overloaded."
    if exc is not None:
        return "Provider request failed with %s." % type(exc).__name__
    return "Provider request failed."


def _extract_usage(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        usage = value.get("_usage") or value.get("usage")
        if isinstance(usage, Mapping):
            return usage
        return None
    usage = getattr(value, "usage", None)
    if usage is None:
        return None
    result: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        if hasattr(usage, key):
            result[key] = getattr(usage, key)
    return result or None


__all__ = [
    "BASE_DELAY_MS",
    "DEFAULT_MAX_RETRIES",
    "FLOOR_OUTPUT_TOKENS",
    "HEARTBEAT_INTERVAL_MS",
    "MAX_529_RETRIES",
    "PERSISTENT_MAX_BACKOFF_MS",
    "PERSISTENT_RESET_CAP_MS",
    "CannotRetryError",
    "FallbackTriggeredError",
    "ProviderAttemptContext",
    "ProviderRequestContext",
    "ProviderRequestPolicyError",
    "ProviderResponseEnvelope",
    "ProviderRetryDecision",
    "ProviderStreamIdleTimeoutError",
    "RetryContext",
    "classify_provider_error",
    "collect_stream_with_policy",
    "compute_max_tokens_after_overflow",
    "decide_retry",
    "execute_with_policy",
    "is_abort_signal_set",
    "new_request_id",
    "parse_max_tokens_context_overflow_error",
]
