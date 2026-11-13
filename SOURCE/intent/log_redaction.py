"""Redaction helpers for persisted logs and runtime artifacts.

The card-data layer is intentionally narrow and deterministic: it masks
PAN-shaped strings and card security codes before data reaches durable logs.
The PII layer masks common identifiers (email, phone, SSN, street address).

Street addresses are recognised by shape (house number, street name, street
type) plus the address fields of structured payloads, and over-masking a log
line is the accepted cost: Viola logged the home address given for an order
while masking the phone number next to it (1.0.5 build 9).

Not redacted by default:
- Names, because precision is poor and false positives would damage replay/debug
  value.
- Expiration dates, because month/year alone has low standalone risk and is
  often needed to debug checkout form routing.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.secrets_mask import mask_dict_secrets, mask_secrets_in_text
from services.credential_boundary import is_plausible_card_pan

_PAN_RE = re.compile(
    r"(?<![\d.])(\d{4}(?:[-\s]*\d{4}){3}(?:[-\s]*\d{1,3})?|\d{4}(?:[-\s]*\d{4}){2}[-\s]*\d{1,4})(?![-\s]*\d|\.)"
)
_CVC_INLINE_RE = re.compile(
    r"(?i)([\"']?\b(?:cvc2?|cvv2?|card[_\s-]?(?:cvc|cvv)|payment[_\s-]?(?:cvc|cvv)|local[_\s-]?payment[_\s-]?(?:cvc|cvv)|security[_\s-]?code|card[_\s-]?security[_\s-]?code)\b[\"']?(?:\s*[:=]\s*|\s+)[\"']?)(\d{3,4})([\"']?)"
)
_SPOKEN_DIGIT_WORDS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
_SPOKEN_DIGIT_TOKEN_RE = re.compile(r"\b(?:%s)\b" % "|".join(_SPOKEN_DIGIT_WORDS), re.IGNORECASE)
_SPOKEN_CVC_INLINE_RE = re.compile(
    r"(?i)(\b(?:cvc2?|cvv2?|security[_\s-]?code|card[_\s-]?security[_\s-]?code)\b(?:\s*[:=]\s*|\s+))"
    r"((?:(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)[\s,.-]*){3,4})"
)
_SPOKEN_NUMERIC_CVC_INLINE_RE = re.compile(
    r"(?i)(\b(?:cvc2?|cvv2?|security[_\s-]?code|card[_\s-]?security[_\s-]?code)\b(?:\s*[:=]\s*|\s+))"
    r"((?:\d[\s,.-]*){3,4})(?!\d)"
)
_SPOKEN_NUMERIC_PAN_RE = re.compile(r"(?<!\d)((?:\d[\s,.-]+){12,18}\d)(?!\d)")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w-])")
_PHONE_RE = re.compile(r"(?<![\w/-])(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?![\w/-])")
_SSN_RE = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_PHONE_CONTEXT_CHARS = frozenset("0123456789+().- ")
# "7228 W Honeycreek Dr", "four two one one north main street", "12 Oak Ave Apt 3,
# Milwaukee, WI 53219", and the same in a URL query ("7228+W+Honeycreek+Dr"). The
# number must stand alone, so "76th Street" (a place name) is left alone.
_ADDRESS_GAP = r"(?:[\s+]|%20)+"
_ADDRESS_NUMBER_WORD = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_ADDRESS_NOT_NAME = r"(?!(?:the|of|in|to|for|a|an|and|is|on|at|by|with|from|or|as|it|this|that)\b)"
_ADDRESS_STREET_TYPE = (
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct|way|place|pl|"
    r"circle|cir|terrace|ter|highway|hwy|parkway|pkwy|trail|trl|square|sq|loop|alley|aly|crossing|xing|run|pike)"
)
_STREET_ADDRESS_RE = re.compile(
    r"(?i)(?<![\w.,:/#$-])"
    # "48.12 Maple Street": speech-to-text writes a house number said as two
    # pairs with a point or a comma in it, and that is still a house number.
    r"(?:\d{1,6}(?:[.,]\d{1,6})?[a-z]?|%(word)s(?:[\s-]+%(word)s){1,6})"
    r"(?:%(gap)s(?:n|s|e|w|ne|nw|se|sw|north|south|east|west)\.?)?"
    r"(?:%(gap)s%(not_name)s[a-z][a-z'.-]*|%(gap)s\d{1,3}(?:st|nd|rd|th)){1,4}?"
    r"%(gap)s%(street_type)s\b\.?"
    r"(?:,?%(gap)s(?:apt|apartment|unit|suite|ste|#)\.?%(gap)s?[\w-]{1,6})?"
    r"(?:,?%(gap)s[a-z][a-z.-]*(?:%(gap)s[a-z][a-z.-]*)?,?%(gap)s[a-z]{2}%(gap)s\d{5}(?:-\d{4})?)?"
    % {
        "word": _ADDRESS_NUMBER_WORD,
        "gap": _ADDRESS_GAP,
        "not_name": _ADDRESS_NOT_NAME,
        "street_type": _ADDRESS_STREET_TYPE,
    }
)
_CITY_STATE_ZIP_RE = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)?, [A-Z]{2},? \d{5}(?:-\d{4})?\b")
_LABELLED_ZIP_RE = re.compile(r"(?i)\b(zip(?:[\s_-]?code)?|postal[\s_-]?code)\b([\s\"':=]+)\d{5}(?:-\d{4})?\b")
# A phone number as speech-to-text writes it when Viola reads one back ("plus 1,
# 4, 1, 4, 5, 5, 5, ...", "+1, 414, 555, 1234", "four one four five five five
# ...") or as an E.164 string outside the US layout ("+447946095812"). The plain
# US pattern above misses every one of these, and a self-heard confirm prompt put
# a full number into viola-qt.log that way (1.0.5 laptop, session 4).
_SPOKEN_PHONE_TOKEN = r"(?:\d{1,4}|zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_SPOKEN_PHONE_RUN_RE = re.compile(
    r"(?i)(?<![\w.:/+-])(?P<prefix>(?:\+|\bplus\b)[\s,]*)?"
    r"(?P<body>%s(?:(?:[\s,]+|\s*-\s*|\.\s+)%s)+)"
    r"(?![\w:/-]|\.\d)" % (_SPOKEN_PHONE_TOKEN, _SPOKEN_PHONE_TOKEN)
)
_E164_PHONE_RE = re.compile(r"(?<![\w+])\+\d{8,15}(?!\d)")
_THOUSANDS_NUMBER_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_SPOKEN_PHONE_TOKEN_SPLIT_RE = re.compile(r"[\s,.-]+")
_REDACTED_EMAIL = "[REDACTED:EMAIL]"
_REDACTED_PHONE = "[REDACTED:PHONE]"
_REDACTED_SSN = "[REDACTED:SSN]"
_REDACTED_IP = "[REDACTED:IP]"
_REDACTED_ADDRESS = "[REDACTED:ADDRESS]"
_REDACTED_SANITIZER_ERROR = "[REDACTED:SANITIZER_ERROR]"
_REDACTED_CYCLE = "[REDACTED:CYCLE]"
_REDACTED_MAX_DEPTH = "[REDACTED:MAX_DEPTH]"
_MAX_REDACTION_DEPTH = 80
_SANITIZER_EXCEPTIONS = (
    ArithmeticError,
    AttributeError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
    re.error,
)

_CARD_NUMBER_KEYS = {
    "number",
    "cardnumber",
    "card_number",
    "cardno",
    "card",
    "cardpan",
    "primaryaccountnumber",
    "pan",
    "paymentcardnumber",
    "localpaymentcardnumber",
}
_CVC_KEYS = {
    "cvc",
    "cvv",
    "cvc2",
    "cvv2",
    "cardcvc",
    "cardcvv",
    "cardsecuritycode",
    "localpaymentcvc",
    "localpaymentcvv",
    "paymentcvc",
    "paymentcvv",
    "securitycode",
    "security_code",
    "cvconeshot",
    "oneshotcvc",
    "cvcsingleuse",
    "singleusecvc",
}
_PHONE_CONTEXT_KEYS = {
    "phone",
    "phonenumber",
    "mobile",
    "mobilephone",
    "telephone",
    "tel",
    "fax",
    "contactphone",
}
_SSN_KEYS = {
    "ssn",
    "socialsecuritynumber",
}
# Normalized field names. Plain "address" is not here: it also means IP and mail
# addresses, and its value still gets the text patterns.
_ADDRESS_KEYS = {
    "street",
    "streetaddress",
    "street1",
    "street2",
    "line1",
    "line2",
    "addressline1",
    "addressline2",
    "address1",
    "address2",
    "deliveryaddress",
    "shippingaddress",
    "billingaddress",
    "homeaddress",
    "mailingaddress",
    "zip",
    "zipcode",
    "postcode",
    "postalcode",
}


def _normalize_key(key: Any) -> str:
    try:
        text = str(key)
    except _SANITIZER_EXCEPTIONS:
        return ""
    return re.sub(r"[^a-z0-9]", "", text.strip().lower())


def _has_phone_context(key_path: tuple[str, ...]) -> bool:
    return any(part in _PHONE_CONTEXT_KEYS for part in key_path)


def _luhn_valid(digits: str) -> bool:
    """Bare Luhn checksum only -- kept for `scripts/sanitize_logs.py`'s
    diagnostic count and as a building block. Generic (non-key-scoped) PAN
    *detection* in this module must go through `is_plausible_card_pan`
    instead: an arbitrary 13-19 digit run (a timestamp, a counter, ...) has
    roughly a 1-in-10 chance of passing Luhn by pure chance, so Luhn alone
    over-detects. See issue #1811.
    """
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    if len(set(digits)) == 1:
        return False
    total = 0
    double = False
    for char in reversed(digits):
        value = int(char)
        if double:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        double = not double
    return total % 10 == 0


def _mask_digits(digits: str) -> str:
    if len(digits) <= 8:
        return "*" * len(digits)
    return "%s%s%s" % (digits[:4], "*" * (len(digits) - 8), digits[-4:])


def _mask_card_field_value(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> Any:
    if isinstance(value, (str, int)):
        text = str(value)
        digits = re.sub(r"\D", "", text)
        if 13 <= len(digits) <= 19:
            return _mask_digits(digits)
        return _redact_card_string(text, key_path)
    return _redact_card_any(value, key_path, seen, depth)


# The trace writer, the step log and the task log redact the same strings
# (system prompt, tool schemas, earlier messages) on every turn: about 0.6 s of
# regex work per turn in the trace writer alone. That work held the GIL while
# the first spoken sentence was being synthesized (live, 2026-10-31). A string's
# redaction depends only on the string and on whether it sits under a phone
# field, so an identical string reuses its earlier result. Bounded.
_CARD_STRING_MEMO: OrderedDict[tuple[bool, str], str] = OrderedDict()
_CARD_STRING_MEMO_LOCK = threading.Lock()
_CARD_STRING_MEMO_MIN_CHARS = 24
_CARD_STRING_MEMO_MAX_ENTRIES = 20_000
_CARD_STRING_MEMO_MAX_CHARS = 16_000_000
_card_string_memo_chars = 0


def _redact_card_string(value: str, key_path: tuple[str, ...]) -> str:
    global _card_string_memo_chars
    phone_context = _has_phone_context(key_path)
    if len(value) < _CARD_STRING_MEMO_MIN_CHARS or len(value) > _CARD_STRING_MEMO_MAX_CHARS // 8:
        return _redact_card_string_uncached(value, phone_context)
    key = (phone_context, value)
    with _CARD_STRING_MEMO_LOCK:
        cached = _CARD_STRING_MEMO.get(key)
        if cached is not None:
            _CARD_STRING_MEMO.move_to_end(key)
            return cached
    redacted = _redact_card_string_uncached(value, phone_context)
    if redacted == _REDACTED_SANITIZER_ERROR:
        return redacted
    with _CARD_STRING_MEMO_LOCK:
        if key not in _CARD_STRING_MEMO:
            _CARD_STRING_MEMO[key] = redacted
            _card_string_memo_chars += len(value)
            while _CARD_STRING_MEMO and (
                len(_CARD_STRING_MEMO) > _CARD_STRING_MEMO_MAX_ENTRIES
                or _card_string_memo_chars > _CARD_STRING_MEMO_MAX_CHARS
            ):
                (_phone, old_value), _old = _CARD_STRING_MEMO.popitem(last=False)
                _card_string_memo_chars -= len(old_value)
    return redacted


def _redact_card_string_uncached(value: str, phone_context: bool) -> str:
    try:
        redacted = _CVC_INLINE_RE.sub(r"\1***\3", value)
        redacted = _redact_spoken_cvc(redacted)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR
    if phone_context:
        try:
            return mask_secrets_in_text(redacted, include_card_data=False)
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR

    def _replace_pan(match: re.Match[str]) -> str:
        candidate = match.group(1)
        digits = re.sub(r"\D", "", candidate)
        if is_plausible_card_pan(digits):
            return _mask_digits(digits)
        return candidate

    try:
        redacted = _PAN_RE.sub(_replace_pan, redacted)
        redacted = _redact_spoken_pan(redacted)
        return mask_secrets_in_text(redacted, include_card_data=False)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR


def _spoken_words_to_digits(value: str) -> str:
    words = _SPOKEN_DIGIT_TOKEN_RE.findall(value)
    return "".join(_SPOKEN_DIGIT_WORDS[word.lower()] for word in words)


def _redact_spoken_cvc(value: str) -> str:
    def _replace_words(match: re.Match[str]) -> str:
        digits = _spoken_words_to_digits(match.group(2))
        if 3 <= len(digits) <= 4:
            return "%s***" % match.group(1)
        return match.group(0)

    def _replace_numeric(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(2))
        if 3 <= len(digits) <= 4:
            return "%s***" % match.group(1)
        return match.group(0)

    redacted = _SPOKEN_CVC_INLINE_RE.sub(_replace_words, value)
    return _SPOKEN_NUMERIC_CVC_INLINE_RE.sub(_replace_numeric, redacted)


def _redact_spoken_pan(value: str) -> str:
    redacted = _redact_spoken_numeric_pan(value)
    matches = list(_SPOKEN_DIGIT_TOKEN_RE.finditer(redacted))
    if not matches:
        return redacted

    spans: list[tuple[int, int]] = []
    run_start = 0
    for idx in range(1, len(matches) + 1):
        should_end = idx == len(matches)
        if not should_end:
            gap = value[matches[idx - 1].end() : matches[idx].start()]
            should_end = bool(re.search(r"[^,\s.-]", gap))
        if should_end:
            run = matches[run_start:idx]
            digits = "".join(_SPOKEN_DIGIT_WORDS[item.group(0).lower()] for item in run)
            if is_plausible_card_pan(digits):
                spans.append((run[0].start(), run[-1].end()))
            run_start = idx

    if not spans:
        return value

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(redacted[cursor:start])
        parts.append("[REDACTED:CARD_NUMBER]")
        cursor = end
    parts.append(redacted[cursor:])
    return "".join(parts)


def _redact_spoken_numeric_pan(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(1))
        if is_plausible_card_pan(digits):
            return "[REDACTED:CARD_NUMBER]"
        return match.group(0)

    return _SPOKEN_NUMERIC_PAN_RE.sub(_replace, value)


def _redact_card_any(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return _REDACTED_MAX_DEPTH
    if isinstance(value, str):
        return _redact_card_string(value, key_path)
    if isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
        if not _has_phone_context(key_path) and is_plausible_card_pan(text):
            return _mask_digits(text)
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        redacted: dict[Any, Any] = {}
        try:
            for key, inner in value.items():
                normalized = _normalize_key(key)
                child_path = (*key_path, normalized)
                if normalized in _CVC_KEYS:
                    redacted[key] = None if inner is None else "***"
                elif normalized in _CARD_NUMBER_KEYS and not _has_phone_context(key_path):
                    redacted[key] = _mask_card_field_value(inner, child_path, seen, depth + 1)
                else:
                    redacted[key] = _redact_card_any(inner, child_path, seen, depth + 1)
            return redacted
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return [_redact_card_any(item, key_path, seen, depth + 1) for item in value]
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, tuple):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return tuple(_redact_card_any(item, key_path, seen, depth + 1) for item in value)
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, set):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            redacted_items = [_redact_card_any(item, key_path, seen, depth + 1) for item in value]
            try:
                return set(redacted_items)
            except TypeError:
                return redacted_items
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    return value


def redact_card_data(blob: Any) -> Any:
    """Return a copy of *blob* with PAN/CVC card data masked.

    Generic string scanning only masks unbroken 13-19 digit values that pass
    Luhn validation. Card-number fields are masked by key even when the value
    includes common separators. CVC/CVV/security-code fields are replaced with
    ``"***"``.
    """

    try:
        return _redact_card_any(blob, (), set(), 0)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR


def redact_card_field(key: Any, value: Any) -> Any:
    """Redact one top-level field exactly as ``redact_card_data({key: value, ...})`` would.

    Lets a caller redact a large field once and reuse the result (the agent
    step log memoizes its static tool-schema fields). Raises what the sanitizer
    raises, so the caller can fall back to redacting the whole mapping.
    """
    normalized = _normalize_key(key)
    child_path = (normalized,)
    if normalized in _CVC_KEYS:
        return None if value is None else "***"
    if normalized in _CARD_NUMBER_KEYS and not _has_phone_context(()):
        return _mask_card_field_value(value, child_path, set(), 1)
    return _redact_card_any(value, child_path, set(), 1)


def _match_inside_url_token(value: str, start: int, end: int) -> bool:
    left = start
    while left > 0 and not value[left - 1].isspace():
        left -= 1
    right = end
    while right < len(value) and not value[right].isspace():
        right += 1
    token = value[left:right].lower()
    return "://" in token or token.startswith("www.")


def _phone_numeric_context_has_too_many_digits(value: str, start: int, end: int) -> bool:
    left = start
    while left > 0 and value[left - 1] in _PHONE_CONTEXT_CHARS:
        left -= 1
    right = end
    while right < len(value) and value[right] in _PHONE_CONTEXT_CHARS:
        right += 1
    digits = re.sub(r"\D", "", value[left:right])
    return len(digits) > 11


def _replace_phone(match: re.Match[str]) -> str:
    text = match.string
    if _match_inside_url_token(text, match.start(), match.end()):
        return match.group(0)
    if _phone_numeric_context_has_too_many_digits(text, match.start(), match.end()):
        return match.group(0)
    return _REDACTED_PHONE


def _replace_ipv4(match: re.Match[str]) -> str:
    octets = match.group(0).split(".")
    if all(0 <= int(octet) <= 255 for octet in octets):
        return _REDACTED_IP
    return match.group(0)


def _spoken_phone_digits(tokens: list[str]) -> str:
    return "".join(token if token.isdigit() else _SPOKEN_DIGIT_WORDS[token.lower()] for token in tokens)


def _is_counting_sequence(tokens: list[str]) -> bool:
    """``1, 2, 3, ... 10`` is a list, not a phone number."""
    if not all(token.isdigit() for token in tokens):
        return False
    values = [int(token) for token in tokens]
    return all(later == earlier + 1 for earlier, later in itertools.pairwise(values))


def _replace_spoken_phone(match: re.Match[str]) -> str:
    body = match.group("body")
    tokens = [token for token in _SPOKEN_PHONE_TOKEN_SPLIT_RE.split(body) if token]
    if not tokens:
        return match.group(0)
    digits = _spoken_phone_digits(tokens)
    if match.group("prefix"):
        # "+" or "plus" in front of the run: any E.164 length.
        return _REDACTED_PHONE if 8 <= len(digits) <= 15 else match.group(0)
    if not 10 <= len(digits) <= 15:
        return match.group(0)
    if _THOUSANDS_NUMBER_RE.match(body.strip()) or _is_counting_sequence(tokens):
        return match.group(0)
    if all(token.isdigit() for token in tokens) and not re.search(r"[,\s]", body):
        # Only hyphens/dots between groups: dates and ids, and the US pattern
        # above already covers hyphenated phone numbers.
        return match.group(0)
    return _REDACTED_PHONE


def _redact_spoken_phone(value: str) -> str:
    redacted = _E164_PHONE_RE.sub(_REDACTED_PHONE, value)
    return _SPOKEN_PHONE_RUN_RE.sub(_replace_spoken_phone, redacted)


def _redact_street_address(value: str) -> str:
    redacted = _STREET_ADDRESS_RE.sub(_REDACTED_ADDRESS, value)
    redacted = _CITY_STATE_ZIP_RE.sub(_REDACTED_ADDRESS, redacted)
    return _LABELLED_ZIP_RE.sub(lambda match: match.group(1) + match.group(2) + _REDACTED_ADDRESS, redacted)


def _redact_pii_string(value: str) -> str:
    redacted = _EMAIL_RE.sub(_REDACTED_EMAIL, value)
    redacted = _PHONE_RE.sub(_replace_phone, redacted)
    redacted = _redact_spoken_phone(redacted)
    redacted = _SSN_RE.sub(_REDACTED_SSN, redacted)
    redacted = _redact_street_address(redacted)
    return mask_secrets_in_text(_IPV4_RE.sub(_replace_ipv4, redacted), include_card_data=False)


def _redact_pii_any(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return _REDACTED_MAX_DEPTH
    if isinstance(value, str):
        if key_path and key_path[-1] in _SSN_KEYS:
            return _REDACTED_SSN
        if key_path and key_path[-1] in _ADDRESS_KEYS and value.strip():
            return _REDACTED_ADDRESS
        return _redact_pii_string(value)
    if isinstance(value, int) and not isinstance(value, bool):
        if key_path and key_path[-1] in _SSN_KEYS:
            return _REDACTED_SSN
        if key_path and key_path[-1] in _ADDRESS_KEYS:
            return _REDACTED_ADDRESS
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return {
                key: _redact_pii_any(inner, (*key_path, _normalize_key(key)), seen, depth + 1)
                for key, inner in value.items()
            }
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return [_redact_pii_any(item, key_path, seen, depth + 1) for item in value]
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, tuple):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            return tuple(_redact_pii_any(item, key_path, seen, depth + 1) for item in value)
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    if isinstance(value, set):
        value_id = id(value)
        if value_id in seen:
            return _REDACTED_CYCLE
        seen.add(value_id)
        try:
            redacted_items = [_redact_pii_any(item, key_path, seen, depth + 1) for item in value]
            try:
                return set(redacted_items)
            except TypeError:
                return redacted_items
        except _SANITIZER_EXCEPTIONS:
            return _REDACTED_SANITIZER_ERROR
        finally:
            seen.discard(value_id)
    return value


def redact_pii(blob: dict[str, Any] | str) -> dict[str, Any] | str:
    """Return a copy of *blob* with low-ambiguity PII masked.

    This masks email addresses, US-style phone numbers, SSNs and street
    addresses. It does not attempt to detect names.
    """

    try:
        return _redact_pii_any(blob, (), set(), 0)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR


# Tools whose arguments are the user's home address. Their arguments and results
# are masked in plain-text logs because of which tool it is, never because of
# what the text looks like: a house number heard as "48.12" matched no address
# pattern and the whole street line reached viola-qt.log (1.0.5 build 10).
ADDRESS_ARGUMENT_TOOLS = frozenset({"save_delivery_address"})


def _bare_tool_name(tool_name: Any) -> str:
    name = str(tool_name or "").strip()
    for separator in ("__", "."):
        if separator in name:
            name = name.rsplit(separator, 1)[1]
    return name


def is_address_argument_tool(tool_name: Any) -> bool:
    """True for a tool whose arguments are a street address, with or without a server prefix."""
    return _bare_tool_name(tool_name) in ADDRESS_ARGUMENT_TOOLS


def redact_tool_arguments_for_log(tool_name: Any, tool_args: Any) -> Any:
    """Return tool arguments that are safe to print in a plain-text log line.

    An address tool has every argument masked. Any other tool has its address,
    phone and card fields masked by field name before the values are joined into
    one string, where only a pattern could still recognise them.
    """
    if is_address_argument_tool(tool_name):
        if isinstance(tool_args, Mapping):
            return {
                key: _REDACTED_ADDRESS if value not in (None, "", [], {}) else value for key, value in tool_args.items()
            }
        return _REDACTED_ADDRESS
    if isinstance(tool_args, (Mapping, list, tuple, str)):
        return redact_pii(redact_card_data(tool_args))
    return tool_args


def redact_tool_result_for_log(tool_name: Any, result_text: Any) -> Any:
    """The result of an address tool is the address read back: never printed."""
    if is_address_argument_tool(tool_name):
        return _REDACTED_ADDRESS
    return result_text


def redact_tool_error_for_log(tool_name: Any, tool_args: Any, error_text: Any) -> str:
    """An address tool's error with every argument value taken out.

    The error stays readable ("The address could not be saved."), but an
    exception or a validation message can quote an argument back.
    """
    text = str(error_text or "")
    if not is_address_argument_tool(tool_name) or not text:
        return text
    values = tool_args.values() if isinstance(tool_args, Mapping) else [tool_args]
    for value in sorted({str(v).strip() for v in values if v not in (None, "")}, key=len, reverse=True):
        if len(value) >= 3:  # a two-letter state code alone says nothing, and would mangle words
            text = text.replace(value, _REDACTED_ADDRESS)
    return text


def redact_diagnostic_payload(blob: Any) -> Any:
    """Redact secrets, card data, and low-ambiguity PII for logs/traces."""

    redacted = redact_pii(redact_card_data(blob))
    if isinstance(redacted, Mapping):
        masked = mask_dict_secrets(dict(redacted))
        return {key: redact_diagnostic_payload(value) for key, value in masked.items()}
    if isinstance(redacted, list):
        return [redact_diagnostic_payload(item) for item in redacted]
    if isinstance(redacted, tuple):
        return tuple(redact_diagnostic_payload(item) for item in redacted)
    if isinstance(redacted, set):
        return {redact_diagnostic_payload(item) for item in redacted}
    if isinstance(redacted, str):
        return mask_secrets_in_text(redacted, include_card_data=False)
    return redacted
