"""Session identity helpers for branch, fork, and resume flows."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import NewType

_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def new_session_id() -> str:
    """Return a provider-neutral durable session id."""

    return str(uuid.uuid4())


def coerce_session_id(value: object) -> str | None:
    """Return a validated session id or ``None`` for empty input."""

    if isinstance(value, SessionIdentity):
        value = value.session_id
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    if cleaned in {".", ".."} or not _SAFE_SESSION_ID_RE.fullmatch(cleaned):
        raise ValueError("session_id contains unsafe characters")
    return cleaned


NonEmptyUserId = NewType("NonEmptyUserId", str)


def coerce_user_id(value: object, *, field_name: str = "user_id") -> NonEmptyUserId:
    """Return a concrete user id or raise for missing/default owner state."""

    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("%s is required" % field_name)
    if cleaned.lower() == "default":
        raise ValueError("%s must be concrete, not 'default'" % field_name)
    return NonEmptyUserId(cleaned)


make_user_id = coerce_user_id


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Durable identity attached to frames, tools, agents, and resumes."""

    session_id: str
    branch_id: str | None = None
    fork_id: str | None = None
    parent_session_id: str | None = None
    source_frame_uuid: str | None = None
    resumed_from_uuid: str | None = None
    channel: str | None = None


@dataclass(frozen=True, slots=True)
class SessionBranch:
    """Request to copy transcript frames into a new branch session."""

    source_session_id: str
    source_frame_uuid: str
    new_session_id: str | None = None
    reason: str = "branch"
    channel: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    """Request to reopen a known session leaf."""

    session_id: str
    leaf_uuid: str | None = None
    channel: str | None = None


def _clean_required(value: str | None, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError("%s is required" % field_name)
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def create_branch(request: SessionBranch) -> SessionIdentity:
    """Create branch identity from a source session/frame pair."""

    source_session_id = coerce_session_id(request.source_session_id)
    if source_session_id is None:
        raise ValueError("source_session_id is required")
    source_frame_uuid = _clean_required(request.source_frame_uuid, "source_frame_uuid")
    new_id = coerce_session_id(request.new_session_id) if request.new_session_id is not None else None
    new_id = new_id or new_session_id()
    return SessionIdentity(
        session_id=new_id,
        branch_id=new_id,
        parent_session_id=source_session_id,
        source_frame_uuid=source_frame_uuid,
        channel=_clean_optional(request.channel),
    )


def resume_session(request: ResumeRequest) -> SessionIdentity:
    """Create resume identity for a known session leaf."""

    session_id = coerce_session_id(request.session_id)
    if session_id is None:
        raise ValueError("session_id is required")
    return SessionIdentity(
        session_id=session_id,
        resumed_from_uuid=_clean_optional(request.leaf_uuid),
        channel=_clean_optional(request.channel),
    )


__all__ = [
    "NonEmptyUserId",
    "ResumeRequest",
    "SessionBranch",
    "SessionIdentity",
    "coerce_session_id",
    "coerce_user_id",
    "create_branch",
    "make_user_id",
    "new_session_id",
    "resume_session",
]
