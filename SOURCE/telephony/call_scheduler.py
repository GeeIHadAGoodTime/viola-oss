"""Lightweight scheduler for delayed phone calls.

SEC-065: the on-disk store is a desktop-only (Tier-3) file. It must not be a
plaintext, lockless JSON blob — scheduled-call params can include the call task
and caller context, and concurrent writers (the scheduler tick + an agent
scheduling a new call) could clobber each other mid read-modify-write. This
module therefore:

- serializes every read-modify-write under a process lock AND an inter-process
  ``filelock`` so two Viola processes / the tick + the agent cannot race;
- writes atomically (temp file + ``os.replace``) so a crash never leaves a
  truncated store;
- encrypts the payload at rest with the canonical local memory encryption.

The store stays local-only (never synced to cloud) per the three-tier rule.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import InvalidToken
from filelock import FileLock

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

SCHEDULE_PATH = get_data_dir() / "scheduled_calls.enc"
_LEGACY_PLAINTEXT_PATH = get_data_dir() / "scheduled_calls.json"
_LOCK_PATH = get_data_dir() / "scheduled_calls.lock"

# Re-entrant in-process lock so nested helpers can hold it; the cross-process
# FileLock guards against a second Viola process / the scheduler tick.
_process_lock = threading.RLock()


def _get_encryptor():
    """Return the shared local memory encryptor, or None when unavailable.

    Mirrors services/persistence/conversation_crypto.py: the scheduler reuses
    the canonical local-data key chain (VIOLA_MEMORY_ENCRYPTION_KEY / local
    vault) rather than minting its own.
    """
    try:
        from services.memory.store import _get_memory_encryption

        enc = _get_memory_encryption()
        if enc is None or not getattr(enc, "encryption_available", False):
            return None
        return enc
    except (ImportError, OSError, RuntimeError, ValueError):
        logger.warning("Call scheduler encryption unavailable; refusing to persist scheduled calls in plaintext")
        return None


class CallSchedulerEncryptionUnavailableError(RuntimeError):
    """Raised when scheduled calls cannot be encrypted for persistence."""


class CallScheduler:
    def __init__(self):
        SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._file_lock = FileLock(str(_LOCK_PATH))

    @staticmethod
    def _require_user_id(user_id: str) -> str:
        resolved = str(user_id or "").strip()
        if not resolved:
            raise ValueError("CallScheduler requires user_id")
        return resolved

    async def schedule(self, call_params: dict, scheduled_at: datetime, *, user_id: str) -> str:
        uid = self._require_user_id(user_id)
        entry = {
            "id": secrets.token_hex(8),
            "user_id": uid,
            "params": call_params,
            "scheduled_at": scheduled_at.isoformat(),
            "status": "pending",
        }
        with _process_lock, self._file_lock:
            entries = self._load()
            entries.append(entry)
            self._save(entries)
        logger.info(
            "Call scheduled: %s at %s for user=%s",
            entry["id"],
            scheduled_at.isoformat(),
            uid,
        )
        return entry["id"]

    async def get_pending(self, *, user_id: str) -> list[dict]:
        uid = self._require_user_id(user_id)
        now = datetime.now(tz=UTC)
        with _process_lock, self._file_lock:
            entries = self._load()
        return [
            e
            for e in entries
            if e.get("user_id") == uid and e["status"] == "pending" and datetime.fromisoformat(e["scheduled_at"]) <= now
        ]

    async def get_pending_for_all_users(self) -> list[dict]:
        now = datetime.now(tz=UTC)
        with _process_lock, self._file_lock:
            entries = self._load()
        return [
            e
            for e in entries
            if e.get("user_id") and e["status"] == "pending" and datetime.fromisoformat(e["scheduled_at"]) <= now
        ]

    async def mark_completed(self, schedule_id: str, *, user_id: str) -> None:
        uid = self._require_user_id(user_id)
        with _process_lock, self._file_lock:
            entries = self._load()
            for entry in entries:
                if entry["id"] == schedule_id and entry.get("user_id") == uid:
                    entry["status"] = "completed"
                    break
            self._save(entries)

    async def list_scheduled(self, *, user_id: str) -> list[dict]:
        uid = self._require_user_id(user_id)
        with _process_lock, self._file_lock:
            entries = self._load()
        return [e for e in entries if e.get("user_id") == uid and e["status"] == "pending"]

    def _load(self) -> list[dict]:
        # Encrypted store takes precedence; fall back to a one-time read of the
        # legacy plaintext file so existing schedules are not lost on upgrade.
        if SCHEDULE_PATH.exists():
            return self._read_encrypted(SCHEDULE_PATH)
        if _LEGACY_PLAINTEXT_PATH.exists():
            entries = self._read_plaintext(_LEGACY_PLAINTEXT_PATH)
            if entries:
                # Re-persist encrypted and remove the plaintext copy.
                try:
                    self._save(entries)
                    _LEGACY_PLAINTEXT_PATH.unlink(missing_ok=True)
                    logger.info(
                        "Migrated %d scheduled calls from plaintext to encrypted store",
                        len(entries),
                    )
                except CallSchedulerEncryptionUnavailableError:
                    # Leave the plaintext file intact rather than lose data; the
                    # next save with encryption available completes migration.
                    logger.warning("Could not migrate scheduled calls to encrypted store; encryption unavailable")
            return entries
        return []

    def _read_encrypted(self, path: Path) -> list[dict]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        enc = _get_encryptor()
        if enc is None:
            logger.warning("Scheduled-call store is encrypted but no key is available; treating as empty")
            return []
        try:
            decrypted = enc.decrypt(raw)
            data = json.loads(decrypted)
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            logger.warning("Could not decrypt/parse scheduled-call store; treating as empty")
            return []
        return data if isinstance(data, list) else []

    def _read_plaintext(self, path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, entries: list[dict]) -> None:
        enc = _get_encryptor()
        if enc is None:
            raise CallSchedulerEncryptionUnavailableError(
                "Scheduled calls were not stored because local encryption is unavailable. "
                "Set VIOLA_MEMORY_ENCRYPTION_KEY or complete the local vault setup."
            )
        ciphertext = enc.encrypt(json.dumps(entries))
        self._atomic_write(SCHEDULE_PATH, ciphertext)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".scheduled_calls.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            # If os.replace succeeded the temp file is gone; otherwise remove the
            # partial temp so a crash never leaves a stray fragment.
            if tmp_path.exists():
                tmp_path.unlink()


_instance: CallScheduler | None = None


def get_call_scheduler() -> CallScheduler:
    global _instance
    if _instance is None:
        _instance = CallScheduler()
    return _instance
