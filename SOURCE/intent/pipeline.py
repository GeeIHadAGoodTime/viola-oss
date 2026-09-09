"""
Unified Intent Pipeline - Single source of truth for command processing.
Consolidates instant commands, rule-based parsing, AI routing, and execution.
"""

from __future__ import annotations

import asyncio
import contextvars
import random
import re
import time
from collections.abc import Mapping
from typing import cast

from core.logging_config import get_logger
from core.task_tracker import TaskTracker
from intent.commands.registry import CommandInvocation, CommandRegistry, CommandSpec
from intent.hooks.lifecycle import create_hook_registry
from intent.hooks.settings_runner import HookSettingsRunner
from services.conversation import (
    ConversationStateManager,
    get_conversation_manager,
    get_request_conversation_manager,
    use_request_manager,
)
from services.conversation.context_frames import FrameKind, system_reminder_frame

logger = get_logger(__name__)

from intent.instant_commands import InstantCommandHandler

from .command_executor import CommandExecutor, TTSSpeaker
from .input_prefilter import (
    BLOCKED_INPUT_RESPONSE,
    CRISIS_RESPONSE,
    GIBBERISH_RESPONSE,
    OVERLONG_INPUT_RESPONSE,
    SENSITIVE_STORAGE_RESPONSE,
    detect_crisis,
    detect_obvious_attack_input,
    detect_sensitive_request,
    is_gibberish,
)

# Import type/result contracts from dedicated module.
from .pipeline_contracts import (
    GPTPort,
    IntentTTSPort,
    MusicPort,
    PipelineResult,
    StatePort,
)
from .pipeline_processors import IntentPipelineProcessors

# Import extracted modules
from .pipeline_room_tracking import IntentPipelineRoomTracker

# ============================================================================
# HELPERS
# ============================================================================

# DEBOX (2026-05-29 boxing audit, W2 intent lane): removed the plan-refinement
# and capability-setup intent ladders that used to live here:
#   _PLAN_REFINEMENT_RE / _PLAY_PREFIX_RE / _PLAN_TARGET_PREFIX_RE - regex that
#     classified a follow-up as a "plan refinement", then regex-rewrote the
#     user's natural language into a "play X" command and bypassed the model.
#   _SETUP_CONFIRM_RE / _SETUP_DENY_RE - regex that classified an affirmative
#     vs negative reply to a capability-setup offer (deny bypassed the model
#     with a canned line; confirm injected a directive system fragment).
# Both pre-empted the model on decisions it owns. The active plan is surfaced to
# the model as a neutral TASK PLAN context frame (intent/context_builder.py
# _build_task_plan_context); capability-setup offers are model-driven via the
# offers_setup tool data. The setup ladder was additionally dead code -
# _pending_setup was never populated in any production path.

# Raw-LLM baseline token estimates per route type.
# Used to compute "tokens saved" — what the same task would cost via raw LLM.
# Values from docs/EFFICIENCY_AUDIT.md Section 5.
_RAW_LLM_BASELINE: dict[str, int] = {
    "instant": 2_200,  # avg of play/timer/calendar baselines
    "rule": 2_200,  # same class of deterministic commands
    "rules": 2_200,  # alias
    "plugin": 2_200,  # plugin-handled ≈ rule-handled
    "knowledge": 2_000,  # API lookup vs LLM interpretation
    "state": 1_200,  # simple state query
    "llm_ask": 2_000,  # raw LLM Q&A equivalent (Section 5d)
    "llm_ask_simple": 2_000,  # single-call LLM ask (same as llm_ask)
    "llm_ask_agent": 7_100,  # multi-step agent chain
    "llm_route": 4_664,  # raw LLM command routing equivalent
    "agent": 7_100,  # raw LLM agent with browser tools (Section 5d)
    "cache": 2_000,  # cached response saves a full LLM call
    "ai": 0,  # legacy fallback — refined into llm_ask/llm_route/agent
    "fallback": 0,  # no-match: no baseline
    "exception": 0,  # error: no baseline
    "validation": 0,  # empty input: no baseline
}

_INTENT_TO_CATEGORY: dict[str, str] = {
    "play": "music",
    "pause": "music",
    "stop": "music",
    "skip": "music",
    "next": "music",
    "previous": "music",
    "volume": "music",
    "queue": "music",
    "shuffle": "music",
    "repeat": "music",
    "resume": "music",
    "weather": "weather",
    "forecast": "weather",
    "timer": "timer",
    "alarm": "timer",
    "reminder": "timer",
    "answer": "conversation",
    "resume_task": "system",
    "connect_service": "system",
}

# Hard ceiling on user input length at the top of the command path. Every
# pre-LLM prefilter (crisis, attack, sensitive, gibberish) runs regexes on the
# raw input; a long unbounded payload turned one of those regexes into a
# full-API event-loop wedge on 2026-07-05. Capping length at the choke point
# neutralizes the whole "regex on unbounded user input" class structurally, not
# one pattern at a time. 10000 chars is ~20x the codebase's own
# MAX_COMMAND_TEXT_LENGTH (core/constants.py) and far beyond any real voice or
# typed command (~1500-2000 words), so it never rejects a legitimate command,
# while keeping even a hypothetical super-linear prefilter regex sub-second.
_MAX_PIPELINE_INPUT_CHARS = 10000


def _input_exceeds_length_cap(text: str) -> bool:
    """True when input is longer than the pipeline's hard length ceiling.

    Pure predicate so the structural DoS guard is unit-testable without
    constructing the full pipeline.
    """
    return len(text or "") > _MAX_PIPELINE_INPUT_CHARS


# ============================================================================
# UNIFIED INTENT PIPELINE
# ============================================================================


