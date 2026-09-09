from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    session_id: str | None
    role: str


@dataclass(frozen=True)
class SpokePrincipal:
    device_id: str
    token_id: str
    hub_user_id: str | None


AuthPrincipal: TypeAlias = UserPrincipal | SpokePrincipal


__all__ = [
    "AuthPrincipal",
    "SpokePrincipal",
    "UserPrincipal",
]
