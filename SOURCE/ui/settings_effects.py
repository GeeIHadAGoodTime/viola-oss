"""Runtime effects for user settings — the single place that turns a
*persisted* setting into *real* behaviour.

Persisting a setting and applying it are two different things, and for a
while only one of Viola's two write paths did both:

* ``ui/settings_api.py`` (the Settings UI, over REST) persisted and then ran
  an apply block — re-gate the wake detector, register OS auto-start, switch
  the music provider.
* ``intent/tools/settings_tools.py`` (the voice path — Viola's own agent)
  only persisted, then told the user the change was made.

So "Viola, switch to wake word mode" wrote ``voice_mode=wake_word`` to
settings.json, answered "done", and left the wake detector stopped, because
``WakeDetectorFacade.sync_to_state`` — the only code that starts/stops it on
a mode change — was never called. Same shape for auto-start and for the
music provider. The user got a confident confirmation and nothing changed.

This module removes that asymmetry by making the apply step a property of
the *setting*, not of the *caller*. Every setting Viola's agent may change
declares here how its value becomes real:

* ``EffectKind.LIVE`` — the runtime re-reads SettingsManager at the moment
  it uses the value, so persisting IS applying. Nothing to call.
* ``EffectKind.APPLY`` — a change is inert until an apply function runs.
  The apply function lives here and both write paths call it.

Both write paths call :func:`apply_setting_effects` after persisting, and
the result says what actually happened per key, so a caller can report the
truth instead of assuming success. ``scripts/check_agent_settings_have_effect.py``
holds the invariant mechanically: a setting exposed to the agent with no
declared effect here cannot ship.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class EffectKind(str, Enum):
    """How a persisted value becomes real behaviour."""

    LIVE = "live"
    """The runtime reads SettingsManager when it uses the value. Persisting
    is applying; there is nothing to call."""

    APPLY = "apply"
    """The change stays inert until this module's apply function runs."""


class EffectOutcome(str, Enum):
    """What actually happened to a setting change. Callers report this to
    the user rather than assuming the write took effect."""

    LIVE = "live"
    """No apply needed — the runtime picks the new value up on next use."""

    APPLIED = "applied"
    """An apply function ran and the change took hold in this process."""

    DEFERRED = "deferred"
    """There was nothing here to apply it to — e.g. the setting was changed
    from the cloud, or the desktop subsystem it drives is not running in
    this process. The value is saved and applies on the device."""

    FAILED = "failed"
    """The apply function ran and failed. The change did NOT take effect."""


@dataclass(frozen=True)
class EffectResult:
    """Per-key outcome of applying one setting change."""

    key: str
    outcome: EffectOutcome
    detail: str = ""

    @property
    def took_effect(self) -> bool:
        """True when the change is actually in force right now."""
        return self.outcome in (EffectOutcome.LIVE, EffectOutcome.APPLIED)

    def as_dict(self) -> dict[str, object]:
        return {"key": self.key, "outcome": self.outcome.value, "detail": self.detail}


@dataclass(frozen=True)
class SettingEffect:
    """Declares how one setting reaches the runtime.

    ``controls`` is a plain-English statement of what the value actually
    changes. It exists so the declaration is reviewable by a human: if
    nobody can write this sentence truthfully, the setting does not belong
    on the agent's surface.
    """

    kind: EffectKind
    controls: str
    apply: Callable[..., EffectResult] | None = None


# ──────────────────────────────────────────────────────────────────────
# Apply functions
#
# Each one is the single implementation shared by every write path. They
# never raise: an apply that fails returns FAILED so the caller can tell
# the user the truth instead of surfacing a stack trace.
# ──────────────────────────────────────────────────────────────────────


