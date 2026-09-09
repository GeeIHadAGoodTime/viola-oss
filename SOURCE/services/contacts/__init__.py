"""
services/contacts - Contacts service module.

Read-only (#3282, wave-1 Apple-cohort bridge): resolves a name to a phone
number/email via a user's connected CardDAV account (iCloud, or any other
RFC 6352 server). No write path -- see services/contacts/providers/carddav.py
for the full design rationale.
"""

from __future__ import annotations

from services.contacts.providers.carddav import CardDAVContactsProvider

_contacts_provider: CardDAVContactsProvider | None = None


def get_contacts_provider() -> CardDAVContactsProvider:
    """Return the process-wide CardDAV contacts provider singleton."""
    global _contacts_provider
    if _contacts_provider is None:
        _contacts_provider = CardDAVContactsProvider()
    return _contacts_provider


__all__ = ["CardDAVContactsProvider", "get_contacts_provider"]
