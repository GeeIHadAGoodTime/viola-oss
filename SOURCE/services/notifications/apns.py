"""Native APNs device-token registration store (C-510).

This is the server half of native iPhone push. Without it the iPhone's
``PushRegistrationService`` stays behind ``isEnabled = false`` forever and
four capabilities are dead on the phone regardless of how good the client
is: notifications, reminders, the daily briefing, and any server-initiated
Live Activity update.

Deliberately NOT the Web Push shape
-----------------------------------
``services/notifications/web_push.py`` stores an RFC 8291 subscription: an
https:// relay endpoint plus the ``p256dh``/``auth`` ECDH pair used to
encrypt a payload a browser vendor's relay forwards. A native registration
is an opaque hex device token that Viola presents directly to
``api.push.apple.com``. Nothing about the two rows means the same thing.
Reusing the Web Push table would mean writing a fake ``endpoint`` and two
fake key columns, and every later reader would have to guess which kind of
row it was looking at. See migration 072's docstring for the long form.

Ownership model
---------------
An APNs token identifies a (device, app) pair, not a person. It is
therefore globally unique here, and registering a token that already
belongs to another user REASSIGNS it. That is the truthful model: after
user B signs into a phone that user A used, A must stop receiving pushes
on it. The alternative (per-user rows sharing a token) delivers A's
notification content to B's lock screen, which is a data leak dressed up
as a convenience.

Dual-mode storage: PostgreSQL on the cloud surface (RLS-scoped per user by
migration 072), SQLite for a desktop/dev process that has no cloud
database.

WHY EVERY POSTGRES CALL OPENS A TRANSACTION AND SETS ``app.user_id``
--------------------------------------------------------------------
The runtime role ``viola_app`` is NOBYPASSRLS *and* a member of
``authenticated`` (migration 019: it raises if that membership is
missing), so the owner policies genuinely apply to it. ``current_user_id()``
reads ``app.user_id`` off the session, and ``set_rls_context`` sets it
transaction-locally, so a query issued on a bare pooled connection sees
``NULL`` -- a SELECT then returns zero rows and an INSERT fails its
``WITH CHECK``. This module therefore follows the
``services/cloud_settings.py`` pattern (acquire, open a transaction, set
the context, then query), not the ``services/notifications/web_push.py``
pattern, which acquires a bare connection and never sets the context.
Copying the Web Push shape here would produce an endpoint that answers 200
while storing nothing, which is the exact failure this whole change exists
to remove.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.db_backend import get_database_url
from core.logging_config import get_logger
from services.persistence.state_store import STATE_DB_SCHEMA_LOCK

if TYPE_CHECKING:
    import asyncpg

logger = get_logger(__name__)

_DB_FILENAME = "state.sqlite3"
_STORE_LOCK = threading.Lock()
_STORE_SINGLETON: APNsDeviceStore | None = None
_SERVICE_LOCK = threading.Lock()
_SERVICE_SINGLETON: APNsRegistrationService | None = None

_PG_REQUIRED_RELATIONS = ("public.apns_devices",)

# Apple's standard device token is 32 bytes (64 hex chars). Apple has
# reserved the right to change the length, and PushKit/Live Activity
# push-to-start tokens are longer, so we accept a range rather than
# pinning 64 -- but we still bound it. An unbounded TEXT column that any
# authenticated caller can fill is a storage-abuse vector, and a
# non-hex value is guaranteed to be rejected by APNs later, at which
# point the failure is a mystery in a delivery worker instead of a 400
# at the door.
_MIN_TOKEN_HEX_CHARS = 64
_MAX_TOKEN_HEX_CHARS = 200
_TOKEN_RE = re.compile(r"\A[0-9a-f]+\Z")

VALID_ENVIRONMENTS = ("sandbox", "production")
DEFAULT_ENVIRONMENT = "production"
DEFAULT_PLATFORM = "ios"

# One account can legitimately hold several devices (phone, iPad, a
# replacement handset before the old token expires). It cannot
# legitimately hold hundreds. Beyond the cap the least-recently-seen rows
# are pruned, so a token-spraying client bounds its own footprint instead
# of growing the table without limit.
MAX_DEVICES_PER_USER = 20

# Free-form strings echoed from the client. Bounded so a registration
# cannot smuggle a large blob into a column nobody validates.
_MAX_APP_VERSION_CHARS = 64
_MAX_LOCALE_CHARS = 32
_MAX_DEVICE_ID_CHARS = 128
_MAX_BUNDLE_ID_CHARS = 128


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _utcnow_dt() -> datetime:
    return datetime.now(UTC)


def token_hash(device_token: str) -> str:
    """Stable digest of a device token, safe to log and to key on."""
    return hashlib.sha256(device_token.encode("ascii")).hexdigest()


def normalize_device_token(raw: Any) -> str:
    """Validate + canonicalize an APNs device token.

    iOS hands the app a ``Data`` blob which ``PushRegistrationService``
    renders as lowercase hex. Some clients strip the ``<...>`` wrapper of
    the legacy ``description`` form, or send spaces, or uppercase. Accept
    those and canonicalize; reject anything that is not hex, because a
    non-hex token cannot be a real APNs token and would only fail later,
    inside a delivery worker, where the cause is invisible.
    """
    if not isinstance(raw, str):
        raise ValueError("device_token must be a string")
    cleaned = raw.strip().strip("<>").replace(" ", "").replace("-", "").lower()
    if not cleaned:
        raise ValueError("device_token is required")
    if len(cleaned) % 2 != 0:
        raise ValueError("device_token must have an even number of hex characters")
    if not _TOKEN_RE.match(cleaned):
        raise ValueError("device_token must be hexadecimal")
    if len(cleaned) < _MIN_TOKEN_HEX_CHARS:
        raise ValueError("device_token is shorter than %d hex characters" % _MIN_TOKEN_HEX_CHARS)
    if len(cleaned) > _MAX_TOKEN_HEX_CHARS:
        raise ValueError("device_token is longer than %d hex characters" % _MAX_TOKEN_HEX_CHARS)
    return cleaned


def normalize_environment(raw: Any) -> str:
    """Coerce the APNs environment to ``sandbox``/``production``.

    Getting this wrong is the single most common native-push failure: a
    token minted against the sandbox gateway is rejected with
    ``BadDeviceToken`` by the production gateway and vice versa, and the
    symptom is a silent non-delivery. Store it explicitly per row rather
    than inferring it at send time from a process-wide setting, because a
    TestFlight build and an App Store build of the same app can register
    against the same account on the same day.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_ENVIRONMENT
    if not isinstance(raw, str):
        raise ValueError("environment must be a string")
    value = raw.strip().lower()
    aliases = {
        "prod": "production",
        "production": "production",
        "release": "production",
        "dev": "sandbox",
        "development": "sandbox",
        "sandbox": "sandbox",
        "debug": "sandbox",
    }
    resolved = aliases.get(value)
    if resolved is None:
        raise ValueError("environment must be one of %s" % ", ".join(VALID_ENVIRONMENTS))
    return resolved


