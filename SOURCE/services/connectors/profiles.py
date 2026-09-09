"""Persistent, user-scoped connector profiles.

Profiles are the durable provider-switching layer.  They store non-secret
provider configuration per user; secrets live in the encrypted API vault and
are referenced by opaque profile-scoped names.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from core.logging_config import get_logger
from core.platform import get_data_dir
from services.connectors.manifests import SCHEMA_VERSION, get_connector_manifest
from services.connectors.models import ConnectionProfile

logger = get_logger(__name__)

_SECRET_REDACTIONS = frozenset({"***ENCRYPTED***", "••••••", "******"})
_MISSING = object()
_SAFE_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class ConnectorProfileError(ValueError):
    """Raised when a profile operation cannot be completed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorSecretStore(Protocol):
    """Minimal secret store contract used by connection profiles."""

    def set_secret(self, ref: str, value: str, metadata: dict[str, Any] | None = None) -> None: ...

    def get_secret(self, ref: str) -> str | None: ...

    def delete_secret(self, ref: str) -> bool: ...

    def has_secret(self, ref: str) -> bool: ...


class ApiVaultConnectorSecretStore:
    """Connector secret adapter backed by the encrypted API vault."""

    def _vault(self):
        from services.api_vault.vault import get_credential_vault

        return get_credential_vault()

    @staticmethod
    def _user_id_from_ref(ref: str) -> str:
        parts = str(ref or "").split(":")
        if len(parts) < 4 or parts[0] != "connector_profile" or not parts[1]:
            raise ConnectorProfileError("invalid_secret_ref", "connector secret ref must include a user scope")
        return parts[1]

    def set_secret(self, ref: str, value: str, metadata: dict[str, Any] | None = None) -> None:
        self._vault().store_credential(ref, value, metadata=metadata or {}, user_id=self._user_id_from_ref(ref))

    def get_secret(self, ref: str) -> str | None:
        return self._vault().get_credential(ref, user_id=self._user_id_from_ref(ref))

    def delete_secret(self, ref: str) -> bool:
        return bool(self._vault().delete_credential(ref, user_id=self._user_id_from_ref(ref)))

    def has_secret(self, ref: str) -> bool:
        return bool(self._vault().has_credential(ref, user_id=self._user_id_from_ref(ref)))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_user_id(user_id: str) -> str:
    normalized = user_id.strip() if isinstance(user_id, str) else ""
    if not normalized:
        raise ConnectorProfileError("missing_user_id", "user_id is required for connector profiles")
    return normalized


def _safe_user_component(user_id: str) -> str:
    safe = _SAFE_COMPONENT_RE.sub("_", _require_user_id(user_id)).strip("._")
    if not safe:
        raise ConnectorProfileError("invalid_user_id", "user_id cannot be used as a profile path")
    return safe


def _string(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _clean_base_url(value: object) -> str:
    return _string(value).strip().rstrip("/")


def _clean_profile_id(value: object) -> str:
    raw = _string(value).strip()
    if raw and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,96}", raw):
        return raw
    return "cp_%s" % uuid.uuid4().hex


def _is_redacted_secret(value: object) -> bool:
    return isinstance(value, str) and value.strip() in _SECRET_REDACTIONS


