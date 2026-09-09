"""OS-keystore-backed memory-encryption secret (SEC-022 / SEC-028).

The per-install memory-encryption secret is a Tier-3 desktop secret. It lives
in the OS keystore (the same ``keyring`` pattern as ``services/api_vault``),
never in cleartext files and never in database rows sitting next to the data
it protects.

Legacy installs stored a per-install dev secret in the ``memory_metadata``
table (key ``encryption_dev_secret``) — cleartext, in the same SQLite file as
the encrypted rows. Those installs are migrated by the caller: the row value
is handed to :func:`get_or_create_keystore_secret`, copied into the keystore,
verified by read-back, and only after verification does the caller delete the
cleartext row. The keystore entry itself is the idempotency marker — once it
exists, migration never runs again.

The keystore is never reimplemented here: ``keyring`` delegates to Windows
Credential Manager / macOS Keychain / Secret Service.

File fallback (issue #341): the OS keystore is unavailable on some real
installs — a macOS Keychain that is locked or unreachable outside an
interactive session, a headless Linux box with no Secret Service / D-Bus
running, or any other keyring backend error. Before this fallback existed,
those installs hard-failed memory encryption entirely, even though the token
store (``utils/enhancements/secrets.py``) has shipped a file fallback
(``fallback_key_file``) for exactly this situation since day one. This module
mirrors that proven pattern: when the keystore is unavailable, the secret is
stored in a single file under the user's local data directory
(``core.platform.get_data_dir()``), permissioned to the current user only
(``chmod 0600`` on POSIX, an ``icacls`` ACL reset on Windows). That file is a
Tier-3 desktop-only secret (see ``memory/reference_local_only_storage_tier.md``)
— it is never uploaded to cloud storage, never synced, and is a plain separate
file, not a cleartext row sitting next to the encrypted data it protects.

Resolution order in :func:`get_or_create_keystore_secret`:
keystore -> existing file fallback -> generate + store in keystore ->
generate + store in file fallback. Only when *both* the keystore and the file
fallback are unavailable does the caller fail closed.

Headless-keychain hatch (issue #757 / #2683): on macOS the first login-keychain
access by a freshly-signed (or unsigned) binary — ``keyring.get_password`` ->
``SecItemCopyMatching`` — BLOCKS on a SecurityAgent authorization dialog that a
headless test run can never answer. #757 introduced ``VIOLA_DISABLE_OS_KEYRING``
as the greppable escape hatch and wired it into ``utils/enhancements/secrets.py``
(the desktop-token-store consumer that was deadlocking at the time), but this
module's own ``_import_keyring`` predates that hatch (it shipped the day before,
closing #341) and was never retrofitted to check it — the #757 fix was scoped to
the one deadlocking test, not a sweep of every direct keyring consumer, and its
own ratchet gate was explicitly deferred ("Ratchet mismatch (reported, not
papered over)", commit 3b62596). That gap let this module keep calling
``keyring.get_password``/``set_password`` unconditionally, reproducing the exact
#757 deadlock a third time (#2683) once a battery gate started exercising memory
encryption headlessly. ``_import_keyring`` now checks the shared
:func:`utils.enhancements.secrets.is_os_keyring_disabled` hatch — the same
source of truth #757 built, not a second copy of the env-var check — and returns
``None`` instead of importing/touching ``keyring`` at all when it is set,
falling straight to the file fallback above. Production desktop installs never
set the var, so real users still get the OS keychain; the test suite sets it
session-wide in ``tests/conftest.py``. The ``check-keyring-hatch-bypass`` gate
(``scripts/check_keyring_hatch_bypass.py``) enforces that this stays wired for
every direct keyring consumer under ``services/**``.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from core.logging_config import get_logger
from core.platform import get_data_dir
from core.subprocess_utils import run_silent
from utils.enhancements.secrets import is_os_keyring_disabled

logger = get_logger(__name__)

_SERVICE_NAME = "viola-memory-store"
_ACCOUNT = "memory-encryption-secret"
_FALLBACK_KEY_FILENAME = ".memory_key_fallback"


def _import_keyring():
    """Import keyring lazily so tests can stub it and non-desktop paths skip it.

    Returns ``None`` without importing (or touching) the ``keyring`` module at
    all when ``VIOLA_DISABLE_OS_KEYRING`` is set (#757 hatch, checked via the
    shared :func:`is_os_keyring_disabled` — see the module docstring for why
    this module needs its own explicit check, #2683). Callers treat ``None``
    exactly like an unavailable keystore and fall through to the file fallback.
    """
    if is_os_keyring_disabled():
        return None
    import keyring

    return keyring


def get_keystore_secret() -> str | None:
    """Read the memory-encryption secret from the OS keystore (no create)."""
    keyring = _import_keyring()
    if keyring is None:
        return None
    try:
        stored = keyring.get_password(_SERVICE_NAME, _ACCOUNT)
    except Exception:  # noqa: BLE001, RUF100 - keystore backends raise arbitrary errors; degraded path is None
        logger.warning("OS keystore unavailable while reading memory encryption secret", exc_info=True)
        return None
    return stored or None


def _store_and_verify(secret: str) -> bool:
    """Write the secret to the OS keystore and verify it reads back identically."""
    keyring = _import_keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(_SERVICE_NAME, _ACCOUNT, secret)
        return keyring.get_password(_SERVICE_NAME, _ACCOUNT) == secret
    except Exception:  # noqa: BLE001, RUF100 - keystore backends raise arbitrary errors; unverified store -> False
        logger.warning("OS keystore unavailable while storing memory encryption secret", exc_info=True)
        return False


def _default_fallback_key_file() -> Path:
    """Local, per-user file used only when the OS keystore is unavailable.

    This is an indirection point rather than a module-level constant so tests
    can monkeypatch it to redirect the fallback into a tmp dir instead of the
    real per-install data directory.
    """
    return get_data_dir() / _FALLBACK_KEY_FILENAME


def _try_set_restrictive_perms(path: Path) -> None:
    """Restrict the fallback key file to the current user only.

    Mirrors ``utils.enhancements.secrets._try_set_restrictive_perms``: POSIX
    ``chmod 0600``; Windows ``icacls`` ACL reset to the current user only.
    Best effort — a failure here degrades to default OS permissions and never
    blocks the write itself. The file already improves on the pre-fix
    cleartext-DB-row shape (a separate file vs. a row next to the encrypted
    data); OS-level ACL hardening is defense-in-depth on top of that.
    """
    if os.name == "nt":
        try:
            username = os.environ.get("USERNAME", "")
            if username:
                run_silent(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", "%s:F" % username],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )  # proc-tree-ok: single icacls binary, no shell, no grandchildren
        except Exception:  # noqa: BLE001, RUF100 - best-effort ACL hardening; a failure never blocks the write
            logger.debug("Failed to set Windows ACL on memory-key fallback file", exc_info=True)
        return
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001, RUF100 - best-effort perm hardening; a failure never blocks the write
        logger.debug("Failed to chmod memory-key fallback file", exc_info=True)


def _read_fallback_secret(fallback_key_file: Path) -> str | None:
    """Read (never create) the file-fallback secret."""
    try:
        if not fallback_key_file.exists():
            return None
        value = fallback_key_file.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001, RUF100 - filesystem errors degrade to None, same posture as the keystore path
        logger.warning("Failed to read memory-key file fallback", exc_info=True)
        return None
    if not value:
        logger.warning("Memory-key file fallback exists but is empty/unreadable")
        return None
    return value


def _write_fallback_secret(fallback_key_file: Path, secret: str) -> bool:
    """Write the secret to the file fallback and verify it reads back identically."""
    try:
        fallback_key_file.parent.mkdir(parents=True, exist_ok=True)
        fallback_key_file.write_text(secret, encoding="utf-8")
        _try_set_restrictive_perms(fallback_key_file)
        return fallback_key_file.read_text(encoding="utf-8").strip() == secret
    except Exception:  # noqa: BLE001, RUF100 - filesystem errors degrade to False, same posture as the keystore path
        logger.warning("Failed to write memory-key file fallback", exc_info=True)
        return False


def get_file_fallback_secret() -> str | None:
    """Read the memory-encryption secret from the local file fallback (no create).

    Companion to :func:`get_keystore_secret` for legacy/decrypt-only readers
    (``services/memory/dir.py``): rows may have been encrypted under this
    secret on an install where the OS keystore was unavailable at write time,
    so a decrypt-only reader must consider it too.
    """
    return _read_fallback_secret(_default_fallback_key_file())


def get_or_create_keystore_secret(legacy_secret: str | None = None) -> tuple[str, bool]:
    """Resolve the per-install memory-encryption secret.

    Resolution order:
    1. Existing OS keystore entry — authoritative once present.
    2. ``legacy_secret`` (the old cleartext ``encryption_dev_secret`` row) —
       migrated into the keystore, or into the local file fallback when the
       keystore is unavailable. Returns ``migrated=True`` ONLY after the copy
       is verified by read-back, so the caller may then delete the cleartext
       row without risk of orphaning the install.
    3. An existing file-fallback entry — a secret written by a prior run on
       this same install because the keystore was unavailable then too.
    4. A freshly generated secret, stored in the keystore, or in the file
       fallback when the keystore is unavailable.

    Returns ``("", False)`` ONLY when BOTH the keystore AND the file fallback
    are unavailable; the caller decides the fail-closed behavior at that
    point. The file fallback is a Tier-3 desktop-only local file (never
    cloud), permissioned to the current user only — see the module docstring.
    """
    existing = get_keystore_secret()
    if existing:
        return existing, False

    fallback_key_file = _default_fallback_key_file()

    if legacy_secret:
        if _store_and_verify(legacy_secret):
            logger.info("Migrated per-install memory encryption secret into the OS keystore")
            return legacy_secret, True
        if _write_fallback_secret(fallback_key_file, legacy_secret):
            logger.warning(
                "OS keystore unavailable; migrated per-install memory encryption secret into "
                "the local file fallback instead"
            )
            return legacy_secret, True
        return "", False

    existing_fallback = _read_fallback_secret(fallback_key_file)
    if existing_fallback:
        return existing_fallback, False

    fresh = secrets.token_urlsafe(48)
    if _store_and_verify(fresh):
        logger.info("Generated new per-install memory encryption secret in the OS keystore")
        return fresh, False
    if _write_fallback_secret(fallback_key_file, fresh):
        logger.warning(
            "OS keystore unavailable; generated new per-install memory encryption secret in "
            "the local file fallback instead"
        )
        return fresh, False
    return "", False
