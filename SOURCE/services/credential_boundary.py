from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.secrets_mask import contains_maskable_secret

TIER3_CREDENTIAL_ERROR_CODE = "tier3_credential_not_allowed"

_TIER3_KEY_PARTS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "bearer",
        "billing_zip",
        "browser_profile",
        "byok",
        "card_label",
        "card_number",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "credit_card",
        "cvc",
        "cvv",
        "exp_month",
        "exp_year",
        "expiration_month",
        "expiration_year",
        "holder_name",
        "id_token",
        "last4",
        "oauth",
        "pan",
        "password",
        "payment_card",
        "payment_method",
        "payment_methods",
        "payment_vault",
        "private_key",
        "refresh_token",
        "secret",
        "session_cookie",
        "session_token",
        "token_value",
    }
)
_TIER3_EXACT_KEYS: frozenset[str] = frozenset({"jwt", "token"})
_TIER3_TOKEN_SUFFIXES: tuple[str, ...] = ("_token", "-token", ".token")
_TIER3_VALUE_MARKERS: frozenset[str] = frozenset(
    {
        "browser.cookie_export",
        "cookie_export",
        "cookies.sqlite",
        "login data",
        "network/cookies",
        "token_vault",
    }
)
_PAYMENT_CARD_PAN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_RAW_TIER3_KEY_RE = re.compile(
    rb'(?i)(["\'])(?:access[_-]?token|api[_-]?key|authorization|auth[_-]?token|bearer|browser[_-]?profile|'
    rb"client[_-]?secret|cookies?|credentials?|cvc|cvv|id[_-]?token|oauth|pan|password|private[_-]?key|"
    rb"refresh[_-]?token|secret|session[_-]?(?:cookie|token)|token)\1\s*:"
)
_RAW_TIER3_KEY_TEXT_RE = re.compile(
    r"""(?i)(["'])(?:access[_-]?token|api[_-]?key|authorization|auth[_-]?token|bearer|browser[_-]?profile|"""
    r"""client[_-]?secret|cookies?|credentials?|cvc|cvv|id[_-]?token|oauth|pan|password|private[_-]?key|"""
    r"""refresh[_-]?token|secret|session[_-]?(?:cookie|token)|token)\1\s*:"""
)
_RAW_BEARER_RE = re.compile(rb"(?i)(?:^|[^a-z0-9_])bearer\s+[a-z0-9._~+/\-=]{12,}")


@dataclass(frozen=True)
class Tier3CredentialViolation:
    field: str
    reason: str


class Tier3CredentialBoundaryError(ValueError):
    def __init__(self, *, field: str, surface: str, reason: str) -> None:
        self.field = field
        self.surface = surface
        self.reason = reason
        super().__init__("Tier-3 credential data is not allowed on %s: %s" % (surface, field))

    @property
    def details(self) -> dict[str, str]:
        return {"surface": self.surface, "field": self.field, "reason": self.reason}


