"""Canonical frame helpers for voice/audio turns.

Voice remains a Viola-specific product surface, but transcribed turns and
voice runtime events share the same ``Frame`` carrier as typed chat.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    SystemReminderBlock,
    TextBlock,
)


@dataclass(frozen=True)
class VoiceTurnMetadata:
    """Provider-neutral metadata for one transcribed voice turn."""

    source: str
    confidence: float | None = None
    device_id: str | None = None
    wake_event_id: str | None = None
    barge_in: bool = False
    audio_session_id: str | None = None
    channel: str | None = None


@dataclass(frozen=True)
class VoiceTurnContext:
    """Scoped voice-turn context used by canonical frame writers."""

    transcript: str
    metadata: VoiceTurnMetadata
    session_id: str | None = None


_current_voice_turn: contextvars.ContextVar[VoiceTurnContext | None] = contextvars.ContextVar(
    "current_voice_turn",
    default=None,
)


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, numeric))


def _metadata_payload(metadata: VoiceTurnMetadata) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": _clean_text(metadata.source) or "voice",
        "barge_in": bool(metadata.barge_in),
    }
    confidence = _normalized_confidence(metadata.confidence)
    if confidence is not None:
        payload["confidence"] = confidence
    for key in ("device_id", "wake_event_id", "audio_session_id", "channel"):
        value = _clean_text(getattr(metadata, key))
        if value is not None:
            payload[key] = value
    return payload


def _event_metadata_payload(metadata: VoiceTurnMetadata | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(metadata, VoiceTurnMetadata):
        return _metadata_payload(metadata)
    if not isinstance(metadata, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for key, value in metadata.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key_text] = value
    return payload


class VoiceTurnFrameFactory:
    """Build canonical frames for transcribed voice input."""

    @staticmethod
    def from_transcript(text: str, metadata: VoiceTurnMetadata, session_id: str) -> Frame:
        transcript = str(text or "").strip()
        extra = {
            "input_modality": "voice",
            "spoken_text": transcript,
            "text_equivalent": transcript,
            "voice": _metadata_payload(metadata),
        }
        return Frame(
            kind=FrameKind.USER_INPUT,
            role=FrameRole.USER,
            blocks=(TextBlock(text=transcript),),
            origin="voice",
            session_id=(session_id or None),
            extra=extra,
        )


def VoiceEventFrame(
    event_type: str,
    metadata: VoiceTurnMetadata | Mapping[str, Any] | None,
    session_id: str,
) -> Frame:
    """Build a structured meta frame for voice-only runtime events."""

    event_name = _clean_text(event_type) or "voice_event"
    payload = _event_metadata_payload(metadata)
    payload["event_type"] = event_name
    text = "voice_event: %s" % event_name
    reason = _clean_text(payload.get("reason"))
    if reason is not None:
        text = "%s\nreason: %s" % (text, reason)
    return Frame(
        kind=FrameKind.SYSTEM_REMINDER,
        role=FrameRole.META_USER,
        blocks=(SystemReminderBlock(text=text, source_tag="voice-event"),),
        is_meta=True,
        origin="voice_event",
        session_id=(session_id or None),
        extra={"voice_event": payload},
    )


@contextlib.contextmanager
def use_voice_turn_context(
    metadata: VoiceTurnMetadata,
    *,
    transcript: str,
    session_id: str | None = None,
) -> Iterator[VoiceTurnContext]:
    """Publish voice metadata for canonical writes in the current async task."""

    ctx = VoiceTurnContext(
        transcript=str(transcript or "").strip(),
        metadata=metadata,
        session_id=session_id,
    )
    token = _current_voice_turn.set(ctx)
    try:
        yield ctx
    finally:
        _current_voice_turn.reset(token)


def get_current_voice_turn_context() -> VoiceTurnContext | None:
    """Return the active voice-turn context, if any."""

    return _current_voice_turn.get()


def current_voice_turn_frame(text: str, *, session_id: str | None = None) -> Frame | None:
    """Return a voice input frame when ``text`` is the active transcript."""

    ctx = get_current_voice_turn_context()
    if ctx is None:
        return None
    transcript = str(text or "").strip()
    if not transcript or transcript != ctx.transcript:
        return None
    return VoiceTurnFrameFactory.from_transcript(
        transcript,
        ctx.metadata,
        session_id or ctx.session_id or "",
    )


__all__ = [
    "VoiceEventFrame",
    "VoiceTurnContext",
    "VoiceTurnFrameFactory",
    "VoiceTurnMetadata",
    "current_voice_turn_frame",
    "get_current_voice_turn_context",
    "use_voice_turn_context",
]
