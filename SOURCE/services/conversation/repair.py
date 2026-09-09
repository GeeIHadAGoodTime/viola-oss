"""Conversation-repair helpers shared by prompt and cloud dispatch paths.

Note (R5-P0-C, 2026-05-30): the user-turn repair regex classifier
(`classify_repair_reference`, `repair_instruction_for`,
`build_repair_prompt_context`, `extract_music_repair_query`, and their
supporting regex tables) was deleted because it boxed the model on raw
user phrasing -- a runtime classifier that injected a turn-classification
prompt fragment mid-task to steer the LLM. The Claude Code TS parity
target has no such layer; the model interprets ``"actually I meant X"``
itself from the raw conversation turns. Guidance moved into the unified
prompt (``services/llm/prompts/viola_unified.py``). The retired surface
is now forbidden by
``scripts/check_no_repair_classifier_prompt_fragment.py``.

The transcript-recovery machinery below is unrelated: it repairs saved
transcript tails on resume (orphan tool results, interrupted tool calls,
no-response sentinels). That is structural and stays.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from services.conversation.lineage import (
    assign_lineage,
    recover_orphaned_parallel_tool_results,
    walk_active_leaf,
)
from services.conversation.message_invariants import (
    SYNTHETIC_TOOL_RESULT_CONTENT,
    build_orphan_tool_result_placeholder_frame,
)

logger = get_logger(__name__)

TRANSCRIPT_RECOVERY_CONTINUE_TEXT = "Continue from where you left off."
_TRANSCRIPT_RECOVERY_ORIGIN = "transcript_recovery"
_TRANSCRIPT_RECOVERY_SOURCE_TAG = "transcript-recovery"

# Mirror Claude's `NO_RESPONSE_REQUESTED` assistant sentinel from
# `src/utils/conversationRecovery.ts:241`. When resume lands on a user-last
# transcript with no pending tool use, the deserializer appends this synthetic
# assistant message so the next turn isn't an immediate model call.
NO_RESPONSE_REQUESTED_SENTINEL = "(no response requested)"
_NO_RESPONSE_REQUESTED_ORIGIN = "no_response_requested"
_NO_RESPONSE_REQUESTED_SOURCE_TAG = "no-response-requested"

# Permission modes Claude's deserializer treats as valid plus Viola's
# extensions. Mirrors ``intent.permissions.policy._VALID_MODES`` so resume
# does not strip canonical Viola modes. Anything else gets scrubbed from
# frames on resume to avoid leaking deleted/renamed modes into the next
# turn. Kept as a literal frozenset (rather than importing from policy)
# to avoid an import cycle through ``services.conversation`` ->
# ``intent.permissions``.
_VALID_PERMISSION_MODES = frozenset(
    {
        # Claude PERMISSION_MODES (src/types/permissions.ts:33)
        "default",
        "acceptEdits",
        "bypassPermissions",
        "dontAsk",
        "plan",
        "auto",
        # Viola extension (intent.permissions.policy.PermissionMode)
        "bubble",
        # Permission *behavior* values that pre-S8 fixtures stored in the
        # permission_mode field. Treat as valid to avoid silently stripping
        # legacy persisted state; new code should write a real mode value.
        "ask",
        "allow",
        "deny",
    }
)


@dataclass(frozen=True)
class TranscriptRecoveryResult:
    """Recovered transcript chain plus test-visible repair accounting."""

    frames: list[Frame]
    stripped_orphan_tool_results: int = 0
    synthetic_tool_results: int = 0
    appended_continue: bool = False
    # Recovery accounting follows the documented
    # ``deserializeMessagesWithInterruptDetection`` decision tree. These
    # counters surface filter activity that previously vanished into the
    # leaf walk, so callers (and tests) can verify the recovery contract.
    stripped_thinking_only: int = 0
    stripped_whitespace_only: int = 0
    recovered_parallel_tool_results: int = 0
    appended_no_response_sentinel: bool = False
    stripped_invalid_permission_modes: int = 0
    migrated_legacy_attachments: int = 0


# Interruption classes mirrored from Claude Code's
# ``src/utils/conversationRecovery.ts:164-292``.  The classifier decides which
# repair shape to apply when a saved transcript is resumed.
#
# * ``completed``           — last behavioral frame is a finished assistant
#                              turn (no trailing orphan tool_use).
# * ``interrupted_prompt``  — last behavioral frame is a user turn with no
#                              following assistant turn.  Replay should just
#                              re-enter the loop.
# * ``interrupted_tool``    — last behavioral frame is an assistant turn that
#                              contains tool_use blocks lacking matching
#                              tool_results.  Replay should inject synthetic
#                              tool_results + a continue reminder.
# * ``trailing_user``       — last behavioral frame is a user turn AND there is
#                              a still-pending assistant tool_use ahead of it
#                              (a user message that arrived while a tool was
#                              in flight).  Treated as ``interrupted_tool``
#                              for the repair shape but flagged so the caller
#                              can choose whether to honor the new user input.
# * ``attachment_only``     — last behavioral frame is a user turn whose only
#                              content is non-text (e.g. system reminders or
#                              attachment blocks).  Replay needs an extra
#                              "continue" prompt to give the model an anchor.
INTERRUPTION_COMPLETED = "completed"
INTERRUPTION_PROMPT = "interrupted_prompt"
INTERRUPTION_TOOL = "interrupted_tool"
INTERRUPTION_TRAILING_USER = "trailing_user"
INTERRUPTION_ATTACHMENT_ONLY = "attachment_only"
INTERRUPTION_EMPTY = "empty"

_INTERRUPTION_VALUES = frozenset(
    {
        INTERRUPTION_COMPLETED,
        INTERRUPTION_PROMPT,
        INTERRUPTION_TOOL,
        INTERRUPTION_TRAILING_USER,
        INTERRUPTION_ATTACHMENT_ONLY,
        INTERRUPTION_EMPTY,
    }
)


@dataclass(frozen=True)
class InterruptionClassification:
    """Outcome of classifying a checkpoint's resume shape."""

    state: str
    unmatched_tool_use_ids: tuple[str, ...] = ()
    last_user_text: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _INTERRUPTION_VALUES:
            raise ValueError("Unknown interruption state: %s" % self.state)


