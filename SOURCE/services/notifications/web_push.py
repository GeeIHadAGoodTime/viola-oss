"""Web Push notification service with dual-mode subscription storage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
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
_STORE_SINGLETON: WebPushSubscriptionStore | None = None
_SERVICE_LOCK = threading.Lock()
_SERVICE_SINGLETON: WebPushService | None = None
_DEFAULT_TTL_SECONDS = 60

_PG_REQUIRED_RELATIONS = ("public.web_push_subscriptions",)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _utcnow_dt() -> datetime:
    return datetime.now(UTC)


def _validate_push_endpoint(endpoint: str) -> None:
    """Reject Web Push endpoints that point at internal / private targets.

    Browsers obtain push endpoints from their browser-vendor relay (FCM,
    Mozilla autopush, Apple APNs gateway). Those endpoints are always
    https:// on a public host. An authenticated user can otherwise submit
    a private-network URL (127.0.0.1, 10.0.0.0/8, link-local, cloud
    metadata) and trick the cloud worker into making SSRF requests when a
    later send_notification fires.

    We require https://, reject blocked hostnames, and reject raw IPs that
    fall inside the SSRF-blocked ranges. We do not allowlist push-service
    hostnames (browser vendors add new endpoints over time) but the
    https + non-private-network gate is the structural fix for the SSRF
    bug class.
    """
    from core.url_validation import validate_external_url

    try:
        validate_external_url(endpoint)
    except ValueError as exc:
        raise ValueError("Web Push endpoint is not allowed: %s" % exc) from exc
    if not endpoint.lower().startswith("https://"):
        raise ValueError("Web Push endpoint must use https://")


@dataclass(frozen=True)
class WebPushSubscription:
    """Stored browser push subscription."""

    id: int
    user_id: str
    endpoint: str
    p256dh: str
    auth: str
    device_id: str | None
    user_agent: str | None
    created_at: str
    updated_at: str

    def to_subscription_info(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "keys": {
                "p256dh": self.p256dh,
                "auth": self.auth,
            },
        }


class WebPushSubscriptionStore:
    """Persist Web Push subscriptions in SQLite or PostgreSQL."""

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
            except Exception:
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

    async def pg_initialize(self) -> None:
        """Ensure PostgreSQL schema exists."""
        if not self._use_pg or self._pg_initialized:
            return
        pool = await self._pg_pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(conn, _PG_REQUIRED_RELATIONS, owner="WebPushSubscriptionStore")
        self._pg_initialized = True
        logger.info("WebPushSubscriptionStore initialized (PostgreSQL)")

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id     TEXT NOT NULL,
                    device_id   TEXT,
                    endpoint    TEXT NOT NULL,
                    p256dh      TEXT NOT NULL,
                    auth        TEXT NOT NULL,
                    user_agent  TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    UNIQUE(user_id, endpoint)
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_user
                ON web_push_subscriptions(user_id)
                """)

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row) -> WebPushSubscription:
        return WebPushSubscription(
            id=row["id"],
            user_id=row["user_id"],
            endpoint=row["endpoint"],
            p256dh=row["p256dh"],
            auth=row["auth"],
            device_id=row["device_id"],
            user_agent=row["user_agent"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _pg_row_to_subscription(row: asyncpg.Record) -> WebPushSubscription:
        return WebPushSubscription(
            id=row["id"],
            user_id=row["user_id"],
            endpoint=row["endpoint"],
            p256dh=row["p256dh"],
            auth=row["auth"],
            device_id=row["device_id"],
            user_agent=row["user_agent"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def subscribe(
        self,
        user_id: str,
        subscription: dict[str, Any],
        device_id: str | None = None,
        user_agent: str | None = None,
    ) -> WebPushSubscription:
        """Create or update a Web Push subscription."""
        endpoint = str(subscription.get("endpoint", "")).strip()
        keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
        p256dh = str(keys.get("p256dh", "")).strip()
        auth = str(keys.get("auth", "")).strip()
        if not endpoint or not p256dh or not auth:
            raise ValueError("Subscription must include endpoint, keys.p256dh, and keys.auth")
        # SSRF guard: the server will later POST to this endpoint when
        # delivering notifications. An authenticated user can otherwise
        # register a private-network/metadata/localhost URL and force the
        # cloud worker to make arbitrary internal HTTP requests.
        _validate_push_endpoint(endpoint)

        if self._use_pg:
            await self.pg_initialize()
            now = _utcnow_dt()
            pool = await self._pg_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO web_push_subscriptions
                        (user_id, device_id, endpoint, p256dh, auth, user_agent, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
                    ON CONFLICT (user_id, endpoint)
                    DO UPDATE SET
                        device_id = EXCLUDED.device_id,
                        p256dh = EXCLUDED.p256dh,
                        auth = EXCLUDED.auth,
                        user_agent = EXCLUDED.user_agent,
                        updated_at = EXCLUDED.updated_at
                    RETURNING *
                    """,
                    user_id,
                    device_id,
                    endpoint,
                    p256dh,
                    auth,
                    user_agent,
                    now,
                )
            assert row is not None
            return self._pg_row_to_subscription(row)

        now = _utcnow_iso()
        assert self._conn is not None
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO web_push_subscriptions
                    (user_id, device_id, endpoint, p256dh, auth, user_agent, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, endpoint)
                DO UPDATE SET
                    device_id = excluded.device_id,
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent,
                    updated_at = excluded.updated_at
                """,
                (user_id, device_id, endpoint, p256dh, auth, user_agent, now, now),
            )
            row = self._conn.execute(
                """
                SELECT *
                FROM web_push_subscriptions
                WHERE user_id = ? AND endpoint = ?
                """,
                (user_id, endpoint),
            ).fetchone()
        assert row is not None
        return self._row_to_subscription(row)

    async def list_subscriptions(self, user_id: str) -> list[WebPushSubscription]:
        """Return all subscriptions for a user."""
        if self._use_pg:
            await self.pg_initialize()
            pool = await self._pg_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM web_push_subscriptions
                    WHERE user_id = $1
                    ORDER BY updated_at DESC
                    """,
                    user_id,
                )
            return [self._pg_row_to_subscription(row) for row in rows]

        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM web_push_subscriptions
                WHERE user_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    async def unsubscribe(
        self,
        user_id: str,
        endpoint: str | None = None,
        device_id: str | None = None,
    ) -> int:
        """Delete subscriptions for a user by endpoint and/or device."""
        filters = ["user_id = ?"]
        params: list[Any] = [user_id]
        if endpoint:
            filters.append("endpoint = ?")
            params.append(endpoint)
        if device_id:
            filters.append("device_id = ?")
            params.append(device_id)

        if self._use_pg:
            await self.pg_initialize()
            pool = await self._pg_pool()
            clauses = ["user_id = $1"]
            pg_params: list[Any] = [user_id]
            next_index = 2
            if endpoint:
                clauses.append(f"endpoint = ${next_index}")
                pg_params.append(endpoint)
                next_index += 1
            if device_id:
                clauses.append(f"device_id = ${next_index}")
                pg_params.append(device_id)
            sql = "DELETE FROM web_push_subscriptions WHERE %s" % " AND ".join(
                clauses
            )  # nosec B608 — hardcoded WHERE fragments; values parameterized
            pool = await self._pg_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(sql, *pg_params)
            return int(result.rsplit(" ", 1)[-1])

        assert self._conn is not None
        sql = "DELETE FROM web_push_subscriptions WHERE %s" % " AND ".join(
            filters
        )  # nosec B608 — hardcoded WHERE fragments; values parameterized
        with self._lock, self._conn:
            cursor = self._conn.execute(sql, tuple(params))
        return int(cursor.rowcount)

    async def remove_endpoint(self, endpoint: str) -> int:
        """Delete subscriptions by endpoint regardless of user."""
        if self._use_pg:
            await self.pg_initialize()
            pool = await self._pg_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM web_push_subscriptions WHERE endpoint = $1",
                    endpoint,
                )
            return int(result.rsplit(" ", 1)[-1])

        assert self._conn is not None
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM web_push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            )
        return int(cursor.rowcount)


class WebPushService:
    """Manage Web Push subscription CRUD and delivery."""

    def __init__(
        self,
        store: WebPushSubscriptionStore | None = None,
        vapid_public_key: str | None = None,
        vapid_private_key: str | None = None,
        vapid_subject: str | None = None,
    ) -> None:
        if store is None:
            store = get_web_push_store()

        if vapid_public_key is None or vapid_private_key is None or vapid_subject is None:
            try:
                from config.settings import settings as app_settings
            except Exception:
                app_settings = None
            if app_settings is not None:
                vapid_public_key = vapid_public_key or getattr(app_settings, "web_push_vapid_public_key", None)
                vapid_private_key = vapid_private_key or getattr(app_settings, "web_push_vapid_private_key", None)
                vapid_subject = vapid_subject or getattr(app_settings, "web_push_vapid_subject", None)

        self._store = store
        self._vapid_public_key = (vapid_public_key or "").strip()
        self._vapid_private_key = (vapid_private_key or "").strip()
        self._vapid_subject = (vapid_subject or "").strip()
        self._warned_missing_dependency = False

    @property
    def vapid_public_key(self) -> str | None:
        return self._vapid_public_key or None

    @property
    def is_configured(self) -> bool:
        return bool(self._vapid_public_key and self._vapid_private_key and self._vapid_subject)

    async def subscribe(
        self,
        user_id: str,
        subscription: dict[str, Any],
        device_id: str | None = None,
        user_agent: str | None = None,
    ) -> WebPushSubscription:
        return await self._store.subscribe(
            user_id=user_id,
            subscription=subscription,
            device_id=device_id,
            user_agent=user_agent,
        )

    async def unsubscribe(
        self,
        user_id: str,
        endpoint: str | None = None,
        device_id: str | None = None,
    ) -> int:
        return await self._store.unsubscribe(
            user_id=user_id,
            endpoint=endpoint,
            device_id=device_id,
        )

    async def list_subscriptions(self, user_id: str) -> list[WebPushSubscription]:
        return await self._store.list_subscriptions(user_id)

    async def send_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        urgency: str = "normal",
        ttl: int = _DEFAULT_TTL_SECONDS,
    ) -> int:
        """Deliver a notification to all of a user's Web Push subscriptions."""
        if not self.is_configured:
            logger.debug("Web push not configured; skipping delivery for user=%s", user_id)
            return 0

        subscriptions = await self.list_subscriptions(user_id)
        if not subscriptions:
            return 0

        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "data": data or {},
            },
            separators=(",", ":"),
        )

        sent = 0
        for subscription in subscriptions:
            delivered = await self._deliver_subscription(
                subscription=subscription,
                payload=payload,
                urgency=urgency,
                ttl=ttl,
            )
            sent += int(delivered)
        if sent:
            logger.info("Web push delivered to %d subscription(s) for user=%s", sent, user_id)
        return sent

    async def _deliver_subscription(
        self,
        subscription: WebPushSubscription,
        payload: str,
        urgency: str,
        ttl: int,
    ) -> bool:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError:
            if not self._warned_missing_dependency:
                logger.warning("pywebpush is not installed; Web Push delivery is disabled")
                self._warned_missing_dependency = True
            return False

        # Defense-in-depth: re-validate the stored endpoint at delivery
        # time. Historical bad rows (or schema migrations) shouldn't be
        # able to trigger SSRF the moment a send_notification fires.
        try:
            _validate_push_endpoint(subscription.endpoint)
        except ValueError as exc:
            logger.warning(
                "Skipping push delivery to disallowed endpoint=%s reason=%s",
                subscription.endpoint[:80],
                exc,
            )
            await self._store.remove_endpoint(subscription.endpoint)
            return False

        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription.to_subscription_info(),
                data=payload,
                vapid_private_key=self._vapid_private_key,
                vapid_claims={"sub": self._vapid_subject},
                ttl=max(0, int(ttl)),
                headers={"Urgency": urgency},
            )
            return True
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(
                "Web push delivery failed for endpoint=%s status=%s error=%s",
                subscription.endpoint[:80],
                status_code,
                exc,
            )
            if status_code in (404, 410):
                await self._store.remove_endpoint(subscription.endpoint)
            return False
        except Exception:
            logger.exception("Unexpected Web Push delivery error for endpoint=%s", subscription.endpoint[:80])
            return False


def get_web_push_store(root: Path | None = None) -> WebPushSubscriptionStore:
    """Return the process-wide Web Push subscription store."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        with _STORE_LOCK:
            if _STORE_SINGLETON is None:
                _STORE_SINGLETON = WebPushSubscriptionStore(root=root)
    return _STORE_SINGLETON


def get_web_push_service(
    store: WebPushSubscriptionStore | None = None,
    vapid_public_key: str | None = None,
    vapid_private_key: str | None = None,
    vapid_subject: str | None = None,
) -> WebPushService:
    """Return the process-wide Web Push service."""
    global _SERVICE_SINGLETON
    if _SERVICE_SINGLETON is None:
        with _SERVICE_LOCK:
            if _SERVICE_SINGLETON is None:
                _SERVICE_SINGLETON = WebPushService(
                    store=store,
                    vapid_public_key=vapid_public_key,
                    vapid_private_key=vapid_private_key,
                    vapid_subject=vapid_subject,
                )
    return _SERVICE_SINGLETON


def reset_web_push_services_for_tests() -> None:
    """Reset Web Push singletons for tests."""
    global _STORE_SINGLETON, _SERVICE_SINGLETON
    with _STORE_LOCK:
        _STORE_SINGLETON = None
    with _SERVICE_LOCK:
        _SERVICE_SINGLETON = None


__all__ = [
    "WebPushService",
    "WebPushSubscription",
    "WebPushSubscriptionStore",
    "get_web_push_service",
    "get_web_push_store",
    "reset_web_push_services_for_tests",
]
