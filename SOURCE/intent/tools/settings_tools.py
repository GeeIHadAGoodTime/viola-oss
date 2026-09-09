"""Agent tool handlers for user-configurable settings.

These tools let the LLM ACTUALLY change Viola's runtime settings (wake
sensitivity, voice mode, quiet hours, music provider, etc.) by writing
to ``ui.settings_manager.SettingsManager``. They are distinct from
``intent/tools/memory.py``: the memory tool stores arbitrary facts that
inform future replies but does NOT change Viola's behaviour. The settings
tool changes how Viola operates and persists to ``settings.json`` (or to
the per-user preferences DB on multi-tenant cloud).

Voice flow example:
    User: "set wake sensitivity to 0.85"
    LLM picks user_settings(action="set", key="wake_sensitivity", value="0.85")
    Tool validates 0 <= 0.85 <= 1.0, calls SettingsManager.set(...),
    then applies the change through ``ui.settings_effects`` and reports
    whether it actually took effect.

Saving is not the same as applying, and this is the seam where the two used
to come apart: the Settings UI persisted AND applied, while this tool only
persisted and returned ok, so Viola confirmed changes that never reached the
subsystem they named. Every key on ``_ADJUSTABLE_SETTINGS`` therefore has to
declare, in ``ui/settings_effects.py``, how its value becomes real, and
``scripts/check_agent_settings_have_effect.py`` refuses a key that does not.
The rule the whole module exists to keep: no confirmation without effect.
"""

from __future__ import annotations

import re

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Allowlist of voice-adjustable settings.
#
# Each entry describes one setting the LLM is allowed to modify on
# behalf of the user via natural-language commands. Anything not on
# this list MUST be edited through the Settings UI — we keep the agent
# surface tight to avoid plan/auth/billing escalation, system-key
# tampering, or accidental wipes of advanced configuration.
#
# Each spec carries:
#   - kind: "float" | "int" | "bool" | "enum" | "string" | "time" | "music_provider"
#   - min/max for numeric kinds (inclusive)
#   - choices for enum kinds (frozenset of valid values, all lowercase)
#   - aliases mapped to canonical values for fuzzy enum matching
#   - description: short human-readable summary used in the tool's
#     "list_adjustable" action so the LLM can pick the right key.
# ──────────────────────────────────────────────────────────────────────

_VOICE_MODE_ALIASES: dict[str, str] = {
    "off": "disabled",
    "disable": "disabled",
    "disabled": "disabled",
    "push to talk": "push_to_talk",
    "push-to-talk": "push_to_talk",
    "push_to_talk": "push_to_talk",
    "ptt": "push_to_talk",
    "wake word": "wake_word",
    "wake-word": "wake_word",
    "wake_word": "wake_word",
    "hotword": "wake_word",
}

_THEME_ALIASES: dict[str, str] = {
    "dark": "dark",
    "light": "light",
    "system": "system",
    "auto": "system",
}

_TIME_FORMAT_ALIASES: dict[str, str] = {
    "auto": "auto",
    "12": "12h",
    "12h": "12h",
    "12-hour": "12h",
    "12 hour": "12h",
    "12hr": "12h",
    "twelve": "12h",
    "24": "24h",
    "24h": "24h",
    "24-hour": "24h",
    "24 hour": "24h",
    "24hr": "24h",
    "twenty-four": "24h",
    "military": "24h",
}

_MUSIC_PROVIDER_ALIASES: dict[str, str | None] = {
    # Canonical
    "spotify": "spotify",
    "youtube_music": "youtube_music",
    "youtube_iframe": "youtube_iframe",
    "local": "local",
    "browser": "browser",
    # Friendly
    "youtube music": "youtube_music",
    "youtube": "youtube_music",
    "youtube iframe": "youtube_iframe",
    "iframe": "youtube_iframe",
    "local library": "local",
    "local music": "local",
    "library": "local",
    # Off / clear
    "none": None,
    "clear": None,
    "off": None,
    "disable": None,
    "disabled": None,
    "no provider": None,
}


