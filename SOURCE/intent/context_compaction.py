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
from intent.active_task import ActiveTask, find_request_index
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
from services.conversation.typed_compaction import ACTIVE_REQUEST_FRAME_ORIGIN, CompactionRequest, compact_frames
from services.openai_background import run_background_openai_response

logger = get_logger(__name__)

_COMPACT_RATIO = 0.6  # summarise oldest 60% of messages
BACKGROUND_COMPACTION_MODEL = DEFAULT_BACKGROUND_TASK_MODEL

# INT-11: Upper bound on per-compaction summariser input size, measured in
# characters.  ~4 chars per token -> 8k tokens ~= 32k chars.  The transcript is
# always fitted to this (see ``_fit_summary_transcript``), so a large history
# makes the input smaller, never a reason to skip the summary. Build 9 measured
# the raw message size against this limit, used up its retries on a slab the
# renderer would have shrunk anyway (92177 chars > 32000), and left the agent
# with no idea what it had been asked.
_MAX_SUMMARY_INPUT_CHARS = 32_000
# Tool output is the bulk of a long run, so each fitting pass cuts it harder
# before any message is left out.
_SUMMARY_TOOL_RESULT_CHAR_STEPS = (1_500, 600, 200)
_SUMMARY_MESSAGE_CHARS = 6_000
# History from before the current request only ever gets this share, so the
# current run is never crowded out by older tasks.
_SUMMARY_EARLIER_HISTORY_SHARE = 0.25
_SUMMARY_MAX_OUTPUT_TOKENS = 3_000
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
    "Carry on with the user's current request using this context. Do not "
    "ask the user to repeat information or answer more questions. Start the "
    "next useful action directly, without announcing the handoff, restating "
    "the summary, or introducing a plan to resume."
)
# When the LLM call itself fails (no quota / network / 400) we still need
# SOMETHING in the summary slot. The fallback is a single line of
# CONTINUATION text, NOT a Python prose-bucketed re-classification. The
# recent-tail messages are preserved verbatim so the model can still see
# the live state, and the active task block that follows carries the request
# and the step record; the missing piece is detail from the older steps.
_COMPACT_FALLBACK_BODY = (
    "Older details were left out because no summary could be made. You have not "
    "lost the task: the request and the step record below are exact, and the "
    "recent messages that follow carry the live state. Continue from there."
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
        content = message.get("content")
        # A continuity record is how every OpenAI-shaped assistant turn is
        # stored, tool calls included. It renders by name below; dropping it
        # hid all the agent's actions from the summary.
        continuity_record = isinstance(content, dict) and content.get("_openai_assistant")
        if not continuity_record and _content_looks_like_runtime_metadata_dump(content):
            continue
        filtered.append(message)
    return filtered


def _render_messages_for_summarizer(
    messages: list[dict[str, Any]],
    *,
    tool_result_chars: int = _SUMMARY_TOOL_RESULT_CHAR_STEPS[0],
) -> str:
    """Render the filtered messages as a single role-tagged transcript.

    The summarizer gets ``[role] content`` lines so the LLM can produce
    the "All user messages" / "Files and Code Sections" sections from
    the same role typing the next agent will see. Each message keeps
    its native role (user / assistant) — no re-bucketing happens here.
    Tool blocks render in-place with the tool name so files-touched
    inference works.
    """

    return "\n\n".join(_rendered_message_parts(messages, tool_result_chars=tool_result_chars))


def _rendered_message_parts(messages: list[dict[str, Any]], *, tool_result_chars: int) -> list[str]:
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")
        rendered = _render_message_content_for_summarizer(content, tool_result_chars=tool_result_chars)
        if not rendered.strip():
            continue
        parts.append("[%s]\n%s" % (role, rendered.strip()))
    return parts


def _render_continuity_record(record: dict[str, Any]) -> str:
    """Render an ``_openai_assistant`` record by what it did, never as a dict repr."""

    parts: list[str] = []
    text = record.get("content")
    if isinstance(text, str) and text.strip():
        parts.append(text)
    tool_calls = record.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict):
                parts.append(
                    "[tool_use:%s] %s"
                    % (str(function.get("name") or "tool"), str(function.get("arguments") or "")[:600])
                )
    return "\n".join(parts)


def _render_message_content_for_summarizer(
    content: Any,
    *,
    tool_result_chars: int = _SUMMARY_TOOL_RESULT_CHAR_STEPS[0],
) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("_openai_assistant"):
            return _render_continuity_record(content)
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
            parts.append("[tool_result:%s]\n%s" % (tname, text[:tool_result_chars]))
        elif btype in {"image", "input_image"}:
            parts.append("[image]")
        elif btype in {"document", "input_file"}:
            parts.append("[document]")
    return "\n".join(part for part in parts if part)


