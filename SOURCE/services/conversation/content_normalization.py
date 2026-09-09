"""Secret and media normalization for canonical conversation content."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

# F-034 (R3-A): the canonical content_normalization layer separates
# provider-bound rendering from storage/diagnostic rendering. Generic
# secret masking (``mask_secrets_in_text``) mutates user/tool text that
# the model has to read verbatim — auth headers, URL credentials, exact
# diagnostic strings — so it stays OFF the provider boundary. Curated
# high-confidence spans (``redact_secret_spans_in_text``) are still
# stripped on both lanes because those are real secrets (API keys,
# OAuth tokens) that should never reach the model regardless. The
# storage/diagnostic lane layers ``mask_secrets_in_text`` on top via
# ``intent.log_redaction.redact_diagnostic_payload`` which already
# threads through ``redact_card_data`` and friends.
#
# The G21 parity gate bans ``mask_secrets_in_text`` from being imported
# here so a future change can't re-introduce generic masking on the
# provider boundary by accident.
from core.secrets_mask import redact_secret_spans_in_text, scan_secrets_in_text
from intent.log_redaction import redact_diagnostic_payload
from services.conversation.context_frames import (
    ContentBlock,
    Frame,
    SystemReminderBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024
API_MAX_MEDIA_PER_REQUEST = 100
_BASE64_MEDIA_RE = re.compile(
    r"data:(?P<mime>image/[a-z0-9.+-]+|application/pdf|audio/[a-z0-9.+-]+);base64," r"(?P<data>[A-Za-z0-9+/=_-]{16,})",
    re.IGNORECASE,
)
_BASE64_KEY_PARTS = frozenset(
    {
        "base64",
        "imagebase64",
        "imageb64",
        "screenshotb64",
        "screenshotbase64",
        "audio_base64",
        "document_base64",
    }
)
_REQUEST_TOO_LARGE_MARKERS = (
    "request too large",
    "maximum request size",
    "payload too large",
)
_IMAGE_TOO_LARGE_MARKERS = (
    "image was too large",
    "image base64 size",
    "image too large",
)
_DOCUMENT_ERROR_MARKERS = (
    "pdf too large",
    "pdf is password protected",
    "pdf file was not valid",
    "the pdf file was not valid",
)
_SYNTHETIC_ERROR_KEYS = (
    "synthetic_api_error",
    "is_synthetic_api_error",
    "_synthetic_api_error",
)
_SECRET_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "auth_key",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "token",
        "access_token",
    }
)
_PROVIDER_INTERNAL_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "tool_reference"})


@dataclass(frozen=True)
class SecretScanResult:
    redacted_text: str
    labels: tuple[str, ...]
    found_secret: bool = False


@dataclass(frozen=True)
class MediaLimitPolicy:
    max_images: int
    max_bytes: int
    allow_pdf: bool = False
    allow_audio: bool = False


DEFAULT_MEDIA_LIMIT_POLICY = MediaLimitPolicy(
    max_images=API_MAX_MEDIA_PER_REQUEST,
    max_bytes=API_IMAGE_MAX_BASE64_SIZE,
)
STORAGE_MEDIA_LIMIT_POLICY = MediaLimitPolicy(max_images=0, max_bytes=0)


def scan_and_redact_text(text: str, *, diagnostic: bool = False) -> SecretScanResult:
    """Return text with high-confidence secrets (and inline media) redacted.

    F-034 (R3-A): provider-bound normalization (``diagnostic=False``) only
    strips CURATED high-confidence secret spans (``redact_secret_spans_in_text``)
    plus inline media. Generic ``api_key=...``/``token=...`` masking is a
    storage/diagnostic concern (handled by ``redact_diagnostic_payload``)
    and is NOT applied to model-bound text — Claude's provider boundary
    (``src/utils/messages.ts:731-760``, ``src/services/api/claude.ts:1265-1301``)
    repairs shape and pairs tool_result envelopes but does not mutate user
    or tool-result text with a blanket secret mask. Mutating live model
    input breaks tasks that require using a user-provided header, URL
    credential, or exact diagnostic output, and changes provider cache
    keys vs Claude.
    """

    if not text:
        return SecretScanResult(redacted_text=text, labels=(), found_secret=False)
    matches = scan_secrets_in_text(text)
    redacted = redact_secret_spans_in_text(text)
    found_secret = bool(matches) or redacted != text
    if diagnostic:
        # Storage/log/trace mode layers the generic mask (via
        # ``redact_diagnostic_payload`` which threads through
        # ``mask_secrets_in_text`` + ``redact_card_data``).
        redacted = str(redact_diagnostic_payload(redacted))
    redacted = _redact_inline_base64_media(redacted)
    labels = tuple(match.label for match in matches)
    return SecretScanResult(
        redacted_text=redacted,
        labels=labels,
        found_secret=found_secret,
    )


def normalize_frame_for_provider(frame: Frame, provider_caps: Any = None) -> Frame:
    """Normalize secrets and unsupported media before provider rendering."""

    return _normalize_frame(frame, _policy_from_caps(provider_caps), for_storage=False)


def normalize_frame_for_storage(frame: Frame) -> Frame:
    """Normalize secrets and raw media before durable diagnostic storage."""

    return _normalize_frame(frame, STORAGE_MEDIA_LIMIT_POLICY, for_storage=True)


def normalize_tool_result_frame(frame: Frame) -> Frame:
    """Normalize a tool-result frame without changing pairing metadata."""

    return _normalize_frame(frame, STORAGE_MEDIA_LIMIT_POLICY, for_storage=True)


def normalize_native_messages_for_provider(
    messages: Sequence[dict[str, Any]],
    provider_caps: Any = None,
) -> list[dict[str, Any]]:
    """Normalize native provider messages without losing tool-pair structure."""

    policy = _policy_from_caps(provider_caps)
    recovered = recover_request_too_large_media(messages)
    limited = strip_excess_media_items(recovered, policy)
    normalized: list[dict[str, Any]] = []
    for message in limited:
        normalized_message = _normalize_message(copy.deepcopy(message), policy=policy, for_storage=False)
        if _message_has_content(normalized_message):
            normalized.append(normalized_message)
    return normalized


def normalize_native_messages_for_storage(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize native messages for logs/traces/compact summaries."""

    normalized: list[dict[str, Any]] = []
    for message in messages:
        normalized_message = _normalize_message(
            copy.deepcopy(message),
            policy=STORAGE_MEDIA_LIMIT_POLICY,
            for_storage=True,
        )
        if _message_has_content(normalized_message):
            normalized.append(normalized_message)
    return normalized


