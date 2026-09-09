"""Conversation state, persistence, and compaction helpers.

Manages conversation history with:
- SQLite persistence of conversation turns (A2)
- Typed-frame context compaction (A3)
- Pre-compaction memory extraction flush (A7)
- Multi-step plan tracking
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

from config.defaults import DEFAULT_GPT_MODEL
from core.logging_config import get_logger
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
from services.conversation.lineage import assign_lineage
from services.conversation.repair import recover_transcript_on_resume
from services.conversation.session_identity import (
    ResumeRequest,
    SessionBranch,
    SessionIdentity,
    coerce_session_id,
    create_branch as build_session_branch,
    new_session_id,
    resume_session as build_resume_session,
)
from services.conversation.typed_compaction import (
    CompactionRequest,
    compact_frames,
    microcompact_frames,
)
from services.openai_background import run_background_openai_response
from services.persistence.state_store import STATE_DB_SCHEMA_LOCK

logger = get_logger(__name__)

_SENSITIVE_METADATA_KEY_RE = re.compile(
    r"(?:password|passcode|secret|token|api[_-]?key|card|cvv|cvc|ssn|social_security)",
    re.IGNORECASE,
)


def _scrub_sensitive_metadata(value: object) -> object:
    """Drop sensitive fields before turn metadata is persisted."""

    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_METADATA_KEY_RE.search(key_text):
                continue
            cleaned[key_text] = _scrub_sensitive_metadata(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub_sensitive_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_sensitive_metadata(item) for item in value]
    return value


class ConversationEncryptionUnavailableError(RuntimeError):
    """Raised when conversation content cannot be encrypted for persistence."""


def _get_conversation_encryption(db_path: Path | None = None):
    """Return the shared _MemoryEncryption singleton for conversation content."""
    try:
        from services.memory.store import _get_memory_encryption

        enc = _get_memory_encryption(db_path=db_path)
        if enc is None or not getattr(enc, "encryption_available", False):
            raise ConversationEncryptionUnavailableError(
                "Conversation history encryption is unavailable. Set "
                "VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup "
                "before storing conversation history."
            )
        return enc
    except ConversationEncryptionUnavailableError:
        raise
    except Exception as exc:
        raise ConversationEncryptionUnavailableError(
            "Conversation history encryption is unavailable. Set "
            "VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup "
            "before storing conversation history."
        ) from exc


_DEFAULT_MODEL = DEFAULT_GPT_MODEL
_DEFAULT_CONTEXT_TOKENS = 4_000
_DEFAULT_PLAN_STEP_TIMEOUT_SECONDS = 3_600.0
_MESSAGE_TOKEN_OVERHEAD = 4
# Soft checkpoint threshold for recent legacy history and post-compact frame
# replay. Canonical frames are never raw-FIFO deleted before a compact boundary.
MAX_HISTORY = 200

# Conversation persistence defaults (A2)
_DEFAULT_RESTORE_TURNS = 20
_PRUNE_DAYS = 30

# Compaction defaults (A3)
_COMPACTION_TRIGGER_RATIO = 0.75  # Trigger compaction at 75% of token budget
_DB_FILENAME = "state.sqlite3"

# CC-Pattern-2: Hard ceiling prevents infinite compaction retry loops.
# Claude Code discovered 250K wasted API calls/day from sessions stuck in
# compaction retries.
MAX_CONSECUTIVE_COMPACTION_FAILURES = 3

# S8-007 / Claude `autoCompact.ts` constants. The effective context window
# reserves a fixed budget for the summary output itself; once removed,
# autocompact's threshold sits a buffer under that effective window so
# reactive compaction still has headroom. Mirrors Claude's
# AUTOCOMPACT_BUFFER_TOKENS / WARNING_THRESHOLD_BUFFER_TOKENS /
# ERROR_THRESHOLD_BUFFER_TOKENS / MANUAL_COMPACT_BUFFER_TOKENS /
# MAX_OUTPUT_TOKENS_FOR_SUMMARY.
_MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000
_AUTOCOMPACT_BUFFER_TOKENS = 13_000
_MANUAL_COMPACT_BUFFER_TOKENS = 3_000
_WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
_ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
_MAX_SINGLETON_MANAGERS = 5_000
_INTERNAL_HISTORY_TOOL_TYPES = frozenset({"tool_call", "tool_use", "function_call"})
_INTERNAL_HISTORY_MARKERS = ("echo_probe",)
_INTERNAL_HISTORY_GAUNTLET_RE = re.compile(r"\bCG-[A-Z0-9][A-Z0-9_-]*-\d{9,}\b")
_INTERNAL_HISTORY_TOOL_JSON_RE = re.compile(
    r'\{\s*"type"\s*:\s*"(?:tool_call|tool_use|function_call)"',
    re.IGNORECASE,
)
_FRAME_META_BLOCK_TYPE = "_frame_block_type"
_FRAME_META_SOURCE_TAG = "_frame_source_tag"
_FRAME_META_TOOL_NAME = "_frame_tool_name"
_FRAME_META_TOOL_IS_ERROR = "_frame_tool_is_error"
_FRAME_META_SOURCE_TOOL_ASSISTANT_UUID = "_frame_source_tool_assistant_uuid"
_FRAME_META_PERMISSION_MODE = "_frame_permission_mode"
_FRAME_META_TTL_TURNS = "_frame_ttl_turns"
_FRAME_META_RELEVANCE = "_frame_relevance"
_FRAME_META_UUID = "_frame_uuid"
_FRAME_META_PARENT_UUID = "_frame_parent_uuid"
_FRAME_META_LOGICAL_PARENT_UUID = "_frame_logical_parent_uuid"
_FRAME_META_SESSION_ID = "_frame_session_id"
_FRAME_RESERVED_METADATA_KEYS = frozenset(
    {
        _FRAME_META_BLOCK_TYPE,
        _FRAME_META_SOURCE_TAG,
        _FRAME_META_TOOL_NAME,
        _FRAME_META_TOOL_IS_ERROR,
        _FRAME_META_SOURCE_TOOL_ASSISTANT_UUID,
        _FRAME_META_PERMISSION_MODE,
        _FRAME_META_TTL_TURNS,
        _FRAME_META_RELEVANCE,
        _FRAME_META_UUID,
        _FRAME_META_PARENT_UUID,
        _FRAME_META_LOGICAL_PARENT_UUID,
        _FRAME_META_SESSION_ID,
    }
)
_CONVERSATION_LOG_FRAME_MIGRATIONS = (
    (
        "frame_kind",
        "ALTER TABLE conversation_log ADD COLUMN frame_kind TEXT DEFAULT 'assistant_text'",
    ),
    ("origin", "ALTER TABLE conversation_log ADD COLUMN origin TEXT"),
    ("is_meta", "ALTER TABLE conversation_log ADD COLUMN is_meta INTEGER DEFAULT 0"),
    (
        "is_compact_summary",
        "ALTER TABLE conversation_log ADD COLUMN is_compact_summary INTEGER DEFAULT 0",
    ),
    ("task_id", "ALTER TABLE conversation_log ADD COLUMN task_id TEXT"),
    ("tool_use_id", "ALTER TABLE conversation_log ADD COLUMN tool_use_id TEXT"),
    (
        "schema_version",
        "ALTER TABLE conversation_log ADD COLUMN schema_version INTEGER DEFAULT 0",
    ),
    ("frame_uuid", "ALTER TABLE conversation_log ADD COLUMN frame_uuid TEXT"),
    ("parent_uuid", "ALTER TABLE conversation_log ADD COLUMN parent_uuid TEXT"),
    (
        "logical_parent_uuid",
        "ALTER TABLE conversation_log ADD COLUMN logical_parent_uuid TEXT",
    ),
    (
        "source_tool_assistant_uuid",
        "ALTER TABLE conversation_log ADD COLUMN source_tool_assistant_uuid TEXT",
    ),
    ("permission_mode", "ALTER TABLE conversation_log ADD COLUMN permission_mode TEXT"),
)


def _is_internal_history_artifact(role: str, content: str) -> bool:
    """Return true for tool protocol fragments that should not seed future prompts."""

    role_name = str(role or "").strip().lower()
    text = str(content or "").strip()
    if not text:
        return False
    if "[TOOL_RESULT:" in text:
        return True
    if any(marker in text for marker in _INTERNAL_HISTORY_MARKERS):
        return True
    if _INTERNAL_HISTORY_GAUNTLET_RE.search(text):
        return True
    if role_name == "assistant" and _INTERNAL_HISTORY_TOOL_JSON_RE.search(text):
        return True
    if role_name != "assistant":
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return str(parsed.get("type") or "").strip().lower() in _INTERNAL_HISTORY_TOOL_TYPES


def _scrub_internal_prompt_artifacts(value: object) -> object | None:
    """Remove validation-only protocol fragments before metadata reaches prompts."""

    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            scrubbed = _scrub_internal_prompt_artifacts(item)
            if scrubbed in (None, "", [], {}):
                continue
            cleaned[str(key)] = scrubbed
        return cleaned
    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            scrubbed = _scrub_internal_prompt_artifacts(item)
            if scrubbed in (None, "", [], {}):
                continue
            cleaned_list.append(scrubbed)
        return cleaned_list
    if isinstance(value, tuple):
        return _scrub_internal_prompt_artifacts(list(value))
    if isinstance(value, str):
        text = value.strip()
        if _is_internal_history_artifact("assistant", text):
            return None
        return value
    return value


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "")


def _coerce_frame_role(value: object, *, is_meta: bool = False) -> FrameRole:
    if is_meta:
        return FrameRole.META_USER
    if isinstance(value, FrameRole):
        return value
    role_name = _enum_value(value).strip().lower()
    try:
        return FrameRole(role_name)
    except ValueError:
        return FrameRole.USER


def _coerce_frame_kind(value: object, default: FrameKind) -> FrameKind:
    if isinstance(value, FrameKind):
        return value
    raw = _enum_value(value).strip()
    if raw:
        try:
            return FrameKind(raw)
        except ValueError:
            legacy_to_small = {
                "system_static": FrameKind.SYSTEM_REMINDER,
                "system_dynamic": FrameKind.SYSTEM_REMINDER,
                "gate_state": FrameKind.SYSTEM_REMINDER,
                "task_notification": FrameKind.SYSTEM_REMINDER,
                "account_owner": FrameKind.SYSTEM_REMINDER,
                "profile": FrameKind.SYSTEM_REMINDER,
                "delivery_address": FrameKind.SYSTEM_REMINDER,
                "user_model": FrameKind.SYSTEM_REMINDER,
                "memory": FrameKind.SYSTEM_REMINDER,
                "task_plan": FrameKind.SYSTEM_REMINDER,
                "channel": FrameKind.SYSTEM_REMINDER,
                "runtime_state": FrameKind.SYSTEM_REMINDER,
                "active_agent": FrameKind.SYSTEM_REMINDER,
                "music_route": FrameKind.SYSTEM_REMINDER,
                "hook_context": FrameKind.SYSTEM_REMINDER,
                "skill_load": FrameKind.SYSTEM_REMINDER,
                "compact_summary": FrameKind.SYSTEM_REMINDER,
                "repair_instruction": FrameKind.SYSTEM_REMINDER,
            }
            mapped = legacy_to_small.get(raw)
            if mapped is not None:
                return mapped
    return default


def _coerce_frame_origin(value: object) -> str | None:
    raw = _enum_value(value).strip()
    if not raw or raw in {"human", "model"}:
        return None
    return raw


def _default_kind_for_frame(
    role: FrameRole,
    *,
    is_meta: bool,
) -> FrameKind:
    if is_meta:
        return FrameKind.SYSTEM_REMINDER
    if role is FrameRole.USER:
        return FrameKind.USER_INPUT
    if role is FrameRole.ASSISTANT:
        return FrameKind.ASSISTANT_TEXT
    if role is FrameRole.TOOL:
        return FrameKind.TOOL_RESULT
    return FrameKind.SYSTEM_REMINDER


def _metadata_extra(metadata: dict[str, object] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): copy.deepcopy(value)
        for key, value in metadata.items()
        if str(key) not in _FRAME_RESERVED_METADATA_KEYS
    }


def _frame_text(frame: Frame) -> str:
    parts: list[str] = []
    for block in frame.blocks:
        if isinstance(block, (TextBlock, SystemReminderBlock)):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
        elif isinstance(block, ToolUseBlock):
            try:
                args = json.dumps(block.input, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                args = repr(block.input)
            parts.append("%s %s" % (block.name, args))
    return "\n".join(part for part in parts if part)


def _session_identity_payload(identity: SessionIdentity) -> dict[str, object]:
    return {
        "session_id": identity.session_id,
        "branch_id": identity.branch_id,
        "fork_id": identity.fork_id,
        "parent_session_id": identity.parent_session_id,
        "source_frame_uuid": identity.source_frame_uuid,
        "resumed_from_uuid": identity.resumed_from_uuid,
        "channel": identity.channel,
    }


def _session_event_frame(
    *,
    identity: SessionIdentity,
    origin: str,
    text: str,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    extra: dict[str, object] | None = None,
) -> Frame:
    payload = _session_identity_payload(identity)
    event_extra: dict[str, object] = {
        "session_identity": payload,
        "session_event": origin,
    }
    if extra:
        event_extra.update(copy.deepcopy(extra))
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag=origin.replace("_", "-")),),
        is_meta=True,
        origin=origin,
        extra=event_extra,
        uuid=str(uuid.uuid4()),
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=identity.session_id,
    )


def _make_text_frame(
    *,
    role: object,
    content: str,
    metadata: dict[str, object] | None = None,
    frame_kind: object | None = None,
    origin: object | None = None,
    is_meta: bool = False,
    is_compact_summary: bool = False,
    task_id: str | None = None,
    tool_use_id: str | None = None,
    permission_mode: str | None = None,
    timestamp_ms: int | None = None,
    schema_version: int = 1,
    frame_uuid: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    session_id: str | None = None,
) -> Frame:
    frame_role = _coerce_frame_role(role, is_meta=is_meta)
    frame_origin = _coerce_frame_origin(origin)
    meta_flag = bool(is_meta or frame_role is FrameRole.META_USER)
    default_kind = _default_kind_for_frame(frame_role, is_meta=meta_flag)
    kind = _coerce_frame_kind(frame_kind, default_kind)
    text = str(content or "").strip()
    if frame_role is FrameRole.USER and kind is FrameKind.USER_INPUT and not meta_flag:
        from core.voice_canonical_carrier import current_voice_turn_frame

        voice_frame = current_voice_turn_frame(text, session_id=session_id)
        if voice_frame is not None:
            extra = copy.deepcopy(voice_frame.extra)
            extra.update(_metadata_extra(metadata))
            return replace(
                voice_frame,
                is_compact_summary=bool(is_compact_summary),
                origin=frame_origin or voice_frame.origin,
                task_id=task_id,
                tool_use_id=tool_use_id,
                permission_mode=permission_mode,
                timestamp_ms=(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)),
                schema_version=schema_version,
                extra=extra,
                uuid=frame_uuid,
                parent_uuid=parent_uuid,
                logical_parent_uuid=logical_parent_uuid,
                session_id=session_id or voice_frame.session_id,
            )
    if frame_role is FrameRole.META_USER:
        blocks: tuple[ContentBlock, ...] = (
            SystemReminderBlock(text=text, source_tag=str(kind.value).replace("_", "-")),
        )
    else:
        blocks = (TextBlock(text=text),)
    return Frame(
        kind=kind,
        role=frame_role,
        blocks=blocks,
        is_meta=meta_flag,
        is_compact_summary=bool(is_compact_summary),
        origin=frame_origin,
        task_id=task_id,
        tool_use_id=tool_use_id,
        permission_mode=permission_mode,
        timestamp_ms=(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)),
        schema_version=schema_version,
        extra=_metadata_extra(metadata),
        uuid=frame_uuid,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=session_id,
    )


def _frame_with_defaults(
    frame: Frame,
    timestamp_ms: int,
    *,
    session_id: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
) -> Frame:
    return replace(
        frame,
        timestamp_ms=frame.timestamp_ms or timestamp_ms,
        extra=copy.deepcopy(frame.extra),
        uuid=frame.uuid or str(uuid.uuid4()),
        parent_uuid=frame.parent_uuid or parent_uuid,
        logical_parent_uuid=frame.logical_parent_uuid or logical_parent_uuid,
        session_id=frame.session_id or session_id,
    )


def _frame_with_timestamp(frame: Frame, timestamp_ms: int) -> Frame:
    return _frame_with_defaults(frame, timestamp_ms)


def _frame_to_storage_payload(frame: Frame) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = copy.deepcopy(frame.extra) if isinstance(frame.extra, dict) else {}
    metadata[_FRAME_META_RELEVANCE] = frame.relevance
    if frame.uuid is not None:
        metadata[_FRAME_META_UUID] = frame.uuid
    if frame.parent_uuid is not None:
        metadata[_FRAME_META_PARENT_UUID] = frame.parent_uuid
    if frame.logical_parent_uuid is not None:
        metadata[_FRAME_META_LOGICAL_PARENT_UUID] = frame.logical_parent_uuid
    if frame.session_id is not None:
        metadata[_FRAME_META_SESSION_ID] = frame.session_id
    if frame.source_tool_assistant_uuid is not None:
        metadata[_FRAME_META_SOURCE_TOOL_ASSISTANT_UUID] = frame.source_tool_assistant_uuid
    if frame.permission_mode is not None:
        metadata[_FRAME_META_PERMISSION_MODE] = frame.permission_mode
    if frame.ttl_turns is not None:
        metadata[_FRAME_META_TTL_TURNS] = frame.ttl_turns

    if len(frame.blocks) == 1:
        block = frame.blocks[0]
        if isinstance(block, SystemReminderBlock):
            metadata[_FRAME_META_BLOCK_TYPE] = "system_reminder"
            if block.source_tag is not None:
                metadata[_FRAME_META_SOURCE_TAG] = block.source_tag
            return block.text, metadata
        if isinstance(block, TextBlock):
            metadata[_FRAME_META_BLOCK_TYPE] = "text"
            return block.text, metadata
        if isinstance(block, ToolResultBlock):
            metadata[_FRAME_META_BLOCK_TYPE] = "tool_result"
            metadata[_FRAME_META_TOOL_NAME] = block.tool_name
            metadata[_FRAME_META_TOOL_IS_ERROR] = bool(block.is_error)
            return block.content, metadata
        if isinstance(block, ToolUseBlock):
            metadata[_FRAME_META_BLOCK_TYPE] = "tool_use"
            metadata[_FRAME_META_TOOL_NAME] = block.name
            try:
                return (
                    json.dumps(block.input, ensure_ascii=False, sort_keys=True),
                    metadata,
                )
            except (TypeError, ValueError):
                return (
                    json.dumps({"_repr": repr(block.input)}, ensure_ascii=False),
                    metadata,
                )

    # Multi-block frame: persist the full block array structurally so the
    # rehydrated frame keeps tool_use / tool_result blocks instead of collapsing
    # to flattened text. Claude TS's normalizeMessages splits multi-block messages
    # into typed single-block messages; we preserve the same fidelity by
    # round-tripping the original blocks via JSON.
    metadata[_FRAME_META_BLOCK_TYPE] = "multi"
    serialised_blocks = [_block_to_storage_dict(block) for block in frame.blocks]
    try:
        return json.dumps({"blocks": serialised_blocks}, ensure_ascii=False), metadata
    except (TypeError, ValueError):
        # Last-resort fallback: flatten to text rather than crashing persistence.
        # The metadata marker is downgraded so rehydration produces a plain TextBlock.
        metadata[_FRAME_META_BLOCK_TYPE] = "text"
        return _frame_text(frame), metadata


def _block_to_storage_dict(block: ContentBlock) -> dict[str, Any]:
    """Serialize a single ContentBlock to a JSON-safe dict for multi-block storage."""

    if isinstance(block, SystemReminderBlock):
        payload: dict[str, Any] = {"type": "system_reminder", "text": block.text}
        if block.source_tag is not None:
            payload["source_tag"] = block.source_tag
        return payload
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        try:
            input_payload: Any = json.loads(json.dumps(block.input, ensure_ascii=False))
        except (TypeError, ValueError):
            input_payload = {"_repr": repr(block.input)}
        return {
            "type": "tool_use",
            "tool_use_id": block.tool_use_id,
            "name": block.name,
            "input": input_payload,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "tool_name": block.tool_name,
            "content": block.content,
            "is_error": bool(block.is_error),
        }
    # Defensive fallback for unknown future block types.
    return {"type": "unknown", "repr": repr(block)}


def _block_from_storage_dict(payload: dict[str, Any], *, fallback_tool_use_id: str | None) -> ContentBlock | None:
    block_type = str(payload.get("type") or "").strip()
    if block_type == "system_reminder":
        text = payload.get("text")
        if not isinstance(text, str):
            return None
        source_tag = payload.get("source_tag")
        return SystemReminderBlock(
            text=text,
            source_tag=(str(source_tag) if isinstance(source_tag, str) and source_tag else None),
        )
    if block_type == "text":
        text = payload.get("text")
        if not isinstance(text, str):
            return None
        return TextBlock(text=text)
    if block_type == "tool_use":
        tool_use_id = str(payload.get("tool_use_id") or fallback_tool_use_id or "")
        name = str(payload.get("name") or "")
        input_args = payload.get("input")
        if not isinstance(input_args, dict):
            input_args = {"value": input_args} if input_args is not None else {}
        return ToolUseBlock(tool_use_id=tool_use_id, name=name, input=input_args)
    if block_type == "tool_result":
        tool_use_id = str(payload.get("tool_use_id") or fallback_tool_use_id or "")
        tool_name = str(payload.get("tool_name") or "")
        content = payload.get("content")
        if not isinstance(content, str):
            content = "" if content is None else json.dumps(content, ensure_ascii=False)
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            content=content,
            is_error=bool(payload.get("is_error")),
        )
    return None


def _blocks_from_storage(
    *,
    frame_role: FrameRole,
    content: str,
    metadata: dict[str, object],
    tool_use_id: str | None,
) -> tuple[ContentBlock, ...]:
    block_type = str(metadata.get(_FRAME_META_BLOCK_TYPE) or "").strip()
    if block_type == "multi":
        # Multi-block frames are persisted as a JSON {"blocks": [...]} payload so
        # the original block order/types (e.g. interleaved TextBlock + ToolUseBlock)
        # survive resume. Fall through to text rehydration if parsing fails.
        try:
            parsed = json.loads(content) if isinstance(content, str) and content else None
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            raw_blocks = parsed.get("blocks")
            if isinstance(raw_blocks, list) and raw_blocks:
                rebuilt: list[ContentBlock] = []
                for raw in raw_blocks:
                    if not isinstance(raw, dict):
                        continue
                    block = _block_from_storage_dict(raw, fallback_tool_use_id=tool_use_id)
                    if block is not None:
                        rebuilt.append(block)
                if rebuilt:
                    return tuple(rebuilt)
        # Fall through to text rehydration when the multi-payload is malformed.
        return (TextBlock(text=content),)
    if block_type == "system_reminder" or frame_role is FrameRole.META_USER:
        source_tag = metadata.get(_FRAME_META_SOURCE_TAG)
        return (
            SystemReminderBlock(
                text=content,
                source_tag=(str(source_tag) if isinstance(source_tag, str) and source_tag else None),
            ),
        )
    if block_type == "tool_result":
        tool_name = metadata.get(_FRAME_META_TOOL_NAME)
        is_error = bool(metadata.get(_FRAME_META_TOOL_IS_ERROR))
        return (
            ToolResultBlock(
                tool_use_id=tool_use_id or "",
                tool_name=str(tool_name or ""),
                content=content,
                is_error=is_error,
            ),
        )
    if block_type == "tool_use":
        tool_name = metadata.get(_FRAME_META_TOOL_NAME)
        try:
            parsed_input = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_input = {}
        return (
            ToolUseBlock(
                tool_use_id=tool_use_id or "",
                name=str(tool_name or ""),
                input=(parsed_input if isinstance(parsed_input, dict) else {"value": parsed_input}),
            ),
        )
    return (TextBlock(text=content),)


def _hydrate_frame_from_storage(
    *,
    role: object,
    content: str,
    timestamp: object,
    metadata: dict[str, object] | None,
    frame_kind: object | None,
    origin: object | None,
    is_meta: object,
    is_compact_summary: object,
    task_id: object,
    tool_use_id: object,
    schema_version: object,
    frame_uuid: object = None,
    parent_uuid: object = None,
    logical_parent_uuid: object = None,
    source_tool_assistant_uuid: object = None,
    permission_mode: object = None,
    session_id: object = None,
) -> Frame:
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    schema_num = int(schema_version or 0)
    frame_role = _coerce_frame_role(role, is_meta=bool(is_meta))
    frame_is_meta = bool(is_meta or frame_role is FrameRole.META_USER)
    frame_origin = _coerce_frame_origin(origin)
    default_kind = _default_kind_for_frame(frame_role, is_meta=frame_is_meta)
    kind = _coerce_frame_kind(frame_kind, default_kind)
    timestamp_ms = int(float(timestamp or 0.0) * 1000)
    tool_id = str(tool_use_id) if tool_use_id else None
    source_uuid = source_tool_assistant_uuid or metadata_dict.get(_FRAME_META_SOURCE_TOOL_ASSISTANT_UUID)
    perm_mode = permission_mode or metadata_dict.get(_FRAME_META_PERMISSION_MODE)
    return Frame(
        kind=kind,
        role=frame_role,
        blocks=_blocks_from_storage(
            frame_role=frame_role,
            content=content,
            metadata=metadata_dict,
            tool_use_id=tool_id,
        ),
        is_meta=frame_is_meta,
        is_compact_summary=bool(is_compact_summary),
        source_tool_assistant_uuid=str(source_uuid) if source_uuid else None,
        origin=frame_origin,
        tool_use_id=tool_id,
        task_id=str(task_id) if task_id else None,
        permission_mode=str(perm_mode) if perm_mode else None,
        timestamp_ms=timestamp_ms,
        ttl_turns=(
            int(metadata_dict[_FRAME_META_TTL_TURNS]) if metadata_dict.get(_FRAME_META_TTL_TURNS) is not None else None
        ),
        relevance=str(metadata_dict.get(_FRAME_META_RELEVANCE) or "always"),
        schema_version=schema_num,
        extra=_metadata_extra(metadata_dict),
        uuid=str(frame_uuid or metadata_dict.get(_FRAME_META_UUID) or "") or None,
        parent_uuid=str(parent_uuid or metadata_dict.get(_FRAME_META_PARENT_UUID) or "") or None,
        logical_parent_uuid=str(logical_parent_uuid or metadata_dict.get(_FRAME_META_LOGICAL_PARENT_UUID) or "")
        or None,
        session_id=str(session_id or metadata_dict.get(_FRAME_META_SESSION_ID) or "") or None,
    )


def _frame_to_legacy_message(frame: Frame) -> ConversationMessage | None:
    content = _frame_text(frame).strip()
    role = frame.role.value
    if frame.role is FrameRole.META_USER or frame.is_meta:
        return None
    if _is_internal_history_artifact(role, content):
        return None
    token_count = len(content.split()) + _MESSAGE_TOKEN_OVERHEAD
    message: ConversationMessage = {
        "role": role,
        "content": content,
        "timestamp": ((frame.timestamp_ms / 1000.0) if frame.timestamp_ms else time.time()),
        "token_count": token_count,
    }
    if frame.extra:
        message["metadata"] = copy.deepcopy(frame.extra)
    return message


def _frame_tool_use_ids(frame: Frame) -> set[str]:
    ids: set[str] = set()
    if frame.kind is FrameKind.TOOL_USE and frame.tool_use_id:
        ids.add(frame.tool_use_id)
    for block in frame.blocks:
        if isinstance(block, ToolUseBlock) and block.tool_use_id:
            ids.add(block.tool_use_id)
    return ids


def _frame_tool_result_ids(frame: Frame) -> set[str]:
    ids: set[str] = set()
    if frame.kind is FrameKind.TOOL_RESULT and frame.tool_use_id:
        ids.add(frame.tool_use_id)
    for block in frame.blocks:
        if isinstance(block, ToolResultBlock) and block.tool_use_id:
            ids.add(block.tool_use_id)
    return ids


def _frame_chain_fingerprint(frames: Iterable[Frame]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            frame.uuid,
            frame.session_id,
            frame.parent_uuid,
            frame.logical_parent_uuid,
            frame.source_tool_assistant_uuid,
            frame.kind.value,
            frame.role.value,
            frame.origin,
            frame.tool_use_id,
            frame.task_id,
            frame.permission_mode,
            frame.is_meta,
            frame.is_compact_summary,
            _frame_text(frame),
        )
        for frame in frames
    )


# Type-only persistence contracts kept for conversation and plan annotations.
class ConversationMessage(TypedDict, total=False):
    """Stored conversation message with token metadata."""

    role: str
    content: str
    timestamp: float
    token_count: int
    metadata: dict[str, object]


class PlanStep(TypedDict, total=False):
    """Single plan step tracked across turns."""

    id: str
    description: str
    status: str
    result: str | None
    created_at: float
    updated_at: float
    expires_at: float | None
    metadata: dict[str, object]


class PlanState(TypedDict, total=False):
    """Tracked multi-step plan state."""

    id: str
    status: str
    created_at: float
    updated_at: float
    last_refined_at: float
    steps: list[PlanStep]


# ---------------------------------------------------------------------------
# Conversation Persistence Store (A2)
# ---------------------------------------------------------------------------


class ConversationPersistence:
    """Persist conversation turns to SQLite for cross-session continuity.

    Uses the existing ``state.sqlite3`` database with the ``conversation_log``
    table.  Thread-safe via internal lock.
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is not None:
            base_path = Path(root)
        else:
            try:
                from config.settings import settings as _cfg

                base_path = Path(_cfg.data_dir)
            except Exception:
                base_path = Path.cwd()

        self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the conversation_log table if absent."""
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       TEXT NOT NULL,
                    session_id    TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    content       TEXT NOT NULL,
                    timestamp     REAL NOT NULL,
                    metadata_json TEXT
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_user_session
                ON conversation_log(user_id, session_id, timestamp)
                """)
            existing_columns = {
                str(row["name"]) for row in self._conn.execute("PRAGMA table_info(conversation_log)").fetchall()
            }
            for column_name, statement in _CONVERSATION_LOG_FRAME_MIGRATIONS:
                if column_name not in existing_columns:
                    self._conn.execute(statement)
        logger.debug("conversation_log schema ensured at %s", self._db_path)

    def write_turn(
        self,
        user_id: str,
        session_id: str,
        frame: Frame | str | None = None,
        content: str | None = None,
        metadata: dict[str, object] | None = None,
        *,
        role: str | None = None,
    ) -> None:
        """Persist a single typed conversation frame (encrypted at rest)."""

        timestamp_ms = int(time.time() * 1000)
        if isinstance(frame, Frame):
            stored_frame = _frame_with_defaults(frame, timestamp_ms, session_id=session_id)
        else:
            stored_frame = _make_text_frame(
                role=role if frame is None else frame,
                content=content or "",
                metadata=metadata,
                timestamp_ms=timestamp_ms,
                session_id=session_id,
            )
        stored_plaintext, frame_metadata = _frame_to_storage_payload(stored_frame)
        meta_json = json.dumps(frame_metadata, ensure_ascii=False) if frame_metadata else None
        ts = stored_frame.timestamp_ms / 1000.0 if stored_frame.timestamp_ms else time.time()
        try:
            # Encrypt content at rest using the shared _MemoryEncryption singleton.
            stored_content = _get_conversation_encryption(self._db_path).encrypt(stored_plaintext)
        except Exception as exc:
            logger.error("Conversation content encryption failed; refusing to store conversation turn")
            raise ConversationEncryptionUnavailableError(
                "Conversation history was not stored because encryption is unavailable. "
                "Set VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup "
                "before storing conversation history."
            ) from exc
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO conversation_log (
                    user_id,
                    session_id,
                    role,
                    content,
                    timestamp,
                    metadata_json,
                    frame_kind,
                    origin,
                    is_meta,
                    is_compact_summary,
                    task_id,
                    tool_use_id,
                    schema_version,
                    frame_uuid,
                    parent_uuid,
                    logical_parent_uuid,
                    source_tool_assistant_uuid,
                    permission_mode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    stored_frame.role.value,
                    stored_content,
                    ts,
                    meta_json,
                    stored_frame.kind.value,
                    stored_frame.origin,
                    1 if stored_frame.is_meta else 0,
                    1 if stored_frame.is_compact_summary else 0,
                    stored_frame.task_id,
                    stored_frame.tool_use_id,
                    stored_frame.schema_version,
                    stored_frame.uuid,
                    stored_frame.parent_uuid,
                    stored_frame.logical_parent_uuid,
                    stored_frame.source_tool_assistant_uuid,
                    stored_frame.permission_mode,
                ),
            )

    def replace_session_frames(self, user_id: str, session_id: str, frames: Iterable[Frame]) -> None:
        """Atomically replace one persisted session with a typed frame chain."""

        session_key = str(session_id or "").strip()
        if not session_key:
            raise ValueError("session_id is required")

        timestamp_ms = int(time.time() * 1000)
        rows: list[tuple[object, ...]] = []
        try:
            enc = _get_conversation_encryption(self._db_path)
            for index, frame in enumerate(frames):
                stored_frame = _frame_with_defaults(
                    frame,
                    timestamp_ms + index,
                    session_id=session_key,
                )
                stored_plaintext, frame_metadata = _frame_to_storage_payload(stored_frame)
                meta_json = json.dumps(frame_metadata, ensure_ascii=False) if frame_metadata else None
                stored_content = enc.encrypt(stored_plaintext)
                ts = stored_frame.timestamp_ms / 1000.0 if stored_frame.timestamp_ms else time.time()
                rows.append(
                    (
                        user_id,
                        session_key,
                        stored_frame.role.value,
                        stored_content,
                        ts,
                        meta_json,
                        stored_frame.kind.value,
                        stored_frame.origin,
                        1 if stored_frame.is_meta else 0,
                        1 if stored_frame.is_compact_summary else 0,
                        stored_frame.task_id,
                        stored_frame.tool_use_id,
                        stored_frame.schema_version,
                        stored_frame.uuid,
                        stored_frame.parent_uuid,
                        stored_frame.logical_parent_uuid,
                        stored_frame.source_tool_assistant_uuid,
                        stored_frame.permission_mode,
                    )
                )
        except Exception as exc:
            logger.error("Conversation content encryption failed; refusing to replace conversation session")
            raise ConversationEncryptionUnavailableError(
                "Conversation history was not replaced because encryption is unavailable. "
                "Set VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup "
                "before storing conversation history."
            ) from exc

        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM conversation_log WHERE user_id = ? AND session_id = ?",
                (user_id, session_key),
            )
            self._conn.executemany(
                """
                INSERT INTO conversation_log (
                    user_id,
                    session_id,
                    role,
                    content,
                    timestamp,
                    metadata_json,
                    frame_kind,
                    origin,
                    is_meta,
                    is_compact_summary,
                    task_id,
                    tool_use_id,
                    schema_version,
                    frame_uuid,
                    parent_uuid,
                    logical_parent_uuid,
                    source_tool_assistant_uuid,
                    permission_mode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load_recent_turns(
        self,
        user_id: str,
        limit: int = _DEFAULT_RESTORE_TURNS,
        session_id: str | None = None,
    ) -> list[Frame]:
        """Load the most recent conversation frames.

        If ``session_id`` is None, loads from the most recent session.
        Returns frames in chronological order (oldest first).
        """
        with self._lock:
            if session_id is None:
                # Find the most recent session
                row = self._conn.execute(
                    """
                    SELECT session_id
                    FROM conversation_log
                    WHERE user_id = ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    return []
                session_id = row["session_id"]

            rows = self._conn.execute(
                """
                SELECT
                    role,
                    content,
                    timestamp,
                    metadata_json,
                    frame_kind,
                    origin,
                    is_meta,
                    is_compact_summary,
                    task_id,
                    tool_use_id,
                    schema_version,
                    frame_uuid,
                    parent_uuid,
                    logical_parent_uuid,
                    source_tool_assistant_uuid,
                    permission_mode,
                    session_id
                FROM conversation_log
                WHERE user_id = ? AND session_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (user_id, session_id, max(1, limit)),
            ).fetchall()

        # Reverse to chronological order; decrypt content at rest.
        try:
            enc = _get_conversation_encryption(self._db_path)
        except ConversationEncryptionUnavailableError:
            logger.warning("Conversation history encryption unavailable; refusing to load persisted turns")
            return []
        result: list[Frame] = []
        for row in reversed(rows):
            raw_content = row["content"]
            if enc is not None:
                try:
                    raw_content = enc.decrypt(raw_content)
                except Exception:
                    pass  # legacy plaintext or key mismatch — return as-is
            metadata: dict[str, object] = {}
            if row["metadata_json"]:
                try:
                    raw_metadata = json.loads(row["metadata_json"])
                    if isinstance(raw_metadata, dict):
                        metadata = raw_metadata
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(
                _hydrate_frame_from_storage(
                    role=row["role"],
                    content=str(raw_content or ""),
                    timestamp=row["timestamp"],
                    metadata=metadata,
                    frame_kind=row["frame_kind"],
                    origin=row["origin"],
                    is_meta=row["is_meta"],
                    is_compact_summary=row["is_compact_summary"],
                    task_id=row["task_id"],
                    tool_use_id=row["tool_use_id"],
                    schema_version=row["schema_version"],
                    frame_uuid=row["frame_uuid"],
                    parent_uuid=row["parent_uuid"],
                    logical_parent_uuid=row["logical_parent_uuid"],
                    source_tool_assistant_uuid=row["source_tool_assistant_uuid"],
                    permission_mode=row["permission_mode"],
                    session_id=row["session_id"],
                )
            )
        return result

    def load_session_frames(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[Frame]:
        """Load frames for an explicit session in chronological order."""

        session_key = str(session_id or "").strip()
        if not session_key:
            return []

        max_limit = max(1, int(limit)) if limit is not None else None
        with self._lock:
            if max_limit is None:
                rows = self._conn.execute(
                    """
                    SELECT
                        role,
                        content,
                        timestamp,
                        metadata_json,
                        frame_kind,
                        origin,
                        is_meta,
                        is_compact_summary,
                        task_id,
                        tool_use_id,
                        schema_version,
                        frame_uuid,
                        parent_uuid,
                        logical_parent_uuid,
                        source_tool_assistant_uuid,
                        permission_mode,
                        session_id
                    FROM conversation_log
                    WHERE user_id = ? AND session_id = ?
                    ORDER BY timestamp ASC
                    """,
                    (user_id, session_key),
                ).fetchall()
            else:
                rows = list(
                    reversed(
                        self._conn.execute(
                            """
                            SELECT
                                role,
                                content,
                                timestamp,
                                metadata_json,
                                frame_kind,
                                origin,
                                is_meta,
                                is_compact_summary,
                                task_id,
                                tool_use_id,
                                schema_version,
                                frame_uuid,
                                parent_uuid,
                                logical_parent_uuid,
                                source_tool_assistant_uuid,
                                permission_mode,
                                session_id
                            FROM conversation_log
                            WHERE user_id = ? AND session_id = ?
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            (user_id, session_key, max_limit),
                        ).fetchall()
                    )
                )

        try:
            enc = _get_conversation_encryption(self._db_path)
        except ConversationEncryptionUnavailableError:
            logger.warning("Conversation history encryption unavailable; refusing to load session frames")
            return []

        frames: list[Frame] = []
        for row in rows:
            raw_content = row["content"]
            if enc is not None:
                try:
                    raw_content = enc.decrypt(raw_content)
                except Exception:
                    pass
            metadata: dict[str, object] = {}
            if row["metadata_json"]:
                try:
                    raw_metadata = json.loads(row["metadata_json"])
                    if isinstance(raw_metadata, dict):
                        metadata = raw_metadata
                except (json.JSONDecodeError, TypeError):
                    pass
            frames.append(
                _hydrate_frame_from_storage(
                    role=row["role"],
                    content=str(raw_content or ""),
                    timestamp=row["timestamp"],
                    metadata=metadata,
                    frame_kind=row["frame_kind"],
                    origin=row["origin"],
                    is_meta=row["is_meta"],
                    is_compact_summary=row["is_compact_summary"],
                    task_id=row["task_id"],
                    tool_use_id=row["tool_use_id"],
                    schema_version=row["schema_version"],
                    frame_uuid=row["frame_uuid"],
                    parent_uuid=row["parent_uuid"],
                    logical_parent_uuid=row["logical_parent_uuid"],
                    source_tool_assistant_uuid=row["source_tool_assistant_uuid"],
                    permission_mode=row["permission_mode"],
                    session_id=row["session_id"],
                )
            )
        return frames

    def session_exists(self, user_id: str, session_id: str) -> bool:
        """Return whether an explicit persisted session exists for a user."""

        session_key = str(session_id or "").strip()
        if not session_key:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM conversation_log
                WHERE user_id = ? AND session_id = ?
                LIMIT 1
                """,
                (user_id, session_key),
            ).fetchone()
        return row is not None

    def latest_session_id(
        self,
        user_id: str,
        *,
        exclude_session_id: str | None = None,
    ) -> str | None:
        """Return the most recently touched session for a user."""

        excluded = str(exclude_session_id or "").strip()
        with self._lock:
            if excluded:
                row = self._conn.execute(
                    """
                    SELECT session_id, MAX(timestamp) AS last_ts
                    FROM conversation_log
                    WHERE user_id = ? AND session_id != ?
                    GROUP BY session_id
                    ORDER BY last_ts DESC
                    LIMIT 1
                    """,
                    (user_id, excluded),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT session_id, MAX(timestamp) AS last_ts
                    FROM conversation_log
                    WHERE user_id = ?
                    GROUP BY session_id
                    ORDER BY last_ts DESC
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
        return str(row["session_id"]) if row is not None else None

    def prune_old(self, user_id: str, days: int = _PRUNE_DAYS) -> int:
        """Delete conversation entries older than ``days``.  Returns count deleted."""
        cutoff = time.time() - days * 86400
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM conversation_log WHERE user_id = ? AND timestamp < ?",
                (user_id, cutoff),
            )
            count = cursor.rowcount
        if count > 0:
            logger.info("Pruned %d conversation log entries older than %d days", count, days)
        return count

    def clear_history(self, user_id: str, session_id: str | None = None) -> int:
        """Delete persisted conversation turns for a user or a specific session."""
        with self._lock, self._conn:
            if session_id:
                cursor = self._conn.execute(
                    "DELETE FROM conversation_log WHERE user_id = ? AND session_id = ?",
                    (user_id, session_id),
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM conversation_log WHERE user_id = ?",
                    (user_id,),
                )
            count = cursor.rowcount
        if count > 0:
            if session_id:
                logger.info(
                    "Cleared %d persisted conversation turn(s) for user=%s session=%s",
                    count,
                    user_id,
                    session_id,
                )
            else:
                logger.info(
                    "Cleared %d persisted conversation turn(s) for user=%s",
                    count,
                    user_id,
                )
        return count

    def close(self) -> None:
        """Checkpoint WAL and close."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass


