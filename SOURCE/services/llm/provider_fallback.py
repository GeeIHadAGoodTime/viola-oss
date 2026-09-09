"""Provider-level fallback chain for LLM calls."""

from __future__ import annotations

import copy
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from core.exceptions import ProviderUnavailableError
from core.logging_config import get_logger
from services.llm.operator_diagnostics import (
    classify_llm_operator_error,
    should_fallback_for_operator_diagnostic,
)
from services.llm.providers.base import BaseLLMProvider, LLMTestResult

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle


class LLMProviderFallbackChain(BaseLLMProvider):
    """Try a managed primary provider, then configured backup providers.

    Fallback policy (matches Claude Code's
    ``FallbackTriggeredError`` semantics in ``withRetry.ts:326-351``):

    - **Hard failures** (auth/billing/bad-request/quota-exhausted) advance
      providers immediately.  The primary cannot recover by waiting.
    - **Transient failures** (5xx / 529 / connection errors) carry a
      ``retry_primary_first=True`` hint from the operator diagnostic.  We
      now require ``CONSECUTIVE_TRANSIENT_BEFORE_FALLBACK`` (default 3)
      consecutive transient failures from the same provider before
      advancing.  This mirrors Claude's ``MAX_529_RETRIES`` rule and
      prevents flapping after a single 503.

    Provider adapters own their same-key retry attempts through the shared
    request policy; this gate runs AFTER those attempts are exhausted.
    """

    PRIMARY_RETRY_INTERVAL_SECONDS = 300.0
    CONSECUTIVE_TRANSIENT_BEFORE_FALLBACK = 3

    def __init__(
        self,
        primary: BaseLLMProvider,
        fallbacks: list[BaseLLMProvider],
        *,
        unavailable_fallbacks: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(primary.config)
        self._providers = [primary, *fallbacks]
        self._unavailable_fallbacks = [copy.deepcopy(entry) for entry in (unavailable_fallbacks or [])]
        self._active_index = self._first_available_index()
        self._last_successful_index = self._active_index
        self._last_fallback_monotonic: float | None = None
        self._last_fallback_event: dict[str, Any] | None = None
        # Per-provider counter of consecutive transient failures.  Reset on
        # any non-transient outcome (success, hard failure).  Used to gate
        # "should we advance the provider?" against Claude's repeat-failure
        # threshold.
        self._consecutive_transient: dict[int, int] = {}
        self.SUPPORTS_NATIVE_TOOLS = any(
            bool(getattr(provider, "SUPPORTS_NATIVE_TOOLS", False))
            or callable(getattr(provider, "route_command_native", None))
            for provider in self._providers
        )

    @property
    def primary_provider(self) -> BaseLLMProvider:
        return self._providers[0]

    @property
    def active_provider(self) -> BaseLLMProvider:
        idx = min(max(self._active_index, 0), len(self._providers) - 1)
        return self._providers[idx]

    def _first_available_index(self) -> int:
        for index, provider in enumerate(getattr(self, "_providers", ())):
            try:
                if provider.is_available():
                    return index
            except Exception:
                logger.debug("Provider availability check failed for chain index %d", index)
        return 0

    def _provider_identity(self, provider: BaseLLMProvider, index: int) -> dict[str, Any]:
        config = getattr(provider, "config", None)
        try:
            provider_type = provider.get_provider_type()
        except Exception:
            provider_type = getattr(config, "provider", type(provider).__name__)
        try:
            provider_name = provider.get_provider_name()
        except Exception:
            provider_name = str(provider_type or type(provider).__name__)
        return {
            "chain_index": index,
            "provider": provider_type,
            "name": provider_name,
            "model": getattr(config, "model", ""),
            "provider_class": type(provider).__name__,
            "available": self._is_provider_available(provider),
        }

    def _is_provider_available(self, provider: BaseLLMProvider) -> bool:
        try:
            return provider.is_available()
        except Exception:
            logger.debug("Provider availability check failed for %s", type(provider).__name__)
            return False

    def _operation_available(self, provider: BaseLLMProvider, operation_name: str) -> bool:
        operation = getattr(provider, operation_name, None)
        return callable(operation)

    def _sync_runtime_state(self, provider: BaseLLMProvider) -> None:
        for attr in (
            "_agent_system_prompt",
            "_native_tools",
            "_agent_tool_choice",
            "_preferred_first_tool",
            "_ask_tier_native",
            "_settle_user_id",
            "_settle_estimated_tokens",
        ):
            if hasattr(self, attr):
                try:
                    setattr(provider, attr, getattr(self, attr))
                except Exception:
                    logger.debug("Could not sync %s to provider %s", attr, type(provider).__name__)

    def _call_kwargs_for_provider(self, index: int, kwargs: dict[str, Any]) -> dict[str, Any]:
        provider_kwargs = dict(kwargs)
        if index > 0:
            provider_kwargs.pop("model_override", None)
        return provider_kwargs

    def _candidate_indices(self, operation_name: str) -> list[int]:
        should_probe_primary = True
        if self._active_index > 0 and self._last_fallback_monotonic is not None:
            elapsed = time.monotonic() - self._last_fallback_monotonic
            should_probe_primary = elapsed >= self.PRIMARY_RETRY_INTERVAL_SECONDS

        ordered = (
            list(range(len(self._providers)))
            if should_probe_primary
            else list(range(self._active_index, len(self._providers)))
        )
        return [
            index
            for index in ordered
            if self._is_provider_available(self._providers[index])
            and self._operation_available(self._providers[index], operation_name)
        ]

    def _has_later_candidate(self, current_index: int, operation_name: str) -> bool:
        for index in range(current_index + 1, len(self._providers)):
            provider = self._providers[index]
            if self._is_provider_available(provider) and self._operation_available(provider, operation_name):
                return True
        return False

    def _next_candidate_identity(self, current_index: int, operation_name: str) -> dict[str, Any] | None:
        for index in range(current_index + 1, len(self._providers)):
            provider = self._providers[index]
            if self._is_provider_available(provider) and self._operation_available(provider, operation_name):
                return self._provider_identity(provider, index)
        return None

    def _unavailable_fallback_summary(self) -> str:
        parts = []
        for entry in self._unavailable_fallbacks:
            name = str(entry.get("name") or entry.get("source") or "unknown")
            reason = str(entry.get("reason") or "unavailable")
            parts.append("%s: %s" % (name, reason))
        return "; ".join(parts)

    def _log_unavailable_fallbacks(self, operation_name: str) -> None:
        for entry in self._unavailable_fallbacks:
            name = str(entry.get("name") or entry.get("source") or "unknown")
            reason = str(entry.get("reason") or "unavailable")
            logger.info("%s fallback skipped for %s: %s", name, operation_name, reason)

    def _diagnostic_from_result(self, result: Any) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None
        containers: list[dict[str, Any]] = [result]
        for key in ("no_result", "error_state"):
            value = result.get(key)
            if isinstance(value, dict):
                containers.append(value)
        for container in containers:
            diagnostic = container.get("operator_diagnostic")
            if isinstance(diagnostic, dict):
                return diagnostic
        if result.get("type") == "ai_no_result":
            reason = str(result.get("reason") or result.get("error_category") or "")
            if reason:
                return classify_llm_operator_error(reason)
        return None

    def _record_failure(
        self,
        *,
        index: int,
        operation_name: str,
        diagnostic: dict[str, Any],
        exc: BaseException | None,
        fallback_requested: bool,
        will_fallback: bool,
    ) -> None:
        provider = self._providers[index]
        next_provider = self._next_candidate_identity(index, operation_name) if will_fallback else None
        event = {
            "operation": operation_name,
            "failed_provider": self._provider_identity(provider, index),
            "diagnostic": copy.deepcopy(diagnostic),
            "next_provider": next_provider,
            "exception_type": type(exc).__name__ if exc is not None else None,
            "message": str(exc)[:500] if exc is not None else None,
            "ts_monotonic": time.monotonic(),
        }
        self._last_fallback_event = event
        self._last_fallback_monotonic = event["ts_monotonic"]
        if will_fallback and next_provider is not None:
            self._active_index = int(next_provider["chain_index"])
            logger.warning(
                "LLM provider %s failed with %s; trying fallback provider %s",
                event["failed_provider"]["name"],
                diagnostic.get("category"),
                next_provider["name"],
            )
        elif fallback_requested:
            self._log_unavailable_fallbacks(operation_name)
            skipped = self._unavailable_fallback_summary()
            if skipped:
                logger.warning(
                    "LLM provider %s failed with %s; fallback requested for %s but no backup provider is available "
                    "(skipped: %s)",
                    event["failed_provider"]["name"],
                    diagnostic.get("category"),
                    operation_name,
                    skipped,
                )
            else:
                logger.warning(
                    "LLM provider %s failed with %s; fallback requested for %s but no backup provider is available",
                    event["failed_provider"]["name"],
                    diagnostic.get("category"),
                    operation_name,
                )

    def _record_success(self, index: int) -> None:
        self._active_index = index
        self._last_successful_index = index
        if index == 0:
            self._last_fallback_event = None
            self._last_fallback_monotonic = None

    def _annotate_result(self, result: Any, index: int) -> Any:
        if not isinstance(result, dict):
            return result
        provider = self._providers[index]
        provider_info = self._provider_identity(provider, index)
        fallback_meta = {
            "active": index > 0,
            "served_by": provider_info,
            "primary": self._provider_identity(self.primary_provider, 0),
            "last_event": copy.deepcopy(self._last_fallback_event),
        }
        annotated = dict(result)
        annotated.setdefault("_model_name", provider_info.get("model") or "")
        annotated["_llm_provider"] = provider_info
        annotated["_llm_fallback"] = fallback_meta
        return annotated

    def _should_advance_on_diagnostic(
        self,
        *,
        index: int,
        diagnostic: dict[str, Any],
    ) -> bool:
        """Apply Claude's consecutive-failure gate to transient errors.

        Returns True if the failure type WARRANTS advancing the provider on
        this occurrence.  Hard failures (auth/quota-exhausted) bypass the
        counter; transient (5xx/529/connection) advance only after
        ``CONSECUTIVE_TRANSIENT_BEFORE_FALLBACK`` consecutive hits.
        """
        if not should_fallback_for_operator_diagnostic(diagnostic):
            self._consecutive_transient[index] = 0
            return False

        category = diagnostic.get("category")
        policy = diagnostic.get("fallback_policy")
        retry_primary_first = bool(isinstance(policy, dict) and policy.get("retry_primary_first"))

        # Hard failures with should_fallback=True (auth/quota/billing) do
        # not have retry_primary_first set — advance immediately.
        if not retry_primary_first:
            self._consecutive_transient[index] = 0
            return True

        # Transient: increment the counter, advance only after threshold.
        # Counter persists across calls so flapping 5xx between two
        # requests still advances after N occurrences.
        prev = self._consecutive_transient.get(index, 0)
        new_count = prev + 1
        self._consecutive_transient[index] = new_count
        if new_count >= self.CONSECUTIVE_TRANSIENT_BEFORE_FALLBACK:
            # Reset for next time so the new provider gets a fresh window.
            self._consecutive_transient[index] = 0
            logger.warning(
                "LLM provider chain[%d] hit %d consecutive %s; advancing",
                index,
                new_count,
                category,
            )
            return True
        logger.info(
            "LLM provider chain[%d] transient %s (%d/%d) — retrying primary",
            index,
            category,
            new_count,
            self.CONSECUTIVE_TRANSIENT_BEFORE_FALLBACK,
        )
        return False

    async def _run_chain(
        self,
        operation_name: str,
        call_factory: Callable[[BaseLLMProvider, dict[str, Any]], Awaitable[Any]],
        kwargs: dict[str, Any],
    ) -> Any:
        last_error: BaseException | None = None
        last_result: Any = None
        candidates = self._candidate_indices(operation_name)
        if not candidates:
            raise ProviderUnavailableError("all", "No LLM provider in the fallback chain is available.")

        for index in candidates:
            provider = self._providers[index]
            self._sync_runtime_state(provider)
            provider_kwargs = self._call_kwargs_for_provider(index, kwargs)
            try:
                result = await call_factory(provider, provider_kwargs)
            except Exception as exc:
                diagnostic = classify_llm_operator_error(exc)
                last_error = exc
                fallback_requested = should_fallback_for_operator_diagnostic(diagnostic)
                # Claude-parity: gate transient failures behind a
                # consecutive-count threshold.  Hard failures advance
                # immediately.
                advance = self._should_advance_on_diagnostic(index=index, diagnostic=diagnostic)
                will_fallback = advance and self._has_later_candidate(
                    index,
                    operation_name,
                )
                self._record_failure(
                    index=index,
                    operation_name=operation_name,
                    diagnostic=diagnostic,
                    exc=exc,
                    fallback_requested=fallback_requested,
                    will_fallback=will_fallback,
                )
                if not will_fallback:
                    raise
                continue

            diagnostic = self._diagnostic_from_result(result)
            fallback_requested = bool(diagnostic and should_fallback_for_operator_diagnostic(diagnostic))
            # Diagnostic-from-result follows the same gating policy.
            advance = bool(diagnostic) and self._should_advance_on_diagnostic(index=index, diagnostic=diagnostic)
            will_fallback = bool(diagnostic and advance and self._has_later_candidate(index, operation_name))
            if will_fallback:
                last_result = result
                self._record_failure(
                    index=index,
                    operation_name=operation_name,
                    diagnostic=diagnostic,
                    exc=None,
                    fallback_requested=True,
                    will_fallback=True,
                )
                continue
            if fallback_requested and diagnostic:
                self._record_failure(
                    index=index,
                    operation_name=operation_name,
                    diagnostic=diagnostic,
                    exc=None,
                    fallback_requested=True,
                    will_fallback=False,
                )

            # Successful (or below-threshold transient) — reset counter.
            self._consecutive_transient.pop(index, None)
            self._record_success(index)
            return self._annotate_result(result, index)

        if last_error is not None:
            raise last_error
        return last_result

    async def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        async def _call(provider: BaseLLMProvider, kwargs: dict[str, Any]) -> Any:
            return await provider.ask(question, **kwargs)

        return await self._run_chain(
            "ask",
            _call,
            {
                "system_prompt": system_prompt,
                "include_history": include_history,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._warn_ignored_history_arg(history, "route_command")

        async def _call(provider: BaseLLMProvider, provider_kwargs: dict[str, Any]) -> Any:
            return await provider.route_command(text, **provider_kwargs)

        route_kwargs = {
            "context_bundle": context_bundle,
            "max_tokens": max_tokens,
            **kwargs,
        }
        return await self._run_chain("route_command", _call, route_kwargs)

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        async def _call(provider: BaseLLMProvider, provider_kwargs: dict[str, Any]) -> Any:
            return await provider.route_command_native(messages=messages, **provider_kwargs)

        return await self._run_chain("route_command_native", _call, kwargs)

    async def compact_responses_continuity(
        self,
        *,
        continuity: dict[str, Any] | None,
        model_override: str | None = None,
    ) -> dict[str, Any] | None:
        for index in self._candidate_indices("compact_responses_continuity"):
            provider = self._providers[index]
            compact_fn = getattr(provider, "compact_responses_continuity", None)
            if callable(compact_fn):
                self._sync_runtime_state(provider)
                kwargs = self._call_kwargs_for_provider(index, {"model_override": model_override})
                return await compact_fn(continuity=continuity, **kwargs)
        return None

    def is_available(self) -> bool:
        return any(self._is_provider_available(provider) for provider in self._providers)

    async def test_connection(self) -> LLMTestResult:
        for index in self._candidate_indices("test_connection"):
            provider = self._providers[index]
            result = await provider.test_connection()
            if result.success:
                self._record_success(index)
                return result
        return await self.primary_provider.test_connection()

    def get_available_models(self) -> list[str]:
        models: list[str] = []
        for provider in self._providers:
            try:
                models.extend(provider.get_available_models())
            except Exception:
                logger.debug("Could not read models for provider %s", type(provider).__name__)
        return sorted(set(models))

    def get_provider_type(self) -> str:
        return self.active_provider.get_provider_type()

    def get_provider_name(self) -> str:
        name = self.active_provider.get_provider_name()
        if self.get_fallback_status()["degraded"]:
            return "%s (fallback)" % name
        return name

    def get_unavailable_reason(self) -> str | None:
        if self.is_available():
            return None
        reasons = []
        for index, provider in enumerate(self._providers):
            reason = provider.get_unavailable_reason()
            if reason:
                reasons.append("%d:%s" % (index, reason))
        return "; ".join(reasons) or "No fallback-chain providers available"

    def clear_history(self) -> None:
        for provider in self._providers:
            provider.clear_history()

    def get_history(self) -> list[dict[str, str]]:
        return self.active_provider.get_history()

    def add_to_history(self, role: str, content: str) -> None:
        self.active_provider.add_to_history(role, content)

    def get_fallback_status(self) -> dict[str, Any]:
        active_index = self._active_index
        if not self._is_provider_available(self._providers[active_index]):
            active_index = self._first_available_index()
        return {
            "enabled": True,
            "degraded": active_index > 0,
            "active_index": active_index,
            "active_provider": self._provider_identity(self._providers[active_index], active_index),
            "primary": self._provider_identity(self.primary_provider, 0),
            "chain": [self._provider_identity(provider, index) for index, provider in enumerate(self._providers)],
            "unavailable_fallbacks": copy.deepcopy(self._unavailable_fallbacks),
            "last_event": copy.deepcopy(self._last_fallback_event),
        }


__all__ = ["LLMProviderFallbackChain"]