def _bounded(raw: Any, limit: int, field: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("%s must be a string" % field)
    value = raw.strip()
    if not value:
        return None
    if len(value) > limit:
        raise ValueError("%s must be at most %d characters" % (field, limit))
    return value


@dataclass(frozen=True)
class APNsDevice:
    """One registered native push destination."""

    id: int
    user_id: str
    device_token_hash: str
    environment: str
    bundle_id: str
    platform: str
    device_id: str | None
    app_version: str | None
    locale: str | None
    active: bool
    created_at: str
    updated_at: str
    last_seen_at: str

    def to_public_dict(self) -> dict[str, Any]:
        """Client-facing view.

        The raw ``device_token`` is deliberately absent from this dataclass
        entirely, not merely filtered here: the API never needs to echo a
        push token back, and a field that does not exist cannot leak into a
        response, a log line, or a Sentry breadcrumb by accident.
        """
        return asdict(self)


class APNsDeviceStore:
    """Persist native APNs device registrations in SQLite or PostgreSQL."""

    def __init__(self, root: Path | None = None) -> None:
        self._pg_url = get_database_url()
        self._use_pg = self._pg_url is not None
        self._lock = threading.RLock()

        if self._use_pg:
            self._db_path = None
            self._conn = None
            self._pg_initialized = False
            return

        if root is not None:
            base_path = Path(root)
        else:
            try:
                from config.settings import settings as app_settings

                base_path = Path(app_settings.data_dir)
            except (ImportError, AttributeError):
                base_path = Path.cwd()

        self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    async def _pg_pool(self) -> asyncpg.Pool:
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    async def _run_pg(self, user_id: str, operation):
        """Run ``operation(conn)`` inside an RLS-scoped transaction.

        ``set_rls_context`` uses a transaction-local ``set_config``, so the
        transaction is not optional: without it ``public.current_user_id()``
        is NULL for the statement and every owner policy on
        ``apns_devices`` denies the row.
        """
        await self.pg_initialize()
        from services.sync.middleware_helpers import set_rls_context

        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await set_rls_context(conn, str(user_id))
                return await operation(conn)

    async def pg_initialize(self) -> None:
        """Fail closed if the cloud table is missing.

        Same contract as ``WebPushSubscriptionStore.pg_initialize``: the
        cloud process must not silently accept registrations into a table
        that a migration never created, because every one of them would be
        lost and the endpoint would still answer 200.
        """
        if not self._use_pg or self._pg_initialized:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="APNsDeviceStore")
        self._pg_initialized = True
        logger.info("APNsDeviceStore initialized (PostgreSQL)")

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS apns_devices (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id           TEXT NOT NULL,
                    device_token_hash TEXT NOT NULL UNIQUE,
                    device_token      TEXT NOT NULL,
                    environment       TEXT NOT NULL,
                    bundle_id         TEXT NOT NULL,
                    platform          TEXT NOT NULL DEFAULT 'ios',
                    device_id         TEXT,
                    app_version       TEXT,
                    locale            TEXT,
                    active            INTEGER NOT NULL DEFAULT 1,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL,
                    last_seen_at      TEXT NOT NULL
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_apns_devices_user
                ON apns_devices(user_id)
                """)

    @staticmethod
    def _row_to_device(row: Any) -> APNsDevice:
        return APNsDevice(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            device_token_hash=str(row["device_token_hash"]),
            environment=str(row["environment"]),
            bundle_id=str(row["bundle_id"]),
            platform=str(row["platform"]),
            device_id=row["device_id"],
            app_version=row["app_version"],
            locale=row["locale"],
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_seen_at=str(row["last_seen_at"]),
        )

    async def register(
        self,
        *,
        user_id: str,
        device_token: str,
        environment: str,
        bundle_id: str,
        platform: str = DEFAULT_PLATFORM,
        device_id: str | None = None,
        app_version: str | None = None,
        locale: str | None = None,
    ) -> APNsDevice:
        """Create or re-own a device registration, then prune past the cap."""
        digest = token_hash(device_token)

        if self._use_pg:
            now = _utcnow_dt()

            async def _operation(conn):
                # A token already owned by ANOTHER user is invisible to this
                # transaction under RLS, so the ON CONFLICT arm can never
                # fire for it and the INSERT would raise a unique violation
                # instead. Delete-then-insert the digest first, as the only
                # statement in this module that reaches across owners: the
                # device physically moved to this account, and leaving the
                # stale row would keep delivering the previous owner's
                # notifications to a phone that is no longer theirs.
                await conn.execute(
                    "DELETE FROM apns_devices WHERE device_token_hash = $1 AND user_id <> $2::uuid",
                    digest,
                    str(user_id),
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO apns_devices
                        (user_id, device_token_hash, device_token, environment,
                         bundle_id, platform, device_id, app_version, locale,
                         active, created_at, updated_at, last_seen_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, $10, $10, $10)
                    ON CONFLICT (device_token_hash)
                    DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        device_token = EXCLUDED.device_token,
                        environment = EXCLUDED.environment,
                        bundle_id = EXCLUDED.bundle_id,
                        platform = EXCLUDED.platform,
                        device_id = EXCLUDED.device_id,
                        app_version = EXCLUDED.app_version,
                        locale = EXCLUDED.locale,
                        active = true,
                        updated_at = EXCLUDED.updated_at,
                        last_seen_at = EXCLUDED.last_seen_at
                    RETURNING *
                    """,
                    user_id,
                    digest,
                    device_token,
                    environment,
                    bundle_id,
                    platform,
                    device_id,
                    app_version,
                    locale,
                    now,
                )
                assert row is not None
                # Prune inside the SAME transaction: a separate connection
                # would need its own RLS context, and a crash between the
                # two would leave the cap unenforced.
                await conn.execute(
                    """
                    DELETE FROM apns_devices
                    WHERE id IN (
                        SELECT id FROM apns_devices
                        WHERE user_id = $1::uuid
                        ORDER BY last_seen_at DESC, id DESC
                        OFFSET $2
                    )
                    """,
                    str(user_id),
                    MAX_DEVICES_PER_USER,
                )
                return row

            return self._row_to_device(await self._run_pg(user_id, _operation))

        now = _utcnow_iso()
        assert self._conn is not None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO apns_devices
                    (user_id, device_token_hash, device_token, environment,
                     bundle_id, platform, device_id, app_version, locale,
                     active, created_at, updated_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(device_token_hash)
                DO UPDATE SET
                    user_id = excluded.user_id,
                    device_token = excluded.device_token,
                    environment = excluded.environment,
                    bundle_id = excluded.bundle_id,
                    platform = excluded.platform,
                    device_id = excluded.device_id,
                    app_version = excluded.app_version,
                    locale = excluded.locale,
                    active = 1,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    user_id,
                    digest,
                    device_token,
                    environment,
                    bundle_id,
                    platform,
                    device_id,
                    app_version,
                    locale,
                    now,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM apns_devices WHERE device_token_hash = ?",
                (digest,),
            ).fetchone()
        assert row is not None
        device = self._row_to_device(row)
        self._prune_sqlite(user_id)
        return device

    def _prune_sqlite(self, user_id: str) -> None:
        assert self._conn is not None
        with self._lock, self._conn:
            self._conn.execute(
                """
                DELETE FROM apns_devices
                WHERE id IN (
                    SELECT id FROM apns_devices
                    WHERE user_id = ?
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (user_id, MAX_DEVICES_PER_USER),
            )

    async def list_devices(self, user_id: str) -> list[APNsDevice]:
        """All registrations owned by this user, newest first."""
        if self._use_pg:

            async def _operation(conn):
                return await conn.fetch(
                    """
                    SELECT * FROM apns_devices
                    WHERE user_id = $1::uuid
                    ORDER BY last_seen_at DESC, id DESC
                    """,
                    str(user_id),
                )

            rows = await self._run_pg(user_id, _operation)
            return [self._row_to_device(row) for row in rows]

        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM apns_devices
                WHERE user_id = ?
                ORDER BY last_seen_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_device(row) for row in rows]

    async def unregister(
        self,
        user_id: str,
        *,
        device_token_hash: str | None = None,
        device_id: str | None = None,
    ) -> int:
        """Delete this user's registrations by token digest and/or device id.

        Always scoped by ``user_id``. A caller can only ever remove a row
        they own, even if they happen to know another user's token digest.
        """
        if not device_token_hash and not device_id:
            raise ValueError("device_token or device_id is required")

        if self._use_pg:
            clauses = ["user_id = $1::uuid"]
            params: list[Any] = [str(user_id)]
            index = 2
            if device_token_hash:
                clauses.append("device_token_hash = $%d" % index)
                params.append(device_token_hash)
                index += 1
            if device_id:
                clauses.append("device_id = $%d" % index)
                params.append(device_id)
            sql = "DELETE FROM apns_devices WHERE %s" % " AND ".join(
                clauses
            )  # nosec B608 - hardcoded WHERE fragments; values parameterized

            async def _operation(conn):
                return await conn.execute(sql, *params)

            result = await self._run_pg(user_id, _operation)
            return int(str(result).rsplit(" ", 1)[-1])

        filters = ["user_id = ?"]
        sqlite_params: list[Any] = [user_id]
        if device_token_hash:
            filters.append("device_token_hash = ?")
            sqlite_params.append(device_token_hash)
        if device_id:
            filters.append("device_id = ?")
            sqlite_params.append(device_id)
        sql = "DELETE FROM apns_devices WHERE %s" % " AND ".join(
            filters
        )  # nosec B608 - hardcoded WHERE fragments; values parameterized
        assert self._conn is not None
        with self._lock, self._conn:
            cursor = self._conn.execute(sql, tuple(sqlite_params))
        return int(cursor.rowcount)

    # DELIBERATELY ABSENT: a cross-owner ``deactivate_token_hash`` for the
    # APNs feedback path (Apple answering ``Unregistered`` /
    # ``BadDeviceToken`` for a token whose user is not in scope). Under the
    # owner policy such an UPDATE matches zero rows and returns "UPDATE 0"
    # without raising, so shipping it now would add a method that reports
    # success while doing nothing. It belongs with the delivery worker that
    # will need it, and that worker needs an explicitly elevated context,
    # which is a decision to make in the open rather than to inherit
    # silently from a helper written months earlier.


def get_apns_device_store() -> APNsDeviceStore:
    global _STORE_SINGLETON
    with _STORE_LOCK:
        if _STORE_SINGLETON is None:
            _STORE_SINGLETON = APNsDeviceStore()
        return _STORE_SINGLETON


def reset_apns_device_store() -> None:
    """Drop the cached singleton (tests / surface switches)."""
    global _STORE_SINGLETON
    with _STORE_LOCK:
        _STORE_SINGLETON = None


class APNsRegistrationService:
    """Validation + policy in front of :class:`APNsDeviceStore`."""

    def __init__(self, store: APNsDeviceStore | None = None, default_bundle_id: str | None = None) -> None:
        self._store = store if store is not None else get_apns_device_store()
        if default_bundle_id is None:
            try:
                from config.settings import settings as app_settings

                default_bundle_id = getattr(app_settings, "ios_bundle_id", None)
            except ImportError:
                default_bundle_id = None
        self._default_bundle_id = (default_bundle_id or "com.useviola.viola").strip()

    @property
    def default_bundle_id(self) -> str:
        return self._default_bundle_id

    async def register(
        self,
        *,
        user_id: str,
        device_token: str,
        environment: Any = None,
        bundle_id: Any = None,
        platform: Any = None,
        device_id: Any = None,
        app_version: Any = None,
        locale: Any = None,
    ) -> APNsDevice:
        if not str(user_id or "").strip():
            raise ValueError("user_id is required")
        normalized_token = normalize_device_token(device_token)
        normalized_environment = normalize_environment(environment)
        normalized_bundle = _bounded(bundle_id, _MAX_BUNDLE_ID_CHARS, "bundle_id") or self._default_bundle_id
        normalized_platform = (_bounded(platform, 32, "platform") or DEFAULT_PLATFORM).lower()
        return await self._store.register(
            user_id=str(user_id),
            device_token=normalized_token,
            environment=normalized_environment,
            bundle_id=normalized_bundle,
            platform=normalized_platform,
            device_id=_bounded(device_id, _MAX_DEVICE_ID_CHARS, "device_id"),
            app_version=_bounded(app_version, _MAX_APP_VERSION_CHARS, "app_version"),
            locale=_bounded(locale, _MAX_LOCALE_CHARS, "locale"),
        )

    async def list_devices(self, user_id: str) -> list[APNsDevice]:
        return await self._store.list_devices(str(user_id))

    async def unregister(
        self,
        user_id: str,
        *,
        device_token: Any = None,
        device_id: Any = None,
    ) -> int:
        digest: str | None = None
        if device_token is not None and str(device_token).strip():
            digest = token_hash(normalize_device_token(device_token))
        resolved_device_id = _bounded(device_id, _MAX_DEVICE_ID_CHARS, "device_id")
        if digest is None and resolved_device_id is None:
            raise ValueError("device_token or device_id is required")
        return await self._store.unregister(
            str(user_id),
            device_token_hash=digest,
            device_id=resolved_device_id,
        )


def get_apns_registration_service() -> APNsRegistrationService:
    global _SERVICE_SINGLETON
    with _SERVICE_LOCK:
        if _SERVICE_SINGLETON is None:
            _SERVICE_SINGLETON = APNsRegistrationService()
        return _SERVICE_SINGLETON


def reset_apns_registration_service() -> None:
    """Drop the cached singleton (tests / surface switches)."""
    global _SERVICE_SINGLETON
    with _SERVICE_LOCK:
        _SERVICE_SINGLETON = None


__all__ = [
    "DEFAULT_ENVIRONMENT",
    "MAX_DEVICES_PER_USER",
    "VALID_ENVIRONMENTS",
    "APNsDevice",
    "APNsDeviceStore",
    "APNsRegistrationService",
    "get_apns_device_store",
    "get_apns_registration_service",
    "normalize_device_token",
    "normalize_environment",
    "reset_apns_device_store",
    "reset_apns_registration_service",
    "token_hash",
]
