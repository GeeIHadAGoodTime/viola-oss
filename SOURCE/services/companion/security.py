from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from auth.utils import hash_password, verify_password
from core.constants import TIMEOUT_5_MINUTES, TIMEOUT_MINUTE
from services.credential_boundary import (
    Tier3CredentialBoundaryError,
    reject_tier3_credential_payload,
)

from .protocol import (
    CompanionMessage,
    message_action,
    message_scope,
    normalize_message_type,
)

DEFAULT_COMMANDS_PER_MINUTE = 60
DEFAULT_WS_INVALID_TOKEN_ATTEMPTS_PER_MINUTE_PER_IP = 20
DEFAULT_WS_INVALID_TOKEN_ATTEMPTS_PER_MINUTE_PER_DEVICE = 6
# Abuse-defense ceilings for untrusted companion JSON. These bound request
# parsing and persistence work; they are not model/prompt input limits.
MAX_COMPANION_CAPABILITIES_JSON_BYTES = 8 * 1024
MAX_COMPANION_COMMAND_PAYLOAD_JSON_BYTES = 64 * 1024
MAX_COMPANION_JSON_DEPTH = 16
MAX_COMPANION_JSON_OBJECT_KEYS = 128
MAX_COMPANION_JSON_ARRAY_ITEMS = 512
MAX_COMPANION_JSON_STRING_BYTES = 16 * 1024
DESKTOP_CONSENT_SCOPE = "desktop"
SMART_HOME_CONSENT_ACTIONS = frozenset({"ha_control"})


class CompanionSecurityError(RuntimeError):
    """Raised when a companion action violates security policy."""


class CompanionOfflineError(CompanionSecurityError):
    """Raised when a companion command targets an offline device."""


class CompanionPayloadTooLarge(ValueError):
    def __init__(self, *, field: str, max_bytes: int, size_bytes: int) -> None:
        self.field = field
        self.max_bytes = int(max_bytes)
        self.size_bytes = int(size_bytes)
        super().__init__("%s exceeds %d bytes" % (field, max_bytes))


def generate_device_token() -> str:
    """Return a URL-safe 256-bit device token."""
    return secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    return hash_password(token)


def verify_device_token(token: str, token_hash: str) -> bool:
    return verify_password(token, token_hash)


def _sanitize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_json_value(item) for item in value]
    return str(value)


def _companion_json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("companion payload must be JSON serializable") from exc


