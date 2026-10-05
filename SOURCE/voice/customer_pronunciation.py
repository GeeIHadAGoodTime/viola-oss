"""Offline English Misaki adapter for the explicit customer Kokoro profile.

Never guesses an unsupported locale, drops unknown words/phonemes, invokes
an eSpeak fallback or downloads assets during speech. Packaging qualification
must separately verify the installed graph, bundled files and actual audio.
"""

from __future__ import annotations

import importlib.metadata
import re
import threading
from pathlib import Path

from voice.english_numbers import num2words
from voice.pronunciation_tables import _ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS

_FORK_VERSION = "0.9.4+viola.1"
_MODEL_VERSION = "3.8.0"
_MAX_PHONEMES = 510
_MAX_TEXT = 5000
# Misaki 0.9.4 spoken phones, including special-case a/er and the v1 flap.
# Stress/tone/punctuation symbols alone cannot constitute a spoken word.
_SPOKEN_PHONEMES = frozenset("AIOQWYaiuæɑɐɒɔəɚɛɜɪʊʌᵻᵊʔbdfhjklmnpstvwzðŋɡɹɾʃʒʤʧθT")
_EXPLICIT_OVERRIDE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
# Exact respelling fragments already emitted by Viola's retained brand/acronym
# and default-name tables. These small additions are not a general OOV fallback.
_RESPELLINGS = {
    "luh": "lə",
    "Ann": "ˈæn",
    "stuh": "stə",
    "roh": "ɹˈO",
    "koo": "kˈu",
    "bohz": "bˈOz",
    "muh": "mə",
    "ih": "ɪ",
    "nuh": "nə",
    "trell": "tɹˈɛl",
    "giff": "ɡˈɪf",
    "nassa": "nˈæsə",
    "tock": "tˈɑk",
    "vid": "vˈɪd",
    "ee": "i",
    "ven": "vˈɛn",
    "moe": "mˈO",
    "oo": "ˈu",
    "ber": "bɚ",
}


# Derive only complete spaced-letter phrases from the same retained tables
# used by the formatter. Open A I contributes A I; ordinary article contexts
# are not guessed. Source changes to a table are covered by complete-table tests.
_A_INITIALISM_PHRASES = frozenset(
    match.group(0)
    for table in (_ACRONYM_INITIALISMS, _ACRONYM_WORDS, _BRAND_PRONUNCIATIONS)
    for expansion in table.values()
    for match in re.finditer(r"(?<!\S)(?:[A-Z] +)+[A-Z](?=\s|$)", expansion)
    if "A" in match.group(0).split()
)
_A_INITIALISM_PATTERN = "|".join(
    re.escape(phrase).replace(r"\ ", " +")
    for phrase in sorted(_A_INITIALISM_PHRASES, key=lambda phrase: (-len(phrase), phrase))
)
_INITIALISM_RUN = re.compile(r'(?<![^\s(\["“‘])(?:' + _A_INITIALISM_PATTERN + r')(?=[.!?,;:)\]"”’]*(?:\s|$))')
_SINGLE_QUOTED_INITIALISM = re.compile(
    r"(?<![^\s(\[\"“‘])'(?P<letters>" + _A_INITIALISM_PATTERN + r")(?P<punct>[.!?,;:]*)'(?=[.!?,;:)\]\"”’]*(?:\s|$))"
)


def _protect_initialism_a(text: str) -> str:
    """Disambiguate A only inside the retained A-bearing initialism phrases.

    The recognized phrases are derived from the retained formatter tables.
    An article before another initialism (A U S citizen) must stay an article.
    Misaki otherwise reads their A as an article. Ordinary articles and caller-supplied
    pronunciation markup remain byte-identical; other pronunciation backends
    never receive this customer-only syntax.
    """

    def replace_run(match: re.Match) -> str:
        return re.sub(r"\bA\b", "[A](/ˈA/)", match.group(0))

    def protect_segment(segment: str) -> str:
        # Paired ASCII quotes are explicit delimiters, not an apostrophe inside
        # a name/word. Misaki already handles quoted AI, but not quoted API.
        segment = _SINGLE_QUOTED_INITIALISM.sub(
            lambda match: "'" + re.sub(r"\bA\b", "[A](/ˈA/)", match.group("letters")) + match.group("punct") + "'",
            segment,
        )
        return _INITIALISM_RUN.sub(replace_run, segment)

    parts, cursor = [], 0
    for override in _EXPLICIT_OVERRIDE.finditer(text):
        parts.append(protect_segment(text[cursor : override.start()]))
        parts.append(override.group(0))
        cursor = override.end()
    parts.append(protect_segment(text[cursor:]))
    return "".join(parts)