def _fit_messages_to_budget(messages: list[dict[str, Any]], budget: int) -> str:
    """Render ``messages`` into at most ``budget`` characters, newest kept first.

    Tool output is cut harder on each pass. If that is not enough the oldest
    messages are left out, with a note saying so. This cannot fail: however big
    the input, what comes back fits.
    """

    if budget <= 0:
        return ""
    parts: list[str] = []
    for tool_result_chars in _SUMMARY_TOOL_RESULT_CHAR_STEPS:
        parts = [
            part if len(part) <= _SUMMARY_MESSAGE_CHARS else "%s\n[message shortened]" % part[:_SUMMARY_MESSAGE_CHARS]
            for part in _rendered_message_parts(messages, tool_result_chars=tool_result_chars)
        ]
        joined = "\n\n".join(parts)
        if len(joined) <= budget:
            return joined

    kept: list[str] = []
    used = 0
    for part in reversed(parts):
        if used + len(part) + 2 > budget:
            break
        kept.append(part)
        used += len(part) + 2
    if not kept:
        return "%s\n[message shortened]" % parts[-1][: max(0, budget - 24)] if parts else ""
    left_out = len(parts) - len(kept)
    note = "[%d older messages left out to fit]\n\n" % left_out if left_out else ""
    return "%s%s" % (note, "\n\n".join(reversed(kept)))


def _fit_summary_transcript(
    messages: list[dict[str, Any]],
    active_task: ActiveTask | None,
    budget: int,
) -> str:
    """Build the summariser's transcript within ``budget`` characters.

    Build 9 took the first 12,000 characters of the rendered slab. The oldest
    messages are the previous task, so the summary described the old water order
    and never saw the brakes request. The current run now gets the budget first,
    newest messages first, and older history is labelled as background.
    """

    request_index = find_request_index(messages, active_task.request) if active_task else None
    if not active_task or request_index is None or request_index == 0:
        return _fit_messages_to_budget(messages, budget)

    current_heading = "=== THE CURRENT REQUEST AND THE WORK ON IT ===\n"
    earlier_heading = "=== BEFORE THE CURRENT REQUEST (background, finished or replaced) ===\n"
    earlier_share = int(budget * _SUMMARY_EARLIER_HISTORY_SHARE)
    current_budget = budget - earlier_share - len(current_heading)
    current_text = _fit_messages_to_budget(messages[request_index:], current_budget)
    earlier_budget = budget - len(current_heading) - len(current_text) - len(earlier_heading) - 2
    earlier_text = _fit_messages_to_budget(messages[:request_index], earlier_budget)
    sections = ["%s%s" % (earlier_heading, earlier_text)] if earlier_text else []
    sections.append("%s%s" % (current_heading, current_text))
    return "\n\n".join(sections)


def _summary_system_prompt(active_task: ActiveTask | None) -> str:
    if active_task is None or not active_task.request:
        return _TS_COMPACT_PROMPT
    return "%s\n%s\n" % (_TS_COMPACT_PROMPT, active_task.render_for_summary_prompt())


