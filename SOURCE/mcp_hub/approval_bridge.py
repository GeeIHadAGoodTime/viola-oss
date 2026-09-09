"""Approval bridge: wraps the existing ApprovalManager for MCP tool calls.

Maps MCP tool names to advisory risk levels, asks the shared permission
policy engine for allow/ask/deny behavior, then delegates ask prompts to
the existing ApprovalManager from intent/approval.py.

Risk derivation priority (2026-04-08):
  1. Manual RISK_MAP overrides (for cases where annotations are wrong)
  2. Annotation-based derivation (readOnlyHint / destructiveHint)
  3. Default to CONFIRM (safe middle ground for unknown tools)
"""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, cast

from core.logging_config import get_logger
from intent.irreversible_actions import is_irreversible_tool_call
from intent.permissions.policy import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionHookProvenance,
    PermissionMode,
    PermissionPolicy,
    PermissionRule,
)
from intent.permissions.remote_relay import (
    relay_consent_blocks,
    relay_consent_denial_reason,
)
from intent.tool_types import RiskLevel

# F-AGENT-03 (2026-04-17): path-like argument names whose values should be
# resolved to absolute paths before being rendered in an approval dialog.
# A relative path in the dialog could let an agent trick a user into
# approving a delete that resolves outside the directory the user expected.
_PATH_LIKE_ARG_NAMES: frozenset[str] = frozenset(
    {
        "path",
        "file",
        "file_path",
        "filepath",
        "filename",
        "source",
        "src",
        "destination",
        "dest",
        "target",
        "directory",
        "dir",
        "cwd",
        "working_directory",
    }
)

_PLAYLIST_SAFE_ACTIONS: frozenset[str] = frozenset({"list"})
_PLAYLIST_CONFIRM_ACTIONS: frozenset[str] = frozenset({"play", "play_favorites"})
_GMAIL_SAFE_ACTIONS: frozenset[str] = frozenset({"inbox", "read", "search", "daily_summary", "list_labels"})
_GMAIL_CONFIRM_ACTIONS: frozenset[str] = frozenset({"draft", "draft_reply", "download_attachment", "create_label"})
_GMAIL_DANGEROUS_ACTIONS: frozenset[str] = frozenset({"send", "send_draft", "modify", "batch_modify", "modify_thread"})
_WORKBENCH_SAFE_ACTIONS: frozenset[str] = frozenset({"", "list", "open_folder", "path_for", "read", "search"})
_WORKBENCH_CONFIRM_ACTIONS: frozenset[str] = frozenset({"remember"})
_WORKBENCH_DANGEROUS_ACTIONS: frozenset[str] = frozenset({"delete", "forget", "remove"})
_CALENDAR_SAFE_ACTIONS: frozenset[str] = frozenset({"", "list", "list_calendars", "get", "get_event", "find_free_time"})
_CALENDAR_DANGEROUS_ACTIONS: frozenset[str] = frozenset(
    {
        "add",
        "create",
        "create_event",
        "update",
        "update_event",
        "delete",
        "delete_event",
        "respond",
        "respond_to_event",
    }
)
_GMAIL_GRANULAR_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "gmail_search",
        "gmail_get",
        "gmail_listLabels",
    }
)
_GMAIL_GRANULAR_CONFIRM_TOOLS: frozenset[str] = frozenset(
    {
        "gmail_createDraft",
        "gmail_downloadAttachment",
        "gmail_createLabel",
    }
)
_GMAIL_GRANULAR_DANGEROUS_TOOLS: frozenset[str] = frozenset(
    {
        "gmail_send",
        "gmail_sendDraft",
        "gmail_modify",
        "gmail_batchModify",
        "gmail_modifyThread",
    }
)
_COMPUTER_CONFIRM_ACTIONS: frozenset[str] = frozenset(
    {
        "screenshot",
        "observe_region",
        "analyze_screen",
        "list_windows",
        "read_window",
        "wait",
        "focus_window",
        "type",
        "key",
        "click",
        "double_click",
        "right_click",
        "scroll",
        # Pre-existing gap (fix 2026-05-02 trace 08ffbafdc09d step 28):
        # mouse_move was never in the CONFIRM allow-list, so it fell
        # through to the default DANGEROUS classification for the
        # `computer` tool. The agent's drives that try to position
        # before a ref-less click hit APPROVAL_BLOCKED and abandoned the
        # path entirely. mouse_move is strictly
        # less dangerous than click (which is already CONFIRM): it
        # only positions the cursor, no button event. Drag is left
        # DANGEROUS by intent — drag-to-trash / drag-to-move can be
        # destructive in ways an instant click typically isn't.
        "mouse_move",
        # Ref-based architecture (2026-05-01): inspect/click_ref are no
        # more dangerous than read_window/click; background_* are observation
        # or focus-free input equivalents. Without these in the allow-list,
        # the approval gate emits APPROVAL_BLOCKED and the agent abandons
        # the desktop path entirely (trace 27958a329b28).
        "inspect_window",
        "click_ref",
        "background_type",
        "background_screenshot",
        # Low-level input primitives (2026-05-02). Relative movement and
        # keyboard key down/up/hold are guarded by key-policy checks in the
        # computer-use server. Mouse down/hold remain DANGEROUS because they
        # can synthesize a drag through separate movement calls.
        "mouse_move_relative",
        "mouse_button_up",
        "key_down",
        "key_up",
        "key_hold",
        "mouse_move_angle",
    }
)
_COMPUTER_VOLUME_CONFIRM_ACTIONS: frozenset[str] = frozenset(
    {
        "get",
        "read",
        "status",
        "set",
        "mute",
        "unmute",
        "up",
        "volume_up",
        "increase",
        "louder",
        "down",
        "volume_down",
        "decrease",
        "quieter",
    }
)


def _computer_use_whitelist_allows(args: Mapping[str, Any] | None) -> bool:
    """Return True when the local user's per-app whitelist covers this target."""
    if args is None:
        return False
    try:
        from core.user_context import get_current_or_device_user_id
        from services.computer_use.safety import is_app_whitelisted

        target = _computer_use_target_app(args)
        allowed = is_app_whitelisted(target, user_id=get_current_or_device_user_id())
        if allowed:
            logger.info("Auto-approving computer use for whitelisted app '%s'", target)
        return allowed
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Could not evaluate computer-use app whitelist")
        return False


