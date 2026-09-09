"""Idempotency helpers for retry-safe command and tool execution.

The caches in this module are process-local and intentionally short lived.
All keys include an explicit user scope so retries from one user cannot affect
another user's commands.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from core.logging_config import get_logger

logger = get_logger(__name__)

ActionDuplicateBehavior = Literal["confirm", "hard_block", "silent_dedup", "soft_dedup"]

HTTP_IDEMPOTENCY_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Duplicate policy for one high-side-effect action class."""

    action_class: str
    window_seconds: float
    behavior: ActionDuplicateBehavior


ACTION_POLICIES: dict[str, ActionPolicy] = {
    "send_email": ActionPolicy("send_email", 60.0, "confirm"),
    "schedule_event": ActionPolicy("schedule_event", 30.0, "confirm"),
    "place_order": ActionPolicy("place_order", 300.0, "hard_block"),
    "send_sms": ActionPolicy("send_sms", 60.0, "confirm"),
    "smart_home_set_state": ActionPolicy("smart_home_set_state", 30.0, "silent_dedup"),
    "play_music": ActionPolicy("play_music", 5.0, "soft_dedup"),
}


@dataclass(frozen=True, slots=True)
class DuplicateDecision:
    """Decision returned when a tool call is probably a duplicate."""

    action_class: str
    behavior: ActionDuplicateBehavior
    key: str
    age_seconds: float
    window_seconds: float
    message: str


@dataclass(frozen=True, slots=True)
class HttpIdempotencyClaim:
    """Route-level claim result for a client-supplied idempotency key."""

    cache_key: str
    is_owner: bool
    response: dict[str, Any] | None = None
    in_progress: bool = False


@dataclass(slots=True)
class _CacheEntry:
    created_at: float
    value: Any


@dataclass(slots=True)
class _HttpEntry:
    created_at: float
    event: asyncio.Event
    response: dict[str, Any] | None = None


class TTLCache:
    """Small thread-safe TTL cache used by idempotency guards."""

    def __init__(self, *, ttl_seconds: float, max_entries: int = 2048, now: Any | None = None) -> None:
        self._ttl_seconds = max(0.1, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._now = now or time.monotonic
        self._lock = threading.RLock()
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, key: str, *, ttl_seconds: float | None = None) -> tuple[Any, float] | None:
        """Return ``(value, age_seconds)`` if key is present and unexpired."""

        ttl = self._ttl_seconds if ttl_seconds is None else max(0.1, float(ttl_seconds))
        now = float(self._now())
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            age = now - entry.created_at
            if age > ttl:
                self._entries.pop(key, None)
                return None
            return copy.deepcopy(entry.value), age

    def set(self, key: str, value: Any) -> None:
        """Store a value under ``key``."""

        now = float(self._now())
        with self._lock:
            self._entries[key] = _CacheEntry(created_at=now, value=copy.deepcopy(value))
            self._prune_locked(now)

    def clear(self) -> None:
        """Clear all entries."""

        with self._lock:
            self._entries.clear()

    def _prune_locked(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if now - entry.created_at > self._ttl_seconds]
        for key in expired:
            self._entries.pop(key, None)
        if len(self._entries) <= self._max_entries:
            return
        ordered = sorted(self._entries.items(), key=lambda item: item[1].created_at)
        for key, _entry in ordered[: len(self._entries) - self._max_entries]:
            self._entries.pop(key, None)


