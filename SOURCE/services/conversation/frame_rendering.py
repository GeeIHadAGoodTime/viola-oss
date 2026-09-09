"""Render Frames to provider wire format at the adapter boundary.

This is the ONLY place in the codebase that should serialize Frames to
text/JSON for an LLM call. Storage layer keeps Frames structured;
provider adapters call into this module.

Contract — see ``_diag/CONTEXT_FRAMES_SCHEMA_LOCK.md`` §2.

Two rendering targets are supported:

- ``render_for_anthropic``: native Anthropic message blocks with
  optional Anthropic cache annotations.
- ``render_for_openai_responses``: OpenAI Responses API format
  (function_call / function_call_output).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Iterable, Literal

from services.conversation.chain_validator import validate_canonical_chain
from services.conversation.content_normalization import (
    DEFAULT_MEDIA_LIMIT_POLICY,
    MediaLimitPolicy,
    normalize_frame_for_provider,
)
from services.conversation.context_frames import (
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    ContentBlock,
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from services.conversation.message_invariants import (
    dedupe_tool_use_ids,
    ensure_tool_result_pairing,
    preserve_role_alternation,
    validate_no_behavioral_bail_examples,
)

if TYPE_CHECKING:
    from services.llm.anthropic_cache import AnthropicCachePolicy

# ----- Wrapping helpers -----


def wrap_system_reminder(text: str, *, source_tag: str | None = None) -> str:
    """Wrap text in ``<system-reminder>...</system-reminder>``.

    If ``source_tag`` is provided, wrap as
    ``<source-tag>...</source-tag>`` INSIDE the reminder so the model
    can distinguish gate state from task notification from memory recall.
    """
    body = text.strip()
    if source_tag:
        tag = source_tag.strip()
        body = f"<{tag}>\n{body}\n</{tag}>"
    return f"<system-reminder>\n{body}\n</system-reminder>"


def ensure_system_reminder_wrap(frame: Frame) -> Frame:
    """If a META_USER frame's text isn't already wrapped, wrap it.

    Idempotent: re-wrapping a frame whose text already starts with
    ``<system-reminder>`` returns the frame unchanged.
    """
    if frame.role is not FrameRole.META_USER:
        return frame
    new_blocks: list[ContentBlock] = []
    changed = False
    for block in frame.blocks:
        if isinstance(block, SystemReminderBlock):
            stripped = block.text.lstrip()
            if stripped.startswith("<system-reminder>"):
                new_blocks.append(block)
            else:
                wrapped_text = wrap_system_reminder(block.text, source_tag=block.source_tag)
                new_blocks.append(SystemReminderBlock(text=wrapped_text, source_tag=block.source_tag))
                changed = True
        elif isinstance(block, TextBlock):
            wrapped_text = wrap_system_reminder(block.text)
            new_blocks.append(SystemReminderBlock(text=wrapped_text, source_tag=None))
            changed = True
        else:
            new_blocks.append(block)
    if not changed:
        return frame
    return replace(frame, blocks=tuple(new_blocks), is_meta=True)


def smoosh_system_reminder_siblings(frames: list[Frame]) -> list[Frame]:
    """Merge system-reminder META_USER frames into adjacent TOOL_RESULT frames.

    Claude Code's ``smooshSystemReminderSiblings`` (``src/utils/messages.ts:1835-1873``)
    applies specifically to user messages whose content array contains a
    ``tool_result`` — it appends the reminder text into that tool_result's
    rendered content so the model never sees a bare meta-only user turn
    sandwiched between tool calls. A reminder that arrives in the same
    user-message position as the tool_result (whether the reminder block
    appears before or after the tool_result block in the array) gets folded
    into the tool_result's text.

    Ordinary adjacent user/meta message merging is a SEPARATE step in Claude
    (``src/utils/messages.ts:2411-2515``) with text-seam, ``isMeta``, and UUID
    rules; we leave that lane to the per-provider ``_merge_adjacent_*_messages``
    helpers further down the pipeline.

    The earlier implementation also fused unrelated adjacent META_USER
    reminders pre-render, which (a) departed from Claude's narrower
    tool-result-adjacency rule and (b) flattened multiple distinct
    source_tags into one frame, weakening audit trails. The current
    implementation only fuses a reminder into an adjacent tool_result;
    standalone reminders pass through untouched and let provider message
    merging fold them as ordinary adjacent user content.

    Idempotent: running smoosh twice yields the same output as once.
    """
    if not frames:
        return frames

    def _merge_reminder_into_tool_result(target: Frame, reminder: Frame) -> Frame | None:
        if target is None or target.kind is not FrameKind.TOOL_RESULT:
            return None
        if len(target.blocks) != 1:
            return None
        block = target.blocks[0]
        if not isinstance(block, ToolResultBlock):
            return None
        extra_lines: list[str] = []
        for rb in reminder.blocks:
            if isinstance(rb, SystemReminderBlock):
                extra_lines.append(rb.text)
        if not extra_lines:
            return None
        merged_content = block.content + "\n\n" + "\n\n".join(extra_lines)
        merged_block = ToolResultBlock(
            tool_use_id=block.tool_use_id,
            tool_name=block.tool_name,
            content=merged_content,
            is_error=block.is_error,
        )
        return replace(target, blocks=(merged_block,))

    result: list[Frame] = []
    pending_reminders: list[Frame] = []

    for frame in frames:
        is_reminder = frame.role is FrameRole.META_USER and any(
            isinstance(b, SystemReminderBlock) for b in frame.blocks
        )
        if is_reminder:
            # Try a backward merge first: a reminder that lands right after a
            # tool_result merges into its content (Claude TS post-tool case).
            if result:
                merged_previous = _merge_reminder_into_tool_result(result[-1], frame)
                if merged_previous is not None:
                    result[-1] = merged_previous
                    continue
            # Otherwise the reminder may be pre-tool: hold it until we see a
            # tool_result we can fold it into. If the next frame is anything
            # else, the reminder is emitted as-is so provider message merging
            # can handle ordinary adjacent meta-user content.
            pending_reminders.append(frame)
            continue
        if pending_reminders and frame.kind is FrameKind.TOOL_RESULT:
            merged = frame
            for reminder in pending_reminders:
                fused = _merge_reminder_into_tool_result(merged, reminder)
                if fused is not None:
                    merged = fused
            pending_reminders = []
            result.append(merged)
            continue
        # Pending reminders couldn't be folded into a tool_result; emit them
        # as standalone frames in original order so audit trails survive.
        if pending_reminders:
            result.extend(pending_reminders)
            pending_reminders = []
        result.append(frame)

    # Trailing pending reminders never reached a tool_result; emit standalone.
    if pending_reminders:
        result.extend(pending_reminders)
    return result


# ----- Provider-boundary normalizer -----


def _message_frames_from_bundle(bundle: PromptFrameBundle) -> list[Frame]:
    return bundle.to_messages()


def normalize_prompt_bundle_for_provider(
    bundle: PromptFrameBundle,
    *,
    mode: Literal["full", "delta"] = "full",
    provider_caps: MediaLimitPolicy | dict[str, Any] | None = None,
) -> PromptFrameBundle:
    """Return a PromptFrameBundle whose message frames passed the one normalizer."""

    if bundle.provider_normalized:
        return bundle

    policy = provider_caps or DEFAULT_MEDIA_LIMIT_POLICY
    system_static_blocks = [normalize_frame_for_provider(frame, policy) for frame in bundle.system_static_blocks]
    system_dynamic_blocks = [normalize_frame_for_provider(frame, policy) for frame in bundle.system_dynamic_blocks]
    frames = [
        normalize_frame_for_provider(ensure_system_reminder_wrap(frame), policy)
        for frame in _message_frames_from_bundle(bundle)
    ]
    frames = smoosh_system_reminder_siblings(frames)
    if mode == "delta":
        validate_no_behavioral_bail_examples(frames)
        frames = preserve_role_alternation(dedupe_tool_use_ids(frames))
        validate_no_behavioral_bail_examples(frames)
    else:
        frames = ensure_tool_result_pairing(frames)

    return PromptFrameBundle(
        system_static_blocks=system_static_blocks,
        system_dynamic_blocks=system_dynamic_blocks,
        cache_boundary_present=bundle.cache_boundary_present,
        frames=frames,
        provider_normalized=True,
    )


# ----- Provider adapters -----


def _render_static_system_text(frames: Iterable[Frame]) -> str:
    parts: list[str] = []
    for frame in frames:
        for block in frame.blocks:
            if isinstance(block, (TextBlock, SystemReminderBlock)):
                parts.append(block.text.strip())
    return "\n\n".join(p for p in parts if p)


def _render_dynamic_system_text(frames: Iterable[Frame]) -> str:
    return _render_static_system_text(frames)


def _render_meta_user_frame_to_text(frame: Frame) -> str:
    chunks: list[str] = []
    for block in frame.blocks:
        if isinstance(block, SystemReminderBlock):
            chunks.append(block.text)
        elif isinstance(block, TextBlock):
            chunks.append(wrap_system_reminder(block.text))
    return "\n\n".join(chunks)


def _render_user_frame_to_text(frame: Frame) -> str:
    parts: list[str] = []
    for block in frame.blocks:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "\n".join(parts)


def render_for_anthropic(
    bundle: PromptFrameBundle,
    *,
    cache_policy: AnthropicCachePolicy | None = None,
) -> dict[str, Any]:
    """Render a PromptFrameBundle to Anthropic ``messages`` API shape.

    Returns ``{"system": list[block], "messages": list[message]}`` where:

    - ``system`` is an array of text blocks; the first carries
      ``cache_control={"type": "ephemeral"}`` when the cache boundary
      was present, or when an explicit Anthropic cache policy requests it.
    - ``messages`` preserves role typing; tool_use/tool_result are
      structured content blocks on user/assistant messages.

    META_USER frames become user messages with text content (their text
    is already wrapped in ``<system-reminder>`` by
    ``ensure_system_reminder_wrap``).
    """
    normalized = normalize_prompt_bundle_for_provider(bundle)
    static_text = _render_static_system_text(normalized.system_static_blocks)
    dynamic_text = _render_dynamic_system_text(normalized.system_dynamic_blocks)

    system_blocks: list[dict[str, Any]] = []
    if static_text:
        block: dict[str, Any] = {"type": "text", "text": static_text}
        if normalized.cache_boundary_present:
            block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(block)
    if dynamic_text:
        system_blocks.append({"type": "text", "text": dynamic_text})

    messages: list[dict[str, Any]] = []

    for frame in normalized.frames:
        msg = _frame_to_anthropic_message(frame)
        if msg is not None:
            messages.append(msg)
    messages = _merge_adjacent_anthropic_messages(messages)
    validate_canonical_chain(messages, provider="anthropic")

    rendered = {"system": system_blocks, "messages": messages}
    if cache_policy is not None:
        from services.llm.anthropic_cache import apply_anthropic_cache_controls

        return apply_anthropic_cache_controls(rendered, cache_policy)
    return rendered


def _merge_adjacent_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if merged and role == merged[-1].get("role"):
            content = _merge_anthropic_content(merged[-1].get("content"), message.get("content"))
            if content is not None:
                merged[-1] = {**merged[-1], "content": content}
                continue
        merged.append(message)
    return merged


def _merge_anthropic_content(left: Any, right: Any) -> Any | None:
    if isinstance(left, str) and isinstance(right, str):
        return "\n\n".join(part for part in (left, right) if part)
    if isinstance(left, list) and isinstance(right, list):
        return [*left, *right]
    if isinstance(left, str) and isinstance(right, list):
        return [{"type": "text", "text": left}, *right] if left else list(right)
    if isinstance(left, list) and isinstance(right, str):
        return [*left, {"type": "text", "text": right}] if right else list(left)
    return None


def _frame_to_anthropic_message(frame: Frame) -> dict[str, Any] | None:
    if frame.role is FrameRole.USER:
        text = _render_user_frame_to_text(frame)
        return {"role": "user", "content": text} if text else None
    if frame.role is FrameRole.META_USER:
        wrapped = ensure_system_reminder_wrap(frame)
        text = _render_meta_user_frame_to_text(wrapped)
        return {"role": "user", "content": text} if text else None
    if frame.role is FrameRole.ASSISTANT:
        content_blocks: list[dict[str, Any]] = []
        for block in frame.blocks:
            if isinstance(block, TextBlock):
                content_blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.tool_use_id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        if not content_blocks:
            return None
        return {"role": "assistant", "content": content_blocks}
    if frame.role is FrameRole.TOOL:
        content_blocks = []
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock):
                content_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    }
                )
        if not content_blocks:
            return None
        return {"role": "user", "content": content_blocks}
    if frame.role is FrameRole.SYSTEM:
        return None
    return None


def render_for_openai_responses(bundle: PromptFrameBundle) -> dict[str, Any]:
    """Render to OpenAI Responses API format.

    Returns ``{"instructions": str, "input": list[item]}`` where:

    - ``instructions`` is the joined system text.
    - ``input`` is the message+tool item list. ToolUse → ``function_call``,
      ToolResult → ``function_call_output``.
    """
    normalized = normalize_prompt_bundle_for_provider(bundle)
    static_text = _render_static_system_text(normalized.system_static_blocks)
    dynamic_text = _render_dynamic_system_text(normalized.system_dynamic_blocks)
    instructions = "\n\n".join(p for p in (static_text, dynamic_text) if p)

    items: list[dict[str, Any]] = []

    for frame in normalized.frames:
        items.extend(_frame_to_openai_items(frame))
    items = _merge_adjacent_openai_message_items(items)
    validate_canonical_chain(items, provider="openai_responses")

    return {"instructions": instructions, "input": items}


def _merge_adjacent_openai_message_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        if (
            merged
            and item.get("type") == "message"
            and merged[-1].get("type") == "message"
            and item.get("role") == merged[-1].get("role")
        ):
            content = _merge_openai_message_content(merged[-1].get("content"), item.get("content"))
            if content is not None:
                merged[-1] = {**merged[-1], "content": content}
                continue
        merged.append(item)
    return merged


def _merge_openai_message_content(left: Any, right: Any) -> Any | None:
    if isinstance(left, str) and isinstance(right, str):
        return "\n\n".join(part for part in (left, right) if part)
    if isinstance(left, list) and isinstance(right, list):
        return [*left, *right]
    if isinstance(left, str) and isinstance(right, list):
        return [{"type": "input_text", "text": left}, *right] if left else list(right)
    if isinstance(left, list) and isinstance(right, str):
        return [*left, {"type": "input_text", "text": right}] if right else list(left)
    return None


def render_openai_responses_instructions(bundle: PromptFrameBundle) -> str:
    """Render only the OpenAI Responses system instructions for a bundle.

    Native provider turns use ``messages`` as the canonical replay stream. This
    helper lets providers keep deriving system text from PromptFrameBundle
    without also rendering history frames into a second input chain.
    """

    normalized = normalize_prompt_bundle_for_provider(bundle)
    static_text = _render_static_system_text(normalized.system_static_blocks)
    dynamic_text = _render_dynamic_system_text(normalized.system_dynamic_blocks)
    return "\n\n".join(p for p in (static_text, dynamic_text) if p)


def _frame_to_openai_items(frame: Frame) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if frame.role is FrameRole.USER:
        text = _render_user_frame_to_text(frame)
        if text:
            items.append({"type": "message", "role": "user", "content": text})
        return items
    if frame.role is FrameRole.META_USER:
        wrapped = ensure_system_reminder_wrap(frame)
        text = _render_meta_user_frame_to_text(wrapped)
        if text:
            items.append({"type": "message", "role": "user", "content": text})
        return items
    if frame.role is FrameRole.ASSISTANT:
        # Walk blocks in original order so interleaved text + tool_use stays
        # interleaved in the Responses items stream. The previous implementation
        # concatenated all TextBlocks into a single leading "message" and emitted
        # function_calls afterwards, dropping the "I'm thinking about X, calling
        # tool 1, then text B explains why tool 2" intent. Consecutive TextBlocks
        # still merge into one "message" item so we don't fragment an unbroken
        # narration.
        pending_text: list[str] = []

        def _flush_pending_text() -> None:
            if pending_text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "\n".join(pending_text),
                    }
                )
                pending_text.clear()

        for block in frame.blocks:
            if isinstance(block, TextBlock):
                pending_text.append(block.text)
            elif isinstance(block, ToolUseBlock):
                _flush_pending_text()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.tool_use_id,
                        "name": block.name,
                        "arguments": _maybe_json(block.input),
                    }
                )
        _flush_pending_text()
        return items
    if frame.role is FrameRole.TOOL:
        for block in frame.blocks:
            if isinstance(block, ToolResultBlock):
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": block.content,
                    }
                )
        return items
    return items


def _maybe_json(input_args: dict[str, Any]) -> str:
    import json

    try:
        return json.dumps(input_args, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"_repr": repr(input_args)})


# ----- Split helper for the boundary marker -----


def split_at_dynamic_boundary(rendered_prompt: str) -> tuple[str, str, bool]:
    """Split a rendered prompt string at ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY``.

    Returns ``(static_prefix, dynamic_suffix, boundary_present)``. If
    the boundary is absent, returns the whole string as static_prefix
    and an empty dynamic_suffix.
    """
    if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in rendered_prompt:
        static_prefix, _, dynamic_suffix = rendered_prompt.partition(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        return static_prefix.rstrip("\n"), dynamic_suffix.lstrip("\n"), True
    return rendered_prompt, "", False


__all__ = [
    "ensure_system_reminder_wrap",
    "normalize_prompt_bundle_for_provider",
    "render_for_anthropic",
    "render_for_openai_responses",
    "render_openai_responses_instructions",
    "smoosh_system_reminder_siblings",
    "split_at_dynamic_boundary",
    "wrap_system_reminder",
]
