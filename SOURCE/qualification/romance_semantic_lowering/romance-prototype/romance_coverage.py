"""Inactive, source-covered Romance G2P to Kokoro-v1 semantic lowering.

Uses only the exact previously inspected MIT frontend subset. No optional
frontend, installed package, model, eSpeak, registry, or runtime is imported.
The output is a qualification record, never a product readiness declaration.
"""

from __future__ import annotations

import hashlib
import importlib
import unicodedata
from dataclasses import asdict
from pathlib import Path


class CoverageError(ValueError):
    """Source, phone meaning or prosody cannot be preserved by this prototype."""


_PINS = {
    "base": "de7cd41b7d7c932ffe7c8a23cd2fa8c9cafa037ab4d16f2180e5768d67de207a",
    "spanish": "dd1506f6e1030c14ef1b9a4997e6155746cf5594ee47907d5282c14c20e97327",
    "french": "f188040463369508218947a6e241bbb5a219b09c10515fa3e5bb996e192e73a9",
    "portuguese": "005304915518187a49f13a2ca3217d23abce9530d432b68d19a198bd0aa4db41",
}
_LOCALES = {
    "es": ("spanish", "phonemize_spanish_with_prosody", "áéíóúüñ", ",.;:!?¡¿", "seseo"),
    "fr": (
        "french",
        "phonemize_french_with_prosody",
        "àâæéèêëîïôùûüœçñ",
        ",.;:!?¡¿—–…«»",
        "upstream limited liaison/elision",
    ),
    "pt-br": ("portuguese", "phonemize_portuguese_with_prosody", "áàâãéêíóôõúüçñ", ",.;:!?¡¿—–…", "BR"),
    "pt-pt": (
        "portuguese",
        "phonemize_european_portuguese_with_prosody",
        "áàâãéêíóôõúüçñ",
        ",.;:!?¡¿—–…",
        "EU, unsupported phones fail",
    ),
}
# This is a closed IPA-unit inventory, NOT arbitrary character-vocabulary
# membership. In particular a Latin string such as Spotify is not a phone.
_VOWELS = frozenset("aeiouyæɑɐɒɔəɚɛɜɪʊʌɨɯɤøœ")
_CONSONANTS = frozenset("bdfhjk lmnpqrstvwxyzçðŋɕɖɟɡɣɥɰɲɳɴɸɹɻɽɾʁʂʃʈʋʎʒʔʝβθχ".replace(" ", ""))
_VOWEL_UNITS = _VOWELS | frozenset(("ã", "ẽ", "ĩ", "õ", "ũ", "ɛ̃", "ɑ̃", "ɔ̃", "ɐ̃", "ã", "ẽ", "ĩ", "õ", "ũ"))
_UNIT_MAP = {unit: unit for unit in _VOWELS | _CONSONANTS}
_UNIT_MAP.update({unit: unicodedata.normalize("NFD", unit) for unit in _VOWEL_UNITS if unit not in _VOWELS})
_UNIT_MAP.update({"rr": "r", "y_vowel": "y", "tʃ": "ʧ", "dʒ": "ʤ"})
_VOWEL_UNITS = _VOWEL_UNITS | {"y_vowel"}
_PUNCT_MAP = {symbol: symbol for symbol in ",.;:!?—…"}
_PUNCT_MAP.update({"«": "“", "»": "”"})
_MARKERS = {"ˈ": 2, "ˌ": 1}
_CLITICS = frozenset(("l", "d", "j", "n", "s", "c", "m", "t", "qu"))


