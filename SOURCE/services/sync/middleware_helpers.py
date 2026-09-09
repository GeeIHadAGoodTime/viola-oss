"""Helpers every Tier-2 surface store calls to set up RLS context.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
"""

from __future__ import annotations

import asyncpg


async def set_rls_context(conn: asyncpg.Connection, user_id: str) -> None:
    """SELECT set_config('app.user_id', user_id, true) — transaction-local."""
    await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_id))