def classify_interruption_state(
    messages: Any,
) -> InterruptionClassification:
    """Classify a saved transcript's interruption shape for resume.

    Accepts either typed ``Frame`` objects or native dict messages (as the
    checkpoint store persists both shapes depending on which provider wrote
    them).  Returns an ``InterruptionClassification`` describing the resume
    shape; the caller decides which repair behaviour to apply.
    """

    if not messages:
        return InterruptionClassification(state=INTERRUPTION_EMPTY)

    typed_frames = [m for m in messages if isinstance(m, Frame)]
    if typed_frames:
        return _classify_typed_interruption(typed_frames)
    dict_messages = [m for m in messages if isinstance(m, dict)]
    if dict_messages:
        return _classify_dict_interruption(dict_messages)
    return InterruptionClassification(state=INTERRUPTION_EMPTY)


def _classify_typed_interruption(frames: list[Frame]) -> InterruptionClassification:
    behavioural = [frame for frame in frames if not frame.is_meta]
    if not behavioural:
        return InterruptionClassification(state=INTERRUPTION_EMPTY)
    tool_use_ids: dict[str, Frame] = {}
    seen_results: set[str] = set()
    for frame in behavioural:
        for block in frame.blocks:
            if isinstance(block, ToolUseBlock) and block.tool_use_id:
                tool_use_ids.setdefault(block.tool_use_id, frame)
            elif isinstance(block, ToolResultBlock) and block.tool_use_id:
                seen_results.add(block.tool_use_id)
    unmatched = tuple(tuid for tuid in tool_use_ids if tuid not in seen_results)
    last = behavioural[-1]
    last_role = last.role
    if last_role is FrameRole.ASSISTANT:
        if unmatched:
            return InterruptionClassification(
                state=INTERRUPTION_TOOL,
                unmatched_tool_use_ids=unmatched,
            )
        return InterruptionClassification(state=INTERRUPTION_COMPLETED)

    # Last behavioral frame is user-shaped (USER, META_USER, or TOOL).
    last_text = _frame_text(last)
    if unmatched:
        return InterruptionClassification(
            state=INTERRUPTION_TRAILING_USER,
            unmatched_tool_use_ids=unmatched,
            last_user_text=last_text or None,
        )
    if not last_text:
        return InterruptionClassification(
            state=INTERRUPTION_ATTACHMENT_ONLY,
            last_user_text=None,
        )
    return InterruptionClassification(
        state=INTERRUPTION_PROMPT,
        last_user_text=last_text,
    )


