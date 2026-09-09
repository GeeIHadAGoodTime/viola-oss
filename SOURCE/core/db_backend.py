"""Shared database backend utilities for dual-mode SQLite/PostgreSQL stores.

All Viola stores that need PostgreSQL support use this module for:
- Detecting the active backend via VIOLA_DATABASE_URL
- Lazy asyncpg pool creation (shared across stores)
- SQL dialect helpers (placeholder conversion, upsert syntax)

Usage::

    from core.db_backend import get_database_url, get_pg_pool, is_postgres

    if is_postgres():
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(...)
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import uuid
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from core.logging_config import get_logger

if TYPE_CHECKING:
    import asyncpg

logger = get_logger("viola.core.db_backend")

PG_POOL_MIN_SIZE = 1
PG_POOL_MAX_SIZE = 5
PG_POOL_MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS = 120
PG_POOL_COMMAND_TIMEOUT_SECONDS = 30
PG_POOL_SERVER_SETTINGS = {"application_name": "viola-cloud"}

# SERVING-LOOP-WEDGE GUARD: discarding a stale/cross-loop pool must NEVER hold
# ``_get_pool_lock`` across an unbounded ``pool.close()``. asyncpg's
# ``Pool.close()`` blocks until every checked-out connection is released; a
# connection stranded on a dead/other event loop is never released, so
# ``close()`` never returns. When that ``await`` runs under ``_get_pool_lock``
# on the serving loop (get_pg_pool's discard path, db_backend recreation during
# an agent task), the lock is held forever and every subsequent get_pg_pool() on
# the serving loop — request middleware, the /health DB touch, the metrics
# writer's own coroutine — parks on it. The loop is not busy (CPU ~3%); it is
# parked on a permanently-held asyncio.Lock. That is the 2026-07-06 ~40-minute
# wedge (api.useviola.com Up-but-unhealthy, all routes 000). Bounding the close
# and falling back to the immediate, non-blocking ``terminate()`` guarantees the
# lock is always released promptly, converting a permanent park into a bounded
# self-heal.
PG_POOL_CLOSE_TIMEOUT_SECONDS = 5.0

_pool_lock: asyncio.Lock | None = None
_pool_lock_loop: asyncio.AbstractEventLoop | None = None
_pool: ResilientAsyncpgPool | None = None
_pool_url: str | None = None
_pool_server_settings_key: tuple[tuple[str, str], ...] | None = None
_db_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "db_user_id",
    default=None,
)

_UNDEFINED_COLUMN_RE = re.compile(
    r'column "(?P<column>[^"]+)" (?:of relation "(?P<table>[^"]+)" )?does not exist',
    re.IGNORECASE,
)
_CREATE_TABLE_IF_NOT_EXISTS_RE = re.compile(
    r"^\s*CREATE\s+(?:UNLOGGED\s+|TEMPORARY\s+|TEMP\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<name>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)",
    re.IGNORECASE,
)
_CREATE_INDEX_IF_NOT_EXISTS_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<name>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)",
    re.IGNORECASE,
)
_ALTER_ADD_COLUMN_IF_NOT_EXISTS_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+"
    r"(?P<table>(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*))?)"
    r"\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
    r"(?P<column>\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)

_UNDEFINED_COLUMN_ERROR_TYPES: tuple[type[BaseException], ...] | None = None


def _extract_undefined_column_error(
    exc: BaseException,
) -> tuple[str | None, str | None]:
    """Extract missing table/column from asyncpg UndefinedColumnError."""
    match = _UNDEFINED_COLUMN_RE.search(str(exc))
    if not match:
        return None, None

    column = match.group("column")
    table = match.group("table")
    if table and "." in table:
        table = table.rsplit(".", 1)[-1]
    if table == "public":
        table = None
    return table, column


def _is_metadata_schema_table(table: str | None) -> bool:
    if table is None:
        return False
    return table == "schema_version" or table.endswith("_schema_version")


def _query_table_hint(sql: Any) -> str | None:
    if not isinstance(sql, str):
        return None

    patterns = [r"\b(?:FROM|INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?([A-Za-z_][A-Za-z0-9_]*)(?:\s|\()"]
    for expression in patterns:
        match = re.search(expression, sql, re.IGNORECASE)
        if match:
            table = match.group(1)
            return table.strip().strip('"').strip("'")
    return None


def _default_execute_command(sql: Any) -> str:
    if isinstance(sql, str):
        prefix = sql.lstrip().split(maxsplit=1)[0].upper()
        if prefix in {
            "INSERT",
            "UPDATE",
            "DELETE",
            "SELECT",
            "CREATE",
            "DROP",
            "ALTER",
        }:
            return f"{prefix} 0"
    return "COMMAND 0"


def _safe_table_fallback(method_name: str, sql: Any, exc: BaseException, logger_name: str) -> Any:
    table, column = _extract_undefined_column_error(exc)
    if table is None:
        table = _query_table_hint(sql)

    if _is_metadata_schema_table(table):
        raise

    get_logger(logger_name).warning(
        "PostgreSQL schema drift detected; skipping operation for missing column",
        extra={
            "operation": method_name,
            "table": table,
            "column": column,
            "query": str(sql)[:240] if isinstance(sql, str) else None,
        },
    )

    if method_name == "fetchrow" or method_name == "fetchval":
        return None
    if method_name == "fetch":
        return []
    if method_name == "executemany":
        return None
    return _default_execute_command(sql)


def _is_asyncpg_undefined_column_error(exc: BaseException) -> bool:
    global _UNDEFINED_COLUMN_ERROR_TYPES
    if _UNDEFINED_COLUMN_ERROR_TYPES is None:
        try:
            import asyncpg
        except ImportError:
            _UNDEFINED_COLUMN_ERROR_TYPES = ()
        else:
            undefined_error = getattr(asyncpg.exceptions, "UndefinedColumnError", None)
            if undefined_error is None:
                undefined_error = getattr(asyncpg, "UndefinedColumnError", None)
            if isinstance(undefined_error, type):
                _UNDEFINED_COLUMN_ERROR_TYPES = (undefined_error,)
            else:
                _UNDEFINED_COLUMN_ERROR_TYPES = ()

    if not _UNDEFINED_COLUMN_ERROR_TYPES:
        return type(exc).__name__ == "UndefinedColumnError"
    return isinstance(exc, _UNDEFINED_COLUMN_ERROR_TYPES)


def _is_asyncpg_insufficient_privilege_error(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "42501" or type(exc).__name__ == "InsufficientPrivilegeError"


def _unquote_pg_identifier(identifier: str) -> str:
    cleaned = identifier.strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
        return cleaned[1:-1].replace('""', '"')
    return cleaned.lower()


def _split_relation_name(name: str) -> tuple[str, str]:
    parts = [part.strip() for part in name.split(".", 1)]
    if len(parts) == 1:
        return "public", _unquote_pg_identifier(parts[0])
    return _unquote_pg_identifier(parts[0]), _unquote_pg_identifier(parts[1])


async def _relation_exists(conn: Any, relation_name: str) -> bool:
    schema, relation = _split_relation_name(relation_name)
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1
                  AND c.relname = $2
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
            )
            """,
            schema,
            relation,
        )
    )


