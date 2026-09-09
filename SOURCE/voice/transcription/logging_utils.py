"""Log-safe helpers for transcription text."""

from __future__ import annotations

import re

_TRANSCRIPT_LOG_PREVIEW_CHARS = 50
_PHONE_NUMBER_LOG_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")


def _mask_phone_numbers(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not digits:
            return "[masked]"
        return "****%s" % digits[-4:]

    return _PHONE_NUMBER_LOG_RE.sub(_replace, text)


def transcript_log_preview(text: str) -> str:
    """Return a short preview of transcript text for logs."""
    text = _mask_phone_numbers(text)
    if len(text) <= _TRANSCRIPT_LOG_PREVIEW_CHARS:
        return text
    return "%s..." % text[:_TRANSCRIPT_LOG_PREVIEW_CHARS]