def _apply_voice_mode(key: str, value: object, *, settings_mgr: Any, **_: object) -> EffectResult:
    """Start/stop the wake detector so it matches the new listening mode.

    Without this, changing ``voice_mode`` only rewrites settings.json: the
    detector keeps running (or keeps not running) until Viola restarts.
    """
    try:
        from voice.wake_detector.facade import WakeDetectorFacade
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        return EffectResult(key, EffectOutcome.DEFERRED, "wake detector unavailable here: %s" % exc)

    if WakeDetectorFacade.get_instance() is None:
        return EffectResult(key, EffectOutcome.DEFERRED, "no wake detector running in this process")

    try:
        mic_muted = bool(settings_mgr.get("mic_muted", False))
        WakeDetectorFacade.sync_to_state(str(value), mic_muted)
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        logger.exception("Failed to sync wake detector to voice_mode=%s", value)
        return EffectResult(key, EffectOutcome.FAILED, "could not re-arm the wake detector: %s" % exc)

    return EffectResult(key, EffectOutcome.APPLIED, "wake detector synced to voice_mode=%s" % value)


def _apply_start_on_boot(key: str, value: object, **_: object) -> EffectResult:
    """Register or clear the OS auto-start entry.

    Without this, ``start_on_boot`` is a flag in settings.json that no
    operating system ever reads.
    """
    try:
        from utils.autostart import set_autostart
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        return EffectResult(key, EffectOutcome.DEFERRED, "auto-start unavailable here: %s" % exc)

    try:
        ok = set_autostart(bool(value))
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        logger.exception("Failed to set auto-start to %s", value)
        return EffectResult(key, EffectOutcome.FAILED, "could not update auto-start: %s" % exc)

    if not ok:
        return EffectResult(key, EffectOutcome.FAILED, "the operating system refused the auto-start change")
    return EffectResult(key, EffectOutcome.APPLIED, "auto-start set to %s" % bool(value))


def _apply_wake_sensitivity(key: str, value: object, **_: object) -> EffectResult:
    """Point the running wake policy at the new threshold.

    The policy builds its config once from AppConfig, so without this the new
    sensitivity would not be consulted until the next restart.
    """
    try:
        from voice.wake_detector.wake_decision_policy import set_wake_sensitivity
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        return EffectResult(key, EffectOutcome.DEFERRED, "wake policy unavailable here: %s" % exc)

    try:
        updated = set_wake_sensitivity(float(value))
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        logger.exception("Failed to set wake sensitivity to %s", value)
        return EffectResult(key, EffectOutcome.FAILED, "could not update wake sensitivity: %s" % exc)

    if not updated:
        return EffectResult(
            key,
            EffectOutcome.DEFERRED,
            "saved; the wake detector picks it up when it next starts",
        )
    return EffectResult(key, EffectOutcome.APPLIED, "wake threshold now %s" % value)


def _apply_tts_rate(key: str, value: object, **_: object) -> EffectResult:
    """Re-speed the running speech engine.

    The engine reads its speed once at construction, so a new rate would
    otherwise sit in settings.json while Viola kept talking at the old pace.
    """
    try:
        from voice.synthesis import factory as tts_factory
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        return EffectResult(key, EffectOutcome.DEFERRED, "speech engine unavailable here: %s" % exc)

    # Read the module global rather than calling get_shared_kokoro(), which
    # would build an engine as a side effect of changing a setting.
    engine = getattr(tts_factory, "_shared_kokoro", None)
    if engine is None:
        return EffectResult(
            key,
            EffectOutcome.DEFERRED,
            "saved; it applies when Viola's voice next starts up",
        )

    try:
        rate = float(value)
        if rate <= 5:
            return EffectResult(key, EffectOutcome.FAILED, "speaking rate %s is out of range" % value)
        # tts_rate is words per minute against a 150 wpm baseline; the engine
        # holds it as a speed multiplier and reads it on every utterance.
        engine._speed = rate / 150.0
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        logger.exception("Failed to set speaking rate to %s", value)
        return EffectResult(key, EffectOutcome.FAILED, "could not update speaking rate: %s" % exc)

    return EffectResult(key, EffectOutcome.APPLIED, "speaking rate now %s words per minute" % value)


