"""Message and tool-pair invariants for model-bound history frames."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from core.logging_config import get_logger
from services.conversation.content_normalization import (
    normalize_frame_for_provider,
    normalize_native_messages_for_provider,
)
from services.conversation.context_frames import (
    ContentBlock,
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

logger = get_logger(__name__)

SYNTHETIC_TOOL_RESULT_CONTENT = "[Tool result missing due to internal error]"
REJECT_TOOL_USE_CONTENT = (
    "The user declined this tool action, so the requested change did not happen. "
    "For an attempted file edit, the file still has its previous contents. Stop the "
    "current work and wait for the user's next instruction before taking any further action."
)
ORPHAN_TOOL_RESULT_REMOVED_CONTENT = "[Orphaned tool result removed due to conversation resume]"
ORPHAN_TOOL_RESULT_REPAIR_ORIGIN = "orphan_tool_result_repair"
# Server-side tool block types that carry their result in the same
# assistant message (Claude parity). When found without a matching
# *_tool_result block, they must be stripped from the assistant content
# array — the provider rejects orphan ``server_tool_use`` /
# ``mcp_tool_use`` blocks with a pairing 400.
_SERVER_TOOL_USE_BLOCK_TYPES = frozenset({"server_tool_use", "mcp_tool_use"})
_SERVER_TOOL_RESULT_BLOCK_TYPES = frozenset({"server_tool_result", "mcp_tool_result"})


def get_strict_pairing_mode() -> bool:
    """Return True when strict tool-result pairing mode is enabled.

    Strict mode (``VIOLA_STRICT_TOOL_RESULT_PAIRING=1``) makes repair
    raise instead of inserting synthetic placeholders — Claude parity
    with ``getStrictToolResultPairing`` (``utils/messages.ts:5127-
    5131``). Used when the consumer would rather fail the trajectory
    than condition a model on synthetic data.
    """

    raw = os.environ.get("VIOLA_STRICT_TOOL_RESULT_PAIRING")
    if not raw:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class ToolResultPairingError(RuntimeError):
    """Raised in strict mode when tool_use/tool_result pairing is broken."""

    def __init__(self, message: str, *, repaired_messages: int = 0) -> None:
        super().__init__(message)
        self.repaired_messages = repaired_messages


def synthetic_tool_result_content(reason: str = "missing_tool_result", *, errored_tool_description: str = "") -> str:
    """Return Claude-shaped synthetic ``tool_result`` text for a repair reason."""

    reason_text = str(reason or "missing_tool_result").strip()
    if reason_text == "user_interrupted":
        return REJECT_TOOL_USE_CONTENT
    if reason_text == "streaming_fallback":
        return "<tool_use_error>Error: Streaming fallback - tool execution discarded</tool_use_error>"
    if reason_text == "sibling_error":
        desc = str(errored_tool_description or "").strip()
        msg = "Cancelled: parallel tool call %s errored" % desc if desc else "Cancelled: parallel tool call errored"
        return "<tool_use_error>%s</tool_use_error>" % msg
    if reason_text in {
        "missing_tool_result",
        "prior_tool_execution_incomplete",
        "agent_terminated_before_tool_execution",
        "history_trimmed",
    }:
        return SYNTHETIC_TOOL_RESULT_CONTENT
    return "<tool_use_error>%s</tool_use_error>" % reason_text


def synthetic_tool_use_result(reason: str = "missing_tool_result", *, errored_tool_description: str = "") -> str:
    """Return the non-model-visible result summary for synthetic tool results."""

    reason_text = str(reason or "missing_tool_result").strip()
    if reason_text == "user_interrupted":
        return "User rejected tool use"
    if reason_text == "streaming_fallback":
        return "Streaming fallback - tool execution discarded"
    if reason_text == "sibling_error":
        desc = str(errored_tool_description or "").strip()
        return "Cancelled: parallel tool call %s errored" % desc if desc else "Cancelled: parallel tool call errored"
    return reason_text


def build_synthetic_tool_result_block(
    tool_use_id: str,
    reason: str = "missing_tool_result",
    *,
    errored_tool_description: str = "",
) -> dict[str, Any]:
    """Build one Claude-shaped synthetic error ``tool_result`` block."""

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": synthetic_tool_result_content(reason, errored_tool_description=errored_tool_description),
        "is_error": True,
    }


def ensure_tool_result_pairing(frames: list[Frame]) -> list[Frame]:
    """Return frames with tool_use/tool_result pairing repaired."""
    frames = [normalize_frame_for_provider(frame) for frame in frames]
    validate_no_behavioral_bail_examples(frames)
    repaired = dedupe_tool_use_ids(frames)
    repaired = strip_orphan_tool_results(repaired)
    expected_pairs = _expected_tool_pairs(repaired)
    repaired = insert_synthetic_error_tool_results(repaired, expected_pairs)
    repaired = preserve_role_alternation(repaired)
    validate_no_behavioral_bail_examples(repaired)
    return repaired


def strip_orphan_tool_results(frames: list[Frame]) -> list[Frame]:
    """Drop TOOL_RESULT blocks whose tool_use_id has no preceding TOOL_USE."""
    seen_tool_uses: set[str] = set()
    seen_results: set[str] = set()
    stripped: list[Frame] = []

    for frame in frames:
        changed = False
        blocks: list[ContentBlock] = []
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock):
                tool_use_id = block.tool_use_id
                if not tool_use_id or tool_use_id not in seen_tool_uses or tool_use_id in seen_results:
                    changed = True
                    continue
                seen_results.add(tool_use_id)
            blocks.append(block)

        if changed:
            if not blocks:
                if not _has_rendered_message(stripped):
                    stripped.append(build_orphan_tool_result_placeholder_frame(frame))
                for tool_use_id in _tool_use_ids(frame):
                    seen_tool_uses.add(tool_use_id)
                continue
            frame = _replace_blocks(frame, blocks)

        stripped.append(frame)
        for tool_use_id in _tool_use_ids(frame):
            seen_tool_uses.add(tool_use_id)

    return stripped


def build_orphan_tool_result_placeholder_frame(
    source_frame: Frame | None = None,
    *,
    session_id: str | None = None,
    origin: str = ORPHAN_TOOL_RESULT_REPAIR_ORIGIN,
) -> Frame:
    """Build the meta-user placeholder used after stripping leading orphans."""

    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=ORPHAN_TOOL_RESULT_REMOVED_CONTENT),),
        is_meta=True,
        origin=origin,
        task_id=source_frame.task_id if source_frame is not None else None,
        timestamp_ms=source_frame.timestamp_ms if source_frame is not None else 0,
        relevance=source_frame.relevance if source_frame is not None else "always",
        schema_version=source_frame.schema_version if source_frame is not None else 1,
        session_id=session_id or (source_frame.session_id if source_frame is not None else None),
        extra={"reason": "leading_orphan_tool_result_removed"},
    )


def insert_synthetic_error_tool_results(
    frames: list[Frame],
    expected_pairs: Mapping[str, Frame] | Iterable[Frame],
) -> list[Frame]:
    """Insert synthetic error TOOL_RESULT frames for missing tool pairs."""
    expected = _coerce_expected_pairs(expected_pairs)
    result_ids = _tool_result_ids(frames)
    repaired: list[Frame] = []

    for frame in frames:
        repaired.append(frame)
        missing_ids = [
            tool_use_id
            for tool_use_id in _tool_use_ids(frame)
            if tool_use_id in expected and tool_use_id not in result_ids
        ]
        if not missing_ids:
            continue

        synthetic_blocks: list[ContentBlock] = []
        for tool_use_id in missing_ids:
            synthetic_blocks.append(
                ToolResultBlock(
                    tool_use_id=tool_use_id,
                    tool_name=_tool_name_for_id(expected[tool_use_id], tool_use_id),
                    content=synthetic_tool_result_content("missing_tool_result"),
                    is_error=True,
                )
            )
            result_ids.add(tool_use_id)

        first_expected = expected[missing_ids[0]]
        repaired.append(
            Frame(
                kind=FrameKind.TOOL_RESULT,
                role=FrameRole.TOOL,
                blocks=tuple(synthetic_blocks),
                is_meta=True,
                origin="synthetic_error",
                task_id=first_expected.task_id,
                tool_use_id=missing_ids[0] if len(missing_ids) == 1 else None,
                source_tool_assistant_uuid=first_expected.source_tool_assistant_uuid,
                permission_mode=first_expected.permission_mode,
                timestamp_ms=first_expected.timestamp_ms,
                relevance=first_expected.relevance,
                schema_version=first_expected.schema_version,
                extra={"synthetic_reason": "missing_tool_result"},
            )
        )

    return repaired


def dedupe_tool_use_ids(frames: list[Frame]) -> list[Frame]:
    """Remove duplicate TOOL_USE blocks while preserving the first occurrence."""
    seen: set[str] = set()
    deduped: list[Frame] = []

    for frame in frames:
        changed = False
        blocks: list[ContentBlock] = []
        for block in frame.blocks:
            if isinstance(block, ToolUseBlock):
                tool_use_id = block.tool_use_id
                if not tool_use_id or tool_use_id in seen:
                    changed = True
                    continue
                seen.add(tool_use_id)
            blocks.append(block)

        if changed:
            if not blocks:
                continue
            frame = _replace_blocks(frame, blocks)
        deduped.append(frame)

    return deduped


def preserve_role_alternation(frames: list[Frame]) -> list[Frame]:
    """Merge compatible adjacent frames so rendered history alternates roles."""
    alternated: list[Frame] = []
    last_message_index: int | None = None
    last_wire_role: str | None = None

    for frame in frames:
        wire_role = _wire_role(frame)
        if wire_role is None:
            alternated.append(frame)
            continue

        if wire_role == last_wire_role and last_message_index is not None:
            merged = _merge_compatible_frames(alternated[last_message_index], frame)
            if merged is not None:
                alternated[last_message_index] = merged
                continue

        alternated.append(frame)
        last_message_index = len(alternated) - 1
        last_wire_role = wire_role

    return alternated


def validate_no_behavioral_bail_examples(frames: list[Frame]) -> None:
    """Reject meta frames rendered as assistant behavioral examples."""
    for frame in frames:
        if frame.role is FrameRole.ASSISTANT and frame.is_meta:
            raise ValueError(
                "Meta frame cannot be rendered as an assistant example: kind=%s origin=%s"
                % (frame.kind.value, frame.origin or "")
            )


def repair_full_transcript(
    messages: list[dict[str, Any]],
    *,
    strict: bool | None = None,
) -> list[dict[str, Any]]:
    """Validate the entire native message list as a self-contained transcript.

    Full-transcript repair can safely synthesize missing tool results because
    every prior assistant tool call needed to justify a result is expected to be
    present in this same message list.

    Claude parity (S6-013): when ``strict`` is True (or the
    ``VIOLA_STRICT_TOOL_RESULT_PAIRING`` env var is set), any detected
    mismatch raises :class:`ToolResultPairingError` instead of repairing.
    """

    if not messages:
        return []

    if strict is None:
        strict = get_strict_pairing_mode()

    messages = normalize_native_messages_for_provider(messages)
    repaired = _dedupe_native_tool_use_ids(messages)
    repaired = _strip_orphan_server_tool_uses(repaired)
    repaired = _strip_orphan_native_tool_results(repaired)
    pre_synthetic_len = len(repaired)
    repaired = _insert_synthetic_native_tool_results(repaired)
    repaired = _preserve_native_role_alternation(repaired)
    if strict and (len(repaired) != pre_synthetic_len or _has_pairing_mismatch(repaired)):
        raise ToolResultPairingError(
            "Strict mode: tool_use/tool_result pairing mismatch — refusing to repair "
            "with synthetic placeholders. Set VIOLA_STRICT_TOOL_RESULT_PAIRING=0 to "
            "fall back to repair mode.",
            repaired_messages=len(repaired),
        )
    return repaired


def _has_pairing_mismatch(messages: list[dict[str, Any]]) -> bool:
    """Return True if any tool_use has no following tool_result."""

    pending_ids: set[str] = set()
    seen_results: set[str] = set()
    for msg in messages:
        for tool_use_id in _native_tool_use_ids(msg):
            pending_ids.add(tool_use_id)
        result_ids = _native_message_result_ids(msg)
        for rid in result_ids:
            seen_results.add(rid)
    return any(uid not in seen_results for uid in pending_ids)


def _native_message_result_ids(message: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tid = str(block.get("tool_use_id") or "")
                if tid:
                    ids.add(tid)
    tool_call_id = message.get("tool_call_id") or message.get("tool_use_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        ids.add(tool_call_id)
    return ids


def _strip_orphan_server_tool_uses(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ``server_tool_use``/``mcp_tool_use`` blocks lacking same-message results.

    Claude parity (S6-013, ``utils/messages.ts:5205-5243``): server-side
    tool blocks carry their result in the same assistant content array.
    If the stream was interrupted before the result arrived, the orphan
    use block breaks the provider request.
    """

    stripped: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if copied.get("role") != "assistant" or not isinstance(content, list):
            stripped.append(copied)
            continue
        # Collect server-side result IDs in this message.
        server_result_ids: set[str] = set()
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in _SERVER_TOOL_RESULT_BLOCK_TYPES:
                tid = str(block.get("tool_use_id") or block.get("id") or "")
                if tid:
                    server_result_ids.add(tid)
        new_content: list[Any] = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") in _SERVER_TOOL_USE_BLOCK_TYPES:
                tid = str(block.get("id") or "")
                if tid not in server_result_ids:
                    changed = True
                    continue
            new_content.append(block)
        if changed:
            if not new_content:
                # Preserve role alternation: keep an assistant placeholder.
                new_content = [{"type": "text", "text": "[Tool use interrupted]"}]
            copied["content"] = new_content
        stripped.append(copied)
    return stripped