def lower_units(tokens, prosody, vocab):
    """Preserve explicit stress and insert missing nucleus-anchored stress.

    rr is one alveolar trill r; y_vowel is the vowel y. Affricates use the
    established Kokoro-v1 unit symbols. Nasal vowels preserve their IPA
    modifier. Unsupported PUA, dark-l, unknown sequences and prosody reject.
    """
    if (
        not isinstance(tokens, (list, tuple))
        or not isinstance(prosody, (list, tuple))
        or not tokens
        or len(tokens) > 20000
        or len(tokens) != len(prosody)
    ):
        raise CoverageError("Nonempty aligned phone/prosody sequences are required")
    from _romance_vendor.base import ProsodyInfo

    output, traces = [], []
    cursor = 0
    for index, (token, info) in enumerate(zip(tokens, prosody)):
        if type(info) is not ProsodyInfo or any(type(x) is not int for x in (info.a1, info.a2, info.a3)):
            raise CoverageError("Prosody must retain the exact typed integer fields")
        if info.a1 != 0 or info.a2 not in (0, 1, 2) or not 0 <= info.a3 <= 10000:
            raise CoverageError("Unsupported prosody value")
        if not isinstance(token, str) or not token or any(unicodedata.category(c) == "Co" for c in token):
            raise CoverageError("Empty, malformed or PUA phone unit")
        inserted = ""
        if token == " " or token in _PUNCT_MAP:
            if (info.a1, info.a2, info.a3) != (0, 0, 0):
                raise CoverageError("Boundary token carries inconsistent prosody")
            lowered = token if token == " " else _PUNCT_MAP[token]
        elif token in _MARKERS:
            if info.a2 != _MARKERS[token] or info.a3 <= 0 or index + 1 == len(tokens):
                raise CoverageError("Stress marker has no matching nucleus")
            next_info = prosody[index + 1]
            if (
                tokens[index + 1] not in _VOWEL_UNITS
                or type(next_info) is not ProsodyInfo
                or next_info.a2 != info.a2
                or next_info.a3 != info.a3
            ):
                raise CoverageError("Explicit stress is not aligned to its nucleus")
            lowered = token
        else:
            if token not in _UNIT_MAP or info.a3 <= 0:
                raise CoverageError("Unsupported semantic phone unit: " + token)
            if info.a2:
                if token not in _VOWEL_UNITS:
                    raise CoverageError("Stress metadata is attached to a non-vowel")
                expected_marker = "ˈ" if info.a2 == 2 else "ˌ"
                if index == 0 or tokens[index - 1] != expected_marker:
                    inserted = expected_marker
            lowered = inserted + _UNIT_MAP[token]
        if any(character not in vocab for character in lowered):
            raise CoverageError("Semantic lowering is outside the selected Kokoro vocabulary")
        output.append(lowered)
        traces.append(
            {
                "upstream_index": index,
                "unit": token,
                "prosody": asdict(info),
                "model_start": cursor,
                "model_end": cursor + len(lowered),
                "model_text": lowered,
                "inserted_stress": inserted,
            }
        )
        cursor += len(lowered)
    return "".join(output), traces


