"""
Encrypted token vault used by the consent orchestrator.

The vault stores per-provider refresh tokens in an encrypted payload on disk
using the existing `SecureSettingsManager` helper (AES-128 via Fernet with an
OS keyring backed master key).  Non-sensitive metadata lives alongside the
secrets in a JSON manifest for quick inspection and UI rendering.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from core.json_types import JsonDict, to_json_value
from core.logging_config import get_logger
from music.consent.exceptions import TokenVaultError
from music.consent.models import ProviderLinkRecord, ProviderLinkState, TokenBundle
from utils.enhancements.secrets import SecureSettingsManager, SecurityError

logger = get_logger("viola.music.consent.vault")

try:
    from services.persistence.state_store import get_state_store as _get_state_store
except Exception:  # pragma: no cover - optional dependency
    get_state_store: Callable[[], object] | None = None
else:
    get_state_store = _get_state_store


class _MetadataStore(Protocol):
    def delete_token_metadata(self, user_id: str, provider_id: str) -> bool: ...

    def upsert_token_metadata(self, user_id: str, provider_id: str, payload: dict[str, object]) -> None: ...


class EncryptedTokenVault:
    """
    Persistent encrypted token vault.

    The vault supports multiple users. Secrets are stored with keys of the
    shape `{user_id}:{provider_id}:{kind}` to avoid key clashes with other
    SecureSettingsManager clients.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        manifest_path: Path | None = None,
        secrets_path: Path | None = None,
        secure_manager: SecureSettingsManager | None = None,
        user_id: str | None = None,
    ) -> None:
        self._manifest_path = manifest_path or self._default_manifest_path()
        self._secrets_path = secrets_path or self._manifest_path.with_suffix(".secrets.json")
        self._secure_manager = secure_manager or self._build_secure_manager()
        self._lock = threading.RLock()
        self._manifest: dict[str, dict[str, dict[str, object]]] = {}
        self._state_store: _MetadataStore | None = None
        if get_state_store is not None:
            try:
                self._state_store = cast(_MetadataStore, get_state_store())
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Vault persistence unavailable: %r", exc)
        self._load()

    # ------------------------------------------------------------------#
    # Public API
    # ------------------------------------------------------------------#
    def list_providers(self, *, user_id: str) -> Iterable[ProviderLinkRecord]:
        with self._lock:
            uid = self._require_user_id(user_id)
            provider_records = self._manifest.get(uid, {})
            for payload in provider_records.values():
                yield self._decode_record(uid, payload)

    def get_provider(self, provider_id: str, *, user_id: str) -> ProviderLinkRecord | None:
        with self._lock:
            uid = self._require_user_id(user_id)
            target = self._manifest.get(uid, {}).get(provider_id.lower())
            if not target:
                return None
            return self._decode_record(uid, target)

    def get_token(self, provider_id: str, *, user_id: str) -> TokenBundle | None:
        with self._lock:
            uid = self._require_user_id(user_id)
            provider = self._manifest.get(uid, {}).get(provider_id.lower())
            if not provider:
                return None
            refresh_key = provider.get("refresh_reference")
            if not isinstance(refresh_key, str) or not refresh_key:
                return None
            refresh_token = self._get_secret(refresh_key)
            if refresh_token is None:
                return None

            access_ref = provider.get("access_reference")
            access_key = access_ref if isinstance(access_ref, str) and access_ref else None
            access_token = self._get_secret(access_key) if access_key else None

            expires_at_value = provider.get("expires_at")
            expires_at = _parse_datetime(expires_at_value) if isinstance(expires_at_value, str) else None

            scopes_value = provider.get("scopes")
            scopes: tuple[str, ...] = ()
            if isinstance(scopes_value, list) and all(isinstance(scope, str) for scope in scopes_value):
                scopes = tuple(scopes_value)

            metadata_value = provider.get("metadata")
            metadata_json = to_json_value(metadata_value)
            metadata: JsonDict = metadata_json if isinstance(metadata_json, dict) else {}

            token_type_value = provider.get("token_type")
            token_type = token_type_value if isinstance(token_type_value, str) else "Bearer"
            return TokenBundle(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                scopes=scopes,
                metadata=metadata,
                token_type=token_type,
            )

    def store_token(
        self,
        provider_id: str,
        bundle: TokenBundle,
        *,
        user_id: str,
        capability_metadata: dict[str, str] | None = None,
    ) -> ProviderLinkRecord:
        uid = self._require_user_id(user_id)
        with self._lock:
            records = self._manifest.setdefault(uid, {})
            linked_at = datetime.now(UTC)
            refresh_key = _secret_key(uid, provider_id, "refresh")
            access_key = _secret_key(uid, provider_id, "access")

            self._set_secret(refresh_key, bundle.refresh_token)
            if bundle.access_token:
                self._set_secret(access_key, bundle.access_token)
            else:
                self._delete_secret(access_key)

            payload: dict[str, object] = {
                "provider_id": provider_id,
                "user_id": uid,
                "linked_at": linked_at.isoformat(),
                "updated_at": linked_at.isoformat(),
                "refresh_reference": refresh_key,
                "access_reference": access_key if bundle.access_token else None,
                "expires_at": (bundle.expires_at.isoformat() if bundle.expires_at else None),
                "scopes": list(bundle.scopes),
                "token_type": bundle.token_type,
                "status": ProviderLinkState.LINKED.value,
                "metadata": dict(bundle.metadata),
            }
            if capability_metadata:
                metadata_payload = payload.get("metadata")
                if isinstance(metadata_payload, dict):
                    metadata_payload.update(capability_metadata)

            records[provider_id.lower()] = payload
            self._persist()
            self._persist_token_metadata(uid, provider_id)
            return self._decode_record(uid, payload)

    def update_access_token(
        self,
        provider_id: str,
        bundle: TokenBundle,
        *,
        user_id: str,
    ) -> ProviderLinkRecord:
        uid = self._require_user_id(user_id)
        with self._lock:
            provider_key = provider_id.lower()
            provider = self._manifest.get(uid, {}).get(provider_key)
            if not provider:
                raise TokenVaultError(f"Provider '{provider_id}' not linked for user '{uid}'")

            access_key = _secret_key(uid, provider_id, "access")
            if bundle.access_token:
                self._set_secret(access_key, bundle.access_token)
                provider["access_reference"] = access_key
            else:
                self._delete_secret(access_key)
                provider["access_reference"] = None

            if bundle.expires_at:
                provider["expires_at"] = bundle.expires_at.isoformat()
            provider["updated_at"] = datetime.now(UTC).isoformat()
            if bundle.metadata:
                metadata_value = provider.get("metadata")
                metadata_payload = dict(metadata_value) if isinstance(metadata_value, dict) else {}
                metadata_payload.update(bundle.metadata)
                provider["metadata"] = metadata_payload
            self._persist()
            self._persist_token_metadata(uid, provider_id)
            return self._decode_record(uid, provider)

    def mark_status(
        self,
        provider_id: str,
        status: ProviderLinkState,
        *,
        user_id: str,
        error: str | None = None,
    ) -> None:
        uid = self._require_user_id(user_id)
        with self._lock:
            provider = self._manifest.get(uid, {}).get(provider_id.lower())
            if not provider:
                return
            provider["status"] = status.value
            provider["last_error"] = error
            provider["updated_at"] = datetime.now(UTC).isoformat()
            self._persist()
            self._persist_token_metadata(uid, provider_id)

    def remove_provider(self, provider_id: str, *, user_id: str) -> None:
        uid = self._require_user_id(user_id)
        with self._lock:
            provider = self._manifest.get(uid, {}).pop(provider_id.lower(), None)
            if not provider:
                return
            for key_name in ("refresh_reference", "access_reference"):
                ref = provider.get(key_name)
                if isinstance(ref, str) and ref:
                    self._delete_secret(ref)
            self._persist()
        self._persist_token_metadata(uid, provider_id)

    # ------------------------------------------------------------------#
    # Internal helpers
    # ------------------------------------------------------------------#
    def _load(self) -> None:
        with self._lock:
            if self._manifest_path.exists():
                try:
                    loaded = json.loads(self._manifest_path.read_text("utf-8"))
                    if isinstance(loaded, dict):
                        self._manifest = cast(dict[str, dict[str, dict[str, object]]], loaded)
                    else:
                        self._manifest = {}
                except Exception as exc:
                    logger.warning("Failed to read vault manifest: %s", exc)
                    self._manifest = {}
            else:
                self._manifest = {}

            if self._secure_manager:
                try:
                    self._secure_manager.load_from_file(self._secrets_path)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Failed to read vault store: %s", exc)

            # Ensure schema shape
            for uid, records in list(self._manifest.items()):
                if not isinstance(records, dict):
                    logger.warning("Invalid vault manifest for user %s; resetting entry", uid)
                    self._manifest[uid] = {}
        self._sync_all_token_metadata()

    def _persist(self) -> None:
        try:
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(
                self._manifest,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            self._manifest_path.write_text(content, encoding="utf-8")
            if self._secure_manager:
                self._secure_manager.save_to_file(self._secrets_path)
        except Exception as exc:
            raise TokenVaultError(f"Failed to persist vault: {exc}") from exc

    def _persist_token_metadata(self, user_id: str, provider_id: str) -> None:
        store = self._state_store
        if store is None:
            return
        provider_key = provider_id.lower()
        with self._lock:
            provider = self._manifest.get(user_id, {}).get(provider_key)
            if not provider:
                payload: dict[str, object] | None = None
            else:
                scopes_value = provider.get("scopes")
                scopes_payload: list[str] = []
                if isinstance(scopes_value, list) and all(isinstance(scope, str) for scope in scopes_value):
                    scopes_payload = list(scopes_value)

                metadata_value = provider.get("metadata")
                metadata_payload: dict[str, object] = dict(metadata_value) if isinstance(metadata_value, dict) else {}
                payload = {
                    "user_id": user_id,
                    "status": provider.get("status"),
                    "linked_at": provider.get("linked_at"),
                    "updated_at": provider.get("updated_at"),
                    "expires_at": provider.get("expires_at"),
                    "scopes": scopes_payload,
                    "metadata": metadata_payload,
                    "last_error": provider.get("last_error"),
                }
        try:
            if payload is None:
                store.delete_token_metadata(user_id, provider_id)
            else:
                store.upsert_token_metadata(
                    user_id,
                    provider_id,
                    payload,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Failed to persist vault metadata for provider '%s': %r",
                provider_id,
                exc,
            )

    def _sync_all_token_metadata(self) -> None:
        store = self._state_store
        if store is None:
            return
        with self._lock:
            all_pairs = [
                (user_id, provider_id)
                for user_id, providers in self._manifest.items()
                for provider_id in providers.keys()
            ]
        for user_id, provider_id in all_pairs:
            self._persist_token_metadata(user_id, provider_id)

    def _decode_record(self, user_id: str, payload: dict[str, object]) -> ProviderLinkRecord:
        access_reference_value = payload.get("access_reference")
        access_reference = access_reference_value if isinstance(access_reference_value, str) else ""

        expires_at_value = payload.get("expires_at")
        expires_at = _parse_datetime(expires_at_value) if isinstance(expires_at_value, str) else None

        scopes_value = payload.get("scopes")
        scopes: tuple[str, ...] = ()
        if isinstance(scopes_value, list) and all(isinstance(scope, str) for scope in scopes_value):
            scopes = tuple(scopes_value)

        status_value = payload.get("status")
        status = status_value if isinstance(status_value, str) else ProviderLinkState.LINKED.value

        last_error_value = payload.get("last_error")
        last_error = last_error_value if isinstance(last_error_value, str) else ""

        metadata_value = payload.get("metadata")
        metadata = dict(metadata_value) if isinstance(metadata_value, dict) else {}

        return ProviderLinkRecord(
            provider_id=str(payload["provider_id"]),
            user_id=user_id,
            linked_at=_parse_datetime(str(payload["linked_at"])) or datetime.now(UTC),
            updated_at=_parse_datetime(str(payload["updated_at"])) or datetime.now(UTC),
            token_reference=str(payload.get("refresh_reference", "")),
            access_reference=access_reference,
            expires_at=expires_at,
            scopes=scopes,
            status=ProviderLinkState(status),
            last_error=last_error,
            metadata=metadata,
        )

    def _set_secret(self, key: str, value: str) -> None:
        if self._secure_manager is None:
            raise TokenVaultError("Encrypted storage unavailable")
        try:
            self._secure_manager.set_secret(key, value)
        except SecurityError as exc:
            raise TokenVaultError(str(exc)) from exc

    def _get_secret(self, key: str | None) -> str | None:
        if not key:
            return None
        if self._secure_manager is None:
            raise TokenVaultError("Encrypted storage unavailable")
        try:
            return self._secure_manager.get_secret(key)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to decrypt entry %s: %s", key, exc)
            return None

    def _delete_secret(self, key: str | None) -> None:
        if not key:
            return
        if self._secure_manager is None:
            raise TokenVaultError("Encrypted storage unavailable")
        # SecureSettingsManager does not currently expose delete; overwrite with empty string.
        try:
            self._secure_manager.set_secret(key, "")
        except Exception as exc:
            logger.exception("Entry deletion failed (non-critical): %s", exc)

    @staticmethod
    def _metadata_store_key(user_id: str, provider_id: str) -> str:
        return "%s:%s" % (user_id, provider_id.lower())

    @staticmethod
    def _require_user_id(user_id: str | None) -> str:
        if not user_id:
            raise ValueError("user_id is required")
        return user_id

    @staticmethod
    def _default_manifest_path() -> Path:
        from core.platform import get_data_dir

        return get_data_dir() / "token_vault.json"

    def _build_secure_manager(self) -> SecureSettingsManager:
        manager = SecureSettingsManager(app_name="viola-credential-vault")
        if not manager.encryption_enabled:
            raise TokenVaultError("Encrypted storage unavailable (encryption disabled).")
        return manager


def _secret_key(user_id: str, provider_id: str, kind: str) -> str:
    return "%s:%s:%s" % (user_id, provider_id.lower(), kind)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError as e:
        logger.exception("Failed to parse datetime '%s': %s", value, e)
        return None
