from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator


class CommandCtx(BaseModel):
    platform: str | None = Field(None, max_length=50)
    locale: str | None = Field(None, max_length=10)
    user: str | None = Field(None, max_length=100)


class HistoryMessage(BaseModel):
    role: str  # "user" or "assistant"
    message: str = Field(..., max_length=500)
    timestamp: str | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ["user", "assistant", "system"]:
            raise ValueError('role must be "user", "assistant", or "system"')
        return value


class CommandIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)
    ctx: CommandCtx | None = None
    history: list[HistoryMessage] | None = None

    @field_validator("text")
    @classmethod
    def sanitize_text(cls, value: str) -> str:
        import re

        sanitized_value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
        sanitized_value = sanitized_value.strip()
        if not sanitized_value:
            raise ValueError("text must not be empty after stripping")
        return sanitized_value

    @field_validator("history")
    @classmethod
    def validate_history_size(cls, value: list[HistoryMessage] | None) -> list[HistoryMessage] | None:
        if value:
            if len(value) > 10:
                raise ValueError("history may include at most 10 entries")
            total_size = sum(len(msg.message) for msg in value)
            if total_size > 5000:
                raise ValueError(f"Total history size ({total_size}) exceeds limit (5000 chars)")
        return value


class CommandOut(BaseModel):
    ok: bool
    intent: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class DebugEventIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="web", min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def sanitize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("source")
    @classmethod
    def sanitize_source(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source must not be empty")
        return value


class PlayIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    source: str | None = Field(default=None, description="ytsearch1, url, local, spotify_cdp, youtube_music")
    target_room: str | None = Field(default=None, max_length=80, description="Optional in-house room target")

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        allowed = {"ytsearch1", "url", "local", "spotify_cdp", "youtube_music"}
        if value is not None and value not in allowed:
            raise ValueError("source must be one of: %s" % ", ".join(sorted(allowed)))
        return value

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        source = info.data.get("source") if info.data else None
        if source == "url":
            if not (value.startswith("http://") or value.startswith("https://")):
                raise ValueError("URL source requires http:// or https:// prefix")
        return value

    @field_validator("target_room")
    @classmethod
    def validate_target_room(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class VolumeIn(BaseModel):
    level: int = Field(..., ge=0)

    @field_validator("level")
    @classmethod
    def clamp_level(cls, value: int) -> int:
        return max(0, min(100, value))


__all__ = [
    "CommandCtx",
    "CommandIn",
    "CommandOut",
    "DebugEventIn",
    "HistoryMessage",
    "PlayIn",
    "VolumeIn",
]
