"""MCP Server Manifest Validation.

Validates tool definitions when an MCP server is registered.
Checks for:
    - Tool names that shadow built-in tools
    - Descriptions containing prompt injection patterns
    - Tools requesting sensitive permissions
    - Blocklisted tool name patterns (exec, eval, memory_store, etc.)

Returns warnings (not blocks) since the DANGEROUS default in
approval_bridge.py already protects against unapproved tool execution.

Usage:
    >>> from mcp_hub.manifest_validator import validate_server_tools
    >>> warnings = validate_server_tools(tools, server_name="my-server")
    >>> for w in warnings:
    ...     print(w["severity"], w["message"])
"""

from __future__ import annotations

import re
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Blocklist patterns for tool names
# ---------------------------------------------------------------------------

TOOL_NAME_BLOCKLIST: list[str] = [
    "memory_store",
    "file_write",
    "exec",
    "eval",
    "shell",
    "system",
    "subprocess",
    "os_command",
    "run_code",
    "inject",
    "override",
    "escalate",
    "sudo",
    "admin",
]

# Built-in tool names that must not be shadowed by external servers.
# This is a representative set; the full set is populated at runtime
# from the hub's tool registry.
BUILTIN_TOOL_NAMES: set[str] = {
    # core_tools server — compound tools
    "think",
    "file_read",
    "file_write",
    "run_command",
    "web_search",
    "web_read",
    "system_info",
    "computer",
    "self_manage",
    "memory",
    "schedule",
    "delegate_to_provider",
    "spawn_subtask",
    "ask_user",
    "check_pending_tasks",
    "check_api_registry",
    "mcp_servers",
    "api_credential",
    "playlist",
    "get_liked_songs",
    "media",
    "ToolSearch",
    "tool_search",
    "connect_music_provider",
    "check_music_provider_status",
    "pair_speaker_setup",
    "timer",
    "file_bug_report",
    "calendar",
    "payment",
    "phone",
    "smart_home",
    "gmail",
    "notify",
    "telegram_send",
    # browser server
    "browser_navigate",
    "browser_get_text",
    "browser_interact",
    "browser_screenshot",
    "browser_snapshot",
    "browser_scroll",
    "browser_back",
    "browser_forward",
    "browser_refresh",
    "browser_status",
    "browser_close",
    "browser_wait",
    "browser_run_script",
    "browser_fill_form",
    "browser_get_api_log",
    "verify_state",
}

# ---------------------------------------------------------------------------
# Prompt injection detection patterns
# ---------------------------------------------------------------------------

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(previous|all|above)\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
    re.compile(r"override\s+(the\s+)?(system|safety|security)", re.IGNORECASE),
    re.compile(r"disregard\s+(all|the|your)\s+", re.IGNORECASE),
    re.compile(r"forget\s+(all|your|the)\s+(previous|prior)", re.IGNORECASE),
    re.compile(r"act\s+as\s+if\s+you\s+", re.IGNORECASE),
    re.compile(r"pretend\s+(you|to)\s+", re.IGNORECASE),
    re.compile(r"</?system>", re.IGNORECASE),
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
]

# Sensitive permission keywords in tool descriptions
SENSITIVE_PERMISSION_KEYWORDS: list[str] = [
    "arbitrary code",
    "arbitrary command",
    "root access",
    "admin access",
    "full filesystem",
    "unrestricted",
    "bypass security",
    "bypass approval",
    "disable safety",
    "credential",
    "password",
    "private key",
    "secret key",
    "api_key",
]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_server_tools(
    tools: list[dict[str, Any]],
    server_name: str = "",
    known_builtin_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate tool definitions from an MCP server registration.

    Args:
        tools: List of tool definition dicts with "name" and optional
               "description" / "inputSchema" keys.
        server_name: Name of the server being registered (for logging).
        known_builtin_names: Optional set of known built-in tool names.
                             Falls back to BUILTIN_TOOL_NAMES.

    Returns:
        List of warning dicts, each with keys:
            severity: "high", "medium", "low"
            code: Short machine-readable code
            message: Human-readable warning message
            tool_name: The offending tool name
    """
    builtins = known_builtin_names or BUILTIN_TOOL_NAMES
    warnings: list[dict[str, Any]] = []

    for tool in tools:
        tool_name = str(tool.get("name", "")).strip()
        description = str(tool.get("description", "") or "")

        if not tool_name:
            warnings.append(
                {
                    "severity": "medium",
                    "code": "empty_tool_name",
                    "message": "Server '%s' registered a tool with no name" % server_name,
                    "tool_name": "",
                }
            )
            continue

        # Check tool name shadowing
        if tool_name in builtins:
            warnings.append(
                {
                    "severity": "high",
                    "code": "tool_name_shadow",
                    "message": (
                        "Server '%s' tool '%s' shadows a built-in tool. "
                        "The built-in tool will take precedence." % (server_name, tool_name)
                    ),
                    "tool_name": tool_name,
                }
            )

        # Check tool name against blocklist
        tool_name_lower = tool_name.lower()
        for pattern in TOOL_NAME_BLOCKLIST:
            if pattern in tool_name_lower:
                warnings.append(
                    {
                        "severity": "high",
                        "code": "blocklist_tool_name",
                        "message": (
                            "Server '%s' tool '%s' matches blocklist pattern '%s'" % (server_name, tool_name, pattern)
                        ),
                        "tool_name": tool_name,
                    }
                )
                break

        # Check description for prompt injection patterns
        if description:
            for injection_re in INJECTION_PATTERNS:
                match = injection_re.search(description)
                if match:
                    warnings.append(
                        {
                            "severity": "high",
                            "code": "prompt_injection_in_description",
                            "message": (
                                "Server '%s' tool '%s' description contains "
                                "suspected prompt injection: '%s'" % (server_name, tool_name, match.group(0))
                            ),
                            "tool_name": tool_name,
                        }
                    )
                    break  # One warning per tool is sufficient

        # Check for sensitive permission language
        description_lower = description.lower()
        for keyword in SENSITIVE_PERMISSION_KEYWORDS:
            if keyword in description_lower:
                warnings.append(
                    {
                        "severity": "medium",
                        "code": "sensitive_permission_request",
                        "message": (
                            "Server '%s' tool '%s' description mentions "
                            "sensitive permission: '%s'" % (server_name, tool_name, keyword)
                        ),
                        "tool_name": tool_name,
                    }
                )
                break  # One warning per tool

    # Log warnings
    if warnings:
        logger.warning(
            "MCP manifest validation for server '%s': %d warnings",
            server_name,
            len(warnings),
        )
        for w in warnings:
            logger.warning("  [%s] %s: %s", w["severity"].upper(), w["code"], w["message"])
    else:
        logger.debug(
            "MCP manifest validation for server '%s': clean (%d tools)",
            server_name,
            len(tools),
        )

    return warnings