async def _column_exists(conn: Any, table_name: str, column_name: str) -> bool:
    schema, table = _split_relation_name(table_name)
    column = _unquote_pg_identifier(column_name)
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = $1
                  AND table_name = $2
                  AND column_name = $3
            )
            """,
            schema,
            table,
            column,
        )
    )


async def _maybe_skip_existing_idempotent_ddl(
    conn: Any,
    sql: Any,
    exc: BaseException,
) -> str | None:
    if not isinstance(sql, str) or not _is_asyncpg_insufficient_privilege_error(exc):
        return None

    create_table_match = _CREATE_TABLE_IF_NOT_EXISTS_RE.match(sql)
    if create_table_match and await _relation_exists(conn, create_table_match.group("name")):
        logger.warning(
            "Skipping existing PostgreSQL table DDL under non-owner role",
            extra={"relation": create_table_match.group("name")},
        )
        return _default_execute_command(sql)

    create_index_match = _CREATE_INDEX_IF_NOT_EXISTS_RE.match(sql)
    if create_index_match and await _relation_exists(conn, create_index_match.group("name")):
        logger.warning(
            "Skipping existing PostgreSQL index DDL under non-owner role",
            extra={"relation": create_index_match.group("name")},
        )
        return _default_execute_command(sql)

    alter_column_match = _ALTER_ADD_COLUMN_IF_NOT_EXISTS_RE.match(sql)
    if alter_column_match and await _column_exists(
        conn,
        alter_column_match.group("table"),
        alter_column_match.group("column"),
    ):
        logger.warning(
            "Skipping existing PostgreSQL column DDL under non-owner role",
            extra={
                "relation": alter_column_match.group("table"),
                "column": alter_column_match.group("column"),
            },
        )
        return _default_execute_command(sql)

    return None


async def _safe_pg_call(
    method_name: str,
    call,
    *args: Any,
    logger_name: str = "viola.core.db_backend",
    **kwargs: Any,
) -> Any:
    """Run a single PG method and degrade on schema-column drift."""
    try:
        return await call(*args, **kwargs)
    except Exception as exc:  # pylint: disable=broad-except
        if not _is_asyncpg_undefined_column_error(exc):
            raise
        return _safe_table_fallback(method_name, args[0] if args else None, exc, logger_name)


class _SafePgConnection:
    """Proxy around asyncpg connections that degrades gracefully on schema drift.

    Also the seam that enforces RLS tenant context: a data mutation issued on a
    connection with no ``app.user_id`` wired (and not inside ``system_db_scope``)
    against an owner-RLS table raises ``RlsContextRequiredError`` before the
    statement reaches Postgres. ``rls_context_set`` seeds True when the ambient
    user-scoped transaction already ran ``set_config('app.user_id', ...)``; a
    later ``set_rls_context`` (which flows through ``execute`` here) flips it too.
    """

    def __init__(self, conn: Any, *, rls_context_set: bool = False) -> None:
        self._conn = conn
        self._rls_context_set = rls_context_set

    def transaction(self, *args: Any, **kwargs: Any):
        return self._conn.transaction(*args, **kwargs)

    async def _guard_tenant_write(self, sql: Any) -> None:
        """Fail closed on a context-less tenant mutation (cloud surface only)."""
        if self._rls_context_set or not _db_user_context_enabled():
            return
        if is_sql_set_app_user_id(sql):
            # This very statement wires the tenant; let it through and remember it.
            self._rls_context_set = True
            return
        is_mutation, _table = sql_mutation_target(sql)
        if not is_mutation or _db_system_scope.get(False):
            return
        guarded = await _load_guarded_rls_tables(self._conn)
        _assert_tenant_write_permitted(sql, context_set=False, guarded_tables=guarded)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            await self._guard_tenant_write(args[0])
        try:
            return await _safe_pg_call(
                "execute",
                self._conn.execute,
                *args,
                logger_name="viola.core.db_backend.connection",
                **kwargs,
            )
        except Exception as exc:  # pylint: disable=broad-except
            skipped = await _maybe_skip_existing_idempotent_ddl(
                self._conn,
                args[0] if args else None,
                exc,
            )
            if skipped is not None:
                return skipped
            raise

    async def executemany(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            await self._guard_tenant_write(args[0])
        return await _safe_pg_call(
            "executemany",
            self._conn.executemany,
            *args,
            logger_name="viola.core.db_backend.connection",
            **kwargs,
        )

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        # RETURNING mutations and data-modifying CTEs run through fetch*; a plain
        # SELECT is classified as a non-mutation, so this only guards writes.
        if args:
            await self._guard_tenant_write(args[0])
        return await _safe_pg_call(
            "fetch",
            self._conn.fetch,
            *args,
            logger_name="viola.core.db_backend.connection",
            **kwargs,
        )

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            await self._guard_tenant_write(args[0])
        return await _safe_pg_call(
            "fetchrow",
            self._conn.fetchrow,
            *args,
            logger_name="viola.core.db_backend.connection",
            **kwargs,
        )

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            await self._guard_tenant_write(args[0])
        return await _safe_pg_call(
            "fetchval",
            self._conn.fetchval,
            *args,
            logger_name="viola.core.db_backend.connection",
            **kwargs,
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover
        return getattr(self._conn, name)


def _asyncpg_connection_errors() -> tuple[type[BaseException], ...]:
    try:
        import asyncpg as _asyncpg
    except ImportError:
        return ()

    error_types: list[type[BaseException]] = []
    interface_error = getattr(_asyncpg, "InterfaceError", None)
    if isinstance(interface_error, type):
        error_types.append(interface_error)

    connection_gone_error = getattr(_asyncpg, "ConnectionDoesNotExistError", None)
    if connection_gone_error is None:
        connection_gone_error = getattr(getattr(_asyncpg, "exceptions", None), "ConnectionDoesNotExistError", None)
    if isinstance(connection_gone_error, type) and connection_gone_error not in error_types:
        error_types.append(connection_gone_error)

    return tuple(error_types)


def _is_asyncpg_connection_error(exc: BaseException) -> bool:
    error_types = _asyncpg_connection_errors()
    return bool(error_types) and isinstance(exc, error_types)


def set_db_user_id(user_id: str) -> contextvars.Token[str | None]:
    """Set the authenticated database user for RLS-scoped cloud connections."""
    value = str(user_id or "").strip()
    if not value:
        raise ValueError("db user_id must be a non-empty UUID string")
    return _db_user_id.set(value)


def reset_db_user_id(token: contextvars.Token[str | None]) -> None:
    """Reset the database user context using a token from ``set_db_user_id``."""
    _db_user_id.reset(token)


def get_db_user_id() -> str | None:
    """Return the ambient database user id, or None when no request set it."""
    return _db_user_id.get(None)


def _db_user_context_enabled() -> bool:
    try:
        from config.settings import settings

        surface = str(getattr(settings, "app_surface", "")).strip().lower()
    except Exception:
        surface = str(os.environ.get("VIOLA_APP_SURFACE", "")).strip().lower()
    return surface == "cloud"


# =============================================================================
# RLS context enforcement at the connection seam (fail-closed tenant writes)
# =============================================================================
#
# Recurrence cluster #2106 / #2123 / #2113 / #737 / #738 / #1365 / #2288: an
# admin or subscription write path acquires a viola_app connection and issues a
# mutation against an owner-RLS table WITHOUT first wiring ``app.user_id`` (via
# the ambient ``set_db_user_id`` contextvar or a per-connection
# ``set_rls_context``). Because viola_app is NOSUPERUSER/NOBYPASSRLS, the write
# then fails closed at the DB with a confusing ``InsufficientPrivilegeError`` on
# INSERT or a SILENT 0-row no-op on UPDATE/DELETE — and, worse, any future path
# that ran under a BYPASSRLS role would cross tenant boundaries instead.
#
# Prior art fixed this one call-site at a time. This is the CLASS fix: enforce
# the invariant at the structural seam every connection passes through, so a
# tenant-scoped write with no resolvable ``app.user_id`` RAISES before the
# statement reaches Postgres — never silently reaches the DB with unset context.
#
# The guard is cloud-only (``_db_user_context_enabled()``); desktop/SQLite is
# unaffected. A write is "tenant-scoped" iff it targets a table that Postgres
# itself protects with an owner policy requiring ``current_user_id()`` and does
# NOT also carry a ``*_system_null_*`` permissive policy (the deliberate
# global/system rows — webhooks, payment/auth events, gdpr audit). That guarded
# set is read from ``pg_policies`` at runtime (authoritative + drift-free: a new
# owner-RLS table added by a future migration is covered automatically), cached
# per DB URL. Genuinely global writes declare themselves with ``system_db_scope``.


class RlsContextRequiredError(RuntimeError):
    """A tenant-scoped write was attempted with ``app.user_id`` unset.

    Raised at the connection seam BEFORE the statement reaches Postgres. The fix
    is to wire the tenant on the connection first — either set the ambient
    ``set_db_user_id(user_id)`` context (request middleware does this) or call
    ``services.sync.middleware_helpers.set_rls_context(conn, user_id)`` inside
    the same ``conn.transaction()`` block — or, for a deliberate global/system
    row (no tenant), wrap the write in ``core.db_backend.system_db_scope()``.
    """


# Ambient marker: the current async context is a deliberate global/system DB
# operation with no tenant (schema DDL, a null-context system-row write). Set it
# with ``system_db_scope()``. This is the ONLY sanctioned way to write without
# app.user_id on the cloud surface.
_db_system_scope: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "db_system_scope",
    default=False,
)


@contextmanager
def system_db_scope() -> Iterator[None]:
    """Mark the enclosed DB work as a deliberate global/system (no-tenant) op.

    Inside this scope the tenant-write guard permits mutations that have no
    ``app.user_id`` set (schema DDL, ``*_system_null_*`` rows). Use it ONLY for
    writes that legitimately have no tenant — never to silence the guard on a
    per-user write (wire the tenant instead).
    """
    token = _db_system_scope.set(True)
    try:
        yield
    finally:
        _db_system_scope.reset(token)


# --- mutation + target-table extraction (closed statement-verb classification,
#     not semantic/intent parsing) ------------------------------------------------

_LEADING_NOISE_RE = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/)+", re.DOTALL)
_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED_IDENT = r"%s(?:\.%s)?" % (_IDENT, _IDENT)
_DATA_MODIFYING_LEAD = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "COPY"})
_SET_APP_USER_ID_RE = re.compile(r"set_config\s*\(\s*'app\.user_id'", re.IGNORECASE)
_CTE_MUTATION_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\b",
    re.IGNORECASE,
)
_TABLE_AFTER_VERB: dict[str, re.Pattern[str]] = {
    "INSERT": re.compile(r"^INSERT\s+INTO\s+(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
    "UPDATE": re.compile(r"^UPDATE\s+(?:ONLY\s+)?(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
    "DELETE": re.compile(r"^DELETE\s+FROM\s+(?:ONLY\s+)?(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
    "MERGE": re.compile(r"^MERGE\s+INTO\s+(?:ONLY\s+)?(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
    "TRUNCATE": re.compile(r"^TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
    "COPY": re.compile(r"^COPY\s+(%s)" % _QUALIFIED_IDENT, re.IGNORECASE),
}


def _strip_sql_ident(token: str) -> str:
    token = token.strip()
    if token.startswith('"') and token.endswith('"') and len(token) >= 2:
        return token[1:-1].replace('""', '"')
    return token.lower()


def _normalize_table(raw: str) -> str:
    """Return ``schema.table`` (lowercased, unquoted) for a raw dotted identifier."""
    parts = [p for p in raw.split(".", 1)]
    return ".".join(_strip_sql_ident(p) for p in parts)


def is_sql_set_app_user_id(sql: Any) -> bool:
    """True when *sql* wires ``app.user_id`` (``SELECT set_config('app.user_id',...)``)."""
    return isinstance(sql, str) and bool(_SET_APP_USER_ID_RE.search(sql))


def sql_mutation_target(sql: Any) -> tuple[bool, str | None]:
    """Classify *sql*: ``(is_data_mutation, target_table_or_None)``.

    Closed classification on the statement's leading verb — INSERT/UPDATE/DELETE/
    MERGE/TRUNCATE and COPY...FROM are data mutations; a leading ``WITH`` is a
    mutation iff it carries a data-modifying CTE. Reads (SELECT/SHOW/SET/COPY..TO)
    return ``(False, None)``. ``target_table`` is ``None`` when the statement is a
    mutation whose target could not be extracted (the caller fails closed on it).
    """
    if not isinstance(sql, str):
        return (False, None)
    stripped = _LEADING_NOISE_RE.sub("", sql).lstrip()
    if not stripped:
        return (False, None)
    lead_match = re.match(r"[A-Za-z_]+", stripped)
    if lead_match is None:
        return (False, None)
    lead = lead_match.group(0).upper()

    if lead == "WITH":
        cte_hit = _CTE_MUTATION_RE.search(stripped)
        if cte_hit is None:
            return (False, None)  # read-only CTE (WITH ... SELECT)
        verb = cte_hit.group(1).split()[0].upper()
        target = _TABLE_AFTER_VERB[verb].match(stripped[cte_hit.start() :])
        return (True, _normalize_table(target.group(1)) if target else None)

    if lead == "COPY":
        # COPY t FROM ... writes; COPY t TO ... / COPY (query) TO ... reads.
        if not re.search(r"\bFROM\b", stripped, re.IGNORECASE):
            return (False, None)

    if lead in _DATA_MODIFYING_LEAD:
        target = _TABLE_AFTER_VERB[lead].match(stripped)
        return (True, _normalize_table(target.group(1)) if target else None)

    return (False, None)


# Per-DB-URL cache of the owner-RLS guarded-table set, keyed by the redacted URL
# so a pool rebuild pointed at a different DB re-introspects. ``None`` = not yet
# loaded for that URL.
_rls_guarded_tables: dict[str, frozenset[str]] = {}

# Tables that require a real tenant to write: an owner policy references
# current_user_id(), and the table has NO permissive null-context policy (the
# ``*_system_null_*`` family, or a policy whose predicate admits a NULL tenant).
_RLS_GUARDED_TABLES_SQL = """
WITH pol AS (
    SELECT schemaname, tablename, policyname,
           lower(coalesce(qual, '') || ' ' || coalesce(with_check, '')) AS expr
    FROM pg_policies
)
SELECT DISTINCT p.schemaname, p.tablename
FROM pol p
WHERE p.expr LIKE '%current_user_id%'
  AND NOT EXISTS (
      SELECT 1 FROM pol q
      WHERE q.schemaname = p.schemaname
        AND q.tablename = p.tablename
        AND (
            q.policyname ILIKE '%system_null%'
            OR q.expr LIKE '%current_user_id() is null%'
            OR q.expr LIKE '%current_user_id() is not distinct from null%'
        )
  )
