"""Core Tools MCP Server.

Wraps agentic tools from ``intent/tools/`` as MCP tools exposed
via a FastMCP server named "viola-core-tools".

Browser tools live in the separate Playwright MCP server
(``mcp_servers/browser/``).

Each tool delegates to the existing handler function in the relevant
``intent.tools.*`` module and converts the ``ToolResult`` dataclass to
a plain string for the MCP protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
from collections.abc import Iterator
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from core.logging_config import get_logger
from core.platform import get_data_dir
from intent.agent_subagent_types import SUBAGENT_TYPE_FIELD_DESCRIPTION
from intent.tools.alarm_tools import (
    cancel_alarm_handler as _cancel_alarm_handler,
    list_alarms_handler as _list_alarms_handler,
    play_alarm_sound_handler as _play_alarm_sound_handler,
    set_alarm_handler as _set_alarm_handler,
)
from intent.tools.api_registry_tool import (
    check_api_registry_handler as _check_api_registry,
)
from intent.tools.ask_user import ask_user_handler as _ask_user
from intent.tools.bug_reporting import file_bug_report_handler as _file_bug_report
from intent.tools.delegation import delegate_to_provider as _delegate_to_provider

# Gmail: provided by Google Workspace MCP server (google-workspace, MCP-4).
# Stub IMAP/SMTP tools removed (2026-04-09) — all email goes through Gmail.
# ---------------------------------------------------------------------------
# Handler imports
# ---------------------------------------------------------------------------
from intent.tools.filesystem import (
    delete_file as _delete_file,
    file_info as _file_info,
    list_directory as _list_directory,
    read_file as _read_file,
    search_files as _search_files,
    write_file as _write_file,
)
from intent.tools.liked_songs_tools import get_liked_songs_handler as _get_liked_songs
from intent.tools.media_tools import media_handler as _media
from intent.tools.memory import memory_handler as _memory
from intent.tools.music_connect import (
    check_music_provider_status_handler as _check_music_provider_status,
    connect_music_provider_handler as _connect_music_provider,
)
from intent.tools.music_tools import (
    playback_control_handler as _playback_control_handler,
    rate_track_handler as _rate_track_handler,
)
from intent.tools.notification_tools import (
    list_notify_reminders_handler as _list_notify_reminders_handler,
    notify_handler as _notify_handler,
)
from intent.tools.phone_call import (
    check_call_status as _check_call_status,
    end_phone_call as _end_phone_call,
    get_call_transcript as _get_call_transcript,
    make_phone_call as _make_phone_call,
)

try:
    from intent.tools.phone_transmit import handle_transmit_payment_to_call as _transmit_payment_to_call
except ModuleNotFoundError as exc:
    if exc.name not in {"intent.tools.phone_transmit", "services.payments"}:
        raise

    async def _transmit_payment_to_call(_args: dict[str, Any]) -> str:
        return json.dumps({"ok": False, "code": "payment_feature_unavailable"}, separators=(",", ":"))


from intent.tools.playlist_tools import (
    add_track_to_playlist_handler as _add_track_to_playlist,
    create_playlist_handler as _create_playlist,
    delete_playlist_handler as _delete_playlist,
    list_playlists_handler as _list_playlists,
    play_favorites_handler as _play_favorites,
    play_playlist_handler as _play_playlist_mcp,
)
from intent.tools.scheduling import (
    schedule_create_handler as _schedule_create,
    schedule_delete_handler as _schedule_delete,
    schedule_list_handler as _schedule_list,
    schedule_update_handler as _schedule_update,
)
from intent.tools.self_management import (
    install_package as _install_package,
    list_mcp_servers as _list_mcp_servers,
    register_mcp_server as _register_mcp_server,
    setup_codex as _setup_codex,
    update_status as _update_status,
)
from intent.tools.settings_tools import (
    settings_get_handler as _settings_get,
    settings_list_adjustable_handler as _settings_list_adjustable,
    settings_set_handler as _settings_set,
)
from intent.tools.shell import run_command as _run_command
from intent.tools.system_state import system_info as _system_info
from intent.tools.telegram_tools import telegram_send_handler as _telegram_send
from intent.tools.timer_tools import (
    cancel_all_timers_handler as _cancel_all_timers_handler,
    cancel_sleep_timers_handler as _cancel_sleep_timers_handler,
    cancel_timer_handler as _cancel_timer_handler,
    list_timers_handler as _list_timers_handler,
    set_sleep_timer_handler as _set_sleep_timer_handler,
    set_timer_handler as _set_timer_handler,
)
from intent.tools.tool_search import tool_search_handler as _tool_search
from intent.tools.user_capabilities import (
    create_user_capability as _create_user_capability,
    delete_user_capability as _delete_user_capability,
    list_user_capabilities as _list_user_capabilities,
    run_user_capability as _run_user_capability,
    toggle_user_capability as _toggle_user_capability,
    update_user_capability as _update_user_capability,
)
from intent.tools.web_read import web_read as _web_read
from intent.tools.web_search import web_search as _web_search
from intent.tools.workbench import (
    workbench_forget_handler as _workbench_forget,
    workbench_list_handler as _workbench_list,
    workbench_path_for_handler as _workbench_path_for,
    workbench_read_handler as _workbench_read,
    workbench_remember_handler as _workbench_remember,
    workbench_search_handler as _workbench_search,
)
from mcp_servers.phone_tool_descriptions import (
    CALL_ID_DESCRIPTION,
    CALLER_NAME_DESCRIPTION,
    CORE_PHONE_TOOL_DESCRIPTION,
    EXTRA_CONTEXT_DESCRIPTION,
    PHONE_ACTION_DESCRIPTION,
    PHONE_NUMBER_DESCRIPTION,
    TASK_DESCRIPTION,
    WAIT_FOR_COMPLETION_DESCRIPTION,
)
from services.telnyx_sender import (
    APPROVED_SMS_FROM_NUMBER,
    LAUNCH_SMS_ALLOWED_RECIPIENTS,
    LAUNCH_SMS_ALLOWED_TO_NUMBER,
)
from utils.speaker_pairing_flow import build_speaker_pairing_payload

# Smart home and notification handlers (lazy imports for optional deps)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Annotation presets
# ---------------------------------------------------------------------------

_SAFE = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_CONFIRM = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DANGEROUS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

_SHARE_CONTENT_SOURCES: frozenset[str] = frozenset({"last_assistant_message", "explicit_text", "last_n_messages"})
_SHARE_DESTINATION_KINDS: frozenset[str] = frozenset({"sms", "email", "file"})
_TELNYX_SMS_SETUP_DOC = "docs/TELNYX_SMS_SETUP.md"
_TELNYX_MESSAGING_PORTAL_URL = "https://portal.telnyx.com/#/app/messaging"
_TELNYX_10DLC_PORTAL_URL = "https://portal.telnyx.com/#/app/10dlc"
_TELNYX_10DLC_DOC_URL = "https://developers.telnyx.com/docs/messaging/10dlc/phone-number-assignment"
_TELNYX_ERROR_DOC_URL = "https://developers.telnyx.com/docs/messaging/messages/error-codes"
_TELNYX_DELIVERY_FAILURE_STATUSES: frozenset[str] = frozenset({"delivery_failed", "failed", "undelivered", "rejected"})
_TELNYX_DELIVERY_PENDING_STATUSES: frozenset[str] = frozenset({"queued", "sending"})
_TELNYX_DELIVERY_POLL_ATTEMPTS = 5
_TELNYX_DELIVERY_POLL_INTERVAL_SECONDS = 1.0
_SMS_INVALID_RECIPIENT_FORMAT = "invalid_recipient_format"
_SMS_10DLC_UNREGISTERED = "10dlc_unregistered"
_SMS_TOLLFREE_UNVERIFIED = "tollfree_unverified"
_SMS_GATEWAY_REJECTION = "gateway_rejection"
_SMS_UNCONFIGURED_SENDER = "unconfigured_sender"
_SMS_CARRIER_BLOCKED = "carrier_blocked"
_SMS_LAUNCH_RECIPIENT_BLOCKED = "launch_recipient_blocked"
_SMS_RECIPIENT_OPTED_OUT = "recipient_opted_out"


class ParallelSubtaskSpec(BaseModel):
    """Constructable child-agent task spec for spawn_parallel_subtasks."""

    task: str = Field(description="Self-contained subtask description for one child agent.")


# ---------------------------------------------------------------------------
# A6: Concurrency-safe tool flag
# ---------------------------------------------------------------------------

_CONCURRENCY_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "think",
        "file_read",
        "web_search",
        "web_read",
        "system_info",
        "check_pending_tasks",
        "check_api_registry",
        "ToolSearch",
        "tool_search",
        "get_liked_songs",
        "check_music_provider_status",
        "open_app_panel",
        "pair_speaker_setup",
        "gmail_inbox",
        "gmail_search",
        "gmail_read",
        "gmail_daily_summary",
        "google_workspace",
        "setup_sms_provider",
    }
)

_APP_PANEL_ACTIONS: dict[str, dict[str, str]] = {
    "settings": {
        "ui_action": "open_settings",
        "path_identifier": "settings",
    },
    "rooms.add_speaker": {
        "ui_action": "open_rooms_add_speaker",
        "path_identifier": "rooms.add_speaker",
        "rooms_modal_tab": "add-speaker",
    },
    "calendar": {
        "ui_action": "open_calendar",
        "path_identifier": "calendar",
    },
    "music_accounts": {
        "ui_action": "open_music_accounts",
        "path_identifier": "settings.music_accounts",
        "tab": "music_voice",
        "section": "music_accounts",
    },
    "payment_methods": {
        "ui_action": "open_payment_methods",
        "path_identifier": "settings.payment",
        "tab": "payment",
    },
    "help": {
        "ui_action": "open_help",
        "path_identifier": "help",
    },
}

_APP_PANEL_ALIASES: dict[str, str] = {
    "rooms_add_speaker": "rooms.add_speaker",
    "room_add_speaker": "rooms.add_speaker",
    "add_speaker": "rooms.add_speaker",
    "add_room": "rooms.add_speaker",
    "speaker_setup": "rooms.add_speaker",
    "speakers": "rooms.add_speaker",
    "rooms": "rooms.add_speaker",
    "music": "music_accounts",
    "music_voice": "music_accounts",
    "music_account": "music_accounts",
    "connected_music": "music_accounts",
    "payment": "payment_methods",
    "payments": "payment_methods",
    "cards": "payment_methods",
    "saved_cards": "payment_methods",
    "calendar_view": "calendar",
    "settings_page": "settings",
    "help_panel": "help",
    "docs": "help",
}

_APP_PANEL_SETTINGS_TABS: dict[str, str] = {
    "ai": "ai_agents",
    "ai_agents": "ai_agents",
    "agents": "ai_agents",
    "account": "account",
    "accounts": "account",
    "profile": "account",
    "music": "music_voice",
    "music_voice": "music_voice",
    "music_accounts": "music_voice",
    "voice": "music_voice",
    "messaging": "messaging",
    "messages": "messaging",
    "services": "services",
    "connected_services": "services",
    "payment": "payment",
    "payments": "payment",
    "payment_methods": "payment",
    "preferences": "preferences",
    "appearance": "preferences",
    "weather": "preferences",
}


def is_concurrency_safe(tool_name: str) -> bool:
    """Return True if the tool is safe for parallel execution (read-only, no side effects)."""
    return tool_name in _CONCURRENCY_SAFE_TOOLS


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

server = FastMCP("viola-core-tools")


def _get_call_meta() -> dict[str, object]:
    """Return request metadata for the current MCP tool invocation."""
    try:
        context = server.get_context()
        request_context = context.request_context
        meta = getattr(request_context, "meta", None)
        if meta is None:
            return {}
        if hasattr(meta, "model_dump"):
            dumped = meta.model_dump()
            return dumped if isinstance(dumped, dict) else {}
        if isinstance(meta, dict):
            return meta
    except Exception:
        logger.debug("No MCP request metadata available for core tool call")
    return {}


@contextlib.contextmanager
def _caller_cloud_bearer_bound() -> Iterator[None]:
    """Bind the CALLING request's cloud bearer for the body of a tool call.

    GitHub #584. This server is connected over the MCP memory transport, which
    runs it in an anyio task created once when the hub is built. That task froze
    its ``contextvars`` at creation time, so a tool handler here observes the
    hub-build request's context forever, never the context of the request
    actually calling the tool. Any cloud tool that authenticates as the user
    (the phone tool POSTs ``/api/phone/call``) therefore resolved a stale or
    empty bearer and got 401 "Cloud authentication failed" even after the user
    had confirmed the action.

    The hub attaches the live bearer per call for in-process servers only
    (``mcp_hub/client_hub.py::_attach_inprocess_cloud_bearer``). Binding it here
    for the duration of the call, and resetting it after, is what makes
    ``_resolve_cloud_bearer_token``'s request-scoped source current again.
    Nothing is bound when the caller supplied no bearer, so behaviour on the
    desktop surface (which resolves its token from the session store instead) is
    unchanged.
    """

    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    access_token = ""
    if isinstance(user_context, dict):
        candidate = user_context.get("cloud_access_token")
        if isinstance(candidate, str):
            access_token = candidate.strip()
    if not access_token:
        yield
        return

    from core.user_context import (
        reset_current_cloud_access_token,
        set_current_cloud_access_token,
    )

    token = set_current_cloud_access_token(access_token)
    try:
        yield
    finally:
        try:
            reset_current_cloud_access_token(token)
        except ValueError:
            logger.debug("Cloud bearer context token belongs to another context; skipping reset")


def _get_call_user_id() -> str | None:
    """Extract the caller user_id from MCP request metadata."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        user_id = user_context.get("user_id")
        if isinstance(user_id, str) and user_id:
            return user_id
    user_id = meta.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    return None


def _call_shell_permission_granted() -> bool:
    """Return True only for a shell call immediately preceded by user approval."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    if isinstance(user_context, dict):
        return user_context.get("shell_permission_granted") is True
    return False


# Tools whose whole contract is "act on the machine the user is sitting at".
# ``core-tools`` is registered unconditionally on every surface
# (``mcp_hub/runtime_config.py``), including the cloud API container, so
# without this guard a cloud-executed turn ran them against the CONTAINER and
# the answer was handed to the user as their own computer -- "list my
# Downloads" returning the container's home directory, an ``~/.ssh`` read
# returning the server's, a ``run_command`` executing inside production.
#
# The cloud allowlist in ``services/cloud_intent/tool_availability.py`` is the
# first line and no longer offers these on cloud. This is the second, and it
# is the one that holds: it does not care how the call was routed, what the
# manifest claimed, or whether a companion was online. There is deliberately
# no "partial" cloud mode -- reading the server's disk is never a useful
# approximation of the user's disk, it is a wrong answer to the question
# actually asked, so the only correct cloud behavior is to decline and say
# where the work has to happen.
_LOCAL_SURFACE_TOOL_CAPABILITY: dict[str, str] = {
    "file_read": "reads files on the user's own computer",
    "file_write": "writes and deletes files on the user's own computer",
    "run_command": "runs shell commands on the user's own computer",
    "system_info": "reports the user's own computer's hardware and OS",
}


def _local_surface_refusal(tool_name: str) -> str | None:
    """Return a refusal payload when a local-only tool is called on cloud.

    ``None`` means this process IS the user's machine, so the call proceeds.
    Fails CLOSED: any error resolving the surface refuses rather than falling
    through to touching a filesystem we cannot prove belongs to the user.
    """
    try:
        from services.computer_use.cloud_guard import is_cloud_surface

        if not is_cloud_surface():
            return None
        reason_suffix = ""
    except Exception:
        logger.exception("Surface check failed for %s; refusing local execution", tool_name)
        reason_suffix = " (surface could not be determined, so this failed closed)"

    capability = _LOCAL_SURFACE_TOOL_CAPABILITY.get(tool_name, "acts on the user's own computer")
    return json.dumps(
        {
            "ok": False,
            "error_category": "LOCAL_SURFACE_UNAVAILABLE",
            "reason": (
                "%s %s. This turn is running on Viola's cloud server, which has no route to that "
                "machine -- and this server's own filesystem is NOT the user's, so answering from "
                "it would be wrong, not partial. Tell the user this needs the Viola desktop app on "
                "the computer in question; do not describe this server as their machine.%s"
                % (tool_name, capability, reason_suffix)
            ),
        }
    )


def _require_call_user_id(tool_name: str) -> str:
    """Return the concrete caller user_id for user-stateful MCP tools."""
    user_id = _get_call_user_id()
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
        if user_id:
            return user_id
    except (ImportError, LookupError):
        pass
    raise ValueError("%s requires a concrete user_id" % tool_name)


def _is_expected_auth_error(exc: Exception) -> bool:
    return isinstance(exc, ValueError) and "requires a concrete user_id" in str(exc)


def _expected_auth_error_payload(exc: Exception) -> str:
    return json.dumps({"error": str(exc), "error_category": "EXPECTED_AUTH", "retryable": False})


async def _call_user_scoped(handler: object, *args: object, **kwargs: object) -> object:
    """Call a handler and inject user_id when the handler supports it."""
    tool_name = getattr(handler, "__name__", type(handler).__name__)
    user_id = _require_call_user_id(str(tool_name))

    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        signature = None

    if signature is not None and "user_id" in signature.parameters:
        kwargs = {**kwargs, "user_id": user_id}

    return await handler(*args, **kwargs)  # type: ignore[misc]


async def _call_with_required_user_id(
    handler: object,
    user_id: str,
    *args: object,
    **kwargs: object,
) -> object:
    """Call a handler under an explicit, already-required caller user_id."""
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        signature = None

    if signature is not None and "user_id" in signature.parameters:
        kwargs = {**kwargs, "user_id": user_id}

    from core.user_context import user_scope

    with user_scope(user_id):
        return await handler(*args, **kwargs)  # type: ignore[misc]


async def _call_with_current_user_context(handler: object, *args: object, **kwargs: object) -> object:
    """Call a handler that resolves user_id from core.user_context only."""
    user_id = _get_call_user_id()
    if user_id:
        from core.user_context import user_scope

        with user_scope(user_id):
            return await handler(*args, **kwargs)  # type: ignore[misc]
    return await handler(*args, **kwargs)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helper: convert ToolResult -> str
# ---------------------------------------------------------------------------


def _format_result(result: object) -> str:
    """Convert a ``ToolResult`` to a plain string for the MCP protocol.

    Returns a JSON string when the payload is a dict or list, the plain
    ``str()`` representation for other truthy data, and a JSON error
    object when ``result.ok`` is False.

    Error results are returned as ``{"error": "..."}`` JSON so the hub's
    soft-failure detection (``_convert_result``) can propagate
    ``success: False`` to the executor/spin detector.  Previously,
    errors were plain ``"Error: ..."`` strings that the hub treated as
    ``success: True`` — causing the LLM to hallucinate success (SEC-007).
    """
    if result.ok:  # type: ignore[union-attr]
        data = result.data  # type: ignore[union-attr]
        if isinstance(data, (dict, list)):
            return json.dumps(data, default=str, ensure_ascii=False)
        return str(data) if data is not None else "OK"
    return json.dumps({"error": result.error or "Unknown error"})  # type: ignore[union-attr]


def _format_result_with_error_data(result: object) -> str:
    """Format a ToolResult while preserving structured failure data."""
    if result.ok:  # type: ignore[union-attr]
        return _format_result(result)

    payload: dict[str, object] = {
        "ok": False,
        "error": result.error or "Unknown error",  # type: ignore[union-attr]
    }
    data = result.data  # type: ignore[union-attr]
    if data is not None:
        payload["data"] = _strip_model_steering_fields(data)
    for attr in ("error_category", "required_tier"):
        value = getattr(result, attr, None)
        if value:
            payload[attr] = value
    if getattr(result, "retryable", False):
        payload["retryable"] = True
    if isinstance(data, dict):
        for key in (
            "error_code",
            "error_category",
            "action",
            "provider",
            "connected",
            "logged_in",
            "login_url_to_show_user",
            "signin_url",
            "signup_url",
            "tos_status_url",
            "accept_tos_url",
            "message",
            "blocked",
            "reason",
        ):
            if key in data:
                payload.setdefault(key, data[key])
        payload.update(_blocked_call_safety_advice_for_model(data))
    return json.dumps(payload, default=str, ensure_ascii=False)


_BLOCKED_CALL_SAFETY_ADVICE_REASONS = frozenset(
    {
        "crisis_lifeline_redirect",
        "emergency_number_blocked",
    }
)
_MODEL_SAFETY_ADVICE_KEY = "advice_for_assistant"
_MODEL_STEERING_DATA_KEYS = frozenset(
    {
        _MODEL_SAFETY_ADVICE_KEY,
        "next_action",
        "recommended_next_tool",
        "recommended_next_tool_args",
    }
)


def _blocked_call_safety_advice_for_model(data: dict[str, object]) -> dict[str, object]:
    if data.get("blocked") is not True:
        return {}
    if data.get("reason") not in _BLOCKED_CALL_SAFETY_ADVICE_REASONS:
        return {}
    advice = data.get(_MODEL_SAFETY_ADVICE_KEY)
    if not isinstance(advice, str) or not advice.strip():
        return {}
    return {_MODEL_SAFETY_ADVICE_KEY: advice}


def _strip_model_steering_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_model_steering_fields(item)
            for key, item in value.items()
            if str(key) not in _MODEL_STEERING_DATA_KEYS
        }
    if isinstance(value, list):
        return [_strip_model_steering_fields(item) for item in value]
    return value


def _normalize_app_panel_key(value: str | None) -> str:
    if not value:
        return ""
    key = re.sub(r"[\s\-\/]+", "_", value.strip().lower())
    key = key.replace("rooms_add_room", "rooms_add_speaker")
    key = key.replace("rooms_add_speaker", "rooms.add_speaker")
    return _APP_PANEL_ALIASES.get(key, key)


def _normalize_settings_tab(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[\s\-\/]+", "_", value.strip().lower())
    return _APP_PANEL_SETTINGS_TABS.get(key, key)


class _AppPanelPrefill(BaseModel):
    """Strict MCP schema for UI prefill values."""

    model_config = ConfigDict(extra="forbid")

    room_name: str | None = Field(
        default=None,
        description="Room or speaker name to prefill in the Add Room flow.",
    )


def _normalize_app_panel_prefill(
    prefill: _AppPanelPrefill | dict[str, Any] | str | None,
) -> dict[str, Any]:
    if isinstance(prefill, BaseModel):
        return {str(k): v for k, v in prefill.model_dump(mode="json").items() if v is not None}
    if isinstance(prefill, dict):
        return {str(k): v for k, v in prefill.items() if v is not None}
    if isinstance(prefill, str) and prefill.strip():
        return {"room_name": prefill.strip()}
    return {}


def _gate_payload(ok: bool, **fields: object) -> str:
    """Return a JSON payload that MCPClientHub can classify as success/error."""
    payload = {"ok": ok, **fields}
    return json.dumps(payload, ensure_ascii=False, default=str)


def _gate_error(message: str, **fields: object) -> str:
    return _gate_payload(False, error=message, **fields)


def _payment_gate_error(message: str, **fields: object) -> str:
    return _gate_error(message, outcome="payment_gate_error", **fields)


def _share_payload(ok: bool, **fields: object) -> str:
    """Return a structured share_response payload."""

    payload = {"ok": ok, **fields}
    return json.dumps(payload, ensure_ascii=False, default=str)


def _share_error(message: str, **fields: object) -> str:
    return _share_payload(False, status="error", error=message, **fields)


def _email_text(value: object) -> str:
    text = str(value or "").strip()
    return text if "@" in text else ""


async def _resolve_share_account_email(user_id: str) -> str:
    """Resolve the account owner's email for "send to my email" follow-ups."""
    meta = _get_call_meta()
    user_context = meta.get("viola_user_context")
    for source in (
        meta,
        user_context if isinstance(user_context, dict) else None,
        meta.get("account_owner"),
        meta.get("share_response_context"),
    ):
        if not isinstance(source, dict):
            continue
        for key in ("account_email", "user_email", "email", "email_address"):
            email = _email_text(source.get(key))
            if email:
                return email

    # GoTrue on cloud, legacy SQLite users on desktop, local profile last --
    # never a bare public.users query on Postgres (dropped by migration 066, #1233).
    try:
        from auth.account_lookup import resolve_account_email

        email = _email_text(await resolve_account_email(user_id))
        if email:
            return email
    except Exception:
        logger.debug("Could not resolve account email for share_response user %s", user_id)
    return ""


