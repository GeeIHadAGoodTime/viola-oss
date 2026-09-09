"""Short-lived, single-use pairing tickets for the Add Room QR flow.

Why this exists (#4434 / C-664)
-------------------------------
The desktop "Add Room" screen publishes a URL that a phone opens to join as a
speaker. That URL used to carry a **real** spoke credential
(``issue_spoke_credential``) which is valid for months and authorises the live
audio/mic WebSockets. The same URL is drawn twice on one screen (inside the QR
image and as monospace text), so a photo or a screen-share handed over a
working, long-lived live-audio credential. It reached a marketing take.

A pairing ticket replaces the credential in that URL. It is:

* **short-lived** — ``PAIRING_TICKET_MAX_AGE_SECONDS`` (minutes, not months);
* **single-use** — redeeming one marks it consumed, so a photo taken while the
  real device was pairing is already dead;
* **powerless on its own** — no WebSocket, REST route, or audio path accepts a
  ticket. Its only privilege is being exchanged, once, from the LAN, at
  ``POST /bootstrap/claim`` for a real spoke credential that is delivered
  straight into the joining device's cookie jar and never displayed.

The ticket is signed with the same HMAC secret as spoke credentials but under a
different prefix, so the two token families can never be confused for each
other: ``verify_spoke_credential`` rejects a ticket on the prefix check, and
``redeem_pairing_ticket`` rejects a credential the same way.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from core.logging_config import get_logger
from ui.security.spoke_credentials import ensure_spoke_token_secret, load_spoke_token_secret

logger = get_logger(__name__)

# Query parameter the pairing URL/QR carries. Deliberately NOT ``spoke_token``:
# a device landing with this parameter holds nothing that authenticates it.
PAIRING_TICKET_QUERY_PARAM = "pair"

# Lifetime of a pairing ticket. Matches the PIN pairing-session window in
# ui/security/bootstrap.py so both join paths expire on the same clock.
PAIRING_TICKET_MAX_AGE_SECONDS = 300

# Tickets dated further ahead than this are treated as forged/tampered.
PAIRING_TICKET_FUTURE_SKEW_SECONDS = 60

_TICKET_PREFIX = "vpair1"  # nosec B105

_lock = threading.RLock()
# ticket_id -> epoch seconds after which the entry can be forgotten. A ticket
# cannot outlive its own max age, so the consumed set stays tiny and self-pruning.
_consumed_tickets: dict[str, float] = {}


@dataclass(frozen=True)
class IssuedPairingTicket:
    token: str
    ticket_id: str
    issued_at: int
    expires_in: int


@dataclass(frozen=True)
class RedeemedPairingTicket:
    ticket_id: str
    issued_at: int


class PairingTicketError(Exception):
    """Raised when a pairing ticket cannot be redeemed.

    ``reason`` is a stable machine-readable code:
    ``invalid`` (malformed/bad signature), ``expired`` (past its max age),
    ``used`` (already redeemed), ``unavailable`` (no signing secret yet).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _sign(secret: str, ticket_id: str, issued_at: int) -> str:
    message = f"{_TICKET_PREFIX}.{ticket_id}.{issued_at}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _prune_consumed(now: float) -> None:
    stale = [ticket_id for ticket_id, expiry in _consumed_tickets.items() if expiry <= now]
    for ticket_id in stale:
        del _consumed_tickets[ticket_id]


def issue_pairing_ticket() -> IssuedPairingTicket:
    """Mint a short-lived, single-use pairing ticket for the Add Room URL/QR."""
    secret = ensure_spoke_token_secret()
    issued_at = int(time.time())
    ticket_id = secrets.token_urlsafe(12)
    signature = _sign(secret, ticket_id, issued_at)
    token = f"{_TICKET_PREFIX}.{ticket_id}.{issued_at}.{signature}"
    return IssuedPairingTicket(
        token=token,
        ticket_id=ticket_id,
        issued_at=issued_at,
        expires_in=PAIRING_TICKET_MAX_AGE_SECONDS,
    )


def redeem_pairing_ticket(token: str) -> RedeemedPairingTicket:
    """Consume a pairing ticket, or raise :class:`PairingTicketError`.

    Redemption is one-shot: the ticket id is recorded as consumed before this
    returns, so a second presentation of the same ticket (a photographed QR
    scanned after the real device paired) fails with ``used``.
    """
    candidate = (token or "").strip()
    if not candidate:
        raise PairingTicketError("invalid", "Pairing link is missing its code.")

    parts = candidate.split(".", 3)
    if len(parts) != 4 or parts[0] != _TICKET_PREFIX:
        raise PairingTicketError("invalid", "That pairing link is not valid.")

    _, ticket_id, issued_at_str, signature = parts
    secret = load_spoke_token_secret()
    if not secret:
        raise PairingTicketError("unavailable", "This Viola cannot pair right now.")

    try:
        issued_at = int(issued_at_str)
    except ValueError:
        raise PairingTicketError("invalid", "That pairing link is not valid.") from None

    expected = _sign(secret, ticket_id, issued_at)
    if not hmac.compare_digest(signature, expected):
        raise PairingTicketError("invalid", "That pairing link is not valid.")

    now = time.time()
    age = now - issued_at
    if age > PAIRING_TICKET_MAX_AGE_SECONDS:
        raise PairingTicketError("expired", "That pairing link has expired.")
    if age < -PAIRING_TICKET_FUTURE_SKEW_SECONDS:
        raise PairingTicketError("invalid", "That pairing link is not valid.")

    with _lock:
        _prune_consumed(now)
        if ticket_id in _consumed_tickets:
            raise PairingTicketError("used", "That pairing link has already been used.")
        _consumed_tickets[ticket_id] = issued_at + PAIRING_TICKET_MAX_AGE_SECONDS

    logger.info("Pairing ticket redeemed (ticket %s)", ticket_id[:8])
    return RedeemedPairingTicket(ticket_id=ticket_id, issued_at=issued_at)


def reset_consumed_tickets_for_tests() -> None:
    """Clear the consumed-ticket set (test helper only)."""
    with _lock:
        _consumed_tickets.clear()


__all__ = [
    "PAIRING_TICKET_FUTURE_SKEW_SECONDS",
    "PAIRING_TICKET_MAX_AGE_SECONDS",
    "PAIRING_TICKET_QUERY_PARAM",
    "IssuedPairingTicket",
    "PairingTicketError",
    "RedeemedPairingTicket",
    "issue_pairing_ticket",
    "redeem_pairing_ticket",
    "reset_consumed_tickets_for_tests",
]
