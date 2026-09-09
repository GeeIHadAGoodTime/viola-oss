"""Prompt-frame composition for Viola LLM interactions."""

from __future__ import annotations

from core.logging_config import get_logger
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    PromptFrameBundle,
    SystemReminderBlock,
    TextBlock,
    text_frame,
)
from services.llm.prompts.viola_unified import VIOLA_UNIFIED_PROMPT

logger = get_logger(__name__)

_DELIVERY_BY_CHANNEL = {
    "voice": "spoken",
    "phone": "spoken",
    "web": "written",
    "http": "written",
    "console": "written",
    "telegram": "written",
    "discord": "written",
    "matrix": "written",
    "slack": "written",
    "signal": "written",
    "whatsapp": "written",
    "sms": "written",
    "email": "written",
}

_JSON_TOOL_RESPONSE_CONTRACT = (
    "For this non-native tool runtime, return exactly one JSON object when a tool is needed.\n"
    'Tool call: {"type": "tool_call", "tool": "<tool_name>", "args": {"param": "value"}}\n'
    'Answer: {"type": "answer", "answer": "<plain response>", "continue_listening": false}\n'
    'Question: {"type": "answer", "answer": "<one focused question>", "continue_listening": true}\n'
    "If your answer asks the user to provide a required value, it is a Question and continue_listening must be true.\n"
    "For requested phone calls, a missing destination number is a question with continue_listening true; ask the user for the phone number to call, in plain words and including the country code.\n"
    'Ignore ambient speech only when clearly not directed at Viola: {"type": "ignore", "reason": "<why>", '
    '"continue_listening": false}\n'
    "Return raw JSON only. No markdown fences, prose preambles, or hidden reasoning."
)


def build_channel_context(channel_type: str | None, *, session_id: str | None = None) -> str:
    """Render the runtime channel block consumed by ``VIOLA_UNIFIED_PROMPT``."""

    raw_channel = str(channel_type or "").strip().lower()
    if not raw_channel or raw_channel == "none":
        return ""
    normalized = "web" if raw_channel == "http" else raw_channel
    delivery = _DELIVERY_BY_CHANNEL.get(normalized, "mixed")

    lines = [
        "<CHANNEL>",
        "channel_type: %s" % normalized,
        "delivery: %s" % delivery,
    ]
    if session_id:
        lines.append("session_id: %s" % str(session_id).strip())
    lines.append("</CHANNEL>")
    return "\n".join(lines)


def _system_text_frame(text: str, *, origin: str) -> Frame | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    return text_frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.SYSTEM,
        text=clean,
        origin=origin,
    )


def _user_text_frame(text: str, *, role: FrameRole, origin: str | None = None) -> Frame | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    return Frame(
        kind=(FrameKind.USER_INPUT if role is FrameRole.USER else FrameKind.ASSISTANT_TEXT),
        role=role,
        blocks=(TextBlock(text=clean),),
        origin=origin,
    )


def _copy_bundle(bundle: PromptFrameBundle | None) -> PromptFrameBundle:
    if bundle is None:
        return PromptFrameBundle()
    kwargs: dict[str, object] = {
        "system_static_blocks": list(bundle.system_static_blocks),
        "system_dynamic_blocks": list(bundle.system_dynamic_blocks),
        "meta_user_frames": list(bundle.meta_user_frames),
        "history_frames": list(bundle.history_frames),
        "current_user_frame": bundle.current_user_frame,
        "cache_boundary_present": bundle.cache_boundary_present,
        "provider_normalized": bundle.provider_normalized,
    }
    if bundle.frames:
        kwargs["frames"] = list(bundle.frames)
    return PromptFrameBundle(**kwargs)


def _bundle_has_origin(bundle: PromptFrameBundle, origin: str) -> bool:
    for frame in (
        bundle.system_static_blocks
        + bundle.system_dynamic_blocks
        + bundle.meta_user_frames
        + bundle.history_frames
        + bundle.frames
    ):
        if frame.origin == origin:
            return True
    current = bundle.current_user_frame
    return current is not None and current.origin == origin


