"""Standalone Sentry event privacy filter.

This module intentionally does not import ``sentry_sdk`` or the diagnostics
package. The Python Sentry lane can wire :func:`filter_sentry_event` as
``before_send`` later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REDACTED_BINARY = "[REDACTED:BINARY]"
_REDACTED_CYCLE = "[REDACTED:CYCLE]"
_REDACTED_DEVICE = "[REDACTED:DEVICE]"
_REDACTED_MAX_DEPTH = "[REDACTED:MAX_DEPTH]"
_REDACTED_PII = "[REDACTED:PII]"
_REDACTED_REQUEST_BODY = "[REDACTED:REQUEST_BODY]"
_REDACTED_SECRET = "[REDACTED:SECRET]"
_MAX_REDACTION_DEPTH = 80
_SANITIZER_EXCEPTIONS = (
    ArithmeticError,
    AttributeError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
    re.error,
)

_WINDOWS_USER_PATH_RE = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/:\s]+")
_POSIX_USER_PATH_RE = re.compile(r"(?i)(/home/)[^/\s]+")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
_PHONE_RE = re.compile(r"(?<![\w/-])(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?![\w/-])")
_SSN_RE = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_AUTH_VALUE_RE = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_CARD_SHAPE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SUPPORT_SECRET_KEY_VALUE_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|secret|access[_-]?token|refresh[_-]?token|authorization|auth[_-]?token)"
    r"\b[\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]+)([\"']?)"
)

_TIER3_TIER_KEYS = frozenset(
    {
        "data_tier",
        "origin_tier",
        "privacy_tier",
        "storage_tier",
        "tier",
        "viola_data_tier",
        "viola_storage_tier",
    }
)
_TIER3_TIER_VALUES = frozenset(
    {
        "desktop_only",
        "local_only",
        "local_user_only",
        "never_cloud",
        "tier3",
        "tier_3",
    }
)
_TIER3_BOOLEAN_KEYS = frozenset(
    {
        "desktop_only",
        "is_tier3",
        "local_only",
        "never_cloud",
        "tier3",
        "tier_3",
    }
)
_TIER3_CATEGORY_KEYS = frozenset(
    {
        "artifact_category",
        "category",
        "storage_category",
        "sync_category",
        "viola_category",
    }
)
_TIER3_STORAGE_CATEGORIES = frozenset({"browser_artifacts", "diagnostics", "exports", "snapshots"})
_TIER3_ORIGIN_KEYS = frozenset(
    {
        "artifact_origin",
        "component",
        "data_origin",
        "origin",
        "source",
        "source_component",
        "subsystem",
        "surface",
    }
)
_TIER3_PATH_KEYS = frozenset(
    {
        "abs_path",
        "absolute_path",
        "culprit",
        "filename",
        "logger",
        "module",
        "path",
        "pathname",
        "route",
        "source",
        "source_path",
        "transaction",
        "url",
    }
)
_TIER3_VALUE_MARKERS = (
    "agent_audit",
    "api_vault",
    "browser_artifacts",
    "browser_profile",
    "browser_voice_usage",
    "byok",
    "calendar.sqlite",
    "codex_auth",
    "command_ledger",
    "cookie_export",
    "cookies.sqlite",
    "desktop_auth",
    "events.db",
    "gemini_cli_workspace_token",
    "gotrue_tokens",
    "custom_wake_word_model",
    "device_settings",
    "keyring",
    "local_calendar",
    "network/cookies",
    "oauth_tokens",
    "llm_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "/api/payments/cards",
    "/api/v1/browser/cookies",
    "/confirm/",
    "/v1/browser/auth",
    "payment_card",
    "payment_method",
    "payment_vault",
    "phone_call_history",
    "state.sqlite3",
    "state_store_snapshots",
    "task_checkpoints",
    "task_trace",
    "token_vault",
    "trace_decrypt",
    "traces/by_user",
    "user_credentials",
    "vault_master_key",
    "wake_word_model",
)
_TIER3_PATH_MARKERS = (
    "auth/tier3_dsr_registry",
    "diagnostics/operation_trace",
    "diagnostics/step_log_analyzer",
    "diagnostics/wake_decision_trace",
    "intent/agent_audit_log",
    "intent/task_trace",
    "intent/task_trace_reader",
    "mcp_servers/browser/browser_manager",
    "music/consent/token_manager",
    "music/consent/vault",
    "music/spotify/cdp_controller",
    "music/spotify/cookie_bridge",
    "services/calendar/providers/local",
    "services/api_vault/vault",
    "services/browser/task_session",
    "services/credential_boundary",
    "services/oauth/credentials",
    "services/oauth/workspace_bridge",
    "services/payments/ephemeral_payment_secret",
    "services/payments/key_protection",
    "services/payments/payment_vault",
    "services/settings/credential_vault",
    "services/persistence/trace_keys",
    "tools/trace_export",
    "tools/trace_grep",
    "tools/trace_purge",
    "ui/api/routes/payment_cards",
    "ui/api/routes/payment_confirm",
    "ui/api/routes/spotify_cdp",
    "ui/api/routes/wake_training",
    "ui/qt_native/webview_window",
)

_REQUEST_BODY_KEYS = frozenset({"body", "data", "file", "files", "form", "forms", "json", "post_data"})
_REQUEST_COOKIE_KEYS = frozenset({"cookie", "cookies"})
_SENSITIVE_CONTENT_KEYS = frozenset(
    {
        "command_text",
        "email_body",
        "file_contents",
        "message_body",
        "payment_details",
        "prompt",
        "request_body",
        "screenshot",
        "sentry_dsn",
    }
)
_STRIP_HEADER_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "proxy_authorization",
        "x_api_key",
        "x_auth_token",
        "x_session_token",
        "x_viola_api_key",
    }
)
_PII_EXACT_KEYS = frozenset(
    {
        "address",
        "birthdate",
        "city",
        "client_ip",
        "date_of_birth",
        "display_name",
        "dob",
        "email",
        "email_address",
        "first_name",
        "full_name",
        "home_address",
        "hostname",
        "ip",
        "ip_address",
        "last_name",
        "lat_lng",
        "latitude",
        "longitude",
        "machine_name",
        "mac_address",
        "name",
        "phone",
        "phone_number",
        "postal_code",
        "remote_addr",
        "server_name",
        "ssn",
        "state",
        "street",
        "street_address",
        "username",
        "zip",
        "zipcode",
    }
)
_PII_KEY_MARKERS = (
    "billing_address",
    "billing_zip",
    "device_fingerprint",
    "device_id",
    "geo_location",
    "gps_coordinates",
    "hardware_fingerprint",
    "hardware_id",
    "holder_name",
    "mailing_address",
    "billing_zip",
    "precise_location",
    "shipping_address",
    "social_security",
)


def filter_sentry_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return a Sentry-safe event or ``None`` to drop it.

    The filter is fail-closed: if classification or redaction fails, the event
    is dropped instead of risking a cloud leak.
    """

    try:
        if is_tier3_origin_event(event, hint):
            return None
        return scrub_sentry_event(event)
    except _SANITIZER_EXCEPTIONS:
        return None


