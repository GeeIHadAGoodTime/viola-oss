"""Shared contacts provider normalization helpers.

Mirrors services/calendar/providers/base.py's normalize_event/
normalize_calendar pattern (#3282) so every contacts provider returns the
same provider-agnostic shape regardless of backend.
"""

from __future__ import annotations

from typing import Any


def normalize_contact(
    *,
    provider: str,
    contact_id: str,
    name: str,
    nickname: str | None = None,
    phones: list[str] | None = None,
    emails: list[str] | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a provider-agnostic contact record.

    ``phone``/``email`` are convenience singular fields (the first entry, if
    any) for callers that just want "a" number/address; ``phones``/``emails``
    carry the full list for callers that need to disambiguate (e.g. "mobile"
    vs "home").
    """

    phone_list = [str(value).strip() for value in (phones or []) if str(value).strip()]
    email_list = [str(value).strip() for value in (emails or []) if str(value).strip()]

    return {
        "provider": provider,
        "contact_id": contact_id,
        "name": name or "Unnamed Contact",
        "nickname": nickname or None,
        "phone": phone_list[0] if phone_list else None,
        "email": email_list[0] if email_list else None,
        "phones": phone_list,
        "emails": email_list,
        "raw": raw or {},
    }
