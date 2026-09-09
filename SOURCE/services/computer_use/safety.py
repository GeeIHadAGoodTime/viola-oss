"""Safety checks for desktop computer use."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_CHARS_PER_TYPE = 2000
# SEC-035: a per-app desktop-control grant must expire and require re-consent
# instead of lasting the whole process lifetime. After this many seconds of
# wall-clock time the grant lapses and the next mutating action re-prompts the
# user. This bounds the blast radius of a single approval on the highest-power
# tool (full desktop control) without nagging on every action.
SESSION_APP_GRANT_TTL_SECONDS = 15 * 60
# Renamed 2026-05-11: was COMPUTER_USE_REQUIRED_TIER. The value names the
# agent autonomy mode (solo/ensemble/symphony), not a billing tier — see
# ui/settings_manager.py:DEFAULT_SETTINGS['agent_autonomy'].
COMPUTER_USE_REQUIRED_AUTONOMY = "symphony"
# Back-compat alias for external callers.
COMPUTER_USE_REQUIRED_TIER = COMPUTER_USE_REQUIRED_AUTONOMY

BLOCKED_KEY_CHORDS: frozenset[str] = frozenset(
    {
        "ctrl+alt+del",
        "control+alt+delete",
        "ctrl+alt+delete",
        "win+l",
        "windows+l",
        "meta+l",
        "cmd+l",
        "alt+f4",
    }
)
_KEY_PART_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "delete": "del",
    "windows": "win",
    "meta": "win",
    "cmd": "win",
    "lwin": "win",
    "rwin": "win",
}
_BLOCKED_KEY_PART_SETS: tuple[frozenset[str], ...] = (
    frozenset({"ctrl", "alt", "del"}),
    frozenset({"win", "l"}),
    frozenset({"alt", "f4"}),
)

UAC_EXECUTABLES: frozenset[str] = frozenset({"consent.exe"})
UAC_CLASS_MARKERS: tuple[str, ...] = ("credential dialog xaml host",)
PASSWORD_MARKERS: tuple[str, ...] = (
    "password",
    "passcode",
    "pin",
    "security code",
    "cvv",
    "cvc",
)
COMPUTER_USE_MUTATING_ACTIONS: frozenset[str] = frozenset(
    {
        "launch_app",
        "focus_window",
        "click",
        "double_click",
        "right_click",
        "type",
        "key",
        "scroll",
        "mouse_move",
        "drag",
        "click_ref",
        "background_type",
        "mouse_move_relative",
        "mouse_button_down",
        "mouse_button_up",
        "mouse_button_hold",
        "key_down",
        "key_up",
        "key_hold",
        "mouse_move_angle",
    }
)
# SEC-035: maps grant key -> monotonic expiry deadline. Time-boxed so a single
# approval cannot grant desktop control for the whole process lifetime.
_SESSION_APP_GRANTS: dict[tuple[str, str, str], float] = {}
_LOW_LEVEL_HELD_KEYS: dict[tuple[str, str], set[str]] = {}
_CONTROL_SESSION_LOCKS: dict[str, ComputerUseControlSession] = {}
_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "delete": "del",
    "windows": "win",
    "super": "win",
    "command": "cmd",
}
_KEY_ORDER: tuple[str, ...] = ("ctrl", "alt", "shift", "win", "meta", "cmd")
OBSCURED_TEXT_MARKERS: tuple[str, ...] = ("••••", "****", "●●●●", "password")


@dataclass(frozen=True)
class ComputerUseRefusal(Exception):
    """Structured refusal for unsafe computer-use actions."""

    error_category: str
    reason: str
    action: str = ""
    target_app_executable: str = ""

    def to_envelope(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_category": self.error_category,
            "reason": self.reason,
            "action": self.action,
            "target_app_executable": self.target_app_executable,
        }


@dataclass(frozen=True)
class ComputerUseControlSession:
    """Active desktop-control lease for one local user."""

    user_id: str
    session_id: str
    action: str


def normalize_executable_name(value: str | None) -> str:
    """Normalize app executable names for whitelist and approval keys."""
    return Path(str(value or "").strip()).name.lower()


def _resolve_local_user_id(user_id: str | None) -> str:
    from core.user_context import get_current_or_device_user_id, user_id_or_none

    return user_id_or_none(user_id) or get_current_or_device_user_id()


def _settings_get(key: str, default: object, *, user_id: str | None = None) -> object:
    try:
        from ui.settings_manager import get_settings_manager

        return get_settings_manager().get(key, default, user_id=_resolve_local_user_id(user_id))
    except Exception:
        logger.debug("SettingsManager unavailable for computer-use setting '%s'", key)
        return default


def get_agent_autonomy(*, user_id: str | None = None) -> str:
    """Return the normalized agent autonomy mode used for computer-use access.

    The SettingsManager `get()` aliases `capability_tier` to `agent_autonomy`,
    so this reads the canonical key directly.
    """
    raw = _settings_get("agent_autonomy", "solo", user_id=user_id)
    return str(raw or "solo").strip().lower()


# Back-compat alias. Callers that imported the old name keep working.
def get_capability_tier(*, user_id: str | None = None) -> str:
    return get_agent_autonomy(user_id=user_id)


def is_computer_use_allowed(*, user_id: str | None = None) -> bool:
    """Return whether the user's agent autonomy mode allows desktop computer use."""
    return get_agent_autonomy(user_id=user_id) == COMPUTER_USE_REQUIRED_AUTONOMY