async def _summarise_messages_with_background_model(
    messages: list[dict[str, Any]],
    *,
    user_id: str = "",
    active_task: ActiveTask | None = None,
    char_budget: int = _MAX_SUMMARY_INPUT_CHARS,
) -> str:
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

    transcript = _fit_summary_transcript(relevant, active_task, min(char_budget, _MAX_SUMMARY_INPUT_CHARS))

    raw = await run_background_openai_response(
        system_prompt=_summary_system_prompt(active_task),
        user_content=transcript,
        max_output_tokens=_SUMMARY_MAX_OUTPUT_TOKENS,
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


async def _summary_body_for(
    messages: list[dict[str, Any]],
    *,
    user_id: str,
    active_task: ActiveTask | None,
) -> tuple[str, str]:
    """Return ``(summary body, method)``. Always returns; a failure yields the fallback.

    TS parity (compact.ts:243-291): when the summariser itself reports the
    prompt is too long, retry with less. The input is fitted before each try, so
    this only guards against a provider with a smaller window than expected.
    """

    char_budget = _MAX_SUMMARY_INPUT_CHARS
    for attempt in range(_PTL_RETRY_MAX_ATTEMPTS):
        try:
            body = await _summarise_messages_with_background_model(
                messages,
                user_id=user_id,
                active_task=active_task,
                char_budget=char_budget,
            )
        except Exception as exc:
            if attempt + 1 < _PTL_RETRY_MAX_ATTEMPTS and is_token_limit_error(exc):
                char_budget //= 2
                logger.warning(
                    "Native compaction summary too large for the model; retrying with %d chars (%d/%d)",
                    char_budget,
                    attempt + 1,
                    _PTL_RETRY_MAX_ATTEMPTS,
                )
                continue
            # G201 + #9
            logger.exception(
                "Native summarization failed, emitting fallback summary for %d messages",
                len(messages),
            )
            return _compact_fallback_summary(messages), "fifo_prune"
        logger.info(
            "Native context compaction: summarized %d messages into %d chars",
            len(messages),
            len(body),
        )
        return body, "typed_summary" if attempt == 0 else "typed_summary_ptl_retry_%d" % attempt
    return _compact_fallback_summary(messages), "fifo_prune"


def _compact_summary_text(summary_body: str, active_task: ActiveTask | None) -> str:
    """Assemble the handoff. The runtime's task block follows the model's summary, so it has the last word."""

    anchor = active_task.render_anchor() if active_task is not None else ""
    sections = [_COMPACT_SUMMARY_HEADER, summary_body]
    if anchor:
        sections.append(anchor)
    sections.append(_COMPACT_SUMMARY_CONTINUATION)
    return "\n\n".join(sections)


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


# TS parity (compact.ts MAX_PTL_RETRIES): tries before the fallback note when the
# summariser reports its prompt is too long.
_PTL_RETRY_MAX_ATTEMPTS = 3


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


def _native_messages_to_frames(
    messages: list[dict[str, Any]],
    *,
    active_task: ActiveTask | None = None,
) -> list[Frame]:
    frames: list[Frame] = []
    tool_names: dict[str, str] = {}
    chain = filter_compact_boundaries_for_provider(messages_after_compact_boundary(messages))
    request_index = (
        find_request_index(chain, active_task.request, exact=True) if active_task and active_task.request else None
    )
    for index, message in enumerate(chain):
        role = str(message.get("role") or "")
        content = message.get("content")
        if index == request_index:
            # The user's own message stays a real message after compaction.
            frames.append(_active_request_frame(content))
            continue
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


def _active_request_frame(content: Any) -> Frame:
    return text_frame(
        kind=FrameKind.USER_INPUT,
        role=FrameRole.USER,
        text=_native_content_to_text(content),
        origin=ACTIVE_REQUEST_FRAME_ORIGIN,
    )


def _native_boundary_message(result: Any) -> dict[str, Any]:
    compact_metadata = copy.deepcopy(getattr(result.boundary_frame, "extra", {}).get("compact_metadata", {}))
    return {
        "role": "system",
        "content": "Conversation compacted",
        _NATIVE_COMPACT_BOUNDARY_KEY: True,
        "compact_metadata": compact_metadata,
        "compactMetadata": copy.deepcopy(compact_metadata),
    }


def _as_loop_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """Store an assistant tool turn the way the agent loop stores every other one.

    The frame renderer gives Anthropic ``tool_use`` blocks. Managed turns are
    converted on the cloud, whose converter dropped those blocks and then every
    kept tool result as an orphan, so this must not depend on a cloud deploy.
    """
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, list):
        return message
    tool_calls = [
        {
            "id": block.get("id", ""),
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False, default=str),
            },
        }
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    if not tool_calls:
        return message
    text = "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and "text" in block)
    return {**message, "content": {"_openai_assistant": True, "content": text, "tool_calls": tool_calls}}


def _frames_to_native_messages(frames: list[Frame]) -> list[dict[str, Any]]:
    rendered = render_for_anthropic(PromptFrameBundle(frames=frames))
    messages = rendered.get("messages")
    if not isinstance(messages, list):
        return []
    return [_as_loop_assistant_message(message) if isinstance(message, dict) else message for message in messages]


async def compact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_system: bool = True,
    user_id: str = "",
    active_task: ActiveTask | None = None,
) -> list[dict[str, Any]]:
    """Compact *messages* by summarising the oldest 60%.

    Preserves the first message (system/user request) and the most
    recent 40% of messages.  The middle portion is replaced with a
    single summary message.

    Falls back to a continuation note if summarisation fails. Either way the
    active task block goes into the summary message.

    Args:
        messages: Full message list (will NOT be mutated).
        keep_system: If True, always preserve messages[0].
        active_task: The run's task; read from the chain when not given.

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

    task = active_task or ActiveTask.from_messages(messages)
    try:
        summary_body = await _summarise_messages_with_background_model(
            to_summarise,
            user_id=user_id,
            active_task=task,
        )
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
        "content": _compact_summary_text(summary_body, task),
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
    active_task: ActiveTask | None = None,
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
        active_task=active_task,
    )


async def compact_native_messages(
    messages: list[dict[str, Any]],
    *_unused: Any,
    user_id: str = "",
    session_id: str | None = None,
    replacement_writer: Any | None = None,
    content_replacement_state: Any | None = None,
    active_task: ActiveTask | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Compact native provider messages through the typed frame pipeline.

    ``active_task`` is the run's task. It is read from the chain when a caller
    has none, so no route through here can compact the request away.
    """

    budgeted = project_native_messages_for_provider(
        messages,
        content_replacement_state=content_replacement_state,
        replacement_writer=replacement_writer,
        session_id=session_id,
    )
    if len(budgeted) <= 6:
        prepared = filter_compact_boundaries_for_provider(budgeted)
        return prepared, None

    task = active_task if active_task is not None and active_task.request else ActiveTask.from_messages(budgeted)
    split = max(2, int(len(budgeted) * _COMPACT_RATIO))
    if split >= len(budgeted):
        split = max(0, len(budgeted) - 2)
    summary_body, method = await _summary_body_for(budgeted[:split], user_id=user_id, active_task=task)
    summary_text = _compact_summary_text(summary_body, task)
    frames = _native_messages_to_frames(budgeted, active_task=task)
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
