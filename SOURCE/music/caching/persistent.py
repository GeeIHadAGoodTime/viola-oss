"""
music/caching/persistent.py

Disk-persisted cache implementation.
Survives restarts but slower than memory cache.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from base64 import b64decode, b64encode
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)


class _FernetCipher(Protocol):
    @classmethod
    def generate_key(cls) -> bytes: ...

    def __init__(self, key: bytes) -> None: ...

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, token: bytes, ttl: int | None = None) -> bytes: ...


class _SecureSettingsManager(Protocol):
    def __init__(self, app_name: str = ..., fallback_key_file: Path | None = ...) -> None: ...

    def get_secret(self, name: str) -> str | None: ...

    def set_secret(self, name: str, value: str) -> bool: ...


_FernetImpl: type[_FernetCipher] | None = None
_InvalidTokenImpl: type[Exception] | None = None
_SecureSettingsManagerImpl: type[_SecureSettingsManager] | None = None

try:  # pragma: no cover - optional dependency
    from cryptography.fernet import Fernet, InvalidToken

    _FernetImpl = Fernet  # Fernet structurally matches _FernetCipher protocol
    _InvalidTokenImpl = InvalidToken
except ImportError:  # pragma: no cover - gracefully degrade
    logger.debug("cryptography not installed - persistent cache encryption disabled")

try:  # pragma: no cover - optional dependency
    from utils.enhancements.secrets import SecureSettingsManager

    _SecureSettingsManagerImpl = SecureSettingsManager  # matches _SecureSettingsManager protocol
except ImportError:  # pragma: no cover - gracefully degrade
    logger.debug("SecureSettingsManager unavailable - persistent cache encryption disabled")

_FERNET_FACTORY: type[_FernetCipher] | None = _FernetImpl
_INVALID_TOKEN_TYPE: type[Exception] | None = _InvalidTokenImpl
_SECURE_SETTINGS_MANAGER: type[_SecureSettingsManager] | None = _SecureSettingsManagerImpl

from .base import CacheEntry, ResolutionCache


class PersistentCache(ResolutionCache):
    """
    Thread-safe disk-persisted cache with optional encryption and lease tracking.

    Features:
    - Survives application restarts
    - Automatic background persistence
    - Lazy loading (loads on first access)
    - Configurable size and expiration
    - Provider-aware lease invalidation for compliance
    - At-rest encryption guarded by SecureSettingsManager
    """

    def __init__(
        self,
        cache_path: Path | None = None,
        max_size: int = 200,
        max_age_seconds: float = 6 * 3600,
    ):
        """
        Initialize persistent cache.

        Args:
            cache_path: Path to cache file (default: platform data dir / resolution_cache.json)
            max_size: Maximum number of entries
            max_age_seconds: Maximum age before expiration
        """
        self._cache_path = cache_path or (get_data_dir() / "resolution_cache.json")
        self._max_size = max_size
        self._max_age = max_age_seconds

        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._dirty = False
        self._hits = 0
        self._misses = 0
        self._cipher: _FernetCipher | None = None
        self._encryption_enabled = False
        self._secure_manager: _SecureSettingsManager | None = None
        self._provider_leases: dict[str, str] = {}

        # Ensure directory exists
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_encryption()

    def get(self, key: str) -> CacheEntry | None:
        """Get entry from cache, loading from disk if needed."""
        self._ensure_loaded()

        with self._lock:
            entry = self._cache.get(key)

            if entry is None:
                self._misses += 1
                return None

            if entry.provider:
                lease = self._provider_leases.get(entry.provider.lower())
                if lease is not None and lease != entry.lease_id:
                    logger.debug(
                        "Lease mismatch for provider=%s, key=%s. Invalidating cached entry.",
                        entry.provider,
                        key,
                    )
                    self._cache.pop(key, None)
                    self._dirty = True
                    self._misses += 1
                    return None

            # Check expiration
            if entry.is_expired(self._max_age):
                self._cache.pop(key, None)
                self._dirty = True
                self._misses += 1
                return None

            self._hits += 1
            return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        """Store entry in cache and mark for persistence."""
        self._ensure_loaded()

        with self._lock:
            if entry.provider and not entry.lease_id:
                lease = self._provider_leases.get(entry.provider.lower())
                if lease:
                    entry.lease_id = lease
            self._cache[key] = entry
            self._dirty = True

            # Evict oldest if over capacity
            if len(self._cache) > self._max_size:
                self._evict_oldest()

            # Trigger async save
            self._schedule_save()

    def clear(self) -> None:
        """Clear cache and delete file."""
        with self._lock:
            self._cache.clear()
            self._dirty = True
            try:
                if self._cache_path.exists():
                    self._cache_path.unlink()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to delete cache file: %s", exc)
            finally:
                self._loaded = False

    def size(self) -> int:
        """Get current cache size."""
        self._ensure_loaded()
        with self._lock:
            return len(self._cache)

    def invalidate(self, key: str) -> bool:
        """Invalidate specific entry."""
        self._ensure_loaded()
        with self._lock:
            if key in self._cache:
                self._cache.pop(key)
                self._dirty = True
                self._schedule_save()
                return True
            return False

    def invalidate_provider(self, provider: str) -> int:
        """
        Invalidate all entries for a provider (e.g., revoked consent).

        Returns:
            Number of entries removed.
        """
        self._ensure_loaded()
        with self._lock:
            normalized = provider.lower()
            removed = [
                key for key, entry in self._cache.items() if entry.provider and entry.provider.lower() == normalized
            ]
            for key in removed:
                self._cache.pop(key, None)
            if removed:
                self._dirty = True
                self._schedule_save()
            self._provider_leases.pop(normalized, None)
            return len(removed)

    def set_provider_lease(self, provider: str, lease_id: str) -> None:
        """
        Register/rotate the active lease token for provider.

        Cached entries tied to previous leases are purged automatically.
        """
        self._ensure_loaded()
        with self._lock:
            normalized = provider.lower()
            previous = self._provider_leases.get(normalized)
            self._provider_leases[normalized] = lease_id
            if previous and previous != lease_id:
                logger.info("Rotating lease for provider=%s", provider)
                keys_to_remove = [
                    key
                    for key, entry in self._cache.items()
                    if entry.provider and entry.provider.lower() == normalized and entry.lease_id != lease_id
                ]
                for key in keys_to_remove:
                    self._cache.pop(key, None)
                if keys_to_remove:
                    self._dirty = True
                    self._schedule_save()

    def get_provider_lease(self, provider: str) -> str | None:
        """Return current lease token for provider if known."""
        with self._lock:
            return self._provider_leases.get(provider.lower())

    def stats(self) -> dict[str, object]:
        """Get cache statistics."""
        self._ensure_loaded()
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0

            return {
                "type": "PersistentCache",
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "max_age_hours": self._max_age / 3600,
                "cache_path": str(self._cache_path),
                "loaded": self._loaded,
                "encryption": "enabled" if self._encryption_enabled else "disabled",
                "provider_leases": len(self._provider_leases),
            }

    def flush(self) -> None:
        """Force immediate write to disk."""
        with self._lock:
            if self._dirty:
                self._save_to_disk()

    # ------------------------------------------------------------------ #
    # Legacy compatibility helpers
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        """Explicitly load cache contents from disk (legacy API)."""
        self._ensure_loaded()

    def save(self) -> None:
        """Explicit flush wrapper used by integration tests."""
        with self._lock:
            self._save_to_disk()

    def set(self, key: str, value: object) -> None:
        """Legacy alias for `put` that accepts dict payloads."""
        entry = self._coerce_entry(value, key=key)
        self.put(key, entry)

    def _ensure_loaded(self) -> None:
        """Lazy load cache from disk on first access."""
        with self._lock:
            if self._loaded:
                return

            try:
                if self._cache_path.exists():
                    with open(self._cache_path) as handle:
                        raw_data = json.load(handle)

                    if not isinstance(raw_data, dict):
                        logger.warning("Persistent cache file corrupted: non-dict payload")
                        self._cache.clear()
                        self._loaded = True
                        return

                    payload = self._decode_payload(cast(dict[str, object], raw_data))
                    if payload is None:
                        logger.warning("Persistent cache payload invalid. Starting with empty cache.")
                        self._cache.clear()
                        self._loaded = True
                        return

                    loaded_count = 0
                    expired_count = 0

                    for key, entry_dict in payload.items():
                        try:
                            entry = CacheEntry.from_serializable(entry_dict)

                            # Skip expired entries
                            if entry.is_expired(self._max_age):
                                expired_count += 1
                                continue

                            self._cache[key] = entry
                            if entry.provider and entry.lease_id:
                                self._provider_leases.setdefault(entry.provider.lower(), entry.lease_id)
                            loaded_count += 1
                        except Exception as exc:  # pragma: no cover - defensive
                            logger.debug("Failed to load cache entry: %s", exc)

                    logger.info(
                        "Loaded %s cache entries from disk (%s expired)",
                        loaded_count,
                        expired_count,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load cache from disk: %s", exc)
            finally:
                self._loaded = True

    def _save_to_disk(self) -> None:
        """Write cache to disk (must be called with lock held)."""
        try:
            payload: dict[str, dict[str, object]] = {
                key: cast(dict[str, object], entry.to_serializable()) for key, entry in self._cache.items()
            }
            data = self._encode_payload(payload)

            temp_path = self._cache_path.with_suffix(".tmp")
            with open(temp_path, "w") as handle:
                json.dump(data, handle, indent=2)

            temp_path.replace(self._cache_path)
            self._dirty = False
            logger.debug("Saved %s cache entries to disk", len(payload))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to save cache to disk: %s", exc)

    def _schedule_save(self) -> None:
        """Schedule async save to disk."""
        if not self._dirty:
            return

        def delayed_save() -> None:
            import time

            time.sleep(1.0)  # Debounce writes
            with self._lock:
                if self._dirty:
                    self._save_to_disk()

        threading.Thread(target=delayed_save, daemon=True).start()

    def _evict_oldest(self) -> None:
        """Evict oldest entries to maintain size limit (must be called with lock held)."""
        if len(self._cache) <= self._max_size:
            return

        sorted_entries = sorted(
            self._cache.items(),
            key=lambda item: item[1].timestamp,
        )

        entries_to_remove = len(self._cache) - self._max_size
        for index in range(entries_to_remove):
            key = sorted_entries[index][0]
            self._cache.pop(key, None)

    def _coerce_entry(self, value: object, *, key: str | None = None) -> CacheEntry:
        """Convert dict-like payloads into CacheEntry instances."""
        if isinstance(value, CacheEntry):
            return value
        if isinstance(value, Mapping):
            payload: dict[str, object] = {str(k): v for k, v in value.items()}
            if not payload.get("url"):
                placeholder_key = key or payload.get("id") or str(uuid.uuid4())
                payload["url"] = f"legacy://{placeholder_key}"
            if "title" not in payload:
                payload["title"] = payload.get("name") or payload["url"]
            payload.setdefault("timestamp", time.time())
            entry = CacheEntry.from_serializable(payload)
            entry.raw_payload = dict(payload)
            return entry
        raise TypeError("PersistentCache.set() expects a CacheEntry or dict payload")

    def _initialize_encryption(self) -> None:
        """Initialize Fernet encryption using SecureSettingsManager."""
        if _FERNET_FACTORY is None or _SECURE_SETTINGS_MANAGER is None:
            return

        try:
            self._secure_manager = _SECURE_SETTINGS_MANAGER(app_name="viola.music")
            if self._secure_manager is not None:
                key = self._secure_manager.get_secret("music_cache_encryption_key")
                if not key:
                    generated = _FERNET_FACTORY.generate_key()
                    self._secure_manager.set_secret("music_cache_encryption_key", generated.decode())
                    key = generated.decode()
                    logger.info("Generated new encryption key for music cache")
                key_bytes = key.encode() if isinstance(key, str) else key
                self._cipher = _FERNET_FACTORY(key_bytes)
                self._encryption_enabled = True
                logger.info("Persistent music cache encryption enabled")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to initialize persistent cache encryption: %s", exc)
            self._cipher = None
            self._encryption_enabled = False

    def _encode_payload(self, payload: dict[str, dict[str, object]]) -> dict[str, object]:
        """Encode payload with optional encryption wrapper."""
        if not self._encryption_enabled or not self._cipher:
            return {"encrypted": False, "payload": payload}

        try:
            plaintext = json.dumps(payload).encode()
            token = self._cipher.encrypt(plaintext)
            encoded = b64encode(token).decode()
            return {"encrypted": True, "payload": encoded}
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to encrypt persistent cache payload: %s", exc)
            return {"encrypted": False, "payload": payload}

    def _decode_payload(self, data: dict[str, object]) -> dict[str, dict[str, object]] | None:
        """Decode payload handling both encrypted and legacy formats."""

        def _coerce_payload(
            raw: dict[str, object],
        ) -> dict[str, dict[str, object]] | None:
            coerced: dict[str, dict[str, object]] = {}
            for entry_key, entry_value in raw.items():
                if not isinstance(entry_value, dict):
                    logger.warning(
                        "Persistent cache payload malformed (expected dict entry for key=%s)",
                        entry_key,
                    )
                    return None
                coerced[entry_key] = cast(dict[str, object], entry_value)
            return coerced

        if "encrypted" not in data:
            return _coerce_payload(data)

        encrypted = data.get("encrypted")
        if encrypted is not True:
            payload = data.get("payload")
            if isinstance(payload, dict):
                return _coerce_payload(cast(dict[str, object], payload))
            logger.warning("Persistent cache payload malformed (expected dict)")
            return None

        if not self._cipher:
            logger.error("Cache payload encrypted but encryption unavailable - delete cache file to recover")
            return None

        encoded_payload = data.get("payload")
        if not isinstance(encoded_payload, str):
            logger.warning("Encrypted cache payload malformed")
            return None

        try:
            token = b64decode(encoded_payload.encode())
            plaintext = self._cipher.decrypt(token)
            decoded = json.loads(plaintext.decode())
            if isinstance(decoded, dict):
                return _coerce_payload(cast(dict[str, object], decoded))
            logger.warning("Decrypted cache payload malformed (expected dict)")
        except Exception as exc:
            if _INVALID_TOKEN_TYPE is not None and isinstance(exc, _INVALID_TOKEN_TYPE):
                logger.error("Invalid encryption material for persistent cache - key mismatch")
            else:
                logger.error("Failed to decrypt persistent cache payload: %s", exc)
        return None
