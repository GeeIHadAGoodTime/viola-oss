"""Compatibility shim for the retired OpenAI Agents SDK provider.

This module is now a compatibility shim around ``openai_compatible.py`` for
older factory/Codex call sites. It does not run the OpenAI Agents SDK.

Important design constraint:
- ``intent.agent_executor.AgentExecutor`` must remain the orchestration owner.

Tool execution remains owned by ``intent.agent_executor.AgentExecutor`` via
the compat Responses path.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from core.logging_config import get_logger
from services.llm.openai_utils import OPENAI_DEFAULT_BASE_URL
from services.llm.providers.base import BaseLLMProvider, LLMConfig, LLMTestResult
from services.llm.providers.openai_compatible import OpenAICompatibleProvider

logger = get_logger(__name__)

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle

AGENTS_SDK_AVAILABLE = False


_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_OPENAI_TOOL_NAME_INVALID = __import__("re").compile(r"[^a-zA-Z0-9_-]")
_OPENAI_TOOL_NAME_MAX_LEN = 64
# Canonical Viola browser tool -> Playwright MCP tool equivalents.
# The model sees the Playwright-native tool names. We normalize them back to
# Viola's local tool contract before the executor dispatches anything.
_PLAYWRIGHT_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "browser_back": ("browser_navigate_back",),
    "browser_interact": ("browser_click", "browser_select_option"),
    "browser_close": ("browser_close",),
    "browser_evaluate": ("browser_evaluate",),
    "browser_fill_form": ("browser_fill_form", "browser_type"),
    "browser_forward": ("browser_navigate_forward",),
    "browser_navigate": ("browser_navigate",),
    "browser_press_key": ("browser_press_key",),
    "browser_screenshot": ("browser_take_screenshot",),
    "browser_snapshot": ("browser_snapshot",),
    "browser_wait": ("browser_wait_for",),
}

_PLAYWRIGHT_SELECTOR_KEYS = ("element", "selector", "locator")
_PLAYWRIGHT_REF_RE = re.compile(r"@?e\d+$", re.IGNORECASE)


def should_use_openai_agents_sdk(config: LLMConfig | None = None) -> bool:
    """Return False: the Agents SDK execution path is retired."""

    raw = os.environ.get("VIOLA_USE_AGENTS_SDK", "").strip().lower()
    if raw in _TRUTHY_VALUES:
        provider = (config.provider if config is not None else "") or ""
        parsed = urlparse((config.base_url if config is not None else "") or OPENAI_DEFAULT_BASE_URL)
        logger.warning(
            "VIOLA_USE_AGENTS_SDK is ignored; OpenAI Agents SDK execution is retired " "(provider=%s, endpoint=%s)",
            provider,
            parsed.netloc or "api.openai.com",
        )
    return False


def _sanitize_tool_name(name: str) -> str:
    """Return a tool name that passes OpenAI's function-tool naming rules."""
    sanitized = _OPENAI_TOOL_NAME_INVALID.sub("_", name)
    return sanitized[:_OPENAI_TOOL_NAME_MAX_LEN]


