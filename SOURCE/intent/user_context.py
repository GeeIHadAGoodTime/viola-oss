"""Shared helpers for resolving user-facing context blocks.

The builder, user profile, and learned user-model all use the same
duplicate-detection and rendering rules so prompts stay compact and
authoritative.

CANON: this module does NOT classify user input. Earlier revisions held a
``_LEGAL_TASK_RE`` / ``_PREFILL_TASK_RE`` wordlist that decided which
profile and user-model lines reached the model. That violated
"Viola's runtime must trust the model" — a regex matched "filing" but
missed "paperwork", matched "food" but missed "lunch order", so the
model silently got the wrong context window every time the user phrased
a request outside the author's word list. Deleted 2026-05-30 (R5-P0-D).
Emit every user line every turn; the model picks what's relevant.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

USER_CONTEXT_HEADER = "USER CONTEXT:"


def normalize_context_text(text: str | None) -> str:
    """Return a stable lowercase signature for duplicate detection."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text.lower())).strip()


def line_signature(line: str) -> str:
    """Return a duplicate-detection signature for a rendered context line."""
    cleaned = line.strip().lstrip("-•*").strip()
    if not cleaned:
        return ""
    if ":" in cleaned:
        label, value = cleaned.split(":", 1)
        return "kv|%s|%s" % (normalize_context_text(label), normalize_context_text(value))
    return "text|%s" % normalize_context_text(cleaned)


def merge_context_sections(sections: Sequence[str]) -> str:
    """Merge rendered context sections while removing duplicate lines."""
    seen: set[str] = set()
    rendered: list[str] = []

    for section in sections:
        if not section:
            continue

        section_lines: list[str] = []
        for line in section.splitlines():
            if not line.strip():
                if section_lines and section_lines[-1] != "":
                    section_lines.append("")
                continue

            signature = line_signature(line)
            if signature and signature in seen:
                continue
            if signature:
                seen.add(signature)
            section_lines.append(line)

        if not section_lines:
            continue
        if rendered:
            rendered.append("")
        rendered.extend(section_lines)

    return "\n".join(rendered)


def render_user_context_block(sections: Sequence[str]) -> str:
    """Render user-specific sections under the canonical prompt envelope."""
    merged = merge_context_sections(sections)
    if not merged:
        return ""
    merged = "\n".join(line for line in merged.splitlines() if line.strip() != USER_CONTEXT_HEADER)
    if not merged:
        return ""
    return "%s\n%s" % (USER_CONTEXT_HEADER, merged)