# Setting specifications. Order matters for the human-readable list output.
_ADJUSTABLE_SETTINGS: dict[str, dict[str, object]] = {
    "wake_sensitivity": {
        "kind": "float",
        "min": 0.0,
        "max": 1.0,
        "description": (
            "How sensitive the wake-word detector is. 0.0 is least sensitive "
            "(never triggers), 1.0 is most sensitive (may have false wakes)."
        ),
    },
    "voice_mode": {
        "kind": "enum",
        "choices": frozenset({"disabled", "push_to_talk", "wake_word"}),
        "aliases": _VOICE_MODE_ALIASES,
        "description": (
            "How Viola listens for commands. 'wake_word' listens for the wake "
            "word continuously, 'push_to_talk' requires holding a hotkey, "
            "'disabled' turns voice input off."
        ),
    },
    "quiet_hours_enabled": {
        "kind": "bool",
        "description": "Master toggle for quiet hours (suppresses TTS and notifications).",
    },
    "quiet_hours_start": {
        "kind": "time",
        "description": "Start of quiet hours window in 24-hour HH:MM format (e.g. 22:00).",
    },
    "quiet_hours_end": {
        "kind": "time",
        "description": "End of quiet hours window in 24-hour HH:MM format (e.g. 07:00).",
    },
    "active_music_provider_id": {
        "kind": "music_provider",
        "aliases": _MUSIC_PROVIDER_ALIASES,
        "description": (
            "Which music provider Viola uses by default. Valid: spotify, "
            "youtube_music, youtube_iframe, local, browser. Pass 'none' "
            "or 'clear' to unset."
        ),
    },
    "tts_volume": {
        "kind": "float",
        "min": 0.0,
        "max": 1.0,
        "description": "Text-to-speech volume from 0.0 (silent) to 1.0 (full volume).",
    },
    "tts_rate": {
        "kind": "int",
        "min": 50,
        "max": 400,
        "description": "Text-to-speech speaking rate in words per minute (typical range 100-250).",
    },
    # NOTE: microphone_volume and speaker_volume used to sit here. Nothing in
    # Viola has ever read either one — no capture stage applies mic gain and
    # Viola does not control the operating system's master output level — so
    # setting them by voice stored a number, confirmed it, and changed nothing
    # the user could hear. They are off the agent's surface until something
    # actually consumes them; ui/settings_effects.py is where that would be
    # declared, and scripts/check_agent_settings_have_effect.py refuses to let
    # a key back onto this list without it.
    "default_music_volume": {
        "kind": "int",
        "min": 0,
        "max": 100,
        "description": "Default music playback volume (0-100).",
    },
    "theme": {
        "kind": "enum",
        "choices": frozenset({"dark", "light", "system"}),
        "aliases": _THEME_ALIASES,
        "description": "UI colour theme: dark, light, or system (follow OS).",
    },
    "time_display_format": {
        "kind": "enum",
        "choices": frozenset({"auto", "12h", "24h"}),
        "aliases": _TIME_FORMAT_ALIASES,
        "description": "Clock display format: auto (locale-based), 12h, or 24h.",
    },
    "locale": {
        "kind": "string",
        "max_len": 16,
        "pattern": re.compile(r"^[a-zA-Z]{2,3}(?:[-_][a-zA-Z]{2,4})?$"),
        "description": "BCP-47 locale identifier such as 'en-US' or 'fr-FR'.",
    },
    "weather_location": {
        "kind": "string",
        "max_len": 200,
        # Cities can include letters, spaces, commas, apostrophes, hyphens, periods.
        "pattern": re.compile(r"^[\w\s,.\-'/]+$", re.UNICODE),
        "description": "Default weather location, e.g. 'Milwaukee, WI' or 'Tokyo'.",
    },
    "calendar_reminders_enabled": {
        "kind": "bool",
        "description": "Speak and push a reminder ahead of each upcoming calendar event.",
    },
    "calendar_reminder_lead_minutes": {
        "kind": "int",
        "min": 1,
        "max": 180,
        "description": "How many minutes before a calendar event Viola sends the reminder.",
    },
    # NOTE: allow_explicit used to sit here. Nothing filters on it: no music
    # provider ever reports a track as explicit (every one of them hardcodes
    # is_explicit=False, and is_explicit=True appears nowhere in the tree), so
    # the flag was stored, confirmed, and could not have changed which tracks
    # played even if a filter existed. Wiring a filter against a field that is
    # always False would be the same failure wearing a fix. It comes back to
    # this list when providers actually supply explicitness.
    "autoplay_enabled": {
        "kind": "bool",
        "description": "Continue playing similar tracks when a queue runs out.",
    },
    "ai_autoplay_enabled": {
        "kind": "bool",
        "description": "Use the LLM to pick autoplay continuations (vs. provider-supplied).",
    },
    "autoplay_min_queue": {
        "kind": "int",
        "min": 1,
        "max": 50,
        "description": "Trigger autoplay refill when fewer than this many tracks remain queued.",
    },
    "speak_all_replies": {
        "kind": "bool",
        "description": "Read every assistant reply aloud (when off, only voice-initiated replies are spoken).",
    },
    "voice_muted": {
        "kind": "bool",
        "description": "Mute Viola's voice output entirely (TTS off).",
    },
    "show_notifications": {
        "kind": "bool",
        "description": "Show desktop notifications for events (timers, alerts).",
    },
    "minimize_to_tray": {
        "kind": "bool",
        "description": "When closing the main window, minimize to system tray instead of exiting.",
    },
    "start_on_boot": {
        "kind": "bool",
        "description": "Launch Viola automatically when the OS starts.",
    },
}