def _normalize_playwright_tool_call(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate a Playwright MCP tool call into Viola's local tool contract."""
    clean_args = dict(args or {})
    normalized_name = _sanitize_tool_name(name)

    if normalized_name == "browser_click":
        ref = clean_args.get("ref")
        if isinstance(ref, str) and _PLAYWRIGHT_REF_RE.fullmatch(ref.strip()):
            return "browser_interact", {"action": "click_ref", "ref": ref}
        selector = next(
            (
                str(clean_args[key])
                for key in _PLAYWRIGHT_SELECTOR_KEYS
                if isinstance(clean_args.get(key), str) and clean_args.get(key)
            ),
            "",
        )
        text = str(clean_args.get("text", "") or "")
        return "browser_interact", {"action": "click", "selector": selector or text}

    if normalized_name == "browser_type":
        ref = clean_args.get("ref")
        text = clean_args.get("text", clean_args.get("value", ""))
        if isinstance(ref, str) and _PLAYWRIGHT_REF_RE.fullmatch(ref.strip()):
            return "browser_fill_form", {"fields": [{"ref": ref, "value": str(text)}]}
        selector = next(
            (
                str(clean_args[key])
                for key in _PLAYWRIGHT_SELECTOR_KEYS
                if isinstance(clean_args.get(key), str) and clean_args.get(key)
            ),
            "",
        )
        return "browser_fill_form", {"fields": [{"selector": selector, "value": str(text)}]}

    if normalized_name == "browser_fill_form":
        fields = clean_args.get("fields") or []
        normalized_fields: list[dict[str, Any]] = []
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                ref = field.get("ref")
                value = field.get("value", field.get("text", ""))
                if not isinstance(ref, str) or not ref:
                    continue
                normalized_fields.append(
                    {
                        "ref": ref,
                        "value": str(value),
                        "select": bool(field.get("select", False)),
                    }
                )
        return "browser_fill_form", {"fields": normalized_fields}

    if normalized_name == "browser_select_option":
        ref = clean_args.get("ref")
        value = clean_args.get("value")
        if value is None:
            values = clean_args.get("values")
            if isinstance(values, list) and values:
                value = values[0]
        if isinstance(ref, str) and ref:
            return "browser_interact", {"action": "select_ref", "ref": ref, "value": str(value or "")}
        selector = next(
            (
                str(clean_args[key])
                for key in _PLAYWRIGHT_SELECTOR_KEYS
                if isinstance(clean_args.get(key), str) and clean_args.get(key)
            ),
            "",
        )
        return "browser_interact", {"action": "select", "selector": selector, "value": str(value or "")}

    if normalized_name == "browser_take_screenshot":
        return "browser_screenshot", {"full_page": bool(clean_args.get("full_page", False))}

    if normalized_name == "browser_wait_for":
        selector = next(
            (
                str(clean_args[key])
                for key in ("selector", "text", "element")
                if isinstance(clean_args.get(key), str) and clean_args.get(key)
            ),
            "body",
        )
        timeout = clean_args.get("timeout", 5000)
        try:
            timeout_int = int(timeout)
        except (TypeError, ValueError):
            timeout_int = 5000
        return "browser_wait", {"selector": selector, "timeout": timeout_int}

    if normalized_name == "browser_navigate_back":
        return "browser_back", {}

    if normalized_name == "browser_navigate_forward":
        return "browser_forward", {}

    return normalized_name, clean_args


class OpenAIAgentsProvider(BaseLLMProvider):
    """Legacy import shim that delegates native turns to OpenAICompatibleProvider."""

    SUPPORTS_NATIVE_TOOLS = True
    NATIVE_TOOL_FORMAT = "mcp"
    sdk_requires_mcp_preconnect = False

    def __init__(self, config: LLMConfig, openai_client: Any = None):
        super().__init__(config)

        self._compat_provider = OpenAICompatibleProvider(config)
        # Store the pre-authenticated Codex client for use in fallback path
        if openai_client is not None:
            self._compat_provider._client = openai_client
        self._preferred_first_tool: str | None = None
        self._mcp_server_status_provider: Any | None = None

        if not config.api_key:
            self.last_error = "API key is required"
            logger.warning("%s", self.last_error)
            return

        logger.info(
            "OpenAI Agents shim initialized with compat native path: model=%s, base_url=%s",
            config.model,
            config.base_url or "https://api.openai.com/v1",
        )

    def is_available(self) -> bool:
        """Check whether the compat-backed native provider is ready to use."""
        return self._compat_provider.is_available()

    def get_provider_name(self) -> str:
        display_name = getattr(self, "_provider_display_name", "")
        if isinstance(display_name, str) and display_name.strip():
            return display_name.strip()
        if self.config.api_key == "codex-subscription":  # pragma: allowlist secret
            return "Codex"
        return super().get_provider_name()

    def get_available_models(self) -> list[str]:
        """Delegate model listing to the existing OpenAI-compatible provider."""
        return self._compat_provider.get_available_models()

    @property
    def effective_model(self) -> str:
        """Return the model name in use (for step logging)."""
        return self.config.model

    @property
    def mcp_server_status(self) -> dict[str, bool]:
        """Return MCP hub readiness for the compat-backed native path."""
        if callable(self._mcp_server_status_provider):
            try:
                status = self._mcp_server_status_provider()
            except Exception:
                logger.exception("MCP server status provider failed")
            else:
                if isinstance(status, dict):
                    return {str(key): bool(value) for key, value in status.items()}
        return {"core": False, "browser": False, "browser_local": False}

    def set_mcp_server_status_provider(self, provider: Any | None) -> None:
        """Attach the live MCP hub status provider used by runtime diagnostics."""
        self._mcp_server_status_provider = provider

    @property
    def active_native_tool_count(self) -> int:
        """Return tool availability for the actual compat-backed native path."""
        tools = self._compat_provider._native_tools or self._native_tools or []
        return len(tools)

    def get_native_tool_availability(self) -> dict[str, Any]:
        """Return availability for the actual native execution path."""
        tools = self._compat_provider._native_tools or self._native_tools or []
        return {
            "execution_path": "compat_native_tools",
            "has_tools": bool(tools),
            "tool_count": len(tools),
            "mcp_server_status": self.mcp_server_status,
        }

    async def test_connection(self) -> LLMTestResult:
        """Delegate connection testing to the existing OpenAI-compatible provider."""
        return await self._compat_provider.test_connection()

    async def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        include_history: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> dict[str, Any]:
        """Delegate non-agent ask() calls to the existing OpenAI-compatible provider."""
        self._sync_compat_provider_state()
        return await self._compat_provider.ask(
            question=question,
            system_prompt=system_prompt,
            include_history=include_history,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def route_command(
        self,
        text: str,
        history: list[dict] | None = None,
        context_bundle: PromptFrameBundle | None = None,
        max_tokens: int = 300,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Delegate non-native routing to the existing OpenAI-compatible provider."""
        self._warn_ignored_history_arg(history, "route_command")
        self._reject_route_command_agent_state()
        self._sync_compat_provider_state()
        return await self._compat_provider.route_command(
            text=text,
            context_bundle=context_bundle,
            max_tokens=max_tokens,
            model_override=model_override,
        )

    async def route_command_native(
        self,
        messages: list[dict[str, Any]],
        first_turn: bool = False,
        tool_choice_override: Any | None = None,
        native_tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = 1024,
        model_override: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute exactly one native agent turn via the compat Responses API.

        History: An Agents SDK Runner path was drafted here (Agent/Runner/
        MCPServerStdio) but never wired â€” the function returned unconditionally
        before reaching it, so the SDK Runner code was unreachable for the
        entire lifetime of the file.  Removed on 2026-04-16 to stop lying
        about "Phase 2 wired" in MEMORY.md.  If the SDK Runner path is
        needed later, re-implement it against the current Agents SDK API
        rather than resurrecting the stale branch from git history.

        The compat provider path inlines previous items (with IDs stripped)
        rather than referencing them by server-side ID, so multi-turn works
        with or without server-side store.  On Codex, the codex_auth
        transport overrides store=true to store=false transparently.
        """
        return await self._fallback_to_compat_native(
            messages=messages,
            first_turn=first_turn,
            tool_choice_override=tool_choice_override,
            native_tools=native_tools,
            max_tokens=max_tokens,
            model_override=model_override,
            **kwargs,
        )

    async def compact_responses_continuity(
        self,
        *,
        continuity: dict[str, Any] | None,
        model_override: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegate stateless Responses compaction to the compat provider."""
        self._sync_compat_provider_state()
        return await self._compat_provider.compact_responses_continuity(
            continuity=continuity,
            model_override=model_override,
        )

    async def aclose(self) -> None:
        """Best-effort cleanup for the delegated compat provider."""
        close = getattr(self._compat_provider, "aclose", None)
        if callable(close):
            await close()

    async def ensure_servers_ready(self) -> None:
        """Retired SDK hook kept for older executor calls."""
        return None

    async def update_tool_filters(
        self,
        allowed_tools: set[str] | None = None,
        rejected_tools: set[str] | None = None,
    ) -> None:
        """Compatibility hook for active-loop tool filter updates.

        IMPORTANT: This method MUST NOT mutate ``self._native_tools``.
        The native tools list is the single source of truth for the tool
        schemas sent to the LLM via the Responses API.  Destructively
        filtering it caused tool-set divergence where the model could see
        43 tools but the executor's validator only knew about 13, producing
        spurious "Tool does not exist" errors.

        Category/focused allowlists are retired. ``allowed_tools`` and
        ``rejected_tools`` are accepted for older callers but ignored; approval
        denials are enforced by the executor before tool execution.
        """
        return None

    def _sync_compat_provider_state(self) -> None:
        """Keep delegated compat-provider state aligned with this provider."""
        self._compat_provider._agent_system_prompt = self._agent_system_prompt
        self._compat_provider._native_tools = self._native_tools
        self._compat_provider._ask_tier_native = getattr(self, "_ask_tier_native", False)
        self._compat_provider._preferred_first_tool = self._preferred_first_tool
        self._compat_provider._agent_tool_choice = getattr(self, "_agent_tool_choice", "auto")
        # B3: sync settle info so the compat provider can call settle()
        self._compat_provider._settle_user_id = getattr(self, "_settle_user_id", None)
        self._compat_provider._settle_estimated_tokens = getattr(self, "_settle_estimated_tokens", 0)

    async def _fallback_to_compat_native(
        self,
        messages: list[dict[str, Any]],
        first_turn: bool,
        tool_choice_override: Any | None,
        native_tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        model_override: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Use the compat provider when the Agents SDK path is unavailable."""
        self._sync_compat_provider_state()
        return await self._compat_provider.route_command_native(
            messages=messages,
            first_turn=first_turn,
            tool_choice_override=tool_choice_override,
            native_tools=native_tools,
            max_tokens=max_tokens,
            model_override=model_override,
            **kwargs,
        )

    def _get_desired_tool_names(self) -> set[str]:
        """Return the controller-selected canonical tool names."""
        tools = self._native_tools or []
        names: set[str] = set()
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name")
                if isinstance(name, str) and name:
                    names.add(_sanitize_tool_name(name))
        return names

    def _resolve_tool_choice(self, tool_choice_override: Any, first_turn: bool, has_tools: bool) -> str | None:
        """Translate the executor's tool choice override into an OpenAI tool name."""
        if not has_tools:
            return None
        if isinstance(tool_choice_override, str) and tool_choice_override:
            return tool_choice_override
        if isinstance(tool_choice_override, dict):
            function = tool_choice_override.get("function", {})
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    return _sanitize_tool_name(name)
        if first_turn and self._preferred_first_tool and self._preferred_first_tool in self._get_desired_tool_names():
            return _sanitize_tool_name(self._preferred_first_tool)
        return "auto"