def _parse_tool_payload(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


def _resolve_share_user_id() -> str | None:
    """Resolve an explicit per-user scope for share/export actions."""

    user_id = _get_call_user_id()
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except Exception:
        return None


def _normalize_share_messages(raw_messages: object) -> list[dict[str, str]]:
    if not isinstance(raw_messages, list):
        return []

    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if isinstance(item, dict):
            role_raw = item.get("role")
            content_raw = item.get("content") or item.get("message") or item.get("text")
        else:
            role_raw = getattr(item, "role", None)
            content_raw = (
                getattr(item, "content", None) or getattr(item, "message", None) or getattr(item, "text", None)
            )

        role = str(role_raw or "").strip().lower()
        content = str(content_raw or "").strip()
        if role not in {"user", "assistant", "system"} or not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


def _get_share_context_messages(user_id: str) -> list[dict[str, str]]:
    """Load current request history first, then per-user conversation state."""

    meta = _get_call_meta()
    share_context = meta.get("share_response_context")
    if isinstance(share_context, dict):
        messages = _normalize_share_messages(share_context.get("conversation_history"))
        if messages:
            return messages

    try:
        from services.conversation.state_manager import (
            get_conversation_manager,
            get_request_conversation_manager,
        )

        manager = get_request_conversation_manager() or get_conversation_manager(user_id)
        return _normalize_share_messages(manager.get_context(max_tokens=20_000))
    except Exception:
        logger.debug("Failed to load conversation history for share_response", exc_info=True)
        return []


def _last_assistant_message(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = str(message.get("content") or "").strip()
            if content:
                return content
    return ""


def _format_last_messages(messages: list[dict[str, str]], last_n_messages: int) -> str:
    count = max(1, min(int(last_n_messages or 1), 20))
    selected = messages[-count:]
    return "\n\n".join("%s: %s" % (msg["role"], msg["content"]) for msg in selected)


def _resolve_share_content(
    *,
    user_id: str,
    content_source: str,
    explicit_text: str,
    last_n_messages: int,
) -> tuple[str, str]:
    source = (content_source or "last_assistant_message").strip().lower()
    if source not in _SHARE_CONTENT_SOURCES:
        return "", "content_source must be one of: %s" % ", ".join(sorted(_SHARE_CONTENT_SOURCES))

    if source == "explicit_text":
        content = (explicit_text or "").strip()
        if not content:
            return "", "explicit_text is required when content_source='explicit_text'."
        return content, ""

    messages = _get_share_context_messages(user_id)
    if source == "last_n_messages":
        content = _format_last_messages(messages, last_n_messages)
    else:
        content = _last_assistant_message(messages)

    if not content:
        return "", "No prior assistant message was available to share."
    return content, ""


def _canonical_share_destination_kind(destination_kind: str) -> str:
    kind = (destination_kind or "").strip().lower()
    aliases = {
        "text": "sms",
        "message": "sms",
        "sms_message": "sms",
        "mail": "email",
        "gmail": "email",
        "save": "file",
        "file_save": "file",
    }
    return aliases.get(kind, kind)


def _slug_part(value: str, *, fallback: str = "assistant-response") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:80]


def _title_from_content(content: str) -> str:
    for line in content.splitlines():
        stripped = re.sub(r"[*_`#>\[\]():]+", " ", line).strip()
        if stripped:
            words = stripped.split()[:8]
            return " ".join(words)
    return "assistant response"


def _resolve_share_export_path(
    *,
    user_id: str,
    destination: str,
    title: str,
    file_format: str,
    content: str,
) -> Path:
    requested = Path((destination or "").strip()) if destination.strip() else None
    requested_suffix = requested.suffix.lower().lstrip(".") if requested and requested.suffix else ""
    fmt = (file_format or requested_suffix or "md").strip().lower()
    if fmt not in {"md", "txt"}:
        fmt = "md"

    if requested is not None and requested.name:
        stem_source = requested.stem
    elif title.strip():
        stem_source = title
    else:
        stem_source = _title_from_content(content)

    user_slug = _slug_part(user_id, fallback="user")
    title_slug = _slug_part(stem_source)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    export_dir = get_data_dir() / "exports" / user_slug
    candidate = export_dir / ("%s_%s.%s" % (timestamp, title_slug, fmt))
    counter = 2
    while candidate.exists():
        candidate = export_dir / ("%s_%s_%d.%s" % (timestamp, title_slug, counter, fmt))
        counter += 1
    return candidate


def _require_gate_user_id() -> str | None:
    """Return the explicit MCP caller user_id for sensitive gate tools."""
    user_id = _get_call_user_id()
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except Exception:
        return None


def _load_waiting_gate_checkpoint(task_id: str, user_id: str, gate_type: str) -> tuple[Any | None, str | None]:
    """Load and validate a waiting checkpoint for a specific gate type."""
    from intent.task_checkpoint import load_checkpoint

    checkpoint = load_checkpoint(task_id, user_id=user_id)
    if checkpoint is None:
        return None, "Checkpoint %s was not found for this user." % task_id
    status = str(getattr(checkpoint, "status", "") or "")
    if status != "waiting_for_user":
        return None, "Checkpoint %s is no longer waiting_for_user (state: %s)." % (
            task_id,
            status or "unknown",
        )
    context = getattr(checkpoint, "context", None) or {}
    actual_type = str(context.get("pending_gate_type") or "").strip().lower()
    if actual_type != gate_type:
        return None, "Checkpoint %s is not a %s gate (type: %s)." % (
            task_id,
            gate_type,
            actual_type or "unknown",
        )
    return checkpoint, None


def _load_latest_waiting_gate_checkpoint(user_id: str, gate_type: str) -> tuple[Any | None, str, str | None]:
    """Load the latest waiting checkpoint for the current user and gate type."""
    from intent.task_checkpoint import get_latest_waiting_checkpoint

    checkpoint = get_latest_waiting_checkpoint(user_id, gate_type=gate_type)
    if checkpoint is None:
        return None, "", "No pending %s gate is waiting for this user." % gate_type

    task_id = str(getattr(checkpoint, "task_id", "") or "").strip()
    if not task_id:
        return None, "", "The pending %s gate has no checkpoint id." % gate_type

    status = str(getattr(checkpoint, "status", "") or "")
    if status != "waiting_for_user":
        return (
            None,
            task_id,
            "Checkpoint %s is no longer waiting_for_user (state: %s)."
            % (
                task_id,
                status or "unknown",
            ),
        )
    context = getattr(checkpoint, "context", None) or {}
    actual_type = str(context.get("pending_gate_type") or "").strip().lower()
    if actual_type != gate_type:
        return (
            None,
            task_id,
            "Checkpoint %s is not a %s gate (type: %s)."
            % (
                task_id,
                gate_type,
                actual_type or "unknown",
            ),
        )
    return checkpoint, task_id, None


def _resolve_latest_payment_gate(
    user_id: str,
    *,
    allow_confirmed: bool = False,
) -> tuple[Any | None, Any | None, str, str | None]:
    """Return the current pending payment manager, session, and token for a user."""
    try:
        from intent.task_checkpoint import get_latest_waiting_checkpoint
        from services.payments.confirmation import (
            ConfirmationStatus,
            get_confirmation_manager,
        )

        mgr = get_confirmation_manager()
        token = ""
        checkpoint = get_latest_waiting_checkpoint(user_id, gate_type="payment")
        if checkpoint is not None:
            context = getattr(checkpoint, "context", None) or {}
            token = str(context.get("confirm_token") or "").strip()

        if not token:
            token = str(mgr.get_active_session_for_user(user_id) or "").strip()
        if not token:
            active_by_user = getattr(mgr, "_active_by_user", {})
            if isinstance(active_by_user, dict):
                token = str(active_by_user.get(user_id) or "").strip()
        if not token:
            return mgr, None, "", "No pending payment gate is waiting for this user."

        session = mgr.get_session(token)
        if session is None:
            return mgr, None, token, "Payment gate token is invalid or expired."
        if session.user_id != user_id:
            return mgr, None, token, "Payment gate token is not valid for this user."
        allowed_statuses = {ConfirmationStatus.PENDING}
        if allow_confirmed:
            allowed_statuses.add(ConfirmationStatus.CONFIRMED)
        if session.status not in allowed_statuses:
            return (
                mgr,
                None,
                token,
                "Payment gate is no longer pending (state: %s)." % session.status.value,
            )
        return mgr, session, token, None
    except Exception as exc:
        logger.exception("Failed to resolve latest payment gate for user")
        return None, None, "", "Could not resolve payment gate: %s" % exc


def _get_runtime_ai_controller() -> Any | None:
    """Best-effort lookup of the live AIController for checkpoint resume."""
    try:
        from ui.server import get_app

        app = get_app()
        state = getattr(app, "state", None) if app is not None else None
        candidates = [
            getattr(state, "intent_pipeline", None),
            getattr(state, "pipeline", None),
            getattr(getattr(state, "intent", None), "_pipeline", None),
        ]
        for pipeline in candidates:
            controller = getattr(pipeline, "ai_controller", None)
            if controller is not None and hasattr(controller, "_try_resume_checkpoint"):
                return controller
    except Exception:
        logger.debug("Runtime AIController lookup failed for gate resume")
    return None


_SIGNATURE_RESUME_CONSUMING_TOOLS = frozenset(
    {
        "browser_click",
        "browser_click_ref",
        "browser_fill_form",
        "browser_fill_ref",
        "browser_interact",
        "browser_press_key",
        "browser_run_script",
        "browser_evaluate",
        "browser_select",
        "browser_select_ref",
        "browser_type",
    }
)


def _normalize_tools_called(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    try:
        return [str(tool).strip() for tool in value if str(tool).strip()]  # type: ignore[union-attr]
    except TypeError:
        text = str(value).strip()
        return [text] if text else []


def _payload_tools_called(payload: dict[str, object]) -> tuple[list[str], bool]:
    if "tools_called" in payload and payload.get("tools_called") is not None:
        return _normalize_tools_called(payload.get("tools_called")), True
    data = payload.get("data")
    if isinstance(data, dict) and "tools_called" in data and data.get("tools_called") is not None:
        return _normalize_tools_called(data.get("tools_called")), True
    return [], False


def _signature_resume_failure_preserves_override(payload: dict[str, object]) -> bool:
    tools_called, has_tools_evidence = _payload_tools_called(payload)
    if not has_tools_evidence:
        return False
    return not any(tool in _SIGNATURE_RESUME_CONSUMING_TOOLS for tool in tools_called)


def _agent_result_to_gate_payload(agent_result: Any, *, task_id: str) -> str:
    ok = bool(getattr(agent_result, "ok", False))
    answer = str(getattr(agent_result, "answer", "") or "")
    error = str(getattr(agent_result, "error", "") or "")
    if not ok:
        raw_tools_called = getattr(agent_result, "tools_called", None)
        tool_fields = {}
        if raw_tools_called is not None:
            tool_fields["tools_called"] = _normalize_tools_called(raw_tools_called)
        return _gate_error(
            error or "Checkpoint %s resume failed." % task_id,
            task_id=task_id,
            answer=answer,
            **tool_fields,
        )
    return _gate_payload(
        True,
        task_id=task_id,
        message=answer,
        tools_called=getattr(agent_result, "tools_called", []) or [],
        payment_gate=bool(getattr(agent_result, "payment_gate", False)),
        signature_gate=bool(getattr(agent_result, "signature_gate", False)),
        confirmation_url=getattr(agent_result, "confirmation_url", None),
    )


async def _resume_checkpoint_from_runtime(task_id: str, user_id: str, resume_user_reply: str) -> str:
    """Resume a checkpoint through the live controller or active executor."""
    checkpoint = None
    consumed_gate_type = ""
    try:
        from intent.ai_controller import _resume_gate_type_from_checkpoint_context
        from intent.task_checkpoint import load_checkpoint

        checkpoint = load_checkpoint(task_id, user_id=user_id)
        consumed_gate_type = _resume_gate_type_from_checkpoint_context(getattr(checkpoint, "context", None))
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.debug(
            "Could not resolve consumed gate type before checkpoint resume",
            exc_info=True,
        )

    controller = _get_runtime_ai_controller()
    if controller is not None:
        result = await controller._try_resume_checkpoint(
            task_id,
            user_id=user_id,
            resume_user_reply=resume_user_reply,
        )
        if result is None:
            return _gate_error("Checkpoint %s could not be resumed." % task_id, task_id=task_id)
        ok = bool(result.get("ok", result.get("success", False))) if isinstance(result, dict) else False
        if not ok:
            result_data = result.get("data") if isinstance(result, dict) else None
            tool_fields = {}
            if isinstance(result_data, dict) and result_data.get("tools_called") is not None:
                tool_fields["tools_called"] = _normalize_tools_called(result_data.get("tools_called"))
            answer = ""
            if isinstance(result_data, dict):
                answer = str(result_data.get("answer") or "")
            return _gate_error(
                str(result.get("message") or result.get("error") or "Checkpoint resume failed."),
                task_id=task_id,
                answer=answer,
                **tool_fields,
            )
        return _gate_payload(
            True,
            task_id=task_id,
            message=str(result.get("message") or result.get("response") or ""),
            tools_called=(
                (result.get("data") or {}).get("tools_called", []) if isinstance(result.get("data"), dict) else []
            ),
            payment_gate=(
                bool((result.get("data") or {}).get("payment_gate")) if isinstance(result.get("data"), dict) else False
            ),
            signature_gate=(
                bool((result.get("data") or {}).get("signature_gate"))
                if isinstance(result.get("data"), dict)
                else False
            ),
            confirmation_url=(
                (result.get("data") or {}).get("confirmation_url") if isinstance(result.get("data"), dict) else None
            ),
        )

    try:
        from intent.agent_executor import AgentExecutor, get_active_executor

        active = get_active_executor(user_id=user_id)
        if active is None or getattr(active, "_user_id", None) != user_id:
            return _gate_error(
                "No active agent runtime is available to resume checkpoint %s." % task_id,
                task_id=task_id,
            )

        native_tools = getattr(active, "_native_tools", None)
        tool_surface = getattr(active, "_request_tool_surface", None)
        context_bundle = getattr(active, "_prompt_context_bundle", None)
        if consumed_gate_type in {"signature", "payment"}:
            try:
                from intent.ai_controller import _filter_active_resume_surface
                from services.llm.prompts import runtime_context_bundle

                native_tools, tool_surface = _filter_active_resume_surface(
                    native_tools,
                    tool_surface,
                    consumed_gate_type,
                )
                task_description = str(getattr(checkpoint, "task_description", "") or "").strip()
                context_bundle = runtime_context_bundle(
                    "RESUMING TASK: %s" % (task_description or task_id),
                    origin="task_resume",
                )
            except Exception:
                logger.exception(
                    "Failed to clean active %s resume surface for %s",
                    consumed_gate_type,
                    task_id,
                )
                return _gate_error(
                    "Checkpoint %s resume failed before tool execution." % task_id,
                    task_id=task_id,
                )

        agent_result = await AgentExecutor.resume_from_checkpoint(
            task_id=task_id,
            llm_caller=active._llm,
            approval_manager=active._approval,
            mcp_hub=active._mcp_hub,
            tts_speaker=active._tts,
            channel=active._channel,
            user_id=user_id,
            resume_user_reply=resume_user_reply,
            native_tools=native_tools,
            tool_surface=tool_surface,
            context_bundle=context_bundle,
        )
        return _agent_result_to_gate_payload(agent_result, task_id=task_id)
    except Exception as exc:
        logger.exception("Checkpoint resume failed for %s", task_id)
        return _gate_error("Checkpoint %s resume failed: %s" % (task_id, exc), task_id=task_id)


# ===========================================================================
# THINK TOOL (Anthropic "think" pattern — strategic reasoning scratchpad)
# ===========================================================================


@server.tool(
    description=(
        "Private reasoning scratchpad for planning multi-step tasks, weighing tradeoffs, or resolving ambiguity before acting. "
        "No side effects — returns your thought as a record."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def think(
    thought: Annotated[
        str,
        Field(
            description="Your private strategic reasoning before acting. Summarize the goal, options, missing information, risks, and next steps in plain text; do not claim results you have not observed."
        ),
    ],
) -> str:
    """Use this tool to think through your approach before acting.

    Call this BEFORE executing complex tasks to:
    - Assess the best way to accomplish the user's goal
    - Consider 2-3 realistic approaches and their tradeoffs
    - Identify what information you still need from the user
    - Plan your execution strategy

    This tool has no side effects — it just logs your reasoning.
    The thought is returned back to you as a record.
    """
    logger.info("Agent think: %s", thought[:200])
    return thought


# ===========================================================================
# FILESYSTEM TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Read from the local filesystem without making changes. "
        "Actions: list (browse directory), read (file contents), info (size/dates/permissions), search (glob filenames)."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def file_read(
    action: Annotated[
        str,
        Field(description="Filesystem read action. Use one of: 'list', 'read', 'info', or 'search'."),
    ] = "list",
    path: Annotated[
        str,
        Field(
            description="Filesystem path to inspect. Use a directory for 'list' and 'search', or a file path for 'read' and 'info'."
        ),
    ] = "~",
    offset: Annotated[
        int,
        Field(description="Zero-based line offset for action='read'."),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of lines to return for action='read'."),
    ] = 100,
    pattern: Annotated[
        str,
        Field(description="Filename glob pattern for action='search', such as '*.py' or 'README*'."),
    ] = "",
) -> str:
    """Read from the filesystem.

    Examples:
    - file_read(action="list", path="~/Documents")
    - file_read(action="read", path="~/notes/todo.txt", offset=0, limit=50)
    - file_read(action="info", path="~/notes/todo.txt")
    - file_read(action="search", path="~/Documents", pattern="*.pdf")
    """
    refusal = _local_surface_refusal("file_read")
    if refusal is not None:
        return refusal
    try:
        if action == "list":
            return _format_result(await _list_directory(path))
        if action == "read":
            return _format_result(await _read_file(path, offset, limit))
        if action == "info":
            return _format_result(await _file_info(path))
        if action == "search":
            return _format_result(await _search_files(pattern, path))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("file_read failed for action=%s path=%s", action, path)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Write or delete files on the local filesystem. Actions: write (create/overwrite entire file), delete (remove single file). "
        "Read first with file_read if you need to preserve existing content."
    ),
    annotations=_DANGEROUS,
    meta={
        "risk": "destructive",
        "irreversible_actions": ["delete"],
        "irreversible_class": "file_delete",
    },
)
async def file_write(
    action: Annotated[
        str,
        Field(description="Filesystem write action. Use one of: 'write' or 'delete'."),
    ] = "write",
    path: Annotated[
        str,
        Field(description="File path to write or delete. Deletion only supports single files, not directories."),
    ] = "",
    content: Annotated[
        str,
        Field(description="Full file contents for action='write'. This replaces the entire file."),
    ] = "",
) -> str:
    """Modify the filesystem.

    Examples:
    - file_write(action="write", path="~/notes/todo.txt", content="Buy milk")
    - file_write(action="delete", path="~/notes/old.txt")
    """
    refusal = _local_surface_refusal("file_write")
    if refusal is not None:
        return refusal
    try:
        if action == "write":
            return _format_result(await _write_file(path, content))
        if action == "delete":
            return _format_result(await _delete_file(path))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("file_write failed for action=%s path=%s", action, path)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# SHELL TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Execute a shell command and return stdout, stderr, and exit code. "
        "Last resort — prefer dedicated tools (file_read, web_search, media) over shell. "
        "30s default timeout, 4000 char output cap."
    ),
    annotations=_DANGEROUS,
    meta={"risk": "destructive"},
)
async def run_command(
    command: Annotated[
        str,
        Field(
            description="Shell command string to execute exactly as written. Include all arguments and quoting; use this only when a direct tool cannot accomplish the task."
        ),
    ],
    timeout: Annotated[
        float,
        Field(
            description="Maximum execution time in seconds before the command is terminated. Use a larger value for long-running commands; default is 30."
        ),
    ] = 30.0,
    working_directory: Annotated[
        str,
        Field(
            description="Working directory for the command. Leave empty to use the default process directory, or provide an absolute path to run elsewhere."
        ),
    ] = "",
) -> str:
    """Execute a shell command. Returns stdout, stderr, and exit code. 30s timeout, 4000 char output cap."""
    refusal = _local_surface_refusal("run_command")
    if refusal is not None:
        return refusal
    try:
        permission_mode = "bypassPermissions" if _call_shell_permission_granted() else "default"
        result = await _run_command(
            command,
            timeout,
            working_directory,
            permission_mode=permission_mode,
        )
        return _format_result(result)
    except Exception as exc:
        logger.exception("run_command failed for command_chars=%d", len(command or ""))
        return json.dumps({"error": str(exc)})


# ===========================================================================
# WEB TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Search engine that returns result snippets with stable result ids, titles, and URLs. "
        "Useful for public facts, prices, reviews, how-tos, current events, regulations, and unusual questions "
        "when no dedicated structured tool covers the request or when a structured result needs outside context. "
        "Use this before browser navigation when the correct source or URL is uncertain; inspect objective result fields such as tld_class, engine_consensus_count, domain_age_days, snippet_has_specific_data, https, and redirect_chain before choosing a URL. No aggregate trust score is provided - weigh the raw fields yourself. "
        "For medical or health questions, prefer trusted medical sources such as NIH, CDC, FDA, MedlinePlus, Mayo Clinic, major hospital systems, .gov, or .edu when available. "
        "Does not open pages, interact with sites, read account-only content, or replace structured tools for weather, calendar, music, memory, timers, or local state. "
        "When snippets are not enough, web_read can read a selected public result URL; browser tools are available for JavaScript, login, forms, commerce, and live interaction."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def web_search(
    query: Annotated[
        str,
        Field(
            description="Search query. Include the user's city for local searches, specific requirements (size, price, features) from the request, and the current year for time-sensitive info. Example: 'stainless steel built-in dishwasher under $600 quiet 2026 Lowe's Milwaukee'."
        ),
    ],
    category: Annotated[
        str,
        Field(
            description="Optional SearXNG category. Use 'general' by default; 'maps' for place/map searches; 'news' for news."
        ),
    ] = "general",
    page: Annotated[
        int,
        Field(description="SearXNG result page number, 1 or higher."),
    ] = 1,
    time_range: Annotated[
        str,
        Field(description="Optional freshness filter: day, week, month, or year."),
    ] = "",
) -> str:
    """Search the web for public information: facts, prices, reviews, comparisons, how-tos, current events, and advice.

    Results are snippets. They may not contain live stock availability,
    account-only data, checkout state, appointment slots, or full page text.
    Page-specific follow-up can use web_read for public text or browser tools
    when live page interaction is the better fit.
    """
    try:
        result = await _web_search(
            query,
            category=category,
            page=page,
            time_range=time_range or None,
        )
        return _format_result(result)
    except Exception as exc:
        logger.exception("web_search failed")
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Read content from a public HTTP/HTTPS URL as cleaned text. This is the primary reading tool after web_search for articles, news, blogs, documentation, reviews, product pages, and other fetchable public pages. "
        "Browser tools are better when the page needs JavaScript, login, forms, screenshots, account or commerce flow, or live interaction, or when web_read cannot extract enough content. "
        "Does not interact with sites, execute JavaScript-heavy flows, fill forms, or bypass access controls."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def web_read(
    url: Annotated[
        str,
        Field(description="HTTP or HTTPS URL to fetch and extract as cleaned page text."),
    ],
    max_chars: Annotated[
        int | None,
        Field(
            description="Maximum characters to return. Defaults to the configured web_read default and is capped by settings."
        ),
    ] = None,
    offset: Annotated[
        int,
        Field(description="Character offset for continuing long pages."),
    ] = 0,
) -> str:
    """Fetch and extract text from a public web page."""
    try:
        result = await _web_read(url, max_chars=max_chars, offset=offset)
        return _format_result(result)
    except Exception as exc:
        logger.exception("web_read failed for url=%s", url[:120])
        return json.dumps({"error": str(exc)})


