"""Context frames for model-bound conversation state.

Every per-turn unit of context (system prompt section, user message,
assistant message, tool call, tool result, gate state, profile preflight,
memory recall, etc.) is represented as a Frame here.

Frames get rendered to provider wire format (Anthropic message blocks,
OpenAI Responses items) at the adapter boundary in
``services.conversation.frame_rendering``. NEVER serialize frames to
prose blobs in storage or in the model's context.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union


class FrameRole(str, Enum):
    """Wire-level role on the resulting model message.

    Mirrors Anthropic message roles. META_USER is a synthetic user
    message wrapped in ``<system-reminder>`` at render time.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    META_USER = "meta_user"


class FrameKind(str, Enum):
    """Small content-kind universe; role and block shape carry the rest."""

    USER_INPUT = "user_input"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    SYSTEM_REMINDER = "system_reminder"


# ----- Content blocks -----


@dataclass(frozen=True)
class TextBlock:
    """Plain text content."""

    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    """An assistant's tool call. Mirrors Anthropic tool_use block."""

    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """A tool execution result. Mirrors Anthropic tool_result block."""

    tool_use_id: str
    tool_name: str
    content: str
    is_error: bool


@dataclass(frozen=True)
class SystemReminderBlock:
    """Renders as ``<system-reminder>...</system-reminder>`` at render time.

    Carries provenance for runtime-injected context. NEVER used as
    storage format - only at the final adapter boundary.
    """

    text: str
    source_tag: str | None = None


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock, SystemReminderBlock]


# ----- Frame -----


@dataclass(frozen=True)
class Frame:
    """A single unit of context.

    ``is_meta`` is the behavioral-history boundary: meta frames are retained
    for runtime continuity but are not treated as user/assistant examples.
    ``origin=None`` means ordinary human/model turn metadata, matching Claude
    Code's optional origin field.
    """

    kind: FrameKind
    role: FrameRole
    blocks: tuple[ContentBlock, ...]
    is_meta: bool = False
    is_compact_summary: bool = False
    source_tool_assistant_uuid: str | None = None
    origin: str | None = None
    tool_use_id: str | None = None
    task_id: str | None = None
    permission_mode: str | None = None
    timestamp_ms: int = 0
    ttl_turns: int | None = None
    relevance: str = "always"
    schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)
    uuid: str | None = None
    parent_uuid: str | None = None
    logical_parent_uuid: str | None = None
    session_id: str | None = None


# ----- Bundle -----


_UNSET = object()


@dataclass(init=False)
class PromptFrameBundle:
    """The complete model input for one turn, pre-render.

    Provider adapters in ``frame_rendering`` consume this and produce
    wire-format messages.
    """

    system_static_blocks: list[Frame]
    system_dynamic_blocks: list[Frame]
    meta_user_frames: list[Frame]
    history_frames: list[Frame]
    current_user_frame: Frame | None = None
    cache_boundary_present: bool = False
    frames: list[Frame]
    provider_normalized: bool = False
    _frames_explicit: bool = field(default=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        system_static_blocks: Sequence[Frame] | None = None,
        system_dynamic_blocks: Sequence[Frame] | None = None,
        meta_user_frames: Sequence[Frame] | None = None,
        history_frames: Sequence[Frame] | None = None,
        current_user_frame: Frame | None = None,
        cache_boundary_present: bool = False,
        frames: Sequence[Frame] | object = _UNSET,
        provider_normalized: bool = False,
    ) -> None:
        self.system_static_blocks = list(system_static_blocks or [])
        self.system_dynamic_blocks = list(system_dynamic_blocks or [])
        self.meta_user_frames = list(meta_user_frames or [])
        self.history_frames = list(history_frames or [])
        self.current_user_frame = current_user_frame
        self.cache_boundary_present = cache_boundary_present
        self._frames_explicit = frames is not _UNSET
        self.frames = list(frames or []) if frames is not _UNSET else []
        self.provider_normalized = provider_normalized
        self.__post_init__()

    def __post_init__(self) -> None:
        legacy_frames = self._legacy_message_frames()
        if self._frames_explicit and not self.frames and legacy_frames:
            raise ValueError(
                "PromptFrameBundle is ambiguous: explicit frames=[] cannot be mixed with "
                "meta_user_frames/history_frames/current_user_frame"
            )
        if self.frames and legacy_frames:
            missing = [frame for frame in legacy_frames if frame not in self.frames]
            if missing:
                raise ValueError(
                    "PromptFrameBundle is ambiguous: frames must be the complete message chain "
                    "when legacy message fields are also populated"
                )

    def _legacy_message_frames(self) -> list[Frame]:
        frames: list[Frame] = []
        frames.extend(self.meta_user_frames)
        frames.extend(self.history_frames)
        if self.current_user_frame is not None:
            frames.append(self.current_user_frame)
        return frames

    def to_messages(self) -> list[Frame]:
        """Return the single ordered message chain represented by this bundle."""

        if self.frames:
            return list(self.frames)
        return self._legacy_message_frames()