_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")
_BOOL_TRUE = frozenset({"1", "true", "yes", "y", "on", "enable", "enabled"})
_BOOL_FALSE = frozenset({"0", "false", "no", "n", "off", "disable", "disabled"})


def _is_adjustable(key: str) -> bool:
    return key in _ADJUSTABLE_SETTINGS


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        # Reject bools — they'd silently coerce to 0.0/1.0.
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                f = float(text)
                if f.is_integer():
                    return int(f)
            except ValueError:
                return None
    return None


def _validate_time(value: object) -> tuple[str | None, str | None]:
    """Return (canonical HH:MM string, error message)."""
    if not isinstance(value, str):
        return None, "value must be a string in HH:MM format"
    text = value.strip()
    match = _TIME_PATTERN.match(text)
    if not match:
        return None, "value must match HH:MM (e.g. '22:00' or '7:30')"
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23:
        return None, "hour must be 0-23"
    if minute < 0 or minute > 59:
        return None, "minute must be 0-59"
    return "%02d:%02d" % (hour, minute), None


def _resolve_alias(value: object, aliases: dict[str, object]) -> object:
    """Look up *value* in *aliases* (case-insensitive). Returns the
    canonical value or the original input if no alias matches."""
    if not isinstance(value, str):
        return value
    text = value.strip().lower()
    if text in aliases:
        return aliases[text]
    return value


