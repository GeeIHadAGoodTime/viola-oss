"""Reconstructed explicit CJK/English dispatcher, inactive pending fresh review.

Tracks complete source spans, validates selected pinyin before IPA conversion,
and serializes temporary Cutlet observation across all route instances. This
file is new source; historical missing receipts do not qualify these bytes.
"""

from __future__ import annotations

import re
import threading

from .customer_english_span import CustomerEnglishBridge

_CUTLET_LOCK = threading.RLock()
_NUMBER = re.compile(r"[-−]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
_PINYIN = re.compile(r"[a-züv]+[1-5]\Z")
_OVERRIDE = re.compile(r"\[[^\]]+\]\([^)]*\)")
_ENGLISH = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
_PUNCTUATION = {
    "。": ".",
    "、": ",",
    "，": ",",
    "！": "!",
    "？": "?",
    "：": ":",
    "；": ";",
    "「": "“",
    "」": "”",
    "『": "“",
    "』": "”",
    "（": "(",
    "）": ")",
}
_ASCII_PUNCTUATION = frozenset('.,!?;:()“”"')


def _han(char):
    return "\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U0002ebef"


def _japanese(char):
    return _han(char) or "\u3041" <= char <= "\u30ff" or "\uff66" <= char <= "\uff9f" or char in "々〆"


class CoveredCJK:
    def __init__(
        self,
        *,
        locale,
        vocab,
        english,
        chinese=None,
        jieba=None,
        pinyin=None,
        pinyin_style=None,
        cutlet=None,
        number_preparer=None,
    ):
        if locale not in {"ja", "zh"} or type(english) is not CustomerEnglishBridge:
            raise ValueError("Explicit CJK locale and bound English bridge are required")
        if english.vocab != vocab:
            raise ValueError("CJK and English vocabularies differ")
        if locale == "ja" and cutlet is None:
            raise ValueError("Japanese frontend is missing")
        if locale == "zh" and any(v is None for v in (chinese, jieba, pinyin, pinyin_style)):
            raise ValueError("Mandarin frontend components are missing")
        self.locale, self.vocab, self.english = locale, dict(vocab), english
        self.chinese, self.jieba, self.pinyin, self.pinyin_style = chinese, jieba, pinyin, pinyin_style
        self.cutlet, self.number_preparer = cutlet, number_preparer

    def _phones(self, phones):
        if not isinstance(phones, str) or not phones.strip() or any(c not in self.vocab for c in phones):
            raise ValueError("CJK phonemes are empty or unsupported")
        return phones

    def _native(self, source):
        if self.locale == "zh":
            words = list(self.jieba.lcut(source, cut_all=False))
            if not words or any(not isinstance(w, str) or not w for w in words) or "".join(words) != source:
                raise ValueError("Mandarin segmentation did not consume its exact source")
            output, traces = [], []
            for word in words:
                readings = self.pinyin(
                    word, style=self.pinyin_style, neutral_tone_with_five=True, errors=lambda unknown: [unknown]
                )
                if (
                    not isinstance(readings, (list, tuple))
                    or len(readings) != len(word)
                    or any(not isinstance(p, str) or _PINYIN.fullmatch(p) is None for p in readings)
                ):
                    raise ValueError("Mandarin reading contains an unconsumed source syllable")
                # Use the exact selected readings. Do not invoke a second global
                # word segmenter or pinyin lookup through legacy_call/word2ipa.
                phones = "".join(self.chinese.py2ipa(p) for p in readings)
                # Preserve ZHG2P.legacy_call's existing Kokoro compatibility
                # stage; py2ipa alone retains the U+032F non-syllabic marker.
                # Every other unsupported character still fails _phones below.
                phones = phones.replace(chr(815), "")
                output.append(self._phones(phones))
                traces.append({"source": word, "readings": list(readings), "phonemes": phones})
            return " ".join(output), traces
        with _CUTLET_LOCK:
            cutlet = self.cutlet
            original = cutlet._romaji_word
            had_instance = "_romaji_word" in vars(cutlet)
            prior = vars(cutlet).get("_romaji_word")
            records = []

            def observed(word):
                if not isinstance(word.surface, str) or not word.surface or word.char_type != 6:
                    raise ValueError("Japanese source word was not consumed as a reading")
                hira = word.hira
                if not isinstance(hira, str) or not hira:
                    raise ValueError("Japanese reading is missing")
                for i, c in enumerate(hira):
                    if c in "゙゚々〃ゝゞヽヾ" or (c not in cutlet.table and c not in "っんー"):
                        raise ValueError("Japanese reading contains an unsupported or dropped character")
                    if c in "ゃゅょぁぃぅぇぉ" and (i == 0 or hira[i - 1 : i + 1] not in cutlet.table):
                        raise ValueError("Japanese small kana has no consumed predecessor")
                    if c == "ー" and i == 0:
                        raise ValueError("Japanese length marker has no consumed predecessor")
                phones = self._phones(original(word))
                records.append({"source": word.surface, "reading": hira, "phonemes": phones})
                return phones

            try:
                cutlet._romaji_word = observed
                expected = cutlet._normalize_text(source)
                phones, _ = cutlet(source)
            finally:
                if had_instance:
                    cutlet._romaji_word = prior
                else:
                    del cutlet._romaji_word
            if not records or "".join(r["source"] for r in records) != expected:
                raise ValueError("Japanese frontend did not consume its exact normalized source")
            return self._phones(phones), records

    def phonemize(self, source):
        if not isinstance(source, str) or not source.strip() or len(source) > 5000:
            raise ValueError("Expected 1 to 5000 source characters")
        spans, output, cursor = [], [], 0
        native = _han if self.locale == "zh" else _japanese
        while cursor < len(source):
            start = cursor
            char = source[cursor]
            trace = None
            if char.isspace():
                while cursor < len(source) and source[cursor].isspace():
                    cursor += 1
                phones, kind = " ", "space"
            elif char in _PUNCTUATION or char in _ASCII_PUNCTUATION:
                # Decimal points at the start of a numeric token are not punctuation.
                if char == "." and cursor + 1 < len(source) and source[cursor + 1].isdigit():
                    kind = "number"
                else:
                    cursor += 1
                    phones, kind = _PUNCTUATION.get(char, char), "punctuation"
            elif char.isascii() and char.isdigit() or char in "-−":
                kind = "number"
            elif native(char):
                cursor += 1
                while cursor < len(source) and native(source[cursor]):
                    cursor += 1
                phones, trace = self._native(source[start:cursor])
                kind = "cjk"
            else:
                atom = _OVERRIDE.match(source, cursor) or _ENGLISH.match(source, cursor)
                if atom is None:
                    raise ValueError("Unknown source requires explicit pronunciation")
                cursor = atom.end()
                # Preserve contiguous English word context, including articles
                # and existing explicit overrides, rather than isolating words.
                while cursor < len(source):
                    gap = re.match(r"[ \t]+", source[cursor:])
                    if gap is None:
                        break
                    end = cursor + gap.end()
                    following = _OVERRIDE.match(source, end) or _ENGLISH.match(source, end)
                    if following is None:
                        break
                    cursor = following.end()
                if cursor < len(source) and source[cursor].isascii() and source[cursor].isalnum():
                    raise ValueError("Mixed identifier cannot be split into a number")
                trace = CustomerEnglishBridge.phonemize(self.english, source[start:cursor])
                phones, kind = trace["phonemes"], "english"
            if kind == "number":
                if self.number_preparer is None:
                    raise ValueError("Locale number converter is missing")
                match = _NUMBER.match(source, start)
                if match is None or not any(c.isdigit() for c in match.group()):
                    raise ValueError("Malformed numeric source")
                cursor = match.end()
                if (
                    start
                    and source[start - 1].isascii()
                    and source[start - 1].isalnum()
                    or cursor < len(source)
                    and source[cursor] in ":+-−"
                    or cursor + 1 < len(source)
                    and source[cursor] in ".,"
                    and source[cursor + 1].isdigit()
                ):
                    raise ValueError("Time, identifier or grouped number requires explicit disambiguation")
                value = match.group()
                unit_match = re.match(r"[ \t]+(USD|JPY|CNY)(?![A-Za-z0-9])", source[cursor:])
                unit = unit_match.group(1) if unit_match else None
                if unit_match:
                    cursor += unit_match.end()
                elif cursor < len(source) and source[cursor].isascii() and source[cursor].isalpha():
                    raise ValueError("Unresolved unit or numeric identifier")
                trace = self.number_preparer(value, self.locale, unit=unit)
                if (
                    not isinstance(trace, dict)
                    or trace.get("source_value") != value
                    or trace.get("locale") != self.locale
                    or trace.get("unit") != unit
                ):
                    raise ValueError("Number preparation lost its source value or unit")
                chunks = trace.get("spoken_tokens") if self.locale == "ja" else [trace.get("spoken_text")]
                if (
                    not isinstance(chunks, list)
                    or not chunks
                    or any(not isinstance(x, str) or not x or any(not native(c) for c in x) for x in chunks)
                ):
                    raise ValueError("Number preparation contains unconsumed characters")
                converted = [self._native(chunk) for chunk in chunks]
                phones = " ".join(part[0] for part in converted)
                trace = dict(trace, converted_tokens=[part[1] for part in converted])
            if cursor <= start:
                raise ValueError("No source progress")
            self._phones(phones) if kind != "space" else None
            output.append(phones)
            spans.append(
                {
                    "start": start,
                    "end": cursor,
                    "source": source[start:cursor],
                    "kind": kind,
                    "phonemes": phones,
                    "trace": trace,
                }
            )
        if "".join(span["source"] for span in spans) != source:
            raise ValueError("Incomplete source coverage")
        return {"source": source, "locale": self.locale, "phonemes": "".join(output), "spans": spans}