def _classify_dict_interruption(messages: list[dict[str, Any]]) -> InterruptionClassification:
    tool_use_ids: dict[str, dict[str, Any]] = {}
    seen_results: set[str] = set()
    for msg in messages:
        if msg.get("type") == "function_call":
            call_id = str(msg.get("call_id") or "")
            if call_id:
                tool_use_ids.setdefault(call_id, msg)
            continue
        if msg.get("type") == "function_call_output":
            call_id = str(msg.get("call_id") or "")
            if call_id:
                seen_results.add(call_id)
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    block_id = str(block.get("id") or "")
                    if block_id:
                        tool_use_ids.setdefault(block_id, msg)
        if role == "assistant" and isinstance(content, dict) and content.get("_openai_assistant"):
            tool_calls = content.get("tool_calls") or []
            for tc in tool_calls if isinstance(tool_calls, list) else ():
                if isinstance(tc, dict):
                    tc_id = str(tc.get("id") or "")
                    if tc_id:
                        tool_use_ids.setdefault(tc_id, msg)
            response_items = content.get("response_items") or []
            for item in response_items if isinstance(response_items, list) else ():
                if isinstance(item, dict) and item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or "")
                    if call_id:
                        tool_use_ids.setdefault(call_id, msg)
                elif isinstance(item, dict) and item.get("type") == "function_call_output":
                    call_id = str(item.get("call_id") or "")
                    if call_id:
                        seen_results.add(call_id)
        if (role in {"user", "tool"}) and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id") or "")
                    if tool_use_id:
                        seen_results.add(tool_use_id)
        if role == "tool":
            tcid = str(msg.get("tool_call_id") or msg.get("tool_use_id") or "")
            if tcid:
                seen_results.add(tcid)

    unmatched = tuple(tuid for tuid in tool_use_ids if tuid not in seen_results)
    last = messages[-1]
    last_role = last.get("role")
    last_type = last.get("type")
    last_is_assistant = last_role == "assistant" or last_type == "function_call"
    if last_is_assistant:
        if unmatched:
            return InterruptionClassification(
                state=INTERRUPTION_TOOL,
                unmatched_tool_use_ids=unmatched,
            )
        return InterruptionClassification(state=INTERRUPTION_COMPLETED)

    last_text = _dict_message_text(last)
    if unmatched:
        return InterruptionClassification(
            state=INTERRUPTION_TRAILING_USER,
            unmatched_tool_use_ids=unmatched,
            last_user_text=last_text or None,
        )
    if not last_text:
        return InterruptionClassification(
            state=INTERRUPTION_ATTACHMENT_ONLY,
            last_user_text=None,
        )
    return InterruptionClassification(
        state=INTERRUPTION_PROMPT,
        last_user_text=last_text,
    )


def _dict_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        parts.append(text)
                elif btype == "tool_result":
                    # tool_result blocks are not user "intent" text; skip them
                    # so attachment-only classification fires correctly.
                    continue
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = str(content.get("content") or content.get("text") or "").strip()
        return text
    return ""