"""


def _guarded_tables_cache_key() -> str:
    return _redact_url(_pool_url) if _pool_url else "default"


async def _load_guarded_rls_tables(reader: Any) -> frozenset[str]:
    """Read + cache the owner-RLS guarded-table set via *reader* (a conn/pool ``fetch``)."""
    key = _guarded_tables_cache_key()
    cached = _rls_guarded_tables.get(key)
    if cached is not None:
        return cached
    rows = await reader.fetch(_RLS_GUARDED_TABLES_SQL)
    tables: set[str] = set()
    for row in rows:
        schema = str(row["schemaname"]).lower()
        table = str(row["tablename"]).lower()
        tables.add(table)
        tables.add("%s.%s" % (schema, table))
    result = frozenset(tables)
    if not result:
        # An empty guarded set on the cloud surface means pg_policies advertises
        # no owner RLS — a serious misconfiguration. Warn loudly, but do not hard
        # block every write (that would wedge signup/login); the static guard-
        # present gate + the per-connection RLS remain the belt-and-suspenders.
        logger.warning(
            "RLS tenant-write guard: pg_policies advertises zero owner-RLS tables; "
            "guard is inert this run — verify owner RLS is provisioned on the cloud DB",
        )
    _rls_guarded_tables[key] = result
    return result


def _assert_tenant_write_permitted(
    sql: Any,
    *,
    context_set: bool,
    guarded_tables: frozenset[str],
) -> None:
    """Raise ``RlsContextRequiredError`` for a context-less tenant mutation.

    ``context_set`` is True when ``app.user_id`` has been established on this
    connection (ambient auto-transaction or a ``set_config('app.user_id', ...)``
    call). A no-op unless the statement is a data mutation, context is unset, and
    the caller is not inside ``system_db_scope()``.
    """
    if context_set or _db_system_scope.get(False):
        return
    is_mutation, table = sql_mutation_target(sql)
    if not is_mutation:
        return
    if table is not None:
        bare = table.rsplit(".", 1)[-1]
        if bare not in guarded_tables and table not in guarded_tables:
            # A non-owner-RLS / system-null / pre-auth table (users, magic_links,
            # oauth_states, webhooks, ...) — a context-less write is legitimate.
            return
    # Either an owner-RLS table with no tenant wired, or an unresolvable mutation
    # target with no tenant wired: fail closed, loud, before Postgres sees it.
    preview = re.sub(r"\s+", " ", str(sql)).strip()[:120]
    target_desc = ("table %r" % table) if table is not None else "an unresolved target"
    raise RlsContextRequiredError(
        "tenant-scoped write to %s attempted with app.user_id unset. Wire the tenant "
        "on the connection first (ambient set_db_user_id(user_id), or "
        "set_rls_context(conn, user_id) inside the same conn.transaction()), or wrap a "
        "genuine no-tenant system write in core.db_backend.system_db_scope(). SQL: %s" % (target_desc, preview)
    )


def _connection_db_user_id() -> str | None:
    if not _db_user_context_enabled():
        return None
    user_id = get_db_user_id()
    if not user_id:
        return None
    try:
        return str(uuid.UUID(user_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("cloud database user_id must be a valid UUID") from exc


async def _begin_user_scoped_transaction(conn: Any) -> Any | None:
    user_id = _connection_db_user_id()
    if user_id is None:
        return None

    transaction = conn.transaction()
    await transaction.__aenter__()
    try:
        await conn.execute("SELECT set_config('app.user_id', $1, true)", user_id)
    except Exception as exc:
        await transaction.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    return transaction


async def _verify_asyncpg_connection(conn: Any) -> None:
    await conn.fetchval("SELECT 1")


async def _configure_asyncpg_connection(conn: Any) -> None:
    await conn.set_type_codec(
        "uuid",
        schema="pg_catalog",
        encoder=str,
        decoder=str,
        format="text",
    )


async def assert_pg_relations(conn: Any, relations: list[str] | tuple[str, ...], *, owner: str) -> None:
    """Fail closed when Alembic-owned PostgreSQL relations are absent."""
    missing: list[str] = []
    for relation in relations:
        if not await _relation_exists(conn, relation):
            missing.append(relation)
    if missing:
        raise RuntimeError("%s missing Alembic-owned PostgreSQL relations: %s" % (owner, ", ".join(sorted(missing))))


async def assert_pg_columns(conn: Any, columns_by_relation: dict[str, set[str]], *, owner: str) -> None:
    """Fail closed when Alembic-owned PostgreSQL columns are absent."""
    missing: list[str] = []
    for relation, expected_columns in columns_by_relation.items():
        schema, table = _split_relation_name(relation)
        rows = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = $1
              AND table_name = $2
            """,
            schema,
            table,
        )
        actual = {str(row["column_name"]) for row in rows}
        missing.extend("%s.%s" % (relation, column) for column in sorted(expected_columns - actual))
    if missing:
        raise RuntimeError("%s missing Alembic-owned PostgreSQL columns: %s" % (owner, ", ".join(missing)))


