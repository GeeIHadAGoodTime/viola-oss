"""Pydantic models for user-authored phrase-triggered routines."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Tier = Literal["solo", "ensemble", "symphony"]
CreatedBy = Literal["viola", "user"]

CAPABILITY_ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,79}$"
_CAPABILITY_ID_RE = re.compile(CAPABILITY_ID_PATTERN)
_TIER_VALUES: set[str] = {"solo", "ensemble", "symphony"}


def normalize_tier(value: object) -> Tier:
    """Normalize a capability tier value."""
    tier = str(value or "").strip().lower()
    if tier not in _TIER_VALUES:
        raise ValueError("tier must be one of: solo, ensemble, symphony")
    return tier  # type: ignore[return-value]


def slugify_capability_id(value: str) -> str:
    """Derive a stable capability id slug from a user-facing name."""
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return (slug or "routine")[:80].strip("_") or "routine"


def validate_capability_id(value: str) -> str:
    """Validate a saved capability id."""
    capability_id = (value or "").strip().lower()
    if not _CAPABILITY_ID_RE.match(capability_id):
        raise ValueError("capability id must be a lowercase slug up to 80 characters")
    return capability_id


def utc_now_iso() -> str:
    """Return a UTC timestamp in the saved JSON shape."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _one_line(value: object, *, field_name: str, max_len: int) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError("%s is required" % field_name)
    if len(text) > max_len:
        raise ValueError("%s must be %d characters or fewer" % (field_name, max_len))
    return text


def _normalize_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("created_at is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class PhraseTrigger(BaseModel):
    """Phrase trigger for v1 routines."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["phrase"] = "phrase"
    phrases: list[str] = Field(min_length=1, max_length=12)

    @field_validator("phrases")
    @classmethod
    def normalize_phrases(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for phrase in value:
            text = _one_line(phrase, field_name="trigger phrase", max_len=120)
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        if not cleaned:
            raise ValueError("at least one trigger phrase is required")
        return cleaned


class CallCapabilityAction(BaseModel):
    """Call an existing MCP tool with fixed arguments."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["call_capability"]
    name: str = Field(min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        text = _one_line(value, field_name="capability name", max_len=128)
        if text.lower() in {"run_user_capability", "user_capabilities"}:
            raise ValueError("capability shortcuts cannot call the user capability management tools")
        if not re.match(r"^[A-Za-z0-9_.-]+$", text):
            raise ValueError("capability name must be an MCP tool name")
        return text


class SummarizeAction(BaseModel):
    """Ask the LLM to summarize prior routine step outputs."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["summarize"]
    style: str = Field(default="concise", min_length=1, max_length=240)

    @field_validator("style")
    @classmethod
    def normalize_style(cls, value: str) -> str:
        return _one_line(value, field_name="summary style", max_len=240)


CapabilityAction = Annotated[CallCapabilityAction | SummarizeAction, Field(discriminator="type")]


class CapabilityDraft(BaseModel):
    """Authored routine input before server-side id/tier normalization."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    id: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=240)
    trigger: PhraseTrigger
    actions: list[CapabilityAction] = Field(min_length=1, max_length=20)
    required_tier: Tier | None = None
    disabled: bool = False
    created_by: CreatedBy = "viola"
    created_at: str | None = None

    @field_validator("id")
    @classmethod
    def normalize_optional_id(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return validate_capability_id(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _one_line(value, field_name="name", max_len=120)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return _one_line(value, field_name="description", max_len=240)

    @field_validator("required_tier", mode="before")
    @classmethod
    def normalize_optional_tier(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return normalize_tier(value)

    @field_validator("created_at")
    @classmethod
    def normalize_optional_created_at(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        return _normalize_timestamp(value)


class UserCapability(BaseModel):
    """Saved per-user routine."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    id: str = Field(min_length=1, max_length=80, pattern=CAPABILITY_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=240)
    trigger: PhraseTrigger
    actions: list[CapabilityAction] = Field(min_length=1, max_length=20)
    required_tier: Tier
    disabled: bool = False
    created_by: CreatedBy = "viola"
    created_at: str

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return validate_capability_id(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _one_line(value, field_name="name", max_len=120)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return _one_line(value, field_name="description", max_len=240)

    @field_validator("required_tier", mode="before")
    @classmethod
    def normalize_required_tier(cls, value: object) -> object:
        return normalize_tier(value)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: object) -> str:
        return _normalize_timestamp(value)
