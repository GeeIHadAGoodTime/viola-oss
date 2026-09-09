"""Per-device, per-surface pull cursors.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
"""

from __future__ import annotations

import asyncpg

from services.sync.consent import current_consent_generation


async def get_cursor(conn: asyncpg.Connection, user_id: str, device_id: str, surface: str) -> int:
    """Return the last-pulled commit_seq for this user/device/surface (0 if none)."""
    value = await conn.fetchval(
        """
        SELECT cursor_commit_seq
        FROM sync_cursors
        WHERE user_id = $1::uuid
          AND device_id = $2
          AND surface = $3
        """,
        str(user_id),
        str(device_id),
        str(surface),
    )
    return int(value or 0)


async def set_cursor(
    conn: asyncpg.Connection,
    user_id: str,
    device_id: str,
    surface: str,
    commit_seq: int,
) -> None:
    """Persist the new cursor. Monotonic: never decreases."""
    await conn.execute(
        """
        INSERT INTO sync_cursors (
            user_id,
            device_id,
            surface,
            cursor_commit_seq,
            consent_generation,
            version_vector,
            last_pull_at,
            updated_at
        )
        VALUES ($1::uuid, $2, $3, $4, $5, '{}'::jsonb, now(), now())
        ON CONFLICT (user_id, device_id, surface) DO UPDATE
        SET cursor_commit_seq = EXCLUDED.cursor_commit_seq,
            consent_generation = EXCLUDED.consent_generation,
            last_pull_at = now(),
            updated_at = now()
        WHERE sync_cursors.cursor_commit_seq IS NULL
           OR EXCLUDED.cursor_commit_seq > sync_cursors.cursor_commit_seq
        """,
        str(user_id),
        str(device_id),
        str(surface),
        int(commit_seq),
        await current_consent_generation(conn, user_id),
    )
