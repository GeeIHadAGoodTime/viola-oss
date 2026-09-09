"""Redaction boundary for browser API-log previews."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core.secrets_mask import mask_dict_secrets, mask_secrets_in_text

_REQUEST_BODY_PREVIEW_CHARS = 200
_RESPONSE_PREVIEW_CHARS = 300


def _redact_api_log_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return mask_dict_secrets(dict(value))
    if isinstance(value, list):
        return [_redact_api_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_api_log_value(item) for item in value)
    if isinstance(value, str):
        return mask_secrets_in_text(value)
    return value


def _preview(value: Any, max_chars: int, *, empty_value: str | None) -> str | None:
    if not value:
        return empty_value
    redacted = _redact_api_log_value(value)
    if isinstance(redacted, (Mapping, list, tuple)):
        text = json.dumps(redacted, default=str, sort_keys=True)
    else:
        text = str(redacted)
    return text[:max_chars]


def format_api_log_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return a browser API-log entry safe for agent/tool-result exposure."""

    return {
        "method": entry.get("method"),
        "url": mask_secrets_in_text(str(entry.get("url", ""))),
        "status": entry.get("status"),
        "request_body": _preview(
            entry.get("request_body"),
            _REQUEST_BODY_PREVIEW_CHARS,
            empty_value=None,
        ),
        "response_preview": _preview(
            entry.get("response_body"),
            _RESPONSE_PREVIEW_CHARS,
            empty_value="",
        ),
        "timestamp": entry.get("timestamp"),
    }