def native_message_tool_use_ids(message: dict[str, Any]) -> list[str]:
    """Return the tool-use ids declared by a single native (Anthropic-shape or
    OpenAI Responses-shape) message.

    Exposed so callers that need to seed delta repair with continuity-known ids
    (Responses ``previous_response_id`` / ``response_items``) don't reach into
    the module-private ``_native_tool_use_ids`` helper.
    """

    return list(_native_tool_use_ids(message))


def repair_delta_messages(
    messages: list[dict[str, Any]],
    *,
    continuity_seen_tool_uses: set[str] | None = None,
    strict: bool | None = None,
) -> list[dict[str, Any]]:
    """Validate native delta messages against provider continuity state.

    Delta batches may contain tool results whose matching OpenAI Responses
    ``function_call`` items live outside this batch. Those continuity ids are
    treated as already-seen tool uses while still stripping real or duplicate
    orphans from the delta itself.

    ``strict`` (Claude parity S6-013): when True, any pairing mismatch
    raises :class:`ToolResultPairingError` instead of silently stripping.
    Defaults to the environment-driven flag.
    """

    if not messages:
        return []

    if strict is None:
        strict = get_strict_pairing_mode()

    messages = normalize_native_messages_for_provider(messages)
    repaired = _dedupe_native_tool_use_ids(messages)
    repaired = _strip_orphan_server_tool_uses(repaired)
    pre_strip_len = sum(len(_native_message_content_blocks(m)) for m in repaired)
    repaired = _strip_orphan_native_tool_results(
        repaired,
        initial_seen_tool_uses=continuity_seen_tool_uses,
    )
    post_strip_len = sum(len(_native_message_content_blocks(m)) for m in repaired)
    repaired = _preserve_native_role_alternation(repaired)
    if strict and post_strip_len != pre_strip_len:
        raise ToolResultPairingError(
            "Strict mode: delta batch contained orphan tool_result blocks.",
            repaired_messages=len(repaired),
        )
    return repaired