def _normalize_token(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return normalized


def _compact_token(value: str) -> str:
    return "".join(character for character in value if character.isalnum())


def _payload_path(parent: str, key: object) -> str:
    key_text = str(key)
    return key_text if not parent else "%s.%s" % (parent, key_text)


def _luhn_valid(digits: str) -> bool:
    checksum = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def _matches_known_card_brand_iin(digits: str) -> bool:
    """True if `digits` starts with a real card network's IIN/BIN range AND
    has that network's real PAN length.

    Luhn alone is a weak signal: an arbitrary 13-19 digit run (a millisecond
    timestamp, a big counter, an order number, ...) has roughly a 1-in-10
    chance of passing Luhn by pure chance, since Luhn's check digit is only a
    function of the other digits. Every *real* card PAN also starts with one
    of a small set of publicly documented issuer-network prefixes at a fixed
    length, so requiring both is strictly more precise than Luhn alone: it
    cannot miss a genuine card number (every issued PAN matches one of these
    ranges), and it stops flagging incidental Luhn-valid digit runs that
    aren't card-shaped at all.
    """
    length = len(digits)
    # Visa: starts with 4; 13, 16, or 19 digits.
    if digits[0] == "4" and length in (13, 16, 19):
        return True
    if length == 16:
        # Mastercard: 51-55 or 2221-2720.
        prefix2 = int(digits[:2])
        if 51 <= prefix2 <= 55:
            return True
        prefix4 = int(digits[:4])
        if 2221 <= prefix4 <= 2720:
            return True
        # JCB: 3528-3589.
        if 3528 <= prefix4 <= 3589:
            return True
    if length == 15 and digits[:2] in ("34", "37"):
        # American Express.
        return True
    if length == 14:
        # Diners Club: 300-305, 3095, 36, 38, 39.
        prefix3 = int(digits[:3])
        if 300 <= prefix3 <= 305 or digits[:4] == "3095" or digits[:2] in ("36", "38", "39"):
            return True
    if length in (16, 19):
        # Discover: 6011, 622126-622925, 644-649, 65.
        if digits[:4] == "6011" or digits[:2] == "65" or digits[:3] in {"644", "645", "646", "647", "648", "649"}:
            return True
        if len(digits) >= 6 and 622126 <= int(digits[:6]) <= 622925:
            return True
    if length in (16, 17, 18, 19) and digits[:2] == "62":
        # UnionPay.
        return True
    return False


def is_plausible_card_pan(digits: str) -> bool:
    """Canonical "does this digit string look like a real card PAN" test.

    This is the ONE place that combines Luhn validity with the card-brand
    IIN/length check; every generic (non-key-scoped) PAN scanner in the
    codebase should call this rather than re-implementing Luhn-only
    detection, which is exactly the drift that let a bare-Luhn duplicate
    survive in `intent/log_redaction.py` after this module was hardened
    (issue #1811).
    """
    return 13 <= len(digits) <= 19 and _luhn_valid(digits) and _matches_known_card_brand_iin(digits)


def contains_payment_card_pan(value: object) -> bool:
    if not isinstance(value, str):
        return False
    for match in _PAYMENT_CARD_PAN_RE.finditer(value):
        digits = "".join(character for character in match.group(0) if character.isdigit())
        if is_plausible_card_pan(digits):
            return True
    return False


def is_tier3_credential_key(key: object) -> bool:
    normalized = _normalize_token(key)
    compact = _compact_token(normalized)
    if normalized in _TIER3_EXACT_KEYS or compact in _TIER3_EXACT_KEYS:
        return True
    if normalized.endswith(_TIER3_TOKEN_SUFFIXES):
        return True
    return any(part in normalized or part.replace("_", "") in compact for part in _TIER3_KEY_PARTS)


def is_tier3_credential_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(("bearer ", "basic ")):
        return True
    if lowered in _TIER3_VALUE_MARKERS:
        return True
    if any(marker in lowered for marker in _TIER3_VALUE_MARKERS):
        return True
    return contains_maskable_secret(stripped) or contains_payment_card_pan(stripped)


def find_tier3_credential_violation(payload: Any, *, path: str = "") -> Tier3CredentialViolation | None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            key_path = _payload_path(path, key)
            if is_tier3_credential_key(key):
                return Tier3CredentialViolation(field=key_path, reason="credential_key")
            if is_tier3_credential_value(value):
                return Tier3CredentialViolation(field=key_path, reason="credential_value")
            nested = find_tier3_credential_violation(value, path=key_path)
            if nested is not None:
                return nested
        return None
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            nested = find_tier3_credential_violation(value, path="%s[%d]" % (path, index))
            if nested is not None:
                return nested
        return None
    if is_tier3_credential_value(payload):
        return Tier3CredentialViolation(field=path or "<value>", reason="credential_value")
    return None


def reject_tier3_credential_payload(surface: str, payload: Any) -> None:
    violation = find_tier3_credential_violation(payload)
    if violation is None:
        return
    raise Tier3CredentialBoundaryError(field=violation.field, surface=surface, reason=violation.reason)


def reject_tier3_credential_json_body_bytes(raw_body: bytes, *, surface: str) -> None:
    """Reject credential-shaped JSON bodies before route-level parsing or DB work."""
    if not raw_body:
        return
    key_match = _RAW_TIER3_KEY_RE.search(raw_body)
    if key_match is not None:
        marker = key_match.group(0).decode("utf-8", errors="ignore").split(":", 1)[0].strip("\"'")
        raise Tier3CredentialBoundaryError(field=marker or "<body>", surface=surface, reason="credential_key")
    if _RAW_BEARER_RE.search(raw_body):
        raise Tier3CredentialBoundaryError(field="<body>", surface=surface, reason="credential_value")
    text = raw_body.decode("utf-8", errors="ignore")
    normalized_text = unicodedata.normalize("NFKC", text)
    text_key_match = _RAW_TIER3_KEY_TEXT_RE.search(normalized_text)
    if text_key_match is not None:
        marker = text_key_match.group(0).split(":", 1)[0].strip("\"'")
        raise Tier3CredentialBoundaryError(field=marker or "<body>", surface=surface, reason="credential_key")
    if contains_maskable_secret(text) or contains_payment_card_pan(text):
        raise Tier3CredentialBoundaryError(field="<body>", surface=surface, reason="credential_value")
