"""Deterministic hygiene helpers for markdown memory files."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

HYGIENE_SETTINGS = "hygiene.json"

_DEFAULT_ENTRY_MAX_BYTES = 2048
_DEFAULT_ENTRY_MAX_LINES = 30
_DEFAULT_MEMORY_FILE_MAX_BYTES = 256 * 1024
_DEFAULT_MEMORY_FILE_MAX_LINES = 4000
_DEFAULT_AUDIT_ROTATE_BYTES = 5 * 1024 * 1024
_DEFAULT_AUDIT_ROTATE_KEEP = 5
_DEFAULT_DEDUP_WORD_OVERLAP = 0.92

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9']*")
_BULLET_RE = re.compile(r"^(?:[-*]\s+|\d+\.\s+)")

# Full-entry matches only: incidental greeting words inside useful memories
# must not discard durable facts or feedback. This guard belongs to semantic
# memory writes, never general markdown documents or existing retained files.
_EPHEMERAL_CONTENT_RE = re.compile(
    r"(?:(?:user (?:said|says) )?"
    r"(?:hi|hello|hey|good morning|good afternoon|good evening|goodnight|good night|"
    r"goodbye|bye|thanks|thank you|sounds good|okay|ok)"
    r"|(?:user thinks )?viola is (?:awesome|great|amazing|helpful)"
    r"|user is feeling (?:happy|sad|tired|good|bad) (?:today|right now))"
)


# A memory states something lasting about the user. These shapes state something
# else: a report of one turn, or the runtime's own text echoed back. Build 9's
# context.md held "User asked a simple arithmetic question: 2 + 2", "System
# reminder indicates..." and raw session ids. This judges stored memory text
# only; it never decides what a user's request does.
_TURN_REPORT_RE = re.compile(
    r"^(?:the\s+)?(?:user|they|he|she)\s+"
    r"(?:just\s+)?(?:asked|wanted|requested|inquired|said|told|was\s+asking|is\s+asking|wants\s+to\s+know)\b"
    r"|^(?:the\s+)?(?:assistant|viola|agent)\s+(?:said|answered|replied|responded|told|played|asked)\b",
    re.IGNORECASE,
)
_LASTING_FACT_RE = re.compile(
    r"\b(?:prefers?|always|never|every|favou?rite|allerg\w*|call\s+(?:me|him|her|them)|likes?|loves?|hates?|dislikes?)\b",
    re.IGNORECASE,
)
_RUNTIME_TEXT_RE = re.compile(
    r"system[-\s]reminder|runtime[-\s]context|tool[-\s]permission\s+level"
    r"|\b(?:session|task|trace|request|conversation)[-_\s]?id\b"
    r"|\bchannel(?:_type)?\b.{0,24}\b(?:web|voice|written|spoken)\b"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


# A copy of the moment, not a fact about the user: the runtime state block that
# is rebuilt for every command ("CURRENT SYSTEM STATE", "Date:", "Playback:",
# "Timers:") and sentences made from it ("Current time is 9:14 PM", "Now playing:
# ..."). Build 10's cleanup knew turn reports and runtime ids only, so about 148
# of these stayed in the Context topic on the laptop.
_STATE_SNAPSHOT_RE = re.compile(
    r"current\s+system\s+state"
    r"|^(?:the\s+)?current(?:ly)?\s+(?:local\s+)?(?:time|date|day|volume|weather|temperature|track|song|playback|"
    r"music|provider|source|mode|state|status|queue|timers?)\b"
    r"|^(?:it\s+is\s+)?(?:now|currently)\s+playing\b"
    r"|^(?:nothing|music|playback)\s+is\s+(?:currently\s+|now\s+)?(?:playing|paused|stopped)\b"
    r"|^(?:date|playback|timers?|volume|speaking\s+with|user\s+preferences|custom\s+shortcuts|now\s+playing)\s*:"
    r"|\bcurrent\s+(?:local\s+)?(?:time|date)\s+(?:is|was)\b",
    re.IGNORECASE,
)
# A log of one file action ("Wrote notes.txt to the Desktop"), saved as a fact.
_FILE_ACTION_VERB_RE = re.compile(
    r"\b(?:created?|wrote|written|writes|saved|deleted?|removed?|moved|renamed?|copied|opened|edited|appended|"
    r"overwrote|overwritten|downloaded|uploaded)\b",
    re.IGNORECASE,
)
_FILE_NAME_RE = re.compile(
    r"\b[a-z]:[\\/]|(?:^|\s)[~.]?/[\w.\-]+/"
    r"|\b[\w\-]+\.(?:txt|md|docx?|pdf|csv|json|py|js|xlsx?|pptx?|png|jpe?g|gif|html?|log|zip|mp3|wav)\b",
    re.IGNORECASE,
)
_LABELLED_ZIP_RE = re.compile(
    r"(\b(?:zip(?:[\s_-]?code)?|postal[\s_-]?code|postcode)\b(?:\s+(?:is|was))?[\s:=]*)\d{5}(?:-\d{4})?\b",
    re.IGNORECASE,
)
# Words that carry no fact of their own. Two entries that are equal once these
# are gone say the same thing ("User's sister is Mara", "The user has a sister
# named Mara"). Verbs of liking stay: they change the meaning.
# fmt: off
_FILLER_WORDS = frozenset({
    "a", "an", "the", "user", "users", "user's", "their", "they", "them", "he", "she", "his", "her", "my",
    "me", "i", "we", "our", "your", "you", "it", "its", "this", "that", "is", "are", "was", "were", "be",
    "been", "am", "has", "have", "had", "named", "name", "called", "of", "to", "in", "on", "at", "for",
    "and", "with", "as",
})
# fmt: on
_NEGATION_WORDS = frozenset({"not", "no", "never", "nor", "neither", "without", "instead", "rather", "but"})


def is_state_snapshot_line(content: str) -> bool:
    """True when a memory line copies the runtime state or the time of one command."""
    text = _BULLET_RE.sub("", (content or "").strip())
    return bool(text) and bool(_STATE_SNAPSHOT_RE.search(text))


def is_file_action_line(content: str) -> bool:
    """True when a memory line is a log of one file action, not a lasting fact."""
    text = _BULLET_RE.sub("", (content or "").strip())
    if not text or _LASTING_FACT_RE.search(text):
        return False
    return bool(_FILE_ACTION_VERB_RE.search(text)) and bool(_FILE_NAME_RE.search(text))


def mask_labelled_zip(text: str) -> str:
    """Mask a ZIP code that is named as one; the rest of the line stays."""
    return _LABELLED_ZIP_RE.sub(lambda match: match.group(1) + "[redacted]", text or "")


def memory_fact_key(text: str) -> frozenset[str]:
    """The words of a memory line that carry its fact, for duplicate checks.

    A line with a negation gets no key (an empty set): the key ignores word
    order, and "likes tea, not coffee" must never count as a repeat of "likes
    coffee, not tea". Such lines are still compared by word overlap.
    """
    words = []
    # The model writes "sister’s" with a curly apostrophe as often as a straight one.
    straight = (text or "").replace("’", "'").replace("‘", "'")
    for word in _WORD_RE.findall(canonical_memory_text(straight)):
        if word in _NEGATION_WORDS or word.endswith("n't"):
            return frozenset()
        word = word.removesuffix("'s").strip("'")
        if word and word not in _FILLER_WORDS:
            words.append(word)
    return frozenset(words)


def same_memory_fact(a: str, b: str) -> bool:
    """True when two lines hold the same fact in different filler words."""
    left = memory_fact_key(a)
    return len(left) >= 2 and left == memory_fact_key(b)


def is_junk_memory_line(content: str) -> bool:
    """True when a memory line reports one turn, one moment, or repeats runtime text."""
    text = _BULLET_RE.sub("", (content or "").strip())
    if not text:
        return False
    if _RUNTIME_TEXT_RE.search(text) or is_state_snapshot_line(text) or is_file_action_line(text):
        return True
    return bool(_TURN_REPORT_RE.match(text)) and not _LASTING_FACT_RE.search(text)


def validate_semantic_content(content: str) -> None:
    """Reject obvious conversational filler without retaining its text."""
    normalized = " ".join(_BULLET_RE.sub("", content.strip()).casefold().split()).strip(".!?")
    if _EPHEMERAL_CONTENT_RE.fullmatch(normalized):
        raise ValueError("Cannot store ephemeral conversation filler in memory")


@dataclass
class MemoryHygienePolicy:
    """Per-account memory hygiene policy loaded from ``memory/hygiene.json``."""

    dedup_enabled: bool = True
    dedup_word_overlap: float = _DEFAULT_DEDUP_WORD_OVERLAP
    entry_max_bytes: int = _DEFAULT_ENTRY_MAX_BYTES
    entry_max_lines: int = _DEFAULT_ENTRY_MAX_LINES
    memory_file_max_bytes: int = _DEFAULT_MEMORY_FILE_MAX_BYTES
    memory_file_max_lines: int = _DEFAULT_MEMORY_FILE_MAX_LINES
    audit_rotate_bytes: int = _DEFAULT_AUDIT_ROTATE_BYTES
    audit_rotate_keep: int = _DEFAULT_AUDIT_ROTATE_KEEP
    empty_topic_cleanup_enabled: bool = True
    topic_slug_normalization_enabled: bool = True
    settings_error: str = ""

    @classmethod
    def load(cls, path: Path) -> MemoryHygienePolicy:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = "Invalid memory hygiene settings at %s: %s" % (path, exc)
            logger.error(message)
            return cls(settings_error=message)
        if not isinstance(raw, dict):
            message = "Invalid memory hygiene settings at %s: expected JSON object" % path
            logger.error(message)
            return cls(settings_error=message)

        policy = cls()
        errors: list[str] = []
        for key, value in raw.items():
            if not hasattr(policy, key) or key == "settings_error":
                errors.append("unknown key %r" % key)
                continue
            current = getattr(policy, key)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    errors.append("%s must be boolean" % key)
                    continue
                setattr(policy, key, value)
                continue
            if isinstance(current, int):
                if not isinstance(value, int):
                    errors.append("%s must be integer" % key)
                    continue
                setattr(policy, key, value)
                continue
            if isinstance(current, float):
                if not isinstance(value, (int, float)):
                    errors.append("%s must be number" % key)
                    continue
                setattr(policy, key, float(value))
                continue

        if policy.dedup_word_overlap <= 0 or policy.dedup_word_overlap > 1:
            errors.append("dedup_word_overlap must be > 0 and <= 1")
            policy.dedup_word_overlap = _DEFAULT_DEDUP_WORD_OVERLAP
        if policy.audit_rotate_keep < 1:
            errors.append("audit_rotate_keep must be >= 1")
            policy.audit_rotate_keep = _DEFAULT_AUDIT_ROTATE_KEEP

        if errors:
            policy.settings_error = "Invalid memory hygiene settings at %s: %s" % (path, "; ".join(errors))
            logger.error(policy.settings_error)
        return policy

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_memory_text(text: str) -> str:
    """Normalize one markdown memory line for deterministic duplicate checks."""

    stripped = _BULLET_RE.sub("", (text or "").strip().lower())
    return " ".join(_WORD_RE.findall(stripped))


def memory_words(text: str) -> set[str]:
    return set(_WORD_RE.findall(canonical_memory_text(text)))


def word_overlap(a: str, b: str) -> float:
    left = memory_words(a)
    right = memory_words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))


def text_bytes(text: str) -> int:
    return len((text or "").encode("utf-8"))


def text_lines(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def validate_entry_content(content: str, policy: MemoryHygienePolicy) -> None:
    byte_count = text_bytes(content)
    line_count = text_lines(content)
    if policy.entry_max_bytes > 0 and byte_count > policy.entry_max_bytes:
        raise ValueError(
            "Memory entry too long: %d bytes exceeds the %d-byte hygiene cap. "
            "Split long notes into smaller memories or workbench files, or adjust memory/%s."
            % (byte_count, policy.entry_max_bytes, HYGIENE_SETTINGS)
        )
    if policy.entry_max_lines > 0 and line_count > policy.entry_max_lines:
        raise ValueError(
            "Memory entry too long: %d lines exceeds the %d-line hygiene cap. "
            "Split long notes into smaller memories or workbench files, or adjust memory/%s."
            % (line_count, policy.entry_max_lines, HYGIENE_SETTINGS)
        )


def validate_file_content(relative_path: str, content: str, policy: MemoryHygienePolicy) -> None:
    byte_count = text_bytes(content)
    line_count = text_lines(content)
    if policy.memory_file_max_bytes > 0 and byte_count > policy.memory_file_max_bytes:
        raise ValueError(
            "Memory file %s would be too large: %d bytes exceeds the %d-byte hygiene cap. "
            "Move detail into another topic or adjust memory/%s."
            % (relative_path, byte_count, policy.memory_file_max_bytes, HYGIENE_SETTINGS)
        )
    if policy.memory_file_max_lines > 0 and line_count > policy.memory_file_max_lines:
        raise ValueError(
            "Memory file %s would be too large: %d lines exceeds the %d-line hygiene cap. "
            "Move detail into another topic or adjust memory/%s."
            % (relative_path, line_count, policy.memory_file_max_lines, HYGIENE_SETTINGS)
        )
