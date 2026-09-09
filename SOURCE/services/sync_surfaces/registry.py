"""Single registry for Cloud Tier-2 sync surface contracts.

This module is intentionally static and boring: it gives tests and reviewers one
place to compare the migration table, route surface, RLS policy, and GDPR entry
for every ``sync_*`` table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncSurfaceContract:
    table: str
    surface: str
    storage_owner: str
    read_routes: tuple[str, ...]
    write_routes: tuple[str, ...]
    legacy_sibling: str | None = None
    validators: tuple[str, ...] = ()

    @property
    def rls_policy(self) -> str:
        return "%s_owner_rw" % self.table

    @property
    def gdpr_key(self) -> str:
        return "public.%s" % self.table


def _contract(
    table: str,
    surface: str,
    storage_owner: str,
    read_routes: tuple[str, ...],
    write_routes: tuple[str, ...],
    *,
    legacy_sibling: str | None = None,
    validators: tuple[str, ...] = (),
) -> SyncSurfaceContract:
    return SyncSurfaceContract(
        table=table,
        surface=surface,
        storage_owner=storage_owner,
        read_routes=read_routes,
        write_routes=write_routes,
        legacy_sibling=legacy_sibling,
        validators=validators,
    )


CENTRAL_SURFACE_VALIDATORS = (
    "services.sync_surfaces.base.asyncpg_input_mapper",
    "services.sync_surfaces.base.SurfaceDefinition",
    "services.sync.consent.has_cloud_sync_consent",
)

SYNC_TABLE_CONTRACTS: tuple[SyncSurfaceContract, ...] = (
    _contract(
        "sync_user_profiles",
        "user_profiles",
        "services.sync_surfaces.user_profiles",
        ("/v1/sync/user-profile",),
        ("/v1/sync/user-profile",),
        legacy_sibling="public.user_profiles",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_user_models",
        "user_models",
        "services.sync_surfaces.user_models",
        ("/v1/sync/user-model",),
        ("/v1/sync/user-model",),
        legacy_sibling="public.user_models",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_user_preferences",
        "user_preferences",
        "services.sync_surfaces.user_preferences",
        (
            "/v1/sync/user-preferences",
            "/api/v1/cloud/settings",
            "/api/v1/cloud/settings/{key}",
        ),
        ("/v1/sync/user-preferences/{key}", "/api/v1/cloud/settings/{key}"),
        legacy_sibling="public.user_preferences, public.user_settings, public.sync_user_settings",
        validators=(
            *CENTRAL_SURFACE_VALIDATORS,
            "consent_cloud_sync bypass only",
            "backend.cloud_app cloud settings consent/classifier/type/size validators",
        ),
    ),
    _contract(
        "sync_channel_conversations",
        "channel_conversations",
        "services.persistence.cloud_channel_store",
        ("/v1/channels/{channel}/conversations",),
        ("/v1/channels/{channel}/conversations",),
        legacy_sibling="public.telegram_conversations, public.discord_conversations, public.matrix_conversations",
        validators=("pydantic route model", "CloudChannelStore._prepare_scoped_conn"),
    ),
    _contract(
        "sync_metadata",
        "metadata",
        "services.sync_surfaces.metadata",
        ("/v1/sync/metadata", "/v1/sync/pull"),
        ("/v1/sync/metadata/{key}", "/v1/sync/push"),
        legacy_sibling="public.metadata",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_queue_items",
        "queue_items",
        "services.sync_surfaces.queue_items",
        ("/v1/sync/queue",),
        ("/v1/sync/queue",),
        legacy_sibling="public.queue_items",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_token_metadata",
        "token_metadata",
        "services.sync_surfaces.token_metadata",
        ("/v1/sync/token-metadata",),
        ("/v1/sync/token-metadata/{provider_id}",),
        legacy_sibling="public.token_metadata",
        validators=(
            *CENTRAL_SURFACE_VALIDATORS,
            "reject_tier3_sync_payload",
            "sanitize_token_metadata",
        ),
    ),
    _contract(
        "sync_chat_threads",
        "chat_threads",
        "services.persistence.cloud_chat_store",
        ("/v1/chat/threads",),
        ("/v1/chat/threads", "/v1/chat/threads/{thread_id}"),
        legacy_sibling="public.web_conversations",
        validators=("pydantic route model", "CloudChatStore._prepare_scoped_conn"),
    ),
    _contract(
        "sync_chat_messages",
        "chat_messages",
        "services.persistence.cloud_chat_store",
        ("/v1/chat/threads/{thread_id}/messages",),
        ("/v1/chat/threads/{thread_id}/messages", "/v1/chat/messages/{message_id}"),
        validators=("pydantic route model", "CloudChatStore._prepare_scoped_conn"),
    ),
    _contract(
        "sync_conversation_log",
        "conversation_log",
        "services.persistence.cloud_chat_store",
        ("/v1/conversation/history", "/api/v1/cloud/conversations"),
        ("/v1/conversation/history",),
        legacy_sibling="public.web_conversations",
        validators=("pydantic route model", "CloudChatStore._prepare_scoped_conn"),
    ),
    _contract(
        "sync_memories",
        "memories",
        "services.memory.cloud_store",
        ("/v1/memories", "/v1/memories/{memory_id}"),
        ("/v1/memories", "/v1/memories/{memory_id}", "/v1/memories/{memory_id}/verify"),
        validators=("pydantic route model", "cloud memory consent/RLS wrapper"),
    ),
    _contract(
        "sync_memory_index",
        "memory_index",
        "services.memory.cloud_store",
        ("/v1/memories/index",),
        ("/v1/memories/index/{topic_key}",),
        validators=("pydantic route model", "cloud memory consent/RLS wrapper"),
    ),
    _contract(
        "sync_memory_quarantine",
        "memory_quarantine",
        "services.memory.cloud_store",
        ("/v1/memories/quarantine",),
        ("/v1/memories/quarantine", "/v1/memories/quarantine/{quarantine_id}/restore"),
        validators=("pydantic route model", "cloud memory consent/RLS wrapper"),
    ),
    _contract(
        "sync_user_capabilities",
        "user_capabilities",
        "services.sync_surfaces.user_capabilities",
        ("/v1/sync/user-capabilities",),
        ("/v1/sync/user-capabilities/{capability_id}",),
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_connector_profiles",
        "connector_profiles",
        "services.connectors.cloud_connector_store",
        ("/v1/connectors", "/v1/connectors/status"),
        ("/v1/connectors", "/v1/connectors/{profile_id}"),
        validators=(
            "pydantic route model",
            "Tier-3 forbidden field scrubber",
            "CloudConnectorStore._prepare_conn",
        ),
    ),
    _contract(
        "sync_connector_profile_selections",
        "connector_selections",
        "services.connectors.cloud_connector_store",
        ("/v1/connectors/selections",),
        ("/v1/connectors/selections/{category}",),
        validators=("pydantic route model", "CloudConnectorStore._prepare_conn"),
    ),
    _contract(
        "sync_playlists",
        "playlists",
        "music.playlists.cloud_playlist_store",
        ("/v1/playlists", "/v1/playlists/{playlist_id}"),
        ("/v1/playlists", "/v1/playlists/{playlist_id}"),
        legacy_sibling="public.playlists",
        validators=("music.playlists.cloud_sync_pg.require_cloud_sync_consent",),
    ),
    _contract(
        "sync_playlist_tracks",
        "playlist_tracks",
        "music.playlists.cloud_playlist_store",
        ("/v1/playlists/{playlist_id}/tracks",),
        (
            "/v1/playlists/{playlist_id}/tracks",
            "/v1/playlists/{playlist_id}/tracks/{track_id}",
        ),
        legacy_sibling="public.playlist_tracks",
        validators=("music.playlists.cloud_sync_pg.require_cloud_sync_consent",),
    ),
    _contract(
        "sync_playlist_user_settings",
        "playlist_user_settings",
        "music.playlists.playlist_store.PostgresPlaylistStore",
        ("playlist manager default-playlist reads",),
        ("playlist manager default-playlist writes",),
        legacy_sibling="public.playlist_user_settings",
        validators=("music.playlists.cloud_sync_pg.require_cloud_sync_consent",),
    ),
    _contract(
        "sync_liked_songs",
        "liked_songs",
        "music.playlists.cloud_likes_store",
        ("/v1/liked-songs",),
        ("/v1/liked-songs", "/v1/liked-songs/{liked_id}"),
        legacy_sibling="public.liked_songs",
        validators=("music.playlists.cloud_sync_pg.require_cloud_sync_consent",),
    ),
    _contract(
        "sync_song_ratings",
        "ratings",
        "music.cloud_rating_store",
        ("/v1/ratings", "/v1/ratings/track/{provider}/{track_uri}"),
        ("/v1/ratings/track/{provider}/{track_uri}", "/v1/track/rate"),
        legacy_sibling="public.song_ratings",
        validators=("music.playlists.cloud_sync_pg.require_cloud_sync_consent",),
    ),
    _contract(
        "sync_cloud_music_sessions",
        "cloud_music_sessions",
        "services.sync_surfaces.cloud_music_sessions",
        ("/v1/sync/music-sessions",),
        ("/v1/sync/music-sessions",),
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_cloud_files",
        "cloud_files",
        "services.sync_surfaces.cloud_files, services.cloud_storage.backends.postgres",
        ("/v1/sync/files", "cloud storage metadata reads"),
        ("/v1/sync/files", "cloud storage metadata writes"),
        legacy_sibling="public.cloud_files",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_cloud_call_records",
        "cloud_call_records",
        "services.sync_surfaces.cloud_call_records, services.cloud_storage.backends.postgres",
        ("/v1/sync/pull?surface=cloud_call_records", "cloud storage call-record reads"),
        ("/v1/sync/push", "cloud storage call-record writes"),
        legacy_sibling="public.cloud_call_records",
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_schedules",
        "schedules",
        "services.sync_surfaces.schedules",
        ("/v1/sync/schedules",),
        ("/v1/sync/schedules",),
        validators=(*CENTRAL_SURFACE_VALIDATORS, "prepare_schedule_payload"),
    ),
    _contract(
        "sync_proactive_tasks",
        "proactive_tasks",
        "services.sync_surfaces.proactive_tasks",
        ("/v1/sync/proactive-tasks",),
        ("/v1/sync/proactive-tasks",),
        validators=(*CENTRAL_SURFACE_VALIDATORS, "prepare_proactive_task_payload"),
    ),
    _contract(
        "sync_action_recipes",
        "action_recipes",
        "services.sync_surfaces.action_recipes",
        ("/v1/sync/action-recipes",),
        ("/v1/sync/action-recipes",),
        validators=CENTRAL_SURFACE_VALIDATORS,
    ),
    _contract(
        "sync_cursors",
        "cursors",
        "services.sync.cursors",
        ("/v1/sync/cursor",),
        ("/v1/sync/cursor/{surface}",),
        validators=("sync_bulk auth/consent/RLS wrapper",),
    ),
    _contract(
        "sync_dead_letters",
        "dead_letters",
        "ui.api.routes.sync_bulk",
        ("/v1/sync/dead-letters",),
        ("/v1/sync/push rejected mutation sink",),
        validators=("sync_bulk auth/consent/RLS wrapper",),
    ),
    _contract(
        "sync_change_journal",
        "change_journal",
        "services.sync.journal",
        ("/v1/sync/pull",),
        ("write_journal callers",),
        validators=(
            "write_journal idempotency key",
            "sync_bulk auth/consent/RLS wrapper",
        ),
    ),
)

SYNC_TABLE_CONTRACTS_BY_TABLE = {contract.table: contract for contract in SYNC_TABLE_CONTRACTS}

__all__ = [
    "CENTRAL_SURFACE_VALIDATORS",
    "SYNC_TABLE_CONTRACTS",
    "SYNC_TABLE_CONTRACTS_BY_TABLE",
    "SyncSurfaceContract",
]
