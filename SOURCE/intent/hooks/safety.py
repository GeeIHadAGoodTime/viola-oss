"""Pre-tool-use safety classifier (2C).

Adapted from Claude Code's auto-permissions safety classifier (A8z/BLOCK rules).
Claude Code has BLOCK rules for git destructive, prod deploy, security weakening,
permission grants, TLS weakening. Viola's equivalents are voice-assistant-specific:
sensitive form data, phone calls, external messaging, destructive shell commands.

Registered as a ``pre_tool_use`` lifecycle hook so it fires automatically before
every tool call.  Returns nothing — hooks that need to block a tool call inject
a ``ToolResult(ok=False)`` by raising ``SafetyBlockError``.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from core.logging_config import get_logger
from intent.hooks.lifecycle import register_hook

logger = get_logger(__name__)


class SafetyBlockError(Exception):
    """Raised when a tool call is blocked by the safety classifier."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# ── ALWAYS BLOCK patterns (destructive shell commands) ──────────────────

_BLOCKED_COMMAND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),
    re.compile(r"\bfdisk\b", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\bdel\s+/s\s+/q", re.IGNORECASE),
]

# ── ALWAYS CONFIRM fields (sensitive personal/financial data) ───────────

_SENSITIVE_FIELD_PATTERNS: list[str] = [
    "password",
    "ssn",
    "social security",
    "credit card",
    "card number",
    "cvv",
    "bank account",
    "routing number",
]

# ── Tools that send external communications ─────────────────────────────

# External communication approval is owned by mcp_hub.approval_bridge.
# Duplicating that gate here caused self-contradicting context: the prompt told
# the model to send a confirmed message while this hook blocked the same call
# before the approval bridge could apply SEC-8 risk policy.


def _check_safety(**kwargs: Any) -> None:
    """Pre-tool-use safety check.

    Raises :class:`SafetyBlockError` for destructive commands and
    sensitive data handling that should be confirmed or blocked.
    """
    tool_name: str = kwargs.get("tool_name", "")
    args: dict[str, Any] = kwargs.get("args", {})

    try:
        from services.scheduler.service import current_scheduled_action_denied_tools

        scheduled_denied_tools = current_scheduled_action_denied_tools()
    except ImportError:
        scheduled_denied_tools = frozenset()
    if tool_name in scheduled_denied_tools:
        message = (
            "SAFETY: scheduled actions cannot run unattended tool '%s'. "
            "Ask the user to run this action interactively." % tool_name
        )
        logger.warning("%s", message)
        raise SafetyBlockError(message)

    # ── 1. BLOCK destructive shell commands ──────────────────────────
    if tool_name == "run_command":
        command = str(args.get("command", ""))
        for pattern in _BLOCKED_COMMAND_PATTERNS:
            if pattern.search(command):
                raise SafetyBlockError(
                    "BLOCKED: destructive command detected (%s). "
                    "This command could cause irreversible damage." % pattern.pattern
                )

    # ── 2. CONFIRM sensitive form fields ─────────────────────────────
    if tool_name == "browser_fill_form":
        fields = args.get("fields", [])
        for field in fields:
            field_name = str(field.get("ref", "") or field.get("name", "")).lower()
            field_value = str(field.get("value", "") or field.get("text", "")).lower()
            for sensitive in _SENSITIVE_FIELD_PATTERNS:
                if sensitive in field_name or sensitive in field_value:
                    raise SafetyBlockError(
                        "SAFETY: this form contains sensitive data (%s). "
                        "Use ask_user to confirm with the user before "
                        "submitting sensitive personal or financial information." % sensitive
                    )

    # ── 3. CONFIRM phone calls (high-commitment) ────────────────────
    if tool_name == "make_phone_call":
        number = str(args.get("number", args.get("phone_number", "")))
        raise SafetyBlockError(
            "SAFETY: phone calls require user confirmation. "
            "Use ask_user to confirm before calling %s." % (number[:4] + "****" if len(number) > 4 else "this number")
        )


def register_safety_hook(register: Callable[..., None] = register_hook) -> None:
    """Register the built-in safety hook as an explicit global policy hook."""

    register("pre_tool_use", _check_safety, global_hook=True)


# Auto-register on import for the compatibility registry.
register_safety_hook()
logger.debug("Safety classifier hook registered (pre_tool_use)")
