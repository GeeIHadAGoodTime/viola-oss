"""
Database Layer for Viola Authentication.

This module provides repository implementations for storing users, sessions,
magic links, and OAuth identities. Supports both SQLite (local) and
PostgreSQL (cloud) backends.

Usage:
    >>> from auth.database import SQLiteAuthDatabase, get_auth_db
    >>> db = SQLiteAuthDatabase("auth.db")
    >>> await db.initialize()
    >>> user = await db.users.create_user(...)

Architecture:
    - AuthDatabase: Abstract interface for auth data storage
    - SQLiteAuthDatabase: SQLite implementation for local/dev
    - PostgresAuthDatabase: PostgreSQL implementation for cloud
    - Repository classes for each entity type
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias, TypedDict, cast

from auth.models import (
    MagicLink,
    MagicLinkInDB,
    OAuthIdentity,
    OAuthProvider,
    Session,
    SessionInDB,
    Subscription,
    SubscriptionSource,
    SubscriptionStatus,
    User,
    UserInDB,
)
from auth.utils import mask_email
from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from core.product import PlanId, coerce_plan_id

logger = get_logger("viola.auth.database")

DbParam: TypeAlias = str | int | float | bytes | None


class _UserRow(TypedDict):
    id: str
    email: str | None
    email_verified: int
    email_encrypted: str | None
    phone: str | None
    phone_verified: int
    sms_consent: int
    sms_consent_at: str | None
    sms_consent_text_version: str | None
    created_at: str
    updated_at: str


class _SubscriptionRow(TypedDict, total=False):
    id: str
    user_id: str
    status: str
    plan: str | None
    payment_provider: str | None
    external_subscription_id: str | None
    current_period_start: str | None
    current_period_end: str | None
    canceled_at: str | None
    subscription_source: str
    granted_by_admin_token_digest: str | None
    granted_reason: str | None
    created_at: str
    updated_at: str
    activation_pending: int


# =============================================================================
# Repository Protocols
# =============================================================================


class UserRepository(ABC):
    """Abstract repository for user operations."""

    @abstractmethod
    async def create_user(
        self,
        id: str,
        email: str,
        email_verified: bool = False,
        password_hash: str | None = None,
        tos_accepted_at: str | None = None,
    ) -> User:
        """Create a new user."""
        ...

    @abstractmethod
    async def get_user_by_id(self, user_id: str) -> User | None:
        """Get user by ID."""
        ...

    @abstractmethod
    async def get_user_by_email(self, email: str) -> User | None:
        """Get user by email."""
        ...

    @abstractmethod
    async def get_user_with_password(self, email: str) -> UserInDB | None:
        """Get user with password hash for authentication."""
        ...

    @abstractmethod
    async def update_user(self, user_id: str, **updates: DbParam) -> User | None:
        """Update user fields."""
        ...

    @abstractmethod
    async def update_password(self, user_id: str, password_hash: str) -> bool:
        """Update user password."""
        ...

    async def set_password_hash(self, user_id: str, password_hash: str) -> bool:
        """Compatibility alias for password-reset call sites."""
        return await self.update_password(user_id, password_hash)

    @abstractmethod
    async def verify_email(self, user_id: str) -> bool:
        """Mark user email as verified."""
        ...

    @abstractmethod
    async def delete_user(self, user_id: str) -> bool:
        """Delete user and all related data."""
        ...

    @abstractmethod
    async def is_orphan_account(self, user_id: str) -> bool:
        """Check if a user account is an orphan (no password, unverified, no active subscription)."""
        ...

    @abstractmethod
    async def cleanup_orphan_accounts(self, max_age_hours: int = 168) -> int:
        """Delete orphan accounts older than max_age_hours (default 7 days).

        Returns the number of accounts deleted.
        """
        ...

    @abstractmethod
    async def upgrade_orphan_account(self, user_id: str, password_hash: str) -> User | None:
        """Upgrade an orphan guest-checkout account by setting a password.

        Returns the updated User, or None if not an orphan.
        """
        ...


class SessionRepository(ABC):
    """Abstract repository for session operations.

    Note: For type hints, prefer the Protocol in auth.sessions.SessionRepository
    which provides structural subtyping. This ABC exists for explicit inheritance.
    """

    @abstractmethod
    async def create_session(self, session: SessionInDB) -> Session:
        """Store a new session."""
        ...

    @abstractmethod
    async def get_session_by_token_hash(self, token_hash: str) -> SessionInDB | None:
        """Get session by token hash."""
        ...

    @abstractmethod
    async def get_session_by_id(self, session_id: str) -> SessionInDB | None:
        """Get session by ID."""
        ...

    @abstractmethod
    async def get_sessions_by_user(self, user_id: str) -> list[Session]:
        """Get all sessions for a user."""
        ...

    @abstractmethod
    async def update_session_last_used(self, session_id: str) -> None:
        """Update session last_used_at."""
        ...

    @abstractmethod
    async def update_session_expires(self, session_id: str, new_expires_at: datetime) -> None:
        """Update session expiration time (for sliding window refresh)."""
        ...

    @abstractmethod
    async def rotate_session_token(
        self,
        session_id: str,
        old_token_hash: str,
        new_token_hash: str,
        new_expires_at: datetime,
        ip_address: str | None = None,
    ) -> SessionInDB | None:
        """Atomically replace a refresh token hash."""
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        ...

    @abstractmethod
    async def delete_sessions_by_user(self, user_id: str) -> int:
        """Delete all sessions for a user."""
        ...


class MagicLinkRepository(ABC):
    """Abstract repository for magic link operations."""

    @abstractmethod
    async def create_magic_link(self, link: MagicLinkInDB) -> MagicLink:
        """Store a new magic link."""
        ...

    @abstractmethod
    async def get_magic_link_by_token_hash(self, token_hash: str) -> MagicLinkInDB | None:
        """Get magic link by token hash."""
        ...

    @abstractmethod
    async def mark_magic_link_used(self, link_id: str) -> None:
        """Mark magic link as used."""
        ...

    @abstractmethod
    async def invalidate_links_for_email(self, email: str) -> int:
        """Invalidate all unused magic links for an email. Returns count invalidated."""
        ...

    @abstractmethod
    async def count_recent_links_for_email(self, email: str, since: datetime) -> int:
        """Count recent magic links for rate limiting."""
        ...

    @abstractmethod
    async def cleanup_expired_links(self) -> int:
        """Delete expired magic links."""
        ...


class OAuthIdentityRepository(ABC):
    """Abstract repository for OAuth identity operations."""

    @abstractmethod
    async def create(self, identity: OAuthIdentity) -> OAuthIdentity:
        """Create OAuth identity link."""
        ...

    @abstractmethod
    async def get_by_provider_user_id(
        self,
        provider: OAuthProvider,
        provider_user_id: str,
    ) -> OAuthIdentity | None:
        """Get identity by provider and provider user ID."""
        ...

    @abstractmethod
    async def get_by_user_id(self, user_id: str) -> list[OAuthIdentity]:
        """Get all OAuth identities for a user."""
        ...

    @abstractmethod
    async def delete(self, identity_id: str) -> bool:
        """Delete OAuth identity."""
        ...


class SubscriptionRepository(ABC):
    """Abstract repository for subscription operations."""

    @abstractmethod
    async def get_subscription(self, user_id: str) -> Subscription | None:
        """Get subscription for user."""
        ...

    @abstractmethod
    async def create_subscription(self, subscription: Subscription) -> Subscription:
        """Create subscription."""
        ...

    @abstractmethod
    async def update_subscription(
        self,
        user_id: str,
        **updates: DbParam,
    ) -> Subscription | None:
        """Update subscription."""
        ...


# =============================================================================
# SQLite Implementation
# =============================================================================


def _quote_sqlite_identifier(identifier: str) -> str:
    """AUTH-08: return a safely double-quoted SQLite identifier.

    SQLite has no parameter-binding form for table/column names, so we
    validate the raw identifier and then emit it inside the SQLite
    identifier-quote form (``"name"``). Rejects any identifier containing
    characters that could escape the quote (``"``, NUL, control bytes) or
    that is empty.
    """
    if not isinstance(identifier, str) or not identifier:
        raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    for ch in identifier:
        # Reject embedded double-quote, NUL, and any ASCII control character.
        if ch == '"' or ord(ch) < 0x20:
            raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    return '"' + identifier + '"'


def _restrict_windows_permissions(path: Path) -> None:
    """Restrict a file or directory to the current user on Windows via icacls.

    Uses a marker file so icacls (which takes ~10s) only runs once per
    install rather than on every process start.  Permissions persist
    across restarts — re-running icacls every boot is waste.
    """
    from subprocess import SubprocessError

    from core.subprocess_utils import run_silent

    marker = path / ".permissions_set"
    if marker.exists():
        return

    try:
        username = os.environ.get("USERNAME", "")
        if not username:
            return
        # Remove inherited permissions and grant full control to current user only
        result = run_silent(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                "%s:(OI)(CI)F" % username,
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            try:
                marker.write_text("1")
            except OSError:
                logger.debug("Failed to write Windows permission marker for %s", path)
    except (OSError, RuntimeError, SubprocessError):
        logger.debug("Failed to restrict Windows permissions on %s", path, exc_info=True)


def _resolve_user_email_from_row(row: dict[str, Any]) -> str:
    """Return the account email from plaintext or encrypted storage."""
    email = str(row.get("email") or "").strip()
    if email:
        return email

    encrypted_email = str(row.get("email_encrypted") or "").strip()
    if not encrypted_email:
        return ""

    try:
        from auth.field_encryption import get_field_encryptor

        return get_field_encryptor().decrypt(encrypted_email).strip()
    except Exception as exc:
        logger.debug("Could not decrypt account email for user %s: %s", row.get("id"), exc)
        return ""


class SQLiteAuthDatabase:
    """
    SQLite-based auth database for local/development use.

    Thread-safe implementation using connection pooling.
    """

    SCHEMA = """
    -- Users table
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT COLLATE NOCASE,  -- nullable: plaintext scrubbed after encryption backfill
        email_verified INTEGER DEFAULT 0,
        password_hash TEXT,
        encryption_salt TEXT,
        email_encrypted TEXT,
        email_hmac TEXT UNIQUE,
        tos_accepted_at TEXT,
        phone TEXT,
        phone_verified INTEGER NOT NULL DEFAULT 0,
        sms_consent INTEGER NOT NULL DEFAULT 0,
        sms_consent_at TEXT,
        sms_consent_text_version TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
    CREATE INDEX IF NOT EXISTS idx_users_email_hmac ON users(email_hmac);
    CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_verified_phone_unique
        ON users(phone)
        WHERE phone IS NOT NULL AND phone_verified = 1;

    -- Sessions table
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT UNIQUE NOT NULL,
        device_name TEXT,
        device_id TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        ip_address TEXT,
        user_agent_hash TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
    CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);

    -- Magic links table
    CREATE TABLE IF NOT EXISTS magic_links (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL COLLATE NOCASE,
        token_hash TEXT UNIQUE NOT NULL,
        short_code TEXT,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_magic_links_token_hash ON magic_links(token_hash);
    CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email);
    CREATE INDEX IF NOT EXISTS idx_magic_links_email_short_code
        ON magic_links(email, short_code);

    -- OAuth identities table
    CREATE TABLE IF NOT EXISTS oauth_identities (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        provider_user_id TEXT NOT NULL,
        email TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(provider, provider_user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_oauth_provider ON oauth_identities(provider, provider_user_id);

    -- Subscriptions table
    CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY,
        user_id TEXT UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'free',
        plan TEXT,
        payment_provider TEXT,
        external_subscription_id TEXT,
        current_period_start TEXT,
        current_period_end TEXT,
        canceled_at TEXT,
        subscription_source TEXT NOT NULL DEFAULT 'commercial',
        granted_by_admin_token_digest TEXT,
        granted_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        activation_pending INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);

    -- User devices table
    CREATE TABLE IF NOT EXISTS user_devices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        device_name TEXT NOT NULL,
        device_type TEXT,
        last_seen_at TEXT,
        room_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_devices_user_id ON user_devices(user_id);

    -- OAuth state table (for CSRF protection, survives restarts)
    CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        code_verifier TEXT,
        redirect_uri TEXT NOT NULL,
        extra_data TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_oauth_states_expires ON oauth_states(expires_at);

    -- OAuth tokens table (for storing provider tokens with encryption)
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        access_token_encrypted TEXT,
        refresh_token_encrypted TEXT,
        id_token TEXT,
        expires_at TEXT,
        scope TEXT,
        key_version INTEGER DEFAULT 0,
        user_salt TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(user_id, provider)
    );
    CREATE INDEX IF NOT EXISTS idx_oauth_tokens_user_id ON oauth_tokens(user_id);
    CREATE INDEX IF NOT EXISTS idx_oauth_tokens_provider ON oauth_tokens(provider);

    -- User settings table (cross-device sync)
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        settings_json TEXT NOT NULL DEFAULT '{}',
        version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- SMS verification and consent state
    CREATE TABLE IF NOT EXISTS sms_verifications (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        phone TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts_remaining INTEGER NOT NULL DEFAULT 5,
        telnyx_message_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, phone)
    );
    CREATE INDEX IF NOT EXISTS idx_sms_verifications_user_id ON sms_verifications(user_id);
    CREATE INDEX IF NOT EXISTS idx_sms_verifications_phone ON sms_verifications(phone);
    CREATE INDEX IF NOT EXISTS idx_sms_verifications_expires ON sms_verifications(expires_at);

    CREATE TABLE IF NOT EXISTS sms_otp_sends (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        phone TEXT NOT NULL,
        sent_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sms_otp_sends_user ON sms_otp_sends(user_id, sent_at);
    CREATE INDEX IF NOT EXISTS idx_sms_otp_sends_phone ON sms_otp_sends(phone, sent_at);

    CREATE TABLE IF NOT EXISTS sms_consent_audit (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        phone_last4 TEXT,
        consent_text_version TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sms_consent_audit_user ON sms_consent_audit(user_id, created_at);

    CREATE TABLE IF NOT EXISTS sms_inbound_audit (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        phone TEXT NOT NULL,
        text TEXT NOT NULL,
        received_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sms_inbound_audit_user ON sms_inbound_audit(user_id, received_at);

    CREATE TABLE IF NOT EXISTS sms_phone_opt_outs (
        phone TEXT PRIMARY KEY,
        opted_out_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'telnyx',
        last_to_number TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- User profile documents
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id TEXT PRIMARY KEY,
        profile_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- User model documents
    CREATE TABLE IF NOT EXISTS user_models (
        user_id TEXT PRIMARY KEY,
        model_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- Per-user preferences (key-value, user-scoped settings)
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, key)
    );
    CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id ON user_preferences(user_id);

    -- GDPR audit log table (persists after user deletion)
    CREATE TABLE IF NOT EXISTS gdpr_audit_log (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        user_email TEXT,
        action TEXT NOT NULL,
        reason TEXT,
        details TEXT,
        ip_address TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_gdpr_audit_user_id ON gdpr_audit_log(user_id);

    -- LLM usage logs table
    CREATE TABLE IF NOT EXISTS llm_usage_logs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider TEXT,
        model TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_llm_usage_user_id ON llm_usage_logs(user_id);

    -- MFA TOTP table
    CREATE TABLE IF NOT EXISTS mfa_totp (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        secret_encrypted TEXT NOT NULL,
        enabled INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    -- MFA backup codes table
    CREATE TABLE IF NOT EXISTS mfa_backup_codes (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mfa_backup_codes_user_id ON mfa_backup_codes(user_id);

    -- WebAuthn credentials table
    CREATE TABLE IF NOT EXISTS webauthn_credentials (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        credential_id TEXT UNIQUE NOT NULL,
        public_key TEXT NOT NULL,
        sign_count INTEGER DEFAULT 0,
        name TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_webauthn_user_id ON webauthn_credentials(user_id);

    -- Per-user service credentials (messaging tokens, etc.)
    CREATE TABLE IF NOT EXISTS user_credentials (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        service TEXT NOT NULL,
        credential_encrypted TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, service)
    );
    CREATE INDEX IF NOT EXISTS idx_user_credentials_user_id ON user_credentials(user_id);

    -- LLM quotas table
    CREATE TABLE IF NOT EXISTS llm_quotas (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        daily_requests INTEGER DEFAULT 0,
        daily_tokens INTEGER DEFAULT 0,
        monthly_requests INTEGER DEFAULT 0,
        monthly_tokens INTEGER DEFAULT 0,
        daily_reset_at TEXT,
        monthly_reset_at TEXT,
        updated_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str | Path) -> None:
        """
        Initialize SQLite auth database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._initialized = False
        self._permissions_set = False

        # Create repository instances
        self.users = SQLiteUserRepository(self)
        self.sessions = SQLiteSessionRepository(self)
        self.magic_links = SQLiteMagicLinkRepository(self)
        self.oauth_identities = SQLiteOAuthIdentityRepository(self)
        self.subscriptions = SQLiteSubscriptionRepository(self)
        self.oauth_states = SQLiteOAuthStateRepository(self)
        self.oauth_tokens = SQLiteOAuthTokenRepository(self)
        self.user_settings = SQLiteUserSettingsRepository(self)
        self.sms = SQLiteSmsRepository(self)
        self.user_preferences = SQLiteUserPreferencesRepository(self)
        self.gdpr_audit = SQLiteGDPRAuditRepository(self)
        self.llm_usage = SQLiteLLMUsageRepository(self)
        self.llm_quotas = SQLiteLLMQuotaRepository(self)
        self.user_devices = SQLiteUserDeviceRepository(self)
        self.mfa_totp = SQLiteMFATOTPRepository(self)
        self.mfa_backup_codes = SQLiteMFABackupCodesRepository(self)
        self.webauthn = SQLiteWebAuthnRepository(self)
        self.user_credentials = SQLiteUserCredentialsRepository(self)

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            # Restrict data directory to owner-only access (once per process,
            # not per thread — icacls subprocess takes up to 10s on Windows).
            if not self._permissions_set:
                self._permissions_set = True
                if sys.platform != "win32":
                    os.chmod(self.db_path.parent, stat.S_IRWXU)
                else:
                    _restrict_windows_permissions(self.db_path.parent)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = conn
        return self._local.connection

    @property
    def connection(self) -> sqlite3.Connection:
        """Get database connection."""
        return self._get_connection()

    def _in_transaction(self) -> bool:
        """Return whether the current thread is inside a managed transaction."""
        return getattr(self._local, "transaction_depth", 0) > 0

    def _commit_if_needed(self, conn: sqlite3.Connection) -> None:
        """Commit writes unless an outer managed transaction will handle it."""
        if not self._in_transaction():
            conn.commit()

    @contextmanager
    def transaction(self):
        """
        Execute multiple repository writes atomically on the thread-local connection.

        Nested callers share the same outer transaction boundary.
        """
        conn = self._get_connection()
        depth = getattr(self._local, "transaction_depth", 0)

        if depth == 0:
            conn.execute("BEGIN")
        self._local.transaction_depth = depth + 1

        try:
            yield conn
            if depth == 0:
                conn.commit()
        except Exception:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._local.transaction_depth = depth

    async def initialize(self) -> None:
        """Initialize database schema."""
        if self._initialized:
            return

        conn = self._get_connection()

        # Add columns BEFORE executescript for existing databases.
        # The schema's CREATE INDEX references columns (e.g. email_hmac) that
        # may not exist in old tables.  CREATE TABLE IF NOT EXISTS skips
        # existing tables, so those columns never get added by the schema —
        # but CREATE INDEX still runs and fails.  Pre-migrating adds the
        # columns first so the indexes succeed.
        # For new databases _add_column_if_missing is a no-op (no tables yet).
        self._add_missing_columns(conn)

        conn.executescript(self.SCHEMA)
        conn.commit()

        # Run full migrations (columns + backfill) — for brand-new databases
        # the first _add_missing_columns was a no-op, so columns are added
        # here; for existing databases the backfill runs.
        self._run_migrations(conn)

        self._initialized = True
        logger.info("Auth database initialized at %s", self.db_path)

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        """Add columns that may be missing from older database versions.

        Safe to call before or after executescript — skips tables that
        don't exist yet (new database) and columns that already exist.
        """
        # Migration: Add ip_address and user_agent_hash to sessions table
        self._add_column_if_missing(conn, "sessions", "ip_address", "TEXT")
        self._add_column_if_missing(conn, "sessions", "user_agent_hash", "TEXT")

        # Migration: Add key_version and user_salt to oauth_tokens table
        self._add_column_if_missing(conn, "oauth_tokens", "key_version", "INTEGER DEFAULT 0")
        self._add_column_if_missing(conn, "oauth_tokens", "user_salt", "TEXT")

        # Migration: Add encryption_salt to users table (per-user OAuth key derivation)
        self._add_column_if_missing(conn, "users", "encryption_salt", "TEXT")

        # Migration: Add encrypted email columns to users table
        self._add_column_if_missing(conn, "users", "email_encrypted", "TEXT")
        self._add_column_if_missing(conn, "users", "email_hmac", "TEXT")

        # Migration: Add ToS clickwrap acceptance timestamp to users table
        self._add_column_if_missing(conn, "users", "tos_accepted_at", "TEXT")

        # Migration: Add SMS consent and phone verification fields.
        self._add_column_if_missing(conn, "users", "phone", "TEXT")
        self._add_column_if_missing(conn, "users", "phone_verified", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "users", "sms_consent", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing(conn, "users", "sms_consent_at", "TEXT")
        self._add_column_if_missing(conn, "users", "sms_consent_text_version", "TEXT")

        # Migration: Add activation_pending to subscriptions table so paid
        # subscriptions can stay pending until the buyer's email is verified.
        self._add_column_if_missing(
            conn,
            "subscriptions",
            "activation_pending",
            "INTEGER NOT NULL DEFAULT 0",
        )
        self._add_column_if_missing(
            conn,
            "subscriptions",
            "subscription_source",
            "TEXT NOT NULL DEFAULT 'commercial'",
        )
        self._add_column_if_missing(conn, "subscriptions", "granted_by_admin_token_digest", "TEXT")
        self._add_column_if_missing(conn, "subscriptions", "granted_reason", "TEXT")

        # Migration: Add Path C2 short_code to magic_links so the typed
        # 8-char login code flow works on databases created before that
        # column existed. Without this, the lazy ALTER inside
        # SQLiteMagicLinkRepository.create_magic_link can swallow a real
        # ALTER failure and the next INSERT will surface "no such column".
        self._add_column_if_missing(conn, "magic_links", "short_code", "TEXT")

        conn.commit()

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply incremental schema migrations for existing databases."""
        self._add_missing_columns(conn)
        self._ensure_user_document_tables(conn)
        self._ensure_users_email_nullable(conn)

        # Backfill: encrypt existing plaintext emails
        self._backfill_encrypted_emails(conn)

    @classmethod
    def _ensure_users_email_nullable(cls, conn: sqlite3.Connection) -> None:
        """Rebuild legacy users tables whose plaintext email column is NOT NULL.

        AUTH-12 scrubs plaintext email after encrypted-email backfill. Older
        databases may still have ``users.email TEXT NOT NULL`` from the
        pre-scrub schema, which makes startup fail before auth rate limiting can
        answer login spam with a 429.
        """
        table_info = conn.execute('PRAGMA table_info("users")').fetchall()
        if not table_info:
            return

        email_info = next((row for row in table_info if row[1] == "email"), None)
        if email_info is None or not bool(email_info[3]):
            return

        existing_columns = {row[1] for row in table_info}
        column_defs = [
            ("id", "TEXT PRIMARY KEY"),
            ("email", "TEXT COLLATE NOCASE"),
            ("email_verified", "INTEGER DEFAULT 0"),
            ("password_hash", "TEXT"),
            ("encryption_salt", "TEXT"),
            ("email_encrypted", "TEXT"),
            ("email_hmac", "TEXT UNIQUE"),
            ("tos_accepted_at", "TEXT"),
            ("created_at", "TEXT NOT NULL"),
            ("updated_at", "TEXT NOT NULL"),
        ]
        optional_defs = [
            ("email_scrubbed", "INTEGER NOT NULL DEFAULT 0"),
            ("deleted_at", "TEXT"),
        ]
        column_defs.extend((name, sql) for name, sql in optional_defs if name in existing_columns)

        shared_columns = [name for name, _sql in column_defs if name in existing_columns]
        if "id" not in shared_columns:
            logger.warning("Skipping users.email nullability migration: users.id column missing")
            return

        create_columns_sql = ",\n                ".join(f"{name} {sql}" for name, sql in column_defs)
        copy_columns_sql = ", ".join(cls._quote_column(name) for name in shared_columns)

        conn.commit()
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            conn.execute("DROP TABLE IF EXISTS users_email_nullable_migration")
            conn.execute(f"""
                CREATE TABLE users_email_nullable_migration (
                    {create_columns_sql}
                )
                """)  # nosec B608 - DDL is built from hard-coded column definitions.
            # Column names are allow-listed above.
            copy_users_sql = (
                f"INSERT INTO users_email_nullable_migration ({copy_columns_sql}) "  # nosec B608
                f"SELECT {copy_columns_sql} FROM users"
            )
            conn.execute(copy_users_sql)
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_email_nullable_migration RENAME TO users")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_hmac ON users(email_hmac)")
            conn.commit()
            logger.info("Migrated users.email to nullable plaintext-scrub schema")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            logger.exception("Failed to migrate users.email nullability")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON" if foreign_keys_enabled else "PRAGMA foreign_keys=OFF")

    @classmethod
    def _ensure_user_document_tables(cls, conn: sqlite3.Connection) -> None:
        """Ensure per-user profile/model/preferences tables exist on upgraded databases."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_models (
                user_id TEXT PRIMARY KEY,
                model_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id
                ON user_preferences(user_id);
            """)
        for table in cls._USER_CACHE_TABLES:
            cls._ensure_user_cache_table_fk_free(conn, table)
        conn.commit()

    # AUTH-08: identifier allow-list for DDL that cannot use parameter binding.
    # SQLite has no bind syntax for table/column identifiers, so every path
    # that formats them into DDL text must validate against a whitelist to
    # avoid subtle injection if these helpers are later fed user data.
    _ALLOWED_TABLES: frozenset[str] = frozenset(
        {
            "users",
            "sessions",
            "magic_links",
            "oauth_identities",
            "oauth_tokens",
            "subscriptions",
            "user_devices",
            "user_settings",
            "user_profiles",
            "user_models",
            "user_preferences",
            "mfa_totp",
            "mfa_backup_codes",
            "mfa_recovery_codes",
            "llm_usage_logs",
            "llm_quotas",
            "gdpr_audit_log",
            "sms_verifications",
            "sms_otp_sends",
            "sms_consent_audit",
            "sms_inbound_audit",
            "sms_phone_opt_outs",
            "login_attempts",
            "auth_audit_log",
            "webauthn_credentials",
            "api_keys",
            "oauth_tokens_v2",
            "jwks_cache",
        }
    )
    _ALLOWED_COLUMNS: frozenset[str] = frozenset(
        {
            "id",
            "profile_json",
            "model_json",
            "settings_json",
            "user_id",
            "key",
            "value",
            "version",
            "email",
            "email_encrypted",
            "email_hmac",
            "email_scrubbed",
            "deleted_at",
            "activation_pending",
            "subscription_source",
            "granted_by_admin_token_digest",
            "granted_reason",
            "created_at",
            "updated_at",
            "last_seen_at",
            "tos_accepted_at",
            "phone",
            "phone_verified",
            "sms_consent",
            "sms_consent_at",
            "sms_consent_text_version",
            "code_hash",
            "sent_at",
            "attempts_remaining",
            "telnyx_message_id",
            "action",
            "phone_last4",
            "consent_text_version",
            "encryption_salt",
            "password_hash",
            "email_verified",
            "ip_address",
            "user_agent_hash",
            "user_agent",
            "key_version",
            "user_salt",
            "expires_at",
            "used_at",
            "device_name",
            "device_id",
            "device_type",
            "room_id",
            "plan",
            "status",
            "external_subscription_id",
            "payment_provider",
            "current_period_start",
            "current_period_end",
            "canceled_at",
            "provider",
            "provider_user_id",
            "token_hash",
            "short_code",
        }
    )

    @classmethod
    def _assert_identifier(cls, ident: str, *, kind: str) -> str:
        """AUTH-08: assert an identifier is on the table/column whitelist.

        SQLite DDL cannot use ``?`` binding for identifiers, so the only
        safe way to format them into a statement is to refuse anything that
        isn't a known constant. The SQLite identifier-quote (``"x"``) is
        returned for use in statements.
        """
        allowed = cls._ALLOWED_TABLES if kind == "table" else cls._ALLOWED_COLUMNS
        if ident not in allowed:
            raise ValueError(f"Refusing to build DDL for unknown {kind} identifier: {ident!r}")
        # Double any embedded quote to be safe, then wrap in double-quotes —
        # SQLite's identifier-quote form.
        return '"' + ident.replace('"', '""') + '"'

    @classmethod
    def _quote_table(cls, name: str) -> str:
        return cls._assert_identifier(name, kind="table")

    @classmethod
    def _quote_column(cls, name: str) -> str:
        return cls._assert_identifier(name, kind="column")

    _USER_CACHE_TABLES: tuple[str, ...] = (
        "user_settings",
        "user_profiles",
        "user_models",
        "user_preferences",
    )
    _USER_CACHE_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
        "user_settings": ("user_id", "settings_json", "version", "created_at", "updated_at"),
        "user_profiles": ("user_id", "profile_json", "created_at", "updated_at"),
        "user_models": ("user_id", "model_json", "created_at", "updated_at"),
        "user_preferences": ("user_id", "key", "value", "updated_at"),
    }
    _USER_CACHE_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
        "user_settings": frozenset({"user_id"}),
        "user_profiles": frozenset({"user_id"}),
        "user_models": frozenset({"user_id"}),
        "user_preferences": frozenset({"user_id", "key"}),
    }

    @classmethod
    def _create_user_cache_table(cls, conn: sqlite3.Connection, table: str) -> None:
        table_q = cls._quote_table(table)
        if table == "user_settings":
            conn.execute(f"""
                CREATE TABLE {table_q} (
                    user_id TEXT PRIMARY KEY,
                    settings_json TEXT NOT NULL DEFAULT '{{}}',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)  # nosec B608 - table identifier is allow-listed.
            return
        if table == "user_profiles":
            conn.execute(f"""
                CREATE TABLE {table_q} (
                    user_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)  # nosec B608 - table identifier is allow-listed.
            return
        if table == "user_models":
            conn.execute(f"""
                CREATE TABLE {table_q} (
                    user_id TEXT PRIMARY KEY,
                    model_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)  # nosec B608 - table identifier is allow-listed.
            return
        if table == "user_preferences":
            conn.execute(f"""
                CREATE TABLE {table_q} (
                    user_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                )
                """)  # nosec B608 - table identifier is allow-listed.
            return
        raise ValueError(f"unsupported user cache table: {table!r}")

    @classmethod
    def _ensure_user_cache_table_fk_free(cls, conn: sqlite3.Connection, table: str) -> None:
        """Rebuild legacy desktop cache tables that FK ``user_id`` to ``users``."""
        if table not in cls._USER_CACHE_TABLES:
            raise ValueError(f"unsupported user cache table: {table!r}")

        table_q = cls._quote_table(table)
        table_info = conn.execute(f"PRAGMA table_info({table_q})").fetchall()  # nosec B608
        if not table_info:
            return

        fk_rows = conn.execute(f"PRAGMA foreign_key_list({table_q})").fetchall()  # nosec B608
        if not any(row["from"] == "user_id" and row["table"] == "users" for row in fk_rows):
            return

        logger.info("Migrating %s to remove legacy users(id) foreign key", table)
        cls._rebuild_user_cache_table_fk_free(conn, table, table_info)

    @classmethod
    def _rebuild_user_cache_table_fk_free(
        cls,
        conn: sqlite3.Connection,
        table: str,
        table_info: list[sqlite3.Row],
    ) -> None:
        table_q = cls._quote_table(table)
        legacy_name = f"{table}_legacy_users_fk"
        legacy_q = '"' + legacy_name.replace('"', '""') + '"'
        existing_columns = {str(row["name"]) for row in table_info}
        required_columns = cls._USER_CACHE_REQUIRED_COLUMNS[table]
        missing_required = required_columns - existing_columns
        if missing_required:
            raise RuntimeError("Cannot migrate %s: missing required columns %s" % (table, sorted(missing_required)))

        defaults: dict[str, object] = {
            "settings_json": "{}",
            "version": 1,
            "profile_json": "{}",
            "model_json": "{}",
            "value": "",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        insert_columns = cls._USER_CACHE_TABLE_COLUMNS[table]
        insert_sql = ", ".join(cls._quote_column(column) for column in insert_columns)
        select_exprs: list[str] = []
        params: list[object] = []
        for column in insert_columns:
            if column in existing_columns:
                column_q = cls._quote_column(column)
                if column in defaults:
                    select_exprs.append(f"COALESCE({column_q}, ?)")
                    params.append(defaults[column])
                else:
                    select_exprs.append(column_q)
            else:
                select_exprs.append("?")
                params.append(defaults[column])

        required_where = " AND ".join(f"{cls._quote_column(column)} IS NOT NULL" for column in required_columns)
        copy_sql = (
            f"INSERT INTO {table_q} ({insert_sql}) "  # nosec B608 - identifiers are allow-listed.
            f"SELECT {', '.join(select_exprs)} FROM {legacy_q} WHERE {required_where}"
        )

        conn.commit()
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            conn.execute(f"DROP TABLE IF EXISTS {legacy_q}")  # nosec B608 - generated internal table name.
            conn.execute(f"ALTER TABLE {table_q} RENAME TO {legacy_q}")  # nosec B608 - identifiers are allow-listed.
            cls._create_user_cache_table(conn, table)
            conn.execute(copy_sql, tuple(params))
            conn.execute(f"DROP TABLE {legacy_q}")  # nosec B608 - generated internal table name.
            if table == "user_preferences":
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_user_id ON user_preferences(user_id)")
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            logger.exception("Failed to migrate %s to FK-free desktop cache schema", table)
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON" if foreign_keys_enabled else "PRAGMA foreign_keys=OFF")

    # AUTH-08: only the following legacy document-table helper inputs are
    # accepted. Any other combination is treated as a bug or injection attempt.
    _USER_DOCUMENT_CASCADE_PAIRS: frozenset[tuple[str, str]] = frozenset(
        {
            ("user_profiles", "profile_json"),
            ("user_models", "model_json"),
        }
    )

    @classmethod
    def _ensure_user_document_cascade(cls, conn: sqlite3.Connection, table: str, json_column: str) -> None:
        """Compatibility wrapper for the retired document-cascade migration.

        Refuses any ``(table, json_column)`` pair not explicitly on the
        whitelist — a typo or an injected identifier from an untrusted
        source must never flow into DDL here.
        """
        if table not in {t for t, _ in cls._USER_DOCUMENT_CASCADE_PAIRS}:
            raise ValueError(f"unsupported cascade migration table: {table!r}")
        allowed_columns = {c for t, c in cls._USER_DOCUMENT_CASCADE_PAIRS if t == table}
        if json_column not in allowed_columns:
            raise ValueError(f"unsupported cascade migration column for table {table!r}: {json_column!r}")
        cls._ensure_user_cache_table_fk_free(conn, table)

    @staticmethod
    def _backfill_encrypted_emails(conn: sqlite3.Connection) -> None:
        """Encrypt existing plaintext emails that haven't been encrypted yet.

        AUTH-12: marks rows with an ``email_scrubbed=1`` tombstone after
        nulling the plaintext column, so a retrying backfill doesn't
        re-encrypt and re-scrub rows that are already fully migrated. The
        previous implementation used ``email_hmac IS NULL`` as the gate,
        but that condition also matches rows where hmac backfill previously
        crashed mid-write — making the backfill non-idempotent.
        """
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
        except (ValueError, Exception) as e:
            # JWT_SECRET not configured — skip encryption backfill
            logger.debug("Skipping email encryption backfill: %s", e)
            return

        # AUTH-12: ensure the tombstone column exists (idempotent migration).
        try:
            SQLiteAuthDatabase._add_column_if_missing(
                conn,
                "users",
                "email_scrubbed",
                "INTEGER NOT NULL DEFAULT 0",
            )
        except Exception:  # pragma: no cover — column migration is best-effort
            logger.exception("Failed to add email_scrubbed tombstone column")

        # Only touch rows that still have plaintext AND haven't been marked
        # scrubbed yet. Rows already scrubbed stay untouched (idempotent).
        rows = conn.execute("""
            SELECT id, email FROM users
            WHERE email IS NOT NULL
              AND (email_scrubbed IS NULL OR email_scrubbed = 0)
            """).fetchall()

        if not rows:
            return
        count = 0
        for row in rows:
            data = dict(row)
            email = data["email"]
            # Always (re)write the encrypted columns so partial migrations
            # converge — the encryptor is deterministic-enough for blind
            # index HMAC and idempotent for cipher-text as far as correctness
            # is concerned; the ciphertext rotation is handled by a
            # separate key-version backfill.
            conn.execute(
                "UPDATE users SET email_encrypted = ?, email_hmac = ? WHERE id = ?",
                (encryptor.encrypt(email), encryptor.blind_index(email), data["id"]),
            )
            conn.execute(
                "UPDATE users SET email = NULL, email_scrubbed = 1 WHERE id = ?",
                (data["id"],),
            )
            count += 1

        if count > 0:
            conn.commit()
            logger.info("Backfilled encrypted emails for %d users", count)

    # Allow-list for ALTER TABLE ... ADD COLUMN type specifiers. SQLite is
    # forgiving here, but we still constrain the set of types to avoid DDL
    # surprises if a typo is introduced.
    _ALLOWED_COLUMN_TYPES: frozenset[str] = frozenset(
        {
            "INTEGER",
            "INTEGER DEFAULT 0",
            "INTEGER DEFAULT 1",
            "INTEGER NOT NULL DEFAULT 0",
            "INTEGER NOT NULL DEFAULT 1",
            "TEXT",
            "TEXT DEFAULT NULL",
            "TEXT NOT NULL DEFAULT ''",
            "TEXT NOT NULL DEFAULT 'commercial'",
            "BLOB",
            "REAL",
        }
    )

    @classmethod
    def _add_column_if_missing(
        cls,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        col_type: str,
    ) -> None:
        """Add a column to a table if it doesn't already exist (AUTH-08 hardened)."""
        table_q = cls._quote_table(table)
        column_q = cls._quote_column(column)
        col_type_norm = " ".join(col_type.split())
        col_type_sql = col_type_norm if "'" in col_type_norm else col_type_norm.upper()
        if col_type_sql not in cls._ALLOWED_COLUMN_TYPES:
            raise ValueError(f"Refusing ALTER TABLE with non-whitelisted column type: {col_type!r}")

        cursor = conn.execute(f"PRAGMA table_info({table_q})")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if not existing_columns:
            return  # Table doesn't exist yet — will be created by SCHEMA
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table_q} ADD COLUMN {column_q} {col_type_sql}")  # nosec B608
            logger.info("Added column %s.%s", table, column)

    async def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


class SQLiteUserRepository(UserRepository):
    """SQLite implementation of UserRepository."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def create_user(
        self,
        id: str,
        email: str,
        email_verified: bool = False,
        password_hash: str | None = None,
        tos_accepted_at: str | None = None,
    ) -> User:
        from auth.kdf import generate_user_salt

        now = datetime.now(UTC).isoformat()
        conn = self.db.connection

        # Generate unique per-user salt for OAuth token encryption
        encryption_salt = generate_user_salt()

        # Encrypt email at rest with blind index for lookups
        email_encrypted = None
        email_hmac = None
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
            email_encrypted = encryptor.encrypt(email.lower())
            email_hmac = encryptor.blind_index(email.lower())
        except (ValueError, Exception) as e:
            logger.debug("Field encryption not available: %s", e)

        conn.execute(
            """
            INSERT INTO users (id, email, email_verified, password_hash, encryption_salt,
                               email_encrypted, email_hmac, tos_accepted_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                id,
                email.lower(),
                int(email_verified),
                password_hash,
                encryption_salt,
                email_encrypted,
                email_hmac,
                tos_accepted_at,
                now,
                now,
            ),
        )
        conn.commit()

        return User(
            id=id,
            email=email.lower(),
            email_verified=email_verified,
            subscription_status=SubscriptionStatus.FREE,
            plan_id=PlanId.FREE,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )

    async def get_user_by_id(self, user_id: str) -> User | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_user(cast(_UserRow, dict(row)))

    async def get_user_by_email(self, email: str) -> User | None:
        conn = self.db.connection
        normalized_email = email.lower()

        # Prefer blind index lookup (encrypted path) over plaintext email
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
            hmac_hashes = encryptor.blind_indexes(normalized_email)
            placeholders = ", ".join("?" for _ in hmac_hashes)
            row = conn.execute(
                "SELECT * FROM users WHERE email_hmac IN (%s)" % placeholders,  # nosec B608
                tuple(hmac_hashes),
            ).fetchone()
            if row is not None and dict(row).get("email_hmac") != hmac_hashes[0]:
                conn.execute(
                    "UPDATE users SET email_encrypted = ?, email_hmac = ?, updated_at = ? WHERE id = ?",
                    (
                        encryptor.encrypt(normalized_email),
                        hmac_hashes[0],
                        datetime.now(UTC).isoformat(),
                        dict(row)["id"],
                    ),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE id = ?", (dict(row)["id"],)).fetchone()
        except (ValueError, Exception):
            # Fall back to plaintext email lookup (JWT_SECRET not configured)
            row = None

        if row is None:
            # Fallback: plaintext email lookup (for pre-migration or no encryption)
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
                (normalized_email,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_user(cast(_UserRow, dict(row)))

    async def get_user_with_password(self, email: str) -> UserInDB | None:
        conn = self.db.connection
        normalized_email = email.lower()

        # Prefer blind index lookup
        row = None
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
            hmac_hashes = encryptor.blind_indexes(normalized_email)
            placeholders = ", ".join("?" for _ in hmac_hashes)
            row = conn.execute(
                "SELECT * FROM users WHERE email_hmac IN (%s)" % placeholders,  # nosec B608
                tuple(hmac_hashes),
            ).fetchone()
            if row is not None and dict(row).get("email_hmac") != hmac_hashes[0]:
                conn.execute(
                    "UPDATE users SET email_encrypted = ?, email_hmac = ?, updated_at = ? WHERE id = ?",
                    (
                        encryptor.encrypt(normalized_email),
                        hmac_hashes[0],
                        datetime.now(UTC).isoformat(),
                        dict(row)["id"],
                    ),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE id = ?", (dict(row)["id"],)).fetchone()
        except (ImportError, RuntimeError, TypeError, ValueError):
            logger.debug("Encrypted email lookup migration skipped", exc_info=True)

        if row is None:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
                (normalized_email,),
            ).fetchone()

        if row is None:
            return None

        data = dict(row)
        user = self._row_to_user(cast(_UserRow, data))

        return UserInDB(
            **user.model_dump(),
            password_hash=data.get("password_hash"),
        )

    async def get_user_encryption_salt(self, user_id: str) -> str | None:
        """Get the encryption salt for a user, generating one if missing."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT encryption_salt FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        salt = dict(row).get("encryption_salt")
        if salt is None:
            # Backfill: generate salt for existing users who don't have one
            from auth.kdf import generate_user_salt

            salt = generate_user_salt()
            conn.execute(
                "UPDATE users SET encryption_salt = ? WHERE id = ?",
                (salt, user_id),
            )
            conn.commit()
            logger.info("Backfilled encryption salt for user %s", user_id)

        return salt

    _ALLOWED_USER_COLUMNS = frozenset(
        {
            "email",
            "email_verified",
            "email_encrypted",
            "email_hmac",
            "password_hash",
            "phone",
            "phone_verified",
            "sms_consent",
            "sms_consent_at",
            "sms_consent_text_version",
            "updated_at",
        }
    )

    async def update_user(self, user_id: str, **updates: DbParam) -> User | None:
        if not updates:
            return await self.get_user_by_id(user_id)

        bad_keys = set(updates.keys()) - self._ALLOWED_USER_COLUMNS
        if bad_keys:
            msg = "Invalid column(s) for user update: %s"
            logger.error(msg, bad_keys)
            raise ValueError(msg % bad_keys)

        conn = self.db.connection
        updates["updated_at"] = datetime.now(UTC).isoformat()

        # If email is being updated, also update encrypted fields
        if "email" in updates and isinstance(updates["email"], str):
            try:
                from auth.field_encryption import get_field_encryptor

                encryptor = get_field_encryptor()
                email_val = updates["email"].lower()
                updates["email_encrypted"] = encryptor.encrypt(email_val)
                updates["email_hmac"] = encryptor.blind_index(email_val)
            except (ImportError, RuntimeError, TypeError, ValueError):
                logger.debug("Encrypted email fields were not updated", exc_info=True)

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [user_id]

        conn.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",  # nosec B608
            values,
        )
        conn.commit()

        return await self.get_user_by_id(user_id)

    async def update_password(self, user_id: str, password_hash: str) -> bool:
        conn = self.db.connection
        cursor = conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, datetime.now(UTC).isoformat(), user_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    async def verify_email(self, user_id: str) -> bool:
        conn = self.db.connection
        cursor = conn.execute(
            "UPDATE users SET email_verified = 1, updated_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), user_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    async def is_orphan_account(self, user_id: str) -> bool:
        """Check if a user account is an orphan (no password, unverified, no active subscription).

        An orphan account is one created during guest checkout that was never
        completed: no password set, email not verified, and no active/trialing
        subscription.
        """
        conn = self.db.connection
        row = conn.execute(
            "SELECT password_hash, email_verified FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return False
        data = dict(row)
        if data.get("password_hash") is not None:
            return False
        if data.get("email_verified", 0):
            return False

        # Check for active subscription
        sub_row = conn.execute(
            "SELECT status FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if sub_row is not None:
            sub_status = sub_row["status"]
            if sub_status in ("active", "trialing", "past_due"):
                return False

        return True

    async def cleanup_orphan_accounts(self, max_age_hours: int = 168) -> int:
        """Delete orphan accounts older than max_age_hours (default 7 days).

        Orphan = no password, email not verified, no active subscription,
        created more than max_age_hours ago.

        Returns the number of accounts deleted.
        """
        from datetime import timedelta

        conn = self.db.connection
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()

        # SELECT first to verify each account truly has no data worth keeping
        rows = conn.execute(
            """
            SELECT u.id, u.email, u.created_at
            FROM users u
            WHERE u.password_hash IS NULL
              AND u.email_verified = 0
              AND u.created_at < ?
            """,
            (cutoff,),
        ).fetchall()

        if not rows:
            return 0

        deleted = 0
        for row in rows:
            user_id = row["id"]

            # Double-check: skip users with active/trialing/past_due subscriptions
            sub_row = conn.execute(
                "SELECT status FROM subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if sub_row is not None and sub_row["status"] in ("active", "trialing", "past_due"):
                continue

            # Double-check: skip users with any OAuth identities (they authenticated via OAuth)
            oauth_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM oauth_identities WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if oauth_row is not None and oauth_row["cnt"] > 0:
                continue

            # Double-check: skip users with any sessions (they logged in somehow)
            session_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if session_row is not None and session_row["cnt"] > 0:
                continue

            # Safe to delete — CASCADE will clean up subscriptions, sessions, etc.
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            deleted += 1
            logger.info("Cleaned up orphan account %s (email: %s)", user_id, mask_email(row["email"]))

        if deleted > 0:
            conn.commit()
            logger.info("Orphan account cleanup: deleted %d accounts", deleted)

        return deleted

    async def upgrade_orphan_account(
        self,
        user_id: str,
        password_hash: str,
    ) -> User | None:
        """Upgrade an orphan account to a real account by setting a password.

        This is used when a user registers with an email that belongs to an
        orphan guest-checkout account. Instead of blocking registration, we
        upgrade the existing account.

        Returns the updated User, or None if the user was not found or is not
        actually an orphan.
        """
        if not await self.is_orphan_account(user_id):
            return None

        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, user_id),
        )
        conn.commit()

        logger.info("Upgraded orphan account %s to registered account", user_id)
        return await self.get_user_by_id(user_id)

    async def delete_user(self, user_id: str) -> bool:
        conn = self.db.connection
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0

    def _row_to_user(self, row: _UserRow) -> User:
        """Convert database row to User model.

        Note: Subscription state is loaded from the subscriptions table.
        If no subscription record exists, defaults to the free plan.
        """
        subscription_status = SubscriptionStatus.FREE
        plan_id = PlanId.FREE
        current_period_end = None
        activation_pending = False
        conn = self.db.connection
        sub_row = conn.execute(
            "SELECT status, plan, current_period_end, activation_pending FROM subscriptions WHERE user_id = ?",
            (row["id"],),
        ).fetchone()

        if sub_row is not None:
            subscription_status = SubscriptionStatus(sub_row["status"])
            plan_id = coerce_plan_id(sub_row["plan"])
            if sub_row["current_period_end"]:
                current_period_end = datetime.fromisoformat(sub_row["current_period_end"])
            activation_pending = bool(sub_row["activation_pending"])

        data = dict(row)
        return User(
            id=data["id"],
            email=_resolve_user_email_from_row(data),
            email_verified=bool(data["email_verified"]),
            subscription_status=subscription_status,
            plan_id=plan_id,
            current_period_end=current_period_end,
            activation_pending=activation_pending,
            phone=data.get("phone"),
            phone_verified=bool(data.get("phone_verified", 0)),
            sms_consent=bool(data.get("sms_consent", 0)),
            sms_consent_at=(datetime.fromisoformat(data["sms_consent_at"]) if data.get("sms_consent_at") else None),
            sms_consent_text_version=data.get("sms_consent_text_version"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )


class SQLiteSessionRepository(SessionRepository):
    """SQLite implementation of SessionRepository."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    @staticmethod
    def _row_to_session_in_db(row: sqlite3.Row) -> SessionInDB:
        data = dict(row)
        return SessionInDB(
            id=data["id"],
            user_id=data["user_id"],
            token_hash=data["token_hash"],
            device_name=data["device_name"],
            device_id=data["device_id"],
            expires_at=datetime.fromisoformat(data["expires_at"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_used_at=datetime.fromisoformat(data["last_used_at"]),
            ip_address=data.get("ip_address"),
            user_agent_hash=data.get("user_agent_hash"),
        )

    async def create_session(self, session: SessionInDB) -> Session:
        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO sessions
            (id, user_id, token_hash, device_name, device_id, expires_at,
             created_at, last_used_at, ip_address, user_agent_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.user_id,
                session.token_hash,
                session.device_name,
                session.device_id,
                session.expires_at.isoformat(),
                session.created_at.isoformat(),
                session.last_used_at.isoformat(),
                session.ip_address,
                session.user_agent_hash,
            ),
        )
        self.db._commit_if_needed(conn)

        return Session(
            id=session.id,
            user_id=session.user_id,
            device_name=session.device_name,
            device_id=session.device_id,
            expires_at=session.expires_at,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
        )

    async def get_session_by_token_hash(self, token_hash: str) -> SessionInDB | None:
        conn = self.db.connection
        # mt-ok: token_hash is a per-session secret that is itself the auth proof;
        # the resolved row's user_id is what authorizes downstream queries.
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_session_in_db(row)

    async def get_session_by_id(self, session_id: str) -> SessionInDB | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_session_in_db(row)

    async def get_sessions_by_user(self, user_id: str) -> list[Session]:
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM sessions WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        sessions = []
        for row in rows:
            data = dict(row)
            sessions.append(
                Session(
                    id=data["id"],
                    user_id=data["user_id"],
                    device_name=data["device_name"],
                    device_id=data["device_id"],
                    expires_at=datetime.fromisoformat(data["expires_at"]),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    last_used_at=datetime.fromisoformat(data["last_used_at"]),
                )
            )
        return sessions

    async def update_session_last_used(self, session_id: str) -> None:
        conn = self.db.connection
        # mt-ok: session id is the per-row primary key; user_id scoping is implicit.
        conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), session_id),
        )
        conn.commit()

    async def update_session_expires(self, session_id: str, new_expires_at: datetime) -> None:
        """Update session expiration time (for sliding window refresh)."""
        conn = self.db.connection
        # mt-ok: session id is the per-row primary key; user_id scoping is implicit.
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (new_expires_at.isoformat(), session_id),
        )
        conn.commit()

    async def rotate_session_token(
        self,
        session_id: str,
        old_token_hash: str,
        new_token_hash: str,
        new_expires_at: datetime,
        ip_address: str | None = None,
    ) -> SessionInDB | None:
        """Atomically replace a refresh token hash."""
        conn = self.db.connection
        now = datetime.now(UTC)
        cursor = conn.execute(
            """
            UPDATE sessions
            SET token_hash = ?, expires_at = ?, last_used_at = ?, ip_address = COALESCE(?, ip_address)
            WHERE id = ? AND token_hash = ?
            """,
            (
                new_token_hash,
                new_expires_at.isoformat(),
                now.isoformat(),
                ip_address,
                session_id,
                old_token_hash,
            ),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None
        return await self.get_session_by_id(session_id)

    async def delete_session(self, session_id: str) -> bool:
        conn = self.db.connection
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cursor.rowcount > 0

    async def delete_sessions_by_user(self, user_id: str) -> int:
        conn = self.db.connection
        cursor = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount


class SQLiteMagicLinkRepository(MagicLinkRepository):
    """SQLite implementation of MagicLinkRepository."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def create_magic_link(self, link: MagicLinkInDB) -> MagicLink:
        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO magic_links (id, email, token_hash, short_code, expires_at, used_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link.id,
                link.email.lower(),
                link.token_hash,
                link.short_code,
                link.expires_at.isoformat(),
                link.used_at.isoformat() if link.used_at else None,
                link.created_at.isoformat(),
            ),
        )
        conn.commit()

        return MagicLink(
            id=link.id,
            email=link.email,
            expires_at=link.expires_at,
            used_at=link.used_at,
            created_at=link.created_at,
        )

    async def get_magic_link_by_token_hash(self, token_hash: str) -> MagicLinkInDB | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM magic_links WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        data = dict(row)
        return MagicLinkInDB(
            id=data["id"],
            email=data["email"],
            token_hash=data["token_hash"],
            short_code=data.get("short_code"),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            used_at=(datetime.fromisoformat(data["used_at"]) if data["used_at"] else None),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_magic_link_by_email_and_short_code(self, email: str, short_code: str) -> MagicLinkInDB | None:
        """C2: lookup the most-recent link for (email, short_code)."""
        conn = self.db.connection
        row = conn.execute(
            """
            SELECT * FROM magic_links
            WHERE email = ? COLLATE NOCASE AND short_code = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (email.lower(), short_code),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        return MagicLinkInDB(
            id=data["id"],
            email=data["email"],
            token_hash=data["token_hash"],
            short_code=data.get("short_code"),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            used_at=(datetime.fromisoformat(data["used_at"]) if data["used_at"] else None),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def mark_magic_link_used(self, link_id: str) -> None:
        conn = self.db.connection
        conn.execute(
            "UPDATE magic_links SET used_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), link_id),
        )
        self.db._commit_if_needed(conn)

    async def invalidate_links_for_email(self, email: str) -> int:
        """Invalidate all unused magic links for an email."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        cursor = conn.execute(
            "UPDATE magic_links SET used_at = ? WHERE email = ? COLLATE NOCASE AND used_at IS NULL",
            (now, email.lower()),
        )
        conn.commit()
        return cursor.rowcount

    async def count_recent_links_for_email(self, email: str, since: datetime) -> int:
        conn = self.db.connection
        row = conn.execute(
            "SELECT COUNT(*) as count FROM magic_links WHERE email = ? AND created_at > ?",
            (email.lower(), since.isoformat()),
        ).fetchone()
        return row["count"] if row else 0

    async def cleanup_expired_links(self) -> int:
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM magic_links WHERE expires_at < ?",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
        return cursor.rowcount

    async def cleanup_used_links(self, retention_hours: int = 24) -> int:
        """Delete used magic links older than retention period."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=retention_hours)).isoformat()
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM magic_links WHERE used_at IS NOT NULL AND used_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cursor.rowcount


class SQLiteOAuthIdentityRepository(OAuthIdentityRepository):
    """SQLite implementation of OAuthIdentityRepository."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def create(self, identity: OAuthIdentity) -> OAuthIdentity:
        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO oauth_identities
            (id, user_id, provider, provider_user_id, email, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                identity.id,
                identity.user_id,
                identity.provider.value,
                identity.provider_user_id,
                identity.email,
                identity.created_at.isoformat(),
            ),
        )
        conn.commit()
        return identity

    async def get_by_provider_user_id(
        self,
        provider: OAuthProvider,
        provider_user_id: str,
    ) -> OAuthIdentity | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM oauth_identities WHERE provider = ? AND provider_user_id = ?",
            (provider.value, provider_user_id),
        ).fetchone()

        if row is None:
            return None

        data = dict(row)
        return OAuthIdentity(
            id=data["id"],
            user_id=data["user_id"],
            provider=OAuthProvider(data["provider"]),
            provider_user_id=data["provider_user_id"],
            email=data["email"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_by_user_id(self, user_id: str) -> list[OAuthIdentity]:
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM oauth_identities WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        identities = []
        for row in rows:
            data = dict(row)
            identities.append(
                OAuthIdentity(
                    id=data["id"],
                    user_id=data["user_id"],
                    provider=OAuthProvider(data["provider"]),
                    provider_user_id=data["provider_user_id"],
                    email=data["email"],
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
            )
        return identities

    async def delete(self, identity_id: str) -> bool:
        conn = self.db.connection
        # mt-ok: identity id is the per-row primary key; caller already authorized the user.
        cursor = conn.execute("DELETE FROM oauth_identities WHERE id = ?", (identity_id,))
        conn.commit()
        return cursor.rowcount > 0


class SQLiteSubscriptionRepository(SubscriptionRepository):
    """SQLite implementation of SubscriptionRepository."""

    _ALLOWED_SUBSCRIPTION_COLUMNS = frozenset(
        {
            "status",
            "plan",
            "payment_provider",
            "external_subscription_id",
            "current_period_start",
            "current_period_end",
            "canceled_at",
            "subscription_source",
            "granted_by_admin_token_digest",
            "granted_reason",
            "updated_at",
            "activation_pending",
        }
    )

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_subscription(self, user_id: str) -> Subscription | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_subscription(cast(_SubscriptionRow, dict(row)))

    async def create_subscription(self, subscription: Subscription) -> Subscription:
        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO subscriptions
            (id, user_id, status, plan, payment_provider, external_subscription_id,
             current_period_start, current_period_end, canceled_at, subscription_source,
             granted_by_admin_token_digest, granted_reason, created_at, updated_at,
             activation_pending)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription.id,
                subscription.user_id,
                subscription.status.value,
                subscription.plan_id.value,
                (subscription.payment_provider.value if subscription.payment_provider else None),
                subscription.external_subscription_id,
                (subscription.current_period_start.isoformat() if subscription.current_period_start else None),
                (subscription.current_period_end.isoformat() if subscription.current_period_end else None),
                (subscription.canceled_at.isoformat() if subscription.canceled_at else None),
                subscription.subscription_source.value,
                subscription.granted_by_admin_token_digest,
                subscription.granted_reason,
                subscription.created_at.isoformat(),
                subscription.updated_at.isoformat(),
                1 if subscription.activation_pending else 0,
            ),
        )
        conn.commit()
        return subscription

    async def update_subscription(
        self,
        user_id: str,
        **updates: DbParam,
    ) -> Subscription | None:
        if not updates:
            return await self.get_subscription(user_id)

        bad_keys = set(updates.keys()) - self._ALLOWED_SUBSCRIPTION_COLUMNS
        if bad_keys:
            msg = "Invalid column(s) for subscription update: %s"
            logger.error(msg, bad_keys)
            raise ValueError(msg % bad_keys)

        conn = self.db.connection
        updates["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [user_id]

        conn.execute(
            f"UPDATE subscriptions SET {set_clause} WHERE user_id = ?",  # nosec B608
            values,
        )
        conn.commit()

        return await self.get_subscription(user_id)

    def _row_to_subscription(self, row: _SubscriptionRow) -> Subscription:
        from auth.models import parse_payment_provider

        return Subscription(
            id=row["id"],
            user_id=row["user_id"],
            status=SubscriptionStatus(row["status"]),
            plan_id=coerce_plan_id(row["plan"]),
            payment_provider=(parse_payment_provider(row["payment_provider"]) if row["payment_provider"] else None),
            external_subscription_id=row["external_subscription_id"],
            current_period_start=(
                datetime.fromisoformat(row["current_period_start"]) if row["current_period_start"] else None
            ),
            current_period_end=(
                datetime.fromisoformat(row["current_period_end"]) if row["current_period_end"] else None
            ),
            canceled_at=(datetime.fromisoformat(row["canceled_at"]) if row["canceled_at"] else None),
            subscription_source=SubscriptionSource(row.get("subscription_source") or "commercial"),
            granted_by_admin_token_digest=row.get("granted_by_admin_token_digest"),
            granted_reason=row.get("granted_reason"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            activation_pending=bool(row.get("activation_pending") or 0),
        )


class SQLiteOAuthStateRepository:
    """
    SQLite-backed OAuth state storage.

    Replaces in-memory OAuthStateStore for production deployments.
    Survives restarts and works across multiple instances.
    """

    def __init__(self, db: SQLiteAuthDatabase, ttl_minutes: int = 10) -> None:
        """
        Initialize OAuth state repository.

        Args:
            db: Database instance
            ttl_minutes: State TTL in minutes (default 10)
        """
        self.db = db
        self.ttl_minutes = ttl_minutes

    def create_state(self, data: JsonDict | None = None) -> str:
        """
        Create and store a new state parameter.

        Args:
            data: Optional data to associate with state

        Returns:
            Generated state string
        """
        import json
        import secrets
        from datetime import timedelta

        state = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=self.ttl_minutes)

        payload: JsonDict = dict(data) if data is not None else {}
        provider_value = payload.pop("provider", "")
        provider = provider_value if isinstance(provider_value, str) else ""

        code_verifier_value = payload.pop("code_verifier", None)
        code_verifier = code_verifier_value if isinstance(code_verifier_value, str) else None

        redirect_uri_value = payload.pop("redirect_uri", "")
        redirect_uri = redirect_uri_value if isinstance(redirect_uri_value, str) else ""

        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO oauth_states
            (state, provider, code_verifier, redirect_uri, extra_data, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state,
                provider,
                code_verifier,
                redirect_uri,
                json.dumps(payload) if payload else None,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        conn.commit()

        # Cleanup expired states occasionally
        self._cleanup_expired()

        return state

    def validate_state(self, state: str) -> JsonDict | None:
        """
        Validate and consume a state parameter.

        Args:
            state: State to validate

        Returns:
            Associated data if valid, None otherwise
        """
        import json

        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state = ?",
            (state,),
        ).fetchone()

        if row is None:
            return None

        # Delete the state (consume it)
        conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        conn.commit()

        data = dict(row)

        # Check if expired
        expires_at = datetime.fromisoformat(data["expires_at"])
        if datetime.now(UTC) > expires_at:
            return None

        # Reconstruct the data dict
        result: JsonDict = {}
        if data["provider"]:
            result["provider"] = data["provider"]
        if data["code_verifier"]:
            result["code_verifier"] = data["code_verifier"]
        if data["redirect_uri"]:
            result["redirect_uri"] = data["redirect_uri"]
        if data["extra_data"]:
            extra_value = to_json_value(json.loads(data["extra_data"]))
            if isinstance(extra_value, dict):
                result.update(extra_value)

        return result

    def _cleanup_expired(self) -> int:
        """Remove expired states."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM oauth_states WHERE expires_at < ?",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()
        return cursor.rowcount


class SQLiteOAuthTokenRepository:
    """
    SQLite-backed OAuth token storage with per-user encryption.

    Stores provider tokens (access/refresh) encrypted with per-user keys
    derived via PBKDF2-HMAC-SHA256 (600k iterations) with unique salt per user.

    Migration: Tokens encrypted with the legacy shared key (v0) are automatically
    decrypted and re-encrypted with the per-user key (v2) on first read.
    """

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        """Initialize OAuth token repository."""
        self.db = db
        self._jwt_secret: str | None = None
        self._jwt_secret_previous: str | None = None

    def _get_jwt_secret(self) -> str:
        """Get JWT secret from settings.

        Raises:
            ValueError: If JWT_SECRET is not configured
        """
        if self._jwt_secret is None:
            from config.settings import settings

            if not settings.jwt_secret:
                raise ValueError(
                    "JWT_SECRET environment variable is required for OAuth token storage. "
                    "Set JWT_SECRET to a secure random string (32+ characters)."
                )
            self._jwt_secret = settings.jwt_secret
        return self._jwt_secret

    def _get_jwt_secret_previous(self) -> str | None:
        """Get the previous JWT secret configured for rotation overlap."""
        if self._jwt_secret_previous is None:
            from config.settings import settings

            previous_raw = getattr(settings, "jwt_secret_previous", "")
            previous = previous_raw.strip() if isinstance(previous_raw, str) else ""
            self._jwt_secret_previous = previous or ""
        return self._jwt_secret_previous or None

    def _encrypt_with_key(self, plaintext: str, fernet_key: bytes) -> str:
        """Encrypt a string using a specific Fernet key."""
        from cryptography.fernet import Fernet

        f = Fernet(fernet_key)
        return f.encrypt(plaintext.encode()).decode()

    def _decrypt_with_key(self, ciphertext: str, fernet_key: bytes) -> str:
        """Decrypt a string using a specific Fernet key."""
        from cryptography.fernet import Fernet

        f = Fernet(fernet_key)
        return f.decrypt(ciphertext.encode()).decode()

    def _get_user_fernet_key(self, user_id: str) -> bytes:
        """Derive per-user Fernet key (v2)."""
        from auth.kdf import KEY_VERSION_2, derive_fernet_key

        salt = self._get_or_create_user_salt(user_id)
        return derive_fernet_key(self._get_jwt_secret(), KEY_VERSION_2, user_salt=salt)

    def _get_legacy_fernet_key(self, version: int = 0) -> bytes:
        """Derive legacy shared Fernet key for migration."""
        from auth.kdf import derive_fernet_key

        return derive_fernet_key(self._get_jwt_secret(), version)

    def _get_fernet_keys_for_version(self, user_id: str, version: int) -> tuple[bytes, bytes | None]:
        """Derive current and optional previous keys for a token version."""
        from auth.kdf import KEY_VERSION_2, derive_fernet_keys

        user_salt = self._get_or_create_user_salt(user_id) if version >= KEY_VERSION_2 else None
        return derive_fernet_keys(
            self._get_jwt_secret(),
            self._get_jwt_secret_previous(),
            version,
            user_salt=user_salt,
        )

    def _get_or_create_user_salt(self, user_id: str) -> str:
        """Get or generate per-user salt from the users table."""
        from auth.kdf import generate_user_salt

        conn = self.db.connection
        row = conn.execute(
            "SELECT encryption_salt FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            raise ValueError("User %s not found" % user_id)

        salt = dict(row).get("encryption_salt")
        if salt is None:
            # Backfill: generate salt for existing users
            salt = generate_user_salt()
            conn.execute(
                "UPDATE users SET encryption_salt = ? WHERE id = ?",
                (salt, user_id),
            )
            conn.commit()
            logger.info("Backfilled encryption salt for user %s", user_id)

        return salt

    def _decrypt_with_fallback(
        self,
        ciphertext: str,
        user_id: str,
        stored_key_version: int | None,
    ) -> str | None:
        """
        Decrypt a token, falling back through key versions if needed.

        Tries the per-user key (v2) first, then falls back to v1 (fixed PBKDF2),
        then v0 (legacy SHA256). This handles seamless migration.

        Returns:
            Decrypted plaintext or None if all versions fail
        """
        plaintext, _used_previous = self._decrypt_token_with_rotation(
            ciphertext,
            user_id,
            stored_key_version,
        )
        return plaintext

    def _decrypt_token_with_rotation(
        self,
        ciphertext: str,
        user_id: str,
        stored_key_version: int | None,
    ) -> tuple[str | None, bool]:
        """Decrypt a token across key-version and secret-rotation fallbacks."""
        from cryptography.fernet import InvalidToken

        from auth.secret_rotation import decrypt_with_fallback

        # Determine which versions to try, ordered by most likely
        versions_to_try = []
        if stored_key_version is not None and stored_key_version >= 2:
            versions_to_try = [2]  # Only try v2 if that's what was stored
        else:
            # Legacy token — try stored version first, then others
            if stored_key_version == 1:
                versions_to_try = [1, 0]
            else:
                versions_to_try = [0, 1]

        for version in versions_to_try:
            try:
                current_key, previous_key = self._get_fernet_keys_for_version(user_id, version)
                plaintext, used_previous = decrypt_with_fallback(ciphertext, current_key, previous_key)
                return plaintext.decode(), used_previous
            except (InvalidToken, Exception):
                continue

        return None, False

    async def store_tokens(
        self,
        user_id: str,
        provider: OAuthProvider,
        access_token: str | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
        expires_at: datetime | None = None,
        scope: str | None = None,
    ) -> None:
        """
        Store OAuth tokens for a user/provider.

        Tokens are encrypted with per-user PBKDF2 keys before storage.
        If tokens already exist, they are updated (upsert behavior).

        Args:
            user_id: User ID
            provider: OAuth provider
            access_token: Access token (encrypted before storage)
            refresh_token: Refresh token (encrypted before storage)
            id_token: ID token (encrypted before storage)
            expires_at: Token expiration
            scope: Token scope
        """
        from auth.kdf import KEY_VERSION_CURRENT
        from auth.utils import generate_id

        conn = self.db.connection
        now = datetime.now(UTC).isoformat()

        # Encrypt tokens with per-user key
        user_key = self._get_user_fernet_key(user_id)
        access_encrypted = self._encrypt_with_key(access_token, user_key) if access_token else None
        refresh_encrypted = self._encrypt_with_key(refresh_token, user_key) if refresh_token else None
        # Also encrypt id_token (was previously stored plaintext)
        id_token_encrypted = self._encrypt_with_key(id_token, user_key) if id_token else None

        # Check if tokens exist for this user/provider
        existing = conn.execute(
            "SELECT id FROM oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider.value),
        ).fetchone()

        if existing:
            # Update existing
            conn.execute(
                """
                UPDATE oauth_tokens SET
                    access_token_encrypted = ?,
                    refresh_token_encrypted = COALESCE(?, refresh_token_encrypted),
                    id_token = ?,
                    expires_at = ?,
                    scope = ?,
                    key_version = ?,
                    updated_at = ?
                WHERE user_id = ? AND provider = ?
                """,
                (
                    access_encrypted,
                    refresh_encrypted,
                    id_token_encrypted,
                    expires_at.isoformat() if expires_at else None,
                    scope,
                    KEY_VERSION_CURRENT,
                    now,
                    user_id,
                    provider.value,
                ),
            )
        else:
            # Insert new
            conn.execute(
                """
                INSERT INTO oauth_tokens
                (id, user_id, provider, access_token_encrypted, refresh_token_encrypted,
                 id_token, expires_at, scope, key_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_id(),
                    user_id,
                    provider.value,
                    access_encrypted,
                    refresh_encrypted,
                    id_token_encrypted,
                    expires_at.isoformat() if expires_at else None,
                    scope,
                    KEY_VERSION_CURRENT,
                    now,
                    now,
                ),
            )

        conn.commit()

    async def get_tokens(
        self,
        user_id: str,
        provider: OAuthProvider,
    ) -> tuple[str | None, str | None, datetime | None]:
        """
        Get stored tokens for a user/provider.

        If tokens are encrypted with a legacy key, they are transparently
        decrypted and re-encrypted with the per-user key (migration on read).

        Returns:
            Tuple of (access_token, refresh_token, expires_at)
            Returns (None, None, None) if not found
        """
        from auth.kdf import KEY_VERSION_CURRENT

        conn = self.db.connection
        row = conn.execute(
            """SELECT id, access_token_encrypted, refresh_token_encrypted,
                      id_token, expires_at, scope, key_version, updated_at
               FROM oauth_tokens WHERE user_id = ? AND provider = ?""",
            (user_id, provider.value),
        ).fetchone()

        if row is None:
            return None, None, None

        data = dict(row)
        stored_version = data.get("key_version") or 0

        access_token = None
        refresh_token = None
        needs_reencrypt = stored_version < KEY_VERSION_CURRENT
        used_previous_secret = False

        if data["access_token_encrypted"]:
            access_token, access_used_previous = self._decrypt_token_with_rotation(
                data["access_token_encrypted"],
                user_id,
                stored_version,
            )
            used_previous_secret = used_previous_secret or access_used_previous
            if access_token is None:
                logger.warning(
                    "Failed to decrypt access token for user=%s provider=%s (all key versions failed)",
                    user_id,
                    provider.value,
                )

        if data["refresh_token_encrypted"]:
            refresh_token, refresh_used_previous = self._decrypt_token_with_rotation(
                data["refresh_token_encrypted"],
                user_id,
                stored_version,
            )
            used_previous_secret = used_previous_secret or refresh_used_previous
            if refresh_token is None:
                logger.warning(
                    "Failed to decrypt refresh token for user=%s provider=%s",
                    user_id,
                    provider.value,
                )

        from auth.secret_rotation import schedule_re_encrypt

        needs_reencrypt = needs_reencrypt or schedule_re_encrypt(used_previous=used_previous_secret)

        # Migrate on read: re-encrypt with per-user key if using a legacy key
        # version or the previous JWT secret during the rotation window.
        if needs_reencrypt and (access_token is not None or refresh_token is not None):
            try:
                user_key = self._get_user_fernet_key(user_id)
                now = datetime.now(UTC).isoformat()
                new_access = self._encrypt_with_key(access_token, user_key) if access_token else None
                new_refresh = self._encrypt_with_key(refresh_token, user_key) if refresh_token else None

                # Also re-encrypt id_token if present. v0/v1 stored plaintext;
                # v2 may need previous-secret fallback during rotation.
                new_id_token = data.get("id_token")
                if new_id_token and stored_version >= 2:
                    id_token_plaintext, _id_used_previous = self._decrypt_token_with_rotation(
                        new_id_token,
                        user_id,
                        stored_version,
                    )
                    new_id_token = self._encrypt_with_key(id_token_plaintext, user_key) if id_token_plaintext else None
                elif new_id_token and stored_version < 2:
                    new_id_token = self._encrypt_with_key(new_id_token, user_key)

                cursor = conn.execute(
                    """
                    UPDATE oauth_tokens SET
                        access_token_encrypted = COALESCE(?, access_token_encrypted),
                        refresh_token_encrypted = COALESCE(?, refresh_token_encrypted),
                        id_token = COALESCE(?, id_token),
                        key_version = ?,
                        updated_at = ?
                    WHERE id = ?
                        AND updated_at = ?
                        AND access_token_encrypted IS ?
                        AND refresh_token_encrypted IS ?
                        AND id_token IS ?
                    """,
                    (
                        new_access,
                        new_refresh,
                        new_id_token,
                        KEY_VERSION_CURRENT,
                        now,
                        data["id"],
                        data["updated_at"],
                        data["access_token_encrypted"],
                        data["refresh_token_encrypted"],
                        data["id_token"],
                    ),
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(
                        "Migrated OAuth tokens to per-user key (user=%s, provider=%s, v%d->v%d)",
                        user_id,
                        provider.value,
                        stored_version,
                        KEY_VERSION_CURRENT,
                    )
                else:
                    logger.info(
                        "Skipped OAuth token lazy re-encryption because row changed concurrently "
                        "(user=%s, provider=%s)",
                        user_id,
                        provider.value,
                    )
            except Exception as e:
                logger.warning("Failed to re-encrypt tokens during migration: %s", e)

        expires_at = None
        if data["expires_at"]:
            expires_at = datetime.fromisoformat(data["expires_at"])

        return access_token, refresh_token, expires_at

    async def delete_tokens(self, user_id: str, provider: OAuthProvider) -> bool:
        """Delete tokens for a user/provider."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider.value),
        )
        conn.commit()
        return cursor.rowcount > 0

    async def get_all_providers_for_user(self, user_id: str) -> list[OAuthProvider]:
        """Get all providers with stored tokens for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT provider FROM oauth_tokens WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        return [OAuthProvider(row["provider"]) for row in rows]


class SQLiteUserSettingsRepository:
    """SQLite repository for user settings (cross-device sync)."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_settings(self, user_id: str) -> tuple[dict, int] | None:
        """Get user settings and version. Returns None if no settings exist."""
        import json as _json

        conn = self.db.connection
        row = conn.execute(
            "SELECT settings_json, version FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        data = dict(row)
        return _json.loads(data["settings_json"]), data["version"]

    async def save_settings(
        self,
        user_id: str,
        settings_json: str,
        version: int,
    ) -> int:
        """Save user settings with upsert. Returns new version."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()

        existing = conn.execute(
            "SELECT version FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO user_settings (user_id, settings_json, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, settings_json, version, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE user_settings SET settings_json = ?, version = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (settings_json, version, now, user_id),
            )

        conn.commit()
        return version


class SQLiteSmsRepository:
    """SQLite repository for SMS OTP, consent, and verification state."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_status(self, user_id: str) -> dict[str, Any]:
        conn = self.db.connection
        row = conn.execute(
            """
            SELECT phone, phone_verified, sms_consent, sms_consent_at, sms_consent_text_version
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return {
                "phone": None,
                "phone_verified": False,
                "sms_consent": False,
                "sms_consent_at": None,
                "sms_consent_text_version": None,
            }
        return {
            "phone": row["phone"],
            "phone_verified": bool(row["phone_verified"]),
            "sms_consent": bool(row["sms_consent"]),
            "sms_consent_at": row["sms_consent_at"],
            "sms_consent_text_version": row["sms_consent_text_version"],
        }

    async def get_user_id_by_phone(self, phone: str) -> str | None:
        # Only verified phones are matched so a forged inbound From header for a
        # stranger's number cannot resolve to (and reach the agent as) a user.
        conn = self.db.connection
        rows = conn.execute(
            """
            SELECT id
            FROM users
            WHERE phone = ? AND phone_verified = 1
            ORDER BY updated_at DESC, id
            LIMIT 2
            """,
            (phone,),
        ).fetchall()
        if len(rows) > 1:
            logger.error("SMS phone lookup ambiguous for verified phone ending %s", phone[-4:])
            return None
        return str(rows[0]["id"]) if rows else None

    async def is_phone_opted_out(self, phone: str) -> bool:
        conn = self.db.connection
        row = conn.execute("SELECT 1 FROM sms_phone_opt_outs WHERE phone = ?", (phone,)).fetchone()
        return row is not None

    async def record_phone_opt_out(
        self,
        *,
        phone: str,
        opted_out_at: datetime,
        source: str = "telnyx",
        last_to_number: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        opted_out_text = opted_out_at.isoformat()
        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO sms_phone_opt_outs (phone, opted_out_at, source, last_to_number, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                opted_out_at = excluded.opted_out_at,
                source = excluded.source,
                last_to_number = excluded.last_to_number,
                updated_at = excluded.updated_at
            """,
            (phone, opted_out_text, source, last_to_number, now, now),
        )
        self.db._commit_if_needed(conn)

    async def opt_out_by_phone(self, *, user_id: str, phone: str, opted_out_at: datetime) -> bool:
        from auth.utils import generate_id

        now = opted_out_at.isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sms_phone_opt_outs (phone, opted_out_at, source, last_to_number, created_at, updated_at)
                VALUES (?, ?, 'telnyx', NULL, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    opted_out_at = excluded.opted_out_at,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (phone, now, now, now),
            )
            cursor = conn.execute(
                """
                UPDATE users
                SET sms_consent = 0,
                    sms_consent_at = NULL,
                    sms_consent_text_version = NULL,
                    updated_at = ?
                WHERE id = ? AND phone = ?
                """,
                (now, user_id, phone),
            )
            if cursor.rowcount == 0:
                return False
            conn.execute(
                """
                INSERT INTO sms_consent_audit (
                    id, user_id, action, phone_last4, consent_text_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (generate_id(), user_id, "stop", phone[-4:], None, now),
            )
        return True

    async def record_inbound(self, user_id: str, phone: str, text: str, received_at: datetime) -> None:
        from auth.utils import generate_id

        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO sms_inbound_audit (id, user_id, phone, text, received_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (generate_id(), user_id, phone, text, received_at.isoformat(), now),
        )
        self.db._commit_if_needed(conn)

    async def upsert_pending_verification(
        self,
        *,
        user_id: str,
        phone: str,
        code_hash: str,
        sent_at: datetime,
        expires_at: datetime,
        attempts_remaining: int,
        telnyx_message_id: str | None,
    ) -> None:
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO sms_verifications (
                user_id, phone, code_hash, sent_at, expires_at, attempts_remaining,
                telnyx_message_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, phone) DO UPDATE SET
                code_hash = excluded.code_hash,
                sent_at = excluded.sent_at,
                expires_at = excluded.expires_at,
                attempts_remaining = excluded.attempts_remaining,
                telnyx_message_id = excluded.telnyx_message_id,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                phone,
                code_hash,
                sent_at.isoformat(),
                expires_at.isoformat(),
                attempts_remaining,
                telnyx_message_id,
                now,
                now,
            ),
        )
        self.db._commit_if_needed(conn)

    async def get_pending_verification(self, user_id: str, phone: str) -> dict[str, Any] | None:
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM sms_verifications WHERE user_id = ? AND phone = ?",
            (user_id, phone),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["sent_at"] = datetime.fromisoformat(data["sent_at"])
        data["expires_at"] = datetime.fromisoformat(data["expires_at"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return data

    async def decrement_attempts(self, user_id: str, phone: str) -> int:
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            UPDATE sms_verifications
            SET attempts_remaining = CASE
                    WHEN attempts_remaining > 0 THEN attempts_remaining - 1
                    ELSE 0
                END,
                updated_at = ?
            WHERE user_id = ? AND phone = ?
            """,
            (now, user_id, phone),
        )
        row = conn.execute(
            "SELECT attempts_remaining FROM sms_verifications WHERE user_id = ? AND phone = ?",
            (user_id, phone),
        ).fetchone()
        self.db._commit_if_needed(conn)
        return int(row["attempts_remaining"]) if row is not None else 0

    async def delete_pending_verification(self, user_id: str, phone: str) -> None:
        conn = self.db.connection
        conn.execute(
            "DELETE FROM sms_verifications WHERE user_id = ? AND phone = ?",
            (user_id, phone),
        )
        self.db._commit_if_needed(conn)

    async def record_send(self, user_id: str, phone: str, sent_at: datetime) -> None:
        from auth.utils import generate_id

        conn = self.db.connection
        conn.execute(
            """
            INSERT INTO sms_otp_sends (id, user_id, phone, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (generate_id(), user_id, phone, sent_at.isoformat()),
        )
        self.db._commit_if_needed(conn)

    async def count_recent_sends_by_user(self, user_id: str, since: datetime) -> int:
        conn = self.db.connection
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM sms_otp_sends WHERE user_id = ? AND sent_at >= ?",
                (user_id, since.isoformat()),
            ).fetchone()["c"]
        )

    async def count_recent_sends_by_phone(self, phone: str, since: datetime) -> int:
        conn = self.db.connection
        return int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM sms_otp_sends WHERE phone = ? AND sent_at >= ?",
                (phone, since.isoformat()),
            ).fetchone()["c"]
        )

    async def verify_phone_and_consume(
        self,
        *,
        user_id: str,
        phone: str,
        consent_at: datetime,
        consent_text_version: str,
    ) -> User | None:
        from auth.utils import generate_id

        now = datetime.now(UTC).isoformat()
        consent_at_text = consent_at.isoformat()
        with self.db.transaction() as conn:
            if await self.is_phone_opted_out(phone):
                raise ValueError("That phone number has opted out of SMS. Re-enable SMS before verifying it.")
            conflict = conn.execute(
                """
                SELECT id
                FROM users
                WHERE phone = ? AND phone_verified = 1 AND id != ?
                LIMIT 1
                """,
                (phone, user_id),
            ).fetchone()
            if conflict is not None:
                raise ValueError("That phone number is already verified on another Viola account.")
            cursor = conn.execute(
                """
                UPDATE users
                SET phone = ?,
                    phone_verified = 1,
                    sms_consent = 1,
                    sms_consent_at = ?,
                    sms_consent_text_version = ?,
                    updated_at = ?
                WHERE id = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM sms_phone_opt_outs WHERE phone = ?
                  )
                """,
                (phone, consent_at_text, consent_text_version, now, user_id, phone),
            )
            if cursor.rowcount == 0:
                if await self.is_phone_opted_out(phone):
                    raise ValueError("That phone number has opted out of SMS. Re-enable SMS before verifying it.")
                return None
            conn.execute(
                "DELETE FROM sms_verifications WHERE user_id = ? AND phone = ?",
                (user_id, phone),
            )
            conn.execute(
                """
                INSERT INTO sms_consent_audit (
                    id, user_id, action, phone_last4, consent_text_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (generate_id(), user_id, "verify", phone[-4:], consent_text_version, now),
            )
        return await self.db.users.get_user_by_id(user_id)

    async def remove_sms(self, *, user_id: str, consent_text_version: str) -> dict[str, Any]:
        from auth.utils import generate_id

        current = await self.get_status(user_id)
        phone = current.get("phone")
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as txn:
            txn.execute(
                """
                UPDATE users
                SET phone = NULL,
                    phone_verified = 0,
                    sms_consent = 0,
                    sms_consent_at = NULL,
                    sms_consent_text_version = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, user_id),
            )
            txn.execute("DELETE FROM sms_verifications WHERE user_id = ?", (user_id,))
            txn.execute(
                """
                INSERT INTO sms_consent_audit (
                    id, user_id, action, phone_last4, consent_text_version, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_id(),
                    user_id,
                    "remove",
                    str(phone)[-4:] if phone else None,
                    consent_text_version,
                    now,
                ),
            )
        return await self.get_status(user_id)


class SQLiteUserPreferencesRepository:
    """SQLite repository for per-user key-value preferences.

    Stores user-scoped settings (delivery_address, weather_location, etc.)
    separately from device-scoped settings in ``settings.json``.
    """

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    @staticmethod
    def _decode_value(raw_value: object) -> object:
        """Decode a stored JSON preference, preserving legacy raw strings."""
        import json as _json

        if raw_value is None:
            return None
        if not isinstance(raw_value, str):
            return raw_value
        try:
            return _json.loads(raw_value)
        except _json.JSONDecodeError:
            return raw_value

    @staticmethod
    def _encode_value(value: object) -> str:
        """Encode a preference as JSON so all setting types round-trip."""
        import json as _json

        return _json.dumps(to_json_value(value), ensure_ascii=False)

    async def get(self, user_id: str, key: str) -> object | None:
        """Return the value for *key*, or ``None`` if not set."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        if row is None:
            return None
        return self._decode_value(dict(row)["value"])

    async def set(self, user_id: str, key: str, value: object) -> None:
        """Upsert a preference value."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        encoded_value = self._encode_value(value)
        existing = conn.execute(
            "SELECT 1 FROM user_preferences WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO user_preferences (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, key, encoded_value, now),
            )
        else:
            conn.execute(
                "UPDATE user_preferences SET value = ?, updated_at = ? WHERE user_id = ? AND key = ?",
                (encoded_value, now, user_id, key),
            )
        conn.commit()

    async def get_all(self, user_id: str) -> dict[str, object]:
        """Return all preferences for a user as a dict."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT key, value FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {str(row["key"]): self._decode_value(row["value"]) for row in rows}

    async def delete(self, user_id: str, key: str) -> bool:
        """Remove a single preference. Returns True if it existed."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM user_preferences WHERE user_id = ? AND key = ?",
            (user_id, key),
        )
        conn.commit()
        return cursor.rowcount > 0

    async def delete_all(self, user_id: str) -> int:
        """Remove all preferences for a user. Returns count deleted."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM user_preferences WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return cursor.rowcount


class SQLiteUserCredentialsRepository:
    """SQLite repository for per-user service credentials (messaging tokens, etc.).

    Credentials are stored encrypted via FieldEncryptor. Each (user_id, service)
    pair is unique -- calling ``set_credential`` again overwrites the previous value.
    """

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_credential(self, user_id: str, service: str) -> str | None:
        """Get decrypted credential for a user+service pair."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT credential_encrypted FROM user_credentials WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchone()

        if row is None:
            return None

        ciphertext = dict(row)["credential_encrypted"]
        try:
            from auth.field_encryption import get_field_encryptor

            encryptor = get_field_encryptor()
            return encryptor.decrypt(ciphertext)
        except Exception:
            logger.exception("Failed to decrypt credential for user=%s service=%s", user_id, service)
            return None

    async def set_credential(self, user_id: str, service: str, plaintext: str) -> None:
        """Store an encrypted credential for a user+service pair (upsert)."""
        from auth.field_encryption import get_field_encryptor

        encryptor = get_field_encryptor()
        encrypted = encryptor.encrypt(plaintext)
        now = datetime.now(UTC).isoformat()

        conn = self.db.connection
        existing = conn.execute(
            "SELECT 1 FROM user_credentials WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO user_credentials (user_id, service, credential_encrypted, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, service, encrypted, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE user_credentials SET credential_encrypted = ?, updated_at = ?
                WHERE user_id = ? AND service = ?
                """,
                (encrypted, now, user_id, service),
            )
        conn.commit()

    async def delete_credential(self, user_id: str, service: str) -> bool:
        """Delete a credential. Returns True if a row was deleted."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM user_credentials WHERE user_id = ? AND service = ?",
            (user_id, service),
        )
        conn.commit()
        return cursor.rowcount > 0

    async def list_services(self, user_id: str) -> list[str]:
        """List all services with stored credentials for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT service FROM user_credentials WHERE user_id = ? ORDER BY service",
            (user_id,),
        ).fetchall()
        return [dict(row)["service"] for row in rows]

    async def delete_all_for_user(self, user_id: str) -> int:
        """Delete all credentials for a user (GDPR). Returns count deleted."""
        conn = self.db.connection
        cursor = conn.execute(
            "DELETE FROM user_credentials WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return cursor.rowcount


class SQLiteGDPRAuditRepository:
    """SQLite repository for GDPR audit logging."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def record_action(
        self,
        user_id: str,
        user_email: str | None,
        action: str,
        reason: str | None = None,
        details: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Record a GDPR audit action."""
        from auth.utils import generate_id

        conn = self.db.connection
        now = datetime.now(UTC).isoformat()

        conn.execute(
            """
            INSERT INTO gdpr_audit_log
            (id, user_id, user_email, action, reason, details, ip_address, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_id(),
                user_id,
                user_email,
                action,
                reason,
                details,
                ip_address,
                user_agent,
                now,
            ),
        )
        conn.commit()

    async def get_last_export(self, user_id: str) -> dict | None:
        """Get the last data_export audit entry for a user."""
        conn = self.db.connection
        row = conn.execute(
            """SELECT * FROM gdpr_audit_log
               WHERE user_id = ? AND action = 'data_export'
               ORDER BY created_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()

        if row is None:
            return None
        return dict(row)

    async def get_audit_log_for_user(self, user_id: str) -> list[dict]:
        """Get all audit log entries for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM gdpr_audit_log WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()

        return [dict(row) for row in rows]


class SQLiteLLMUsageRepository:
    """SQLite repository for LLM usage logs."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_usage_stats(self, user_id: str) -> dict:
        """Get aggregated LLM usage statistics for a user."""
        conn = self.db.connection
        row = conn.execute(
            """SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens
               FROM llm_usage_logs WHERE user_id = ?""",
            (user_id,),
        ).fetchone()

        if row is None:
            return {
                "total_requests": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
            }

        data = dict(row)
        return {
            "total_requests": data["total_requests"],
            "total_prompt_tokens": data["total_prompt_tokens"],
            "total_completion_tokens": data["total_completion_tokens"],
        }


class SQLiteLLMQuotaRepository:
    """SQLite repository for LLM quota tracking."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_quota(self, user_id: str) -> dict | None:
        """Get LLM quota for a user. Returns None if no quota exists."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM llm_quotas WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            return None
        return dict(row)

    async def get_or_create_quota(self, user_id: str) -> dict:
        """Get quota for a user, creating with defaults if it doesn't exist.

        Also resets daily/monthly counters when their reset times have passed.
        If the user does not exist in the users table (e.g. 'anonymous'),
        returns an in-memory default quota without persisting.
        """
        import sqlite3
        from datetime import UTC, datetime, timedelta

        conn = self.db.connection
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        row = conn.execute(
            "SELECT * FROM llm_quotas WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is None:
            daily_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            monthly_reset = (
                (now.replace(day=1) + timedelta(days=32))
                .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )

            try:
                conn.execute(
                    "INSERT INTO llm_quotas "
                    "(user_id, daily_requests, daily_tokens, monthly_requests, "
                    "monthly_tokens, daily_reset_at, monthly_reset_at, updated_at) "
                    "VALUES (?, 0, 0, 0, 0, ?, ?, ?)",
                    (user_id, daily_reset, monthly_reset, now_iso),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # FK constraint: user doesn't exist in users table (e.g. 'anonymous').
                # Return an in-memory default — rate limiting still works per-request.
                return {
                    "user_id": user_id,
                    "daily_requests": 0,
                    "daily_tokens": 0,
                    "monthly_requests": 0,
                    "monthly_tokens": 0,
                    "daily_reset_at": daily_reset,
                    "monthly_reset_at": monthly_reset,
                    "updated_at": now_iso,
                }

            row = conn.execute(
                "SELECT * FROM llm_quotas WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return dict(row)

        data = dict(row)

        # Reset daily counters if past reset time
        if data["daily_reset_at"] and now_iso >= data["daily_reset_at"]:
            next_daily = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            conn.execute(
                "UPDATE llm_quotas SET daily_requests = 0, daily_tokens = 0, "
                "daily_reset_at = ?, updated_at = ? WHERE user_id = ?",
                (next_daily, now_iso, user_id),
            )
            conn.commit()
            data["daily_requests"] = 0
            data["daily_tokens"] = 0
            data["daily_reset_at"] = next_daily

        # Reset monthly counters if past reset time
        if data["monthly_reset_at"] and now_iso >= data["monthly_reset_at"]:
            next_monthly = (
                (now.replace(day=1) + timedelta(days=32))
                .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                .isoformat()
            )
            conn.execute(
                "UPDATE llm_quotas SET monthly_requests = 0, monthly_tokens = 0, "
                "monthly_reset_at = ?, updated_at = ? WHERE user_id = ?",
                (next_monthly, now_iso, user_id),
            )
            conn.commit()
            data["monthly_requests"] = 0
            data["monthly_tokens"] = 0
            data["monthly_reset_at"] = next_monthly

        return data

    async def increment_usage(self, user_id: str, requests: int = 1, tokens: int = 0) -> dict:
        """Atomically increment usage counters. Supports negative tokens for refunds.

        If the user has no persisted quota row (e.g. anonymous users where
        the FK constraint prevented insertion), returns an in-memory dict
        reflecting the incremented values.
        """
        from datetime import UTC, datetime, timedelta

        conn = self.db.connection
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        conn.execute(
            "UPDATE llm_quotas SET "
            "daily_requests = daily_requests + ?, "
            "daily_tokens = daily_tokens + ?, "
            "monthly_requests = monthly_requests + ?, "
            "monthly_tokens = monthly_tokens + ?, "
            "updated_at = ? "
            "WHERE user_id = ?",
            (requests, tokens, requests, tokens, now_iso, user_id),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM llm_quotas WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if row is not None:
            return dict(row)

        # No persisted row (anonymous/FK-absent user) — return in-memory result
        daily_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        monthly_reset = (
            (now.replace(day=1) + timedelta(days=32))
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        return {
            "user_id": user_id,
            "daily_requests": requests,
            "daily_tokens": tokens,
            "monthly_requests": requests,
            "monthly_tokens": tokens,
            "daily_reset_at": daily_reset,
            "monthly_reset_at": monthly_reset,
            "updated_at": now_iso,
        }


class SQLiteUserDeviceRepository:
    """SQLite repository for user devices."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_devices_by_user(self, user_id: str) -> list[dict]:
        """Get all devices for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM user_devices WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        return [dict(row) for row in rows]

    async def count_devices_by_user(self, user_id: str) -> int:
        """Count devices for a user."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM user_devices WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["cnt"] if row else 0


class SQLiteMFATOTPRepository:
    """SQLite repository for MFA TOTP secrets."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def save_totp(self, user_id: str, secret_encrypted: str) -> None:
        """Save or update TOTP secret for a user (initially disabled)."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO mfa_totp (user_id, secret_encrypted, enabled, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                secret_encrypted = excluded.secret_encrypted,
                enabled = 0,
                updated_at = excluded.updated_at
            """,
            (user_id, secret_encrypted, now, now),
        )
        conn.commit()

    async def get_totp(self, user_id: str) -> dict | None:
        """Get TOTP record for a user."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT * FROM mfa_totp WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None

    async def enable_totp(self, user_id: str) -> None:
        """Mark TOTP as enabled (verified) for a user."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE mfa_totp SET enabled = 1, updated_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()

    async def disable_totp(self, user_id: str) -> None:
        """Delete TOTP record for a user."""
        conn = self.db.connection
        conn.execute("DELETE FROM mfa_totp WHERE user_id = ?", (user_id,))
        conn.commit()

    async def is_totp_enabled(self, user_id: str) -> bool:
        """Check if TOTP is enabled for a user."""
        conn = self.db.connection
        row = conn.execute(
            "SELECT enabled FROM mfa_totp WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row["enabled"])


class SQLiteMFABackupCodesRepository:
    """SQLite repository for MFA backup codes."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def save_codes(self, user_id: str, code_hashes: list[tuple[str, str]]) -> None:
        """Save backup codes (replaces existing). Each item is (id, hash)."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        # Delete existing codes first
        conn.execute("DELETE FROM mfa_backup_codes WHERE user_id = ?", (user_id,))
        for code_id, code_hash in code_hashes:
            conn.execute(
                """
                INSERT INTO mfa_backup_codes (id, user_id, code_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (code_id, user_id, code_hash, now),
            )
        conn.commit()

    async def get_unused_codes(self, user_id: str) -> list[dict]:
        """Get all unused backup codes for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM mfa_backup_codes WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    async def mark_code_used(self, code_id: str) -> None:
        """Mark a backup code as used."""
        conn = self.db.connection
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE mfa_backup_codes SET used_at = ? WHERE id = ?",
            (now, code_id),
        )
        conn.commit()

    async def delete_codes(self, user_id: str) -> None:
        """Delete all backup codes for a user."""
        conn = self.db.connection
        conn.execute("DELETE FROM mfa_backup_codes WHERE user_id = ?", (user_id,))
        conn.commit()


class SQLiteWebAuthnRepository:
    """SQLite repository for WebAuthn credentials."""

    def __init__(self, db: SQLiteAuthDatabase) -> None:
        self.db = db

    async def get_credentials_by_user(self, user_id: str) -> list[dict]:
        """Get all WebAuthn credentials for a user."""
        conn = self.db.connection
        rows = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


# =============================================================================
# Global Database Instance
# =============================================================================

# AuthDB is a union type covering both backends so callers that only need
# the shared repository attributes (.users, .sessions, ...) work with either.
AuthDB = SQLiteAuthDatabase  # will be | PostgresAuthDatabase after import below

_auth_db: SQLiteAuthDatabase | None = None
_pg_auth_db: object | None = None  # PostgresAuthDatabase when Postgres is active
_db_lock = threading.Lock()


def _get_database_url() -> str | None:
    """Return VIOLA_DATABASE_URL if it points at Postgres and asyncpg is available.

    Returns None when the env var is unset or does not point at Postgres.
    If a Postgres URL leaks into a launch environment without ``asyncpg``,
    fall back to SQLite so desktop startup is not blocked.
    """
    url = os.environ.get("VIOLA_DATABASE_URL", "")
    if not url or not (url.startswith("postgres://") or url.startswith("postgresql://")):
        return None

    try:
        from importlib import import_module

        import_module("asyncpg")
    except ImportError:
        message = (
            "VIOLA_DATABASE_URL is set but asyncpg is not installed. "
            "Falling back to SQLite for this launch environment."
        )
        logger.warning(message)
        return None

    return url


def get_auth_db() -> SQLiteAuthDatabase:
    """Get or create global auth database.

    When VIOLA_DATABASE_URL points at Postgres **and** the ``asyncpg``
    package is importable, the returned object is a PostgresAuthDatabase
    (which exposes the same repository attributes).  If ``asyncpg`` is
    missing while Postgres is configured, startup falls back to SQLite.

    The return type is kept as SQLiteAuthDatabase for backward compatibility
    with existing callers; PostgresAuthDatabase is duck-type compatible.
    """
    global _auth_db, _pg_auth_db
    with _db_lock:
        pg_url = _get_database_url()
        if pg_url:
            if _pg_auth_db is None:
                from auth.postgres_database import PostgresAuthDatabase

                _pg_auth_db = PostgresAuthDatabase(pg_url)
            return _pg_auth_db  # type: ignore[return-value] # AUTH-17: PostgresAuthDatabase is duck-type compatible.

        if _auth_db is None:
            from config.settings import get_settings

            settings = get_settings()
            db_path = Path(settings.data_dir) / "auth.db"
            _auth_db = SQLiteAuthDatabase(db_path)
        return _auth_db


async def init_auth_db(timeout: float = 5.0) -> SQLiteAuthDatabase:
    """Initialize auth database with timeout.

    If VIOLA_DATABASE_URL is set and starts with ``postgres``, a
    PostgresAuthDatabase is created and initialized instead.  The return
    type annotation is kept as SQLiteAuthDatabase for backward compat;
    PostgresAuthDatabase is duck-type compatible.

    Args:
        timeout: Maximum time in seconds to wait for initialization (default 5.0)

    Returns:
        Initialized database instance

    Raises:
        asyncio.TimeoutError: If initialization takes longer than timeout
    """
    import asyncio

    db = get_auth_db()
    await asyncio.wait_for(db.initialize(), timeout=timeout)
    return db


def get_user_service() -> SQLiteUserRepository:
    """Get user repository from global database."""
    return get_auth_db().users
