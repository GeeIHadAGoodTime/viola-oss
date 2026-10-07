"""Normal-launch binding for a marked, local speech qualification artifact.

The private frozen startup hook selects this profile before application/native
imports. Ordinary builds and source launches keep their existing behavior.
This binding never grants customer release eligibility.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
from types import MappingProxyType
from types import SimpleNamespace

from voice.customer_voice_routing import require_customer_voice, resolve_kokoro_voice_alias

MARKER_NAME = "customer-speech-qualification.json"
_LANGUAGES = frozenset({"English", "Spanish", "Mandarin"})
_PREFIX_LOCALES = {"a": "en-us", "b": "en-gb", "e": "es", "z": "zh"}
_DEFAULTS = {"en-us": "af_heart", "en-gb": "bf_emma", "es": "ef_dora", "zh": "zf_xiaobei"}
_OFFLINE = ("ORT_DISABLE_TELEMETRY", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "TRANSFORMERS_OFFLINE")
_profile = None
_composition = None
_composition_lock = threading.Lock()


def activate_qualification_profile() -> None:
    """Called only by the qualification artifact's frozen runtime hook."""
    global _profile
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        raise RuntimeError("Customer speech qualification requires its frozen Windows artifact")
    if _profile is not None:
        qualification_profile()
        return
    if "onnxruntime" in sys.modules or "kokoro_onnx" in sys.modules:
        raise RuntimeError("Customer speech qualification must be selected before native speech imports")
    if os.environ.get("VIOLA_KOKORO_PHONEMIZER") not in {None, "misaki-en"}:
        raise RuntimeError("The qualification artifact cannot use another pronunciation backend")
    if os.environ.get("VIOLA_INTERNAL_SPEECH_CANDIDATE", "") not in {"", "0"}:
        raise RuntimeError("Customer speech qualification cannot use internal eSpeak QA")
    marker = Path(sys.executable).resolve().parent / MARKER_NAME
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 65536:
        raise RuntimeError("The customer speech qualification marker is missing or invalid")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The customer speech qualification marker is unreadable") from exc
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int or value["schema_version"] != 1
        or value.get("artifact_kind") != "customer-speech-qualification-only"
        or value.get("qualification_runtime_selected") is not True
        or value.get("release_gate_eligible") is not False
        or value.get("customer_release_eligible") is not False
        or value.get("selected_languages") != sorted(_LANGUAGES)
    ):
        raise RuntimeError("The artifact does not declare the selected qualification-only speech profile")
    voices = value.get("selected_voice_ids")
    if (
        type(voices) is not list or not voices or len(voices) > 39
        or any(type(voice) is not str or not voice or voice[0] not in _PREFIX_LOCALES for voice in voices)
        or len(set(voices)) != len(voices)
    ):
        raise RuntimeError("The qualification artifact has an invalid selected voice inventory")
    locales = []
    for voice in voices:
        locale = _PREFIX_LOCALES[voice[0]]
        require_customer_voice(locale, voice)
        if locale not in locales:
            locales.append(locale)
    if {"English" if locale.startswith("en-") else "Spanish" if locale == "es" else "Mandarin" for locale in locales} != _LANGUAGES:
        raise RuntimeError("The qualification artifact must select English, Spanish and Mandarin voices")
    for name in _OFFLINE:
        os.environ[name] = "1"
    os.environ["VIOLA_KOKORO_PHONEMIZER"] = "misaki-en"
    _profile = MappingProxyType({
        "artifact_kind": value["artifact_kind"],
        "selected_voice_ids": tuple(voices),
        "languages": tuple(sorted(_LANGUAGES)),
        "locales": tuple(locales),
        "customer_release_eligible": False,
    })


def qualification_profile():
    """Return immutable process selection; environment changes cannot downgrade it."""
    if _profile is not None:
        if os.environ.get("VIOLA_KOKORO_PHONEMIZER") != "misaki-en" or any(os.environ.get(key) != "1" for key in _OFFLINE):
            raise RuntimeError("The qualification speech profile cannot change after startup")
        if os.environ.get("VIOLA_INTERNAL_SPEECH_CANDIDATE", "") not in {"", "0"}:
            raise RuntimeError("The qualification speech profile cannot acquire internal QA inputs")
    return _profile


def require_qualification_bootstrap() -> None:
    """A marker without its startup hook must not silently launch the QA path."""
    if getattr(sys, "frozen", False):
        marker = Path(sys.executable).resolve().parent / MARKER_NAME
        if marker.exists() and qualification_profile() is None:
            raise RuntimeError("The customer qualification startup hook did not run")
    qualification_profile()


def resolve_selection(config) -> tuple[str, str]:
    profile = qualification_profile()
    if profile is None:
        raise RuntimeError("No customer qualification artifact is selected")
    locale = getattr(config, "tts_language", "en-us")
    voice = getattr(config, "tts_voice", "default")
    if type(locale) is not str or locale not in profile["locales"]:
        raise ValueError("The speech output language is not selected in this qualification artifact")
    if voice == "default":
        preferred = _DEFAULTS[locale]
        voice = preferred if preferred in profile["selected_voice_ids"] else next(
            item for item in profile["selected_voice_ids"] if _PREFIX_LOCALES[item[0]] == locale
        )
    elif type(voice) is str:
        voice = resolve_kokoro_voice_alias(voice)
    if type(voice) is not str or voice not in profile["selected_voice_ids"]:
        raise ValueError("The named voice is not selected in this qualification artifact")
    require_customer_voice(locale, voice)
    return locale, voice


def get_qualification_composition():
    """Build one existing EN/ES/Mandarin owner; never return a partial route."""
    global _composition
    if qualification_profile() is None:
        raise RuntimeError("No customer qualification artifact is selected")
    with _composition_lock:
        if _composition is None:
            from kokoro_onnx.config import DEFAULT_VOCAB
            from voice.customer_composition import compose_customer_tokenizer
            from voice.customer_pronunciation import CustomerTokenizer

            owner = compose_customer_tokenizer(
                CustomerTokenizer(DEFAULT_VOCAB), mandarin=True, romance_locales=("es",)
            )
            if not all(owner.supports_locale(locale) for locale in qualification_profile()["locales"]):
                raise RuntimeError("The qualification pronunciation routes are incomplete")
            _composition = owner
        return _composition


def resolve_phone_voice(voice: str) -> tuple[str, str]:
    """Bind the phone's existing named-voice setting to its initial locale."""
    if not isinstance(voice, str):
        raise ValueError("The qualification phone voice must be named")
    voice = resolve_kokoro_voice_alias(voice)
    locale = _PREFIX_LOCALES.get(voice[:1])
    return resolve_selection(SimpleNamespace(tts_language=locale, tts_voice=voice))
