"""Shared utilities for OpenAI-related LLM providers.

Constants, tool-name sanitisation, code-fence stripping, MCP-to-OpenAI
schema conversion, and Responses API helpers that are used by
``openai_direct``, ``openai_compatible``, and ``openai_agents_provider``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# OpenAI requires tool names to match ^[a-zA-Z0-9_-]{1,64}$.
OPENAI_TOOL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
OPENAI_TOOL_NAME_MAX_LEN = 64
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_RESPONSES_SERVER_ONLY_KEYS = frozenset({"id", "status"})
RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID = "previous_response_id"
RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS = "response_items"
RESPONSES_CONTINUITY_COMPACTED_WINDOW_KEYS = frozenset(
    {
        "compacted_window",
        "preserve_compacted_window",
        "standalone_compact_output",
    }
)
_RESPONSES_CONTINUITY_MODE_STORED = "stored"
_RESPONSES_CONTINUITY_MODE_ENCRYPTED_REASONING = "encrypted_reasoning"
_RESPONSES_REDACTED_TEXT_KEYS = frozenset(
    {
        "arguments",
        "content",
        "image_url",
        "input",
        "instructions",
        "output",
        "refusal",
        "text",
    }
)
_RESPONSES_VISUAL_HISTORY_OUTPUT_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_interact",
        "browser_fill_form",
        "browser_run_script",
        "computer",
    }
)
_RESPONSES_VISUAL_HISTORY_OUTPUT_MAX_BYTES = 50_000
_STALE_VERIFIER_PROMPT_MARKERS = (
    "before finalizing",
    "did the last tool call confirm success",
    "repeat your answer",
)
_INTERNAL_FINAL_TURN_PROMPT_MARKER_SETS = (
    _STALE_VERIFIER_PROMPT_MARKERS,
    (
        "consecutive tool calls have failed",
        "identify the root cause",
        "explain to the user what went wrong",
    ),
    (
        "you've been working for a while",
        "summarize what you found",
        "or accomplished so far",
    ),
    (
        "you are stuck in a loop and must stop now",
        "summarize what you accomplished",
        "do not call any more tools",
    ),
    (
        "system note (error patterns detected)",
        "recommendation:",
    ),
)

# ---------------------------------------------------------------------------
# Tool-name sanitisation
# ---------------------------------------------------------------------------


def sanitize_tool_name(name: str) -> str:
    """Return a tool name that passes OpenAI's ``^[a-zA-Z0-9_-]{1,64}$`` check.

    Any character outside ``[a-zA-Z0-9_-]`` is replaced with ``_`` and the
    result is truncated to 64 characters.  The root cause of invalid names is
    the ``server_name.tool_name`` dot-separator used for namespaced external
    MCP servers; that has been fixed in ``mcp_hub/client_hub.py`` (now uses
    ``__``), but this function provides a defensive fallback for any future
    external tool names that slip through.
    """
    sanitized = OPENAI_TOOL_NAME_INVALID.sub("_", name)
    return sanitized[:OPENAI_TOOL_NAME_MAX_LEN]


# ---------------------------------------------------------------------------
# Code-fence stripping
# ---------------------------------------------------------------------------


def strip_llm_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM response text.

    Handles ``\\`\\`\\`json ... \\`\\`\\`` wrapping as well as partial/trailing
    fences.  Returns cleaned text ready for JSON parsing.
    """
    content = text.strip()
    # Full fence: ```json\n...\n```  or  ```\n...\n```
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", content, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Leading fence only
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    # Trailing fence only
    if content.endswith("```"):
        content = content[:-3]
    # Trailing incomplete fence fragment
    content = re.sub(r"\s*```(?:json)?\s*$", "", content)
    return content.strip()


def is_stale_verifier_prompt(text: Any) -> bool:
    """Return True for internal verifier/diagnostic nudges replayed before a final turn."""
    if not isinstance(text, str):
        return False
    normalized = " ".join(text.lower().split())
    return any(all(marker in normalized for marker in markers) for markers in _INTERNAL_FINAL_TURN_PROMPT_MARKER_SETS)


# ---------------------------------------------------------------------------
# MCP → OpenAI schema conversion
# ---------------------------------------------------------------------------


