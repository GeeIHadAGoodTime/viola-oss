"""Shared helpers for the desktop LAN speaker pairing flow."""

from __future__ import annotations

import socket
from urllib.parse import quote

from config.settings import settings
from core.constants import LOCALHOST
from core.json_types import JsonDict
from core.logging_config import get_logger
from ui.security.config import get_security_config
from ui.security.spoke_pairing_ticket import (
    PAIRING_TICKET_MAX_AGE_SECONDS,
    PAIRING_TICKET_QUERY_PARAM,
    issue_pairing_ticket,
)
from utils.pairing_codec import encode_ip

logger = get_logger(__name__)


def get_pairing_lan_ip() -> str:
    """Return the hub LAN IP used by the speaker pairing URL."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:
        logger.debug("Could not determine speaker pairing LAN IP, using localhost")
        return LOCALHOST


def build_spoke_url_prefix(ip: str, port: int) -> str:
    """Return the QR URL prefix used before appending the room slug.

    The URL carries a short-lived, single-use PAIRING TICKET, never a live
    spoke credential (#4434). The Add Room screen shows this URL twice — inside
    the QR image and, historically, as plain text beside it — so anything it
    carries is one screenshot or one camera away from being public. A ticket
    opens no audio socket by itself: the joining device exchanges it once, from
    the LAN, at ``POST /bootstrap/claim`` for a real credential that lands in
    its cookie jar and is never displayed anywhere.
    """
    scheme = "https" if settings.ssl_enabled else "http"
    base_url = "%s://%s:%s/" % (scheme, ip, port)
    if get_security_config().auth_enabled:
        ticket = issue_pairing_ticket()
        token = quote(ticket.token, safe="")
        return "%s?%s=%s&room=" % (base_url, PAIRING_TICKET_QUERY_PARAM, token)
    return "%s?room=" % base_url


def build_local_address_payload() -> JsonDict:
    """Return the same local-address payload used by the Add Room UI."""
    ip = get_pairing_lan_ip()
    port = int(settings.api_port)
    return {
        "ip": ip,
        "port": port,
        "spoke_url": build_spoke_url_prefix(ip, port),
        "pairing_code": encode_ip(ip),
        # The panel masks the ticket and re-fetches before it lapses, so the QR
        # on screen is always redeemable while the user is looking at it.
        "pairing_ticket_param": PAIRING_TICKET_QUERY_PARAM,
        "pairing_ticket_expires_in": PAIRING_TICKET_MAX_AGE_SECONDS,
    }


def room_label(target_room: str) -> str:
    """Return a normalized room label for structured routing metadata."""
    return target_room.strip().strip("'\"") or "room"


def build_speaker_pairing_flow(target_room: str) -> JsonDict:
    """Build UI metadata that opens the existing in-house speaker pairing QR flow."""
    local_address = build_local_address_payload()
    room_slug = quote((target_room.strip() or "speaker"), safe="")
    spoke_url_prefix = str(local_address["spoke_url"])
    spoke_url = "%s%s" % (spoke_url_prefix, room_slug)
    base_url = spoke_url_prefix.split("/?", 1)[0]
    pairing_code = str(local_address["pairing_code"])
    return {
        "action": "pair_speaker",
        "available": True,
        "path_identifier": "rooms.add_speaker",
        "ui_action": "open_rooms_add_speaker",
        "target_room": target_room,
        "rooms_modal_tab": "add-speaker",
        "connect_page_url": "%s/connect" % base_url,
        "spoke_url": spoke_url,
        "qr_url": spoke_url,
        "qr_endpoint_url": spoke_url,
        "pairing_code": pairing_code,
        "qr_available": True,
        "qr_location": "Rooms > Add Room",
    }


def build_speaker_pairing_payload(target_room: str) -> JsonDict:
    """Return the structured setup envelope consumed by UI and LLM responses."""
    room_name = room_label(target_room)
    pairing_flow = build_speaker_pairing_flow(target_room)
    qr_data = {
        "payload": pairing_flow["spoke_url"],
        "qr_url": pairing_flow["qr_url"],
        "spoke_url": pairing_flow["spoke_url"],
        "connect_page_url": pairing_flow["connect_page_url"],
        "pairing_code": pairing_flow["pairing_code"],
        "room_name": room_name,
    }
    return {
        "message": "speaker_pairing_flow_available",
        "target_room": target_room,
        "room_name": room_name,
        "prefill": {"room_name": room_name},
        "paired": False,
        "pairing_flow_available": True,
        "pairing_flow": pairing_flow,
        "qr_data": qr_data,
        "pairing_code": pairing_flow["pairing_code"],
        "spoke_url": pairing_flow["spoke_url"],
        "ui_action": pairing_flow["ui_action"],
        "rooms_modal_tab": pairing_flow["rooms_modal_tab"],
    }