class IntentPipeline:
    """
    Unified intent processing pipeline.

    Single source of truth for command interpretation and execution.
    This is the CANONICAL path for all voice/text command processing.

    Architecture Overview:
    =====================

    Processing Flow — 5 major stages with sub-phases (in priority order):

        User Input
            │
            ▼
        Phase 0:   Input security filter (blocklist, injection defense)
        Phase 1:   Instant Commands (regex, ~30 types, ~1ms, no AI cost)
                   "stop", "pause", "next", "volume up", timers
            │ (if no match)
            ▼
        Phase 2:   AI Routing — full LLM call (all tools, no filtering)
                   Questions, commands, recommendations, agentic tasks
            │ (if no match)
            ▼
        Phase 3:   Fallback (user-friendly error, suggests rephrasing)

    Error Handling:
    - Each phase catches exceptions and continues to next
    - Errors are logged with consistent identifiers (PIPELINE_*)
    - User-facing messages are randomized for natural feel

    Cancellation Protocol:
    - STOP-class intents (stop, pause, shut up) trigger cancellation
    - In-flight STT/LLM work is cancelled via TaskTracker

    Policy Flags:
    - Results include policy_flags for routing decisions
    - Flags: ["instant", "rule_based", "ai_routed", "no_match", "exception"]

    Usage:
        from intent.pipeline import create_pipeline

        pipeline = create_pipeline(music, tts, state, gpt_handler)
        result = await pipeline.process("play bohemian rhapsody")

        if result.ok:
            logger.info("Executed: %s", result.intent)
        else:
            logger.info("Error: %s", result.error)

    See Also:
        - intent.instant_commands: Instant command handlers
        - services.llm.provider_router: AI routing
        - docs/WAKE_TUNING.md: Wake word tuning guide
    """

    def __init__(
        self,
        music: MusicPort,
        tts: IntentTTSPort | None = None,
        state: StatePort | None = None,
        gpt_handler: GPTPort | None = None,
        node_id: str | None = None,
        is_hub: bool = False,
        voice_pipeline: object | None = None,
        plugin_manager: object | None = None,
        conversation_state_manager: ConversationStateManager | None = None,
        user_id: str | None = None,
    ):
        """
        Initialize unified pipeline.

        Args:
            music: Music player instance
            tts: TTS engine (optional)
            state: Application state (optional)
            gpt_handler: GPT handler for AI routing (optional)
            node_id: Node identifier for active room tracking (optional)
            is_hub: Whether this node is the Hub (for supremacy)
            voice_pipeline: Voice pipeline instance for cancellation support (optional)
        """
        self.music = music
        self.tts = tts
        self.state = state
        self.gpt_handler = gpt_handler
        self.node_id = node_id
        self.is_hub = is_hub
        self.voice_pipeline = voice_pipeline
        resolved_user_id = self._resolve_constructor_user_id(
            user_id=user_id,
            conversation_state_manager=conversation_state_manager,
            state=state,
        )
        self.user_id = resolved_user_id
        # Default state manager used when no per-user manager has been
        # published via ``use_request_manager`` (single-user desktop,
        # non-request setup code). Runtime requests resolve the correct
        # per-user manager via the ``conversation_state_manager`` property
        # below — the shared-pipeline model used by the messaging
        # MessageRouter previously treated this construction-time singleton
        # as the per-request source, which caused cross-tenant context
        # bleed between linked Discord/Matrix users (CHAN-R1).
        self._default_state_manager = conversation_state_manager or ConversationStateManager(
            session_id=resolved_user_id,
            user_id=resolved_user_id,
        )
        self.command_registry = CommandRegistry()
        self.hook_registry = create_hook_registry(default_user_id=resolved_user_id)
        self.hook_settings_runner = HookSettingsRunner()
        # F-004: merge settings/plugin/skill hooks into the production runner.
        # Loading is best-effort — missing files / parse errors degrade to an
        # empty config rather than refusing to boot the pipeline.
        try:
            self._load_production_hook_sources()
        except (RuntimeError, OSError, ValueError) as hook_load_err:
            logger.warning("Production hook source load failed: %s", hook_load_err)
        self._register_builtin_commands()

        # Pending state is keyed by user. A shared pipeline can serve multiple
        # cloud or messaging users, so a process-global yes/no or follow-up flag
        # would let one tenant answer another tenant's prompt.
        self._pending_followup_by_user: dict[str, str | None] = {}

        # C3: QueryGuard — deduplicate rapid-fire identical commands
        self._recent_queries: dict[str, float] = {}
        self._query_guard_window: float = 2.0

        # C2: Session lane queuing — one lock per session key
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_guard = asyncio.Lock()  # protects the dict itself
        # No durable per-user command queue exists here. Fail fast instead of
        # letting a second command hang or eventually run unlocked.
        self._session_lock_timeout: float = 1.0

        # Track in-flight work for cancellation
        self._active_stt_task: asyncio.Task | None = None
        self._active_llm_task: asyncio.Task | None = None
        # mt-ok: bg tasks are keyed by the explicit owning user and step id.
        # Keyed by ``(user_key, step_id)`` so two tenants can have
        # pending plan steps with identical step_id values without
        # interfering with each other's cancel/refresh logic.
        self._scheduled_step_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._tts_tasks = TaskTracker()

        # Initialize helper classes
        self._room_tracker = IntentPipelineRoomTracker(self)
        self._processors = IntentPipelineProcessors(self)
        self._command_executor = CommandExecutor(self.music, self.tts)
        self._tts_speaker = TTSSpeaker(self.tts, self._tts_tasks)

        # Initialize sub-components
        self.instant_handler = InstantCommandHandler(music, state)
        # Inject TTS reference so scheduler-dispatched handlers (e.g. play_alarm_sound)
        # can speak notifications directly without a voice command handler in the loop.
        self.instant_handler.tts = self.tts

        # Initialize AI controller for AI-enhanced processing
        from intent.ai_controller import AIController

        self.ai_controller = AIController(None)
        self.ai_controller.hook_registry = self.hook_registry
        self.ai_controller.hook_settings_runner = self.hook_settings_runner
        self.ai_controller.music = self.music
        if self.music is not None:
            self.ai_controller._context_builder.set_music_player(self.music)
        # C5: Give AI controller access to instant command handler for non-music commands
        self.ai_controller._instant_handler = self.instant_handler
        # Agent loop: give AI controller access to voice pipeline and TTS speaker
        self.ai_controller._voice_pipeline = self.voice_pipeline
        self.ai_controller._tts_speaker = self._tts_speaker

        # Set up GPT handler - auto-create LLM router if none provided
        effective_handler = gpt_handler
        if effective_handler is None:
            # Auto-create LLM router based on PRD settings
            effective_handler = create_llm_router()
            if effective_handler is not None:
                logger.debug("Auto-created LLM router for pipeline")
                self.gpt_handler = effective_handler

        if effective_handler:
            self.ai_controller.client = effective_handler
            set_manager = getattr(effective_handler, "set_conversation_manager", None)
            if callable(set_manager):
                set_manager(self._default_state_manager)

        self.ai_controller.set_conversation_state_manager(self._default_state_manager)

        # Active room tracking (lazy import, Phase 2+ multi-room feature)
        self._active_room_tracker: object | None = None
        if node_id:
            try:
                from experimental.phase2_multiroom.nodes import get_active_room_tracker

                self._active_room_tracker = get_active_room_tracker()
            except Exception as exc:
                logger.debug("Active room tracker not available: %s", exc)

        logger.info(
            "IntentPipeline initialized (security → instant → AI → execute, node_id=%s, is_hub=%s)",
            node_id,
            is_hub,
        )

    async def shutdown(self) -> None:
        """Shut down pipeline resources (MCP hub, etc.)."""
        scheduled_tasks = list(self._scheduled_step_tasks.values())
        self._scheduled_step_tasks.clear()
        for task in scheduled_tasks:
            task.cancel()
        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks, return_exceptions=True)
        if hasattr(self, "ai_controller"):
            await self.ai_controller.shutdown()

    def _load_production_hook_sources(self) -> None:
        """F-004: merge settings/plugin/skill hooks into the production runner.

        Parity reference: ``utils/hooks.ts:1492-1565`` —
        ``initializeHooksFromConfig`` merges user/project/policy settings,
        plugin hooks, and skill-bundled hooks at session bootstrap. The
        Viola hook runner previously started empty and never picked up
        anything from these sources, so settings hooks defined in
        ``~/.viola/settings.json`` / ``.viola/settings.json`` were dead
        code on the production runtime path.
        """

        from intent.hooks.settings_runner import (
            load_production_hook_config,
            static_file_changed_watch_paths,
        )

        user_settings: Any = None
        project_settings: Any = None
        policy_settings: Any = None
        try:
            from ui.settings_manager import get_settings_manager

            settings_manager = get_settings_manager()
            getter = getattr(settings_manager, "get", None)
            if callable(getter):
                user_settings = {"hooks": getter("hooks", None)}
                project_settings = {"hooks": getter("project_hooks", None)}
                policy_settings = {"hooks": getter("policy_hooks", None)}
        except (ImportError, RuntimeError) as exc:
            logger.debug("Settings manager unavailable for hook load: %s", exc)

        config = load_production_hook_config(
            user_settings=user_settings,
            project_settings=project_settings,
            policy_settings=policy_settings,
        )
        if not config.is_empty():
            self.hook_settings_runner.merge(config)
            static_watch_paths = static_file_changed_watch_paths(config, cwd=os.getcwd())
            if static_watch_paths:
                self.hook_registry.seed_watch_paths(None, static_watch_paths)
            logger.info(
                "Loaded production hook sources (events=%s)",
                sorted(event.value for event in config.matchers_by_event),
            )

    def _register_builtin_commands(self) -> None:
        self.command_registry.register(
            CommandSpec(
                name="help",
                aliases=("/help", "slash help"),
                source="builtin",
                handler=self._handle_help_command,
                remote_safe=True,
                description="Show available session commands.",
            )
        )
        self.command_registry.register(
            CommandSpec(
                name="status",
                aliases=("/status", "slash status"),
                source="builtin",
                handler=self._handle_status_command,
                remote_safe=True,
                description="Show current session status.",
            )
        )
        # S9-01: Claude-compatible slash command surface. These wrap the
        # canonical session-control commands (/clear, /compact, /agents,
        # /model, /permissions) so the registry exposes the same shape
        # callers expect from Claude Code's commands surface.
        try:
            from intent.commands.builtin import (
                agents_command_spec,
                clear_command_spec,
                compact_command_spec,
                model_command_spec,
                permissions_command_spec,
            )
        except ImportError as exc:
            logger.debug("Builtin slash command registration skipped: %s", exc)
            return
        for builder in (
            clear_command_spec,
            compact_command_spec,
            agents_command_spec,
            model_command_spec,
            permissions_command_spec,
        ):
            try:
                self.command_registry.register(builder(pipeline=self))
            except Exception as exc:
                logger.warning("Failed to register %s slash command: %s", builder.__module__, exc)
        try:
            from skills.manager import get_skill_manager

            self.command_registry.add_loader(get_skill_manager().command_specs)
        except (ImportError, RuntimeError, OSError, ValueError) as exc:
            logger.warning("Failed to register SKILL.md slash commands: %s", exc)

    def _handle_help_command(self, _invocation: CommandInvocation) -> dict[str, object]:
        available_specs = self.command_registry.list_available()
        available = ", ".join("/%s" % spec.name for spec in available_specs)
        message = "Available commands: %s." % available if available else "No commands are currently available."
        return {
            "message": message,
            "data": {"available_commands": [spec.name for spec in available_specs]},
        }

    def _handle_status_command(self, _invocation: CommandInvocation) -> dict[str, object]:
        active_plan = self.conversation_state_manager.get_active_plan()
        if isinstance(active_plan, Mapping):
            steps = active_plan.get("steps", [])
            pending_count = sum(
                1 for step in steps if isinstance(step, Mapping) and step.get("status") in {"pending", "in_progress"}
            )
            status = str(active_plan.get("status") or "active")
            message = "Session status: %s, %d pending step(s)." % (
                status,
                pending_count,
            )
        else:
            message = "Session status: no active plan."
        return {"message": message}

    def _record_command_invocation_frame(self, invocation: CommandInvocation) -> None:
        frame = self.command_registry.build_invocation_frame(invocation)
        manager = self.conversation_state_manager
        append_frame = getattr(manager, "append_frame", None)
        if callable(append_frame):
            try:
                append_frame(frame, parent_uuid=invocation.source_frame_uuid)
                return
            except Exception as exc:
                logger.debug("append_frame failed for command invocation: %s", exc)
        add_message = getattr(manager, "add_message", None)
        if callable(add_message):
            try:
                add_message(frame)
            except Exception as exc:
                logger.debug("add_message(Frame) failed for command invocation: %s", exc)

    async def try_command_registry(self, text: str, *, user_key: str = "") -> PipelineResult | None:
        """Resolve and execute a registry command.

        Local commands (``command_type="local"`` / ``"local-jsx"``) execute
        their handler and return a result with ``bypass_ai=True`` — the
        pipeline returns this directly to the caller without entering the
        agent loop.

        Prompt commands (``command_type="prompt"``) — e.g. SkillMd / Claude-
        style markdown slash commands — invoke the handler to obtain a
        model-visible prompt body and return a result with ``bypass_ai=False``
        AND ``data["prompt_rewrite"]`` set. The pipeline caller MUST replace
        the active user-turn text with that prompt body and continue into the
        AI/agent loop. This is F-007 parity with Claude's
        ``processSlashCommand`` path (``processSlashCommand.tsx:723-920``).
        """

        normalized = self._normalize_input(text)
        if not normalized:
            return None
        invocation = self.command_registry.match(
            normalized,
            session_id=user_key or getattr(self._default_state_manager, "session_id", None),
        )
        if invocation is None:
            if normalized.startswith("/"):
                # F-039: distinguish path-like slashes ("/usr/bin/x") from
                # missing-command slashes ("/foo"). The helper falls back to
                # ``unknown_command`` only for the latter.
                path_like_outcome = self._maybe_path_like_slash_passthrough(normalized)
                if path_like_outcome is not None:
                    return path_like_outcome
                command_name = normalized.split(maxsplit=1)[0]
                message = "Unknown command: %s" % command_name
                return PipelineResult(
                    ok=False,
                    intent="unknown_command",
                    data={
                        "message": message,
                        "command_invocation": {
                            "name": command_name.lstrip("/"),
                            "args": {},
                            "source": "command_registry",
                        },
                        "already_executed": True,
                    },
                    error=message,
                    source="command_registry",
                    requires_clarification=False,
                    bypass_ai=True,
                    policy_flags=["command_registry", "unknown_command"],
                    source_node_id=self.node_id,
                )
            return None

        self._record_command_invocation_frame(invocation)
        # Resolve the spec to know whether this is a prompt-style command.
        spec = self.command_registry.get_spec(invocation.name)
        command_type = spec.command_type if spec is not None else "local"
        handler_result = await self.command_registry.invoke(invocation)
        data: dict[str, object] = {
            "command_invocation": {
                "name": invocation.name,
                "args": invocation.args,
                "source": invocation.source,
                "output_style": invocation.output_style,
                "command_type": command_type,
            },
            "already_executed": True,
        }
        ok = True
        error: str | None = None
        if isinstance(handler_result, PipelineResult):
            handler_result.source = "command_registry"
            handler_result.bypass_ai = True
            handler_result.policy_flags = list(handler_result.policy_flags) + ["command_registry"]
            return handler_result
        if isinstance(handler_result, Mapping):
            ok = bool(handler_result.get("ok", True))
            error_value = handler_result.get("error")
            error = str(error_value) if error_value else None
            nested_data = handler_result.get("data")
            if isinstance(nested_data, Mapping):
                data.update(dict(nested_data))
            for key in ("message", "answer", "response"):
                value = handler_result.get(key)
                if isinstance(value, str) and value.strip():
                    data["message"] = value
                    break
        elif isinstance(handler_result, str):
            data["message"] = handler_result
        if "message" not in data:
            data["message"] = "Command /%s completed." % invocation.name

        # F-007: prompt commands rewrite the user turn into a model prompt and
        # continue into the agent loop instead of returning visible text.
        if command_type == "prompt" and ok and error is None:
            prompt_body_value = data.get("message")
            prompt_body = (
                str(prompt_body_value).strip()
                if isinstance(prompt_body_value, str) and prompt_body_value.strip()
                else None
            )
            if prompt_body:
                data["prompt_rewrite"] = prompt_body
                allowed_tools = data.get("allowed_tools")
                if allowed_tools:
                    data["prompt_allowed_tools"] = allowed_tools
                model_override = data.get("model_override")
                if model_override:
                    data["prompt_model_override"] = model_override
                return PipelineResult(
                    ok=True,
                    intent=invocation.name,
                    data=data,
                    error=None,
                    source="command_registry",
                    requires_clarification=False,
                    bypass_ai=False,
                    policy_flags=["command_registry", "prompt_command"],
                    source_node_id=self.node_id,
                )

        return PipelineResult(
            ok=ok,
            intent=invocation.name,
            data=data,
            error=error,
            source="command_registry",
            requires_clarification=False,
            bypass_ai=True,
            policy_flags=["command_registry"],
            source_node_id=self.node_id,
        )

    def _maybe_path_like_slash_passthrough(self, text: str) -> PipelineResult | None:
        """Return None for normal slash misses; bypass for path-like input.

        Claude's ``processSlashCommand.tsx:304-380`` distinguishes a
        slash-shaped command miss ("/foo") from a leading-slash path
        ("/usr/local/bin", "/Users/jay/...", "/c/code/..."). When the input
        looks like a filesystem path or URL-style fragment, Claude passes the
        original text to the model instead of returning unknown-command.

        For F-039 we mirror the heuristic and return ``None`` so the caller
        falls through to the normal LLM routing path with the path-like text
        intact. Returning ``None`` here is the signal that this is not a
        command — callers already treat ``None`` as "not a command".
        """

        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        first_token = stripped.split(maxsplit=1)[0]
        body = first_token[1:]
        if not body:
            return None
        # Multi-segment paths: "/usr/bin", "/home/foo/bar".
        if "/" in body or "\\" in body:
            return self._make_path_passthrough_result(stripped)
        # Drive-letter style: "/c/projects".
        if len(body) == 1 and body[0].isalpha():
            return self._make_path_passthrough_result(stripped)
        # Looks like a file with extension: "/foo.py".
        if "." in body and not body.startswith("."):
            base, _, ext = body.partition(".")
            if base.isidentifier() and ext.isidentifier():
                return self._make_path_passthrough_result(stripped)
        return None

    def _make_path_passthrough_result(self, text: str) -> PipelineResult:
        """Build a path-like passthrough result with prompt_rewrite set."""

        return PipelineResult(
            ok=True,
            intent="path_passthrough",
            data={
                "message": "Path-like slash input passed to model.",
                "prompt_rewrite": text,
                "already_executed": False,
            },
            error=None,
            source="command_registry",
            requires_clarification=False,
            bypass_ai=False,
            policy_flags=["command_registry", "path_passthrough"],
            source_node_id=self.node_id,
        )

    @property
    def conversation_state_manager(self) -> ConversationStateManager:
        """Return the state manager for the current request's user.

        Resolution order:
        1. Per-request manager published by ``_process_inner`` via
           ``use_request_manager`` (always the per-user manager for the
           active ``user_key``).
        2. Default manager bound at construction (single-user desktop,
           non-request setup code).

        This is the single chokepoint that prevents cross-tenant context
        bleed when one pipeline serves multiple users — i.e. the messaging
        ``MessageRouter`` path where one pipeline is shared across all
        linked Discord/Matrix users (CHAN-R1).
        """
        requested = get_request_conversation_manager()
        if requested is not None:
            return requested
        return self._default_state_manager

    @staticmethod
    def _normalize_required_user_id(value: object, *, source: str) -> str:
        from core.user_context import user_id_or_none

        normalized = user_id_or_none(value)
        if normalized is not None:
            return normalized
        raise ValueError("IntentPipeline requires explicit user_id (%s did not provide one)" % source)

    def _resolve_constructor_user_id(
        self,
        *,
        user_id: str | None,
        conversation_state_manager: ConversationStateManager | None,
        state: StatePort | None,
    ) -> str:
        candidates: list[tuple[str, object]] = [("user_id", user_id)]
        if conversation_state_manager is not None:
            candidates.append(
                (
                    "conversation_state_manager.user_id",
                    getattr(conversation_state_manager, "user_id", ""),
                )
            )
        if state is not None:
            candidates.append(("state.user_id", getattr(state, "user_id", "")))
        for source, candidate in candidates:
            try:
                return self._normalize_required_user_id(candidate, source=source)
            except ValueError:
                continue
        raise ValueError("IntentPipeline construction requires an explicit user_id")

    def _pending_key(self, user_key: str) -> str:
        candidate = user_key or self._default_state_manager.user_id
        return self._normalize_required_user_id(candidate, source="pending_state")

    @property
    def _pending_followup(self) -> bool:
        return self._pending_key("") in self._pending_followup_by_user

    @_pending_followup.setter
    def _pending_followup(self, value: bool) -> None:
        key = self._pending_key("")
        if value:
            self._pending_followup_by_user.setdefault(key, None)
        else:
            self._pending_followup_by_user.pop(key, None)

    @property
    def _pending_followup_category(self) -> str | None:
        return self._pending_followup_by_user.get(self._pending_key(""))

    @_pending_followup_category.setter
    def _pending_followup_category(self, value: str | None) -> None:
        key = self._pending_key("")
        if key in self._pending_followup_by_user or value is not None:
            self._pending_followup_by_user[key] = value

    def _set_pending_followup(self, user_key: str, category: object = None) -> None:
        key = self._pending_key(user_key)
        self._pending_followup_by_user[key] = str(category).strip() if category else None

    def _clear_pending_followup(self, user_key: str) -> None:
        self._pending_followup_by_user.pop(self._pending_key(user_key), None)

    def _has_pending_followup(self, user_key: str) -> bool:
        return self._pending_key(user_key) in self._pending_followup_by_user

    def _pending_followup_category_for(self, user_key: str) -> str | None:
        return self._pending_followup_by_user.get(self._pending_key(user_key))

    def _resolve_request_manager(self, user_key: str) -> ConversationStateManager:
        """Pick the correct per-user manager for this request.

        Returns the construction-time default when ``user_key`` is empty
        or matches the default manager's bound user_id (single-user
        desktop), otherwise delegates to the per-user singleton registry.
        """
        if not user_key:
            return self._default_state_manager
        if user_key == self._default_state_manager.user_id:
            return self._default_state_manager
        return get_conversation_manager(user_key)

    def _record_system_reminder_frame(self, *, text: str, origin: str, source_tag: str) -> None:
        """Append runtime model guidance to the canonical frame chain."""
        self.conversation_state_manager.add_message(
            system_reminder_frame(
                kind=FrameKind.SYSTEM_REMINDER,
                text=text,
                origin=origin,
                source_tag=source_tag,
            )
        )

    async def _dispatch_user_prompt_submit_hooks(self, prompt: str, *, user_key: str) -> PipelineResult | None:
        """Run UserPromptSubmit hooks before the prompt reaches the model."""

        from intent.hooks.dispatcher import dispatch_hook
        from intent.hooks.schema import HookEvent, HookEventName, HookResult
        from intent.hooks.settings_runner import HookExecutionContext

        manager = self.conversation_state_manager
        session_id = self._manager_session_id(manager)
        payload: dict[str, object] = {"prompt": prompt}
        if user_key:
            payload["user_id"] = user_key
        if session_id:
            payload["session_id"] = session_id
        transcript_path = self._hook_transcript_path(manager)
        if transcript_path:
            payload["transcript_path"] = transcript_path
        agent_type = self._hook_agent_type()
        if agent_type:
            payload["agent_type"] = agent_type

        event = HookEvent(
            name=HookEventName.USER_PROMPT_SUBMIT,
            payload=payload,
            session_id=session_id,
        )
        aggregate = HookResult()

        settings_runner = getattr(self, "hook_settings_runner", None)
        if settings_runner is not None:
            context = HookExecutionContext(
                cwd=self._hook_cwd(),
                permission_mode=self._hook_permission_mode(),
                transcript_path=transcript_path,
                agent_type=agent_type,
            )
            dispatch_async = getattr(settings_runner, "dispatch_async", None)
            if callable(dispatch_async):
                aggregate = aggregate.merge(await dispatch_async(event, context))
            else:
                aggregate = aggregate.merge(settings_runner.dispatch(event, context))

        registry = getattr(self, "hook_registry", None)
        if registry is not None:
            aggregate = aggregate.merge(
                dispatch_hook(
                    event,
                    dispatch_fn=registry.dispatch,
                    dispatch_state=getattr(registry, "dispatch_state", None),
                )
            )
        else:
            aggregate = aggregate.merge(dispatch_hook(event))

        self._record_hook_result_frames(event, aggregate)
        if not aggregate.blocks_tool:
            return None

        reason = aggregate.reason or "No reason provided."
        prevented = aggregate.prevent_continuation
        message = (
            "Operation stopped by UserPromptSubmit hook: %s"
            if prevented
            else "UserPromptSubmit operation blocked by hook: %s"
        ) % reason
        return PipelineResult(
            ok=False,
            intent="blocked",
            data={
                "message": message,
                "hook_event": HookEventName.USER_PROMPT_SUBMIT.value,
                "reason": reason,
                "prevent_continuation": prevented,
            },
            error="hook_prevented_continuation" if prevented else "hook_blocked",
            source="hook",
            requires_clarification=False,
            policy_flags=["hook_prevented_continuation" if prevented else "hook_blocked"],
            source_node_id=self.node_id,
        )

    def _record_hook_result_frames(self, event: object, result: object) -> None:
        """Persist model-visible hook output as canonical meta frames."""

        from intent.hooks.dispatcher import hook_result_frames

        if not getattr(result, "additional_contexts", ()) and not getattr(result, "system_message", None):
            if not getattr(result, "blocks_tool", False):
                return
        manager = self.conversation_state_manager
        for frame in hook_result_frames(event, result):  # type: ignore[arg-type]  # AGENT-02: event/result are runtime HookEvent/HookResult; this method takes object to avoid coupling the pipeline to the hooks module.
            try:
                manager.add_message(frame)
            except (RuntimeError, AttributeError, ValueError) as exc:
                logger.warning("UserPromptSubmit hook frame persistence failed: %s", exc)

    def _manager_session_id(self, manager: object) -> str | None:
        for attr in ("session_id", "_session_id"):
            value = getattr(manager, attr, None)
            if value:
                return str(value)
        return None

    def _hook_cwd(self) -> str:
        try:
            import os

            return os.getcwd()
        except OSError:
            return ""

    def _hook_permission_mode(self) -> str | None:
        try:
            from bootstrap.session_state import get_session_state

            mode = getattr(get_session_state(), "permission_mode", None)
            return str(mode) if mode else None
        except (ImportError, RuntimeError, AttributeError, ValueError):
            return None

    def _hook_agent_type(self) -> str | None:
        try:
            from bootstrap.session_state import get_session_state

            agent_type = getattr(get_session_state(), "agent_type", None)
            if agent_type is None:
                agent_type = getattr(get_session_state(), "main_thread_agent_type", None)
            if agent_type:
                text = str(agent_type).strip()
                return text or None
            return None
        except (ImportError, RuntimeError, AttributeError, ValueError):
            return None

    def _hook_transcript_path(self, manager: object | None) -> str | None:
        for attr in ("transcript_path", "_transcript_path"):
            value = getattr(manager, attr, None)
            if value:
                return str(value)
        try:
            from services.conversation.state_manager import get_conversation_persistence

            path = getattr(get_conversation_persistence(), "_db_path", None)
            return str(path) if path else None
        except (ImportError, OSError, RuntimeError, AttributeError, ValueError):
            return None

    def _extract_result_message(self, result: PipelineResult) -> str:
        """Best-effort assistant message for state tracking."""
        message_obj = result.data.get("message")
        if isinstance(message_obj, str) and message_obj.strip():
            return message_obj.strip()
        if result.error:
            return result.error
        return ""

    async def _execute_delayed_step(
        self,
        step_id: str,
        command_text: str,
        delay_seconds: float,
        *,
        user_key: str = "",
    ) -> None:
        """Execute a pending plan step after its configured delay.

        Multi-tenant: ``user_key`` is the tenant who scheduled the plan.
        It is rebound on the context (``set_current_user_id``) and
        propagated to ``self.process`` so the delayed command runs in
        the owner's lane / state manager / dedupe bucket — not under
        whichever tenant happens to dispatch the asyncio.sleep wakeup.
        Per-task keying of ``_scheduled_step_tasks`` uses
        ``(user_key, step_id)`` for the same reason.
        """
        scheduled_task_key = (user_key, step_id)
        current_task = asyncio.current_task()
        user_context_token: contextvars.Token[str] | None = None
        try:
            await asyncio.sleep(delay_seconds)
            if user_key:
                try:
                    from core.user_context import set_current_user_id

                    # #1907: capture the reset Token (matching the established
                    # convention in auth/middleware.py / voice_stream.py)
                    # instead of discarding it -- reset in the finally below
                    # so this scheduled Task never leaves the ambient identity
                    # bound past its own delayed-step execution.
                    user_context_token = set_current_user_id(user_key)
                except Exception:
                    logger.debug("Delayed step %s: failed to set ambient user_id", step_id)
            target_manager = self._resolve_request_manager(user_key)
            active_plan = target_manager.get_active_plan()
            if active_plan is None:
                return

            step_still_pending = any(
                step.get("id") == step_id and step.get("status") == "pending" for step in active_plan.get("steps", [])
            )
            if not step_still_pending:
                return

            logger.debug(
                "Executing delayed plan step %s after %.0fs (user=%s)",
                step_id,
                delay_seconds,
                user_key or "default",
            )
            target_manager.update_plan_step(step_id, "in_progress")
            result = await self.process(command_text, user_key=user_key)
            target_manager.update_plan_step(
                step_id,
                "completed" if result.ok else "failed",
                result=self._extract_result_message(result),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to execute delayed plan step %s", step_id)
            try:
                self._resolve_request_manager(user_key).update_plan_step(
                    step_id,
                    "failed",
                    result="Delayed plan step failed unexpectedly.",
                )
            except Exception:
                logger.debug(
                    "Failed to record delayed-step failure on user=%s",
                    user_key or "default",
                )
        finally:
            if current_task is not None and self._scheduled_step_tasks.get(scheduled_task_key) is current_task:
                self._scheduled_step_tasks.pop(scheduled_task_key, None)
            if user_context_token is not None:
                from core.user_context import reset_current_user_id

                reset_current_user_id(user_context_token)

    def _schedule_pending_plan_steps(self, *, user_key: str = "") -> None:
        """Create background tasks for delayed pending plan steps.

        Multi-tenant: ``user_key`` identifies which tenant owns the
        plan; we use ``(user_key, step_id)`` as the bookkeeping key so
        two tenants with the same generated step id do not collide.
        When ``user_key`` is empty we fall back to the resolved owner
        of the state manager being read.
        """
        target_manager = self._resolve_request_manager(user_key)
        active_plan = target_manager.get_active_plan()
        if active_plan is None:
            return

        owning_user = user_key or target_manager.user_id or ""

        for step in active_plan.get("steps", []):
            if step.get("status") != "pending" or not isinstance(step.get("id"), str):
                continue
            metadata = step.get("metadata", {})
            if not isinstance(metadata, Mapping):
                continue

            delay_seconds = metadata.get("delay_seconds")
            command_text = metadata.get("command_text")
            if not isinstance(delay_seconds, (int, float)) or not isinstance(command_text, str):
                continue
            if delay_seconds <= 0:
                continue

            step_id = cast(str, step["id"])
            task_key = (owning_user, step_id)
            existing_task = self._scheduled_step_tasks.get(task_key)
            if existing_task is not None:
                if existing_task.done():
                    self._scheduled_step_tasks.pop(task_key, None)
                else:
                    continue

            self._scheduled_step_tasks[task_key] = asyncio.create_task(
                self._execute_delayed_step(
                    step_id,
                    command_text,
                    delay_seconds,
                    user_key=owning_user,
                )
            )

    def _finalize_pipeline_result(
        self,
        *,
        user_text: str,
        result: PipelineResult,
        user_key: str = "",
        initial_plan_step_id: str | None = None,
    ) -> PipelineResult:
        """Record conversation state and update tracked plan metadata."""
        if result.source not in {"ai", "command_registry"}:
            assistant_message = self._extract_result_message(result)
            if assistant_message:
                self.conversation_state_manager.add_message("user", user_text)
                self.conversation_state_manager.add_message("assistant", assistant_message)

        if initial_plan_step_id:
            status = "completed" if result.ok else "failed"
            self.conversation_state_manager.update_plan_step(
                initial_plan_step_id,
                status,
                result=self._extract_result_message(result),
            )

        active_plan = self.conversation_state_manager.get_active_plan()
        if active_plan is not None:
            result.data["plan_state"] = active_plan

        # --- Follow-up routing: mark pending when continue_listening is set ---
        cl_flag = result.data.get("continue_listening")
        if cl_flag:
            # Preserve the task category so follow-ups inherit it instead of
            # re-classifying short replies like "Online" or "Wisconsin" which
            # lose the original context (e.g. web → general).
            _cat = result.data.get("task_category")
            self._set_pending_followup(user_key, _cat)
            logger.info(
                "continue_listening=True — next command will bypass fast-path classifiers (category=%s)",
                self._pending_followup_category_for(user_key),
            )
        else:
            # Any completed response without continue_listening clears the flag
            self._clear_pending_followup(user_key)

        # --- Self-diagnosis: log failures for later review ---
        # Viola "files a bug on herself" when she can't satisfy a request.
        if not result.ok and result.error:
            try:
                from diagnostics.bus import get_diagnostics_bus

                get_diagnostics_bus().emit(
                    "pipeline.unrecoverable_failure",
                    severity="WARNING",
                    message="Viola could not satisfy request",
                    user_text=user_text[:200],
                    intent=result.intent,
                    error=str(result.error)[:300],
                    source=result.source,
                )
            except Exception:
                # Diagnostics must never block the response; log and continue.
                logger.debug("Pipeline diagnostics record failed (non-blocking)", exc_info=True)

        # --- Response quality gate (post-processing, AI responses only) ---
        # Validate LLM output for template leaks, hallucinated actions,
        # vague non-answers, fabricated data, and sensitive solicitation.
        if result.source == "ai":
            try:
                from intent.response_quality_gate import apply_response_quality_gate

                msg = result.data.get("message")
                if isinstance(msg, str) and msg.strip():
                    ai_data = result.data.get("ai_data") or {}
                    command_results = ai_data.get("command_results")
                    tools_called = ai_data.get("tools_called")
                    cleaned = apply_response_quality_gate(
                        msg,
                        command_results=command_results,
                        tools_called=tools_called,
                    )
                    if cleaned is not msg:
                        result.data["message"] = cleaned
            except Exception:
                logger.debug("Response quality gate skipped (non-fatal)", exc_info=True)

        # --- Suggestion engine (post-processing) ---
        # Record command context and attach optional follow-up suggestion to
        # successful results.  Suggestions go into metadata only — never into
        # spoken text — so the UI can surface them as chips/cards.
        try:
            from intent.suggestion_engine import (
                maybe_suggest_followup,
                record_command,
            )

            suggestion_user_id = self._pending_key(user_key)
            record_command(result.intent, ok=result.ok, user_id=suggestion_user_id)

            if result.ok:
                suggestion = maybe_suggest_followup(result.intent, user_id=suggestion_user_id)
                if suggestion:
                    result.suggestion = suggestion
        except Exception:
            logger.debug("Suggestion engine skipped (non-fatal)", exc_info=True)

        return result

    async def route_command(
        self,
        text: str,
        history: list[dict[str, object]] | None = None,
        channel: object | None = None,
        user_key: str = "",
    ) -> dict[str, object]:
        """
        GPTPort-compatible routing method.

        Wraps the process() method to provide the route_command interface
        expected by AIInterpreter and other consumers.

        Args:
            text: User's input text
            history: Legacy external API parameter; model context comes from
                ConversationStateManager canonical frames.
            channel: Optional MessageChannel for approval and progress

        Returns:
            Dict with "type" ("command" or "answer"), and relevant data
        """
        result = await self.process(text, history=history, channel=channel, user_key=user_key)

        logger.debug(
            "route_command: result.intent=%s, result.ok=%s, result.source=%s",
            result.intent,
            result.ok,
            result.source,
        )

        # Metrics instrumentation — record routing source + category
        try:
            from admin.instrumentation import record_command_routed

            _src = result.source or "unknown"
            _cat = _INTENT_TO_CATEGORY.get(result.intent, "unknown")
            if _src in ("instant",):
                record_command_routed("instant", success=result.ok, category=_cat)
            elif _src in ("rule", "rules"):
                record_command_routed("rule_matched", success=result.ok, category=_cat)
            elif _src == "knowledge":
                record_command_routed("knowledge_resolved", success=result.ok, category=_cat)
            elif _src == "state":
                record_command_routed("state_answered", success=result.ok, category=_cat)
            elif result.intent == "answer":
                record_command_routed("llm_routed", success=result.ok, category=_cat)
            else:
                record_command_routed("llm_routed", success=result.ok, category=_cat)
        except Exception:
            logger.debug("Failed to record command routing metrics", exc_info=True)

        # Convert PipelineResult to GPT-style response format
        response: dict[str, object]
        if result.intent == "answer":
            # AI answered a question
            message_obj = result.data.get("message", "")
            message = message_obj if isinstance(message_obj, str) else ""
            logger.debug("route_command: returning type=answer, message_len=%d", len(message))
            response = {
                "type": "answer",
                "answer": message,
                "command": None,
                "params": {},
            }
            cl = result.data.get("continue_listening")
            if cl is not None:
                response["continue_listening"] = cl
            # Propagate content card from AI result for UI display
            _card = result.data.get("card")
            if _card and isinstance(_card, dict):
                response["card"] = _card
        elif result.source in (
            "instant",
            "rule",
            "rules",
            "knowledge",
            "state",
            "cache",
            "command_registry",
        ):
            # Already-executed instant/rule/knowledge/state command — return as answer so the
            # outer interpreter doesn't try to re-execute via CommandExecutor.
            # Return regardless of ok status: failed instant commands should
            # still propagate their error message (e.g. "Screenshot requires mss")
            # rather than falling through to rule-based parsing as type=error.
            message_obj = result.data.get("message", "")
            message = message_obj if isinstance(message_obj, str) else ""
            if not message:
                message = result.data.get("description", f"Done: {result.intent}")
            logger.debug(
                "route_command: instant/rule already executed, returning type=answer for %s",
                result.intent,
            )
            response = {
                "type": "answer",
                "answer": message,
                "command": None,
                "params": {},
                "already_executed": True,
                "source": result.source,
                "original_intent": result.intent,
            }
            # Propagate resume checkpoint data so callers can trigger
            # task resume when the instant command is resume_task.
            if result.intent == "resume_task":
                checkpoint_id = result.data.get("resume_checkpoint_id")
                if checkpoint_id:
                    response["resume_checkpoint_id"] = checkpoint_id
                    response["task_description"] = result.data.get("task_description", "")
        elif result.intent == "ai_no_result":
            no_result = result.data.get("no_result")
            if not isinstance(no_result, dict):
                no_result = {
                    "reason": "unknown_ai_no_result",
                    "retryable": True,
                }
            error_state = result.data.get("error_state")
            if not isinstance(error_state, dict):
                error_state = {"type": "ai_no_result", **no_result}
            response = {
                "type": "ai_no_result",
                "reason": str(no_result.get("reason", "unknown_ai_no_result")),
                "retryable": bool(no_result.get("retryable", True)),
                "no_result": no_result,
                "error_state": error_state,
                "command": None,
                "params": {},
                "already_spoken": True,
            }
        elif result.ok:
            # Successful command (from AI or other source — needs execution by caller)
            logger.debug("route_command: returning type=command, intent=%s", result.intent)
            params_obj = result.data.get("params", {})
            params = cast(dict[str, object], params_obj) if isinstance(params_obj, dict) else {}
            answer_obj = result.data.get("message")
            answer = answer_obj if isinstance(answer_obj, str) else None
            response = {
                "type": "command",
                "command": result.intent,
                "params": params,
                "answer": answer,  # Optional explanation
            }
        else:
            # Error or unknown - signal this properly so callers can fall back
            logger.debug("route_command: returning type=error, source=%s", result.source)
            message_obj = result.data.get("message", "")
            message = message_obj if isinstance(message_obj, str) else ""
            response = {
                "type": "error",
                "error": result.error or "No handler found",
                "message": message,
                "intent": result.intent,
                "source": result.source,
                "command": None,
                "params": {},
                "already_spoken": True,  # Pipeline already spoke via TTS
            }

        # Propagate suggestion metadata so downstream consumers (UI, WebSocket)
        # can render follow-up chips/cards without polling the REST endpoint.
        if result.suggestion is not None:
            response["suggestion"] = result.suggestion

        return response

    async def process(
        self,
        text: str,
        history: list[dict[str, object]] | None = None,
        force_ai: bool = False,
        channel: object | None = None,
        user_key: str = "",
    ) -> PipelineResult:
        """
        Process user input through unified pipeline.

        Multi-tenant: when the pipeline is running on a multi-tenant
        surface (cloud server, messaging router with linked users), the
        caller MUST supply ``user_key``.  We resolve that against the
        configured pipeline user_id to decide whether to fall through to
        the desktop default lane.

        Args:
            text: User input text
            history: Legacy external API parameter; model context comes from
                ConversationStateManager canonical frames.
            force_ai: Skip instant/rule matching, go straight to AI
            channel: Optional MessageChannel for approval and progress updates

        Returns:
            PipelineResult with execution details
        """
        request_user_key = self._pending_key(user_key)

        # Set ambient user identity for all downstream code. Even desktop
        # default-lane calls must use the explicit construction-time user_id;
        # never leave the context unset for callees to invent a fallback.
        # #1907: capture the reset Token (matching the established convention
        # in auth/middleware.py's AuthMiddleware and voice_stream.py's
        # _run_voice_stream_connection) instead of discarding it. This is the
        # main command-pipeline entry point, so every exit path -- including
        # the early lock-timeout returns inside ``_process_with_session_lock``
        # -- must unwind the binding in this outer finally rather than leave
        # it readable via get_current_user_id() past this request/Task.
        from core.user_context import reset_current_user_id, set_current_user_id

        user_context_token = set_current_user_id(request_user_key)
        try:
            return await self._process_with_session_lock(
                text=text,
                force_ai=force_ai,
                channel=channel,
                request_user_key=request_user_key,
            )
        finally:
            reset_current_user_id(user_context_token)

    async def _process_with_session_lock(
        self,
        *,
        text: str,
        force_ai: bool,
        channel: object | None,
        request_user_key: str,
    ) -> PipelineResult:
        """Session-lock-serialised execution body of ``process()``.

        Split out of ``process()`` so the identity-context capture/reset in
        that method wraps a single awaited call rather than re-indenting this
        whole body -- keeps this method's existing logic a no-op diff.
        """
        # Resolve the per-user state manager and publish it via contextvar
        # so every ``self.conversation_state_manager`` access inside the
        # request resolves to the correct user's history + plan. Without
        # this, a shared pipeline (desktop daemon + messaging listener
        # with multiple linked users) would leak user A's turns into user
        # B's context (CHAN-R1 cross-tenant leak).
        request_manager = self._resolve_request_manager(request_user_key)

        # C2: Session lane queuing — serialise commands per session key.
        # Multi-tenant: never collapse a missing ``user_key`` to a shared
        # "default" lane. The only fallback is the explicit construction-time
        # user id already validated above.
        session_key = request_user_key
        session_lock = await self._get_session_lock(session_key)
        acquired = False
        try:
            acquired = await asyncio.wait_for(
                session_lock.acquire(),
                timeout=self._session_lock_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Session lock timeout for key=%s after %.0fs; rejecting concurrent command",
                session_key,
                self._session_lock_timeout,
            )
            return PipelineResult(
                ok=False,
                intent="command_in_progress",
                data={
                    "message": "I'm still working on your previous request. Please wait for it to finish, then try again.",
                    "retryable": True,
                },
                error="command_in_progress",
                source="session_lock",
                bypass_ai=True,
                policy_flags=["session_busy"],
            )
        except Exception:  # noqa: BLE001, RUF100 - pre-existing lock-acquisition fail-open, unchanged by #1907
            logger.debug("Session lock acquisition failed for key=%s", session_key, exc_info=True)
            return PipelineResult(
                ok=False,
                intent="command_in_progress",
                data={
                    "message": "I'm still working on another request. Please wait for it to finish, then try again.",
                    "retryable": True,
                },
                error="command_lock_unavailable",
                source="session_lock",
                bypass_ai=True,
                policy_flags=["session_busy"],
            )

        try:
            # CHAN-R7: publish the caller's channel via a contextvar so
            # ``AIController._channel`` and ``TTSSpeaker._channel``
            # properties resolve to THIS request's channel during any
            # direct access — preventing cross-tenant progress sends and
            # TTS-gate flips when concurrent different-user requests
            # share the pipeline's ai_controller / tts_speaker singletons.
            from messaging.channel import use_request_channel

            with use_request_manager(request_manager), use_request_channel(channel):
                result = await self._process_inner(
                    text=text,
                    force_ai=force_ai,
                    channel=channel,
                    user_key=request_user_key,
                )
            return result
        finally:
            if acquired:
                session_lock.release()

    async def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Return the per-session lock, creating it if needed."""
        async with self._session_lock_guard:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = asyncio.Lock()
            return self._session_locks[session_key]

    async def _process_inner(
        self,
        text: str,
        force_ai: bool = False,
        channel: object | None = None,
        user_key: str = "",
        allow_multi_step_plan: bool = True,
    ) -> PipelineResult:
        """Inner processing logic (called under session lock)."""
        del allow_multi_step_plan

        # Wire channel into AI controller for this request
        if channel is not None:
            self.ai_controller._channel = channel
            self._tts_speaker._channel = channel
        else:
            self._tts_speaker._channel = None
        text = self._normalize_input(text)

        # ── C3: QueryGuard — deduplicate rapid-fire identical commands ──
        # Multi-tenant: scope the dedupe bucket by the resolved tenant
        # rather than collapsing missing keys to "default" — that
        # bucket would otherwise be shared across every userless
        # background path.
        import hashlib as _hlib

        _user_key_or_default = self._pending_key(user_key)
        _qhash = _hlib.md5((_user_key_or_default + ":" + text.strip().lower()).encode()).hexdigest()  # nosec B324
        _now = time.monotonic()
        _prev = self._recent_queries.get(_qhash)
        if _prev is not None and (_now - _prev) < self._query_guard_window:
            return PipelineResult(
                ok=True,
                intent="debounced",
                data={"message": "I'm already working on that."},
                policy_flags=["debounced"],
            )
        self._recent_queries[_qhash] = _now
        # Prune entries older than 30 seconds
        _cutoff = _now - 30.0
        self._recent_queries = {k: v for k, v in self._recent_queries.items() if v > _cutoff}

        original_text = text
        initial_plan_step_id: str | None = None

        def finish(result: PipelineResult) -> PipelineResult:
            return self._finalize_pipeline_result(
                user_text=original_text,
                result=result,
                user_key=user_key,
                initial_plan_step_id=initial_plan_step_id,
            )

        if not text:
            return finish(
                PipelineResult(
                    ok=False,
                    intent="unknown",
                    data={},
                    error="Empty input",
                    source="validation",
                    requires_clarification=True,
                    policy_flags=["empty_input"],
                    source_node_id=self.node_id,
                )
            )

        # ── Hard input-length cap — the choke point for the ReDoS class ──
        # MUST run before the crisis/attack/sensitive/gibberish prefilters:
        # each of those runs regexes on the raw input, and an unbounded payload
        # turned one into a full-API event-loop wedge (2026-07-05). Reject
        # over-length input cleanly rather than let any prefilter see it.
        if _input_exceeds_length_cap(text):
            logger.warning("Input rejected: %d chars exceeds cap %d", len(text), _MAX_PIPELINE_INPUT_CHARS)
            return finish(
                PipelineResult(
                    ok=False,
                    intent="invalid",
                    data={"message": OVERLONG_INPUT_RESPONSE},
                    error="input_too_long",
                    source="validation",
                    requires_clarification=True,
                    policy_flags=["input_too_long"],
                    source_node_id=self.node_id,
                )
            )

        # ── Crisis detection — runs BEFORE everything else ───────────────
        if detect_crisis(text):
            logger.warning("Crisis language detected in user input")
            return finish(
                PipelineResult(
                    ok=True,
                    intent="crisis",
                    data={"message": CRISIS_RESPONSE},
                    source="prefilter",
                    requires_clarification=False,
                    policy_flags=["crisis_detected"],
                    source_node_id=self.node_id,
                )
            )

        # ── Phase 0: Pre-LLM input security filter ──────────────────────
        attack = detect_obvious_attack_input(text)
        if attack:
            logger.warning(
                "Input prefilter blocked: category=%s pattern=%s",
                attack.category,
                attack.pattern_id,
            )
            return finish(
                PipelineResult(
                    ok=False,
                    intent="blocked",
                    data={"message": BLOCKED_INPUT_RESPONSE},
                    error="blocked_by_prefilter",
                    source="prefilter",
                    requires_clarification=False,
                    policy_flags=["security_blocked"],
                    source_node_id=self.node_id,
                )
            )

        # Sensitive-credential storage request — deterministic refusal BEFORE
        # LLM routing so it survives rate limits and model updates.  Covers
        # SEC-007/8/9/10: password / API key / SSN / credit card storage.
        # TTS is emitted downstream by the chat broadcaster from ``message``,
        # matching the existing crisis/blocked/gibberish short-circuit shape.
        sensitive = detect_sensitive_request(text)
        if sensitive:
            logger.warning(
                "Input prefilter blocked sensitive-credential storage: pattern=%s",
                sensitive.pattern_id,
            )
            return finish(
                PipelineResult(
                    ok=False,
                    intent="blocked",
                    data={
                        "message": SENSITIVE_STORAGE_RESPONSE,
                        "blocked": True,
                    },
                    error="blocked_sensitive_storage",
                    source="prefilter",
                    requires_clarification=False,
                    policy_flags=["security_blocked", "sensitive_credential"],
                    source_node_id=self.node_id,
                )
            )

        if is_gibberish(text):
            logger.info("Gibberish detector triggered for input")
            return finish(
                PipelineResult(
                    ok=False,
                    intent="gibberish",
                    data={"message": GIBBERISH_RESPONSE},
                    error="gibberish_input",
                    source="prefilter",
                    requires_clarification=True,
                    policy_flags=["gibberish_blocked"],
                    source_node_id=self.node_id,
                )
            )

        command_result = await self.try_command_registry(text, user_key=user_key)
        if command_result is not None:
            # F-007 / F-039: prompt-style commands and path-like passthrough
            # rewrite the active user-turn text and continue into the agent
            # loop instead of bypassing AI.
            prompt_rewrite = (
                command_result.data.get("prompt_rewrite") if isinstance(command_result.data, dict) else None
            )
            if not command_result.bypass_ai and isinstance(prompt_rewrite, str) and prompt_rewrite.strip():
                logger.info(
                    "Command registry rewrote user turn (%s) into prompt for agent loop",
                    command_result.intent,
                )
                text = prompt_rewrite.strip()
                # Drop through to the normal LLM routing path below.
            else:
                logger.info("Command registry handled: %s", command_result.intent)
                return finish(command_result)

        logger.info("🎯 Pipeline processing: '%s'", text)
        _t0 = time.perf_counter()
        effective_text = text
        user_prompt_submit_dispatched = False

        async def dispatch_user_prompt_submit_once(
            prompt: str,
        ) -> PipelineResult | None:
            nonlocal user_prompt_submit_dispatched
            if user_prompt_submit_dispatched:
                return None
            user_prompt_submit_dispatched = True
            return await self._dispatch_user_prompt_submit_hooks(prompt, user_key=user_key)

        # --- Follow-up routing: bypass fast-path when continuing a conversation ---
        # When the previous response had continue_listening=True (e.g. "Which state
        # will you file in?"), the next user message (e.g. "Wisconsin") MUST go
        # directly to AI with conversation history — otherwise short answers get
        # misclassified as new intents (weather, music, etc.) by the fast-path
        # classifiers.
        pending_followup = self._has_pending_followup(user_key)
        pending_followup_category = self._pending_followup_category_for(user_key)
        if pending_followup and not force_ai:
            logger.info(
                "Follow-up routing: pending_followup=True, forcing AI tier for '%.60s' (category=%s)",
                text,
                pending_followup_category,
            )
            force_ai = True
            # Flag is cleared in _finalize_pipeline_result based on the next
            # response's continue_listening value.

        ai_controller = getattr(self, "ai_controller", None)
        # Pass the follow-up category override so ai_controller preserves the
        # task category from the previous turn instead of re-classifying short
        # follow-up text like "Online" or "yes" which has no category signal.
        if ai_controller is not None:
            ai_controller._followup_category_override = pending_followup_category if pending_followup else None
        # Room targeting: the LLM tier accepts `target_room` natively as a
        # tool parameter, so no regex pre-processor runs here. The previous
        # `intent.room_target_extractor` was a hard-coded classifier that
        # produced false-positives like "turn on the living room lights" →
        # command_text="turn" (eating the entire smart-home command). Removed
        # 2026-04-27 per founder direction "it should just be the LLM
        # understanding the phrase and taking action" (memory antipattern #3
        # / project_no_tool_classifier.md).

        # Record interaction for active room tracking
        if self._active_room_tracker is not None and self.node_id:
            record = getattr(self._active_room_tracker, "record_interaction", None)
            if callable(record):
                record(self.node_id)

        try:
            # PHASE 1: Instant Commands (fastest path, ~30 safety/latency-critical types)
            # ALWAYS try instant commands first, even when force_ai is set.
            # force_ai was designed for ambiguous follow-up replies ("Wisconsin", "yes")
            # that need conversational context — not for safety/transport commands
            # like "stop" which have deterministic handlers.
            from diagnostics import latency_spans

            with latency_spans.span("TRY_INSTANT"):
                instant_result = await self._processors.try_instant(text, user_key=user_key)
            if instant_result:
                logger.info("⚡ Instant command matched: %s", instant_result.intent)
                self._record_efficiency(instant_result, _t0)
                # When force_ai was set (follow-up routing), clear _pending_followup
                # since the user issued a new clear-intent command, breaking the
                # conversational chain.
                if force_ai:
                    self._clear_pending_followup(user_key)
                    logger.info(
                        "Instant command broke follow-up chain: force_ai was set but '%s' matched instant",
                        instant_result.intent,
                    )
                return finish(instant_result)
            text = effective_text

            # B4: micro_compact — trim conversation context before AI routing
            try:
                _csm = self.conversation_state_manager
                _budget = getattr(_csm, "default_context_tokens", 4000)
                if hasattr(_csm, "micro_compact"):
                    _csm.micro_compact(_budget)
            except Exception:
                logger.debug("Conversation micro-compaction failed")

            # PHASE 2: AI Routing (single LLM call, all tools)
            hook_block = await dispatch_user_prompt_submit_once(text)
            if hook_block is not None:
                return finish(hook_block)

            from diagnostics import latency_spans

            with latency_spans.span("TRY_AI"):
                ai_result = await self._processors.try_ai(text, user_key=user_key)
            if ai_result:
                # Debug logging to catch message field regressions
                logger.debug(
                    "Pipeline AI result: intent=%s has_message=%s message_len=%d data_keys=%s",
                    ai_result.intent,
                    "message" in ai_result.data,
                    (len(message_value) if isinstance((message_value := ai_result.data.get("message")), str) else 0),
                    list(ai_result.data.keys()),
                )
                # Per PRD: STOP-class intents must cancel in-flight STT/LLM work
                if ai_result.intent in ("stop", "pause", "shut up"):
                    self._cancel_inflight_work()

                logger.info("🤖 AI routed: %s", ai_result.intent)
                self._record_efficiency(
                    ai_result,
                    _t0,
                )

                return finish(ai_result)

            # PHASE 4: Last-resort ROUTE escalation before giving up
            # If we got here, Phase 3 returned None (no gpt_handler).
            # Try to create one on-the-fly and re-run with ROUTE tier.
            if not self.gpt_handler:
                logger.info(
                    "Phase 4 fallback: no gpt_handler — attempting on-the-fly LLM router for '%s'",
                    text[:60],
                )
                try:
                    _fallback_router = create_llm_router()
                    if _fallback_router:
                        self.gpt_handler = _fallback_router
                        self.ai_controller.client = _fallback_router
                        _retry_result = await self._processors.try_ai(text, user_key=user_key)
                        if _retry_result:
                            logger.info(
                                "Phase 4 ROUTE escalation succeeded: intent=%s",
                                _retry_result.intent,
                            )
                            self._record_efficiency(_retry_result, _t0)
                            return finish(_retry_result)
                except Exception:
                    logger.debug(
                        "Phase 4 ROUTE escalation failed",
                        exc_info=True,
                    )

            # PHASE 4b: True fallback (no match found)
            # Error identifier for debugging (consistent, searchable in logs)
            error_id = "PIPELINE_NO_MATCH"
            logger.warning("[%s] No match found for: '%s'", error_id, text)

            # User-facing messages (randomized for natural feel)
            error_msgs = [
                "No matching command found. Could you try saying it differently?",
                "That didn't match any known command. Mind rephrasing?",
                "No handler matched your request. Could you try again?",
            ]
            error_msg = random.choice(error_msgs)
            # NOTE: Don't speak here - IntentInterpreter will handle TTS to avoid double-speak
            fallback_result = PipelineResult(
                ok=False,
                intent="unknown",
                data={"message": error_msg, "error_id": error_id},
                error="No handler found for command",
                source="fallback",
                requires_clarification=True,
                policy_flags=["no_match"],
                source_node_id=self.node_id,
            )
            self._record_efficiency(fallback_result, _t0)
            return finish(fallback_result)

        except Exception as e:
            # Error identifier for debugging (consistent, searchable in logs)
            error_id = "PIPELINE_EXCEPTION"
            logger.exception("[%s] Pipeline error: %s", error_id, e)

            # NOTE: Don't speak here - IntentInterpreter will handle TTS to avoid double-speak
            error_state = {
                "type": "pipeline_exception",
                "reason": "pipeline_exception",
                "retryable": True,
                "error_id": error_id,
                "detail": type(e).__name__,
            }
            exc_result = PipelineResult(
                ok=False,
                intent="error",
                data={"error_id": error_id, "error_state": error_state},
                error="pipeline_exception",
                source="exception",
                requires_clarification=False,
                policy_flags=["exception"],
                source_node_id=self.node_id,
            )
            self._record_efficiency(exc_result, _t0)
            return finish(exc_result)

    def _record_efficiency(
        self,
        result: PipelineResult,
        t0: float,
        history_len: int = 0,
    ) -> None:
        """Record pipeline efficiency metrics for a completed request.

        Non-blocking — fires and forgets to the instrumentation layer.

        Args:
            result: The pipeline result.
            t0: Start time from perf_counter.
            history_len: Number of prior turns in conversation (for WS7 tracking).
        """
        try:
            from admin.instrumentation import record_pipeline_request

            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            source = result.source or "unknown"

            # Determine route label for metrics
            route = source
            if source == "ai":
                # Distinguish ask vs route vs agent
                flags = result.policy_flags or []
                if "ai_agent" in flags:
                    route = "llm_ask_agent"
                elif "ai_answer" in flags:
                    route = "llm_ask_simple"
                elif "ai_processed" in flags:
                    route = "llm_route"
                elif result.intent == "answer":
                    route = "llm_ask_simple"
                else:
                    route = "llm_route"

            # ── Telemetry: record pipeline exit phase for cost validation ──
            try:
                from billing.telemetry import record_pipeline_exit as _record_tel_exit

                _ROUTE_TO_PHASE = {
                    "instant": "instant",
                    "rule": "rules",
                    "rules": "rules",
                    "plugin": "rules",
                    "knowledge": "knowledge",
                    "state": "knowledge",
                    "cache": "knowledge",
                    "llm_ask_simple": "llm_ask",
                    "llm_ask": "llm_ask",
                    "llm_route": "llm_route",
                    "llm_ask_agent": "llm_agent",
                    "agent": "llm_agent",
                }
                _tel_phase = _ROUTE_TO_PHASE.get(route)
                if _tel_phase:
                    _record_tel_exit(phase=_tel_phase)
            except Exception:
                # Telemetry must never crash the pipeline; log and continue.
                logger.debug("Pipeline telemetry record failed (non-fatal)", exc_info=True)

            # For local routes: 0 tokens used, baseline from table
            baseline = _RAW_LLM_BASELINE.get(source, 0)

            # For LLM routes: tokens are already recorded by the provider.
            # Here we record 0 for input/output since the LLM provider
            # already tracks those. We just track the route + baseline.
            total_tokens = 0
            if source == "ai":
                # Estimate from known prompt sizes (per efficiency audit)
                if route in ("llm_ask_simple", "llm_ask"):
                    total_tokens = 787  # ask tier avg
                elif route == "llm_ask_agent":
                    total_tokens = 7_100  # agent tier avg
                elif route == "llm_route":
                    total_tokens = 4_776  # route tier avg
                # Use route-specific baseline
                _baseline_key = "llm_ask" if route == "llm_ask_simple" else route
                baseline = _RAW_LLM_BASELINE.get(_baseline_key, 2_000)

            tokens_saved = max(0, baseline - total_tokens)

            record_pipeline_request(
                route=route,
                total_tokens=total_tokens,
                latency_ms=elapsed_ms,
                baseline_tokens=baseline,
                tokens_saved=tokens_saved,
                ok=result.ok,
            )

            # WS7: Record conversation depth + cumulative tokens
            if history_len > 0 and source == "ai":
                try:
                    from telemetry import get_accumulator

                    acc = get_accumulator()
                    if acc is not None:
                        acc.record_conversation_turn(
                            turn_depth=history_len + 1,
                            cumulative_tokens=total_tokens,
                        )
                except Exception:
                    logger.debug("Failed to record conversation turn metrics", exc_info=True)
        except Exception:
            logger.debug(
                "Failed to record pipeline metrics; never block the pipeline",
                exc_info=True,
            )

    def _cancel_inflight_work(self) -> None:
        """
        Cancel any in-flight STT/LLM work per PRD cancellation protocol.

        Called when STOP-class intents (stop, pause, shut up) are received.

        Per PRD Section 11: Cancellation Protocol:
        - Immediately stop consuming current audio stream for that session
        - Discard any buffered audio for that utterance
        - Cancel in-flight LLM calls if platform supports it
        - Do NOT update conversation state based on partial results
        """
        # Cancel voice pipeline STT work if available
        if self.voice_pipeline is not None:
            cancel_stt = getattr(self.voice_pipeline, "cancel_inflight_stt", None)
            if callable(cancel_stt):
                try:
                    cancel_stt()
                    logger.info("Cancelled voice pipeline STT work due to STOP-class intent")
                except Exception as exc:
                    logger.debug("Error cancelling voice pipeline STT work: %s", exc)

        # Also cancel voice command processing if available
        if self.voice_pipeline is not None:
            cancel_active = getattr(self.voice_pipeline, "_cancel_active_command", None)
            if callable(cancel_active):
                try:
                    cancel_active()
                    logger.debug("Cancelled voice pipeline command processing")
                except Exception as exc:
                    logger.debug("Error cancelling voice pipeline command: %s", exc)

        # Cancel tracked STT task (if any)
        if self._active_stt_task and not self._active_stt_task.done():
            self._active_stt_task.cancel()
            logger.info("Cancelled in-flight STT task")
            self._active_stt_task = None

        # Cancel tracked LLM task
        if self._active_llm_task and not self._active_llm_task.done():
            self._active_llm_task.cancel()
            logger.info("Cancelled in-flight LLM work")
            self._active_llm_task = None

        # Cancel any background TTS tasks as part of cancellation protocol
        self._tts_tasks.cancel_all_nowait()

    def _normalize_input(self, text: str) -> str:
        # Mechanical normalization only - collapse surrounding whitespace and
        # trailing terminal punctuation. We deliberately do NOT strip speech
        # disfluencies or leading discourse markers anymore (2026-05-29 boxing
        # audit): rewriting the user's words before the model sees them hides
        # context and silently corrupts real requests (e.g. "play something I
        # like" -> "play something I" via the disfluency strip). The model reads
        # the raw utterance, exactly as Claude Code TS forwards user input
        # verbatim rather than pre-editing filler out of natural prose.
        normalized = (text or "").strip()
        if not normalized:
            return ""
        normalized = normalized.rstrip("?!").strip()
        return normalized

    async def _create_pipeline_result(
        self,
        intent: str,
        params: Mapping[str, object],
        data: Mapping[str, object],
        source: str,
        text: str,
        bypass_ai: bool = False,
        requires_clarification: bool = False,
        policy_flags: list[str] | None = None,
        error: str | None = None,
        ok: bool = True,
    ) -> PipelineResult:
        """Create a standardized PipelineResult with common processing."""
        # Handle stop/pause commands
        if intent in ("stop", "pause"):
            self._cancel_inflight_work()

        # Evaluate clarification and policy flags if not already provided
        if requires_clarification is False and policy_flags is None:
            requires_clarification, policy_flags = self._evaluate_policy_and_clarification(intent, params or {}, text)

        return PipelineResult(
            ok=ok,
            intent=intent,
            data=dict(data),
            error=error,
            source=source,
            source_node_id=self.node_id,
            bypass_ai=bypass_ai,
            requires_clarification=requires_clarification,
            policy_flags=policy_flags or [],
        )

    def _evaluate_policy_and_clarification(
        self, intent_name: str, intent_args: Mapping[str, object], text: str
    ) -> tuple[bool, list[str]]:
        """
        Evaluate whether an intent requires clarification and what policy flags apply.

        This is a deterministic function: same input + state → same output.

        Per PRD:
        - requires_clarification should be True when the intent is ambiguous, risky, or needs follow-up
        - policy_flags should list all policy rule IDs that matched

        Args:
            intent_name: The intent name (e.g., "play", "pause")
            intent_args: The intent arguments
            text: Original user text (for context)

        Returns:
            Tuple of (requires_clarification: bool, policy_flags: List[str])
        """
        requires_clarification = False
        policy_flags: list[str] = []

        # Check for ambiguous queries (play without clear target)
        if intent_name in ("play", "play_next", "add_to_queue"):
            query_obj = intent_args.get("query", "")
            query = query_obj.strip() if isinstance(query_obj, str) else ""
            if not query or len(query) < 2:
                requires_clarification = True
                policy_flags.append("ambiguous_query")

        # Check for provider requirements
        if intent_name in ("play", "play_next", "add_to_queue"):
            # If no active provider detected, this would require clarification
            # This is handled elsewhere (NoActiveProviderError), but we track it
            pass

        # Check for potentially unsafe content (placeholder for future content moderation)
        # For now, we don't have content moderation, so this is a placeholder
        unsafe_keywords: list[str] = []  # Could be populated from policy config
        if intent_name in ("play", "play_next", "add_to_queue"):
            query_obj = intent_args.get("query", "")
            query_lower = query_obj.lower() if isinstance(query_obj, str) else ""
            for keyword in unsafe_keywords:
                if keyword in query_lower:
                    requires_clarification = True
                    policy_flags.append("unsafe_content")
                    break

        # Check for account scope requirements
        # Some intents may require account-level access (e.g., playlist management)
        account_scoped_intents = ["play_playlist", "create_playlist", "delete_playlist"]
        if intent_name in account_scoped_intents:
            policy_flags.append("account_scope_required")
            # Only require clarification if account is not linked
            # This would be checked against actual state, but for now we default to False

        return requires_clarification, policy_flags

    async def cleanup_stale_session_locks(self) -> int:
        """Remove session locks that are not currently held (C2).

        Returns the number of locks removed.  Safe to call periodically.
        """
        removed = 0
        async with self._session_lock_guard:
            stale = [k for k, v in self._session_locks.items() if not v.locked()]
            for k in stale:
                del self._session_locks[k]
                removed += 1
        if removed:
            logger.debug("Cleaned up %d stale session locks", removed)
        return removed


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================


def create_llm_router(
    config_or_settings=None,
    prefer_local: bool | None = None,
) -> GPTPort | None:
    """
    Create an LLM provider router based on user settings.

    This function creates a provider-agnostic router that supports
    multiple LLM backends (OpenAI, Anthropic, Google, Ollama, etc.)
    based on user configuration in Settings > AI.

    Args:
        config_or_settings: Config object or settings dict (for backward compatibility)
        prefer_local: Ignored (kept for backward compatibility)

    Returns:
        ProviderAgnosticRouter instance (implements GPTPort) or None if unavailable
    """
    logger.debug("create_llm_router: starting, config=%s", bool(config_or_settings))
    try:
        from services.llm.provider_router import ProviderAgnosticRouter

        router = ProviderAgnosticRouter(config_or_settings=config_or_settings)

        # Enhanced diagnostics
        status = router.get_status()
        logger.debug(
            "create_llm_router: router created, status=%s",
            {k: v for k, v in status.items() if k != "primary" and k != "fallback"},
        )

        # Only return router if at least one provider is available
        if router.is_available():
            logger.info("✅ Provider-agnostic LLM router created")
            if status.get("primary"):
                logger.info(
                    "   Primary: %s (%s)",
                    status["primary"].get("name"),
                    status["primary"].get("model"),
                )
            return router
        else:
            logger.warning(
                "No LLM providers available for router. init_error=%s",
                status.get("init_error", "unknown"),
            )
            return None
    except Exception as e:
        logger.warning("Could not create LLM router: %s", e, exc_info=True)
        return None


def create_pipeline(
    music: MusicPort,
    tts: IntentTTSPort | None = None,
    state: StatePort | None = None,
    gpt_handler: GPTPort | None = None,
    node_id: str | None = None,
    is_hub: bool = False,
    config_or_settings=None,
    use_llm_router: bool | None = None,
    plugin_manager: object | None = None,
    conversation_state_manager: ConversationStateManager | None = None,
    user_id: str | None = None,
) -> IntentPipeline:
    """
    Factory function to create intent pipeline.

    Canonical AI path: Uses ProviderAgnosticRouter as the default AI engine,
    supporting OpenAI, Anthropic, Google, Ollama, and other LLM providers.

    Args:
        music: Music player
        tts: TTS engine (optional)
        state: App state (optional)
        gpt_handler: GPT handler (optional, will use LLM router if None)
        node_id: Node identifier for active room tracking (optional)
        is_hub: Whether this node is the Hub (for supremacy)
        config_or_settings: Config object for LLM router creation (optional)
        use_llm_router: Ignored (kept for backward compatibility)
        user_id: Explicit owner for desktop/default-lane pipeline state

    Returns:
        Configured IntentPipeline with provider-agnostic LLM router
    """
    # Canonical AI path: Always use LLM router unless explicitly overridden
    if gpt_handler is None:
        router = create_llm_router(config_or_settings)
        if router:
            gpt_handler = router
            logger.info("✅ Canonical LLM router configured for intent pipeline")
        else:
            logger.warning("⚠️ No LLM providers available - AI features will be disabled")

    # Auto-discover plugin manager if not provided
    if plugin_manager is None:
        try:
            from plugins.singleton import get_plugin_manager

            plugin_manager = get_plugin_manager()
        except Exception as exc:
            logger.debug("Plugin manager not available for pipeline: %s", exc)

    return IntentPipeline(
        music,
        tts,
        state,
        gpt_handler,
        node_id,
        is_hub,
        plugin_manager=plugin_manager,
        conversation_state_manager=conversation_state_manager,
        user_id=user_id,
    )


# NOTE: _strip_speech_disfluencies / _SPEECH_DISFLUENCY_RE /
# _LEADING_DISCOURSE_MARKER_RE were removed in the 2026-05-29 boxing audit.
# They mutated the user's utterance ("um/uh/like" + leading "hey/ok/so") before
# the model saw it, which both hid context and corrupted real requests
# (e.g. "play something I like" -> "play something I"). The model now receives
# the raw text; see IntentPipeline._normalize_input for the mechanical-only
# normalization that remains.
