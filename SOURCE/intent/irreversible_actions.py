"""Runtime classification for irreversible agent tool calls."""

from __future__ import annotations

from typing import Any, Mapping

_META_TRUE_VALUES = {True, "true", "1", "yes"}

_DIRECT_ACTION_CLASSES: dict[str, str] = {
    "phone_call": "phone_call",
    "make_phone_call": "phone_call",
    "gmail_send": "send_email",
    "gmail.send": "send_email",
    "gmail_sendDraft": "send_email",
    "gmail.sendDraft": "send_email",
    "send_email": "send_email",
    "email_send": "send_email",
    "sms_send": "send_sms",
    "send_sms": "send_sms",
    "telegram_send": "send_message",
    "chat_sendMessage": "send_message",
    "chat_sendDm": "send_message",
    "share_response": "share_response",
    "delete_file": "file_delete",
    "file_delete": "file_delete",
    "calendar_delete": "calendar_delete",
    "calendar_deleteEvent": "calendar_delete",
    "delete_calendar_event": "calendar_delete",
    "calendar_add": "calendar_write",
    "calendar_addEvent": "calendar_write",
    "calendar_create": "calendar_write",
    "calendar_createEvent": "calendar_write",
    "calendar_create_event": "calendar_write",
    "calendar_update": "calendar_write",
    "calendar_updateEvent": "calendar_write",
    "calendar_update_event": "calendar_write",
    "calendar_respond": "calendar_write",
    "calendar_respondToEvent": "calendar_write",
    "calendar_respond_to_event": "calendar_write",
    "drive_trashFile": "file_delete",
    "memory_delete": "memory_delete",
    "fill_payment_details": "payment",
    "transmit_payment_to_call": "payment",
    "run_command": "shell_command",
    "execute_shell": "shell_command",
    "shell": "shell_command",
    "powershell": "shell_command",
    # SEC-033: registering an MCP server launches a subprocess (arbitrary code
    # via the named command/args) — an irreversible, RCE-equivalent action that
    # must go through the unified confirmation gate, not just the CONFIRM tier.
    "register_mcp_server": "mcp_register",
    "smart_home_unlock": "smart_home_unlock",
    "smart_home_disarm": "smart_home_disarm",
    "oauth_connect": "oauth_connect",
    "oauth_disconnect": "oauth_disconnect",
    "auth_clear": "oauth_disconnect",
    "connect_music_provider": "oauth_connect",
}

