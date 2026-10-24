"""Decide whether automatic discovery can advertise this API on the LAN.

The multiroom feature stays enabled, but its automatic discovery sockets are
useful only for a LAN-bound API. A clean install binds to loopback; opening
mDNS there needlessly triggers Windows Firewall consent over onboarding
(#4867). Explicit LAN/wildcard binds keep discovery available. This policy
does not change bind settings or control user-initiated smart-home scans.

It also records the extra LAN listener the app opens when the user adds a
room (``utils.speaker_pairing_flow``), so checks that depend on LAN exposure,
such as the websocket Origin check, can see it without reading the bind
setting. Recording it opens nothing.
"""

from __future__ import annotations

import ipaddress
import socket
import threading

_ACTIVE_LAN_LISTENER_LOCK = threading.Lock()
_ACTIVE_LAN_LISTENER: tuple[str, int] | None = None


def set_active_lan_listener(host: str | None, port: int | None = None) -> None:
    """Record (or clear, with ``None``) the LAN address the app is serving on."""
    global _ACTIVE_LAN_LISTENER
    with _ACTIVE_LAN_LISTENER_LOCK:
        _ACTIVE_LAN_LISTENER = (str(host), int(port)) if host and port else None


def active_lan_listener() -> tuple[str, int] | None:
    """Return ``(host, port)`` of the running LAN listener, or ``None``."""
    with _ACTIVE_LAN_LISTENER_LOCK:
        return _ACTIVE_LAN_LISTENER


def _is_loopback(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def lan_listeners_allowed(api_host: str | None) -> bool:
    """Allow automatic discovery only for an unambiguously non-loopback bind.

    Resolve named bind hosts, since uvicorn accepts them too. Missing, failed,
    empty or mixed loopback/LAN answers fail closed. Numeric binds and localhost
    need no DNS lookup. The caller supplies its current effective bind host,
    so this module never imports configuration or changes network settings.
    """
    host = (api_host or "").strip().lower()
    if not host or host.rstrip(".") == "localhost":
        return False

    try:
        return not _is_loopback(ipaddress.ip_address(host))
    except ValueError:
        pass

    try:
        answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False

    if not answers:
        return False
    for family, _socktype, _protocol, _canonname, sockaddr in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            return False
        try:
            if not isinstance(sockaddr[0], str):
                return False
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, ValueError):
            return False
        if _is_loopback(address):
            return False
    return True
