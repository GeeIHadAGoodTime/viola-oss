"""Shared capability safety controls and local or database-backed audit storage."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Callable, Protocol
from uuid import uuid4

from core.logging_config import get_logger

logger = get_logger(__name__)


class ControlState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    LIMITED = "limited"
    DRY_RUN = "dry_run"


class ControlSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    PANIC = "panic"


class ControlFailMode(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class ControlSource(StrEnum):
    ADMIN_UI = "admin_ui"
    CLI = "cli"
    ENV_BREAKGLASS = "env_breakglass"
    MIGRATION = "migration"
    AUTO_DISABLE = "auto_disable"
    TEST = "test"


class ControlReadError(RuntimeError):
    """Raised when the operator control store cannot be read."""


KILL_ALL_EXTERNAL_SPEND = "kill_all_external_spend"

TEMPORARILY_PAUSED_MESSAGE = "This capability is temporarily paused for safety."
STORE_UNAVAILABLE_MESSAGE = "This capability is temporarily paused while safety controls recover."


@dataclass(frozen=True)
class ControlDefinition:
    key: str
    default_state: ControlState
    severity: ControlSeverity
    fail_mode: ControlFailMode
    external_spend: bool
    public_message: str = TEMPORARILY_PAUSED_MESSAGE


@dataclass(frozen=True)
class ControlRecord:
    key: str
    state: ControlState
    severity: ControlSeverity
    fail_mode: ControlFailMode
    reason: str
    actor: str
    source: ControlSource
    updated_at: datetime
    revision: int
    external_spend: bool
    expires_at: datetime | None = None
    last_checked_at: datetime | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


@dataclass(frozen=True)
class ControlAuditRecord:
    audit_id: str
    control_key: str
    actor: str
    reason: str
    source: ControlSource
    previous_state: ControlState | None
    new_state: ControlState
    timestamp: datetime
    revision: int
    user_id: str | None = None
    request_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    control_key: str
    reason: str
    public_message: str
    state: ControlState | None = None
    retry_after_seconds: int | None = None
    audit_id: str | None = None
    user_id: str | None = None
    request_id: str | None = None
    action: str | None = None


class OperatorControlStore(Protocol):
    def get_control(self, key: str) -> ControlRecord: ...

    async def aget_control(self, key: str) -> ControlRecord: ...

    def set_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord: ...

    async def aset_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord: ...

    def list_controls(self) -> list[ControlRecord]: ...

    async def alist_controls(self) -> list[ControlRecord]: ...

    def audit_records(self) -> list[ControlAuditRecord]: ...

    async def aaudit_records(self) -> list[ControlAuditRecord]: ...

    def mark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord: ...

    async def amark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord: ...


DEFAULT_CONTROL_DEFINITIONS: tuple[ControlDefinition, ...] = (
    ControlDefinition(
        key=KILL_ALL_EXTERNAL_SPEND,
        default_state=ControlState.ENABLED,
        severity=ControlSeverity.PANIC,
        fail_mode=ControlFailMode.CLOSED,
        external_spend=False,
    ),
    ControlDefinition("managed_ai", ControlState.ENABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("phone_outbound", ControlState.LIMITED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("sms_outbound", ControlState.LIMITED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("cloud_browser", ControlState.LIMITED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("payment_autofill", ControlState.ENABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("merchant_submit", ControlState.ENABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("stripe_checkout", ControlState.ENABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition("new_signups", ControlState.ENABLED, ControlSeverity.WARNING, ControlFailMode.OPEN, False),
    ControlDefinition(
        "external_channels", ControlState.LIMITED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True
    ),
    ControlDefinition("cloud_sync", ControlState.LIMITED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True),
    ControlDefinition(
        "wake_data_upload", ControlState.DISABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, True
    ),
    ControlDefinition("telemetry_ingest", ControlState.ENABLED, ControlSeverity.WARNING, ControlFailMode.OPEN, False),
    ControlDefinition(
        "auto_update_rollout", ControlState.DISABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, False
    ),
    ControlDefinition("admin_writes", ControlState.ENABLED, ControlSeverity.CRITICAL, ControlFailMode.CLOSED, False),
)

DEFAULT_CONTROLS_BY_KEY = {definition.key: definition for definition in DEFAULT_CONTROL_DEFINITIONS}
EXTERNAL_SPEND_CONTROL_KEYS = frozenset(
    definition.key for definition in DEFAULT_CONTROL_DEFINITIONS if definition.external_spend
)


class InMemoryOperatorControlStore:
    def __init__(
        self,
        definitions: tuple[ControlDefinition, ...] = DEFAULT_CONTROL_DEFINITIONS,
        *,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        self._definitions = {definition.key: definition for definition in definitions}
        self._records = {
            definition.key: ControlRecord(
                key=definition.key,
                state=definition.default_state,
                severity=definition.severity,
                fail_mode=definition.fail_mode,
                reason="default control seed",
                actor="system",
                source=ControlSource.MIGRATION,
                updated_at=current_time,
                revision=1,
                external_spend=definition.external_spend,
            )
            for definition in definitions
        }
        self._audit_records: list[ControlAuditRecord] = []

    def get_control(self, key: str) -> ControlRecord:
        record = self._records[key]
        return self._expire_if_needed(record)

    async def aget_control(self, key: str) -> ControlRecord:
        return self.get_control(key)

    def set_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord:
        _validate_mutation(actor=actor, reason=reason, user_id=user_id)
        previous = self.get_control(key)
        current_time = datetime.now(UTC)
        expires_at = current_time + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        updated = replace(
            previous,
            state=state,
            reason=reason.strip(),
            actor=actor.strip(),
            source=source,
            updated_at=current_time,
            revision=previous.revision + 1,
            expires_at=expires_at,
        )
        self._records[key] = updated
        self._append_audit(
            previous=previous,
            updated=updated,
            user_id=user_id,
            request_id=request_id,
        )
        return updated

    async def aset_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord:
        return self.set_control(
            key,
            state,
            actor=actor,
            reason=reason,
            source=source,
            ttl_seconds=ttl_seconds,
            user_id=user_id,
            request_id=request_id,
        )

    def list_controls(self) -> list[ControlRecord]:
        return [self.get_control(key) for key in sorted(self._records)]

    async def alist_controls(self) -> list[ControlRecord]:
        return self.list_controls()

    def audit_records(self) -> list[ControlAuditRecord]:
        return list(self._audit_records)

    async def aaudit_records(self) -> list[ControlAuditRecord]:
        return self.audit_records()

    def mark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord:
        record = self.get_control(key)
        updated = replace(record, last_checked_at=checked_at)
        self._records[key] = updated
        return updated

    async def amark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord:
        return self.mark_checked(key, checked_at=checked_at)

    def _expire_if_needed(self, record: ControlRecord) -> ControlRecord:
        if record.expires_at is None or record.expires_at > datetime.now(UTC):
            return record
        definition = self._definitions[record.key]
        expired = replace(
            record,
            state=definition.default_state,
            reason="temporary control expired",
            actor="system",
            source=ControlSource.AUTO_DISABLE,
            updated_at=datetime.now(UTC),
            revision=record.revision + 1,
            expires_at=None,
        )
        self._records[record.key] = expired
        self._append_audit(previous=record, updated=expired, user_id=None, request_id=None)
        return expired

    def _append_audit(
        self,
        *,
        previous: ControlRecord,
        updated: ControlRecord,
        user_id: str | None,
        request_id: str | None,
    ) -> None:
        self._audit_records.append(
            ControlAuditRecord(
                audit_id=str(uuid4()),
                control_key=updated.key,
                actor=updated.actor,
                reason=updated.reason,
                source=updated.source,
                previous_state=previous.state,
                new_state=updated.state,
                timestamp=updated.updated_at,
                revision=updated.revision,
                user_id=user_id,
                request_id=request_id,
                expires_at=updated.expires_at,
            )
        )


class PostgresOperatorControlStore:
    """Durable cloud operator-control store backed by PostgreSQL."""

    def __init__(
        self,
        *,
        async_runner: Callable[..., Any] | None = None,
        pool_getter: Callable[..., Any] | None = None,
    ) -> None:
        self._initialized = False
        self._init_lock = threading.RLock()
        self._async_runner = async_runner
        self._pool_getter = pool_getter

    def get_control(self, key: str) -> ControlRecord:
        return self._run(self.aget_control(key))

    async def aget_control(self, key: str) -> ControlRecord:
        await self._aensure_initialized()
        row = await self._fetch_control_row(key)
        if row is None:
            raise KeyError(key)
        record = _control_record_from_pg_row(row)
        return await self._aexpire_if_needed(record)

    def set_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord:
        return self._run(
            self.aset_control(
                key,
                state,
                actor=actor,
                reason=reason,
                source=source,
                ttl_seconds=ttl_seconds,
                user_id=user_id,
                request_id=request_id,
            )
        )

    async def aset_control(
        self,
        key: str,
        state: ControlState,
        *,
        actor: str,
        reason: str,
        source: ControlSource = ControlSource.CLI,
        ttl_seconds: int | None = None,
        user_id: str | None = None,
        request_id: str | None = None,
    ) -> ControlRecord:
        _validate_mutation(actor=actor, reason=reason, user_id=user_id)
        await self._aensure_initialized()
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        row = await self._set_control_row(
            key=key,
            state=state,
            actor=actor.strip(),
            reason=reason.strip(),
            source=source,
            expires_at=expires_at,
            user_id=user_id,
            request_id=request_id,
        )
        return _control_record_from_pg_row(row)

    def list_controls(self) -> list[ControlRecord]:
        return self._run(self.alist_controls())

    async def alist_controls(self) -> list[ControlRecord]:
        await self._aensure_initialized()
        rows = await self._fetch_control_rows()
        records: list[ControlRecord] = []
        for row in rows:
            records.append(await self._aexpire_if_needed(_control_record_from_pg_row(row)))
        return sorted(records, key=lambda record: record.key)

    def audit_records(self) -> list[ControlAuditRecord]:
        return self._run(self.aaudit_records())

    async def aaudit_records(self) -> list[ControlAuditRecord]:
        await self._aensure_initialized()
        rows = await self._fetch_audit_rows()
        return [_audit_record_from_pg_row(row) for row in rows]

    def mark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord:
        return self._run(self.amark_checked(key, checked_at=checked_at))

    async def amark_checked(self, key: str, *, checked_at: datetime) -> ControlRecord:
        await self._aensure_initialized()
        row = await self._mark_checked_row(key, checked_at)
        if row is None:
            raise KeyError(key)
        return _control_record_from_pg_row(row)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self._run(self._initialize_async())
            self._initialized = True

    async def _aensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._initialize_async()
        self._initialized = True

    def _run(self, coro: Any) -> Any:
        from core.asyncio_safe import run_async_synchronously

        runner = self._async_runner or run_async_synchronously
        try:
            result = runner(
                coro,
                timeout=10.0,
                timeout_result=_CONTROL_READ_TIMEOUT,
                timeout_log_message="Timed out waiting for PostgreSQL operator control store after %ss",
                logger=logger,
            )
            if result is _CONTROL_READ_TIMEOUT:
                raise ControlReadError("PostgreSQL operator control store timed out")
            return result
        except (KeyError, ValueError):
            raise
        except Exception as exc:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise ControlReadError("PostgreSQL operator control store is unavailable") from exc

    async def _pool(self) -> Any:
        if self._pool_getter is not None:
            return await self._pool_getter()
        from core.db_backend import get_pg_pool

        return await get_pg_pool()

    async def _initialize_async(self) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            from core.db_backend import assert_pg_relations

            await assert_pg_relations(
                conn,
                ("public.operator_controls", "public.operator_control_audit"),
                owner="PostgresOperatorControlStore",
            )
            for definition in DEFAULT_CONTROL_DEFINITIONS:
                await conn.execute(
                    """
                    INSERT INTO operator_controls (
                        key, state, severity, fail_mode, reason, actor, source,
                        updated_at, revision, external_spend, expires_at, last_checked_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, $9, NULL, NULL)
                    ON CONFLICT (key) DO UPDATE SET
                        severity = EXCLUDED.severity,
                        fail_mode = EXCLUDED.fail_mode,
                        external_spend = EXCLUDED.external_spend
                    """,
                    definition.key,
                    definition.default_state.value,
                    definition.severity.value,
                    definition.fail_mode.value,
                    "default control seed",
                    "system",
                    ControlSource.MIGRATION.value,
                    datetime.now(UTC),
                    definition.external_spend,
                )

    async def _fetch_control_row(self, key: str) -> Any:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM operator_controls WHERE key = $1", key)

    async def _fetch_control_rows(self) -> list[Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return list(await conn.fetch("SELECT * FROM operator_controls ORDER BY key"))

    async def _fetch_audit_rows(self) -> list[Any]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM operator_control_audit ORDER BY timestamp ASC, revision ASC, audit_id ASC"
                )
            )

    async def _mark_checked_row(self, key: str, checked_at: datetime) -> Any:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(
                """
                UPDATE operator_controls
                SET last_checked_at = $2
                WHERE key = $1
                RETURNING *
                """,
                key,
                checked_at,
            )

    async def _set_control_row(
        self,
        *,
        key: str,
        state: ControlState,
        actor: str,
        reason: str,
        source: ControlSource,
        expires_at: datetime | None,
        user_id: str | None,
        request_id: str | None,
    ) -> Any:
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchrow("SELECT * FROM operator_controls WHERE key = $1 FOR UPDATE", key)
                if previous is None:
                    raise KeyError(key)
                previous_record = _control_record_from_pg_row(previous)
                updated_at = datetime.now(UTC)
                updated = await conn.fetchrow(
                    """
                    UPDATE operator_controls
                    SET state = $2,
                        reason = $3,
                        actor = $4,
                        source = $5,
                        updated_at = $6,
                        revision = revision + 1,
                        expires_at = $7
                    WHERE key = $1
                    RETURNING *
                    """,
                    key,
                    state.value,
                    reason,
                    actor,
                    source.value,
                    updated_at,
                    expires_at,
                )
                if updated is None:
                    raise KeyError(key)
                await conn.execute(
                    """
                    INSERT INTO operator_control_audit (
                        audit_id, control_key, actor, reason, source, previous_state,
                        new_state, timestamp, revision, user_id, request_id, expires_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """,
                    str(uuid4()),
                    key,
                    actor,
                    reason,
                    source.value,
                    previous_record.state.value,
                    state.value,
                    updated_at,
                    int(updated["revision"]),
                    user_id,
                    request_id,
                    expires_at,
                )
                return updated

    def _expire_if_needed(self, record: ControlRecord) -> ControlRecord:
        if record.expires_at is None or record.expires_at > datetime.now(UTC):
            return record
        definition = DEFAULT_CONTROLS_BY_KEY[record.key]
        return self.set_control(
            record.key,
            definition.default_state,
            actor="system",
            reason="temporary control expired",
            source=ControlSource.AUTO_DISABLE,
        )

    async def _aexpire_if_needed(self, record: ControlRecord) -> ControlRecord:
        if record.expires_at is None or record.expires_at > datetime.now(UTC):
            return record
        definition = DEFAULT_CONTROLS_BY_KEY[record.key]
        return await self.aset_control(
            record.key,
            definition.default_state,
            actor="system",
            reason="temporary control expired",
            source=ControlSource.AUTO_DISABLE,
        )


_CONTROL_READ_TIMEOUT = object()


def require_enabled(
    key: str,
    *,
    store: OperatorControlStore | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
    action: str | None = None,
) -> ControlDecision:
    _validate_user_id(user_id)
    definition = DEFAULT_CONTROLS_BY_KEY[key]

    if definition.external_spend and env_kill_all_external_spend_active():
        return ControlDecision(
            allowed=False,
            control_key=KILL_ALL_EXTERNAL_SPEND,
            reason="VIOLA_OPERATOR_KILL_ALL is active",
            public_message=TEMPORARILY_PAUSED_MESSAGE,
            state=ControlState.DISABLED,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )

    try:
        resolved_store = store or get_operator_control_store()
        if definition.external_spend:
            kill_all = resolved_store.get_control(KILL_ALL_EXTERNAL_SPEND)
            if kill_all.state is ControlState.DISABLED:
                return _denied_decision(
                    control=kill_all,
                    user_id=user_id,
                    request_id=request_id,
                    action=action,
                )
        control = resolved_store.get_control(key)
        checked = resolved_store.mark_checked(control.key, checked_at=datetime.now(UTC))
    except (ControlReadError, KeyError, RuntimeError) as exc:
        return _store_failure_decision(
            definition=definition,
            error=exc,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )

    if checked.state in {ControlState.ENABLED, ControlState.LIMITED, ControlState.DRY_RUN}:
        return ControlDecision(
            allowed=True,
            control_key=checked.key,
            reason=checked.reason,
            public_message="",
            state=checked.state,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )
    return _denied_decision(control=checked, user_id=user_id, request_id=request_id, action=action)


async def require_enabled_async(
    key: str,
    *,
    store: OperatorControlStore | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
    action: str | None = None,
) -> ControlDecision:
    _validate_user_id(user_id)
    definition = DEFAULT_CONTROLS_BY_KEY[key]

    if definition.external_spend and env_kill_all_external_spend_active():
        return ControlDecision(
            allowed=False,
            control_key=KILL_ALL_EXTERNAL_SPEND,
            reason="VIOLA_OPERATOR_KILL_ALL is active",
            public_message=TEMPORARILY_PAUSED_MESSAGE,
            state=ControlState.DISABLED,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )

    try:
        resolved_store = store or get_operator_control_store()
        if definition.external_spend:
            kill_all = await _aget_control(resolved_store, KILL_ALL_EXTERNAL_SPEND)
            if kill_all.state is ControlState.DISABLED:
                return _denied_decision(
                    control=kill_all,
                    user_id=user_id,
                    request_id=request_id,
                    action=action,
                )
        control = await _aget_control(resolved_store, key)
        checked = await _amark_checked(resolved_store, control.key, checked_at=datetime.now(UTC))
    except (ControlReadError, KeyError, RuntimeError) as exc:
        return _store_failure_decision(
            definition=definition,
            error=exc,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )

    if checked.state in {ControlState.ENABLED, ControlState.LIMITED, ControlState.DRY_RUN}:
        return ControlDecision(
            allowed=True,
            control_key=checked.key,
            reason=checked.reason,
            public_message="",
            state=checked.state,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )
    return _denied_decision(control=checked, user_id=user_id, request_id=request_id, action=action)


async def _aget_control(store: OperatorControlStore, key: str) -> ControlRecord:
    getter = getattr(store, "aget_control", None)
    if callable(getter):
        return await getter(key)
    return store.get_control(key)


async def _amark_checked(store: OperatorControlStore, key: str, *, checked_at: datetime) -> ControlRecord:
    marker = getattr(store, "amark_checked", None)
    if callable(marker):
        return await marker(key, checked_at=checked_at)
    return store.mark_checked(key, checked_at=checked_at)


def _denied_decision(
    *,
    control: ControlRecord,
    user_id: str | None,
    request_id: str | None,
    action: str | None,
) -> ControlDecision:
    return ControlDecision(
        allowed=False,
        control_key=control.key,
        reason=control.reason,
        public_message=DEFAULT_CONTROLS_BY_KEY[control.key].public_message,
        state=control.state,
        user_id=user_id,
        request_id=request_id,
        action=action,
    )


def _store_failure_decision(
    *,
    definition: ControlDefinition,
    error: Exception,
    user_id: str | None,
    request_id: str | None,
    action: str | None,
) -> ControlDecision:
    # Log the underlying failure so a safety denial remains diagnosable.
    logger.warning(
        "operator_control store read failed: key=%s fail_mode=%s error=%s: %s",
        definition.key,
        definition.fail_mode.name if hasattr(definition.fail_mode, "name") else str(definition.fail_mode),
        type(error).__name__,
        error,
        exc_info=True,
    )
    if definition.fail_mode is ControlFailMode.OPEN:
        return ControlDecision(
            allowed=True,
            control_key=definition.key,
            reason=f"control store unavailable; fail-open: {type(error).__name__}",
            public_message="",
            state=None,
            user_id=user_id,
            request_id=request_id,
            action=action,
        )
    return ControlDecision(
        allowed=False,
        control_key=definition.key,
        reason=f"control store unavailable; fail-closed: {type(error).__name__}",
        public_message=STORE_UNAVAILABLE_MESSAGE,
        state=None,
        user_id=user_id,
        request_id=request_id,
        action=action,
    )


def _validate_mutation(*, actor: str, reason: str, user_id: str | None) -> None:
    if not actor or not actor.strip():
        raise ValueError("actor is required for operator control mutations")
    if not reason or not reason.strip():
        raise ValueError("reason is required for operator control mutations")
    _validate_user_id(user_id)


def _validate_user_id(user_id: str | None) -> None:
    if user_id == "default":
        raise ValueError('user_id="default" is not allowed for operator controls')


_operator_control_store: OperatorControlStore | None = None


def get_operator_control_store() -> OperatorControlStore:
    global _operator_control_store
    if _operator_control_store is None:
        from core.database_strategy import postgres_url_for_surface

        if postgres_url_for_surface("operator_controls"):
            _operator_control_store = PostgresOperatorControlStore()
        else:
            _operator_control_store = InMemoryOperatorControlStore()
    return _operator_control_store


def set_operator_control_store_for_tests(store: OperatorControlStore | None) -> None:
    global _operator_control_store
    _operator_control_store = store


def env_kill_all_external_spend_active() -> bool:
    value = os.environ.get("VIOLA_OPERATOR_KILL_ALL", "").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def control_record_to_dict(record: ControlRecord) -> dict[str, object]:
    return {
        "key": record.key,
        "state": record.state.value,
        "severity": record.severity.value,
        "fail_mode": record.fail_mode.value,
        "reason": record.reason,
        "actor": record.actor,
        "source": record.source.value,
        "updated_at": record.updated_at.isoformat(),
        "revision": record.revision,
        "external_spend": record.external_spend,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "last_checked_at": record.last_checked_at.isoformat() if record.last_checked_at else None,
    }


def audit_record_to_dict(record: ControlAuditRecord) -> dict[str, object]:
    return {
        "audit_id": record.audit_id,
        "control_key": record.control_key,
        "actor": record.actor,
        "reason": record.reason,
        "source": record.source.value,
        "previous_state": record.previous_state.value if record.previous_state else None,
        "new_state": record.new_state.value,
        "timestamp": record.timestamp.isoformat(),
        "revision": record.revision,
        "user_id": record.user_id,
        "request_id": record.request_id,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
    }


def decision_to_dict(decision: ControlDecision) -> dict[str, object]:
    return {
        "allowed": decision.allowed,
        "control_key": decision.control_key,
        "reason": decision.reason,
        "public_message": decision.public_message,
        "state": decision.state.value if decision.state else None,
        "retry_after_seconds": decision.retry_after_seconds,
        "audit_id": decision.audit_id,
        "user_id": decision.user_id,
        "request_id": decision.request_id,
        "action": decision.action,
    }


def _control_record_from_pg_row(row: Any) -> ControlRecord:
    return ControlRecord(
        key=str(row["key"]),
        state=ControlState(str(row["state"])),
        severity=ControlSeverity(str(row["severity"])),
        fail_mode=ControlFailMode(str(row["fail_mode"])),
        reason=str(row["reason"]),
        actor=str(row["actor"]),
        source=ControlSource(str(row["source"])),
        updated_at=_coerce_datetime(row["updated_at"]),
        revision=int(row["revision"]),
        external_spend=bool(row["external_spend"]),
        expires_at=_coerce_optional_datetime(row["expires_at"]),
        last_checked_at=_coerce_optional_datetime(row["last_checked_at"]),
    )


def _audit_record_from_pg_row(row: Any) -> ControlAuditRecord:
    previous_state = row["previous_state"]
    return ControlAuditRecord(
        audit_id=str(row["audit_id"]),
        control_key=str(row["control_key"]),
        actor=str(row["actor"]),
        reason=str(row["reason"]),
        source=ControlSource(str(row["source"])),
        previous_state=ControlState(str(previous_state)) if previous_state else None,
        new_state=ControlState(str(row["new_state"])),
        timestamp=_coerce_datetime(row["timestamp"]),
        revision=int(row["revision"]),
        user_id=str(row["user_id"]) if row["user_id"] else None,
        request_id=str(row["request_id"]) if row["request_id"] else None,
        expires_at=_coerce_optional_datetime(row["expires_at"]),
    )


def _coerce_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
