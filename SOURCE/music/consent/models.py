"""
Data models for the unified consent orchestrator.

These lightweight dataclasses capture provider metadata, OAuth tokens,
progressive session state, and provider link status that are used across the
consent service, token vault, API layer, and UI.

The models are deliberately serialisable (dictionary friendly) so they can be
returned through FastAPI responses without additional conversion helpers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from core.json_types import JsonDict


class ConsentSessionStatus(str, Enum):
    """Lifecycle states for a progressive consent session."""

    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ERROR = "error"


class ProviderLinkState(str, Enum):
    """Link state for a provider account."""

    UNAVAILABLE = "unavailable"
    NOT_LINKED = "not_linked"
    PENDING = "pending"
    LINKED = "linked"
    ERROR = "error"


@dataclass(slots=True)
class ProviderCapability:
    """Summary of provider specific capabilities surfaced to the UI."""

    supports_offline: bool = False
    max_bitrate_kbps: int | None = None
    supports_lyrics: bool = False
    extras: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "supports_offline": self.supports_offline,
            "supports_lyrics": self.supports_lyrics,
        }
        if self.max_bitrate_kbps is not None:
            payload["max_bitrate_kbps"] = self.max_bitrate_kbps
        if self.extras:
            payload["extras"] = dict(self.extras)
        return payload


@dataclass(slots=True)
class ProviderMetadata:
    """Static metadata describing a provider."""

    provider_id: str
    display_name: str
    scopes: Sequence[str]
    default_redirect_uri: str | None = None
    capability: ProviderCapability = field(default_factory=ProviderCapability)
    can_rotate: bool = True
    notes: str | None = None
    is_music_provider: bool = False

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "scopes": list(self.scopes),
            "capability": self.capability.to_dict(),
            "can_rotate": self.can_rotate,
            "is_music_provider": self.is_music_provider,
        }
        if self.default_redirect_uri:
            payload["default_redirect_uri"] = self.default_redirect_uri
        if self.notes:
            payload["notes"] = self.notes
        return payload


@dataclass(slots=True)
class TokenBundle:
    """
    OAuth token payload stored in the encrypted vault.

    The bundle keeps both access and refresh tokens and their expiry metadata.
    Only the refresh token is strictly required; access tokens are refreshed on
    demand.
    """

    access_token: str | None
    refresh_token: str
    expires_at: datetime | None
    scopes: Sequence[str] = field(default_factory=tuple)
    token_type: str = "Bearer"
    metadata: JsonDict = field(default_factory=dict)

    def is_expired(self, *, skew_seconds: int = 60) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= datetime.now(UTC) + timedelta(seconds=skew_seconds)

    def to_public_dict(self) -> JsonDict:
        """
        Expose non-sensitive token metadata. Actual tokens are never returned.
        """
        return {
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": list(self.scopes),
            "token_type": self.token_type,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ProviderLinkRecord:
    """Persisted vault record for a provider account."""

    provider_id: str
    user_id: str
    linked_at: datetime
    updated_at: datetime
    token_reference: str
    access_reference: str | None
    expires_at: datetime | None
    scopes: Sequence[str]
    status: ProviderLinkState = ProviderLinkState.LINKED
    last_error: str | None = None
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "provider_id": self.provider_id,
            "user_id": self.user_id,
            "linked_at": self.linked_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": list(self.scopes),
            "status": self.status.value,
            "last_error": self.last_error,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ConsentStep:
    """A single provider step within a consent session."""

    provider_id: str
    display_name: str
    authorization_url: str | None
    oauth_state: str | None = None
    status: ProviderLinkState = ProviderLinkState.NOT_LINKED
    error: str | None = None
    step_type: str | None = None
    step_index: int = 0
    required_scopes: Sequence[str] = field(default_factory=tuple)
    completed_data: JsonDict | None = None
    completed_at: datetime | None = None

    def to_dict(self) -> JsonDict:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "authorization_url": self.authorization_url,
            "status": self.status.value,
            "error": self.error,
        }


@dataclass(slots=True)
class ConsentSession:
    """Progressive consent state shared between backend and UI."""

    session_id: str
    user_id: str
    steps: list[ConsentStep]
    created_at: datetime
    expires_at: datetime
    status: ConsentSessionStatus = ConsentSessionStatus.ACTIVE
    current_step_index: int = 0
    redirect_uri: str | None = None
    include_calendar: bool = False
    _reused: bool = False  # Internal flag to track session reuse
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    provider_ids: list[str] = field(default_factory=list)

    def current_step(self) -> ConsentStep | None:
        if self.status != ConsentSessionStatus.ACTIVE or self.current_step_index >= len(self.steps):
            return None
        return self.steps[self.current_step_index]

    def advance(self) -> ConsentStep | None:
        """Move to the next step and return it."""
        self.current_step_index += 1
        if self.current_step_index >= len(self.steps):
            self.status = ConsentSessionStatus.COMPLETED
            return None
        return self.steps[self.current_step_index]

    def mark_error(self, message: str) -> None:
        step = self.current_step()
        if step:
            step.status = ProviderLinkState.ERROR
            step.error = message
        self.status = ConsentSessionStatus.ERROR

    def to_dict(self) -> JsonDict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "current_step_index": self.current_step_index,
            "redirect_uri": self.redirect_uri,
            "include_calendar": self.include_calendar,
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass(slots=True)
class ProviderStatus:
    """Public view of provider link state for UI and API consumers."""

    provider_id: str
    display_name: str
    state: ProviderLinkState
    last_linked_at: datetime | None = None
    expires_at: datetime | None = None
    scopes: Sequence[str] = field(default_factory=tuple)
    capability: ProviderCapability | None = None
    notes: str | None = None
    last_error: str | None = None
    metadata: JsonDict = field(default_factory=dict)
    is_music_provider: bool = False
    is_active_music_provider: bool = False
    authorization_url: str | None = None  # URL to initiate OAuth flow for this provider

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "state": self.state.value,
            "scopes": list(self.scopes),
            "metadata": dict(self.metadata),
            "notes": self.notes,
            "last_error": self.last_error,
            "is_music_provider": self.is_music_provider,
            "is_active_music_provider": self.is_active_music_provider,
        }
        if self.last_linked_at:
            payload["last_linked_at"] = self.last_linked_at.isoformat()
        if self.expires_at:
            payload["expires_at"] = self.expires_at.isoformat()
        if self.capability:
            payload["capability"] = self.capability.to_dict()
        if self.authorization_url:
            payload["authorization_url"] = self.authorization_url
        return payload


@dataclass(slots=True)
class RotationOutcome:
    """Result of running a token rotation job."""

    provider_id: str
    refreshed: bool
    reason: str
    expires_at: datetime | None
    error: str | None = None

    def to_dict(self) -> JsonDict:
        return {
            "provider_id": self.provider_id,
            "refreshed": self.refreshed,
            "reason": self.reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "error": self.error,
        }


def clone_statuses(statuses: Iterable[ProviderStatus]) -> list[JsonDict]:
    """Utility to convert provider statuses to dict."""
    return [status.to_dict() for status in statuses]
