"""Inactive, traced conversion of explicitly accented Italian words.

This is a source/representation experiment, not a lexical pronunciation engine.
Offsets always index Unicode code points in the original caller string.
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
    "map": "8207dbad4e96c104b25d23951be28e1e2eedfeaf73495b84938dbbbba60382ca",
    "pre": "98e391381506cccf1ed489ff6dba9412be557924f2a0540fcaff32137bc39927",
    "post": "fd4104d79ff9896686e7b7e8075115445f733405939c29cc91e18a952b0d6593",
    "strip": "ea73e40f19748cb5b516fb00adff366050ad9cd7b3df21cf3085c3a4c2b5c0fd",
}
_ACCENTS = "\u0300\u0301"
_VOWELS = frozenset("aeiouɛɔ")


class UnsupportedWord(ValueError):
    """The entire word needs a different, explicitly qualified route."""


@dataclass(frozen=True)
class Phone:
    symbol: str
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class Span:
    prepared: str
    source_indices: tuple[int, ...]
    mapped_phones: str
    explicit_stress: bool


@dataclass(frozen=True)
class Result:
    source: str
    normalized: str
    prepared: str
    phonemes: str
    phones: tuple[Phone, ...]
    spans: tuple[Span, ...]
    consumed_source_indices: tuple[int, ...]
    stress_phone_index: int
    stress_source_indices: tuple[int, ...]
    qualification: str = "explicit-accent source/representation subset only"


@dataclass(frozen=True)
class _Unit:
    char: str
    owners: frozenset[int]


def _rewrite(units: list[_Unit], pattern: str, replacement: str) -> list[_Unit]:
    text = "".join(unit.char for unit in units)
    output: list[_Unit] = []
    offset = 0
    for match in re.finditer(pattern, text):
        output.extend(units[offset : match.start()])
        owners = frozenset().union(*(u.owners for u in units[match.start() : match.end()]))
        output.extend(_Unit(char, owners) for char in replacement)
        offset = match.end()
    output.extend(units[offset:])
    return output


class ItalianExplicitStress:
    def __init__(self, vocabulary, data_dir: Path | None = None):
        self.vocabulary = frozenset(vocabulary)
        if "ˈ" not in self.vocabulary:
            raise UnsupportedWord("Selected vocabulary has no explicit-stress token")
        root = Path(data_dir) if data_dir is not None else Path(__file__).with_name("data")
        files = {}
        for name, expected in _HASHES.items():
            raw = (root / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise UnsupportedWord(f"Unqualified Italian rule bytes: {name}")
            files[name] = raw.decode("utf-8")
        rows = list(csv.reader(io.StringIO(files["map"])))
        if rows.pop(0) != ["Orth", "Phon"]:
            raise UnsupportedWord("Unexpected map header")
        self.mapping = dict(rows)
        # Local bounded correction; the retained upstream map is untouched.
        self.mapping.update({"<SC_SOFT>": "ʃ", "<SC_HARD>": "sk"})
        self.keys = sorted(self.mapping, key=len, reverse=True)
        self.pre_rules = []
        for line in files["pre"].splitlines():
            if not line or line.startswith("%"):
                continue
            left, right, context = re.fullmatch(r"(\w+) -> (<\w+>) / _ (\[[a-z]+\])", line).groups()
            self.pre_rules.append((re.escape(left) + "(?=" + context + ")", right))
        voiced = files["post"].splitlines()[0].split(" = ", 1)[1]
        self.voicing_pattern = "s(?=(?:" + voiced + "))"

    def phonemize(self, source: str) -> Result:
        if not isinstance(source, str) or not 1 <= len(source) <= 128:
            raise UnsupportedWord("Expected one word of 1–128 original code points")
        # NFC is used only to classify case. Original offsets remain untouched.
        case_view = unicodedata.normalize("NFC", source)
        if not (case_view.islower() or case_view.isupper() or case_view.istitle()):
            raise UnsupportedWord("Mixed case requires an explicit brand/initialism route")
        units = [
            _Unit(char, frozenset({index}))
            for index, original in enumerate(source)
            for char in unicodedata.normalize("NFD", original.lower())
        ]
        normalized = "".join(unit.char for unit in units)
        if any(char not in "abcdefghijklmnopqrstuvwxyz" + _ACCENTS for char in normalized):
            raise UnsupportedWord("Only Italian letters and one explicit acute/grave accent are covered")
        accents = [index for index, char in enumerate(normalized) if char in _ACCENTS]
        if len(accents) != 1 or accents[0] == 0 or normalized[accents[0] - 1] not in "aeiou":
            raise UnsupportedWord("Exactly one vowel must carry an explicit accent")
        # An unaccented i before another vowel can be spelling-only or vocalic.
        # Reject that unresolved distinction instead of silently dropping it.
        if re.search(r"sci(?=[aeou])", normalized):
            raise UnsupportedWord("Unaccented sci plus vowel needs lexical qualification")
        units = _rewrite(units, r"sc(?=[ei])", "<SC_SOFT>")
        units = _rewrite(units, r"sc(?!h)", "<SC_HARD>")
        for pattern, replacement in self.pre_rules:
            units = _rewrite(units, pattern, replacement)
        prepared = "".join(unit.char for unit in units)
        output: list[_Unit] = []
        spans = []
        stress_index = None
        stress_owners = ()
        offset = 0
        while offset < len(units):
            key = next((key for key in self.keys if prepared.startswith(key, offset)), None)
            if key is None:
                raise UnsupportedWord(f"Unmapped source at original indices {sorted(units[offset].owners)}")
            owners = frozenset().union(*(u.owners for u in units[offset : offset + len(key)]))
            mapped = self.mapping[key]
            accented = any(char in _ACCENTS for char in key)
            if accented:
                if mapped not in _VOWELS:
                    raise UnsupportedWord("Explicit accent does not map to one represented vowel")
                stress_index = len(output)
                stress_owners = tuple(sorted(owners))
            spans.append(Span(key, tuple(sorted(owners)), mapped, accented))
            output.extend(_Unit(char, owners) for char in mapped)
            offset += len(key)
        # Preserve a marked nucleus through postprocessing using its ownership.
        output = _rewrite(output, self.voicing_pattern, "z")
        for source_phone, target in (("t͡s", "ʦ"), ("t͡ʃ", "ʧ"), ("d͡ʒ", "ʤ")):
            output = _rewrite(output, re.escape(source_phone), target)
        candidates = [
            i for i, unit in enumerate(output)
            if unit.char in _VOWELS and tuple(sorted(unit.owners)) == stress_owners
        ]
        if stress_index is None or len(candidates) != 1:
            raise UnsupportedWord("The explicitly accented vowel was not preserved exactly once")
        stress_index = candidates[0]
        output.insert(stress_index, _Unit("ˈ", frozenset(stress_owners)))
        if any(unit.char not in self.vocabulary for unit in output):
            raise UnsupportedWord("Selected vocabulary cannot represent this word without loss")
        consumed = tuple(sorted({i for span in spans for i in span.source_indices}))
        if consumed != tuple(range(len(source))):
            raise UnsupportedWord("Some original source code points were not consumed")
        phones = tuple(Phone(unit.char, tuple(sorted(unit.owners))) for unit in output)
        return Result(source, normalized, prepared, "".join(p.symbol for p in phones), phones,
                      tuple(spans), consumed, stress_index, stress_owners)
