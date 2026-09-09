"""Delegation tool for routing complex tasks to external compute providers.

When Haiku determines a task would benefit from a more capable model,
it calls delegate_to_provider which routes the request through the
MCP hub to a connected external provider (e.g. OpenAI Codex).

If no provider is available, returns a helpful fallback message.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# Singleton reference set by the MCP server at registration time.
# The core-tools server calls set_mcp_hub() during initialization
# so delegate_to_provider can route calls through the hub.
_mcp_hub: Any = None


def set_mcp_hub(hub: Any) -> None:
    """Wire the MCP hub reference for delegation routing.

    Called once during core-tools server setup so the delegation tool
    can discover and call external provider tools.
    """
    global _mcp_hub
    _mcp_hub = hub


def _get_available_providers() -> list[str]:
    """Return names of connected external providers that support delegation.

    Checks which namespaced provider tools are registered in the hub's
    tool routing table.
    """
    if _mcp_hub is None:
        return []

    from config.providers import EXTERNAL_PROVIDERS

    available: list[str] = []
    tool_routes = getattr(_mcp_hub, "_tool_routes", {})
    for provider_key in EXTERNAL_PROVIDERS:
        # External providers are namespaced: "{provider}.{tool}"
        prefix = "%s." % provider_key
        if any(name.startswith(prefix) for name in tool_routes):
            available.append(provider_key)
    return available


def _resolve_provider(requested: str) -> str | None:
    """Resolve which provider to use.

    Args:
        requested: User-specified provider ("codex", "ollama", "auto").

    Returns:
        Provider key string, or None if no provider is available.
    """
    available = _get_available_providers()
    if not available:
        return None

    if requested == "auto":
        # Prefer codex (most capable), then first available
        if "codex" in available:
            return "codex"
        return available[0]

    if requested in available:
        return requested

    return None


def _find_provider_tool(provider: str) -> str | None:
    """Find the primary tool name for a provider.

    External providers expose tools namespaced as "{provider}.{tool}".
    We look for common delegation entry points.
    """
    if _mcp_hub is None:
        return None

    tool_routes = getattr(_mcp_hub, "_tool_routes", {})

    # Try common tool names that delegation providers expose
    candidates = [
        "%s.codex" % provider,
        "%s.generate" % provider,
        "%s.chat" % provider,
        "%s.complete" % provider,
    ]
    for candidate in candidates:
        if candidate in tool_routes:
            return candidate

    # Fall back to the first tool from this provider
    prefix = "%s." % provider
    for name in tool_routes:
        if name.startswith(prefix):
            return name

    return None


async def delegate_to_provider(
    task: str,
    provider: str = "auto",
    context: str = "",
) -> ToolResult:
    """Delegate a complex task to an external compute provider.

    Use this when a task requires complex code generation, deep analysis,
    or multi-step reasoning that exceeds simple Q&A. Do NOT use this for
    simple questions, tool calls you can make yourself, or tasks where
    speed matters more than depth.

    Args:
        task: Clear description of what needs to be done.
        provider: Which provider to use ("codex", "auto").
            "auto" picks the best available provider.
        context: Additional context (file contents, prior results, etc.)
    """
    if not task or not task.strip():
        return ToolResult(ok=False, error="Empty task description")

    resolved = _resolve_provider(provider)
    if resolved is None:
        return ToolResult(
            ok=False,
            error=(
                "No external compute provider configured. "
                "I'll do my best with the resources I have. "
                "To add a provider, set VIOLA_CODEX_ENABLED=true in your .env."
            ),
        )

    tool_name = _find_provider_tool(resolved)
    if tool_name is None:
        return ToolResult(
            ok=False,
            error="Provider '%s' is configured but has no callable tools" % resolved,
        )

    if _mcp_hub is None:
        return ToolResult(ok=False, error="MCP hub not available")

    # Build the prompt for the external provider
    prompt = task.strip()
    if context:
        prompt = "Context:\n%s\n\nTask:\n%s" % (context.strip(), prompt)

    logger.info(
        "Delegating to provider '%s' via tool '%s' (task: %s)",
        resolved,
        tool_name,
        task[:80],
    )

    try:
        result = await _mcp_hub.call_tool(tool_name, {"prompt": prompt})
    except Exception as exc:
        logger.exception("Delegation to '%s' failed", resolved)
        return ToolResult(ok=False, error="Provider '%s' failed: %s" % (resolved, exc))

    if not result.get("success", False):
        error_msg = result.get("error", "Unknown error from provider")
        return ToolResult(ok=False, error="Provider '%s': %s" % (resolved, error_msg))

    # Extract the provider's response
    data = result.get("data", "")
    display = result.get("display", "")
    response_text = display if isinstance(display, str) and display else str(data)

    return ToolResult(
        ok=True,
        data={
            "provider": resolved,
            "response": response_text,
        },
    )
