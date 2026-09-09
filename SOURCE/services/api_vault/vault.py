"""API Credential Vault - OS-keyed, per-user encrypted API key storage.

The LLM never sees raw keys. Callers must provide a concrete authenticated
``user_id`` for every operation; there is no shared/default vault and no disk
keyfile fallback.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)

_VAULT_DIR = get_data_dir() / "api_vault"
_KEYFILE = _VAULT_DIR / ".keyfile"  # legacy name retained for tests/import compatibility; not used
_CREDENTIALS_FILE = _VAULT_DIR / "credentials.enc"  # legacy shared path; not used for new reads/writes
_SERVICE_NAME = "viola-api-vault"
_MASTER_KEY_ACCOUNT_PREFIX = "vault-master-key"
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class CredentialVaultError(RuntimeError):
    """Raised when credential vault storage cannot be used safely."""


def _import_keyring():
    try:
        # keyring-hatch-exempt: no disk fallback by design (module docstring above) --
        # fails closed rather than degrading; battery coverage is via
        # monkeypatch(_import_keyring), not the VIOLA_DISABLE_OS_KEYRING hatch.
        import keyring
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "API credential vault requires keyring>=24.0.0,<25.0.0. "
            "Install the desktop or cloud requirements before using API credentials."
        ) from exc
    return keyring


def _require_user_id(user_id: str | None) -> str:
    normalized = (user_id or "").strip()
    if not normalized:
        raise ValueError("user_id is required for API credential vault access")
    return normalized


def _safe_user_component(user_id: str) -> str:
    safe = _SAFE_COMPONENT_RE.sub("_", _require_user_id(user_id)).strip("._")
    if not safe:
        raise ValueError("user_id cannot be used as an API vault path")
    return safe


def _user_vault_dir(user_id: str) -> Path:
    return _VAULT_DIR / "users" / _safe_user_component(user_id)


def _credentials_file(user_id: str) -> Path:
    return _user_vault_dir(user_id) / "credentials.enc"


def _ensure_user_vault_dir(user_id: str) -> None:
    _user_vault_dir(user_id).mkdir(parents=True, exist_ok=True)


def _keyring_account(user_id: str) -> str:
    return "%s:%s" % (_MASTER_KEY_ACCOUNT_PREFIX, _safe_user_component(user_id))


def _get_or_create_key(user_id: str) -> bytes:
    """Get or create the per-user Fernet key from OS keyring."""
    from cryptography.fernet import Fernet

    normalized = _require_user_id(user_id)
    keyring = _import_keyring()
    account = _keyring_account(normalized)
    stored = keyring.get_password(_SERVICE_NAME, account)
    if stored:
        return stored.encode("utf-8")

    key = Fernet.generate_key()
    keyring.set_password(_SERVICE_NAME, account, key.decode("utf-8"))
    logger.info("Generated new API vault encryption key in OS keyring for user %s", normalized)
    return key


def _get_fernet(user_id: str):
    """Get a Fernet instance for one user's vault."""
    from cryptography.fernet import Fernet

    return Fernet(_get_or_create_key(user_id))


