"""
Context assembly for the AI controller LLM prompt.

Provides ``ContextBuilder`` — a class that assembles the full system-context
frames injected into every LLM call.  Consolidates:

- Playback / self-knowledge state  (``_get_playback_context``)
- User memory recalled for the request  (``_build_memory_context``)
- Structured user profile  (``_build_profile_context``)
- Saved delivery address  (``_build_delivery_address_context``)
- Learned user-model summary  (``_build_user_model_context``)
- Capability registry context  (``_build_capability_context``)

All methods are safe to call even when the underlying service is
unavailable — they return ``""`` on failure and log at DEBUG level.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.asyncio_safe import is_event_loop_closed_error
from core.logging_config import get_logger
from intent.gate_protocol import gate_message_body
from intent.runtime_context.providers import (
    CallableRuntimeContextProvider,
    RuntimeContextRegistry,
    RuntimeContextRequest,
)
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
)

if TYPE_CHECKING:
    from services.conversation.state_manager import ConversationStateManager

logger = get_logger(__name__)


@dataclass(frozen=True)
class _MemorySelectionPrefetch:
    """An in-flight memory side-query fired early in the request path (#465).

    ``future`` resolves to the list of selected ``MemoryHeader`` objects that
    ``select_relevant_memory_headers`` would have returned inline -- so
    consuming it produces the byte-identical selection for THIS turn's prompt.
    """

    user_id: str
    user_text: str
    future: Future[list[Any]]


# Per-request handle to the early-fired memory side-query. A ContextVar is the
# correct store: each request runs in its own asyncio task with its own
# contextvars context, so a prefetch armed in ai_controller.process_request is
# visible to that same request's context-build (and to the copied context the
# concurrent memory provider runs in) and can never leak into another user's or
# another turn's request. Consumed exactly once, in ``_build_memory_context``.
_memory_selection_prefetch: contextvars.ContextVar[_MemorySelectionPrefetch | None] = contextvars.ContextVar(
    "viola_memory_selection_prefetch",
    default=None,
)

# Consuming the early-fired side-query is bounded by ``auto_memory_side_query_max_block_ms``
# on BOTH paths (#2605/#531): TTFT must not wait on the ~1.3 s side-query LLM
# round trip. The old 20 s default-path ceiling (block until the selection lands,
# semantics-preserving) is retired -- the default now bounds the wait at the
# budget and backstops a timeout with the deterministic local ranker
# (``_local_memory_selection``), preserving this turn's memory content without
# blocking the turn. The founder-gated ``auto_memory_side_query_nonblocking``
# flag keeps the manifest-only content-drop trade on a timeout.

# Long-lived pool for the memory side-query prefetch (Lane B, #493). Constructing
# a fresh ``ThreadPoolExecutor`` + spawning a worker thread every turn cost
# ~105-140 ms ON the answer path: under the trace write-behind worker's per-turn
# GIL pressure, ``ThreadPoolExecutor.submit``'s first ``Thread.start()`` blocks
# until the new thread can acquire the GIL to bootstrap, and the turn waited on
# that. One reused pool keeps the worker warm so the per-turn fire cost is a bare
# submit. Threads are created lazily on first submit and reused; the ``submit``
# never blocks the caller. Per-turn / per-user isolation is UNCHANGED -- each
# submit runs in ``contextvars.copy_context()`` and the closure captures this
# turn's ``(user_id, user_text, directory, excluded)``, re-checked against the
# consuming turn in ``_resolve_selected_memory_headers`` so a stale/mismatched
# prefetch is ignored, never applied to the wrong turn. ``max_workers=4`` lets a
# few turns' prefetches overlap (matching the pre-reuse one-thread-per-turn
# behaviour) without ever tearing a thread down mid-turn. Constructing the pool
# object spawns no thread (workers start on first submit), so this import stays
# side-effect free.
_MEMORY_PREFETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="memory-side-query-prefetch",
)

_MUSIC_CONTEXT_PROVIDER_IDS = ("spotify", "youtube_music")
_MUSIC_CONTEXT_PROVIDER_NAMES = {
    "spotify": "Spotify",
    "youtube_music": "YouTube Music",
}
_MUSIC_CONTEXT_SAMPLE_LIMIT = 8
_MUSIC_CONTEXT_MATCH_LIMIT = 5
_MUSIC_CONTEXT_RECENT_LIMIT = 10
_RESUMABLE_CHECKPOINT_SNAPSHOT_LIMIT = 2400


class ContextBuilder:
    """Assembles the full system-context dict/string for an LLM request.

    Instantiated once per ``AIController`` and reused across requests.
    Stateful only in that it holds references to the music player and
    MCP hub (set by the controller after initialisation).

    Usage::

        builder = ContextBuilder()
        builder.set_music_player(music)
        builder.set_mcp_hub(hub)
        builder.set_suppress_memory(True)   # optional: eval mode

        frames = builder.build_frames(user_id, user_text, user_name)
    """

    def __init__(self) -> None:
        self._music: Any = None
        self._mcp_hub: Any = None
        self._approval_manager: Any = None
        self._suppress_memory_context: bool = False
        self._conversation_state_manager: ConversationStateManager | None = None
        # Per-build cache of component strings, populated by build_frames() and read
        # via get_context_components() for telemetry (ai_controller pushes
        # these into the _ctx_system_context_components ContextVar).
        self._last_components: dict[str, str] = {}
        self._surfaced_memory_filenames: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Dependency injection
    # ------------------------------------------------------------------

    def set_music_player(self, music: Any) -> None:
        """Wire the music player for live playback state reads."""
        self._music = music

    def set_mcp_hub(self, hub: Any) -> None:
        """Wire the MCP hub for self-knowledge tool availability."""
        self._mcp_hub = hub

    def set_approval_manager(self, approval_manager: Any) -> None:
        """Wire the shared approval manager for pending confirmation context."""
        self._approval_manager = approval_manager

    def set_suppress_memory(self, suppress: bool) -> None:
        """Suppress user memory context (used in eval / reset scenarios)."""
        self._suppress_memory_context = suppress

    def set_conversation_state_manager(self, manager: ConversationStateManager | None) -> None:
        """Wire the default conversation state manager for plan-state frames."""
        self._conversation_state_manager = manager

    # ------------------------------------------------------------------
    # Memory side-query early-fire (latency lane B, #465)
    # ------------------------------------------------------------------

    # Uniform memory selection window (Claude parity, R5-P0-D). Shared by the
    # inline build path and the early-fire prefetch so both make the identical
    # selector call -- the model sees the same selection either way.
    _MEMORY_SELECTION_BUDGET = 5

    def prefetch_memory(self, user_id: str, user_text: str) -> None:
        """Fire the memory side-query as early as the user text is available.

        Lane B (#465). The ``memory`` provider is the one context source that
        makes a blocking second LLM round trip (``services/memory/selector.py``,
        ~1.2 s median). Kicking it off here -- at the top of the request path,
        before the pre-context glue (history expiry, provider-state reset,
        prompt-prep) -- lets its network wait overlap work the turn must do
        anyway, instead of the turn stalling on it at context-build time. The
        result is consumed by ``_build_memory_context`` for THIS turn's prompt:
        semantics-preserving (the model still sees exactly this turn's memory
        selection in this turn's prompt).

        Safe-by-construction:
        - Per-request/per-user isolation via a ContextVar (each request runs in
          its own asyncio task/context; the concurrent memory provider runs in a
          copy of that context). The ``(user_id, user_text)`` pair is re-checked
          at consume time, so a stale or mismatched prefetch is ignored, never
          applied to the wrong turn.
        - It does NOT touch ``self.client`` / provider state -- the side-query
          uses its own background OpenAI client
          (``services.openai_background``), so firing it early cannot race the
          provider-state reset in ai_controller.
        - On any failure it simply does not arm a prefetch; the inline selector
          call in ``_build_memory_context`` remains the fallback (today's path).
        """
        if self._suppress_memory_context:
            return
        if not str(user_text or "").strip():
            return
        try:
            from services.memory.dir import get_memory_dir, is_auto_memory_enabled

            if not is_auto_memory_enabled():
                return
            directory = get_memory_dir(user_id)
        except (OSError, RuntimeError, ValueError, ImportError, AttributeError) as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Memory prefetch skipped; directory unavailable (non-fatal): %s", detail)
            return

        # Snapshot the excluded set now. Nothing mutates the per-user surfaced
        # set between here and the consume in ``_build_memory_context`` within a
        # single turn (it is only updated at the END of that method), so the
        # early call is given the identical excluded set the inline call would
        # see -- identical selection.
        excluded = set(self._surfaced_memory_filenames.setdefault(user_id, set()))
        budget = self._MEMORY_SELECTION_BUDGET

        ctx = contextvars.copy_context()

        def _run() -> list[Any]:
            from services.memory.selector import select_relevant_memory_headers

            return list(
                select_relevant_memory_headers(
                    directory,
                    user_text,
                    limit=budget,
                    excluded_filenames=excluded,
                    user_id=user_id,
                )
            )

        try:
            # Reuse the long-lived pool (Lane B, #493): a bare submit onto a warm
            # worker instead of constructing + tearing down a ThreadPoolExecutor
            # (and blocking on Thread.start() under GIL pressure) every turn. The
            # submit copies THIS turn's contextvars and captures this turn's
            # (user_id, user_text) in the closure, so isolation is unchanged.
            future: Future[list[Any]] = _MEMORY_PREFETCH_EXECUTOR.submit(ctx.run, _run)
        except RuntimeError as exc:
            # Interpreter shutdown / executor unavailable: fall back to inline
            # (the consume path in _build_memory_context still runs the selector).
            logger.debug("Memory prefetch submit failed (non-fatal): %s", exc)
            return
        _memory_selection_prefetch.set(_MemorySelectionPrefetch(user_id=user_id, user_text=user_text, future=future))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_frames(
        self,
        user_id: str,
        user_text: str = "",
        user_name: str = "",
        channel_type: str | None = None,
        current_agent_id: str | None = None,
        current_task_id: str | None = None,
    ) -> PromptFrameBundle:
        """Assemble typed context frames for the LLM prompt boundary.

        Empty frame groups are silently skipped. The current user message is
        not included here; callers still send it through their existing route
        until Phase 5 moves the full prompt packet to frames.

        Args:
            user_text: The user's raw request text (used for memory relevance
                       ranking and context lookups).
            user_name: Display name of the current user (injected into the
                       self-knowledge block when known).
            user_id: Explicit user identifier for user-scoped context.
            channel_type: Channel identifier ("voice", "web", "phone", etc.).
                          Rendered as runtime context for the unified prompt.
            current_agent_id: Background agent id for the agent receiving this
                              context. Excluded from active-agent awareness.
            current_task_id: Compatibility alias for current_agent_id.

        Returns:
            A ``PromptFrameBundle`` containing only per-turn context frames.
        """
        owner_details = self._get_account_owner_details(user_id)
        provider_state: dict[str, Any] = {}
        manager = self._current_conversation_state_manager()
        request = RuntimeContextRequest(
            user_id=user_id,
            session_id=str(getattr(manager, "session_id", "") or "") or None,
            current_user_text=user_text,
            channel=channel_type,
            tool_state={
                "current_agent_id": current_agent_id or current_task_id,
                "user_name": user_name,
            },
            gate_state=None,
        )

        registry = RuntimeContextRegistry()

        def _coerce_meta(value: Any, *, origin: str, source_tag: str) -> list[Frame]:
            return self._coerce_context_frames(
                value,
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                origin=origin,
                source_tag=source_tag,
            )

        def _register_provider(
            *,
            name: str,
            origin: str,
            source_tag: str,
            priority: int,
            build: Any,
            ttl_turns: int | None = 1,
            relevance: str = "request",
        ) -> None:
            registry.register(
                CallableRuntimeContextProvider(
                    name=name,
                    origin=origin,
                    priority=priority,
                    ttl_turns=ttl_turns,
                    relevance=relevance,
                    build=lambda req: _coerce_meta(build(req), origin=origin, source_tag=source_tag),
                )
            )

        def _channel_context(req: RuntimeContextRequest) -> str:
            from services.llm.prompts import build_channel_context

            return build_channel_context(req.channel)

        def _playback_context(req: RuntimeContextRequest) -> list[Frame]:
            frames = _coerce_meta(
                self._get_playback_context(user_id=req.user_id, user_name=str(user_name or "")),
                origin="self_knowledge",
                source_tag="self-knowledge",
            )
            if frames:
                return frames
            fallback_playback = self._make_context_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                text="CURRENT SYSTEM STATE:\n",
                origin="self_knowledge",
                source_tag="self-knowledge",
            )
            return [fallback_playback] if fallback_playback is not None else []

        def _identity_context(_req: RuntimeContextRequest) -> list[Frame]:
            frames = _coerce_meta(
                self._build_account_owner_context(owner_details=owner_details),
                origin="settings",
                source_tag="account-owner",
            )
            provider_state["identity_present"] = bool(frames)
            return frames

        def _account_status_context(_req: RuntimeContextRequest) -> list[Frame]:
            # Desktop-only: the signed-in state lives in the desktop-local
            # GoTrue session store. On the cloud surface the request is
            # authenticated per-request through a different path, so this
            # local read would be misleading — skip it there.
            from ui.core.security import is_desktop_surface

            if not is_desktop_surface():
                return []
            return _coerce_meta(
                self._build_account_status_context(),
                origin="settings",
                source_tag="account-status",
            )

        def _profile_context(req: RuntimeContextRequest) -> list[Frame]:
            if bool(provider_state.get("identity_present")):
                return []
            return _coerce_meta(
                self._build_profile_context(
                    req.user_id,
                    user_text=req.current_user_text,
                    canonical_name=str(owner_details.get("full_name", "")),
                    canonical_address=owner_details.get("delivery_address"),
                ),
                origin="profile_store",
                source_tag="profile",
            )

        def _user_model_context(req: RuntimeContextRequest) -> list[Frame]:
            return _coerce_meta(
                self._build_user_model_context(
                    req.user_id,
                    user_text=req.current_user_text,
                    canonical_name=str(owner_details.get("full_name", "")),
                    canonical_address=owner_details.get("delivery_address"),
                ),
                origin="profile_store",
                source_tag="user-model",
            )

        def _active_agents_context(req: RuntimeContextRequest) -> list[Frame]:
            tool_state = req.tool_state or {}
            return _coerce_meta(
                self._build_active_agents_context(
                    req.user_id,
                    current_agent_id=str(tool_state.get("current_agent_id") or "") or None,
                ),
                origin="subagent",
                source_tag="active-agent",
            )

        def _user_capabilities_context(req: RuntimeContextRequest) -> str:
            from services.user_capabilities import build_context_for_user

            return build_context_for_user(req.user_id, surface=req.channel)

        _register_provider(
            name="channel",
            origin="channel",
            source_tag="channel",
            priority=10,
            build=_channel_context,
            relevance="always",
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="playback",
                origin="self_knowledge",
                priority=20,
                build=_playback_context,
                ttl_turns=1,
                relevance="request",
            )
        )
        _register_provider(
            name="music_route",
            origin="music_route",
            source_tag="music-route",
            priority=30,
            build=lambda req: self._build_music_route_context(req.user_id, req.current_user_text),
        )
        _register_provider(
            name="pending_gate",
            origin="gate_state",
            source_tag="gate-state",
            priority=40,
            build=lambda req: self.build_pending_gate_frames(req.user_id),
            relevance="always",
        )
        _register_provider(
            name="resumable_checkpoint",
            origin="task_checkpoint",
            source_tag="resumable-checkpoint",
            priority=45,
            build=lambda req: self.build_resumable_checkpoint_frames(req.user_id),
            relevance="always",
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="identity",
                origin="settings",
                priority=50,
                build=_identity_context,
                ttl_turns=1,
                relevance="always",
            )
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="account_status",
                origin="settings",
                priority=55,
                build=_account_status_context,
                ttl_turns=1,
                relevance="always",
            )
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="profile",
                origin="profile_store",
                priority=60,
                build=_profile_context,
                ttl_turns=1,
                relevance="always",
            )
        )
        _register_provider(
            name="delivery_address",
            origin="settings",
            source_tag="delivery-address",
            priority=70,
            build=lambda req: self._build_delivery_address_context(req.user_id),
            relevance="always",
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="user_model",
                origin="profile_store",
                priority=80,
                build=_user_model_context,
                ttl_turns=1,
                relevance="request",
            )
        )
        _register_provider(
            name="memory",
            origin="memory_store",
            source_tag="memory",
            priority=90,
            build=lambda req: self._build_memory_context(req.user_id, req.current_user_text),
            relevance="request",
        )
        _register_provider(
            name="user_capabilities",
            origin="user_capabilities",
            source_tag="user-capabilities",
            priority=100,
            build=_user_capabilities_context,
            relevance="request",
        )
        registry.register(
            CallableRuntimeContextProvider(
                name="active_agents",
                origin="subagent",
                priority=110,
                build=_active_agents_context,
                ttl_turns=1,
                relevance="request",
            )
        )
        _register_provider(
            name="task_plan",
            origin="plan_state",
            source_tag="task-plan",
            priority=120,
            build=lambda req: self._build_task_plan_context(),
            relevance="always",
        )

        system_frames: list[Frame] = []
        # PERF (#412): "memory" is the one provider that makes a serial,
        # blocking LLM round trip (services/memory/selector.py's side-query
        # selector, ~1.3 s median -- see
        # _diag/2026-07-08/latency_decomposition/ATTRIBUTION.md). Every other
        # provider here is local-only (settings/profile/state reads) and
        # completes in single-digit milliseconds. Building "memory" on a
        # background thread lets its network wait overlap with the rest of
        # context assembly instead of sitting in front of it, without
        # touching the sequential ordering the other providers rely on (see
        # RuntimeContextRegistry.build_all's docstring on the identity ->
        # profile coupling).
        provider_meta_frames = registry.build_all(request, concurrent_names=frozenset({"memory"}))

        component_parts: dict[str, list[str]] = {}
        for frame in provider_meta_frames:
            provider_name = str(frame.extra.get("runtime_context_provider") or "").strip()
            if not provider_name:
                continue
            frame_text = self._frame_text(frame)
            if frame_text:
                component_parts.setdefault(provider_name, []).append(frame_text)

        component_keys = (
            "channel",
            "playback",
            "music_route",
            "pending_gate",
            "resumable_checkpoint",
            "identity",
            "account_status",
            "profile",
            "delivery_address",
            "user_model",
            "memory",
            "user_capabilities",
            "active_agents",
            "task_plan",
        )
        components: dict[str, str] = {key: "\n\n".join(component_parts.get(key, [])) for key in component_keys}
        # Conversation repair is read from the typed stored frame chain now.
        components["conversation_repair"] = ""

        stored_frames = self._stored_conversation_frames()
        components["stored_frame_chain"] = self._frames_text(stored_frames)

        static_runtime_frames, volatile_runtime_frames = self._aggregate_runtime_context_frames(provider_meta_frames)
        self._last_components = components
        # R2-P0-A: durable identity/capability/memory-inventory rides
        # system_static_blocks (the cache-able segment). Volatile state
        # (playback, gate, subagents, task plan, music_route, …)
        # remains on the per-turn meta-user channel. Static frames are
        # NOT included in the message-chain ``frames`` list because
        # they are rendered as part of the system prompt, not as
        # in-stream user messages.
        return PromptFrameBundle(
            system_static_blocks=static_runtime_frames,
            system_dynamic_blocks=system_frames,
            meta_user_frames=volatile_runtime_frames,
            history_frames=stored_frames,
            frames=[*volatile_runtime_frames, *stored_frames],
        )

    def get_context_components(self) -> dict[str, str]:
        """Return the component strings from the most recent ``build_frames()`` call.

        Used by ``ai_controller`` to populate the
        ``_ctx_system_context_components`` ContextVar for telemetry /
        step-log attribution. Empty dict if ``build_frames()`` has never run.
        """
        return dict(self._last_components)

    def get_custom_instructions(self) -> str:
        """Return user-supplied custom agent instructions, if any.

        SaaS Custom Instructions feature: users can supply a personality /
        behavioral preamble that the prompt-frame builder can attach as
        custom instructions.

        Currently returns empty string — no persistence layer reads from
        settings yet.  Hook is live so the agent path doesn't raise
        ``AttributeError`` and so the SaaS UI can wire its value in later
        without another migration.
        """
        return ""

    # R2-P0-A (2026-05-30): origins whose content is durable across turns
    # within a session — they ride the prompt cache via
    # ``system_static_blocks`` instead of cache-busting every turn as a
    # per-turn ``<system-reminder>`` user-message. Origins outside this
    # set are genuinely volatile (date/time, playback state, pending-gate
    # state, active subagents, task plan) and remain on the per-turn
    # meta-user channel. Parity target: Claude Code TS
    # ``src/constants/prompts.ts:760-797`` ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY``
    # — env/account info lives in the system prompt array, cached.
    _STATIC_RUNTIME_CONTEXT_ORIGINS: frozenset[str] = frozenset(
        {
            "channel",
            "settings",
            "profile_store",
            "memory_store",
            "user_capabilities",
        }
    )

    @classmethod
    def _aggregate_runtime_context_frames(cls, frames: list[Frame]) -> tuple[list[Frame], list[Frame]]:
        """Partition runtime-context providers into (static, volatile) frames.

        R2-P0-A (2026-05-30, trace d74291f44b8c): the prior shape
        collapsed ALL providers into one per-turn meta-user
        ``<system-reminder>`` frame with ``ttl_turns=1`` — cache-busting
        every turn and re-emitting 3+ KB of durable identity (account
        owner, addresses, memory inventory, capability tier) on every
        request. Verified live on a math-probe trace: 4,279-char block
        on a "what is 1247 * 38" turn.

        Returns ``(static_frames, volatile_frames)``. Static frames
        carry durable identity/capability/memory-inventory bodies that
        change at most per-session — they go into
        ``PromptFrameBundle.system_static_blocks`` so they ride the
        provider's prompt cache. Volatile frames hold things that
        change per-turn (playback, pending-gate, subagents, task plan)
        and remain as the small per-turn meta-user reminder.
        """
        static_parts: list[str] = []
        volatile_parts: list[str] = []
        static_providers: list[str] = []
        volatile_providers: list[str] = []
        for frame in frames:
            text = cls._frame_text(frame)
            if not text:
                continue
            provider_name = str(frame.extra.get("runtime_context_provider") or frame.origin or "").strip()
            origin = (frame.origin or "").strip()
            is_static = origin in cls._STATIC_RUNTIME_CONTEXT_ORIGINS
            if is_static:
                static_parts.append(text)
                if provider_name:
                    static_providers.append(provider_name)
            else:
                volatile_parts.append(text)
                if provider_name:
                    volatile_providers.append(provider_name)

        static_frames: list[Frame] = []
        if static_parts:
            static_frames.append(
                Frame(
                    kind=FrameKind.SYSTEM_REMINDER,
                    role=FrameRole.SYSTEM,
                    blocks=(
                        SystemReminderBlock(
                            text="\n\n".join(static_parts),
                            source_tag="runtime-context-static",
                        ),
                    ),
                    is_meta=True,
                    origin="runtime_context_static",
                    ttl_turns=None,
                    relevance="always",
                    extra={"runtime_context_providers": static_providers},
                )
            )

        volatile_frames: list[Frame] = []
        if volatile_parts:
            volatile_frames.append(
                Frame(
                    kind=FrameKind.SYSTEM_REMINDER,
                    role=FrameRole.META_USER,
                    blocks=(
                        SystemReminderBlock(
                            text="\n\n".join(volatile_parts),
                            source_tag="runtime-context",
                        ),
                    ),
                    is_meta=True,
                    origin="runtime_context",
                    ttl_turns=1,
                    relevance="always",
                    extra={"runtime_context_providers": volatile_providers},
                )
            )

        return static_frames, volatile_frames

    @staticmethod
    def _make_context_frame(
        *,
        kind: FrameKind,
        role: FrameRole,
        text: str,
        origin: str | None,
        source_tag: str | None = None,
        task_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Frame | None:
        text_value = str(text or "").strip()
        if not text_value:
            return None
        if role is FrameRole.META_USER:
            blocks = (SystemReminderBlock(text=text_value, source_tag=source_tag),)
        else:
            blocks = (TextBlock(text=text_value),)
        return Frame(
            kind=kind,
            role=role,
            blocks=blocks,
            is_meta=role is FrameRole.META_USER,
            origin=origin,
            task_id=task_id,
            extra=dict(extra or {}),
        )

    @classmethod
    def _coerce_context_frames(
        cls,
        value: Any,
        *,
        kind: FrameKind,
        role: FrameRole,
        origin: str | None,
        source_tag: str | None = None,
    ) -> list[Frame]:
        """Accept migrated frame helpers and tolerate old string test doubles."""
        if value is None:
            return []
        if isinstance(value, Frame):
            return [value]
        if isinstance(value, str):
            frame = cls._make_context_frame(
                kind=kind,
                role=role,
                text=value,
                origin=origin,
                source_tag=source_tag,
            )
            return [frame] if frame is not None else []

        try:
            items = list(value)
        except TypeError:
            return []

        frames: list[Frame] = []
        for item in items:
            if isinstance(item, Frame):
                frames.append(item)
                continue
            if isinstance(item, str):
                frame = cls._make_context_frame(
                    kind=kind,
                    role=role,
                    text=item,
                    origin=origin,
                    source_tag=source_tag,
                )
                if frame is not None:
                    frames.append(frame)
        return frames

    @staticmethod
    def _frame_text(frame: Frame) -> str:
        parts: list[str] = []
        for block in frame.blocks:
            if isinstance(block, (TextBlock, SystemReminderBlock)):
                parts.append(block.text)
        return "\n".join(part for part in parts if part)

    @classmethod
    def _frames_text(cls, frames: list[Frame]) -> str:
        return "\n\n".join(text for text in (cls._frame_text(frame) for frame in frames) if text)

    def _current_conversation_state_manager(self) -> ConversationStateManager | None:
        try:
            from services.conversation.state_manager import (
                get_request_conversation_manager,
            )

            requested = get_request_conversation_manager()
            if requested is not None:
                return requested
        except Exception as exc:
            logger.debug("Request conversation manager lookup failed (non-fatal): %s", exc)
        return self._conversation_state_manager

    def _stored_conversation_frames(self, limit: int = 200) -> list[Frame]:
        """Pull stored chat history for the model — BEHAVIORAL-ONLY.

        R2-P0-C (2026-05-30): both calls below used to pass
        ``behavioral_only=False``, which deliberately defeated the
        state manager's own meta-frame filter (defined at
        ``services/conversation/state_manager.py:2092,2124``). The
        consequence: every prior turn's recovery / bail-status frames
        (e.g. the ``"Status: I stopped before I could finish the
        task..."`` runtime fallback emitted at
        ``intent/agent_executor.py:1126-1128``) was re-played into the
        model's next-turn context as nested ``<system-reminder>``
        user-side payloads — verified live on anchor trace
        ``d74291f44b8c``. That is a runtime layer surfacing its own
        meta-state back as if it were user content, which violates the
        "trust the model with raw context" rule (CLAUDE.md) and
        encourages the model to react to a recovery message it never
        emitted.

        ``behavioral_only=True`` is the documented escape hatch that
        skips ``is_meta=True`` frames; the state manager keeps the
        recovery rows for UI / debug / persistence, and the model only
        sees user + assistant content.
        """
        manager = self._current_conversation_state_manager()
        if manager is None:
            return []
        try:
            get_chain = getattr(manager, "get_message_chain", None)
            if callable(get_chain):
                frames = get_chain(limit=limit, behavioral_only=True)
            else:
                get_recent = getattr(manager, "get_recent_turns", None)
                if not callable(get_recent):
                    return []
                frames = get_recent(limit=limit, behavioral_only=True)
        except Exception as exc:
            logger.debug("Stored conversation frame-chain lookup failed (non-fatal): %s", exc)
            return []
        return [frame for frame in frames if isinstance(frame, Frame)]

    @staticmethod
    def _compact_context_value(value: Any, *, max_chars: int = 240) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    def _build_task_plan_context(self) -> list[Frame]:
        """Build a TASK_PLAN frame from the active conversation plan state."""
        manager = self._current_conversation_state_manager()
        if manager is None:
            return []
        try:
            get_active_plan = getattr(manager, "get_active_plan", None)
            if not callable(get_active_plan):
                return []
            plan = get_active_plan()
        except Exception as exc:
            logger.debug("Plan-state context lookup failed (non-fatal): %s", exc)
            return []
        if not isinstance(plan, dict):
            return []

        raw_steps = plan.get("steps")
        steps = raw_steps if isinstance(raw_steps, list) else []
        plan_id = self._compact_context_value(plan.get("id"), max_chars=80)
        status = self._compact_context_value(plan.get("status"), max_chars=40) or "active"
        if not steps and not plan_id:
            return []

        lines = ["TASK PLAN:", "Status: %s" % status]
        if plan_id:
            lines.insert(1, "Plan ID: %s" % plan_id)
        for step in steps[:20]:
            if not isinstance(step, dict):
                continue
            description = self._compact_context_value(step.get("description"), max_chars=180)
            if not description:
                continue
            step_status = self._compact_context_value(step.get("status"), max_chars=40) or "pending"
            lines.append("- [%s] %s" % (step_status, description))
            result = self._compact_context_value(step.get("result"), max_chars=220)
            if result:
                lines.append("  result: %s" % result)
        if len(steps) > 20:
            lines.append("- ... (%d more steps omitted)" % (len(steps) - 20))

        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text="\n".join(lines),
            origin="plan_state",
            source_tag="task-plan",
            extra={"plan_id": plan_id, "step_count": len(steps)},
        )
        return [frame] if frame is not None else []

    # ------------------------------------------------------------------
    # Individual context builders (private)
    # ------------------------------------------------------------------

    @staticmethod
    def _quote_context_fragment(value: Any, *, max_chars: int = 220) -> str:
        """Return a compact single-line quoted fragment for prompt context."""
        text = " ".join(str(value or "").replace('"', "'").split())
        if len(text) > max_chars:
            text = text[: max_chars - 3].rstrip() + "..."
        return '"%s"' % text

    # R2-P0-2 (2026-05-30): the per-turn music-intent classifier (a
    # prefix/marker stripper that fed a SQL search against the local
    # music library to inject "matches for current request" hints into
    # every user turn) was DELETED. It fired for EVERY message
    # regardless of intent — verified live on a Tokyo-population probe
    # — which violated the "Viola runtime must trust the model" rule in
    # CLAUDE.md (no runtime query-intent classifiers). The model now
    # sees the raw user text and decides whether to consult a music
    # tool. The static capability hints (library track count, sample
    # entries) still render, but only as steady-state metadata — they
    # no longer index against the user's current phrase. The classifier
    # function name itself is forbidden from reappearing by ratchet
    # check-no-runtime-music-intent-classifier.

    def _format_track_summary(self, track: Any) -> str:
        if not isinstance(track, dict):
            return ""
        title = str(track.get("title") or track.get("file_name") or "").strip()
        artist = str(track.get("artist") or "").strip()
        album = str(track.get("album") or "").strip()
        parts: list[str] = []
        if title:
            parts.append(title)
        if artist:
            parts.append("by %s" % artist)
        if album:
            parts.append("from %s" % album)
        return " ".join(parts)[:140]

    @staticmethod
    def _track_field(track: Any, key: str) -> str:
        if isinstance(track, dict):
            value = track.get(key)
        else:
            value = getattr(track, key, None)
            if value is None and hasattr(track, "model_dump"):
                try:
                    value = track.model_dump().get(key)
                except Exception:
                    value = None
        return str(value or "").strip()

    def _format_played_track_summary(self, track: Any) -> str:
        title = self._track_field(track, "title") or self._track_field(track, "id")
        artist = self._track_field(track, "artist")
        provider = self._track_field(track, "provider") or self._track_field(track, "source")
        if not title:
            return ""
        parts = [title]
        if artist:
            parts.append("by %s" % artist)
        if provider:
            parts.append("(provider=%s)" % provider)
        return " ".join(parts)[:160]

    def _build_recent_music_context(self, user_id: str = "") -> str:
        """Return empty — per-turn recently-played-tracks injection retired.

        R2-P0-B (2026-05-30, trace d74291f44b8c): the prior
        implementation rendered up to N recently played tracks (durable
        per-user recents from ``MusicRecentsService`` + in-memory player
        history) into the runtime context on EVERY turn, regardless of
        intent. Verified live on a "what is 1247 * 38" probe — 10
        unrelated tracks were stuffed into a math query's context. Same
        anti-pattern class as the music-intent classifier deleted in
        R2-P0-2 and the prefix classifiers deleted in earlier de-box
        waves: a per-turn injection of domain history into a non-domain
        conversation. Claude Code TS has no equivalent domain-history
        injection. The model can read recents via a music tool when it
        judges them relevant; the runtime no longer hand-feeds them on
        every turn.

        The method is kept (returning empty) so its call site in
        ``_build_music_route_context`` does not need to know the
        injection is gone; a future cleanup can fold the empty branch
        out.
        """
        del user_id  # R2-P0-B: no per-turn domain-history injection.
        return ""

    def _build_user_music_preferences_context(self, user_id: str = "") -> str:
        """Surface known user music taste so the LLM can curate intelligently.

        Reads the music sub-dict from UserPreferencesStore (e.g. preferred
        artist, genre, mood, style) and emits a single-line hint. Empty when
        no preferences are stored — never invent values.

        Personalization audit: every successful injection records a
        ``music_preferences_context_injected`` event with the field key list
        (not values) and the user_id so the user-facing personalization
        ledger reflects every learned-profile path that reaches the LLM.
        Without this hook the music-route preference frame would have been
        a parallel, unaudited injection of personalized data into the LLM
        prompt — fail-closed if the audit write cannot persist.
        """
        if not user_id:
            return ""
        try:
            from services.user_model.profile import get_user_model

            prefs = get_user_model(user_id).preferences or {}
        except Exception as exc:
            logger.debug("user_model preferences read failed: %s", exc)
            return ""
        music_prefs = prefs.get("music") if isinstance(prefs, dict) else None
        if not isinstance(music_prefs, dict) or not music_prefs:
            return ""
        parts: list[str] = []
        used_keys: list[str] = []
        for key in ("artist", "genre", "mood", "style", "platform"):
            value = music_prefs.get(key)
            if isinstance(value, str) and value.strip():
                parts.append("%s=%s" % (key, value.strip()))
                used_keys.append(key)
        if not parts:
            return ""
        try:
            from services.profile.personalization_audit import (
                PersonalizationAuditError,
                require_personalization_event,
            )

            require_personalization_event(
                user_id,
                "music_preferences_context_injected",
                details={"keys": used_keys, "source": "user_model.music"},
            )
        except PersonalizationAuditError as audit_exc:
            logger.warning(
                "Music preference injection withheld; personalization audit failed: %s",
                audit_exc,
            )
            return ""
        except ImportError as audit_exc:
            logger.warning(
                "Music preference injection withheld; personalization audit unavailable: %s",
                audit_exc,
            )
            return ""
        return "User music preferences: %s." % ", ".join(parts)

    def _build_local_music_library_context(self, user_text: str) -> list[str]:
        """Return steady-state local-library capability hints (no per-turn classifier).

        R2-P0-2 (2026-05-30): the prior implementation called a
        deleted music-keyword prefix stripper on ``user_text`` and ran
        a SQL search against the local library for every user turn,
        then injected "Local metadata matches for current request: N /
        Matching local entries: <track list>" into the model's
        context. That fired on EVERY message regardless of intent
        (verified live on a Tokyo-population probe). It violated
        "Viola runtime must trust the model" (CLAUDE.md). The per-turn
        match-injection is gone; only steady-state hints remain.

        ``user_text`` is now unused but kept in the signature for
        compatibility with the registered runtime-context build path.
        """
        del user_text  # R2-P0-2: no more per-turn classifier.
        lines: list[str] = [
            "Local library metadata fields: title, artist, album, file name, media type.",
            "Local library metadata not reliable for: genre, mood, topic, year.",
        ]
        try:
            from music.providers.local.db import get_local_library_repo

            repo = get_local_library_repo()
            initialize = getattr(repo, "initialize", None)
            if callable(initialize):
                initialize()
            conn = repo.connection
            try:
                total = int(conn.execute("SELECT COUNT(*) FROM library").fetchone()[0])
            except Exception as exc:
                logger.debug("Local library context count failed: %s", exc)
                lines.append("Local library index: unavailable or not scanned.")
                return lines

            lines.append("Local library indexed tracks: %d." % total)

            if total:
                rows = conn.execute(
                    """
                    SELECT title, artist, album, file_name, media_type
                    FROM library
                    ORDER BY COALESCE(NULLIF(title, ''), file_name)
                    LIMIT ?
                    """,
                    (_MUSIC_CONTEXT_SAMPLE_LIMIT,),
                ).fetchall()
                sample_text = "; ".join(
                    summary for summary in (self._format_track_summary(dict(row)) for row in rows) if summary
                )
                if sample_text:
                    lines.append("Sample local entries: %s." % sample_text[:520])
        except Exception as exc:
            logger.debug("Local library context failed (non-fatal): %s", exc)
            lines.append("Local library index: unavailable.")
        return lines

    def _build_music_provider_context(self, user_id: str) -> list[str]:
        lines: list[str] = []
        active_provider = ""
        try:
            from music.providers.active_provider import get_active_music_provider_id

            active_provider = str(get_active_music_provider_id(user_id=user_id) or "").strip()
        except Exception as exc:
            logger.debug("Music provider context settings lookup failed: %s", exc)

        if not active_provider:
            active_provider = "none configured"
        lines.append("Active music provider setting: %s." % active_provider)

        statuses: dict[str, str] = {}
        try:
            from music.consent import get_consent_service

            for status in get_consent_service().list_statuses(user_id=user_id):
                provider_id = str(getattr(status, "provider_id", "") or "")
                if provider_id not in _MUSIC_CONTEXT_PROVIDER_IDS:
                    continue
                state_obj = getattr(status, "state", None)
                state_value = str(getattr(state_obj, "value", state_obj) or "unknown")
                statuses[provider_id] = "authenticated" if state_value == "linked" else state_value
        except Exception as exc:
            logger.debug("Music provider status context failed: %s", exc)

        for provider_id in _MUSIC_CONTEXT_PROVIDER_IDS:
            label = _MUSIC_CONTEXT_PROVIDER_NAMES[provider_id]
            state = statuses.get(provider_id, "unknown")
            lines.append("%s authentication: %s." % (label, state))

        try:
            from music.providers.selection import is_youtube_anonymous_search_available

            youtube_fallback = "available" if is_youtube_anonymous_search_available() else "unavailable"
        except Exception as exc:
            logger.debug("YouTube anonymous fallback context failed: %s", exc)
            youtube_fallback = "unknown"
        lines.append("Anonymous YouTube fallback: %s." % youtube_fallback)
        return lines

    def _build_browser_music_context(self) -> str:
        if self._mcp_hub is None:
            return "Browser tools for ad-hoc lookup: unknown."
        try:
            list_tools = getattr(self._mcp_hub, "list_tools", None)
            if not callable(list_tools):
                return "Browser tools for ad-hoc lookup: unknown."
            tools = list_tools(interactive=True)
            names = {
                str((tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "")) or "") for tool in tools
            }
            if any(name.startswith("browser_") for name in names):
                return "Browser tools for ad-hoc lookup: available."
            return "Browser tools for ad-hoc lookup: unavailable."
        except Exception as exc:
            logger.debug("Browser music context failed: %s", exc)
            return "Browser tools for ad-hoc lookup: unknown."

    def _build_music_route_context(self, user_id: str, user_text: str) -> list[Frame]:
        """Build bounded music provider and library facts without scripting replies."""
        lines = ["# Music context"]
        provider_lines, _provider_facts = self._build_music_provider_context_with_facts(user_id)
        lines.extend(provider_lines)
        lines.append(
            "Provider omitted resolver cascade: configured provider, connected token provider, "
            "local indexed-field match, anonymous YouTube fallback when available."
        )
        local_lines, _local_facts = self._build_local_music_library_context_with_facts(user_text)
        lines.extend(local_lines)
        recent_music_context = self._build_recent_music_context(user_id=user_id)
        if recent_music_context:
            lines.append(recent_music_context)
        prefs_context = self._build_user_music_preferences_context(user_id=user_id)
        if prefs_context:
            lines.append(prefs_context)
        lines.append(self._build_browser_music_context())
        lines.append("Filename text is not genre, mood, or topic metadata.")
        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text="\n".join(lines),
            origin="music_route",
            source_tag="music-route",
        )
        return [frame] if frame is not None else []

    def _build_music_provider_context_with_facts(self, user_id: str) -> tuple[list[str], dict[str, Any]]:
        """Return provider-context lines plus structured facts for the recommendation builder."""
        lines = self._build_music_provider_context(user_id)
        facts: dict[str, Any] = {
            "active_provider": "",
            "authenticated_providers": [],
            "anonymous_youtube_available": False,
        }
        for line in lines:
            if line.startswith("Active music provider setting: "):
                value = line[len("Active music provider setting: ") :].rstrip(".").strip()
                facts["active_provider"] = value
            elif " authentication: " in line:
                label, _, status = line.partition(" authentication: ")
                if status.rstrip(".").strip().lower() == "authenticated":
                    for pid, name in _MUSIC_CONTEXT_PROVIDER_NAMES.items():
                        if name == label.strip():
                            facts["authenticated_providers"].append(pid)
                            break
            elif line.startswith("Anonymous YouTube fallback: "):
                value = line[len("Anonymous YouTube fallback: ") :].rstrip(".").strip().lower()
                facts["anonymous_youtube_available"] = value == "available"
        return lines, facts

    def _build_local_music_library_context_with_facts(self, user_text: str) -> tuple[list[str], dict[str, Any]]:
        """Return local-library context lines plus structured facts for the recommendation builder.

        R2-P0-2: facts no longer carry a per-turn classifier-driven
        ``context_query`` / ``match_count``. The recommendation builder
        callers must derive the user's intent from the user's message
        itself, not from a runtime keyword-classifier output.
        """
        lines = self._build_local_music_library_context(user_text)
        facts: dict[str, Any] = {"context_query": "", "match_count": None}
        return lines, facts

    @staticmethod
    def _payment_summary_text(order_summary: Any) -> str:
        """Render a payment order summary without exposing payment credentials."""
        if not isinstance(order_summary, dict):
            return str(order_summary or "").strip()

        parts: list[str] = []
        for key in ("merchant", "total", "description", "summary", "notes"):
            value = str(order_summary.get(key) or "").strip()
            if value:
                label = key.replace("_", " ").title()
                parts.append("%s: %s" % (label, value))
        if not parts and order_summary:
            parts.append(str(order_summary))
        return " ".join(parts)

    def build_resumable_checkpoint_frames(self, user_id: str) -> list[Frame]:
        """Build typed prompt frames for an unfinished non-gate task checkpoint."""
        if not user_id:
            return []

        try:
            from intent.task_checkpoint import (
                RESUMABLE_CHECKPOINT_CONTEXT_TTL_SECONDS,
                get_latest_resumable,
            )

            checkpoint = get_latest_resumable(
                user_id,
                max_age_seconds=RESUMABLE_CHECKPOINT_CONTEXT_TTL_SECONDS,
            )
        except (ImportError, RuntimeError, OSError, TypeError, ValueError) as exc:
            logger.debug("Resumable checkpoint lookup failed (non-fatal): %s", exc)
            return []

        if checkpoint is None:
            return []
        payload = self._build_resumable_checkpoint_payload(checkpoint)
        if payload is None:
            return []
        text = "<resumable-task-checkpoint>\n%s\n</resumable-task-checkpoint>" % json.dumps(
            payload,
            sort_keys=True,
        )
        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text=text,
            origin="task_checkpoint",
            source_tag="resumable-checkpoint",
            task_id=str(payload.get("task_id") or "") or None,
        )
        return [frame] if frame is not None else []

    def _build_resumable_checkpoint_payload(self, checkpoint: Any) -> dict[str, Any] | None:
        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if not status or status == "waiting_for_user":
            return None
        context = getattr(checkpoint, "context", None) or {}
        if isinstance(context, dict) and str(context.get("pending_gate_type") or "").strip():
            return None

        browser_state = self._latest_checkpoint_browser_state(checkpoint)
        payload: dict[str, Any] = {
            "schema": "viola.resumable_task_checkpoint.v1",
            "task_id": str(getattr(checkpoint, "task_id", "") or "").strip(),
            "status": status,
            "outcome": str(getattr(checkpoint, "outcome", "") or "").strip(),
            "task_description": self._checkpoint_text(getattr(checkpoint, "task_description", ""), limit=600),
            "step_index": int(getattr(checkpoint, "step_index", 0) or 0),
        }
        payload.update(browser_state)
        if not payload["task_id"] and not browser_state:
            return None
        return {key: value for key, value in payload.items() if value not in ("", None, [])}

    @classmethod
    def _latest_checkpoint_browser_state(cls, checkpoint: Any) -> dict[str, Any]:
        steps = list(getattr(checkpoint, "steps", None) or [])
        for step in reversed(steps):
            tool = str(getattr(step, "tool", "") or "").strip()
            if not tool.startswith("browser"):
                continue
            payload = cls._checkpoint_step_payload(getattr(step, "output", ""))
            state = cls._browser_state_from_payload(payload)
            if not state:
                continue
            state["last_browser_tool"] = tool
            index = getattr(step, "index", None)
            if index is not None:
                state["last_browser_step_index"] = index
            return state
        return {}

    @staticmethod
    def _checkpoint_step_payload(output: Any) -> dict[str, Any]:
        if isinstance(output, dict):
            return output
        if not isinstance(output, str) or not output.strip():
            return {}
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _browser_state_from_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        candidates = [payload]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)

        for candidate in candidates:
            url = str(candidate.get("url") or candidate.get("current_url") or "").strip()
            title = str(candidate.get("title") or "").strip()
            snapshot = cls._checkpoint_text(
                candidate.get("snapshot") or candidate.get("page_snapshot") or "",
                limit=_RESUMABLE_CHECKPOINT_SNAPSHOT_LIMIT,
            )
            if not url and not title and not snapshot:
                continue
            state: dict[str, Any] = {}
            if url:
                state["last_browser_url"] = url
            if title:
                state["last_browser_title"] = title
            if snapshot:
                state["last_browser_snapshot"] = snapshot
            return state
        return {}

    @staticmethod
    def _checkpoint_text(value: Any, *, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def build_pending_gate_frames(self, user_id: str) -> list[Frame]:
        """Build typed prompt frames for pending signature/payment or confirmation gates."""
        confirmation_frames = self._build_pending_confirmation_frames()
        if not user_id:
            return confirmation_frames

        try:
            from intent.task_checkpoint import (
                WAITING_GATE_CONTEXT_TTL_SECONDS,
                get_latest_waiting_checkpoint,
            )

            checkpoint = get_latest_waiting_checkpoint(
                user_id,
                max_age_seconds=WAITING_GATE_CONTEXT_TTL_SECONDS,
            )
        except Exception as exc:
            logger.debug("Pending gate checkpoint lookup failed (non-fatal): %s", exc)
            checkpoint = None

        if checkpoint is not None:
            if getattr(checkpoint, "status", "") != "waiting_for_user":
                return confirmation_frames
            context = getattr(checkpoint, "context", None) or {}
            gate_type = str(context.get("pending_gate_type") or "").strip().lower()
            if gate_type == "signature":
                frame = self._build_signature_gate_frame_from_checkpoint(checkpoint)
                return ([frame] if frame is not None else []) + confirmation_frames

            if gate_type == "payment":
                frame = self._build_payment_gate_frame_from_checkpoint(checkpoint)
                return ([frame] if frame is not None else []) + confirmation_frames

        manager_frame = self._build_payment_gate_frame_from_manager(user_id)
        return ([manager_frame] if manager_frame is not None else []) + confirmation_frames

    def _build_pending_confirmation_frames(self) -> list[Frame]:
        pending_provider = getattr(self._approval_manager, "pending_confirmations_for_prompt", None)
        if not callable(pending_provider):
            return []

        try:
            pending_items = pending_provider()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Pending confirmation lookup failed (non-fatal): %s", exc)
            return []

        frames: list[Frame] = []
        for item in pending_items:
            if not isinstance(item, dict):
                continue
            confirmation_id = str(item.get("confirmation_id") or "").strip()
            tool_name = str(item.get("tool_name") or "").strip()
            if not confirmation_id or not tool_name:
                continue
            action_description = str(item.get("action_description") or "").strip()
            tool_args = item.get("tool_args") if isinstance(item.get("tool_args"), dict) else {}
            confirmation_args = item.get("confirmation_args") if isinstance(item.get("confirmation_args"), dict) else {}
            lines = [
                "name: DEFERRED_CONFIRMATION",
                "status: awaiting_user_confirmation",
                "tool_name: %s" % tool_name,
                "confirmation_id: %s" % confirmation_id,
                "risk: %s" % str(item.get("risk") or "").strip(),
                "action: %s" % self._quote_context_fragment(action_description or "Pending tool action"),
                "expires_in_seconds: %s" % int(item.get("expires_in_seconds") or 0),
                "tool_args_json: %s" % json.dumps(tool_args, sort_keys=True, separators=(",", ":"), default=str),
                "confirmation_args_json: %s"
                % json.dumps(
                    confirmation_args,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ),
                (
                    "instruction: If the current user explicitly confirms this same pending action, call "
                    "tool_name with tool_args_json plus confirmation_args_json. If the user cancels, do not "
                    "call the tool. If the user changes details, call the tool with the updated details and "
                    "without confirmation_args_json so safety can re-prompt."
                ),
            ]
            frames.append(
                Frame(
                    kind=FrameKind.SYSTEM_REMINDER,
                    role=FrameRole.META_USER,
                    blocks=(SystemReminderBlock(text="\n".join(lines), source_tag="pending-confirmation"),),
                    is_meta=True,
                    origin="deferred_confirmation",
                    task_id=str(item.get("created_task_id") or "") or None,
                    extra={
                        "runtime_context_provider": "pending_confirmation",
                        "confirmation_id": confirmation_id,
                        "tool_name": tool_name,
                    },
                )
            )
        return frames

    def _build_signature_gate_frame_from_checkpoint(self, checkpoint: Any) -> Frame | None:
        """Build a signature gate GATE_STATE frame from a waiting checkpoint."""
        context = getattr(checkpoint, "context", None) or {}
        task_id = str(getattr(checkpoint, "task_id", "") or "").strip()
        if not task_id:
            return None
        page = str(context.get("signature_gate_page_url") or context.get("gate_page_url") or "").strip()
        summary = str(context.get("pending_question") or "").strip()
        summary = gate_message_body(summary, "signature")
        summary = summary.replace("Reply yes to sign and continue, or tell me what to change", "").strip()
        if not summary:
            summary = "Legal signature or certification is waiting for the user's decision"
        return self._format_gate_state_frame(
            gate_type="signature",
            status=self._gate_state_status(context, default="awaiting_signature"),
            task_id=task_id,
            token=str(context.get("signature_token") or context.get("token") or "").strip(),
            confirmation_url=page,
            summary=summary,
            last_blocker=self._gate_last_blocker(context, default="awaiting_signature_decision"),
            required_fields=self._gate_field_list(context.get("required_fields"), default=["signature_decision"]),
            collected_fields=self._gate_field_list(
                context.get("collected_fields"),
                default=self._gate_collected_fields(
                    {
                        "task_id": task_id,
                        "signature_gate_page_url": page,
                        "summary": summary,
                    }
                ),
            ),
            origin="signature_gate",
        )

    def _build_payment_gate_frame_from_checkpoint(self, checkpoint: Any) -> Frame | None:
        """Build a payment gate GATE_STATE frame from a waiting checkpoint."""
        context = getattr(checkpoint, "context", None) or {}
        token = str(context.get("confirm_token") or "").strip()
        if not token:
            return None

        session = None
        try:
            from services.payments.confirmation import (
                ConfirmationStatus,
                get_confirmation_manager,
            )

            mgr = get_confirmation_manager()
            session = mgr.get_session(token)
            if session is not None and session.status not in (
                ConfirmationStatus.PENDING,
                ConfirmationStatus.CONFIRMED,
            ):
                return None
        except Exception as exc:
            logger.debug("Payment gate manager lookup failed (non-fatal): %s", exc)

        page = str(context.get("confirmation_url") or "").strip()
        order_summary = getattr(session, "order_summary", None) if session is not None else None
        if order_summary is None:
            order_summary = context.get("payment_order_summary")
        summary = self._payment_summary_text(order_summary) or str(context.get("pending_question") or "").strip()
        task_id = str(getattr(checkpoint, "task_id", "") or getattr(session, "task_id", "") or "").strip()
        return self._format_gate_state_frame(
            gate_type="payment",
            status=self._payment_gate_status(context, getattr(session, "status", None)),
            token=token,
            task_id=task_id,
            confirmation_url=page,
            summary=summary,
            last_blocker=self._gate_last_blocker(context, default="awaiting_payment_confirmation"),
            required_fields=self._gate_field_list(context.get("required_fields"), default=["payment_confirmation"]),
            collected_fields=self._gate_field_list(
                context.get("collected_fields"),
                default=self._gate_collected_fields(order_summary)
                + self._gate_collected_fields({"confirm_token": token, "confirmation_url": page}),
            ),
            origin="payment_gate",
        )

    def _build_payment_gate_frame_from_manager(self, user_id: str) -> Frame | None:
        """Build a payment gate GATE_STATE frame from active confirmation state."""
        try:
            from services.payments.confirmation import (
                build_public_confirmation_url,
                get_confirmation_manager,
            )

            mgr = get_confirmation_manager()
            token = mgr.get_active_session_for_user(user_id)
            if not token:
                return None
            session = mgr.get_session(token)
            if session is None:
                return None
            page = build_public_confirmation_url(token)
            order_summary = getattr(session, "order_summary", None)
            return self._format_gate_state_frame(
                gate_type="payment",
                status=self._payment_gate_status({}, getattr(session, "status", None)),
                token=token,
                task_id=str(getattr(session, "task_id", "") or ""),
                confirmation_url=page,
                summary=self._payment_summary_text(order_summary),
                last_blocker="active_payment_confirmation_session",
                required_fields=["payment_confirmation"],
                collected_fields=self._gate_collected_fields(order_summary)
                + self._gate_collected_fields({"confirm_token": token, "confirmation_url": page}),
                origin="payment_gate",
            )
        except Exception as exc:
            logger.debug("Payment gate active-session lookup failed (non-fatal): %s", exc)
            return None

    @staticmethod
    def _gate_state_status(context: dict[str, Any], *, default: str) -> str:
        for key in ("gate_status", "pending_gate_status", "status"):
            value = str(context.get(key) or "").strip()
            if value:
                return value
        return default

    @classmethod
    def _payment_gate_status(cls, context: dict[str, Any], session_status: Any) -> str:
        raw_status = str(getattr(session_status, "value", "") or session_status or "").strip()
        if raw_status == "confirmed":
            return "confirmed"
        return cls._gate_state_status(context, default="awaiting_confirmation")

    @staticmethod
    def _gate_last_blocker(context: dict[str, Any], *, default: str) -> str:
        for key in ("last_blocker", "blocker", "pending_gate_last_blocker"):
            value = str(context.get(key) or "").strip()
            if value:
                return value
        return default

    @staticmethod
    def _gate_field_list(value: Any, *, default: list[str]) -> list[str]:
        if isinstance(value, dict):
            fields = [str(key).strip() for key, item in value.items() if str(key).strip() and bool(item)]
            return fields or list(default)
        if isinstance(value, (list, tuple, set)):
            fields = [str(item).strip() for item in value if str(item).strip()]
            return fields or list(default)
        if isinstance(value, str) and value.strip():
            fields = [part.strip() for part in value.split(",") if part.strip()]
            return fields or [value.strip()]
        return list(default)

    @staticmethod
    def _gate_collected_fields(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        fields: list[str] = []
        for key, item in value.items():
            if str(key).strip() and item not in (None, "", [], {}):
                fields.append(str(key).strip())
        return fields

    @staticmethod
    def _gate_state_attr(value: str) -> str:
        return str(value or "").replace('"', "'").strip()

    def _format_gate_state_frame(
        self,
        *,
        gate_type: str,
        status: str,
        task_id: str,
        token: str,
        confirmation_url: str,
        summary: str,
        last_blocker: str,
        required_fields: list[str],
        collected_fields: list[str],
        origin: str | None,
    ) -> Frame:
        """Build the schema-locked GATE_STATE frame."""
        gate_name = "%s_GATE" % gate_type.upper()
        safe_status = status or "awaiting_user"
        if gate_type == "signature":
            approval_action = "resume_signature_gate"
            decline_action = "cancel_signature_gate"
        elif gate_type == "payment":
            approval_action = "resume_payment_gate"
            decline_action = "cancel_payment_gate"
        else:
            approval_action = ""
            decline_action = ""
        gate_state = {
            "gate_type": gate_type,
            "name": gate_name,
            "status": safe_status,
            "task_id": task_id,
            "token": token,
            "confirmation_url": confirmation_url,
            "summary": summary,
            "last_blocker": last_blocker,
            "required_fields": list(required_fields),
            "collected_fields": list(dict.fromkeys(collected_fields)),
            "approval_action": approval_action,
            "decline_action": decline_action,
            "do_not_treat_prior_bail_messages_as_examples": True,
        }
        lines = [
            "name: %s" % self._gate_state_attr(gate_name),
            "gate_type: %s" % gate_type,
            "status: %s" % self._gate_state_attr(safe_status),
        ]
        if task_id:
            lines.append("task_id: %s" % task_id)
        if token:
            lines.append("token: %s" % token)
        if confirmation_url:
            lines.append("confirmation_url: %s" % confirmation_url)
        lines.extend(
            [
                "summary: %s" % self._quote_context_fragment(summary or "Gate is waiting for the user's decision"),
                "last_blocker: %s" % (last_blocker or "awaiting_user"),
                "required_fields: %s" % ", ".join(required_fields),
                "collected_fields: %s" % ", ".join(gate_state["collected_fields"]),
                "approval_action: %s" % approval_action,
                "decline_action: %s" % decline_action,
                "do_not_treat_prior_bail_messages_as_examples: true",
            ]
        )
        return Frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            blocks=(SystemReminderBlock(text="\n".join(lines), source_tag="gate-state"),),
            is_meta=True,
            origin=origin,
            task_id=task_id or None,
            extra=gate_state,
        )

    def _get_account_owner_details(self, user_id: str | None = None) -> dict[str, Any]:
        """Return canonical account-owner identity details from user settings."""
        try:
            from ui.settings_manager import get_settings_manager

            sm = get_settings_manager()
            if user_id:
                user_name = str(sm.get_user_setting(user_id, "user_name", "") or "").strip()
            else:
                user_name = str(sm.get("user_name", "") or "").strip()
            if not user_name:
                return {}

            details: dict[str, Any] = {"full_name": user_name}
            parts = user_name.split()
            if parts:
                details["first_name"] = parts[0]
            if len(parts) > 1:
                details["last_name"] = parts[-1]

            if user_id:
                addr = sm.get_user_setting(user_id, "delivery_address", {})
            else:
                addr = sm.get("delivery_address", {})
            if isinstance(addr, dict):
                details["delivery_address"] = {
                    "street": str(addr.get("street", "")).strip(),
                    "city": str(addr.get("city", "")).strip(),
                    "state": str(addr.get("state", "")).strip(),
                    "zip": str(addr.get("zip", "")).strip(),
                }
            return details
        except Exception as exc:
            logger.debug("Account owner identity lookup failed (non-fatal): %s", exc)
            return {}

    def _build_account_owner_context(self, owner_details: dict[str, Any] | None = None) -> list[Frame]:
        """Build the canonical ACCOUNT OWNER identity block.

        Single authoritative source of "who is the user" for form-filling,
        ordering, and account creation.  Reads from ``settings.json``
        (user_name + delivery_address) — NOT from memory or user_profile.json
        which may contain stale test data or third-party contact information.
        """
        try:
            owner_details = owner_details or self._get_account_owner_details()
            user_name = str(owner_details.get("full_name", "")).strip()
            if not user_name:
                return []

            parts = user_name.strip().split()
            first_name = parts[0] if parts else ""
            last_name = parts[-1] if len(parts) > 1 else ""

            lines = [
                "ACCOUNT OWNER (use this identity for ALL forms, orders, and registrations):",
                "  Full Name: %s" % user_name,
            ]
            if first_name:
                lines.append("  First Name: %s" % first_name)
            if last_name:
                lines.append("  Last Name: %s" % last_name)

            addr = owner_details.get("delivery_address", {})
            if isinstance(addr, dict):
                street = str(addr.get("street", "")).strip()
                city = str(addr.get("city", "")).strip()
                state = str(addr.get("state", "")).strip()
                zip_code = str(addr.get("zip", "")).strip()
                if street:
                    lines.append("  Street: %s" % street)
                if city:
                    lines.append("  City: %s" % city)
                if state:
                    lines.append("  State: %s" % state)
                if zip_code:
                    lines.append("  ZIP: %s" % zip_code)

            lines.append(
                "Any other names in your memories are CONTACTS, not the account owner. "
                "When a form asks for YOUR name, use the account owner above."
            )
            frame = self._make_context_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                text="\n".join(lines),
                origin="settings",
                source_tag="account-owner",
            )
            return [frame] if frame is not None else []
        except Exception as exc:
            logger.debug("Account owner context failed (non-fatal): %s", exc)
            return []

    def _build_account_status_context(self) -> list[Frame]:
        """Build the VIOLA ACCOUNT sign-in status block.

        The authoritative answer to identity questions about THIS Viola app
        ("am I logged in", "what account is this", "what's my email"). Reads
        the desktop-local GoTrue session store (ground truth for desktop
        sign-in) with a local-only, no-network lookup, so it is cheap on every
        turn. This is distinct from the ACCOUNT OWNER block, which is a
        manually-entered display name/address used for form-filling and says
        nothing about whether a Viola account is actually signed in.
        """
        try:
            from auth.desktop_session import get_desktop_account_identity

            identity = get_desktop_account_identity()
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Account status context lookup failed (non-fatal): %s", exc)
            return []

        header = "VIOLA ACCOUNT (this Viola app's own sign-in state on this device):"
        if identity.signed_in:
            lines = [header, "  Signed in: yes"]
            if identity.email:
                lines.append("  Account email: %s" % identity.email)
            if identity.user_id:
                plan_line = self._build_plan_status_line(identity.user_id)
                if plan_line:
                    lines.append(plan_line)
        else:
            lines = [
                header,
                "  Signed in: no",
                "  No Viola account is signed in on this device. Sign-in lives in the Viola app's Account settings.",
            ]

        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text="\n".join(lines),
            origin="settings",
            source_tag="account-status",
        )
        return [frame] if frame is not None else []

    def _build_plan_status_line(self, user_id: str) -> str:
        """Build the one-line real commercial-plan status for the VIOLA ACCOUNT block.

        Resolves through ``resolve_runtime_plan_id`` -- the same billing-authoritative
        function the entitlement/spend-cap enforcement path uses (mirrored from the
        live Stripe subscription state on both surfaces via ``billing_plan_id``, see
        ``billing/service.py`` and ``core/request_context.py``) -- so the answer here
        always matches what the account dashboard shows. This is real subscription
        state, not the internal tool-permission tier self_knowledge.py reports
        separately (issue #1406); "what plan am I on" has nothing else in context to
        ground it on today (issue #2590).
        """
        try:
            from billing.models import get_plan
            from core.request_context import resolve_runtime_plan_id

            plan_id = resolve_runtime_plan_id(user_id)
            plan = get_plan(plan_id)
            plan_name = plan.name if plan is not None else plan_id
            return "  Plan: %s" % plan_name
        except Exception as exc:  # noqa: BLE001, RUF100 - best-effort context; never fails prompt build
            logger.debug("Plan status line lookup failed (non-fatal): %s", exc)
            return ""

    def _get_playback_context(self, user_id: str, user_name: str = "") -> list[Frame]:
        """Build self-knowledge context for LLM.

        Wires the music player reference into the builder so it reads
        playback state, volume, and queue directly from the player
        (ground truth) instead of the StateHub (which may be stale).

        Args:
            user_name: Display name of the current user (if known).
            user_id: User identifier for per-user cache isolation.
        """
        from utils.self_knowledge import get_self_knowledge_builder

        builder = get_self_knowledge_builder()
        # Wire the MCP hub if available and not yet set
        if self._mcp_hub is not None:
            builder.set_mcp_hub(self._mcp_hub)
        # Wire the music player for direct state reads (R2-LLM fix)
        if self._music is not None:
            builder.set_music_player(self._music)
        state_text = builder.build(force_refresh=True, user_name=user_name, user_key=user_id)
        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text="CURRENT SYSTEM STATE:\n%s" % state_text,
            origin="self_knowledge",
            source_tag="self-knowledge",
        )
        return [frame] if frame is not None else []

    # Category display order and human-readable headers for memory injection.
    _MEMORY_CATEGORY_HEADERS: dict[str, str] = {
        "fact": "FACTS",
        "preference": "PREFERENCES",
        "routine": "ROUTINES",
        "correction": "CORRECTIONS",
        "context": "ACTIVE CONTEXT",
        "note": "NOTES",
    }
    # Claude-parity (S10-MEM-004): MEMORY.md is the always-loaded index,
    # capped at 25 KiB / 200 lines (~6k tokens). Recall-injected memories are
    # additive context, so we hold them to a much tighter budget — when
    # memories are clearly useful they should be a few lines, not pages. See
    # src/memdir/memdir.ts:34-90 (MAX_ENTRYPOINT_BYTES / MAX_ENTRYPOINT_LINES).
    _MEMORY_WORD_BUDGET = 600
    _MEMORY_STALE_AFTER_SECONDS = 24 * 60 * 60

    def _memory_context_frames(self, text: str) -> list[Frame]:
        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text=text,
            origin="memory_store",
            source_tag="memory",
        )
        return [frame] if frame is not None else []

    @staticmethod
    def _strip_memory_frontmatter(content: str) -> str:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return content
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1 :])
        return content

    @classmethod
    def _memory_freshness_header(cls, header: Any, *, now_ms: float | None = None) -> str:
        mtime_ms = float(getattr(header, "mtime_ms", 0.0) or 0.0)
        path = "topics/%s" % getattr(header, "filename", "")
        if mtime_ms <= 0:
            return "Memory: %s:" % path

        now = float(now_ms if now_ms is not None else time.time() * 1000.0)
        age_seconds = max(0.0, (now - mtime_ms) / 1000.0)
        age_days = int(age_seconds // cls._MEMORY_STALE_AFTER_SECONDS)
        if age_days == 0:
            return "Memory (saved today): %s:" % path
        if age_days == 1:
            return "Memory (saved yesterday): %s:" % path
        return (
            "This memory is %d days old. Memories are point-in-time observations, not live state -- "
            "claims about code behavior or file:line citations may be outdated. Verify against current code "
            "before asserting as fact.\nMemory (saved %d days ago): %s:" % (age_days, age_days, path)
        )

    @staticmethod
    def _selector_failure_exceptions() -> tuple[type[BaseException], ...]:
        """Exception classes a memory side-query failure must degrade (not raise).

        Covers the deterministic transport/timeout/import errors plus the
        OpenAI SDK exception base -- ``RateLimitError`` subclasses
        ``openai.OpenAIError`` (an ``Exception``, NOT ``RuntimeError``), so
        without an explicit handle it would propagate past the narrow catches
        and re-introduce G10 (a failure stub licensing the model to fabricate
        personal facts). A failed side-query yields an empty selection; the
        always-on manifest layer is unaffected.
        """
        try:
            from openai import OpenAIError as _OpenAIError
        except ImportError:
            _OpenAIError = type("_OpenAIErrorMissing", (Exception,), {})
        return (
            OSError,
            RuntimeError,
            ValueError,
            TimeoutError,
            ImportError,
            AttributeError,
            _OpenAIError,
        )

    def _resolve_selected_memory_headers(
        self,
        *,
        user_id: str,
        user_text: str,
        directory: Any,
        budget: int,
        surfaced: set[str],
    ) -> list[Any]:
        """Return this turn's selected memory headers, consuming the early-fire.

        Lane B (#465): when ``prefetch_memory`` armed the side-query at the top
        of the request path, consume its (usually already-complete) result here
        rather than starting a fresh blocking call -- so the ~1.2 s network wait
        overlapped the pre-context glue instead of stalling the turn. The
        consumed selection is byte-identical to the inline call (same selector,
        same ``(user_text, excluded, budget, user_id)`` inputs), so the model
        sees exactly this turn's selection in this turn's prompt.

        Default path (#2605/#531 TTFT cut): TTFT must not wait on the ~1.3 s
        side-query LLM round trip. The turn blocks at most
        ``auto_memory_side_query_max_block_ms`` for the early-fired selection.
        When it lands in time (fast/cached turns) the model gets the LLM
        selection with zero added latency. When it does not, this turn's memory
        is surfaced via the deterministic local relevance ranker
        (``_local_memory_selection`` -> ``_fallback_metadata_selector``, no
        network, single-digit ms) so the model NEVER loses this turn's relevant
        topic bodies to buy latency -- content is preserved every turn.

        Founder-gated content-drop trade (``settings.auto_memory_side_query_nonblocking``
        ON, default OFF): a side-query still in flight past the budget degrades to
        the always-on manifest for this turn (empty selection, no local ranker) --
        the model does not see this turn's topic bodies. This is the one genuine
        model-visible trade and stays a founder decision.

        A successful *empty* selection (the LLM completed and judged nothing
        relevant) is honored as-is on both paths -- the local ranker only
        backstops a timeout/failure, never overrides a completed "nothing
        relevant" answer (matches the inline selector's own no-metadata-fallback
        default).

        When no matching prefetch is armed (suppressed, disabled, a non-agent
        entrypoint that did not prefetch, or a stale/mismatched pair), it falls
        back to the identical inline selector call -- today's behavior.
        """
        exc_types = self._selector_failure_exceptions()

        # Consume-once: clear the ContextVar so a mismatched or already-used
        # prefetch can never be applied to a later provider build.
        prefetch = _memory_selection_prefetch.get()
        if prefetch is not None:
            _memory_selection_prefetch.set(None)

        matches = (
            prefetch is not None
            and prefetch.user_id == user_id
            and prefetch.user_text == user_text
            and not self._suppress_memory_context
        )
        if matches and prefetch is not None:
            from config.settings import settings

            budget_ms = int(getattr(settings, "auto_memory_side_query_max_block_ms", 150) or 0)
            wait_s: float = max(0.0, budget_ms / 1000.0)
            # Founder-gated (default OFF): when ON, a still-in-flight side-query
            # past the budget drops to the always-on manifest only (no this-turn
            # topic bodies). Default is content-preserving -- the deterministic
            # local ranker backstops instead of dropping content.
            drop_to_manifest = bool(getattr(settings, "auto_memory_side_query_nonblocking", False))
            try:
                return list(prefetch.future.result(timeout=wait_s))
            except FutureTimeoutError:
                if drop_to_manifest:
                    logger.debug(
                        "Memory side-query still in flight past the %d ms budget; "
                        "manifest-only this turn (founder-gated non-blocking drop).",
                        budget_ms,
                    )
                    return []
                logger.debug(
                    "Memory side-query still in flight past the %d ms budget; surfacing this "
                    "turn's memory via the local relevance ranker (LLM selection discarded, "
                    "content preserved -- #2605/#531).",
                    budget_ms,
                )
                return self._local_memory_selection(
                    user_id=user_id,
                    user_text=user_text,
                    directory=directory,
                    budget=budget,
                    surfaced=surfaced,
                )
            except exc_types as exc:
                detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
                if drop_to_manifest:
                    logger.debug(
                        "Memory side-query prefetch failed (non-fatal, manifest still emits): %s",
                        detail,
                    )
                    return []
                logger.debug(
                    "Memory side-query prefetch failed (non-fatal); surfacing this turn's memory "
                    "via the local relevance ranker (content preserved): %s",
                    detail,
                )
                return self._local_memory_selection(
                    user_id=user_id,
                    user_text=user_text,
                    directory=directory,
                    budget=budget,
                    surfaced=surfaced,
                )

        # Inline fallback: no early-fire armed for this exact turn (suppressed,
        # disabled, a non-agent entrypoint that did not prefetch, or a
        # stale/mismatched pair).
        #
        # #531: this path used to call the selector and block on it with NO
        # budget, so it inherited the side-query's own 15 s ceiling
        # (services/memory/selector.py:89 ->
        # ``run_async_synchronously(..., timeout=15.0)``). The registry's
        # concurrent-provider backstop does not clip it either -- that is 20 s
        # (intent/runtime_context/providers.py:31), deliberately ABOVE the
        # side-query timeout -- so a slow or wedged side-query stalled the whole
        # turn in front of the model call for up to 15 s. That is exactly the
        # shape measured in _diag/2026-07-09/lat531_local_spans (CTX_PROVIDER
        # provider=memory 15004-15022 ms, 15.0 s of a 15.6 s pre-model window).
        # #560 removed the serving-loop deadlock that made it fire every warm
        # turn, and #2605 bounded the PREFETCH consume -- but this branch kept
        # the unbounded block, and two of the three ``build_frames`` entrypoints
        # (``_build_streaming_context_bundle`` and the checkpoint-resume path in
        # intent/ai_controller.py) never arm a prefetch, so they always land
        # here.
        #
        # Cut: run the selector on the same long-lived pool the early-fire uses
        # and consume it under the identical ``auto_memory_side_query_max_block_ms``
        # budget + local-ranker backstop. TTFT can no longer wait on the
        # side-query LLM round trip from ANY entrypoint, and content is still
        # preserved every turn (the deterministic ranker backstops a timeout
        # rather than dropping this turn's topic bodies). Semantics are now
        # identical on both paths instead of diverging by entrypoint.
        from config.settings import settings

        budget_ms = int(getattr(settings, "auto_memory_side_query_max_block_ms", 150) or 0)
        wait_s = max(0.0, budget_ms / 1000.0)
        drop_to_manifest = bool(getattr(settings, "auto_memory_side_query_nonblocking", False))

        def _inline_backstop() -> list[Any]:
            if drop_to_manifest:
                return []
            return self._local_memory_selection(
                user_id=user_id,
                user_text=user_text,
                directory=directory,
                budget=budget,
                surfaced=surfaced,
            )

        ctx = contextvars.copy_context()

        def _run_inline() -> list[Any]:
            from services.memory.selector import select_relevant_memory_headers

            return list(
                select_relevant_memory_headers(
                    directory,
                    user_text,
                    limit=budget,
                    excluded_filenames=surfaced,
                    user_id=user_id,
                )
            )

        try:
            inline_future: Future[list[Any]] = _MEMORY_PREFETCH_EXECUTOR.submit(ctx.run, _run_inline)
        except RuntimeError as exc:
            # Interpreter shutdown / executor unavailable. Do NOT fall back to a
            # blocking inline call -- that is the unbounded 15 s path this cut
            # exists to remove. The local ranker still preserves this turn's
            # memory content with no network call.
            logger.debug(
                "Memory side-query submit failed (non-fatal); surfacing this turn's memory "
                "via the local relevance ranker: %s",
                exc,
            )
            return _inline_backstop()

        try:
            return list(inline_future.result(timeout=wait_s))
        except FutureTimeoutError:
            logger.debug(
                "Memory side-query (inline, no early-fire armed) still in flight past the %d ms "
                "budget; surfacing this turn's memory via the local relevance ranker "
                "(LLM selection discarded, content preserved -- #531).",
                budget_ms,
            )
            return _inline_backstop()
        except exc_types as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug(
                "Memory side-query selector failed (non-fatal, manifest+directive still emit): %s",
                detail,
            )
            return _inline_backstop()

    def _local_memory_selection(
        self,
        *,
        user_id: str,
        user_text: str,
        directory: Any,
        budget: int,
        surfaced: set[str],
    ) -> list[Any]:
        """Deterministic, no-network memory selection for the off-path turn.

        #2605/#531: when the early-fired side-query LLM round trip has not landed
        within the turn's small block budget, TTFT must not wait on it. Rather
        than degrade to manifest-only (the founder-gated
        ``auto_memory_side_query_nonblocking`` trade, which withholds this turn's
        topic bodies), surface this turn's most relevant memory via the shipped
        deterministic metadata ranker (``select_relevant_memory_headers`` with a
        no-op side-query + ``allow_metadata_fallback=True`` ->
        ``_fallback_metadata_selector``: token overlap over the manifest's
        name/description/type/filename, single-digit ms, no LLM call). Memory
        context is preserved every turn; the model never loses access to relevant
        topic bodies to buy latency.

        This is retrieval ranking over the user's own memory store, not a
        query-intent classifier: it selects which stored memory to surface --
        exactly the side-query's job -- and never classifies intent or alters
        agent routing/behavior. The same ``excluded_filenames``/``limit`` inputs
        as the LLM path keep the selection consistent (no double-surfacing).
        """
        exc_types = self._selector_failure_exceptions()
        try:
            from services.memory.selector import select_relevant_memory_headers

            return list(
                select_relevant_memory_headers(
                    directory,
                    user_text,
                    limit=budget,
                    excluded_filenames=surfaced,
                    selector=lambda _request: [],
                    allow_metadata_fallback=True,
                    user_id=user_id,
                )
            )
        except exc_types as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug(
                "Local memory relevance ranker failed (non-fatal, manifest still emits): %s",
                detail,
            )
            return []

    def _build_memory_context(self, user_id: str, user_text: str) -> list[Frame]:
        """Inject relevance-ranked memories into a categorised context block.

        Parity with Claude Code's two-layer memory injection:
        - Always-on layer: VIOLA.md + memory manifest (the index of available
          topic files with their descriptions) + form-filling directive. None
          of these depend on the side-query LLM; a side-query rate-limit or
          timeout MUST NOT erase them. Matches `utils/claudemd.ts`'s eager
          MEMORY.md injection.
        - Side-query layer: topic file CONTENT surfacing via
          ``select_relevant_memory_headers``. Failure here yields an empty
          surfaced set — the always-on layer is unaffected. Matches
          `memdir/findRelevantMemories.ts` returning [] on failure.

        We NEVER emit a runtime-context-failure stub into the model's
        context. A failure marker tells the model "memory is unavailable",
        which licenses fabricating personal facts from thin air (G10
        incident 2026-05-30, task ``448a27f65cd2``: model invented
        "favorite language is Rust" after a side-query RateLimitError
        emitted a stub instead of degrading to manifest-only).
        """
        if self._suppress_memory_context:
            return []
        try:
            from services.memory.dir import get_memory_dir, is_auto_memory_enabled

            if not is_auto_memory_enabled():
                return []

            directory = get_memory_dir(user_id)
        except (OSError, RuntimeError, ValueError, ImportError, AttributeError) as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Memory directory unavailable (non-fatal, no frame emitted): %s", detail)
            return []

        viola_file = ""
        try:
            viola_file = directory.read_viola_file().strip()
        except (OSError, RuntimeError, ValueError) as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("VIOLA.md context retrieval failed (non-fatal): %s", detail)

        # Always-on manifest: list of available topic headers (name +
        # description). Deterministic, local-only — no LLM call. Even when
        # the side-query selector rate-limits, the model still sees what
        # memory exists and can decide to recall a specific topic via the
        # memory tool.
        all_headers: list[Any] = []
        try:
            all_headers = directory.scan_memory_files()
        except (OSError, RuntimeError, ValueError) as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Memory manifest scan failed (non-fatal): %s", detail)

        # Uniform memory budget across all turns. The earlier
        # ``budget = 4 if is_forms_or_legal_task(...) else 5`` branched on
        # a user-text regex wordlist and silently shrank the memory
        # selector window for any phrasing the author had thought of.
        # R5-P0-D (2026-05-30): trust the model with the same memory
        # window every turn; the selector itself decides relevance.
        budget = self._MEMORY_SELECTION_BUDGET
        surfaced = self._surfaced_memory_filenames.setdefault(user_id, set())

        selected_headers = self._resolve_selected_memory_headers(
            user_id=user_id,
            user_text=user_text,
            directory=directory,
            budget=budget,
            surfaced=surfaced,
        )
        surfaced.update(header.filename for header in selected_headers)

        # Read topic bodies for the selector's picks. Bodies are gated on
        # the side-query; per-file read failures are non-fatal.
        by_category: dict[str, list[str]] = {}
        seen_memory_lines: set[str] = set()
        try:
            from services.memory.hygiene import canonical_memory_text
            from services.memory.store import SENSITIVE_CONTENT_RE, redact_pii_output
        except (
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:  # pragma: no cover — import path is stable in prod
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Memory hygiene/store import failed (non-fatal): %s", detail)
            SENSITIVE_CONTENT_RE = None  # type: ignore[assignment]
            redact_pii_output = lambda s: s  # type: ignore[assignment]
            canonical_memory_text = lambda s: s  # type: ignore[assignment]

        for header in selected_headers:
            path = "topics/%s" % header.filename
            try:
                body = self._strip_memory_frontmatter(directory.read(path))
            except (OSError, RuntimeError, ValueError) as exc:
                detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
                logger.debug("Memory topic read failed for %s (non-fatal): %s", path, detail)
                continue
            category = (header.type or "note").strip().lower() or "note"
            file_lines: list[str] = []
            for raw_line in body.splitlines():
                content = raw_line.strip()
                if not content:
                    continue
                content = content.removeprefix("-").strip()
                if not content or (SENSITIVE_CONTENT_RE is not None and SENSITIVE_CONTENT_RE.search(content)):
                    continue
                canonical = canonical_memory_text(content)
                if not canonical or canonical in seen_memory_lines:
                    continue
                seen_memory_lines.add(canonical)
                file_lines.append("- %s" % redact_pii_output(content))
            if file_lines:
                category_lines = by_category.setdefault(category, [])
                category_lines.append(self._memory_freshness_header(header))
                category_lines.extend(file_lines)

        # R3-P1-J (2026-05-30): the per-turn form_directive that used to
        # be appended here is DELETED. The canonical form-filling
        # directive lives in services/llm/prompts/viola_unified.py:76-79
        # and rides system_static_blocks (cached). The previous duplicate
        # was a per-turn echo of the same directive into every memory
        # context frame, motivated by an old PAYMENT_GATE regression
        # (2026-05-20) where the model stalled at "cart is ready for
        # payment approval" without the runtime nudge. The right fix is
        # the canonical text in the unified prompt, not a per-turn echo
        # in the memory context — the latter cache-busts and box-the-lens
        # a guidance string twice.

        lines: list[str] = []
        if viola_file:
            lines.extend(["VIOLA.md (user-edited instructions):", viola_file, ""])

        # Always emit the manifest when headers exist so the model sees the
        # memory index even on side-query failure. Surfaced filenames are
        # included so the model knows what topic-content was loaded inline
        # vs what's available via the memory tool.
        if all_headers:
            try:
                manifest_text = directory.format_memory_manifest(all_headers)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
                logger.debug("Memory manifest format failed (non-fatal): %s", detail)
                manifest_text = ""
            if manifest_text:
                lines.append("AVAILABLE MEMORY FILES:")
                lines.append(manifest_text)
                lines.append("")

        word_count = 0
        if by_category:
            lines.append("USER MEMORIES (things you've learned about the user):")
            for cat, header in self._MEMORY_CATEGORY_HEADERS.items():
                mems = by_category.pop(cat, None)
                if not mems:
                    continue
                lines.append("[%s]" % header)
                for index, mem in enumerate(mems):
                    line = mem
                    line_words = len(line.split())
                    if word_count + line_words > self._MEMORY_WORD_BUDGET:
                        lines.append("- ... (%d more truncated)" % (len(mems) - index))
                        break
                    lines.append(line)
                    word_count += line_words

            # Any categories not in the header map (future-proofing)
            for cat, mems in by_category.items():
                lines.append("[%s]" % cat.upper())
                for index, mem in enumerate(mems):
                    line = mem
                    line_words = len(line.split())
                    if word_count + line_words > self._MEMORY_WORD_BUDGET:
                        lines.append("- ... (%d more truncated)" % (len(mems) - index))
                        break
                    lines.append(line)
                    word_count += line_words

        # R3-P1-J: no longer appending form_directive (deleted per the
        # iter-3 audit; canonical text lives in the unified prompt).
        # If ``lines`` is empty after VIOLA.md + manifest + topic content
        # all returned nothing, emit no frame.
        if not lines:
            return []
        return self._memory_context_frames("\n".join(lines))

    def _profile_context_frames(self, text: str) -> list[Frame]:
        frame = self._make_context_frame(
            kind=FrameKind.SYSTEM_REMINDER,
            role=FrameRole.META_USER,
            text=text,
            origin="profile_store",
            source_tag="profile",
        )
        return [frame] if frame is not None else []

    def _build_profile_context(
        self,
        user_id: str,
        user_text: str = "",
        *,
        canonical_name: str = "",
        canonical_address: dict[str, Any] | None = None,
    ) -> list[Frame]:
        """Retrieve structured user profile for LLM context injection.

        Uses the cached profile (`_PROFILE_CACHE`); `save_profile` updates
        the cache on writes, so the chat hot path stays sub-ms instead of
        triggering 3-4 sync->async bridge calls per turn.
        """
        try:
            from services.user_profile import get_user_profile

            reload_sig = inspect.signature(get_user_profile)
            if "user_id" not in reload_sig.parameters:
                logger.debug(
                    "User profile service is not user-scoped yet; skipping profile context for %s",
                    user_id,
                )
                return []

            profile = get_user_profile(user_id=user_id)
            profile_context = getattr(profile, "get_profile_context", None)
            if profile_context is None:
                return []
            profile_sig = inspect.signature(profile_context)
            if "user_id" in profile_sig.parameters:
                frames = self._profile_context_frames(profile_context(user_id=user_id))
            elif "task_text" in profile_sig.parameters:
                frames = self._profile_context_frames(
                    profile_context(
                        task_text=user_text,
                        canonical_name=canonical_name,
                        canonical_address=canonical_address,
                    )
                )
            else:
                frames = self._profile_context_frames(profile_context())
            if frames:
                from services.profile.personalization_audit import (
                    require_personalization_event,
                )

                require_personalization_event(
                    user_id,
                    "profile_context_injected",
                    details={"frame_count": len(frames), "source": "user_profile"},
                )
            return frames
        except Exception as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Profile context retrieval failed (non-fatal): %s", detail)
            return []

    def _build_active_agents_context(self, user_id: str, current_agent_id: str | None = None) -> list[Frame]:
        """Return a per-turn block listing the user's active background agents.

        Lets the orchestrator (main agent) reason about what's running so it
        can respond to user references like "cancel that one" via the
        cancel_agent / check_agents tools.

        The currently executing background agent is excluded so a child does
        not see itself as separate work to cancel or poll.

        Returns empty string when there are no active agents.
        """
        if not user_id:
            return []
        try:
            from datetime import UTC, datetime

            from services.agent_runtime.registry import agent_registry

            current_agent_id = str(current_agent_id or "").strip()
            active = [
                task
                for task in agent_registry.list_active_sync(user_id)
                if not current_agent_id or task.agent_id != current_agent_id
            ]
            if not active:
                return []

            now = datetime.now(UTC)
            lines = ["ACTIVE BACKGROUND AGENTS:"]
            for task in active[:10]:
                started = task.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                elapsed = max(0, int((now - started).total_seconds()))
                lines.append("- agent_id=%s (running %ds): %s" % (task.agent_id, elapsed, task.task[:120]))
            lines.append("Related tools available for active-agent state: cancel_agent, check_agents.")
            frame = self._make_context_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                text="\n".join(lines),
                origin="subagent",
                source_tag="active-agent",
            )
            return [frame] if frame is not None else []
        except Exception as exc:
            logger.debug("active_agents context build failed (non-fatal): %s", exc)
            return []

    def _build_delivery_address_context(self, user_id: str) -> list[Frame]:
        """Return delivery address context block if an address is saved.

        Reads from the per-user preferences DB.  Includes decomposed
        components (street, city, state, zip) so the agent can fill form
        fields individually without guessing.
        """
        try:
            from services.user_model.preferences_store import get_user_preferences_store

            store = get_user_preferences_store()
            addr = store.get(user_id, "delivery_address")
            if not isinstance(addr, dict):
                return []

            street = addr.get("street", "").strip()
            if not street:
                return []

            parts = ["Street: %s" % street]
            city = addr.get("city", "").strip()
            if city:
                parts.append("City: %s" % city)
            state = addr.get("state", "").strip()
            if state:
                parts.append("State: %s" % state)
            zip_code = addr.get("zip", "").strip()
            if zip_code:
                parts.append("ZIP: %s" % zip_code)

            text = (
                "SAVED DELIVERY ADDRESS:\n  %s\n"
                "Components are split by field for forms that ask for a delivery address."
            ) % "\n  ".join(parts)
            frame = self._make_context_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                text=text,
                origin="settings",
                source_tag="delivery-address",
            )
            return [frame] if frame is not None else []
        except Exception as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("Delivery address context failed (non-fatal): %s", detail)
            return []

    def _build_user_model_context(
        self,
        user_id: str,
        user_text: str = "",
        *,
        canonical_name: str = "",
        canonical_address: dict[str, Any] | None = None,
    ) -> list[Frame]:
        """Retrieve learned user model summary for LLM context injection.

        Returns a compact summary of learned preferences, facts, and
        interaction patterns.  Returns empty string if the user model
        feature is disabled or no data exists.
        """
        try:
            from config.settings import settings as _cfg

            if not getattr(_cfg, "user_model_enabled", False):
                return []

            from services.user_model.profile import get_user_model

            model = get_user_model(user_id=user_id)
            text = model.get_profile_summary(
                task_text=user_text,
                canonical_name=canonical_name,
                canonical_address=canonical_address,
            )
            frame = self._make_context_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                role=FrameRole.META_USER,
                text=text,
                origin="profile_store",
                source_tag="user-model",
            )
            frames = [frame] if frame is not None else []
            if frames:
                from services.profile.personalization_audit import (
                    require_personalization_event,
                )

                require_personalization_event(
                    user_id,
                    "user_model_context_injected",
                    details={"frame_count": len(frames), "source": "user_model"},
                )
            return frames
        except Exception as exc:
            detail = "closed event loop" if is_event_loop_closed_error(exc) else str(exc)
            logger.debug("User model context retrieval failed (non-fatal): %s", detail)
            return []

    def _build_capability_context(self, text: str) -> str:
        """Build dynamic capability context from the registry for the LLM.

        Production no longer uses a domain classifier here; capability context
        is requested for the general all-tools path.
        Returns empty string if registry is not available.
        """
        try:
            from services.capability_registry import CapabilityRegistry

            registry = CapabilityRegistry.get_instance()
            if registry is None:
                return ""

            return registry.get_llm_capability_context(None)
        except Exception:
            logger.exception("Capability context build failed")
            return ""