def _apply_music_provider(
    key: str,
    value: object,
    *,
    previous: object = None,
    music_service: object | None = None,
    **_: object,
) -> EffectResult:
    """Stop playback and clear the queue so the old provider's items do not
    leak into the new provider's session."""
    if previous == value:
        return EffectResult(key, EffectOutcome.APPLIED, "provider unchanged")

    try:
        from music.providers.active_provider import handle_provider_switch
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        return EffectResult(key, EffectOutcome.DEFERRED, "music runtime unavailable here: %s" % exc)

    try:
        handle_provider_switch(
            previous if isinstance(previous, str) or previous is None else str(previous),
            value if isinstance(value, str) or value is None else str(value),
            music_service=music_service,
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - an apply must report its outcome, never raise into the caller
        logger.exception("Failed to handle music provider switch to %s", value)
        return EffectResult(key, EffectOutcome.FAILED, "could not switch music provider: %s" % exc)

    # The provider registry reads this key live, so the switch itself is in
    # force either way. ``music_service`` only decides whether the previous
    # provider's playback could also be stopped here.
    if music_service is None:
        return EffectResult(
            key,
            EffectOutcome.APPLIED,
            "music provider switched to %s; no player in this process, so nothing was stopped" % value,
        )
    return EffectResult(
        key,
        EffectOutcome.APPLIED,
        "music provider switched to %s and the previous provider's playback was stopped" % value,
    )


# ──────────────────────────────────────────────────────────────────────
# The registry
#
# Every key the agent may set (intent/tools/settings_tools.py
# _ADJUSTABLE_SETTINGS) MUST appear here, and the gate enforces it. A LIVE
# entry additionally has to have a real runtime reader somewhere outside
# the settings plumbing — that is what stops a key from being stored
# correctly and read by nothing.
# ──────────────────────────────────────────────────────────────────────

SETTING_EFFECTS: dict[str, SettingEffect] = {
    "voice_mode": SettingEffect(
        kind=EffectKind.APPLY,
        controls="Starts or stops the wake-word detector to match the listening mode.",
        apply=_apply_voice_mode,
    ),
    "start_on_boot": SettingEffect(
        kind=EffectKind.APPLY,
        controls="Registers or clears the operating system's auto-start entry for Viola.",
        apply=_apply_start_on_boot,
    ),
    "active_music_provider_id": SettingEffect(
        kind=EffectKind.APPLY,
        controls="Selects the music provider and clears the previous provider's playback queue.",
        apply=_apply_music_provider,
    ),
    "wake_sensitivity": SettingEffect(
        kind=EffectKind.APPLY,
        controls="Threshold the wake-word detector compares each detection score against.",
        apply=_apply_wake_sensitivity,
    ),
    "quiet_hours_enabled": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the quiet-hours window suppresses spoken output and notifications.",
    ),
    "quiet_hours_start": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Start of the quiet-hours window, read when quiet hours are evaluated.",
    ),
    "quiet_hours_end": SettingEffect(
        kind=EffectKind.LIVE,
        controls="End of the quiet-hours window, read when quiet hours are evaluated.",
    ),
    "tts_volume": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Loudness the speech engine renders Viola's replies at.",
    ),
    "tts_rate": SettingEffect(
        kind=EffectKind.APPLY,
        controls="Speaking rate the speech engine renders Viola's replies at.",
        apply=_apply_tts_rate,
    ),
    "default_music_volume": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Volume the music player starts new playback at (music/player/initializer.py).",
    ),
    "theme": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Colour theme the interface renders in.",
    ),
    "time_display_format": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Clock format the interface renders times in.",
    ),
    "locale": SettingEffect(
        kind=EffectKind.LIVE,
        # Deliberately narrow wording. The only reader is
        # utils/self_knowledge.py, which puts the value in the model's context
        # each turn. No date, time or number formatter reads it - every
        # formatter in the front end passes its own locale - so describing this
        # as "formats dates and times" would be the same overclaim this module
        # exists to stop.
        controls="Locale Viola is told the user is in, used as context when it answers.",
    ),
    "weather_location": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Location weather lookups default to.",
    ),
    "calendar_reminders_enabled": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the reminder scheduler announces upcoming calendar events.",
    ),
    "calendar_reminder_lead_minutes": SettingEffect(
        kind=EffectKind.LIVE,
        controls="How far ahead of an event the reminder scheduler fires.",
    ),
    "autoplay_enabled": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the autoplay controller refills the queue when it runs low.",
    ),
    "ai_autoplay_enabled": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether autoplay picks continuations with the LLM instead of the provider's own suggestions.",
    ),
    "autoplay_min_queue": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Queue length below which the autoplay controller refills.",
    ),
    "speak_all_replies": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the command route speaks every reply or only voice-initiated ones.",
    ),
    "voice_muted": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the command route suppresses spoken output entirely.",
    ),
    "show_notifications": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether the desktop notifier raises notifications for timers and alerts.",
    ),
    "minimize_to_tray": SettingEffect(
        kind=EffectKind.LIVE,
        controls="Whether closing the main window hides Viola to the system tray instead of exiting.",
    ),
}


