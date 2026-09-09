from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(frozen=True)
class IntentBridgeConfig:
    enable_gpt: bool = True
    enable_skills: bool = True
    skill_timeout_seconds: float = 6.0
    interpret_timeout_seconds: float = 610.0  # Must exceed agent _DEFAULT_TIMEOUT (600s) for tool-use loops
    dispatch_timeout_seconds: float = 6.0
    intent_heartbeat_grace_seconds: float = 180.0


def load_config(settings: Any) -> IntentBridgeConfig:
    """
    Load feature toggles for the intent bridge.
    """

    enable_gpt = _coerce_bool(getattr(settings, "intent_enable_gpt", None), True)
    enable_skills = _coerce_bool(getattr(settings, "intent_enable_skills", None), True)
    skill_timeout_seconds = float(getattr(settings, "intent_skill_timeout_seconds", 6.0) or 6.0)
    interpret_timeout_seconds = float(getattr(settings, "intent_interpret_timeout_seconds", 610.0) or 610.0)
    dispatch_timeout_seconds = float(getattr(settings, "intent_dispatch_timeout_seconds", 6.0) or 6.0)
    intent_heartbeat_grace_seconds = float(getattr(settings, "intent_heartbeat_grace_seconds", 180.0) or 180.0)

    return IntentBridgeConfig(
        enable_gpt=enable_gpt,
        enable_skills=enable_skills,
        skill_timeout_seconds=skill_timeout_seconds,
        interpret_timeout_seconds=interpret_timeout_seconds,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
        intent_heartbeat_grace_seconds=intent_heartbeat_grace_seconds,
    )
