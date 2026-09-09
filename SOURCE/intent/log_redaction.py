"""Redaction helpers for persisted logs and runtime artifacts.

The card-data layer is intentionally narrow and deterministic: it masks
PAN-shaped strings and card security codes before data reaches durable logs.
The PII layer masks common low-ambiguity identifiers (email, phone, SSN).

Not redacted by default:
- Names and street addresses, because precision is poor without a structured
  address parser and false positives would damage replay/debug value.
- Expiration dates, because month/year alone has low standalone risk and is
  often needed to debug checkout form routing.
"""

from __future__ import annotations

import re
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
_REDACTED_EMAIL = "[REDACTED:EMAIL]"
_REDACTED_PHONE = "[REDACTED:PHONE]"
_REDACTED_SSN = "[REDACTED:SSN]"
_REDACTED_IP = "[REDACTED:IP]"
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


def _redact_card_string(value: str, key_path: tuple[str, ...]) -> str:
    try:
        redacted = _CVC_INLINE_RE.sub(r"\1***\3", value)
        redacted = _redact_spoken_cvc(redacted)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR
    if _has_phone_context(key_path):
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


def _redact_pii_string(value: str) -> str:
    redacted = _EMAIL_RE.sub(_REDACTED_EMAIL, value)
    redacted = _PHONE_RE.sub(_replace_phone, redacted)
    redacted = _SSN_RE.sub(_REDACTED_SSN, redacted)
    return mask_secrets_in_text(_IPV4_RE.sub(_replace_ipv4, redacted), include_card_data=False)


def _redact_pii_any(value: Any, key_path: tuple[str, ...], seen: set[int], depth: int) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return _REDACTED_MAX_DEPTH
    if isinstance(value, str):
        if key_path and key_path[-1] in _SSN_KEYS:
            return _REDACTED_SSN
        return _redact_pii_string(value)
    if isinstance(value, int) and not isinstance(value, bool):
        if key_path and key_path[-1] in _SSN_KEYS:
            return _REDACTED_SSN
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

    This masks email addresses, US-style phone numbers, and SSNs. It does not
    attempt to detect names or street addresses.
    """

    try:
        return _redact_pii_any(blob, (), set(), 0)
    except _SANITIZER_EXCEPTIONS:
        return _REDACTED_SANITIZER_ERROR


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