def _validate_value(key: str, value: object) -> tuple[object, str | None]:
    """Validate and coerce *value* for setting *key*.

    Returns (canonical_value, error_message). On error the canonical
    value is None and error_message describes what was wrong.
    """
    spec = _ADJUSTABLE_SETTINGS[key]
    kind = spec["kind"]

    if kind == "float":
        coerced = _coerce_float(value)
        if coerced is None:
            return None, "value must be a number"
        lo = float(spec.get("min", float("-inf")))
        hi = float(spec.get("max", float("inf")))
        if coerced < lo or coerced > hi:
            return None, "value must be between %g and %g (got %g)" % (lo, hi, coerced)
        return coerced, None

    if kind == "int":
        coerced = _coerce_int(value)
        if coerced is None:
            return None, "value must be an integer"
        lo = int(spec.get("min", -(2**31)))
        hi = int(spec.get("max", 2**31 - 1))
        if coerced < lo or coerced > hi:
            return None, "value must be between %d and %d (got %d)" % (lo, hi, coerced)
        return coerced, None

    if kind == "bool":
        coerced = _coerce_bool(value)
        if coerced is None:
            return None, "value must be a boolean (true/false, on/off, yes/no, 1/0)"
        return coerced, None

    if kind == "enum":
        choices = spec["choices"]
        aliases = spec.get("aliases", {})
        resolved = _resolve_alias(value, aliases) if aliases else value
        if not isinstance(resolved, str):
            return None, "value must be one of: %s" % ", ".join(sorted(choices))
        canonical = resolved.strip().lower()
        if canonical not in choices:
            return None, "value must be one of: %s (got %r)" % (
                ", ".join(sorted(choices)),
                value,
            )
        return canonical, None

    if kind == "music_provider":
        aliases = spec.get("aliases", {})
        if value is None or (isinstance(value, str) and value.strip().lower() in {"none", "null", "clear"}):
            return None, None  # explicit "unset"
        resolved = _resolve_alias(value, aliases)
        if resolved is None:
            return None, None
        if isinstance(resolved, str):
            canonical = resolved.strip().lower()
            valid_choices = {v for v in aliases.values() if isinstance(v, str)}
            if canonical not in valid_choices:
                return None, "unknown music provider %r. Valid: %s" % (
                    value,
                    ", ".join(sorted(valid_choices)),
                )
            return canonical, None
        return None, "value must be a provider id string or 'none'"

    if kind == "time":
        return _validate_time(value)

    if kind == "string":
        if not isinstance(value, str):
            return None, "value must be a string"
        text = value.strip()
        if not text:
            return None, "value must not be empty"
        max_len = int(spec.get("max_len", 200))
        if len(text) > max_len:
            return None, "value exceeds maximum length of %d characters" % max_len
        pattern = spec.get("pattern")
        if pattern is not None and not pattern.match(text):
            return None, "value contains disallowed characters"
        return text, None

    return None, "internal error: unknown setting kind %r" % kind


def _resolve_user_id(user_id: str | None) -> str:
    if user_id and user_id.strip():
        return user_id.strip()
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError:
        from core.user_context import get_current_or_device_user_id

        return get_current_or_device_user_id()


# ──────────────────────────────────────────────────────────────────────
# Public handlers
# ──────────────────────────────────────────────────────────────────────


async def settings_get_handler(key: str, user_id: str = "") -> ToolResult:
    """Read the current value of an adjustable user setting."""
    cleaned_key = (key or "").strip()
    if not cleaned_key:
        return ToolResult(ok=False, data=None, error="key is required")
    if not _is_adjustable(cleaned_key):
        return ToolResult(
            ok=False,
            data=None,
            error=(
                "Setting %r is not voice-adjustable. Use the Settings UI for "
                "advanced configuration. Adjustable keys: %s" % (cleaned_key, ", ".join(sorted(_ADJUSTABLE_SETTINGS)))
            ),
        )

    resolved_user_id = _resolve_user_id(user_id)
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        value = sm.get(cleaned_key, None, user_id=resolved_user_id)
    except Exception as exc:
        logger.exception("settings_get_handler failed for key=%s", cleaned_key)
        return ToolResult(ok=False, data=None, error="failed to read setting: %s" % exc)

    return ToolResult(
        ok=True,
        data={
            "key": cleaned_key,
            "value": value,
            "description": _ADJUSTABLE_SETTINGS[cleaned_key]["description"],
        },
    )


