"""Transcript lineage helpers for canonical conversation frames."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from services.conversation.context_frames import Frame


@dataclass(frozen=True)
class TranscriptLink:
    uuid: str
    session_id: str
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    branch_id: str | None = None
    forked_from_uuid: str | None = None


@dataclass(frozen=True)
class LineageReport:
    total_frames: int
    active_leaf_uuid: str | None
    missing_uuid_indexes: tuple[int, ...] = ()
    missing_session_id_indexes: tuple[int, ...] = ()
    missing_parent_uuid_indexes: tuple[int, ...] = ()
    orphan_parent_uuids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (
            self.missing_uuid_indexes
            or self.missing_session_id_indexes
            or self.missing_parent_uuid_indexes
            or self.orphan_parent_uuids
        )


def assign_lineage(frame: Frame, previous: Frame | None, session_id: str) -> Frame:
    """Return ``frame`` with stable Claude-style transcript lineage."""

    frame_uuid = frame.uuid or str(uuid.uuid4())
    parent_uuid = frame.parent_uuid
    if parent_uuid is None and previous is not None:
        parent_uuid = previous.uuid
    logical_parent_uuid = frame.logical_parent_uuid
    if logical_parent_uuid is None:
        logical_parent_uuid = parent_uuid
    return replace(
        frame,
        uuid=frame_uuid,
        session_id=frame.session_id or session_id,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
    )


def walk_active_leaf(frames: Sequence[Frame]) -> list[Frame]:
    """Return the root-to-leaf chain for the newest active transcript leaf."""

    ordered = [frame for frame in frames if isinstance(frame, Frame)]
    if not ordered:
        return []

    uuid_to_frame = {frame.uuid: frame for frame in ordered if frame.uuid}
    if not uuid_to_frame:
        return ordered

    parent_uuids = {frame.parent_uuid for frame in ordered if frame.parent_uuid}
    if not parent_uuids:
        return ordered

    indexed = {id(frame): index for index, frame in enumerate(ordered)}
    leaves = [frame for frame in ordered if frame.uuid and frame.uuid not in parent_uuids]
    if not leaves:
        return ordered

    leaf = max(leaves, key=lambda frame: (frame.timestamp_ms or 0, indexed[id(frame)]))
    chain: list[Frame] = []
    seen: set[str] = set()
    current: Frame | None = leaf
    while current is not None and current.uuid and current.uuid not in seen:
        chain.append(current)
        seen.add(current.uuid)
        parent_uuid = current.parent_uuid
        current = uuid_to_frame.get(parent_uuid) if parent_uuid else None
    chain.reverse()
    return chain or ordered


def recover_orphaned_parallel_tool_results(
    frames: Sequence[Frame],
    active_chain: Sequence[Frame],
) -> list[Frame]:
    """Recover tool-result siblings that the active-leaf walk would drop.

    Mirrors Claude's ``sessionStorage.recoverOrphanedParallelToolResults``:
    after computing the active leaf, scan the full frame set for tool-result
    frames whose ``source_tool_assistant_uuid`` points at an assistant frame
    in the active chain but whose ``tool_use_id`` is not already represented
    in the active chain. Splice them in immediately after the assistant
    frame they originated from so parallel tool invocations from the same
    assistant turn survive an active-leaf walk that would otherwise pick
    just one branch.

    Returns a new chain. Active-chain ordering is preserved apart from the
    inserted sibling tool-result frames.
    """

    from services.conversation.context_frames import ToolResultBlock, ToolUseBlock

    if not active_chain:
        return list(active_chain)

    ordered_all = [frame for frame in frames if isinstance(frame, Frame)]
    active_uuids = {frame.uuid for frame in active_chain if frame.uuid}

    orphans_by_source: dict[str, list[Frame]] = {}
    for frame in ordered_all:
        if frame.uuid in active_uuids:
            continue
        if not frame.source_tool_assistant_uuid:
            continue
        is_tool_result = any(isinstance(block, ToolResultBlock) for block in frame.blocks)
        if not is_tool_result:
            continue
        orphans_by_source.setdefault(frame.source_tool_assistant_uuid, []).append(frame)

    if not orphans_by_source:
        return list(active_chain)

    active_result_ids: set[str] = set()
    for frame in active_chain:
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock) and block.tool_use_id:
                active_result_ids.add(block.tool_use_id)

    recovered: list[Frame] = []
    for frame in active_chain:
        recovered.append(frame)
        if not frame.uuid:
            continue
        candidates = orphans_by_source.get(frame.uuid) or []
        if not candidates:
            continue
        issued_ids = {
            block.tool_use_id for block in frame.blocks if isinstance(block, ToolUseBlock) and block.tool_use_id
        }
        if not issued_ids:
            continue
        for sibling in candidates:
            sibling_ids = {
                block.tool_use_id
                for block in sibling.blocks
                if isinstance(block, ToolResultBlock) and block.tool_use_id
            }
            new_ids = (sibling_ids & issued_ids) - active_result_ids
            if not new_ids:
                continue
            recovered.append(sibling)
            active_result_ids.update(new_ids)

    return recovered


def validate_lineage(frames: Sequence[Frame]) -> LineageReport:
    """Validate stable IDs, session stamps, and parent references."""

    ordered = [frame for frame in frames if isinstance(frame, Frame)]
    uuid_to_frame = {frame.uuid: frame for frame in ordered if frame.uuid}
    missing_uuid: list[int] = []
    missing_session_id: list[int] = []
    missing_parent_uuid: list[int] = []
    orphan_parents: set[str] = set()

    for index, frame in enumerate(ordered):
        if not frame.uuid:
            missing_uuid.append(index)
        if not frame.session_id:
            missing_session_id.append(index)
        if index > 0 and not frame.parent_uuid:
            missing_parent_uuid.append(index)
        if frame.parent_uuid and frame.parent_uuid not in uuid_to_frame:
            orphan_parents.add(frame.parent_uuid)

    active = walk_active_leaf(ordered)
    return LineageReport(
        total_frames=len(ordered),
        active_leaf_uuid=active[-1].uuid if active else None,
        missing_uuid_indexes=tuple(missing_uuid),
        missing_session_id_indexes=tuple(missing_session_id),
        missing_parent_uuid_indexes=tuple(missing_parent_uuid),
        orphan_parent_uuids=tuple(sorted(orphan_parents)),
    )


__all__ = [
    "LineageReport",
    "TranscriptLink",
    "assign_lineage",
    "recover_orphaned_parallel_tool_results",
    "validate_lineage",
    "walk_active_leaf",
]
