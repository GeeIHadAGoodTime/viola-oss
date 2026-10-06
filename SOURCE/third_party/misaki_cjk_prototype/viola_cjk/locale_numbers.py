"""Unintegrated exact-decimal preparation for CJK qualification.

Callers supply the already-disambiguated unit. This module does not infer a
currency from a symbol, parse times/identifiers, change product settings, or
claim locale/audio qualification. Fractions remain strings throughout.
"""

from __future__ import annotations

import re
from typing import Callable

_NUMBER = re.compile(r"([-−]?)(0|[1-9][0-9]*)?(?:\.([0-9]{1,20}))?\Z")
_DIGITS_ZH = tuple("零一二三四五六七八九")
_DIGITS_JA = ("ゼロ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう")
# Japanese CNY reading: https://www.smbcnikko.co.jp/terms/japan/o/J0859.html
_UNITS = {
    "zh": {"USD": "美元", "JPY": "日元", "CNY": "人民币元"},
    "ja": {"USD": "米ドル", "JPY": "円", "CNY": "じんみんげん"},
}


def prepare_exact_number(
    value: str, locale: str, *, integer_converter: Callable[[str], str], unit: str | None = None
) -> dict:
    if locale not in _UNITS or not isinstance(value, str):
        raise ValueError("An explicit supported locale and exact decimal string are required")
    match = _NUMBER.fullmatch(value)
    if match is None or (match.group(2) is None and match.group(3) is None):
        raise ValueError("Value is outside the bounded exact-decimal contract")
    sign, integer, fraction = match.groups()
    # Omitted whole digits mean exact zero, without changing the raw source trace.
    integer = integer or "0"
    if len(integer) > (9 if locale == "ja" else 16):
        raise ValueError("Integer exceeds the pinned locale converter's supported range")
    if unit is not None and unit not in _UNITS[locale]:
        raise ValueError("Unit must already be resolved to a supported explicit currency code")
    whole = integer_converter(integer)
    if not isinstance(whole, str) or not whole or any(c.isdigit() for c in whole):
        raise ValueError("Locale integer conversion is incomplete")
    digits = _DIGITS_JA if locale == "ja" else _DIGITS_ZH
    prefix = ("マイナス" if locale == "ja" else "负") if sign else ""
    point = "てん" if locale == "ja" else "点"
    tail = point + "".join(digits[int(c)] for c in fraction) if fraction is not None else ""
    suffix = _UNITS[locale].get(unit, "")
    # Keep numeric reading units apart in Japanese: joined kana can be
    # retokenized as unrelated words and homographic currency kanji misread.
    spoken_tokens = ([prefix] if prefix else []) + whole.split()
    if fraction is not None:
        spoken_tokens += [point] + [digits[int(c)] for c in fraction]
    if suffix:
        spoken_tokens.append(suffix)
    spoken = " ".join(spoken_tokens) if locale == "ja" else prefix + whole + tail + suffix
    return {
        "source_value": value,
        "locale": locale,
        "unit": unit,
        "spoken_text": spoken,
        "spoken_tokens": spoken_tokens,
        "fraction_digits": fraction,
        "fraction_digit_names": [digits[int(c)] for c in fraction or ""],
        "release_eligible": False,
    }
