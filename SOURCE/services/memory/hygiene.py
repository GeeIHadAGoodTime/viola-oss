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
