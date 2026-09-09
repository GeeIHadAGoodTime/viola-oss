"""Cloud-sync consent gate.

Source of truth: ``sync_user_preferences`` row where ``key='consent_cloud_sync'``
and ``value_json`` parses to true AND ``deleted_at IS NULL``.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import asyncpg

from services.sync.middleware_helpers import set_rls_context

# Setting keys whose writes bypass the consent gate. Keep this to the literal
# opt-in flag; wider keys become a pre-consent cloud-write channel.
SETTINGS_CONSENT_OPTIONAL: frozenset[str] = frozenset({"consent_cloud_sync"})
_CONSENT_KEY = "consent_cloud_sync"
_SYNC_TABLE_NAME_RE = re.compile(r"^sync_[a-z0-9_]+$")
_WITHDRAWAL_DELETE_TABLES: frozenset[str] = frozenset(
    {
        "sync_change_journal",
        "sync_cursors",
        "sync_dead_letters",
    }
)


async def has_cloud_sync_consent(conn: asyncpg.Connection, user_id: str) -> bool:
    """Return True iff the user has set ``consent_cloud_sync = true``."""
    await set_rls_context(conn, user_id)
    value = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM sync_user_preferences
            WHERE user_id = $1::uuid
              AND key = 'consent_cloud_sync'
              AND deleted_at IS NULL
              AND value_json IN ('true'::jsonb, '"true"'::jsonb)
        )
        """,
        str(user_id),
    )
    return bool(value)


async def has_cloud_sync_consent_locked(conn: asyncpg.Connection, user_id: str) -> bool:
    """Return cloud-sync consent while locking the consent row for this transaction.

    Bulk Tier-2 writes use this so a concurrent consent revocation cannot commit
    between the consent check and the data mutation. If the revocation gets the
    row first, this returns false; if a data push gets it first, the revocation
    waits and the write is ordered before revocation.
    """
    await set_rls_context(conn, user_id)
    row = await conn.fetchrow(
        """
        SELECT value_json::text AS value_json, deleted_at
        FROM sync_user_preferences
        WHERE user_id = $1::uuid
          AND key = 'consent_cloud_sync'
        FOR UPDATE
        """,
        str(user_id),
    )
    if row is None or row["deleted_at"] is not None:
        return False
    return row["value_json"] in {"true", '"true"'}


async def current_consent_generation(conn: asyncpg.Connection, user_id: str) -> int:
    """Return the user's current consent_generation counter (the consent era).

    Read the era regardless of the consent row's ``deleted_at`` state (#2780).
    ``consent_generation`` is a monotonic era counter, not live preference data:
    it is bumped inside the withdrawal transaction (see
    ``tombstone_cloud_sync_data_after_withdrawal``), and a withdrawal can leave
    the consent row *soft-deleted* — an ``op=delete`` push on the
    ``consent_cloud_sync`` key soft-deletes the row rather than setting it to
    ``false``. If this read filtered ``deleted_at IS NULL`` it would return 0
    for a soft-deleted row, and the re-grant upsert — which stamps
    ``consent_generation`` from exactly this value (``EXCLUDED.consent_generation``,
    sourced from ``services.sync.engine.stamp``) — would reset the era back to 0,
    re-opening the replay hole the increment is meant to close. There is at most
    one row per ``(user_id, 'consent_cloud_sync')`` (composite PK + ON CONFLICT
    upsert), so dropping the ``deleted_at`` filter still returns that single
    row's true era.
    """
    await set_rls_context(conn, user_id)
    value = await conn.fetchval(
        """
        SELECT consent_generation
        FROM sync_user_preferences
        WHERE user_id = $1::uuid
          AND key = 'consent_cloud_sync'
        """,
        str(user_id),
    )
    return int(value or 0)


def _command_count(status: str) -> int:
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


def _safe_sync_table(table_name: str) -> str:
    if _SYNC_TABLE_NAME_RE.fullmatch(table_name) is None:
        raise ValueError("Unsafe sync table name: %s" % table_name)
    return table_name


async def _sync_tables_with_columns(conn: asyncpg.Connection, required_columns: tuple[str, ...]) -> list[str]:
    rows = await conn.fetch(
        """
        SELECT c.relname AS table_name
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        WHERE c.relkind IN ('r', 'p')
          AND left(c.relname, 5) = 'sync_'
          AND pg_catalog.pg_table_is_visible(c.oid)
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND a.attname = ANY($1::text[])
        GROUP BY c.oid, c.relname
        HAVING count(DISTINCT a.attname) = $2
        ORDER BY c.relname
        """,
        list(required_columns),
        len(required_columns),
    )
    return [_safe_sync_table(str(row["table_name"])) for row in rows]


