"""HTTP-facing helpers for Tier-2 sync route modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

import asyncpg

from services.sync import set_rls_context

T = TypeVar("T")


async def run_with_sync_connection(
    user_id: str,
    operation: Callable[[asyncpg.Connection], Awaitable[T]],
) -> T:
    """Run a sync operation inside a user-scoped RLS transaction."""
    from core.db_backend import get_pg_pool

    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await set_rls_context(conn, str(user_id))
            return await operation(conn)