_persistence_singleton: ConversationPersistence | None = None
_persistence_lock = threading.Lock()


def get_conversation_persistence(root: Path | None = None) -> ConversationPersistence:
    """Return the process-wide ConversationPersistence singleton."""
    global _persistence_singleton
    if _persistence_singleton is None:
        with _persistence_lock:
            if _persistence_singleton is None:
                _persistence_singleton = ConversationPersistence(root=root)
    return _persistence_singleton


# ---------------------------------------------------------------------------
# Context Compaction (A3) + Pre-Compaction Flush (A7)
# ---------------------------------------------------------------------------


async def _extract_facts_for_memory(
    messages: list[ConversationMessage],
    *,
    user_id: str = "",
    session_id: str = "",
) -> list[str]:
    """Extract key facts from conversation messages before compaction (A7).

    Uses the shared background compaction helper for cheap extraction.
    The inline system prompt below is private extraction helper text, not a
    user-facing agent prompt; never move it to module level.
    Returns a list of fact strings to store in MemoryStore.
    """
    if not messages:
        return []

    # Build a condensed transcript
    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            transcript_parts.append("%s: %s" % (role, content[:500]))

    transcript = "\n".join(transcript_parts[-20:])  # Last 20 messages max
    if not transcript.strip():
        return []

    extraction_prompt = (
        "Extract the key facts, preferences, and important information from this conversation "
        "that should be remembered long-term. Return ONLY a JSON array of short fact strings. "
        "Include: names, preferences, locations, important dates, corrections, routines. "
        "Exclude: greetings, filler, questions, temporary info.\n\n"
        "Conversation:\n%s\n\n"
        'Return a JSON array like: ["User\'s name is Alice", "Prefers jazz music"]'
    ) % transcript

    try:
        text = (
            await run_background_openai_response(
                system_prompt="You extract key facts from conversations. Return only a JSON array of fact strings.",
                user_content=extraction_prompt,
                max_output_tokens=500,
                user_id=user_id,
                session_id=session_id,
                timeout_s=15.0,
            )
        ).strip()

        # Parse JSON array from response
        # Handle potential markdown code blocks
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        facts = json.loads(text)
        if isinstance(facts, list):
            return [str(f) for f in facts if f and len(str(f)) >= 5]
    except Exception as exc:
        logger.debug("Fact extraction failed, skipping pre-compaction flush: %s", exc)

    return []


