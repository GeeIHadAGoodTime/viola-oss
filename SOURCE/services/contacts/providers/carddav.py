"""Read-only CardDAV contacts provider (iCloud, or any RFC 6352 server).

Wave-1 Apple-cohort bridge (#3282): a thin, provider-agnostic CardDAV client
mirroring services/calendar/providers/caldav.py's shape and credential-storage
pattern. Read-only per the wave-1 scope -- no contact writes.

Buy-vs-build (investigated 2026-07-21, see #3282): the pinned ``caldav``
package (services/calendar/providers/caldav.py's dependency) has zero
addressbook/CardDAV support -- verified by inspecting ``caldav.DAVClient``/
``caldav.Principal`` directly. The CardDAV ecosystem's other tools
(``vdirsyncer`` etc.) are full bidirectional sync frameworks, not a fit for
"read contacts into Viola's context". The right buy is narrower: ``vobject``
(already a transitive dependency of ``caldav``, a mature vCard parser) for
parsing, plus ``httpx`` (already a direct dependency) for the raw WebDAV
PROPFIND/REPORT calls -- a client the same size/shape as caldav.py, not a new
heavy subsystem.

Discovery flow (RFC 6352 well-known URI + RFC 6764 service discovery):
  1. PROPFIND https://<host>/.well-known/carddav (Depth 0) -- the server
     redirects to the account's real DAV host (iCloud's contacts host
     differs per-account, e.g. https://pNN-contacts.icloud.com/...).
  2. PROPFIND the redirected URL for {DAV:}current-user-principal.
  3. PROPFIND the principal URL for
     {urn:ietf:params:xml:ns:carddav}addressbook-home-set.
  4. PROPFIND the home-set (Depth 1) for each addressbook collection
     ({DAV:}resourcetype containing card:addressbook) + its displayname.
  5. REPORT (addressbook-query) each addressbook for member vCards
     (card:address-data), parsed with vobject.

Credential sharing: a user who already connected iCloud Calendar (#3281) used
the same Apple ID + app-specific password iCloud Contacts needs, so
``load_credentials`` falls back to the CalDAV calendar provider's stored
credential (services/calendar/providers/caldav.py's ``calendar_caldav``
service key) when no dedicated ``contacts_carddav`` credential is stored --
this avoids a second "enter your Apple ID" form. Only username/password/
icloud-ness are shared; the calendar credential's ``url`` is the CALENDAR
host (caldav.icloud.com) and is never reused for contacts discovery, which
always re-runs its own well-known lookup against the contacts host.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as DefusedET

from core.logging_config import get_logger
from services.contacts.providers.base import normalize_contact

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_CREDENTIAL_SERVICE = "contacts_carddav"
_SHARED_CALENDAR_CREDENTIAL_SERVICE = "calendar_caldav"
_ICLOUD_CONTACTS_WELL_KNOWN = "https://contacts.icloud.com/.well-known/carddav"
_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)

_DAV_NS = "DAV:"
_CARD_NS = "urn:ietf:params:xml:ns:carddav"

_CURRENT_USER_PRINCIPAL_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
_ADDRESSBOOK_HOME_SET_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><card:addressbook-home-set/></d:prop></d:propfind>"
)
_RESOURCETYPE_DISPLAYNAME_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>"
)
_ADDRESSBOOK_QUERY_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:getetag/><card:address-data/></d:prop><card:filter/></card:addressbook-query>"
)


class CardDAVProviderError(RuntimeError):
    """Base error for CardDAV provider failures."""


class CardDAVDependencyError(CardDAVProviderError):
    """Raised when a required CardDAV dependency is unavailable."""


class CardDAVCredentialsError(CardDAVProviderError):
    """Raised when CardDAV credentials are missing or invalid."""


class _CredentialRepository(Protocol):
    async def get_credential(self, user_id: str, service: str) -> str | None: ...

    async def set_credential(self, user_id: str, service: str, plaintext: str) -> None: ...

    async def delete_credential(self, user_id: str, service: str) -> bool: ...


@dataclass(slots=True)
class CardDAVCredentials:
    """Per-user CardDAV connection settings."""

    url: str
    username: str
    password: str
    icloud: bool = False
    verify_ssl: bool = True
    timeout_seconds: int | None = None

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "username": self.username,
            "password": self.password,
            "icloud": self.icloud,
            "verify_ssl": self.verify_ssl,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_storage_dict(cls, payload: dict[str, object]) -> CardDAVCredentials:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not username or not password:
            raise CardDAVCredentialsError("Stored CardDAV credentials are incomplete.")
        return cls(
            url=str(payload.get("url") or "").strip(),
            username=username,
            password=password,
            icloud=bool(payload.get("icloud", False)),
            verify_ssl=bool(payload.get("verify_ssl", True)),
            timeout_seconds=_optional_int(payload.get("timeout_seconds")),
        )

    @classmethod
    def from_shared_calendar_storage_dict(cls, payload: dict[str, object]) -> CardDAVCredentials | None:
        """Build contacts credentials from a stored CalDAV *calendar* credential.

        Only username/password/icloud-ness carry over -- the calendar
        credential's ``url`` points at the calendar host, never the contacts
        host, so it is intentionally dropped here (contacts discovery always
        re-runs its own well-known lookup).
        """
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not username or not password:
            return None
        icloud = str(payload.get("features") or "").strip().lower() == "icloud"
        return cls(
            url="",
            username=username,
            password=password,
            icloud=icloud,
            verify_ssl=bool(payload.get("verify_ssl", True)),
        )


def _well_known_url(credentials: CardDAVCredentials) -> str:
    if credentials.icloud or not credentials.url:
        return _ICLOUD_CONTACTS_WELL_KNOWN
    parsed = urlsplit(credentials.url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/.well-known/carddav", "", ""))


def _local_responses(xml_text: str) -> list[ET.Element]:
    # A remote CardDAV server's WebDAV multistatus response is untrusted
    # input; parse with defusedxml rather than stdlib ElementTree directly
    # (bandit B314) to guard against XML entity-expansion/XXE-class attacks.
    root = DefusedET.fromstring(xml_text)
    return root.findall(f"{{{_DAV_NS}}}response")


def _find_href_in_prop(xml_text: str, tag: str) -> str | None:
    for response_el in _local_responses(xml_text):
        for propstat in response_el.findall(f"{{{_DAV_NS}}}propstat"):
            if "200" not in (propstat.findtext(f"{{{_DAV_NS}}}status") or ""):
                continue
            prop = propstat.find(f"{{{_DAV_NS}}}prop")
            if prop is None:
                continue
            target = prop.find(tag)
            if target is None:
                continue
            href = target.find(f"{{{_DAV_NS}}}href")
            if href is not None and href.text:
                return href.text
    return None


class CardDAVContactsProvider:
    """Read-only, provider-agnostic CardDAV contacts client."""

    provider_id = "carddav"

    def __init__(
        self,
        *,
        auth_db_factory: Callable[[], Any] | None = None,
        client_factory: Callable[[CardDAVCredentials], Any] | None = None,
    ) -> None:
        self._auth_db_factory = auth_db_factory
        self._client_factory = client_factory

    # ------------------------------------------------------------------
    # Credential storage (mirrors services/calendar/providers/caldav.py)
    # ------------------------------------------------------------------

    async def is_configured(self, user_id: str) -> bool:
        try:
            credentials = await self.load_credentials(user_id)
            if credentials is None:
                return False
            await asyncio.to_thread(self._discover_addressbooks_sync, credentials)
            return True
        except CardDAVProviderError:
            logger.exception("carddav is_configured failed for user=%s", user_id)
            return False

    async def load_credentials(self, user_id: str, *, allow_shared: bool = True) -> CardDAVCredentials | None:
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        raw = await repository.get_credential(uid, _CREDENTIAL_SERVICE)
        if raw:
            payload = _decode_json_object(raw, "Stored CardDAV credentials")
            return CardDAVCredentials.from_storage_dict(payload)

        if not allow_shared:
            return None

        shared_raw = await repository.get_credential(uid, _SHARED_CALENDAR_CREDENTIAL_SERVICE)
        if not shared_raw:
            return None
        shared_payload = _decode_json_object(shared_raw, "Stored CalDAV calendar credentials")
        return CardDAVCredentials.from_shared_calendar_storage_dict(shared_payload)

    async def store_credentials(self, user_id: str, credentials: CardDAVCredentials) -> None:
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        await repository.set_credential(uid, _CREDENTIAL_SERVICE, json.dumps(credentials.to_storage_dict()))

    async def delete_credentials(self, user_id: str) -> bool:
        uid = self._require_user_id(user_id)
        repository = await self._get_credential_repository()
        return await repository.delete_credential(uid, _CREDENTIAL_SERVICE)

    # ------------------------------------------------------------------
    # Read-only contact access
    # ------------------------------------------------------------------

    async def list_contacts(self, user_id: str, *, max_results: int = 200) -> list[dict[str, object]]:
        try:
            credentials = await self._require_credentials(user_id)
            return await asyncio.to_thread(self._list_contacts_sync, credentials, max_results)
        except CardDAVCredentialsError:
            return []
        except CardDAVProviderError:
            logger.exception("carddav list_contacts failed for user=%s", user_id)
            return []

    async def find_contact(self, user_id: str, query: str) -> list[dict[str, object]]:
        """Resolve a spoken/typed name to matching contact record(s).

        Exact (case-insensitive) name/nickname matches are returned alone
        when present; otherwise every contact whose name or nickname
        contains the query, or whose name contains every whitespace-split
        token in the query (handles "first last" vs "last, first" storage
        order), is returned so the caller/agent can disambiguate.
        """
        normalized_query = (query or "").strip().lower()
        if not normalized_query:
            return []

        contacts = await self.list_contacts(user_id)
        exact: list[dict[str, object]] = []
        partial: list[dict[str, object]] = []
        query_tokens = [token for token in normalized_query.split() if token]

        for contact in contacts:
            name = str(contact.get("name") or "").strip().lower()
            nickname = str(contact.get("nickname") or "").strip().lower()
            if name == normalized_query or (nickname and nickname == normalized_query):
                exact.append(contact)
                continue
            if normalized_query in name or (nickname and normalized_query in nickname):
                partial.append(contact)
                continue
            if query_tokens and all(token in name for token in query_tokens):
                partial.append(contact)

        return exact or partial

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_user_id(user_id: str | None) -> str:
        if not user_id:
            raise CardDAVCredentialsError("user_id is required")
        return user_id

    async def _require_credentials(self, user_id: str) -> CardDAVCredentials:
        credentials = await self.load_credentials(user_id)
        if credentials is None:
            raise CardDAVCredentialsError("No CardDAV credentials stored for user.")
        return credentials

    async def _get_credential_repository(self) -> _CredentialRepository:
        if self._auth_db_factory is None:
            from auth.database import get_auth_db

            db = get_auth_db()
        else:
            db = self._auth_db_factory()

        if not getattr(db, "_initialized", True):
            await db.initialize()

        if hasattr(db, "_pool"):
            # Cloud Postgres deployments never reach here in practice: iCloud
            # Contacts credentials are Tier-3 (desktop-only, per CLAUDE.md's
            # storage rule), same as the CalDAV calendar credential this
            # shares with. Kept for parity with caldav.py's repository lookup
            # rather than assuming a desktop-only auth DB shape.
            raise CardDAVProviderError(
                "CardDAV contacts credentials are desktop-only and are not available on this deployment."
            )

        repository = getattr(db, "user_credentials", None)
        if repository is None:
            raise CardDAVProviderError("Auth database does not expose user_credentials.")
        return repository

    def _make_client(self, credentials: CardDAVCredentials) -> Any:
        if self._client_factory is not None:
            return self._client_factory(credentials)

        try:
            import httpx
        except ImportError as exc:
            raise CardDAVDependencyError(
                "httpx dependency missing. Install 'httpx' to enable CardDAV contacts."
            ) from exc

        return httpx.Client(
            auth=(credentials.username, credentials.password),
            verify=credentials.verify_ssl,
            timeout=credentials.timeout_seconds or 30.0,
            follow_redirects=False,
        )

    def _propfind(self, client: Any, url: str, *, depth: str, body: str) -> Any:
        response = client.request(
            "PROPFIND",
            url,
            content=body.encode("utf-8"),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code not in (200, 207, *_REDIRECT_STATUS_CODES):
            raise CardDAVProviderError("CardDAV PROPFIND %s failed: HTTP %s" % (url, response.status_code))
        return response

    def _follow_redirect_if_needed(self, client: Any, response: Any, *, depth: str, body: str) -> Any:
        if response.status_code not in _REDIRECT_STATUS_CODES:
            return response
        location = response.headers.get("Location")
        if not location:
            raise CardDAVProviderError("CardDAV discovery redirect is missing a Location header.")
        redirected_url = urljoin(str(response.url), location)
        return self._propfind(client, redirected_url, depth=depth, body=body)

    def _discover_principal_url(self, client: Any, base_url: str) -> str:
        response = self._propfind(client, base_url, depth="0", body=_CURRENT_USER_PRINCIPAL_BODY)
        response = self._follow_redirect_if_needed(client, response, depth="0", body=_CURRENT_USER_PRINCIPAL_BODY)
        href = _find_href_in_prop(response.text, f"{{{_DAV_NS}}}current-user-principal")
        if not href:
            raise CardDAVProviderError("CardDAV server did not return a current-user-principal.")
        return urljoin(str(response.url), href)

    def _discover_addressbook_home_set(self, client: Any, principal_url: str) -> str:
        response = self._propfind(client, principal_url, depth="0", body=_ADDRESSBOOK_HOME_SET_BODY)
        href = _find_href_in_prop(response.text, f"{{{_CARD_NS}}}addressbook-home-set")
        if not href:
            raise CardDAVProviderError("CardDAV server did not return an addressbook-home-set.")
        return urljoin(str(response.url), href)

    def _list_addressbooks(self, client: Any, home_set_url: str) -> list[dict[str, str]]:
        response = self._propfind(client, home_set_url, depth="1", body=_RESOURCETYPE_DISPLAYNAME_BODY)
        addressbooks: list[dict[str, str]] = []
        for response_el in _local_responses(response.text):
            href = response_el.findtext(f"{{{_DAV_NS}}}href")
            if not href:
                continue
            is_addressbook = False
            displayname: str | None = None
            for propstat in response_el.findall(f"{{{_DAV_NS}}}propstat"):
                if "200" not in (propstat.findtext(f"{{{_DAV_NS}}}status") or ""):
                    continue
                prop = propstat.find(f"{{{_DAV_NS}}}prop")
                if prop is None:
                    continue
                resourcetype = prop.find(f"{{{_DAV_NS}}}resourcetype")
                if resourcetype is not None and resourcetype.find(f"{{{_CARD_NS}}}addressbook") is not None:
                    is_addressbook = True
                displayname = prop.findtext(f"{{{_DAV_NS}}}displayname") or displayname
            if is_addressbook:
                addressbooks.append({"href": urljoin(str(response.url), href), "name": displayname or href})
        return addressbooks

    def _report_vcards(self, client: Any, addressbook_url: str) -> list[tuple[str, str]]:
        response = client.request(
            "REPORT",
            addressbook_url,
            content=_ADDRESSBOOK_QUERY_BODY.encode("utf-8"),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if response.status_code not in (200, 207):
            raise CardDAVProviderError("CardDAV REPORT %s failed: HTTP %s" % (addressbook_url, response.status_code))
        vcards: list[tuple[str, str]] = []
        for response_el in _local_responses(response.text):
            href = response_el.findtext(f"{{{_DAV_NS}}}href") or ""
            for propstat in response_el.findall(f"{{{_DAV_NS}}}propstat"):
                if "200" not in (propstat.findtext(f"{{{_DAV_NS}}}status") or ""):
                    continue
                prop = propstat.find(f"{{{_DAV_NS}}}prop")
                if prop is None:
                    continue
                address_data = prop.findtext(f"{{{_CARD_NS}}}address-data")
                if address_data:
                    vcards.append((href, address_data))
        return vcards

    def _discover_addressbooks_sync(self, credentials: CardDAVCredentials) -> list[dict[str, str]]:
        with self._make_client(credentials) as client:
            principal_url = self._discover_principal_url(client, _well_known_url(credentials))
            home_set_url = self._discover_addressbook_home_set(client, principal_url)
            return self._list_addressbooks(client, home_set_url)

    def _list_contacts_sync(self, credentials: CardDAVCredentials, max_results: int) -> list[dict[str, object]]:
        contacts: list[dict[str, object]] = []
        with self._make_client(credentials) as client:
            principal_url = self._discover_principal_url(client, _well_known_url(credentials))
            home_set_url = self._discover_addressbook_home_set(client, principal_url)
            for addressbook in self._list_addressbooks(client, home_set_url):
                for href, vcard_text in self._report_vcards(client, addressbook["href"]):
                    contact = self._parse_vcard(href, vcard_text)
                    if contact is not None:
                        contacts.append(contact)
                    if max_results > 0 and len(contacts) >= max_results:
                        return contacts
        return contacts

    def _parse_vcard(self, href: str, vcard_text: str) -> dict[str, object] | None:
        try:
            import vobject
        except ImportError as exc:
            raise CardDAVDependencyError(
                "vobject dependency missing. Install 'vobject' to enable CardDAV contacts."
            ) from exc

        try:
            vcard = vobject.readOne(vcard_text)
        except Exception:
            logger.exception("Failed to parse vCard at %s", href)
            return None

        name = _vcard_text_value(vcard, "fn") or _vcard_text_value(vcard, "n")
        nickname = _vcard_text_value(vcard, "nickname")
        phones = _vcard_text_values(vcard, "tel_list")
        emails = _vcard_text_values(vcard, "email_list")
        uid = _vcard_text_value(vcard, "uid") or href

        return normalize_contact(
            provider=self.provider_id,
            contact_id=uid,
            name=name or "Unnamed Contact",
            nickname=nickname,
            phones=phones,
            emails=emails,
            raw={"href": href},
        )


def _vcard_text_value(vcard: Any, attr: str) -> str | None:
    node = getattr(vcard, attr, None)
    if node is None:
        return None
    value = getattr(node, "value", None)
    return str(value).strip() if value else None


def _vcard_text_values(vcard: Any, list_attr: str) -> list[str]:
    nodes = getattr(vcard, list_attr, None) or []
    values: list[str] = []
    for node in nodes:
        value = getattr(node, "value", None)
        if value:
            values.append(str(value).strip())
    return values


def _decode_json_object(raw: str, label: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CardDAVCredentialsError("%s are not valid JSON." % label) from exc
    if not isinstance(payload, dict):
        raise CardDAVCredentialsError("%s must be a JSON object." % label)
    return payload


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
