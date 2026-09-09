"""Encrypted credential storage using Fernet symmetric encryption.

Provides at-rest encryption for service credentials (Tidal, iCloud, etc.)
that cannot use OAuth.  Credentials are encrypted with a dedicated key
stored in the OS keyring (preferred) or derived from ``settings.jwt_secret``
(legacy fallback) using PBKDF2-HMAC-SHA256 with 600,000 iterations.

Key hierarchy (M12 fix):
1. Dedicated key from OS keyring (``viola-credential-key``) — preferred
2. JWT secret fallback — backward compatible, logs migration suggestion

Storage location: ``{data_dir}/credentials.enc`` (JSON, per-service keys).
File permissions are restricted to owner-only on Unix.

Usage::

    from utils.secure_credentials import credential_store

    # Store an encrypted credential
    credential_store.set("tidal", "password", "my_secret_password", user_id="user-123")

    # Retrieve a decrypted credential
    password = credential_store.get("tidal", "password", user_id="user-123")

    # Check if a credential exists
    if credential_store.has("tidal", "password", user_id="user-123"):
        ...

Headless-keychain hatch (#757 / #2683 / #2715): both direct ``import keyring``
sites below (``_get_encryption_key`` and ``migrate_encryption_key``) are
guarded by :func:`utils.enhancements.secrets.is_os_keyring_disabled` before
they ever import or call into ``keyring``, so a headless/test run with
``VIOLA_DISABLE_OS_KEYRING`` set degrades straight to the existing JWT-secret
fallback (or, for migration, a clean no-op) instead of blocking on a macOS
SecurityAgent authorization dialog. See ``services/memory/key_provider.py``
for the sanctioned pattern this mirrors, and
``scripts/check_keyring_hatch_bypass.py`` for the ratchet gate that enforces
it repo-wide.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.subprocess_utils import run_silent
from utils.enhancements.secrets import is_os_keyring_disabled

logger = get_logger(__name__)

_KEYRING_SERVICE = "viola-credential-key"
_KEYRING_ACCOUNT = "encryption-key"
_DEFAULT_USER_SCOPE = "default"
_jwt_fallback_logged = False
_keyring_unavailable_logged = False

# Lazy import to avoid circular deps at module load
_Fernet: Any = None


def _keyring_errors() -> tuple[type[BaseException], ...]:
    """Return keyring backend exception types worth catching for a soft fallback.

    Covers ``keyring.errors.KeyringError`` (and subclasses like
    ``NoKeyringError`` raised by the ``fail`` backend on Linux when no Secret
    Service is installed), plus ``OSError`` / ``RuntimeError`` from the underlying
    ``secretstorage``/``jeepney`` D-Bus stack when the session bus or keyring
    daemon is missing/misbehaving, and ``ImportError`` if keyring itself is
    absent. ``RuntimeError`` is deliberately included to match the payment-vault
    protector's keyring catch-set (``linux_secret_service_protector._keyring_errors``):
    a D-Bus binding that raises ``RuntimeError`` on a degraded box must DEGRADE to
    the derived-key fallback (logged once, never silent), not crash the credential
    read. Anything outside this set still propagates so a genuine programming
    error is never swallowed.
    """
    types: list[type[BaseException]] = [OSError, RuntimeError, ImportError]
    try:
        from keyring.errors import KeyringError
    except ImportError:
        return tuple(types)
    types.append(KeyringError)
    return tuple(types)


def _get_fernet() -> Any:
    global _Fernet
    if _Fernet is None:
        from cryptography.fernet import Fernet

        _Fernet = Fernet
    return _Fernet


def _get_machine_id() -> bytes:
    """Return a stable machine identifier for salt uniqueness.

    Uses the platform node (MAC-based) which is stable across reboots.
    Falls back to empty bytes if unavailable — the fixed salt component
    still provides domain separation.
    """
    try:
        import uuid

        return str(uuid.getnode()).encode("utf-8")
    except Exception:
        return b""


def _derive_key(secret: str) -> bytes:
    """Derive Fernet key using PBKDF2 with installation-unique salt (M13 fix).

    The salt combines a fixed domain-separator with the machine ID so that
    identical secrets on different installations produce different keys.
    Falls back to legacy fixed salt for decryption of existing data.
    """
    from hashlib import pbkdf2_hmac

    # Installation-unique salt: fixed prefix + machine identifier
    machine_id = _get_machine_id()
    salt = b"viola-credential-store-v2:" + machine_id
    raw = pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        600_000,  # Match auth/kdf.py iteration count
    )
    return base64.urlsafe_b64encode(raw)


def _derive_key_v1(secret: str) -> bytes:
    """Derive Fernet key using original fixed salt (for backward compat decryption)."""
    from hashlib import pbkdf2_hmac

    raw = pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        b"viola-credential-store-v1",
        600_000,
    )
    return base64.urlsafe_b64encode(raw)


def _derive_key_legacy(secret: str) -> bytes:
    """Legacy key derivation (SHA-256 only). Used for migration."""
    raw = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw)


class SecureCredentialStore:
    """Thread-safe encrypted credential store backed by a JSON file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, dict[str, str]]] | None = None

    @staticmethod
    def _normalize_user_id(user_id: str | None) -> str:
        """Return the storage scope for a user_id."""
        normalized = (user_id or _DEFAULT_USER_SCOPE).strip()
        return normalized or _DEFAULT_USER_SCOPE

    @staticmethod
    def _is_legacy_service_scope(value: Any) -> bool:
        """Return True for legacy ``service -> key -> value`` payloads."""
        return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())

    @classmethod
    def _is_user_scoped_services(cls, value: Any) -> bool:
        """Return True for ``user_id -> service -> key -> value`` payloads."""
        return isinstance(value, dict) and all(
            isinstance(service, str) and cls._is_legacy_service_scope(credentials)
            for service, credentials in value.items()
        )

    @classmethod
    def _normalize_store_shape(cls, raw_store: Any) -> tuple[dict[str, dict[str, dict[str, str]]], bool]:
        """Normalize legacy store shapes into ``user_id/service/key`` form."""
        if not isinstance(raw_store, dict):
            return {}, False

        normalized: dict[str, dict[str, dict[str, str]]] = {}
        default_services: dict[str, dict[str, str]] = {}
        migrated = False

        for top_level_key, top_level_value in raw_store.items():
            if not isinstance(top_level_key, str):
                migrated = True
                continue

            if cls._is_legacy_service_scope(top_level_value):
                default_services[top_level_key] = dict(top_level_value)
                migrated = True
                continue

            if cls._is_user_scoped_services(top_level_value):
                normalized[top_level_key] = {
                    service: dict(credentials) for service, credentials in top_level_value.items()
                }
                continue

            migrated = True

        if default_services:
            normalized.setdefault(_DEFAULT_USER_SCOPE, {}).update(default_services)

        return normalized, migrated

    def _get_encryption_key(self) -> str | None:
        """Get the encryption key, preferring a dedicated keyring key.

        Key resolution order (M12 fix — decouple from JWT secret):
        1. Dedicated key from OS keyring (``viola-credential-key``)
        2. JWT secret fallback (backward compatible)

        When the JWT fallback is used, a one-time INFO message suggests
        running ``migrate_encryption_key()`` to decouple.
        """
        global _jwt_fallback_logged

        # Try dedicated keyring key first -- skipped entirely (never imports or
        # touches `keyring`) when the headless hatch is set (#757/#2683/#2715),
        # so a test/battery run degrades straight to the JWT fallback below
        # instead of blocking on a macOS SecurityAgent keychain dialog.
        if not is_os_keyring_disabled():
            try:
                import keyring

                stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
                if stored:
                    return stored
            except _keyring_errors() as exc:
                # keyring unavailable — fall through to JWT secret. This is an
                # acceptable degraded path for these credentials (Tier-2/3 service
                # creds like Tidal/iCloud, NOT the payment vault), but it must not be
                # silent: on Linux this commonly means no Secret Service backend is
                # installed/running (keyring's ``fail`` backend), so log it once so
                # operators can install secretstorage+jeepney for stronger at-rest
                # protection.
                global _keyring_unavailable_logged
                if not _keyring_unavailable_logged:
                    _keyring_unavailable_logged = True
                    logger.info(
                        "OS keyring unavailable for credential store (%s: %s); "
                        "using derived key fallback — install an OS keyring backend "
                        "for stronger at-rest protection",
                        type(exc).__name__,
                        exc,
                    )

        # Fallback to JWT secret for backward compatibility
        from config.settings import settings

        secret = settings.jwt_secret
        if secret and not _jwt_fallback_logged:
            _jwt_fallback_logged = True
            logger.info(
                "Credential store using JWT secret as encryption key; "
                "run credential_store.migrate_encryption_key() to use a dedicated key"
            )
        return secret

    def _get_store_path(self) -> Path:
        """Get the credential store file path."""
        from config.settings import settings

        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "credentials.enc"

    def _load(self) -> dict[str, dict[str, dict[str, str]]]:
        """Load and decrypt the credential store."""
        if self._cache is not None:
            return self._cache

        path = self._get_store_path()
        if not path.exists():
            self._cache = {}
            return self._cache

        secret = self._get_encryption_key()
        if not secret:
            logger.warning("No encryption key configured; cannot decrypt credentials")
            self._cache = {}
            return self._cache

        try:
            Fernet = _get_fernet()
            encrypted_data = path.read_bytes()

            # Try current key (machine-unique salt, M13 fix) first
            try:
                f = Fernet(_derive_key(secret))
                decrypted = f.decrypt(encrypted_data)
                raw_store = json.loads(decrypted.decode("utf-8"))
                self._cache, migrated = self._normalize_store_shape(raw_store)
                if migrated:
                    logger.info("Migrating credential store to user-scoped key layout")
                    self._save()
            except Exception:
                # Try v1 fixed-salt PBKDF2 key (pre-M13)
                try:
                    f_v1 = Fernet(_derive_key_v1(secret))
                    decrypted = f_v1.decrypt(encrypted_data)
                    raw_store = json.loads(decrypted.decode("utf-8"))
                    self._cache, _migrated = self._normalize_store_shape(raw_store)
                    logger.info("Migrating credential store from fixed salt to machine-unique salt")
                    self._save()
                except Exception:
                    # Fall back to legacy SHA-256 key and re-encrypt (H14 migration)
                    try:
                        f_legacy = Fernet(_derive_key_legacy(secret))
                        decrypted = f_legacy.decrypt(encrypted_data)
                        raw_store = json.loads(decrypted.decode("utf-8"))
                        self._cache, _migrated = self._normalize_store_shape(raw_store)
                        logger.info("Migrating credential store from legacy SHA-256 to PBKDF2 key derivation")
                        self._save()
                    except Exception:
                        logger.exception("Failed to decrypt credential store with all key derivations; starting fresh")
                        self._cache = {}
        except Exception:
            logger.exception("Failed to read credential store; starting fresh")
            self._cache = {}

        return self._cache

    def _save(self) -> None:
        """Encrypt and save the credential store."""
        if self._cache is None:
            return

        secret = self._get_encryption_key()
        if not secret:
            logger.warning("No encryption key configured; cannot save credentials")
            return

        path = self._get_store_path()

        try:
            Fernet = _get_fernet()
            f = Fernet(_derive_key(secret))
            plaintext = json.dumps(self._cache).encode("utf-8")
            encrypted = f.encrypt(plaintext)
            path.write_bytes(encrypted)

            # Restrict file permissions
            if os.name == "nt":
                try:
                    username = os.environ.get("USERNAME", "")
                    if username:
                        run_silent(
                            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
                            capture_output=True,
                            timeout=10,
                        )
                except Exception:
                    logger.warning("Could not restrict file permissions on Windows: %s", path)
            else:
                os.chmod(path, 0o600)
        except Exception:
            logger.exception("Failed to save credential store")

    def get(self, service: str, key: str, user_id: str | None = None) -> str | None:
        """Get a decrypted credential value."""
        with self._lock:
            store = self._load()
            service_data = store.get(self._normalize_user_id(user_id), {}).get(service)
            if service_data is None:
                return None
            return service_data.get(key)

    def set(self, service: str, key: str, value: str, user_id: str | None = None) -> None:
        """Store an encrypted credential value."""
        with self._lock:
            secret = self._get_encryption_key()
            if not secret:
                logger.warning("No encryption key configured; cannot save credentials")
                return

            store = self._load()
            scoped_user_id = self._normalize_user_id(user_id)
            if scoped_user_id not in store:
                store[scoped_user_id] = {}
            if service not in store[scoped_user_id]:
                store[scoped_user_id][service] = {}
            store[scoped_user_id][service][key] = value
            self._save()
            logger.info("Credential stored: %s/%s/%s", scoped_user_id, service, key)

    def has(self, service: str, key: str, user_id: str | None = None) -> bool:
        """Check if a credential exists."""
        with self._lock:
            store = self._load()
            scoped_user_id = self._normalize_user_id(user_id)
            return key in store.get(scoped_user_id, {}).get(service, {})

    def delete(self, service: str, key: str | None = None, user_id: str | None = None) -> None:
        """Delete a credential (or all credentials for a service)."""
        with self._lock:
            store = self._load()
            scoped_user_id = self._normalize_user_id(user_id)
            user_store = store.get(scoped_user_id)
            if not user_store or service not in user_store:
                return
            if key is None:
                del user_store[service]
            elif key in user_store[service]:
                del user_store[service][key]
                if not user_store[service]:
                    del user_store[service]
            if not user_store:
                del store[scoped_user_id]
            self._save()

    def invalidate_cache(self) -> None:
        """Clear the in-memory cache (for testing)."""
        with self._lock:
            self._cache = None

    def migrate_encryption_key(self) -> bool:
        """Re-encrypt all credentials with a dedicated keyring key.

        Generates a new dedicated encryption key in the OS keyring and
        re-encrypts all stored credentials.  After migration, the JWT
        secret is no longer used for credential encryption.

        Returns True if migration succeeded, False otherwise.
        """
        with self._lock:
            if is_os_keyring_disabled():
                # Headless hatch set (#757/#2683/#2715) -- never import/touch
                # `keyring`; this migration only makes sense with a real OS
                # keychain, so skip it cleanly rather than block on the
                # macOS SecurityAgent dialog.
                logger.warning(
                    "OS keyring is disabled (VIOLA_DISABLE_OS_KEYRING); "
                    "cannot migrate credential encryption to a dedicated keyring key"
                )
                return False
            try:
                import secrets as _secrets

                import keyring
            except ImportError:
                logger.error("keyring package required for encryption key migration")
                return False

            # Load existing data with current key (may be JWT secret)
            store = self._load()
            if not store:
                logger.info("No credentials to migrate")

            # Generate and store a dedicated key
            new_secret = _secrets.token_urlsafe(48)
            try:
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, new_secret)
            except Exception:
                logger.exception("Failed to store dedicated key in OS keyring")
                return False

            # Re-encrypt with the new key (invalidate cache to force re-derive)
            self._cache = store  # keep data in memory
            self._save()  # _save calls _get_encryption_key which now finds the keyring key

            logger.info(
                "Migrated credential encryption to dedicated keyring key " "(%s credentials re-encrypted)",
                len(store),
            )
            return True


# Module-level singleton
credential_store = SecureCredentialStore()