_SEND_EMAIL_ACTIONS = {"send", "send_draft"}
_FILE_WRITE_ACTIONS = {"write", "save", "create", "overwrite"}
_FILE_DELETE_ACTIONS = {"delete", "forget", "trash"}
_CALENDAR_DELETE_ACTIONS = {"delete", "delete_event", "remove", "cancel"}
_CALENDAR_WRITE_ACTIONS = {
    "add",
    "create",
    "create_event",
    "update",
    "update_event",
    "respond",
    "respond_to_event",
}
_MEMORY_DELETE_ACTIONS = {"delete", "forget", "delete_all", "clear"}
_PAYMENT_SAFE_ACTIONS = {"", "list", "request_review", "open_secure_card_entry"}
_PAYMENT_ACTIONS = {"pay", "submit", "confirm", "purchase", "place_order"}
_SMART_HOME_UNLOCK_ACTIONS = {"unlock"}
_SMART_HOME_DISARM_ACTIONS = {"disarm"}
_OAUTH_DISCONNECT_ACTIONS = {"disconnect", "clear", "revoke", "logout", "unlink"}
_OAUTH_CONNECT_ACTIONS = {"connect", "login", "link", "authorize", "auth"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _action(args: Mapping[str, Any] | None) -> str:
    if not args:
        return ""
    return _clean(args.get("action")).lower()


def _meta(schema: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not schema:
        return {}
    raw = schema.get("_meta") or schema.get("meta") or {}
    return raw if isinstance(raw, Mapping) else {}


def _meta_marks_irreversible(meta: Mapping[str, Any], action: str) -> bool:
    if meta.get("irreversible") in _META_TRUE_VALUES:
        return True
    actions = meta.get("irreversible_actions")
    if isinstance(actions, (list, tuple, set, frozenset)):
        return action in {str(item).strip().lower() for item in actions}
    return False


def irreversible_action_class(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the user-confirmation class for an irreversible tool call."""
    name = _clean(tool_name)
    if not name:
        return None

    action = _action(args)
    meta = _meta(schema)
    if _meta_marks_irreversible(meta, action):
        if name == "smart_home":
            control_action = _clean((args or {}).get("control_action")).lower()
            if action in _SMART_HOME_UNLOCK_ACTIONS or control_action in _SMART_HOME_UNLOCK_ACTIONS:
                return "smart_home_unlock"
            if action in _SMART_HOME_DISARM_ACTIONS or control_action in _SMART_HOME_DISARM_ACTIONS:
                return "smart_home_disarm"
        meta_class = _clean(meta.get("irreversible_class"))
        if meta_class:
            return meta_class
        if name == "share_response":
            return _share_response_class(args)
        return _DIRECT_ACTION_CLASSES.get(name, name)

    direct = _DIRECT_ACTION_CLASSES.get(name)
    if direct is not None:
        if name == "share_response":
            return _share_response_class(args)
        return direct

    if name == "mcp_servers" and action == "register":
        return "mcp_register"
    if name == "phone" and action == "call":
        return "phone_call"
    if name == "gmail" and action in _SEND_EMAIL_ACTIONS:
        return "send_email"
    if name == "file_write" and (not action or action in _FILE_WRITE_ACTIONS):
        return "file_write"
    if name in {"file_write", "workbench"} and action in _FILE_DELETE_ACTIONS:
        return "file_delete"
    if name in {"calendar", "google_calendar"}:
        if action in _CALENDAR_DELETE_ACTIONS:
            return "calendar_delete"
        if action in _CALENDAR_WRITE_ACTIONS:
            return "calendar_write"
    if name == "memory" and action in _MEMORY_DELETE_ACTIONS:
        return "memory_delete"
    if name == "payment":
        if action in _PAYMENT_SAFE_ACTIONS:
            return None
        if not action or action in _PAYMENT_ACTIONS:
            return "payment"
    if name.startswith("payment_"):
        return "payment"
    if name == "smart_home":
        control_action = _clean((args or {}).get("control_action")).lower()
        if action in _SMART_HOME_UNLOCK_ACTIONS or control_action in _SMART_HOME_UNLOCK_ACTIONS:
            return "smart_home_unlock"
        if action in _SMART_HOME_DISARM_ACTIONS or control_action in _SMART_HOME_DISARM_ACTIONS:
            return "smart_home_disarm"
    if name in {"google_auth", "oauth"} and action in _OAUTH_DISCONNECT_ACTIONS:
        return "oauth_disconnect"
    if name in {"oauth", "music_accounts"} and action in _OAUTH_CONNECT_ACTIONS:
        return "oauth_connect"

    return None


def is_irreversible_tool_call(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when the tool call requires the unified confirmation gate."""
    return irreversible_action_class(tool_name, args, schema) is not None


def describe_irreversible_action(tool_name: str, args: Mapping[str, Any] | None = None) -> str:
    """Build a short user-facing description for an irreversible action."""
    name = _clean(tool_name)
    params = args or {}
    action_class = irreversible_action_class(name, params)

    if action_class == "phone_call":
        number = _clean(params.get("phone_number") or params.get("number") or params.get("to"))
        return "call %s" % number if number else "place the phone call"
    if action_class == "send_email":
        recipient = _clean(params.get("to") or params.get("recipient") or params.get("destination"))
        return "send the email to %s" % recipient if recipient else "send the email"
    if action_class == "send_sms":
        recipient = _clean(params.get("to") or params.get("phone_number") or params.get("destination"))
        return "send the text message to %s" % recipient if recipient else "send the text message"
    if name == "share_response":
        kind = _clean(params.get("destination_kind")).lower() or "destination"
        destination = _clean(params.get("destination"))
        return "share this by %s to %s" % (kind, destination) if destination else "share this by %s" % kind
    if action_class == "file_delete":
        path = _clean(params.get("path") or params.get("filename") or params.get("file_id"))
        return "delete %s" % path if path else "delete the file"
    if action_class == "file_write":
        path = _clean(params.get("path") or params.get("filename") or params.get("file_id"))
        return "write %s" % path if path else "write the file"
    if action_class == "shell_command":
        command = _clean(params.get("command"))
        return "run shell command %s" % command if command else "run the shell command"
    if action_class == "mcp_register":
        server = _clean(params.get("name"))
        command = _clean(params.get("command"))
        raw_args = params.get("args") or []
        arg_str = " ".join(str(a) for a in raw_args) if isinstance(raw_args, (list, tuple)) else _clean(raw_args)
        full_cmd = (command + (" " + arg_str if arg_str else "")).strip()
        if server and full_cmd:
            return "register MCP server %s which runs: %s" % (server, full_cmd)
        if full_cmd:
            return "register an MCP server which runs: %s" % full_cmd
        return "register an MCP server"
    if action_class == "calendar_delete":
        event = _clean(params.get("event_id") or params.get("title") or params.get("summary"))
        return "delete the calendar event %s" % event if event else "delete the calendar event"
    if action_class == "calendar_write":
        event = _clean(params.get("title") or params.get("summary") or params.get("event_id"))
        action = _clean(params.get("action")).lower()
        verb = {
            "add": "create",
            "create": "create",
            "create_event": "create",
            "update": "update",
            "update_event": "update",
            "respond": "respond to",
            "respond_to_event": "respond to",
        }.get(action, "change")
        return "%s the calendar event %s" % (verb, event) if event else "%s the calendar event" % verb
    if action_class == "memory_delete":
        target = _clean(params.get("path") or params.get("query") or params.get("section_title"))
        return "delete the memory entry %s" % target if target else "delete memory"
    if action_class == "payment":
        return "submit payment details"
    if action_class == "shell_command":
        command = _clean(params.get("command") or params.get("cmd") or params.get("script"))
        return "run the command: %s" % command if command else "run the shell command"
    if action_class == "smart_home_unlock":
        entity = _clean(params.get("entity_id") or params.get("device") or params.get("name"))
        return "unlock %s" % entity if entity else "unlock the smart-home device"
    if action_class == "smart_home_disarm":
        entity = _clean(params.get("entity_id") or params.get("device") or params.get("name"))
        return "disarm %s" % entity if entity else "disarm the smart-home system"
    if action_class == "oauth_connect":
        provider = _clean(params.get("provider") or params.get("service") or params.get("service_name"))
        return "connect %s" % provider if provider else "connect the account"
    if action_class == "oauth_disconnect":
        provider = _clean(params.get("provider") or params.get("service") or params.get("service_name"))
        return "disconnect %s" % provider if provider else "disconnect the account"

    if params:
        summary = ", ".join("%s=%s" % (key, value) for key, value in list(params.items())[:3])
        return "%s(%s)" % (name, summary)
    return name.replace("_", " ")


def _share_response_class(args: Mapping[str, Any] | None) -> str:
    kind = _clean((args or {}).get("destination_kind")).lower()
    if kind in {"email", "mail", "gmail"}:
        return "send_email"
    if kind in {"sms", "text", "message", "sms_message"}:
        return "send_sms"
    if kind == "file":
        return "share_response_file"
    return "share_response"
