"""Runtime context provider contracts for canonical meta-frame injection."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from core.logging_config import get_logger
from services.conversation.context_frames import (
    Frame,
    FrameRole,
)

logger = get_logger(__name__)

# Backstop for providers dispatched via ``concurrent_names``: bounds how long
# ``build_all`` waits on a background provider before degrading it to "no
# frames" (never blocks forever, mirrors the omit-on-failure contract every
# other provider already gets). Providers that make their own bounded network
# call (e.g. the memory side-query's 15 s OpenAI timeout) will normally
# resolve well before this fires; it exists only for a provider that hangs
# without honoring its own timeout.
_CONCURRENT_PROVIDER_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class RuntimeContextRequest:
    """Request data available to runtime context providers."""

    user_id: str
    session_id: str | None
    current_user_text: str
    channel: str | None
    tool_state: dict[str, Any] | None = None
    gate_state: dict[str, Any] | None = None


class RuntimeContextProvider(Protocol):
    """A source that contributes runtime context as canonical meta frames."""

    name: str
    origin: str
    ttl_turns: int | None
    relevance: str
    priority: int

    def build_frames(self, request: RuntimeContextRequest) -> list[Frame]:
        """Build model-bound context frames without making provider API calls."""


@dataclass(frozen=True)
class CallableRuntimeContextProvider:
    """Small adapter for existing ContextBuilder methods."""

    name: str
    origin: str
    priority: int
    build: Callable[[RuntimeContextRequest], Iterable[Frame] | Frame | None]
    ttl_turns: int | None = 1
    relevance: str = "request"

    def build_frames(self, request: RuntimeContextRequest) -> list[Frame]:
        value = self.build(request)
        if value is None:
            return []
        if isinstance(value, Frame):
            return [value]
        return list(value)


@dataclass
class RuntimeContextRegistry:
    """Ordered runtime context registry.

    The registry does not own conversation history. It only builds the
    per-request meta frames that are prepended to the canonical frame chain.
    """

    _providers: list[tuple[int, RuntimeContextProvider]] = field(default_factory=list)
    _next_order: int = 0

    def __init__(self, providers: Iterable[RuntimeContextProvider] | None = None) -> None:
        self._providers = []
        self._next_order = 0
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: RuntimeContextProvider) -> None:
        """Register one provider, preserving stable priority order."""

        name = str(getattr(provider, "name", "") or "").strip()
        origin = str(getattr(provider, "origin", "") or "").strip()
        if not name:
            raise ValueError("Runtime context provider name is required.")
        if not origin:
            raise ValueError("Runtime context provider origin is required.")
        if any(existing.name == name for _, existing in self._providers):
            raise ValueError("Runtime context provider '%s' is already registered." % name)
        self._providers.append((self._next_order, provider))
        self._next_order += 1

    def build_all(
        self,
        request: RuntimeContextRequest,
        *,
        concurrent_names: frozenset[str] = frozenset(),
    ) -> list[Frame]:
        """Build all provider frames, omitting failed optional context.

        Providers named in ``concurrent_names`` are dispatched to background
        threads immediately, then every other provider still builds in its
        existing priority-ordered, same-thread sequence -- so a provider that
        blocks on a slow network round trip (e.g. the memory side-query's
        second LLM call, `services/memory/selector.py`) no longer serializes
        its wait in front of every other provider's build. Providers *not*
        named here keep their exact current single-threaded semantics,
        including any cross-provider ordering coupling implemented via
        closures over ``context_builder.py``'s ``provider_state`` dict (e.g.
        ``identity`` must run, and be observed, before ``profile``) --
        concurrency here is opt-in per provider, not a blanket parallel
        rewrite, precisely so that coupling is never put at risk.

        Final frame order is unaffected by which providers ran concurrently:
        results are re-sorted by (priority, registration order) before
        normalization, identical to the fully sequential path's output
        order. A concurrent provider that fails or exceeds
        ``_CONCURRENT_PROVIDER_TIMEOUT_S`` degrades to "no frames", the same
        omit-on-failure contract every provider gets below.
        """
        ordered = sorted(self._providers, key=lambda item: (item[1].priority, item[0]))
        concurrent = [(order, provider) for order, provider in ordered if provider.name in concurrent_names]
        sequential = [(order, provider) for order, provider in ordered if provider.name not in concurrent_names]

        executor: ThreadPoolExecutor | None = None
        futures: dict[str, Future[list[Frame]]] = {}
        if concurrent:
            executor = ThreadPoolExecutor(
                max_workers=len(concurrent),
                thread_name_prefix="runtime-context-concurrent",
            )
            for _, provider in concurrent:
                # Bare executor.submit runs the callable in an EMPTY
                # contextvars context. Pre-concurrency, every provider ran on
                # the calling thread and therefore saw the caller's
                # request-scoped ContextVars (e.g. ai_controller sets
                # _ctx_user_id immediately before build_frames). Copy the
                # caller's context into the worker so a concurrent-dispatched
                # provider observes exactly what it observed when it ran
                # sequentially -- otherwise any provider reading a ContextVar
                # instead of the request object silently sees stale/empty
                # state (verified live: latency-span turn tags came back
                # empty on concurrent-dispatched providers).
                ctx = contextvars.copy_context()
                futures[provider.name] = executor.submit(ctx.run, self._safe_normalized_frames, provider, request)

        results: list[tuple[tuple[int, int], list[Frame]]] = []
        for order, provider in sequential:
            results.append(
                (
                    (provider.priority, order),
                    self._safe_normalized_frames(provider, request),
                )
            )

        if executor is not None:
            try:
                for order, provider in concurrent:
                    try:
                        provider_frames = futures[provider.name].result(timeout=_CONCURRENT_PROVIDER_TIMEOUT_S)
                    except FutureTimeoutError:
                        logger.debug(
                            "Runtime context provider %s omitted after exceeding %.1fs concurrent budget",
                            provider.name,
                            _CONCURRENT_PROVIDER_TIMEOUT_S,
                        )
                        provider_frames = []
                    results.append(((provider.priority, order), provider_frames))
            finally:
                # Never block the turn waiting for threads to join; a
                # timed-out provider's thread is abandoned (it still holds
                # only local state, nothing shared, per the docstring above).
                executor.shutdown(wait=False)

        results.sort(key=lambda item: item[0])
        frames: list[Frame] = []
        for _, provider_frames in results:
            frames.extend(provider_frames)
        return frames

    @classmethod
    def _safe_normalized_frames(cls, provider: RuntimeContextProvider, request: RuntimeContextRequest) -> list[Frame]:
        """Build and normalize one provider's frames, omitting it entirely on any failure.

        Build failures and normalization failures (a provider returning a
        non-Frame or non-meta value) are both non-fatal to the rest of
        context assembly -- same contract as the pre-concurrency
        ``build_all``, which ran both steps inside one try/except per
        provider.
        """
        from diagnostics import latency_spans

        try:
            with latency_spans.span("CTX_PROVIDER", provider=provider.name):
                raw_frames = provider.build_frames(request)
            return cls._normalize_provider_frames(provider, raw_frames)
        except Exception as exc:  # noqa: BLE001, RUF100 - fail-open: omit the provider, never the turn
            logger.debug(
                "Runtime context provider %s omitted after failure: %s",
                provider.name,
                exc,
                exc_info=True,
            )
            return []

    @staticmethod
    def _normalize_provider_frames(provider: RuntimeContextProvider, frames: Iterable[Frame]) -> list[Frame]:
        normalized: list[Frame] = []
        for frame in frames:
            if not isinstance(frame, Frame):
                raise TypeError("Runtime context provider '%s' returned a non-Frame value." % provider.name)
            if not frame.is_meta or frame.role is not FrameRole.META_USER:
                raise ValueError("Runtime context provider '%s' returned a non-meta frame." % provider.name)
            extra = dict(frame.extra)
            extra.setdefault("runtime_context_provider", provider.name)
            extra.setdefault("runtime_context_priority", provider.priority)
            normalized.append(
                replace(
                    frame,
                    origin=frame.origin or provider.origin,
                    ttl_turns=(frame.ttl_turns if frame.ttl_turns is not None else provider.ttl_turns),
                    relevance=(frame.relevance if frame.relevance != "always" else provider.relevance),
                    extra=extra,
                )
            )
        return normalized


__all__ = [
    "CallableRuntimeContextProvider",
    "RuntimeContextProvider",
    "RuntimeContextRegistry",
    "RuntimeContextRequest",
]