def _frame_text(frame: Frame) -> str:
    parts: list[str] = []
    for block in frame.blocks:
        if isinstance(block, (TextBlock, SystemReminderBlock)):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
        elif isinstance(block, ToolUseBlock):
            parts.append("%s(%s)" % (block.name, block.tool_use_id))
    return "\n".join(part for part in parts if part).strip()


def _display_path_for_attachment(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        path = Path(text)
        if path.is_absolute():
            try:
                return str(path.relative_to(Path.cwd()))
            except ValueError:
                return text
    except (TypeError, ValueError):
        return text
    return text


def _migrate_attachment_payload(payload: object) -> tuple[object, int]:
    if not isinstance(payload, dict):
        return payload, 0

    attachment = dict(payload)
    migrated = 0
    attachment_type = str(attachment.get("type") or "")
    display_source: object | None = None

    if attachment_type == "new_file":
        attachment["type"] = "file"
        display_source = attachment.get("filename")
        migrated = 1
    elif attachment_type == "new_directory":
        attachment["type"] = "directory"
        display_source = attachment.get("path")
        migrated = 1
    elif "displayPath" not in attachment:
        display_source = attachment.get("filename") or attachment.get("path") or attachment.get("skillDir")

    if display_source is not None and (
        "displayPath" not in attachment or attachment_type in {"new_file", "new_directory"}
    ):
        display_path = _display_path_for_attachment(display_source)
        if display_path is not None:
            if attachment.get("displayPath") != display_path:
                migrated = 1
            attachment["displayPath"] = display_path

    if migrated:
        return attachment, migrated
    return payload, 0


def _migrate_legacy_attachment_fields(frame: Frame) -> tuple[Frame, int]:
    if not isinstance(frame.extra, dict) or not frame.extra:
        return frame, 0

    extra = dict(frame.extra)
    migrated = 0
    if "attachment" in extra:
        extra["attachment"], count = _migrate_attachment_payload(extra["attachment"])
        migrated += count
    if isinstance(extra.get("attachments"), list):
        attachments: list[object] = []
        for attachment in extra["attachments"]:
            migrated_attachment, count = _migrate_attachment_payload(attachment)
            attachments.append(migrated_attachment)
            migrated += count
        extra["attachments"] = attachments

    if not migrated:
        return frame, 0
    return replace(frame, extra=extra), migrated


def build_transcript_continue_frame(
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    reason: str = "interrupted_resume",
    checkpoint_task_id: str | None = None,
) -> Frame:
    """Build the Claude-style meta continuation reminder."""

    extra: dict[str, object] = {"reason": reason}
    if checkpoint_task_id:
        extra["checkpoint_task_id"] = checkpoint_task_id
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(
            SystemReminderBlock(
                text=TRANSCRIPT_RECOVERY_CONTINUE_TEXT,
                source_tag=_TRANSCRIPT_RECOVERY_SOURCE_TAG,
            ),
        ),
        is_meta=True,
        origin=_TRANSCRIPT_RECOVERY_ORIGIN,
        task_id=task_id,
        session_id=session_id,
        extra=extra,
    )


