"""Typed connector contracts shared by provider/status APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectorAction:
    """User-visible action a connector can expose without hiding side effects."""

    id: str
    label: str
    method: str
    path: str
    mutates_connection: bool = False
    mutates_selection: bool = False
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "method": self.method,
            "path": self.path,
            "mutates_connection": self.mutates_connection,
            "mutates_selection": self.mutates_selection,
            "destructive": self.destructive,
        }


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    """Static truth for one connectable provider/source."""

    id: str
    category: str
    display_name: str
    description: str
    connector_kind: str
    provider_key: str
    adapter: str
    auth_type: str
    privacy_boundary: str
    requires_api_key: bool = False
    default_base_url: str | None = None
    default_models: tuple[str, ...] = ()
    popular_models: tuple[str, ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)
    actions: tuple[ConnectorAction, ...] = ()
    tags: tuple[str, ...] = ()
    docs_url: str | None = None
    setting_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "display_name": self.display_name,
            "description": self.description,
            "connector_kind": self.connector_kind,
            "provider_key": self.provider_key,
            "adapter": self.adapter,
            "auth_type": self.auth_type,
            "privacy_boundary": self.privacy_boundary,
            "requires_api_key": self.requires_api_key,
            "default_base_url": self.default_base_url,
            "default_models": list(self.default_models),
            "popular_models": list(self.popular_models),
            "capabilities": dict(self.capabilities),
            "actions": [action.to_dict() for action in self.actions],
            "tags": list(self.tags),
            "setting_hints": dict(self.setting_hints),
        }
        if self.docs_url:
            payload["docs_url"] = self.docs_url
        return payload


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    """Runtime truth for one connector for a concrete user."""

    id: str
    category: str
    connected: bool
    selected: bool
    ready: bool
    state: str
    reason: str
    status_source: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    actions: tuple[ConnectorAction, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "connected": self.connected,
            "selected": self.selected,
            "ready": self.ready,
            "state": self.state,
            "reason": self.reason,
            "status_source": self.status_source,
            "capabilities": dict(self.capabilities),
            "diagnostics": dict(self.diagnostics),
            "profile": dict(self.profile),
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class ConnectionProfile:
    """Saved provider configuration for one user and connector.

    Secrets are never stored on the profile itself.  ``secret_refs`` points to
    encrypted credential-vault entries and API payloads expose only presence.
    """

    profile_id: str
    connector_id: str
    category: str
    display_name: str
    provider_key: str
    adapter: str
    auth_type: str
    privacy_boundary: str
    base_url: str = ""
    model: str = ""
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    secret_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self, *, selected: bool = False) -> dict[str, Any]:
        """Return the user/API safe representation of this profile."""

        return {
            "profile_id": self.profile_id,
            "connector_id": self.connector_id,
            "category": self.category,
            "display_name": self.display_name,
            "provider_key": self.provider_key,
            "adapter": self.adapter,
            "auth_type": self.auth_type,
            "privacy_boundary": self.privacy_boundary,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "selected": selected,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "capabilities": dict(self.capabilities),
            "validation": dict(self.validation),
            "metadata": dict(self.metadata),
            "secrets": {name: bool(ref) for name, ref in self.secret_refs.items()},
        }
