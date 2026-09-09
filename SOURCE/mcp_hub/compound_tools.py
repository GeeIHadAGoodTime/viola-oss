"""Google Workspace compound tool proxy.

Consolidates 56 granular Google Workspace MCP tools into 9 compound tools.
Each compound tool exposes an ``action`` enum that dispatches to the original
granular tool, reducing the LLM's tool surface from 56 entries to 9.

The proxy is transparent: ``call_tool("gmail", {"action": "search", ...})``
resolves to ``call_tool("gmail_search", {...})`` internally.

Architecture:
    build_compound_schemas(discovered)  -> list of compound tool schema dicts
    resolve_compound_call(name, args)   -> (real_tool, real_args) or None
    HIDDEN_GRANULAR_TOOLS               -> set of tool names to hide from LLM
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Compound tool registry
# ---------------------------------------------------------------------------
# Each entry maps: compound_name -> list of (action, original_tool, description)
# The ``action`` value is what the LLM passes; ``original_tool`` is the real
# MCP tool name that will be dispatched.

COMPOUND_REGISTRY: dict[str, list[tuple[str, str, str]]] = {
    "gmail": [
        ("search", "gmail_search", "Search Gmail messages by query"),
        ("read", "gmail_get", "Read a specific email by message ID"),
        ("send", "gmail_send", "Send an email"),
        ("draft", "gmail_createDraft", "Create an email draft"),
        ("send_draft", "gmail_sendDraft", "Send a previously created draft"),
        ("modify", "gmail_modify", "Modify message labels (archive, star, etc.)"),
        ("batch_modify", "gmail_batchModify", "Batch modify multiple messages"),
        ("modify_thread", "gmail_modifyThread", "Modify an entire thread's labels"),
        ("download_attachment", "gmail_downloadAttachment", "Download an email attachment"),
        ("list_labels", "gmail_listLabels", "List all Gmail labels"),
        ("create_label", "gmail_createLabel", "Create a new Gmail label"),
    ],
    "google_calendar": [
        ("list_calendars", "calendar_list", "List available calendars"),
        ("list_events", "calendar_listEvents", "List events in a calendar"),
        ("get_event", "calendar_getEvent", "Get details of a specific event"),
        ("create_event", "calendar_createEvent", "Create a new calendar event"),
        ("update_event", "calendar_updateEvent", "Update an existing event"),
        ("delete_event", "calendar_deleteEvent", "Delete a calendar event"),
        ("find_free_time", "calendar_findFreeTime", "Find free time slots"),
        ("respond", "calendar_respondToEvent", "Respond to an event invitation"),
    ],
    "google_chat": [
        ("list_spaces", "chat_listSpaces", "List Chat spaces"),
        ("find_space", "chat_findSpaceByName", "Find a Chat space by name"),
        ("send_message", "chat_sendMessage", "Send a message to a Chat space"),
        ("get_messages", "chat_getMessages", "Get messages from a Chat space"),
        ("send_dm", "chat_sendDm", "Send a direct message"),
        ("find_dm", "chat_findDmByEmail", "Find a DM space by email"),
        ("list_threads", "chat_listThreads", "List threads in a Chat space"),
        ("setup_space", "chat_setUpSpace", "Set up a new Chat space"),
    ],
    "google_docs": [
        ("get_text", "docs_getText", "Get text content of a Google Doc"),
        ("create", "docs_create", "Create a new Google Doc"),
        ("write_text", "docs_writeText", "Write text to a Google Doc"),
        ("replace_text", "docs_replaceText", "Find and replace text in a Doc"),
        ("format_text", "docs_formatText", "Format text in a Google Doc"),
        ("get_suggestions", "docs_getSuggestions", "Get suggested edits from a Doc"),
    ],
    "google_drive": [
        ("search", "drive_search", "Search files in Google Drive"),
        ("download", "drive_downloadFile", "Download a file from Drive"),
        ("move", "drive_moveFile", "Move a file to a different folder"),
        ("trash", "drive_trashFile", "Move a file to the trash"),
        ("rename", "drive_renameFile", "Rename a file"),
        ("find_folder", "drive_findFolder", "Find a folder by name"),
        ("create_folder", "drive_createFolder", "Create a new folder"),
        ("get_comments", "drive_getComments", "Get comments on a Drive file"),
    ],
    "google_sheets": [
        ("get_text", "sheets_getText", "Get text content of a spreadsheet"),
        ("get_range", "sheets_getRange", "Get data from a specific range"),
        ("get_metadata", "sheets_getMetadata", "Get spreadsheet metadata"),
    ],
    "google_slides": [
        ("get_text", "slides_getText", "Get text content of a presentation"),
        ("get_metadata", "slides_getMetadata", "Get presentation metadata"),
        ("get_images", "slides_getImages", "Get images from a presentation"),
        ("get_thumbnail", "slides_getSlideThumbnail", "Get a slide thumbnail"),
    ],
    "google_people": [
        ("get_profile", "people_getUserProfile", "Get a user's profile"),
        ("get_me", "people_getMe", "Get the authenticated user's profile"),
        ("get_relations", "people_getUserRelations", "Get a user's relations"),
    ],
    "google_auth": [
        ("clear", "auth_clear", "Clear authentication credentials"),
        ("refresh", "auth_refreshToken", "Manually refresh the auth token"),
    ],
}


# Granular tools to hide from the LLM (they remain callable internally).
# Built from COMPOUND_REGISTRY + redundant time tools.
HIDDEN_GRANULAR_TOOLS: set[str] = set()

# Populate from registry
for _actions in COMPOUND_REGISTRY.values():
    for _action, _original, _desc in _actions:
        HIDDEN_GRANULAR_TOOLS.add(_original)

# Also hide redundant time tools (system context already provides date/time).
HIDDEN_GRANULAR_TOOLS |= {
    "time_getCurrentDate",
    "time_getCurrentTime",
    "time_getTimeZone",
}

# Compound name -> action -> original tool name (fast lookup)
_ACTION_MAP: dict[str, dict[str, str]] = {}
for _compound, _actions in COMPOUND_REGISTRY.items():
    _ACTION_MAP[_compound] = {action: original for action, original, _desc in _actions}


def build_compound_schemas(
    discovered_schemas: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Generate compound tool schemas from discovered granular schemas.

    For each compound tool, builds a schema with:
    - ``action`` enum parameter listing all available actions
    - All parameters from all child tools (flattened, deduplicated)
    - Per-action descriptions in the tool description

    Args:
        discovered_schemas: Map of tool_name -> schema dict from MCP discovery.

    Returns:
        List of compound tool schema dicts ready for registration.
    """
    compound_schemas: list[dict[str, Any]] = []

    for compound_name, actions in COMPOUND_REGISTRY.items():
        # Check which actions are actually available (server may have some disabled)
        available_actions: list[tuple[str, str, str]] = []
        for action_name, original_tool, action_desc in actions:
            if original_tool in discovered_schemas:
                available_actions.append((action_name, original_tool, action_desc))

        if not available_actions:
            logger.debug(
                "Compound tool '%s' has no available actions, skipping",
                compound_name,
            )
            continue

        # Build the compound description — explicitly tell the model
        # to pass action as a parameter, not as a dot-separated tool name.
        desc_lines = [
            "Google Workspace compound tool. Pass the action parameter to select an operation. "
            'Example: %s(action="%s", ...). Available actions:' % (compound_name, available_actions[0][0]),
        ]
        for action_name, _original, action_desc in available_actions:
            desc_lines.append("  - %s: %s" % (action_name, action_desc))

        # Collect all parameters from child tools (flattened)
        all_properties: dict[str, Any] = {}
        all_required: list[str] = []

        for _action_name, original_tool, _action_desc in available_actions:
            child = discovered_schemas.get(original_tool, {})
            child_input = child.get("inputSchema", {})
            props = child_input.get("properties", {})
            for prop_name, prop_schema in props.items():
                if prop_name not in all_properties:
                    all_properties[prop_name] = dict(prop_schema)

        # Action parameter (required enum)
        action_enum = [a[0] for a in available_actions]
        all_properties["action"] = {
            "type": "string",
            "enum": action_enum,
            "description": "The action to perform. One of: %s" % ", ".join(action_enum),
        }
        all_required.append("action")

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": all_properties,
            "required": all_required,
        }

        schema_entry: dict[str, Any] = {
            "name": compound_name,
            "description": "\n".join(desc_lines),
            "inputSchema": input_schema,
            # Internal marker — not exposed to LLM
            "_compound": True,
            "_original_name": compound_name,
        }

        compound_schemas.append(schema_entry)
        logger.debug(
            "Built compound schema '%s' with %d actions",
            compound_name,
            len(available_actions),
        )

    logger.info(
        "Built %d compound tool schemas from %d granular tools",
        len(compound_schemas),
        len(HIDDEN_GRANULAR_TOOLS),
    )
    return compound_schemas


