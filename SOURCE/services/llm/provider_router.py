"""
Provider-Agnostic LLM Router

Routes LLM requests using the new provider factory system.
Supports multiple providers (OpenAI, Anthropic, Google, Ollama, etc.)
based on user settings.

Includes timeout protection and circuit breaker for reliability.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, NamedTuple

from core.constants import OLLAMA_DEFAULT_BASE_URL, TIMEOUT_HOUR
from core.exceptions import LLMError, LLMTimeoutError, ProviderUnavailableError
from core.logging_config import get_logger
from services.conversation.message_invariants import (
    repair_delta_messages,
    repair_full_transcript,
)
from services.llm.request_policy import (
    ProviderRequestContext,
    ProviderResponseEnvelope,
    execute_with_policy,
    new_request_id,
)
from utils.timeout_manager import get_timeout_manager

if TYPE_CHECKING:
    from services.llm.prompts.types import PromptFrameBundle

logger = get_logger(__name__)
_ACTIVE_ROUTER: ProviderAgnosticRouter | None = None  # type: ignore[name-defined]


class _ProviderSnapshot(NamedTuple):
    signature: tuple[Any, ...] | None
    provider: Any
    fallback_provider: Any
    haiku_provider: Any


_FREEZE_PROVIDER_SELECTION: ContextVar[bool] = ContextVar("freeze_provider_selection", default=False)
_FROZEN_PROVIDER_SNAPSHOTS: ContextVar[dict[int, _ProviderSnapshot] | None] = ContextVar(
    "frozen_provider_snapshots",
    default=None,
)


def get_settings_manager():
    from ui.settings_manager import get_settings_manager as _get_settings_manager

    return _get_settings_manager()


def create_provider_from_settings():
    from services.llm.factory import (
        create_provider_from_settings as _create_provider_from_settings,
    )

    return _create_provider_from_settings()


def _extract_continuity_tool_uses(
    provider: Any | None,
    continuity: dict[str, Any] | None = None,
    *,
    messages: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Return tool-call ids already present in provider continuity state."""

    state = continuity if isinstance(continuity, dict) else None
    if state is None:
        for candidate in (provider, getattr(provider, "_compat_provider", None)):
            candidate_state = getattr(candidate, "_responses_continuity", None)
            if isinstance(candidate_state, dict):
                state = candidate_state
                break
    if not state:
        return set()

    mode = str(state.get("mode") or state.get("continuity_mode") or "").strip().lower()
    if mode == "response_items":
        response_items = state.get("response_items")
        if not isinstance(response_items, list):
            return set()
        ids: set[str] = set()
        for item in response_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"function_call", "tool_use"}:
                tool_use_id = str(item.get("call_id") or item.get("id") or item.get("tool_use_id") or "").strip()
                if tool_use_id:
                    ids.add(tool_use_id)
        return ids

    if mode == "previous_response_id":
        return _tool_result_ids_from_messages(messages or [])

    return set()


def _tool_result_ids_from_messages(messages: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id") or "").strip()
                if tool_use_id:
                    ids.add(tool_use_id)
        tool_call_id = message.get("tool_call_id") or message.get("tool_use_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.add(tool_call_id)
    return ids


_RESPONSES_DELTA_KWARGS = frozenset(
    {
        "messages_are_delta",
        "continuity",
        "responses_continuity",
        "previous_response_id",
        "response_items",
    }
)


def _strip_responses_delta_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    fallback_kwargs = dict(kwargs)
    for key in _RESPONSES_DELTA_KWARGS:
        fallback_kwargs.pop(key, None)
    return fallback_kwargs


def _parse_responses_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return copy.deepcopy(arguments)
    if not isinstance(arguments, str):
        return {}

    raw_arguments = arguments.strip()
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"arguments": raw_arguments}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


def _responses_content_to_native_text_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "text", "text": text}] if text else []
    if not isinstance(content, list):
        return []

    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type not in {"input_text", "output_text", "text", "refusal"}:
            continue
        text = str(block.get("text") or block.get("content") or block.get("refusal") or "").strip()
        if text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _responses_continuity_item_to_native_message(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    if item_type == "function_call":
        call_id = str(item.get("call_id") or item.get("id") or "").strip()
        if not call_id:
            return None
        return {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": str(item.get("name") or "unknown_tool"),
                    "input": _parse_responses_tool_arguments(item.get("arguments")),
                }
            ],
        }

    if item_type == "function_call_output":
        call_id = str(item.get("call_id") or item.get("tool_use_id") or "").strip()
        if not call_id:
            return None
        output = item.get("output", "")
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": output if output is not None else "",
                }
            ],
        }

    if item_type == "compaction":
        summary = str(item.get("summary") or "").strip()
        if not summary:
            return None
        return {"role": "user", "content": [{"type": "text", "text": summary}]}

    role = str(item.get("role") or "")
    if item_type == "message" and role not in {"user", "assistant"}:
        role = "assistant"
    if role not in {"user", "assistant"}:
        return None

    content = item.get("content")
    if isinstance(content, str):
        text = content.strip()
        return {"role": role, "content": text} if text else None
    blocks = _responses_content_to_native_text_blocks(content)
    return {"role": role, "content": blocks} if blocks else None


