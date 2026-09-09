"""Anthropic prompt-cache annotations for provider-bound requests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

CacheTTL = Literal["1h"]
CacheScope = Literal["global"]
CacheEditPlacement = tuple[int, Mapping[str, Any]]


@dataclass(frozen=True)
class AnthropicCachePolicy:
    """Provider-local cache policy for Anthropic request construction."""

    enabled: bool = True
    static_prompt: bool = True
    compact_summary: bool = True
    long_lived_meta: bool = True
    tool_schemas: bool = True
    ttl: CacheTTL | None = None
    scope: CacheScope | None = None
    cache_edits: tuple[Mapping[str, Any], ...] = ()
    pinned_cache_edits: tuple[CacheEditPlacement, ...] = ()
    skip_cache_write: bool = False


@dataclass(frozen=True)
class CacheAnnotatedMessage:
    """Rendered Anthropic message plus the cache annotation applied to it."""

    message: dict[str, Any]
    cache_control: dict[str, Any] | None = None


def _cache_control(policy: AnthropicCachePolicy) -> dict[str, Any]:
    control: dict[str, Any] = {"type": "ephemeral"}
    if policy.ttl is not None:
        control["ttl"] = policy.ttl
    if policy.scope is not None:
        control["scope"] = policy.scope
    return control


def _ensure_content_array(message: dict[str, Any]) -> list[Any]:
    content = message.get("content")
    if isinstance(content, str):
        blocks: list[Any] = [{"type": "text", "text": content}]
        message["content"] = blocks
        return blocks
    if isinstance(content, list):
        blocks = list(content)
        message["content"] = blocks
        return blocks
    blocks = []
    message["content"] = blocks
    return blocks


def _strip_message_cache_controls(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, Mapping) and "cache_control" in block:
                copied = dict(block)
                copied.pop("cache_control", None)
                new_content.append(copied)
                changed = True
            else:
                new_content.append(block)
        if changed:
            message["content"] = new_content


def _last_cacheable_block_index(blocks: Sequence[Any]) -> int | None:
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if not isinstance(block, Mapping):
            continue
        if block.get("type") in {"thinking", "redacted_thinking"}:
            continue
        return index
    return None


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _message_allowed_by_policy(message: Mapping[str, Any], policy: AnthropicCachePolicy) -> bool:
    text = _message_text(message).lstrip()
    if not policy.long_lived_meta and text.startswith("<system-reminder>"):
        return False
    compact_markers = ("compact_summary", "<compact-summary", "<session-memory-compact")
    if not policy.compact_summary and any(marker in text for marker in compact_markers):
        return False
    return True


def _cache_marker_index(messages: Sequence[dict[str, Any]], policy: AnthropicCachePolicy) -> int | None:
    preferred_index = len(messages) - 2 if policy.skip_cache_write else len(messages) - 1
    for index in range(preferred_index, -1, -1):
        if _message_allowed_by_policy(messages[index], policy):
            return index
    return None


def _add_message_breakpoint(
    messages: list[dict[str, Any]],
    policy: AnthropicCachePolicy,
) -> CacheAnnotatedMessage | None:
    if not messages:
        return None
    marker_index = _cache_marker_index(messages, policy)
    if marker_index is None:
        return None

    message = messages[marker_index]
    blocks = _ensure_content_array(message)
    block_index = _last_cacheable_block_index(blocks)
    if block_index is None:
        return None

    block = blocks[block_index]
    if not isinstance(block, Mapping):
        return None
    control = _cache_control(policy)
    blocks[block_index] = {**dict(block), "cache_control": control}
    return CacheAnnotatedMessage(message=message, cache_control=control)


def _normalize_cache_edits_block(block: Mapping[str, Any]) -> dict[str, Any] | None:
    if block.get("type") != "cache_edits":
        return None
    raw_edits = block.get("edits")
    if not isinstance(raw_edits, Sequence) or isinstance(raw_edits, (str, bytes)):
        return None
    edits: list[dict[str, str]] = []
    for raw_edit in raw_edits:
        if not isinstance(raw_edit, Mapping):
            continue
        cache_reference = raw_edit.get("cache_reference")
        if not isinstance(cache_reference, str) or not cache_reference.strip():
            continue
        edit_type = raw_edit.get("type", "delete")
        if edit_type != "delete":
            continue
        edits.append({"type": "delete", "cache_reference": cache_reference.strip()})
    if not edits:
        return None
    return {"type": "cache_edits", "edits": edits}


def _dedupe_cache_edits_block(
    block: Mapping[str, Any],
    seen_delete_refs: set[str],
) -> dict[str, Any] | None:
    normalized = _normalize_cache_edits_block(block)
    if normalized is None:
        return None
    edits: list[dict[str, str]] = []
    for edit in normalized["edits"]:
        ref = edit["cache_reference"]
        if ref in seen_delete_refs:
            continue
        seen_delete_refs.add(ref)
        edits.append(edit)
    if not edits:
        return None
    return {"type": "cache_edits", "edits": edits}


def _insert_block_after_tool_results(content: list[Any], block: dict[str, Any]) -> None:
    last_tool_result_index = -1
    for index, item in enumerate(content):
        if isinstance(item, Mapping) and item.get("type") == "tool_result":
            last_tool_result_index = index

    if last_tool_result_index >= 0:
        insert_pos = last_tool_result_index + 1
        content.insert(insert_pos, block)
        if insert_pos == len(content) - 1:
            content.append({"type": "text", "text": "."})
        return

    insert_index = max(0, len(content) - 1)
    content.insert(insert_index, block)


def _insert_cache_edits(
    messages: list[dict[str, Any]],
    policy: AnthropicCachePolicy,
) -> None:
    seen_delete_refs: set[str] = set()

    for message_index, raw_block in policy.pinned_cache_edits:
        if message_index < 0 or message_index >= len(messages):
            continue
        message = messages[message_index]
        if message.get("role") != "user":
            continue
        block = _dedupe_cache_edits_block(raw_block, seen_delete_refs)
        if block is None:
            continue
        _insert_block_after_tool_results(_ensure_content_array(message), block)

    for raw_block in policy.cache_edits:
        block = _dedupe_cache_edits_block(raw_block, seen_delete_refs)
        if block is None:
            continue
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            _insert_block_after_tool_results(_ensure_content_array(message), block)
            break


def _last_cache_control_message_index(messages: Sequence[dict[str, Any]]) -> int:
    last_index = -1
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(isinstance(block, Mapping) and "cache_control" in block for block in content):
            last_index = message_index
    return last_index


def _add_cache_references_to_prefix_tool_results(messages: list[dict[str, Any]]) -> None:
    last_cache_control_index = _last_cache_control_message_index(messages)
    if last_cache_control_index <= 0:
        return

    for message in messages[:last_cache_control_index]:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id.strip():
                    new_content.append({**dict(block), "cache_reference": tool_use_id})
                    changed = True
                    continue
            new_content.append(block)
        if changed:
            message["content"] = new_content


def _annotate_system_blocks(bundle: dict[str, Any], policy: AnthropicCachePolicy) -> None:
    if not policy.static_prompt:
        return
    system = bundle.get("system")
    if not isinstance(system, list) or not system:
        return
    first = system[0]
    if isinstance(first, Mapping):
        system[0] = {**dict(first), "cache_control": _cache_control(policy)}


def _annotate_tools(bundle: dict[str, Any], policy: AnthropicCachePolicy) -> None:
    if not policy.tool_schemas:
        return
    tools = bundle.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    last = tools[-1]
    if isinstance(last, Mapping):
        tools[-1] = {**dict(last), "cache_control": _cache_control(policy)}


def apply_anthropic_cache_controls(
    rendered_bundle: Mapping[str, Any],
    policy: AnthropicCachePolicy,
) -> dict[str, Any]:
    """Return an Anthropic request shape with Claude-style cache annotations."""

    bundle = deepcopy(dict(rendered_bundle))
    if not policy.enabled:
        return bundle

    messages = bundle.get("messages")
    if isinstance(messages, list):
        typed_messages = [message for message in messages if isinstance(message, dict)]
        if len(typed_messages) == len(messages):
            _strip_message_cache_controls(typed_messages)
            _add_message_breakpoint(typed_messages, policy)
            _insert_cache_edits(typed_messages, policy)
            _add_cache_references_to_prefix_tool_results(typed_messages)
            bundle["messages"] = typed_messages

    _annotate_system_blocks(bundle, policy)
    _annotate_tools(bundle, policy)
    return bundle


# ---------------------------------------------------------------------------
# Provider request-shape helpers
# ---------------------------------------------------------------------------

# Matches Claude Code's ``MAX_NON_STREAMING_TOKENS`` (claude.ts ~3354). When
# the wire layer falls back from streaming to non-streaming after a stream
# failure, ``max_tokens`` must be clamped under the Anthropic API's non-
# streaming ceiling. Exceeding it returns 400 invalid_request_error.
MAX_NON_STREAMING_TOKENS: int = 64_000


def adjust_params_for_non_streaming(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``body`` safe for ``stream=False`` recovery.

    Claude-parity (claude.ts:2406-2417): when retrying after a streaming
    failure, clamp ``max_tokens`` to :data:`MAX_NON_STREAMING_TOKENS` and
    strip the ``stream`` flag so the Anthropic SDK sends a non-streaming
    request.

    The cache-control annotations remain on the body — they are valid for
    both streaming and non-streaming responses.
    """
    adjusted: dict[str, Any] = deepcopy(dict(body))
    adjusted.pop("stream", None)
    max_tokens = adjusted.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > MAX_NON_STREAMING_TOKENS:
        adjusted["max_tokens"] = MAX_NON_STREAMING_TOKENS
        max_tokens = MAX_NON_STREAMING_TOKENS
    thinking = adjusted.get("thinking")
    if isinstance(thinking, Mapping) and isinstance(max_tokens, int):
        budget = thinking.get("budget_tokens")
        if isinstance(budget, int) and budget >= max_tokens:
            adjusted["thinking"] = {**dict(thinking), "budget_tokens": max(1, max_tokens - 1)}
    return adjusted


def count_cache_breakpoints(body: Mapping[str, Any]) -> int:
    """Count the total ``cache_control`` markers across system + tools + messages.

    Claude requires exactly one message-level ``cache_control`` marker per
    request (the system + trailing-tool markers are separate). Used by tests
    and by debug logging to verify breakpoint policy invariants.
    """
    count = 0
    for section in ("system", "tools"):
        for block in body.get(section) or []:
            if isinstance(block, Mapping) and "cache_control" in block:
                count += 1
    for message in body.get("messages") or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and "cache_control" in block:
                count += 1
    return count


__all__ = [
    "MAX_NON_STREAMING_TOKENS",
    "AnthropicCachePolicy",
    "CacheAnnotatedMessage",
    "adjust_params_for_non_streaming",
    "apply_anthropic_cache_controls",
    "count_cache_breakpoints",
]
