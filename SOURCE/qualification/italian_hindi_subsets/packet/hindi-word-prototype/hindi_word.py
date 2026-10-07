"""Inactive, traced Hindi rule subset with strict source/vocabulary rejection.

An accepted word is representable by these pinned rules; it is not certified as
native pronunciation. No Epitran, model, dictionary or native module is imported.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import unicodedata


_HASHES = {
    "map": "47fda9f103ed0b70b812e28cb7589b03e58f8c9025bae99c20a45d126d8e57bb",
    "post": "6f21a68256a0732c1d1019e895563bec66b82bea53b75947e0a1072a442ea3e4",
}
_CONSONANTS = frozenset("कखगघङहचछजझञयशटठडढणरषतथदधनलसपफबभमव")
_INDEPENDENT = frozenset("अआइईउऊऋॠऌॡएऐओऔ")
_DEPENDENT = frozenset("ािीुूृॄॢॣेैोौॉ")
_SIGNS = frozenset("ँंः")
_UNQUALIFIED_PHONES = frozenset("ɦ\u0324\u0325\u0329")


class UnsupportedWord(ValueError):
    """No partial output may be used for an unsupported source word."""


@dataclass(frozen=True)
class Phone:
    symbol: str
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class Span:
    grapheme: str
    source_indices: tuple[int, ...]
    mapped_phones: str


@dataclass(frozen=True)
class Rewrite:
    rule: str
    removed: str
    inserted: str
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class Result:
    source: str
    normalized: str
    phonemes: str
    phones: tuple[Phone, ...]
    spans: tuple[Span, ...]
    rewrites: tuple[Rewrite, ...]
    consumed_source_indices: tuple[int, ...]
    qualification: str = "pinned-rule source/representation subset only"


@dataclass(frozen=True)
class _Unit:
    char: str
    owners: frozenset[int]


def _validate_syllables(word: str) -> None:
    if "अं" in word:
        raise UnsupportedWord("Pinned अं mapping loses the independent vowel")
    if word.endswith("ं"):
        raise UnsupportedWord("Final anusvara needs a qualified nasal-vowel rule")
    offset = 0
    while offset < len(word):
        char = word[offset]
        if char not in _CONSONANTS | _INDEPENDENT:
            raise UnsupportedWord("Expected a Devanagari consonant or independent vowel")
        offset += 1
        if char in _CONSONANTS:
            if offset < len(word) and word[offset] == "़":
                offset += 1
            if offset < len(word) and word[offset] == "्":
                offset += 1
                if offset < len(word) and word[offset] not in _CONSONANTS:
                    raise UnsupportedWord("A nonfinal virama must precede a consonant")
                continue
            if offset < len(word) and word[offset] in _DEPENDENT:
                offset += 1
        if offset < len(word) and word[offset] in _SIGNS:
            offset += 1


class HindiRepresentableWord:
    def __init__(self, vocabulary, data_dir: Path | None = None):
        self.vocabulary = frozenset(vocabulary)
        root = Path(data_dir) if data_dir is not None else Path(__file__).with_name("data")
        files = {}
        for name, expected in _HASHES.items():
            raw = (root / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise UnsupportedWord(f"Unqualified Hindi rule bytes: {name}")
            files[name] = raw.decode("utf-8")
        rows = list(csv.reader(io.StringIO(files["map"])))
        if rows.pop(0) != ["Orth", "Phon"]:
            raise UnsupportedWord("Unexpected map header")
        self.mapping = {unicodedata.normalize("NFD", key): unicodedata.normalize("NFD", value)
                        for key, value in rows}
        self.keys = sorted(self.mapping, key=len, reverse=True)
        self.rules = []
        symbols = {}
        for raw in files["post"].splitlines():
            line = unicodedata.normalize("NFD", raw.strip())
            if not line or line.startswith("%"):
                continue
            if line.startswith("::"):
                key, value = line.split(" = ", 1)
                symbols[key] = value
                continue
            expanded = line
            for key, value in symbols.items():
                expanded = expanded.replace(key, value)
            target, replacement, left, right = re.fullmatch(r"(\S+)\s*->\s*(\S+)\s*/\s*(\S*)\s*_\s*(\S*)", expanded).groups()
            left, right = left.replace("#", "^"), right.replace("#", "$")
            target, replacement = target.replace("0", ""), replacement.replace("0", "")
            pattern = re.compile(f"(?P<left>{left})(?P<target>{target})(?P<right>{right})")
            self.rules.append((line, pattern, replacement, "(?P<sw1>" in target))

    def phonemize(self, source: str) -> Result:
        if not isinstance(source, str) or not 1 <= len(source) <= 128:
            raise UnsupportedWord("Expected one word of 1–128 original code points")
        units = [_Unit(char, frozenset({i})) for i, original in enumerate(source)
                 for char in unicodedata.normalize("NFD", original)]
        normalized = "".join(unit.char for unit in units)
        _validate_syllables(normalized)
        spans = []
        output = []
        offset = 0
        while offset < len(units):
            key = next((key for key in self.keys if normalized.startswith(key, offset)), None)
            if key is None:
                raise UnsupportedWord(f"Unmapped source at original indices {sorted(units[offset].owners)}")
            mapped = self.mapping[key]
            if any(char in _UNQUALIFIED_PHONES for char in mapped):
                raise UnsupportedWord("Breathy, voiceless-rhotic, syllabic or ɦ features are not qualified")
            owners = frozenset().union(*(u.owners for u in units[offset : offset + len(key)]))
            spans.append(Span(key, tuple(sorted(owners)), mapped))
            output.extend(_Unit(char, owners) for char in mapped)
            offset += len(key)
        events = []
        for label, pattern, replacement, swap in self.rules:
            text = "".join(unit.char for unit in output)
            rewritten = []
            offset = 0
            for match in pattern.finditer(text):
                start, end = match.span("target")
                rewritten.extend(output[offset:start])
                owners = frozenset().union(*(u.owners for u in output[start:end]))
                if swap:
                    first, last = match.span("sw2")
                    inserted = output[first:last]
                    first, last = match.span("sw1")
                    inserted += output[first:last]
                else:
                    inserted = [_Unit(char, owners) for char in replacement]
                rewritten.extend(inserted)
                rewritten.extend(output[end:match.end()])
                events.append(Rewrite(label, text[start:end], "".join(u.char for u in inserted), tuple(sorted(owners))))
                offset = match.end()
            rewritten.extend(output[offset:])
            output = rewritten
        # Model's single-code-point affricates preserve the full mapped cluster.
        for source_phone, target in (("t͡ʃ", "ʧ"), ("d͡ʒ", "ʤ")):
            text = "".join(unit.char for unit in output)
            rewritten = []
            offset = 0
            for match in re.finditer(re.escape(source_phone), text):
                rewritten.extend(output[offset:match.start()])
                owners = frozenset().union(*(u.owners for u in output[match.start():match.end()]))
                rewritten.append(_Unit(target, owners))
                offset = match.end()
            rewritten.extend(output[offset:])
            output = rewritten
        if not output or any(unit.char not in self.vocabulary for unit in output):
            raise UnsupportedWord("Selected vocabulary cannot represent the whole word without loss")
        consumed = tuple(sorted({i for span in spans for i in span.source_indices}))
        if consumed != tuple(range(len(source))):
            raise UnsupportedWord("Some original source code points were not consumed")
        phones = tuple(Phone(unit.char, tuple(sorted(unit.owners))) for unit in output)
        return Result(source, normalized, "".join(p.symbol for p in phones), phones, tuple(spans),
                      tuple(events), consumed)
