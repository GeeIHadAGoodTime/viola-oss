"""Desktop-local sessions for cloud GoTrue sign-in.

Desktop sign-in is validated by the cloud GoTrue service, but the desktop
runtime cannot safely hold GoTrue's HS256 signing secret or query the cloud-only
GoTrue/Postgres session stores on every localhost request. This module bridges
that boundary by minting an opaque local session id after a successful cloud
token response and storing the cloud token pair in Tier-3 local encrypted
storage.

Trust boundary: identity is accepted only from the HTTPS response returned by
``api.useviola.com`` through Viola's own desktop GoTrue proxy. The browser never
gets a forgeable account token as its desktop auth cookie; it only gets a random
local handle that must resolve in this server-side store.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import httpx

from auth.models import Session, SubscriptionStatus, User
from auth.utils import generate_secure_token, hash_token
from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from core.platform import get_data_dir
from core.product import PlanId
from utils.enhancements.secrets import SecureSettingsManager, SecurityError

logger = get_logger("viola.auth.desktop_session")

LOCAL_SESSION_PREFIX = "vls_"
_SESSION_DB_RELATIVE = ("desktop_auth", "sessions.sqlite3")
_TOKEN_CACHE_FILENAME = ".gotrue_tokens.enc"
_TOKEN_KEY_FILENAME = ".gotrue_tokens.master_key"
_TOKEN_SECRET_APP = "viola-desktop-gotrue-tokens"  # pragma: allowlist secret
_LOCAL_SESSION_TTL_DAYS = 30
_ACCESS_REFRESH_SKEW_SECONDS = 30
_DEFAULT_ACCESS_TTL_SECONDS = 15 * 60
_SQLITE_TIMEOUT_SECONDS = 5.0


class DesktopSessionError(RuntimeError):
    """Raised when a desktop-local session cannot be safely minted."""


class DesktopSessionRefreshTransientError(Exception):
    """A GoTrue token refresh failed for a TRANSIENT reason (gateway 5xx,
    network drop, timeout) — NOT a genuine revocation.

    Deliberately NOT a subclass of ``DesktopSessionError`` so it is never
    swept up by the broad ``except`` blocks that delete the session on
    permanent failure. A transient failure must PRESERVE the stored session
    (fail only the current request/refresh attempt) so a momentary cloud
    blip — e.g. right as a laptop wakes from sleep and the network stack is
    still re-establishing — cannot permanently sign a desktop user out
    (issue #340: a >30-min-old desktop session showed "signed-in: none" and
    kept polling with a rejected token after exactly this kind of hiccup hit
    the scheduled access-token refresh).
    """


@dataclass(frozen=True)
class MintedDesktopSession:
    session_token: str
    user_id: str
    session_id: str
    max_age_seconds: int


@dataclass(frozen=True)
class DesktopSessionValidation:
    authenticated: bool
    user: User | None = None
    session: Session | None = None
    access_token: str | None = None
    refreshed: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class DesktopAccountIdentity:
    """Local-only snapshot of the desktop Viola-account sign-in state.

    The authoritative answer to "am I signed in to Viola, and as whom" on the
    desktop surface, derived from the local session store WITHOUT a network
    refresh so it is cheap enough to read on every agent turn. ``signed_in`` is
    False for the signed-out state (no valid local account session).
    """

    signed_in: bool
    user_id: str | None = None
    email: str | None = None
    email_verified: bool = False


@dataclass(frozen=True)
class _TokenPayload:
    access_token: str
    refresh_token: str
    user_id: str
    email: str
    email_verified: bool
    access_expires_at: datetime
    gotrue_session_id: str | None


@dataclass(frozen=True)
class _SessionRow:
    session_hash: str
    user_id: str
    email: str
    email_verified: bool
    gotrue_session_id: str | None
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime
    access_expires_at: datetime


class DesktopSessionStore:
    """SQLite-backed local session index plus encrypted GoTrue token cache."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._root_dir = Path(root_dir) if root_dir is not None else get_data_dir()
        self._auth_dir = self._root_dir.joinpath("desktop_auth")
        self._db_path = self._root_dir.joinpath(*_SESSION_DB_RELATIVE)
        self._token_cache_path = self._auth_dir / _TOKEN_CACHE_FILENAME
        self._token_key_path = self._auth_dir / _TOKEN_KEY_FILENAME
        self._lock = threading.RLock()
        # Serializes the GoTrue refresh network round-trip process-wide. GoTrue
        # ROTATES the refresh token on every use and treats a second use of the
        # already-consumed token as theft (replay detection), revoking the whole
        # session family. Multiple legitimate consumers (auth middleware, the
        # phone tool, the phone cloud-event relay) hit the same expiry window
        # concurrently, so exactly ONE of them may perform the refresh; the rest
        # must wait and re-read the persisted rotated pair. A ``threading.Lock``
        # (acquired off the event loop via ``asyncio.to_thread``) is used instead
        # of ``asyncio.Lock`` so consumers on DIFFERENT event loops/threads are
        # serialized too. Per-store (per-install), never global.
        self._refresh_serial_lock = threading.Lock()
        self._secrets: SecureSettingsManager | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def token_cache_path(self) -> Path:
        return self._token_cache_path

    def create_session(self, payload: Mapping[str, Any]) -> MintedDesktopSession | None:
        token_payload = _token_payload_from_gotrue_response(payload)
        if token_payload is None:
            return None

        session_token = "%s%s" % (LOCAL_SESSION_PREFIX, generate_secure_token(32))
        session_hash = hash_token(session_token)
        now = _utcnow()
        expires_at = now + timedelta(days=_LOCAL_SESSION_TTL_DAYS)
        gotrue_session_id = token_payload.gotrue_session_id or session_hash

        with self._lock:
            self._ensure_sqlite_schema()
            try:
                self._store_token_pair(
                    user_id=token_payload.user_id,
                    session_hash=session_hash,
                    access_token=token_payload.access_token,
                    refresh_token=token_payload.refresh_token,
                )
            except Exception as exc:
                raise DesktopSessionError("desktop token cache write failed") from exc
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO desktop_sessions
                        (session_hash, user_id, email, email_verified, gotrue_session_id,
                         created_at, expires_at, last_used_at, access_expires_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_hash,
                            token_payload.user_id,
                            token_payload.email,
                            1 if token_payload.email_verified else 0,
                            gotrue_session_id,
                            _format_dt(now),
                            _format_dt(expires_at),
                            _format_dt(now),
                            _format_dt(token_payload.access_expires_at),
                        ),
                    )
            except sqlite3.Error as exc:
                self._delete_token_pair(token_payload.user_id, session_hash)
                raise DesktopSessionError("desktop session store write failed") from exc

        return MintedDesktopSession(
            session_token=session_token,
            user_id=token_payload.user_id,
            session_id=gotrue_session_id,
            max_age_seconds=int((expires_at - now).total_seconds()),
        )

    async def validate_session_token(self, session_token: str) -> DesktopSessionValidation:
        if not is_desktop_local_session_token(session_token):
            return DesktopSessionValidation(authenticated=False, reason="not_desktop_local_session")

        session_hash = hash_token(session_token)
        try:
            row = self._get_session_row(session_hash)
            if row is None:
                return DesktopSessionValidation(authenticated=False, reason="unknown_desktop_session")

            return await self._validate_session_row(session_hash, row)
        except (
            DesktopSessionError,
            OSError,
            RuntimeError,
            SecurityError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            try:
                self.delete_session_token(session_token)
            except (
                OSError,
                RuntimeError,
                SecurityError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ):
                logger.debug(
                    "Desktop local session cleanup after validation failure also failed",
                    exc_info=True,
                )
            logger.exception("Desktop local session validation failed closed")
            return DesktopSessionValidation(authenticated=False, reason="desktop_session_validation_error")

    async def active_access_token(self) -> str | None:
        """Return the newest valid desktop GoTrue access token, refreshing if needed."""
        try:
            rows = self._session_rows_by_recent_use()
        except (
            DesktopSessionError,
            OSError,
            RuntimeError,
            SecurityError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            logger.exception("Desktop active session lookup failed closed")
            return None

        for row in rows:
            try:
                validation = await self._validate_session_row(row.session_hash, row)
            except (
                DesktopSessionError,
                OSError,
                RuntimeError,
                SecurityError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ):
                logger.debug("Desktop active session row validation failed closed", exc_info=True)
                self.delete_session_hash(row.session_hash)
                continue
            if validation.authenticated and validation.access_token:
                return validation.access_token
        return None

    def current_account_identity(self) -> DesktopAccountIdentity:
        """Return the desktop Viola-account sign-in state without a network call.

        A local-only read of the most-recently-used session row that is not
        expired and still has its GoTrue token pair cached. It deliberately
        does NOT trigger a token refresh (unlike ``active_access_token`` /
        ``validate_session_token``), so it is safe to call on every agent turn
        for the "am I logged in / what account is this" context. Returns
        ``signed_in=False`` when no such row exists — the signed-out state.

        Rows in ``desktop_sessions`` are only ever real GoTrue accounts
        (``_require_uuid_user_id`` enforces a UUID at insert time), so any live
        row here is an authenticated account, never a ``device-*`` principal.
        """
        try:
            with self._lock:
                rows = self._session_rows_by_recent_use()
                now = _utcnow()
                for row in rows:
                    try:
                        if row.expires_at <= now:
                            continue
                        if not self._get_token(row, "access") or not self._get_token(row, "refresh"):
                            continue
                        return DesktopAccountIdentity(
                            signed_in=True,
                            user_id=row.user_id,
                            email=_nonempty_str(row.email),
                            email_verified=bool(row.email_verified),
                        )
                    except (
                        OSError,
                        RuntimeError,
                        SecurityError,
                        sqlite3.Error,
                        TypeError,
                        ValueError,
                    ):
                        logger.debug("Desktop account identity row skipped", exc_info=True)
                        continue
        except (
            DesktopSessionError,
            OSError,
            RuntimeError,
            SecurityError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            logger.debug("Desktop account identity lookup failed closed", exc_info=True)
        return DesktopAccountIdentity(signed_in=False)

    async def _validate_session_row(self, session_hash: str, row: _SessionRow) -> DesktopSessionValidation:
        now = _utcnow()
        if row.expires_at <= now:
            self.delete_session_hash(session_hash)
            return DesktopSessionValidation(authenticated=False, reason="expired_desktop_session")

        access_token = self._get_token(row, "access")
        refresh_token = self._get_token(row, "refresh")
        if not access_token or not refresh_token:
            self.delete_session_hash(session_hash)
            return DesktopSessionValidation(authenticated=False, reason="missing_desktop_gotrue_tokens")

        refreshed = False
        if row.access_expires_at <= now + timedelta(seconds=_ACCESS_REFRESH_SKEW_SECONDS):
            try:
                refresh_result = await self._refresh_session_row_serialized(session_hash)
            except DesktopSessionRefreshTransientError:
                # Cloud 5xx / network blip — the stored session is almost
                # certainly still valid. PRESERVE it (do NOT delete): only this
                # request fails unauthenticated, and the next validation retries
                # the refresh once the cloud recovers. Deleting here would log
                # users out permanently on a transient blip (issue #340).
                return DesktopSessionValidation(authenticated=False, reason="gotrue_refresh_transient")
            except (
                DesktopSessionError,
                OSError,
                RuntimeError,
                SecurityError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ):
                self.delete_session_hash(session_hash)
                return DesktopSessionValidation(authenticated=False, reason="gotrue_refresh_invalid")
            if refresh_result is None:
                return DesktopSessionValidation(authenticated=False, reason="gotrue_refresh_failed")
            row, access_token = refresh_result
            refreshed = True

        self._touch_session(session_hash)
        user = _row_to_user(row)
        session = _row_to_session(row)
        return DesktopSessionValidation(
            authenticated=True,
            user=user,
            session=session,
            access_token=access_token,
            refreshed=refreshed,
        )

    async def _refresh_session_row_serialized(self, session_hash: str) -> tuple[_SessionRow, str] | None:
        """Refresh one session's GoTrue token pair with process-wide single-flight.

        GoTrue rotates the refresh token on every use and revokes the whole
        session family when an already-consumed refresh token is presented again
        (replay detection). Concurrent legitimate consumers — the auth
        middleware, the phone tool, and the phone cloud-event relay — all hit
        the same 5-minute expiry boundary, so without serialization two of them
        read the SAME stored refresh token and the second POST looks like theft,
        self-destructing the session minutes after login.

        The whole critical section (re-read → maybe refresh → persist) runs in
        one worker thread under ``self._refresh_serial_lock``:

        * exactly one consumer performs the network refresh per expiry;
        * every waiter blocks off the event loop, then re-reads the persisted
          rotated pair instead of replaying the consumed token;
        * the rotated pair is durably persisted *before* the lock is released,
          so no waiter can ever observe (and replay) the consumed token;
        * cancellation of the awaiting task cannot orphan the lock — the worker
          thread runs the ``with`` block to completion on its own.

        Returns ``(row, access_token)`` with a fresh token, or ``None`` when the
        session is gone/invalid (fail closed — genuine revocation stays fatal).
        """
        return await asyncio.to_thread(self._refresh_session_row_locked_sync, session_hash)

    def _refresh_session_row_locked_sync(self, session_hash: str) -> tuple[_SessionRow, str] | None:
        with self._refresh_serial_lock:
            return self._refresh_session_row_assume_locked(session_hash)

    def _refresh_session_row_assume_locked(self, session_hash: str) -> tuple[_SessionRow, str] | None:
        """Critical section body of the serialized refresh.

        MUST be called with ``self._refresh_serial_lock`` already held.
        """
        row = self._get_session_row(session_hash)
        if row is None:
            # Deleted while we waited — e.g. the refresh winner hit a
            # genuine revocation and removed the session. Fail closed.
            return None
        access_token = self._get_token(row, "access")
        refresh_token = self._get_token(row, "refresh")
        if not access_token or not refresh_token:
            self.delete_session_hash(session_hash)
            return None
        if row.access_expires_at > _utcnow() + timedelta(seconds=_ACCESS_REFRESH_SKEW_SECONDS):
            # Another consumer already refreshed while we waited on the
            # lock; the rotated pair is persisted — reuse it, never replay.
            return row, access_token
        # This thread has no running event loop, so the (monkeypatchable)
        # async GoTrue boundary runs on its own short-lived loop.
        refreshed_payload = asyncio.run(_refresh_gotrue_tokens(refresh_token))
        if refreshed_payload is None:
            self.delete_session_hash(session_hash)
            return None
        return self._apply_refreshed_tokens(session_hash, row, refreshed_payload)

    async def gotrue_refresh_grant(
        self,
        session_token: str | None,
        presented_refresh_token: str | None = None,
    ) -> dict[str, Any] | None:
        """Serve a webview/browser ``grant_type=refresh_token`` from THE store.

        The desktop webview's React app is itself a GoTrue client: it holds its
        own copy of the access+refresh pair (in memory, SEC-017) and
        auto-refreshes via the same-origin ``/auth/v1/token`` proxy. If that
        proxied refresh were forwarded raw to cloud GoTrue, it would race the
        store's serialized refresh over the SAME rotating token family — the
        exact cross-process replay the in-process ``_refresh_serial_lock``
        cannot see. This method is the single serialization point for those
        clients: it resolves the session (by the desktop session cookie, or by
        matching the presented refresh token against the stored one), runs the
        SAME single-flight critical section every other consumer uses, and
        answers with the store's current rotated pair in GoTrue wire shape.
        The presented refresh token is NEVER forwarded upstream — even a stale
        webview copy (e.g. after a reload) gets the persisted rotated pair
        instead of triggering replay detection.

        Returns the GoTrue-shaped token payload, or ``None`` when no desktop
        session matches / the session is genuinely invalid (fail closed).
        Raises ``DesktopSessionRefreshTransientError`` (uncaught here, by
        design) when the refresh hit a cloud 5xx / network blip — the caller
        (``auth/desktop_gotrue_proxy.py``) must NOT treat that the same as a
        genuine rejection: the stored session survives a transient failure,
        so the response must not wipe the webview's auth cookies either
        (issue #340).
        """
        try:
            return await asyncio.to_thread(
                self._gotrue_refresh_grant_locked_sync,
                session_token,
                presented_refresh_token,
            )
        except (
            DesktopSessionError,
            OSError,
            RuntimeError,
            SecurityError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            logger.exception("Desktop GoTrue refresh grant failed closed")
            return None

    def _gotrue_refresh_grant_locked_sync(
        self,
        session_token: str | None,
        presented_refresh_token: str | None,
    ) -> dict[str, Any] | None:
        with self._refresh_serial_lock:
            session_hash = self._resolve_refresh_grant_session_hash(session_token, presented_refresh_token)
            if session_hash is None:
                return None
            row = self._get_session_row(session_hash)
            if row is None:
                return None
            if row.expires_at <= _utcnow():
                self.delete_session_hash(session_hash)
                return None
            result = self._refresh_session_row_assume_locked(session_hash)
            if result is None:
                return None
            row, access_token = result
            refresh_token = self._get_token(row, "refresh")
            if not refresh_token:
                self.delete_session_hash(session_hash)
                return None
            self._touch_session(session_hash)
            return _gotrue_wire_payload(row, access_token, refresh_token)

    def _resolve_refresh_grant_session_hash(
        self,
        session_token: str | None,
        presented_refresh_token: str | None,
    ) -> str | None:
        """Resolve which desktop session a proxied refresh grant belongs to.

        The desktop session cookie is authoritative. A cookie-less client that
        can present the CURRENT stored refresh token (possession proof — the
        same credential GoTrue itself accepts for this grant) is also served;
        anything else fails closed. Called with the refresh lock held so the
        match cannot race a concurrent rotation.
        """
        if is_desktop_local_session_token(session_token):
            candidate = hash_token(session_token)  # type: ignore[arg-type]
            if self._get_session_row(candidate) is not None:
                return candidate
        presented = _nonempty_str(presented_refresh_token)
        if presented:
            for row in self._session_rows_by_recent_use():
                stored = self._get_token(row, "refresh")
                if stored and hmac.compare_digest(stored, presented):
                    return row.session_hash
        return None

    def delete_session_token(self, session_token: str) -> bool:
        if not is_desktop_local_session_token(session_token):
            return False
        session_hash = hash_token(session_token)
        return self.delete_session_hash(session_hash)

    def delete_session_hash(self, session_hash: str) -> bool:
        with self._lock:
            row = self._get_session_row(session_hash)
            if row is not None:
                self._delete_token_pair(row.user_id, session_hash)
            try:
                with self._connect() as conn:
                    cursor = conn.execute(
                        "DELETE FROM desktop_sessions WHERE session_hash = ?",
                        (session_hash,),
                    )
                return bool(cursor.rowcount)
            except sqlite3.Error:
                logger.exception("Failed to delete desktop local session")
                return False

    def purge_user(self, user_id: str) -> dict[str, Any]:
        normalized = _require_user_id(user_id)
        deleted_rows = 0
        failures: list[str] = []
        with self._lock:
            try:
                self._ensure_sqlite_schema()
                rows = self._session_hashes_for_user(normalized)
                for session_hash in rows:
                    self._delete_token_pair(normalized, session_hash)
                with self._connect() as conn:
                    cursor = conn.execute(
                        "DELETE FROM desktop_sessions WHERE user_id = ?",
                        (normalized,),
                    )
                    deleted_rows = int(cursor.rowcount if cursor.rowcount is not None else 0)
            except (OSError, sqlite3.Error, ValueError) as exc:
                failures.append(str(exc))
        return {
            "deleted": deleted_rows,
            "sqlite_rows": deleted_rows,
            "failures": failures,
            "remaining_count_exempt_reason": "desktop auth sqlite/token-cache files are row-filtered",
        }

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=_SQLITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_sqlite_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS desktop_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    email_verified INTEGER NOT NULL DEFAULT 0,
                    gotrue_session_id TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    access_expires_at TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_desktop_sessions_user_id
                ON desktop_sessions(user_id)
                """)

    def _secret_manager(self) -> SecureSettingsManager:
        if self._secrets is None:
            self._auth_dir.mkdir(parents=True, exist_ok=True)
            manager = SecureSettingsManager(
                app_name=_TOKEN_SECRET_APP,
                fallback_key_file=self._token_key_path,
            )
            manager.load_from_file(self._token_cache_path)
            self._secrets = manager
        return self._secrets

    def _store_token_pair(self, *, user_id: str, session_hash: str, access_token: str, refresh_token: str) -> None:
        manager = self._secret_manager()
        manager.set_secret(_token_secret_ref(user_id, session_hash, "access"), access_token)
        manager.set_secret(_token_secret_ref(user_id, session_hash, "refresh"), refresh_token)
        manager.save_to_file(self._token_cache_path)

    def _delete_token_pair(self, user_id: str, session_hash: str) -> None:
        manager = self._secret_manager()
        removed_access = manager.delete_secret(_token_secret_ref(user_id, session_hash, "access"))
        removed_refresh = manager.delete_secret(_token_secret_ref(user_id, session_hash, "refresh"))
        if removed_access or removed_refresh:
            manager.save_to_file(self._token_cache_path)

    def _get_token(self, row: _SessionRow, kind: str) -> str | None:
        return self._secret_manager().get_secret(_token_secret_ref(row.user_id, row.session_hash, kind))

    def _get_session_row(self, session_hash: str) -> _SessionRow | None:
        self._ensure_sqlite_schema()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_hash, user_id, email, email_verified, gotrue_session_id,
                       created_at, expires_at, last_used_at, access_expires_at
                FROM desktop_sessions
                WHERE session_hash = ?
                """,
                (session_hash,),
            ).fetchone()
        return _row_from_sqlite(row) if row is not None else None

    def _session_hashes_for_user(self, user_id: str) -> list[str]:
        self._ensure_sqlite_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_hash FROM desktop_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [str(row["session_hash"]) for row in rows]

    def _session_rows_by_recent_use(self) -> list[_SessionRow]:
        self._ensure_sqlite_schema()
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT session_hash, user_id, email, email_verified, gotrue_session_id,
                       created_at, expires_at, last_used_at, access_expires_at
                FROM desktop_sessions
                ORDER BY datetime(last_used_at) DESC, datetime(created_at) DESC
                """).fetchall()
        return [_row_from_sqlite(row) for row in rows]

    def _touch_session(self, session_hash: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE desktop_sessions SET last_used_at = ? WHERE session_hash = ?",
                (_format_dt(_utcnow()), session_hash),
            )

    def _apply_refreshed_tokens(
        self,
        session_hash: str,
        row: _SessionRow,
        payload: Mapping[str, Any],
    ) -> tuple[_SessionRow, str]:
        refreshed = _token_payload_from_gotrue_response(
            payload,
            fallback_user_id=row.user_id,
            fallback_email=row.email,
            fallback_email_verified=row.email_verified,
        )
        if refreshed is None:
            raise DesktopSessionError("GoTrue refresh returned no token payload")
        if refreshed.user_id != row.user_id:
            raise DesktopSessionError("GoTrue refresh user_id changed")

        email = refreshed.email or row.email
        email_verified = refreshed.email_verified or row.email_verified
        gotrue_session_id = refreshed.gotrue_session_id or row.gotrue_session_id
        self._store_token_pair(
            user_id=row.user_id,
            session_hash=session_hash,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE desktop_sessions
                SET email = ?, email_verified = ?, gotrue_session_id = ?,
                    access_expires_at = ?, last_used_at = ?
                WHERE session_hash = ? AND user_id = ?
                """,
                (
                    email,
                    1 if email_verified else 0,
                    gotrue_session_id,
                    _format_dt(refreshed.access_expires_at),
                    _format_dt(_utcnow()),
                    session_hash,
                    row.user_id,
                ),
            )
        return (
            _SessionRow(
                session_hash=session_hash,
                user_id=row.user_id,
                email=email,
                email_verified=email_verified,
                gotrue_session_id=gotrue_session_id,
                created_at=row.created_at,
                expires_at=row.expires_at,
                last_used_at=_utcnow(),
                access_expires_at=refreshed.access_expires_at,
            ),
            refreshed.access_token,
        )


_store: DesktopSessionStore | None = None
_store_lock = threading.Lock()


def get_desktop_session_store() -> DesktopSessionStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DesktopSessionStore()
    return _store


def reset_desktop_session_store_for_tests(
    root_dir: Path | None = None,
) -> DesktopSessionStore:
    global _store
    with _store_lock:
        _store = DesktopSessionStore(root_dir=root_dir)
        return _store


def is_desktop_local_session_token(token: object) -> bool:
    return isinstance(token, str) and token.startswith(LOCAL_SESSION_PREFIX) and len(token) > len(LOCAL_SESSION_PREFIX)


def create_desktop_session_from_gotrue_payload(
    payload: Mapping[str, Any],
) -> MintedDesktopSession | None:
    return get_desktop_session_store().create_session(payload)


async def validate_desktop_session_token(
    session_token: str,
) -> DesktopSessionValidation:
    return await get_desktop_session_store().validate_session_token(session_token)


async def desktop_access_token_for_session_token(session_token: str) -> str | None:
    validation = await validate_desktop_session_token(session_token)
    if not validation.authenticated:
        return None
    return validation.access_token


async def desktop_access_token_for_active_session() -> str | None:
    return await get_desktop_session_store().active_access_token()


def get_desktop_account_identity() -> DesktopAccountIdentity:
    """Return the current desktop Viola-account sign-in state (local-only read)."""
    return get_desktop_session_store().current_account_identity()


async def serve_desktop_gotrue_refresh_grant(
    session_token: str | None,
    presented_refresh_token: str | None = None,
) -> dict[str, Any] | None:
    """Serve a proxied ``grant_type=refresh_token`` through the store's single-flight.

    May raise ``DesktopSessionRefreshTransientError`` — deliberately NOT
    caught here, see ``DesktopSessionStore.gotrue_refresh_grant``.
    """
    return await get_desktop_session_store().gotrue_refresh_grant(session_token, presented_refresh_token)


def delete_desktop_session_token(session_token: str) -> bool:
    return get_desktop_session_store().delete_session_token(session_token)


def purge_desktop_sessions_for_user(user_id: str) -> dict[str, Any]:
    return get_desktop_session_store().purge_user(user_id)


def _token_payload_from_gotrue_response(
    payload: Mapping[str, Any],
    *,
    fallback_user_id: str | None = None,
    fallback_email: str | None = None,
    fallback_email_verified: bool | None = None,
) -> _TokenPayload | None:
    access_token = _nonempty_str(payload.get("access_token"))
    refresh_token = _nonempty_str(payload.get("refresh_token"))
    if not access_token and not refresh_token:
        return None
    if not access_token or not refresh_token:
        raise DesktopSessionError("GoTrue token response must include access and refresh tokens")

    user_info = _as_mapping(payload.get("user"))
    access_claims = _decode_unverified_jwt_payload(access_token)
    if access_claims is None:
        raise DesktopSessionError("GoTrue access_token must be a JWT")

    claim_user_id = _nonempty_str(access_claims.get("sub"))
    if not claim_user_id:
        raise DesktopSessionError("GoTrue access_token missing sub")
    gotrue_session_id = _nonempty_str(access_claims.get("session_id"))
    if not gotrue_session_id:
        raise DesktopSessionError("GoTrue access_token missing session_id")

    user_id = _nonempty_str(user_info.get("id")) or claim_user_id or fallback_user_id
    email = _nonempty_str(user_info.get("email")) or _nonempty_str(access_claims.get("email")) or fallback_email
    if not user_id:
        raise DesktopSessionError("GoTrue token response missing user_id")
    if not email:
        raise DesktopSessionError("GoTrue token response missing email")
    _require_uuid_user_id(user_id)

    if claim_user_id != user_id:
        raise DesktopSessionError("GoTrue token response user_id mismatch")

    email_verified_claim = access_claims.get("email_verified")
    if email_verified_claim is False:
        raise DesktopSessionError("GoTrue token response email must be verified")
    email_verified = _truthy(email_verified_claim) or bool(_nonempty_str(user_info.get("email_confirmed_at")))
    if not email_verified and fallback_email_verified is not None:
        email_verified = bool(fallback_email_verified)
    if not email_verified:
        raise DesktopSessionError("GoTrue token response email must be verified")
    access_expires_at = _access_expires_at(payload, access_claims)
    return _TokenPayload(
        access_token=access_token,
        refresh_token=refresh_token,
        user_id=user_id,
        email=email,
        email_verified=email_verified,
        access_expires_at=access_expires_at,
        gotrue_session_id=gotrue_session_id,
    )


async def _refresh_gotrue_tokens(refresh_token: str) -> Mapping[str, Any] | None:
    try:
        from auth.routes import _desktop_should_proxy_to_cloud, _get_cloud_base_url

        if not _desktop_should_proxy_to_cloud():
            logger.warning("Desktop local session refresh refused non-cloud base URL")
            return None
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
            response = await client.post(
                "%s/auth/v1/token" % _get_cloud_base_url(),
                params={"grant_type": "refresh_token"},
                json={"refresh_token": refresh_token},
            )
        if response.status_code >= 500:
            # Gateway / upstream-unavailable (502/503/504): TRANSIENT. The
            # refresh token is almost certainly still valid — the cloud just
            # blipped. Preserve the session; only THIS refresh attempt fails.
            logger.warning(
                "Desktop local session refresh transient failure status=%s (session preserved)",
                response.status_code,
            )
            raise DesktopSessionRefreshTransientError("gotrue_status_%s" % response.status_code)
        if response.status_code >= 400:
            # Genuine rejection (401 revoked / 400 malformed refresh token):
            # PERMANENT — fail closed, the caller deletes the session.
            logger.warning("Desktop local session refresh rejected status=%s", response.status_code)
            return None
        data = response.json()
        return data if isinstance(data, Mapping) else None
    except httpx.HTTPError as exc:
        # Network drop / timeout / connection error: TRANSIENT, not a
        # revocation — preserve the session for the next attempt. This is
        # exactly what a laptop-sleep/wake network hiccup looks like.
        logger.warning("Desktop local session refresh transport failure (session preserved): %s", exc)
        raise DesktopSessionRefreshTransientError("transport") from exc
    except (ValueError, TypeError):
        # Malformed/undecodable success body — a valid GoTrue response never
        # looks like this; treat as permanent so we don't loop forever.
        logger.exception("Desktop local session refresh payload failure")
        return None


def _decode_unverified_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)
        decoded = base64.urlsafe_b64decode((payload_segment + padding).encode("ascii"))
        parsed = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _access_expires_at(payload: Mapping[str, Any], claims: Mapping[str, Any]) -> datetime:
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        return _utcnow() + timedelta(seconds=expires_in)
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, int) and expires_at > 0:
        return datetime.fromtimestamp(expires_at, UTC)
    exp = claims.get("exp")
    if isinstance(exp, int) and exp > 0:
        return datetime.fromtimestamp(exp, UTC)
    return _utcnow() + timedelta(seconds=_DEFAULT_ACCESS_TTL_SECONDS)


