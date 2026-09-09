"""Auto-compaction of agent context on token-limit overflow (C1).

R5-P0-I (2026-05-30, full Claude Code TS parity): when context fills up
the runtime asks the background LLM to write a coherent prose summary
of the conversation so far (TS analog: ``src/services/compact/compact.ts:387``
``compactConversation``), wraps that summary as ONE user-role message
with an ``is_compact_summary`` flag, and emits a separate boundary
marker carrying the compact metadata. The post-compaction message stream
is ``[boundary, summary, ...recent-tail-preserved-verbatim]`` — recent
tail messages KEEP their original roles and tool_use ids; they are NOT
re-bucketed into prose.

Earlier shape (pre-R5-P0-I) used a Python ``_deterministic_compaction_summary``
that bucketed every message into "What the user asked:" / "What was done:"
sections — that pattern (a) lost message attribution, (b) re-classified
``role:"user"`` system-reminders as user-authored text, and (c) stringified
runtime continuity dicts (the ``_openai_assistant`` records) into the
"What was done" prose. The bucketer is deleted; the parity-shape replacement
calls the LLM with the Claude TS ``<analysis>``/``<summary>`` prompt
verbatim, then strips ``<analysis>``.

A compaction counter prevents infinite loops (max 2 per task).
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from config.defaults import DEFAULT_BACKGROUND_TASK_MODEL, DEFAULT_GPT_MODEL
from core.logging_config import get_logger
from intent.token_budget import _estimate_tokens
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    text_frame,
    tool_result_frame,
    tool_use_frame,
)
from services.conversation.frame_rendering import render_for_anthropic
from services.conversation.typed_compaction import CompactionRequest, compact_frames
from services.openai_background import run_background_openai_response

logger = get_logger(__name__)

_COMPACT_RATIO = 0.6  # summarise oldest 60% of messages
BACKGROUND_COMPACTION_MODEL = DEFAULT_BACKGROUND_TASK_MODEL

# INT-11: Upper bound on per-compaction summariser input size, measured in
# characters.  ~4 chars per token -> 8k tokens ~= 32k chars.  When the oldest
# 60% of history would exceed this, skip the LLM summariser and FIFO-prune
# instead so we never send an oversized request that itself would 400.
_MAX_SUMMARY_INPUT_CHARS = 32_000
_NATIVE_COMPACT_BOUNDARY_KEY = "_viola_compact_boundary"

# R2-P0-1: tool-result truncation budgets. Parity target is Claude Code's
# BASH_MAX_OUTPUT_DEFAULT = 30_000 chars (src/utils/shell/outputLimits.ts:4).
# Tool results larger than this are HEAD-KEPT (first N chars verbatim) with
# a "[M lines truncated]" footer, NOT replaced with a ref-marker stub. The
# old 4_000-char REPLACEMENT pattern caused the Round-2 P0-1 agent-runaway:
# any web_search/web_read result over 4 KB was stripped on the next turn,
# blinding the model to the answer it had already retrieved.
_NATIVE_TOOL_RESULT_CHAR_BUDGET = 30_000
_NATIVE_TOOL_RESULT_MESSAGE_BUDGET = 90_000

# R5-P0-I: header and continuation that wrap the LLM-generated summary.
# Keep the established compact-message shape so the next turn re-enters
# seamlessly without treating the handoff as a new user request.
_COMPACT_SUMMARY_HEADER = (
    "Earlier turns exceeded the available conversation space. The following "
    "summary preserves their relevant context for this ongoing session."
)
_COMPACT_SUMMARY_CONTINUATION = (
    "Carry on with the latest unfinished request using this context. Do not "
    "ask the user to repeat information or answer more questions. Start the "
    "next useful action directly, without announcing the handoff, restating "
    "the summary, or introducing a plan to resume."
)
# When the LLM call itself fails (no quota / network / 400) we still need
# SOMETHING in the summary slot. The fallback is a single line of
# CONTINUATION text, NOT a Python prose-bucketed re-classification. The
# recent-tail messages are preserved verbatim so the model can still see
# the live state; the missing piece is the older context, and lying about
# it with a hand-bucketed re-summary is worse than admitting we lost it.
_COMPACT_FALLBACK_BODY = (
    "Earlier turns were dropped because the background summarizer was "
    "unavailable. The recent uncompressed messages that follow below carry "
    "the live state; continue from there."
)

# Per-message-origin keys that mark a native dict-shaped message as
# runtime-injected meta (system-reminder, runtime-context, compact-boundary,
# tool-reference attachments, etc). Compaction's summarizer-input filter
# excludes these — they are NOT user-authored text and bucketing them by
# ``role == 'user'`` is the bug the R5-P0-I refactor closes.
_NATIVE_META_MARKER_KEYS = frozenset(
    {
        "_viola_compact_boundary",
        "_viola_meta",
        "_viola_system_reminder",
        "_viola_runtime_context",
        "_viola_history_snip",
    }
)
# Pattern matching the stringified ``_openai_assistant`` continuity record
# we used to splat into prose summaries (e.g.
# ``{'_openai_assistant': True, 'role': 'assistant', 'content': '', ...}``).
# Catching it lets the gate flag any future re-introduction at scan time;
# the runtime filter below also drops the shape during summarizer-input
# construction.
_RUNTIME_METADATA_DICT_RE = re.compile(r"\{['\"]_openai_assistant['\"]")


def is_native_compact_boundary_message(message: dict[str, Any]) -> bool:
    """Return True for non-provider compact-boundary control messages."""

    return bool(isinstance(message, dict) and message.get(_NATIVE_COMPACT_BOUNDARY_KEY) is True)


def _native_message_id(message: dict[str, Any]) -> str | None:
    for key in ("uuid", "id", "frame_uuid", "message_uuid"):
        value = message.get(key)
        if value:
            return str(value)
    metadata = message.get("metadata")
    if isinstance(metadata, dict):
        for key in ("uuid", "id", "frame_uuid", "message_uuid"):
            value = metadata.get(key)
            if value:
                return str(value)
    return None


def project_snipped_native_messages(
    messages: list[dict[str, Any]],
    *,
    snipped_message_ids: set[str] | frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Remove HISTORY_SNIP-marked native messages and report freed text chars."""

    snipped = {str(item) for item in (snipped_message_ids or set()) if str(item)}
    projected: list[dict[str, Any]] = []
    freed_chars = 0
    removed = 0
    for message in messages:
        message_id = _native_message_id(message)
        if message.get("_viola_history_snip") is True or (message_id and message_id in snipped):
            freed_chars += len(str(message.get("content", "")))
            removed += 1
            continue
        projected.append(message)
    return projected, {
        "snipped_messages": removed,
        "tokens_freed": max(0, freed_chars // 4),
    }


def messages_after_compact_boundary(
    messages: list[dict[str, Any]],
    *,
    snipped_message_ids: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Project the live replay window after the latest compact boundary."""

    boundary_index: int | None = None
    for index, message in enumerate(messages):
        if is_native_compact_boundary_message(message):
            boundary_index = index
    if boundary_index is None:
        projected = list(messages)
    else:
        projected = list(messages[boundary_index:])
    projected, _ = project_snipped_native_messages(projected, snipped_message_ids=snipped_message_ids)
    return projected


def filter_compact_boundaries_for_provider(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove internal compact-boundary controls before provider rendering."""

    return [copy.deepcopy(message) for message in messages if not is_native_compact_boundary_message(message)]


def project_native_messages_for_provider(
    messages: list[dict[str, Any]],
    *,
    snipped_message_ids: set[str] | frozenset[str] | None = None,
    content_replacement_state: Any | None = None,
    replacement_writer: Any | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the post-boundary provider replay chain with tool-result budgets applied."""

    projected = filter_compact_boundaries_for_provider(
        messages_after_compact_boundary(messages, snipped_message_ids=snipped_message_ids)
    )
    budget_session_id = str(session_id or "default")
    if content_replacement_state is not None:
        from services.conversation.tool_result_storage import apply_tool_result_budget

        projected = apply_tool_result_budget(
            projected,
            content_replacement_state,
            session_id=budget_session_id,
            write_to_transcript=replacement_writer,
        )
    return apply_native_tool_result_budget(
        projected,
        replacement_writer=replacement_writer,
        session_id=budget_session_id,
    )


def _head_keep_truncate(content: str, max_chars: int) -> tuple[str, int]:
    """Truncate ``content`` to first ``max_chars`` + a ``[N lines truncated]`` footer.

    Returns ``(truncated, original_size)``. If ``content`` is already within
    budget, returns it unchanged. Parity-target: Claude Code TS
    ``src/tools/BashTool/utils.ts:147-164`` — head-keep + footer; the
    answer-bearing prefix stays in-context where the model can use it on
    the next turn.
    """
    original_size = len(content)
    if original_size <= max_chars:
        return content, original_size
    head = content[:max_chars]
    remaining_lines = content.count("\n", max_chars) + 1
    truncated = "%s\n\n... [%d lines truncated] ..." % (head, remaining_lines)
    return truncated, original_size


def apply_native_tool_result_budget(
    messages: list[dict[str, Any]],
    *,
    per_result_chars: int = _NATIVE_TOOL_RESULT_CHAR_BUDGET,
    per_message_chars: int = _NATIVE_TOOL_RESULT_MESSAGE_BUDGET,
    replacement_writer: Any | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Truncate oversized native tool_result content head-keep + footer.

    R2-P0-1 (2026-05-30): the previous implementation REPLACED any
    tool_result content over 4_000 chars with a 102-char ref-marker stub
    on the very next agent-loop turn — empirically the root cause of an
    agent-runaway loop on a "what's the population of Tokyo" probe (trace
    ``da21fd52b418``, 38 unnecessary tool calls; the rank-1 web_search
    snippet *"Tokyo's 2026 population is now estimated at 10,316,210"*
    was visible on turn 1, gone on turn 2, and the model spent 37 more
    turns hunting it down again).

    Parity-target: Claude Code TS keeps the first ``max_chars`` verbatim
    and appends ``"\\n\\n... [N lines truncated] ..."`` (see
    ``src/utils/shell/outputLimits.ts:1-13`` and
    ``src/tools/BashTool/utils.ts:147-164``). The model always sees the
    head — which is where the answer almost always lives.

    F-047 (R3-A) resume/fork continuity: a durable
    ``ToolResultReplacementRecord``-shaped record is still written via
    ``replacement_writer`` so the resume path can rehydrate the full
    untruncated content if needed. ``replacement`` now stores the
    truncated text (what the model actually saw), not a stub.
    """

    from services.conversation.tool_result_storage import is_content_already_compacted

    copied = [copy.deepcopy(message) for message in messages]
    max_result = max(80, int(per_result_chars))
    max_message = max(max_result, int(per_message_chars))
    replacement_records: list[dict[str, Any]] = []
    for msg_index, message in enumerate(copied):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        candidates: list[tuple[int, int]] = []
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            block_content = block.get("content", "")
            if is_content_already_compacted(block_content):
                continue
            size = _native_content_len_value(block_content)
            if size > 0:
                candidates.append((block_index, size))
        if not candidates:
            continue

        # Per-result truncation: every block over ``max_result`` is
        # head-kept + footered. Per-message budget is enforced AFTER
        # per-result truncation by shrinking the largest remaining blocks
        # further, oldest-first; no block is ever stub-replaced.
        per_result_budgets: dict[int, int] = {}
        for idx, _size in candidates:
            per_result_budgets[idx] = max_result

        total_size = sum(min(size, max_result) for _, size in candidates)
        if total_size > max_message:
            # Shrink blocks further, largest first, down to a minimum
            # floor of 1_000 chars (still enough to carry a snippet)
            # until total_size fits the per-message budget.
            min_floor = min(1_000, max_result)
            for idx, _size in sorted(candidates, key=lambda item: per_result_budgets[item[0]], reverse=True):
                if total_size <= max_message:
                    break
                current = per_result_budgets[idx]
                if current <= min_floor:
                    continue
                # Halve the budget; capped at min_floor.
                new_budget = max(min_floor, current // 2)
                total_size -= current - new_budget
                per_result_budgets[idx] = new_budget

        for block_index, _ in candidates:
            block = content[block_index]
            if not isinstance(block, dict):
                continue
            original = block.get("content", "")
            if not isinstance(original, str):
                # Non-string content (lists of structured blocks etc.)
                # stays untouched — Claude Code only truncates string output.
                continue
            budget = per_result_budgets.get(block_index, max_result)
            truncated, original_size = _head_keep_truncate(original, budget)
            if truncated == original:
                continue
            block["content"] = truncated
            tool_use_id = str(block.get("tool_use_id") or "unknown")
            # F-047: durable replacement record for resume/fork. The
            # ``replacement`` field now carries the in-context truncated
            # text (what the model saw), not a ref-marker stub.
            replacement_records.append(
                {
                    "kind": "tool-result",
                    "tool_use_id": tool_use_id,
                    "replacement": truncated,
                    "original_chars": original_size,
                    "message_index": msg_index,
                    "session_id": session_id,
                }
            )
    if replacement_records and replacement_writer is not None:
        try:
            replacement_writer(replacement_records)
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.exception(
                "F-047 replacement-record writer failed; truncations applied but not persisted",
            )
    return copied


# ---------------------------------------------------------------------------
# Token-limit error detection
# ---------------------------------------------------------------------------


def is_token_limit_error(exc: BaseException) -> bool:
    """Return True if *exc* indicates recoverable context/media overflow.

    Covers OpenAI (400 + "context_length_exceeded" / "maximum context length"),
    Anthropic (400 + "prompt is too long"), media/request-size failures that
    can recover after history/media stripping, and generic HTTP 400/413 patterns.
    """
    msg = str(exc).lower()
    indicators = (
        "413",
        "context_length_exceeded",
        "document too large",
        "image size",
        "image too large",
        "images too large",
        "maximum context length",
        "maximum image",
        "maximum number of images",
        "prompt is too long",
        "media size",
        "media too large",
        "max_tokens",
        "payload too large",
        "request entity too large",
        "token limit",
        "too many images",
        "request too large",
        "context window",
    )
    # Must also look like a 400/413-class error (not a 500), unless the
    # provider already surfaced one of the canonical overflow strings.
    has_400 = "400" in msg or "413" in msg or "invalid_request_error" in msg or any(ind in msg for ind in indicators)
    return has_400 and any(ind in msg for ind in indicators)


# ---------------------------------------------------------------------------
# Message summarisation
# ---------------------------------------------------------------------------


# The prompt asks the model for an ``<analysis>`` coverage check followed by
# a ``<summary>`` block; the runtime strips ``<analysis>`` and keeps the
# ``<summary>`` body. The numbered section contract remains stable so compact
# handoffs preserve the information needed to resume work.
_TS_COMPACT_PROMPT = """\
Prepare a handoff that lets another assistant continue this conversation accurately. Work only from the supplied conversation; tools are unavailable and must not be called. Return plain text containing an <analysis> block for a brief coverage check, then a <summary> block containing the handoff.

Read the conversation in order. Track the user's requests, corrections and changes of direction alongside the assistant's actions and their observed results. Keep the technical detail needed to resume: decisions and reasons, architecture, code patterns, exact file paths, relevant function signatures and code snippets, edits, errors and their resolutions. Distinguish completed work from attempts, open problems and requested work that remains. Verify the handoff against the conversation for accuracy and omissions.

Inside <summary>, use these numbered sections in this order:
1. Primary Request and Intent: Record the user's goals, explicit requests, constraints and corrections in detail.
2. Key Technical Concepts: Identify the relevant technologies, frameworks and technical ideas.
3. Files and Code Sections: List files inspected, added or changed; explain each relevant part and retain the code detail needed for continuity.
4. Errors and fixes: Describe observed errors, attempted remedies and outcomes, including user feedback about how to proceed.
5. Problem Solving: State what has been resolved and which investigations remain open.
6. All user messages: Preserve every user-authored message that is not a tool result so the next assistant can follow the user's changing intent.
7. Pending Tasks: List the work the user requested that is still unfinished.
8. Current Work: Explain the precise task and state immediately before this handoff.
9. Optional Next Step: Include a next action only when it advances that latest authorized work; do not introduce another objective.

Close </summary> after section 9. Supply the handoff itself, with no tool calls or commentary outside the two required blocks.
"""


def _format_compact_summary(raw: str) -> str:
    """Strip ``<analysis>`` and return the ``<summary>`` body.

    Parity target: ``formatCompactSummary`` in
    ``src/services/compact/prompt.ts`` — Claude TS strips the drafting
    scratchpad and lifts the ``<summary>`` body out (the "Summary:\\n..."
    header is added by ``getCompactUserSummaryMessage``).
    """

    text = raw or ""
    text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", text, count=1)
    match = re.search(r"<summary>([\s\S]*?)</summary>", text)
    if match:
        body = (match.group(1) or "").strip()
        if body:
            return body
        return text.strip()
    return text.strip()


def _is_native_meta_message(message: Any) -> bool:
    """Return True for runtime-injected meta messages.

    These carry compact boundaries, system-reminders, runtime-context
    blocks, history-snip markers, or hook-result envelopes. They are
    NOT user-authored text even when their wire role is ``user``;
    compaction must not bucket them as such.
    """

    if not isinstance(message, dict):
        return False
    for key in _NATIVE_META_MARKER_KEYS:
        if bool(message.get(key)):
            return True
    if message.get("role") == "system":
        return True
    return False


def _content_looks_like_runtime_metadata_dump(value: Any) -> bool:
    """Detect stringified runtime-continuity dicts inside message content.

    The old prose-bucketer ran ``str(msg.get('content'))`` on
    ``_openai_assistant`` continuity records, splatting raw Python dict
    repr into the summary. Catch any future re-introduction at filter
    time and drop it before it reaches the LLM.
    """

    text: str
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        text = "\n".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = repr(value)
    else:
        return False
    return bool(_RUNTIME_METADATA_DICT_RE.search(text))


def _summary_relevant_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out meta/system-reminder/continuity-dict messages for the LLM.

    The summarizer sees ordinary user+assistant turns and their tool
    blocks — the same surface a fresh agent would see. Meta envelopes
    (compact boundaries, runtime-context reminders, history snips) and
    accidental dict-repr blobs are dropped so the LLM does not include
    them as user-asked items in its prose summary.
    """

    filtered: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if _is_native_meta_message(message):
            continue
        if _content_looks_like_runtime_metadata_dump(message.get("content")):
            continue
        filtered.append(message)
    return filtered


def _render_messages_for_summarizer(messages: list[dict[str, Any]]) -> str:
    """Render the filtered messages as a single role-tagged transcript.

    The summarizer gets ``[role] content`` lines so the LLM can produce
    the "All user messages" / "Files and Code Sections" sections from
    the same role typing the next agent will see. Each message keeps
    its native role (user / assistant) — no re-bucketing happens here.
    Tool blocks render in-place with the tool name so files-touched
    inference works.
    """

    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")
        rendered = _render_message_content_for_summarizer(content)
        if not rendered.strip():
            continue
        parts.append("[%s]\n%s" % (role, rendered.strip()))
    return "\n\n".join(parts)


def _render_message_content_for_summarizer(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # An assistant continuity record reached the rendering loop —
        # skip it (the filter should have caught it; render defensively).
        if content.get("_openai_assistant"):
            return ""
        return _native_content_to_text(content)
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype in {"text", "input_text"}:
            parts.append(str(block.get("text", "")))
        elif btype == "tool_use":
            tname = str(block.get("name") or "tool")
            tinput = block.get("input")
            if isinstance(tinput, (dict, list)):
                tinput_repr = json.dumps(tinput, ensure_ascii=False)[:600]
            else:
                tinput_repr = str(tinput)[:600]
            parts.append("[tool_use:%s] %s" % (tname, tinput_repr))
        elif btype == "tool_result":
            inner = block.get("content", "")
            text = _native_content_to_text(inner) if not isinstance(inner, str) else inner
            tname = str(block.get("tool_name") or "tool")
            parts.append("[tool_result:%s]\n%s" % (tname, text[:1500]))
        elif btype in {"image", "input_image"}:
            parts.append("[image]")
        elif btype in {"document", "input_file"}:
            parts.append("[document]")
    return "\n".join(part for part in parts if part)


async def _summarise_messages_with_background_model(messages: list[dict[str, Any]], *, user_id: str = "") -> str:
    """Ask the background LLM for a Claude-shaped prose summary.

    R5-P0-I parity: send the verbatim ``<analysis>``/``<summary>`` prompt
    (``src/services/compact/prompt.ts``) and strip the analysis block on
    return. Runtime-injected meta messages (system-reminders,
    runtime-context envelopes, compact boundaries) are filtered OUT of
    the transcript before it reaches the LLM — see
    ``_summary_relevant_messages``. Returns the summary body (without
    the ``<summary>`` wrapper) so callers can splice it under
    ``_COMPACT_SUMMARY_HEADER``.

    Raises on failure (network / quota / 400 / empty response) so the
    caller can fall back to ``_COMPACT_FALLBACK_BODY``; the fallback is
    a one-line continuation note, NOT bucketed prose.
    """

    relevant = _summary_relevant_messages(messages)
    if not relevant:
        # Nothing genuinely user-authored survived the filter (e.g. the
        # slab was entirely runtime-context reminders). The summarizer
        # would invent content from meta noise; refuse instead and let
        # the caller emit the fallback summary.
        raise ValueError("No user-authored content survived meta-filter for compaction summary")

    transcript = _render_messages_for_summarizer(relevant)
    transcript = transcript[: min(12_000, _MAX_SUMMARY_INPUT_CHARS)]

    raw = await run_background_openai_response(
        system_prompt=_TS_COMPACT_PROMPT,
        user_content=transcript,
        max_output_tokens=1500,
        user_id=user_id,
        model=BACKGROUND_COMPACTION_MODEL,
    )

    body = _format_compact_summary(raw)
    if not body.strip():
        # The LLM returned tool_use only or empty text — the surrounding
        # streaming retry already escalated; surface the empty result so
        # the caller falls back instead of recording a blank summary.
        raise ValueError("Compact summarizer returned no usable text content")
    return body


# ---------------------------------------------------------------------------
# Compaction logic
# ---------------------------------------------------------------------------


def _native_content_len_value(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += _native_content_len_value(block.get("text", block.get("content", "")))
            else:
                total += len(str(block))
        return total
    if content is None:
        return 0
    return len(str(content))


def _summary_input_char_count(messages: list[dict[str, Any]]) -> int:
    return sum(_native_content_len_value(message.get("content", "")) for message in messages)


def _halved_summary_input(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newer half of an oversized summary slab for one retry."""
    if len(messages) <= 1:
        return [], 0
    reduced = messages[len(messages) // 2 :]
    return reduced, _summary_input_char_count(reduced)


# TS parity port: src/services/compact/compact.ts:243-291 truncateHeadForPTLRetry.
# When the summary LLM call would be prompt-too-long, iteratively drop the
# oldest 20% of messages and retry. Keeps at least 1 message group so the
# summarizer has something to summarize. TS uses 3 attempts before giving up
# (MAX_PTL_RETRIES).
_PTL_RETRY_MAX_ATTEMPTS = 3
_PTL_RETRY_DROP_RATIO = 0.2


def _truncate_head_for_ptl_retry(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Drop the oldest 20% of messages for a PTL retry; None when can't shrink further."""
    if len(messages) < 2:
        return None
    drop_count = max(1, int(len(messages) * _PTL_RETRY_DROP_RATIO))
    drop_count = min(drop_count, len(messages) - 1)
    if drop_count < 1:
        return None
    return messages[drop_count:]


def _native_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type in {"text", "input_text"}:
                parts.append(str(block.get("text", "")))
            elif block_type in {"image", "input_image"}:
                parts.append("[image omitted from compacted history]")
            elif block_type in {"document", "input_file"}:
                parts.append("[document omitted from compacted history]")
            elif block_type == "tool_result":
                parts.append(_native_content_to_text(block.get("content", "")))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _compact_fallback_summary(_messages: list[dict[str, Any]]) -> str:
    """Return the fallback summary body when the LLM call is unavailable.

    Parity target: when Claude TS hits ``ERROR_MESSAGE_INCOMPLETE_RESPONSE``
    after retries, it raises — there is no Python-bucketed re-classification
    of the conversation into "What the user asked" / "What was done". Viola
    used to ship such a bucketer (the 2026-05-30 trace ``c9c5d111ce30``
    showed it active); the bucketer (a) re-classified
    ``role:"user"`` system-reminder messages as user-authored requests
    and (b) stringified ``_openai_assistant`` continuity dicts into the
    summary prose.

    The replacement is a one-line continuation note. The recent-tail
    messages preserved after the boundary still carry the live state, so
    the next turn has the working surface it needs; lying about the older
    context with a hand-bucketed re-summary was worse than admitting the
    summarizer hop was unavailable.
    """

    return _COMPACT_FALLBACK_BODY


def _native_messages_to_frames(messages: list[dict[str, Any]]) -> list[Frame]:
    frames: list[Frame] = []
    tool_names: dict[str, str] = {}
    for message in filter_compact_boundaries_for_provider(messages_after_compact_boundary(messages)):
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "user":
            if isinstance(content, str):
                if content.strip():
                    frames.append(text_frame(kind=FrameKind.USER_INPUT, role=FrameRole.USER, text=content))
                continue
            if not isinstance(content, list):
                text = _native_content_to_text(content)
                if text.strip():
                    frames.append(text_frame(kind=FrameKind.USER_INPUT, role=FrameRole.USER, text=text))
                continue
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue
                block_type = block.get("type")
                if block_type == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if not tool_use_id:
                        continue
                    frames.append(
                        tool_result_frame(
                            tool_use_id=tool_use_id,
                            tool_name=tool_names.get(tool_use_id, str(block.get("tool_name") or "tool")),
                            content=_native_content_to_text(block.get("content", "")),
                            is_error=bool(block.get("is_error")),
                        )
                    )
                elif block_type in {"text", "input_text"}:
                    text_parts.append(str(block.get("text", "")))
                elif block_type in {"image", "input_image"}:
                    text_parts.append("[image omitted from compacted history]")
                elif block_type in {"document", "input_file"}:
                    text_parts.append("[document omitted from compacted history]")
            text = "\n".join(part for part in text_parts if part)
            if text.strip():
                frames.append(text_frame(kind=FrameKind.USER_INPUT, role=FrameRole.USER, text=text))
            continue

        if role != "assistant":
            continue

        if isinstance(content, dict) and content.get("_openai_assistant"):
            assistant_text = str(content.get("content") or "")
            if assistant_text.strip():
                frames.append(
                    text_frame(
                        kind=FrameKind.ASSISTANT_TEXT,
                        role=FrameRole.ASSISTANT,
                        text=assistant_text,
                    )
                )
            tool_calls = content.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    tool_use_id = str(call.get("id") or "")
                    name = str(function.get("name") or "")
                    if not tool_use_id or not name:
                        continue
                    try:
                        input_args = json.loads(str(function.get("arguments") or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        input_args = {}
                    if not isinstance(input_args, dict):
                        input_args = {}
                    tool_names[tool_use_id] = name
                    frames.append(tool_use_frame(tool_use_id=tool_use_id, name=name, input_args=input_args))
            continue

        if isinstance(content, str):
            if content.strip():
                frames.append(
                    text_frame(
                        kind=FrameKind.ASSISTANT_TEXT,
                        role=FrameRole.ASSISTANT,
                        text=content,
                    )
                )
            continue
        if not isinstance(content, list):
            text = _native_content_to_text(content)
            if text.strip():
                frames.append(
                    text_frame(
                        kind=FrameKind.ASSISTANT_TEXT,
                        role=FrameRole.ASSISTANT,
                        text=text,
                    )
                )
            continue
        text_parts = []
        for block in content:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_use_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                input_args = block.get("input")
                if not isinstance(input_args, dict):
                    input_args = {}
                if tool_use_id and name:
                    tool_names[tool_use_id] = name
                    frames.append(tool_use_frame(tool_use_id=tool_use_id, name=name, input_args=input_args))
            elif block_type in {"text", "input_text"}:
                text_parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in text_parts if part)
        if text.strip():
            frames.append(text_frame(kind=FrameKind.ASSISTANT_TEXT, role=FrameRole.ASSISTANT, text=text))
    return frames


def _native_boundary_message(result: Any) -> dict[str, Any]:
    compact_metadata = copy.deepcopy(getattr(result.boundary_frame, "extra", {}).get("compact_metadata", {}))
    return {
        "role": "system",
        "content": "Conversation compacted",
        _NATIVE_COMPACT_BOUNDARY_KEY: True,
        "compact_metadata": compact_metadata,
        "compactMetadata": copy.deepcopy(compact_metadata),
    }


def _frames_to_native_messages(frames: list[Frame]) -> list[dict[str, Any]]:
    rendered = render_for_anthropic(PromptFrameBundle(frames=frames))
    messages = rendered.get("messages")
    return messages if isinstance(messages, list) else []


async def compact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_system: bool = True,
    user_id: str = "",
) -> list[dict[str, Any]]:
    """Compact *messages* by summarising the oldest 60%.

    Preserves the first message (system/user request) and the most
    recent 40% of messages.  The middle portion is replaced with a
    single summary message.

    Falls back to FIFO pruning if summarisation fails.

    Args:
        messages: Full message list (will NOT be mutated).
        keep_system: If True, always preserve messages[0].

    Returns:
        A new, shorter message list.
    """
    if len(messages) <= 4:
        return list(messages)

    # Determine split point
    start_idx = 1 if keep_system else 0
    compactable = messages[start_idx:]
    split = max(2, int(len(compactable) * _COMPACT_RATIO))
    # Ensure even split for native mode (user/assistant pairs)
    if split % 2 != 0:
        split += 1
    if split >= len(compactable):
        split = len(compactable) - 2

    to_summarise = compactable[:split]
    to_keep = compactable[split:]

    # INT-11: if the oldest-60% slab would overflow the summariser input
    # ceiling we skip the LLM call entirely and go straight to FIFO pruning.
    # Otherwise a 200k-token history would 400 the request itself.
    _approx_chars = sum(len(str(msg.get("content", ""))) for msg in to_summarise)
    _oversized = _approx_chars > _MAX_SUMMARY_INPUT_CHARS

    if _oversized:
        logger.warning(
            "Compaction input too large (%d chars > %d) — emitting fallback summary for %d messages",
            _approx_chars,
            _MAX_SUMMARY_INPUT_CHARS,
            split,
        )
        summary_body = _compact_fallback_summary(to_summarise)
    else:
        try:
            summary_body = await _summarise_messages_with_background_model(to_summarise, user_id=user_id)
            logger.info(
                "Context compaction: summarised %d messages into %d chars",
                len(to_summarise),
                len(summary_body),
            )
        except Exception:
            # G201 + #9: surface the actual cause (was silently swallowed; in
            # practice this most commonly fires when the background model
            # endpoint hits insufficient_quota / rate-limit / network failure
            # and we could not tell from the log alone).
            logger.exception(
                "Summarisation failed, falling back to TS-shaped continuation note for %d messages",
                split,
            )
            summary_body = _compact_fallback_summary(to_summarise)

    summary_msg = {
        "role": "user",
        "content": "%s\n\n%s\n\n%s"
        % (
            _COMPACT_SUMMARY_HEADER,
            summary_body,
            _COMPACT_SUMMARY_CONTINUATION,
        ),
        "_viola_compact_summary": True,
    }

    result = []
    if keep_system and messages:
        result.append(messages[0])
    result.append(summary_msg)
    result.extend(to_keep)
    return result


async def _compact_native_messages_legacy(
    messages: list[dict[str, Any]],
    *_unused: Any,
    user_id: str = "",
    session_id: str | None = None,
    replacement_writer: Any | None = None,
    content_replacement_state: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Back-compat shim — delegates to the typed-frame ``compact_native_messages``.

    The pre-typed-frame path that used to live here ran a Python
    bucketer (the "What the user asked / What was done" splatter that
    R5-P0-I deleted). It is now a thin forwarder so any latent caller
    (Anthropic-route reactivation, tests) still resolves the symbol.
    """
    return await compact_native_messages(
        messages,
        user_id=user_id,
        session_id=session_id,
        replacement_writer=replacement_writer,
        content_replacement_state=content_replacement_state,
    )


async def compact_native_messages(
    messages: list[dict[str, Any]],
    *_unused: Any,
    user_id: str = "",
    session_id: str | None = None,
    replacement_writer: Any | None = None,
    content_replacement_state: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Compact native provider messages through the typed frame pipeline."""

    budgeted = project_native_messages_for_provider(
        messages,
        content_replacement_state=content_replacement_state,
        replacement_writer=replacement_writer,
        session_id=session_id,
    )
    if len(budgeted) <= 6:
        prepared = filter_compact_boundaries_for_provider(budgeted)
        return prepared, None

    split = max(2, int(len(budgeted) * _COMPACT_RATIO))
    if split >= len(budgeted):
        split = max(0, len(budgeted) - 2)
    to_summarise = budgeted[:split]
    approx_chars = _summary_input_char_count(to_summarise)
    oversized = approx_chars > _MAX_SUMMARY_INPUT_CHARS

    if oversized:
        # TS parity (compact.ts:387-491): iteratively drop oldest 20% of
        # messages and retry the LLM summary until under the size limit OR
        # _PTL_RETRY_MAX_ATTEMPTS exhausted. Only fall back to the generic
        # continuation note when retries are spent — that preserves working
        # memory across compaction, fixing the LLC spin where post-compaction
        # the model re-tried clicks it had already failed (live evidence:
        # task c3a279968c1f step 30 re-clicked _btnStart that failed at step 9
        # because the previous halved-once fallback dropped the failure record).
        summary_body = None
        retry_msgs: list[dict[str, Any]] = to_summarise
        retry_chars = approx_chars
        for attempt in range(_PTL_RETRY_MAX_ATTEMPTS):
            truncated = _truncate_head_for_ptl_retry(retry_msgs)
            if truncated is None:
                break
            truncated_chars = _summary_input_char_count(truncated)
            logger.warning(
                "Native compaction input too large (%d chars > %d) - PTL retry %d/%d with %d messages (%d chars)",
                retry_chars,
                _MAX_SUMMARY_INPUT_CHARS,
                attempt + 1,
                _PTL_RETRY_MAX_ATTEMPTS,
                len(truncated),
                truncated_chars,
            )
            if truncated_chars <= _MAX_SUMMARY_INPUT_CHARS:
                try:
                    summary_body = await _summarise_messages_with_background_model(truncated, user_id=user_id)
                    method = "typed_summary_ptl_retry_%d" % (attempt + 1)
                    logger.info(
                        "Native context compaction: PTL retry %d succeeded, summarized %d messages into %d chars",
                        attempt + 1,
                        len(truncated),
                        len(summary_body),
                    )
                    break
                except (RuntimeError, ValueError, OSError):
                    logger.exception(
                        "Native PTL-retry %d/%d summary call failed",
                        attempt + 1,
                        _PTL_RETRY_MAX_ATTEMPTS,
                    )
                    summary_body = None
            retry_msgs = truncated
            retry_chars = truncated_chars

        if summary_body is None:
            method = "fifo_prune_oversized"
            logger.warning(
                "Native compaction input too large (%d chars > %d) - all %d PTL retries exhausted, emitting fallback for %d messages",
                approx_chars,
                _MAX_SUMMARY_INPUT_CHARS,
                _PTL_RETRY_MAX_ATTEMPTS,
                len(to_summarise),
            )
            summary_body = _compact_fallback_summary(to_summarise)
    else:
        try:
            summary_body = await _summarise_messages_with_background_model(to_summarise, user_id=user_id)
            method = "typed_summary"
            logger.info(
                "Native context compaction: summarized %d messages into %d chars",
                len(to_summarise),
                len(summary_body),
            )
        except Exception:
            method = "fifo_prune"
            # G201 + #9
            logger.exception(
                "Native summarization failed, emitting fallback summary for %d messages",
                len(to_summarise),
            )
            summary_body = _compact_fallback_summary(to_summarise)

    summary_text = "%s\n\n%s\n\n%s" % (
        _COMPACT_SUMMARY_HEADER,
        summary_body,
        _COMPACT_SUMMARY_CONTINUATION,
    )
    frames = _native_messages_to_frames(budgeted)
    if len(frames) <= 2:
        prepared = filter_compact_boundaries_for_provider(budgeted)
        return prepared, None

    token_counter = lambda text: _estimate_tokens(text, model=DEFAULT_GPT_MODEL)
    token_before = sum(token_counter(_frame_text(frame)) for frame in frames)
    result_obj = compact_frames(
        CompactionRequest(
            chain=frames,
            token_budget=max(1, int(token_before * (1.0 - _COMPACT_RATIO))),
            reason="agent_loop_compact",
            summary_text=summary_text,
            provider="native_agent",
            force=True,
            token_counter=token_counter,
            tool_result_char_budget=_NATIVE_TOOL_RESULT_CHAR_BUDGET,
        )
    )
    summary_and_tail = _frames_to_native_messages([result_obj.summary_frame, *result_obj.kept_frames])
    # R5-P0-I parity: the rendered summary message lands as the first
    # ``role:"user"`` entry in this list (Claude TS marks it
    # ``isCompactSummary: true``). Tag it on the native side so
    # downstream consumers can identify the summary without parsing the
    # content prefix, and stamp the matching frame uuid for traceability.
    summary_uuid = getattr(result_obj.summary_frame, "uuid", "") or ""
    for message in summary_and_tail:
        if isinstance(message, dict) and message.get("role") == "user":
            message["_viola_compact_summary"] = True
            if summary_uuid:
                message["_viola_compact_summary_uuid"] = summary_uuid
            break
    result = [
        _native_boundary_message(result_obj),
        *summary_and_tail,
    ]
    compact_metadata = copy.deepcopy(getattr(result_obj.boundary_frame, "extra", {}).get("compact_metadata", {}))
    true_post_compact_tokens = sum(
        token_counter(_frame_text(frame))
        for frame in (
            result_obj.boundary_frame,
            result_obj.summary_frame,
            *result_obj.kept_frames,
        )
    )
    meta = {
        "messages_before": len(messages),
        "messages_after": len(result),
        "method": method,
        "summary_text": summary_text[:500],
        "frames_before": len(frames),
        "frames_after": len(result_obj.kept_frames) + 1,
        "dropped_frames": len(result_obj.dropped_frames),
        "token_before": result_obj.token_before,
        "token_after": result_obj.token_after,
        "true_post_compact_tokens": true_post_compact_tokens,
        "will_retrigger_next_turn": true_post_compact_tokens >= result_obj.token_before,
        "compact_metadata": compact_metadata,
        "chain_depth": len(result),
    }
    return result, meta


def _frame_text(frame: Frame) -> str:
    parts: list[str] = []
    for block in frame.blocks:
        value = getattr(block, "text", None)
        if value is None:
            value = getattr(block, "content", None)
        if value is None:
            value = getattr(block, "input", None)
        if value is not None:
            parts.append(str(value))
    return "\n".join(parts)