class _RetryingAcquire:
    def __init__(self, pool: Any, *args: Any, **kwargs: Any) -> None:
        self._pool = pool
        self._args = args
        self._kwargs = kwargs
        self._manager: Any | None = None
        self._conn: Any | None = None
        self._transaction: Any | None = None

    async def __aenter__(self) -> Any:
        last_exc: Exception | None = None
        for attempt in range(2):
            manager = self._pool.acquire(*self._args, **self._kwargs)
            self._manager = manager
            try:
                self._conn = await manager.__aenter__()
                self._transaction = await _begin_user_scoped_transaction(self._conn)
                # When the ambient user-scoped transaction was opened it already
                # ran set_config('app.user_id', ...) on this connection, so the
                # tenant is wired; otherwise the connection carries no context and
                # the guard fails closed on any tenant mutation.
                return _SafePgConnection(self._conn, rls_context_set=self._transaction is not None)
            except Exception as exc:
                last_exc = exc
                with suppress(Exception):
                    await manager.__aexit__(type(exc), exc, exc.__traceback__)
                self._manager = None
                self._conn = None
                if not _is_asyncpg_connection_error(exc) or attempt > 0:
                    raise
                logger.warning(
                    "asyncpg pool acquire failed with %s; retrying once",
                    exc.__class__.__name__,
                )
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("asyncpg pool acquire failed before returning a connection")

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if self._manager is None:
            return None
        if self._transaction is not None:
            try:
                await self._transaction.__aexit__(exc_type, exc, tb)
            except Exception as tx_exc:
                await self._manager.__aexit__(type(tx_exc), tx_exc, tx_exc.__traceback__)
                raise
            finally:
                self._transaction = None
        return await self._manager.__aexit__(exc_type, exc, tb)


