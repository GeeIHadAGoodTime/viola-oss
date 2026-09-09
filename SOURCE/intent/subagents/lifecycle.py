"""Subagent lifecycle primitives for Claude Code parity."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.logging_config import get_logger
from core.platform import get_data_dir
from intent.subagents.fork_messages import (
    FORK_BOILERPLATE_TAG,
    FORK_DIRECTIVE_PREFIX,
    FORK_PLACEHOLDER_RESULT,
    build_child_directive_text,
    build_forked_messages,
    filter_incomplete_tool_calls,
)
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
)

logger = get_logger(__name__)

SubagentMode = Literal["fresh", "fork"]
SubagentStatus = Literal[
    "running",
    "idle",
    "completed",
    "cancelled",
    "killed",
    "error",
    "interrupted",
    "failed",
]
# ``idle`` mirrors Claude Code's ``InProcessTeammateTaskState.isIdle`` — a
# subagent that has finished its current turn but is still alive, waiting on
# ``send_message`` input. Distinguishing this from ``running`` lets the parent
# decide whether to deliver another message synchronously vs. wake the child.
# ``killed`` mirrors Claude Code's user-cancellation status and is separate
# from ``failed`` (crash/API error).
_VALID_SUBAGENT_STATUSES = frozenset(
    {
        "running",
        "idle",
        "completed",
        "cancelled",
        "killed",
        "error",
        "interrupted",
        "failed",
    }
)
SUBAGENT_TRANSCRIPT_DIR = get_data_dir() / "subagents"


@dataclass(frozen=True, slots=True)
class SubagentRequest:
    """Normalized request used to start a child agent."""

    prompt: str
    agent_type: str | None = None
    mode: SubagentMode = "fresh"
    inherited_frame_uuids: tuple[str, ...] = ()
    parent_session_id: str | None = None
    parent_agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubagentHandle:
    """Stable child-agent identity returned by lifecycle/registry code."""

    agent_id: str
    user_id: str
    session_id: str
    mode: SubagentMode
    parent_session_id: str | None
    status: SubagentStatus
    agent_type: str | None = None
    parent_agent_id: str | None = None
    name: str | None = None
    # S5-08: extra resume metadata that Claude Code persists via
    # ``writeAgentMetadata`` (``src/utils/sessionStorage.ts:283-299``).
    # ``worktree_path`` lets the resume path restore cwd; ``use_exact_tools``
    # signals the fork path to skip rebuilding the child tool list
    # and reuse the parent's; ``content_replacements`` carries the compaction
    # replacements. Everything is optional so existing transcripts
    # remain readable.
    worktree_path: str | None = None
    worktree_branch: str | None = None
    use_exact_tools: bool = False
    fork_system_prompt_fingerprint: str | None = None
    content_replacements: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SubagentTranscript:
    """Durable provider-message transcript used for SendMessage resume."""

    agent_id: str
    user_id: str
    session_id: str
    mode: SubagentMode
    status: SubagentStatus
    messages: tuple[dict[str, Any], ...]
    agent_type: str | None = None
    parent_session_id: str | None = None
    parent_agent_id: str | None = None
    name: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    use_exact_tools: bool = False
    fork_system_prompt_fingerprint: str | None = None
    content_replacements: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SubagentNotification:
    """Queued task notification addressed to one current agent."""

    agent_id: str
    user_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    frame: Frame | None = None
    frame_uuid: str | None = None
    parent_session_id: str | None = None
    target_agent_id: str | None = None
    created_at_ms: int = 0


def normalize_subagent_mode(
    mode: object = None,
    *,
    fork_context: object = None,
    subagent_type_present: bool = True,
) -> SubagentMode:
    """Return the explicit fresh/fork start mode.

    Claude Code treats omitted ``subagent_type`` as a fork when the fork
    feature is enabled. Viola mirrors that while still accepting explicit
    ``mode`` or ``fork_context`` arguments from tests or future tool schemas.
    """

    if _truthy(fork_context):
        return "fork"
    if fork_context is False:
        return "fresh"

    raw_mode = str(mode or "").strip().lower().replace("-", "_")
    if raw_mode in {"fork", "forked", "inherit", "inherited"}:
        return "fork"
    if raw_mode in {"fresh", "new", "standalone"}:
        return "fresh"

    return "fresh" if subagent_type_present else "fork"


def make_subagent_session_id(parent_session_id: str | None, agent_id: str) -> str:
    """Build a stable child session id from parent session and agent id."""

    parent = str(parent_session_id or "").strip()
    agent = str(agent_id or "").strip()
    if parent and agent:
        return "%s:subagent:%s" % (parent, agent)
    if agent:
        return "subagent:%s" % agent
    return "subagent"


def clone_prompt_bundle_for_subagent(bundle: PromptFrameBundle | None, mode: SubagentMode) -> PromptFrameBundle:
    """Return the immutable parent context slice visible to a child."""

    if mode == "fresh" or bundle is None:
        return PromptFrameBundle()
    return PromptFrameBundle(
        system_static_blocks=copy.deepcopy(bundle.system_static_blocks),
        system_dynamic_blocks=copy.deepcopy(bundle.system_dynamic_blocks),
        meta_user_frames=copy.deepcopy(bundle.meta_user_frames),
        history_frames=copy.deepcopy(bundle.history_frames),
        current_user_frame=copy.deepcopy(bundle.current_user_frame),
        cache_boundary_present=bundle.cache_boundary_present,
        frames=copy.deepcopy(bundle.frames),
        provider_normalized=False,
    )


def is_task_notification_frame(frame: Frame) -> bool:
    """Return True for queued task-notification meta frames."""

    if frame.origin != "subagent":
        return False
    if frame.extra.get("notification_type") == "task-notification":
        return True
    for block in frame.blocks:
        if isinstance(block, SystemReminderBlock) and "<task-notification>" in block.text:
            return True
    return False


def bundle_with_drained_notifications(
    bundle: PromptFrameBundle | None,
    notification_frames: Sequence[Frame],
) -> PromptFrameBundle:
    """Strip stale notification frames, then insert this turn's drained ones."""

    source = bundle or PromptFrameBundle()
    drained = [frame for frame in notification_frames if isinstance(frame, Frame)]
    filtered_meta = [frame for frame in source.meta_user_frames if not is_task_notification_frame(frame)]
    filtered_history = [frame for frame in source.history_frames if not is_task_notification_frame(frame)]
    filtered_frames = [frame for frame in source.frames if not is_task_notification_frame(frame)]
    if source.frames:
        existing_frames = filtered_frames
    else:
        existing_frames = [*filtered_meta, *filtered_history]
        if source.current_user_frame is not None and not is_task_notification_frame(source.current_user_frame):
            existing_frames.append(source.current_user_frame)

    return PromptFrameBundle(
        system_static_blocks=list(source.system_static_blocks),
        system_dynamic_blocks=list(source.system_dynamic_blocks),
        cache_boundary_present=source.cache_boundary_present,
        frames=[*drained, *existing_frames],
        provider_normalized=False,
    )


