"""URL validation utilities for return URL / redirect safety.

Shared by auth and billing routes to prevent open-redirect attacks.
Also provides SSRF protection for outbound HTTP requests.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# SSRF protection — block requests to private / internal networks
# ---------------------------------------------------------------------------

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
]

_BLOCKED_HOSTNAMES = frozenset(
    {
        "ip6-localhost",
        "ip6-loopback",
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.aws.internal",
        "169.254.169.254",
    }
)

_LOCALHOST_SUFFIXES = (".localhost", ".localhost.localdomain", ".lvh.me", ".localtest.me", ".vcap.me")
_IP_ECHO_SUFFIXES = (".nip.io", ".sslip.io")
_NUMERIC_IPV4_RE = re.compile(r"(?:0x[0-9a-f]+|0[0-7]*|[0-9]+)(?:\.(?:0x[0-9a-f]+|0[0-7]*|[0-9]+)){0,3}$")


def _parse_ipv4_int(part: str) -> int:
    lower = part.lower()
    if lower.startswith("0x"):
        return int(lower, 16)
    if len(lower) > 1 and lower.startswith("0"):
        return int(lower, 8)
    return int(lower, 10)


def _parse_numeric_ipv4_host(hostname: str) -> ipaddress.IPv4Address | None:
    """Parse legacy IPv4 spellings accepted by common URL stacks."""

    lower = hostname.rstrip(".").lower()
    if not _NUMERIC_IPV4_RE.fullmatch(lower):
        return None
    parts = [_parse_ipv4_int(part) for part in lower.split(".")]
    if len(parts) == 1:
        value = parts[0]
        if not 0 <= value <= 0xFFFFFFFF:
            return None
        return ipaddress.IPv4Address(value)

    if any(not 0 <= part <= 255 for part in parts[:-1]):
        return None
    tail_bits = 8 * (5 - len(parts))
    if not 0 <= parts[-1] < (1 << tail_bits):
        return None
    value = 0
    for index, part in enumerate(parts[:-1]):
        value |= part << (8 * (3 - index))
    value |= parts[-1]
    return ipaddress.IPv4Address(value)


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _is_blocked_address(mapped)
    if not addr.is_global:
        return True
    return any(addr in network for network in _BLOCKED_NETWORKS)


def _embedded_ip_from_rebinding_hostname(hostname: str) -> ipaddress.IPv4Address | None:
    lower = hostname.rstrip(".").lower()
    for suffix in _IP_ECHO_SUFFIXES:
        if not lower.endswith(suffix):
            continue
        prefix = lower[: -len(suffix)]
        dotted = prefix.replace("-", ".")
        return _parse_numeric_ipv4_host(dotted)
    return None


def _resolved_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve hostname best-effort so rebinding-to-private shapes fail closed."""

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return addresses
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            addresses.append(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:
            continue
    return addresses


def validate_external_url(url: str, *, allow_private: bool = False) -> str:
    """Validate that *url* does not target internal / private networks.

    Returns the URL unchanged if valid.  Raises ``ValueError`` if the URL
    uses a non-HTTP(S) scheme, has no hostname, or resolves to a blocked
    network / hostname.

    Args:
        url: The URL to validate.
        allow_private: If ``True``, skip the private-network check
            (useful for local development).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https scheme")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a hostname")
    if not allow_private:
        lower = hostname.rstrip(".").lower()
        if lower in _BLOCKED_HOSTNAMES or any(lower.endswith(suffix) for suffix in _LOCALHOST_SUFFIXES):
            raise ValueError("URL targets a blocked hostname")
        embedded_ip = _embedded_ip_from_rebinding_hostname(lower)
        if embedded_ip is not None and _is_blocked_address(embedded_ip):
            raise ValueError("URL targets a private/internal network")
        numeric_ip = _parse_numeric_ipv4_host(lower)
        if numeric_ip is not None and _is_blocked_address(numeric_ip):
            raise ValueError("URL targets a private/internal network")
        try:
            addr = ipaddress.ip_address(hostname)
            if _is_blocked_address(addr):
                raise ValueError("URL targets a private/internal network")
        except ValueError as exc:
            if "private" in str(exc) or "internal" in str(exc) or "blocked" in str(exc):
                raise
            # hostname is a DNS name, not a raw IP; already checked above
        for resolved in _resolved_addresses(lower):
            if _is_blocked_address(resolved):
                raise ValueError("URL resolves to a private/internal network")
    return url


# Hardcoded trusted domains for billing/checkout redirects
_TRUSTED_DOMAINS: frozenset[str] = frozenset({"useviola.com", "www.useviola.com"})


def validate_return_url(
    return_url: str | None,
    *,
    fallback: str | None = None,
) -> str | None:
    """Validate that a return URL is safe (same origin, trusted domain, or relative).

    Accepts:
      - Relative paths (no scheme, no netloc)
      - Same-origin URLs (matching settings.api_host)
      - Trusted domains (useviola.com, cloud_url)

    Args:
        return_url: The URL to validate. If None, returns fallback.
        fallback: Default URL to return if validation fails or return_url is None.

    Returns:
        The validated URL, the fallback, or None.
    """
    if not return_url:
        return fallback

    from config.settings import settings

    parsed = urlparse(return_url)

    # Allow relative URLs (no scheme, no netloc)
    if not parsed.scheme and not parsed.netloc:
        return return_url

    # Build set of trusted hostnames
    trusted_hosts: set[str] = set(_TRUSTED_DOMAINS)

    # Trust the configured API host
    api_host = settings.api_host
    trusted_hosts.add(api_host)
    if api_host == "127.0.0.1":
        trusted_hosts.add("localhost")
    elif api_host == "localhost":
        trusted_hosts.add("127.0.0.1")

    # Trust the cloud URL hostname if configured
    cloud_url = getattr(settings, "cloud_url", None)
    if cloud_url:
        cloud_parsed = urlparse(cloud_url)
        if cloud_parsed.hostname:
            trusted_hosts.add(cloud_parsed.hostname)

    # Check parsed hostname against trusted set
    parsed_hostname = parsed.hostname or ""
    if parsed_hostname in trusted_hosts:
        return return_url

    # Reject — log and return fallback
    logger.warning("Blocked potential open redirect to: %s", return_url)
    return fallback
