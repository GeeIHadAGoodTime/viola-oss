"""Ambient user identity for multi-user data isolation.

Every HTTP request sets the current user_id via the auth middleware.
Any code at any layer can read it with ``get_current_user_id()``.
No explicit parameter threading needed.

Usage::

    # Reading (anywhere in the call stack):
    from core.user_context import get_current_user_id
    user_id = get_current_user_id()

    # Setting (auth middleware, background tasks):
    from core.user_context import set_current_user_id
    token = set_current_user_id("user-123")

    # Scoped override (admin operations):
    from core.user_context import user_scope
    with user_scope("other-user"):
        ...  # all reads see "other-user"
"""

from __future__ import annotations

import contextlib
import contextvars
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user_id",
)
_current_cloud_access_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_cloud_access_token",
    default=None,
)
_RETIRED_LOCAL_SENTINEL_PARTS = ("local", "user")
_INVALID_USER_ID_SENTINELS = frozenset({"default", "desktop-local"})
_LEGACY_DESKTOP_AUTH_DB = "auth.db"
_LEGACY_DESKTOP_SESSION_DEVICE_PREFIX = "viola desktop"


def legacy_local_user_id() -> str:
    """Return the retired desktop sentinel for legacy fixture/migration checks."""
    return "-".join(_RETIRED_LOCAL_SENTINEL_PARTS)


def is_legacy_local_user_id(value: object) -> bool:
    """Return True when *value* is the retired desktop sentinel."""
    return isinstance(value, str) and value.strip() == legacy_local_user_id()


def is_placeholder_user_id(value: object) -> bool:
    """Return True for old pseudo-user values that must not be canonical."""
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return not normalized or normalized in _INVALID_USER_ID_SENTINELS or is_legacy_local_user_id(normalized)


def user_id_or_none(value: object) -> str | None:
    """Normalize an explicit user id, returning None for empty/retired sentinels."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if is_placeholder_user_id(normalized):
        return None
    return normalized


def is_device_user_id(value: object) -> bool:
    return isinstance(value, str) and value.strip().startswith("device-")


def is_desktop_local_principal(value: object) -> bool:
    """Return True for desktop-local pseudo-identities with no auth DB row."""
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return is_legacy_local_user_id(normalized) or normalized.startswith(("device-", "session:", "transient:"))


def get_current_user_id() -> str:
    """Get the authenticated user_id for the current request/task.

    Raises LookupError if called before the auth middleware has
    identified the user. This is intentional — silent fallbacks
    cause cross-user data leakage.
    """
    return _current_user_id.get()


def set_current_user_id(user_id: str) -> contextvars.Token[str]:
    """Set the user_id for the current context. Returns a reset token."""
    return _current_user_id.set(user_id)


def reset_current_user_id(token: contextvars.Token[str]) -> None:
    """Reset the current user context using a token from ``set_current_user_id``."""
    _current_user_id.reset(token)


def get_current_cloud_access_token() -> str | None:
    """Return the current request's validated GoTrue access token, if present."""
    token = _current_cloud_access_token.get()
    return token if token else None


def set_current_cloud_access_token(access_token: str) -> contextvars.Token[str | None]:
    """Bind a validated GoTrue access token for downstream cloud API calls."""
    normalized = access_token.strip() if isinstance(access_token, str) else ""
    return _current_cloud_access_token.set(normalized or None)


def reset_current_cloud_access_token(token: contextvars.Token[str | None]) -> None:
    """Reset the current cloud access token context using a token from ``set``."""
    _current_cloud_access_token.reset(token)


@contextlib.contextmanager
def user_scope(user_id: str) -> Generator[None, None, None]:
    """Context manager for scoped user identity override."""
    token = _current_user_id.set(user_id)
    try:
        yield
    finally:
        reset_current_user_id(token)


# ── Device-based anonymous identity ─────────────────────────────────────
# Used for rate limiting anonymous/unauthenticated users.  Produces a
# stable, deterministic ID per physical machine so that plan limits still
# apply even without a login.

_device_user_id: str | None = None


def get_device_user_id() -> str:
    """Return a stable device-based user ID for anonymous rate limiting.

    Format: ``device-<12-hex-chars>`` derived from hostname + MAC + a
    persistent random salt.  The salt makes the ID unpredictable even
    if the hostname is known.

    Cached after first call.
    """
    global _device_user_id
    if _device_user_id is not None:
        return _device_user_id

    import hashlib
    import platform
    import uuid

    from core.platform import get_data_dir

    machine_id = platform.node() or "unknown-device"
    mac_addr = str(uuid.getnode())

    # Persistent random salt — generated once, stored on disk
    salt_dir = get_data_dir()
    salt_file = salt_dir / "device_salt"
    try:
        if salt_file.exists():
            salt = salt_file.read_bytes()
        else:
            import secrets

            salt_dir.mkdir(parents=True, exist_ok=True)
            salt = secrets.token_bytes(16)
            salt_file.write_bytes(salt)
    except OSError:
        # If we can't read/write the salt file, use a deterministic
        # fallback so the ID is at least stable within a session
        salt = b"fallback-no-disk-access"

    combined = machine_id.encode() + salt + mac_addr.encode()
    _device_user_id = "device-" + hashlib.sha256(combined).hexdigest()[:12]
    return _device_user_id


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud"
    except (ImportError, AttributeError, TypeError, ValueError, RuntimeError):
        return True


