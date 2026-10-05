"""Small English speech number converter, independently implemented for Viola.

This module contains no num2words code and has no optional dependencies. It
implements only the cardinal, ordinal and year forms used by Viola and Misaki.
Values outside the bounded contract fail explicitly rather than truncate.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALES = ("", "thousand", "million", "billion", "trillion", "quadrillion", "quintillion")
_ORDINALS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
_MAX_ABS = 10 ** (3 * len(_SCALES))


def _under_thousand(number: int) -> str:
    if number < 20:
        return _ONES[number]
    if number < 100:
        tens, units = divmod(number, 10)
        return _TENS[tens] + ("-" + _ONES[units] if units else "")
    hundreds, remainder = divmod(number, 100)
    return _ONES[hundreds] + " hundred" + (" and " + _under_thousand(remainder) if remainder else "")


def _cardinal_integer(number: int) -> str:
    if abs(number) >= _MAX_ABS:
        raise ValueError("English speech number is outside the supported range")
    if number < 0:
        return "minus " + _cardinal_integer(-number)
    if number < 1000:
        return _under_thousand(number)
    groups = []
    index = 0
    while number:
        number, group = divmod(number, 1000)
        if group:
            groups.append((_under_thousand(group) + (" " + _SCALES[index] if index else ""), index, group))
        index += 1
    groups.reverse()
    result = groups[0][0]
    for words, index, group in groups[1:]:
        result += (" and " if index == 0 and group < 100 else ", ") + words
    return result


def num2words(number: object, *, to: str = "cardinal", lang: str = "en") -> str:
    """Convert finite English numbers; signature matches the retained callers."""
    if lang not in {"en", "en_US", "en_GB"} or to not in {"cardinal", "ordinal", "year"}:
        raise ValueError("Unsupported English speech conversion")
    if isinstance(number, bool):
        raise ValueError("Boolean is not a speech number")
    try:
        # Bound input before Decimal expansion, including huge exponent strings.
        raw = str(number)
        if len(raw) > 128:
            raise ValueError("English speech number is too long")
        value = Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid English speech number") from exc
    if not value.is_finite() or value.copy_abs() >= _MAX_ABS or value.as_tuple().exponent < -20:
        raise ValueError("English speech number is outside the supported range")
    integral = int(value)
    if to in {"ordinal", "year"}:
        if value != integral or integral < 0:
            raise ValueError("Ordinal and year speech require a non-negative integer")
        if to == "year":
            if not 0 <= integral <= 9999:
                raise ValueError("English speech year must be between 0 and 9999")
            century, remainder = divmod(integral, 100)
            if century and (integral < 2000 or integral >= 2010):
                suffix = (
                    "hundred" if not remainder else (("oh-" if remainder < 10 else "") + _cardinal_integer(remainder))
                )
                return _cardinal_integer(century) + " " + suffix
            return _cardinal_integer(integral)
        words = _cardinal_integer(integral)
        split = max(words.rfind(" "), words.rfind("-"))
        last = words[split + 1 :]
        ending = _ORDINALS.get(last, last[:-1] + "ieth" if last.endswith("y") else last + "th")
        return words[: split + 1] + ending
    if value == integral:
        return _cardinal_integer(integral)
    fixed = format(value.copy_abs(), "f")
    whole, fraction = fixed.split(".")
    return (
        ("minus " if value < 0 else "")
        + _cardinal_integer(int(whole))
        + " point "
        + " ".join(_ONES[int(c)] for c in fraction)
    )