class CredentialVault:
    """Encrypted credential store for user-provided API keys."""

    def __init__(self) -> None:
        self._credentials_by_user: dict[str, dict[str, dict[str, Any]]] = {}

    def _load_for_user(self, user_id: str) -> dict[str, dict[str, Any]]:
        normalized = _require_user_id(user_id)
        if normalized in self._credentials_by_user:
            return self._credentials_by_user[normalized]

        path = _credentials_file(normalized)
        if not path.exists():
            self._credentials_by_user[normalized] = {}
            return self._credentials_by_user[normalized]
        try:
            decrypted = _get_fernet(normalized).decrypt(path.read_bytes())
            data = json.loads(decrypted.decode("utf-8"))
        except Exception as exc:
            logger.exception("Failed to load API credential vault for user %s", normalized)
            raise CredentialVaultError("API credential vault could not be decrypted") from exc
        if not isinstance(data, dict):
            raise CredentialVaultError("API credential vault has invalid structure")
        self._credentials_by_user[normalized] = {
            str(name): entry for name, entry in data.items() if isinstance(entry, dict)
        }
        logger.debug(
            "API vault loaded for user %s: %d credentials", normalized, len(self._credentials_by_user[normalized])
        )
        return self._credentials_by_user[normalized]

    def _save_for_user(self, user_id: str, credentials: dict[str, dict[str, Any]]) -> None:
        normalized = _require_user_id(user_id)
        try:
            _ensure_user_vault_dir(normalized)
            plaintext = json.dumps(credentials, indent=2, sort_keys=True).encode("utf-8")
            encrypted = _get_fernet(normalized).encrypt(plaintext)
            _credentials_file(normalized).write_bytes(encrypted)
            self._credentials_by_user[normalized] = credentials
            logger.debug("API vault saved for user %s: %d credentials", normalized, len(credentials))
        except Exception as exc:
            logger.exception("Failed to save API credential vault for user %s", normalized)
            raise CredentialVaultError("API credential vault could not be saved") from exc

    def store_credential(
        self,
        service_name: str,
        api_key: str,
        metadata: dict[str, Any] | None = None,
        *,
        user_id: str,
    ) -> None:
        """Store an encrypted credential for one user."""
        credentials = dict(self._load_for_user(user_id))
        credentials[service_name] = {
            "api_key": api_key,
            "metadata": metadata or {},
            "stored_at": datetime.now(UTC).isoformat(),
            "last_used": None,
        }
        self._save_for_user(user_id, credentials)
        logger.info("API credential stored for user %s service: %s", user_id, service_name)

    def get_credential(self, service_name: str, *, user_id: str) -> str | None:
        """Retrieve a decrypted API key for execution-time use only."""
        credentials = dict(self._load_for_user(user_id))
        entry = credentials.get(service_name)
        if entry is None:
            return None
        entry["last_used"] = datetime.now(UTC).isoformat()
        self._save_for_user(user_id, credentials)
        key = entry.get("api_key")
        return str(key) if key is not None else None

    def get_metadata(self, service_name: str, *, user_id: str) -> dict[str, Any]:
        """Get credential metadata without the key itself."""
        entry = self._load_for_user(user_id).get(service_name)
        if entry is None:
            return {}
        metadata = entry.get("metadata", {})
        return dict(metadata) if isinstance(metadata, dict) else {}

    def delete_credential(self, service_name: str, *, user_id: str) -> bool:
        """Remove one credential from one user's vault."""
        credentials = dict(self._load_for_user(user_id))
        if service_name not in credentials:
            return False
        del credentials[service_name]
        self._save_for_user(user_id, credentials)
        logger.info("API credential deleted for user %s service: %s", user_id, service_name)
        return True

    def list_services(self, *, user_id: str) -> list[dict[str, Any]]:
        """List stored services for one user (metadata only, never keys)."""
        result = []
        for name, entry in self._load_for_user(user_id).items():
            result.append(
                {
                    "service_name": name,
                    "stored_at": entry.get("stored_at"),
                    "last_used": entry.get("last_used"),
                    "metadata": entry.get("metadata", {}),
                }
            )
        return result

    def has_credential(self, service_name: str, *, user_id: str) -> bool:
        """Check if one user's vault contains a service credential."""
        return service_name in self._load_for_user(user_id)

    def delete_all_for_user(self, user_id: str) -> int:
        """Delete every API credential for one user."""
        normalized = _require_user_id(user_id)
        existing_count = len(self._load_for_user(normalized))
        path = _credentials_file(normalized)
        if path.exists():
            path.unlink()
        self._credentials_by_user[normalized] = {}
        return existing_count


def scrub_secrets(text: str, vault: CredentialVault | None = None, *, user_id: str | None = None) -> str:
    """Replace raw API keys with redaction markers."""
    if vault is None:
        vault = get_credential_vault()

    result = text
    credential_sets: list[dict[str, dict[str, Any]]] = []
    if user_id:
        credential_sets.append(vault._load_for_user(user_id))
    else:
        credential_sets.extend(vault._credentials_by_user.values())

    for credentials in credential_sets:
        for name, entry in credentials.items():
            key = str(entry.get("api_key", ""))
            if key and len(key) >= 8 and key in result:
                result = result.replace(key, "[REDACTED:%s]" % name)

    result = re.sub(
        r"(?:sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36,}|xoxb-[0-9-]+|AIza[a-zA-Z0-9_-]{35})",
        "[REDACTED:unknown_key]",
        result,
    )
    return result


_vault: CredentialVault | None = None


def get_credential_vault() -> CredentialVault:
    """Get or create the process-wide credential vault facade."""
    global _vault
    if _vault is None:
        _vault = CredentialVault()
    return _vault