async def _summarize_messages(
    messages: list[ConversationMessage],
    *,
    user_id: str = "",
    session_id: str = "",
) -> str | None:
    """Summarize a list of messages into a compact context string (A3).

    Uses the shared background compaction helper.  Preserves identifiers
    (file paths, names, IDs) through summarization.
    The inline system prompt below is private summary helper text, not a
    user-facing agent prompt; never move it to module level.

    Returns the summary string, or None if summarization fails.
    """
    if not messages:
        return None

    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            transcript_parts.append("%s: %s" % (role, content[:800]))

    transcript = "\n".join(transcript_parts)
    if not transcript.strip():
        return None

    summarize_prompt = (
        "Summarize this conversation into a compact context paragraph. "
        "PRESERVE all specific identifiers: names, file paths, URLs, IDs, numbers, dates. "
        "Focus on: decisions made, information shared, tasks completed or in progress. "
        "Be concise but complete. Do not lose any actionable details.\n\n"
        "Conversation:\n%s"
    ) % transcript

    try:
        summary = (
            await run_background_openai_response(
                system_prompt="You create concise conversation summaries. Preserve all specific identifiers.",
                user_content=summarize_prompt,
                max_output_tokens=500,
                user_id=user_id,
                session_id=session_id,
                timeout_s=15.0,
            )
        ).strip()
        if summary:
            return summary
    except Exception as exc:
        logger.debug("Message summarization failed: %s", exc)

    return None