# ----- Sentinels -----


SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__VIOLA_SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
"""Literal marker in the assembled system prompt string that separates
durable cacheable doctrine from dynamic per-turn state. Provider adapters
split on this marker to attach Anthropic ``cache_control`` to the static
prefix. Other providers ignore the marker."""


# ----- Convenience constructors -----


def text_frame(
    *,
    kind: FrameKind,
    role: FrameRole,
    text: str,
    origin: str | None = None,
    is_meta: bool = False,
    task_id: str | None = None,
    timestamp_ms: int = 0,
    permission_mode: str | None = None,
    uuid: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    session_id: str | None = None,
) -> Frame:
    """Build a Frame with a single TextBlock."""
    return Frame(
        kind=kind,
        role=role,
        blocks=(TextBlock(text=text),),
        is_meta=is_meta,
        origin=origin,
        timestamp_ms=timestamp_ms,
        task_id=task_id,
        permission_mode=permission_mode,
        uuid=uuid,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=session_id,
    )


def system_reminder_frame(
    *,
    kind: FrameKind,
    text: str,
    source_tag: str | None = None,
    origin: str | None = None,
    task_id: str | None = None,
    timestamp_ms: int = 0,
    permission_mode: str | None = None,
    uuid: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    session_id: str | None = None,
) -> Frame:
    """Build a META_USER frame wrapping a SystemReminderBlock.

    The renderer will emit this as ``<system-reminder>...</system-reminder>``
    (or a more specific tag if source_tag is set).
    """
    return Frame(
        kind=kind,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag=source_tag),),
        is_meta=True,
        origin=origin,
        timestamp_ms=timestamp_ms,
        task_id=task_id,
        permission_mode=permission_mode,
        uuid=uuid,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=session_id,
    )


def tool_use_frame(
    *,
    tool_use_id: str,
    name: str,
    input_args: dict[str, Any],
    task_id: str | None = None,
    timestamp_ms: int = 0,
    source_tool_assistant_uuid: str | None = None,
    permission_mode: str | None = None,
    uuid: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    session_id: str | None = None,
) -> Frame:
    """Build a Frame representing an assistant's tool call."""
    return Frame(
        kind=FrameKind.TOOL_USE,
        role=FrameRole.ASSISTANT,
        blocks=(ToolUseBlock(tool_use_id=tool_use_id, name=name, input=input_args),),
        tool_use_id=tool_use_id,
        source_tool_assistant_uuid=source_tool_assistant_uuid,
        timestamp_ms=timestamp_ms,
        task_id=task_id,
        permission_mode=permission_mode,
        uuid=uuid,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=session_id,
    )


def tool_result_frame(
    *,
    tool_use_id: str,
    tool_name: str,
    content: str,
    is_error: bool,
    task_id: str | None = None,
    source_tool_assistant_uuid: str | None = None,
    timestamp_ms: int = 0,
    origin: str | None = None,
    permission_mode: str | None = None,
    uuid: str | None = None,
    parent_uuid: str | None = None,
    logical_parent_uuid: str | None = None,
    session_id: str | None = None,
) -> Frame:
    """Build a Frame representing a tool execution result.

    Set ``is_error=True`` for any non-success outcome (failure, denial,
    cancellation, synthetic error from runtime).
    """
    return Frame(
        kind=FrameKind.TOOL_RESULT,
        role=FrameRole.TOOL,
        blocks=(
            ToolResultBlock(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                content=content,
                is_error=is_error,
            ),
        ),
        source_tool_assistant_uuid=source_tool_assistant_uuid,
        origin=origin,
        tool_use_id=tool_use_id,
        timestamp_ms=timestamp_ms,
        task_id=task_id,
        permission_mode=permission_mode,
        uuid=uuid,
        parent_uuid=parent_uuid,
        logical_parent_uuid=logical_parent_uuid,
        session_id=session_id,
    )


__all__ = [
    "SYSTEM_PROMPT_DYNAMIC_BOUNDARY",
    "ContentBlock",
    "Frame",
    "FrameKind",
    "FrameRole",
    "PromptFrameBundle",
    "SystemReminderBlock",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "system_reminder_frame",
    "text_frame",
    "tool_result_frame",
    "tool_use_frame",
]