def get_app_whitelist(*, user_id: str | None = None) -> set[str]:
    """Return lower-case executable names allowed without per-action approval."""
    raw = _settings_get("computer_use_app_whitelist", [], user_id=user_id)
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list | tuple | set):
        values = [str(item).strip() for item in raw]
    else:
        values = []
    return {normalize_executable_name(item) for item in values if normalize_executable_name(item)}


def is_app_whitelisted(executable: str | None, *, user_id: str | None = None) -> bool:
    """Return True when an executable is on the user's computer-use whitelist."""
    normalized = normalize_executable_name(executable)
    return bool(normalized and normalized in get_app_whitelist(user_id=user_id))


def _grant_key(
    target_app_executable: str | None,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> tuple[str, str, str] | None:
    target = normalize_executable_name(target_app_executable)
    if not target:
        return None
    owner = _resolve_local_user_id(user_id)
    session = str(session_id or "local-session").strip() or "local-session"
    return (owner, session, target)


def is_mutating_action(action: str) -> bool:
    return str(action or "").strip().lower() in COMPUTER_USE_MUTATING_ACTIONS


def _grant_ttl_seconds() -> float:
    """Resolve the grant TTL, allowing a per-user override but never unbounded."""
    raw = _settings_get("computer_use_grant_ttl_seconds", SESSION_APP_GRANT_TTL_SECONDS)
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        return float(SESSION_APP_GRANT_TTL_SECONDS)
    # Bound the override: a non-positive or absurd value falls back to the
    # default so the grant can never be made effectively permanent.
    if ttl <= 0 or ttl > 24 * 3600:
        return float(SESSION_APP_GRANT_TTL_SECONDS)
    return ttl


def grant_session_app(
    target_app_executable: str | None,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    """Grant this user/session permission to mutate one desktop executable.

    The grant is time-boxed (SEC-035): it lapses after the configured TTL so the
    user must re-consent rather than the first approval lasting forever.
    """
    key = _grant_key(target_app_executable, user_id=user_id, session_id=session_id)
    if key is None:
        return False
    _SESSION_APP_GRANTS[key] = time.monotonic() + _grant_ttl_seconds()
    return True


def has_session_app_grant(
    target_app_executable: str | None,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> bool:
    key = _grant_key(target_app_executable, user_id=user_id, session_id=session_id)
    if key is None:
        return False
    expiry = _SESSION_APP_GRANTS.get(key)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        # Lapsed: drop it so the next action re-prompts and the map stays small.
        _SESSION_APP_GRANTS.pop(key, None)
        logger.info("Computer-use app grant expired; re-consent required for %s", key[2])
        return False
    return True


def clear_session_app_grants(*, user_id: str | None = None, session_id: str | None = None) -> None:
    """Clear desktop app grants for tests, session reset, or shutdown cleanup."""
    if user_id is None and session_id is None:
        _SESSION_APP_GRANTS.clear()
        return
    owner = _resolve_local_user_id(user_id)
    session = str(session_id or "local-session").strip() or "local-session"
    for key in [key for key in _SESSION_APP_GRANTS if key[0] == owner and key[1] == session]:
        _SESSION_APP_GRANTS.pop(key, None)


def require_action_consent(
    action: str,
    *,
    target_app_executable: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Refuse desktop mutations without a saved whitelist or session app grant."""
    if not is_mutating_action(action):
        return
    target = normalize_executable_name(target_app_executable)
    if not target:
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_APP_CONSENT_REQUIRED",
            reason="Desktop mutations require an application consent scope.",
            action=action,
            target_app_executable=target,
        )
    if is_app_whitelisted(target, user_id=user_id):
        return
    if has_session_app_grant(target, user_id=user_id, session_id=session_id):
        return
    raise ComputerUseRefusal(
        error_category="COMPUTER_USE_APP_CONSENT_REQUIRED",
        reason="Desktop mutation requires approval for this application in the current session.",
        action=action,
        target_app_executable=target,
    )


def acquire_control_session(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    action: str,
) -> ComputerUseControlSession | None:
    """Acquire the per-user desktop-control lock for mutating computer-use actions."""
    if not is_mutating_action(action):
        return None
    owner = _resolve_local_user_id(user_id)
    session = str(session_id or "local-session").strip() or "local-session"
    existing = _CONTROL_SESSION_LOCKS.get(owner)
    if existing is not None:
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_SESSION_BUSY",
            reason="Another computer-use action is already controlling this desktop.",
            action=action,
        )
    token = ComputerUseControlSession(user_id=owner, session_id=session, action=action)
    _CONTROL_SESSION_LOCKS[owner] = token
    return token


def release_control_session(token: ComputerUseControlSession | None) -> None:
    if token is None:
        return
    if _CONTROL_SESSION_LOCKS.get(token.user_id) == token:
        _CONTROL_SESSION_LOCKS.pop(token.user_id, None)


def abort_control_session(*, user_id: str | None = None, session_id: str | None = None) -> bool:
    owner = _resolve_local_user_id(user_id)
    existing = _CONTROL_SESSION_LOCKS.get(owner)
    if existing is None:
        return False
    requested_session = str(session_id or "").strip()
    if requested_session and existing.session_id != requested_session:
        return False
    _CONTROL_SESSION_LOCKS.pop(owner, None)
    return True


def control_session_indicator(
    token: ComputerUseControlSession | None,
) -> dict[str, object] | None:
    if token is None:
        return None
    return {
        "active": True,
        "user_id": token.user_id,
        "session_id": token.session_id,
        "action": token.action,
        "abort_supported": True,
        "cleanup_on_abort": True,
    }


def clear_control_sessions(*, user_id: str | None = None) -> None:
    if user_id is None:
        _CONTROL_SESSION_LOCKS.clear()
        return
    owner = _resolve_local_user_id(user_id)
    _CONTROL_SESSION_LOCKS.pop(owner, None)


def get_max_chars_per_type(*, user_id: str | None = None) -> int:
    raw = _settings_get("computer_use_max_chars_per_type", DEFAULT_MAX_CHARS_PER_TYPE, user_id=user_id)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS_PER_TYPE


def get_screenshot_format(*, user_id: str | None = None) -> str:
    raw = _settings_get("computer_use_screenshot_format", "png", user_id=user_id)
    normalized = str(raw or "png").strip().lower()
    return "jpeg" if normalized in {"jpg", "jpeg"} else "png"


def get_screenshot_quality(*, user_id: str | None = None) -> int:
    raw = _settings_get("computer_use_screenshot_quality", 80, user_id=user_id)
    try:
        return max(1, min(int(raw), 100))
    except (TypeError, ValueError):
        return 80


def should_log_window_titles(*, user_id: str | None = None) -> bool:
    return bool(_settings_get("computer_use_log_window_titles_enabled", False, user_id=user_id))


def require_enabled(action: str, *, user_id: str | None = None) -> None:
    """Refuse when the user's agent autonomy mode does not include computer use."""
    if not is_computer_use_allowed(user_id=user_id):
        raise ComputerUseRefusal(
            # Error code preserved for client compatibility — UI may key off it.
            error_category="COMPUTER_USE_TIER_REQUIRED",
            reason="Computer use requires the Symphony autonomy mode.",
            action=action,
        )


def normalize_key_chord(key: str) -> str:
    return "+".join(
        _KEY_PART_ALIASES.get(part.strip().lower(), part.strip().lower())
        for part in str(key or "").replace(" ", "").split("+")
        if part.strip()
    )


def _key_chord_parts(key: str) -> frozenset[str]:
    normalized = normalize_key_chord(key)
    return frozenset(part for part in normalized.split("+") if part)


def is_blocked_key(key: str) -> bool:
    """Return True for OS/security keystrokes that must never be sent."""
    normalized = normalize_key_chord(key)
    if normalized in BLOCKED_KEY_CHORDS:
        return True
    parts = _key_chord_parts(normalized)
    if any(blocked_parts <= parts for blocked_parts in _BLOCKED_KEY_PART_SETS):
        return True
    return any(marker in normalized for marker in ("shutdown", "restart", "logoff", "lockworkstation"))


def require_key_allowed(key: str, *, action: str = "key", target_app_executable: str = "") -> None:
    """Refuse unsafe key chords."""
    if is_blocked_key(key):
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_BLOCKED_KEY",
            reason="Refused to send a blocked system keystroke",
            action=action,
            target_app_executable=target_app_executable,
        )


def _key_scope(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> tuple[str, str]:
    owner = _resolve_local_user_id(user_id)
    session = str(session_id or "local-session").strip() or "local-session"
    return owner, session


def _normalize_key_parts(key: str) -> tuple[str, ...]:
    parts = normalize_key_chord(key).split("+")
    return tuple(_KEY_ALIASES.get(part, part) for part in parts if part)


def _format_key_parts(parts: set[str] | frozenset[str]) -> str:
    ordered = [part for part in _KEY_ORDER if part in parts]
    ordered.extend(sorted(part for part in parts if part not in set(_KEY_ORDER)))
    return "+".join(ordered)


def _blocked_key_parts(parts: set[str] | frozenset[str]) -> bool:
    if not parts:
        return False
    return any(is_blocked_key(part) for part in parts) or is_blocked_key(_format_key_parts(parts))


def require_low_level_key_allowed(
    key: str,
    *,
    action: str,
    target_app_executable: str = "",
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Refuse low-level key primitives that directly or cumulatively form a blocked chord."""
    parts = set(_normalize_key_parts(key))
    if not parts:
        return
    if _blocked_key_parts(parts):
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_BLOCKED_KEY",
            reason="Refused to send a blocked system keystroke",
            action=action,
            target_app_executable=target_app_executable,
        )
    if action in {"key_down", "key_hold"}:
        held = _LOW_LEVEL_HELD_KEYS.get(_key_scope(user_id=user_id, session_id=session_id), set())
        if _blocked_key_parts(held | parts):
            raise ComputerUseRefusal(
                error_category="COMPUTER_USE_BLOCKED_KEY",
                reason="Refused to compose a blocked system keystroke from held keys",
                action=action,
                target_app_executable=target_app_executable,
            )


def record_low_level_key_down(
    key: str,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    parts = set(_normalize_key_parts(key))
    if not parts:
        return
    scope = _key_scope(user_id=user_id, session_id=session_id)
    _LOW_LEVEL_HELD_KEYS.setdefault(scope, set()).update(parts)


def record_low_level_key_up(
    key: str,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    parts = set(_normalize_key_parts(key))
    if not parts:
        return
    scope = _key_scope(user_id=user_id, session_id=session_id)
    held = _LOW_LEVEL_HELD_KEYS.get(scope)
    if held is None:
        return
    held.difference_update(parts)
    if not held:
        _LOW_LEVEL_HELD_KEYS.pop(scope, None)


def clear_low_level_held_keys(*, user_id: str | None = None, session_id: str | None = None) -> None:
    if user_id is None and session_id is None:
        _LOW_LEVEL_HELD_KEYS.clear()
        return
    _LOW_LEVEL_HELD_KEYS.pop(_key_scope(user_id=user_id, session_id=session_id), None)


def require_text_allowed(
    text: str,
    *,
    action: str = "type",
    target_app_executable: str = "",
    user_id: str | None = None,
) -> None:
    """Refuse overlong text entry."""
    max_chars = get_max_chars_per_type(user_id=user_id)
    if len(text) > max_chars:
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_TEXT_TOO_LONG",
            reason="Refused to type more than %d characters in one action" % max_chars,
            action=action,
            target_app_executable=target_app_executable,
        )


def is_uac_window(window_info: Mapping[str, Any] | Any | None) -> bool:
    """Return True when window metadata looks like UAC/elevation UI."""
    if window_info is None:
        return False
    if isinstance(window_info, Mapping):
        executable = normalize_executable_name(str(window_info.get("executable", "")))
        class_name = str(window_info.get("class_name", "") or "").lower()
    else:
        executable = normalize_executable_name(str(getattr(window_info, "executable", "")))
        class_name = str(getattr(window_info, "class_name", "") or "").lower()
    return executable in UAC_EXECUTABLES or any(marker in class_name for marker in UAC_CLASS_MARKERS)


def require_not_uac(action: str, *, target_app_executable: str = "") -> None:
    """Refuse actions when the foreground window is UAC/elevation UI."""
    try:
        from services.computer_use.window_manager import get_foreground_window_info

        foreground = get_foreground_window_info()
    except Exception:
        logger.debug("Could not inspect foreground window for UAC")
        foreground = None
    if is_uac_window(foreground):
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_UAC_REFUSED",
            reason="Refused to interact with a UAC or credential dialog",
            action=action,
            target_app_executable=target_app_executable,
        )


def _get_attr_text(obj: Any, attr_names: tuple[str, ...]) -> str:
    values: list[str] = []
    for attr_name in attr_names:
        value = getattr(obj, attr_name, "")
        if callable(value):
            try:
                value = value()
            except Exception:
                value = ""
        if value:
            values.append(str(value))
    return " ".join(values).lower()


def focused_control_password_like(focused_control: Any | None) -> bool:
    """Best-effort password-field heuristic for a focused UIA control."""
    if focused_control is None:
        return False
    element_info = getattr(focused_control, "element_info", focused_control)
    if bool(getattr(element_info, "is_password", False) or getattr(focused_control, "is_password", False)):
        return True
    metadata = _get_attr_text(
        element_info,
        (
            "name",
            "class_name",
            "control_type",
            "localized_control_type",
            "automation_id",
        ),
    )
    if any(marker in metadata for marker in PASSWORD_MARKERS):
        return True
    value_text = _get_attr_text(focused_control, ("window_text", "texts"))
    return any(marker in value_text for marker in OBSCURED_TEXT_MARKERS)


def detect_password_like_focus(focused_control: Any | None = None) -> bool:
    """Return True when the current focused control looks password-like."""
    if focused_control is not None:
        return focused_control_password_like(focused_control)
    try:
        import importlib

        Desktop = importlib.import_module("pywinauto").Desktop
        desktop = Desktop(backend="uia")
        getter = getattr(desktop, "get_focus", None) or getattr(desktop, "get_focused", None)
        if getter is None:
            return False
        return focused_control_password_like(getter())
    except Exception:
        logger.debug("Could not inspect focused control for password heuristic")
        return False


def require_not_password_field(
    *,
    action: str = "type",
    target_app_executable: str = "",
    focused_control: Any | None = None,
) -> None:
    """Refuse typing into password-like focused controls."""
    if detect_password_like_focus(focused_control):
        raise ComputerUseRefusal(
            error_category="COMPUTER_USE_PASSWORD_FIELD_REFUSED",
            reason="Refused to type into a password-like field",
            action=action,
            target_app_executable=target_app_executable,
        )


def action_chain_status(action_count: int, *, user_id: str | None = None) -> dict[str, object]:
    """Per-chain action count is informational only; no cap is enforced."""
    del action_count, user_id
    return {"ok": True}