class ConversationStateManager:
    """Track long-running conversation history and multi-step plans.

    Enhanced with:
    - SQLite persistence of turns (A2)
    - Context compaction via LLM summarization (A3)
    - Pre-compaction memory extraction (A7)
    """

    def __init__(
        self,
        *,
        user_id: str,
        model: str = _DEFAULT_MODEL,
        default_context_tokens: int = _DEFAULT_CONTEXT_TOKENS,
        plan_step_timeout_seconds: float = _DEFAULT_PLAN_STEP_TIMEOUT_SECONDS,
        session_id: str | None = None,
        restore_turns: int = _DEFAULT_RESTORE_TURNS,
        allow_implicit_resume: bool = False,
    ) -> None:
        self.model = model
        self.default_context_tokens = max(1, int(default_context_tokens))
        self.plan_step_timeout_seconds = max(0.0, float(plan_step_timeout_seconds))
        self._user_id = user_id
        self.conversation_frames: list[Frame] = []
        self.conversation_history: list[ConversationMessage] = []
        self.context_window_tokens: int = 0
        self.plan_state: PlanState | None = None
        self._encoding_name = "cl100k_base"
        self._lock = threading.RLock()
        requested_session_id = coerce_session_id(session_id)
        if requested_session_id == user_id:
            logger.warning(
                "Ignoring user_id-as-session_id for conversation manager user=%s; creating explicit session identity",
                user_id,
            )
            requested_session_id = None
        # Track whether session_id was caller-provided. _restore_from_persistence
        # uses this to decide between strict (caller-scoped) and implicit-resume
        # (window-bounded) load behavior.
        self._session_explicit: str | None = requested_session_id
        self._session_id = requested_session_id or new_session_id()
        self._restore_turns = restore_turns
        self._allow_implicit_resume = bool(allow_implicit_resume)
        self._compaction_in_progress = False
        self._consecutive_compaction_failures = 0
        self._compaction_disabled = False
        self._last_compaction_meta: dict[str, Any] | None = None
        self._surfaced_memory_ids: set[int] = set()

        # B6: Post-compaction hooks — callables invoked after compaction
        # to let external components (e.g. AgentExecutor C2 cache) react.
        self._post_compaction_hooks: list[Callable[[], None]] = []

        # S8-014: Session-scoped content replacement records, mirroring
        # Claude's transcript-side content_replacement state. Keyed by
        # ``tool_use_id`` -> reference token for large tool outputs that
        # were compacted but whose stable identity must survive
        # fork/resume/subagent-resume. The tool-result budget
        # storage owns persistence; we own integration into branch/resume.
        self._content_replacement_state: dict[str, dict[str, Any]] = {}

        # S8-005 (deferred): snipped frame uuids for the model-facing
        # projection. Snipping is a UI-driven operation; storing the set
        # here lets ``get_message_chain`` filter snipped frames while the
        # durable transcript stays intact.
        self._snipped_frame_uuids: set[str] = set()

        # Restore recent turns from persistence (A2)
        self._restore_from_persistence()

    def _peek_recent_session(self, persistence: Any) -> str | None:
        """Return the most-recent session_id for this user_id ONLY if its
        last turn was within ``_IMPLICIT_RESUME_WINDOW_SECONDS``.

        Direct SQL read so we don't depend on persistence-layer API
        additions. Returns None when no recent session exists or the most
        recent one is past the window — caller starts a fresh thread.
        """
        try:
            row = persistence._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT session_id, MAX(timestamp) AS last_ts
                FROM conversation_log
                WHERE user_id = ?
                GROUP BY session_id
                ORDER BY last_ts DESC
                LIMIT 1
                """,
                (self._user_id,),
            ).fetchone()
        except Exception as exc:  # pragma: no cover — read-only peek
            logger.debug("Recent-session peek failed: %s", exc)
            return None
        if row is None:
            return None
        last_ts = float(row["last_ts"] or 0.0)
        if last_ts <= 0.0:
            return None
        if (time.time() - last_ts) > self._IMPLICIT_RESUME_WINDOW_SECONDS:
            return None
        return str(row["session_id"])

    # Cross-session implicit-resume window. If the caller does NOT pass
    # session_id, we treat the most-recent persisted session as a
    # legitimate continuation only when its last turn was less than this
    # many seconds ago. Beyond the window, a fresh thread is started so
    # unrelated old turns don't pollute the new conversation. 10 min
    # matches the typical "I had to grab my keys, I'm back" envelope and
    # lines up with industry voice-assistant decay norms; long-form
    # multi-turn agentic flows that legitimately span hours/days should
    # use explicit ``session_id`` (or the future explicit-resume verb).
    _IMPLICIT_RESUME_WINDOW_SECONDS = 600

    def _restore_from_persistence(self) -> None:
        """Load recent turns from SQLite on startup (A2).

        Boundary rules:
        - Caller passed an explicit ``session_id`` → load ONLY that
          session's turns. This is how the orchestrator/test harness
          forces a fresh thread (pass a brand-new UUID).
        - Caller did not pass ``session_id`` (we generated one) → only
          inherit the most-recent persisted session if its last turn was
          within ``_IMPLICIT_RESUME_WINDOW_SECONDS``. Beyond that window,
          start clean — a stranger-friendly default that prevents prior
          unrelated context from leaking into a new utterance.

        Long-term cross-session continuity flows through
        ``services/memory/store`` (curated facts), not raw transcripts.
        """
        try:
            persistence = get_conversation_persistence()
            session_to_load: str | None = self._session_explicit
            implicit_resume = False
            if session_to_load is None and self._implicit_resume_allowed():
                # Implicit-resume path: peek at the most-recent persisted
                # session and only adopt it if it's still inside the
                # window. Otherwise a new thread starts now.
                last_session = self._peek_recent_session(persistence)
                if last_session is not None:
                    session_to_load = last_session
                    # Adopt that session_id so subsequent writes append to
                    # the same thread rather than forking.
                    self._session_id = last_session
                    implicit_resume = True
            if session_to_load is None:
                return
            turns = persistence.load_recent_turns(
                self._user_id,
                limit=self._restore_turns,
                session_id=session_to_load,
            )
            if turns:
                turns = self._recover_loaded_frames(
                    turns,
                    persistence=persistence,
                    session_id=session_to_load,
                )
                for frame in turns:
                    self.conversation_frames.append(frame)
                    if frame.is_meta:
                        continue
                    msg = _frame_to_legacy_message(frame)
                    if msg is None:
                        continue
                    self.conversation_history.append(msg)
                    self.context_window_tokens += msg.get("token_count", 0)
                logger.info(
                    "Restored %d conversation frames from persistence (session=%s)",
                    len(turns),
                    self._session_id,
                )
                if implicit_resume:
                    leaf_uuid = turns[-1].uuid
                    identity = build_resume_session(
                        ResumeRequest(
                            session_id=self._session_id,
                            leaf_uuid=leaf_uuid,
                            channel="implicit_recent",
                        )
                    )
                    self.append_frame(
                        _session_event_frame(
                            identity=identity,
                            origin="session_resume",
                            text="Resumed recent conversation session %s." % self._session_id,
                            parent_uuid=leaf_uuid,
                            logical_parent_uuid=leaf_uuid,
                            extra={"resume_entrypoint": "implicit_recent"},
                        )
                    )
            # Auto-prune old entries
            persistence.prune_old(self._user_id, _PRUNE_DAYS)
        except Exception as exc:
            logger.debug("Failed to restore conversation from persistence: %s", exc)

    def _implicit_resume_allowed(self) -> bool:
        user_key = str(self._user_id or "").strip()
        return bool(self._allow_implicit_resume and user_key and user_key != "default")

    def _last_frame_uuid_locked(self) -> str | None:
        if not self.conversation_frames:
            return None
        return self.conversation_frames[-1].uuid

    def _infer_tool_source_from_chain(self, frame: Frame, prior_frames: Iterable[Frame]) -> Frame:
        if frame.source_tool_assistant_uuid:
            return frame
        result_ids = _frame_tool_result_ids(frame)
        if not result_ids:
            return frame
        for prior in reversed(list(prior_frames)):
            if not prior.uuid:
                continue
            if _frame_tool_use_ids(prior) & result_ids:
                return replace(frame, source_tool_assistant_uuid=prior.uuid)
        return frame

    def _prepare_frame_for_append_locked(self, frame: Frame) -> Frame:
        previous = self.conversation_frames[-1] if self.conversation_frames else None
        frame = self._infer_tool_source_from_chain(frame, self.conversation_frames)
        return assign_lineage(frame, previous, self._session_id)

    def _prune_pre_compact_frames_locked(self) -> None:
        """Prune only frames made obsolete by the latest compact boundary."""

        if len(self.conversation_frames) <= MAX_HISTORY:
            return
        boundary_index: int | None = None
        for index, frame in enumerate(self.conversation_frames):
            if (frame.origin or "") in {"compact_boundary", "microcompact_boundary"}:
                boundary_index = index
        if boundary_index is None or boundary_index <= 0:
            return
        del self.conversation_frames[:boundary_index]

    def _normalize_frame_chain(
        self,
        frames: Iterable[Frame],
        *,
        session_id: str | None = None,
        rewrite_timestamps: bool = False,
    ) -> list[Frame]:
        session_key = str(session_id or self._session_id or "").strip()
        timestamp_base = int(time.time() * 1000)
        ordered = [frame for frame in frames if isinstance(frame, Frame)]
        retained_uuids = {frame.uuid for frame in ordered if frame.uuid}
        normalized: list[Frame] = []
        previous: Frame | None = None

        for index, frame in enumerate(ordered):
            timestamp_ms = (
                timestamp_base + index if rewrite_timestamps else frame.timestamp_ms or timestamp_base + index
            )
            frame = _frame_with_defaults(frame, timestamp_ms, session_id=session_key)
            if rewrite_timestamps:
                frame = replace(frame, timestamp_ms=timestamp_ms)
            frame = self._infer_tool_source_from_chain(frame, normalized)

            original_parent_uuid = frame.parent_uuid
            if previous is None:
                if frame.parent_uuid and frame.parent_uuid not in retained_uuids:
                    frame = replace(
                        frame,
                        parent_uuid=None,
                        logical_parent_uuid=frame.logical_parent_uuid or frame.parent_uuid,
                    )
            elif not frame.parent_uuid or frame.parent_uuid not in retained_uuids:
                frame = replace(
                    frame,
                    parent_uuid=previous.uuid,
                    logical_parent_uuid=frame.logical_parent_uuid or original_parent_uuid or previous.uuid,
                )

            frame = assign_lineage(frame, previous, session_key)
            normalized.append(frame)
            previous = frame

        return normalized

    def _install_frame_chain(self, frames: Iterable[Frame], *, persist: bool = False) -> None:
        normalized = self._normalize_frame_chain(frames, rewrite_timestamps=persist)
        if len(normalized) > MAX_HISTORY:
            normalized = normalized[-MAX_HISTORY:]
            normalized = self._normalize_frame_chain(normalized, rewrite_timestamps=persist)

        with self._lock:
            self.conversation_frames = normalized
            self._rebuild_legacy_history_locked()

        if persist:
            persistence = get_conversation_persistence()
            persistence.replace_session_frames(self._user_id, self._session_id, normalized)

    def _persist_append_only_frames(self, frames: Iterable[Frame]) -> None:
        """Append compact control frames without rewriting prior transcript rows."""

        persistence = get_conversation_persistence()
        for frame in frames:
            persistence.write_turn(
                user_id=self._user_id,
                session_id=self._session_id,
                frame=frame,
            )

    def _recover_loaded_frames(
        self,
        frames: Iterable[Frame],
        *,
        persistence: ConversationPersistence,
        session_id: str,
        append_no_response_sentinel: bool = False,
        persist_recovery: bool = True,
    ) -> list[Frame]:
        """Run transcript recovery on a freshly-loaded chain.

        ``append_no_response_sentinel`` defaults to ``False`` because the
        common caller is auto-restore on manager construction, which should
        not stamp a synthetic assistant turn into the live window. Explicit
        resume / branch override to ``True`` so the durable transcript
        matches Claude's REPL contract (``deserializeMessagesWithInterruptDetection``).
        """

        original = [frame for frame in frames if isinstance(frame, Frame)]
        recovered = recover_transcript_on_resume(
            original,
            session_id=session_id,
            append_no_response_sentinel=append_no_response_sentinel,
        )
        candidate = recovered.frames or original
        normalized = self._normalize_frame_chain(candidate, session_id=session_id)
        changed = (
            bool(recovered.stripped_orphan_tool_results)
            or bool(recovered.synthetic_tool_results)
            or bool(recovered.appended_continue)
            or bool(recovered.appended_no_response_sentinel)
            or _frame_chain_fingerprint(original) != _frame_chain_fingerprint(normalized)
        )
        if changed and persist_recovery:
            persisted = self._normalize_frame_chain(normalized, session_id=session_id, rewrite_timestamps=True)
            persistence.replace_session_frames(self._user_id, session_id, persisted)
            return persisted
        return normalized

    def _rebuild_legacy_history_locked(self) -> None:
        self.conversation_history.clear()
        self.context_window_tokens = 0
        for frame in self.conversation_frames:
            if frame.is_meta:
                continue
            message = _frame_to_legacy_message(frame)
            if message is None:
                continue
            self.conversation_history.append(message)
            self.context_window_tokens += message.get("token_count", 0)

    def add_message(
        self,
        role: str | Frame,
        content: str | None = None,
        metadata: dict[str, object] | None = None,
        *,
        frame_kind: FrameKind | str | None = None,
        origin: str | None = None,
        is_meta: bool = False,
        is_compact_summary: bool = False,
        task_id: str | None = None,
        tool_use_id: str | None = None,
        permission_mode: str | None = None,
    ) -> Frame | None:
        """Append a typed frame to the durable conversation history.

        Also persists to SQLite (A2) for cross-session continuity.
        """
        timestamp_ms = int(time.time() * 1000)
        if isinstance(role, Frame):
            frame = _frame_with_defaults(role, timestamp_ms, session_id=self._session_id)
        else:
            normalized_role = (role or "user").strip() or "user"
            normalized_content = (content or "").strip()
            if _is_internal_history_artifact(normalized_role, normalized_content):
                logger.warning("Skipped storing internal probe artifact in conversation history")
                return None
            scrubbed_metadata = _scrub_sensitive_metadata(metadata) if isinstance(metadata, dict) else None
            metadata_copy = copy.deepcopy(scrubbed_metadata) if isinstance(scrubbed_metadata, dict) else None
            if isinstance(metadata_copy, dict):
                metadata_copy = _scrub_internal_prompt_artifacts(metadata_copy)
                metadata_copy = metadata_copy if isinstance(metadata_copy, dict) else None
            frame = _make_text_frame(
                role=normalized_role,
                content=normalized_content,
                metadata=metadata_copy,
                frame_kind=frame_kind,
                origin=origin,
                is_meta=is_meta,
                is_compact_summary=is_compact_summary,
                task_id=task_id,
                tool_use_id=tool_use_id,
                permission_mode=permission_mode,
                timestamp_ms=timestamp_ms,
                session_id=self._session_id,
            )
            frame = _frame_with_defaults(frame, timestamp_ms, session_id=self._session_id)

        frame_content = _frame_text(frame).strip()
        if _is_internal_history_artifact(frame.role.value, frame_content):
            logger.warning("Skipped storing internal probe artifact in conversation history")
            return None

        with self._lock:
            frame = self._prepare_frame_for_append_locked(frame)
            token_count = self._count_tokens(frame_content) + _MESSAGE_TOKEN_OVERHEAD
            message = _frame_to_legacy_message(frame)
            if message is not None:
                message["token_count"] = token_count

            try:
                persistence = get_conversation_persistence()
                persistence.write_turn(
                    user_id=self._user_id,
                    session_id=self._session_id,
                    frame=frame,
                )
            except ConversationEncryptionUnavailableError:
                logger.error("Conversation turn persistence failed because encryption is unavailable")
                raise
            except Exception as exc:
                logger.error("Conversation turn persistence failed: %s", exc)
                raise RuntimeError(
                    "Conversation turn persistence failed; refusing to append an unpersisted conversation frame."
                ) from exc

            self.conversation_frames.append(frame)
            self._prune_pre_compact_frames_locked()
            if message is not None and not frame.is_meta:
                self.conversation_history.append(message)
                self.context_window_tokens += token_count
            # FIFO cap: prune oldest non-system messages to prevent unbounded growth
            while len(self.conversation_history) > MAX_HISTORY:
                for idx, msg in enumerate(self.conversation_history):
                    role_name = msg.get("role")
                    if role_name not in {"system"}:
                        removed = self.conversation_history.pop(idx)
                        self.context_window_tokens -= removed.get("token_count", 0)
                        break
                else:
                    # All remaining entries are system messages -- remove the oldest
                    removed = self.conversation_history.pop(0)
                    self.context_window_tokens -= removed.get("token_count", 0)

        return frame

    def append_frame(
        self,
        frame: Frame,
        *,
        parent_uuid: str | None = None,
        logical_parent_uuid: str | None = None,
    ) -> Frame:
        """Append a typed frame and return the stored frame instance."""

        with self._lock:
            inferred_parent_uuid = parent_uuid if parent_uuid is not None else self._last_frame_uuid_locked()
        stored_frame = _frame_with_defaults(
            frame,
            int(time.time() * 1000),
            session_id=self._session_id,
            parent_uuid=inferred_parent_uuid,
            logical_parent_uuid=logical_parent_uuid,
        )
        return self.add_message(stored_frame) or stored_frame

    def add_voice_event(self, event_type: str, metadata: object | None = None) -> Frame:
        """Append a voice runtime event as a canonical meta frame."""

        from core.voice_canonical_carrier import VoiceEventFrame

        frame = VoiceEventFrame(event_type, metadata, self._session_id)
        return self.add_message(frame) or frame

    def record_exchange(
        self,
        user_text: str,
        assistant_text: str,
        *,
        intent: str | None = None,
        params: dict[str, object] | None = None,
        user_origin: str | None = None,
        assistant_origin: str | None = None,
        user_is_meta: bool = False,
        assistant_is_meta: bool = False,
        task_id: str | None = None,
    ) -> None:
        """Append a user/assistant exchange as two typed Frame records."""

        turn_id = str(uuid.uuid4())
        user_metadata: dict[str, object] = {
            "turn_id": turn_id,
            "turn_role": "request",
        }
        assistant_metadata: dict[str, object] = {
            "turn_id": turn_id,
            "turn_role": "response",
        }
        if intent:
            user_metadata["intent"] = intent
            assistant_metadata["intent"] = intent
        if isinstance(params, dict) and params:
            safe_params = _scrub_internal_prompt_artifacts(copy.deepcopy(params))
            safe_params = safe_params if isinstance(safe_params, dict) else {}
        else:
            safe_params = {}
        if safe_params:
            user_metadata["params"] = safe_params
            assistant_metadata["params"] = safe_params

        assistant_kind: FrameKind | None = None if assistant_is_meta else FrameKind.ASSISTANT_TEXT

        self.add_message(
            "user",
            user_text,
            metadata=user_metadata,
            frame_kind=None if user_is_meta else FrameKind.USER_INPUT,
            origin=user_origin,
            is_meta=user_is_meta,
            task_id=task_id,
        )
        self.add_message(
            "assistant",
            assistant_text,
            metadata=assistant_metadata,
            frame_kind=assistant_kind,
            origin=assistant_origin,
            is_meta=assistant_is_meta,
            task_id=task_id,
        )

    def get_context(self, max_tokens: int = _DEFAULT_CONTEXT_TOKENS) -> list[dict[str, str]]:
        """Return the newest messages that fit within the token budget."""
        with self._lock:
            truncated = self._truncate_context(self.conversation_history, max_tokens=max_tokens)
        context: list[dict[str, str]] = []
        for msg in truncated:
            role = str(msg["role"])
            content = str(msg["content"])
            if _is_internal_history_artifact(role, content):
                logger.warning("Dropped internal tool artifact from conversation context")
                continue
            context.append({"role": role, "content": content})
        return context

    def get_recent_turns(
        self,
        limit: int = 5,
        *,
        behavioral_only: bool = True,
    ) -> list[Frame]:
        """Return recent typed Frames, excluding meta turns by default."""

        max_frames = max(1, min(int(limit or 1), 40))
        with self._lock:
            frames = list(self.conversation_frames)

        recent_frames: list[Frame] = []
        for frame in frames:
            content = _frame_text(frame)
            if _is_internal_history_artifact(frame.role.value, content):
                logger.warning("Dropped internal tool artifact from recent-turn frames")
                continue
            if behavioral_only and frame.is_meta:
                continue
            recent_frames.append(frame)
        return recent_frames[-max_frames:]

    def get_message_chain(
        self,
        limit: int = MAX_HISTORY,
        *,
        behavioral_only: bool = False,
    ) -> list[Frame]:
        """Return the ordered canonical Frame chain for prompt assembly."""

        max_frames = max(1, int(limit or 1))
        with self._lock:
            frames = list(self.conversation_frames)
            chain = self._project_prompt_frame_chain_locked(frames, behavioral_only=behavioral_only)
        return chain[-max_frames:]

    def _project_prompt_frame_chain_locked(
        self,
        frames: Iterable[Frame],
        *,
        behavioral_only: bool = False,
    ) -> list[Frame]:
        snipped = set(self._snipped_frame_uuids)
        chain: list[Frame] = []
        for frame in frames:
            content = _frame_text(frame)
            if _is_internal_history_artifact(frame.role.value, content):
                logger.warning("Dropped internal tool artifact from canonical frame chain")
                continue
            if behavioral_only and frame.is_meta:
                continue
            if frame.uuid and frame.uuid in snipped:
                continue
            chain.append(frame)

        boundary_index: int | None = None
        for index, frame in enumerate(chain):
            if (frame.origin or "") in {"compact_boundary", "microcompact_boundary"}:
                boundary_index = index
        if boundary_index is not None:
            chain = chain[boundary_index:]

        if not snipped:
            return chain

        projected: list[Frame] = []
        for frame in chain:
            if frame.parent_uuid and frame.parent_uuid in snipped:
                previous_uuid = projected[-1].uuid if projected else None
                frame = replace(
                    frame,
                    parent_uuid=previous_uuid,
                    logical_parent_uuid=frame.logical_parent_uuid or frame.parent_uuid,
                )
            projected.append(frame)
        return projected

    def mark_snipped_frames(self, frame_uuids: Iterable[str]) -> dict[str, int]:
        """Exclude frame ids from model-facing history while retaining storage."""

        cleaned = {str(frame_uuid or "").strip() for frame_uuid in frame_uuids}
        cleaned.discard("")
        if not cleaned:
            return {"snipped_frames": 0, "tokens_freed": 0}

        with self._lock:
            self._snipped_frame_uuids.update(cleaned)
            tokens_freed = sum(
                self._count_tokens(_frame_text(frame))
                for frame in self.conversation_frames
                if frame.uuid and frame.uuid in cleaned
            )
        return {"snipped_frames": len(cleaned), "tokens_freed": tokens_freed}

    def clear_snipped_frames(self, frame_uuids: Iterable[str] | None = None) -> None:
        """Restore model-facing visibility for previously snipped frames."""

        with self._lock:
            if frame_uuids is None:
                self._snipped_frame_uuids.clear()
                return
            cleaned = {str(frame_uuid or "").strip() for frame_uuid in frame_uuids}
            self._snipped_frame_uuids.difference_update(cleaned)

    def get_canonical_chain(
        self,
        limit: int = MAX_HISTORY,
        *,
        behavioral_only: bool = False,
    ):
        """Return recent frames wrapped in the schema-locked chain object."""

        from services.conversation.canonical_chain import CanonicalFrameChain

        return CanonicalFrameChain(self.get_message_chain(limit=limit, behavioral_only=behavioral_only))

    def branch_session(
        self,
        *,
        source_frame_uuid: str | None = None,
        new_session_id: str | None = None,
        reason: str = "save_task",
        channel: str | None = None,
    ) -> SessionIdentity:
        """Fork the persisted transcript into a new session.

        S8-002 parity: read the **persisted** transcript from SQLite (not
        just the in-memory tail), so frames already evicted from the
        model-facing window survive the fork. Mirrors Claude
        ``src/commands/branch/branch.ts:77-160`` which reads the transcript
        file, filters to the main-conversation entries, copies content
        replacement records, and writes a distinct forked transcript file.

        S8-014: also copies the manager's content-replacement state onto
        the forked transcript metadata so the fork can rehydrate stable
        tool-result references that were inline-replaced in the source.

        S8-010: dispatches SessionEnd on the source and SessionStart on the
        fork around the switch.

        S8-004: branch does NOT cap the persisted transcript to
        ``MAX_HISTORY``. The full chain is written to persistence so a
        future resume can reach back beyond the model-facing window.
        """

        with self._lock:
            in_memory_frames = list(self.conversation_frames)
            source_session_id = self._session_id

        # S8-002: persisted transcript is the source of truth. Fall back
        # to in-memory frames only if persistence is empty.
        persistence = get_conversation_persistence()
        persisted_frames: list[Frame] = []
        try:
            persisted_frames = persistence.load_session_frames(self._user_id, source_session_id)
        except Exception as exc:
            logger.debug(
                "Branch: persisted-transcript load failed for session=%s: %s",
                source_session_id,
                exc,
            )
            persisted_frames = []

        source_frames = persisted_frames or in_memory_frames
        if not source_frames:
            raise ValueError("No conversation frames are available to branch")

        # Reconcile: if in-memory tail has frames not yet flushed to
        # persistence, append them so the fork captures both.
        if persisted_frames and in_memory_frames:
            persisted_uuids = {frame.uuid for frame in persisted_frames if frame.uuid}
            for frame in in_memory_frames:
                if frame.uuid and frame.uuid not in persisted_uuids:
                    source_frames.append(frame)
                    persisted_uuids.add(frame.uuid)

        source_uuid = str(source_frame_uuid or source_frames[-1].uuid or "").strip()
        if not source_uuid:
            source_uuid = str(uuid.uuid4())
            source_frames[-1] = replace(source_frames[-1], uuid=source_uuid)

        selected_frames: list[Frame] = []
        found_source = False
        for frame in source_frames:
            frame_uuid = frame.uuid or str(uuid.uuid4())
            normalized = replace(frame, uuid=frame_uuid)
            selected_frames.append(normalized)
            if frame_uuid == source_uuid:
                found_source = True
                break
        if not found_source:
            raise ValueError("Source frame %s was not found in session %s" % (source_uuid, source_session_id))

        identity = build_session_branch(
            SessionBranch(
                source_session_id=source_session_id,
                source_frame_uuid=source_uuid,
                new_session_id=new_session_id,
                reason=reason,
                channel=channel,
            )
        )

        if persistence.session_exists(self._user_id, identity.session_id):
            raise ValueError("Session %s already exists" % identity.session_id)

        # S8-014: copy the content-replacement state into the fork. We
        # serialize through the frame extras so a later resume can
        # reconstruct the manager-level state.
        replacement_snapshot = copy.deepcopy(self._content_replacement_state)

        branched_frames: list[Frame] = []
        parent_uuid: str | None = None
        for frame in selected_frames:
            source_message_uuid = frame.uuid or str(uuid.uuid4())
            frame_uuid = str(uuid.uuid4())
            frame_extra = copy.deepcopy(frame.extra) if isinstance(frame.extra, dict) else {}
            frame_extra["forked_from"] = {
                "session_id": source_session_id,
                "message_uuid": source_message_uuid,
            }
            frame_extra["branch_reason"] = reason
            branched_frames.append(
                replace(
                    frame,
                    uuid=frame_uuid,
                    parent_uuid=parent_uuid,
                    logical_parent_uuid=frame.logical_parent_uuid or frame.parent_uuid,
                    session_id=identity.session_id,
                    extra=frame_extra,
                )
            )
            parent_uuid = frame_uuid

        event_extra: dict[str, object] = {
            "reason": reason,
            "source_session_id": source_session_id,
            "source_frame_uuid": source_uuid,
        }
        if replacement_snapshot:
            event_extra["content_replacement_state"] = replacement_snapshot
        event = _session_event_frame(
            identity=identity,
            origin="session_branch",
            text="Saved task branch %s from session %s." % (identity.session_id, source_session_id),
            parent_uuid=parent_uuid,
            logical_parent_uuid=source_uuid,
            extra=event_extra,
        )
        branched_frames.append(event)

        # S8-010: SessionEnd on the source before switching.
        self._dispatch_session_hook(
            event_name="SessionEnd",
            reason="branch",
            target_session_id=source_session_id,
            channel=channel,
            leaf_uuid=source_uuid,
        )

        with self._lock:
            self._session_id = identity.session_id
            self._session_explicit = identity.session_id
            # S8-004: keep the full persisted chain in memory if it fits in
            # the live window; the model-facing projection cap is handled
            # at ``get_message_chain(limit=...)`` time, not by truncating
            # the durable canon.
            self.conversation_frames = list(branched_frames)
            if len(self.conversation_frames) > MAX_HISTORY:
                # Live frame chain still has a soft cap for memory hygiene,
                # but the durable persistence below keeps the full set.
                self.conversation_frames = self.conversation_frames[-MAX_HISTORY:]
            self._rebuild_legacy_history_locked()

        # S8-004: write the FULL forked chain to persistence, not just the
        # capped in-memory tail.
        for frame in branched_frames:
            persistence.write_turn(
                user_id=self._user_id,
                session_id=identity.session_id,
                frame=frame,
            )

        # S8-010: SessionStart on the new session.
        self._dispatch_session_hook(
            event_name="SessionStart",
            reason="branch",
            target_session_id=identity.session_id,
            channel=channel,
            leaf_uuid=event.uuid,
        )

        return identity

    def resume_session(
        self,
        session_id: str | None = None,
        *,
        leaf_uuid: str | None = None,
        channel: str | None = None,
        source: str = "session_id",
    ) -> SessionIdentity:
        """Reopen a known session and record an in-band resume frame.

        S8-001 parity with Claude `loadConversationForResume`
        (``src/utils/conversationRecovery.ts:456``):

        - ``source='session_id'`` (default): explicit ``session_id`` provided.
        - ``source='latest'``: most recent persisted session for this user.
        - ``source='leaf_uuid'``: resolve the session that owns ``leaf_uuid``.

        S8-004: hydrates the FULL persisted transcript into manager memory
        (subject to the soft live-frame cap). Resume does not rewrite
        durable persistence with the truncated tail.

        S8-010: dispatches SessionEnd on the current session before the
        switch, then SessionStart on the resumed session — mirroring
        ``REPL.tsx:1774-1782``.

        S8-014: reconstructs ``content_replacement_state`` from the
        ``session_branch`` event extras when one is present in the
        persisted chain.
        """

        target_session_id = self._resolve_resume_session_id(
            session_id=session_id,
            leaf_uuid=leaf_uuid,
            source=source,
        )

        persistence = get_conversation_persistence()
        # S8-001/S8-004: load the FULL persisted chain, not just the tail.
        frames = persistence.load_session_frames(self._user_id, target_session_id)
        with self._lock:
            if not frames and target_session_id == self._session_id:
                frames = list(self.conversation_frames)
        if not frames:
            raise ValueError("Session %s was not found" % target_session_id)

        # If caller asked for a specific leaf, walk back to that frame so
        # the resumed window terminates there (Claude's resume picker can
        # rewind into the middle of a transcript).
        leaf_truncated_for_recovery = False
        if leaf_uuid:
            leaf_str = str(leaf_uuid).strip()
            if leaf_str:
                truncated: list[Frame] = []
                for frame in frames:
                    truncated.append(frame)
                    if frame.uuid == leaf_str:
                        break
                if truncated and truncated[-1].uuid == leaf_str:
                    frames = truncated
                    leaf_truncated_for_recovery = True

        # Explicit resume always honors Claude's resume contract: append the
        # NO_RESPONSE_REQUESTED sentinel when the user-last transcript has
        # no pending tool call, so the resumed loop does not auto-call the
        # model with stale context (S8-008). Leaf resumes recover after
        # truncation so the sentinel is not appended past the requested leaf,
        # but still avoid rewriting durable persistence to the truncated view
        # (S8-004).
        frames = self._recover_loaded_frames(
            frames,
            persistence=persistence,
            session_id=target_session_id,
            append_no_response_sentinel=True,
            persist_recovery=not leaf_truncated_for_recovery,
        )

        resume_leaf = str(leaf_uuid or frames[-1].uuid or "").strip() or None
        identity = build_resume_session(
            ResumeRequest(
                session_id=target_session_id,
                leaf_uuid=resume_leaf,
                channel=channel,
            )
        )

        previous_session_id = self._session_id

        # S8-010: SessionEnd on the outgoing session before switching.
        if previous_session_id and previous_session_id != target_session_id:
            self._dispatch_session_hook(
                event_name="SessionEnd",
                reason="resume",
                target_session_id=previous_session_id,
                channel=channel,
                leaf_uuid=None,
            )

        with self._lock:
            self._session_id = identity.session_id
            self._session_explicit = identity.session_id
            # S8-004: live chain holds at most MAX_HISTORY frames; durable
            # persistence stays full. Subsequent compactions update only
            # the live chain (and post-compact boundaries) — they no
            # longer collapse the persisted transcript.
            full_frames = list(frames)
            self.conversation_frames = full_frames[-MAX_HISTORY:] if len(full_frames) > MAX_HISTORY else full_frames
            self._rebuild_legacy_history_locked()
            # S8-014: rebuild the content-replacement state from any
            # ``session_branch`` event in the chain.
            self._content_replacement_state = self._extract_content_replacement_state(full_frames)

        self.append_frame(
            _session_event_frame(
                identity=identity,
                origin="session_resume",
                text="Resumed saved task session %s." % identity.session_id,
                parent_uuid=resume_leaf,
                logical_parent_uuid=resume_leaf,
                extra={"resume_entrypoint": source},
            ),
            parent_uuid=resume_leaf,
            logical_parent_uuid=resume_leaf,
        )

        # S8-010: SessionStart on the resumed session.
        self._dispatch_session_hook(
            event_name="SessionStart",
            reason="resume",
            target_session_id=identity.session_id,
            channel=channel,
            leaf_uuid=resume_leaf,
        )

        return identity

    def _resolve_resume_session_id(
        self,
        *,
        session_id: str | None,
        leaf_uuid: str | None,
        source: str,
    ) -> str:
        """Resolve a resume request to a concrete target session id.

        Mirrors Claude's resume source selection in
        ``loadConversationForResume`` (``src/utils/conversationRecovery.ts``).
        """

        explicit = coerce_session_id(session_id)
        normalized_source = (source or "session_id").strip().lower()

        if normalized_source == "latest":
            persistence = get_conversation_persistence()
            target = persistence.latest_session_id(self._user_id)
            if not target:
                raise ValueError("No prior session was found to resume")
            return target

        if normalized_source == "leaf_uuid":
            leaf = str(leaf_uuid or "").strip()
            if not leaf:
                raise ValueError("leaf_uuid is required for resume source=leaf_uuid")
            persistence = get_conversation_persistence()
            target = self._find_session_for_leaf(persistence, leaf)
            if not target:
                raise ValueError("No session was found for leaf %s" % leaf)
            return target

        if not explicit:
            raise ValueError("session_id is required")
        return explicit

    def _find_session_for_leaf(self, persistence: Any, leaf_uuid: str) -> str | None:
        """Return the session id that owns a specific frame uuid, if any."""

        try:
            row = persistence._conn.execute(  # type: ignore[attr-defined]
                """
                SELECT session_id
                FROM conversation_log
                WHERE user_id = ? AND frame_uuid = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """,
                (self._user_id, leaf_uuid),
            ).fetchone()
        except Exception as exc:
            logger.debug("Leaf lookup failed: %s", exc)
            return None
        if row is None:
            return None
        return str(row["session_id"])

    @staticmethod
    def _extract_content_replacement_state(
        frames: Sequence[Frame],
    ) -> dict[str, dict[str, Any]]:
        """Pull the content_replacement_state snapshot from session events.

        S8-014: a fork stores its full ``content_replacement_state`` in
        the ``session_branch`` event's ``extra``. The most recent such
        event in the chain is the active state.
        """

        for frame in reversed(list(frames)):
            if frame.origin != "session_branch":
                continue
            extra = frame.extra or {}
            snapshot = extra.get("content_replacement_state")
            if isinstance(snapshot, dict):
                return {str(key): dict(value) for key, value in snapshot.items() if isinstance(value, dict)}
        return {}

    def latest_session_id(self, *, exclude_current: bool = True) -> str | None:
        """Return the latest persisted session id for this manager's user."""

        exclude = self._session_id if exclude_current else None
        try:
            return get_conversation_persistence().latest_session_id(
                self._user_id,
                exclude_session_id=exclude,
            )
        except Exception as exc:
            logger.debug("Latest session lookup failed: %s", exc)
            return None

    def _conversation_frame_tokens(self, frames: Iterable[Frame]) -> int:
        return sum(self._count_tokens(_frame_text(frame)) + _MESSAGE_TOKEN_OVERHEAD for frame in frames)

    def _legacy_messages_from_frames(self, frames: Iterable[Frame]) -> list[ConversationMessage]:
        messages: list[ConversationMessage] = []
        for frame in frames:
            message = _frame_to_legacy_message(frame)
            if message is not None:
                messages.append(message)
        return messages

    def _messages_for_compaction_summary(self, frames: Iterable[Frame]) -> list[ConversationMessage]:
        messages: list[ConversationMessage] = []
        for frame in frames:
            message = _frame_to_legacy_message(frame)
            if message is not None:
                messages.append(message)
                continue
            if not (frame.is_compact_summary or frame.is_meta):
                continue
            content = _frame_text(frame).strip()
            if not content or _is_internal_history_artifact(frame.role.value, content):
                continue
            messages.append(
                {
                    "role": "system" if frame.role is FrameRole.SYSTEM else "user",
                    "content": content,
                    "timestamp": ((frame.timestamp_ms / 1000.0) if frame.timestamp_ms else time.time()),
                    "token_count": len(content.split()) + _MESSAGE_TOKEN_OVERHEAD,
                    "metadata": copy.deepcopy(frame.extra) if frame.extra else {},
                }
            )
        return messages

    def _coerce_post_compaction_hook_frames(self, output: object, *, hook_index: int) -> list[Frame]:
        if output is None:
            return []
        if isinstance(output, Frame):
            return [
                replace(
                    output,
                    is_meta=True,
                    origin=output.origin or "hook",
                    session_id=output.session_id or self._session_id,
                )
            ]
        if isinstance(output, str):
            text = output.strip()
            if not text:
                return []
            return [
                Frame(
                    kind=FrameKind.SYSTEM_REMINDER,
                    role=FrameRole.META_USER,
                    blocks=(SystemReminderBlock(text=text, source_tag="post-compact-hook"),),
                    is_meta=True,
                    origin="hook",
                    session_id=self._session_id,
                    extra={"hook_event": "PostCompact", "hook_index": hook_index},
                )
            ]
        if isinstance(output, Iterable):
            frames: list[Frame] = []
            for item in output:
                frames.extend(self._coerce_post_compaction_hook_frames(item, hook_index=hook_index))
            return frames
        return []

    def _run_post_compaction_hooks_as_frames(self) -> list[Frame]:
        hook_frames: list[Frame] = []
        for index, hook in enumerate(list(self._post_compaction_hooks)):
            try:
                output = hook()
            except Exception:
                logger.debug("Post-compaction hook failed", exc_info=True)
                continue
            hook_frames.extend(self._coerce_post_compaction_hook_frames(output, hook_index=index))
        return hook_frames

    def _effective_context_window_size(self, token_budget: int | None = None) -> int:
        """Return the model-facing context window minus the summary output reserve.

        Mirrors Claude `autoCompact.ts:getEffectiveContextWindowSize`. The
        reserved tokens for the compact-summary output stay capped at
        ``_MAX_OUTPUT_TOKENS_FOR_SUMMARY`` so a small-budget model still
        leaves a usable working window.
        """

        budget = max(1, int(token_budget or self.default_context_tokens))
        # Reserve no more than the summary output cap, and never more than
        # half the budget (avoids producing a negative working window on
        # tiny test budgets).
        reserved = min(_MAX_OUTPUT_TOKENS_FOR_SUMMARY, max(1, budget // 2))
        return max(1, budget - reserved)

    def _autocompact_threshold(self, token_budget: int | None = None) -> int:
        """Return the autocompact firing threshold for the given model budget.

        Mirrors Claude `autoCompact.ts:getAutoCompactThreshold`: effective
        window minus the autocompact buffer. When the effective window is
        smaller than the buffer (e.g. test budgets), fall back to the legacy
        Viola ratio so the trigger fires for very small chains as well.
        """

        effective = self._effective_context_window_size(token_budget)
        threshold = effective - _AUTOCOMPACT_BUFFER_TOKENS
        if threshold <= 0:
            budget = max(1, int(token_budget or self.default_context_tokens))
            return int(budget * _COMPACTION_TRIGGER_RATIO)
        return threshold

    def autocompact_circuit_open(self) -> bool:
        """Return True when the autocompact circuit breaker has tripped.

        Mirrors Claude `autoCompact.ts:autoCompactIfNeeded` circuit breaker:
        after ``MAX_CONSECUTIVE_COMPACTION_FAILURES`` consecutive failed
        compactions, do not attempt another one this session. Reset by a
        successful compaction.
        """

        return self._consecutive_compaction_failures >= MAX_CONSECUTIVE_COMPACTION_FAILURES

    @staticmethod
    def _compaction_source_guarded(query_source: str | None) -> bool:
        return str(query_source or "").strip().lower() in {"session_memory", "compact"}

    def needs_compaction(self, token_budget: int | None = None, *, query_source: str | None = None) -> bool:
        """Return whether the typed frame chain exceeds the compaction trigger.

        Honors Claude's autocompact contract:
        - Circuit-broken sessions never trigger again until reset.
        - When ``compaction_disabled`` is set, autocompact is suppressed.
        - When the chain has already been compacted (post-boundary live
          window is below the threshold), no further trigger.
        - Effective threshold = ``effective_context_window - autocompact_buffer``
          rather than a flat ``budget * 0.75``. The legacy ratio still applies
          to tiny test budgets (see ``_autocompact_threshold``).
        """

        if (
            self._compaction_disabled
            or self.autocompact_circuit_open()
            or self._compaction_source_guarded(query_source)
        ):
            return False

        with self._lock:
            prompt_frames = self._project_prompt_frame_chain_locked(self.conversation_frames)
            token_count = self._conversation_frame_tokens(prompt_frames)
        threshold = self._autocompact_threshold(token_budget)
        return token_count > threshold

    def _dispatch_pre_compact_hook(
        self,
        *,
        trigger: str,
        reason: str,
        custom_instructions: str | None,
    ) -> str | None:
        """Dispatch the PreCompact lifecycle hook; return merged custom instructions.

        Lazy-imports the dispatcher so the conversation package does not
        depend on the agent-loop wiring at import time. Failures here are
        non-fatal — Claude treats hook failures the same way (logged then
        ignored).
        """

        try:
            from intent.hooks.dispatcher import dispatch_lifecycle, has_registered_hooks
        except Exception:
            return custom_instructions

        try:
            if not has_registered_hooks("PreCompact"):
                return custom_instructions
            hook_result = dispatch_lifecycle(
                "PreCompact",
                {
                    "reason": reason,
                    "trigger": trigger,
                    "user_id": self._user_id,
                    "session_id": self._session_id,
                    "custom_instructions": custom_instructions,
                },
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.debug("PreCompact hook dispatch failed: %s", exc)
            return custom_instructions

        merged = custom_instructions
        try:
            payload = getattr(hook_result, "data", None) or {}
            extra = payload.get("newCustomInstructions") or payload.get("new_custom_instructions")
            if isinstance(extra, str) and extra.strip():
                merged = "%s\n\n%s" % (custom_instructions, extra.strip()) if custom_instructions else extra.strip()
        except Exception:
            pass
        return merged

    def _dispatch_post_compact_hook(
        self,
        *,
        trigger: str,
        reason: str,
        pre_tokens: int,
        post_tokens: int,
        dropped: int,
        kept: int,
    ) -> None:
        """Dispatch the PostCompact lifecycle hook (advisory)."""

        try:
            from intent.hooks.dispatcher import dispatch_lifecycle, has_registered_hooks
        except Exception:
            return

        try:
            if not has_registered_hooks("PostCompact"):
                return
            dispatch_lifecycle(
                "PostCompact",
                {
                    "reason": reason,
                    "trigger": trigger,
                    "user_id": self._user_id,
                    "session_id": self._session_id,
                    "pre_tokens": pre_tokens,
                    "post_tokens": post_tokens,
                    "dropped": dropped,
                    "kept": kept,
                },
                session_id=self._session_id,
            )
        except Exception as exc:
            logger.debug("PostCompact hook dispatch failed: %s", exc)

    def _dispatch_session_hook(
        self,
        *,
        event_name: str,
        reason: str,
        target_session_id: str,
        channel: str | None,
        leaf_uuid: str | None,
    ) -> None:
        """Dispatch SessionStart or SessionEnd around branch/resume boundaries.

        Mirrors Claude `src/screens/REPL.tsx:1774-1782` and
        `src/utils/conversationRecovery.ts:564`.
        """

        try:
            from intent.hooks.dispatcher import dispatch_lifecycle, has_registered_hooks
        except Exception:
            return
        try:
            if not has_registered_hooks(event_name):
                return
            dispatch_lifecycle(
                event_name,
                {
                    "reason": reason,
                    "user_id": self._user_id,
                    "session_id": target_session_id,
                    "previous_session_id": self._session_id,
                    "channel": channel,
                    "leaf_uuid": leaf_uuid,
                    "source": "state_manager",
                },
                session_id=target_session_id,
            )
        except Exception as exc:
            logger.debug("%s hook dispatch failed: %s", event_name, exc)

    async def compact(
        self,
        token_budget: int | None = None,
        *,
        trigger: str = "auto",
        reason: str = "session_memory_compact",
        query_source: str | None = None,
        custom_instructions: str | None = None,
    ) -> bool:
        """Compact the canonical typed frame chain.

        S8-010: dispatches ``PreCompact`` and ``PostCompact`` lifecycle hooks
        through ``intent.hooks.dispatcher`` so external observers (audit
        sinks, CC2 cache resets, AgentExecutor invalidators) see the same
        signal Claude emits via ``executePreCompactHooks`` /
        ``processSessionStartHooks('compact', ...)``.

        S8-007: enforces the autocompact circuit breaker so a session that
        cannot compact (e.g. summarizer permanently 4xx) stops re-trying.
        """

        if self._compaction_in_progress:
            return False

        if self._compaction_disabled:
            return False

        if trigger == "auto" and self._compaction_source_guarded(query_source):
            logger.debug("Skipping autocompact for guarded query_source=%s", query_source)
            return False

        if self.autocompact_circuit_open():
            logger.debug(
                "Skipping compact for session=%s: autocompact circuit-breaker open (%d consecutive failures)",
                self._session_id,
                self._consecutive_compaction_failures,
            )
            return False

        budget = token_budget or self.default_context_tokens
        self._compaction_in_progress = True
        # Track whether we even started a compaction attempt that should
        # bump the failure counter on early exit (e.g. summary unavailable).
        _attempt_started = False
        try:
            with self._lock:
                frames = list(self.conversation_frames)

            if len(frames) < 6:
                return False

            # S8-010: PreCompact hook dispatch. Surface failures must not
            # block the compaction itself — Claude treats hook output as
            # advisory ``customInstructions`` merging. We only honor the
            # ``newCustomInstructions`` payload when present; everything
            # else is queued as a passive listener.
            custom_instructions = self._dispatch_pre_compact_hook(
                trigger=trigger,
                reason=reason,
                custom_instructions=custom_instructions,
            )

            _attempt_started = True
            request = CompactionRequest(
                chain=frames,
                token_budget=budget,
                reason=reason,
                provider=self.model,
                token_counter=self._count_tokens,
            )
            initial_result = compact_frames(request)
            if not initial_result.compacted:
                # Nothing to drop — not a failure, just no-op. Reset
                # _attempt_started so we don't bump the counter.
                _attempt_started = False
                return False

            dropped_messages = self._legacy_messages_from_frames(initial_result.dropped_frames)

            try:
                facts = await _extract_facts_for_memory(
                    dropped_messages,
                    user_id=self._user_id,
                    session_id=self._session_id,
                )
                if facts:
                    from services.memory.store import get_memory_store

                    store = get_memory_store()
                    for fact in facts:
                        try:
                            store.add(
                                self._user_id,
                                fact,
                                category="fact",
                                source="inferred",
                                confidence=0.7,
                            )
                        except (ValueError, Exception) as exc:
                            logger.debug("Failed to store extracted fact: %s", exc)
                    logger.info(
                        "Pre-compaction flush: stored %d facts from %d frame-backed messages",
                        len(facts),
                        len(dropped_messages),
                    )
            except Exception as exc:
                logger.debug("Pre-compaction fact extraction failed: %s", exc)

            summary_messages = self._messages_for_compaction_summary(initial_result.dropped_frames)
            try:
                summary = await _summarize_messages(
                    summary_messages,
                    user_id=self._user_id,
                    session_id=self._session_id,
                )
            except Exception as exc:
                logger.warning(
                    "Compaction summary generation failed for session=%s: %s",
                    self._session_id,
                    exc,
                )
                summary = None

            chosen_summary = summary
            if not chosen_summary and custom_instructions:
                # PreCompact hooks may have supplied a static instruction we can fall
                # back to so the summarized turn still has continuity context.
                chosen_summary = custom_instructions

            result = compact_frames(
                CompactionRequest(
                    chain=frames,
                    token_budget=budget,
                    reason=reason,
                    provider=self.model,
                    summary_text=chosen_summary,
                    token_counter=self._count_tokens,
                )
            )

            if not result.compacted:
                return False

            hook_frames = self._run_post_compaction_hooks_as_frames()
            self._install_frame_chain([*result.frames, *hook_frames], persist=False)
            with self._lock:
                append_only_frames = list(self.conversation_frames)
            self._persist_append_only_frames(append_only_frames)
            with self._lock:
                self._surfaced_memory_ids.clear()
                token_count = self._conversation_frame_tokens(self.conversation_frames)
                will_retrigger = token_count > self._autocompact_threshold(budget)
                self._last_compaction_meta = {
                    "method": "typed_compaction",
                    "reason": reason,
                    "trigger": trigger,
                    "query_source": query_source,
                    "tokens_before": result.token_before,
                    "tokens_after": result.token_after,
                    "true_post_compact_tokens": token_count,
                    "will_retrigger_next_turn": will_retrigger,
                    "chain_depth": len(self.conversation_frames),
                    "dropped_frames": len(result.dropped_frame_uuids),
                    "kept_frames": len(result.kept_frames),
                }

            # F-048 (R3-A): the HISTORY_SNIP filter
            # (``intent.context_compaction.project_snipped_native_messages``)
            # used to be consumer-only — the schema accepted a snipped
            # set on read but production never populated it, leaving the
            # filter as dead code. Now that typed compaction has just
            # decided which frame uuids to drop from the live chain,
            # propagate the same set through ``mark_snipped_frames`` so
            # downstream native-message replay (which uses the legacy
            # message-id path, not the typed frame chain) honors the
            # same hide decision. Claude's ``snipCompactIfNeeded()``
            # (``query.ts:396``) yields a compact boundary AND a
            # snipped-message-id set on the next turn; this mirrors
            # the same writer side of that contract.
            if result.dropped_frame_uuids:
                try:
                    self.mark_snipped_frames(result.dropped_frame_uuids)
                except Exception:
                    logger.exception(
                        "F-048: mark_snipped_frames after compaction failed (filter consumer may see stale view)"
                    )

            # S8-007: success path resets the circuit breaker.
            self._consecutive_compaction_failures = 0
            _attempt_started = False
            logger.info(
                "Typed compaction complete: dropped=%d kept=%d tokens_before=%d tokens_after=%d live_tokens=%d",
                len(result.dropped_frame_uuids),
                len(result.kept_frames),
                result.token_before,
                result.token_after,
                token_count,
            )
            # S8-010: PostCompact hook dispatch (advisory).
            self._dispatch_post_compact_hook(
                trigger=trigger,
                reason=reason,
                pre_tokens=result.token_before,
                post_tokens=result.token_after,
                dropped=len(result.dropped_frame_uuids),
                kept=len(result.kept_frames),
            )
            return True

        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Compaction failed for session=%s consecutive=%d: %s",
                self._session_id,
                self._consecutive_compaction_failures,
                exc,
                exc_info=True,
            )
            raise
        finally:
            if _attempt_started:
                # The compaction attempt did real work but never reset the
                # counter (no summary, summary-but-empty result, exception),
                # so it counts as a failure for the circuit breaker.
                self._consecutive_compaction_failures += 1
                if self._consecutive_compaction_failures >= MAX_CONSECUTIVE_COMPACTION_FAILURES:
                    logger.warning(
                        "Autocompact circuit breaker open for session=%s after %d consecutive failures",
                        self._session_id,
                        self._consecutive_compaction_failures,
                    )
            self._compaction_in_progress = False

    def add_plan_step(
        self,
        step_description: str,
        status: str = "pending",
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Add a plan step and return its stable step id."""
        now = time.time()
        step_id = str(uuid.uuid4())
        step: PlanStep = {
            "id": step_id,
            "description": step_description.strip(),
            "status": status,
            "result": None,
            "created_at": now,
            "updated_at": now,
            "expires_at": (now + self.plan_step_timeout_seconds if self.plan_step_timeout_seconds else None),
            "metadata": dict(metadata or {}),
        }

        with self._lock:
            if self.plan_state is None:
                self.plan_state = {
                    "id": str(uuid.uuid4()),
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "last_refined_at": now,
                    "steps": [],
                }
            self.plan_state["steps"].append(step)
            self.plan_state["updated_at"] = now
            self._refresh_plan_status_locked(now)

        return step_id

    def update_plan_step(self, step_id: str, status: str, result: str | None = None) -> None:
        """Update an existing plan step."""
        now = time.time()
        with self._lock:
            if self.plan_state is None:
                return

            for step in self.plan_state["steps"]:
                if step["id"] != step_id:
                    continue
                step["status"] = status
                step["updated_at"] = now
                if result is not None:
                    step["result"] = result
                self.plan_state["updated_at"] = now
                if status in {"completed", "cancelled", "expired", "failed"}:
                    step["expires_at"] = now
                self._refresh_plan_status_locked(now)
                return

    def get_active_plan(self) -> dict[str, object] | None:
        """Return a copy of the current plan state after expiring stale steps."""
        now = time.time()
        with self._lock:
            self._expire_steps_locked(now)
            if self.plan_state is None:
                return None
            return copy.deepcopy(self.plan_state)

    def clear_plan(self) -> None:
        """Clear the current plan after it reaches a terminal state."""
        with self._lock:
            if self.plan_state is None:
                return
            self._expire_steps_locked(time.time())
            statuses = {step["status"] for step in self.plan_state["steps"]}
            if statuses and statuses.issubset({"completed", "cancelled", "expired", "failed"}):
                self.plan_state = None

    def clear_history(self) -> None:
        """Clear all stored conversation messages."""
        with self._lock:
            self.conversation_frames.clear()
            self.conversation_history.clear()
            self.context_window_tokens = 0
            self._surfaced_memory_ids.clear()
            self._session_id = new_session_id()
        try:
            persistence = get_conversation_persistence()
            persistence.clear_history(self._user_id)
        except Exception as exc:
            logger.debug("Failed to clear persisted conversation history: %s", exc)

    def micro_compact(self, token_budget: int) -> None:
        """Microcompact oversized tool-result blocks in the typed frame chain."""

        with self._lock:
            frames = list(self.conversation_frames)
            token_count = self._conversation_frame_tokens(frames)
        if token_count < token_budget * 0.5 or len(frames) <= 6:
            return

        result = microcompact_frames(
            frames,
            token_budget=token_budget,
            provider=self.model,
            token_counter=self._count_tokens,
        )
        if not result.compacted:
            return

        self._install_frame_chain(result.frames, persist=False)
        logger.debug(
            "micro_compact: replaced %d typed tool result frame(s)",
            len(result.microcompacted_frame_uuids),
        )

    def get_history(self) -> list[dict[str, object]]:
        """Return a copy of the full stored conversation history."""
        with self._lock:
            return copy.deepcopy(self.conversation_history)

    @property
    def session_id(self) -> str:
        """Return the current session ID."""
        return self._session_id

    @property
    def user_id(self) -> str:
        """Return the owning user ID for this conversation manager."""
        return self._user_id

    @property
    def surfaced_memory_ids(self) -> set[int]:
        """Return the set of memory IDs already surfaced this cycle."""
        return self._surfaced_memory_ids

    def reset_surfaced_memories(self) -> None:
        """Clear the set of surfaced memory IDs (thread-safe)."""
        with self._lock:
            self._surfaced_memory_ids.clear()

    def add_surfaced_ids(self, ids: set[int]) -> None:
        """Record memory IDs as surfaced (thread-safe)."""
        with self._lock:
            self._surfaced_memory_ids.update(ids)

    def register_post_compaction_hook(self, hook: Callable[[], None]) -> None:
        """Register a callable to be invoked after compaction (B6/C2).

        Deduplicates — the same callable is not registered twice.
        Matters because ``AIController`` registers a fresh
        ``invalidate_context_cache`` bound method for every agent-loop
        invocation, and after CHAN-R1 every per-user manager is a
        long-lived singleton. Without dedup a busy user's manager
        accumulates hundreds of bound-method references to retired
        ``AgentExecutor`` instances — correct, but slowly leaks memory.
        """
        if hook in self._post_compaction_hooks:
            return
        self._post_compaction_hooks.append(hook)

    def _count_tokens(self, text: str) -> int:
        """Count tokens with tiktoken when available."""
        if not text:
            return 0

        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                encoding = tiktoken.get_encoding(self._encoding_name)
            return len(encoding.encode(text))
        except Exception as exc:
            logger.debug("Falling back to heuristic token count: %s", exc)
            return max(1, len(text) // 4)

    def _truncate_context(
        self,
        messages: list[ConversationMessage],
        max_tokens: int,
    ) -> list[ConversationMessage]:
        """Keep the newest messages that fit within the context budget."""
        if not messages or max_tokens <= 0:
            return []

        budget = max(1, int(max_tokens))
        selected: list[ConversationMessage] = []
        tokens_used = 0

        for message in reversed(messages):
            message_tokens = int(message.get("token_count", 0)) or (
                self._count_tokens(message.get("content", "")) + _MESSAGE_TOKEN_OVERHEAD
            )

            if not selected and message_tokens > budget:
                truncated_content = self._truncate_text_to_tokens(
                    message.get("content", ""),
                    max(1, budget - _MESSAGE_TOKEN_OVERHEAD),
                )
                selected.append(
                    {
                        "role": message["role"],
                        "content": truncated_content,
                        "timestamp": message["timestamp"],
                        "token_count": self._count_tokens(truncated_content) + _MESSAGE_TOKEN_OVERHEAD,
                    }
                )
                break

            if tokens_used + message_tokens > budget:
                continue

            selected.append(message)
            tokens_used += message_tokens

        selected.reverse()
        return selected

    def _truncate_text_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text content to fit within the target token count."""
        if not text:
            return ""

        budget = max(1, int(max_tokens))
        try:
            import tiktoken

            try:
                encoding = tiktoken.encoding_for_model(self.model)
            except KeyError:
                encoding = tiktoken.get_encoding(self._encoding_name)
            encoded = encoding.encode(text)
            if len(encoded) <= budget:
                return text
            return encoding.decode(encoded[-budget:])
        except Exception:
            return text[-(budget * 4) :]

    def _expire_steps_locked(self, now: float) -> None:
        """Mark pending plan steps expired after their timeout."""
        if self.plan_state is None:
            return

        changed = False
        for step in self.plan_state["steps"]:
            expires_at = step.get("expires_at")
            if step["status"] not in {"pending", "in_progress"}:
                continue
            if expires_at is None or now <= expires_at:
                continue
            step["status"] = "expired"
            step["updated_at"] = now
            step["result"] = step.get("result") or "Step expired before execution."
            changed = True

        if changed:
            self.plan_state["updated_at"] = now
        self._refresh_plan_status_locked(now)

    def _refresh_plan_status_locked(self, now: float) -> None:
        """Update plan-level status from the step statuses."""
        if self.plan_state is None:
            return

        steps = self.plan_state["steps"]
        if not steps:
            self.plan_state["status"] = "active"
            self.plan_state["updated_at"] = now
            return

        statuses = {step["status"] for step in steps}
        if any(status in {"in_progress", "pending"} for status in statuses):
            plan_status = "active"
        elif statuses == {"completed"}:
            plan_status = "completed"
        elif "failed" in statuses:
            plan_status = "failed"
        elif statuses.issubset({"completed", "cancelled", "expired"}):
            plan_status = "completed"
        else:
            plan_status = "active"

        self.plan_state["status"] = plan_status
        self.plan_state["updated_at"] = now


_singleton_managers: OrderedDict[str, ConversationStateManager] = OrderedDict()
_singleton_lock = threading.Lock()


def get_conversation_manager(user_id: str) -> ConversationStateManager:
    """Return the process-wide conversation state manager for a user."""
    with _singleton_lock:
        manager = _singleton_managers.get(user_id)
        if manager is not None:
            _singleton_managers.move_to_end(user_id)
            return manager

        if len(_singleton_managers) >= _MAX_SINGLETON_MANAGERS:
            evicted_user_id, _ = _singleton_managers.popitem(last=False)
            logger.info(
                "Evicted conversation manager for user=%s due to cache cap",
                evicted_user_id,
            )

        manager = ConversationStateManager(user_id=user_id)
        _singleton_managers[user_id] = manager
        return manager


# ---------------------------------------------------------------------------
# Per-request manager selection (multi-tenant isolation — CHAN-R1)
# ---------------------------------------------------------------------------
# When one IntentPipeline is shared across users (e.g. desktop daemon serving
# multiple linked Discord/Matrix users through one shared pipeline), the
# pipeline's construction-time ``conversation_state_manager`` MUST NOT be the
# source of history for each request — that would leak user A's turns into
# user B's context (the failure mode fixed by CHAN-R1).
#
# Pipelines and AIControllers resolve the correct per-user manager at read
# time via this contextvar; IntentPipeline._process_inner publishes the
# manager returned by ``get_conversation_manager(user_key)`` at the start
# of each request using ``use_request_manager``.

_request_manager_cv: contextvars.ContextVar[ConversationStateManager | None] = contextvars.ContextVar(
    "viola_request_conversation_manager",
    default=None,
)


def get_request_conversation_manager() -> ConversationStateManager | None:
    """Return the per-request conversation manager if set, else None."""
    return _request_manager_cv.get()


def set_request_conversation_manager(
    manager: ConversationStateManager | None,
) -> contextvars.Token[ConversationStateManager | None]:
    """Publish *manager* as the per-request manager. Returns a reset token."""
    return _request_manager_cv.set(manager)


@contextlib.contextmanager
def use_request_manager(
    manager: ConversationStateManager | None,
) -> Generator[None, None, None]:
    """Scoped context: bind *manager* as per-request for the duration."""
    token = _request_manager_cv.set(manager)
    try:
        yield
    finally:
        _request_manager_cv.reset(token)
