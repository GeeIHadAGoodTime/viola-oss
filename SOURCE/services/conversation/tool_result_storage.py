"""Persist oversized tool responses and replay stable conversation replacements.

Decisions belong to a conversation: an already-seen result keeps its original
representation. Transcript records preserve the exact replacement text on resume
and forks, independently of later changes to preview formatting.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)
TOOL_RESULTS_SUBDIR = "tool-results"
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
TOOL_RESULT_CLEARED_MESSAGE = "[Old tool result content cleared]"
PREVIEW_SIZE_BYTES = 2_000
DEFAULT_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 150_000
DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
BYTES_PER_TOKEN = 4
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DEFAULT_ROOT: Path | None = None
_DEFAULT_ROOT_LOCK = threading.Lock()
_PERSIST_LOCK = threading.RLock()


@dataclass(frozen=True)
class PersistedToolResult:
    filepath: str
    original_size: int
    is_json: bool
    preview: str
    has_more: bool


@dataclass(frozen=True)
class PersistToolResultError:
    error: str


@dataclass(frozen=True)
class ToolResultReplacementRecord:
    tool_use_id: str
    replacement: str
    kind: str = "tool-result"

    def to_dict(self) -> dict[str, str]:
        return dict(kind=self.kind, tool_use_id=self.tool_use_id, replacement=self.replacement)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResultReplacementRecord:
        return cls(
            str(data.get("tool_use_id", "")), str(data.get("replacement", "")), str(data.get("kind", "tool-result"))
        )


@dataclass
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)

    def clone(self) -> ContentReplacementState:
        return ContentReplacementState(self.seen_ids.copy(), self.replacements.copy())


def _resolve_default_root() -> Path:
    global _DEFAULT_ROOT
    with _DEFAULT_ROOT_LOCK:
        if _DEFAULT_ROOT is None:
            try:
                from core.platform import get_data_dir

                _DEFAULT_ROOT = get_data_dir() / "tool_results"
            except Exception:
                logger.exception("Cannot resolve application data directory; using local storage")
                _DEFAULT_ROOT = Path.cwd() / "data" / "tool_results"
        return _DEFAULT_ROOT


def reset_default_root_for_tests() -> None:
    global _DEFAULT_ROOT
    with _DEFAULT_ROOT_LOCK:
        _DEFAULT_ROOT = None


def get_tool_results_dir(session_id: str = "default", *, root: Path | None = None) -> Path:
    directory = Path(root) if root is not None else _resolve_default_root()
    return directory / (session_id if _SAFE_ID_RE.fullmatch(session_id) else "default")


def get_tool_result_path(
    tool_use_id: str, is_json: bool, *, session_id: str = "default", root: Path | None = None
) -> Path:
    if not _SAFE_ID_RE.fullmatch(tool_use_id):
        raise ValueError("Unsafe tool_use_id for filesystem path: %r" % tool_use_id)
    return get_tool_results_dir(session_id, root=root) / (tool_use_id + (".json" if is_json else ".txt"))


def ensure_tool_results_dir(session_id: str = "default", *, root: Path | None = None) -> Path:
    directory = get_tool_results_dir(session_id, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_tool_result_content_empty(content: Any) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                return False
            if isinstance(block.get("text"), str) and block["text"].strip():
                return False
        return True
    return False


def is_content_already_compacted(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(PERSISTED_OUTPUT_TAG)


def _content_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block["text"])
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        )
    return 0


def generate_preview(content: str, max_bytes: int = PREVIEW_SIZE_BYTES) -> tuple[str, bool]:
    """The historical limit argument counts characters, as does the wire budget."""
    ceiling = max(0, max_bytes)
    if len(content) <= ceiling:
        return content, False
    prefix = content[:ceiling]
    boundary = prefix.rfind("\n")
    if boundary > ceiling / 2:
        prefix = prefix[:boundary]
    return prefix, True


def build_large_tool_result_message(result: PersistedToolResult) -> str:
    """Create a new preview; old transcript replacements are never reformatted."""
    lines = [
        PERSISTED_OUTPUT_TAG,
        "Full tool output saved at: " + result.filepath,
        "Original length: %d characters. Preview:" % result.original_size,
        result.preview,
    ]
    if result.has_more:
        lines.append("[Preview ends; read the saved file for the remaining output.]")
    return "\n".join([*lines, PERSISTED_OUTPUT_CLOSING_TAG])


def persist_tool_result(
    content: Any, tool_use_id: str, *, session_id: str = "default", root: Path | None = None
) -> PersistedToolResult | PersistToolResultError:
    if isinstance(content, str):
        serialized, as_json = content, False
    elif isinstance(content, list) and all(isinstance(item, dict) and item.get("type") == "text" for item in content):
        try:
            serialized = json.dumps(content, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            return PersistToolResultError(str(exc))
        as_json = True
    else:
        return PersistToolResultError("Tool-result persistence accepts only text or a list of text blocks")
    try:
        desired = get_tool_result_path(tool_use_id, as_json, session_id=session_id, root=root)
        alternate = desired.with_suffix(".txt" if as_json else ".json")
        # Serialize callers within this process so different content types cannot
        # create competing files for the same tool invocation.
        with _PERSIST_LOCK:
            ensure_tool_results_dir(session_id, root=root)
            actual = next((p for p in (desired, alternate) if p.exists()), desired)
            try:
                with actual.open("x", encoding="utf-8") as handle:
                    handle.write(serialized)
            except FileExistsError:
                serialized = actual.read_text(encoding="utf-8")
        preview, has_more = generate_preview(serialized)
        return PersistedToolResult(str(actual), len(serialized), actual.suffix == ".json", preview, has_more)
    except (OSError, ValueError, UnicodeError) as exc:
        logger.warning("Cannot persist tool result %s: %s", tool_use_id, exc)
        return PersistToolResultError(str(exc))


def is_persist_error(result: Any) -> bool:
    return isinstance(result, PersistToolResultError)


def create_content_replacement_state() -> ContentReplacementState:
    return ContentReplacementState()


def clone_content_replacement_state(source: ContentReplacementState) -> ContentReplacementState:
    return source.clone()


def _eligible_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return []
    selected = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result" or not block.get("tool_use_id"):
            continue
        payload = block.get("content")
        if not payload or is_content_already_compacted(payload):
            continue
        if isinstance(payload, list) and any(
            isinstance(item, dict) and item.get("type") == "image" for item in payload
        ):
            continue
        selected.append(block)
    return selected


def _wire_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Follow provider grouping: only a new assistant identity ends a group."""
    groups: list[list[dict[str, Any]]] = [[]]
    identities: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            nested = message.get("message")
            shapes = (message, nested) if isinstance(nested, dict) else (message,)
            identity = next(
                (
                    shape[key].strip()
                    for shape in shapes
                    for key in ("id", "uuid", "message_id")
                    if isinstance(shape.get(key), str) and shape[key].strip()
                ),
                "",
            )
            if not identity or identity not in identities:
                if groups[-1]:
                    groups.append([])
                if identity:
                    identities.add(identity)
        groups[-1].extend(_eligible_blocks(message))
    return [group for group in groups if group]


