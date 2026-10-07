"""Settings contract for an explicitly marked speech qualification artifact.

No pronunciation assets or engines are loaded here. The runtime owns artifact
admission and locale/voice validation; ordinary installations remain unchanged.
"""

from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

SPEECH_SELECTION_KEYS = frozenset({"tts_language", "tts_voice"})
_LOCALE_LABELS = {
    "en-us": "English (US)",
    "en-gb": "English (UK)",
    "es": "Spanish",
    "zh": "Mandarin",
    "fr": "French",
    "hi": "Hindi",
    "it": "Italian",
    "ja": "Japanese",
    "pt-br": "Portuguese (Brazil)",
}
_selection_lock = threading.RLock()
_selection_binding = None
_selection_effect = None


def qualification_profile():
    """Ordinary cloud builds may omit the optional voice source package."""
    if not getattr(sys, "frozen", False) and os.getenv("VIOLA_KOKORO_PHONEMIZER") != "misaki-en":
        return None
    from voice.customer_runtime import qualification_profile as selected_profile

    return selected_profile()


def customer_speech_payload(settings: dict) -> dict | None:
    """Describe only the selected artifact inventory, never dormant assets."""
    profile = qualification_profile()
    if profile is None:
        return None
    from voice.customer_runtime import resolve_selection
    from voice.customer_voice_routing import require_customer_voice
    locales = []
    for locale, label in _LOCALE_LABELS.items():
        voices = []
        for voice in profile["selected_voice_ids"]:
            try:
                require_customer_voice(locale, voice)
            except ValueError:
                continue
            voices.append(voice)
        if voices:
            locales.append({"value": locale, "label": label, "voices": voices})
    selection = None
    try:
        locale, voice = resolve_selection(SimpleNamespace(**settings))
        selection = {"language": locale, "voice": voice}
    except (ValueError, RuntimeError):
        pass  # Invalid saved state stays visible and requires explicit repair.
    return {"locales": locales, "selection": selection, "release_eligible": False}


def validate_customer_speech_update(updates: dict, settings_mgr, *, user_id=None) -> dict:
    """Return one validated pair for a single atomic settings-store update."""
    if not SPEECH_SELECTION_KEYS.intersection(updates):
        return updates
    if qualification_profile() is None:
        if "tts_language" in updates and updates["tts_language"] != settings_mgr.get(
            "tts_language", "en-us", user_id=user_id
        ):
            raise ValueError("Speech output language is available only in a speech qualification artifact")
        return updates
    from voice.customer_runtime import resolve_selection
    proposed = {
        key: (
            updates[key]
            if key in updates
            else settings_mgr.get(
                key, "en-us" if key == "tts_language" else "default", user_id=user_id, on_load_error="raise"
            )
        )
        for key in SPEECH_SELECTION_KEYS
    }
    locale, voice = resolve_selection(SimpleNamespace(**proposed))
    return {**updates, "tts_language": locale, "tts_voice": voice}


def apply_customer_speech_selection(key, settings_mgr, *, user_id=None):
    """Apply the user's complete pair, serializing engine/config publication."""
    with _selection_lock:
        return _apply_customer_speech_selection(key, settings_mgr, user_id=user_id)


def _apply_customer_speech_selection(key, settings_mgr, *, user_id=None):
    global _selection_binding, _selection_effect

    from ui.settings_effects import EffectOutcome, EffectResult

    binding = None
    app_config = None
    engine = None
    route_attempted = False
    try:
        if qualification_profile() is None:
            return EffectResult(key, EffectOutcome.DEFERRED, "ordinary voice settings are unchanged")
        from voice.customer_runtime import resolve_selection
        from config.settings import settings as app_config
        from voice.synthesis import factory as tts_factory

        user_id = settings_mgr._resolve_user_id(user_id)
        engine = getattr(tts_factory, "_shared_kokoro", None)
        binding = (settings_mgr, user_id, engine)
        selected = SimpleNamespace(
            **{
                name: settings_mgr.get(
                    name, "en-us" if name == "tts_language" else "default", user_id=user_id, on_load_error="raise"
                )
                for name in SPEECH_SELECTION_KEYS
            }
        )
        locale, voice = resolve_selection(selected)

        if engine is not None:
            route_attempted = True
            engine.set_customer_route(locale, voice)
        # Publish config only after the running engine accepts the pair. When
        # unmaterialized, its ordinary first-use constructor reads these values.
        app_config.tts_language = locale
        app_config.tts_voice = voice
        app_config._customer_speech_selection_failed = False
        result = EffectResult(
            key,
            EffectOutcome.APPLIED if engine is not None else EffectOutcome.DEFERRED,
            "speech selection applied" if engine is not None else "saved; applies when speech next starts",
        )
    except Exception:
        if app_config is not None:
            app_config._customer_speech_selection_failed = True
        if engine is not None and not route_attempted:
            # Loading a new user's preference can fail before route validation.
            # Retire the previous user's pending speech instead of leaving it usable.
            engine.invalidate_customer_route()
        result = EffectResult(key, EffectOutcome.FAILED, "the selected speech route could not be applied")
    if binding is not None:
        # Remember failures as well: refreshing the same account must never
        # clear the runtime's failed state. An explicit save owns that retry.
        _selection_binding, _selection_effect = binding, result
    return result


def refresh_customer_speech_user(settings_mgr, *, user_id=None):
    """Bind the existing authenticated settings refresh to the current user."""
    with _selection_lock:
        return _refresh_customer_speech_user(settings_mgr, user_id=user_id)


def _refresh_customer_speech_user(settings_mgr, *, user_id=None):
    if qualification_profile() is None:
        return None
    from voice.synthesis import factory as tts_factory
    from ui.settings_effects import apply_setting_effect

    user_id = settings_mgr._resolve_user_id(user_id)
    engine = getattr(tts_factory, "_shared_kokoro", None)
    if (
        _selection_binding is not None
        and _selection_binding[0] is settings_mgr
        and _selection_binding[1] == user_id
        and _selection_binding[2] is engine
    ):
        from config.settings import settings as app_config
        from ui.settings_effects import EffectOutcome, EffectResult

        if getattr(app_config, "_customer_speech_selection_failed", False):
            return EffectResult("tts_language", EffectOutcome.FAILED, "the selected speech route is unavailable")
        return _selection_effect
    return apply_setting_effect("tts_language", None, settings_mgr=settings_mgr, user_id=user_id)