def recover_transcript_on_resume(
    frames: Sequence[Frame],
    *,
    session_id: str | None = None,
    append_no_response_sentinel: bool = True,
) -> TranscriptRecoveryResult:
    """Walk the active leaf and repair resumable transcript-tail defects.

    Mirrors Claude's ``deserializeMessagesWithInterruptDetection``
    (``src/utils/conversationRecovery.ts:164``):

    1. Walk the active leaf.
    2. Recover orphaned parallel tool-result siblings (S8-009).
    3. Strip invalid permission modes (mirrors Claude attachment migrate).
    4. Filter thinking-only and whitespace-only assistant frames.
    5. Strip orphaned tool_result blocks.
    6. Insert synthetic error tool_results for interrupted assistant tool calls.
    7. Append a continuation reminder when we synthesized results.
    8. Otherwise, if the last frame is a behavioral user message, append the
       ``NO_RESPONSE_REQUESTED`` assistant sentinel so the resumed loop does
       not auto-call the model.

    ``append_no_response_sentinel`` lets callers (auto-restore on manager
    construction) skip step 8. Explicit resume / branch should keep it True
    so the persistence rehydration matches Claude's REPL contract.
    """

    ordered_all = [frame for frame in frames if isinstance(frame, Frame)]
    active_chain = walk_active_leaf(ordered_all)
    if not active_chain:
        return TranscriptRecoveryResult(frames=[])

    # S8-009: Parallel tool-result DAG recovery. Splice in sibling tool results
    # that share the same source_tool_assistant_uuid but whose tool_use_id is
    # not represented in the active chain. Claude does this BEFORE the
    # tool-pairing walk so the survivors get pairing credit.
    active_before_recover = list(active_chain)
    active_chain = recover_orphaned_parallel_tool_results(ordered_all, active_chain)
    parallel_recovered = max(0, len(active_chain) - len(active_before_recover))

    repaired: list[Frame] = []
    seen_tool_uses: dict[str, Frame] = {}
    seen_tool_results: set[str] = set()
    stripped_orphans = 0
    stripped_thinking_only = 0
    stripped_whitespace_only = 0
    stripped_invalid_permission_modes = 0
    migrated_legacy_attachments = 0

    for frame in active_chain:
        frame, migrated_count = _migrate_legacy_attachment_fields(frame)
        migrated_legacy_attachments += migrated_count

        # S8-008: strip invalid permission_mode markers. Claude does the same
        # via ``migrateAttachmentInvalidations`` on resume.
        if frame.permission_mode and frame.permission_mode not in _VALID_PERMISSION_MODES:
            stripped_invalid_permission_modes += 1
            frame = replace(frame, permission_mode=None)

        # S8-008: skip assistant frames whose only content is thinking blocks
        # or empty whitespace text. Claude's resume deserializer drops both
        # because they cannot survive a provider replay without re-deriving
        # signatures, and they are not behaviorally meaningful.
        if frame.role is FrameRole.ASSISTANT and not frame.is_meta:
            if _is_thinking_only_frame(frame):
                stripped_thinking_only += 1
                continue
            if _is_whitespace_only_frame(frame):
                stripped_whitespace_only += 1
                continue

        retained_blocks: list[Any] = []
        changed = False
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock):
                tool_use_id = block.tool_use_id
                if not tool_use_id or tool_use_id not in seen_tool_uses or tool_use_id in seen_tool_results:
                    stripped_orphans += 1
                    changed = True
                    continue
                seen_tool_results.add(tool_use_id)
            retained_blocks.append(block)

        if changed:
            if not retained_blocks:
                continue
            frame = replace(frame, blocks=tuple(retained_blocks))

        repaired.append(frame)
        for tool_use_id in _tool_use_ids(frame):
            seen_tool_uses.setdefault(tool_use_id, frame)

    repaired = _ensure_user_leading_recovery(repaired, session_id=session_id)
    last_behavioral = _last_behavioral_frame(repaired)
    missing_tail_tool_uses = [
        tool_use_id for tool_use_id in _tool_use_ids(last_behavioral) if tool_use_id not in seen_tool_results
    ]
    synthetic_count = 0
    appended_continue = False
    appended_no_response_sentinel = False
    if last_behavioral is not None and missing_tail_tool_uses:
        synthetic_frame = _build_synthetic_tool_result_frame(
            last_behavioral,
            missing_tail_tool_uses,
            session_id=session_id,
        )
        repaired.append(_with_recovery_lineage(synthetic_frame, repaired, session_id=session_id))
        synthetic_count = len(missing_tail_tool_uses)
        if not _has_recovery_continue(repaired):
            continue_frame = build_transcript_continue_frame(
                session_id=session_id or last_behavioral.session_id,
                task_id=last_behavioral.task_id,
                reason="interrupted_tool_resume",
            )
            repaired.append(_with_recovery_lineage(continue_frame, repaired, session_id=session_id))
            appended_continue = True
    elif stripped_orphans and not _has_recovery_continue(repaired):
        notice = build_orphan_tool_result_placeholder_frame(
            active_chain[-1],
            origin=_TRANSCRIPT_RECOVERY_ORIGIN,
            session_id=session_id or active_chain[-1].session_id,
        )
        repaired.append(_with_recovery_lineage(notice, repaired, session_id=session_id))
    else:
        # S8-008: NO_RESPONSE_REQUESTED sentinel. When resume lands on a
        # user-last transcript without an interrupted tool call, Claude's
        # deserializer appends a synthetic assistant message so the resumed
        # loop does not immediately re-call the model with stale context.
        if (
            append_no_response_sentinel
            and last_behavioral is not None
            and last_behavioral.role is FrameRole.USER
            and not _has_no_response_sentinel(repaired)
        ):
            sentinel_frame = _build_no_response_sentinel_frame(
                last_behavioral,
                session_id=session_id,
            )
            repaired.append(_with_recovery_lineage(sentinel_frame, repaired, session_id=session_id))
            appended_no_response_sentinel = True

    return TranscriptRecoveryResult(
        frames=repaired,
        stripped_orphan_tool_results=stripped_orphans,
        synthetic_tool_results=synthetic_count,
        appended_continue=appended_continue,
        stripped_thinking_only=stripped_thinking_only,
        stripped_whitespace_only=stripped_whitespace_only,
        recovered_parallel_tool_results=parallel_recovered,
        appended_no_response_sentinel=appended_no_response_sentinel,
        stripped_invalid_permission_modes=stripped_invalid_permission_modes,
        migrated_legacy_attachments=migrated_legacy_attachments,
    )