class ConnectionProfileStore:
    """File-backed user profile store with encrypted secret references."""

    def __init__(
        self,
        storage_root: Path | None = None,
        *,
        secret_store: ConnectorSecretStore | None = None,
    ) -> None:
        self._storage_root = storage_root or (get_data_dir() / "connector_profiles")
        self._secret_store = secret_store or ApiVaultConnectorSecretStore()
        self._lock = threading.RLock()

    def _profile_path(self, user_id: str) -> Path:
        return self._storage_root / ("%s.json" % _safe_user_component(user_id))

    def _load_document(self, user_id: str) -> dict[str, Any]:
        path = self._profile_path(user_id)
        if not path.exists():
            return {"schema_version": SCHEMA_VERSION, "selected": {}, "profiles": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Connector profile document could not be read for user=%s: %s", user_id, exc)
            return {"schema_version": SCHEMA_VERSION, "selected": {}, "profiles": []}
        if not isinstance(data, dict):
            return {"schema_version": SCHEMA_VERSION, "selected": {}, "profiles": []}
        profiles = data.get("profiles")
        selected = data.get("selected")
        rows = profiles if isinstance(profiles, list) else []
        selected_map = selected if isinstance(selected, dict) else {}
        profile_categories_by_id: dict[str, str] = {}
        for row in rows:
            profile = self._row_to_profile(row)
            if profile is not None:
                profile_categories_by_id[profile.profile_id] = profile.category
        normalized_selected: dict[str, str] = {}
        for raw_category, raw_profile_id in selected_map.items():
            category = str(raw_category).strip().lower()
            profile_id = _string(raw_profile_id).strip()
            if not category:
                continue
            normalized_selected[category] = profile_id
        return {
            "schema_version": SCHEMA_VERSION,
            "selected": normalized_selected,
            "profiles": rows,
        }

    def _save_document(self, user_id: str, document: dict[str, Any]) -> None:
        path = self._profile_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "selected": document.get("selected") if isinstance(document.get("selected"), dict) else {},
            "profiles": document.get("profiles") if isinstance(document.get("profiles"), list) else [],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _row_to_profile(self, row: object) -> ConnectionProfile | None:
        if not isinstance(row, dict):
            return None
        profile_id = _string(row.get("profile_id")).strip()
        connector_id = _string(row.get("connector_id")).strip()
        manifest = get_connector_manifest(connector_id)
        if not profile_id or manifest is None:
            return None
        return ConnectionProfile(
            profile_id=profile_id,
            connector_id=manifest.id,
            category=manifest.category,
            display_name=_string(row.get("display_name"), manifest.display_name) or manifest.display_name,
            provider_key=manifest.provider_key,
            adapter=manifest.adapter,
            auth_type=manifest.auth_type,
            privacy_boundary=manifest.privacy_boundary,
            base_url=_clean_base_url(row.get("base_url", manifest.default_base_url or "")),
            model=_string(row.get("model")).strip(),
            enabled=bool(row.get("enabled", True)),
            created_at=_string(row.get("created_at")),
            updated_at=_string(row.get("updated_at")),
            capabilities=dict(row.get("capabilities")) if isinstance(row.get("capabilities"), dict) else {},
            validation=dict(row.get("validation")) if isinstance(row.get("validation"), dict) else {},
            metadata=dict(row.get("metadata")) if isinstance(row.get("metadata"), dict) else {},
            secret_refs=dict(row.get("secret_refs")) if isinstance(row.get("secret_refs"), dict) else {},
        )

    @staticmethod
    def _profile_to_row(profile: ConnectionProfile) -> dict[str, Any]:
        return {
            "profile_id": profile.profile_id,
            "connector_id": profile.connector_id,
            "display_name": profile.display_name,
            "base_url": profile.base_url,
            "model": profile.model,
            "enabled": profile.enabled,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
            "capabilities": dict(profile.capabilities),
            "validation": dict(profile.validation),
            "metadata": dict(profile.metadata),
            "secret_refs": dict(profile.secret_refs),
        }

    def list_profiles(self, user_id: str, category: str | None = None) -> list[ConnectionProfile]:
        normalized_category = (category or "").strip().lower()
        with self._lock:
            document = self._load_document(_require_user_id(user_id))
            profiles = [profile for row in document["profiles"] if (profile := self._row_to_profile(row))]
        if normalized_category:
            return [profile for profile in profiles if profile.category == normalized_category]
        return profiles

    def get_profile(self, user_id: str, profile_id: str) -> ConnectionProfile | None:
        normalized = _string(profile_id).strip()
        if not normalized:
            return None
        return next((profile for profile in self.list_profiles(user_id) if profile.profile_id == normalized), None)

    def get_selected_profile_id(self, user_id: str, category: str) -> str | None:
        normalized_category = category.strip().lower()
        with self._lock:
            document = self._load_document(_require_user_id(user_id))
            selected = document.get("selected") if isinstance(document.get("selected"), dict) else {}
            raw = selected.get(normalized_category)
        selected_id = _string(raw).strip()
        return selected_id or None

    def get_selected_profile(self, user_id: str, category: str) -> ConnectionProfile | None:
        selected_id = self.get_selected_profile_id(user_id, category)
        if not selected_id:
            return None
        return self.get_profile(user_id, selected_id)

    def _secret_ref(self, user_id: str, profile_id: str, secret_name: str) -> str:
        return "connector_profile:%s:%s:%s" % (_safe_user_component(user_id), profile_id, secret_name)

    def profile_has_secret(self, profile: ConnectionProfile, secret_name: str) -> bool:
        ref = profile.secret_refs.get(secret_name)
        return bool(ref and self._secret_store.has_secret(ref))

    def get_profile_secret(self, profile: ConnectionProfile, secret_name: str) -> str | None:
        ref = profile.secret_refs.get(secret_name)
        if not ref:
            return None
        return self._secret_store.get_secret(ref)

    def save_profile(
        self,
        user_id: str,
        connector_id: str,
        *,
        profile_id: str | None = None,
        display_name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: object = _MISSING,
        enabled: bool | None = None,
        metadata: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        selected: bool = False,
    ) -> ConnectionProfile:
        normalized_user = _require_user_id(user_id)
        manifest = get_connector_manifest(connector_id)
        if manifest is None:
            raise ConnectorProfileError("unknown_connector", "Unknown connector: %s" % connector_id)

        with self._lock:
            document = self._load_document(normalized_user)
            rows = [row for row in document["profiles"] if isinstance(row, dict)]
            normalized_profile_id = _clean_profile_id(profile_id)
            existing_index = next(
                (index for index, row in enumerate(rows) if row.get("profile_id") == normalized_profile_id),
                None,
            )
            existing = self._row_to_profile(rows[existing_index]) if existing_index is not None else None
            now = _utc_now()
            secret_refs = dict(existing.secret_refs) if existing else {}

            if api_key is not _MISSING and api_key is not None and not _is_redacted_secret(api_key):
                api_key_text = _string(api_key).strip()
                ref = self._secret_ref(normalized_user, normalized_profile_id, "api_key")
                if api_key_text:
                    self._secret_store.set_secret(
                        ref,
                        api_key_text,
                        metadata={
                            "connector_id": manifest.id,
                            "category": manifest.category,
                            "provider_key": manifest.provider_key,
                        },
                    )
                    secret_refs["api_key"] = ref
                elif "api_key" in secret_refs:
                    self._secret_store.delete_secret(secret_refs["api_key"])
                    secret_refs.pop("api_key", None)

            profile = ConnectionProfile(
                profile_id=normalized_profile_id,
                connector_id=manifest.id,
                category=manifest.category,
                display_name=(
                    display_name.strip()
                    if isinstance(display_name, str) and display_name.strip()
                    else existing.display_name if existing else manifest.display_name
                ),
                provider_key=manifest.provider_key,
                adapter=manifest.adapter,
                auth_type=manifest.auth_type,
                privacy_boundary=manifest.privacy_boundary,
                base_url=_clean_base_url(
                    base_url
                    if base_url is not None
                    else existing.base_url if existing else manifest.default_base_url or ""
                ),
                model=(
                    model.strip()
                    if isinstance(model, str)
                    else existing.model if existing else (manifest.default_models[0] if manifest.default_models else "")
                ),
                enabled=bool(enabled) if enabled is not None else (existing.enabled if existing else True),
                created_at=existing.created_at if existing and existing.created_at else now,
                updated_at=now,
                capabilities=dict(manifest.capabilities),
                validation=validation if validation is not None else (existing.validation if existing else {}),
                metadata=metadata if metadata is not None else (existing.metadata if existing else {}),
                secret_refs=secret_refs,
            )

            row = self._profile_to_row(profile)
            if existing_index is None:
                rows.append(row)
            else:
                rows[existing_index] = row
            document["profiles"] = rows
            if selected:
                selected_map = document.get("selected") if isinstance(document.get("selected"), dict) else {}
                selected_map[profile.category] = profile.profile_id
                document["selected"] = selected_map
            self._save_document(normalized_user, document)
        return profile

    def select_profile(self, user_id: str, profile_id: str) -> ConnectionProfile:
        normalized_user = _require_user_id(user_id)
        profile = self.get_profile(normalized_user, profile_id)
        if profile is None:
            raise ConnectorProfileError("unknown_profile", "Unknown connector profile: %s" % profile_id)
        if not profile.enabled:
            raise ConnectorProfileError("profile_disabled", "Connector profile is disabled")
        with self._lock:
            document = self._load_document(normalized_user)
            selected_map = document.get("selected") if isinstance(document.get("selected"), dict) else {}
            selected_map[profile.category] = profile.profile_id
            document["selected"] = selected_map
            self._save_document(normalized_user, document)
        return profile

    def delete_profile(self, user_id: str, profile_id: str) -> bool:
        normalized_user = _require_user_id(user_id)
        normalized_profile_id = _string(profile_id).strip()
        if not normalized_profile_id:
            return False
        with self._lock:
            document = self._load_document(normalized_user)
            rows = [row for row in document["profiles"] if isinstance(row, dict)]
            profile = next(
                (
                    candidate
                    for row in rows
                    if (candidate := self._row_to_profile(row)) and candidate.profile_id == normalized_profile_id
                ),
                None,
            )
            if profile is None:
                return False
            document["profiles"] = [row for row in rows if row.get("profile_id") != normalized_profile_id]
            selected_map = document.get("selected") if isinstance(document.get("selected"), dict) else {}
            if selected_map.get(profile.category) == profile.profile_id:
                selected_map.pop(profile.category, None)
            document["selected"] = selected_map
            for ref in profile.secret_refs.values():
                self._secret_store.delete_secret(ref)
            self._save_document(normalized_user, document)
        return True

    def update_validation(
        self,
        user_id: str,
        profile_id: str,
        validation: dict[str, Any],
    ) -> ConnectionProfile:
        profile = self.get_profile(user_id, profile_id)
        if profile is None:
            raise ConnectorProfileError("unknown_profile", "Unknown connector profile: %s" % profile_id)
        updated = replace(profile, validation=dict(validation), updated_at=_utc_now())
        with self._lock:
            document = self._load_document(user_id)
            rows = [row for row in document["profiles"] if isinstance(row, dict)]
            for index, row in enumerate(rows):
                if row.get("profile_id") == profile.profile_id:
                    rows[index] = self._profile_to_row(updated)
                    break
            document["profiles"] = rows
            self._save_document(user_id, document)
        return updated

    def selected_map(self, user_id: str) -> dict[str, str]:
        with self._lock:
            document = self._load_document(_require_user_id(user_id))
            selected = document.get("selected") if isinstance(document.get("selected"), dict) else {}
            return {str(key): str(value) for key, value in selected.items() if isinstance(value, str)}

    def has_explicit_selection_state(self, user_id: str, category: str) -> bool:
        normalized_category = category.strip().lower()
        with self._lock:
            document = self._load_document(_require_user_id(user_id))
            selected = document.get("selected") if isinstance(document.get("selected"), dict) else {}
            return normalized_category in selected


def profile_payload(store: ConnectionProfileStore, user_id: str, category: str | None = None) -> dict[str, Any]:
    selected = store.selected_map(user_id)
    profiles = store.list_profiles(user_id, category)
    return {
        "schema_version": SCHEMA_VERSION,
        "profiles": [
            profile.to_dict(selected=selected.get(profile.category) == profile.profile_id) for profile in profiles
        ],
        "selected": selected,
        "summary": {
            "total": len(profiles),
            "selected": sum(1 for profile in profiles if selected.get(profile.category) == profile.profile_id),
            "with_secrets": sum(1 for profile in profiles if bool(profile.secret_refs)),
        },
    }


def native_tool_config_fingerprint(profile: ConnectionProfile, api_key: str) -> str:
    """Bind a tool-contract observation to the configuration that produced it."""
    data = [profile.adapter, profile.base_url, profile.model, api_key]
    return hashlib.sha256(json.dumps(data, ensure_ascii=True).encode()).hexdigest()


def profile_native_tools_verified(profile: ConnectionProfile, api_key: str) -> bool:
    contract = profile.validation.get("tool_contract", {})
    if not isinstance(contract, dict):
        return False
    probe = contract.get("live_tool_probe", {})
    return (
        profile.validation.get("valid") is True
        and isinstance(probe, dict)
        and probe.get("success") is True
        and contract.get("config_fingerprint") == native_tool_config_fingerprint(profile, api_key)
    )


def _tool_probe_success(response: object) -> bool:
    if not isinstance(response, dict):
        return False
    response_type = _string(response.get("type")).strip().lower()
    tool_name = _string(response.get("tool") or response.get("name")).strip()
    arguments = response.get("args", response.get("arguments", response.get("input")))
    return (
        response_type in {"tool_call", "tool_use"}
        and tool_name == "echo_probe"
        and isinstance(arguments, dict)
        and arguments == {"value": "ok"}
    )


async def _run_tool_contract_probe(provider: Any) -> dict[str, Any]:
    start = time.time()
    native_supported = bool(getattr(provider, "SUPPORTS_NATIVE_TOOLS", False))
    try:
        if native_supported and callable(getattr(provider, "route_command_native", None)):
            response = await provider.route_command_native(
                messages=[{"role": "user", "content": "Use the echo_probe tool with value ok."}],
                native_tools=[
                    {
                        "name": "echo_probe",
                        "description": "Return the requested probe value.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ],
                system_prompt="You are validating tool calling. Use tools when a matching tool is available.",
                first_turn=True,
                max_tokens=128,
            )
            schema_delivery = "native_tools"
        else:
            return {
                "ran": True,
                "success": False,
                "schema_delivery": "native_tools",
                "latency_ms": round((time.time() - start) * 1000),
                "error": "NativeToolCallingUnsupported",
                "message": "Tool contract probes require route_command_native().",
            }
    except Exception as exc:
        return {
            "ran": True,
            "success": False,
            "schema_delivery": "native_tools" if native_supported else "json_in_prompt",
            "latency_ms": round((time.time() - start) * 1000),
            "error": type(exc).__name__,
            "message": str(exc),
        }

    return {
        "ran": True,
        "success": _tool_probe_success(response),
        "schema_delivery": schema_delivery,
        "latency_ms": round((time.time() - start) * 1000),
        "response_type": _string(response.get("type")) if isinstance(response, dict) else type(response).__name__,
        "tool": _string(response.get("tool") or response.get("name")) if isinstance(response, dict) else "",
    }


async def validate_llm_profile(
    store: ConnectionProfileStore,
    user_id: str,
    profile_id: str,
    *,
    probe_tools: bool = False,
) -> dict[str, Any]:
    profile = store.get_profile(user_id, profile_id)
    if profile is None:
        raise ConnectorProfileError("unknown_profile", "Unknown connector profile: %s" % profile_id)
    if profile.category != "llm":
        raise ConnectorProfileError("unsupported_category", "Only LLM profiles can be validated here")

    from services.llm.factory import LLMProviderFactory
    from services.llm.providers.base import LLMConfig

    provider = None
    api_key = store.get_profile_secret(profile, "api_key") or ""
    validation_model = profile.model
    if profile.connector_id in {"llm.managed", "llm.codex"}:
        try:
            from ui.settings_manager import get_settings_manager

            settings = get_settings_manager()
            if profile.connector_id == "llm.managed":
                provider = LLMProviderFactory._create_managed_provider_gated(settings)
            else:
                provider = LLMProviderFactory._create_codex_provider(settings, model=profile.model or None)
        except Exception as exc:
            validation = {
                "valid": False,
                "message": str(exc),
                "latency_ms": None,
                "model": validation_model,
                "error_code": "not_available",
                "available_models": [],
                "tool_contract": {
                    "native_tools_supported": False,
                    "schema_delivery": "native_tools",
                    "live_tool_probe": "not_run",
                },
            }
            store.update_validation(user_id, profile.profile_id, validation)
            return validation
    else:
        effective_api_key = api_key
        if (
            profile.adapter == "openai_compatible"
            and not effective_api_key
            and LLMProviderFactory._is_local_base_url(profile.base_url)
        ):
            effective_api_key = (
                "local-ai"  # pragma: allowlist secret -- local OpenAI-compatible servers often accept any key
            )

        config = LLMConfig(
            provider=profile.adapter,
            api_key=effective_api_key or None,
            model=profile.model,
            base_url=profile.base_url or None,
        )
        validation_model = config.model
        provider = LLMProviderFactory.create_provider(config)

    if provider is None:
        validation = {
            "valid": False,
            "message": "Provider is not available",
            "latency_ms": None,
            "model": validation_model,
            "error_code": "not_available",
            "available_models": [],
            "tool_contract": {
                "native_tools_supported": False,
                "schema_delivery": "native_tools",
                "live_tool_probe": "not_run",
            },
        }
        store.update_validation(user_id, profile.profile_id, validation)
        return validation

    result = await provider.test_connection()
    tool_contract = {
        "native_tools_supported": bool(getattr(provider, "SUPPORTS_NATIVE_TOOLS", False)),
        "schema_delivery": "native_tools" if getattr(provider, "SUPPORTS_NATIVE_TOOLS", False) else "json_in_prompt",
        "live_tool_probe": "not_run",
    }
    if probe_tools:
        # Explicit validation permits one native request against a generic
        # endpoint. It does not enable agent turns until the response proves
        # the advertised tool name AND arguments, and connection test passes.
        old_native_support = provider.SUPPORTS_NATIVE_TOOLS
        if profile.adapter == "openai_compatible":
            provider.SUPPORTS_NATIVE_TOOLS = True
        try:
            probe = await _run_tool_contract_probe(provider)
        finally:
            provider.SUPPORTS_NATIVE_TOOLS = old_native_support
        tool_contract["live_tool_probe"] = probe
        tool_contract["native_tools_supported"] = bool(result.success and probe.get("success"))
        tool_contract["schema_delivery"] = "native_tools"
        tool_contract["config_fingerprint"] = native_tool_config_fingerprint(profile, api_key)

    tool_probe_failed = probe_tools and tool_contract["native_tools_supported"] is not True
    validation = {
        "valid": result.success and not tool_probe_failed,
        "message": (
            "Native tool calling could not be verified for this profile." if tool_probe_failed else result.message
        ),
        "latency_ms": result.latency_ms,
        "model": validation_model,
        "error_code": result.error_code or ("native_tool_contract_failed" if tool_probe_failed else None),
        "available_models": provider.get_available_models(),
        "tool_contract": tool_contract,
    }
    store.update_validation(user_id, profile.profile_id, validation)
    return validation


_STORE: ConnectionProfileStore | None = None


def get_connection_profile_store() -> ConnectionProfileStore:
    global _STORE
    if _STORE is None:
        _STORE = ConnectionProfileStore()
    return _STORE
