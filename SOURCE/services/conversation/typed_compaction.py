"""Typed-frame compaction for canonical conversation chains."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

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

_DEFAULT_KEEP_TOKEN_RATIO = 0.40
_DEFAULT_MIN_KEEP_FRAMES = 6
_DEFAULT_TOOL_RESULT_CHAR_BUDGET = 1_200
_TOKEN_CHAR_RATIO = 4

_COMPACT_BOUNDARY_ORIGINS = frozenset({"compact_boundary", "microcompact_boundary"})
_PROTECTED_META_ORIGIN_MARKERS = (
    "gate",
    "permission",
    "tool_reference",
    "hook",
    "subagent",
    "task_notification",
    "stop",
)


TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class CompactionRequest:
    """Input to frame-aware compaction."""

    chain: object
    token_budget: int
    reason: str
    provider: str | None = None
    summary_text: str | None = None
    token_counter: TokenCounter | None = None
    min_keep_frames: int = _DEFAULT_MIN_KEEP_FRAMES
    keep_token_ratio: float = _DEFAULT_KEEP_TOKEN_RATIO
    force: bool = False
    tool_result_char_budget: int = _DEFAULT_TOOL_RESULT_CHAR_BUDGET


@dataclass(frozen=True)
class CompactionResult:
    """Post-compaction frame chain plus audit evidence."""

    summary_frame: Frame
    kept_frames: tuple[Frame, ...]
    dropped_frame_uuids: tuple[str, ...]
    token_before: int
    token_after: int
    reason: str
    boundary_frame: Frame
    dropped_frames: tuple[Frame, ...] = ()
    compacted: bool = True
    microcompacted_frame_uuids: tuple[str, ...] = ()
    replacement_refs: tuple[str, ...] = ()

    @property
    def frames(self) -> tuple[Frame, ...]:
        """Return the ordered chain to continue with after compaction."""

        if not self.compacted:
            return self.kept_frames
        return (self.boundary_frame, self.summary_frame, *self.kept_frames)


def compact_frames(request: CompactionRequest) -> CompactionResult:
    """Compact a canonical frame chain without falling back to prose history."""

    frames = _project_after_last_compact_boundary(_coerce_chain_frames(request.chain))
    frames = tuple(_ensure_uuid(frame) for frame in frames)
    token_before = _count_frame_tokens(frames, request.token_counter)
    budget = max(1, int(request.token_budget))

    if not request.force and token_before <= budget:
        return _no_compaction_result(frames, token_before, request)

    working_frames = tuple(frame for frame in frames if not _is_compact_boundary_artifact(frame))
    # Match Claude's layered order: replace huge historical tool bodies before
    # deciding which segment is summarized, so neither the summarizer input nor
    # the retained tail carries oversized tool payloads through the next retry.
    replaced_working_frames, micro_ids, refs = _replace_oversized_tool_results(
        working_frames,
        token_counter=request.token_counter,
        char_budget=request.tool_result_char_budget,
    )
    working_frames = tuple(replaced_working_frames)
    keep_indexes = _select_keep_indexes(
        working_frames,
        request=request,
        token_before=token_before,
    )
    keep_indexes = _expand_tool_pairs(working_frames, keep_indexes)

    kept: list[Frame] = []
    dropped: list[Frame] = []
    for idx, frame in enumerate(working_frames):
        if idx in keep_indexes:
            kept.append(frame)
        else:
            dropped.append(frame)

    if not dropped:
        return _no_compaction_result(frames, token_before, request)

    boundary = build_compact_boundary_frame(
        reason=request.reason,
        token_before=token_before,
        token_budget=budget,
        dropped_frames=dropped,
        kept_frames=kept,
        provider=request.provider,
        last_pre_compact_uuid=working_frames[-1].uuid if working_frames else None,
        microcompacted_frame_uuids=micro_ids,
        replacement_refs=refs,
    )
    summary = build_compact_summary_frame(
        summary_text=request.summary_text,
        reason=request.reason,
        dropped_frames=dropped,
        token_before=token_before,
        provider=request.provider,
        parent_uuid=boundary.uuid,
        session_id=_chain_session_id(working_frames),
    )
    token_after = _count_frame_tokens((boundary, summary, *kept), request.token_counter)
    return CompactionResult(
        summary_frame=summary,
        kept_frames=tuple(kept),
        dropped_frame_uuids=tuple(_frame_id(frame) for frame in dropped),
        token_before=token_before,
        token_after=token_after,
        reason=request.reason,
        boundary_frame=boundary,
        dropped_frames=tuple(dropped),
        microcompacted_frame_uuids=tuple(micro_ids),
        replacement_refs=tuple(refs),
    )


def microcompact_frames(
    chain: object,
    *,
    token_budget: int,
    reason: str = "microcompact",
    provider: str | None = None,
    token_counter: TokenCounter | None = None,
    preserve_last_frames: int = _DEFAULT_MIN_KEEP_FRAMES,
    tool_result_char_budget: int = _DEFAULT_TOOL_RESULT_CHAR_BUDGET,
) -> CompactionResult:
    """Replace oversized older tool results with stable references."""

    frames = tuple(_ensure_uuid(frame) for frame in _project_after_last_compact_boundary(_coerce_chain_frames(chain)))
    token_before = _count_frame_tokens(frames, token_counter)
    if token_before <= 0:
        return _no_compaction_result(
            frames,
            token_before,
            CompactionRequest(chain=frames, token_budget=token_budget, reason=reason),
        )

    boundary_index = max(0, len(frames) - max(0, int(preserve_last_frames)))
    candidates = list(frames[:boundary_index])
    protected_tail = list(frames[boundary_index:])
    replaced, micro_ids, refs = _replace_oversized_tool_results(
        candidates,
        token_counter=token_counter,
        char_budget=tool_result_char_budget,
    )
    if not micro_ids:
        return _no_compaction_result(
            frames,
            token_before,
            CompactionRequest(
                chain=frames,
                token_budget=token_budget,
                reason=reason,
                provider=provider,
            ),
        )

    kept = tuple(replaced + protected_tail)
    token_after_replacements = _count_frame_tokens(kept, token_counter)
    tokens_saved = max(0, token_before - token_after_replacements)
    boundary = build_compact_boundary_frame(
        reason=reason,
        token_before=token_before,
        token_budget=token_budget,
        dropped_frames=(),
        kept_frames=kept,
        provider=provider,
        last_pre_compact_uuid=frames[-1].uuid if frames else None,
        microcompacted_frame_uuids=micro_ids,
        replacement_refs=refs,
        trigger="auto",
        tokens_saved=tokens_saved,
    )
    summary = build_compact_summary_frame(
        summary_text="Large historical tool results were replaced with stable references.",
        reason=reason,
        dropped_frames=(),
        token_before=token_before,
        provider=provider,
        parent_uuid=boundary.uuid,
        session_id=_chain_session_id(frames),
        replacement_refs=refs,
    )
    token_after = _count_frame_tokens((boundary, summary, *kept), token_counter)
    return CompactionResult(
        summary_frame=summary,
        kept_frames=kept,
        dropped_frame_uuids=(),
        token_before=token_before,
        token_after=token_after,
        reason=reason,
        boundary_frame=boundary,
        microcompacted_frame_uuids=tuple(micro_ids),
        replacement_refs=tuple(refs),
    )


def build_compact_boundary_frame(
    *,
    reason: str,
    token_before: int,
    token_budget: int,
    dropped_frames: Sequence[Frame],
    kept_frames: Sequence[Frame],
    provider: str | None = None,
    last_pre_compact_uuid: str | None = None,
    microcompacted_frame_uuids: Sequence[str] = (),
    replacement_refs: Sequence[str] = (),
    trigger: str = "auto",
    tokens_saved: int | None = None,
    cleared_attachment_uuids: Sequence[str] = (),
) -> Frame:
    """Return the non-behavioral boundary that marks a compacted segment.

    S8-013: aligns metadata with Claude's
    ``createCompactBoundaryMessage`` / ``createMicrocompactBoundaryMessage``:

    - Boundary subtype is ``compact_boundary`` or ``microcompact_boundary``,
      surfaced via ``extra['subtype']`` (Frame already carries ``origin``).
    - Compact boundaries store ``compact_metadata`` with ``trigger``,
      ``pre_tokens``, ``messages_summarized``, etc.
    - Microcompact boundaries additionally store a SEPARATE
      ``microcompact_metadata`` shape with ``tokens_saved``,
      ``compacted_tool_ids``, ``cleared_attachment_uuids`` so downstream
      observers can differentiate the two without re-parsing.
    - Viola's frame model keeps ``is_meta=True`` for the boundary because
      our behavioral-bail invariant requires meta on the system rail
      (Claude renders boundaries as ``type='system'`` with ``isMeta=false``
      because Claude's `system` channel is never sent to the model).
      The shapes are equivalent on the model-facing wire.
    """

    boundary_uuid = str(uuid.uuid4())
    dropped_ids = tuple(_frame_id(frame) for frame in dropped_frames)
    kept_ids = tuple(_frame_id(frame) for frame in kept_frames)
    is_microcompact = reason == "microcompact" or (len(dropped_frames) == 0 and bool(microcompacted_frame_uuids))
    subtype = "microcompact_boundary" if is_microcompact else "compact_boundary"
    content_text = "Context microcompacted" if is_microcompact else "Conversation compacted"

    compact_metadata: dict[str, Any] = {
        "reason": reason,
        "trigger": trigger,
        "pre_tokens": token_before,
        "token_budget": token_budget,
        "messages_summarized": len(dropped_frames),
        "dropped_frame_uuids": list(dropped_ids),
        "kept_frame_uuids": list(kept_ids),
        "microcompacted_frame_uuids": list(microcompacted_frame_uuids),
        "replacement_refs": list(replacement_refs),
        "provider": provider,
    }
    if kept_frames:
        compact_metadata["preserved_segment"] = {
            "head_uuid": _frame_id(kept_frames[0]),
            "anchor_uuid": boundary_uuid,
            "tail_uuid": _frame_id(kept_frames[-1]),
        }

    extra: dict[str, Any] = {
        "subtype": subtype,
        "compact_metadata": compact_metadata,
        "compactMetadata": compact_metadata,
    }

    if is_microcompact:
        microcompact_metadata = {
            "trigger": trigger,
            "pre_tokens": token_before,
            "tokens_saved": int(tokens_saved if tokens_saved is not None else 0),
            "compacted_tool_ids": list(microcompacted_frame_uuids),
            "cleared_attachment_uuids": list(cleared_attachment_uuids),
        }
        extra["microcompact_metadata"] = microcompact_metadata

    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.SYSTEM,
        blocks=(TextBlock(text=content_text),),
        is_meta=True,
        origin=subtype,
        timestamp_ms=int(time.time() * 1000),
        schema_version=1,
        extra=extra,
        uuid=boundary_uuid,
        logical_parent_uuid=last_pre_compact_uuid,
        session_id=_chain_session_id((*dropped_frames, *kept_frames)),
    )


def build_compact_summary_frame(
    *,
    summary_text: str | None,
    reason: str,
    dropped_frames: Sequence[Frame],
    token_before: int,
    provider: str | None = None,
    parent_uuid: str | None = None,
    session_id: str | None = None,
    replacement_refs: Sequence[str] = (),
) -> Frame:
    """Return the canonical compact-summary frame.

    F-003 (R3-A): emit the summary as a regular user-role frame with
    ``is_compact_summary=True`` and ``is_meta=False`` so the next model
    turn sees a Claude-style continuation (``createUserMessage``,
    ``isCompactSummary: true``, ``isMeta`` unset — see
    ``src/services/compact/compact.ts:596-623``,
    ``src/utils/messages.ts:460-511``). The previous shape (META_USER
    + ``SystemReminderBlock``) made the summary render as a
    ``<system-reminder>`` block inside a meta-user turn, which changed
    model semantics after compaction and skipped Claude's continuation
    wording.
    """

    text = (summary_text or "").strip()
    if not text:
        text = "Earlier conversation was compacted; continue from the preserved frames below."
    extra = {
        "compact_summary": {
            "reason": reason,
            "pre_tokens": token_before,
            "messages_summarized": len(dropped_frames),
            "dropped_frame_uuids": [_frame_id(frame) for frame in dropped_frames],
            "replacement_refs": list(replacement_refs),
            "provider": provider,
        }
    }
    return Frame(
        kind=FrameKind.USER_INPUT,
        role=FrameRole.USER,
        blocks=(TextBlock(text=text),),
        is_meta=False,
        is_compact_summary=True,
        origin="compact",
        timestamp_ms=0,
        schema_version=1,
        extra=extra,
        uuid=str(uuid.uuid4()),
        parent_uuid=parent_uuid,
        logical_parent_uuid=parent_uuid,
        session_id=session_id,
    )


def _coerce_chain_frames(chain: object) -> tuple[Frame, ...]:
    frames = getattr(chain, "frames", chain)
    if frames is None:
        return ()
    return tuple(frame for frame in frames if isinstance(frame, Frame))


def _project_after_last_compact_boundary(frames: Sequence[Frame]) -> tuple[Frame, ...]:
    """Return the live replay window after the most recent compact boundary."""

    boundary_index: int | None = None
    for index, frame in enumerate(frames):
        if _is_compact_boundary_artifact(frame):
            boundary_index = index
    if boundary_index is None:
        return tuple(frames)
    return tuple(frames[boundary_index:])


def _ensure_uuid(frame: Frame) -> Frame:
    if frame.uuid:
        return frame
    return replace(frame, uuid=str(uuid.uuid4()))


def _frame_id(frame: Frame) -> str:
    return frame.uuid or ""


def _chain_session_id(frames: Sequence[Frame]) -> str | None:
    for frame in reversed(frames):
        if frame.session_id:
            return frame.session_id
    return None


def _no_compaction_result(
    frames: Sequence[Frame],
    token_before: int,
    request: CompactionRequest,
) -> CompactionResult:
    boundary = build_compact_boundary_frame(
        reason=request.reason,
        token_before=token_before,
        token_budget=max(1, int(request.token_budget)),
        dropped_frames=(),
        kept_frames=frames,
        provider=request.provider,
        last_pre_compact_uuid=frames[-1].uuid if frames else None,
    )
    summary = build_compact_summary_frame(
        summary_text=request.summary_text,
        reason=request.reason,
        dropped_frames=(),
        token_before=token_before,
        provider=request.provider,
        parent_uuid=boundary.uuid,
        session_id=_chain_session_id(frames),
    )
    return CompactionResult(
        summary_frame=summary,
        kept_frames=tuple(frames),
        dropped_frame_uuids=(),
        token_before=token_before,
        token_after=token_before,
        reason=request.reason,
        boundary_frame=boundary,
        compacted=False,
    )


def _select_keep_indexes(
    frames: Sequence[Frame],
    *,
    request: CompactionRequest,
    token_before: int,
) -> set[int]:
    protected = {idx for idx, frame in enumerate(frames) if _must_survive_compaction(frame)}
    keep_indexes: set[int] = set(protected)
    keep_token_target = max(1, int(max(1, request.token_budget) * max(0.05, request.keep_token_ratio)))
    min_keep = max(0, min(int(request.min_keep_frames), len(frames)))
    selected_tokens = sum(_count_frame_tokens((frames[idx],), request.token_counter) for idx in keep_indexes)

    for idx in range(len(frames) - 1, -1, -1):
        if idx in keep_indexes:
            continue
        if (
            len(keep_indexes) >= min_keep
            and selected_tokens >= keep_token_target
            and token_before > request.token_budget
        ):
            break
        keep_indexes.add(idx)
        selected_tokens += _count_frame_tokens((frames[idx],), request.token_counter)

    if len(keep_indexes) == len(frames) and len(frames) > min_keep:
        for idx, frame in enumerate(frames):
            if idx in protected or _tool_ids(frame):
                continue
            keep_indexes.remove(idx)
            break

    return keep_indexes


def _expand_tool_pairs(frames: Sequence[Frame], keep_indexes: set[int]) -> set[int]:
    by_tool_id: dict[str, set[int]] = {}
    for idx, frame in enumerate(frames):
        for tool_id in _tool_ids(frame):
            by_tool_id.setdefault(tool_id, set()).add(idx)

    expanded = set(keep_indexes)
    changed = True
    while changed:
        changed = False
        for indexes in by_tool_id.values():
            if indexes & expanded and not indexes.issubset(expanded):
                expanded.update(indexes)
                changed = True
    return expanded


def _tool_ids(frame: Frame) -> set[str]:
    ids: set[str] = set()
    if frame.tool_use_id:
        ids.add(frame.tool_use_id)
    for block in frame.blocks:
        if isinstance(block, (ToolUseBlock, ToolResultBlock)) and block.tool_use_id:
            ids.add(block.tool_use_id)
    return ids


def _must_survive_compaction(frame: Frame) -> bool:
    if _is_compact_boundary_artifact(frame):
        return False
    if not frame.is_meta:
        return False
    origin = (frame.origin or "").lower()
    if any(marker in origin for marker in _PROTECTED_META_ORIGIN_MARKERS):
        return True
    if frame.task_id or frame.permission_mode:
        return True
    if _contains_tool_reference(frame.extra):
        return True
    return False


def _contains_tool_reference(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            "tool_reference" in str(key).lower() or _contains_tool_reference(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_tool_reference(item) for item in value)
    if isinstance(value, str):
        return "tool_reference" in value.lower()
    return False


def _is_compact_boundary_artifact(frame: Frame) -> bool:
    return (frame.origin or "") in _COMPACT_BOUNDARY_ORIGINS


def _replace_oversized_tool_results(
    frames: Sequence[Frame],
    *,
    token_counter: TokenCounter | None,
    char_budget: int,
) -> tuple[list[Frame], list[str], list[str]]:
    replaced_frames: list[Frame] = []
    microcompacted_ids: list[str] = []
    refs: list[str] = []
    max_chars = max(80, int(char_budget))

    for frame in frames:
        new_blocks: list[ContentBlock] = []
        changed = False
        frame_refs: list[str] = []
        for block in frame.blocks:
            if not isinstance(block, ToolResultBlock):
                new_blocks.append(block)
                continue
            content = block.content or ""
            if len(content) <= max_chars:
                new_blocks.append(block)
                continue
            ref = _tool_result_ref(frame, block)
            token_estimate = _count_text_tokens(content, token_counter)
            replacement = "[tool result compacted: ref=%s, original_chars=%d, approx_tokens=%d]" % (
                ref,
                len(content),
                token_estimate,
            )
            new_blocks.append(
                ToolResultBlock(
                    tool_use_id=block.tool_use_id,
                    tool_name=block.tool_name,
                    content=replacement,
                    is_error=block.is_error,
                )
            )
            changed = True
            frame_refs.append(ref)
            refs.append(ref)

        if changed:
            frame_id = _frame_id(frame)
            microcompacted_ids.append(frame_id)
            extra = dict(frame.extra)
            prior_refs = list(extra.get("compacted_tool_result_refs", []))
            extra["compacted_tool_result_refs"] = [*prior_refs, *frame_refs]
            extra["tool_result_content_replaced"] = True
            frame = replace(frame, blocks=tuple(new_blocks), extra=extra)
        replaced_frames.append(frame)

    return replaced_frames, microcompacted_ids, refs


def _tool_result_ref(frame: Frame, block: ToolResultBlock) -> str:
    frame_part = frame.uuid or "unpersisted"
    tool_part = block.tool_use_id or frame.tool_use_id or "unknown"
    return "tool-result:%s:%s" % (tool_part, frame_part)


def _count_frame_tokens(frames: Sequence[Frame], token_counter: TokenCounter | None) -> int:
    return sum(_count_text_tokens(_frame_text(frame), token_counter) for frame in frames)


def _count_text_tokens(text: str, token_counter: TokenCounter | None) -> int:
    if not text:
        return 0
    if token_counter is not None:
        return max(1, int(token_counter(text)))
    return max(1, len(text) // _TOKEN_CHAR_RATIO)


def _frame_text(frame: Frame) -> str:
    parts: list[str] = []
    for block in frame.blocks:
        if isinstance(block, (TextBlock, SystemReminderBlock)):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
        elif isinstance(block, ToolUseBlock):
            parts.append("%s %s" % (block.name, block.input))
    return "\n".join(part for part in parts if part)


__all__ = [
    "CompactionRequest",
    "CompactionResult",
    "build_compact_boundary_frame",
    "build_compact_summary_frame",
    "compact_frames",
    "microcompact_frames",
]