def before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Alias with the exact shape Sentry SDK expects."""

    return filter_sentry_event(event, hint)


def is_tier3_origin_event(event: Mapping[str, Any], hint: Mapping[str, Any] | None = None) -> bool:
    """Return True when structured event metadata says this came from Tier 3."""

    try:
        if _hint_has_tier3_origin(hint):
            return True
        return _scan_tier3_origin(event, (), set(), 0)
    except _SANITIZER_EXCEPTIONS:
        return True


def scrub_sentry_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of *event* with secrets and PII removed.

    Only the normal Sentry user identity, ``event["user"]["id"]``, is restored
    after redaction. Email, username, IP address, request bodies, breadcrumbs,
    extra data, and context data are scrubbed like any other event payload.
    """

    preserved_user_id = _extract_safe_user_id(event)
    redacted = _sanitize_any(event, (), set(), 0)
    if not isinstance(redacted, dict):
        return {}

    redacted = _apply_existing_redactors(redacted)
    _strip_request_payloads(redacted)
    _restore_user_identity(redacted, preserved_user_id)
    return redacted


def _normalize_key(value: Any) -> str:
    try:
        text = str(value)
    except _SANITIZER_EXCEPTIONS:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def _normalize_value(value: Any) -> str:
    try:
        text = str(value)
    except _SANITIZER_EXCEPTIONS:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return normalized