def _is_thinking_only_frame(frame: Frame) -> bool:
    """Return True for assistant frames whose only content is `thinking` blocks.

    Claude annotates internal reasoning as a `thinking` block that cannot be
    safely replayed without provider-signed signatures. Detected here via the
    frame's extra metadata (which the provider adapters tag) or via the
    absence of any user-visible content block.
    """

    extra = frame.extra or {}
    if extra.get("thinking_only") is True:
        return True
    if extra.get("content_kind") == "thinking":
        return True
    visible_blocks = [
        block
        for block in frame.blocks
        if isinstance(block, (TextBlock, ToolUseBlock, ToolResultBlock, SystemReminderBlock))
    ]
    if not visible_blocks:
        # No visible blocks but the frame has *some* blocks: likely thinking-only.
        return bool(frame.blocks)
    return False


def _is_whitespace_only_frame(frame: Frame) -> bool:
    """Return True for assistant frames whose visible text is all whitespace."""

    for block in frame.blocks:
        if isinstance(block, (ToolUseBlock, ToolResultBlock)):
            return False
        text = getattr(block, "text", None) or getattr(block, "content", None)
        if isinstance(text, str) and text.strip():
            return False
    return True


def _has_no_response_sentinel(frames: Sequence[Frame]) -> bool:
    return any(frame.origin == _NO_RESPONSE_REQUESTED_ORIGIN for frame in frames)


def _build_no_response_sentinel_frame(
    source_frame: Frame,
    *,
    session_id: str | None,
) -> Frame:
    """Build the synthetic `NO_RESPONSE_REQUESTED` sentinel.

    Claude renders this as an assistant message with the literal text
    ``(no response requested)``. Viola's behavioral-bail invariant rejects
    meta + assistant frames, so we render the sentinel as a non-meta
    assistant text frame whose origin marks it as a recovery sentinel for
    callers that need to distinguish it from a real model turn. The origin
    + extra metadata are the discriminators; the wire shape stays compatible
    with Claude.
    """

    return Frame(
        kind=FrameKind.ASSISTANT_TEXT,
        role=FrameRole.ASSISTANT,
        blocks=(TextBlock(text=NO_RESPONSE_REQUESTED_SENTINEL),),
        is_meta=False,
        origin=_NO_RESPONSE_REQUESTED_ORIGIN,
        task_id=source_frame.task_id,
        session_id=session_id or source_frame.session_id,
        extra={"reason": "user_last_no_response_required", "sentinel": True},
    )