def _native_message_content_blocks(message: dict[str, Any]) -> list[Any]:
    content = message.get("content")
    if isinstance(content, list):
        return list(content)
    return []


def ensure_native_message_invariants(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Back-compat alias for full-transcript native message repair."""
    return repair_full_transcript(messages)


def _expected_tool_pairs(frames: list[Frame]) -> dict[str, Frame]:
    expected: dict[str, Frame] = {}
    for frame in frames:
        for tool_use_id in _tool_use_ids(frame):
            expected.setdefault(tool_use_id, frame)
    return expected


def _coerce_expected_pairs(expected_pairs: Mapping[str, Frame] | Iterable[Frame]) -> dict[str, Frame]:
    if isinstance(expected_pairs, Mapping):
        return dict(expected_pairs)
    return _expected_tool_pairs(list(expected_pairs))


def _tool_use_ids(frame: Frame) -> list[str]:
    ids: list[str] = []
    for block in frame.blocks:
        if isinstance(block, ToolUseBlock) and block.tool_use_id:
            ids.append(block.tool_use_id)
    return ids


def _tool_result_ids(frames: list[Frame]) -> set[str]:
    ids: set[str] = set()
    for frame in frames:
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock) and block.tool_use_id:
                ids.add(block.tool_use_id)
    return ids


def _tool_name_for_id(frame: Frame, tool_use_id: str) -> str:
    for block in frame.blocks:
        if isinstance(block, ToolUseBlock) and block.tool_use_id == tool_use_id:
            return block.name
    return "unknown_tool"


def _replace_blocks(frame: Frame, blocks: list[ContentBlock]) -> Frame:
    tool_use_ids = [block.tool_use_id for block in blocks if isinstance(block, ToolUseBlock) and block.tool_use_id]
    tool_result_ids = [
        block.tool_use_id for block in blocks if isinstance(block, ToolResultBlock) and block.tool_use_id
    ]
    kind = frame.kind
    role = frame.role
    tool_use_id = frame.tool_use_id

    if tool_use_ids:
        kind = FrameKind.TOOL_USE
        role = FrameRole.ASSISTANT
        tool_use_id = tool_use_ids[0] if len(tool_use_ids) == 1 else None
    elif tool_result_ids:
        kind = FrameKind.TOOL_RESULT
        role = FrameRole.TOOL
        tool_use_id = tool_result_ids[0] if len(tool_result_ids) == 1 else None
    elif frame.kind is FrameKind.TOOL_USE:
        kind = FrameKind.ASSISTANT_TEXT
        tool_use_id = None
    elif frame.kind is FrameKind.TOOL_RESULT:
        tool_use_id = None

    return replace(frame, kind=kind, role=role, blocks=tuple(blocks), tool_use_id=tool_use_id)


def _wire_role(frame: Frame) -> str | None:
    if frame.role is FrameRole.ASSISTANT:
        return "assistant"
    if frame.role in (FrameRole.USER, FrameRole.META_USER, FrameRole.TOOL):
        return "user"
    return None


def _has_rendered_message(frames: list[Frame]) -> bool:
    return any(_wire_role(frame) is not None for frame in frames)


def _merge_compatible_frames(left: Frame, right: Frame) -> Frame | None:
    if left.role is not right.role:
        return None
    if left.role is FrameRole.ASSISTANT:
        if not _all_blocks(left.blocks, (TextBlock, ToolUseBlock)) or not _all_blocks(
            right.blocks, (TextBlock, ToolUseBlock)
        ):
            return None
    elif left.role is FrameRole.TOOL:
        if not _all_blocks(left.blocks, (ToolResultBlock,)) or not _all_blocks(right.blocks, (ToolResultBlock,)):
            return None
    elif left.role is FrameRole.USER:
        if not _all_blocks(left.blocks, (TextBlock,)) or not _all_blocks(right.blocks, (TextBlock,)):
            return None
    elif left.role is FrameRole.META_USER:
        if not _all_blocks(left.blocks, (TextBlock, SystemReminderBlock)) or not _all_blocks(
            right.blocks,
            (TextBlock, SystemReminderBlock),
        ):
            return None
    else:
        return None
    return _replace_blocks(left, [*left.blocks, *right.blocks])


def _all_blocks(blocks: tuple[ContentBlock, ...], allowed_types: tuple[type, ...]) -> bool:
    return all(isinstance(block, allowed_types) for block in blocks)


def _dedupe_native_tool_use_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []

    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if copied.get("role") == "assistant" and isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id") or "")
                    if not tool_use_id or tool_use_id in seen:
                        continue
                    seen.add(tool_use_id)
                blocks.append(block)
            if not blocks:
                continue
            copied["content"] = blocks
        elif copied.get("role") == "assistant" and isinstance(content, dict) and content.get("_openai_assistant"):
            copied["content"] = _dedupe_openai_assistant_content(content, seen)
        deduped.append(copied)

    return deduped


def _dedupe_openai_assistant_content(content: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    copied = dict(content)
    retained_ids: set[str] = set()
    tool_calls = copied.get("tool_calls")
    if isinstance(tool_calls, list):
        local_tool_call_ids: set[str] = set()
        filtered_tool_calls = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                filtered_tool_calls.append(tool_call)
                continue
            tool_use_id = str(tool_call.get("id") or "")
            if not tool_use_id or tool_use_id in seen or tool_use_id in local_tool_call_ids:
                continue
            local_tool_call_ids.add(tool_use_id)
            retained_ids.add(tool_use_id)
            filtered_tool_calls.append(tool_call)
        copied["tool_calls"] = filtered_tool_calls

    response_items = copied.get("response_items")
    if isinstance(response_items, list):
        local_function_call_ids: set[str] = set()
        filtered_items = []
        for item in response_items:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                filtered_items.append(item)
                continue
            tool_use_id = str(item.get("call_id") or "")
            if not tool_use_id or tool_use_id in seen or tool_use_id in local_function_call_ids:
                continue
            local_function_call_ids.add(tool_use_id)
            retained_ids.add(tool_use_id)
            filtered_items.append(item)
        copied["response_items"] = filtered_items
    seen.update(retained_ids)
    return copied


def _strip_orphan_native_tool_results(
    messages: list[dict[str, Any]],
    *,
    initial_seen_tool_uses: set[str] | None = None,
) -> list[dict[str, Any]]:
    seen_tool_uses: set[str] = set(initial_seen_tool_uses or ())
    seen_results: set[str] = set()
    stripped: list[dict[str, Any]] = []

    for message in messages:
        copied = dict(message)
        content = copied.get("content")
        if _top_level_function_output_id(copied):
            tool_use_id = _top_level_function_output_id(copied)
            if tool_use_id not in seen_tool_uses or tool_use_id in seen_results:
                continue
            seen_results.add(tool_use_id)
        elif copied.get("role") in {"user", "tool"} and isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if not tool_use_id or tool_use_id not in seen_tool_uses or tool_use_id in seen_results:
                        continue
                    seen_results.add(tool_use_id)
                blocks.append(block)
            if not blocks:
                continue
            copied["content"] = blocks
        elif copied.get("role") == "tool":
            tool_use_id = str(copied.get("tool_call_id") or copied.get("tool_use_id") or "")
            if tool_use_id and (tool_use_id not in seen_tool_uses or tool_use_id in seen_results):
                continue
            if tool_use_id:
                seen_results.add(tool_use_id)

        stripped.append(copied)
        for tool_use_id in _native_tool_use_ids(copied):
            seen_tool_uses.add(tool_use_id)

    return stripped


def _insert_synthetic_native_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result_ids = _native_result_ids(messages)
    repaired: list[dict[str, Any]] = []

    for message in messages:
        repaired.append(message)
        missing_ids: list[str] = []
        for tool_use_id in _native_tool_use_ids(message):
            if tool_use_id in result_ids or tool_use_id in missing_ids:
                continue
            missing_ids.append(tool_use_id)
        if not missing_ids:
            continue
        repaired.append(
            {
                "role": "user",
                "content": [
                    build_synthetic_tool_result_block(tool_use_id, "missing_tool_result") for tool_use_id in missing_ids
                ],
            }
        )
        result_ids.update(missing_ids)

    return repaired


def _preserve_native_role_alternation(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alternated: list[dict[str, Any]] = []
    last_message_index: int | None = None
    last_role: str | None = None

    for message in messages:
        wire_role = _native_wire_role(message)
        if wire_role is None:
            alternated.append(message)
            continue
        if wire_role == last_role and last_message_index is not None:
            merged = _merge_native_messages(alternated[last_message_index], message)
            if merged is not None:
                alternated[last_message_index] = merged
                continue
            # Same wire-role but the two messages cannot be merged (e.g. an
            # ``_openai_assistant`` continuity dict followed by a list-content
            # assistant turn, or two adjacent top-level ``function_call`` items
            # from a parallel tool call — ``_merge_native_content`` returns None
            # for any dict/None-content combination). Fall through and KEEP the
            # message as its own entry: the earlier shape ``continue``-d
            # unconditionally and silently DROPPED it, losing a real turn and
            # orphaning any later tool_result / function_call_output paired with
            # it. Mirrors the correct frame-level ``preserve_role_alternation``.
        alternated.append(message)
        last_message_index = len(alternated) - 1
        last_role = wire_role

    return alternated


def _native_tool_use_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    ids: list[str] = []
    if message.get("role") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_use_id = str(block.get("id") or "")
                if tool_use_id:
                    ids.append(tool_use_id)
    elif message.get("role") == "assistant" and isinstance(content, dict) and content.get("_openai_assistant"):
        tool_calls = content.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    tool_use_id = str(tool_call.get("id") or "")
                    if tool_use_id:
                        ids.append(tool_use_id)
        response_items = content.get("response_items")
        if isinstance(response_items, list):
            for item in response_items:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    tool_use_id = str(item.get("call_id") or "")
                    if tool_use_id:
                        ids.append(tool_use_id)
    elif message.get("type") == "function_call":
        tool_use_id = str(message.get("call_id") or "")
        if tool_use_id:
            ids.append(tool_use_id)
    return ids


def _native_result_ids(messages: list[dict[str, Any]]) -> set[str]:
    result_ids: set[str] = set()
    for message in messages:
        top_level_id = _top_level_function_output_id(message)
        if top_level_id:
            result_ids.add(top_level_id)
        if message.get("role") == "tool":
            tool_use_id = str(message.get("tool_call_id") or message.get("tool_use_id") or "")
            if tool_use_id:
                result_ids.add(tool_use_id)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if tool_use_id:
                        result_ids.add(tool_use_id)
        elif isinstance(content, dict) and content.get("_openai_assistant"):
            response_items = content.get("response_items")
            if isinstance(response_items, list):
                for item in response_items:
                    if isinstance(item, dict) and item.get("type") == "function_call_output":
                        tool_use_id = str(item.get("call_id") or "")
                        if tool_use_id:
                            result_ids.add(tool_use_id)
    return result_ids


def _top_level_function_output_id(message: dict[str, Any]) -> str:
    if message.get("type") != "function_call_output":
        return ""
    return str(message.get("call_id") or "")


def _native_wire_role(message: dict[str, Any]) -> str | None:
    role = message.get("role")
    if role == "assistant" or message.get("type") == "function_call":
        return "assistant"
    if role in {"user", "tool"} or message.get("type") == "function_call_output":
        return "user"
    return None


def _merge_native_messages(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    if _native_wire_role(left) != _native_wire_role(right):
        return None
    left_content = left.get("content")
    right_content = right.get("content")
    merged_content = _merge_native_content(left_content, right_content)
    if merged_content is None:
        return None
    return {**left, "role": left.get("role") or right.get("role"), "content": merged_content}


def _merge_native_content(left: Any, right: Any) -> Any:
    if isinstance(left, str) and isinstance(right, str):
        return "\n\n".join(part for part in (left, right) if part)
    if isinstance(left, list) and isinstance(right, list):
        return [*left, *right]
    if isinstance(left, str) and isinstance(right, list):
        return [{"type": "text", "text": left}, *right] if left else list(right)
    if isinstance(left, list) and isinstance(right, str):
        return [*left, {"type": "text", "text": right}] if right else list(left)
    return None


__all__ = [
    "ORPHAN_TOOL_RESULT_REMOVED_CONTENT",
    "ORPHAN_TOOL_RESULT_REPAIR_ORIGIN",
    "SYNTHETIC_TOOL_RESULT_CONTENT",
    "ToolResultPairingError",
    "build_orphan_tool_result_placeholder_frame",
    "build_synthetic_tool_result_block",
    "dedupe_tool_use_ids",
    "ensure_native_message_invariants",
    "ensure_tool_result_pairing",
    "get_strict_pairing_mode",
    "insert_synthetic_error_tool_results",
    "native_message_tool_use_ids",
    "preserve_role_alternation",
    "repair_delta_messages",
    "repair_full_transcript",
    "strip_orphan_tool_results",
    "synthetic_tool_result_content",
    "synthetic_tool_use_result",
    "validate_no_behavioral_bail_examples",
]