def resolve_compound_call(
    name: str,
    args: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Resolve a compound tool call to the underlying granular tool.

    Args:
        name: Tool name (e.g. ``"gmail"``).
        args: Tool arguments including ``action``.

    Returns:
        ``(real_tool_name, real_args)`` if this is a compound tool call,
        ``None`` if the name is not a compound tool.
    """
    action_map = _ACTION_MAP.get(name)
    if action_map is None:
        return None

    action = args.get("action")
    if action is None:
        # No action specified — return error-friendly info
        available = sorted(action_map.keys())
        logger.warning(
            "Compound tool '%s' called without 'action' parameter. " "Available actions: %s",
            name,
            available,
        )
        return None

    real_tool = action_map.get(action)
    if real_tool is None:
        available = sorted(action_map.keys())
        logger.warning(
            "Compound tool '%s' has no action '%s'. Available: %s",
            name,
            action,
            available,
        )
        return None

    # Forward all args except 'action' to the real tool
    real_args = {k: v for k, v in args.items() if k != "action"}

    # Auto-inject calendarId="primary" for calendar tools that require it.
    # The MCP schema mandates calendarId but models rarely provide it —
    # the internal service always uses "primary" anyway.
    if name == "google_calendar" and "calendarId" not in real_args:
        real_args["calendarId"] = "primary"

    logger.debug(
        "Resolved compound call: %s(action=%s) -> %s(%s)",
        name,
        action,
        real_tool,
        sorted(real_args.keys()),
    )
    return (real_tool, real_args)
