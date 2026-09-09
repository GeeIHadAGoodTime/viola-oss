"""Canonical phone-call disclosure text."""

from __future__ import annotations

import re

_SPACE_RE = re.compile(r"\s+")


def expected_disclosure_sentence(
    record_on: bool,
    transcript_on: bool,
    user_display_name: str,
) -> str | None:
    """Return the exact disclosure sentence required for a call."""

    del user_display_name
    if record_on and transcript_on:
        return "This call may be recorded and transcribed."
    if record_on:
        return "This call may be recorded."
    if transcript_on:
        return "This call may be transcribed."
    return None


def _clean_user_display_name(user_display_name: str) -> str:
    return _SPACE_RE.sub(" ", str(user_display_name or "").strip()) or "the user"