def _get_pool_lock() -> asyncio.Lock:
    global _pool_lock, _pool_lock_loop

    loop = asyncio.get_running_loop()
    if _pool_lock is None or _pool_lock_loop is not loop or _pool_lock_loop.is_closed():
        _pool_lock = asyncio.Lock()
        _pool_lock_loop = loop
    return _pool_lock


def _raw_pool_loop(pool: Any) -> asyncio.AbstractEventLoop | None:
    raw_pool = getattr(pool, "_pool", pool)
    loop = getattr(raw_pool, "_loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop):
        return loop
    return None


def _current_pool_loop(pool: Any) -> asyncio.AbstractEventLoop | None:
    loop = getattr(pool, "loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop):
        return loop
    return _raw_pool_loop(pool)


def _pg_pool_server_settings_key() -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in PG_POOL_SERVER_SETTINGS.items()))


def is_resilient_pool_usable(pool: Any, loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Return whether an asyncpg pool can be safely used on *loop*."""
    if loop is None:
        loop = asyncio.get_running_loop()
    pool_loop = _current_pool_loop(pool)
    if pool_loop is None:
        return True
    return pool_loop is loop and not pool_loop.is_closed()


class ResilientAsyncpgPool:
    """Small wrapper adding one retry around stale asyncpg pooled handles."""

    def __init__(
        self,
        pool: Any,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        refresh: Any | None = None,
    ) -> None:
        self._pool = pool
        if loop is None:
            loop = _raw_pool_loop(pool)
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        self._loop = loop
        # Optional async ``() -> ResilientAsyncpgPool`` used to rebuild this pool
        # after a cross-loop/stale failure. A standalone (e.g. metrics-owned)
        # pool passes its own factory so recovery recreates ITSELF and never
        # closes/recreates the shared module-global pool bound to the serving
        # loop. When None (the shared pool), recovery uses the module globals.
        self._refresh = refresh

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def acquire(self, *args: Any, **kwargs: Any) -> _RetryingAcquire:
        return _RetryingAcquire(self._pool, *args, **kwargs)

    async def close(self) -> None:
        await self._pool.close()

    def terminate(self) -> None:
        terminate = getattr(self._pool, "terminate", None)
        if callable(terminate):
            terminate()

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_retry("execute", *args, **kwargs)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_retry("fetch", *args, **kwargs)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_retry("fetchrow", *args, **kwargs)

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call_with_retry("fetchval", *args, **kwargs)

    async def _call_with_retry(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if _connection_db_user_id() is not None:
            async with self.acquire() as conn:
                return await getattr(conn, method_name)(*args, **kwargs)

        # No ambient tenant context: this branch calls the raw pool directly,
        # bypassing _SafePgConnection, so apply the same tenant-write guard here.
        # Covers execute/executemany AND fetch* (RETURNING mutations / data-
        # modifying CTEs). A context-less mutation against an owner-RLS table
        # fails closed unless inside system_db_scope().
        if (
            args
            and _db_user_context_enabled()
            and not _db_system_scope.get(False)
            and not is_sql_set_app_user_id(args[0])
        ):
            is_mutation, _table = sql_mutation_target(args[0])
            if is_mutation:
                guarded = await _load_guarded_rls_tables(self._pool)
                _assert_tenant_write_permitted(args[0], context_set=False, guarded_tables=guarded)

        method = getattr(self._pool, method_name)
        try:
            return await method(*args, **kwargs)
        except Exception as exc:
            if _is_asyncpg_undefined_column_error(exc):
                return _safe_table_fallback(
                    method_name,
                    args[0] if args else None,
                    exc,
                    "viola.core.db_backend.pool",
                )
            if not _is_asyncpg_connection_error(exc):
                raise
            # Cross-loop / stale-pool failure (the ASYNC-1 family -
            # "pool is closed", "Event loop is closed", "got result for
            # unknown protocol state 3"). Retrying on the same pool object
            # cannot recover because the pool is bound to a dead loop.
            logger.warning(
                "asyncpg pool %s failed with %s; refreshing pool and retrying",
                method_name,
                exc.__class__.__name__,
            )
            fresh = await self._refresh_pool()
            try:
                return await getattr(fresh._pool, method_name)(*args, **kwargs)
            except Exception as retry_exc:
                if _is_asyncpg_undefined_column_error(retry_exc):
                    return _safe_table_fallback(
                        method_name,
                        args[0] if args else None,
                        retry_exc,
                        "viola.core.db_backend.pool",
                    )
                raise

    async def _refresh_pool(self) -> ResilientAsyncpgPool:
        # Standalone pools (metrics-owned) rebuild themselves; the shared pool
        # refreshes the module globals. Retrying on the same pool object cannot
        # recover a cross-loop / dead-loop failure because the pool is bound to a
        # dead loop.
        if self._refresh is not None:
            fresh = await self._refresh()
            return fresh if isinstance(fresh, ResilientAsyncpgPool) else self
        await close_pg_pool()
        return await get_pg_pool()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


async def create_resilient_pg_pool(
    dsn: str,
    *,
    refresh: Any | None = None,
) -> ResilientAsyncpgPool:
    import asyncpg as _asyncpg

    loop = asyncio.get_running_loop()
    pool = await _asyncpg.create_pool(
        dsn=dsn,
        min_size=PG_POOL_MIN_SIZE,
        max_size=PG_POOL_MAX_SIZE,
        max_inactive_connection_lifetime=PG_POOL_MAX_INACTIVE_CONNECTION_LIFETIME_SECONDS,
        command_timeout=PG_POOL_COMMAND_TIMEOUT_SECONDS,
        server_settings=dict(PG_POOL_SERVER_SETTINGS),
        init=_configure_asyncpg_connection,
        setup=_verify_asyncpg_connection,
    )
    return ResilientAsyncpgPool(pool, loop=loop, refresh=refresh)


def get_database_url() -> str | None:
    """Return VIOLA_DATABASE_URL if it points at Postgres and asyncpg is available.

    Returns None when the env var is unset or does not point at Postgres.
    If a Postgres URL leaks into a launch environment without asyncpg,
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


def is_postgres() -> bool:
    """Return True if the database backend is PostgreSQL."""
    return get_database_url() is not None


async def get_pg_pool() -> ResilientAsyncpgPool:
    """Return the shared asyncpg connection pool, creating it if needed.

    Raises RuntimeError if VIOLA_DATABASE_URL is not set or asyncpg is missing.
    """
    global _pool, _pool_server_settings_key, _pool_url

    url = get_database_url()
    if url is None:
        msg = "get_pg_pool() called but VIOLA_DATABASE_URL is not set or asyncpg unavailable"
        raise RuntimeError(msg)

    loop = asyncio.get_running_loop()
    settings_key = _pg_pool_server_settings_key()

    if (
        _pool is not None
        and _pool_url == url
        and _pool_server_settings_key == settings_key
        and is_resilient_pool_usable(_pool, loop)
    ):
        return _pool

    async with _get_pool_lock():
        # Double-check after acquiring lock
        if (
            _pool is not None
            and _pool_url == url
            and _pool_server_settings_key == settings_key
            and is_resilient_pool_usable(_pool, loop)
        ):
            return _pool
        if _pool is not None:
            pool_loop = _current_pool_loop(_pool)
            if _pool_url != url:
                reason = "different URL"
            elif _pool_server_settings_key != settings_key:
                reason = "different server settings"
            else:
                reason = "closed" if pool_loop is not None and pool_loop.is_closed() else "different"
            logger.warning(
                "Discarding shared asyncpg pool bound to %s; recreating",
                reason,
            )
            await _close_pool_from_any_loop(_pool)
            _pool = None
            _pool_url = None
            _pool_server_settings_key = None

        logger.info("Creating shared asyncpg pool for %s", _redact_url(url))
        pool = await create_resilient_pg_pool(url)
        _pool = pool
        _pool_url = url
        _pool_server_settings_key = settings_key
        return pool


def _terminate_pool_quietly(pool: ResilientAsyncpgPool) -> None:
    try:
        pool.terminate()
    except (AttributeError, RuntimeError, OSError) as exc:
        logger.debug(
            "Ignoring asyncpg pool terminate failure: %s",
            exc,
            exc_info=True,
        )


async def _close_pool_from_any_loop(pool: ResilientAsyncpgPool) -> None:
    # BOUNDED close (SERVING-LOOP-WEDGE GUARD, see PG_POOL_CLOSE_TIMEOUT_SECONDS).
    # Every branch is wrapped in asyncio.wait_for + a terminate() fallback so the
    # caller — which holds ``_get_pool_lock`` on the serving loop — can never park
    # on a close() that waits on a connection stranded on a dead/other loop.
    pool_loop = _current_pool_loop(pool)
    current_loop = asyncio.get_running_loop()
    if pool_loop is None or pool_loop is current_loop:
        try:
            await asyncio.wait_for(pool.close(), timeout=PG_POOL_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "asyncpg pool.close() exceeded %.1fs (connection likely stranded on a dead loop); "
                "terminating pool so the pool lock is released and the serving loop cannot wedge",
                PG_POOL_CLOSE_TIMEOUT_SECONDS,
            )
            _terminate_pool_quietly(pool)
        # close() may raise anything; terminate() must still release the pool lock.
        except Exception as exc:  # noqa: BLE001, RUF100
            logger.warning("asyncpg pool.close() failed (%s); terminating pool", exc.__class__.__name__)
            _terminate_pool_quietly(pool)
        return
    if not pool_loop.is_closed() and pool_loop.is_running():
        try:
            future = asyncio.run_coroutine_threadsafe(pool.close(), pool_loop)
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=PG_POOL_CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "cross-loop asyncpg pool.close() exceeded %.1fs; terminating pool so the pool lock is "
                "released and the serving loop cannot wedge",
                PG_POOL_CLOSE_TIMEOUT_SECONDS,
            )
            _terminate_pool_quietly(pool)
        # close() may raise anything; terminate() must still release the pool lock.
        except Exception as exc:  # noqa: BLE001, RUF100
            logger.warning("cross-loop asyncpg pool.close() failed (%s); terminating pool", exc.__class__.__name__)
            _terminate_pool_quietly(pool)
        return
    _terminate_pool_quietly(pool)