class IdempotencyStore:
    """Process-local idempotency state for HTTP requests and agent actions."""

    def __init__(self, *, now: Any | None = None) -> None:
        self._now = now or time.monotonic
        self._actions = TTLCache(ttl_seconds=300.0, now=self._now)
        self._http_entries: dict[str, _HttpEntry] = {}
        self._http_lock = asyncio.Lock()

    def clear(self) -> None:
        """Clear all process-local idempotency state."""

        self._actions.clear()
        self._http_entries.clear()

    def action_fingerprint(self, action_class: str, params: dict[str, Any], *, user_id: str) -> str:
        """Build a stable action fingerprint scoped to a user."""

        scope = _require_scope(user_id)
        payload = {
            "action_class": action_class,
            "params": _normalize_for_hash(params),
            "user_id": scope,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check_action_duplicate(
        self,
        action_class: str,
        params: dict[str, Any],
        *,
        user_id: str,
    ) -> DuplicateDecision | None:
        """Return duplicate decision for an action, or None when it should run."""

        policy = ACTION_POLICIES.get(action_class)
        if policy is None:
            return None
        key = self.action_fingerprint(action_class, params, user_id=user_id)
        cached = self._actions.get(key, ttl_seconds=policy.window_seconds)
        if cached is None:
            return None
        _value, age_seconds = cached
        return DuplicateDecision(
            action_class=action_class,
            behavior=policy.behavior,
            key=key,
            age_seconds=age_seconds,
            window_seconds=policy.window_seconds,
            message=duplicate_action_message(action_class, policy.behavior, age_seconds),
        )

    def record_action(self, action_class: str, params: dict[str, Any], *, user_id: str) -> str | None:
        """Record a completed action. Returns the fingerprint if recorded."""

        if action_class not in ACTION_POLICIES:
            return None
        key = self.action_fingerprint(action_class, params, user_id=user_id)
        self._actions.set(key, {"action_class": action_class})
        return key

    async def claim_http_response(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        ttl_seconds: float = HTTP_IDEMPOTENCY_TTL_SECONDS,
    ) -> HttpIdempotencyClaim:
        """Claim or replay a route-level idempotency response.

        The first request for a key becomes the owner. Concurrent retries wait
        for the owner to finish, then replay its response instead of executing
        the command again. If the owner is still running after the TTL, the
        retry receives an in-progress claim so the route can avoid duplicates.
        """

        cache_key = self.http_fingerprint(user_id=user_id, idempotency_key=idempotency_key)
        ttl = max(0.1, float(ttl_seconds))
        now = float(self._now())
        async with self._http_lock:
            self._prune_http_locked(now, ttl)
            entry = self._http_entries.get(cache_key)
            if entry is None:
                self._http_entries[cache_key] = _HttpEntry(created_at=now, event=asyncio.Event())
                return HttpIdempotencyClaim(cache_key=cache_key, is_owner=True)
            if entry.response is not None:
                return HttpIdempotencyClaim(
                    cache_key=cache_key,
                    is_owner=False,
                    response=copy.deepcopy(entry.response),
                )
            event = entry.event

        try:
            await asyncio.wait_for(event.wait(), timeout=ttl)
        except TimeoutError:
            return HttpIdempotencyClaim(cache_key=cache_key, is_owner=False, in_progress=True)

        async with self._http_lock:
            entry = self._http_entries.get(cache_key)
            if entry is not None and entry.response is not None:
                return HttpIdempotencyClaim(
                    cache_key=cache_key,
                    is_owner=False,
                    response=copy.deepcopy(entry.response),
                )
        return HttpIdempotencyClaim(cache_key=cache_key, is_owner=False, in_progress=True)

    async def complete_http_response(self, cache_key: str, response: dict[str, Any]) -> None:
        """Store the completed response for waiting/retry callers."""

        async with self._http_lock:
            entry = self._http_entries.get(cache_key)
            if entry is None:
                entry = _HttpEntry(created_at=float(self._now()), event=asyncio.Event())
                self._http_entries[cache_key] = entry
            entry.response = copy.deepcopy(response)
            entry.event.set()

    async def abandon_http_response(self, cache_key: str) -> None:
        """Release waiters without caching a failed or interrupted response."""

        async with self._http_lock:
            entry = self._http_entries.pop(cache_key, None)
            if entry is not None:
                entry.event.set()

    def http_fingerprint(self, *, user_id: str, idempotency_key: str) -> str:
        """Build a stable fingerprint for a client-supplied HTTP key."""

        scope = _require_scope(user_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        payload = {"endpoint": "/v1/command", "idempotency_key": key, "user_id": scope}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _prune_http_locked(self, now: float, ttl_seconds: float) -> None:
        stale = [key for key, entry in self._http_entries.items() if now - entry.created_at > ttl_seconds]
        for key in stale:
            entry = self._http_entries.pop(key, None)
            if entry is not None:
                entry.event.set()


def classify_action(tool_name: str, params: dict[str, Any]) -> str | None:
    """Map a concrete tool call to a high-side-effect action class."""

    name = (tool_name or "").strip()
    action = str(params.get("action") or "").strip().lower()

    if name in {"gmail_send", "gmail_sendDraft"} or (name == "gmail" and action in {"send", "send_draft"}):
        return "send_email"
    if name == "share_response":
        destination_kind = str(params.get("destination_kind") or "").strip().lower()
        if destination_kind in {"email", "mail", "gmail"}:
            return "send_email"
        if destination_kind in {"sms", "text", "message", "sms_message"}:
            return "send_sms"

    if name in {"calendar_createEvent", "calendar_addEvent"}:
        return "schedule_event"
    if name == "calendar" and action == "add":
        return "schedule_event"
    if name == "google_calendar" and action == "create_event":
        return "schedule_event"

    if name in {"payment", "fill_payment_details"}:
        return "place_order"
    if name == "browser_interact" and _looks_like_order_submission(params):
        return "place_order"

    if name in {"send_sms", "sms_send", "telegram_send"}:
        return "send_sms"

    if name in {"smart_home", "home_assistant"} and action in {"set", "set_state", "turn_on", "turn_off", "toggle"}:
        return "smart_home_set_state"

    if name == "play_music":
        return "play_music"
    if name == "playlist" and action in {"play", "play_favorites"}:
        return "play_music"

    return None


def duplicate_action_message(
    action_class: str,
    behavior: ActionDuplicateBehavior,
    age_seconds: float,
) -> str:
    """Return user-facing duplicate text for an action decision."""

    age = max(0, round(age_seconds))
    if action_class == "send_email":
        return "I just sent this %d seconds ago. Send it again?" % age
    if action_class == "schedule_event":
        return "I just scheduled this %d seconds ago. Want a duplicate event?" % age
    if action_class == "place_order":
        return "I just placed this %d seconds ago. Wait a few minutes or give me a different order." % age
    if action_class == "send_sms":
        return "I just sent this %d seconds ago. Send it again?" % age
    if action_class == "play_music":
        return "I just started this %d seconds ago. I will leave it playing unless you want me to restart it." % age
    if behavior == "silent_dedup":
        return "Already done."
    return "I think I just did this %d seconds ago. Do you want me to do it again?" % age


def get_idempotency_store() -> IdempotencyStore:
    """Return the process-wide idempotency store."""

    global _IDEMPOTENCY_STORE
    try:
        return _IDEMPOTENCY_STORE
    except NameError:
        _IDEMPOTENCY_STORE = IdempotencyStore()
        return _IDEMPOTENCY_STORE


def _require_scope(user_id: str) -> str:
    scope = str(user_id or "").strip()
    if not scope:
        raise ValueError("user_id is required for idempotency")
    return scope


def _normalize_for_hash(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda item: str(item[0])):
            if item is None or item == "":
                continue
            normalized[str(key)] = _normalize_for_hash(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_for_hash(item) for item in value if item is not None and item != ""]
    if isinstance(value, str):
        return " ".join(value.strip().split())
    return value


def _looks_like_order_submission(params: dict[str, Any]) -> bool:
    text = json.dumps(_normalize_for_hash(params), sort_keys=True, ensure_ascii=False, default=str).lower()
    return any(
        marker in text
        for marker in (
            "place order",
            "submit order",
            "complete order",
            "confirm order",
            "submit purchase",
            "complete purchase",
            "pay now",
        )
    )