def _rewrite(messages: list[dict[str, Any]], replacements: dict[str, str]) -> list[dict[str, Any]]:
    updated = list(messages)
    changed = False
    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        blocks = list(content)
        edited = False
        for position, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                key = str(block.get("tool_use_id") or "")
                if key in replacements:
                    blocks[position] = dict(block, content=replacements[key])
                    edited = True
        if edited:
            updated[index] = dict(message, content=blocks)
            changed = True
    return updated if changed else messages


def enforce_tool_result_budget(
    messages: list[dict[str, Any]],
    state: ContentReplacementState,
    *,
    limit: int = DEFAULT_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS,
    session_id: str = "default",
    root: Path | None = None,
    skip_tool_names: set[str] | None = None,
    tool_name_by_id: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[ToolResultReplacementRecord]]:
    replacements: dict[str, str] = {}
    records: list[ToolResultReplacementRecord] = []
    skipped_names = skip_tool_names or set()
    names = tool_name_by_id or {}
    for group in _wire_groups(messages):
        pending: list[tuple[str, Any, int]] = []
        frozen_size = 0
        for block in group:
            key, payload = str(block["tool_use_id"]), block["content"]
            if key in state.replacements:
                replacements[key] = state.replacements[key]
            elif key in state.seen_ids:
                frozen_size += _content_size(payload)
            elif names.get(key, "") not in skipped_names:
                pending.append((key, payload, _content_size(payload)))
            state.seen_ids.add(key)
        remaining = frozen_size + sum(size for _, _, size in pending)
        for key, payload, size in sorted(pending, key=lambda row: row[2], reverse=True):
            if remaining <= limit:
                break
            remaining -= size
            persisted = persist_tool_result(payload, key, session_id=session_id, root=root)
            if is_persist_error(persisted):
                continue
            text = build_large_tool_result_message(persisted)
            replacements[key] = state.replacements[key] = text
            records.append(ToolResultReplacementRecord(key, text))
    return _rewrite(messages, replacements) if replacements else messages, records


def apply_tool_result_budget(
    messages: list[dict[str, Any]],
    state: ContentReplacementState | None,
    *,
    limit: int = DEFAULT_MAX_TOOL_RESULTS_PER_MESSAGE_CHARS,
    session_id: str = "default",
    root: Path | None = None,
    skip_tool_names: set[str] | None = None,
    tool_name_by_id: dict[str, str] | None = None,
    write_to_transcript: Any = None,
) -> list[dict[str, Any]]:
    if state is None:
        return messages
    updated, records = enforce_tool_result_budget(
        messages,
        state,
        limit=limit,
        session_id=session_id,
        root=root,
        skip_tool_names=skip_tool_names,
        tool_name_by_id=tool_name_by_id,
    )
    if records and write_to_transcript is not None:
        try:
            write_to_transcript(records)
        except Exception:
            logger.exception("Replacement transcript callback failed")
    return updated


def reconstruct_content_replacement_state(
    messages: list[dict[str, Any]],
    records: list[ToolResultReplacementRecord] | list[dict[str, Any]],
    *,
    inherited_replacements: dict[str, str] | None = None,
) -> ContentReplacementState:
    present = {str(block["tool_use_id"]) for message in messages for block in _eligible_blocks(message)}
    replacements = {key: value for key, value in (inherited_replacements or {}).items() if key in present}
    for entry in records:
        if isinstance(entry, ToolResultReplacementRecord):
            entry = entry.to_dict()
        if isinstance(entry, dict) and entry.get("kind") == "tool-result":
            key = str(entry.get("tool_use_id", ""))
            if key in present:
                replacements[key] = str(entry.get("replacement", ""))
    return ContentReplacementState(present, replacements)


def provision_content_replacement_state(
    initial_messages: list[dict[str, Any]] | None = None,
    initial_records: list[ToolResultReplacementRecord] | list[dict[str, Any]] | None = None,
    *,
    enabled: bool = True,
) -> ContentReplacementState | None:
    if not enabled:
        return None
    return reconstruct_content_replacement_state(initial_messages or [], initial_records or [])