class CoveredRomance:
    """Trusted pinned frontend, with explicit per-instance locale and no fallback."""

    def __init__(self, locale, vocab):
        if not isinstance(locale, str) or locale not in _LOCALES or not isinstance(vocab, dict) or not vocab:
            raise CoverageError("An explicit supported prototype locale and vocabulary are required")
        self._locale = locale
        self.vocab = dict(vocab)
        name, function, letters, punctuation, dialect = _LOCALES[locale]
        root = Path(__file__).with_name("_romance_vendor")
        for module_name in ("base", name):
            raw = (root / (module_name + ".py")).read_bytes()
            if hashlib.sha256(raw).hexdigest() != _PINS[module_name]:
                raise CoverageError("Pinned upstream frontend identity differs")
        self.module = importlib.import_module("_romance_vendor." + name)
        if Path(self.module.__file__).resolve() != (root / (name + ".py")).resolve():
            raise CoverageError("Loaded frontend comes from a different source")
        base = importlib.import_module("_romance_vendor.base")
        if Path(base.__file__).resolve() != (root / "base.py").resolve():
            raise CoverageError("Loaded prosody type comes from a different source")
        self._call = getattr(self.module, function)
        self._letters = frozenset("abcdefghijklmnopqrstuvwxyz" + letters)
        self._punctuation = frozenset(punctuation)
        self.dialect = dialect

    @property
    def locale(self):
        return self._locale

    def _source(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise CoverageError("A nonempty bounded source string is required")
        spans, events, canonical = [], [], []
        index = 0
        while index < len(text):
            first = index
            character = text[index]
            if character in " \t":
                while index < len(text) and text[index] in " \t":
                    index += 1
                kind, normalized = "space", " "
            elif character in self._punctuation:
                index += 1
                kind, normalized = "punctuation", character
            elif unicodedata.category(character).startswith("L"):
                index += 1
                while index < len(text) and (
                    unicodedata.category(text[index]).startswith(("L", "M"))
                    or (self.locale == "fr" and text[index] in "'’‘")
                ):
                    index += 1
                normalized = unicodedata.normalize("NFC", text[first:index].lower()).replace("’", "'").replace("‘", "'")
                parts = normalized.split("'")
                if len(parts) > 1 and self.locale != "fr":
                    raise CoverageError("Unqualified apostrophe/elision structure")
                if any(not part or set(part) - self._letters for part in parts):
                    raise CoverageError("Source word contains unsupported script or letters")
                kind = "word"
            else:
                raise CoverageError("Unconsumed source character at " + str(index) + ": " + repr(character))
            row = {"start": first, "end": index, "source": text[first:index], "kind": kind, "normalized": normalized}
            spans.append(row)
            canonical.append(normalized)
            if kind != "space":
                # Retain the upstream's documented clitic grouping without
                # losing the source apostrophe span. A non-clitic split can
                # map one raw word span to several frontend words.
                words = [normalized]
                if kind == "word" and self.locale == "fr" and "'" in normalized:
                    words, buffer = [], ""
                    parts = normalized.split("'")
                    for position, part in enumerate(parts):
                        if buffer:
                            buffer += part
                        elif position < len(parts) - 1 and part in _CLITICS:
                            buffer = part
                        else:
                            words.append(part)
                    if buffer:
                        words.append(buffer)
                row["frontend_tokens"] = words
                for word in words:
                    events.append({"kind": kind, "token": word, "span_index": len(spans) - 1})
        if not any(row["kind"] == "word" for row in spans):
            raise CoverageError("Source contains no lexical content")
        frontend_text = "".join(canonical).strip()
        # Case/NFC/elision and horizontal spacing are recorded, not silently
        # confused with source offsets. Newlines/controls/numbers/currency reject.
        assert "".join(row["source"] for row in spans) == text
        normalized = self.module._normalize(frontend_text)
        if normalized != frontend_text:
            raise CoverageError("Frontend applied an unaccounted normalization")
        upstream_tokens = (
            self.module._RE_TOKEN.findall(normalized) if self.locale == "es" else self.module._split_words(normalized)
        )
        flattened = []
        for token in upstream_tokens:
            flattened.extend(list(token) if all(c in self._punctuation for c in token) else [token])
        if flattened != [row["token"] for row in events]:
            raise CoverageError("Frontend tokenization omitted or changed source content")
        return frontend_text, spans, events

    def phonemize(self, text):
        frontend_text, spans, expected = self._source(text)
        try:
            tokens, prosody = self._call(frontend_text)
        except ValueError as exc:
            raise CoverageError("Pinned frontend rejected unconsumed source: " + str(exc)) from exc
        phones, mappings = lower_units(tokens, prosody, self.vocab)
        actual, group = [], []

        def flush():
            if not group:
                return
            units = [i for i in group if tokens[i] not in _MARKERS]
            if not units or any(prosody[i].a3 != len(units) for i in group):
                raise CoverageError("Word phoneme count/prosody is incomplete")
            actual.append({"kind": "word", "indexes": list(group)})
            group.clear()

        for index, token in enumerate(tokens):
            if token == " ":
                flush()
            elif token in self._punctuation:
                flush()
                actual.append({"kind": "punctuation", "token": token, "indexes": [index]})
            else:
                group.append(index)
        flush()
        if len(actual) != len(expected):
            raise CoverageError("Frontend emitted incomplete word/punctuation coverage")
        for source, result in zip(expected, actual):
            if source["kind"] != result["kind"] or (
                source["kind"] == "punctuation" and source["token"] != result["token"]
            ):
                raise CoverageError("Frontend reordered or dropped source boundaries")
            row = spans[source["span_index"]]
            row.setdefault("upstream_token_indexes", []).extend(result["indexes"])
            row.setdefault("model_start", mappings[result["indexes"][0]]["model_start"])
            row["model_end"] = mappings[result["indexes"][-1]]["model_end"]
        if not any(token in _UNIT_MAP for token in tokens):
            raise CoverageError("No spoken phone unit was produced")
        return {
            "source": text,
            "frontend_text": frontend_text,
            "locale": self.locale,
            "dialect": self.dialect,
            "frontend_commit": "82ee4e7a9b7aded42e0d0d5fd8298b42bfa51a16",
            "upstream_tokens": list(tokens),
            "upstream_prosody": [asdict(p) for p in prosody],
            "phonemes": phones,
            "source_spans": spans,
            "phone_mappings": mappings,
            "release_eligible": False,
            "phonological_or_acoustic_qualification": False,
            "customer_profile_activated": False,
        }