def _computer_use_target_app(args: Mapping[str, Any] | None) -> str:
    if args is None:
        return ""
    try:
        from services.computer_use.safety import normalize_executable_name
        from services.computer_use.window_manager import (
            get_foreground_app_executable_name,
        )

        target = normalize_executable_name(
            str(
                args.get("app_executable")
                or args.get("app_name")
                or args.get("title")
                or args.get("window_title")
                or ""
            )
        )
        if not target:
            target = normalize_executable_name(get_foreground_app_executable_name())
        return target
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Could not resolve computer-use target app")
        return ""


def _computer_use_action_requires_app_consent(args: Mapping[str, Any] | None) -> bool:
    if args is None:
        return False
    try:
        from services.computer_use.safety import is_mutating_action

        return is_mutating_action(str(args.get("action") or ""))
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        return False


def _computer_use_session_grant_allows(
    args: Mapping[str, Any] | None,
    *,
    user_id: str,
    session_id: str,
) -> bool:
    if args is None:
        return False
    try:
        from services.computer_use.safety import has_session_app_grant

        return has_session_app_grant(_computer_use_target_app(args), user_id=user_id, session_id=session_id)
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        return False


def _grant_computer_use_session_app(
    args: Mapping[str, Any] | None,
    *,
    user_id: str,
    session_id: str,
) -> None:
    if not _computer_use_action_requires_app_consent(args):
        return
    try:
        from services.computer_use.safety import grant_session_app

        target = _computer_use_target_app(args)
        if grant_session_app(target, user_id=user_id, session_id=session_id):
            logger.info("Granted computer-use session app consent for '%s'", target)
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.debug("Could not grant computer-use session app consent")


def _normalize_workspace_tool_name(tool_name: str) -> str:
    return str(tool_name or "").strip().replace(".", "_")


def _active_agent_task_id() -> str | None:
    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor()
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        return None
    task_id = str(getattr(executor, "task_id", "") or "").strip()
    return task_id or None


def _is_namespaced_external_tool_name(tool_name: str) -> bool:
    """Return True for external MCP tool names that carry a server namespace."""
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return len(parts) == 3 and bool(parts[1]) and bool(parts[2])
    if "__" in name:
        server_name, external_tool_name = name.split("__", 1)
        return bool(server_name and external_tool_name)
    if "." in name:
        server_name, external_tool_name = name.split(".", 1)
        return bool(server_name and external_tool_name)
    return False