async def settings_set_handler(key: str, value: object, user_id: str = "") -> ToolResult:
    """Validate *value* and persist it to ``SettingsManager`` under *key*.

    Returns a structured ToolResult; on success ``data`` includes the
    canonical (post-coercion) value so the LLM can confirm exactly what
    was applied.
    """
    cleaned_key = (key or "").strip()
    if not cleaned_key:
        return ToolResult(ok=False, data=None, error="key is required")
    if not _is_adjustable(cleaned_key):
        return ToolResult(
            ok=False,
            data=None,
            error=(
                "Setting %r is not voice-adjustable. Use the Settings UI for "
                "advanced configuration. Adjustable keys: %s" % (cleaned_key, ", ".join(sorted(_ADJUSTABLE_SETTINGS)))
            ),
        )

    canonical, err = _validate_value(cleaned_key, value)
    if err is not None:
        return ToolResult(
            ok=False,
            data={"key": cleaned_key, "rejected_value": value},
            error="invalid value for %s: %s" % (cleaned_key, err),
        )

    resolved_user_id = _resolve_user_id(user_id)
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        # Read prior value for confirmation context.
        try:
            previous = sm.get(cleaned_key, None, user_id=resolved_user_id)
        except Exception:
            previous = None

        # Use set_user_setting so multi-tenant cloud goes to per-user prefs
        # and desktop device/session users go to settings.json. SettingsManager
        # routes correctly based on _uses_global_settings_only(user_id).
        ok = sm.set_user_setting(resolved_user_id, cleaned_key, canonical, save_immediately=True)
    except Exception as exc:
        logger.exception("settings_set_handler failed for key=%s", cleaned_key)
        return ToolResult(ok=False, data=None, error="failed to persist setting: %s" % exc)

    if not ok:
        return ToolResult(
            ok=False,
            data=None,
            error="settings manager rejected the write (system key or storage failure)",
        )

    # Persisting is not the same as applying. Some settings are inert until
    # something re-arms the subsystem they name (the wake detector, the OS
    # auto-start entry, the music provider), and this used to be the seam
    # where the voice path diverged from the Settings UI: the UI persisted
    # AND applied, the agent only persisted and then reported success. Run
    # the same apply the UI runs, and report what actually happened so the
    # confirmation the user hears matches reality.
    from ui.settings_effects import (
        EffectOutcome,
        apply_setting_effect,
        broadcast_settings_changed,
    )

    effect = apply_setting_effect(
        cleaned_key,
        canonical,
        settings_mgr=sm,
        previous=previous,
    )

    # An open window holds its own copy of the settings and only replaces it
    # when this message arrives, so without it a voice-set theme or clock
    # format is saved and confirmed while the screen keeps showing the old one.
    # The REST path has always sent this; the voice path never did.
    await broadcast_settings_changed(sm, user_id=resolved_user_id)

    if effect.outcome is EffectOutcome.FAILED:
        return ToolResult(
            ok=False,
            data={"key": cleaned_key, "value": canonical, "previous_value": previous},
            error=(
                "%s was saved to settings but could not be applied, so the change is not in effect: %s"
                % (cleaned_key, effect.detail)
            ),
        )

    return ToolResult(
        ok=True,
        data={
            "key": cleaned_key,
            "value": canonical,
            "previous_value": previous,
            "description": _ADJUSTABLE_SETTINGS[cleaned_key]["description"],
            "in_effect": effect.took_effect,
            "effect": effect.as_dict(),
        },
    )


async def settings_list_adjustable_handler(user_id: str = "") -> ToolResult:
    """List the voice-adjustable settings and their current values."""
    resolved_user_id = _resolve_user_id(user_id)
    out: list[dict[str, object]] = []
    try:
        from ui.settings_effects import get_setting_effect
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        for setting_key, spec in _ADJUSTABLE_SETTINGS.items():
            try:
                current = sm.get(setting_key, None, user_id=resolved_user_id)
            except Exception:
                current = None
            entry: dict[str, object] = {
                "key": setting_key,
                "kind": spec["kind"],
                "description": spec["description"],
                "current": current,
            }
            # What the value actually changes at runtime. Every adjustable
            # key has a declared effect (enforced by
            # scripts/check_agent_settings_have_effect.py), so this is the
            # real capability surface rather than a restatement of the name.
            declared = get_setting_effect(setting_key)
            if declared is not None:
                entry["controls"] = declared.controls
            if "min" in spec:
                entry["min"] = spec["min"]
            if "max" in spec:
                entry["max"] = spec["max"]
            if "choices" in spec:
                entry["choices"] = sorted(spec["choices"])
            out.append(entry)
    except Exception as exc:
        logger.exception("settings_list_adjustable_handler failed")
        return ToolResult(ok=False, data=None, error="failed to enumerate settings: %s" % exc)

    return ToolResult(ok=True, data={"settings": out, "count": len(out)})


__all__ = [
    "settings_get_handler",
    "settings_list_adjustable_handler",
    "settings_set_handler",
]
