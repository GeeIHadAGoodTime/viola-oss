"""Encrypted local vault for settings credentials."""

from __future__ import annotations

from pathlib import Path

from core.logging_config import get_logger
from utils.enhancements.secrets import SecureSettingsManager

logger = get_logger(__name__)

_APP_NAME = "viola-settings-credentials"
_CACHE_FILENAME = ".settings_credentials.enc"
_KEY_FILENAME = ".settings_credentials.master_key"


class SettingsCredentialVault:
    """Small encrypted vault for credential-bearing settings keys."""

    def __init__(self, vault_dir: Path | None = None) -> None:
        if vault_dir is None:
            from core.platform import get_data_dir

            vault_dir = get_data_dir()
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.vault_dir / _CACHE_FILENAME
        self._manager = SecureSettingsManager(
            app_name=_APP_NAME,
            fallback_key_file=self.vault_dir / _KEY_FILENAME,
        )
        self._manager.load_from_file(self.cache_path)

    @staticmethod
    def _secret_ref(user_id: str, key: str) -> str:
        normalized_user = user_id.strip()
        normalized_key = key.strip()
        if not normalized_user:
            raise ValueError("user_id is required for settings credential vault")
        if not normalized_key:
            raise ValueError("setting key is required for settings credential vault")
        return "%s:%s" % (normalized_user, normalized_key)

    def set_credential(self, user_id: str, key: str, value: str) -> None:
        """Store a credential value and persist the encrypted cache."""
        self._manager.set_secret(self._secret_ref(user_id, key), value)
        self._manager.save_to_file(self.cache_path)
        logger.info("Stored settings credential key=%s user_id=%s", key, user_id)

    def get_credential(self, user_id: str, key: str) -> str | None:
        """Read a credential value from the encrypted cache."""
        return self._manager.get_secret(self._secret_ref(user_id, key))

    def delete_credential(self, user_id: str, key: str) -> bool:
        """Remove a credential value from the encrypted cache."""
        deleted = self._manager.delete_secret(self._secret_ref(user_id, key))
        if deleted:
            self._manager.save_to_file(self.cache_path)
            logger.info("Deleted settings credential key=%s user_id=%s", key, user_id)
        return deleted