def _resolve_path_like_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``args`` with path-like string values resolved to
    absolute paths.

    Any value whose key matches a known path-like argument name (see
    ``_PATH_LIKE_ARG_NAMES``) and which is a string is resolved via
    ``os.path.abspath(os.path.expanduser(...))``. Non-string values,
    non-path keys, and paths that cannot be resolved are passed through
    unchanged.

    This is used by :class:`ApprovalBridge` when rendering action
    descriptions so that users see the absolute filesystem path an agent
    is asking permission for, not a relative or ambiguous path that could
    resolve somewhere unexpected. (Audit finding F-AGENT-03, 2026-04-17.)
    """
    if not args:
        return dict(args)
    out: dict[str, Any] = dict(args)
    for key, value in args.items():
        if key not in _PATH_LIKE_ARG_NAMES:
            continue
        if not isinstance(value, str) or not value:
            continue
        try:
            resolved = os.path.abspath(os.path.expanduser(value))
        except (OSError, ValueError):
            # If resolution fails for any reason, leave the original.
            continue
        out[key] = resolved
    return out


if TYPE_CHECKING:
    from intent.approval import ApprovalManager

logger = get_logger(__name__)

# Module-level flag: set to True after the first RISK_MAP validation pass.
_risk_map_validated: bool = False

# Module-level cache of tool annotations populated by the hub during
# _discover_tools().  Keys are tool names, values are dicts with
# "readOnlyHint" and "destructiveHint" (both bool | None).
_tool_annotations: dict[str, dict[str, Any]] = {}

# INT-13: Tools we've already warned about for defaulting to CONFIRM.
# Prevents log spam — one WARN per unique unknown tool per process.
_warned_unknown: set[str] = set()


def set_tool_annotations(annotations: dict[str, dict[str, Any]]) -> None:
    """Populate the annotation cache from the hub's discovered tool schemas.

    Called once after MCP hub initialization so the approval bridge can
    derive risk levels from MCP tool annotations.
    """
    _tool_annotations.clear()
    _tool_annotations.update(annotations)
    logger.debug("Loaded annotations for %d tools", len(annotations))


def _derive_risk_from_annotations(tool_name: str) -> RiskLevel | None:
    """Derive a risk level from MCP tool annotations.

    Returns:
        RiskLevel if annotations are available and deterministic,
        None if the tool has no annotations (caller should use default).
    """
    if _is_namespaced_external_tool_name(tool_name):
        # External stdio servers control their own MCP metadata. Treat those
        # annotations as descriptive only; permission risk must come from a
        # code-owned RISK_MAP entry or the DANGEROUS external default.
        return None

    ann = _tool_annotations.get(tool_name)
    if ann is None:
        return None

    read_only = ann.get("readOnlyHint")
    destructive = ann.get("destructiveHint")

    # readOnlyHint=True AND destructiveHint=False → SAFE
    if read_only is True and destructive is False:
        return RiskLevel.SAFE

    # destructiveHint=True → DANGEROUS
    if destructive is True:
        return RiskLevel.DANGEROUS

    # All other combinations (readOnly=False, destructive=False, or Nones) → CONFIRM
    return RiskLevel.CONFIRM


def _derive_call_risk_from_args(tool_name: str, args: Mapping[str, Any] | None) -> RiskLevel | None:
    """Return a per-call risk override when a compound tool mixes safe and dangerous actions."""
    normalized_tool_name = _normalize_workspace_tool_name(tool_name)
    if normalized_tool_name in _GMAIL_GRANULAR_SAFE_TOOLS:
        return RiskLevel.SAFE
    if normalized_tool_name in _GMAIL_GRANULAR_CONFIRM_TOOLS:
        return RiskLevel.CONFIRM
    if normalized_tool_name in _GMAIL_GRANULAR_DANGEROUS_TOOLS:
        return RiskLevel.DANGEROUS

    if is_irreversible_tool_call(tool_name, args, None):
        return RiskLevel.DANGEROUS

    if tool_name.startswith("api_"):
        method = str((args or {}).get("method") or "GET").strip().upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return RiskLevel.CONFIRM
        return RiskLevel.DANGEROUS

    if tool_name == "playlist":
        if not args:
            return RiskLevel.SAFE

        action = str(args.get("action") or "").strip().lower()
        if not action or action in _PLAYLIST_SAFE_ACTIONS:
            return RiskLevel.SAFE
        if action in _PLAYLIST_CONFIRM_ACTIONS:
            return RiskLevel.CONFIRM

        return RiskLevel.DANGEROUS

    if tool_name == "gmail":
        if not args:
            return RiskLevel.SAFE

        action = str(args.get("action") or "").strip().lower()
        if not action or action in _GMAIL_SAFE_ACTIONS:
            return RiskLevel.SAFE
        if action in _GMAIL_CONFIRM_ACTIONS:
            return RiskLevel.CONFIRM
        if action in _GMAIL_DANGEROUS_ACTIONS:
            return RiskLevel.DANGEROUS

        return RiskLevel.CONFIRM

    if tool_name in {"calendar", "google_calendar"}:
        action = str((args or {}).get("action") or "").strip().lower()
        if action in _CALENDAR_SAFE_ACTIONS:
            return RiskLevel.SAFE
        if action in _CALENDAR_DANGEROUS_ACTIONS:
            return RiskLevel.DANGEROUS
        return RiskLevel.CONFIRM

    if tool_name == "workbench":
        action = str((args or {}).get("action") or "search").strip().lower()
        if action in _WORKBENCH_SAFE_ACTIONS:
            return RiskLevel.SAFE
        if action in _WORKBENCH_CONFIRM_ACTIONS:
            return RiskLevel.CONFIRM
        if action in _WORKBENCH_DANGEROUS_ACTIONS:
            return RiskLevel.DANGEROUS
        return RiskLevel.CONFIRM

    if tool_name == "computer":
        if not args:
            return None

        action = str(args.get("action") or "").strip().lower()
        if _computer_use_action_requires_app_consent(args):
            if _computer_use_whitelist_allows(args):
                return RiskLevel.CONFIRM
            return RiskLevel.DANGEROUS
        if action == "volume":
            volume_action = str(args.get("volume_action") or args.get("action_name") or "get").strip().lower()
            if volume_action in _COMPUTER_VOLUME_CONFIRM_ACTIONS:
                return RiskLevel.CONFIRM
            return None
        if action in _COMPUTER_CONFIRM_ACTIONS:
            return RiskLevel.CONFIRM
        return None

    return None


def validate_risk_map(
    registered_tool_names: set[str] | frozenset[str],
) -> None:
    """Validate RISK_MAP against registered tools and annotations.

    1. Logs a WARNING for any tool NOT in RISK_MAP and without annotations
       (built-in unknown tools default to CONFIRM; namespaced external tools
       default to DANGEROUS because their side effects are not knowable).
    2. Logs a WARNING for manual RISK_MAP entries that match what
       annotations would derive (indicating the manual entry is redundant).

    Called once after MCP hub initialization.  Subsequent calls are no-ops.
    """
    global _risk_map_validated
    if _risk_map_validated:
        return
    _risk_map_validated = True

    if not registered_tool_names:
        logger.debug("validate_risk_map: no registered tools to check")
        return

    # Check for tools with neither manual entry nor annotations
    uncovered = []
    for tool_name in sorted(registered_tool_names):
        if tool_name in RISK_MAP:
            continue
        if _is_namespaced_external_tool_name(tool_name):
            continue
        derived = _derive_risk_from_annotations(tool_name)
        if derived is None:
            uncovered.append(tool_name)

    if uncovered:
        for tool_name in uncovered:
            default_risk = "DANGEROUS" if _is_namespaced_external_tool_name(tool_name) else "CONFIRM"
            logger.warning(
                "Tool '%s' registered but has no RISK_MAP entry and no annotations — defaulting to %s",
                tool_name,
                default_risk,
            )
        logger.warning(
            "Risk coverage gap: %d of %d registered tools have no explicit risk level or annotations",
            len(uncovered),
            len(registered_tool_names),
        )
    else:
        logger.info(
            "Risk map validated: all %d registered tools have explicit risk levels or annotation-derived risk",
            len(registered_tool_names),
        )

    # Check for redundant manual entries (match what annotations derive)
    redundant = []
    for tool_name, manual_risk in RISK_MAP.items():
        derived = _derive_risk_from_annotations(tool_name)
        if derived is not None and derived == manual_risk:
            redundant.append(tool_name)

    if redundant:
        logger.info(
            "RISK_MAP has %d redundant entries (match annotation-derived "
            "risk): %s — these can be removed to reduce maintenance burden",
            len(redundant),
            sorted(redundant),
        )


# ---------------------------------------------------------------------------
# Manual RISK_MAP overrides — entries where annotation-derived risk is wrong.
#
# After the annotation-based derivation (2026-04-08), only entries that
# DISAGREE with what annotations would derive need to be here.  Tools whose
# annotations correctly express their risk are auto-derived.
#
# Annotation derivation rules:
#   readOnlyHint=True  + destructiveHint=False → SAFE
#   destructiveHint=True                       → DANGEROUS
#   otherwise                                  → CONFIRM
# ---------------------------------------------------------------------------
# INT-06: RISK_MAP is exposed as a read-only MappingProxyType to preserve
# the safety invariant that no code path can mutate entries at runtime.
# The underlying dict (_RISK_MAP_RAW) is kept private; all external code
# must go through the proxy below.
_RISK_MAP_RAW: dict[str, RiskLevel] = {
    # --- Overrides: explicit dangerous operations ---
    # Shell commands must not be batch- or agent-auto-approved.
    "run_command": RiskLevel.DANGEROUS,
    # --- Overrides: annotation says DANGEROUS but we want CONFIRM ---
    # file_write: base metadata stays CONFIRM for compatibility; write/delete
    # calls are upgraded to DANGEROUS by the irreversible classifier.
    "file_write": RiskLevel.CONFIRM,
    # self_manage: annotations say destructive (→ DANGEROUS), which is correct
    # but we want to be explicit about this critical override.
    "self_manage": RiskLevel.DANGEROUS,
    # --- Overrides: annotation says CONFIRM but we want SAFE ---
    # memory: annotations say readOnly=False, destructive=False (→ CONFIRM),
    # but memory ops are low-risk and high-frequency. SAFE prevents friction.
    "memory": RiskLevel.SAFE,
    "media": RiskLevel.SAFE,
    "playback": RiskLevel.SAFE,
    "open_app_panel": RiskLevel.SAFE,
    "pair_speaker_setup": RiskLevel.SAFE,
    "rate_track": RiskLevel.SAFE,
    # delegate_to_provider: routes to external compute, no local side effects.
    "delegate_to_provider": RiskLevel.SAFE,
    # Built-in Codex MCP delegation is external compute with no local side effects.
    # Shell-shaped prompt payloads are rejected first in client_hub.
    "codex__codex": RiskLevel.SAFE,
    "codex.codex": RiskLevel.SAFE,
    # setup_sms_provider: read-only Telnyx SMS setup inspection, no provider mutation.
    "setup_sms_provider": RiskLevel.SAFE,
    # ask_user: asks user a question — zero side effects.
    "ask_user": RiskLevel.SAFE,
    # browser safe reads/navigation should stay frictionless even before MCP
    # annotations are loaded into the process.
    "browser_navigate": RiskLevel.SAFE,
    "browser_get_text": RiskLevel.SAFE,
    "browser_get_links": RiskLevel.SAFE,
    "browser_get_page_info": RiskLevel.SAFE,
    "browser_get_form_fields": RiskLevel.SAFE,
    "browser_snapshot": RiskLevel.SAFE,
    "browser_screenshot": RiskLevel.SAFE,
    "browser_scroll": RiskLevel.SAFE,
    "browser_back": RiskLevel.SAFE,
    "browser_forward": RiskLevel.SAFE,
    "browser_refresh": RiskLevel.SAFE,
    "browser_status": RiskLevel.SAFE,
    "browser_close": RiskLevel.SAFE,
    "browser_wait": RiskLevel.SAFE,
    "browser_get_api_log": RiskLevel.SAFE,
    # browser mutations / scripted actions need confirm-level approval.
    "browser_click": RiskLevel.CONFIRM,
    "browser_type": RiskLevel.CONFIRM,
    "browser_select": RiskLevel.CONFIRM,
    "browser_press_key": RiskLevel.CONFIRM,
    "browser_interact": RiskLevel.CONFIRM,
    "browser_fill_form": RiskLevel.CONFIRM,
    "browser_evaluate": RiskLevel.CONFIRM,
    "browser_run_script": RiskLevel.CONFIRM,
    # Pending gate controls are invoked only after the current user reply
    # confirms/declines the gate.  That reply is the approval; a second
    # ApprovalManager prompt contradicts the pending-gate system prompt.
    "resume_signature_gate": RiskLevel.CONFIRM,
    "cancel_signature_gate": RiskLevel.CONFIRM,
    "resume_payment_gate": RiskLevel.CONFIRM,
    "cancel_payment_gate": RiskLevel.CONFIRM,
    # --- Overrides: user-confirmed messaging/notification tools -> CONFIRM ---
    # SEC-8: CONFIRM is the logging/audit tier and auto-approves in agent mode.
    # The prompt requires the model to ask for missing destination/content, and a
    # direct user request with destination is approval to send.
    "send_notification": RiskLevel.CONFIRM,
    "notify": RiskLevel.CONFIRM,
    "telegram_send": RiskLevel.CONFIRM,
    "sms_send": RiskLevel.CONFIRM,
    # --- Overrides: communication/execution tools → DANGEROUS ---
    # These tools can send to arbitrary external parties or register code.
    # They MUST require explicit user confirmation — NEVER auto-approved.
    # gmail_send: sends email (legacy direct tool name).
    "gmail_send": RiskLevel.DANGEROUS,
    "gmail_sendDraft": RiskLevel.DANGEROUS,
    "gmail_modify": RiskLevel.DANGEROUS,
    "gmail_batchModify": RiskLevel.DANGEROUS,
    "gmail_modifyThread": RiskLevel.DANGEROUS,
    "gmail_createDraft": RiskLevel.CONFIRM,
    "gmail_downloadAttachment": RiskLevel.CONFIRM,
    "gmail_createLabel": RiskLevel.CONFIRM,
    # gmail: compound Google Workspace tool — send action dispatches email.
    "gmail": RiskLevel.DANGEROUS,
    # mcp_servers: can register arbitrary MCP servers (RCE vector).
    "mcp_servers": RiskLevel.DANGEROUS,
    # fill_payment_details: CONFIRM-tier audit surface. PAYMENT_GATE and the
    # hosted confirmation resume path are the stronger payment safety gates.
    "fill_payment_details": RiskLevel.CONFIRM,
    # computer: arbitrary desktop GUI control across third-party apps remains
    # DANGEROUS by default. _derive_call_risk_from_args promotes known routine
    # observe/focus/type/click actions to CONFIRM so the tool's own desktop
    # safety checks can run during agent execution.
    "computer": RiskLevel.DANGEROUS,
    # --- Overrides: no annotations available (external/special tools) ---
    # phone calling — CONFIRM: the agent already asks the user in conversation
    # before reaching the tool call.
    "phone": RiskLevel.CONFIRM,
    # Phone payment transmit is protected by PAYMENT_GATE + hosted confirmation;
    # card digits bypass LLM context and the tool result exposes last4 only.
    "transmit_payment_to_call": RiskLevel.CONFIRM,
}

# INT-06: Read-only proxy — supports .get, .items, "x in RISK_MAP", etc.
# Any attempt to assign or delete keys via RISK_MAP raises TypeError.
RISK_MAP: Mapping[str, RiskLevel] = MappingProxyType(_RISK_MAP_RAW)

# ---------------------------------------------------------------------------
# CONFIRM auto-approval rationale (SEC-8 decision, 2026-04-10)
#
# Three-tier risk model:
#   SAFE      → always auto-approved (read-only, no side effects)
#   CONFIRM   → auto-approved during agent execution (see rationale below)
#   DANGEROUS → NEVER auto-approved; requires explicit user confirmation
#               via ApprovalManager.request_approval() ("yes I'm sure")
#
# WHY CONFIRM tools are auto-approved:
#   1. Voice UX.  Viola is voice-first.  Asking "should I search the web?"
#      or "should I navigate to Amazon?" on every tool call makes the
#      assistant unusable.  The March 23 collapse (326 blocked tool calls)
#      proved this — defaulting unknown tools to DANGEROUS paralyzed the
#      entire agent.
#   2. Defense-in-depth elsewhere.  The truly dangerous actions (payments,
#      self-management, phone calls) are protected by separate, stronger
#      mechanisms:
#        - PAYMENT_GATE (intent/agent_executor.py): deterministic code-level
#          gate that intercepts checkout pages, blocks card-field access,
#          and forces a user confirmation link before any payment proceeds.
#          This is NOT prompt-dependent — it cannot be bypassed by prompt
#          injection.  See PAY-GATE-1 through PAY-GATE-7.
#        - DANGEROUS risk level: tools annotated destructiveHint=True
#          (self_manage, playlist, make_phone_call, end_phone_call) go
#          through full interactive confirmation requiring "yes I'm sure".
#   3. Agent tool selection sees all visible tools. Safety boundaries are
#      tier gating, runtime-hidden checks, deterministic gates, and DANGEROUS
#      approval, not task-category filtering.
#      CONFIRM is a logging/audit tier, not a user-facing approval tier.
#
# If a tool MUST require user confirmation, classify it as DANGEROUS
# (via annotations or RISK_MAP override), not CONFIRM.
# ---------------------------------------------------------------------------
AGENT_AUTO_APPROVE: frozenset[str] = frozenset(name for name, risk in RISK_MAP.items() if risk == RiskLevel.CONFIRM)

# Deprecated alias — name is misleading (covers ALL CONFIRM tools, not just browser).
# Use AGENT_AUTO_APPROVE directly. Kept for backward compat with tests.
BROWSER_AGENT_AUTO_APPROVE = AGENT_AUTO_APPROVE


# INT-07: Track recompute invocations so startup ordering assertions
# elsewhere (mcp_hub/client_hub.py) can prove this ran before the first
# agent task.  Idempotent: calling twice with the same annotations yields
# the same frozenset.
_recompute_count: int = 0


def recompute_auto_approve() -> None:
    """Recompute AGENT_AUTO_APPROVE to include annotation-derived CONFIRM tools.

    Called after set_tool_annotations() so that tools with CONFIRM annotations
    but no RISK_MAP entry are still auto-approved during agent execution.
    Idempotent — safe to call after every annotation load.
    """
    global AGENT_AUTO_APPROVE, BROWSER_AGENT_AUTO_APPROVE, _recompute_count

    confirm_tools: set[str] = set()

    # Manual RISK_MAP entries
    for name, risk in RISK_MAP.items():
        if risk == RiskLevel.CONFIRM:
            confirm_tools.add(name)

    # Annotation-derived entries not in RISK_MAP
    for tool_name in _tool_annotations:
        if tool_name in RISK_MAP:
            continue
        derived = _derive_risk_from_annotations(tool_name)
        if derived == RiskLevel.CONFIRM:
            confirm_tools.add(tool_name)

    AGENT_AUTO_APPROVE = frozenset(confirm_tools)
    BROWSER_AGENT_AUTO_APPROVE = AGENT_AUTO_APPROVE
    _recompute_count += 1
    logger.info(
        "AGENT_AUTO_APPROVE recomputed (call #%d): %d tools",
        _recompute_count,
        len(AGENT_AUTO_APPROVE),
    )


# Human-readable description templates per tool.
# {args} is replaced with a formatted argument summary.
_DESCRIPTION_TEMPLATES: dict[str, str] = {
    "file_read": "perform file_read action '{action}'",
    "file_write": "perform file_write action '{action}'",
    # F-AGENT-03: file ops include the absolute resolved path when present
    "write_file": "write to {path}",
    "delete_file": "delete {path}",
    "file_delete": "delete {path}",
    "read_file": "read {path}",
    "run_command": "run the command: {command}. Should I go ahead?",
    "web_search": "search the web for: {query}",
    "web_read": "read the web page: {url}",
    "system_info": "check system information",
    "computer": "perform desktop computer action '{action}'",
    "self_manage": "perform self_manage action '{action}'",
    "mcp_servers": "perform mcp_servers action '{action}'",
    "delegate_to_provider": "delegate a task to external provider",
    "memory": "perform memory action '{action}'",
    "schedule": "perform schedule action '{action}'",
    "phone": "perform phone action '{action}'",
    "transmit_payment_to_call": "transmit approved payment details to the active phone call",
    "browser_navigate": "open {url} in the browser",
    "browser_get_content": "read content from the page",
    "browser_get_links": "get links from the page",
    "browser_get_form_fields": "check form fields on the page",
    "browser_interact": "interact with browser: {action}",
    "browser_screenshot": "take a browser screenshot",
    "browser_back": "go back a page in the browser",
    "browser_close": "close the browser",
    "browser_run_script": "Execute a Playwright script on {url}",
    "browser_snapshot": "Get accessibility tree snapshot of current page",
    "browser_get_api_log": "View captured API calls from browser session",
    "verify_state": "verify browser action succeeded",
    "fill_payment_details": "fill saved payment card into checkout form",
    "payment": "perform payment action '{action}'",
    # Smart home tools
    "smart_home": "perform smart_home action '{action}'",
    "api_credential": "perform api_credential action '{action}'",
    "gmail": "perform Gmail action '{action}'",
    "media": "search or play media",
    "playlist": "perform playlist action '{action}'",
    "timer": "perform timer action '{action}'",
    "alarm": "perform alarm action '{action}'",
    "calendar": "perform calendar action '{action}'",
    # Push notifications
    "notify": "send or schedule notification: {message}",
    "send_notification": "send {priority} notification: {message}",
    "setup_sms_provider": "inspect Telnyx SMS setup",
    "sms_send": "send SMS to {to}: {message}",
}


class ApprovalBridge:
    """Bridge between MCP tool calls and permission/approval plumbing.

    Risk lookup priority:
      1. Manual RISK_MAP overrides
      2. Annotation-derived risk (readOnlyHint / destructiveHint)
      3. Default to CONFIRM for unknown tools

    The resolved risk is advisory metadata. ``PermissionPolicy`` makes the
    first-class allow/ask/deny decision before any UI prompt occurs.
    """

    def __init__(
        self,
        approval_manager: ApprovalManager,
        *,
        permission_policy: PermissionPolicy | None = None,
        permission_mode: PermissionMode = "default",
        user_id: str | None = None,
        session_id: str | None = None,
        hook_registry: Any | None = None,
    ) -> None:
        from core.user_context import get_current_or_device_user_id, user_id_or_none

        self._approval = approval_manager
        self._permission_policy = permission_policy or PermissionPolicy(mode=permission_mode)
        self._permission_mode = permission_mode
        self._user_id = user_id_or_none(user_id) or get_current_or_device_user_id()
        self._session_id = session_id
        self._hook_registry = hook_registry
        self.last_permission_decision: PermissionDecision | None = None

    def get_risk(self, tool_name: str) -> RiskLevel:
        """Look up risk level for a tool.

        Priority:
          1. Manual RISK_MAP entry (overrides)
          2. Annotation-derived risk
          3. Default to CONFIRM

        Built-in unknown tools default to CONFIRM so a missing local RISK_MAP
        entry does not paralyze the agent.  Namespaced external tools default
        to DANGEROUS so an unannotated third-party server cannot silently send,
        mutate, or spend through the CONFIRM auto-allow path.
        """
        # 1. Check manual overrides first
        manual = RISK_MAP.get(tool_name)
        if manual is not None:
            return manual

        # 2. Derive from annotations
        derived = _derive_risk_from_annotations(tool_name)
        if derived is not None:
            return derived

        # 3a. Namespaced external MCP defaults — DANGEROUS until annotations
        # or explicit RISK_MAP entries prove a narrower approval policy.
        if _is_namespaced_external_tool_name(tool_name):
            if tool_name and tool_name not in _warned_unknown:
                _warned_unknown.add(tool_name)
                logger.warning(
                    "Unknown external MCP tool '%s' defaulting to DANGEROUS — add RISK_MAP entry or MCP annotations",
                    tool_name,
                )
            return RiskLevel.DANGEROUS

        # 3b. Built-in unknown default — CONFIRM (auto-approved, not blocking).
        # INT-13: Warn once per unknown tool so unowned RISK_MAP entries are
        # visible.  CONFIRM (not DANGEROUS) is intentional — see SEC-8 /
        # March 23 collapse: DANGEROUS-default paralyzes the agent.
        if tool_name and tool_name not in _warned_unknown:
            _warned_unknown.add(tool_name)
            logger.warning(
                "Unknown tool '%s' defaulting to CONFIRM — add RISK_MAP entry or MCP annotations",
                tool_name,
            )
        return RiskLevel.CONFIRM

    def get_call_risk(self, tool_name: str, args: Mapping[str, Any] | None = None) -> RiskLevel:
        """Look up risk for a specific tool call, including action-sensitive overrides."""
        override = _derive_call_risk_from_args(tool_name, args)
        if override is not None:
            return override
        return self.get_risk(tool_name)

    def build_description(self, tool_name: str, args: dict[str, Any]) -> str:
        """Build a human-readable action description for approval prompts.

        F-AGENT-03 (2026-04-17): path-like arguments are resolved to
        absolute paths before rendering so that approval dialogs always
        show the exact filesystem location the agent is targeting.
        """
        if tool_name == "workbench":
            action = str(args.get("action") or "search").strip() or "search"
            target = str(args.get("filename") or args.get("item_id") or args.get("result_id") or "").strip()
            if target:
                return "perform Workbench action '%s' on '%s'" % (action, target)
            return "perform Workbench action '%s'" % action

        # SEC-033 (W2B-13): the register action launches a subprocess. The
        # approval prompt MUST render the executable + args so the user sees the
        # exact command being run — never a bare "action 'register'".
        if tool_name in ("mcp_servers", "register_mcp_server"):
            action = str(args.get("action") or ("register" if tool_name == "register_mcp_server" else "list")).strip()
            if action == "register":
                server = str(args.get("name") or "").strip()
                command = str(args.get("command") or "").strip()
                raw_args = args.get("args") or []
                if isinstance(raw_args, (list, tuple)):
                    arg_str = " ".join(str(a) for a in raw_args)
                else:
                    arg_str = str(raw_args)
                full_cmd = (command + (" " + arg_str if arg_str else "")).strip() or "(no command)"
                if server:
                    return "register MCP server '%s' which runs: %s" % (
                        server,
                        full_cmd,
                    )
                return "register an MCP server which runs: %s" % full_cmd
            return "perform mcp_servers action '%s'" % (action or "list")

        resolved_args = _resolve_path_like_args(args)
        template = _DESCRIPTION_TEMPLATES.get(tool_name)
        if template is None:
            # Generic fallback for unknown/external tools
            args_summary = (
                ", ".join("%s=%s" % (k, v) for k, v in resolved_args.items()) if resolved_args else "no arguments"
            )
            return "%s(%s)" % (tool_name, args_summary)

        try:
            return template.format(**resolved_args)
        except KeyError:
            # Template references args not provided — fall back to generic
            args_summary = ", ".join("%s=%s" % (k, v) for k, v in resolved_args.items()) if resolved_args else ""
            return "%s(%s)" % (tool_name, args_summary)

    def check_permission(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        channel: Any | None = None,
        hook_provenance: tuple[PermissionHookProvenance, ...] = (),
    ) -> PermissionDecision:
        """Return the first-class permission decision for a tool call."""

        _normalize_approval_only_args(tool_name, args)
        relay_denial = self._relay_consent_denial(tool_name)
        if relay_denial is not None:
            self.last_permission_decision = relay_denial
            return relay_denial
        risk = self.get_call_risk(tool_name, args)
        context = self._build_permission_context(
            tool_name,
            args,
            risk=risk,
            channel=channel,
            hook_provenance=hook_provenance,
        )
        decision = self._permission_policy.check(context)
        self.last_permission_decision = decision
        return decision

    async def check_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        channel: Any | None = None,
    ) -> bool:
        """Check whether a tool call is approved.

        Args:
            tool_name: MCP tool name.
            args: Tool arguments.
            channel: Optional MessageChannel for interactive approval.
                     If the ApprovalManager already has a channel, this
                     is ignored; otherwise it's set temporarily.

        Returns:
            True if approved, False if denied.

        S9-11 closure: when a ``PermissionRequest`` hook is registered for the
        current request (via the bridge's hook registry or any default
        registry), dispatch it before falling back to the interactive prompt.
        This means all callers of ``check_approval`` route through Claude's
        PermissionRequest hook semantics — not just the agent loop.
        """
        _normalize_approval_only_args(tool_name, args)
        relay_denial = self._relay_consent_denial(tool_name)
        if relay_denial is not None:
            self.last_permission_decision = relay_denial
            self._append_permission_frame(relay_denial)
            return False
        risk = self.get_call_risk(tool_name, args)
        context = self._build_permission_context(tool_name, args, risk=risk, channel=channel)
        task_id = _active_agent_task_id()
        decision = self._permission_policy.check(context)
        self.last_permission_decision = decision

        if decision.behavior == "allow":
            _apply_updated_input(args, decision)
            if risk == RiskLevel.CONFIRM:
                logger.debug("Permission policy allowed CONFIRM tool '%s'", tool_name)
            if tool_name == "computer":
                _grant_computer_use_session_app(args, user_id=context.user_id, session_id=context.session_id)
            return True
        if decision.behavior == "deny":
            self._append_permission_frame(decision)
            return False

        # S9-11: PermissionRequest hook can race the interactive prompt. We
        # dispatch synchronously here (Viola has no first-class concurrency
        # bridge for the approval UI yet); deny+interrupt aborts immediately,
        # allow short-circuits the prompt, anything else falls through to the
        # standard ask path.
        hook_outcome = await self._dispatch_permission_request_hook(
            tool_name=tool_name,
            args=args,
            decision=decision,
            channel=channel,
        )
        if hook_outcome is True:
            _apply_updated_input(args, decision)
            if tool_name == "computer":
                _grant_computer_use_session_app(args, user_id=context.user_id, session_id=context.session_id)
            return True
        if hook_outcome is False:
            self._append_permission_frame(decision)
            return False

        self._append_permission_frame(decision)
        description = self.build_description(tool_name, args)

        consume_deferred = getattr(self._approval, "consume_deferred_confirmation", None)
        if callable(consume_deferred):
            confirmation_state = consume_deferred(
                tool_name=tool_name,
                risk=risk,
                tool_args=args,
                task_id=task_id,
                allow_implicit_match=True,
            )
            if confirmation_state == "accepted":
                final_decision = self._permission_policy.resolve_user_response(
                    context,
                    decision,
                    approved=True,
                )
                self.last_permission_decision = final_decision
                if final_decision.behavior == "allow":
                    _apply_updated_input(args, final_decision)
                    if tool_name == "computer":
                        _grant_computer_use_session_app(args, user_id=context.user_id, session_id=context.session_id)
                    return True
                self._append_permission_frame(final_decision)
                return False
            if confirmation_state == "rejected":
                final_decision = self._permission_policy.resolve_user_response(
                    context,
                    decision,
                    approved=False,
                )
                self.last_permission_decision = final_decision
                self._append_permission_frame(final_decision)
                return False

        original_channel = getattr(self._approval, "channel", None)
        if channel is not None and original_channel is None:
            self._approval._channel = channel

        try:
            approved = await _request_permission_decision(
                self._approval,
                decision,
                action_description=description,
                tool_name=tool_name,
                tool_args=args,
                task_id=task_id,
            )
            final_decision = self._permission_policy.resolve_user_response(
                context,
                decision,
                approved=approved,
            )
            self.last_permission_decision = final_decision
            if final_decision.behavior == "allow":
                _apply_updated_input(args, final_decision)
                if tool_name == "computer":
                    _grant_computer_use_session_app(args, user_id=context.user_id, session_id=context.session_id)
                return True
            self._append_permission_frame(final_decision)
            return False
        finally:
            if channel is not None and original_channel is None:
                self._approval._channel = original_channel

    async def _dispatch_permission_request_hook(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        decision: PermissionDecision,
        channel: Any | None,
    ) -> bool | None:
        """Dispatch a ``PermissionRequest`` hook if registered.

        Returns:
            ``True`` if the hook resolved to allow,
            ``False`` if the hook resolved to deny (with or without interrupt),
            ``None`` if no hook applies — caller should fall through to prompt.
        """

        try:
            from intent.hooks.dispatcher import dispatch_tool_hook, has_registered_hooks
        except Exception:
            return None
        registry = getattr(self, "_hook_registry", None)
        if not has_registered_hooks("PermissionRequest", registry=registry, user_id=self._user_id):
            return None
        try:
            payload = {
                "tool_name": tool_name,
                "permission_decision": decision.to_dict(),
                "permission_mode": self._permission_mode,
                "permission_suggestions": [
                    {
                        "type": "add",
                        "behavior": "allow",
                        "tool": tool_name,
                        "scope": "session",
                    }
                ],
                "user_id": self._user_id,
            }
            if registry is not None and hasattr(registry, "dispatch_tool_hook"):
                hook_result = registry.dispatch_tool_hook(
                    "PermissionRequest",
                    tool_name,
                    args,
                    payload,
                    session_id=self._session_id,
                )
            else:
                hook_result = dispatch_tool_hook(
                    "PermissionRequest",
                    tool_name,
                    args,
                    payload,
                    session_id=self._session_id,
                )
        except Exception as exc:
            logger.debug("PermissionRequest hook dispatch failed: %s", exc)
            return None
        # interrupt overrides everything
        if getattr(hook_result, "interrupt", False):
            return False
        request_result = getattr(hook_result, "permission_request_result", None)
        if isinstance(request_result, dict):
            behavior = str(request_result.get("behavior") or "").strip().lower()
            if behavior == "allow":
                updated_input = request_result.get("updatedInput") or request_result.get("updated_input")
                if isinstance(updated_input, dict):
                    args.clear()
                    args.update(updated_input)
                updated_permissions = request_result.get("updatedPermissions") or request_result.get(
                    "updated_permissions"
                )
                _apply_permission_updates_from_entries(
                    self._permission_policy,
                    updated_permissions,
                    tool_name=tool_name,
                )
                return True
            if behavior == "deny":
                return False
        # Top-level decision (Claude approve|block top-level).
        if hook_result.decision == "allow":
            if getattr(hook_result, "updated_input", None):
                args.clear()
                args.update(hook_result.updated_input)
            return True
        if hook_result.decision == "deny":
            return False
        return None

    def _relay_consent_denial(self, tool_name: str) -> PermissionDecision | None:
        """Deny desktop-control tools inside an unconsented relayed turn.

        This runs BEFORE the policy check, and before the computer-use
        whitelist / session-grant overrides in
        ``_build_permission_context``, on purpose: those overrides model a
        user approving something at their own machine, which says nothing
        about a principal on another device. Letting them run first is what
        produced ``Permission policy allowed CONFIRM tool 'computer'`` for a
        relayed turn -- the desktop's own auto-approval applied unchanged to
        a remote caller that never held the consent grant the bounded
        ``desktop.*`` path demands.

        The marker defaults to False, so a turn that is not a relay is
        unaffected and this returns ``None`` for every tool. Imported at
        module scope (``intent.permissions.remote_relay`` is stdlib-only, and
        ``intent.permissions.policy`` is already imported there) so there is
        no swallowed-import path that could quietly stop enforcing.
        """
        if not relay_consent_blocks(tool_name):
            return None
        return PermissionDecision(
            behavior="deny",
            source="remoteRelayConsent",
            mode=self._permission_mode,
            reason=relay_consent_denial_reason(tool_name),
        )

    def _build_permission_context(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        risk: RiskLevel,
        channel: Any | None,
        hook_provenance: tuple[PermissionHookProvenance, ...] = (),
    ) -> PermissionContext:
        user_id, session_id = self._resolve_permission_identity()
        rule_overrides: list[PermissionRule] = []
        if tool_name == "computer" and risk == RiskLevel.DANGEROUS and _computer_use_whitelist_allows(args):
            rule_overrides.append(
                PermissionRule(
                    tool_name="computer",
                    behavior="allow",
                    source="policySettings",
                    reason="local computer-use app whitelist allowed this action",
                )
            )
        elif (
            tool_name == "computer"
            and risk == RiskLevel.DANGEROUS
            and _computer_use_session_grant_allows(args, user_id=user_id, session_id=session_id)
        ):
            rule_overrides.append(
                PermissionRule(
                    tool_name="computer",
                    behavior="allow",
                    source="policySettings",
                    reason="session app consent allowed this computer-use action",
                )
            )
        return PermissionContext(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=args,
            channel=_channel_name(channel or getattr(self._approval, "channel", None)),
            mode=self._permission_mode,
            risk_level=risk,
            rule_overrides=tuple(rule_overrides),
            hook_provenance=hook_provenance,
        )

    def _resolve_permission_identity(self) -> tuple[str, str]:
        try:
            from services.conversation.state_manager import (
                get_request_conversation_manager,
            )

            manager = get_request_conversation_manager()
            if manager is not None:
                return manager.user_id(), manager.session_id()
        except Exception as exc:
            logger.debug(
                "Could not resolve request conversation manager for permission policy: %s",
                exc,
            )
        return self._user_id, self._session_id or "local-session"

    def _append_permission_frame(self, decision: PermissionDecision) -> None:
        if decision.frame is None:
            return
        try:
            from services.conversation.state_manager import (
                get_request_conversation_manager,
            )

            manager = get_request_conversation_manager()
            if manager is not None:
                manager.add_message(decision.frame)
        except Exception as exc:
            logger.debug("Could not append permission meta frame: %s", exc)


def _normalize_approval_only_args(tool_name: str, args: dict[str, Any]) -> None:
    """Remove fields that do not participate in the requested action."""

    if tool_name != "phone":
        return
    if str(args.get("action") or "").strip().lower() != "call":
        return
    if "call_id" in args:
        args["call_id"] = ""


def _apply_updated_input(args: dict[str, Any], decision: PermissionDecision) -> None:
    if decision.updated_input is None:
        return
    args.clear()
    args.update(decision.updated_input)


def _apply_permission_hook_updates(permission_policy: PermissionPolicy, hook_result: Any, *, tool_name: str) -> None:
    updated_permissions = getattr(hook_result, "updated_permissions", ()) or ()
    _apply_permission_updates_from_entries(permission_policy, updated_permissions, tool_name=tool_name)


def _apply_permission_updates_from_entries(
    permission_policy: PermissionPolicy,
    updated_permissions: Any,
    *,
    tool_name: str,
) -> None:
    if not updated_permissions:
        return
    rule_list = getattr(permission_policy, "_rules", None)
    if rule_list is None:
        return
    new_rules: list[PermissionRule] = []
    for entry in updated_permissions:
        if not isinstance(entry, Mapping):
            continue
        op = str(entry.get("type") or "add").strip().lower()
        behavior = str(entry.get("behavior") or "allow").strip().lower()
        rule_tool = str(entry.get("tool") or entry.get("toolName") or tool_name).strip()
        if op != "add" or behavior not in {"allow", "ask", "deny"} or not rule_tool:
            continue
        new_rules.append(
            PermissionRule(
                tool_name=rule_tool,
                behavior=cast(PermissionBehavior, behavior),
                source="hook:PermissionRequest",
                reason=str(entry.get("reason") or "PermissionRequest hook accepted this permission"),
            )
        )
    if new_rules:
        permission_policy._rules = tuple([*new_rules, *rule_list])


async def _request_permission_decision(
    approval_manager: Any,
    decision: PermissionDecision,
    *,
    action_description: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> bool:
    request_decision = getattr(approval_manager, "request_permission_decision", None)
    if callable(request_decision):
        try:
            result = await request_decision(
                decision,
                action_description=action_description,
                tool_name=tool_name,
                tool_args=tool_args,
                task_id=task_id,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            result = await request_decision(
                decision,
                action_description=action_description,
                tool_name=tool_name,
            )
        return bool(result)
    try:
        result = await approval_manager.request_approval(
            action_description=action_description,
            risk=_risk_from_permission_decision(decision),
            tool_name=tool_name,
            tool_args=tool_args,
            task_id=task_id,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        result = await approval_manager.request_approval(
            action_description=action_description,
            risk=_risk_from_permission_decision(decision),
            tool_name=tool_name,
        )
    return bool(result)


def _risk_from_permission_decision(decision: PermissionDecision) -> RiskLevel:
    risk = decision.risk_level
    if isinstance(risk, RiskLevel):
        return risk
    if isinstance(risk, str):
        try:
            return RiskLevel(risk)
        except ValueError:
            return RiskLevel.DANGEROUS
    return RiskLevel.DANGEROUS


def _channel_name(channel: Any | None) -> str | None:
    if channel is None:
        return None
    channel_type = getattr(channel, "channel_type", None)
    if isinstance(channel_type, str) and channel_type:
        return channel_type
    return type(channel).__name__
