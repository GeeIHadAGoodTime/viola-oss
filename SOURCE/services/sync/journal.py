"""Tier-2 change journal — single-surface and cross-surface pull feed.

S8 (codex-tier2-sync-engine) owns the bodies. Surface agents import + call.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import asyncpg

from services.sync.consent import current_consent_generation

_SURFACE_TABLES: dict[str, str] = {
    "action_recipes": "sync_action_recipes",
    "channel_conversations": "sync_channel_conversations",
    "chat_messages": "sync_chat_messages",
    "chat_threads": "sync_chat_threads",
    "cloud_call_records": "sync_cloud_call_records",
    "cloud_files": "sync_cloud_files",
    "cloud_music_sessions": "sync_cloud_music_sessions",
    "connector_profiles": "sync_connector_profiles",
    "connector_selections": "sync_connector_profile_selections",
    "conversation_log": "sync_conversation_log",
    "liked_songs": "sync_liked_songs",
    "memories": "sync_memories",
    "memory_index": "sync_memory_index",
    "memory_quarantine": "sync_memory_quarantine",
    "metadata": "sync_metadata",
    "playlist_tracks": "sync_playlist_tracks",
    "playlist_user_settings": "sync_playlist_user_settings",
    "playlists": "sync_playlists",
    "proactive_tasks": "sync_proactive_tasks",
    "queue_items": "sync_queue_items",
    "ratings": "sync_song_ratings",
    "schedules": "sync_schedules",
    "token_metadata": "sync_token_metadata",
    "user_capabilities": "sync_user_capabilities",
    "user_models": "sync_user_models",
    "user_preferences": "sync_user_preferences",
    "user_profiles": "sync_user_profiles",
}


def _jsonb(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _operation(op: Literal["upsert", "delete"]) -> str:
    return "delete" if op == "delete" else "patch"


def _table_name(surface: str) -> str:
    normalized = str(surface or "").strip()
    if not normalized:
        raise ValueError("surface must be non-empty")
    return _SURFACE_TABLES.get(normalized, "sync_%s" % normalized)


async def write_journal(
    conn: asyncpg.Connection,
    user_id: str,
    surface: str,
    row_pk: str,
    commit_seq: int,
    op: Literal["upsert", "delete"],
    mutation_id: str,
    *,
    changed_fields: Sequence[str] | None = None,
    version_vector: Mapping[str, Any] | None = None,
    field_versions: Mapping[str, Any] | None = None,
    updated_by_device_id: str | None = None,
    client_hlc: str | None = None,
) -> None:
    """Append a journal row. Idempotent per (user_id, surface, entity_id, idempotency_key) — matches migration 050."""
    row_key = str(row_pk)
    mutation_key = str(mutation_id or "").strip()
    if not mutation_key:
        raise ValueError("mutation_id must be non-empty")
    payload_hash = hashlib.sha256(("%s:%s:%s:%s" % (user_id, surface, row_key, commit_seq)).encode()).hexdigest()
    device_id = str(updated_by_device_id or "unknown")
    await conn.execute(
        """
        INSERT INTO sync_change_journal (
            commit_seq,
            user_id,
            device_id,
            surface,
            table_name,
            entity_id,
            operation,
            row_pk_json,
            changed_fields_json,
            version_vector,
            field_versions,
            consent_generation,
            idempotency_key,
            payload_hash,
            updated_by_device_id,
            client_hlc
        )
        VALUES (
            $1,
            $2::uuid,
            $11,
            $3,
            $4,
            $5,
            $6,
            $7::jsonb,
            $12::jsonb,
            $13::jsonb,
            $14::jsonb,
            $8,
            $9,
            $10,
            $11,
            $15
        )
        ON CONFLICT (user_id, surface, entity_id, idempotency_key) DO NOTHING
        """,
        int(commit_seq),
        str(user_id),
        str(surface),
        _table_name(surface),
        row_key,
        _operation(op),
        _jsonb({"id": row_key}),
        await current_consent_generation(conn, user_id),
        mutation_key,
        payload_hash,
        device_id,
        _jsonb(list(changed_fields or [])),
        _jsonb(dict(version_vector or {})),
        _jsonb(dict(field_versions or {})),
        str(client_hlc or "0:0:cloud"),
    )


async def journal_since(
    conn: asyncpg.Connection,
    user_id: str,
    since_seq: int,
    surface: str | None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return journal rows with commit_seq > since_seq, ordered ASC."""
    bounded_limit = max(1, min(int(limit), 500))
    if surface:
        rows = await conn.fetch(
            """
            SELECT
                commit_seq,
                user_id::text AS user_id,
                device_id,
                surface,
                table_name,
                entity_id,
                operation,
                row_pk_json::text AS row_pk_json,
                changed_fields_json::text AS changed_fields_json,
                version_vector::text AS version_vector,
                field_versions::text AS field_versions,
                consent_generation,
                idempotency_key,
                payload_hash,
                updated_by_device_id,
                client_hlc,
                created_at,
                dead_letter_id
            FROM sync_change_journal
            WHERE user_id = $1::uuid
              AND commit_seq > $2
              AND surface = $3
            ORDER BY commit_seq ASC
            LIMIT $4
            """,
            str(user_id),
            int(since_seq),
            str(surface),
            bounded_limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT
                commit_seq,
                user_id::text AS user_id,
                device_id,
                surface,
                table_name,
                entity_id,
                operation,
                row_pk_json::text AS row_pk_json,
                changed_fields_json::text AS changed_fields_json,
                version_vector::text AS version_vector,
                field_versions::text AS field_versions,
                consent_generation,
                idempotency_key,
                payload_hash,
                updated_by_device_id,
                client_hlc,
                created_at,
                dead_letter_id
            FROM sync_change_journal
            WHERE user_id = $1::uuid
              AND commit_seq > $2
            ORDER BY commit_seq ASC
            LIMIT $3
            """,
            str(user_id),
            int(since_seq),
            bounded_limit,
        )
    return [dict(row) for row in rows]
