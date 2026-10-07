"""Explicit, inactive composition for English and bounded CJK/Romance G2P.

No profile, language, voice, model or process setting is selected at import.
Callers construct this component explicitly and inject it into the existing
customer Kokoro path. Application defaults and release eligibility are unchanged.
"""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
from types import MappingProxyType

_COMPANION_VERSION = "0.9.4+viola.cjk.3"
_ROMANCE_LOCALES = frozenset({"es", "fr", "pt-br"})
_CJK_DEPENDENCIES = {
    "fugashi": "1.5.2",
    "jaconv": "0.5.0",
    "mojimoji": "0.0.13",
    "pypinyin": "0.55.0",
    "cn2an": "0.5.24",
    "jieba": "0.42.1",
    "ordered-set": "4.1.0",
    "proces": "0.1.7",
}


class ComposedCustomerTokenizer:
    """Keep locale capability attached to one explicitly constructed runtime."""

    def __init__(self, english, routes: dict):
        from voice.customer_pronunciation import CustomerTokenizer

        if type(english) is not CustomerTokenizer:
            raise ValueError("Composition requires the exact existing English customer tokenizer")
        if set(routes) - ({"ja", "zh"} | _ROMANCE_LOCALES):
            raise ValueError("Composition contains an unsupported locale")
        for locale, route in routes.items():
            if locale in _ROMANCE_LOCALES:
                from voice.customer_romance.coverage import CoveredRomance

                if type(route) is not CoveredRomance or route.locale != locale or route.vocab != english.vocab:
                    raise ValueError("Romance routes must bind the exact locale and model vocabulary")
            else:
                from viola_cjk.coverage import CoveredCJK
                from viola_cjk.customer_english_span import CustomerEnglishBridge

                if (
                    type(route) is not CoveredCJK
                    or route.locale != locale
                    or route.vocab != english.vocab
                    or type(route.english) is not CustomerEnglishBridge
                    or route.english._tokenizer is not english
                ):
                    raise ValueError("CJK routes must bind the same English component and model vocabulary")
        self._english = english
        self._routes = MappingProxyType(dict(routes))
        self.vocab = english.vocab

    def supports_locale(self, lang: object) -> bool:
        if not isinstance(lang, str):
            return False
        locale = lang.lower().replace("_", "-")
        return locale in {"en-us", "en-gb"} or locale in self._routes

    def tokenize(self, phonemes: str) -> list[int]:
        from voice.customer_pronunciation import CustomerTokenizer

        return CustomerTokenizer.tokenize(self._english, phonemes)

    def phonemize(self, text: str, lang: str = "en-us", norm: bool = True) -> str:
        from voice.customer_pronunciation import CustomerTokenizer, PronunciationError

        if not self.supports_locale(lang):
            raise PronunciationError("The explicitly composed customer locale is unavailable")
        locale = lang.lower().replace("_", "-")
        if locale in {"en-us", "en-gb"}:
            return CustomerTokenizer.phonemize(self._english, text, lang=locale, norm=norm)
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise PronunciationError("Customer speech text must contain 1 to 5000 characters")
        source = text.strip() if norm else text
        if locale in _ROMANCE_LOCALES:
            from voice.customer_romance.coverage import CoverageError, CoveredRomance

            try:
                result = CoveredRomance.phonemize(self._routes[locale], source)
            except CoverageError:
                raise PronunciationError("Customer speech is outside the bounded Romance coverage") from None
        else:
            from viola_cjk.coverage import CoveredCJK

            result = CoveredCJK.phonemize(self._routes[locale], source)
        # Keep Kokoro's existing lossless batching: phonemize may return more
        # than one batch, while tokenize enforces the model limit per batch.
        phones = result["phonemes"]
        if not isinstance(phones, str) or not phones or any(character not in self.vocab for character in phones):
            raise PronunciationError("Customer speech contains unsupported phonemes")
        return phones