class PronunciationError(ValueError):
    """Input cannot be spoken completely by the qualified English route."""


class CustomerTokenizer:
    @staticmethod
    def supports_locale(lang: object) -> bool:
        """Declare locale capability without loading NLP, assets or a runtime.

        This is not asset readiness, voice/accent suitability or permission to
        activate the customer profile. Keep normalization aligned with phonemize.
        """
        return isinstance(lang, str) and lang.lower().replace("_", "-") in {"en-us", "en-gb"}

    def __init__(self, vocab: dict) -> None:
        # Metadata checks happen before importing any NLP or native runtime.
        if importlib.metadata.version("viola-misaki-en") != _FORK_VERSION:
            raise RuntimeError("The reviewed Viola Misaki English fork is required")
        try:
            importlib.metadata.version("misaki")
        except importlib.metadata.PackageNotFoundError:
            pass
        else:
            raise RuntimeError("Upstream misaki conflicts with the customer English fork")
        if importlib.metadata.version("en-core-web-sm") != _MODEL_VERSION:
            raise RuntimeError("The pinned offline English pronunciation model is required")
        import en_core_web_sm
        import spacy
        from misaki import en

        # Importing the known model package gives an explicit local resource;
        # never call spaCy's download CLI or allow a network model identifier.
        model = Path(en_core_web_sm.__file__).resolve().parent / f"en_core_web_sm-{_MODEL_VERSION}"
        if not (model / "config.cfg").is_file():
            raise RuntimeError("The offline English pronunciation model is incomplete")
        nlp = spacy.load(model, enable=["tok2vec", "tagger"])
        self._g2p = {
            "en-us": en.G2P(nlp=nlp, number_to_words=num2words, british=False),
            "en-gb": en.G2P(nlp=nlp, number_to_words=num2words, british=True),
        }
        for g2p in self._g2p.values():
            g2p.lexicon.golds.update(en.Lexicon.grow_dictionary(_RESPELLINGS))
        self.vocab = vocab
        self._lock = threading.Lock()

    def tokenize(self, phonemes: str) -> list[int]:
        if len(phonemes) > _MAX_PHONEMES:
            raise PronunciationError("Customer phoneme sequence exceeds the model limit")
        if not phonemes or any(character not in self.vocab for character in phonemes):
            raise PronunciationError("Customer phoneme sequence is empty or contains unsupported symbols")
        return [self.vocab[character] for character in phonemes]

    def phonemize(self, text: str, lang: str = "en-us", norm: bool = True) -> str:
        locale = lang.lower().replace("_", "-")
        if locale not in self._g2p:
            raise PronunciationError("Customer Misaki speech supports only en-us and en-gb")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT:
            raise PronunciationError("Customer speech text must contain 1 to 5000 characters")
        # Misaki's explicit phoneme syntax otherwise accepts / / or // and
        # silently suppresses the labelled word. Validate before preprocessing.
        for match in _EXPLICIT_OVERRIDE.finditer(text):
            target = match.group(2)
            if len(target) > 1 and target.startswith("/") and target.endswith("/"):
                override = target.strip("/")
                if not any(character in _SPOKEN_PHONEMES for character in override):
                    raise PronunciationError("Explicit pronunciation overrides must contain spoken phonemes")
        text = _protect_initialism_a(text)
        with self._lock:
            phonemes, tokens = self._g2p[locale](text.strip() if norm else text)
        # Unknown words must not disappear when the neural wrapper filters its
        # vocabulary. Do not include potentially private utterances in errors.
        if not tokens or any(
            token.phonemes is None
            or (
                any(character.isalnum() for character in token.text)
                and not any(character in _SPOKEN_PHONEMES for character in token.phonemes)
            )
            for token in tokens
        ):
            raise PronunciationError("Customer speech contains an unknown word; supply a pronunciation override")
        phonemes = phonemes.strip()
        if not phonemes or any(character not in self.vocab for character in phonemes):
            raise PronunciationError("Customer speech contains unsupported phonemes")
        return phonemes
