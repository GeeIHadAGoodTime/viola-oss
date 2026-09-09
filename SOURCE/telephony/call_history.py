"""Call history — persistent storage for completed call records.

Stores metadata.json alongside audio files in ~/.viola/call_history/{call_id}/.

Security: Sensitive fields (phone_number, transcript) are encrypted at rest
using Fernet (AES-128-CBC with HMAC-SHA256). If the encryption key is not
configured, those fields are redacted before writing.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.logging_config import get_logger
from core.platform import get_data_dir
from telephony.call_disclosures import PHONE_CALL_RETENTION_DAYS

logger = get_logger(__name__)

_HISTORY_DIR = get_data_dir() / "call_history"
_LEGACY_HISTORY_DIR = Path.home().joinpath(".viola", "call_history")
_LEGACY_HISTORY_MIGRATION_DONE = False

# Fields containing PII that must be encrypted at rest
_SENSITIVE_FIELDS = ("phone_number", "transcript")


def _ensure_legacy_history_migrated() -> None:
    global _LEGACY_HISTORY_MIGRATION_DONE
    if _LEGACY_HISTORY_MIGRATION_DONE:
        return
    _LEGACY_HISTORY_MIGRATION_DONE = True
    if _HISTORY_DIR.exists() or not _LEGACY_HISTORY_DIR.exists():
        return
    try:
        shutil.copytree(_LEGACY_HISTORY_DIR, _HISTORY_DIR)
        logger.info(
            "Migrated legacy call history from %s to %s",
            _LEGACY_HISTORY_DIR,
            _HISTORY_DIR,
        )
    except OSError as exc:
        logger.warning(
            "Could not migrate legacy call history from %s to %s: %s",
            _LEGACY_HISTORY_DIR,
            _HISTORY_DIR,
            exc,
        )


def _get_fernet():
    """Return a Fernet instance for call history encryption, or None."""
    try:
        import os

        from cryptography.fernet import Fernet

        # Read directly from os.environ to mirror services/memory/store.py:150.
        # AppConfig does not declare these fields, so getattr always returned None
        # before — leaving every transcript redacted even when the env var was set.
        key = os.environ.get("VIOLA_MEMORY_ENCRYPTION_KEY") or os.environ.get("VIOLA_CALL_HISTORY_ENCRYPTION_KEY") or ""
        if not key:
            return None
        # Derive a 32-byte key via SHA-256, then base64-encode for Fernet
        import base64
        import hashlib

        key_bytes = hashlib.sha256(key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key_bytes))
    except Exception:
        return None


def _encrypt_sensitive(data: dict) -> dict:
    """Encrypt sensitive fields in-place. Redacts if encryption unavailable."""
    fernet = _get_fernet()
    result = dict(data)
    for field_name in _SENSITIVE_FIELDS:
        value = result.get(field_name)
        if value is None or value == "" or value == []:
            continue
        serialized = json.dumps(value) if not isinstance(value, str) else value
        if fernet:
            result[field_name] = "ENC:" + fernet.encrypt(serialized.encode()).decode()
        else:
            # Redact — never store plaintext PII
            if field_name == "phone_number" and isinstance(value, str) and len(value) >= 4:
                result[field_name] = "***" + value[-4:]
            else:
                result[field_name] = "[redacted]"
    return result


def _decrypt_sensitive(data: dict) -> dict:
    """Decrypt sensitive fields in-place. Returns as-is if not encrypted."""
    fernet = _get_fernet()
    result = dict(data)
    for field_name in _SENSITIVE_FIELDS:
        value = result.get(field_name)
        if not isinstance(value, str) or not value.startswith("ENC:"):
            continue
        if not fernet:
            result[field_name] = "[encrypted — key unavailable]"
            continue
        try:
            decrypted = fernet.decrypt(value[4:].encode()).decode()
            # Try to parse back as JSON (for list/dict fields like transcript)
            try:
                result[field_name] = json.loads(decrypted)
            except (json.JSONDecodeError, ValueError):
                result[field_name] = decrypted
        except Exception:
            result[field_name] = "[encrypted — decryption failed]"
    return result


@dataclass
class CallHistoryEntry:
    """Metadata for a completed call, persisted as JSON."""

    call_id: str = ""
    user_id: str = ""
    phone_number: str = ""
    task: str = ""
    caller_name: str = ""
    status: str = ""
    duration_seconds: float = 0.0
    # ISO timestamp for when the call was PLACED. ``started_at`` only exists
    # once the media stream connected, so a no-answer / busy / rejected / failed
    # call carries an empty ``started_at`` and this is the only placed-at time
    # it has (#3554). Empty on records written before this field existed; those
    # still carry ``ended_at``, which the call log falls back to.
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    transcript: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""
    outcome: str = ""
    recording_paths: dict[str, str] = field(default_factory=dict)
    # Whether call recording was ON for this call, and why it stopped if it did.
    # Without these, an empty ``recording_paths`` is ambiguous: it means EITHER the
    # user had recording off (consent respected) OR recording was on and the flush
    # silently failed (``_flush_call_recording`` only logs a warning). A consent
    # surface has to be auditable in both directions, so the toggle state is
    # persisted alongside the artifacts it explains (#2589 acceptance clause:
    # "recording artifact/metadata matches recording_enabled").
    recording_enabled: bool = False
    recording_stopped_reason: str = ""
    disclosure_spoken: bool = False
    cost_breakdown: dict = field(default_factory=dict)
    outcome_analysis: dict = field(default_factory=dict)
    attempt_number: int = 1
    previous_attempts: list[dict] = field(default_factory=list)


def get_call_dir(call_id: str) -> Path:
    """Get the directory for a specific call."""
    _ensure_legacy_history_migrated()
    return _HISTORY_DIR / call_id


def save_call_history(entry: CallHistoryEntry) -> Path:
    """Save call metadata as JSON alongside audio files.

    Sensitive fields (phone_number, transcript) are encrypted at rest.
    If no encryption key is configured, they are redacted.

    Returns:
        Path to the saved metadata.json.
    """
    call_dir = get_call_dir(entry.call_id)
    call_dir.mkdir(parents=True, exist_ok=True)
    meta_path = call_dir / "metadata.json"

    data = _encrypt_sensitive(asdict(entry))
    meta_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    logger.info("Saved call history: %s", meta_path)
    return meta_path


def _load_call_history_entry(call_dir: Path) -> CallHistoryEntry | None:
    meta_path = call_dir / "metadata.json"
    if not meta_path.exists():
        return None

    try:
        data = _decrypt_sensitive(json.loads(meta_path.read_text(encoding="utf-8")))
        return CallHistoryEntry(**{k: v for k, v in data.items() if k in CallHistoryEntry.__dataclass_fields__})
    except Exception as exc:
        logger.warning("Failed to load call history %s: %s", call_dir.name, exc)
        return None


def list_call_history(user_id: str | None, limit: int = 50, offset: int = 0) -> list[CallHistoryEntry]:
    """List saved call records for one user, newest first.

    Args:
        user_id: Authenticated owner. Empty/None returns no records.
        limit: Max records to return.
        offset: Number of records to skip.

    Returns:
        List of CallHistoryEntry objects owned by ``user_id``. Legacy entries
        with an empty user_id are excluded for privacy.
    """
    _ensure_legacy_history_migrated()
    if not user_id or not _HISTORY_DIR.exists():
        return []

    entries: list[CallHistoryEntry] = []
    dirs = sorted(_HISTORY_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

    for call_dir in dirs:
        if not call_dir.is_dir():
            continue
        meta_path = call_dir / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            raw_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to inspect call history %s: %s", call_dir.name, exc)
            continue
        if raw_data.get("user_id") != user_id:
            continue
        entry = _load_call_history_entry(call_dir)
        if entry is not None:
            entries.append(entry)

    return entries[offset : offset + limit]


def list_all_call_history(limit: int = 50, offset: int = 0) -> list[CallHistoryEntry]:
    """List all saved call records for dev/admin aggregate views only."""
    _ensure_legacy_history_migrated()
    if not _HISTORY_DIR.exists():
        return []

    entries: list[CallHistoryEntry] = []
    dirs = sorted(_HISTORY_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    for call_dir in dirs[offset : offset + limit]:
        if not call_dir.is_dir():
            continue
        entry = _load_call_history_entry(call_dir)
        if entry is not None:
            entries.append(entry)
    return entries


def get_call_history(call_id: str) -> CallHistoryEntry | None:
    """Load a specific call record.

    Returns:
        CallHistoryEntry or None if not found.
    """
    meta_path = get_call_dir(call_id) / "metadata.json"
    if not meta_path.exists():
        return None

    try:
        return _load_call_history_entry(get_call_dir(call_id))
    except Exception as exc:
        logger.warning("Failed to load call %s: %s", call_id, exc)
        return None


def delete_call_history(call_id: str) -> bool:
    """Delete a call's directory and all contents.

    Returns:
        True if deleted, False if not found.
    """
    import shutil

    call_dir = get_call_dir(call_id)
    if not call_dir.exists():
        return False

    try:
        shutil.rmtree(call_dir)
        logger.info("Deleted call history: %s", call_id)
        return True
    except Exception as exc:
        logger.warning("Failed to delete call %s: %s", call_id, exc)
        return False


def delete_call_history_for_user(user_id: str, call_ids: Iterable[str] | None = None) -> int:
    """Delete persisted call history for one user.

    Legacy call history entries did not include ``user_id``. For those rows,
    callers can pass call IDs from the phone billing table to complete the
    GDPR linkage.
    """
    _ensure_legacy_history_migrated()
    if not _HISTORY_DIR.exists():
        return 0

    target_call_ids = {str(call_id) for call_id in call_ids or ()}
    deleted = 0
    for call_dir in _HISTORY_DIR.iterdir():
        if not call_dir.is_dir():
            continue
        should_delete = call_dir.name in target_call_ids
        if not should_delete:
            meta_path = call_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                data = _decrypt_sensitive(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Failed to inspect call history %s: %s", call_dir.name, exc)
                continue
            should_delete = data.get("user_id") == user_id

        if should_delete and delete_call_history(call_dir.name):
            deleted += 1

    return deleted


def _call_history_entry_time(call_dir: Path) -> datetime:
    entry_time = datetime.fromtimestamp(call_dir.stat().st_mtime, tz=UTC)
    meta_path = call_dir / "metadata.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_time = data.get("ended_at") or data.get("started_at")
            if raw_time:
                parsed = datetime.fromisoformat(str(raw_time))
                entry_time = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (OSError, json.JSONDecodeError, ValueError):
            logger.debug(
                "Falling back to mtime for call history retention: %s",
                call_dir.name,
            )
    return entry_time


def expired_call_ids(days: int = PHONE_CALL_RETENTION_DAYS) -> list[str]:
    """Return call IDs older than the retention window."""
    _ensure_legacy_history_migrated()
    if not _HISTORY_DIR.exists():
        return []

    cutoff = datetime.now(UTC) - timedelta(days=days)
    expired: list[str] = []
    for call_dir in _HISTORY_DIR.iterdir():
        if call_dir.is_dir() and _call_history_entry_time(call_dir) < cutoff:
            expired.append(call_dir.name)
    return expired


def cleanup_old_call_history(days: int = PHONE_CALL_RETENTION_DAYS) -> int:
    """Delete call history directories older than the fixed retention window."""
    _ensure_legacy_history_migrated()
    if not _HISTORY_DIR.exists():
        return 0

    deleted = 0
    for call_id in expired_call_ids(days):
        if delete_call_history(call_id):
            deleted += 1

    if deleted:
        logger.info("Cleaned up %d call history records older than %d days", deleted, days)
    return deleted
