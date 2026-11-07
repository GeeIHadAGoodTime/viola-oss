"""Pure locale/voice pairing for explicitly selected customer speech.

The shipped asset inventory and pronunciation component still decide availability.
These pairs describe language identity, not acoustic quality or release approval.
No model, native runtime, pronunciation package or process setting is loaded here.
"""

from __future__ import annotations

from collections.abc import Collection


# Prefixes and defaults from the retained Kokoro v1.0 voice inventory. Dormant
# language assets remain usable by explicitly composed future profiles; checking
# a selected locale never constructs or requires the other language routes.
_VOICE_FAMILIES = {
    "en-us": ("af_", "am_", "af_heart"),
    "en-gb": ("bf_", "bm_", "bf_emma"),
    "es": ("ef_", "em_", "ef_dora"),
    "zh": ("zf_", "zm_", "zf_xiaobei"),
    "fr": ("ff_", "fm_", "ff_siwis"),
    "hi": ("hf_", "hm_", "hf_alpha"),
    "it": ("if_", "im_", "if_sara"),
    "ja": ("jf_", "jm_", "jf_alpha"),
    "pt-br": ("pf_", "pm_", "pf_dora"),
}

# Voices that read a phrase inside a reply ("In French, thanks is merci"). They
# are in the shipped voice file already, and they are not output languages: the
# Settings picker and the qualification marker do not offer them.
_PHRASE_VOICES = {"fr": "ff_siwis", "pt-br": "pf_dora"}

_SETTINGS_VOICE_ALIASES = {"alloy": "af_alloy", "echo": "am_echo", "nova": "af_nova"}


_PREVIEW_SENTENCES = {
    "es": "Hola, soy Viola. Así sueno cuando te hablo.",
    "zh": "你好，我是你的语音助手。这就是我的声音。",
}
_ENGLISH_PREVIEW = "Hi, I'm Viola. This is how I sound when I talk to you."


def preview_sentence(locale: object) -> str:
    """What a voice says in the Settings preview, in its own language."""
    key = locale.lower().replace("_", "-").split("-", 1)[0] if isinstance(locale, str) else ""
    return _PREVIEW_SENTENCES.get(key, _ENGLISH_PREVIEW)


def default_voice(locale: str) -> str | None:
    """The locale's default voice ID, or None when the locale has no voice family."""
    family = _VOICE_FAMILIES.get(locale)
    return family[2] if family else None


def resolve_kokoro_voice_alias(voice: str) -> str:
    """Resolve the existing settings UI labels to actual Kokoro asset IDs."""
    return _SETTINGS_VOICE_ALIASES.get(voice, voice)


def uses_named_customer_voices(component: object) -> bool:
    """The explicitly composed route owns named-voice admission.

    Keep the earlier implicit English adapter's style compatibility unchanged.
    The type is already enforced at both explicit injection constructors.
    """
    if component is None:
        return False
    from voice.customer_composition import ComposedCustomerTokenizer

    return type(component) is ComposedCustomerTokenizer


def require_customer_voice(locale: object, voice: object) -> None:
    """Reject mismatched or unidentifiable styles before pronunciation/audio.

    Raw tensors and voice blends have no independently qualified language identity.
    The customer path requires a named voice; the existing QA path is unaffected.
    Existence in the exact loaded voice asset must be checked by the caller too.
    """
    key = locale.lower().replace("_", "-") if isinstance(locale, str) else None
    family = _VOICE_FAMILIES.get(key)
    if family is None or not isinstance(voice, str) or not voice.startswith(family[:2]):
        raise ValueError("Customer speech requires a named voice matching its pronunciation locale")
    from voice.customer_runtime import qualification_profile

    profile = qualification_profile()
    if _PHRASE_VOICES.get(key) == voice:
        return
    if profile is not None and (key not in profile["locales"] or voice not in profile["selected_voice_ids"]):
        raise ValueError("The voice and locale are not selected in this qualification artifact")


def select_customer_voice(locale: str, current: object, available: Collection[str]) -> str:
    """Preserve a compatible selected voice, otherwise select the locale default."""
    if not isinstance(available, Collection) or isinstance(available, (str, bytes)):
        raise ValueError("The loaded customer voice inventory is unavailable")
    from voice.customer_runtime import qualification_profile

    profile = qualification_profile()
    if profile is not None:
        available = tuple(voice for voice in available if voice in profile["selected_voice_ids"])
    try:
        require_customer_voice(locale, current)
    except ValueError:
        pass
    else:
        if current in available:
            return current
    key = locale.lower().replace("_", "-") if isinstance(locale, str) else None
    family = _VOICE_FAMILIES.get(key)
    if family is None or family[2] not in available:
        raise ValueError("The selected customer locale has no available matching default voice")
    return family[2]


def customer_phone_locale_is_explicit(language: object) -> bool:
    """Do not collapse unspecified Chinese regional varieties into Mandarin.

    The registered zh/zh-CN/zh-TW and cmn/cmn-CN aliases select Mandarin.
    Other zh regional variants require their own pronunciation qualification.
    Cantonese's yue code is not an alias of Mandarin.
    """
    value = getattr(language, "value", language)
    if not isinstance(value, str):
        return False
    value = value.lower().replace("_", "-")
    if value == "zh" or value.startswith("zh-"):
        return value in {"zh", "zh-cn", "zh-tw"}
    return value != "yue" and not value.startswith("yue-")
