"""Phone-call opening disclosure helpers."""

from __future__ import annotations

import re

from telephony.disclosure_text import expected_disclosure_sentence

PHONE_CALL_RETENTION_DAYS = 30

_SPACE_RE = re.compile(r"\s+")
_PERSISTENCE_OBJECTION_PHRASES = (
    "stop recording",
    "stop the recording",
    "dont record",
    "don't record",
    "do not record",
    "do not record me",
    "don't record me",
    "no recording",
    "stop transcribing",
    "stop the transcription",
    "dont transcribe",
    "don't transcribe",
    "do not transcribe",
    "do not transcribe me",
    "no transcription",
    "no transcript",
    "i don't consent",
    "i dont consent",
    "i do not consent",
    "i don't agree to being recorded",
    "i do not agree to being recorded",
)
_CALLED_PARTY_OPT_OUT_PHRASES = (
    "do not call me again",
    "don't call me again",
    "dont call me again",
    "do not call this number",
    "don't call this number",
    "dont call this number",
    "remove me from your list",
    "take me off your list",
    "take this number off your list",
    "do not contact me",
    "don't contact me",
    "dont contact me",
    "stop calling",
    "stop contacting",
    "no more calls",
    "never call again",
)


def normalize_call_text(text: str) -> str:
    """Normalize spoken text for conservative substring checks."""
    return _SPACE_RE.sub(" ", str(text or "").replace("\u2019", "'")).strip().lower()


def build_call_identity_sentence(caller_name: str, *, announce_ai_on_calls: bool) -> str:
    caller = _clean_caller_name(caller_name)
    if announce_ai_on_calls:
        return "Hi, this is Viola, %s's automated assistant." % caller
    return "Hi, this is Viola calling for %s." % caller


def build_call_records_disclosure(
    caller_name: str,
    *,
    recording_enabled: bool,
    transcript_retention_enabled: bool,
) -> str:
    return (
        expected_disclosure_sentence(
            recording_enabled,
            transcript_retention_enabled,
            caller_name,
        )
        or ""
    )


def detect_persistence_objection(text: str) -> bool:
    normalized = normalize_call_text(text)
    return any(phrase in normalized for phrase in _PERSISTENCE_OBJECTION_PHRASES)


def detect_called_party_opt_out(text: str) -> bool:
    normalized = normalize_call_text(text)
    return any(phrase in normalized for phrase in _CALLED_PARTY_OPT_OUT_PHRASES)


def _clean_caller_name(caller_name: str) -> str:
    return _SPACE_RE.sub(" ", str(caller_name or "").strip()) or "the user"