def _gotrue_wire_payload(row: _SessionRow, access_token: str, refresh_token: str) -> dict[str, Any]:
    """Shape the store's current token pair as a GoTrue token-grant response.

    Consumed by GoTrue clients in the desktop webview (``@supabase/auth-js``
    and the raw-fetch authClient), so it mirrors the wire fields they read:
    access/refresh tokens, expiry, and a user object rebuilt from the access
    JWT's own claims (id/aud/role/app_metadata drive the UI's plan display).
    """
    claims = _decode_unverified_jwt_payload(access_token) or {}
    now = _utcnow()
    expires_in = max(1, int((row.access_expires_at - now).total_seconds()))
    app_metadata = claims.get("app_metadata")
    user: dict[str, Any] = {
        "id": row.user_id,
        "aud": _nonempty_str(claims.get("aud")) or "authenticated",
        "role": _nonempty_str(claims.get("role")) or "authenticated",
        "email": row.email,
        "app_metadata": dict(app_metadata) if isinstance(app_metadata, Mapping) else {},
        # user_metadata is user-controlled GoTrue signup data and is never
        # trusted or echoed here (user-metadata-trust gate); clients that
        # need it read it from /auth/v1/user, not the refresh grant.
        "user_metadata": {},
    }
    if row.email_verified:
        user["email_confirmed_at"] = _format_dt(row.created_at)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "expires_at": int(row.access_expires_at.timestamp()),
        "refresh_token": refresh_token,
        "user": user,
    }