# ===========================================================================
# SYSTEM TOOLS
# ===========================================================================


@server.tool(
    description=("Get current system information — CPU usage, RAM, disk space, and top processes."),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def system_info() -> str:
    """Get system information (CPU, RAM, disk usage, top processes)."""
    # Already classified UNAVAILABLE in the cloud manifest ("Server system
    # info is not a valid proxy for the user's desktop environment"), so this
    # is the execution-layer half of that same statement rather than a new
    # policy: the classification stops it being offered, this stops it being
    # answered if anything ever routes around the manifest.
    refusal = _local_surface_refusal("system_info")
    if refusal is not None:
        return refusal
    try:
        result = await _system_info()
        return _format_result(result)
    except Exception as exc:
        logger.exception("system_info failed")
        return json.dumps({"error": str(exc)})


# ===========================================================================
# EMAIL / GMAIL TOOLS
# ===========================================================================

# Internal send-email primitive. NOT exposed to the LLM (trimmed from the tool
# surface by ``_trim_legacy_core_tool_surface`` below). share_response is the
# single canonical send path; it calls ``gmail_send`` here as an implementation
# detail. Keeping the @server.tool decorator preserves the registered handler
# so legacy in-process callers continue to resolve, but the LLM never sees it.


@server.tool(
    description=(
        "Internal email primitive used by share_response; not for direct LLM selection. "
        "share_response(destination_kind='email') is the canonical send path."
    ),
    annotations=_DANGEROUS,
    meta={
        "risk": "destructive",
        "irreversible": True,
        "irreversible_class": "send_email",
    },
)
async def gmail_send(
    to: Annotated[str, Field(description="Recipient email address.")],
    subject: Annotated[str, Field(description="Email subject line.")],
    body: Annotated[str, Field(description="Email body text.")],
) -> str:
    """Send an email via the configured backend chain (Gmail OAuth → Resend → SMTP)."""
    try:
        from intent.tools.email import send_email as _send_email

        result = await _send_email(to, subject, body, user_id=_get_call_user_id())
        return _format_result(result)
    except Exception as exc:
        try:
            from auth.utils import mask_email as _mask_email

            recipient_for_log = _mask_email(to)
        except (ImportError, AttributeError, TypeError, ValueError):
            recipient_for_log = "[email redacted]"
        logger.exception("gmail_send failed for recipient=%s", recipient_for_log)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Gmail read-side stubs
#
# When the external Google Workspace MCP server (google-workspace) is connected,
# the richer compound ``gmail`` tool (action=search/read/send/draft/...) handles
# full Gmail functionality. These stubs give the agent discoverable, natural
# Gmail tool names even when Google Workspace is NOT configured, so:
#   1. The LLM stops falling back to ``tool_search`` for "check my email"-style
#      prompts and picks a Gmail tool by name (GSV-001, GSV-003, GSV-004).
#   2. The user gets a clear setup-flow message telling them how to connect
#      Google Workspace (not a generic "I don't know" answer).
#
# When Google Workspace IS connected, the LLM typically prefers the compound
# ``gmail`` tool (richer description / action catalog) — these stubs coexist
# harmlessly as a fallback surface.
#
# Handler signatures mirror the original gmail_tools.py removed in commit
# 56a5106f so the pre-existing test_core_mcp_tools.py::TestGmailReadTool and
# test_mcp_tool_smoke.py::test_gmail_read_smoke contracts still match
# (message_id: str required for gmail_read / gmail_draft_reply).
# ---------------------------------------------------------------------------


def _gmail_setup_payload(action: str, **metadata: object) -> dict[str, object]:
    """Standard structured setup payload for read-side Gmail stubs."""
    return {
        "configured": False,
        "offers_setup": True,
        "provider": "google-workspace",
        "service": "gmail",
        "setup_state": "not_configured",
        "required_connection": "google_workspace",
        "requested_action": action,
        **metadata,
        "setup_url": "/settings#google",
    }


async def _gmail_inbox(max_results: int, unread_only: bool) -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data=_gmail_setup_payload("inbox", max_results=int(max_results), unread_only=bool(unread_only)),
    )


async def _gmail_read(message_id: str) -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data=_gmail_setup_payload("read", message_id=message_id[:80]),
    )


async def _gmail_draft_reply(message_id: str, body: str) -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data=_gmail_setup_payload("draft_reply", message_id=message_id[:80], body_len=len(body or "")),
    )


async def _gmail_search(query: str, max_results: int) -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data=_gmail_setup_payload("search", query=query[:80], max_results=int(max_results)),
    )


async def _gmail_daily_summary() -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data=_gmail_setup_payload("daily_summary"),
    )


async def _google_workspace(action: str, query: str) -> object:
    """Module-level handler — returns ToolResult with setup payload (patchable in tests)."""
    from intent.tool_types import ToolResult

    return ToolResult(
        ok=True,
        data={
            "configured": False,
            "offers_setup": True,
            "provider": "google-workspace",
            "service": "google_workspace",
            "setup_state": "not_configured",
            "required_connection": "google_workspace",
            "action": action,
            "query": query[:200] if query else "",
            "setup_url": "/settings#google",
        },
    )


@server.tool(
    description=(
        "Read-side Gmail compound. Actions: inbox, read, search, daily_summary, draft_reply. "
        "Returns recent message metadata or setup instructions when Google Workspace is not connected. "
        "Use this only for explicit Gmail mailbox reads (inbox, search, draft replies). "
        "For sending email — including 'email that to X', 'send that recipe', or any new outbound "
        "message — use share_response, not gmail or browser webmail. "
        "share_response routes through Gmail OAuth when available and falls back to the configured "
        "system backend (Resend/SMTP); it is the single canonical send path."
    ),
    annotations=_DANGEROUS,
    meta={
        "risk": "destructive",
        "irreversible_actions": ["send", "send_draft"],
        "irreversible_class": "send_email",
    },
)
async def gmail(
    action: Annotated[
        str,
        Field(description="Gmail read action: inbox, read, search, daily_summary, or draft_reply."),
    ] = "inbox",
    message_id: Annotated[
        str,
        Field(description="Message ID for action='read' or action='draft_reply'."),
    ] = "",
    query: Annotated[
        str,
        Field(description="Gmail search query for action='search'."),
    ] = "",
    body: Annotated[
        str,
        Field(description="Draft body for action='draft_reply'."),
    ] = "",
    max_results: Annotated[
        int,
        Field(description="Maximum messages for action='inbox' or action='search'."),
    ] = 10,
    unread_only: Annotated[
        bool,
        Field(description="For action='inbox', only return unread messages."),
    ] = False,
) -> str:
    """Compound Gmail read-side fallback used when external Google Workspace is not configured."""
    action_key = (action or "inbox").strip().lower()
    try:
        if action_key == "inbox":
            return _format_result(await _gmail_inbox(max_results, unread_only))
        if action_key == "read":
            return _format_result(await _gmail_read(message_id))
        if action_key == "search":
            return _format_result(await _gmail_search(query, max_results))
        if action_key == "daily_summary":
            return _format_result(await _gmail_daily_summary())
        if action_key in {"draft", "draft_reply"}:
            return _format_result(await _gmail_draft_reply(message_id, body))
        if action_key == "send":
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "gmail.send is not the canonical send path. Use "
                        "share_response(destination_kind='email', destination='<address>', "
                        "content_source='last_assistant_message') — it routes through Gmail OAuth "
                        "when available and falls back to the configured system backend."
                    ),
                    "error_category": "TOOL_DEPRECATED",
                }
            )
        return json.dumps({"error": "Unknown Gmail action: %s" % action})
    except Exception as exc:
        logger.exception("gmail failed for action=%s", action_key[:40])
        return "Error: %s" % exc