def get_setting_effect(key: str) -> SettingEffect | None:
    """Return the declared effect for *key*, or None when the key has none."""
    return SETTING_EFFECTS.get(key)


async def broadcast_settings_changed(settings_mgr: Any, *, user_id: str | None = None) -> bool:
    """Tell an open interface that settings changed, so it repaints.

    The React front end holds settings in state and only replaces them on a
    ``settings_changed`` WebSocket message (``useSettings.js``). That message
    was only ever sent by the REST write path (``ui/settings_api.py``), so a
    setting changed by voice repainted nothing: "switch to dark theme" was
    saved and confirmed while the open window stayed light until the user
    reloaded or opened and closed the Settings panel.

    Desktop only, and deliberately so: this repaints the local window, and
    ``settings_mgr.settings`` is the effective view only for a device user.
    Returns True when a message went out.
    """
    try:
        from ui.core.security import is_desktop_surface

        if not is_desktop_surface():
            return False

        from ui.websocket.event_hub import get_event_hub

        hub = get_event_hub()
        if hub is None:
            return False

        # Reuse the REST path's payload builder so the shape the front end
        # receives is identical, and so secrets go through the same redaction.
        from ui.settings_api import _settings_response_payload

        payload = _settings_response_payload(dict(settings_mgr.settings))
        await hub.broadcast("settings_changed", payload, user_id=user_id, force=True)
    except Exception:  # noqa: BLE001, RUF100 - a repaint hint must never break the write that succeeded
        logger.debug("Could not broadcast settings_changed", exc_info=True)
        return False

    return True


def apply_setting_effect(
    key: str,
    value: object,
    *,
    settings_mgr: Any,
    previous: object = None,
    music_service: object | None = None,
) -> EffectResult:
    """Make one persisted setting real, and report what actually happened.

    Never raises — an unknown key or a failing apply comes back as a
    FAILED/DEFERRED result so the caller can tell the user the truth.
    """
    effect = SETTING_EFFECTS.get(key)
    if effect is None:
        return EffectResult(
            key,
            EffectOutcome.FAILED,
            "no runtime effect is declared for this setting",
        )

    if effect.kind is EffectKind.LIVE or effect.apply is None:
        return EffectResult(key, EffectOutcome.LIVE, effect.controls)

    return effect.apply(
        key,
        value,
        settings_mgr=settings_mgr,
        previous=previous,
        music_service=music_service,
    )


def apply_setting_effects(
    changes: Mapping[str, object],
    *,
    settings_mgr: Any,
    previous: Mapping[str, object] | None = None,
    music_service: object | None = None,
) -> dict[str, EffectResult]:
    """Apply every change in *changes* that has a declared effect.

    Keys with no declared effect are skipped rather than reported, because
    this is also called from the Settings UI write path, which legitimately
    writes many keys this registry says nothing about (secrets, device
    identifiers, onboarding flags). The agent surface is the one that must
    be fully declared, and the gate enforces that separately.
    """
    prior = previous or {}
    results: dict[str, EffectResult] = {}
    for key, value in changes.items():
        if key not in SETTING_EFFECTS:
            continue
        results[key] = apply_setting_effect(
            key,
            value,
            settings_mgr=settings_mgr,
            previous=prior.get(key),
            music_service=music_service,
        )
    return results


__all__ = [
    "SETTING_EFFECTS",
    "EffectKind",
    "EffectOutcome",
    "EffectResult",
    "SettingEffect",
    "apply_setting_effect",
    "apply_setting_effects",
    "get_setting_effect",
]
