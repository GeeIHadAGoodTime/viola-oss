"""Encryption helpers for persisted conversation text."""

from __future__ import annotations

from services.conversation.state_manager import ConversationEncryptionUnavailableError, _get_conversation_encryption


def encrypt_conversation_text(value: str | None) -> str:
    """Encrypt required conversation text before it is written to persistence."""
    plaintext = str(value or "")
    try:
        return _get_conversation_encryption().encrypt(plaintext)
    except Exception as exc:
        raise ConversationEncryptionUnavailableError(
            "Conversation history was not stored because encryption is unavailable. "
            "Set VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup before storing conversation history."
        ) from exc


def decrypt_conversation_text(value: str | None) -> str | None:
    """Decrypt persisted conversation text, leaving legacy plaintext readable."""
    if value is None:
        return None
    ciphertext = str(value)
    return _get_conversation_encryption().decrypt(ciphertext)


__all__ = ["decrypt_conversation_text", "encrypt_conversation_text"]
