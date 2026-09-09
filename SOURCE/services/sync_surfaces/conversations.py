"""Bulk sync definitions for Tier-2 conversation surfaces."""

from __future__ import annotations

from typing import Any

from services.persistence.conversation_crypto import decrypt_conversation_text, encrypt_conversation_text
from services.sync_surfaces.base import SurfaceDefinition

CHAT_THREADS = SurfaceDefinition(
    surface="chat_threads",
    table="sync_chat_threads",
    pk_columns=("user_id", "id"),
    data_columns=("title", "model", "archived", "created_at", "updated_at"),
    json_columns=("version_vector", "field_versions"),
)

CHAT_MESSAGES = SurfaceDefinition(
    surface="chat_messages",
    table="sync_chat_messages",
    pk_columns=("user_id", "id"),
    data_columns=("thread_id", "role", "content", "metadata_json", "parent_id", "status", "created_at", "updated_at"),
    json_columns=("metadata_json", "version_vector", "field_versions"),
)

CONVERSATION_LOG = SurfaceDefinition(
    surface="conversation_log",
    table="sync_conversation_log",
    pk_columns=("user_id", "id"),
    data_columns=("session_id", "role", "content", "metadata_json", "created_at"),
    json_columns=("metadata_json", "version_vector", "field_versions"),
)

CHANNEL_CONVERSATIONS = SurfaceDefinition(
    surface="channel_conversations",
    table="sync_channel_conversations",
    pk_columns=("user_id", "id"),
    data_columns=(
        "channel",
        "external_thread_id",
        "user_text",
        "assistant_text",
        "source",
        "created_at",
        "updated_at",
    ),
    json_columns=("version_vector", "field_versions"),
)


def _copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return dict(payload)


def prepare_chat_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = _copy_payload(payload)
    if "metadata" in clean and "metadata_json" not in clean:
        clean["metadata_json"] = clean.pop("metadata")
    if "content" in clean:
        clean["content"] = encrypt_conversation_text(clean.get("content"))
    return clean


def scrub_chat_message_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    clean = dict(row)
    if "content" in clean:
        clean["content"] = decrypt_conversation_text(clean.get("content"))
    return clean


def prepare_conversation_log_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = _copy_payload(payload)
    if "metadata" in clean and "metadata_json" not in clean:
        clean["metadata_json"] = clean.pop("metadata")
    if "content" in clean:
        clean["content"] = encrypt_conversation_text(clean.get("content"))
    return clean


def scrub_conversation_log_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    clean = dict(row)
    if "content" in clean:
        clean["content"] = decrypt_conversation_text(clean.get("content"))
    return clean


def prepare_channel_conversation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = _copy_payload(payload)
    if "user_text" in clean:
        clean["user_text"] = encrypt_conversation_text(clean.get("user_text"))
    if clean.get("assistant_text") is not None:
        clean["assistant_text"] = encrypt_conversation_text(clean.get("assistant_text"))
    return clean


def scrub_channel_conversation_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    clean = dict(row)
    if "user_text" in clean:
        clean["user_text"] = decrypt_conversation_text(clean.get("user_text"))
    if clean.get("assistant_text") is not None:
        clean["assistant_text"] = decrypt_conversation_text(clean.get("assistant_text"))
    return clean


__all__ = [
    "CHANNEL_CONVERSATIONS",
    "CHAT_MESSAGES",
    "CHAT_THREADS",
    "CONVERSATION_LOG",
    "prepare_channel_conversation_payload",
    "prepare_chat_message_payload",
    "prepare_conversation_log_payload",
    "scrub_channel_conversation_row",
    "scrub_chat_message_row",
    "scrub_conversation_log_row",
]