def _row_from_sqlite(row: sqlite3.Row) -> _SessionRow:
    return _SessionRow(
        session_hash=str(row["session_hash"]),
        user_id=str(row["user_id"]),
        email=str(row["email"]),
        email_verified=bool(row["email_verified"]),
        gotrue_session_id=_nonempty_str(row["gotrue_session_id"]),
        created_at=_parse_dt(row["created_at"]),
        expires_at=_parse_dt(row["expires_at"]),
        last_used_at=_parse_dt(row["last_used_at"]),
        access_expires_at=_parse_dt(row["access_expires_at"]),
    )


def _row_to_user(row: _SessionRow) -> User:
    return User(
        id=row.user_id,
        email=row.email,
        email_verified=row.email_verified,
        subscription_status=SubscriptionStatus.FREE,
        plan_id=PlanId.FREE,
        created_at=row.created_at,
        updated_at=row.last_used_at,
    )


def _row_to_session(row: _SessionRow) -> Session:
    return Session(
        id=row.gotrue_session_id or row.session_hash,
        user_id=row.user_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _token_secret_ref(user_id: str, session_hash: str, kind: str) -> str:
    return "desktop_gotrue:%s:%s:%s" % (_require_user_id(user_id), session_hash, kind)


def _require_user_id(user_id: str) -> str:
    normalized = (user_id or "").strip()
    if not normalized:
        raise ValueError("user_id is required")
    return normalized


def _require_uuid_user_id(user_id: str) -> None:
    try:
        UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise DesktopSessionError("Desktop cloud user_id must be a UUID") from exc


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonempty_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _format_dt(value: datetime) -> str:
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise ValueError("invalid datetime value")


__all__ = [
    "LOCAL_SESSION_PREFIX",
    "DesktopAccountIdentity",
    "DesktopSessionError",
    "DesktopSessionRefreshTransientError",
    "DesktopSessionStore",
    "create_desktop_session_from_gotrue_payload",
    "delete_desktop_session_token",
    "desktop_access_token_for_active_session",
    "desktop_access_token_for_session_token",
    "get_desktop_account_identity",
    "get_desktop_session_store",
    "is_desktop_local_session_token",
    "purge_desktop_sessions_for_user",
    "reset_desktop_session_store_for_tests",
    "serve_desktop_gotrue_refresh_grant",
    "validate_desktop_session_token",
]