def compose_customer_tokenizer(
    english,
    *,
    japanese_dictionary_dir: str | Path | None = None,
    mandarin: bool = False,
    english_locale: str = "en-us",
    romance_locales: tuple[str, ...] = (),
) -> ComposedCustomerTokenizer:
    """Build only explicitly requested routes from the existing pinned inputs.

    Japanese requires the reviewed explicit eighteen-file dictionary. Mandarin
    initializes the installed Jieba dictionary and existing pypinyin provider;
    no independent global-dictionary contract or custom lexicon is invented.
    Romance accepts an explicit tuple drawn from es/fr/pt-br, with the retained
    phrase/representation limits. No implicit Portuguese dialect is selected.
    CJK dependencies are required only when a CJK route is requested. Failure
    returns no partially ready composition. No voice is selected.
    """
    from voice.customer_pronunciation import CustomerTokenizer

    if type(english) is not CustomerTokenizer:
        raise ValueError("Composition requires the exact existing English customer tokenizer")
    if type(mandarin) is not bool:
        raise ValueError("Mandarin construction requires an explicit boolean")
    if (
        type(romance_locales) is not tuple
        or any(type(locale) is not str or locale not in _ROMANCE_LOCALES for locale in romance_locales)
        or len(set(romance_locales)) != len(romance_locales)
    ):
        raise ValueError("Romance construction requires distinct explicit es/fr/pt-br locales")

    routes = {}
    if romance_locales:
        from voice.customer_romance.coverage import CoveredRomance

        routes.update((locale, CoveredRomance(locale, english.vocab)) for locale in romance_locales)
    if not mandarin and japanese_dictionary_dir is None:
        return ComposedCustomerTokenizer(english, routes)

    if importlib.metadata.version("viola-misaki-cjk-prototype") != _COMPANION_VERSION:
        raise RuntimeError("The reviewed inactive CJK companion distribution is required")
    for name, version in _CJK_DEPENDENCIES.items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("The reviewed CJK dependency version is required: " + name)

    from viola_cjk.coverage import CoveredCJK
    from viola_cjk.customer_english_span import CustomerEnglishBridge
    from viola_cjk.locale_numbers import prepare_exact_number

    bridge = CustomerEnglishBridge(english, english_locale)
    if mandarin:
        import cn2an
        import jieba
        import pypinyin
        from misaki.zh import ZHG2P

        dictionary = Path(jieba.__file__).resolve().with_name("dict.txt")
        if not dictionary.is_file():
            raise RuntimeError("The packaged Mandarin segmentation dictionary is missing")
        segmenter = jieba.Tokenizer(dictionary=str(dictionary))
        segmenter.initialize()

        def chinese_number(value, locale, unit=None):
            return prepare_exact_number(
                value, locale, unit=unit, integer_converter=lambda digits: cn2an.an2cn(digits, "low")
            )

        routes["zh"] = CoveredCJK(
            locale="zh",
            vocab=english.vocab,
            english=bridge,
            chinese=ZHG2P(),
            jieba=segmenter,
            pinyin=pypinyin.lazy_pinyin,
            pinyin_style=pypinyin.Style.TONE3,
            number_preparer=chinese_number,
        )
    # Finish pure-component initialization before creating the native Japanese frontend.
    if japanese_dictionary_dir is not None:
        from misaki.num2kana import do_convert, hiragana_dict
        from viola_cjk.japanese_factory import create_cutlet

        cutlet = create_cutlet(japanese_dictionary_dir)

        def japanese_number(value, locale, unit=None):
            return prepare_exact_number(
                value, locale, unit=unit, integer_converter=lambda digits: do_convert(digits, hiragana_dict)
            )

        routes["ja"] = CoveredCJK(
            locale="ja", vocab=english.vocab, english=bridge, cutlet=cutlet, number_preparer=japanese_number
        )
    return ComposedCustomerTokenizer(english, routes)


def require_customer_composition(component, *, vocab: dict | None = None) -> None:
    """Validate explicit injection before native construction, without activation."""
    if os.getenv("VIOLA_KOKORO_PHONEMIZER", "espeak") != "misaki-en":
        raise ValueError("Explicit customer pronunciation requires the existing customer profile")
    if type(component) is not ComposedCustomerTokenizer:
        raise ValueError("Explicit customer pronunciation requires the exact composed tokenizer")
    if vocab is not None and component.vocab != vocab:
        raise ValueError("Explicit customer pronunciation must bind the selected model vocabulary")