def mcp_tools_to_openai(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert MCP tool schemas to OpenAI function-calling format.

    Accepts both MCP format (``inputSchema``) and Anthropic format
    (``input_schema``).  Ensures every schema has ``type: object`` and
    conservative ``required`` defaults.

    Args:
        tools: List of tool dicts from MCPClientHub.list_tools().

    Returns:
        List of OpenAI-format tool definitions with ``type: function``.
    """
    result: list[dict[str, Any]] = []
    for tool in tools:
        raw_name = tool["name"]
        clean_name = sanitize_tool_name(raw_name)
        if clean_name != raw_name:
            logger.warning(
                "Tool name sanitized for OpenAI: %r -> %r",
                raw_name,
                clean_name,
            )
        parameters = dict(tool.get("input_schema") or tool.get("inputSchema") or {})
        if "type" not in parameters:
            parameters["type"] = "object"
        if "properties" not in parameters:
            parameters["properties"] = {}
        if "required" not in parameters and parameters["properties"]:
            parameters["required"] = list(parameters["properties"].keys())
        result.append(
            {
                "type": "function",
                "function": {
                    "name": clean_name,
                    "description": tool.get("description", ""),
                    "parameters": parameters,
                },
            },
        )
    return result


# ---------------------------------------------------------------------------
# Responses API helpers
# ---------------------------------------------------------------------------


def _sdk_value_to_jsonable(value: Any) -> Any:
    """Convert SDK objects, dicts, and lists into plain Python data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _sdk_value_to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sdk_value_to_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _sdk_value_to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_") and item is not None
        }
    return value


def response_output_item_to_dict(item: Any) -> dict[str, Any]:
    """Convert a Responses SDK output item to a plain dict."""
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", exclude_none=True)
    if isinstance(item, dict):
        return _sdk_value_to_jsonable(item)
    if hasattr(item, "__dict__"):
        converted = _sdk_value_to_jsonable(item)
        if isinstance(converted, dict):
            return converted
    return {}


def _strip_responses_server_fields(value: Any) -> Any:
    """Remove server-assigned fields before replaying output items as input."""
    if isinstance(value, dict):
        return {
            key: _strip_responses_server_fields(item)
            for key, item in value.items()
            if key not in _RESPONSES_SERVER_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_strip_responses_server_fields(item) for item in value]
    return value


def response_output_item_to_input_item(item: Any) -> dict[str, Any] | None:
    """Convert a continuity item into a replayable Responses input item."""
    item_dict = response_output_item_to_dict(item)
    if isinstance(item_dict.get("role"), str):
        return _strip_responses_server_fields(item_dict)
    item_type = item_dict.get("type")
    if item_type not in {
        "message",
        "reasoning",
        "function_call",
        "compaction",
        "compaction_item",
        "function_call_output",
        "input_text",
        "input_image",
    }:
        return None
    return _strip_responses_server_fields(item_dict)


