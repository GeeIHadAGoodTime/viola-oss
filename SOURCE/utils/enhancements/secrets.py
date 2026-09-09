"""
Encryption helper for sensitive settings.

This module provides a small helper (`SecureSettingsManager`) used by consent
vaults and other callers to store sensitive values encrypted at rest.

Design goals:
- Optional dependencies (cryptography, keyring) are loaded dynamically.
- Fail securely: never persist plaintext when encryption is unavailable.
- Avoid leaking sensitive context in logs (no keys/values in log output).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast

from core.logging_config import get_logger
from core.subprocess_utils import run_silent

logger = get_logger(__name__)

# Master-key provenance labels. Tracked per-manager so a decrypt failure can name
# WHICH key source was resolved (the #2341 split-brain: ciphertext written under
# the keychain key is undecryptable when a reader resolves the file-fallback key,
# and vice versa). Never silently guess — always say the source. Note the OS
# keyring is bypassed entirely when VIOLA_DISABLE_OS_KEYRING is set — that hatch
# is owned by ``_os_keyring_disabled`` / ``_load_keyring`` below (#757), so the
# resolver just consults ``_load_keyring`` and gets file-fallback deterministically.
_KEY_SOURCE_KEYCHAIN = "os-keychain"
_KEY_SOURCE_FILE = "file-fallback"
_KEY_SOURCE_GENERATED = "generated"
_KEY_SOURCE_NONE = "none"


def _key_fingerprint(key: str | None) -> str | None:
    """Non-sensitive fingerprint of a master key for logs (never the key itself)."""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class SecurityError(RuntimeError):
    """Raised when secure storage cannot be used safely."""


class _FernetCipher(Protocol):
    def __init__(self, key: bytes) -> None: ...

    @classmethod
    def generate_key(cls) -> bytes: ...

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, token: bytes) -> bytes: ...


class _Keyring(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


_KEYRING_DISABLED_TRUE = frozenset({"1", "true", "yes", "on"})


def _os_keyring_disabled() -> bool:
    """True when the OS keyring must be bypassed for a file-backed master key.

    On macOS the very first access to the login keychain by a freshly-signed (or
    unsigned) binary — ``keyring.get_password`` -> ``SecItemCopyMatching`` — BLOCKS
    on a SecurityAgent authorization dialog. In a headless pytest run nothing can
    answer that dialog, so a keychain-touching request that the desktop GoTrue proxy
    correctly offloads to a worker thread (the #340 event-loop offload) blocks in the
    native call FOREVER: the blocking-portal ``.result()`` on the main thread never
    returns and the whole test hangs. Because ``precommit-local-battery`` always runs
    that gate, every commit on the Mac dev box froze (#757).

    ``VIOLA_DISABLE_OS_KEYRING`` is the explicit, greppable escape hatch for headless
    and test contexts: it forces ``_get_or_create_master_key`` down its already-present
    file-fallback path (a per-install ``.master_key`` with 0600 perms) instead of the
    OS keyring. Production desktop installs never set it, so real users still get the
    OS keychain. The test suite sets it session-wide in ``tests/conftest.py``.

    Internal to this module — other keyring consumers call the public
    :func:`is_os_keyring_disabled` wrapper below instead of duplicating this check
    (the #2683 escape: ``services/memory/key_provider.py`` had its own unguarded
    ``import keyring`` that predated this hatch and was never wired to it; a second,
    independent env-var read would only recreate the same drift risk).
    """
    return os.environ.get("VIOLA_DISABLE_OS_KEYRING", "").strip().lower() in _KEYRING_DISABLED_TRUE


def is_os_keyring_disabled() -> bool:
    """Public accessor for the ``VIOLA_DISABLE_OS_KEYRING`` hatch (#757 / #2683 / #2715).

    Every direct OS-keyring consumer anywhere in the repo (outside this sanctioned
    lineage) must check this — or an equivalent already-hatch-aware helper — before
    calling ``keyring.get_password``/``set_password``, and skip straight to its own
    file/fail-closed fallback when it returns ``True``. One shared source of truth
    for the env-var name and its accepted truthy spellings, instead of each consumer
    reimplementing (and risking drifting from) the same check. Enforced by the
    ``check-keyring-hatch-bypass`` gate (``scripts/check_keyring_hatch_bypass.py``).
    """
    return _os_keyring_disabled()


def _load_keyring() -> _Keyring | None:
    if _os_keyring_disabled():
        return None
    if importlib.util.find_spec("keyring") is None:
        return None
    try:
        module = importlib.import_module("keyring")
    except Exception:
        return None
    if hasattr(module, "get_password") and hasattr(module, "set_password"):
        return cast(_Keyring, module)
    return None


def _load_fernet() -> tuple[type[_FernetCipher], type[Exception]] | None:
    if importlib.util.find_spec("cryptography.fernet") is None:
        return None
    try:
        module = importlib.import_module("cryptography.fernet")
    except Exception:
        return None

    fernet_obj = getattr(module, "Fernet", None)
    invalid_obj = getattr(module, "InvalidToken", None)
    if not isinstance(fernet_obj, type):
        return None
    if not hasattr(fernet_obj, "generate_key"):
        return None
    invalid_token: type[Exception] = Exception
    if isinstance(invalid_obj, type) and issubclass(invalid_obj, Exception):
        invalid_token = invalid_obj
    return cast(type[_FernetCipher], fernet_obj), invalid_token


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _try_set_restrictive_perms(path: Path) -> None:
    """Restrict file permissions to the current user only (0o600 equivalent).

    On Unix: ``os.chmod(path, 0o600)``
    On Windows: ``icacls`` to remove inheritance and grant only current user (M14 fix)
    """
    if os.name == "nt":
        try:
            username = os.environ.get("USERNAME", "")
            if username:
                run_silent(
                    [
                        "icacls",
                        str(path),
                        "/inheritance:r",
                        "/grant:r",
                        "%s:F" % username,
                    ],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
        except Exception:
            logger.debug("Failed to set Windows ACL on %s", path)
        return
    try:
        os.chmod(path, 0o600)
    except Exception:
        return


class SecureSettingsManager:
    """
    Encrypted settings manager with optional OS keyring integration.

    Values are stored in an in-memory cache (optionally persisted by callers).
    The encryption key is stored in keyring when available, otherwise in a local
    file with restricted permissions.
    """

    def __init__(self, app_name: str = "viola", fallback_key_file: Path | None = None) -> None:
        self.app_name = app_name
        if fallback_key_file is not None:
            self.fallback_key_file = fallback_key_file
        else:
            from core.platform import get_data_dir

            self.fallback_key_file = get_data_dir() / ".master_key"

        self._cipher: _FernetCipher | None = None
        self._encryption_enabled = False
        self._disabled_reason: str | None = None
        self._encrypted_cache: dict[str, str] = {}

        # Provenance of the master key the cipher was built from. Set by
        # ``_get_or_create_master_key`` and named in every decrypt-failure log so
        # a key-source split-brain can never fail silently.
        self._master_key_source: str = _KEY_SOURCE_NONE
        self._master_key_fingerprint: str | None = None

        # Encryption init reads the OS keyring (master_key), which on macOS
        # triggers a *blocking* SecurityAgent authorization dialog the first time
        # a newly-signed app touches the keychain. Doing it in __init__ hung the
        # whole app boot on the main thread (managers are constructed during
        # startup). Defer it to first actual encrypt/decrypt use so construction
        # never touches the keychain and boot is never blocked.
        self._init_lock = threading.Lock()
        self._init_done = False

    def _ensure_initialized(self) -> None:
        if self._init_done:
            return
        with self._init_lock:
            if self._init_done:
                return
            self._initialize_encryption()
            self._init_done = True

    @property
    def encryption_enabled(self) -> bool:
        self._ensure_initialized()
        return self._encryption_enabled

    def _initialize_encryption(self) -> None:
        loaded = _load_fernet()
        if loaded is None:
            self._disabled_reason = "cryptography unavailable"
            self._encryption_enabled = False
            logger.warning("Encryption disabled (missing optional dependency).")
            return

        fernet_cls, _ = loaded
        master_key = self._get_or_create_master_key(fernet_cls)
        if master_key is None:
            self._disabled_reason = "master key unavailable"
            self._encryption_enabled = False
            logger.warning("Encryption disabled (master key unavailable).")
            return

        try:
            self._cipher = fernet_cls(master_key.encode("utf-8"))
        except Exception as exc:
            self._cipher = None
            self._disabled_reason = str(exc) or "cipher init failed"
            self._encryption_enabled = False
            logger.exception("Encryption initialization failed: %s", exc)
            return

        self._encryption_enabled = True
        self._disabled_reason = None
        logger.info("Encryption initialized.")

    def _read_fallback_key(self) -> str | None:
        try:
            if self.fallback_key_file.exists():
                return self.fallback_key_file.read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            logger.warning("Failed to read fallback master key file: %s", exc)
        return None

    def _write_fallback_key(self, key: str) -> bool:
        """Persist ``key`` to the fallback file (the cross-process source of truth).

        The signed app can read the OS keychain headless, but an out-of-process
        reader cannot; mirroring the resolved key here is what lets every reader,
        signed or not, converge on the SAME key instead of forking.
        """
        try:
            _ensure_parent_dir(self.fallback_key_file)
            self.fallback_key_file.write_text(key, encoding="utf-8")
            _try_set_restrictive_perms(self.fallback_key_file)
            return True
        except OSError as exc:
            logger.warning("Failed to write fallback master key file: %s", exc)
            return False

    def _finalize_master_key(self, key: str | None, source: str) -> str | None:
        self._master_key_source = source if key is not None else _KEY_SOURCE_NONE
        self._master_key_fingerprint = _key_fingerprint(key)
        if key is not None:
            logger.info(
                "Master key resolved (app_name=%s, source=%s, fp=%s).",
                self.app_name,
                self._master_key_source,
                self._master_key_fingerprint,
            )
        return key

    def _get_or_create_master_key(self, fernet_cls: type[_FernetCipher]) -> str | None:
        """Resolve ONE master key with a single, deterministic source-of-truth order.

        The #2341 split-brain came from the old order generating a *new* key
        whenever its preferred store missed, so the OS keychain and the fallback
        file could hold two different keys and a reader would resolve whichever
        its environment allowed — decrypting ciphertext written under the other
        was impossible. This resolver never mints a second key when one already
        exists in the other store; it reconciles the two stores instead, mirrors
        the winner into the file so every reader converges, and logs LOUDLY when
        it heals a divergence.
        """
        # ``_load_keyring`` already returns None when VIOLA_DISABLE_OS_KEYRING is
        # set (the #757 headless/out-of-process hatch), so a disabled keyring
        # deterministically resolves the file-fallback key below without blocking.
        keyring = _load_keyring()
        file_key = self._read_fallback_key()

        keyring_key: str | None = None
        keyring_readable = False
        if keyring is not None:
            try:
                existing = keyring.get_password(self.app_name, "master_key")
                keyring_key = str(existing) if existing else None
                keyring_readable = True
            except Exception as exc:  # noqa: BLE001, RUF100
                # Keyring backends raise heterogeneous errors; an unreadable
                # keyring must fall back to the file, never crash resolution.
                # Keyring present but unreadable (e.g. macOS SecurityAgent blocked
                # an unsigned binary). Fall back to the file — NEVER generate a
                # second key, which is exactly what forked the two stores before.
                logger.warning("OS keyring unreadable; using file-fallback master key: %s", exc)

        # 1. Both stores hold a key.
        if keyring_key and file_key:
            if keyring_key == file_key:
                return self._finalize_master_key(keyring_key, _KEY_SOURCE_KEYCHAIN)
            # SPLIT-BRAIN: the stores disagree. Reconcile to the keychain key (the
            # OS-protected store and, empirically, the signed-app writer) and
            # re-mirror it into the file so out-of-process readers stop resolving
            # the stale one. This is the self-heal for an already-forked install.
            logger.error(
                "Master-key SPLIT-BRAIN detected for app_name=%s: keychain(fp=%s) != file(fp=%s). "
                "Reconciling to the keychain key and re-mirroring it to the fallback file so all "
                "readers converge (was #2341: forked keychain/file writers).",
                self.app_name,
                _key_fingerprint(keyring_key),
                _key_fingerprint(file_key),
            )
            self._write_fallback_key(keyring_key)
            return self._finalize_master_key(keyring_key, _KEY_SOURCE_KEYCHAIN)

        # 2. Only the keychain holds a key -> mirror it to the file so an
        #    out-of-process reader (which cannot read the keychain) resolves the
        #    SAME key rather than generating its own.
        if keyring_key and not file_key:
            self._write_fallback_key(keyring_key)
            return self._finalize_master_key(keyring_key, _KEY_SOURCE_KEYCHAIN)

        # 3. Only the file holds a key -> use it, and back-fill an empty writable
        #    keyring so the two stores stay in sync (still never a second key).
        if file_key and not keyring_key:
            if keyring is not None and keyring_readable:
                try:
                    keyring.set_password(self.app_name, "master_key", file_key)
                except Exception as exc:  # noqa: BLE001, RUF100 - best-effort backfill
                    logger.warning("Failed to mirror file master key into keyring: %s", exc)
            return self._finalize_master_key(file_key, _KEY_SOURCE_FILE)

        # 4. Neither store holds a key -> first-ever init. Generate ONE key and
        #    persist it to BOTH stores so no future reader can fork.
        generated = fernet_cls.generate_key()
        value = generated.decode("utf-8") if isinstance(generated, bytes) else str(generated)
        wrote_any = False
        if keyring is not None and keyring_readable:
            try:
                keyring.set_password(self.app_name, "master_key", value)
                wrote_any = True
            except Exception as exc:  # noqa: BLE001, RUF100 - best-effort keyring write
                logger.warning("Failed to store generated master key in keyring: %s", exc)
        if self._write_fallback_key(value):
            wrote_any = True
        if not wrote_any:
            logger.error(
                "Failed to persist a new master key to any store (app_name=%s); encryption unavailable.",
                self.app_name,
            )
            return self._finalize_master_key(None, _KEY_SOURCE_NONE)
        return self._finalize_master_key(value, _KEY_SOURCE_GENERATED)

    def set_secret(self, key: str, value: str) -> bool:
        """
        Store an encrypted value.

        Raises SecurityError when encryption is unavailable.
        """
        self._ensure_initialized()
        if not self._encryption_enabled or self._cipher is None:
            raise SecurityError(f"Encryption unavailable: {self._disabled_reason or 'disabled'}")

        try:
            encrypted = self._cipher.encrypt(value.encode("utf-8"))
        except Exception as exc:
            logger.exception("Encryption failed: %s", exc)
            raise SecurityError("Encryption failed; value not stored.") from exc

        self._encrypted_cache[f"encrypted_{key}"] = encrypted.decode("utf-8")
        logger.info("Encrypted value stored.")
        return True

    def get_secret(self, key: str) -> str | None:
        encrypted_key = f"encrypted_{key}"
        encrypted_value = self._encrypted_cache.get(encrypted_key)
        if encrypted_value is None:
            return None
        self._ensure_initialized()
        if not self._encryption_enabled or self._cipher is None:
            logger.warning(
                "Cannot decrypt value (encryption unavailable, app_name=%s, reason=%s).",
                self.app_name,
                self._disabled_reason or "disabled",
            )
            return None
        try:
            decrypted = self._cipher.decrypt(encrypted_value.encode("utf-8"))
        except Exception:  # noqa: BLE001, RUF100
            # Any decrypt error (InvalidToken, wrong key) must fail LOUD + closed.
            # LOUD by contract (#2341): a decrypt failure almost always means the
            # ciphertext was written under a DIFFERENT master key than the one we
            # resolved. Name the key source + fingerprint so a key-source
            # split-brain is diagnosable from the log instead of failing silently.
            logger.exception(
                "Secure decryption FAILED (app_name=%s, master_key_source=%s, master_key_fp=%s) — "
                "stored ciphertext was written under a DIFFERENT master key (key-source split-brain); "
                "returning None.",
                self.app_name,
                self._master_key_source,
                self._master_key_fingerprint,
            )
            return None
        return decrypted.decode("utf-8")

    def delete_secret(self, key: str) -> bool:
        """Remove an encrypted value from the local cache."""
        encrypted_key = f"encrypted_{key}"
        existed = encrypted_key in self._encrypted_cache
        self._encrypted_cache.pop(encrypted_key, None)
        if existed:
            logger.info("Encrypted value deleted.")
        return existed

    def migrate_from_plaintext(self, plaintext_settings: Mapping[str, str], keys: list[str]) -> list[str]:
        migrated: list[str] = []
        for item_key in keys:
            value = plaintext_settings.get(item_key)
            if not value:
                continue
            if self.set_secret(item_key, value):
                migrated.append(item_key)
        if migrated:
            logger.info("Migrated %s entries to encrypted storage.", len(migrated))
        return migrated

    def save_to_file(self, file_path: Path) -> None:
        try:
            _ensure_parent_dir(file_path)
            file_path.write_text(json.dumps(self._encrypted_cache, indent=2), encoding="utf-8")
            logger.info("Encrypted cache saved.")
        except Exception as exc:
            logger.warning("Failed to save encrypted cache: %s", exc)

    def load_from_file(self, file_path: Path) -> None:
        try:
            if not file_path.exists():
                return
            raw = file_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                self._encrypted_cache = {str(k): str(v) for k, v in parsed.items()}
                logger.info("Encrypted cache loaded.")
        except Exception as exc:
            logger.warning("Failed to load encrypted cache: %s", exc)


class _SettingsManagerSurface(Protocol):
    def get(self, key: str, default: object | None = None) -> object: ...

    def set(self, key: str, value: object) -> None: ...

    def get_all(self) -> object: ...


class _GetCallable(Protocol):
    def __call__(self, key: str, default: object = ...) -> object: ...


class _SetCallable(Protocol):
    def __call__(self, key: str, value: object) -> None: ...


class _PatchableSettingsManager(Protocol):
    _secure_manager: SecureSettingsManager
    _encryption_enhanced: bool
    get: _GetCallable
    set: _SetCallable


def enhance_with_encryption(settings_manager: object) -> object:
    """
    Attach SecureSettingsManager to a settings manager-like object.

    This is a best-effort helper for CLI/tools. It never persists plaintext if
    encryption is unavailable.
    """
    if hasattr(settings_manager, "_encryption_enhanced"):
        return settings_manager

    manager = SecureSettingsManager()

    surface = cast(_PatchableSettingsManager, settings_manager)
    surface._secure_manager = manager
    original_get = cast(_GetCallable | None, getattr(surface, "get", None))
    original_set = cast(_SetCallable | None, getattr(surface, "set", None))

    _default_sentinel = object()

    def secure_get(item_key: str, default: object = _default_sentinel) -> object:
        default_value: object = None if default is _default_sentinel else default
        if any(marker in item_key.lower() for marker in ("key", "token", "password", "secret")):
            value = manager.get_secret(item_key)
            if value is not None:
                return value
        if original_get is None:
            return default_value
        return original_get(item_key, default_value)

    def secure_set(item_key: str, value: object) -> None:
        if any(marker in item_key.lower() for marker in ("key", "token", "password", "secret")):
            manager.set_secret(item_key, str(value))
        if original_set is not None:
            original_set(item_key, value)

    if original_get is not None:
        surface.get = cast(_GetCallable, secure_get)
    if original_set is not None:
        surface.set = cast(_SetCallable, secure_set)

    surface._encryption_enhanced = True
    logger.info("Encryption wrapper enabled for settings manager.")
    return settings_manager