def _rebuild_responses_delta_fallback_messages(
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> list[dict[str, Any]] | None:
    continuity = kwargs.get("responses_continuity")
    if not isinstance(continuity, dict):
        continuity = kwargs.get("continuity")
    if not isinstance(continuity, dict):
        return None

    mode = str(continuity.get("mode") or continuity.get("continuity_mode") or "").strip().lower()
    if mode != "response_items":
        return None

    response_items = continuity.get("response_items")
    if not isinstance(response_items, list):
        response_items = kwargs.get("response_items")
    if not isinstance(response_items, list) or not response_items:
        return None

    rebuilt: list[dict[str, Any]] = []
    for item in response_items:
        if not isinstance(item, dict):
            continue
        native_message = _responses_continuity_item_to_native_message(item)
        if native_message is not None:
            rebuilt.append(native_message)
    if not rebuilt:
        return None
    rebuilt.extend(copy.deepcopy(messages))
    return rebuilt


def _prepare_cross_provider_fallback_native_request(
    *,
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fallback_kwargs = _strip_responses_delta_kwargs(kwargs)
    if not kwargs.get("messages_are_delta"):
        return repair_full_transcript(messages), fallback_kwargs

    rebuilt_messages = _rebuild_responses_delta_fallback_messages(messages, kwargs)
    if rebuilt_messages is None:
        raise RuntimeError("Cannot safely route Responses delta fallback without replayable response_items continuity.")
    return repair_full_transcript(rebuilt_messages), fallback_kwargs


class ProviderAgnosticRouter:
    """
    Provider-agnostic router that creates and manages LLM providers
    based on user settings.

    This router:
    1. Reads provider configuration from SettingsManager
    2. Creates the appropriate provider via LLMProviderFactory
    3. Routes requests to the active provider
    4. Falls back to alternative providers if primary fails
    5. Uses timeout protection and circuit breaker for reliability
    """

    # Timeout configuration for LLM operations
    _ROUTE_COMMAND_TIMEOUT = 25.0  # Slightly more than GPT timeout (20s)
    _ASK_TIMEOUT = 25.0
    _AGENT_LLM_TURN_TIMEOUT = TIMEOUT_HOUR

    def __init__(self, config_or_settings=None):
        """
        Initialize the provider-agnostic router.

        Args:
            config_or_settings: Optional config object (for backward compatibility)
        """
        self.config = config_or_settings
        self._provider = None
        self._fallback_provider = None
        self._haiku_provider = None  # Anthropic Haiku fallback for model failures
        self._init_error: str | None = None
        self._init_error_payload: dict[str, Any] | None = None
        self._timeout_mgr = get_timeout_manager()
        self._provider_sync_lock = asyncio.Lock()
        self._runtime_signature: tuple[Any, ...] | None = None

        global _ACTIVE_ROUTER
        _ACTIVE_ROUTER = self
        self._initialize_providers()
        self._runtime_signature = self._build_runtime_signature()

    def _get_frozen_provider_snapshot(self) -> _ProviderSnapshot | None:
        snapshots = _FROZEN_PROVIDER_SNAPSHOTS.get()
        if not snapshots:
            return None
        return snapshots.get(id(self))

    def _store_frozen_provider_snapshot(self) -> _ProviderSnapshot:
        snapshot = _ProviderSnapshot(
            signature=self._runtime_signature,
            provider=self._provider,
            fallback_provider=self._fallback_provider,
            haiku_provider=self._haiku_provider,
        )
        snapshots = _FROZEN_PROVIDER_SNAPSHOTS.get()
        if snapshots is None:
            snapshots = {}
            _FROZEN_PROVIDER_SNAPSHOTS.set(snapshots)
        snapshots[id(self)] = snapshot
        return snapshot

    def _provider_from_frozen_or_current(self) -> Any:
        frozen = self._get_frozen_provider_snapshot()
        if frozen is not None:
            return frozen.provider
        return self._provider

    def _fallback_from_frozen_or_current(self) -> Any:
        frozen = self._get_frozen_provider_snapshot()
        if frozen is not None:
            return frozen.fallback_provider
        return self._fallback_provider

    async def freeze_current_provider_snapshot(self) -> None:
        """Freeze the current provider selection for this async command context."""
        async with self._provider_sync_lock:
            signature = self._build_runtime_signature()
            if signature != self._runtime_signature:
                logger.info("LLM router context changed; rebuilding active providers")
                # reinitialize() does blocking network I/O (the local/Ollama
                # provider probes ``${base}/api/tags`` via a SYNC httpx.get in
                # services/llm/factory.py). Running that on the event-loop thread
                # inside Starlette's BaseHTTPMiddleware corrupts anyio's cancel
                # scope -> "Attempted to exit a cancel scope that isn't the current
                # task's current cancel scope", crashing EVERY /v1/command on the
                # local-LLM path (2026-07-07). Offload the sync rebuild to a worker
                # thread; the asyncio.Lock still serializes it and to_thread copies
                # the current context so the runtime-signature contextvars resolve.
                await asyncio.to_thread(self.reinitialize)
            self._store_frozen_provider_snapshot()

    def _is_cloud_provider(self, provider) -> bool:
        """Check if a provider transmits data to a cloud service."""
        if provider is None:
            return False
        provider_type = provider.get_provider_type() if hasattr(provider, "get_provider_type") else ""
        base_url = getattr(getattr(provider, "config", None), "base_url", None)
        from services.llm.factory import LLMProviderFactory

        return LLMProviderFactory._is_cloud_provider(provider_type, base_url)

    @property
    def effective_model(self) -> str:
        """Forward effective_model from the active wrapped provider.

        agent_loop / agent_executor log `provider.effective_model` regardless of
        whether the provider is the codex SDK path (OpenAIAgentsProvider, which
        defines its own property) or the base path (OpenAICompatibleProvider et
        al, which inherit BaseLLMProvider.effective_model). When the router is
        passed directly as the provider (fallback path in agent_executor when
        _sdk_dispatch_provider returns None), expose the same shape so callers
        do not need defensive getattr or isinstance branching.
        """
        active = getattr(self, "_provider", None)
        if active is None:
            return ""
        return getattr(active, "effective_model", None) or getattr(getattr(active, "config", None), "model", "") or ""

    def _is_agent_request(self, provider=None) -> bool:
        active_prompt = getattr(provider, "_agent_system_prompt", None) if provider is not None else None
        router_prompt = None
        active_provider = getattr(self, "_provider", None)
        if active_provider is not None:
            router_prompt = getattr(active_provider, "_agent_system_prompt", None)
        return bool(active_prompt or router_prompt)

    def _timeout_for_provider(self, provider, default: float) -> float:
        """Keep route/ask fast while allowing deliberate agent turns."""
        if self._is_agent_request(provider):
            return max(default, float(self._AGENT_LLM_TURN_TIMEOUT))
        return default

    def _initialize_providers(self) -> None:
        """Initialize providers based on settings."""
        try:
            settings = get_settings_manager()
        except ImportError:
            settings = None
            logger.warning("Could not import SettingsManager, using defaults")

        # Check if AI is enabled
        ai_enabled = True
        if settings:
            ai_enabled = (
                settings.is_ai_enabled() if hasattr(settings, "is_ai_enabled") else settings.get("ai_enabled", True)
            )

        if not ai_enabled:
            logger.info("AI features disabled in settings")
            self._init_error = "AI features disabled"
            return

        # Try to create primary provider from settings
        try:
            self._provider = create_provider_from_settings()
            if self._provider and self._provider.is_available():
                # Consent gate removed — factory.create_from_settings() is the
                # authoritative consent check.  It returns None when cloud
                # consent is not given, so no duplicate check is needed here.
                # Runtime consent revocation is handled per-request in
                # route_command() and ask() below.
                logger.info(
                    "Primary LLM provider ready: %s (%s)",
                    self._provider.get_provider_name(),
                    self._provider.config.model,
                )
            elif self._provider:
                reason = self._provider.get_unavailable_reason()
                logger.warning("Primary provider not available: %s", reason)
                self._provider = None
        except Exception as exc:
            if getattr(exc, "error_code", None) == "login_required_for_paid_action":
                self._init_error_payload = getattr(exc, "data", None)
                self._init_error = str(exc)
                logger.info("Managed LLM provider blocked until account login: %s", exc)
                self._provider = None
            elif isinstance(exc, (ImportError, ValueError, RuntimeError)):
                error = ProviderUnavailableError("primary", str(exc))
                logger.warning(
                    "Failed to create primary provider: %s (user message: %s)",
                    exc,
                    error.user_friendly_message(),
                )
                self._init_error = f"Primary provider failed: {exc}"
                self._provider = None
            else:
                raise

        # Log status
        if not self._provider and not self._fallback_provider:
            self._init_error = self._init_error or "No LLM providers available"
            logger.warning(
                "No LLM providers available - AI features will be disabled. "
                "Say 'set up AI' to configure, or start Ollama locally."
            )

    @staticmethod
    def _current_user_id() -> str | None:
        """Resolve the principal that owns the active provider selection.

        This id drives both the runtime signature (whether providers rebuild)
        and the user-scoped settings/profile lookups that build them. Reading
        only the bare request contextvar mis-resolves a logged-in desktop user
        as "no user" whenever the router rebuilds outside the request scope
        (fresh task, startup), so the signature never reflects the account and
        the managed provider never activates after sign-in (M-BILL-1). Use the
        desktop-aware resolver, which returns the request principal when bound,
        otherwise the install's logged-in account (falling back to the
        ``device-*`` identity only when no account is signed in) and raises
        ``LookupError`` on cloud-with-no-request — preserved as ``None`` here so
        cloud signatures still rebuild per request via the contextvar path.
        """
        try:
            from core.user_context import get_current_or_desktop_active_user_id

            user_id = get_current_or_desktop_active_user_id()
        except (ImportError, LookupError):
            return None
        return user_id.strip() if isinstance(user_id, str) and user_id.strip() else None

    def _build_runtime_signature(self) -> tuple[Any, ...]:
        """Capture the request-scoped identity that owns provider state."""

        user_id = self._current_user_id()
        ai_source_override = ""
        ai_source = None
        llm_provider = None
        llm_model = None
        llm_base_url = None
        selected_profile_sig: tuple[Any, ...] | None = None

        try:
            settings = get_settings_manager()
        except ImportError:
            settings = None

        try:
            from config.defaults import DEFAULT_AI_SOURCE
        except Exception:
            DEFAULT_AI_SOURCE = "managed"

        try:
            from config.settings import settings as app_settings

            ai_source_override = (getattr(app_settings, "ai_source_override", "") or "").strip()
        except Exception:
            ai_source_override = ""

        if settings is not None:
            kwargs = {"user_id": user_id} if user_id else {}
            try:
                ai_source = settings.get("ai_source", DEFAULT_AI_SOURCE, **kwargs)
                llm_provider = settings.get("llm_provider", "openai", **kwargs)
                llm_model = settings.get("llm_model", "", **kwargs)
                llm_base_url = settings.get("llm_base_url", "", **kwargs)
            except TypeError:
                ai_source = settings.get("ai_source", DEFAULT_AI_SOURCE)
                llm_provider = settings.get("llm_provider", "openai")
                llm_model = settings.get("llm_model", "")
                llm_base_url = settings.get("llm_base_url", "")

        if user_id:
            try:
                from services.connectors.profiles import get_connection_profile_store

                store = get_connection_profile_store()
                selected_profile_id = store.get_selected_profile_id(user_id, "llm")
                if selected_profile_id:
                    profile = store.get_profile(user_id, selected_profile_id)
                    if profile is None:
                        selected_profile_sig = ("missing", selected_profile_id)
                    else:
                        selected_profile_sig = (
                            profile.profile_id,
                            profile.connector_id,
                            bool(profile.enabled),
                            profile.model,
                            profile.base_url,
                            profile.updated_at,
                        )
            except Exception:
                logger.debug("Failed to resolve selected LLM profile signature", exc_info=True)

        return (
            user_id,
            ai_source_override,
            ai_source,
            llm_provider,
            llm_model,
            llm_base_url,
            selected_profile_sig,
        )

    async def _snapshot_request_providers(self) -> tuple[Any, Any, Any]:
        """Return providers aligned to the current request's user context."""

        frozen = self._get_frozen_provider_snapshot()
        if frozen is not None:
            return frozen.provider, frozen.fallback_provider, frozen.haiku_provider

        async with self._provider_sync_lock:
            signature = self._build_runtime_signature()
            if signature != self._runtime_signature:
                logger.info("LLM router context changed; rebuilding active providers")
                # Offload the blocking rebuild off the event loop — see the note
                # in freeze_current_provider_snapshot (sync Ollama I/O in
                # reinitialize corrupts the anyio cancel scope on the request path).
                await asyncio.to_thread(self.reinitialize)
            if _FREEZE_PROVIDER_SELECTION.get():
                frozen = self._store_frozen_provider_snapshot()
                return frozen.provider, frozen.fallback_provider, frozen.haiku_provider
            return self._provider, self._fallback_provider, self._haiku_provider

    def _sync_runtime_provider_if_needed(self) -> None:
        """Synchronize sync capability probes with request-scoped provider state."""

        if self._get_frozen_provider_snapshot() is not None:
            return

        signature = self._build_runtime_signature()
        if signature != self._runtime_signature:
            logger.info("LLM router context changed; rebuilding active providers")
            self.reinitialize()
        if _FREEZE_PROVIDER_SELECTION.get():
            self._store_frozen_provider_snapshot()

    def _try_create_fallback(self, settings) -> None:
        """Try to create a fallback provider."""
        fallback_strategy = settings.get("llm_fallback_strategy", "auto")

        if fallback_strategy in ("cloud_only", "local_only"):
            # No fallback for these strategies
            return

        primary_provider = settings.get("llm_provider", "openai")

        try:
            from services.llm.factory import create_llm_provider
            from services.llm.providers.base import LLMConfig

            # If primary is cloud-based, try Ollama as fallback
            if primary_provider != "ollama":
                fallback_config = LLMConfig(
                    provider="ollama",
                    model="llama2",
                    base_url=OLLAMA_DEFAULT_BASE_URL,
                )
                self._fallback_provider = create_llm_provider(fallback_config)
                if self._fallback_provider.is_available():
                    logger.info("✅ Fallback provider ready: Ollama (local)")
                else:
                    self._fallback_provider = None

            # If primary is Ollama, try OpenAI as fallback (if key available)
            elif primary_provider == "ollama":
                api_key = settings.get("llm_api_key", "")
                if api_key and api_key != "***ENCRYPTED***":
                    from config.defaults import (
                        DEFAULT_AI_SOURCE,
                        resolve_effective_model,
                    )

                    fallback_config = LLMConfig(
                        provider="openai",
                        api_key=api_key,
                        model=resolve_effective_model(
                            # This is an OpenAI API-key fallback, not the user's
                            # active Codex subscription route.
                            ai_source=DEFAULT_AI_SOURCE,
                            provider="openai",
                            agent=False,
                            candidates=(settings.get("llm_model", ""),),
                        ),
                    )
                    self._fallback_provider = create_llm_provider(fallback_config)
                    if self._fallback_provider.is_available():
                        logger.info("✅ Fallback provider ready: OpenAI (cloud)")
                    else:
                        self._fallback_provider = None

        except Exception as e:
            logger.debug("Could not create fallback provider: %s", e)

        # Haiku fallback: create Anthropic provider for model-level failover
        if primary_provider in ("openai", "openai_compatible", "anthropic", "google"):
            try:
                from config.settings import settings as app_settings
                from services.llm.model_fallback import FALLBACK_MODEL

                anthropic_key = app_settings.anthropic_api_key or ""
                if anthropic_key:
                    haiku_config = LLMConfig(
                        provider="anthropic",
                        api_key=anthropic_key,
                        model=FALLBACK_MODEL,
                    )
                    self._haiku_provider = create_llm_provider(haiku_config)
                    if self._haiku_provider and self._haiku_provider.is_available():
                        logger.info("Haiku fallback provider ready (model-level failover)")
                    else:
                        self._haiku_provider = None
            except Exception as haiku_err:
                logger.debug("Could not create Haiku fallback: %s", haiku_err)

    @property
    def SUPPORTS_NATIVE_TOOLS(self) -> bool:
        """Proxy native tool support from the active provider."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        if provider:
            return getattr(provider, "SUPPORTS_NATIVE_TOOLS", False)
        return False

    @property
    def NATIVE_TOOL_FORMAT(self) -> str:
        """Expose the active provider's native tool schema format."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        if provider:
            return str(getattr(provider, "NATIVE_TOOL_FORMAT", "mcp") or "mcp")
        return "mcp"

    @property
    def _native_tools(self) -> list[dict[str, Any]] | None:
        """Proxy native tool schemas from the active provider."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        if provider:
            return getattr(provider, "_native_tools", None)
        return None

    @_native_tools.setter
    def _native_tools(self, value: list[dict[str, Any]] | None) -> None:
        """Forward native tool schemas to the active provider."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        if provider:
            provider._native_tools = value

    @property
    def _agent_system_prompt(self) -> str | None:
        """Proxy agent system prompt from the active provider."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        if provider:
            return getattr(provider, "_agent_system_prompt", None)
        return None

    @_agent_system_prompt.setter
    def _agent_system_prompt(self, value: str | None) -> None:
        """Forward agent system prompt to the active provider."""
        self._sync_runtime_provider_if_needed()
        provider = self._provider_from_frozen_or_current()
        fallback_provider = self._fallback_from_frozen_or_current()
        if provider:
            provider._agent_system_prompt = value
        if fallback_provider:
            fallback_provider._agent_system_prompt = value

    @property
    def _ask_tier_native(self) -> bool:
        """Proxy ASK-tier-native flag from the active provider."""
        provider = self._provider_from_frozen_or_current()
        if provider:
            return getattr(provider, "_ask_tier_native", False)
        return False

    @_ask_tier_native.setter
    def _ask_tier_native(self, value: bool) -> None:
        """Forward ASK-tier-native flag to the active provider."""
        provider = self._provider_from_frozen_or_current()
        fallback_provider = self._fallback_from_frozen_or_current()
        if provider:
            provider._ask_tier_native = value
        if fallback_provider:
            fallback_provider._ask_tier_native = value

    @property
    def context_window(self) -> int | None:
        """Proxy provider-advertised context window when available."""
        provider = self._provider_from_frozen_or_current()
        if provider:
            value = getattr(provider, "context_window", None)
            if isinstance(value, int) and value > 0:
                return value
        return None

    def set_system_prompt(self, prompt: str | None) -> None:
        """Forward agent system prompt to the active provider."""
        provider = self._provider_from_frozen_or_current()
        fallback_provider = self._fallback_from_frozen_or_current()
        if provider:
            provider._agent_system_prompt = prompt
        if fallback_provider:
            fallback_provider._agent_system_prompt = prompt

    async def compact_responses_continuity(
        self,
        *,
        continuity: dict[str, Any] | None,
        model_override: str | None = None,
    ) -> dict[str, Any] | None:
        """Proxy Responses continuity compaction to the active provider."""
        primary_provider, fallback_provider, _haiku_provider = await self._snapshot_request_providers()
        provider = None
        if primary_provider and primary_provider.is_available():
            provider = primary_provider
        elif fallback_provider and fallback_provider.is_available():
            provider = fallback_provider

        if provider is None:
            logger.debug("Responses continuity compaction skipped: no available provider")
            return None

        compact_fn = getattr(provider, "compact_responses_continuity", None)
        if not callable(compact_fn):
            provider_name = (
                provider.get_provider_name() if hasattr(provider, "get_provider_name") else type(provider).__name__
            )
            logger.debug(
                "Responses continuity compaction unsupported by provider %s",
                provider_name,
            )
            return None

        return await compact_fn(
            continuity=continuity,
            model_override=model_override,
        )

    async def _call_clear_partial_state_for_fallback(
        self,
        hook: Any,
        *,
        messages: list[dict[str, Any]],
        executor: Any | None,
    ) -> None:
        if not callable(hook):
            return
        try:
            params = inspect.signature(hook).parameters
            accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
            hook_kwargs: dict[str, Any] = {}
            if accepts_kwargs or "messages" in params:
                hook_kwargs["messages"] = messages
            if accepts_kwargs or "executor" in params:
                hook_kwargs["executor"] = executor
            result = hook(**hook_kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("clear_partial_state_for_fallback hook raised; continuing")

    async def _clear_partial_state_for_fallback(
        self,
        provider: Any,
        *,
        messages: list[dict[str, Any]],
        executor: Any | None,
    ) -> None:
        await self._call_clear_partial_state_for_fallback(
            getattr(provider, "clear_partial_state_for_fallback", None),
            messages=messages,
            executor=executor,
        )
        if executor is not None and executor is not provider:
            await self._call_clear_partial_state_for_fallback(
                getattr(executor, "clear_partial_state_for_fallback", None),
                messages=messages,
                executor=executor,
            )

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int | None = 1024,
        model_override: str | None = None,
        first_turn: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Proxy native tool-calling to the active provider.

        Used by AgentExecutor for multi-turn native tool loops.

        Args:
            messages: Anthropic-format message history.
            max_tokens: Maximum tokens in response.
            model_override: Optional model name override.
            first_turn: Whether this is the first agent loop turn (controls
                tool_choice: required on first turn, auto thereafter).
            **kwargs: Additional kwargs passed through to the provider
                (e.g. tool_choice_override).

        Returns:
            Standardised response dict.
        """
        executor = kwargs.pop("executor", None)
        # Preserve the caller's original message list. ``repair_*`` below returns
        # a fresh list for the provider call, but the executor's
        # ``clear_partial_state_for_fallback`` hook tombstones its argument
        # in place (``messages[:] = cleaned``) so the caller's own persistent
        # state gets the stale tool_use ids removed. Passing the repaired copy
        # would land the tombstone on a throwaway list and leave the executor's
        # state stale for the next turn.
        caller_messages = messages
        primary_provider, _fallback_provider, haiku_provider = await self._snapshot_request_providers()
        self._raise_init_auth_required()
        if kwargs.get("messages_are_delta"):
            messages = repair_delta_messages(
                messages,
                continuity_seen_tool_uses=_extract_continuity_tool_uses(
                    primary_provider,
                    kwargs.get("responses_continuity") or kwargs.get("continuity"),
                    messages=messages,
                ),
            )
        else:
            messages = repair_full_transcript(messages)

        from core.privacy_consent import is_cloud_llm_consented

        cloud_consented = is_cloud_llm_consented()
        if primary_provider and hasattr(primary_provider, "route_command_native"):
            if self._is_cloud_provider(primary_provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud provider %s native route - no consent",
                    primary_provider.get_provider_name(),
                )
            else:
                from services.llm.request_policy import FallbackTriggeredError

                try:
                    return await primary_provider.route_command_native(
                        messages=messages,
                        max_tokens=max_tokens,
                        model_override=model_override,
                        first_turn=first_turn,
                        **kwargs,
                    )
                except FallbackTriggeredError as fallback_exc:
                    logger.warning(
                        "FallbackTriggeredError raised by primary (%s -> %s); clearing partial state",
                        fallback_exc.original_model,
                        fallback_exc.fallback_model,
                    )
                    await self._clear_partial_state_for_fallback(
                        primary_provider,
                        messages=caller_messages,
                        executor=executor,
                    )
                    from services.llm.model_fallback import get_fallback_tracker

                    get_fallback_tracker().force_active(reason="FallbackTriggeredError")
                    if (
                        haiku_provider
                        and hasattr(haiku_provider, "route_command_native")
                        and not (self._is_cloud_provider(haiku_provider) and not cloud_consented)
                    ):
                        logger.info("Honouring FallbackTriggeredError via Haiku fallback")
                        try:
                            fallback_messages, fallback_kwargs = _prepare_cross_provider_fallback_native_request(
                                messages=messages,
                                kwargs=kwargs,
                            )
                        except RuntimeError as rebuild_exc:
                            raise RuntimeError(str(rebuild_exc)) from fallback_exc
                        return await haiku_provider.route_command_native(
                            messages=fallback_messages,
                            max_tokens=max_tokens,
                            model_override=model_override,
                            first_turn=first_turn,
                            **fallback_kwargs,
                        )
                    raise
                except Exception as generic_exc:
                    # Check if Haiku fallback should handle this
                    from services.llm.model_fallback import get_fallback_tracker

                    _tracker = get_fallback_tracker()
                    if (
                        haiku_provider
                        and _tracker.is_fallback_active
                        and hasattr(haiku_provider, "route_command_native")
                    ):
                        if self._is_cloud_provider(haiku_provider) and not cloud_consented:
                            logger.debug("Skipping Haiku native fallback - no consent")
                        else:
                            logger.info("Trying Haiku fallback for native tool calling")
                            try:
                                fallback_messages, fallback_kwargs = _prepare_cross_provider_fallback_native_request(
                                    messages=messages,
                                    kwargs=kwargs,
                                )
                            except RuntimeError as rebuild_exc:
                                raise RuntimeError(str(rebuild_exc)) from generic_exc
                            return await haiku_provider.route_command_native(
                                messages=fallback_messages,
                                max_tokens=max_tokens,
                                model_override=model_override,
                                first_turn=first_turn,
                                **fallback_kwargs,
                            )
                    raise
        if primary_provider is None or (self._is_cloud_provider(primary_provider) and not cloud_consented):
            raise ProviderUnavailableError(
                "all",
                "No LLM provider available. Say 'set up AI' and I'll walk you through it, or I can use Ollama if it's running locally.",
            )
        raise RuntimeError("Active provider does not support native tool calling")

    def is_available(self) -> bool:
        """Check if any LLM provider is available."""
        if self._provider and self._provider.is_available():
            return True
        if self._fallback_provider and self._fallback_provider.is_available():
            return True
        return False

    def get_status(self) -> dict[str, Any]:
        """
        Get detailed status of all providers.

        Returns:
            Dict with provider statuses and availability info
        """
        primary_info = None
        fallback_status = None
        if self._provider:
            get_fallback_status = getattr(self._provider, "get_fallback_status", None)
            if callable(get_fallback_status):
                fallback_status = get_fallback_status()
            primary_info = {
                "provider": self._provider.get_provider_type(),
                "name": self._provider.get_provider_name(),
                "available": self._provider.is_available(),
                "model": self._provider.config.model,
                "reason": self._provider.get_unavailable_reason(),
            }
            if fallback_status is not None:
                primary_info["fallback_status"] = fallback_status

        fallback_info = None
        if self._fallback_provider:
            fallback_info = {
                "provider": self._fallback_provider.get_provider_type(),
                "name": self._fallback_provider.get_provider_name(),
                "available": self._fallback_provider.is_available(),
                "model": self._fallback_provider.config.model,
                "reason": self._fallback_provider.get_unavailable_reason(),
            }

        return {
            "any_available": self.is_available(),
            "init_error": self._init_error,
            "primary": primary_info,
            "fallback": fallback_info,
            "degradation": {
                "enabled": bool(fallback_status),
                "degraded": bool(fallback_status and fallback_status.get("degraded")),
                "fallback_status": fallback_status,
            },
        }

    def _raise_init_auth_required(self) -> None:
        payload = self._init_error_payload
        if not isinstance(payload, dict):
            return
        if payload.get("error_code") != "login_required_for_paid_action":
            return
        message = str(payload.get("message") or self._init_error or "Sign in to use Viola-managed AI.")
        exc = RuntimeError(message)
        exc.error_code = payload.get("error_code")  # type: ignore[attr-defined]
        exc.data = payload  # type: ignore[attr-defined]
        raise exc

    def _provider_policy_context(
        self,
        provider: Any,
        *,
        operation: str,
        timeout_s: float,
        stream: bool = False,
        fallback_allowed: bool = False,
        model_override: str | None = None,
        fallback_model: str | None = None,
        nonstreaming_fallback: bool = False,
        abort_signal: Any | None = None,
    ) -> ProviderRequestContext:
        provider_type = str(getattr(provider, "get_provider_type", lambda: type(provider).__name__)())
        provider_name = str(getattr(provider, "get_provider_name", lambda: provider_type)())
        config = getattr(provider, "config", None)
        model = model_override or str(getattr(config, "model", "") or "")
        source = "agent_loop" if self._is_agent_request(provider) else operation
        return ProviderRequestContext(
            provider=provider_name or provider_type,
            model=model,
            session_id=self._current_user_id(),
            request_id=new_request_id(operation),
            stream=stream,
            timeout_s=timeout_s,
            fallback_allowed=fallback_allowed,
            source=source,
            query_kind="foreground",
            abort_signal=abort_signal,
            fallback_model=fallback_model,
            nonstreaming_fallback=nonstreaming_fallback,
        )

    async def _execute_provider_call(
        self,
        provider: Any,
        *,
        operation: str,
        timeout_s: float,
        call: Any,
        fallback_allowed: bool = False,
        model_override: str | None = None,
        fallback_model: str | None = None,
    ) -> ProviderResponseEnvelope:
        """Run a provider call through the single runtime request policy.

        Applies the shared retry/backoff/cost-and-quota-terminal semantics
        (:func:`services.llm.request_policy.execute_with_policy`) rather than a
        single-shot timeout. Rate-limit/transient failures retry the same
        provider before the caller falls back; cost- and quota-limit failures
        return a terminal envelope so the caller stops instead of spending more.
        """
        context = self._provider_policy_context(
            provider,
            operation=operation,
            timeout_s=timeout_s,
            fallback_allowed=fallback_allowed,
            model_override=model_override,
            fallback_model=fallback_model,
        )
        return await execute_with_policy(call, context)

    @staticmethod
    def _can_continue_after_policy_failure(envelope: ProviderResponseEnvelope) -> bool:
        """Whether the router may try another provider after a policy failure.

        Cost-, quota-, and interruption-terminal outcomes must NOT trigger a
        fallback attempt: falling back after the cost circuit breaker trips
        would spend more on a second provider, defeating the guard.
        """
        return envelope.status not in {"cost_limited", "quota_limited", "interrupted"}

    @staticmethod
    def _raise_policy_failure(envelope: ProviderResponseEnvelope) -> None:
        envelope.raise_for_status()

    async def route_command(
        self,
        text: str,
        history: list[dict[str, Any]] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        system_context: str | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Route a user request to either a tool call, answer, or ignore result.

        Strategy:
        1. Try primary provider with timeout protection
        2. Fallback to secondary if primary fails
        3. Return error if all fail

        Args:
            text: User's request text
            history: Optional conversation history (compat — providers ignore
                this and use ``context_bundle`` frames instead, per
                ``BaseLLMProvider.route_command`` canon).
            context_bundle: Typed prompt-frame bundle for per-turn context.
                Canonical contract — all live providers
                (``OpenAIAgentsProvider``, ``AnthropicProvider``,
                ``GoogleProvider``, ``OllamaNativeProvider``,
                ``OpenAICompatibleProvider``) accept this directly.
            system_context: Legacy string-shaped fallback used by
                ``OpenAICompatibleProvider`` only. Forwarded only when no
                ``context_bundle`` was provided.
            model_override: Optional model name override for tiered routing

        Returns:
            Dict with "type" ("tool_call", "answer", or "ignore") and the
            corresponding tool/args or answer payload.
        """
        primary_provider, fallback_provider, haiku_provider = await self._snapshot_request_providers()
        self._raise_init_auth_required()
        # Build provider kwargs. Canon (per BaseLLMProvider.route_command) is
        # context_bundle: PromptFrameBundle. system_context is legacy str
        # accepted only by OpenAICompatibleProvider; forward only when the
        # caller didn't supply a bundle.
        provider_kwargs: dict[str, Any] = {
            "history": history,
        }
        if context_bundle is not None:
            provider_kwargs["context_bundle"] = context_bundle
        elif system_context is not None:
            provider_kwargs["system_context"] = system_context
        if model_override is not None:
            provider_kwargs["model_override"] = model_override

        # Runtime consent check — NOT a duplicate of factory's init-time gate.
        # This handles consent *revocation* after the provider was already
        # created, without requiring a restart.
        from core.privacy_consent import is_cloud_llm_consented

        cloud_consented = is_cloud_llm_consented()

        # Try primary provider with timeout
        if primary_provider and primary_provider.is_available():
            if self._is_cloud_provider(primary_provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud provider %s — no consent",
                    primary_provider.get_provider_name(),
                )
            else:
                provider_name = primary_provider.get_provider_name()
                route_timeout = self._timeout_for_provider(primary_provider, self._ROUTE_COMMAND_TIMEOUT)
                envelope = await self._execute_provider_call(
                    primary_provider,
                    operation="route_command",
                    timeout_s=route_timeout,
                    fallback_allowed=bool(fallback_provider or haiku_provider),
                    fallback_model=str(
                        getattr(getattr(haiku_provider or fallback_provider, "config", None), "model", "") or ""
                    )
                    or None,
                    model_override=model_override,
                    call=lambda: primary_provider.route_command(
                        text,
                        **provider_kwargs,
                    ),
                )
                if envelope.ok:
                    logger.debug("%s routed command successfully", provider_name)
                    return envelope.value
                if not self._can_continue_after_policy_failure(envelope):
                    self._raise_policy_failure(envelope)
                logger.warning(
                    "%s routing failed category=%s status=%s, trying fallback",
                    provider_name,
                    envelope.error_category,
                    envelope.status,
                )

        # Try Haiku model-level fallback (when primary model fails repeatedly)
        from services.llm.model_fallback import get_fallback_tracker

        _tracker = get_fallback_tracker()
        if haiku_provider and _tracker.is_fallback_active:
            if self._is_cloud_provider(haiku_provider) and not cloud_consented:
                logger.debug("Skipping Haiku fallback — no consent")
            else:
                haiku_timeout = self._timeout_for_provider(haiku_provider, self._ROUTE_COMMAND_TIMEOUT)
                haiku_envelope = await self._execute_provider_call(
                    haiku_provider,
                    operation="route_command_haiku_fallback",
                    timeout_s=haiku_timeout,
                    model_override=model_override,
                    call=lambda: haiku_provider.route_command(
                        text,
                        **provider_kwargs,
                    ),
                )
                if haiku_envelope.ok:
                    logger.info("Haiku fallback routed command successfully")
                    return haiku_envelope.value
                if not self._can_continue_after_policy_failure(haiku_envelope):
                    self._raise_policy_failure(haiku_envelope)
                logger.warning("Haiku fallback also failed category=%s", haiku_envelope.error_category)

        # Try fallback provider with timeout (consent gate applies here too)
        if fallback_provider and fallback_provider.is_available():
            if self._is_cloud_provider(fallback_provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud fallback %s — no consent",
                    fallback_provider.get_provider_name(),
                )
            else:
                fallback_name = fallback_provider.get_provider_name()
                fallback_timeout = self._timeout_for_provider(fallback_provider, self._ROUTE_COMMAND_TIMEOUT)
                fallback_envelope = await self._execute_provider_call(
                    fallback_provider,
                    operation="route_command_fallback",
                    timeout_s=fallback_timeout,
                    model_override=model_override,
                    call=lambda: fallback_provider.route_command(
                        text,
                        **provider_kwargs,
                    ),
                )
                if fallback_envelope.ok:
                    logger.debug("%s routed command (fallback)", fallback_name)
                    return fallback_envelope.value
                if not self._can_continue_after_policy_failure(fallback_envelope):
                    self._raise_policy_failure(fallback_envelope)
                fallback_error: LLMError = ProviderUnavailableError(
                    fallback_name,
                    str(fallback_envelope.raw_error or fallback_envelope.error_category),
                )
                logger.error("Fallback provider routing failed: %s", fallback_envelope.error_category)
                raise fallback_error from fallback_envelope.raw_error

        self._raise_init_auth_required()

        # No providers available
        no_provider_error = ProviderUnavailableError(
            "all",
            "No LLM provider available. Say 'set up AI' and I'll walk you through it, or I can use Ollama if it's running locally.",
        )
        raise no_provider_error

    async def route_command_streaming(
        self,
        text: str,
        history: list[dict[str, Any]] | None = None,
        system_context: str | None = None,
        model_override: str | None = None,
    ):
        """Streaming variant of :meth:`route_command`.

        Delegates to the primary provider's ``route_command_streaming()`` if
        available.  Falls back to the non-streaming ``route_command()`` and
        yields the complete result as a single ``{"done": True, ...}`` dict.

        Yields:
            ``{"token": str}`` for incremental answer text, then a final
            ``{"done": True, ...}`` with the complete parsed result.
        """
        provider, _fallback_provider, _haiku_provider = await self._snapshot_request_providers()
        self._raise_init_auth_required()

        from core.privacy_consent import is_cloud_llm_consented

        cloud_consented = is_cloud_llm_consented()
        if provider and hasattr(provider, "route_command_streaming") and provider.is_available():
            if self._is_cloud_provider(provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud provider %s streaming route - no consent",
                    provider.get_provider_name(),
                )
            else:
                try:
                    provider_kwargs: dict[str, Any] = {
                        "history": history,
                        "system_context": system_context,
                    }
                    if model_override is not None:
                        provider_kwargs["model_override"] = model_override
                    async for event in provider.route_command_streaming(text, **provider_kwargs):
                        yield event
                    return
                except Exception:
                    logger.debug(
                        "Streaming route_command failed on %s, falling back to batch",
                        provider.get_provider_name(),
                        exc_info=True,
                    )

        # Fallback: non-streaming route_command
        result = await self.route_command(
            text,
            history=history,
            system_context=system_context,
            model_override=model_override,
        )
        result["done"] = True
        yield result

    async def ask(
        self,
        question: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> str:
        """
        Ask a question to the LLM.

        Strategy:
        1. Try primary provider with timeout protection
        2. Fallback to secondary if primary fails
        3. Return error if all fail

        Args:
            question: User's question
            history: Optional conversation history
            system_prompt: Optional system prompt override
            include_history: Whether to include conversation history
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature

        Returns:
            Answer string
        """
        primary_provider, fallback_provider, _haiku_provider = await self._snapshot_request_providers()
        self._raise_init_auth_required()
        # Runtime consent check — NOT a duplicate of factory's init-time gate.
        # This handles consent *revocation* after the provider was already
        # created, without requiring a restart.
        from core.privacy_consent import is_cloud_llm_consented

        cloud_consented = is_cloud_llm_consented()

        # Try primary provider with timeout
        if primary_provider and primary_provider.is_available():
            if self._is_cloud_provider(primary_provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud provider %s — no consent",
                    primary_provider.get_provider_name(),
                )
            else:
                provider_name = primary_provider.get_provider_name()
                ask_timeout = self._timeout_for_provider(primary_provider, self._ASK_TIMEOUT)
                try:
                    result = await self._timeout_mgr.run_with_timeout(
                        operation=lambda: primary_provider.ask(
                            question,
                            system_prompt=system_prompt,
                            include_history=include_history,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        ),
                        operation_name=f"llm_ask_{provider_name}",
                        timeout=ask_timeout,
                        use_circuit_breaker=True,
                    )
                    content = result.get("content", "") if isinstance(result, dict) else str(result)
                    logger.debug("%s answered question successfully", provider_name)
                    return content
                except TimeoutError as exc:
                    error = LLMTimeoutError(provider_name, ask_timeout, "ask")
                    logger.warning(
                        "%s ask timed out: %s (user message: %s), trying fallback",
                        provider_name,
                        exc,
                        error.user_friendly_message(),
                    )
                except (ValueError, RuntimeError) as exc:
                    logger.warning("%s ask failed: %s, trying fallback", provider_name, exc)

        # Try fallback provider with timeout (consent gate applies here too)
        if fallback_provider and fallback_provider.is_available():
            if self._is_cloud_provider(fallback_provider) and not cloud_consented:
                logger.debug(
                    "Skipping cloud fallback %s — no consent",
                    fallback_provider.get_provider_name(),
                )
            else:
                fallback_name = fallback_provider.get_provider_name()
                fallback_timeout = self._timeout_for_provider(fallback_provider, self._ASK_TIMEOUT)
                try:
                    result = await self._timeout_mgr.run_with_timeout(
                        operation=lambda: fallback_provider.ask(
                            question,
                            system_prompt=system_prompt,
                            include_history=include_history,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        ),
                        operation_name=f"llm_ask_{fallback_name}_fallback",
                        timeout=fallback_timeout,
                        use_circuit_breaker=True,
                    )
                    content = result.get("content", "") if isinstance(result, dict) else str(result)
                    logger.debug("%s answered question (fallback)", fallback_name)
                    return content
                except TimeoutError as exc:
                    error = LLMTimeoutError(fallback_name, fallback_timeout, "ask")
                    logger.error(
                        "Fallback provider %s ask timed out: %s (user message: %s)",
                        fallback_name,
                        exc,
                        error.user_friendly_message(),
                    )
                    raise error from exc
                except (ValueError, RuntimeError) as exc:
                    fallback_error: LLMError = ProviderUnavailableError(fallback_name, str(exc))
                    logger.error("Fallback provider ask failed: %s", exc)
                    raise fallback_error from exc

        self._raise_init_auth_required()

        # No providers available
        raise ProviderUnavailableError(
            "all",
            "No LLM provider available. Say 'set up AI' and I'll walk you through it, or I can use Ollama if it's running locally.",
        )

    def clear_history(self) -> None:
        """Clear conversation history from all providers."""
        if self._provider:
            self._provider.clear_history()
        if self._fallback_provider:
            self._fallback_provider.clear_history()

    def reinitialize(self) -> None:
        """Rebuild provider instances from current settings."""
        self._provider = None
        self._fallback_provider = None
        self._haiku_provider = None
        self._init_error = None
        self._init_error_payload = None
        self._initialize_providers()
        self._runtime_signature = self._build_runtime_signature()

    def get_history(self) -> list[dict[str, str]]:
        """Get conversation history from primary provider."""
        if self._provider:
            return self._provider.get_history()
        if self._fallback_provider:
            return self._fallback_provider.get_history()
        return []

    def get_provider_name(self) -> str:
        """Get the name of the active provider."""
        if self._provider and self._provider.is_available():
            return self._provider.get_provider_name()
        if self._fallback_provider and self._fallback_provider.is_available():
            return f"{self._fallback_provider.get_provider_name()} (fallback)"
        return "None"


def create_router(config_or_settings=None) -> ProviderAgnosticRouter:
    """
    Create a new provider-agnostic router.

    Args:
        config_or_settings: Optional config object

    Returns:
        ProviderAgnosticRouter instance
    """
    return ProviderAgnosticRouter(config_or_settings)


def get_active_router() -> ProviderAgnosticRouter | None:
    """Return the most recently created provider router, if any."""
    return _ACTIVE_ROUTER


@asynccontextmanager
async def freeze_provider_selection_for_request(
    router: ProviderAgnosticRouter | None = None,
) -> AsyncIterator[None]:
    """Keep a command on the provider selection it observed at request start."""
    existing_snapshots = _FROZEN_PROVIDER_SNAPSHOTS.get()
    snapshots = dict(existing_snapshots) if existing_snapshots else {}
    selection_token = _FREEZE_PROVIDER_SELECTION.set(True)
    snapshots_token = _FROZEN_PROVIDER_SNAPSHOTS.set(snapshots)
    try:
        if router is not None:
            await router.freeze_current_provider_snapshot()
        yield
    finally:
        _FROZEN_PROVIDER_SNAPSHOTS.reset(snapshots_token)
        _FREEZE_PROVIDER_SELECTION.reset(selection_token)