def _trim_to_latest_compaction_window(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop replay state that predates the newest compaction item."""
    latest_compaction_index = -1
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("type") in {"compaction", "compaction_item"}:
            latest_compaction_index = index
    if latest_compaction_index < 0:
        return items
    return items[latest_compaction_index:]


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8", errors="replace"))


def _compact_prior_visual_outputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize older high-volume visual tool outputs in stateless Responses replay."""
    call_names: dict[str, str] = {}
    visual_output_indices: list[int] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and isinstance(call_id, str):
            name = item.get("name")
            if isinstance(name, str):
                call_names[call_id] = name
        elif item_type == "function_call_output" and isinstance(call_id, str):
            if call_names.get(call_id) in _RESPONSES_VISUAL_HISTORY_OUTPUT_TOOLS:
                visual_output_indices.append(index)

    if len(visual_output_indices) <= 1:
        return items

    for index in visual_output_indices[:-1]:
        item = items[index]
        output = item.get("output")
        output_bytes = _json_size(output)
        if output_bytes <= _RESPONSES_VISUAL_HISTORY_OUTPUT_MAX_BYTES:
            continue
        call_id = item.get("call_id")
        tool_name = call_names.get(call_id, "visual tool") if isinstance(call_id, str) else "visual tool"
        item["output"] = (
            "[compacted visual history: prior %s output was %d bytes and was elided; "
            "use the latest browser/computer observation output for current refs or screen state.]"
        ) % (tool_name, output_bytes)
    return items


def normalize_response_continuity(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize provider continuity metadata into the canonical executor shape."""
    if not isinstance(payload, dict):
        return {}

    state: dict[str, Any] = {}
    mode_raw = str(payload.get("mode") or payload.get("_mode") or payload.get("continuity_mode") or "").strip()

    response_id = payload.get("response_id") or payload.get("_response_id")
    if isinstance(response_id, str) and response_id.strip():
        state["response_id"] = response_id.strip()

    previous_response_id = payload.get("previous_response_id")
    if previous_response_id is None and mode_raw in {
        RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
        _RESPONSES_CONTINUITY_MODE_STORED,
    }:
        previous_response_id = response_id
    if isinstance(previous_response_id, str) and previous_response_id.strip():
        state["previous_response_id"] = previous_response_id.strip()

    response_items = payload.get("response_items")
    if response_items is None:
        response_items = payload.get("encrypted_reasoning_items")
    if response_items is None:
        response_items = payload.get("reasoning_items")
    if isinstance(response_items, list) and response_items:
        preserve_compacted_window = any(bool(payload.get(key)) for key in RESPONSES_CONTINUITY_COMPACTED_WINDOW_KEYS)
        copied_items = _strip_responses_server_fields(response_items)
        if not preserve_compacted_window:
            copied_items = _trim_to_latest_compaction_window(copied_items)
            copied_items = _compact_prior_visual_outputs(copied_items)
        state["response_items"] = copied_items
        if preserve_compacted_window:
            state["compacted_window"] = True
        state["has_encrypted_reasoning"] = any(
            isinstance(item, dict) and item.get("type") == "reasoning" and bool(item.get("encrypted_content"))
            for item in copied_items
        )

    try:
        input_cursor = max(0, int(payload.get("input_cursor", payload.get("cursor", 0)) or 0))
    except (TypeError, ValueError):
        input_cursor = 0

    if mode_raw in {
        RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID,
        _RESPONSES_CONTINUITY_MODE_STORED,
    }:
        state["mode"] = RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID
    elif mode_raw in {
        RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS,
        _RESPONSES_CONTINUITY_MODE_ENCRYPTED_REASONING,
    }:
        state["mode"] = RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
    elif state.get("previous_response_id"):
        state["mode"] = RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID
    elif state.get("response_items"):
        state["mode"] = RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS

    if not state.get("mode"):
        return {}

    if state["mode"] == RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID:
        state.pop("response_items", None)
        state.pop("has_encrypted_reasoning", None)
    elif not state.get("response_items"):
        return {}

    state["continuity_mode"] = state["mode"]
    state["input_cursor"] = input_cursor
    provider = payload.get("provider")
    if isinstance(provider, str) and provider.strip():
        state["provider"] = provider.strip()
    if "has_encrypted_reasoning" not in state:
        state["has_encrypted_reasoning"] = False
    return state


def extract_response_continuity_metadata(
    response: Any,
    *,
    store: bool,
    previous_response_id: str | None = None,
    input_items: list[dict[str, Any]] | None = None,
    preserve_compacted_window: bool = False,
) -> dict[str, Any]:
    """Build provider-facing continuity metadata for one Responses result."""
    replayable_items = [
        item
        for item in (
            response_output_item_to_input_item(output_item) for output_item in (getattr(response, "output", []) or [])
        )
        if item is not None
    ]
    continuity_items = replayable_items
    if not store and isinstance(input_items, list) and input_items:
        continuity_items = [
            item for item in (_strip_responses_server_fields(input_items) + replayable_items) if isinstance(item, dict)
        ]
    return normalize_response_continuity(
        {
            "mode": (
                RESPONSES_CONTINUITY_MODE_PREVIOUS_RESPONSE_ID if store else RESPONSES_CONTINUITY_MODE_RESPONSE_ITEMS
            ),
            "response_id": getattr(response, "id", None),
            "previous_response_id": getattr(response, "id", None) if store else previous_response_id,
            "store": bool(store),
            "has_encrypted_reasoning": any(
                isinstance(item, dict) and item.get("type") == "reasoning" and bool(item.get("encrypted_content"))
                for item in replayable_items
            ),
            "response_items": continuity_items,
            "compacted_window": bool(preserve_compacted_window),
        }
    ) | {"response_id": getattr(response, "id", None), "store": bool(store)}


def _redacted_text(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:12]
    return "[redacted len=%d sha256=%s]" % (len(value), digest)


def redact_responses_payload(payload: Any, *, field_name: str | None = None) -> Any:
    """Return a shape-preserving, text-redacted copy of a Responses payload."""
    if isinstance(payload, dict):
        return {key: redact_responses_payload(value, field_name=key) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact_responses_payload(item, field_name=field_name) for item in payload]
    if isinstance(payload, str) and field_name in _RESPONSES_REDACTED_TEXT_KEYS:
        return _redacted_text(payload)
    return payload


def extract_reasoning_text_from_response(response: Any) -> str:
    """Extract reasoning text or summaries from a Responses API payload."""
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "reasoning":
            continue
        content_items = getattr(item, "content", None) or []
        for block in content_items:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        if content_items:
            continue
        for summary in getattr(item, "summary", None) or []:
            text = getattr(summary, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(part for part in parts if part).strip()


def extract_message_text_from_response(response: Any) -> str:
    """Extract assistant-visible text or refusal text from a Responses payload."""
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "output_text":
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
            elif block_type == "refusal":
                refusal = getattr(block, "refusal", None)
                if refusal:
                    parts.append(str(refusal))
    return "".join(parts).strip()