def _path_text(value: Any) -> str:
    try:
        text = str(value)
    except _SANITIZER_EXCEPTIONS:
        return ""
    return text.replace("\\", "/").lower()


def _truthy_marker(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return _normalize_value(value) not in {"", "0", "false", "no", "none", "null"}
    return bool(value)


def _hint_has_tier3_origin(hint: Mapping[str, Any] | None) -> bool:
    if not hint:
        return False
    exc_info = hint.get("exc_info")
    if not isinstance(exc_info, tuple) or not exc_info:
        return False
    exc_type = exc_info[0]
    module = getattr(exc_type, "__module__", "")
    name = getattr(exc_type, "__name__", "")
    return _value_has_tier3_path(module) or _value_has_tier3_surface(name)


def _scan_tier3_origin(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> bool:
    if depth > _MAX_REDACTION_DEPTH:
        return False
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return False
        seen.add(value_id)
        try:
            for key, inner in value.items():
                normalized = _normalize_key(key)
                child_path = (*key_path, normalized)
                if _field_marks_tier3_origin(normalized, inner, child_path):
                    return True
                if _scan_tier3_origin(inner, child_path, seen, depth + 1):
                    return True
            return False
        finally:
            seen.discard(value_id)
    if isinstance(value, (list, tuple, set, frozenset)):
        value_id = id(value)
        if value_id in seen:
            return False
        seen.add(value_id)
        try:
            return any(_scan_tier3_origin(item, key_path, seen, depth + 1) for item in value)
        finally:
            seen.discard(value_id)
    return False


def _field_marks_tier3_origin(key: str, value: Any, key_path: tuple[str, ...]) -> bool:
    del key_path
    if key in _TIER3_TIER_KEYS and _normalize_value(value) in _TIER3_TIER_VALUES:
        return True
    if key in _TIER3_BOOLEAN_KEYS and _truthy_marker(value):
        return True
    if key in _TIER3_CATEGORY_KEYS and _normalize_value(value) in _TIER3_STORAGE_CATEGORIES:
        return True
    if key in _TIER3_ORIGIN_KEYS and _value_has_tier3_surface(value):
        return True
    if key in _TIER3_PATH_KEYS and _value_has_tier3_path(value):
        return True
    return False


def _value_has_tier3_surface(value: Any) -> bool:
    normalized = _normalize_value(value)
    if not normalized:
        return False
    return normalized in _TIER3_STORAGE_CATEGORIES or any(marker in normalized for marker in _TIER3_VALUE_MARKERS)


def _value_has_tier3_path(value: Any) -> bool:
    text = _path_text(value)
    if not text:
        return False
    if any(marker in text for marker in _TIER3_VALUE_MARKERS):
        return True
    return any(marker in text for marker in _TIER3_PATH_MARKERS)


def _extract_safe_user_id(event: Mapping[str, Any]) -> str | None:
    user = event.get("user")
    if not isinstance(user, Mapping):
        return None
    for key in ("id", "user_id"):
        value = user.get(key)
        if value is None:
            continue
        try:
            text = str(value).strip()
        except _SANITIZER_EXCEPTIONS:
            continue
        if not text or len(text) > 256 or any(char in text for char in "\r\n\t"):
            continue
        if _sanitize_text(text) == text:
            return text
    return None


def _sanitize_any(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return _REDACTED_MAX_DEPTH
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return _REDACTED_BINARY
    if isinstance(value, Path):
        return _sanitize_text(str(value))
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            redacted: dict[str, Any] = {}
            for key, inner in value.items():
                key_text = str(key)
                normalized = _normalize_key(key_text)
                child_path = (*key_path, normalized)
                if _is_request_cookie_key(child_path) or _is_header_key_to_strip(key_path, normalized):
                    continue
                if _is_request_body_key(child_path):
                    redacted[key_text] = _REDACTED_REQUEST_BODY
                elif _is_sensitive_structured_key(child_path, normalized):
                    redacted[key_text] = _placeholder_for_key(normalized)
                else:
                    redacted[key_text] = _sanitize_any(inner, child_path, seen, depth + 1)
            return redacted
        finally:
            seen.discard(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return [_sanitize_any(item, key_path, seen, depth + 1) for item in value]
        finally:
            seen.discard(value_id)
    if isinstance(value, tuple):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return tuple(_sanitize_any(item, key_path, seen, depth + 1) for item in value)
        finally:
            seen.discard(value_id)
    if isinstance(value, set | frozenset):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return [_sanitize_any(item, key_path, seen, depth + 1) for item in value]
        finally:
            seen.discard(value_id)
    return value


def _sanitize_text(value: str) -> str:
    text = _WINDOWS_USER_PATH_RE.sub(r"\1[REDACTED:USER]", value)
    text = _POSIX_USER_PATH_RE.sub(r"\1[REDACTED:USER]", text)
    text = _SUPPORT_SECRET_KEY_VALUE_RE.sub(r"\1[REDACTED:SECRET]\3", text)
    try:
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(text)
    except ImportError:
        redacted = text
    except _SANITIZER_EXCEPTIONS:
        redacted = text
    text = redacted if isinstance(redacted, str) else str(redacted)
    text = _EMAIL_RE.sub("[REDACTED:EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED:PHONE]", text)
    text = _SSN_RE.sub("[REDACTED:SSN]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED:SECRET]", text)
    text = _AUTH_VALUE_RE.sub("[REDACTED:AUTH]", text)
    return _CARD_SHAPE_RE.sub("[REDACTED:CARD]", text)


def _is_request_body_key(key_path: tuple[str, ...]) -> bool:
    if not key_path:
        return False
    return "request" in key_path[:-1] and key_path[-1] in _REQUEST_BODY_KEYS


def _is_request_cookie_key(key_path: tuple[str, ...]) -> bool:
    if not key_path:
        return False
    return "request" in key_path[:-1] and key_path[-1] in _REQUEST_COOKIE_KEYS


def _is_header_key_to_strip(parent_path: tuple[str, ...], key: str) -> bool:
    if not parent_path or parent_path[-1] not in {"headers", "request_headers"}:
        return False
    return (
        key in _STRIP_HEADER_KEYS
        or "authorization" in key
        or "cookie" in key
        or "apikey" in key
        or "api_key" in key
        or "token" in key
        or "secret" in key
    )


def _is_sensitive_structured_key(key_path: tuple[str, ...], key: str) -> bool:
    if not key:
        return False
    if key_path == ("user", "id"):
        return False
    if key in {"user_id", "userid"}:
        return False
    if key in _SENSITIVE_CONTENT_KEYS:
        return True
    if key in _PII_EXACT_KEYS or any(marker in key for marker in _PII_KEY_MARKERS):
        return True
    try:
        from services.credential_boundary import is_tier3_credential_key

        if is_tier3_credential_key(key):
            return True
    except (ImportError, TypeError, ValueError):
        return _fallback_secret_key_match(key)
    return _fallback_secret_key_match(key)


def _fallback_secret_key_match(key: str) -> bool:
    markers = (
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "client_secret",
        "cookie",
        "credential",
        "cvc",
        "cvv",
        "oauth",
        "password",
        "private_key",
        "secret",
        "session",
        "token",
    )
    return any(marker in key for marker in markers)


def _placeholder_for_key(key: str) -> str:
    if any(marker in key for marker in ("device", "fingerprint", "hardware", "hostname", "machine")):
        return _REDACTED_DEVICE
    if _fallback_secret_key_match(key):
        return _REDACTED_SECRET
    return _REDACTED_PII


def _apply_existing_redactors(event: dict[str, Any]) -> dict[str, Any]:
    try:
        from core.secrets_mask import mask_dict_secrets
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(event)
        if isinstance(redacted, Mapping):
            redacted = mask_dict_secrets(dict(redacted))
    except ImportError:
        return event
    except _SANITIZER_EXCEPTIONS:
        return event
    if isinstance(redacted, Mapping):
        return {str(key): value for key, value in redacted.items()}
    return event


def _strip_request_payloads(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    for key in _REQUEST_BODY_KEYS:
        if key in request:
            request[key] = _REDACTED_REQUEST_BODY


def _restore_user_identity(event: dict[str, Any], user_id: str | None) -> None:
    if user_id:
        event["user"] = {"id": user_id}
    else:
        event.pop("user", None)


__all__ = [
    "before_send",
    "filter_sentry_event",
    "is_tier3_origin_event",
    "scrub_sentry_event",
]
