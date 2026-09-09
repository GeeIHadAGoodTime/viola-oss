"""Provider-neutral validation helpers for card-shaped input.

These pure functions contain no payment-provider, account, vault, or billing
integration. They are kept in the public core so browser safety checks can run
without importing the private payment package.
"""

from __future__ import annotations

import datetime
import re


def luhn_check(number: str) -> bool:
    digits = [int(digit) for digit in number]
    total = sum(digits[-1::-2])
    for digit in digits[-2::-2]:
        total += sum(divmod(digit * 2, 10))
    return total % 10 == 0


def validate_card_number(number: str) -> str | None:
    clean = re.sub(r"[\s\-]", "", number)
    if not clean.isdigit():
        return "Card number must contain only digits."
    if len(clean) < 13 or len(clean) > 19:
        return "Card number must be 13-19 digits."
    if not luhn_check(clean):
        return "Invalid card number (failed Luhn check)."
    return None


def validate_expiry(exp_month: str, exp_year: str) -> str | None:
    try:
        month = int(exp_month)
    except ValueError:
        return "Invalid expiration month."
    if month < 1 or month > 12:
        return "Expiration month must be between 1 and 12."
    try:
        year = 2000 + int(exp_year) if len(exp_year) == 2 else int(exp_year) if len(exp_year) == 4 else None
    except ValueError:
        return "Invalid expiration year."
    if year is None:
        return "Expiration year must be 2 or 4 digits."
    now = datetime.datetime.now(tz=datetime.UTC)
    if year < now.year or (year == now.year and month < now.month):
        return "Card is expired."
    return None


def validate_cvc(cvc: str) -> str | None:
    if not re.match(r"^\d{3,4}$", cvc):
        return "CVC must be 3 or 4 digits."
    return None
