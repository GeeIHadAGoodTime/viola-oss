"""Per-task browser storage isolation helpers.

The browser identity store keeps long-lived auth state.  Per-command browser
tasks load a filtered snapshot from that store, then merge back only new or
rotated auth-looking state when the task ends.  Server-side carts can still
follow the user account after login; that is outside this client-storage layer.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CART_EPHEMERAL_PATTERNS: tuple[str, ...] = (
    "cart",
    "basket",
    "checkout",
    "order",
    "wip",
    "draft",
    "temp_session",
    "dz-cart",
    "dz-store",
    "dz-pop",
    "dz-session",
    "_cart_",
    "_session_temp_",
    "current_order",
    "csrf_token",
)

AUTH_LOCAL_STORAGE_HINTS: tuple[str, ...] = (
    "auth",
    "token",
    "jwt",
    "session_persistent",
    "refresh",
    "user_id",
)

_AUTH_VALUE_RE = re.compile(r"^[A-Za-z0-9_+/.=-]+$")
_AUTH_MIN_EXPIRES_SECONDS = 7 * 24 * 60 * 60


@dataclass(slots=True)
class BrowserTaskSession:
    """Runtime state for one top-level browser task."""

    user_id: str
    task_id: str
    mode: str
    context: Any
    identity_profile: Path | None
    loaded_snapshot: dict[str, Any]
    page: Any | None = None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def key(self) -> tuple[str, str]:
        return (self.user_id, self.task_id)


def empty_storage_state() -> dict[str, Any]:
    """Return a Playwright-compatible empty storage state."""

    return {"cookies": [], "origins": []}


def normalize_storage_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return a defensive, Playwright-shaped copy of a storage state."""

    if not isinstance(state, dict):
        return empty_storage_state()
    cookies = state.get("cookies")
    origins = state.get("origins")
    return {
        "cookies": (
            [dict(cookie) for cookie in cookies if isinstance(cookie, dict)] if isinstance(cookies, list) else []
        ),
        "origins": (
            [_normalize_origin(origin) for origin in origins if isinstance(origin, dict)]
            if isinstance(origins, list)
            else []
        ),
    }


def storage_state_has_entries(state: dict[str, Any]) -> bool:
    normalized = normalize_storage_state(state)
    return bool(normalized["cookies"] or normalized["origins"])


def is_cart_or_ephemeral_name(name: str | None) -> bool:
    """Return True when a cookie/localStorage key is task-scoped state."""

    lower = (name or "").strip().lower()
    if not lower:
        return False
    return any(pattern in lower for pattern in CART_EPHEMERAL_PATTERNS)


def filter_ephemeral_storage_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Drop cart/checkout/session-temporary cookies and localStorage keys."""

    normalized = normalize_storage_state(state)
    filtered_cookies = [
        cookie for cookie in normalized["cookies"] if not is_cart_or_ephemeral_name(str(cookie.get("name") or ""))
    ]

    filtered_origins: list[dict[str, Any]] = []
    for origin in normalized["origins"]:
        local_storage = [
            item
            for item in origin.get("localStorage", [])
            if not is_cart_or_ephemeral_name(str(item.get("name") or ""))
        ]
        if local_storage:
            filtered_origins.append({"origin": origin.get("origin", ""), "localStorage": local_storage})

    return {"cookies": filtered_cookies, "origins": filtered_origins}


def auth_cookie_eligible(
    cookie: dict[str, Any],
    *,
    original_cookie: dict[str, Any] | None,
    now: float | None = None,
) -> bool:
    """Return True when a new/rotated cookie looks like long-lived auth."""

    name = str(cookie.get("name") or "")
    value = str(cookie.get("value") or "")
    if not (name or value):
        return False
    if is_cart_or_ephemeral_name(name):
        return False
    if original_cookie is not None and str(original_cookie.get("value") or "") == value:
        return False

    now = time.time() if now is None else now
    expires_raw = cookie.get("expires")
    try:
        expires = float(expires_raw)
    except (TypeError, ValueError):
        expires = 0.0
    long_lived = expires > now and (expires - now) >= _AUTH_MIN_EXPIRES_SECONDS
    http_only = bool(cookie.get("httpOnly"))
    same_site_strict = str(cookie.get("sameSite") or "").strip().lower() == "strict"
    return long_lived or http_only or same_site_strict


def local_storage_auth_eligible(name: str | None, value: str | None) -> bool:
    """Return True when a localStorage key/value pair looks like identity state."""

    key = (name or "").strip()
    val = (value or "").strip()
    if not val or is_cart_or_ephemeral_name(key):
        return False
    key_lower = key.lower()
    if any(hint in key_lower for hint in AUTH_LOCAL_STORAGE_HINTS):
        return True
    return len(val) > 32 and bool(_AUTH_VALUE_RE.fullmatch(val))


def merge_auth_storage_state(
    *,
    original_snapshot: dict[str, Any] | None,
    final_state: dict[str, Any] | None,
    persistent_state: dict[str, Any] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Merge auth-looking task changes into the current persistent identity state."""

    original = filter_ephemeral_storage_state(original_snapshot)
    final = filter_ephemeral_storage_state(final_state)
    merged = filter_ephemeral_storage_state(persistent_state)

    original_cookies = {_cookie_key(cookie): cookie for cookie in original["cookies"]}
    merged_cookies = {_cookie_key(cookie): copy.deepcopy(cookie) for cookie in merged["cookies"]}
    for cookie in final["cookies"]:
        key = _cookie_key(cookie)
        if auth_cookie_eligible(cookie, original_cookie=original_cookies.get(key), now=now):
            merged_cookies[key] = copy.deepcopy(cookie)

    merged_origins = {
        str(origin.get("origin") or ""): {
            str(item.get("name") or ""): str(item.get("value") or "")
            for item in origin.get("localStorage", [])
            if isinstance(item, dict)
        }
        for origin in merged["origins"]
    }
    for origin in final["origins"]:
        origin_name = str(origin.get("origin") or "")
        if not origin_name:
            continue
        target = merged_origins.setdefault(origin_name, {})
        for item in origin.get("localStorage", []):
            if not isinstance(item, dict):
                continue
            key = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if local_storage_auth_eligible(key, value):
                target[key] = value

    return {
        "cookies": list(merged_cookies.values()),
        "origins": [
            {
                "origin": origin,
                "localStorage": [{"name": key, "value": value} for key, value in sorted(values.items())],
            }
            for origin, values in sorted(merged_origins.items())
            if values
        ],
    }


def _normalize_origin(origin: dict[str, Any]) -> dict[str, Any]:
    local_storage = origin.get("localStorage")
    return {
        "origin": str(origin.get("origin") or ""),
        "localStorage": (
            [dict(item) for item in local_storage if isinstance(item, dict)] if isinstance(local_storage, list) else []
        ),
    }


def _cookie_key(cookie: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(cookie.get("domain") or ""),
        str(cookie.get("path") or "/"),
        str(cookie.get("name") or ""),
    )