async def close_pg_pool() -> None:
    """Close the shared asyncpg pool. Safe to call even if no pool exists."""
    global _pool, _pool_server_settings_key, _pool_url
    async with _get_pool_lock():
        if _pool is not None:
            await _close_pool_from_any_loop(_pool)
            logger.info("Shared asyncpg pool closed")
            _pool = None
            _pool_url = None
            _pool_server_settings_key = None


def _redact_url(url: str) -> str:
    """Redact password from a database URL for logging."""
    # postgresql://user:password@host:port/db -> postgresql://user:***@host:port/db  # pragma: allowlist secret
    if "@" in url:
        scheme_user, rest = url.split("@", 1)
        if ":" in scheme_user:
            parts = scheme_user.rsplit(":", 1)
            return parts[0] + ":***@" + rest
    return "postgresql://***"


async def ping_pg_pool() -> int:
    """Run the cheap cloud DB liveness probe through the hardened pool path."""
    pool = await get_pg_pool()
    return int(await pool.fetchval("SELECT 1") or 0)


def sqlite_to_pg_params(sql: str, params: tuple[Any, ...] | list[Any]) -> tuple[str, list[Any]]:
    """Convert SQLite-style ? placeholders to asyncpg $1, $2, ... style.

    Also handles named :param style used in some stores by raising --
    those stores must be converted to positional params first.

    Returns (converted_sql, params_list).
    """
    result = []
    param_index = 0
    i = 0
    while i < len(sql):
        if sql[i] == "?":
            param_index += 1
            result.append("$%d" % param_index)
        else:
            result.append(sql[i])
        i += 1
    return "".join(result), list(params)
