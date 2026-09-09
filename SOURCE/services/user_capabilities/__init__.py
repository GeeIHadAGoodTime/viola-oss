"""Public surface for user-authored phrase-triggered routines."""

from __future__ import annotations

from .schema import CapabilityDraft, UserCapability
from .service import (
    CapabilityArgsValidationError,
    CapabilitySaveResult,
    build_context_for_user,
    build_run_plan,
    create_for_user,
    delete_for_user,
    detect_instant_collisions,
    detect_phrase_collisions,
    list_for_user,
    read_for_user,
    set_disabled_for_user,
    set_mcp_hub,
    update_for_user,
)
from .storage import CapabilityStore

__all__ = [
    "CapabilityArgsValidationError",
    "CapabilityDraft",
    "CapabilitySaveResult",
    "CapabilityStore",
    "UserCapability",
    "build_context_for_user",
    "build_run_plan",
    "create_for_user",
    "delete_for_user",
    "detect_instant_collisions",
    "detect_phrase_collisions",
    "list_for_user",
    "read_for_user",
    "set_disabled_for_user",
    "set_mcp_hub",
    "update_for_user",
]