def _tool_use_ids(frame: Frame | None) -> list[str]:
    if frame is None:
        return []
    return [block.tool_use_id for block in frame.blocks if isinstance(block, ToolUseBlock) and block.tool_use_id]


def _last_behavioral_frame(frames: Sequence[Frame]) -> Frame | None:
    for frame in reversed(frames):
        if not frame.is_meta:
            return frame
    return None


def _has_recovery_continue(frames: Sequence[Frame]) -> bool:
    return any(
        frame.origin == _TRANSCRIPT_RECOVERY_ORIGIN and _frame_text(frame) == TRANSCRIPT_RECOVERY_CONTINUE_TEXT
        for frame in frames
    )


def _ensure_user_leading_recovery(frames: list[Frame], *, session_id: str | None) -> list[Frame]:
    for index, frame in enumerate(frames):
        wire_role = _recovery_wire_role(frame)
        if wire_role is None:
            continue
        if wire_role == "user":
            return frames
        placeholder = build_orphan_tool_result_placeholder_frame(
            frame,
            origin=_TRANSCRIPT_RECOVERY_ORIGIN,
            session_id=session_id or frame.session_id,
        )
        placeholder = _with_recovery_lineage(placeholder, frames[:index], session_id=session_id)
        return [*frames[:index], placeholder, *frames[index:]]
    return frames


def _recovery_wire_role(frame: Frame) -> str | None:
    if frame.role is FrameRole.ASSISTANT:
        return "assistant"
    if frame.role in (FrameRole.USER, FrameRole.META_USER, FrameRole.TOOL):
        return "user"
    return None


def _build_synthetic_tool_result_frame(
    source_frame: Frame,
    tool_use_ids: Sequence[str],
    *,
    session_id: str | None,
) -> Frame:
    blocks: list[ToolResultBlock] = []
    for tool_use_id in tool_use_ids:
        blocks.append(
            ToolResultBlock(
                tool_use_id=tool_use_id,
                tool_name=_tool_name_for_id(source_frame, tool_use_id),
                content=SYNTHETIC_TOOL_RESULT_CONTENT,
                is_error=True,
            )
        )
    return Frame(
        kind=FrameKind.TOOL_RESULT,
        role=FrameRole.TOOL,
        blocks=tuple(blocks),
        is_meta=True,
        origin=_TRANSCRIPT_RECOVERY_ORIGIN,
        task_id=source_frame.task_id,
        tool_use_id=tool_use_ids[0] if len(tool_use_ids) == 1 else None,
        source_tool_assistant_uuid=source_frame.uuid or source_frame.source_tool_assistant_uuid,
        permission_mode=source_frame.permission_mode,
        timestamp_ms=source_frame.timestamp_ms,
        relevance=source_frame.relevance,
        schema_version=source_frame.schema_version,
        session_id=session_id or source_frame.session_id,
        extra={"reason": "interrupted_tool_missing_result"},
    )


def _tool_name_for_id(frame: Frame, tool_use_id: str) -> str:
    for block in frame.blocks:
        if isinstance(block, ToolUseBlock) and block.tool_use_id == tool_use_id:
            return block.name
    return "unknown_tool"


def _with_recovery_lineage(
    frame: Frame,
    prior_frames: Sequence[Frame],
    *,
    session_id: str | None,
) -> Frame:
    previous = prior_frames[-1] if prior_frames else None
    effective_session_id = session_id or frame.session_id or (previous.session_id if previous else "") or ""
    return assign_lineage(frame, previous, effective_session_id)