async def tombstone_cloud_sync_data_after_withdrawal(
    conn: asyncpg.Connection, user_id: str
) -> dict[str, dict[str, int]]:
    """Remove previously synced Tier-2 cloud state after consent withdrawal.

    The caller must invoke this in the same transaction that writes
    ``consent_cloud_sync=false`` or deletes that consent row. Data tables are
    tombstoned so old cloud rows cannot be returned by normal sync reads; sync
    control tables are hard-deleted because they can contain stale cursors,
    dead-letter payloads, or journal entries that would otherwise describe old
    cloud state.
    """

    await set_rls_context(conn, user_id)
    now = datetime.now(UTC)
    tombstoned: dict[str, int] = {}
    deleted: dict[str, int] = {}

    for table_name in await _sync_tables_with_columns(conn, ("user_id", "deleted_at", "commit_seq")):
        if table_name in _WITHDRAWAL_DELETE_TABLES:
            continue
        if table_name == "sync_user_preferences":
            status = await conn.execute(
                """
                UPDATE sync_user_preferences
                SET deleted_at = $2,
                    commit_seq = nextval('sync_commit_seq')
                WHERE user_id = $1::uuid
                  AND key <> $3
                  AND deleted_at IS NULL
                """,
                str(user_id),
                now,
                _CONSENT_KEY,
            )
        else:
            status = await conn.execute(
                "UPDATE %s SET deleted_at = $2, commit_seq = nextval('sync_commit_seq') "
                "WHERE user_id = $1::uuid AND deleted_at IS NULL" % table_name,  # nosec B608 - validated sync table
                str(user_id),
                now,
            )
        tombstoned[table_name] = _command_count(status)

    present_delete_tables = set(await _sync_tables_with_columns(conn, ("user_id",)))
    for table_name in sorted(_WITHDRAWAL_DELETE_TABLES.intersection(present_delete_tables)):
        status = await conn.execute(
            "DELETE FROM %s WHERE user_id = $1::uuid" % table_name,  # nosec B608 - fixed allowlist + validated table
            str(user_id),
        )
        deleted[table_name] = _command_count(status)

    # Advance the consent-generation era on the consent row itself, in the SAME
    # withdrawal transaction (#2780). ``consent_generation`` is stamped onto every
    # Tier-2 row/journal/cursor/dead-letter from this counter (via
    # ``services.sync.engine.stamp`` -> ``current_consent_generation``), but nothing
    # else ever moved it, so it sat permanently at 0 and the withdrawal-purge
    # guarantee was defeatable: after a withdraw -> re-grant, a queued
    # pre-withdrawal push (or a second device's still-local copy) passes the locked
    # consent check and re-materializes exactly the data the withdrawal purged.
    # Bumping here means every mutation authored under the pre-withdrawal era
    # carries an older generation than ``current_consent_generation`` returns
    # post-re-grant, so the push apply path rejects it as stale. This is the single
    # withdrawal chokepoint every path funnels through (services/cloud_settings.py,
    # ui/api/routes/cloud_sync_user.py, ui/api/routes/sync_bulk.py). The
    # ``key <> _CONSENT_KEY`` filter above deliberately excluded the consent row
    # from tombstoning; this UPDATE targets exactly that row, with NO ``deleted_at``
    # filter so an ``op=delete`` withdrawal (which soft-deletes the row) still gets
    # its era bumped. The commit_seq bump re-journals the consent row so a pull
    # surfaces the new era to clients.
    generation_status = await conn.execute(
        """
        UPDATE sync_user_preferences
        SET consent_generation = consent_generation + 1,
            commit_seq = nextval('sync_commit_seq')
        WHERE user_id = $1::uuid
          AND key = $2
        """,
        str(user_id),
        _CONSENT_KEY,
    )
    bumped = {_CONSENT_KEY: _command_count(generation_status)}

    return {
        "tombstoned": tombstoned,
        "deleted": deleted,
        "consent_generation_bumped": bumped,
    }


class ConsentRequiredError(Exception):
    """Raised by surface stores when has_cloud_sync_consent returns False.

    Route handlers catch this and return 403:
        {"ok": false, "error": {"code": "consent_required", "message": "..."}, "data": null}
    """

    def __init__(self, message: str = "Enable cloud sync in Settings to use this feature.") -> None:
        super().__init__(message)
        self.message = message


class ConsentGenerationStaleError(Exception):
    """Raised when a Tier-2 write is authored under a superseded consent era.

    After a consent withdrawal purges Tier-2 data and bumps ``consent_generation``
    (``tombstone_cloud_sync_data_after_withdrawal``), a write stamped with an older
    era is a replay of the purged data — even once consent is re-granted and the
    locked consent check passes again. Route handlers catch this and return 409:
        {"ok": false, "error": {"code": "consent_generation_stale", ...}, "data": null}

    This is the per-surface PUT-route analogue of the ``consent_generation_stale``
    rejection the ``/v1/sync/push`` apply path emits (#2780); it closes the second
    Tier-2 write path (#3517).
    """

    def __init__(self, authored: int, current: int) -> None:
        self.authored = authored
        self.current = current
        message = (
            "Write authored under consent generation %s is older than the current "
            "generation %s; consent was withdrawn and this data was purged. "
            "Re-sync from the current server state." % (authored, current)
        )
        super().__init__(message)
        self.message = message
