"""Agent preflight validation.

Catches failures so fundamental that the LLM cannot even be used to
diagnose them.  These are the ONLY hardcoded checks — everything else
goes through the self-diagnosis engine.

Four checks:
1. API key present and non-empty
2. agent_enabled is True
3. max_tokens >= 256 (below this, even a diagnostic call cannot work)
4. Provider is initialized (not None)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PreflightResult:
    """Result of preflight validation."""

    passed: bool
    blocker: str | None = None
    user_message: str | None = None
    developer_detail: str | None = None


class AgentPreflightValidator:
    """Validate that the agent loop has the minimum requirements to run."""

    def validate(
        self,
        settings: object,
        provider: object | None,
        tool_count: int = 0,
    ) -> PreflightResult:
        """Run all preflight checks.

        Args:
            settings: AppConfig (or any object with the expected attrs).
            provider: The LLM provider instance (BaseLLMProvider or None).
            tool_count: Number of tools registered in the MCP hub.

        Returns:
            PreflightResult indicating pass/fail with details.
        """
        # Check 1: agent_enabled
        agent_enabled = getattr(settings, "agent_enabled", False)
        if not agent_enabled:
            return PreflightResult(
                passed=False,
                blocker="agent_disabled",
                user_message="Agent mode is not enabled.",
                developer_detail=(
                    "Set AGENT_ENABLED=true in .env or " "agent_enabled=True in config to enable agent mode."
                ),
            )

        # Check 2: Provider initialized
        if provider is None:
            return PreflightResult(
                passed=False,
                blocker="no_provider",
                user_message="AI provider is not configured.",
                developer_detail=(
                    "No LLM provider instance available. Check llm_backend "
                    "setting and ensure the provider package is installed."
                ),
            )

        # Check 3: API key present
        # Use getattr with default to safely check nested config.
        # ProviderAgnosticRouter wraps the real provider — its outer
        # .config may be AppConfig (no api_key) so traverse into
        # _provider to find the real LLMConfig with api_key.
        config = getattr(provider, "config", None)
        api_key = getattr(config, "api_key", None) if config else None
        # ProviderAgnosticRouter wraps the real provider; its outer .config is the
        # generic AppConfig (no api_key, .provider not the concrete backend).
        # ALWAYS prefer the real inner provider's config for BOTH the api_key and
        # the provider-type check. Previously the inner config was only adopted
        # when it yielded an api_key (`if api_key is not None: config = inner_config`)
        # — but a keyless provider (Ollama) has api_key=None, so `config` stayed on
        # the outer AppConfig, `provider_type` read as non-ollama, the keyless
        # exemption below never fired, and the local (Ollama) agent was wrongly
        # denied "no_api_key" — a key it never needs (2026-07-07: every local-LLM
        # /v1/command failed the agent preflight and fell through the agent loop).
        inner = getattr(provider, "_provider", None)
        inner_config = getattr(inner, "config", None) if inner is not None else None
        if inner_config is not None:
            if api_key is None:
                api_key = getattr(inner_config, "api_key", None)
            config = inner_config
        # Some providers (e.g. Ollama) don't require API keys
        requires_key = True
        provider_type = getattr(config, "provider", "") if config else ""
        if provider_type in ("ollama",):
            requires_key = False
        # A provider may declare it needs no LOCAL key on the class itself
        # (BaseLLMProvider.REQUIRES_LOCAL_API_KEY). The managed cloud-forward
        # provider (CloudManagedProvider) is the case in point: the SERVER
        # holds the key and the desktop only forwards a GoTrue bearer, so its
        # config carries provider="openai" with api_key=None by design.
        # Pre-fix, this check only exempted provider_type "ollama", so the
        # keyless managed lane was wrongly denied "no_api_key" — the agent
        # loop never ran for a signed-in user on a keyless install
        # (2026-07-17, #337 GUI proof: "Creating managed cloud-forward LLM
        # provider (server holds key)" followed two seconds later by
        # "Agent preflight failed: no_api_key"). Same bug class as the
        # 2026-07-07 Ollama miss above — the exemption must come from the
        # real (inner) provider, not from a hardcoded provider-type list.
        key_holder = inner if inner is not None else provider
        if getattr(key_holder, "REQUIRES_LOCAL_API_KEY", True) is False:
            requires_key = False

        if requires_key and (api_key is None or not str(api_key).strip()):
            return PreflightResult(
                passed=False,
                blocker="no_api_key",
                user_message="AI API key is missing.",
                developer_detail=(
                    "The LLM provider requires an API key but none is "
                    "configured. Set the appropriate key in .env "
                    "(e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY)."
                ),
            )

        # Check 4: max_tokens >= 256
        max_tokens = getattr(config, "max_tokens", 300) if config else 300
        llm_cap = getattr(settings, "llm_max_tokens_cap", 150)
        effective_max = max(max_tokens, llm_cap)
        # In agent mode the cap is lifted to 1024+, so this check is
        # mainly for misconfigured settings that set it absurdly low.
        if effective_max < 256:
            return PreflightResult(
                passed=False,
                blocker="max_tokens_too_low",
                user_message="AI token limit is configured too low.",
                developer_detail=(
                    "effective max_tokens=%d (config=%d, cap=%d). "
                    "Must be >= 256 for agent mode to function. "
                    "Increase llm_max_tokens_cap in .env." % (effective_max, max_tokens, llm_cap)
                ),
            )

        return PreflightResult(passed=True)