def is_cloud_surface() -> bool:
    """Return True when this process is running the cloud surface.

    Public read of the same answer ``_is_cloud_surface`` gives the identity
    helpers in this module, for callers outside it that must gate a
    desktop-only capability (local OS keystore access, local-disk key
    material) on the surface rather than on the shape of a principal id.

    Fails CLOSED: any config resolution problem answers ``True``, so an
    unknown surface never widens a desktop-only capability.
    """
    return _is_cloud_surface()


def get_desktop_authenticated_user_id() -> str:
    """Resolve the most recent logged-in desktop account outside request context.

    Wake-word callbacks run on detector threads, so they cannot depend on the
    HTTP middleware's request-scoped ``current_user_id`` ContextVar. This helper
    reads the desktop-local GoTrue session store and returns the latest valid
    account user_id without falling back to the anonymous device principal.
    """
    if _is_cloud_surface():
        raise LookupError("desktop authenticated user_id is unavailable on cloud surfaces")

    try:
        from auth.desktop_session import _row_from_sqlite, get_desktop_session_store
    except (ImportError, RuntimeError) as exc:
        raise LookupError("desktop authenticated user_id is unavailable") from exc

    try:
        store = get_desktop_session_store()
        with store._lock:
            rows = _desktop_session_rows_by_recent_use(store)
            now = datetime.now(UTC)
            for raw_row in rows:
                try:
                    row = _row_from_sqlite(raw_row)
                    user_id = user_id_or_none(row.user_id)
                    if user_id is None or is_desktop_local_principal(user_id):
                        continue
                    if row.expires_at <= now:
                        continue
                    if not store._get_token(row, "access") or not store._get_token(row, "refresh"):
                        continue
                    return user_id
                except (
                    AttributeError,
                    OSError,
                    RuntimeError,
                    sqlite3.Error,
                    TypeError,
                    ValueError,
                ):
                    continue
    except (
        AttributeError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as exc:
        raise LookupError("desktop authenticated user_id is unavailable") from exc

    legacy_user_id = _legacy_desktop_session_user_id()
    if legacy_user_id is not None:
        return legacy_user_id

    raise LookupError("logged-in desktop user_id is required")


def _desktop_session_rows_by_recent_use(store: Any) -> list[sqlite3.Row]:
    store._ensure_sqlite_schema()
    with store._connect() as conn:
        return list(conn.execute("""
                SELECT session_hash, user_id, email, email_verified, gotrue_session_id,
                       created_at, expires_at, last_used_at, access_expires_at
                FROM desktop_sessions
                ORDER BY datetime(last_used_at) DESC, datetime(created_at) DESC
                """).fetchall())


def _legacy_desktop_session_user_id() -> str | None:
    """Return the latest unexpired legacy desktop login user_id, if present.

    Pre-GoTrue desktop installs can still have a real local login session in
    ``.viola/auth.db`` while the GoTrue desktop session store is empty. Voice
    callbacks need that authenticated desktop account, but must not fall back to
    anonymous device, local, or web-only sessions.
    """
    try:
        from core.platform import get_data_dir
    except (ImportError, RuntimeError):
        return None

    db_path = get_data_dir() / _LEGACY_DESKTOP_AUTH_DB
    if not db_path.is_file():
        return None

    try:
        with sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = list(
                conn.execute(
                    """
                    SELECT s.user_id, s.expires_at, s.created_at, s.last_used_at, u.id AS existing_user_id
                    FROM sessions AS s
                    LEFT JOIN users AS u ON u.id = s.user_id
                    WHERE s.token_hash IS NOT NULL
                      AND trim(s.token_hash) != ''
                      AND lower(coalesce(s.device_name, '')) LIKE ?
                    ORDER BY datetime(coalesce(s.last_used_at, s.created_at)) DESC,
                             datetime(s.created_at) DESC
                    LIMIT 8
                    """,
                    (f"{_LEGACY_DESKTOP_SESSION_DEVICE_PREFIX}%",),
                )
            )
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        return None

    now = datetime.now(UTC)
    for row in rows:
        user_id = user_id_or_none(row["user_id"])
        if user_id is None or is_desktop_local_principal(user_id) or not _is_uuid_user_id(user_id):
            continue
        if user_id_or_none(row["existing_user_id"]) != user_id:
            continue
        expires_at = _parse_sqlite_datetime(row["expires_at"])
        if expires_at is None or expires_at <= now:
            continue
        return user_id
    return None


def _parse_sqlite_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_uuid_user_id(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def get_desktop_active_user_id() -> str:
    """Resolve the desktop install's active principal.

    Returns the most recent authenticated desktop account when one exists,
    otherwise the bootstrap device principal — the canonical pre-login
    identity of a one-user-per-install desktop. Raises LookupError on cloud
    surfaces, where every consumer must carry a request-scoped principal.
    """
    if _is_cloud_surface():
        raise LookupError("desktop active principal is unavailable on cloud surfaces")
    try:
        return get_desktop_authenticated_user_id()
    except LookupError:
        return get_device_user_id()


def get_desktop_local_principals() -> list[str]:
    """Return every principal a desktop LOCAL rebroadcast must reach.

    One-user-per-install: the logged-in GoTrue account and the bootstrap
    device identity are the SAME human. A server-originated local rebroadcast
    (e.g. the phone cloud-event relay's live-transcript hop) may find the tab's
    ``/ws/events`` socket bound under EITHER principal depending on
    restart/login timing — the socket binds whatever ``get_desktop_active_user_id``
    resolved at socket-connect time, which flips account<->device based on
    whether a valid desktop session row is resolvable at that instant, and an
    already-registered socket is never re-bucketed when the answer later changes
    (root cause of the ``local_clients=0`` transcript drop, call 977101ac).

    Delivering to the union of {account, device} closes that temporal race
    without a live rebind: no matter which of the install's two principals the
    socket happened to bind under, and no matter which one the relay resolved,
    the rebroadcast lands. This does NOT widen tenant scope — it resolves at
    most the two ids of THIS one install (never another customer's), and it is
    unavailable on cloud surfaces (raises ``LookupError``), where RLS-isolated
    per-request principals are mandatory and device<->account fan-out would be
    wrong. The list is ordered account-first, then device, and never contains
    duplicates.
    """
    if _is_cloud_surface():
        raise LookupError("desktop local principals are unavailable on cloud surfaces")

    principals: list[str] = []
    try:
        account = user_id_or_none(get_desktop_authenticated_user_id())
    except LookupError:
        account = None
    if account is not None:
        principals.append(account)

    try:
        device = user_id_or_none(get_device_user_id())
    except (LookupError, OSError, RuntimeError, ValueError):
        device = None
    if device is not None and device not in principals:
        principals.append(device)

    if not principals:
        raise LookupError("no desktop local principal could be resolved")
    return principals


@contextlib.contextmanager
def desktop_active_principal_scope() -> Generator[None, None, None]:
    """Bind the desktop active principal for non-request consumers.

    Background loops (player-state broadcaster, music health probe,
    diagnostics state snapshot) run outside any HTTP request, so the auth
    middleware never set ``current_user_id`` for them. On the desktop
    surface the install's active principal (logged-in account, else the
    bootstrap device identity) IS the user, so they bind it here and read
    real state. An already-bound principal is never overridden, and on
    cloud surfaces this is a no-op: cloud code must carry a request
    principal and still fails loudly without one.
    """
    try:
        existing = user_id_or_none(get_current_user_id())
    except LookupError:
        existing = None
    if existing is not None or _is_cloud_surface():
        yield
        return
    with user_scope(get_desktop_active_user_id()):
        yield


def get_current_or_device_user_id() -> str:
    """Resolve the active request user, or the desktop device user outside cloud.

    Cloud paths must have the auth middleware contextvar set. Desktop-only
    paths can use the device binding when no request context exists.
    """
    try:
        current = user_id_or_none(get_current_user_id())
        if current is not None:
            return current
    except LookupError:
        pass

    if _is_cloud_surface():
        raise LookupError("user_id context is required on cloud surfaces")
    return get_device_user_id()


def get_current_or_desktop_active_user_id() -> str:
    """Resolve the request user, else the desktop *active* principal outside cloud.

    Like ``get_current_or_device_user_id`` this returns the request-scoped
    principal when the auth middleware has bound one. But outside a request
    context on the desktop — a background task, the LLM router/factory rebuilding
    on a fresh ``asyncio`` task or worker thread, a startup singleton — it falls
    back to ``get_desktop_active_user_id`` (the install's logged-in GoTrue account
    when a valid desktop session exists, else the bootstrap device identity)
    instead of jumping straight to the anonymous device principal.

    This is the resolver the managed-LLM account gate and the provider router
    need: their entitlement and user-scoped settings lookups must see the
    signed-in account even when provider construction runs outside the
    originating HTTP request (M-BILL-1). The bare device fallback silently
    defeated that — a logged-in desktop user was mis-resolved as "no account",
    so the managed provider raised ``ManagedLLMAuthRequired`` and keyless managed
    AI stayed dead after a real GUI sign-in. Cloud surfaces still fail loud
    (``LookupError``) when no request principal is bound.
    """
    try:
        current = user_id_or_none(get_current_user_id())
        if current is not None:
            return current
    except LookupError:
        pass

    if _is_cloud_surface():
        raise LookupError("user_id context is required on cloud surfaces")
    return get_desktop_active_user_id()