@server.tool(
    description=(
        "Check the user's Gmail inbox for recent messages. "
        "Returns recent email metadata (sender, subject, date). "
        "If Google Workspace is not connected, returns a setup-flow message. "
        "Prefer the 'gmail' compound tool when it is available."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def gmail_inbox(
    max_results: Annotated[
        int,
        Field(description="Maximum number of recent messages to return (default 20, max 50)."),
    ] = 20,
    unread_only: Annotated[
        bool,
        Field(description="Only return unread messages."),
    ] = False,
) -> str:
    """Check the user's Gmail inbox."""
    try:
        result = await _gmail_inbox(max_results, unread_only)
        return _format_result(result)
    except Exception as exc:
        logger.exception("gmail_inbox failed")
        return "Error: %s" % exc


@server.tool(
    description=(
        "Read a specific Gmail message by its message ID. "
        "If Google Workspace is not connected, returns a setup-flow message. "
        "Prefer the 'gmail' compound tool with action='read' when it is available."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def gmail_read(
    message_id: Annotated[
        str,
        Field(description="The Gmail message ID to read."),
    ],
) -> str:
    """Read a Gmail message by ID."""
    try:
        result = await _gmail_read(message_id)
        return _format_result(result)
    except Exception as exc:
        logger.exception("gmail_read failed for message_id_chars=%d", len(message_id or ""))
        return "Error: %s" % exc


@server.tool(
    description=(
        "Draft a reply to a Gmail message. Creates a draft for review — does NOT send. "
        "If Google Workspace is not connected, returns a setup-flow message. "
        "Prefer the 'gmail' compound tool with action='draft' when it is available."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def gmail_draft_reply(
    message_id: Annotated[
        str,
        Field(description="The Gmail message ID to reply to."),
    ],
    body: Annotated[
        str,
        Field(description="Draft reply body text."),
    ],
) -> str:
    """Draft a reply to a Gmail message."""
    try:
        result = await _gmail_draft_reply(message_id, body)
        return _format_result(result)
    except Exception as exc:
        logger.exception(
            "gmail_draft_reply failed for message_id_chars=%d body_chars=%d",
            len(message_id or ""),
            len(body or ""),
        )
        return "Error: %s" % exc


@server.tool(
    description=(
        "Search Gmail messages by query (sender, subject, keyword, date range). "
        "Supports Gmail query syntax: from:name, subject:topic, has:attachment, after:2026/01/01, is:unread. "
        "If Google Workspace is not connected, returns a setup-flow message. "
        "Prefer the 'gmail' compound tool with action='search' when it is available."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def gmail_search(
    query: Annotated[
        str,
        Field(description="Gmail-style search query, e.g. 'from:amazon receipt' or 'subject:invoice'."),
    ],
    max_results: Annotated[
        int,
        Field(description="Maximum results to return (default 10, max 50)."),
    ] = 10,
) -> str:
    """Search Gmail messages."""
    try:
        result = await _gmail_search(query, max_results)
        return _format_result(result)
    except Exception as exc:
        logger.exception("gmail_search failed for query_chars=%d", len(query or ""))
        return "Error: %s" % exc


@server.tool(
    description=(
        "Summarize today's Gmail activity (total count, unread, key senders, important messages). "
        "If Google Workspace is not connected, returns a setup-flow message."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def gmail_daily_summary() -> str:
    """Summarize the user's Gmail activity for today."""
    try:
        result = await _gmail_daily_summary()
        return _format_result(result)
    except Exception as exc:
        logger.exception("gmail_daily_summary failed")
        return "Error: %s" % exc


@server.tool(
    description=(
        "Access Google Workspace services (Drive, Docs, Sheets, Slides) — search files, open documents, "
        "inspect spreadsheets, or enumerate folders. If Google Workspace is not connected, returns a setup-flow message. "
        "Prefer the dedicated compound tools (google_drive, google_docs, google_sheets) when they are available."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def google_workspace(
    action: Annotated[
        str,
        Field(
            description="Workspace action. Use one of: 'drive_search', 'docs_get', 'sheets_get', 'slides_get', 'list'."
        ),
    ] = "drive_search",
    query: Annotated[
        str,
        Field(description="Search terms or file/document identifier for the chosen action."),
    ] = "",
) -> str:
    """Access Google Workspace (Drive/Docs/Sheets/Slides)."""
    try:
        result = await _google_workspace(action, query)
        return _format_result(result)
    except Exception as exc:
        logger.exception("google_workspace failed for action=%s", action[:40])
        return "Error: %s" % exc


@server.tool(
    description=(
        "Compact desktop interaction wrapper. Actions: focus, click, type, key/hotkey, mouse_click, scroll. "
        "Delegates to the unified computer-use safety implementation."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def desktop_interact(
    action: Annotated[
        str,
        Field(description="Desktop action: focus, click, type, key, hotkey, mouse_click, or scroll."),
    ] = "focus",
    title: Annotated[str, Field(description="Window title substring for action='focus'.")] = "",
    window_title: Annotated[str, Field(description="Target window title for scoped click/type/key actions.")] = "",
    name: Annotated[str, Field(description="Accessibility element name for action='click'.")] = "",
    text: Annotated[str, Field(description="Text to type for action='type'.")] = "",
    keys: Annotated[
        str,
        Field(description="Key chord for action='key' or action='hotkey', such as ctrl+s."),
    ] = "",
    x: Annotated[int | None, Field(description="Physical x coordinate for action='mouse_click'.")] = None,
    y: Annotated[int | None, Field(description="Physical y coordinate for action='mouse_click'.")] = None,
    button: Annotated[
        str,
        Field(description="Mouse button for click actions: left, right, or middle."),
    ] = "left",
    direction: Annotated[str, Field(description="Scroll direction: up, down, left, or right.")] = "down",
    amount: Annotated[int, Field(description="Scroll amount for action='scroll'.")] = 5,
    respect_focus: Annotated[
        bool,
        Field(description="For action='type', preserve target-window focus discipline."),
    ] = True,
) -> str:
    """Compatibility wrapper for the compact desktop_interact MCP surface."""
    from mcp_servers.computer_use.server import computer as _computer

    action_key = (action or "focus").strip().lower()
    try:
        if action_key == "focus":
            return await _computer(action="focus_window", title=title or window_title)
        if action_key == "click":
            return await _computer(action="click", name=name, target_window=window_title)
        if action_key == "type":
            return await _computer(
                action="type",
                text=text,
                target_window=window_title,
                respect_focus=respect_focus,
            )
        if action_key in {"hotkey", "key"}:
            return await _computer(action="key", keys=keys, target_window=window_title)
        if action_key == "mouse_click":
            return await _computer(
                action="click",
                x=x,
                y=y,
                button=button,
                coordinate_mode="physical",
            )
        if action_key == "scroll":
            return await _computer(
                action="scroll",
                direction=direction,
                amount=amount,
                target_window=window_title,
            )
        return await _computer(action=action_key)
    except Exception as exc:
        logger.exception("desktop_interact failed for action=%s", action_key[:40])
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Send a message to the user's Telegram chat immediately, with HTML formatting support. "
        "This can be used during a live phone call when the caller asks you to notify the user. "
        "For ordinary notifications and reminders, use notify instead."
    ),
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible": True,
        "irreversible_class": "send_message",
    },
)
async def telegram_send(
    message: Annotated[
        str,
        Field(
            description="Message text to send to the user's Telegram chat. Supports HTML formatting understood by Telegram."
        ),
    ],
) -> str:
    """Send a message to the user via Telegram immediately.

    Delivers the message right now. For timed or scheduled reminders (e.g. "remind me
    at 7:00"), use schedule_create with the reminder as the action instead.

    Args:
        message: The text message to send now (supports HTML formatting).
    """
    try:
        result = await _telegram_send(message)
        return _format_result(result)
    except Exception as exc:
        logger.exception("telegram_send failed")
        return json.dumps({"error": str(exc)})


def _normalize_sms_phone_number(phone_number: str, *, field_name: str) -> str:
    digits = re.sub(r"\D", "", phone_number or "")
    if len(digits) == 10:
        digits = "1%s" % digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError("%s must be a US phone number: 10 digits, or the country code +1 and 10 digits." % field_name)

    normalized = "+%s" % digits
    from telephony.number_validation import validate_phone_number

    validation = validate_phone_number(normalized)
    if not validation.allowed:
        raise ValueError(validation.reason)
    return normalized


def _telnyx_sms_config_values() -> dict[str, str]:
    from config import env
    from config.settings import settings

    return {
        "api_key": (
            getattr(settings, "telnyx_api_key", None)
            or env.get("TELNYX_API_KEY")
            or env.get("VIOLA_TELNYX_API_KEY")
            or ""
        ).strip(),
        "phone_number": (
            getattr(settings, "telnyx_sms_from_number", None) or env.get("TELNYX_SMS_FROM_NUMBER") or ""
        ).strip(),
        "messaging_profile_id": (
            getattr(settings, "telnyx_messaging_profile_id", None)
            or env.get("TELNYX_MESSAGING_PROFILE_ID")
            or env.get("VIOLA_TELNYX_MESSAGING_PROFILE_ID")
            or ""
        ).strip(),
    }


def _sms_setup_payload(**metadata: object) -> dict[str, object]:
    config_values = _telnyx_sms_config_values()
    has_api_key = bool(config_values["api_key"])
    has_sender = bool(config_values["phone_number"])
    has_profile = bool(config_values["messaging_profile_id"])
    configured = has_api_key and has_sender and has_profile

    missing: list[str] = []
    if not has_api_key:
        missing.append("TELNYX_API_KEY or VIOLA_TELNYX_API_KEY")
    if not has_sender:
        missing.append("TELNYX_SMS_FROM_NUMBER")
    if not has_profile:
        missing.append("TELNYX_MESSAGING_PROFILE_ID or VIOLA_TELNYX_MESSAGING_PROFILE_ID")

    setup_state = "configured"
    if not configured:
        setup_state = "missing_configuration"
    if has_api_key and has_sender and not has_profile:
        setup_state = "messaging_profile_missing"

    return {
        "ok": True,
        "configured": configured,
        "offers_setup": True,
        "provider": "telnyx",
        "service": "sms",
        "setup_state": setup_state,
        "missing": missing,
        "local_configuration": {
            "telnyx_api_key": ("configured" if has_api_key else "missing"),  # pragma: allowlist secret
            "telnyx_sms_from_number": (config_values["phone_number"] if has_sender else ""),
            "telnyx_messaging_profile_id": "configured" if has_profile else "missing",
        },
        "required_env_vars": [
            "VIOLA_TELNYX_API_KEY or TELNYX_API_KEY",
            "TELNYX_SMS_FROM_NUMBER",
            "VIOLA_TELNYX_MESSAGING_PROFILE_ID or TELNYX_MESSAGING_PROFILE_ID",
        ],
        "founder_actions": [
            "Create or enable a Telnyx Messaging Profile.",
            "Assign the approved Viola Telnyx SMS toll-free number to that Messaging Profile.",
            "Set TELNYX_MESSAGING_PROFILE_ID or VIOLA_TELNYX_MESSAGING_PROFILE_ID, then restart Viola.",
            "Set TELNYX_SMS_FROM_NUMBER to the approved SMS toll-free number, then restart Viola.",
        ],
        "docs": [_TELNYX_SMS_SETUP_DOC],
        "telnyx_portal_urls": {
            "messaging": _TELNYX_MESSAGING_PORTAL_URL,
            "10dlc": _TELNYX_10DLC_PORTAL_URL,
        },
        "telnyx_docs": {
            "10dlc_assignment": _TELNYX_10DLC_DOC_URL,
            "error_codes": _TELNYX_ERROR_DOC_URL,
        },
        **metadata,
    }


def _extract_telnyx_errors(response_body: str) -> list[dict[str, object]]:
    if not response_body.strip():
        return []
    try:
        response_data = json.loads(response_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(response_data, dict):
        return []
    errors = response_data.get("errors")
    if not isinstance(errors, list):
        return []
    return [error for error in errors if isinstance(error, dict)]


def _telnyx_sms_failure_diagnostics(status_code: int, response_body: str) -> tuple[str, str, bool, str]:
    errors = _extract_telnyx_errors(response_body)
    codes = {str(error.get("code") or "").strip() for error in errors}
    haystack_parts: list[str] = [response_body]
    for error in errors:
        haystack_parts.extend(str(error.get(field) or "") for field in ("code", "title", "detail"))
    haystack = " ".join(haystack_parts).lower()

    if codes.intersection({"40010", "40300"}) or "10dlc" in haystack or "campaign registration" in haystack:
        return (
            _SMS_10DLC_UNREGISTERED,
            "Telnyx rejected the SMS because the sender is not assigned to an active 10DLC campaign.",
            True,
            "telnyx_10dlc_campaign_required",
        )
    if ("tollfree" in haystack or "toll-free" in haystack or "toll free" in haystack or "tfn" in haystack) and (
        "verification" in haystack or "verified" in haystack or "unverified" in haystack
    ):
        return (
            _SMS_TOLLFREE_UNVERIFIED,
            "Telnyx rejected the SMS because toll-free verification is not complete for the sender.",
            True,
            "telnyx_tollfree_verification_required",
        )
    if "40305" in codes or "messaging profile" in haystack:
        return (
            _SMS_UNCONFIGURED_SENDER,
            "Telnyx rejected the SMS because the sender number is not associated with a sending messaging profile.",
            True,
            "telnyx_sender_messaging_profile_required",
        )
    if "40100" in codes or "not messaging enabled" in haystack or "unauthorized" in haystack or "api key" in haystack:
        return (
            _SMS_UNCONFIGURED_SENDER,
            "Telnyx rejected the SMS because the sender or Telnyx credentials are not fully configured for messaging.",
            True,
            "telnyx_messaging_configuration_required",
        )
    if "account" in haystack and ("suspended" in haystack or "billing" in haystack):
        return (
            _SMS_GATEWAY_REJECTION,
            "Telnyx rejected the SMS because the Telnyx account needs attention.",
            True,
            "telnyx_account_attention_required",
        )
    return (
        _SMS_GATEWAY_REJECTION,
        "Telnyx rejected the SMS request. See telnyx_response_body for the provider's exact reason.",
        False,
        "telnyx_gateway_rejection",
    )


def _telnyx_message_detail_summary(response_body: str) -> dict[str, object]:
    try:
        response_data = json.loads(response_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(response_data, dict):
        return {}
    data = response_data.get("data")
    if not isinstance(data, dict):
        return {}

    recipients = data.get("to")
    recipient_statuses: list[str] = []
    if isinstance(recipients, list):
        for recipient in recipients:
            if isinstance(recipient, dict):
                status = str(recipient.get("status") or "").strip()
                if status:
                    recipient_statuses.append(status)

    errors = data.get("errors")
    if not isinstance(errors, list):
        errors = []

    return {
        "message_id": str(data.get("id") or ""),
        "messaging_profile_id": str(data.get("messaging_profile_id") or ""),
        "recipient_statuses": recipient_statuses,
        "errors": [error for error in errors if isinstance(error, dict)],
    }


def _telnyx_delivery_failed(message_detail_summary: dict[str, object]) -> bool:
    errors = message_detail_summary.get("errors")
    if isinstance(errors, list) and errors:
        return True
    statuses = message_detail_summary.get("recipient_statuses")
    if not isinstance(statuses, list):
        return False
    return any(str(status).strip().lower() in _TELNYX_DELIVERY_FAILURE_STATUSES for status in statuses)


async def _poll_telnyx_message_detail(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    message_id: str,
) -> tuple[int | None, str, dict[str, object]]:
    detail_status_code: int | None = None
    detail_body = ""
    detail_summary: dict[str, object] = {}
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Accept": "application/json",
    }

    for attempt in range(_TELNYX_DELIVERY_POLL_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_TELNYX_DELIVERY_POLL_INTERVAL_SECONDS)
        response = await client.get(
            "https://api.telnyx.com/v2/messages/%s" % message_id,
            headers=headers,
        )
        detail_status_code = response.status_code
        detail_body = response.text
        if response.is_error:
            logger.warning(
                "sms_send Telnyx message detail lookup failed: status=%s message_id=%s response_body=%s",
                response.status_code,
                message_id,
                detail_body,
            )
            return detail_status_code, detail_body, detail_summary

        detail_summary = _telnyx_message_detail_summary(detail_body)
        if _telnyx_delivery_failed(detail_summary):
            return detail_status_code, detail_body, detail_summary

        statuses = detail_summary.get("recipient_statuses")
        if isinstance(statuses, list) and statuses:
            normalized_statuses = {str(status).strip().lower() for status in statuses if str(status).strip()}
            if normalized_statuses and not normalized_statuses.issubset(_TELNYX_DELIVERY_PENDING_STATUSES):
                return detail_status_code, detail_body, detail_summary

        if attempt == _TELNYX_DELIVERY_POLL_ATTEMPTS - 1:
            return detail_status_code, detail_body, detail_summary

    return detail_status_code, detail_body, detail_summary


def _sms_error_payload(
    error: str,
    *,
    error_category: str,
    to: str = "",
    from_number: str = "",
    error_code: str = "",
    action: str = "",
    message_id: str = "",
    delivery_status: str = "",
    telnyx_status_code: int | None = None,
    telnyx_response_body: str = "",
    telnyx_errors: list[dict[str, object]] | None = None,
    setup_required: bool = False,
    setup_requirement: str = "",
) -> str:
    payload: dict[str, object] = {
        "ok": False,
        "sent": False,
        "provider": "telnyx",
        "error": error,
        "error_category": error_category,
        "error_type": error_category,
    }
    if error_code:
        payload["error_code"] = error_code
    if action:
        payload["action"] = action
    if to:
        payload["to"] = to
    if from_number:
        payload["from"] = from_number
    if message_id:
        payload["message_id"] = message_id
    if delivery_status:
        payload["delivery_status"] = delivery_status
    if telnyx_status_code is not None:
        payload["telnyx_status_code"] = telnyx_status_code
    if telnyx_response_body:
        payload["telnyx_response_body"] = telnyx_response_body
    if telnyx_errors is not None:
        payload["telnyx_errors"] = telnyx_errors
    if setup_required:
        payload["setup_required"] = True
        payload["setup_docs"] = [_TELNYX_SMS_SETUP_DOC]
        payload["telnyx_portal_urls"] = {
            "messaging": _TELNYX_MESSAGING_PORTAL_URL,
            "10dlc": _TELNYX_10DLC_PORTAL_URL,
        }
        if setup_requirement:
            payload["setup_requirement"] = setup_requirement
    return json.dumps(payload, default=str, ensure_ascii=False)


def _sms_paid_action_login_required_payload(*, to: str = "", from_number: str = "") -> str | None:
    try:
        from core.account_gate import (
            paid_action_login_required,
            paid_action_login_required_data,
        )
        from core.user_context import get_current_user_id, get_device_user_id

        try:
            user_id = get_current_user_id()
        except LookupError:
            # mt-ok: desktop pre-auth fallback for paid-action gate check
            user_id = get_device_user_id()
        if not paid_action_login_required(user_id):
            return None
        data = paid_action_login_required_data(
            action="sms_send",
            message="Sign in to send SMS through Viola.",
        )
    except Exception:
        logger.exception("SMS paid-action account gate failed closed")
        data = {
            "error_code": "login_required_for_paid_action",
            "message": "Sign in to send SMS through Viola.",
            "action": "sms_send",
        }
    return _sms_error_payload(
        str(data.get("message") or "Sign in to send SMS through Viola."),
        error_category="LOGIN_REQUIRED",
        error_code=str(data.get("error_code") or "login_required_for_paid_action"),
        action=str(data.get("action") or "sms_send"),
        to=to,
        from_number=from_number,
    )


async def _sms_owner_control_denial_payload(*, to: str = "", from_number: str = "") -> str | None:
    try:
        from core.user_context import get_current_user_id, get_device_user_id
        from services.operator_controls import require_enabled_async

        try:
            user_id = get_current_user_id()
        except LookupError:
            try:
                # mt-ok: desktop pre-auth fallback for owner safety control check
                user_id = get_device_user_id()
            except Exception:
                user_id = None
        decision = await require_enabled_async("sms_outbound", user_id=user_id, action="sms_send")
    except Exception:
        logger.exception("SMS owner safety control check failed closed")
        return _sms_error_payload(
            "This capability is temporarily paused while safety controls recover.",
            error_category="OWNER_SAFETY_CONTROL",
            error_code="owner_safety_control_unavailable",
            action="sms_send",
            to=to,
            from_number=from_number,
        )
    if decision.allowed:
        return None
    return _sms_error_payload(
        decision.public_message or "This capability is temporarily paused for safety.",
        error_category="OWNER_SAFETY_CONTROL",
        error_code="owner_safety_control_disabled",
        action="sms_send",
        to=to,
        from_number=from_number,
    )


async def _sms_phone_opt_out_denial_payload(*, to: str = "", from_number: str = "") -> str | None:
    try:
        normalized_recipient = _normalize_sms_phone_number(to, field_name="SMS recipient")
    except ValueError:
        return None
    try:
        from auth.database import get_auth_db

        db = get_auth_db()
        await db.initialize()
        if not await db.sms.is_phone_opted_out(normalized_recipient):
            return None
    except Exception:
        logger.exception("SMS phone opt-out check failed closed")
        return _sms_error_payload(
            "SMS opt-out status could not be verified.",
            error_category=_SMS_RECIPIENT_OPTED_OUT,
            error_code="sms_opt_out_check_unavailable",
            action="sms_send",
            to=normalized_recipient,
            from_number=from_number,
        )
    return _sms_error_payload(
        "SMS recipient has opted out.",
        error_category=_SMS_RECIPIENT_OPTED_OUT,
        error_code="sms_recipient_opted_out",
        action="sms_send",
        to=normalized_recipient,
        from_number=from_number,
    )


@server.tool(
    description=(
        "Inspect Telnyx SMS setup state without sending a message. Returns missing "
        "environment variables, Telnyx portal links, and founder-action setup steps."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def setup_sms_provider() -> str:
    """Return structured setup facts for Telnyx SMS."""
    return json.dumps(_sms_setup_payload(), default=str, ensure_ascii=False)


@server.tool(
    description=(
        "Send an SMS text message immediately via the configured Telnyx phone number. "
        "Use during phone calls when the caller has clearly requested a text message and "
        "the recipient phone number is known. If the recipient phone number is missing, "
        "ask one concise follow-up question instead of refusing."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "irreversible": True, "irreversible_class": "send_sms"},
)
async def sms_send(
    to: Annotated[
        str,
        Field(
            description="Recipient phone number, including the country code (e.g. +13125551234), or a US 10-digit phone number."
        ),
    ],
    message: Annotated[str, Field(description="SMS body text to send.")],
    from_number: Annotated[
        str,
        Field(description="Optional Telnyx sender number. Leave blank to use configured TELNYX_SMS_FROM_NUMBER."),
    ] = "",
) -> str:
    """Send an SMS text message via Telnyx."""
    try:
        owner_control_payload = await _sms_owner_control_denial_payload(to=to, from_number=from_number)
        if owner_control_payload is not None:
            return owner_control_payload

        config_values = _telnyx_sms_config_values()
        api_key = config_values["api_key"]
        sender = from_number.strip() or config_values["phone_number"]
        messaging_profile_id = config_values["messaging_profile_id"]
        body = (message or "").strip()

        if not api_key:
            return _sms_error_payload(
                "Telnyx API key is not configured. Set TELNYX_API_KEY or VIOLA_TELNYX_API_KEY.",
                error_category=_SMS_UNCONFIGURED_SENDER,
                to=to,
                setup_required=True,
                setup_requirement="telnyx_api_key_required",
            )
        if not sender:
            return _sms_error_payload(
                "Telnyx sender number is not configured. Set TELNYX_SMS_FROM_NUMBER or pass from_number.",
                error_category=_SMS_UNCONFIGURED_SENDER,
                to=to,
                setup_required=True,
                setup_requirement="telnyx_sender_number_required",
            )
        if not messaging_profile_id:
            return _sms_error_payload(
                "Telnyx messaging_profile_id is not configured for the sender number.",
                error_category=_SMS_UNCONFIGURED_SENDER,
                to=to,
                from_number=sender,
                setup_required=True,
                setup_requirement="telnyx_messaging_profile_required",
            )
        login_required_payload = _sms_paid_action_login_required_payload(to=to, from_number=sender)
        if login_required_payload is not None:
            return login_required_payload
        if not body:
            return _sms_error_payload(
                "SMS message body is required.",
                error_category=_SMS_GATEWAY_REJECTION,
                to=to,
                from_number=sender,
            )

        try:
            normalized_sender = _normalize_sms_phone_number(sender, field_name="SMS sender")
        except ValueError as exc:
            return _sms_error_payload(
                str(exc),
                error_category=_SMS_UNCONFIGURED_SENDER,
                to=to,
                from_number=sender,
                setup_required=True,
                setup_requirement="telnyx_sender_e164_required",
            )

        try:
            normalized_recipient = _normalize_sms_phone_number(to, field_name="SMS recipient")
        except ValueError as exc:
            return _sms_error_payload(
                str(exc),
                error_category=_SMS_INVALID_RECIPIENT_FORMAT,
                to=to,
                from_number=normalized_sender,
            )

        if normalized_sender != APPROVED_SMS_FROM_NUMBER:
            return _sms_error_payload(
                "SMS sender is not the approved Telnyx toll-free number.",
                error_category=_SMS_UNCONFIGURED_SENDER,
                to=normalized_recipient,
                from_number=normalized_sender,
                setup_required=True,
                setup_requirement="approved_sms_sender_required",
            )
        if normalized_recipient not in LAUNCH_SMS_ALLOWED_RECIPIENTS:
            return _sms_error_payload(
                "SMS launch gate allows sending only to the approved launch verification number.",
                error_category=_SMS_LAUNCH_RECIPIENT_BLOCKED,
                to=normalized_recipient,
                from_number=normalized_sender,
            )
        opt_out_payload = await _sms_phone_opt_out_denial_payload(
            to=normalized_recipient,
            from_number=normalized_sender,
        )
        if opt_out_payload is not None:
            return opt_out_payload

        payload: dict[str, object] = {
            "from": normalized_sender,
            "to": normalized_recipient,
            "text": body,
            "type": "SMS",
            "messaging_profile_id": messaging_profile_id,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.telnyx.com/v2/messages",
                headers={
                    "Authorization": "Bearer %s" % api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
            )
            response_body = response.text
            if response.is_error:
                (
                    error_category,
                    diagnostic_message,
                    setup_required,
                    setup_requirement,
                ) = _telnyx_sms_failure_diagnostics(
                    response.status_code,
                    response_body,
                )
                telnyx_errors = _extract_telnyx_errors(response_body)
                logger.error(
                    "sms_send Telnyx rejected message: status=%s to=%s from=%s response_body=%s",
                    response.status_code,
                    payload["to"],
                    payload["from"],
                    response_body,
                )
                return _sms_error_payload(
                    diagnostic_message,
                    error_category=error_category,
                    to=str(payload["to"]),
                    from_number=str(payload["from"]),
                    telnyx_status_code=response.status_code,
                    telnyx_response_body=response_body,
                    telnyx_errors=telnyx_errors,
                    setup_required=setup_required,
                    setup_requirement=setup_requirement,
                )
            response.raise_for_status()
            response_data = response.json()

            data = response_data.get("data", {}) if isinstance(response_data, dict) else {}
            message_id = data.get("id", "") if isinstance(data, dict) else ""
            delivery_status = ""
            if message_id:
                detail_status_code, detail_body, detail_summary = await _poll_telnyx_message_detail(
                    client,
                    api_key=api_key,
                    message_id=str(message_id),
                )
                recipient_statuses = detail_summary.get("recipient_statuses")
                if isinstance(recipient_statuses, list) and recipient_statuses:
                    delivery_status = ",".join(str(status) for status in recipient_statuses)
                if _telnyx_delivery_failed(detail_summary):
                    detail_errors = detail_summary.get("errors")
                    telnyx_errors = (
                        [error for error in detail_errors if isinstance(error, dict)]
                        if isinstance(detail_errors, list)
                        else []
                    )
                    (
                        error_category,
                        diagnostic_message,
                        setup_required,
                        setup_requirement,
                    ) = _telnyx_sms_failure_diagnostics(
                        detail_status_code or 0,
                        detail_body,
                    )
                    if error_category == _SMS_GATEWAY_REJECTION and delivery_status:
                        error_category = _SMS_CARRIER_BLOCKED
                        diagnostic_message = "Telnyx accepted the SMS, but carrier delivery later failed."
                        setup_required = False
                        setup_requirement = "carrier_delivery_failed"
                    logger.error(
                        "sms_send Telnyx delivery failed: message_id=%s delivery_status=%s response_body=%s",
                        message_id,
                        delivery_status,
                        detail_body,
                    )
                    return _sms_error_payload(
                        diagnostic_message,
                        error_category=error_category,
                        to=str(payload["to"]),
                        from_number=str(payload["from"]),
                        message_id=str(message_id),
                        delivery_status=delivery_status,
                        telnyx_status_code=detail_status_code,
                        telnyx_response_body=detail_body,
                        telnyx_errors=telnyx_errors,
                        setup_required=setup_required,
                        setup_requirement=setup_requirement,
                    )

            return json.dumps(
                {
                    "ok": True,
                    "sent": True,
                    "provider": "telnyx",
                    "message_id": message_id,
                    "to": payload["to"],
                    "delivery_status": delivery_status,
                },
                default=str,
                ensure_ascii=False,
            )
    except ValueError as exc:
        logger.warning("sms_send unavailable: %s", exc)
        return _sms_error_payload(
            str(exc),
            error_category=_SMS_UNCONFIGURED_SENDER,
            to=to,
            setup_required=True,
            setup_requirement="telnyx_sms_setup_required",
        )
    except httpx.HTTPError as exc:
        logger.exception(
            "sms_send Telnyx HTTP request failed for to=%s",
            to[:32] if isinstance(to, str) else "",
        )
        return _sms_error_payload(
            str(exc),
            error_category=_SMS_GATEWAY_REJECTION,
            to=to,
        )
    except Exception as exc:
        logger.exception("sms_send failed for to=%s", to[:32] if isinstance(to, str) else "")
        return _sms_error_payload(str(exc), error_category=_SMS_GATEWAY_REJECTION, to=to)


@server.tool(
    description=(
        "Deliver or save content from the previous assistant response, explicit text, or a short transcript. "
        "For email, this uses connected Gmail OAuth when available and otherwise uses Viola's configured "
        "Resend/SMTP sender. "
        "content_source='last_assistant_message' selects the prior answer; 'explicit_text' selects "
        "the exact content supplied in explicit_text; 'last_n_messages' selects a short transcript. "
        "destination_kind must be sms, email, or file. For sms/email, destination is the phone number or "
        "email address. If the user says 'my email' without an address, leave destination blank; "
        "the tool will use the account owner's email. For file, destination is an optional file name or "
        "title; the saved path is always scoped under data/exports/<user_id>/."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "irreversible": True},
)
async def share_response(
    destination_kind: Annotated[
        str,
        Field(description="Delivery channel: sms, email, or file. Aliases like text, mail, and save are accepted."),
    ],
    destination: Annotated[
        str,
        Field(
            description=(
                "Phone number for sms, email address for email, or optional file name/title for file saves. "
                "For email, leave blank when the user asks for 'my email' and did not provide an address. "
                "File saves are always written under data/exports/<user_id>/."
            )
        ),
    ] = "",
    content_source: Annotated[
        str,
        Field(
            description=(
                "Content to share: last_assistant_message for the prior Viola answer, explicit_text for the "
                "explicit_text argument, or last_n_messages for a compact transcript."
            )
        ),
    ] = "last_assistant_message",
    explicit_text: Annotated[
        str,
        Field(description="Exact content to share when content_source='explicit_text'. Leave blank otherwise."),
    ] = "",
    title: Annotated[
        str,
        Field(description="Optional title used for email subject and file-name slug."),
    ] = "",
    file_format: Annotated[
        str,
        Field(description="For destination_kind='file', use md by default or txt when the user explicitly asks."),
    ] = "md",
    last_n_messages: Annotated[
        int,
        Field(description="For content_source='last_n_messages', number of recent messages to include, max 20."),
    ] = 4,
) -> str:
    """Share the prior assistant output via SMS, email, or user-scoped file export."""

    user_id = _resolve_share_user_id()
    if not user_id:
        return _share_error(
            "user_id is required for share_response; no per-user scope was provided.",
            destination_kind=destination_kind,
            destination=destination,
        )

    kind = _canonical_share_destination_kind(destination_kind)
    if kind not in _SHARE_DESTINATION_KINDS:
        return _share_error(
            "destination_kind must be sms, email, or file.",
            destination_kind=destination_kind,
            destination=destination,
        )

    content, content_error = _resolve_share_content(
        user_id=user_id,
        content_source=content_source,
        explicit_text=explicit_text,
        last_n_messages=last_n_messages,
    )
    if content_error:
        return _share_error(
            content_error,
            destination_kind=kind,
            destination=destination,
            content_source=content_source,
        )

    destination_text = (destination or "").strip()
    title_text = (title or _title_from_content(content)).strip()
    content_length = len(content)

    if kind == "sms":
        if not destination_text:
            return _share_error(
                "SMS destination phone number is required.",
                destination_kind=kind,
                content_source=content_source,
                content_length=content_length,
            )
        raw_result = await sms_send(to=destination_text, message=content)
        delivery = _parse_tool_payload(raw_result)
        if "error" in delivery:
            return _share_error(
                str(delivery.get("error") or "SMS send failed."),
                destination_kind=kind,
                destination=destination_text,
                content_source=content_source,
                content_length=content_length,
                delivery=delivery,
            )
        return _share_payload(
            True,
            status="sent",
            destination_kind=kind,
            destination=str(delivery.get("to") or destination_text),
            content_source=content_source,
            content_length=content_length,
            delivery=delivery,
        )

    if kind == "email":
        destination_source = "explicit"
        if not destination_text:
            destination_text = await _resolve_share_account_email(user_id)
            destination_source = "account_email" if destination_text else "missing"
            if not destination_text:
                return _share_error(
                    "Email destination address is required because no account email is available.",
                    destination_kind=kind,
                    content_source=content_source,
                    content_length=content_length,
                    destination_source=destination_source,
                )
        subject = title_text or "Shared from Viola"
        raw_result = await gmail_send(to=destination_text, subject=subject, body=content)
        delivery = _parse_tool_payload(raw_result)
        if "error" in delivery or delivery.get("configured") is False:
            return _share_error(
                str(delivery.get("error") or delivery.get("message") or "Email send failed."),
                destination_kind=kind,
                destination=destination_text,
                content_source=content_source,
                content_length=content_length,
                delivery=delivery,
                destination_source=destination_source,
            )
        return _share_payload(
            True,
            status="sent",
            destination_kind=kind,
            destination=destination_text,
            destination_source=destination_source,
            subject=subject,
            content_source=content_source,
            content_length=content_length,
            delivery=delivery,
        )

    export_path = _resolve_share_export_path(
        user_id=user_id,
        destination=destination_text,
        title=title_text,
        file_format=file_format,
        content=content,
    )
    write_result = await _write_file(str(export_path), content)
    if not getattr(write_result, "ok", False):
        return _share_error(
            getattr(write_result, "error", "") or "File save failed.",
            destination_kind=kind,
            destination=str(export_path),
            content_source=content_source,
            content_length=content_length,
        )
    return _share_payload(
        True,
        status="saved",
        destination_kind=kind,
        destination=str(export_path),
        path=str(export_path),
        content_source=content_source,
        content_length=content_length,
    )


# ===========================================================================
# SELF-MANAGEMENT TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Manage Viola's local tooling and read her own update status. "
        "Actions: update_status (offline version report with manual reinstall guidance), "
        "setup_codex, install_package (pip/npm). "
        "install_package is an extension path — Viola can grow her own dependencies. "
        "Prefer proposing an install over declining when a missing dependency is the "
        "only thing preventing a reasonable task. The user confirms before anything runs. "
        "Note: the agent does NOT apply Viola updates — update_status does not call a manifest; "
        "users reinstall manually from useviola.com."
    ),
    annotations=_DANGEROUS,
    meta={"risk": "destructive"},
)
async def self_manage(
    action: Annotated[
        str,
        Field(
            description=(
                "Self-management action. Use one of: 'update_status', "
                "'setup_codex', or 'install_package'. Legacy aliases "
                "'check_updates' and 'update' both route to the read-only "
                "'update_status' — the agent cannot apply updates."
            )
        ),
    ] = "update_status",
    package: Annotated[
        str,
        Field(description="Package name for action='install_package'."),
    ] = "",
    manager: Annotated[
        str,
        Field(description="Package manager for action='install_package', typically 'pip' or 'npm'."),
    ] = "pip",
    confirmed: Annotated[
        bool,
        Field(
            description=(
                "Required True to actually run the confirmed step for "
                "action='setup_codex' (writing VIOLA_CODEX_ENABLED=true to .env) or "
                "action='install_package' (installing an unrecognized package). "
                "False (default) returns a confirmation_required preview describing "
                "what would happen; call again with confirmed=True to proceed."
            )
        ),
    ] = False,
) -> str:
    """Manage Viola and local tooling.

    Examples:
    - self_manage(action="update_status")
    - self_manage(action="setup_codex")
    - self_manage(action="setup_codex", confirmed=True)
    - self_manage(action="install_package", package="requests", manager="pip", confirmed=True)
    """
    try:
        # Legacy aliases — old prompts/transcripts using "check_updates" or
        # "update" degrade to the read-only status check. The dangerous
        # git-pull path is deliberately gone (S10-UPDATE-001).
        if action in ("update_status", "check_updates", "update"):
            return _format_result(await _update_status())
        if action == "setup_codex":
            return _format_result(await _setup_codex(confirmed=confirmed))
        if action == "install_package":
            return _format_result(await _install_package(package, manager, confirmed=confirmed))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("self_manage failed for action=%s", action)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Manage MCP server registrations at runtime. "
        "Actions: list (show connected servers), register (add a new MCP server without restart). "
        "register is an extension path — Viola can grow her own tool surface by connecting "
        "third-party MCP servers. Prefer proposing a registration over declining when the user "
        "wants a capability that doesn't exist in the current tool set. Use judgement about "
        "which server is appropriate; the user confirms before connection and tools appear "
        "immediately with no restart. Desktop-only — cloud mode rejects."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def mcp_servers(
    action: Annotated[
        str,
        Field(description="MCP server action. Use one of: 'list' or 'register'."),
    ] = "list",
    name: Annotated[
        str,
        Field(description="Unique server name for action='register'."),
    ] = "",
    command: Annotated[
        str,
        Field(description="Executable command for action='register', such as 'python', 'node', or 'uvx'."),
    ] = "",
    args: Annotated[
        list[str] | None,
        Field(description="Optional argument list for action='register'."),
    ] = None,
    transport: Annotated[
        str,
        Field(description="Transport for action='register'. Use 'stdio'."),
    ] = "stdio",
) -> str:
    """Manage MCP server registrations.

    Examples:
    - mcp_servers(action="list")
    - mcp_servers(action="register", name="my-server", command="uvx", args=["my-mcp-package"])
    """
    try:
        if action == "list":
            return _format_result(await _list_mcp_servers())
        if action == "register":
            return _format_result(await _register_mcp_server(name, command, args, transport))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("mcp_servers failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# USER-AUTHORED ROUTINES
# ===========================================================================


class _UserCapabilityTriggerInput(BaseModel):
    """Strict MCP input shape for a phrase-triggered routine trigger."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["phrase"] = "phrase"
    phrases: list[str] = Field(
        default_factory=list,
        description="Trigger phrases that should run the shortcut.",
    )


class _UserCapabilityActionInput(BaseModel):
    """Strict MCP input shape for one routine action."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["call_capability", "summarize"] = "call_capability"
    name: str = Field(
        default="",
        description="MCP tool name for type='call_capability'.",
    )
    args_json: str = Field(
        default="{}",
        description="JSON object string containing fixed arguments for the called tool.",
    )
    style: str = Field(
        default="concise",
        description="Summary style for type='summarize'.",
    )


class _UserCapabilitySpecInput(BaseModel):
    """Strict MCP input shape for routine create/update specs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str | None = None
    name: str = ""
    description: str = ""
    trigger: _UserCapabilityTriggerInput | None = None
    actions: list[_UserCapabilityActionInput] = Field(default_factory=list)
    required_tier: Literal["solo", "ensemble", "symphony"] | None = None
    disabled: bool = False
    created_by: Literal["viola", "user"] = "viola"
    created_at: str | None = None


def _parse_json_object_string(value: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("JSON value must be an object.")
    return parsed


def _normalize_user_capability_spec(
    spec: _UserCapabilitySpecInput | dict[str, Any] | str | None,
) -> dict[str, Any]:
    """Convert strict MCP input into the existing routine service shape."""

    if spec is None:
        return {}
    if isinstance(spec, BaseModel):
        raw: dict[str, Any] = spec.model_dump(mode="json", exclude_none=True)
    elif isinstance(spec, str):
        raw = _parse_json_object_string(spec)
    elif isinstance(spec, dict):
        raw = dict(spec)
    else:
        return {}

    actions: list[dict[str, Any]] = []
    for action in raw.get("actions") or []:
        if isinstance(action, BaseModel):
            item = action.model_dump(mode="json", exclude_none=True)
        elif isinstance(action, dict):
            item = dict(action)
        else:
            continue
        action_type = str(item.get("type") or "call_capability")
        args_json = item.pop("args_json", None)
        if action_type == "call_capability" and "args" not in item:
            try:
                item["args"] = _parse_json_object_string(str(args_json or "{}"))
            except (json.JSONDecodeError, TypeError, ValueError):
                item["args"] = {}
        if action_type == "summarize":
            item.pop("name", None)
            item.pop("args", None)
        actions.append({key: value for key, value in item.items() if value is not None})
    if actions:
        raw["actions"] = actions

    return {key: value for key, value in raw.items() if value is not None}


@server.tool(
    description=(
        "Author user-scoped shortcuts as declarative compositions that can be created, listed, updated, "
        "disabled, enabled, or deleted after confirmation. When you notice the user repeatedly asking for "
        "the same workflow, or when the user explicitly wants a recurring task, propose creating a shortcut "
        "here. Creating a shortcut reports phrase conflicts and deterministic command interception risks. "
        "Confirm before saving."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def user_capabilities(
    action: Annotated[
        str,
        Field(description="Shortcut action. Use one of: 'create', 'list', 'update', 'toggle', or 'delete'."),
    ] = "list",
    spec: Annotated[
        _UserCapabilitySpecInput | str | None,
        Field(description="Shortcut spec for create or update, or a JSON object string."),
    ] = None,
    id: Annotated[
        str,
        Field(description="Shortcut id for update, toggle, or delete."),
    ] = "",
    disabled: Annotated[
        bool,
        Field(description="Whether the shortcut should be disabled for action='toggle'."),
    ] = False,
) -> str:
    """Manage phrase-triggered user routines."""
    try:
        normalized_action = (action or "list").strip().lower()
        normalized_spec = _normalize_user_capability_spec(spec)
        if normalized_action == "create":
            return _format_result(await _call_with_current_user_context(_create_user_capability, normalized_spec))
        if normalized_action == "list":
            return _format_result(await _call_with_current_user_context(_list_user_capabilities))
        if normalized_action == "update":
            return _format_result(await _call_with_current_user_context(_update_user_capability, id, normalized_spec))
        if normalized_action == "toggle":
            return _format_result(await _call_with_current_user_context(_toggle_user_capability, id, disabled))
        if normalized_action == "delete":
            return _format_result(await _call_with_current_user_context(_delete_user_capability, id))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("user_capabilities failed for action=%s", action)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Load one of the current user's custom shortcuts/routines and return a structured action plan. "
        "Disabled shortcuts and shortcuts unavailable on the current surface or tier return a clear reason. "
        "This does not execute tools; the returned plan is data for model-side evaluation."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def run_user_capability(
    id: Annotated[
        str,
        Field(description="Routine id to load and plan."),
    ],
) -> str:
    """Return the action plan for a saved routine."""
    try:
        return _format_result(await _call_with_current_user_context(_run_user_capability, id))
    except Exception as exc:
        logger.exception("run_user_capability failed for id=%s", id)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# DELEGATION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Delegate a task to an external compute provider (e.g. Codex) for deep reasoning or code generation. "
        "The provider has no conversation access — include all context in task/context fields."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def delegate_to_provider(
    task: Annotated[
        str,
        Field(
            description="Self-contained task description for the external provider. Include the goal, required output, constraints, and any critical context the provider will need."
        ),
    ],
    provider: Annotated[
        str,
        Field(
            description="Provider selection. Use 'auto' for automatic routing, or a specific configured provider name when the user requested one."
        ),
    ] = "auto",
    context: Annotated[
        str,
        Field(
            description="Optional supporting context for the delegated task, such as prior findings, relevant files, or user preferences."
        ),
    ] = "",
) -> str:
    """Delegate a complex task to an external compute provider (e.g. OpenAI Codex)."""
    try:
        result = await _delegate_to_provider(task, provider, context)
        return _format_result(result)
    except Exception as exc:
        logger.exception("delegate_to_provider failed for task=%s", task[:80])
        return json.dumps({"error": str(exc)})


# ===========================================================================
# AGENT DELEGATION & TASK MEMORY TOOLS
# ===========================================================================

# Module-level callback set by the agent executor at the start of each run.
# When not set (tool called outside agent mode), spawn_subtask returns an error.
_spawn_subtask_callback: ContextVar[object | None] = ContextVar("_spawn_subtask_cb", default=None)


def set_spawn_subtask_callback(callback: object | None) -> Token[object | None]:
    """Register (or clear) the child-agent callback.

    Called by ``AgentExecutor`` before starting the agent loop.
    """
    return _spawn_subtask_callback.set(callback)


def reset_spawn_subtask_callback(token: Token[object | None]) -> None:
    """Restore the previous child-agent callback for the current async context."""
    _spawn_subtask_callback.reset(token)


@server.tool(
    description=(
        "Delegate a subtask to an independent child agent with its own browser and tools. "
        "Child has no access to your conversation — include all context in the task description. "
        "Use for independent parallel work; skip if the task needs fewer than 3 tool calls."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def spawn_subtask(
    task: Annotated[
        str,
        Field(
            description="Standalone task description for the child agent. Include all necessary context, expected output, and any constraints because the child does not share your full reasoning state."
        ),
    ],
    return_format: Annotated[
        str,
        Field(
            description="Desired response format from the child. Use 'summary' for a concise write-up or 'data' for structured or raw output."
        ),
    ] = "summary",
) -> str:
    """Delegate a subtask to an independent child agent with its own browser and context.

    Args:
        task: Self-contained description with ALL context the child needs.
        return_format: "summary" or "data".
    """
    callback = _spawn_subtask_callback.get(None)
    if callback is None:
        return json.dumps({"error": "spawn_subtask is only available during agent execution"})
    try:
        result = await callback(task, 0, return_format)  # type: ignore[misc]
        return str(result)
    except Exception as exc:
        logger.exception("spawn_subtask failed for task=%s", task[:80])
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Run up to 2 independent subtasks in parallel, each with its own child agent and tools. "
        "Returns a JSON array of results."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def spawn_parallel_subtasks(
    tasks: Annotated[
        list[ParallelSubtaskSpec],
        Field(
            description=(
                "List of subtask objects, each with a 'task' field (string description). "
                "Example: [{'task': 'search for X'}, {'task': 'look up Y'}]"
            ),
        ),
    ],
) -> str:
    """Run independent subtasks concurrently via child agents."""
    import asyncio as _asyncio

    callback = _spawn_subtask_callback.get(None)
    if callback is None:
        return json.dumps({"error": "spawn_parallel_subtasks is only available during agent execution"})
    if not tasks:
        return json.dumps({"error": "No tasks provided"})
    if len(tasks) > 2:
        return json.dumps({"error": "Maximum 2 parallel subtasks allowed"})

    async def _run_one(spec: ParallelSubtaskSpec | dict) -> dict:
        task_text = spec.task if isinstance(spec, ParallelSubtaskSpec) else str(spec.get("task", ""))
        try:
            result = await callback(task_text, 0, "summary")  # type: ignore[misc]
            return {"task": task_text[:80], "ok": True, "data": str(result)}
        except Exception as exc:
            return {"task": task_text[:80], "ok": False, "error": str(exc)}

    results = await _asyncio.gather(*[_run_one(t) for t in tasks], return_exceptions=False)
    return json.dumps(results, ensure_ascii=False, default=str)


@server.tool(
    description=(
        "start_agent(description, prompt, subagent_type, model, name, team_name, mode) launches an async subagent. "
        "Omit subagent_type to fork yourself — a fork inherits your full conversation context and tool array. "
        "Provide prompt as the exact task and description as the short reason/label. "
        "The optional mode selects the child's context model: 'fork' (inherit parent) or 'fresh' (clean slate). "
        "All launches return immediately with an agent_id; completion arrives through task notifications. "
        "check_agents returns status for prior background work."
    ),
    annotations=_SAFE,
    # F-014 (R3-C): ``start_agent`` must remain visible on turn one so the
    # model can fork before reaching ToolSearch. Claude's TS
    # ``tools/ToolSearchTool/prompt.ts:73-81`` carves the Agent tool out of
    # deferral specifically for fork parity — the equivalent here is the
    # ``anthropic/alwaysLoad`` _meta marker honored by
    # ``intent/tools/deferred_tool_schemas.is_deferred_tool``. Without this,
    # bulk-defer mode (any tool list above the threshold with no explicit
    # flags) hides start_agent behind ToolSearch — the opposite of Claude's
    # fork-first contract.
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def start_agent(
    description: Annotated[
        str,
        Field(description="Short task description or reason shown to the parent agent and task UI."),
    ],
    prompt: Annotated[
        str,
        Field(
            description="Self-contained prompt for the subagent. Include all context the agent needs to complete the task."
        ),
    ],
    name: Annotated[
        str,
        Field(
            description="Optional stable name so later send_message calls can address this agent while it is running."
        ),
    ] = "",
    team_name: Annotated[
        str,
        Field(description="Optional team/group name for task UI parity. Does not affect routing."),
    ] = "",
    subagent_type: Annotated[
        Literal["", "default", "Explore", "Order_executor", "Phone_caller", "Research"],
        Field(
            description=(
                SUBAGENT_TYPE_FIELD_DESCRIPTION
                + " Leave empty (default) to fork the parent agent: forks inherit "
                + "the parent's full conversation context and tool array."
            )
        ),
    ] = "",
    mode: Annotated[
        Literal["", "fresh", "fork"],
        Field(
            description=(
                # F-011 (R3-C): mode is the optional fresh/fork context selector,
                # NOT a permission-mode label. The executor at
                # ``intent/agent_executor.py:_handle_start_agent`` consumes this
                # field solely as fresh-vs-fork dispatch (Claude TS parity:
                # ``tools/AgentTool/forkSubagent.ts:21``,
                # ``tools/AgentTool/AgentTool.tsx:318-323``). Permission policy
                # is enforced separately at the hub/agent layer and is not
                # selectable from this field.
                "Optional context selector for the child agent. "
                "Use 'fork' to inherit the parent's full conversation context "
                "and tool array, or 'fresh' for a clean slate with no inherited "
                "context. Omit (default '') to follow the subagent_type contract: "
                "an empty subagent_type implies fork. This field is NOT a "
                "permission-mode label — permissions are enforced separately."
            )
        ),
    ] = "",
    run_in_background: Annotated[
        bool,
        Field(
            description=(
                "Deprecated compatibility field. Ignored by the runtime: every start_agent call launches async "
                "background work and returns immediately with an agent_id."
            ),
            deprecated=True,
        ),
    ] = False,
    model: Annotated[
        str,
        Field(
            description=(
                "Optional model override for this subagent. Leave empty to inherit the configured subagent/default model."
            )
        ),
    ] = "",
    task: Annotated[
        str,
        Field(description="Legacy alias for prompt. Prefer prompt."),
    ] = "",
    reason: Annotated[
        str,
        Field(description="Legacy alias for description. Prefer description."),
    ] = "",
) -> str:
    """Launch a subagent task. Intercepted by AgentExecutor."""
    del team_name
    return json.dumps({"error": "start_agent is only available during agent execution"})


@server.tool(
    description=(
        "List the user's currently running and recently completed background agents. "
        "Use when the user references prior work (e.g. 'cancel the email task') and "
        "you need to find the right agent_id. Returns active and recent_completed lists."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def check_agents() -> str:
    """List background agents for this session. Intercepted by AgentExecutor."""
    return json.dumps({"error": "check_agents is only available during agent execution"})


@server.tool(
    description=(
        "Cancel a running background agent by its agent_id. "
        "Use when the user wants to stop in-flight work (e.g. 'cancel that' or "
        "'never mind on the haircut'). The agent stops at its next safe checkpoint."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def cancel_agent(
    agent_id: Annotated[
        str,
        Field(description="The short agent_id returned by start_agent or visible in check_agents."),
    ],
) -> str:
    """Cancel a running background agent. Intercepted by AgentExecutor."""
    return json.dumps({"error": "cancel_agent is only available during agent execution"})


@server.tool(
    description=(
        "Send a message to a background agent by id or by the optional name supplied to start_agent. "
        "If the agent is running, the message is queued for its next model turn. If the agent has stopped "
        "but has a transcript, it is resumed in the background with this message."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def send_message(
    to: Annotated[
        str,
        Field(description="Agent id returned by start_agent, or the optional name supplied when it was started."),
    ],
    message: Annotated[
        str,
        Field(description="Plain text message to deliver to or resume the target agent with."),
    ],
    summary: Annotated[
        str,
        Field(description="Optional 5-10 word preview for logs/UI; delivery uses the full message."),
    ] = "",
) -> str:
    """Send a message to a background agent. Intercepted by AgentExecutor."""
    return json.dumps({"error": "send_message is only available during agent execution"})


@server.tool(
    description=(
        "Check for paused or interrupted tasks that can be resumed. "
        "Returns up to 5 recent in-progress or waiting tasks, optionally filtered by query keywords."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def check_pending_tasks(
    query: Annotated[
        str,
        Field(
            description="Optional text filter for task descriptions. Leave empty to list recent resumable tasks, or provide keywords to narrow the results."
        ),
    ] = "",
) -> str:
    """Check for paused or interrupted tasks that can be resumed.

    Args:
        query: Optional search terms to filter tasks.
    """
    try:
        from intent import task_checkpoint as checkpoints

        checkpoint_dir = checkpoints.CHECKPOINT_DIR
        if not checkpoint_dir.exists():
            return "No pending tasks found."

        user_id = _require_call_user_id("check_agents")

        candidate_paths = []
        user_dir = checkpoint_dir / user_id
        if user_dir.exists():
            candidate_paths.extend(user_dir.glob("*.json"))
        candidate_paths.extend(checkpoint_dir.glob("*.json"))
        enc = checkpoints._get_checkpoint_encryption()
        results = []
        for path in sorted(
            set(candidate_paths),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                data = checkpoints._read_checkpoint_data(path, enc)
                if path.parent == checkpoint_dir and str(data.get("user_id") or "").strip() != str(user_id):
                    continue
                status = data.get("status", "")
                if status not in ("in_progress", "interrupted", "waiting_for_user"):
                    continue
                desc = data.get("task_description", "")
                if query and query.lower() not in desc.lower():
                    continue
                results.append(
                    {
                        "task_id": data.get("task_id", path.stem),
                        "description": desc[:200],
                        "status": status,
                        "steps_completed": len(data.get("steps", [])),
                        "pending_question": data.get("context", {}).get("pending_question", ""),
                        "updated_at": data.get("updated_at", ""),
                    }
                )
            except Exception:
                continue
            if len(results) >= 5:
                break

        if not results:
            return "No pending tasks found." + (" (searched for: %s)" % query if query else "")

        lines = ["%d pending task(s):" % len(results)]
        for t in results:
            line = "- [%s] %s (status: %s, %d steps done)" % (
                t["task_id"],
                t["description"],
                t["status"],
                t["steps_completed"],
            )
            if t.get("pending_question"):
                line += " — waiting for answer to: %s" % t["pending_question"][:100]
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        if _is_expected_auth_error(exc):
            logger.info("check_pending_tasks missing user_id; failing closed")
            return _expected_auth_error_payload(exc)
        logger.exception("check_pending_tasks failed")
        return json.dumps({"error": str(exc)})


# ===========================================================================
# MEMORY TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Read and write user-specific facts in the user's transparent markdown memory directory. "
        "The account has VIOLA.md (read-only user instructions), memory/MEMORY.md, and memory/topics/*.md. "
        "Use for user-specific facts and preferences; it does not search the public web and is not for calendar, weather, or runtime settings. "
        "It is not a general contacts directory or business lookup; when an external destination such as a business phone number is missing from the current request, ask the user or use the appropriate public-search/contact tool instead of enumerating memory topics. "
        "Actions: recall, read, search, write, edit, delete, list, stats, audit. You decide what is worth remembering. "
        "Deterministic hygiene skips duplicate writes, rejects oversized entries, rotates audit logs, "
        "and reports counts via stats. "
        "Do not store passwords, API keys, tokens, card numbers, SSNs, PINs, or bank details. "
        "When the user explicitly asks you to remember something, write it and briefly acknowledge. "
        "When you decide to remember something on your own, write silently. "
        "Does not search the public web or manage calendar events; use web_search or calendar for those. "
        "For runtime settings use user_settings; memory is not a settings API."
    ),
    annotations=_SAFE,
    meta={
        "risk": "safe",
        "anthropic/alwaysLoad": True,
        "irreversible_actions": ["delete"],
        "irreversible_class": "memory_delete",
    },
)
async def memory(
    action: Annotated[
        str,
        Field(description="Action: recall, read, search, write, edit, delete, list, stats, audit"),
    ] = "read",
    path: Annotated[
        str,
        Field(
            description=(
                "Markdown path such as MEMORY.md, VIOLA.md, topics/allergies.md, or allergies.md. "
                "Empty or 'memory' means whole-memory search; bare 'topics' is invalid."
            )
        ),
    ] = "MEMORY.md",
    query: Annotated[
        str,
        Field(description="Query for action='recall' or explicit body search text for action='search'."),
    ] = "",
    content: Annotated[
        str,
        Field(description="Markdown content for action='write'. Do not store secrets or credentials."),
    ] = "",
    where: Annotated[
        str,
        Field(description="Write target: memory, topic:<name>, or a markdown path under memory/."),
    ] = "memory",
    position: Annotated[
        str,
        Field(description="Write position: append, prepend, or replace."),
    ] = "append",
    find: Annotated[
        str,
        Field(description="Exact text to find for action='edit'."),
    ] = "",
    replace: Annotated[
        str,
        Field(description="Replacement text for action='edit'."),
    ] = "",
    line_number: Annotated[
        int,
        Field(description="1-based line number for action='delete'. Use 0 when deleting by section_title."),
    ] = 0,
    section_title: Annotated[
        str,
        Field(description="Markdown heading title for action='delete'."),
    ] = "",
    limit: Annotated[
        int,
        Field(description="Max audit entries for action='audit'."),
    ] = 20,
) -> str:
    """Manage the markdown memory directory.

    Examples:
    - memory(action="write", content="- User prefers jazz in the morning.")
    - memory(action="write", where="topic:allergies", content="- User is allergic to shellfish.")
    - memory(action="recall", query="shellfish")
    - memory(action="search", query="shellfish")
    - memory(action="delete", path="topics/allergies.md", line_number=3)
    - memory(action="list")
    - memory(action="stats")
    """
    try:
        return _format_result(
            await _call_user_scoped(
                _memory,
                action=action,
                path=path,
                query=query,
                content=content,
                where=where,
                position=position,
                find=find,
                replace=replace,
                line_number=line_number,
                section_title=section_title,
                limit=limit,
            )
        )
    except Exception as exc:
        logger.exception("memory failed for action=%s", action)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "The user's Workbench folder: files, documents, photos, longer notes, and saved emails the "
        "user explicitly dropped or uploaded for Viola to reference. It is just a filesystem folder "
        "Viola checks first before using broader file access. It contains saved prior content such "
        "as resumes, uploaded leases, insurance cards, recipes, photos, and saved email content. "
        "Actions: "
        "remember (save text/voice content), search (find files by name or text), path_for "
        "(return the filesystem path), list (show recent files), forget (delete a file), open_folder. "
        "Short personal facts belong in `memory`; runtime settings belong in `user_settings`. "
        "SSNs, passwords, and payment cards are excluded."
    ),
    annotations=_SAFE,
    meta={
        "risk": "safe",
        "irreversible_actions": ["forget"],
        "irreversible_class": "file_delete",
    },
)
async def workbench(
    action: Annotated[
        str,
        Field(description="Action: remember, search, path_for, list, forget, open_folder"),
    ] = "search",
    content: Annotated[
        str,
        Field(description="Text content for action='remember'. For voice path: the transcribed user content."),
    ] = "",
    query: Annotated[
        str,
        Field(description="Free-text search query for action='search'."),
    ] = "",
    item_id: Annotated[
        str,
        Field(description="Deprecated item id for compatibility. Prefer filename."),
    ] = "",
    result_id: Annotated[
        str,
        Field(description="Stable r1..rN id from a prior action='search' result for action='path_for'."),
    ] = "",
    filename: Annotated[
        str,
        Field(description="Filename for action='path_for' or action='forget'."),
    ] = "",
    title_hint: Annotated[
        str,
        Field(
            description=(
                "Filename for action='remember'. A 3-6 word slug from the content works well "
                "(e.g. 'pizza-recipe', 'monday-dentist-notes', 'apartment-lease-2025'). "
                "Without a hint, the handler derives a fallback title from the content."
            )
        ),
    ] = "",
    tags: Annotated[
        str,
        Field(description="Comma-separated tag filter for action='search'."),
    ] = "",
    limit: Annotated[
        int,
        Field(description="Max results for action='search' or action='list'."),
    ] = 10,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Required True to actually forget for action='forget'. False returns a preview "
                "of files that would be forgotten."
            )
        ),
    ] = False,
) -> str:
    """Manage the user's Workbench folder.

    Examples:
    - workbench(action="remember", content="Pizza recipe text...")  # voice path
    - workbench(action="search", query="apartment lease 2025")
    - workbench(action="path_for", filename="resume.pdf")  # then read_file on the path
    - workbench(action="path_for", result_id="r1")  # from a prior search
    - workbench(action="list")  # show recent files
    - workbench(action="forget", filename="resume.pdf", confirm=True)
    - workbench(action="open_folder")  # open the Workbench UI panel/folder
    """
    try:
        if action == "open_folder":
            payload: dict[str, Any] = {
                "voice_summary": "Opened your Workbench.",
                "ui_action": "open_memory_panel",
            }
            try:
                from services.workbench.folder_open import open_user_folder

                user_id = _get_call_user_id()
                if not user_id:
                    from core.user_context import get_current_user_id as _resolve_uid

                    user_id = _resolve_uid()
                if user_id:
                    folder_result = open_user_folder(user_id)
                    payload["storage_mode"] = folder_result.get("storage_mode", "vault")
                    if folder_result.get("opened"):
                        payload["os_folder_opened"] = True
            except Exception:
                logger.exception("workbench open_folder OS-open path failed; falling back to UI panel")
            return json.dumps(payload)
        if action == "remember":
            return _format_result(
                await _call_user_scoped(
                    _workbench_remember,
                    content=content,
                    title_hint=title_hint,
                    source="voice",
                )
            )
        if action == "search":
            return _format_result(
                await _call_user_scoped(
                    _workbench_search,
                    query=query,
                    tags=tags,
                    limit=limit,
                )
            )
        if action == "path_for":
            return _format_result(
                await _call_user_scoped(
                    _workbench_path_for,
                    result_id=result_id,
                    filename=filename,
                )
            )
        if action == "read":
            return _format_result(
                await _call_user_scoped(
                    _workbench_read,
                    result_id=result_id,
                    item_id=item_id,
                    filename=filename,
                )
            )
        if action == "list":
            return _format_result(
                await _call_user_scoped(
                    _workbench_list,
                    limit=limit,
                )
            )
        if action == "forget":
            return _format_result(
                await _call_user_scoped(
                    _workbench_forget,
                    item_id=item_id,
                    filename=filename or item_id,
                    confirm=confirm,
                )
            )
        return json.dumps(
            {"error": "Unknown action: %s. Use remember, search, path_for, list, forget, or open_folder." % action}
        )
    except Exception as exc:
        logger.exception("workbench failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# USER SETTINGS TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Change Viola's runtime configuration: wake sensitivity, voice mode (push-to-talk vs wake-word), "
        "quiet hours, default music provider, default music volume, theme, volumes, locale, weather location, and similar user-adjustable "
        "preferences. This tool ACTUALLY writes to the settings store (settings.json on desktop, per-user DB on cloud) "
        "and the changes take effect immediately. "
        "USE THIS — not the memory tool — whenever the user asks to set, change, update, configure, turn on, "
        "turn off, enable, disable, or adjust any of these settings. The memory tool only stores facts; it does NOT "
        "change Viola's behaviour. "
        'For default music volume, call user_settings(action="set", key="default_music_volume", value="35"); '
        "do not call desktop_volume, which changes the machine-wide system output volume. "
        "Actions: get (read current value), set (validate and persist), list_adjustable (enumerate all keys you can "
        "change with their current values, valid ranges/choices, and descriptions). "
        "Validation: numeric ranges, enum choices, and time-of-day formats are checked. Invalid values are rejected "
        "with a structured error explaining what was wrong. Reading or modifying secrets, billing/plan, auth, or "
        "deployment configuration is not supported here — those are managed elsewhere."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def user_settings(
    action: Annotated[
        str,
        Field(description="Settings action. Use one of: 'get', 'set', or 'list_adjustable'."),
    ] = "list_adjustable",
    key: Annotated[
        str,
        Field(
            description=(
                "Setting key to read or write, e.g. 'wake_sensitivity', 'voice_mode', 'quiet_hours_start', "
                "'active_music_provider_id'. Required for action='get' and action='set'. "
                "Call action='list_adjustable' first if you need to discover the valid keys."
            ),
        ),
    ] = "",
    value: Annotated[
        str,
        Field(
            description=(
                "New value for action='set'. Strings are validated against the setting's type and range. "
                "Examples: '0.85' for wake_sensitivity, 'wake_word' for voice_mode, '22:00' for quiet_hours_start, "
                "'youtube_music' for active_music_provider_id, 'true' for boolean toggles."
            ),
        ),
    ] = "",
) -> str:
    """Read, write, or enumerate user-adjustable settings.

    Examples:
    - user_settings(action="set", key="wake_sensitivity", value="0.85")
    - user_settings(action="set", key="voice_mode", value="push_to_talk")
    - user_settings(action="set", key="quiet_hours_start", value="22:30")
    - user_settings(action="set", key="active_music_provider_id", value="youtube_music")
    - user_settings(action="set", key="default_music_volume", value="35")
    - user_settings(action="get", key="wake_sensitivity")
    - user_settings(action="list_adjustable")
    """
    try:
        normalized = (action or "").strip().lower()
        if normalized in {"", "list", "list_adjustable", "list-adjustable"}:
            return _format_result(await _call_user_scoped(_settings_list_adjustable))
        if normalized == "get":
            return _format_result(await _call_user_scoped(_settings_get, key=key))
        if normalized == "set":
            return _format_result(await _call_user_scoped(_settings_set, key=key, value=value))
        return json.dumps({"error": "Unknown action: %s. Use 'get', 'set', or 'list_adjustable'." % action})
    except Exception as exc:
        logger.exception("user_settings failed for action=%s key=%s", action, key)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# SCHEDULING TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Manage scheduled automations: reminders, recurring tasks, and one-shot future actions. "
        "Covers requests to remind the user or run a Viola action at/after/before a clock time or on a recurrence. "
        "Actions: create (cron or ISO-8601 datetime), list, delete, update. "
        "Does not create calendar meetings or appointments with attendees; calendar covers events, while timer covers simple countdowns."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def schedule(
    action: Annotated[
        str,
        Field(description="Schedule action. Use one of: 'create', 'list', 'delete', or 'update'."),
    ] = "list",
    label: Annotated[
        str,
        Field(
            description="Human-readable label for action='create' or action='update'. Leave empty on update to keep the current label."
        ),
    ] = "",
    schedule_action: Annotated[
        str,
        Field(
            description="Command text for action='create' or action='update'. Leave empty on update to keep the current action."
        ),
    ] = "",
    schedule_spec: Annotated[
        str,
        Field(description="Cron expression or ISO-8601 datetime for action='create'."),
    ] = "",
    enabled_only: Annotated[
        bool,
        Field(description="For action='list', set true to return only enabled schedules."),
    ] = True,
    schedule_id: Annotated[
        int,
        Field(description="Numeric schedule ID for action='update'. Use 0 when not needed."),
    ] = 0,
    schedule_ref: Annotated[
        str,
        Field(description="Schedule ID or label text for action='delete'."),
    ] = "",
    cron_expr: Annotated[
        str,
        Field(description="New cron expression for action='update'. Leave empty to keep the current schedule."),
    ] = "",
    enabled: Annotated[
        str,
        Field(description="For action='update', set to 'true' or 'false' to change enabled state."),
    ] = "",
) -> str:
    """Manage scheduled automations.

    Examples:
    - schedule(action="create", label="Morning jazz", schedule_action="play jazz", schedule_spec="0 7 * * 1-5")
    - schedule(action="list", enabled_only=True)
    - schedule(action="delete", schedule_ref="12")
    - schedule(action="update", schedule_id=12, cron_expr="0 8 * * 1-5", enabled="true")
    """
    try:
        if action == "create":
            # Auto-populate label from schedule_action if empty — the model
            # often puts the description in schedule_action and forgets label.
            _effective_label = label or schedule_action[:50] or "Unnamed schedule"
            return _format_result(
                await _call_user_scoped(
                    _schedule_create,
                    label=_effective_label,
                    action=schedule_action,
                    schedule=schedule_spec,
                )
            )
        if action == "list":
            return _format_result(await _call_user_scoped(_schedule_list, enabled_only=enabled_only))
        if action == "delete":
            delete_ref = schedule_ref or str(schedule_id)
            return _format_result(await _call_user_scoped(_schedule_delete, schedule_id=delete_ref))
        if action == "update":
            cron_expr = cron_expr or schedule_spec
            return _format_result(
                await _call_user_scoped(
                    _schedule_update,
                    schedule_id=schedule_id,
                    label=label,
                    action=schedule_action,
                    cron_expr=cron_expr,
                    enabled=enabled,
                )
            )
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("schedule failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# API REGISTRY TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Check whether a service name or URL has a native/direct API integration registered. "
        "Returns available tools, auth status, domains, and registry notes as facts. "
        "Does not fetch public information itself; web_search covers research and browser_navigate covers page interaction."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def check_api_registry(
    service_or_url: Annotated[
        str,
        Field(
            description="Service name or URL to check for a direct API integration, for example 'Notion', 'GitHub', or 'https://api.example.com'."
        ),
    ],
) -> str:
    """Check whether Viola has a native API registry entry for a service or URL.

    Args:
        service_or_url: Service name or URL to check for API availability.
    """
    try:
        result = await _check_api_registry(service_or_url)
        return _format_result(result)
    except Exception as exc:
        logger.exception("check_api_registry failed for %s", service_or_url[:60])
        return json.dumps({"error": str(exc)})


# ===========================================================================
# TOOL SEARCH — discover tools on demand (O3e: deferred loading)
# ===========================================================================


@server.tool(
    description=(
        "Fetch full schema definitions for tools in the deferred MCP tool pool so they can be called. "
        "Deferred tools appear by name only, in the <available-deferred-tools> list; until fetched, no "
        "parameter schema is known for them, so they cannot be invoked. Already visible tool schemas are "
        "never in this pool: a tool whose parameters you can already read is loaded, and is "
        "directly callable without ToolSearch, so searching for it finds nothing you do not already have. "
        "Returns tool_reference matches; each returned tool is directly callable on the next turn. "
        "Web content is outside this pool.\n\n"
        "Query forms (port of Claude's ToolSearch grammar):\n"
        '- "select:Read,Edit,Grep" — fetch these exact tools by name (deferred-ref selector).\n'
        '- "Read" / "browser_navigate" — bare exact name returns just that tool.\n'
        '- "mcp__slack" — return every tool whose name starts with this exact prefix.\n'
        '- "+slack send" — terms prefixed with + are hard filters; remaining tokens rank by BM25.\n'
        '- "send an email" — natural-language BM25 keyword search (default fallthrough).'
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def ToolSearch(
    query: Annotated[
        str,
        Field(
            description=(
                "Tool query. Forms: 'select:Read,Edit' for direct selection, 'Read' for "
                "exact-name match, 'mcp__slack' for an MCP-server prefix, '+slack send' to "
                "force 'slack' as a hard filter and rank by remaining terms, or a natural-"
                "language description ('send an email', 'take a screenshot') for BM25 search."
            )
        ),
    ],
    top_n: Annotated[
        int,
        Field(
            description="Maximum number of tool matches to return. Use a small number for precision; the tool caps this at 12."
        ),
    ] = 7,
) -> str:
    """Search deferred tools by name or capability description.

    A ToolSearch match materializes a tool_reference; the matched tool is
    directly callable on the next provider turn. ToolSearch covers the deferred
    MCP tool pool. Web content is outside this pool.

    Query grammar (Claude parity, F-044):

    - ``select:Read,Edit,Grep`` — fetch these exact tools by name.
    - ``Read`` — bare exact-name returns that tool.
    - ``mcp__slack`` — MCP-server prefix returns every ``mcp__slack__*`` tool.
    - ``+slack send`` — terms prefixed with ``+`` are hard filters.
    - ``send an email`` — natural-language BM25 keyword search.

    Args:
        query: Natural-language description OR exact-name / prefix / +required form.
        top_n: Maximum number of tools to return (default 7, max 12).
    """
    try:
        pool_token = None
        pool_payload = _get_call_meta().get("viola_deferred_tool_pool")
        if isinstance(pool_payload, dict):
            from intent.tools.deferred_tool_schemas import (
                deferred_tool_pool_from_payload,
            )
            from intent.tools.tool_search import (
                reset_deferred_tool_pool,
                set_deferred_tool_pool,
            )

            pool_token = set_deferred_tool_pool(deferred_tool_pool_from_payload(pool_payload))
        try:
            result = await _call_with_current_user_context(_tool_search, query, top_n)
        finally:
            if pool_token is not None:
                reset_deferred_tool_pool(pool_token)
        return _format_result(result)
    except Exception as exc:
        logger.exception("ToolSearch failed for query_chars=%d", len(query or ""))
        return json.dumps({"error": str(exc)})


# ===========================================================================
# ASK USER TOOL
# ===========================================================================


@server.tool(
    description=(
        "Ask the user a clarifying question and wait for their response. "
        "Use when a task is blocked on one required value, choice, or confirmation from the user "
        "(for example, a missing phone number for a requested call). "
        "In request/response sessions this returns structured pending-question state instead of blocking."
    ),
    annotations=_SAFE,
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def ask_user(
    question: Annotated[
        str,
        Field(
            description="The exact clarifying question to ask the user. Keep it specific, answerable, and directly tied to a blocker in the current task."
        ),
    ],
    context: Annotated[
        str,
        Field(
            description="Internal context for why the question is being asked. This helps the system route or frame the question and is not shown to the user."
        ),
    ] = "",
) -> str:
    """Ask the user a clarifying question. In foreground sessions, waits for
    voice/messaging response. In non-interactive request/response sessions,
    returns structured pending-question state for the next user turn.

    Args:
        question: The question to ask the user.
        context: Brief internal context for why you're asking (not shown to user).
    """
    try:
        result = await _ask_user(question, context)
        return _format_result(result)
    except Exception as exc:
        logger.exception("ask_user failed for question_chars=%d", len(question or ""))
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# API Credential Vault tools
# ---------------------------------------------------------------------------


_API_VAULT_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "services" / "api_vault" / "templates"
_API_VAULT_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_api_vault_template(template: str) -> Path | None:
    """Resolve a built-in API-vault template name without allowing path traversal."""
    template_name = template.strip()
    if not template_name or not _API_VAULT_TEMPLATE_NAME_RE.fullmatch(template_name):
        return None

    template_path = (_API_VAULT_TEMPLATE_ROOT / ("%s.json" % template_name)).resolve()
    try:
        template_path.relative_to(_API_VAULT_TEMPLATE_ROOT.resolve())
    except ValueError:
        return None
    return template_path


@server.tool(
    description=(
        "Manage API credentials in Viola's encrypted vault. "
        "Actions: store (save key), delete (remove key), validate (check existence), list (show all services). "
        "Encrypted at rest, never logged."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def api_credential(
    action: Annotated[
        str,
        Field(description="Credential action to perform. Use one of 'store', 'delete', 'validate', or 'list'."),
    ] = "list",
    service_name: Annotated[
        str,
        Field(description="Service identifier, such as 'notion', 'github', or 'openweather'."),
    ] = "",
    api_key: Annotated[
        str,
        Field(description="API key or token to store when action is 'store'. Leave empty otherwise."),
    ] = "",
    template: Annotated[
        str,
        Field(description="Optional template name to load after storing, such as 'notion'."),
    ] = "",
) -> str:
    """Manage API credentials in the encrypted vault.

    Examples:
    - api_credential(action="store", service_name="notion", api_key="sk-...")  # pragma: allowlist secret
    - api_credential(action="validate", service_name="github")
    - api_credential(action="delete", service_name="slack")
    - api_credential(action="list")
    """
    try:
        from services.api_vault.vault import get_credential_vault

        user_id = _require_call_user_id("api_credential")
        vault = get_credential_vault()

        if action == "store":
            if not api_key:
                return json.dumps({"error": "api_key is required for store action"})
            vault.store_credential(service_name, api_key, user_id=user_id)
            if template:
                try:
                    from services.api_vault.catalog import get_api_catalog

                    catalog = get_api_catalog()
                    template_path = _resolve_api_vault_template(template)
                    if template_path is not None and template_path.exists():
                        catalog.load_template(template_path)
                except Exception:
                    logger.debug("Template loading is best-effort, continuing without template")
            return "Credential stored for '%s'. Available for future requests by this user." % service_name

        if action == "delete":
            removed = vault.delete_credential(service_name, user_id=user_id)
            if removed:
                return "Credential removed for '%s'." % service_name
            return "No credential found for '%s'." % service_name

        if action == "validate":
            has = vault.has_credential(service_name, user_id=user_id)
            if has:
                return "Credential exists for '%s'." % service_name
            return "No credential stored for '%s'." % service_name

        if action == "list":
            services = vault.list_services(user_id=user_id)
            if not services:
                return "No API credentials stored. Users can provide API keys for services like Notion, GitHub, Slack, etc."
            lines = ["Stored API credentials:"]
            for svc in services:
                lines.append(
                    "- %s (stored: %s, last used: %s)"
                    % (
                        svc["service_name"],
                        svc.get("stored_at", "unknown")[:10],
                        svc.get("last_used", "never") or "never",
                    )
                )
            return "\n".join(lines)

        return json.dumps({"error": "Unknown action '%s'. Use 'store', 'delete', 'validate', or 'list'." % action})
    except Exception as exc:
        if _is_expected_auth_error(exc):
            logger.info("api_credential missing user_id; failing closed")
            return _expected_auth_error_payload(exc)
        logger.exception("api_credential failed: %s", exc)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# PLAYLIST TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Manage the user's saved Viola playlists, stored locally and persistent across sessions. "
        "Actions: create, add_track using a track_uri from media search results, list, delete, play a saved playlist, or play favorites. "
        "Does not search the public web, answer music facts, or control transport; media finds and plays selectable tracks, and playback controls pause/skip/seek."
    ),
    annotations=_DANGEROUS,
    meta={"risk": "destructive"},
)
async def playlist(
    action: Annotated[
        str,
        Field(
            description=(
                "Playlist action. Use one of: 'create', 'add_track', 'list', 'delete', 'play', or 'play_favorites'."
            )
        ),
    ] = "list",
    name: Annotated[
        str,
        Field(description="Playlist name for action='create', 'delete', or 'play'."),
    ] = "",
    url: Annotated[
        str,
        Field(description="Optional source playlist URL for action='create'. Leave empty to create a blank playlist."),
    ] = "",
    provider: Annotated[
        str,
        Field(description="Music provider for action='create' or action='add_track'."),
    ] = "youtube_music",
    shuffle: Annotated[
        bool,
        Field(description="Shuffle preference for action='create' or action='play_favorites'."),
    ] = True,
    limit: Annotated[
        int,
        Field(description="Maximum favorites to consider for action='play_favorites' (default 15, max 30)."),
    ] = 15,
    playlist_name: Annotated[
        str,
        Field(description="Target playlist name for action='add_track'."),
    ] = "",
    track_uri: Annotated[
        str,
        Field(description="Track URI or provider-specific identifier for action='add_track'."),
    ] = "",
    title: Annotated[
        str,
        Field(description="Optional track title for action='add_track'."),
    ] = "",
    artist: Annotated[
        str,
        Field(description="Optional artist name for action='add_track'."),
    ] = "",
) -> str:
    """Manage saved playlists.

    Examples:
    - playlist(action="create", name="Workout Mix", provider="youtube_music")
    - playlist(action="add_track", playlist_name="Workout Mix", provider="youtube_music", track_uri="...")
    - playlist(action="list")
    - playlist(action="delete", name="Old Mix")
    - playlist(action="play", name="Workout Mix")
    - playlist(action="play_favorites", shuffle=True, limit=15)
    """
    try:
        if action == "create":
            return _format_result(await _call_user_scoped(_create_playlist, name, url, provider, shuffle))
        if action == "add_track":
            playlist_name = playlist_name or name
            return _format_result(
                await _call_user_scoped(
                    _add_track_to_playlist,
                    playlist_name=playlist_name,
                    provider=provider,
                    track_uri=track_uri,
                    title=title,
                    artist=artist,
                )
            )
        if action == "list":
            return _format_result(await _call_user_scoped(_list_playlists))
        if action == "delete":
            return _format_result(await _call_user_scoped(_delete_playlist, name))
        if action == "play":
            return _format_result(await _call_user_scoped(_play_playlist_mcp, name))
        if action == "play_favorites":
            return _format_result(await _call_user_scoped(_play_favorites, shuffle=shuffle, limit=limit))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("playlist failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# LIKED SONGS TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Return the user's liked or favorited songs across music providers (Spotify, YouTube Music, local). "
        "Filterable by since date, provider, and limit. Newest first. "
        "Useful for inspecting music taste, history, or building playlists. "
        "Does not start playback; playlist(action='play_favorites') handles favorites playback."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def get_liked_songs(
    since: Annotated[
        str,
        Field(
            description="Optional ISO-8601 date or datetime cutoff. Only songs liked after this value are returned; examples: '2026-03-10' or '2026-03-10T14:30:00+00:00'."
        ),
    ] = "",
    provider: Annotated[
        str,
        Field(
            description="Optional provider filter. Use 'spotify', 'youtube_music', 'youtube', or 'local', or leave empty for all providers."
        ),
    ] = "",
    limit: Annotated[
        int,
        Field(
            description="Maximum number of liked songs to return. Use a smaller number for quick summaries; the tool supports up to 500."
        ),
    ] = 50,
) -> str:
    """Return songs the user has liked, from Viola's cross-provider tracker.

    Viola records every thumbs-up locally regardless of whether the
    provider-side API call (Spotify, YouTube) succeeded.  Use this tool
    to reason about the user's taste, build playlists, or show history.

    Args:
        since: ISO-8601 date or datetime string.  Only songs liked *after*
               this value are returned.  Examples: ``"2026-03-10"``,
               ``"2026-03-10T14:30:00+00:00"``.  Leave empty for all time.
        provider: Filter to a single provider: ``spotify``, ``youtube_music``,
                  ``youtube``, or ``local``.  Leave empty to return all
                  providers.
        limit: Maximum number of results to return (default 50, max 500).

    Returns:
        JSON object with keys:
        - ``songs``: list of ``{title, artist, provider, provider_track_id,
          url, liked_at}`` dicts, newest-first.
        - ``count``: number of songs returned.
    """
    try:
        result = await _call_user_scoped(_get_liked_songs, since=since, provider=provider, limit=limit)
        return _format_result(result)
    except Exception as exc:
        logger.exception("get_liked_songs failed")
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Unified music and video search/play tool. For direct requests like 'play Drake', use "
        "action='search_play' with query to search, select the top playable candidate, and start playback in "
        "one tool call. Search mode: pass query to inspect playable candidates with provider, title, artist, "
        "and track_uri, using recently played tracks to avoid repeats. Play mode: pass track_uri from a "
        "selected candidate; include target_room for Viola's in-house "
        "multi-room playback, room_route facts, and Add Speaker pairing_flow fallback. Use pair_speaker_setup "
        "for speaker setup without playback. Active provider from settings drives where to search; pass "
        "provider explicitly to override. Transport controls use playback."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def media(
    query: Annotated[
        str,
        Field(
            description=(
                "Search text for music or video candidates. Use with action='search_play' for direct play requests. "
                "Leave empty when playing a selected track_uri."
            )
        ),
    ] = "",
    action: Annotated[
        Literal["auto", "search", "play", "search_play"],
        Field(
            description=(
                "Media action. auto preserves compatibility: query searches and track_uri plays. "
                "Use search_play for direct 'play X' requests to search, select the top candidate, and play it."
            )
        ),
    ] = "auto",
    track_uri: Annotated[
        str,
        Field(
            description=(
                "Exact candidate identifier from a selected media search result. When set, media plays this item."
            )
        ),
    ] = "",
    provider: Annotated[
        Literal["auto", "local", "spotify", "youtube", "youtube_music", "any"],
        Field(
            description=(
                "Search/play provider. auto uses the active music provider setting; pass an explicit value to override."
            )
        ),
    ] = "auto",
    limit: Annotated[
        int,
        Field(description="Maximum candidates to return in search mode. Valid range is 1 to 25."),
    ] = 10,
    target_room: Annotated[
        str,
        Field(description="Optional Viola room or paired Spoke speaker for playback mode."),
    ] = "",
) -> str:
    """Search media candidates, then play an exact selected candidate."""
    try:
        required_user_id = _require_call_user_id("media")
        result = await _call_with_required_user_id(
            _media,
            required_user_id,
            query=query,
            track_uri=track_uri,
            action=action,
            provider=provider,
            limit=limit,
            target_room=target_room,
        )
        return _format_result_with_error_data(result)
    except Exception as exc:
        if _is_expected_auth_error(exc):
            logger.info("media missing user_id; failing closed")
            return _expected_auth_error_payload(exc)
        logger.exception("media failed")
        return json.dumps({"error": str(exc)})


# ===========================================================================
# NATIVE AGENT TOOLS — music, timers, calendar, commerce
# ===========================================================================


@server.tool(
    description=(
        "Start the account/browser login flow for a music provider without playing music. "
        "Supported providers are listed in the parameter "
        "schema. The flow uses Viola's dedicated provider auth path (some providers "
        "go through an external Chrome CDP session, others through the music browser). "
        "Returns a structured envelope with ok, status (awaiting_user_login, connected, "
        "or error), provider_chrome_pid, and login_url_to_show_user. The "
        "user completes login externally; this tool returns immediately with status. "
        "The supported provider list is returned in the payload; do not invent or "
        "rank providers."
    ),
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible": True,
        "irreversible_class": "oauth_connect",
    },
)
async def connect_music_provider(
    provider: Annotated[
        Literal["spotify", "youtube_music"],
        Field(description=("Music provider to connect. Allowed: spotify, youtube_music.")),
    ],
) -> str:
    """Start the selected music provider connection flow."""
    try:
        return _format_result(await _connect_music_provider(provider))
    except Exception as exc:
        logger.exception("connect_music_provider failed for provider=%s", provider)
        return json.dumps(
            {
                "ok": False,
                "status": "error",
                "provider": provider,
                "error": str(exc),
                "provider_chrome_pid": None,
                "login_url_to_show_user": None,
            }
        )


@server.tool(
    description=(
        "Check music provider connection state without launching a login flow. "
        "provider='all' returns every supported provider; a specific provider returns one. Status checks "
        "use the dedicated provider auth path (external Chrome CDP for some "
        "providers, the music browser auth for others). Returns all supported "
        "providers when provider='all', with connected/logged_in/session_expired, "
        "status, provider_chrome_pid, and login_url_to_show_user."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def check_music_provider_status(
    provider: Annotated[
        Literal["all", "spotify", "youtube_music"],
        Field(
            description=(
                "Provider to check, or 'all' to list every supported provider. " "Allowed: all, spotify, youtube_music."
            )
        ),
    ] = "all",
) -> str:
    """Return music provider connection state."""
    try:
        return _format_result(await _check_music_provider_status(provider))
    except Exception as exc:
        logger.exception("check_music_provider_status failed for provider=%s", provider)
        return json.dumps(
            {
                "ok": False,
                "status": "error",
                "provider": provider,
                "error": str(exc),
                "provider_chrome_pid": None,
                "login_url_to_show_user": None,
            }
        )


@server.tool(
    description=(
        "Open a Viola app panel for user intents like 'open settings', 'show payment methods', "
        "or 'take me to my calendar'. Available panel_id values: settings, rooms.add_speaker, "
        "calendar, music_accounts, payment_methods, help. Optional sub_tab can name a Settings "
        "tab such as account, ai_agents, music_voice, messaging, services, payment, or preferences. "
        "Optional prefill can pass structured UI defaults such as {'room_name': 'kitchen'}. "
        "Returns a structured ui_action envelope for the React app; this is a UI capability, "
        "not a gate, classifier, or settings mutation."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def open_app_panel(
    panel_id: Annotated[
        str,
        Field(
            description=(
                "Panel to open. Use one of: settings, rooms.add_speaker, calendar, "
                "music_accounts, payment_methods, help."
            )
        ),
    ],
    sub_tab: Annotated[
        str | None,
        Field(
            description=(
                "Optional Settings tab or section. Useful values include account, ai_agents, "
                "music_voice, messaging, services, payment, and preferences."
            )
        ),
    ] = None,
    prefill: Annotated[
        _AppPanelPrefill | str | None,
        Field(description="Optional structured UI prefill, for example {'room_name': 'kitchen'}."),
    ] = None,
) -> str:
    """Return a structured UI-action envelope that the React app can execute."""
    from intent.tool_types import ToolResult

    normalized = _normalize_app_panel_key(panel_id)
    config = _APP_PANEL_ACTIONS.get(normalized)
    if config is None:
        available = ", ".join(_APP_PANEL_ACTIONS)
        return _format_result(
            ToolResult(
                ok=False,
                error="Unknown panel_id '%s'. Available panel_id values: %s" % (panel_id, available),
            )
        )

    payload: dict[str, Any] = {
        "message": "app_panel_open_available",
        "panel_id": normalized,
        **config,
    }

    raw_sub_tab_key = re.sub(r"[\s\-\/]+", "_", sub_tab.strip().lower()) if sub_tab else ""
    normalized_tab = _normalize_settings_tab(sub_tab)
    if normalized == "settings":
        if normalized_tab:
            payload["tab"] = normalized_tab
        if raw_sub_tab_key in {"music_accounts", "connected_music"}:
            payload["section"] = "music_accounts"
    elif normalized in {"music_accounts", "payment_methods"} and normalized_tab:
        payload["sub_tab"] = normalized_tab

    prefill_payload = _normalize_app_panel_prefill(prefill)
    if prefill_payload:
        payload["prefill"] = prefill_payload
        if "room_name" in prefill_payload:
            payload["room_name"] = prefill_payload["room_name"]

    return _format_result(ToolResult(ok=True, data=payload))


@server.tool(
    description=(
        "Add, pair, bind, connect, or register a speaker to a room without starting music. "
        "Open Viola's Add Room speaker setup flow for setup-only requests like "
        "'add a kitchen speaker', 'pair a speaker to the kitchen room', "
        "'bind a speaker to a room', 'register a new speaker', or 'connect a room speaker'. "
        "Returns structured pairing_flow metadata, qr_data, pairing_code, "
        "spoke_url, and ui_action='open_rooms_add_speaker' for the Rooms > Add Room "
        "QR card; the UI auto-opens that panel from the returned ui_action. "
        "Use this instead of media or playback when the user wants "
        "to add, pair, bind, connect, register, or set up a speaker/room and did not ask to start music."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def pair_speaker_setup(
    target_room: Annotated[
        str,
        Field(
            description=(
                "Room or speaker name to prefill in the Add Room flow, for example "
                "'kitchen' from 'add a kitchen speaker'. Use 'speaker' when the user "
                "does not name a room."
            )
        ),
    ] = "speaker",
) -> str:
    """Return the structured Add Room QR/pairing envelope without starting playback."""
    from intent.tool_types import ToolResult

    target = target_room.strip() if target_room.strip() else "speaker"
    return _format_result(ToolResult(ok=True, data=build_speaker_pairing_payload(target)))


@server.tool(
    description=(
        "Control current music playback, music player volume, and queue state. Actions: stop, pause, resume, skip, next, previous, seek_to "
        "(absolute seconds), seek_relative (delta seconds; negative rewinds), "
        "restart, clear_queue, volume_set, volume_up, volume_down, mute, and unmute. Useful for operations on whatever is already playing. "
        "For requests like 'skip', 'next song', 'skip this song', or 'advance to the next track', "
        "call playback directly; do not call view_queue first to decide whether transport control is allowed. "
        "When CURRENT SYSTEM STATE shows music is playing, bare contextual volume requests such as turn it down, "
        "louder, quieter, or volume up/down should use playback's music player volume actions. "
        "Machine-wide OS/system output volume belongs to desktop_volume only when the user explicitly asks for system, OS, desktop, speaker, computer, or master output volume. "
        "Does not start new music, choose tracks, add rooms, or pair speakers; media and "
        "playlist cover starting playback, and pair_speaker_setup covers Add Room setup."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def playback(
    action: Annotated[
        str,
        Field(
            description=(
                "Playback action. Use one of: 'stop', 'pause', 'resume', 'skip', 'next', 'previous', "
                "'seek_to', 'seek_relative', 'restart', 'clear_queue', 'volume_set', 'volume_up', "
                "'volume_down', 'mute', or 'unmute'. "
                "Use 'skip' or 'next' for next-song requests. The alias 'seek' is accepted as 'seek_to'."
            )
        ),
    ] = "pause",
    seek_to_seconds: Annotated[
        float | None,
        Field(description="Absolute target position in seconds for action='seek_to'."),
    ] = None,
    seek_delta_seconds: Annotated[
        float | None,
        Field(
            description=(
                "Relative delta seconds for action='seek_relative'; positive skips ahead and negative rewinds."
            )
        ),
    ] = None,
    seconds: Annotated[
        float | None,
        Field(description=("Compatibility alias for seek_to_seconds when action='seek' or action='seek_to'.")),
    ] = None,
    volume_level: Annotated[
        int | None,
        Field(description="Target music player volume percent for action='volume_set'."),
    ] = None,
    volume_step: Annotated[
        int | None,
        Field(description="Music player volume step percent for action='volume_up' or action='volume_down'."),
    ] = None,
) -> str:
    """Control current playback through Viola's canonical runtime APIs."""
    try:
        required_user_id = _require_call_user_id("playback")
        from core.user_context import user_scope

        effective_seek_to = seek_to_seconds
        if effective_seek_to is None and seconds is not None:
            effective_seek_to = seconds

        with user_scope(required_user_id):
            result = await _playback_control_handler(
                action,
                seek_to_seconds=effective_seek_to,
                seek_delta_seconds=seek_delta_seconds,
                volume_level=volume_level,
                volume_step=volume_step,
                user_id=required_user_id,
            )
        return _format_result(result)
    except Exception as exc:
        if _is_expected_auth_error(exc):
            logger.info("playback missing user_id; failing closed")
            return _expected_auth_error_payload(exc)
        logger.exception("playback failed for action=%s", action)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Show the current music playback queue: now-playing track plus the upcoming queue. "
        "Does not control playback; playback control is handled by the playback tool."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def view_queue() -> str:
    """Return the current playback queue snapshot."""
    try:
        from intent.tools.music_tools import view_queue_handler

        result = await view_queue_handler()
        return _format_result(result)
    except Exception as exc:
        logger.exception("view_queue failed")
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Rate a music track with thumbs up or thumbs down. Without track_id, "
        "rates the currently playing track. With track_id, rates that explicit "
        "track through Viola's rating system."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def rate_track(
    value: Annotated[
        str,
        Field(
            description=(
                "Rating value. Use 'thumbs_up' to like the track or "
                "'thumbs_down' to dislike it. The aliases 'up' and 'down' are accepted."
            )
        ),
    ] = "",
    track_id: Annotated[
        str,
        Field(description="Optional explicit track, video, or local-library ID. Leave empty for the current track."),
    ] = "",
    title: Annotated[
        str,
        Field(description="Optional track title metadata when track_id is provided."),
    ] = "",
    artist: Annotated[
        str,
        Field(description="Optional artist metadata when track_id is provided."),
    ] = "",
    rating: Annotated[
        str,
        Field(description="Compatibility alias for value, accepting 'up' or 'down'."),
    ] = "",
) -> str:
    """Rate the current or explicit track through Viola's canonical rating APIs."""
    try:
        effective_value = value or rating
        result = await _rate_track_handler(
            effective_value,
            track_id=track_id,
            title=title,
            artist=artist,
            user_id=_get_call_user_id() or "",
        )
        return _format_result(result)
    except Exception as exc:
        logger.exception("rate_track failed for value=%s track_id=%s", value or rating, track_id)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Return structured weather for a city or the user's saved location: current conditions (temperature, feels-like, wind, humidity) plus a multi-day daily forecast (today through the next several days, each with high/low, condition, and rain chance) in the daily_forecast field. "
        "Answers current, today, tomorrow, this-weekend, and named-day forecast questions directly from this tool alone, plus rain, temperature, and travel-packing questions. "
        "Does not cover historical weather, climate research, weather news, or severe-alert/news context; web_search can add that outside context."
    ),
    annotations=_SAFE,
    # issue #2094: weather previously deferred behind ToolSearch, costing a
    # full extra LLM round-trip (~4-5s) on every cold weather query before the
    # weather schema was even visible. alwaysLoad removes that round-trip the
    # same way start_agent/memory/ask_user/web_search already do (F-014).
    meta={"risk": "safe", "anthropic/alwaysLoad": True},
)
async def weather(
    city: Annotated[
        str,
        Field(
            description=(
                "City name to look up (e.g. 'Madison, WI', 'New York', 'London'). "
                "Leave empty to use the user's already-configured saved location -- this is a real stored "
                "setting, not a guess or placeholder, so call this with city empty by default when the user "
                "doesn't name a city, instead of asking them first. If there is truly no saved location, the "
                "result reports that plainly so you can ask then; only ask up front if the user wants a "
                "specific different city."
            )
        ),
    ] = "",
) -> str:
    """Return current conditions and a multi-day forecast for the city or saved location."""
    try:
        from intent.tools.weather_tool import weather_handler

        result = await weather_handler(city)
        return _format_result(result)
    except Exception as exc:
        logger.exception("weather failed for city_chars=%d", len(city or ""))
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Assemble the user's daily briefing: one aggregated summary of today built from current weather, "
        "today's calendar events, and the user's scheduled tasks/reminders due today. "
        "Use for 'give me my daily briefing', 'morning briefing', 'what's my day look like', 'brief me', and similar catch-me-up requests. "
        "Returns a spoken 'message' plus a 'sections' map with an honest per-source status (ok, empty, or unavailable) so partial results are never presented as complete. "
        "Does not fetch a single source in isolation (use weather, calendar, or schedule for that) and does not change anything."
    ),
    annotations=_SAFE,
    meta={"risk": "safe"},
)
async def daily_briefing(
    city: Annotated[
        str,
        Field(
            description=(
                "Optional city for the weather portion (e.g. 'Madison, WI'). "
                "Leave empty to use the user's saved weather_location."
            )
        ),
    ] = "",
) -> str:
    """Aggregate weather, today's calendar, and today's tasks into one briefing."""
    try:
        from intent.tools.briefing_tools import daily_briefing_handler

        result = await _call_user_scoped(daily_briefing_handler, city=city)
        return _format_result(result)
    except Exception as exc:
        if _is_expected_auth_error(exc):
            return _expected_auth_error_payload(exc)
        logger.exception("daily_briefing failed for city_chars=%d", len(city or ""))
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Timer tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Manage countdown timers. Actions: set (start timer with minutes and optional label), list/status (show active timers), "
        "cancel (stop by timer_id, or cancel the most recent timer when timer_id is empty), "
        "cancel_all (stop every active timer), sleep_timer (stop playback after minutes), "
        "cancel_sleep_timer (cancel pending sleep timers). "
        "Useful for simple relative countdowns such as cooking or focus timers. "
        "Does not create calendar events or recurring scheduled tasks; calendar and schedule cover those."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def timer(
    action: Annotated[
        str,
        Field(
            description=(
                "Timer action. Use one of: 'set', 'cancel', 'cancel_all', 'list', 'status', "
                "'sleep_timer', or 'cancel_sleep_timer'."
            )
        ),
    ] = "list",
    minutes: Annotated[
        float,
        Field(
            description=(
                "Timer duration in minutes for action='set' or action='sleep_timer'. "
                "Must be greater than 0 and at most 1440."
            )
        ),
    ] = 0.0,
    label: Annotated[
        str,
        Field(description="Optional timer label for action='set' or action='sleep_timer'."),
    ] = "",
    timer_id: Annotated[
        str,
        Field(
            description=(
                "Timer ID for action='cancel'. Leave empty to cancel the most recently created timer. "
                "Use action='cancel_all' to cancel every active timer."
            )
        ),
    ] = "",
) -> str:
    """Manage timers.

    Examples:
    - timer(action="set", minutes=15, label="pasta")
    - timer(action="list")
    - timer(action="cancel", timer_id="3")
    - timer(action="cancel_all")
    - timer(action="sleep_timer", minutes=30)
    """
    try:
        action_name = action.strip().lower()
        if action_name == "set":
            return _format_result(await _call_user_scoped(_set_timer_handler, minutes, label))
        if action_name == "cancel":
            return _format_result(await _call_user_scoped(_cancel_timer_handler, timer_id))
        if action_name in {"cancel_all", "cancel_all_timers"}:
            return _format_result(await _call_user_scoped(_cancel_all_timers_handler))
        if action_name in {"list", "status"}:
            return _format_result(await _call_user_scoped(_list_timers_handler))
        if action_name == "sleep_timer":
            return _format_result(await _call_user_scoped(_set_sleep_timer_handler, minutes, label))
        if action_name in {"cancel_sleep_timer", "cancel_sleep_timers"}:
            return _format_result(await _call_user_scoped(_cancel_sleep_timers_handler, timer_id))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("timer failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Alarm tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Manage alarms. Actions: set (schedule an alarm sound at a time), cancel "
        "(cancel by alarm_id or label, or pending alarms when no selector is supplied), "
        "cancel_all (cancel all pending alarms), list (show pending alarms), and sound "
        "(fire the alarm notification path)."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def alarm(
    action: Annotated[
        str,
        Field(description="Alarm action. Use one of: 'set', 'cancel', 'cancel_all', 'list', or 'sound'."),
    ] = "list",
    when: Annotated[
        str,
        Field(description="Alarm time for action='set', such as '7 AM', 'tomorrow 8 AM', or an ISO datetime."),
    ] = "",
    label: Annotated[
        str,
        Field(description="Optional alarm label or name, used for setting or cancelling a named alarm."),
    ] = "",
    alarm_id: Annotated[
        str,
        Field(description="Optional scheduler/alarm id for action='cancel'."),
    ] = "",
) -> str:
    """Manage alarms through the scheduler."""
    try:
        action_name = action.strip().lower()
        if action_name == "set":
            return _format_result(await _call_user_scoped(_set_alarm_handler, when, label or "Alarm"))
        if action_name == "cancel":
            return _format_result(
                await _call_user_scoped(
                    _cancel_alarm_handler,
                    label=label,
                    alarm_id=alarm_id,
                    cancel_all=False,
                )
            )
        if action_name == "cancel_all":
            return _format_result(await _call_user_scoped(_cancel_alarm_handler, cancel_all=True))
        if action_name in {"list", "status"}:
            return _format_result(await _call_user_scoped(_list_alarms_handler))
        if action_name == "sound":
            return _format_result(await _call_user_scoped(_play_alarm_sound_handler, label=label or "Alarm"))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("alarm failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# User notification tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Send, schedule, or list notifications/reminders. Use action='list' when "
        "the user asks what reminders they have. For send/schedule, provide message, "
        "optional when for delayed delivery, and optional channel. User-scoped delivery "
        "uses the user's subscribed web push devices; phone/SMS delivery requires a "
        "separately configured account-bound notification path."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def notify(
    message: Annotated[
        str,
        Field(description="Notification text or reminder content. Leave empty for action='list'."),
    ] = "",
    when: Annotated[
        str,
        Field(description="Optional delivery time, such as 'in 5 minutes' or an ISO datetime."),
    ] = "",
    channel: Annotated[
        str,
        Field(description="Optional delivery channel. Phone/SMS requires an account-bound notification path."),
    ] = "",
    action: Annotated[
        str,
        Field(description="Notification action. Use 'list' to list pending notify reminders; otherwise omit."),
    ] = "",
    enabled_only: Annotated[
        bool,
        Field(description="For action='list', set true to return only enabled pending reminders."),
    ] = True,
) -> str:
    """Send, schedule, or list notifications/reminders."""
    try:
        action_name = action.strip().lower()
        if action_name == "list":
            return _format_result(await _call_user_scoped(_list_notify_reminders_handler, enabled_only=enabled_only))
        if action_name and action_name not in {"send", "schedule", "create"}:
            return json.dumps({"error": "Unknown action: %s. Use 'list', 'send', 'schedule', or omit action." % action})
        return _format_result(
            await _call_user_scoped(
                _notify_handler,
                message=message,
                when=when or None,
                channel=channel or None,
            )
        )
    except Exception as exc:
        logger.exception("notify failed")
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "View and manage the always-on local primary calendar, with Google, Microsoft, and CalDAV as optional sync targets. "
        "Actions: list (by date range), list_calendars, get, add "
        "(title + start_time, 1hr default duration), update, delete, respond, and find_free_time. "
        "List results include calendars_connected, connected_providers, and events. "
        "Event results include stored_iso_utc for storage/audit and display_local for user-local display; user-facing answers should quote display_local, not stored_iso_utc. "
        "With the local provider connected, events=[] is an empty schedule for the requested range. "
        "Do not present Google/Microsoft/CalDAV setup as a prerequisite; remote connection only upgrades sync. "
        "action='icloud_status' reports whether an iCloud/CalDAV account is connected and, if not, "
        "directs the user to the desktop Settings > Calendar UI to connect it -- this tool never "
        "collects an Apple ID or app-specific password through conversation. "
        "Does not handle countdown timers or recurring agent automations; timer and schedule cover those. Public event research or holiday/news context may need web_search."
    ),
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible_actions": ["delete"],
        "irreversible_class": "calendar_delete",
    },
)
async def calendar(
    action: Annotated[
        str,
        Field(
            description=(
                "Calendar action. Use one of: 'list', 'list_calendars', 'get', 'add', 'update', 'delete', "
                "'respond', 'find_free_time', or 'icloud_status'."
            )
        ),
    ] = "list",
    start_date: Annotated[
        str,
        Field(description="Start of the date range for action='list' in ISO-8601 format."),
    ] = "",
    end_date: Annotated[
        str,
        Field(description="End of the date range for action='list' in ISO-8601 format."),
    ] = "",
    max_results: Annotated[
        int,
        Field(description="Maximum number of events to return for action='list'."),
    ] = 10,
    provider: Annotated[
        str,
        Field(description="Calendar provider. Use 'auto', 'all', 'local', 'google', 'graph', or 'caldav'."),
    ] = "auto",
    calendar_id: Annotated[
        str,
        Field(description="Optional provider-specific calendar ID. Leave blank for the primary/default calendar."),
    ] = "",
    title: Annotated[
        str,
        Field(description="Event title/summary for action='add'. Also accepts 'summary' as an alias."),
    ] = "",
    summary: Annotated[
        str,
        Field(description="Alias for title (Google Calendar convention). Use title or summary, not both."),
    ] = "",
    start_time: Annotated[
        str,
        Field(description="Event start time for action='add' in ISO-8601 format."),
    ] = "",
    end_time: Annotated[
        str,
        Field(description="Optional event end time for action='add' in ISO-8601 format."),
    ] = "",
    description: Annotated[
        str,
        Field(description="Optional event description for action='add'."),
    ] = "",
    location: Annotated[
        str,
        Field(description="Optional event location for action='add'."),
    ] = "",
    all_day: Annotated[
        bool,
        Field(description="Set true for an all-day event when action='add'."),
    ] = False,
    event_id: Annotated[
        str,
        Field(description="Calendar event ID for action='get', 'update', 'delete', or 'respond'."),
    ] = "",
    response_status: Annotated[
        str,
        Field(description="For action='respond': 'accepted', 'declined', or 'tentative'."),
    ] = "",
    attendees: Annotated[
        list[str] | None,
        Field(description="Optional attendee email addresses for action='add' or action='find_free_time'."),
    ] = None,
    duration_minutes: Annotated[
        int,
        Field(description="For action='find_free_time': desired slot duration in minutes."),
    ] = 30,
) -> str:
    """Manage calendar events.

    Examples:
    - calendar(action="list", start_date="2026-04-05", end_date="2026-04-12")
    - calendar(action="add", title="Coffee with Maya", start_time="2026-04-06T14:00")
    - calendar(action="delete", event_id="abc123")
    - calendar(action="list_calendars", provider="all")
    - calendar(action="find_free_time", attendees=["alex@example.com"], start_date="2026-04-05T09:00", end_date="2026-04-05T17:00")
    - calendar(action="icloud_status")
    """
    try:
        from intent.tools.calendar_tools import (
            calendar_add_event_handler,
            calendar_delete_event_handler,
            calendar_find_free_time_handler,
            calendar_get_event_handler,
            calendar_icloud_status_handler,
            calendar_list_calendars_handler,
            calendar_list_events_handler,
            calendar_respond_event_handler,
            calendar_update_event_handler,
        )

        if action == "list":
            return _format_result(
                await _call_user_scoped(
                    calendar_list_events_handler,
                    start_date=start_date,
                    end_date=end_date,
                    max_results=max_results,
                    provider=provider or "all",
                    calendar_id=calendar_id,
                )
            )
        if action == "list_calendars":
            return _format_result(await _call_user_scoped(calendar_list_calendars_handler, provider=provider or "all"))
        if action == "get":
            return _format_result(
                await _call_user_scoped(
                    calendar_get_event_handler,
                    event_id=event_id,
                    provider=provider,
                    calendar_id=calendar_id,
                )
            )
        if action == "add":
            # Normalize: accept 'summary' as alias for 'title' (Google Calendar convention)
            _effective_title = title or summary
            return _format_result(
                await _call_user_scoped(
                    calendar_add_event_handler,
                    title=_effective_title,
                    start_time=start_time,
                    end_time=end_time,
                    description=description,
                    location=location,
                    all_day=all_day,
                    provider=provider,
                    calendar_id=calendar_id,
                    attendees=attendees,
                )
            )
        if action == "update":
            return _format_result(
                await _call_user_scoped(
                    calendar_update_event_handler,
                    event_id=event_id,
                    title=title or summary,
                    start_time=start_time,
                    end_time=end_time,
                    description=description,
                    location=location,
                    all_day=all_day,
                    provider=provider,
                    calendar_id=calendar_id,
                )
            )
        if action == "delete":
            return _format_result(
                await _call_user_scoped(
                    calendar_delete_event_handler,
                    event_id=event_id,
                    provider=provider,
                    calendar_id=calendar_id,
                )
            )
        if action == "respond":
            return _format_result(
                await _call_user_scoped(
                    calendar_respond_event_handler,
                    event_id=event_id,
                    response_status=response_status,
                    provider=provider,
                    calendar_id=calendar_id,
                )
            )
        if action == "find_free_time":
            return _format_result(
                await _call_user_scoped(
                    calendar_find_free_time_handler,
                    attendees=attendees,
                    start_date=start_date,
                    end_date=end_date,
                    duration_minutes=duration_minutes,
                    provider=provider,
                    calendar_id=calendar_id,
                )
            )
        if action == "icloud_status":
            return _format_result(await _call_user_scoped(calendar_icloud_status_handler))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("calendar failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Contacts tools (#3282, read-only CardDAV bridge)
# ---------------------------------------------------------------------------


@server.tool(
    description=(
        "Look up or list a user's connected contacts (name -> phone/email), read-only. "
        "Actions: 'find' (resolve a name, e.g. for 'call Jay' or 'email my mom') or "
        "'list' (browse connected contacts). Backed by a connected iCloud/CardDAV account "
        "(see the calendar tool's icloud_status action for how a user connects one) -- "
        "shares the same Apple ID + app-specific password as iCloud Calendar, so connecting "
        "either connects both. There is no write path; this tool never creates, edits, or "
        "deletes a contact."
    ),
)
async def contacts(
    action: Annotated[
        str,
        Field(description="Contacts action. Use one of: 'find' or 'list'."),
    ] = "find",
    name: Annotated[
        str,
        Field(description="For action='find': the contact name to resolve, e.g. 'Jay' or 'Mom'."),
    ] = "",
    max_results: Annotated[
        int,
        Field(description="For action='list': maximum contacts to return."),
    ] = 50,
) -> str:
    """Resolve or list a user's connected contacts.

    Examples:
    - contacts(action="find", name="Jay Shkoukani")
    - contacts(action="list")
    """
    try:
        from intent.tools.contacts_tools import contacts_find_handler, contacts_list_handler

        if action == "find":
            return _format_result(await _call_user_scoped(contacts_find_handler, name=name))
        if action == "list":
            return _format_result(await _call_user_scoped(contacts_list_handler, max_results=max_results))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("contacts failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# Payment card vault management
# ===========================================================================


@server.tool(
    description=(
        "Request secure payment review or list masked saved cards. "
        "Actions: list (masked cards only), request_review (checkout handoff), "
        "open_secure_card_entry (open local card-entry UI). "
        "Never provide card numbers, CVC/CVV, or raw payment credentials to this tool."
    ),
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible_actions": ["pay", "submit", "confirm", "purchase"],
        "irreversible_class": "payment",
    },
)
async def payment(
    action: Annotated[
        str,
        Field(description="Payment action. Use one of: 'list', 'request_review', or 'open_secure_card_entry'."),
    ] = "list",
    order_summary: Annotated[
        str,
        Field(
            description=(
                "For action='request_review': concise order summary with items and total. "
                "This is the PRIMARY checkout handoff path when payment is ready."
            )
        ),
    ] = "",
    merchant: Annotated[
        str,
        Field(description="For action='request_review': merchant or venue name if known."),
    ] = "",
    total: Annotated[
        str,
        Field(description="For action='request_review': displayed checkout total if known."),
    ] = "",
    notes: Annotated[
        str,
        Field(description="Optional checkout notes or reason for opening secure card entry."),
    ] = "",
) -> str:
    """List masked payment cards or request secure payment handoff.

    Examples:
    - payment(action="list")
    - payment(action="request_review", order_summary="Large pepperoni pizza, breadsticks, total $35.73", merchant="Domino's", total="$35.73")
    - payment(action="open_secure_card_entry", notes="No saved card is available")
    """
    try:
        from services.payments.payment_vault import get_payment_vault

        vault = get_payment_vault()

        if action == "list":
            cards = vault.list_cards()
            if not cards:
                return "No saved payment cards."
            lines = []
            for card in cards:
                parts = ["%s - **** %s, exp %s" % (card["label"], card["last4"], card["exp"])]
                if card.get("holder_name"):
                    parts.append(", %s" % card["holder_name"])
                lines.append("".join(parts))
            return "Saved cards:\n" + "\n".join("  - %s" % line for line in lines)

        if action == "request_review":
            parts: list[str] = []
            if merchant:
                parts.append("merchant=%s" % merchant)
            if total:
                parts.append("total=%s" % total)
            if order_summary:
                parts.append("summary=%s" % order_summary)
            elif notes:
                parts.append("notes=%s" % notes)
            if not parts:
                parts.append("summary=checkout ready for secure payment review")
            return "PAYMENT REVIEW REQUESTED: " + " | ".join(parts)

        if action == "open_secure_card_entry":
            parts = ["SECURE CARD ENTRY REQUESTED: open the local Viola card-entry UI"]
            if merchant:
                parts.append("merchant=%s" % merchant)
            if total:
                parts.append("total=%s" % total)
            if notes:
                parts.append("notes=%s" % notes)
            return " | ".join(parts)

        return json.dumps({"error": "Unknown payment action: %s" % action})
    except Exception as exc:
        logger.exception("payment failed for action=%s", action)
        return json.dumps({"error": "Error handling payment tool action: %s" % exc})


@server.tool(
    description=(
        "Request explicit legal-signature review before Viola signs or submits a filing, "
        "agreement, or certification page. Use action='request_review' when a page says "
        "that checking a box, clicking Next, or clicking Submit constitutes a legal "
        "signature or certification."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def signature(
    action: Annotated[
        str,
        Field(description="Signature action. Use 'request_review' for the explicit legal-signature handoff."),
    ] = "request_review",
    authority: Annotated[
        str,
        Field(description="Agency, site, or authority receiving the signed document."),
    ] = "",
    document: Annotated[
        str,
        Field(description="Document or filing name being signed."),
    ] = "",
    signer: Annotated[
        str,
        Field(description="Signer name or selected signer label, if visible."),
    ] = "",
    summary: Annotated[
        str,
        Field(description="Concise summary of the key facts or fields being attested to."),
    ] = "",
    page_url: Annotated[
        str,
        Field(description="Current page URL for optional review."),
    ] = "",
    certification: Annotated[
        str,
        Field(description="Short certification or attestation text from the page, if visible."),
    ] = "",
    notes: Annotated[
        str,
        Field(description="Optional extra notes about the signature step."),
    ] = "",
) -> str:
    """Request explicit user review before applying a legal signature."""
    if action != "request_review":
        return json.dumps({"error": "Unknown action: %s" % action})

    parts: list[str] = []
    if authority:
        parts.append("authority=%s" % authority)
    if document:
        parts.append("document=%s" % document)
    if signer:
        parts.append("signer=%s" % signer)
    if summary:
        parts.append("summary=%s" % summary)
    if certification:
        parts.append("certification=%s" % certification)
    elif notes:
        parts.append("notes=%s" % notes)
    if page_url:
        parts.append("page_url=%s" % page_url)
    if not parts:
        parts.append("summary=legal signature review required")
    return "SIGNATURE REVIEW REQUESTED: " + " | ".join(parts)


# ===========================================================================
# Pending gate resume/cancel tools
# ===========================================================================


@server.tool(
    description=(
        "Resume a paused legal-signature checkpoint after the user explicitly confirms signing. "
        "Only call this when the current user reply approves the pending signature gate."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def resume_signature_gate() -> str:
    """Resume a waiting legal-signature checkpoint."""
    user_id = _require_gate_user_id()
    if not user_id:
        return _gate_error("user_id is required to resume a signature gate.")

    _checkpoint, task_id, error = _load_latest_waiting_gate_checkpoint(user_id, "signature")
    if _checkpoint is None:
        return _gate_error(error or "Signature gate is not waiting.", task_id=task_id)

    try:
        from mcp_servers.browser.server import (
            grant_signature_gate_override,
            revoke_signature_gate_override,
        )

        grant_signature_gate_override(task_id)
    except Exception as exc:
        logger.exception("Failed to grant signature gate override for %s", task_id)
        return _gate_error("Could not grant signature gate override: %s" % exc, task_id=task_id)

    resume_reply = "yes"
    result_text = await _resume_checkpoint_from_runtime(task_id, user_id, resume_reply)
    try:
        result_payload = json.loads(result_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        result_payload = {}
    if isinstance(result_payload, dict) and result_payload.get("ok") is False:
        if _signature_resume_failure_preserves_override(result_payload):
            logger.info(
                "Preserving signature override for %s after resume failed before a gate-consuming action",
                task_id,
            )
            return result_text
        try:
            revoke_signature_gate_override(task_id)
        except RuntimeError:
            logger.debug(
                "Failed to revoke signature override after resume failure for %s",
                task_id,
            )
    return result_text


@server.tool(
    description=(
        "Cancel a paused legal-signature checkpoint after the user declines signing. "
        "Only call this when the current user reply rejects the pending signature gate."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def cancel_signature_gate() -> str:
    """Mark a waiting legal-signature checkpoint as declined."""
    user_id = _require_gate_user_id()
    if not user_id:
        return _gate_error("user_id is required to cancel a signature gate.")

    checkpoint, task_id, error = _load_latest_waiting_gate_checkpoint(user_id, "signature")
    if checkpoint is None:
        return _gate_error(error or "Signature gate is not waiting.", task_id=task_id)
    reason_text = "User declined signature gate."

    try:
        from core.user_context import user_scope
        from intent.task_checkpoint import mark_complete

        checkpoint.context["pending_gate_decline_reason"] = reason_text
        with user_scope(user_id):
            mark_complete(checkpoint, "completed", "signature_gate_declined")
        return _gate_payload(
            True,
            task_id=task_id,
            outcome="signature_gate_declined",
            message="Signature gate cancelled.",
        )
    except Exception as exc:
        logger.exception("Failed to cancel signature gate %s", task_id)
        return _gate_error("Could not cancel signature gate: %s" % exc, task_id=task_id)


@server.tool(
    description=(
        "Resume a pending payment confirmation after the user explicitly approves it. "
        "This only succeeds when the secure payment session already has a selected card."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def resume_payment_gate() -> str:
    """Approve a pending payment confirmation session."""
    _confirmation_text = "yes"
    user_id = _require_gate_user_id()
    if not user_id:
        return _payment_gate_error("user_id is required to resume a payment gate.")

    token = ""
    try:
        from services.payments.confirmation import (
            ConfirmationStatus,
            build_public_confirmation_url,
        )

        checkpoint, task_id, checkpoint_error = _load_latest_waiting_gate_checkpoint(user_id, "payment")
        mgr, session, token, error = _resolve_latest_payment_gate(user_id, allow_confirmed=True)
        if session is None and mgr is not None and token:
            try:
                refreshed = await mgr.refresh_from_cloud_decision(
                    token,
                    user_id=user_id,
                    allow_missing=True,
                )
            except Exception as exc:
                logger.warning("Payment gate cloud refresh failed before local lookup: %s", exc)
                refreshed = None
            if refreshed is not None:
                session = refreshed
                error = None
        if session is None or mgr is None:
            return _payment_gate_error(error or "Payment gate is not pending.", token=token)

        if (
            session.status == ConfirmationStatus.PENDING
            and not str(getattr(session, "selected_card_label", "") or "").strip()
        ):
            try:
                refreshed = await mgr.refresh_from_cloud_decision(token, user_id=user_id)
            except Exception as exc:
                logger.warning(
                    "Payment gate cloud refresh failed; using local pending session: %s",
                    exc,
                )
                refreshed = None
            if refreshed is not None:
                session = refreshed

        if session.status == ConfirmationStatus.REJECTED:
            return _payment_gate_error("Payment gate was rejected by the user.", token=token)

        card_label = str(getattr(session, "selected_card_label", "") or "").strip()
        if not card_label:
            return _payment_gate_error(
                "Payment gate still needs secure card selection on the confirmation page: %s"
                % build_public_confirmation_url(token),
                token=token,
            )

        if session.status == ConfirmationStatus.PENDING:
            return _payment_gate_error(
                "Payment gate still needs local PIN confirmation on the confirmation page: %s"
                % build_public_confirmation_url(token),
                token=token,
            )
        if session is None or session.status != ConfirmationStatus.CONFIRMED:
            status = str(getattr(getattr(session, "status", None), "value", "") or "unknown")
            return _payment_gate_error("Payment gate is not confirmed yet (state: %s)." % status, token=token)

        if checkpoint is not None and task_id:
            result_text = await _resume_checkpoint_from_runtime(task_id, user_id, _confirmation_text)
            result_payload = _parse_tool_payload(result_text)
            if not bool(result_payload.get("ok", False)):
                return result_text
            message = str(result_payload.get("message") or "")
            stale_payment_wait = message.startswith("PAYMENT_GATE:") and "waiting" in message.lower()
            if not message or stale_payment_wait:
                result_payload["message"] = "Payment gate approved; continuing checkout securely."
            result_payload["token"] = token
            result_payload["outcome"] = "payment_gate_confirmed"
            return json.dumps(result_payload, ensure_ascii=False, default=str)

        if checkpoint_error:
            logger.info(
                "Payment gate confirmed without resumable checkpoint: %s",
                checkpoint_error,
            )
        return _gate_payload(
            True,
            token=token,
            outcome="payment_gate_confirmed",
            message="Payment gate approved; continuing checkout securely.",
        )
    except Exception as exc:
        logger.exception("Failed to resume payment gate %s", token[:8])
        return _payment_gate_error("Could not resume payment gate: %s" % exc, token=token)


@server.tool(
    description=("Cancel a pending payment confirmation after the user declines payment or asks for changes."),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def cancel_payment_gate() -> str:
    """Reject a pending payment confirmation session."""
    user_id = _require_gate_user_id()
    if not user_id:
        return _gate_error("user_id is required to cancel a payment gate.")

    token = ""
    try:
        mgr, session, token, error = _resolve_latest_payment_gate(user_id)
        if session is None or mgr is None:
            return _gate_error(error or "Payment gate is not pending.", token=token)

        reason_text = "User declined payment gate."
        if not mgr.reject_session(token, reason_text):
            return _gate_error("Payment gate could not be cancelled.", token=token)
        return _gate_payload(
            True,
            token=token,
            outcome="payment_gate_declined",
            message="Payment gate cancelled.",
        )
    except Exception as exc:
        logger.exception("Failed to cancel payment gate %s", token[:8])
        return _gate_error("Could not cancel payment gate: %s" % exc, token=token)


# ===========================================================================
# Phone call tools
# ===========================================================================


@server.tool(
    description=CORE_PHONE_TOOL_DESCRIPTION,
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible_actions": ["call"],
        "irreversible_class": "phone_call",
    },
)
async def phone(
    action: Annotated[
        str,
        Field(description=PHONE_ACTION_DESCRIPTION),
    ] = "status",
    phone_number: Annotated[
        str,
        Field(description=PHONE_NUMBER_DESCRIPTION),
    ] = "",
    task: Annotated[
        str,
        Field(description=TASK_DESCRIPTION),
    ] = "",
    caller_name: Annotated[
        str,
        Field(description=CALLER_NAME_DESCRIPTION),
    ] = "",
    extra_context: Annotated[
        str,
        Field(description=EXTRA_CONTEXT_DESCRIPTION),
    ] = "",
    wait_for_completion: Annotated[
        bool,
        Field(description=WAIT_FOR_COMPLETION_DESCRIPTION),
    ] = False,
    call_id: Annotated[
        str,
        Field(description=CALL_ID_DESCRIPTION),
    ] = "",
) -> str:
    """Manage phone calls.

    Examples:
    - phone(action="call", phone_number="+13125551234", task="Book a haircut for Tuesday at 2pm")
    - phone(action="status", call_id="call_123")
    - phone(action="end", call_id="call_123")
    - phone(action="transcript", call_id="call_123")
    """
    try:
        # #584: every branch below authenticates to the cloud as the calling
        # user, so all of them need THIS request's bearer rather than the one
        # frozen into this server's task when the hub was built.
        with _caller_cloud_bearer_bound():
            if action == "call":
                result = await _make_phone_call(
                    phone_number=phone_number,
                    task=task,
                    caller_name=caller_name,
                    extra_context=extra_context,
                    wait_for_completion=wait_for_completion,
                )
                return _format_result_with_error_data(result)
            if action == "status":
                return _format_result(await _check_call_status(call_id))
            if action == "end":
                return _format_result(await _end_phone_call(call_id))
            if action == "transcript":
                return _format_result(await _get_call_transcript(call_id))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("phone failed for action=%s", action)
        return json.dumps({"error": str(exc)})


@server.tool(
    description=(
        "Speak the user's approved saved payment card to the person on the active phone call. "
        "Only use after PAYMENT_GATE approval via the secure confirmation link. "
        "The agent never sees card digits; the runtime transmits them directly to the call audio."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm", "irreversible": True, "irreversible_class": "payment"},
)
async def transmit_payment_to_call(
    card_label: Annotated[
        str,
        Field(description="Optional saved-card label. Leave blank to use the card selected on the confirmation page."),
    ] = "",
) -> str:
    """Transmit approved payment details to the active phone call without exposing digits to the LLM."""
    try:
        return await _transmit_payment_to_call({"card_label": card_label or None})
    except Exception:
        logger.exception("transmit_payment_to_call failed")
        return json.dumps(
            {"ok": False, "code": "call_not_active"},
            separators=(",", ":"),
            sort_keys=True,
        )


# ===========================================================================
# Bug reporting
# ===========================================================================


@server.tool(
    description=("File a bug report with automatic context from recent agent activity. Returns a bug ID for tracking."),
    annotations=_SAFE,
)
async def file_bug_report(
    message: Annotated[
        str,
        Field(
            description="Clear bug report text describing the problem, expected behavior, what happened instead, and any reproduction details."
        ),
    ],
) -> str:
    """File a bug report on behalf of the user.

    Creates a local bug report with context from recent agent activity.
    Returns a bug ID for tracking.

    Args:
        message: The user's bug description.
    """
    return _format_result(await _file_bug_report(message))


# ===========================================================================
# SMART HOME TOOLS
# The compound ``smart_home`` tool is always registered so the agent can
# route household-device requests to a real tool call (which returns a
# structured "not configured" setup message when a smart-home provider is absent)
# instead of stalling with a clarifying question. The underlying handlers
# in services/smart_home/home_assistant.py check ``client.is_configured``
# and surface the setup CTA themselves.
#
# Music-to-room requests are not smart-home requests. Use media with
# target_room so Viola's in-house multi-room registry and pairing flow run first.
# ===========================================================================


def _smart_home_configured() -> bool:
    """Return True when Home Assistant has both a URL and a token in settings."""
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        url = sm.get("home_assistant_url", "")
        token = sm.get("home_assistant_token", "")
        return bool(url and token)
    except Exception:
        return False


_HA_CONFIGURED = _smart_home_configured()
if not _HA_CONFIGURED:
    logger.info(
        "Home Assistant provider not configured - compound smart_home "
        "tool still registered; handlers will return the setup CTA."
    )


@server.tool(
    description=(
        "Viola's in-house multi-room audio uses rooms, room groups, and paired "
        "speakers (Spoke devices), separate from this smart_home tool. "
        "Music-to-room requests such as 'play music in the kitchen' are covered by media with target_room and can return Viola's Add Speaker QR pairing flow. "
        "Control smart home devices and scenes - lights, thermostats, locks, "
        "switches, fans, garage doors, alarms, scenes. Useful for "
        "requests to turn on/off, dim, lock/unlock, or set temperature on "
        "household devices. Actions: list (discover devices, optionally by "
        "domain), state (read one entity), control (turn_on/turn_off/toggle/"
        "set_temperature/set_brightness/lock/unlock), scene (activate). "
        "Configured smart-home providers are optional advanced integrations for "
        "household-device control, at the same priority as other external device "
        "ecosystems. If "
        "no smart-home provider is configured, the tool returns a setup "
        "message - call it anyway for real smart-home requests so the user "
        "gets the setup CTA instead of a clarifying question."
    ),
    annotations=_CONFIRM,
    meta={
        "risk": "confirm",
        "irreversible_actions": ["unlock", "disarm"],
        "irreversible_class": "smart_home",
    },
)
async def smart_home(
    action: Annotated[
        str,
        Field(description="Smart home action. Use one of: 'list', 'control', 'state', or 'scene'."),
    ] = "list",
    domain: Annotated[
        str,
        Field(description="Optional smart-home domain filter for action='list'."),
    ] = "",
    entity_id: Annotated[
        str,
        Field(description="Configured smart-home entity ID for action='control' or action='state'."),
    ] = "",
    control_action: Annotated[
        str,
        Field(
            description="Device control action for action='control', such as 'turn_on', 'turn_off', or 'set_temperature'."
        ),
    ] = "",
    params: Annotated[
        str,
        Field(description="Optional JSON-encoded parameters for action='control'."),
    ] = "",
    scene_name: Annotated[
        str,
        Field(description="Scene name or entity ID for action='scene'."),
    ] = "",
) -> str:
    """Manage smart home devices and scenes.

    Examples:
    - smart_home(action="list", domain="light")
    - smart_home(action="state", entity_id="light.living_room")
    - smart_home(action="control", entity_id="light.living_room", control_action="turn_on")
    - smart_home(action="scene", scene_name="scene.movie_time")
    """
    try:
        from services.smart_home.home_assistant import (
            smart_home_control_handler,
            smart_home_list_handler,
            smart_home_scene_handler,
            smart_home_state_handler,
        )

        if action == "list":
            return _format_result(await smart_home_list_handler(domain))
        if action == "control":
            return _format_result(await smart_home_control_handler(entity_id, control_action, params))
        if action == "state":
            return _format_result(await smart_home_state_handler(entity_id))
        if action == "scene":
            return _format_result(await smart_home_scene_handler(scene_name))
        return json.dumps({"error": "Unknown action: %s" % action})
    except Exception as exc:
        logger.exception("smart_home failed for action=%s", action)
        return json.dumps({"error": str(exc)})


# ===========================================================================
# PUSH NOTIFICATION TOOLS
# ===========================================================================


@server.tool(
    description=(
        "Compatibility alias for notify. Send an immediate notification to the user. "
        "Prefer notify for new calls; use this only when the model selected the older send_notification name."
    ),
    annotations=_CONFIRM,
    meta={"risk": "confirm"},
)
async def send_notification(
    message: Annotated[
        str,
        Field(
            description="Notification text to deliver to the user. Keep it concise and include the important result or next action."
        ),
    ],
    priority: Annotated[
        str,
        Field(
            description="Delivery priority. Use 'normal' by default, 'high' for time-sensitive alerts, 'urgent' for immediate escalation to all channels, or 'low' for silent logging."
        ),
    ] = "normal",
) -> str:
    """Compatibility alias for notify.

    The older tool name caused duplicate surface choices with different
    approval behavior. Route it through the same user-scoped handler as
    notify so both names share behavior and audit policy.
    """
    try:
        return _format_result(
            await _call_user_scoped(
                _notify_handler,
                message=message,
                when=None,
                channel=None,
            )
        )
    except Exception as exc:
        logger.exception("send_notification failed")
        return json.dumps({"error": str(exc)})


_MCP_SURFACE_REPLACEMENTS: dict[str, str] = {
    "gmail_send": "share_response(destination_kind='email')",
    "gmail_inbox": "gmail(action='inbox')",
    "gmail_read": "gmail(action='read')",
    "gmail_draft_reply": "gmail(action='draft_reply')",
    "gmail_search": "gmail(action='search')",
    "gmail_daily_summary": "gmail(action='daily_summary')",
    "send_notification": "notify",
    "desktop_observe": "computer(action='list_windows'|'read_window'|'screenshot')",
    "desktop_volume": "computer(action='volume')",
}


def _launch_gated_core_tools() -> set[str]:
    try:
        from services.oauth.google import is_google_restricted_features_enabled

        if is_google_restricted_features_enabled():
            return set()
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.debug("Restricted Google launch-gate lookup failed closed: %s", exc)
    return {"gmail", "google_workspace"}


def _trim_legacy_core_tool_surface() -> None:
    """Remove duplicate legacy names after keeping their callable handlers."""
    for tool_name in {*_MCP_SURFACE_REPLACEMENTS, *_launch_gated_core_tools()}:
        server._tool_manager._tools.pop(tool_name, None)


_trim_legacy_core_tool_surface()


# ===========================================================================
# Factory and utility
# ===========================================================================


def create_core_tools_server() -> FastMCP:
    """Return the configured core tools MCP server instance."""
    return server


def get_tool_count() -> int:
    """Return number of registered MCP tools."""
    return len(server._tool_manager._tools)