def _bundle_contains_text(bundle: PromptFrameBundle, needle: str) -> bool:
    if not needle:
        return False
    for frame in (
        bundle.system_static_blocks
        + bundle.system_dynamic_blocks
        + bundle.meta_user_frames
        + bundle.history_frames
        + bundle.frames
    ):
        for block in frame.blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str) and needle in text:
                return True
    current = bundle.current_user_frame
    if current is not None:
        for block in current.blocks:
            text = getattr(block, "text", None)
            if isinstance(text, str) and needle in text:
                return True
    return False


def _runtime_meta_frame(text: str, *, origin: str) -> Frame | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=clean, source_tag=origin.replace("_", "-")),),
        is_meta=True,
        origin=origin,
    )


def _active_output_style_frames() -> tuple[Frame | None, Frame | None]:
    """Resolve the active output style and return ``(system_section, meta_frame)``.

    S9-03: We promote the style to a Claude-shaped system-prompt section
    (``# Output Style: <name>``) AND keep the META_USER reminder so any
    downstream consumer that wants the frame metadata can still find it.
    """

    try:
        from services.llm.prompts.output_styles import (
            build_output_style_frame,
            build_output_style_section,
            get_active_output_style,
            load_output_style_registry,
        )
        from ui.settings_manager import get_settings_manager

        style = get_active_output_style(
            get_settings_manager(),
            registry=load_output_style_registry(),
        )
    except Exception as exc:
        logger.debug("Output style resolution skipped: %s", exc)
        return None, None
    if style is None:
        return None, None
    try:
        return build_output_style_section(style), build_output_style_frame(style)
    except Exception as exc:
        logger.debug("Output style frame build failed: %s", exc)
        return None, None


def _active_output_style_frame() -> Frame | None:
    """Backwards-compatible alias returning just the meta-user reminder."""

    _, meta = _active_output_style_frames()
    return meta


def _append_meta_frame(bundle: PromptFrameBundle, frame: Frame) -> None:
    bundle.meta_user_frames.append(frame)
    if bundle.frames:
        bundle.frames.append(frame)


def runtime_context_bundle(text: str, *, origin: str = "runtime") -> PromptFrameBundle:
    """Build a bundle for synthetic runtime context that is not user history."""

    frame = _runtime_meta_frame(text, origin=origin)
    return PromptFrameBundle(
        meta_user_frames=[frame] if frame is not None else [],
        frames=[frame] if frame else [],
    )


def append_system_text(bundle: PromptFrameBundle | None, text: str, *, origin: str) -> PromptFrameBundle:
    """Return a bundle with one additional dynamic system frame."""

    copied = _copy_bundle(bundle)
    frame = _system_text_frame(text, origin=origin)
    if frame is not None:
        copied.system_dynamic_blocks.append(frame)
    return copied


def build_minimal_agent_prompt(
    system_context: str = "",
    schema_text: str = "",
    tool_categories: dict[str, list[str]] | None = None,
    native_tools: bool = False,
    custom_instructions: str = "",
    channel_type: str | None = None,
) -> str:
    """Compatibility wrapper for child-agent system prompt construction."""

    del tool_categories
    parts: list[str] = [VIOLA_UNIFIED_PROMPT]
    context = system_context.strip()
    channel_context = build_channel_context(channel_type)
    if channel_context and "<CHANNEL>" not in context:
        parts.append(channel_context)
    if custom_instructions:
        parts.append("<USER_INSTRUCTIONS>\n%s\n</USER_INSTRUCTIONS>" % custom_instructions.strip())
    if context:
        parts.append(context)
    if schema_text and not native_tools:
        parts.append("TOOL SCHEMAS:\n%s" % schema_text.strip())
        parts.append(_JSON_TOOL_RESPONSE_CONTRACT)
    return "\n\n".join(part for part in parts if part).rstrip()