def provider_messages_to_sidechain_frames(
    messages: Sequence[dict[str, Any]],
    *,
    agent_id: str,
    session_id: str,
    mode: SubagentMode,
    timestamp_ms: int | None = None,
) -> list[Frame]:
    """Convert provider-style sidechain messages into immutable frames."""

    base_ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    frames: list[Frame] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = _coerce_role(message.get("role"))
        content_text = _content_text(message.get("content"))
        if not content_text:
            continue
        frames.append(
            Frame(
                kind=FrameKind.ASSISTANT_TEXT if role is FrameRole.ASSISTANT else FrameKind.USER_INPUT,
                role=role,
                blocks=(TextBlock(text=content_text),),
                origin="subagent_sidechain",
                timestamp_ms=base_ts + index,
                session_id=session_id,
                extra={
                    "agent_id": agent_id,
                    "mode": mode,
                    "sidechain_index": index,
                },
            )
        )
    return frames


class SubagentLifecycleStore:
    """In-memory lifecycle index for active process-local child agents."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[tuple[str, str], SubagentHandle] = {}
        self._names: dict[str, dict[str, str]] = {}
        self._sidechains: dict[tuple[str, str], list[Frame]] = {}
        self._provider_messages: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._pending_messages: dict[tuple[str, str], list[str]] = {}
        self._notifications: dict[str, list[SubagentNotification]] = {}

    def register(self, handle: SubagentHandle) -> None:
        user_id = _require_user_id(handle.user_id)
        agent_id = str(handle.agent_id or "").strip()
        if not agent_id:
            raise ValueError("Subagent handle requires agent_id")
        key = (user_id, agent_id)
        stored = SubagentHandle(
            agent_id=agent_id,
            user_id=user_id,
            session_id=handle.session_id,
            mode=handle.mode,
            parent_session_id=handle.parent_session_id,
            status=handle.status,
            agent_type=handle.agent_type,
            parent_agent_id=handle.parent_agent_id,
            name=handle.name,
            worktree_path=handle.worktree_path,
            worktree_branch=handle.worktree_branch,
            use_exact_tools=handle.use_exact_tools,
            fork_system_prompt_fingerprint=handle.fork_system_prompt_fingerprint,
            content_replacements=tuple(handle.content_replacements or ()),
        )
        with self._lock:
            self._handles[key] = stored
            if stored.name:
                self._names.setdefault(user_id, {})[_normalize_name(stored.name)] = agent_id
            self._write_transcript_locked(user_id, agent_id)

    def update_status(self, agent_id: str, status: SubagentStatus, *, user_id: str) -> None:
        user_id = _require_user_id(user_id)
        agent_id = str(agent_id or "").strip()
        key = (user_id, agent_id)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                return
            self._handles[key] = SubagentHandle(
                agent_id=handle.agent_id,
                user_id=handle.user_id,
                session_id=handle.session_id,
                mode=handle.mode,
                parent_session_id=handle.parent_session_id,
                status=status,
                agent_type=handle.agent_type,
                parent_agent_id=handle.parent_agent_id,
                name=handle.name,
                worktree_path=handle.worktree_path,
                worktree_branch=handle.worktree_branch,
                use_exact_tools=handle.use_exact_tools,
                fork_system_prompt_fingerprint=handle.fork_system_prompt_fingerprint,
                content_replacements=handle.content_replacements,
            )
            self._write_transcript_locked(user_id, agent_id)

    def update_resume_metadata(
        self,
        agent_id: str,
        *,
        user_id: str,
        worktree_path: str | None = None,
        worktree_branch: str | None = None,
        use_exact_tools: bool | None = None,
        fork_system_prompt_fingerprint: str | None = None,
        content_replacements: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """Patch resume metadata on a registered subagent handle (S5-08)."""

        user_id = _require_user_id(user_id)
        agent_id = str(agent_id or "").strip()
        key = (user_id, agent_id)
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                return
            self._handles[key] = SubagentHandle(
                agent_id=handle.agent_id,
                user_id=handle.user_id,
                session_id=handle.session_id,
                mode=handle.mode,
                parent_session_id=handle.parent_session_id,
                status=handle.status,
                agent_type=handle.agent_type,
                parent_agent_id=handle.parent_agent_id,
                name=handle.name,
                worktree_path=worktree_path if worktree_path is not None else handle.worktree_path,
                worktree_branch=worktree_branch if worktree_branch is not None else handle.worktree_branch,
                use_exact_tools=use_exact_tools if use_exact_tools is not None else handle.use_exact_tools,
                fork_system_prompt_fingerprint=(
                    fork_system_prompt_fingerprint
                    if fork_system_prompt_fingerprint is not None
                    else handle.fork_system_prompt_fingerprint
                ),
                content_replacements=(
                    tuple(content_replacements) if content_replacements is not None else handle.content_replacements
                ),
            )
            self._write_transcript_locked(user_id, agent_id)

    def get_handle(self, agent_id: str, *, user_id: str) -> SubagentHandle | None:
        user_id = _require_user_id(user_id)
        with self._lock:
            resolved = self._resolve_agent_id_locked(user_id, agent_id)
            return self._handles.get((user_id, resolved)) if resolved else None

    def record_sidechain(self, agent_id: str, frames: Sequence[Frame], *, user_id: str, replace: bool = False) -> None:
        user_id = _require_user_id(user_id)
        agent_id = str(agent_id or "").strip()
        key = (user_id, agent_id)
        kept = [frame for frame in frames if isinstance(frame, Frame)]
        with self._lock:
            if replace:
                self._sidechains[key] = kept
            else:
                self._sidechains.setdefault(key, []).extend(kept)

    def get_sidechain(self, agent_id: str, *, user_id: str) -> list[Frame]:
        user_id = _require_user_id(user_id)
        with self._lock:
            resolved = self._resolve_agent_id_locked(user_id, agent_id)
            return list(self._sidechains.get((user_id, resolved or agent_id), ()))

    def record_provider_messages(
        self,
        agent_id: str,
        messages: Sequence[dict[str, Any]],
        *,
        user_id: str,
        session_id: str,
        mode: SubagentMode,
        replace: bool = False,
    ) -> None:
        user_id = _require_user_id(user_id)
        agent_id = str(agent_id or "").strip()
        key = (user_id, agent_id)
        kept = [copy.deepcopy(message) for message in messages if isinstance(message, dict)]
        frames = provider_messages_to_sidechain_frames(
            kept,
            agent_id=agent_id,
            session_id=session_id,
            mode=mode,
        )
        with self._lock:
            if replace:
                self._provider_messages[key] = kept
                self._sidechains[key] = frames
            else:
                self._provider_messages.setdefault(key, []).extend(kept)
                self._sidechains.setdefault(key, []).extend(frames)
            self._write_transcript_locked(user_id, agent_id)

    def enqueue_message(self, agent_id_or_name: str, message: str, *, user_id: str) -> str | None:
        user_id = _require_user_id(user_id)
        text = str(message or "").strip()
        if not text:
            return None
        with self._lock:
            agent_id = self._resolve_agent_id_locked(user_id, agent_id_or_name)
            if not agent_id:
                return None
            self._pending_messages.setdefault((user_id, agent_id), []).append(text)
            return agent_id

    def drain_messages(self, agent_id: str, *, user_id: str) -> list[str]:
        user_id = _require_user_id(user_id)
        with self._lock:
            return self._pending_messages.pop((user_id, agent_id), [])

    def resolve_agent_id(self, agent_id_or_name: str, *, user_id: str) -> str | None:
        user_id = _require_user_id(user_id)
        with self._lock:
            return self._resolve_agent_id_locked(user_id, agent_id_or_name)

    def get_transcript(self, agent_id_or_name: str, *, user_id: str) -> SubagentTranscript | None:
        user_id = _require_user_id(user_id)
        raw = str(agent_id_or_name or "").strip()
        with self._lock:
            agent_id = self._resolve_agent_id_locked(user_id, raw) or raw
            if not agent_id:
                return None
            transcript = self._transcript_from_memory_locked(user_id, agent_id)
        if transcript is not None:
            return transcript
        transcript = self._read_transcript(agent_id, user_id=user_id)
        if transcript is not None:
            return transcript
        return self._read_transcript_by_name(raw, user_id=user_id)

    def enqueue_notification(self, notification: SubagentNotification) -> None:
        user_id = _require_user_id(notification.user_id)
        created = notification.created_at_ms or int(time.time() * 1000)
        queued = SubagentNotification(
            agent_id=notification.agent_id,
            user_id=user_id,
            event_type=notification.event_type,
            payload=copy.deepcopy(notification.payload),
            frame=notification.frame,
            frame_uuid=notification.frame_uuid,
            parent_session_id=_clean_optional(notification.parent_session_id),
            target_agent_id=_clean_optional(notification.target_agent_id),
            created_at_ms=created,
        )
        with self._lock:
            self._notifications.setdefault(user_id, []).append(queued)

    def drain_notifications(
        self,
        *,
        user_id: str,
        parent_session_id: str | None,
        current_agent_id: str | None,
        limit: int = 20,
    ) -> list[SubagentNotification]:
        user_id = _require_user_id(user_id)
        parent_key = _clean_optional(parent_session_id)
        target_key = _clean_optional(current_agent_id)
        drained: list[SubagentNotification] = []
        remaining: list[SubagentNotification] = []
        with self._lock:
            notifications = self._notifications.get(user_id, [])
            for notification in notifications:
                if (
                    len(drained) < limit
                    and _clean_optional(notification.parent_session_id) == parent_key
                    and _clean_optional(notification.target_agent_id) == target_key
                ):
                    drained.append(notification)
                else:
                    remaining.append(notification)
            if remaining:
                self._notifications[user_id] = remaining
            else:
                self._notifications.pop(user_id, None)
        return drained

    def reset_for_tests(self) -> None:
        with self._lock:
            self._handles.clear()
            self._names.clear()
            self._sidechains.clear()
            self._provider_messages.clear()
            self._pending_messages.clear()
            self._notifications.clear()

    def reset_for_user(self, user_id: str) -> None:
        """Drop all in-memory lifecycle state for one concrete user."""
        user_id = _require_user_id(user_id)
        with self._lock:
            for mapping in (
                self._handles,
                self._sidechains,
                self._provider_messages,
                self._pending_messages,
            ):
                for key in list(mapping):
                    if key[0] == user_id:
                        del mapping[key]
            self._names.pop(user_id, None)
            for notification_key in list(self._notifications):
                kept = [
                    notification
                    for notification in self._notifications[notification_key]
                    if notification.user_id != user_id
                ]
                if kept:
                    self._notifications[notification_key] = kept
                else:
                    del self._notifications[notification_key]

    def _resolve_agent_id_locked(self, user_id: str, agent_id_or_name: str) -> str | None:
        raw = str(agent_id_or_name or "").strip()
        if not raw:
            return None
        if (user_id, raw) in self._handles:
            return raw
        return self._names.get(user_id, {}).get(_normalize_name(raw))

    def _transcript_from_memory_locked(self, user_id: str, agent_id: str) -> SubagentTranscript | None:
        key = (user_id, agent_id)
        handle = self._handles.get(key)
        messages = self._provider_messages.get(key, [])
        if handle is None and not messages:
            return None
        return SubagentTranscript(
            agent_id=agent_id,
            user_id=user_id,
            session_id=handle.session_id if handle else make_subagent_session_id(None, agent_id),
            mode=handle.mode if handle else "fresh",
            status=handle.status if handle else "interrupted",
            messages=tuple(copy.deepcopy(messages)),
            agent_type=handle.agent_type if handle else None,
            parent_session_id=handle.parent_session_id if handle else None,
            parent_agent_id=handle.parent_agent_id if handle else None,
            name=handle.name if handle else None,
            worktree_path=handle.worktree_path if handle else None,
            worktree_branch=handle.worktree_branch if handle else None,
            use_exact_tools=handle.use_exact_tools if handle else False,
            fork_system_prompt_fingerprint=(handle.fork_system_prompt_fingerprint if handle else None),
            content_replacements=tuple(handle.content_replacements) if handle else (),
        )

    def _write_transcript_locked(self, user_id: str, agent_id: str) -> None:
        transcript = self._transcript_from_memory_locked(user_id, agent_id)
        if transcript is None:
            return
        try:
            directory = _transcript_dir(user_id)
            directory.mkdir(parents=True, exist_ok=True)
            path = _transcript_path(agent_id, user_id=user_id)
            tmp = path.with_suffix(".tmp")
            payload = {
                # S5-08: schema_version bumped to 3 to add resume metadata
                # (worktree, exact-tools, fork system prompt fingerprint,
                # content replacements). Older v2 transcripts are still
                # readable via ``_read_transcript`` because the new fields
                # are all optional with safe defaults.
                "schema_version": 3,
                "agent_id": transcript.agent_id,
                "user_id_hash": _user_id_hash(user_id),
                "session_id": transcript.session_id,
                "mode": transcript.mode,
                "status": transcript.status,
                "agent_type": transcript.agent_type,
                "parent_session_id": transcript.parent_session_id,
                "parent_agent_id": transcript.parent_agent_id,
                "name": transcript.name,
                "worktree_path": transcript.worktree_path,
                "worktree_branch": transcript.worktree_branch,
                "use_exact_tools": bool(transcript.use_exact_tools),
                "fork_system_prompt_fingerprint": transcript.fork_system_prompt_fingerprint,
                "content_replacements": list(transcript.content_replacements),
                "updated_at_ms": int(time.time() * 1000),
                "messages": list(transcript.messages),
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Failed to persist subagent transcript for %s: %s", agent_id, exc)

    def _read_transcript(self, agent_id: str, *, user_id: str) -> SubagentTranscript | None:
        user_id = _require_user_id(user_id)
        try:
            raw = _transcript_path(agent_id, user_id=user_id).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        schema_version = data.get("schema_version")
        if schema_version not in (2, 3) or data.get("user_id_hash") != _user_id_hash(user_id):
            return None
        messages = data.get("messages")
        if not isinstance(messages, list):
            messages = []
        status = str(data.get("status") or "interrupted")
        if status not in _VALID_SUBAGENT_STATUSES:
            status = "interrupted"
        mode = normalize_subagent_mode(data.get("mode"))
        content_replacements_raw = data.get("content_replacements") if schema_version == 3 else None
        if isinstance(content_replacements_raw, list):
            content_replacements = tuple(
                copy.deepcopy(item) for item in content_replacements_raw if isinstance(item, dict)
            )
        else:
            content_replacements = ()
        return SubagentTranscript(
            agent_id=str(data.get("agent_id") or agent_id),
            user_id=user_id,
            session_id=str(data.get("session_id") or make_subagent_session_id(None, agent_id)),
            mode=mode,
            status=status,  # type: ignore[arg-type]
            messages=tuple(copy.deepcopy(message) for message in messages if isinstance(message, dict)),
            agent_type=_clean_optional(data.get("agent_type")),
            parent_session_id=_clean_optional(data.get("parent_session_id")),
            parent_agent_id=_clean_optional(data.get("parent_agent_id")),
            name=_clean_optional(data.get("name")),
            worktree_path=_clean_optional(data.get("worktree_path")) if schema_version == 3 else None,
            worktree_branch=_clean_optional(data.get("worktree_branch")) if schema_version == 3 else None,
            use_exact_tools=bool(data.get("use_exact_tools")) if schema_version == 3 else False,
            fork_system_prompt_fingerprint=(
                _clean_optional(data.get("fork_system_prompt_fingerprint")) if schema_version == 3 else None
            ),
            content_replacements=content_replacements,
        )

    def _read_transcript_by_name(self, name: str, *, user_id: str) -> SubagentTranscript | None:
        user_id = _require_user_id(user_id)
        normalized = _normalize_name(name)
        if not normalized:
            return None
        try:
            paths = sorted(_transcript_dir(user_id).glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for path in paths:
            transcript = self._read_transcript(path.stem, user_id=user_id)
            if transcript is not None and _normalize_name(transcript.name) == normalized:
                return transcript
        return None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "fork"}
    return bool(value)


def _clean_optional(value: object) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _normalize_name(value: object) -> str:
    return str(value or "").strip().lower()


def _require_user_id(user_id: object) -> str:
    raw = str(user_id or "").strip()
    if not raw or raw.lower() == "default":
        raise ValueError("Subagent lifecycle requires a concrete user_id")
    return raw


def _user_id_hash(user_id: str) -> str:
    return hashlib.sha256(_require_user_id(user_id).encode("utf-8", errors="replace")).hexdigest()


def _transcript_dir(user_id: str) -> Path:
    return SUBAGENT_TRANSCRIPT_DIR / "by_user" / _user_id_hash(user_id)


def _safe_agent_file_stem(agent_id: str) -> str:
    raw = str(agent_id or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw) or "subagent"


def _transcript_path(agent_id: str, *, user_id: str) -> Path:
    return _transcript_dir(user_id) / ("%s.json" % _safe_agent_file_stem(agent_id))


def subagent_transcript_path(agent_id: str, *, user_id: str) -> Path:
    """Return the durable transcript path for a subagent id."""

    return _transcript_path(agent_id, user_id=user_id)


def ensure_subagent_transcript_seeded(
    *,
    agent_id: str,
    user_id: str,
    session_id: str,
    mode: SubagentMode,
    status: SubagentStatus = "running",
    agent_type: str | None = None,
    parent_session_id: str | None = None,
    parent_agent_id: str | None = None,
    name: str | None = None,
    messages: Sequence[dict[str, Any]] | None = None,
) -> Path:
    """Ensure a durable subagent transcript exists at registration time.

    Mirrors Claude Code's ``initTaskOutputAsSymlink`` behavior: the parent's
    ``start_agent`` tool result advertises ``output_file``, and the file MUST
    exist by then. The previous code only flushed transcripts on the first
    ``record_provider_messages`` call, so direct no-tool completions and
    fast-fail spawns left ``output_file`` pointing at a missing path.
    """

    user_id = _require_user_id(user_id)
    agent_id = str(agent_id or "").strip()
    if not agent_id:
        raise ValueError("Subagent transcript seed requires agent_id")
    subagent_lifecycle.register(
        SubagentHandle(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            mode=mode,
            parent_session_id=parent_session_id,
            status=status,
            agent_type=agent_type,
            parent_agent_id=parent_agent_id,
            name=name,
        )
    )
    if messages:
        subagent_lifecycle.record_provider_messages(
            agent_id,
            list(messages),
            user_id=user_id,
            session_id=session_id,
            mode=mode,
            replace=True,
        )
    return subagent_transcript_path(agent_id, user_id=user_id)


def _coerce_role(value: object) -> FrameRole:
    raw = str(value or "").strip().lower()
    if raw == "assistant":
        return FrameRole.ASSISTANT
    if raw == "tool":
        return FrameRole.TOOL
    return FrameRole.USER


def _content_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    if value is None:
        return ""
    return str(value).strip()


subagent_lifecycle = SubagentLifecycleStore()


_TRANSCRIPT_EXPORT_KEYS = (
    "schema_version",
    "agent_id",
    "session_id",
    "mode",
    "status",
    "agent_type",
    "parent_session_id",
    "parent_agent_id",
    "name",
    "worktree_path",
    "worktree_branch",
    "use_exact_tools",
    "fork_system_prompt_fingerprint",
    "content_replacements",
    "updated_at_ms",
    "messages",
)


def _transcript_payload_for_export(path: Path, *, user_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") not in (2, 3) or data.get("user_id_hash") != _user_id_hash(user_id):
        return None
    payload = {key: copy.deepcopy(data.get(key)) for key in _TRANSCRIPT_EXPORT_KEYS if key in data}
    if not isinstance(payload.get("messages"), list):
        payload["messages"] = []
    payload["transcript_file"] = path.name
    return payload


def export_subagent_transcripts_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return portable subagent transcripts for one user without other users' hashes."""
    user_id = _require_user_id(user_id)
    directory = _transcript_dir(user_id)
    if not directory.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = _transcript_payload_for_export(path, user_id=user_id)
        if payload is not None:
            payloads.append(payload)
    return payloads