def recover_request_too_large_media(messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip only the media type implicated by a preceding API-size error."""

    recovered = [copy.deepcopy(message) for message in messages]
    for index, message in enumerate(recovered):
        block_types = _error_block_types(message)
        if not block_types:
            continue
        target_index = _nearest_preceding_meta_user_index(recovered, index)
        if target_index is None:
            continue
        recovered[target_index] = _strip_media_types_from_message(recovered[target_index], block_types)
    return recovered


def strip_excess_media_items(
    messages: Sequence[dict[str, Any]],
    policy: MediaLimitPolicy | None = None,
) -> list[dict[str, Any]]:
    """Strip oldest image/document/audio blocks until the media count fits."""

    active_policy = policy or DEFAULT_MEDIA_LIMIT_POLICY
    limit = max(0, int(active_policy.max_images))
    copied = [copy.deepcopy(message) for message in messages]
    media_count = sum(_count_media_in_content(message.get("content")) for message in copied)
    to_remove = media_count - limit
    if to_remove <= 0:
        return copied

    stripped: list[dict[str, Any]] = []
    for message in copied:
        if to_remove <= 0:
            stripped.append(message)
            continue
        content = message.get("content")
        if isinstance(content, list):
            new_content, to_remove = _strip_oldest_media_from_content(content, to_remove)
            if new_content:
                message["content"] = new_content
                stripped.append(message)
            continue
        stripped.append(message)
    return stripped


def _normalize_frame(frame: Frame, policy: MediaLimitPolicy, *, for_storage: bool) -> Frame:
    changed = False
    blocks: list[ContentBlock] = []
    for block in frame.blocks:
        normalized = _normalize_block(block, policy=policy, for_storage=for_storage)
        blocks.append(normalized)
        changed = changed or normalized != block
    if not changed:
        return frame
    return replace(frame, blocks=tuple(blocks))


def _normalize_block(block: ContentBlock, *, policy: MediaLimitPolicy, for_storage: bool) -> ContentBlock:
    if isinstance(block, TextBlock):
        redacted = _redact_text(block.text, for_storage=for_storage)
        return block if redacted == block.text else TextBlock(text=redacted)
    if isinstance(block, SystemReminderBlock):
        redacted = _redact_text(block.text, for_storage=for_storage)
        return block if redacted == block.text else SystemReminderBlock(text=redacted, source_tag=block.source_tag)
    if isinstance(block, ToolUseBlock):
        normalized_input = _normalize_value(block.input, policy=policy, for_storage=for_storage)
        return (
            block if normalized_input == block.input else ToolUseBlock(block.tool_use_id, block.name, normalized_input)
        )
    if isinstance(block, ToolResultBlock):
        normalized_content = _normalize_tool_result_content(block.content, policy=policy, for_storage=for_storage)
        if normalized_content == block.content:
            return block
        return ToolResultBlock(
            tool_use_id=block.tool_use_id,
            tool_name=block.tool_name,
            content=normalized_content,
            is_error=block.is_error,
        )
    return block


def _normalize_tool_result_content(content: str, *, policy: MediaLimitPolicy, for_storage: bool) -> str:
    stripped = str(content or "")
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _redact_text(stripped, for_storage=for_storage)
    normalized = _normalize_value(parsed, policy=policy, for_storage=for_storage)
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return _redact_text(stripped, for_storage=for_storage)


def _normalize_message(message: dict[str, Any], *, policy: MediaLimitPolicy, for_storage: bool) -> dict[str, Any]:
    for key, value in list(message.items()):
        if key == "content":
            if isinstance(value, list):
                message[key] = _normalize_content_list(value, policy=policy, for_storage=for_storage)
            elif isinstance(value, str):
                message[key] = _redact_text(value, for_storage=for_storage)
            else:
                message[key] = _normalize_value(value, policy=policy, for_storage=for_storage)
        else:
            key_text = str(key)
            if _is_base64_key(key_text) and isinstance(value, str):
                message[key] = "[REDACTED:BASE64_MEDIA]"
            elif _is_secret_key(key_text) and isinstance(value, str):
                message[key] = "[REDACTED:SECRET_FIELD]" if value else value
            else:
                message[key] = _normalize_value(value, policy=policy, for_storage=for_storage)
    return message


def _normalize_content_list(
    content: Sequence[Any],
    *,
    policy: MediaLimitPolicy,
    for_storage: bool,
) -> list[Any]:
    normalized: list[Any] = []
    for block in content:
        if isinstance(block, Mapping) and _is_provider_internal_block(block):
            continue
        if isinstance(block, Mapping) and _is_media_block(block):
            replacement = _normalize_media_block(dict(block), policy=policy, for_storage=for_storage)
            if replacement is not None:
                normalized.append(replacement)
            continue
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            copied = dict(block)
            nested = copied.get("content")
            if bool(copied.get("is_error")) and isinstance(nested, list):
                copied["content"] = _normalize_error_tool_result_content(
                    nested,
                    for_storage=for_storage,
                )
            elif isinstance(nested, list):
                copied["content"] = _normalize_content_list(nested, policy=policy, for_storage=for_storage)
            elif isinstance(nested, str):
                copied["content"] = _redact_text(nested, for_storage=for_storage)
            else:
                copied["content"] = _normalize_value(nested, policy=policy, for_storage=for_storage)
            normalized.append(_normalize_value(copied, policy=policy, for_storage=for_storage))
            continue
        normalized.append(_normalize_value(block, policy=policy, for_storage=for_storage))
    return normalized


def _normalize_error_tool_result_content(
    content: Sequence[Any],
    *,
    for_storage: bool,
) -> list[Any]:
    normalized: list[Any] = []
    for block in content:
        if isinstance(block, str):
            text = _redact_text(block, for_storage=for_storage)
            if text:
                normalized.append({"type": "text", "text": text})
            continue
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type in {"text", "input_text"}:
            text = _redact_text(str(block.get("text") or ""), for_storage=for_storage)
            if text:
                normalized.append({"type": "text", "text": text})
    if normalized:
        return normalized
    return [{"type": "text", "text": "[non-text error content omitted]"}]


def _normalize_value(value: Any, *, policy: MediaLimitPolicy, for_storage: bool) -> Any:
    if isinstance(value, str):
        return _redact_text(value, for_storage=for_storage)
    if isinstance(value, Mapping):
        if _is_provider_internal_block(value):
            return {}
        if _is_media_block(value):
            return _normalize_media_block(dict(value), policy=policy, for_storage=for_storage)
        normalized: dict[Any, Any] = {}
        for key, inner in value.items():
            key_text = str(key)
            if _is_base64_key(key_text) and isinstance(inner, str):
                normalized[key] = "[REDACTED:BASE64_MEDIA]"
            elif _is_secret_key(key_text) and isinstance(inner, str):
                normalized[key] = "[REDACTED:SECRET_FIELD]" if inner else inner
            else:
                normalized[key] = _normalize_value(inner, policy=policy, for_storage=for_storage)
        return normalized
    if isinstance(value, list):
        return [_normalize_value(item, policy=policy, for_storage=for_storage) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_value(item, policy=policy, for_storage=for_storage) for item in value)
    if isinstance(value, set):
        return {_normalize_value(item, policy=policy, for_storage=for_storage) for item in value}
    return value


def _normalize_media_block(
    block: dict[str, Any],
    *,
    policy: MediaLimitPolicy,
    for_storage: bool,
) -> dict[str, Any] | None:
    media_type = _media_kind(block)
    if for_storage:
        return _media_text_block(media_type, "removed before storage")
    if media_type == "document" and not policy.allow_pdf:
        return _media_text_block(media_type, "unsupported by provider")
    if media_type == "audio" and not policy.allow_audio:
        return _media_text_block(media_type, "unsupported by provider")
    media_size = _media_payload_size(block)
    if policy.max_bytes >= 0 and media_size > policy.max_bytes:
        return _media_text_block(media_type, "exceeds provider byte limit")
    return copy.deepcopy(block)


def _strip_oldest_media_from_content(content: Sequence[Any], to_remove: int) -> tuple[list[Any], int]:
    stripped: list[Any] = []
    for block in content:
        if to_remove <= 0:
            stripped.append(block)
            continue
        if isinstance(block, Mapping) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            nested, to_remove = _strip_oldest_media_from_content(block["content"], to_remove)
            copied = dict(block)
            copied["content"] = nested
            stripped.append(copied)
            continue
        if isinstance(block, Mapping) and _is_media_block(block):
            to_remove -= 1
            continue
        stripped.append(block)
    return stripped, to_remove


def _strip_media_types_from_message(message: dict[str, Any], block_types: set[str]) -> dict[str, Any]:
    copied = copy.deepcopy(message)
    content = copied.get("content")
    if not isinstance(content, list):
        return copied
    stripped = _strip_media_types_from_content(content, block_types)
    copied["content"] = stripped
    return copied


def _strip_media_types_from_content(content: Sequence[Any], block_types: set[str]) -> list[Any]:
    stripped: list[Any] = []
    for block in content:
        if isinstance(block, Mapping) and _is_media_block(block) and _media_kind(block) in block_types:
            continue
        if isinstance(block, Mapping) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            copied = dict(block)
            copied["content"] = _strip_media_types_from_content(block["content"], block_types)
            stripped.append(copied)
            continue
        stripped.append(block)
    return stripped


def _nearest_preceding_meta_user_index(messages: Sequence[dict[str, Any]], index: int) -> int | None:
    for candidate_index in range(index - 1, -1, -1):
        candidate = messages[candidate_index]
        if _is_meta_user_message(candidate):
            return candidate_index
        if _is_synthetic_error_message(candidate):
            continue
        return None
    return None


def _error_block_types(message: Mapping[str, Any]) -> set[str]:
    if not _is_synthetic_error_message(message):
        return set()
    text = _message_text(message).lower()
    if any(marker in text for marker in _REQUEST_TOO_LARGE_MARKERS):
        return {"image", "document"}
    if any(marker in text for marker in _IMAGE_TOO_LARGE_MARKERS):
        return {"image"}
    if any(marker in text for marker in _DOCUMENT_ERROR_MARKERS):
        return {"document"}
    return set()


def _is_synthetic_error_message(message: Mapping[str, Any]) -> bool:
    if any(bool(message.get(key)) for key in _SYNTHETIC_ERROR_KEYS):
        return True
    metadata = message.get("metadata")
    if isinstance(metadata, Mapping) and any(bool(metadata.get(key)) for key in _SYNTHETIC_ERROR_KEYS):
        return True
    return str(message.get("origin") or "").strip().lower() in {"synthetic_api_error", "api_error"}


def _is_meta_user_message(message: Mapping[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    if bool(message.get("is_meta") or message.get("isMeta")):
        return True
    metadata = message.get("metadata")
    return isinstance(metadata, Mapping) and bool(metadata.get("is_meta") or metadata.get("isMeta"))


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _message_has_content(message: Mapping[str, Any]) -> bool:
    if "content" not in message:
        return True
    content = message.get("content")
    if content is None or content == "" or content == []:
        return False
    return True


def _count_media_in_content(content: Any) -> int:
    if not isinstance(content, list):
        return 0
    count = 0
    for block in content:
        if isinstance(block, Mapping) and _is_media_block(block):
            count += 1
        if isinstance(block, Mapping) and block.get("type") == "tool_result":
            count += _count_media_in_content(block.get("content"))
    return count


def _is_media_block(block: Mapping[str, Any]) -> bool:
    return _media_kind(block) in {"image", "document", "audio"}


def _is_provider_internal_block(block: Mapping[str, Any]) -> bool:
    return str(block.get("type") or "").strip().lower() in _PROVIDER_INTERNAL_BLOCK_TYPES


def _media_kind(block: Mapping[str, Any]) -> str:
    block_type = str(block.get("type") or "").strip().lower()
    if block_type in {"image", "input_image"}:
        return "image"
    if block_type in {"document", "file"}:
        return "document"
    if block_type in {"audio", "input_audio"}:
        return "audio"
    return ""


def _media_payload_size(block: Mapping[str, Any]) -> int:
    source = block.get("source")
    if isinstance(source, Mapping):
        data = source.get("data")
        if isinstance(data, str):
            return len(data)
    image_url = block.get("image_url")
    if isinstance(image_url, str):
        return _data_uri_payload_size(image_url)
    if isinstance(image_url, Mapping):
        url = image_url.get("url")
        if isinstance(url, str):
            return _data_uri_payload_size(url)
    data = block.get("data")
    if isinstance(data, str):
        return len(data)
    return 0


def _data_uri_payload_size(value: str) -> int:
    if ";base64," not in value:
        return 0
    return len(value.rsplit(",", 1)[-1])


def _media_text_block(media_type: str, reason: str) -> dict[str, str]:
    label = media_type.upper() if media_type else "MEDIA"
    return {
        "type": "text",
        "text": "[MEDIA_STRIPPED:%s:%s]" % (label, reason),
    }


def _redact_inline_base64_media(text: str) -> str:
    return _BASE64_MEDIA_RE.sub(
        lambda match: "data:%s;base64,[REDACTED:BASE64_MEDIA]" % match.group("mime"),
        text,
    )


def _redact_text(text: str, *, for_storage: bool) -> str:
    return scan_and_redact_text(text, diagnostic=for_storage).redacted_text


def _is_base64_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.strip().lower())
    return normalized in _BASE64_KEY_PARTS or normalized.endswith("base64") or normalized.endswith("b64")


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    compact = normalized.replace("_", "")
    for part in _SECRET_KEY_PARTS:
        compact_part = part.replace("_", "")
        if normalized == part or normalized.endswith("_" + part) or compact == compact_part:
            return True
    return False


def _policy_from_caps(provider_caps: Any) -> MediaLimitPolicy:
    if isinstance(provider_caps, MediaLimitPolicy):
        return provider_caps
    caps = _normalized_media_caps(provider_caps)
    if caps is not None:
        return MediaLimitPolicy(
            max_images=_caps_int(
                caps,
                ("max_images", "max_image_count", "max_media_items", "image_limit"),
                API_MAX_MEDIA_PER_REQUEST,
            ),
            max_bytes=_caps_int(
                caps,
                ("max_bytes", "max_media_bytes", "max_input_bytes", "media_byte_limit"),
                API_IMAGE_MAX_BASE64_SIZE,
            ),
            allow_pdf=_caps_bool(caps, ("allow_pdf", "supports_pdf", "pdf_inputs"), False)
            or _caps_media_type(caps, "application/pdf"),
            allow_audio=_caps_bool(caps, ("allow_audio", "supports_audio", "audio_inputs"), False)
            or _caps_media_type(caps, "audio/"),
        )
    return DEFAULT_MEDIA_LIMIT_POLICY


def _normalized_media_caps(provider_caps: Any) -> Mapping[str, Any] | None:
    if provider_caps is None:
        return None
    if isinstance(provider_caps, Mapping):
        caps = dict(provider_caps)
    else:
        caps = {
            name: getattr(provider_caps, name)
            for name in (
                "max_images",
                "max_image_count",
                "max_media_items",
                "max_bytes",
                "max_media_bytes",
                "max_input_bytes",
                "allow_pdf",
                "supports_pdf",
                "pdf_inputs",
                "allow_audio",
                "supports_audio",
                "audio_inputs",
                "supported_media_types",
                "input_media_types",
            )
            if hasattr(provider_caps, name)
        }
    media_caps = caps.get("media") or caps.get("media_capabilities") or caps.get("media_caps")
    if isinstance(media_caps, Mapping):
        caps = {**caps, **dict(media_caps)}
    return caps


def _caps_key(key: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key or "").strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _caps_get(caps: Mapping[str, Any], names: Sequence[str]) -> Any:
    normalized = {_caps_key(str(key)): value for key, value in caps.items()}
    for name in names:
        key = _caps_key(name)
        if key in normalized:
            return normalized[key]
    return None


def _caps_int(caps: Mapping[str, Any], names: Sequence[str], default: int) -> int:
    value = _caps_get(caps, names)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _caps_bool(caps: Mapping[str, Any], names: Sequence[str], default: bool) -> bool:
    value = _caps_get(caps, names)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _caps_media_type(caps: Mapping[str, Any], marker: str) -> bool:
    value = _caps_get(caps, ("supported_media_types", "input_media_types", "media_types"))
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, Sequence):
        candidates = [str(item) for item in value]
    else:
        return False
    marker_lower = marker.lower()
    return any(marker_lower in candidate.lower() for candidate in candidates)


def policy_for_provider(provider_name: Any) -> MediaLimitPolicy:
    """Return the media policy for provider capabilities or a known provider name.

    S9-07: callers that know their provider (anthropic, openai, etc.) but
    don't carry a full caps payload can ask this helper for the right
    policy instead of letting :data:`DEFAULT_MEDIA_LIMIT_POLICY` strip
    PDFs unconditionally. Conservative — providers we haven't verified
    fall through to the default.
    """

    if provider_name is not None and not isinstance(provider_name, str):
        return _policy_from_caps(provider_name)
    name = (provider_name or "").strip().lower()
    if not name:
        return DEFAULT_MEDIA_LIMIT_POLICY
    if name in {"anthropic", "claude", "claude-code", "managed-anthropic"}:
        # Anthropic's API accepts PDF + audio document blocks.
        return MediaLimitPolicy(
            max_images=API_MAX_MEDIA_PER_REQUEST,
            max_bytes=API_IMAGE_MAX_BASE64_SIZE,
            allow_pdf=True,
            allow_audio=True,
        )
    if name in {"openai", "managed", "managed-openai", "codex"}:
        # OpenAI Responses API accepts image inputs. PDFs are emitted
        # via file inputs (a different code path); on the message-content
        # path they are still rejected, so we keep the default policy.
        return MediaLimitPolicy(
            max_images=API_MAX_MEDIA_PER_REQUEST,
            max_bytes=API_IMAGE_MAX_BASE64_SIZE,
            allow_pdf=False,
            allow_audio=False,
        )
    if name in {"google", "gemini"}:
        return MediaLimitPolicy(
            max_images=API_MAX_MEDIA_PER_REQUEST,
            max_bytes=API_IMAGE_MAX_BASE64_SIZE,
            allow_pdf=True,
            allow_audio=True,
        )
    return DEFAULT_MEDIA_LIMIT_POLICY


__all__ = [
    "API_IMAGE_MAX_BASE64_SIZE",
    "API_MAX_MEDIA_PER_REQUEST",
    "DEFAULT_MEDIA_LIMIT_POLICY",
    "STORAGE_MEDIA_LIMIT_POLICY",
    "MediaLimitPolicy",
    "SecretScanResult",
    "normalize_frame_for_provider",
    "normalize_frame_for_storage",
    "normalize_native_messages_for_provider",
    "normalize_native_messages_for_storage",
    "normalize_tool_result_frame",
    "policy_for_provider",
    "recover_request_too_large_media",
    "scan_and_redact_text",
    "strip_excess_media_items",
]
