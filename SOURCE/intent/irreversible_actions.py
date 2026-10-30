"""Runtime classification for irreversible agent tool calls."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Mapping
from urllib.parse import urlparse

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

_SEND_EMAIL_ACTIONS = {"send", "send_draft", "reply"}
# Gmail organize actions that change labels. One to five messages just happen
# (all reversible); more than five is a bulk change and asks first.
_GMAIL_ORGANIZE_ACTIONS = {
    "mark_read",
    "mark_unread",
    "star",
    "unstar",
    "archive",
    "unarchive",
    "mark_important",
    "mark_not_important",
    "label",
    "unlabel",
    "untrash",
    "batch_modify",
}
GMAIL_BULK_CHANGE_THRESHOLD = 5
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
    if name == "gmail":
        return _gmail_action_class(action, args)
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
    if name == "file_write":
        # Targeted gates (founder rule 2026-10-24): deletes, changes to files the
        # user never referred to, bulk moves and autostart/program locations
        # ask; everything else is backed up and undoable, so it just happens.
        from intent.file_gates import file_write_action_class

        return file_write_action_class(args)
    if name == "workbench" and action in _FILE_DELETE_ACTIONS:
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
        # Payment extensions may add action names independently of the public
        # core.  Only the small read/review allowlist above is reversible; an
        # unknown non-empty payment action must fail into confirmation rather
        # than silently bypass the irreversible-action gate.
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


def _gmail_message_count(args: Mapping[str, Any] | None) -> int:
    params = args or {}
    ids: set[str] = set()
    raw_ids = params.get("message_ids") or params.get("ids")
    if isinstance(raw_ids, str):
        ids.update(part.strip() for part in raw_ids.replace(";", ",").split(",") if part.strip())
    elif isinstance(raw_ids, (list, tuple, set, frozenset)):
        ids.update(str(item).strip() for item in raw_ids if str(item).strip())
    single = _clean(params.get("message_id"))
    if single:
        ids.add(single)
    return len(ids)


def _gmail_action_class(action: str, args: Mapping[str, Any] | None) -> str | None:
    """Confirm class for one ``gmail`` call (the core tool and the workspace compound).

    Sending (new mail, a draft, or a reply) is ``send_email``; moving mail to
    the trash is ``email_trash``; changing labels on more than five messages at
    once is ``email_bulk``. Reading, searching, drafting and small reversible
    changes (archive, star, mark read on a few emails) just happen.
    """
    if action in _SEND_EMAIL_ACTIONS:
        return "send_email"
    if action == "trash":
        return "email_trash"
    if action in _GMAIL_ORGANIZE_ACTIONS and _gmail_message_count(args) > GMAIL_BULK_CHANGE_THRESHOLD:
        return "email_bulk"
    if action == "batch_modify":
        return "email_bulk"
    return None


def _is_read_only_shell_call(args: Mapping[str, Any] | None) -> bool:
    """A shell call the safety parser proves read-only (echo, dir, git status)."""
    params = args or {}
    command = params.get("command") or params.get("cmd") or params.get("script")
    if not command:
        return False
    from intent.tools.shell import is_read_only_command

    return is_read_only_command(command, params.get("working_directory") or "")


def gate_action_class(
    tool_name: str,
    args: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
    *,
    tainted: bool,
) -> str | None:
    """The class the confirm-or-cancel gate asks about, or None to just do it.

    ``irreversible_action_class`` names what a call IS (and drives the taint
    gate, which must keep seeing every shell command). The gate then lets two
    harmless cases through when no untrusted content is in the conversation
    (founder rule: confirm only at crucial moments):

    * a shell command the parser proves read-only (``echo``, ``dir``,
      ``git status``); it used to ask every time (1.0.5 laptop X8.1);
    * saving an answer as a NEW file (share_response never replaces a file),
      like creating any new file with the file tools.
    """
    action_class = irreversible_action_class(tool_name, args, schema)
    if action_class is None or tainted:
        return action_class
    if action_class == "shell_command" and _is_read_only_shell_call(args):
        return None
    if action_class == "share_response_file":
        return None
    return action_class


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
        recipient = _clean(params.get("recipient_name") or params.get("business_name"))
        if number and recipient:
            return "call %s at %s" % (recipient, number)
        return "call %s" % number if number else "place the phone call"
    if action_class == "send_email":
        recipient = _clean(params.get("to") or params.get("recipient") or params.get("destination"))
        if name == "gmail" and _action(params) == "reply":
            return (
                "send your reply to everyone on that email"
                if params.get("reply_all")
                else "send your reply to that email"
            )
        if name == "gmail" and _action(params) == "send_draft":
            return "send the Gmail draft"
        return "send the email to %s" % recipient if recipient else "send the email"
    if action_class == "email_trash":
        count = _gmail_message_count(params)
        return "move %d emails to the trash" % count if count > 1 else "move that email to the trash"
    if action_class == "email_bulk":
        count = _gmail_message_count(params)
        verb = {
            "mark_read": "mark %d emails as read",
            "mark_unread": "mark %d emails as unread",
            "star": "star %d emails",
            "unstar": "unstar %d emails",
            "archive": "archive %d emails",
            "unarchive": "move %d emails back to the inbox",
            "mark_important": "mark %d emails as important",
            "mark_not_important": "mark %d emails as not important",
            "untrash": "take %d emails out of the trash",
        }.get(_action(params))
        if verb:
            return verb % count
        label = _clean(params.get("label"))
        if _action(params) == "label" and label:
            return "add the label %s to %d emails" % (label, count)
        if _action(params) == "unlabel" and label:
            return "remove the label %s from %d emails" % (label, count)
        return "change %d emails" % count if count else "change those emails"
    if action_class == "send_sms":
        recipient = _clean(params.get("to") or params.get("phone_number") or params.get("destination"))
        return "send the text message to %s" % recipient if recipient else "send the text message"
    if name == "share_response":
        kind = _clean(params.get("destination_kind")).lower() or "destination"
        destination = _clean(params.get("destination"))
        return "share this by %s to %s" % (kind, destination) if destination else "share this by %s" % kind
    if name == "file_write" and action_class is not None:
        from intent.file_gates import describe_file_action

        described = describe_file_action(action_class, params)
        if described:
            return described
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
        # Name the event by its title, never by its storage id: "delete the
        # calendar event local-f21bb6d4-..." told the user nothing (X1.4). The
        # agent executor looks the title and time up before asking; a bare id
        # with no title falls back to the generic wording.
        event = _clean(params.get("title") or params.get("summary"))
        return "delete the calendar event %s" % event if event else "delete that calendar event"
    if action_class == "calendar_write":
        event = _clean(params.get("title") or params.get("summary"))
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


# ---------------------------------------------------------------------------
# Sending through a website in the agent browser (1.0.5 laptop A6.5).
# ---------------------------------------------------------------------------
# A message sent by clicking Send in webmail, a chat app or a social site is as
# final as one sent by a messaging tool, so it asks first too. Only the act
# that sends asks: typing a draft does not. The page URL comes from the agent's
# own browser results, never from the model's arguments.
BROWSER_SEND_CLASS = "browser_send"
_MESSAGING_HOSTS = (
    "mail.google.com",
    "chat.google.com",
    "messages.google.com",
    "outlook.live.com",
    "outlook.office.com",
    "outlook.office365.com",
    "mail.yahoo.com",
    "mail.aol.com",
    "mail.proton.me",
    "mail.zoho.com",
    "fastmail.com",
    "icloud.com",
    "web.whatsapp.com",
    "web.telegram.org",
    "messenger.com",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "reddit.com",
    "discord.com",
    "slack.com",
    "teams.microsoft.com",
    "teams.live.com",
    "bsky.app",
    "threads.net",
)
_SEND_WORD_RE = re.compile(
    r"\b(?:send|sent|post|publish|reply|tweet|share|submit|comment|message)\b",
    re.IGNORECASE,
)
_SEND_KEYS = {"enter", "return", "numpadenter"}
_OPAQUE_REF_RE = re.compile(r"^@?e\d+$", re.IGNORECASE)


def messaging_host(page_url: str | None) -> str | None:
    """The webmail / chat / social host of ``page_url``, or None."""
    try:
        host = (urlparse(str(page_url or "")).hostname or "").lower()
    except ValueError:
        return None
    for known in _MESSAGING_HOSTS:
        if host == known or host.endswith("." + known):
            return host
    return None


def browser_send_action_class(
    tool_name: str,
    args: Mapping[str, Any] | None,
    page_url: str | None,
    describe_target: Callable[[str], str | None] | None = None,
) -> str | None:
    """``browser_send`` when this browser action would send a message, else None.

    ``describe_target`` turns a snapshot ref (``@e12``) into its accessible
    role and name. A ref that cannot be described is treated as a send on a
    messaging site (fails closed).
    """
    if messaging_host(page_url) is None:
        return None
    name = _clean(tool_name)
    params = args or {}
    action = _action(params)
    if name in {"browser_evaluate", "browser_run_script"}:
        return BROWSER_SEND_CLASS
    if name == "browser_press_key" or (name == "browser_interact" and action == "press_key"):
        key = _clean(params.get("key")).lower().replace(" ", "")
        parts = set(key.split("+"))
        return BROWSER_SEND_CLASS if parts & _SEND_KEYS else None
    if name == "browser_click" or (name == "browser_interact" and action == "click"):
        target = _clean(params.get("selector") or params.get("ref") or params.get("element"))
        label = " ".join(part for part in (target, _clean(params.get("text"))) if part)
        if target and _OPAQUE_REF_RE.match(target):
            described = describe_target(target) if describe_target is not None else None
            if not described:
                return BROWSER_SEND_CLASS
            label = "%s %s" % (label, described)
        return BROWSER_SEND_CLASS if _SEND_WORD_RE.search(label) else None
    if name == "browser_fill_form":
        for field in params.get("fields") or ():
            if isinstance(field, Mapping) and _SEND_WORD_RE.search(_clean(field.get("name"))):
                return BROWSER_SEND_CLASS
    return None


def describe_browser_send(page_url: str | None) -> str:
    host = messaging_host(page_url) or "this website"
    return "send a message from %s in the browser" % host