def _validate_companion_json_shape(value: Any, *, path: str, depth: int = 0) -> None:
    if depth > MAX_COMPANION_JSON_DEPTH:
        raise ValueError("%s is too deeply nested" % path)
    if isinstance(value, dict):
        if len(value) > MAX_COMPANION_JSON_OBJECT_KEYS:
            raise ValueError("%s has too many object keys" % path)
        for key, item in value.items():
            key_text = str(key)
            if not key_text:
                raise ValueError("%s contains an empty object key" % path)
            if "\x00" in key_text:
                raise ValueError("%s key contains a disallowed NUL byte" % path)
            _validate_companion_json_shape(item, path="%s.%s" % (path, key_text), depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_COMPANION_JSON_ARRAY_ITEMS:
            raise ValueError("%s has too many list items" % path)
        for index, item in enumerate(value):
            _validate_companion_json_shape(item, path="%s[%d]" % (path, index), depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_COMPANION_JSON_STRING_BYTES:
            raise ValueError("%s string is too large" % path)
        if "\x00" in value:
            raise ValueError("%s string contains a disallowed NUL byte" % path)


def validate_companion_json_payload(
    payload: dict[str, Any] | None,
    *,
    field: str,
    max_bytes: int,
) -> dict[str, Any]:
    if payload is None:
        normalized: dict[str, Any] = {}
    else:
        normalized_raw = _sanitize_json_value(payload)
        if not isinstance(normalized_raw, dict):
            raise ValueError("%s must be a JSON object" % field)
        normalized = normalized_raw
    size_bytes = _companion_json_size(normalized)
    if size_bytes > max_bytes:
        raise CompanionPayloadTooLarge(field=field, max_bytes=max_bytes, size_bytes=size_bytes)
    _validate_companion_json_shape(normalized, path=field)
    try:
        reject_tier3_credential_payload(field, normalized)
    except Tier3CredentialBoundaryError as exc:
        raise ValueError(str(exc)) from exc
    return normalized


def _normalize_action_list(value: Any) -> list[str]:
    if value is True:
        return ["*"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    actions = {str(item).strip().lower().replace(":", ".") for item in value if str(item).strip()}
    return sorted(actions)


def normalize_capabilities(capabilities: dict[str, Any] | None) -> dict[str, Any]:
    if not capabilities:
        return {}

    normalized: dict[str, Any] = {}
    for raw_scope, raw_value in capabilities.items():
        scope = str(raw_scope).strip().lower()
        if not scope:
            continue
        if isinstance(raw_value, dict):
            payload = {str(k): _sanitize_json_value(v) for k, v in raw_value.items()}
            payload["actions"] = _normalize_action_list(raw_value.get("actions", raw_value))
            normalized[scope] = payload
            continue
        normalized[scope] = {"actions": _normalize_action_list(raw_value)}
    return normalized


def capability_allows(capabilities: dict[str, Any] | None, message_type: str) -> bool:
    normalized_type = normalize_message_type(message_type)
    scope = message_scope(normalized_type)
    if scope is None:
        return True

    normalized_capabilities = normalize_capabilities(capabilities)
    entry = normalized_capabilities.get(scope)
    if not isinstance(entry, dict):
        return False
    actions = entry.get("actions")
    if not isinstance(actions, list):
        return False
    action = message_action(normalized_type)
    return "*" in actions or action in actions or normalized_type in actions


def requires_explicit_consent(message_type: str) -> bool:
    normalized_type = normalize_message_type(message_type)
    scope = message_scope(normalized_type)
    if scope == DESKTOP_CONSENT_SCOPE:
        return True
    return scope == "smart_home" and message_action(normalized_type) in SMART_HOME_CONSENT_ACTIONS


def build_audit_details(
    *,
    message: CompanionMessage | None = None,
    status: str | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if message is not None:
        payload["message_type"] = message.type
        payload["request_id"] = message.request_id
    if status:
        payload["status"] = status
    if error:
        payload["error"] = error
    if extra:
        payload.update(_sanitize_json_value(extra))
    return payload


@dataclass(slots=True, frozen=True)
class ConsentGrant:
    consent_session_id: str
    user_id: str
    device_id: str
    scope: str
    created_at: float
    expires_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


class ConsentStore:
    def __init__(self, *, default_ttl_seconds: float = TIMEOUT_5_MINUTES) -> None:
        self._default_ttl_seconds = float(default_ttl_seconds)
        self._grants: dict[str, ConsentGrant] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        user_id: str,
        device_id: str,
        scope: str = DESKTOP_CONSENT_SCOPE,
        ttl_seconds: float | None = None,
        consent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConsentGrant:
        now = time.time()
        ttl = float(ttl_seconds or self._default_ttl_seconds)
        grant = ConsentGrant(
            consent_session_id=consent_session_id or uuid.uuid4().hex,
            user_id=user_id,
            device_id=device_id,
            scope=scope.strip().lower(),
            created_at=now,
            expires_at=now + max(1.0, ttl),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            await self._purge_expired_locked(now)
            self._grants[grant.consent_session_id] = grant
        return grant

    async def sync_from_payload(
        self,
        *,
        user_id: str,
        device_id: str,
        grants: list[dict[str, Any]] | None,
    ) -> None:
        if not grants:
            return
        for item in grants:
            if not isinstance(item, dict):
                continue
            granted = bool(item.get("granted", True))
            session_id = str(item.get("consent_session_id") or "").strip()
            if not granted or not session_id:
                continue
            await self.issue(
                user_id=user_id,
                device_id=device_id,
                scope=str(item.get("scope") or DESKTOP_CONSENT_SCOPE),
                ttl_seconds=float(
                    item.get("ttl_seconds") or item.get("expires_in_seconds") or self._default_ttl_seconds
                ),
                consent_session_id=session_id,
                metadata=dict(item.get("metadata") or {}),
            )

    async def validate(
        self,
        *,
        user_id: str,
        device_id: str,
        consent_session_id: str,
        scope: str = DESKTOP_CONSENT_SCOPE,
    ) -> bool:
        async with self._lock:
            await self._purge_expired_locked(time.time())
            grant = self._grants.get(consent_session_id)
            if grant is None:
                return False
            return (
                grant.user_id == user_id
                and grant.device_id == device_id
                and grant.scope == scope.strip().lower()
                and not grant.is_expired
            )

    async def revoke(self, consent_session_id: str) -> None:
        async with self._lock:
            self._grants.pop(consent_session_id, None)

    async def _purge_expired_locked(self, now: float) -> None:
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            self._grants.pop(key, None)


class CompanionRateLimiter:
    def __init__(
        self,
        *,
        default_limit: int = DEFAULT_COMMANDS_PER_MINUTE,
        window_seconds: float = TIMEOUT_MINUTE,
    ) -> None:
        self._default_limit = int(default_limit)
        self._window_seconds = float(window_seconds)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._redis: Any | None = None

    def set_redis(self, redis_backend: Any | None) -> None:
        self._redis = redis_backend

    async def allow(self, key: str, *, limit: int | None = None, scope: str = "companion.command") -> bool:
        max_requests = int(limit or self._default_limit)
        if self._redis is not None:
            from services.cache.rate_limit import (
                check_redis_sliding_window,
                cloud_rate_limit_fail_closed,
                redis_rate_limit_enabled,
            )

            if redis_rate_limit_enabled():
                decision = await check_redis_sliding_window(
                    self._redis,
                    scope=scope,
                    identifier=key,
                    limit=max_requests,
                    window_seconds=self._window_seconds,
                )
                if not decision.redis_error or cloud_rate_limit_fail_closed():
                    return decision.allowed

        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            allowed = len(bucket) < max_requests
            bucket.append(now)
            return allowed


class CompanionSecurityManager:
    def __init__(self) -> None:
        self._rate_limiter = CompanionRateLimiter()
        self._consents = ConsentStore()

    def set_rate_limiter_redis(self, redis_backend: Any | None) -> None:
        self._rate_limiter.set_redis(redis_backend)

    @property
    def consent_store(self) -> ConsentStore:
        return self._consents

    async def assert_rate_limit(self, *, device_id: str, limit: int | None = None) -> None:
        allowed = await self._rate_limiter.allow(device_id, limit=limit)
        if not allowed:
            raise CompanionSecurityError("Companion command rate limit exceeded")

    async def assert_ws_invalid_token_limit(self, *, client_ip: str, device_id: str) -> None:
        ip_key = "ip:%s" % (client_ip or "unknown")
        device_key = "device:%s" % device_id
        ip_allowed = await self._rate_limiter.allow(
            ip_key,
            limit=DEFAULT_WS_INVALID_TOKEN_ATTEMPTS_PER_MINUTE_PER_IP,
            scope="companion.ws.invalid_token",
        )
        device_allowed = await self._rate_limiter.allow(
            device_key,
            limit=DEFAULT_WS_INVALID_TOKEN_ATTEMPTS_PER_MINUTE_PER_DEVICE,
            scope="companion.ws.invalid_token",
        )
        if not ip_allowed or not device_allowed:
            raise CompanionSecurityError("Companion WebSocket invalid-token rate limit exceeded")

    async def assert_capability(
        self,
        *,
        capabilities: dict[str, Any] | None,
        message_type: str,
    ) -> None:
        if capability_allows(capabilities, message_type):
            return
        raise CompanionSecurityError("Companion capability does not allow %s" % normalize_message_type(message_type))

    async def assert_consent(
        self,
        *,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any],
    ) -> None:
        if not requires_explicit_consent(message_type):
            return
        consent_session_id = str(payload.get("consent_session_id") or "").strip()
        if not consent_session_id:
            raise CompanionSecurityError("Companion action requires consent_session_id")
        is_valid = await self._consents.validate(
            user_id=user_id,
            device_id=device_id,
            consent_session_id=consent_session_id,
            scope=DESKTOP_CONSENT_SCOPE,
        )
        if not is_valid:
            raise CompanionSecurityError("Companion action consent is missing or expired")
