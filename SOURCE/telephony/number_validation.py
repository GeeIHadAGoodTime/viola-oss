"""Phone number validation and safety checks before dialing.

Parsing and number-type classification are delegated to Google's
``phonenumbers`` library. Viola's spend policy remains local: premium-rate,
international, and Caribbean NANP destinations are blocked before dialing.
"""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType
from phonenumbers.phonenumberutil import NumberParseException

from intent.tools.toll_fraud_prefixes import match_toll_fraud_prefix

_US_REGION = "US"


@dataclass(frozen=True)
class NumberValidation:
    """Result of phone number validation."""

    allowed: bool
    reason: str
    estimated_rate_per_min: float  # USD
    normalized_e164: str = ""
    region_code: str = ""
    number_type: str = ""


def _parse_number(phone_number: str):
    try:
        return phonenumbers.parse(str(phone_number or "").strip(), _US_REGION)
    except NumberParseException:
        return None


def _number_type_name(parsed: phonenumbers.PhoneNumber) -> str:
    return PhoneNumberType.to_string(phonenumbers.number_type(parsed))


def normalize_us_e164(phone_number: str, *, field_name: str = "Phone number") -> str:
    """Normalize a US phone number to E.164 or raise ``ValueError``."""

    validation = validate_phone_number(phone_number)
    if not validation.normalized_e164:
        raise ValueError("%s must be a valid US phone number." % field_name)
    if not validation.allowed:
        raise ValueError(validation.reason)
    return validation.normalized_e164


def is_toll_free_number(phone_number: str) -> bool:
    """Return whether *phone_number* is a valid toll-free number."""

    parsed = _parse_number(phone_number)
    if parsed is None:
        return False
    return phonenumbers.is_valid_number(parsed) and phonenumbers.number_type(parsed) == PhoneNumberType.TOLL_FREE


def validate_phone_number(phone_number: str) -> NumberValidation:
    """Validate a phone number before allowing Viola to dial it.

    Args:
        phone_number: Phone number string (any format).

    Returns:
        NumberValidation with allowed flag and reason.
    """
    parsed = _parse_number(phone_number)
    if parsed is None or not phonenumbers.is_possible_number(parsed):
        return NumberValidation(
            allowed=False,
            reason="Invalid number. Viola currently supports US domestic calls only.",
            estimated_rate_per_min=0.0,
        )

    normalized = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    region_code = phonenumbers.region_code_for_number(parsed) or ""
    number_type = phonenumbers.number_type(parsed)
    type_name = _number_type_name(parsed)

    if match_toll_fraud_prefix(normalized) is not None or number_type == PhoneNumberType.PREMIUM_RATE:
        return NumberValidation(
            allowed=False,
            reason="Cannot call premium-rate numbers. These charge $1-40/min.",
            estimated_rate_per_min=0.0,
            normalized_e164=normalized,
            region_code=region_code,
            number_type=type_name,
        )

    if region_code and region_code != _US_REGION:
        return NumberValidation(
            allowed=False,
            reason="Cannot call international or Caribbean numbers (%s). These may charge international rates."
            % region_code,
            estimated_rate_per_min=0.0,
            normalized_e164=normalized,
            region_code=region_code,
            number_type=type_name,
        )

    if not phonenumbers.is_valid_number(parsed):
        return NumberValidation(
            allowed=False,
            reason="Invalid number. Viola currently supports US domestic calls only.",
            estimated_rate_per_min=0.0,
            normalized_e164=normalized,
            region_code=region_code,
            number_type=type_name,
        )

    if number_type == PhoneNumberType.TOLL_FREE:
        return NumberValidation(
            allowed=True,
            reason="Toll-free number.",
            estimated_rate_per_min=0.015,
            normalized_e164=normalized,
            region_code=region_code,
            number_type=type_name,
        )

    return NumberValidation(
        allowed=True,
        reason="US domestic.",
        estimated_rate_per_min=0.007,
        normalized_e164=normalized,
        region_code=region_code,
        number_type=type_name,
    )