def count_subagent_transcripts_for_user(user_id: str) -> int:
    """Count durable subagent transcript files for one user."""
    return len(export_subagent_transcripts_for_user(user_id))


def delete_subagent_transcripts_for_user(user_id: str) -> int:
    """Delete durable and in-memory subagent transcripts for one user."""
    user_id = _require_user_id(user_id)
    directory = _transcript_dir(user_id)
    deleted = 0
    if directory.exists():
        deleted = sum(1 for item in directory.rglob("*") if item.is_file())
        shutil.rmtree(directory, ignore_errors=True)
    subagent_lifecycle.reset_for_user(user_id)
    return deleted


__all__ = [
    "FORK_BOILERPLATE_TAG",
    "FORK_DIRECTIVE_PREFIX",
    "FORK_PLACEHOLDER_RESULT",
    "SubagentHandle",
    "SubagentLifecycleStore",
    "SubagentMode",
    "SubagentNotification",
    "SubagentRequest",
    "SubagentStatus",
    "SubagentTranscript",
    "build_child_directive_text",
    "build_forked_messages",
    "bundle_with_drained_notifications",
    "clone_prompt_bundle_for_subagent",
    "count_subagent_transcripts_for_user",
    "delete_subagent_transcripts_for_user",
    "ensure_subagent_transcript_seeded",
    "export_subagent_transcripts_for_user",
    "filter_incomplete_tool_calls",
    "is_task_notification_frame",
    "make_subagent_session_id",
    "normalize_subagent_mode",
    "provider_messages_to_sidechain_frames",
    "subagent_lifecycle",
    "subagent_transcript_path",
]
