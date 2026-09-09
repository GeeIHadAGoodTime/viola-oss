from __future__ import annotations

import json
from typing import Any

from core.json_types import to_json_value

# Abuse-defense ceilings for untrusted conversation metadata JSON. These bound
# request parsing and database writes; they are not LLM context/token limits.
MAX_CONVERSATION_METADATA_JSON_BYTES = 8 * 1024
MAX_CONVERSATION_METADATA_DEPTH = 12
MAX_CONVERSATION_METADATA_OBJECT_KEYS = 128
MAX_CONVERSATION_METADATA_ARRAY_ITEMS = 512
MAX_CONVERSATION_METADATA_STRING_BYTES = 4096


class ConversationMetadataTooLarge(ValueError):
    def __init__(self, *, field: str, size_bytes: int) -> None:
        self.field = field
        self.size_bytes = int(size_bytes)
        super().__init__("%s exceeds %d bytes" % (field, MAX_CONVERSATION_METADATA_JSON_BYTES))


def _metadata_json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON serializable") from exc


def _validate_metadata_shape(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > MAX_CONVERSATION_METADATA_DEPTH:
        raise ValueError("%s is too deeply nested" % path)
    if isinstance(value, dict):
        if len(value) > MAX_CONVERSATION_METADATA_OBJECT_KEYS:
            raise ValueError("%s has too many object keys" % path)
        for key, item in value.items():
            key_text = str(key)
            if not key_text:
                raise ValueError("%s contains an empty object key" % path)
            if "\x00" in key_text:
                raise ValueError("%s key contains a disallowed NUL byte" % path)
            _validate_metadata_shape(item, path="%s.%s" % (path, key_text), depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_CONVERSATION_METADATA_ARRAY_ITEMS:
            raise ValueError("%s has too many list items" % path)
        for index, item in enumerate(value):
            _validate_metadata_shape(item, path="%s[%d]" % (path, index), depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_CONVERSATION_METADATA_STRING_BYTES:
            raise ValueError("%s string is too large" % path)
        if "\x00" in value:
            raise ValueError("%s string contains a disallowed NUL byte" % path)


def normalize_conversation_metadata(value: dict[str, Any] | None, *, field: str = "metadata") -> dict[str, Any]:
    normalized = to_json_value(value or {})
    if not isinstance(normalized, dict):
        raise ValueError("%s must be a JSON object" % field)
    size_bytes = _metadata_json_size(normalized)
    if size_bytes > MAX_CONVERSATION_METADATA_JSON_BYTES:
        raise ConversationMetadataTooLarge(field=field, size_bytes=size_bytes)
    _validate_metadata_shape(normalized, path=field)
    return normalized


__all__ = [
    "MAX_CONVERSATION_METADATA_JSON_BYTES",
    "ConversationMetadataTooLarge",
    "normalize_conversation_metadata",
]