def build_provider_prompt_bundle(
    *,
    context_bundle: PromptFrameBundle | None = None,
    user_text: str = "",
    schema_text: str = "",
    native_tools: bool = True,
    custom_instructions: str = "",
    channel_type: str | None = None,
    session_id: str | None = None,
    response_contract: str = "",
) -> PromptFrameBundle:
    """Compose the canonical provider prompt as a ``PromptFrameBundle``."""

    bundle = _copy_bundle(context_bundle)
    doctrine = _system_text_frame(VIOLA_UNIFIED_PROMPT, origin="viola_unified")
    if doctrine is not None and not _bundle_contains_text(bundle, VIOLA_UNIFIED_PROMPT[:80]):
        bundle.system_static_blocks.insert(0, doctrine)
        bundle.cache_boundary_present = True

    channel_context = build_channel_context(channel_type, session_id=session_id)
    if channel_context and not _bundle_has_origin(bundle, "channel"):
        frame = _system_text_frame(channel_context, origin="channel")
        if frame is not None:
            bundle.system_dynamic_blocks.append(frame)

    if custom_instructions:
        frame = _system_text_frame(
            "<USER_INSTRUCTIONS>\n%s\n</USER_INSTRUCTIONS>" % custom_instructions.strip(),
            origin="custom_instructions",
        )
        if frame is not None:
            bundle.system_dynamic_blocks.append(frame)

    if not _bundle_has_origin(bundle, "output_style"):
        system_section, meta_frame = _active_output_style_frames()
        if system_section is not None:
            # S9-03: promote to a Claude-shaped system section so it lives
            # under the main prompt rather than as a runtime reminder.
            bundle.system_dynamic_blocks.append(system_section)
        if meta_frame is not None:
            _append_meta_frame(bundle, meta_frame)

    if schema_text and not native_tools:
        for text, origin in (
            ("TOOL SCHEMAS:\n%s" % schema_text.strip(), "tool_schema"),
            (_JSON_TOOL_RESPONSE_CONTRACT, "tool_response_contract"),
        ):
            frame = _system_text_frame(text, origin=origin)
            if frame is not None:
                bundle.system_dynamic_blocks.append(frame)

    if response_contract:
        frame = _system_text_frame(response_contract, origin="response_contract")
        if frame is not None:
            bundle.system_dynamic_blocks.append(frame)

    if user_text:
        bundle.current_user_frame = _user_text_frame(user_text, role=FrameRole.USER)

    ordered_frames = list(bundle.frames)
    if not ordered_frames:
        ordered_frames.extend(bundle.meta_user_frames)
        ordered_frames.extend(bundle.history_frames)
    if bundle.current_user_frame is not None and all(
        frame is not bundle.current_user_frame for frame in ordered_frames
    ):
        ordered_frames.append(bundle.current_user_frame)
    bundle.frames = ordered_frames
    bundle.provider_normalized = False

    legacy_meta = len(bundle.meta_user_frames)
    legacy_history = len(bundle.history_frames)
    legacy_current = bundle.current_user_frame is not None

    # Post-build, the canonical message chain lives exclusively on bundle.frames.
    # Clear the legacy mirror fields so a future caller that mutates them after
    # build_provider_prompt_bundle returns can't silently fork the chain — the
    # rendering pipeline reads bundle.frames and would ignore the mutation. The
    # PromptFrameBundle __post_init__ invariant (G3) still rejects re-constructed
    # bundles that try to set explicit frames=[] alongside non-empty legacy
    # fields, so this cleanup is a one-shot post-build collapse.
    bundle.meta_user_frames = []
    bundle.history_frames = []
    bundle.current_user_frame = None

    logger.debug(
        "build_provider_prompt_bundle: static=%d dynamic=%d frames=%d (legacy_meta=%d, legacy_history=%d, legacy_user=%s)",
        len(bundle.system_static_blocks),
        len(bundle.system_dynamic_blocks),
        len(bundle.frames),
        legacy_meta,
        legacy_history,
        legacy_current,
    )
    return bundle


def build_prompt_frame_bundle(**kwargs: object) -> PromptFrameBundle:
    """Compatibility alias for the canonical post-R2 prompt-frame assembler."""

    return build_provider_prompt_bundle(**kwargs)


__all__ = [
    "append_system_text",
    "build_channel_context",
    "build_minimal_agent_prompt",
    "build_prompt_frame_bundle",
    "build_provider_prompt_bundle",
    "runtime_context_bundle",
]
