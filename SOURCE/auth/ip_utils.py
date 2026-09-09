"""
Trusted-proxy-aware IP extraction for auth modules.

This module provides a hardened alternative to raw X-Forwarded-For parsing.
It only trusts the X-Forwarded-For header when the direct connection comes
from a known trusted proxy IP. This prevents IP spoofing attacks where an
attacker sets X-Forwarded-For to bypass rate limiting or brute-force guards.

Cloud Caddy also overwrites X-Viola-Client-IP from Cloudflare's
CF-Connecting-IP before forwarding selected requests to the API container.
That internal header is accepted only in cloud runtime so public cloud traffic
does not collapse into one Docker-proxy bucket.

The trusted proxy list comes from ui/security/config.py (SecurityConfig).
Cloud deployments must use exact IPs, CIDRs, or resolvable hostnames from
VIOLA_SECURITY_TRUSTED_PROXIES; localhost is not implicitly unioned into an
explicit proxy config.

Design modelled on ui/security/rate_limiting.py:77-105.
"""

from __future__ import annotations

import ipaddress
import os
import socket

from starlette.requests import Request

from core.constants import LOCALHOST, LOCALHOST_NAME
from core.logging_config import get_logger

logger = get_logger(__name__)


def _get_trusted_proxy_ips() -> set[str]:
    """Return the set of trusted proxy entries from SecurityConfig.

    Desktop/dev defaults may trust localhost. Cloud deployments must trust
    only explicit proxy entries so a direct loopback caller inside the API
    container cannot forge edge-owned client-IP headers.
    """
    default_trusted: set[str] = {LOCALHOST, "::1", LOCALHOST_NAME}

    configured: set[str] = set()
    raw_env = os.environ.get("VIOLA_SECURITY_TRUSTED_PROXIES")
    try:
        from ui.security.config import get_security_config

        config = get_security_config()
        if config.trusted_proxies:
            configured = set(config.trusted_proxies)
    except Exception:
        # SecurityConfig may not be available in all contexts (tests, early startup).
        pass

    if raw_env is not None:
        entries = {entry.strip() for entry in raw_env.split(",") if entry.strip()}
    elif _cloud_or_fly_runtime():
        # SecurityConfig.from_env supplies localhost defaults when the env var
        # is absent. On cloud that default is not an edge proxy, so drop it.
        entries = configured - default_trusted
    else:
        entries = configured or default_trusted

    expanded: set[str] = set()
    for entry in entries:
        expanded.update(_expand_trusted_proxy_entry(entry))
    return expanded


def _expand_trusted_proxy_entry(entry: str) -> set[str]:
    """Expand a configured proxy IP/CIDR/hostname into matchable tokens."""
    normalized = str(entry or "").strip()
    if not normalized:
        return set()

    expanded = {normalized}
    try:
        ipaddress.ip_network(normalized, strict=False)
        return expanded
    except ValueError:
        pass

    try:
        ipaddress.ip_address(normalized)
        return expanded
    except ValueError:
        pass

    try:
        for _family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(normalized, None):
            candidate = sockaddr[0]
            if _is_valid_ip(candidate):
                expanded.add(candidate)
    except socket.gaierror:
        logger.debug("Trusted proxy hostname did not resolve: %s", normalized)
    return expanded


def _is_valid_ip(ip: str) -> bool:
    """Basic IP address format validation."""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _get_header(request: Request, name: str) -> str | None:
    """Read a request header case-insensitively, including plain-dict test doubles."""
    value = request.headers.get(name)
    if value is not None:
        return value
    lower_name = name.lower()
    for key, candidate in getattr(request, "headers", {}).items():
        if str(key).lower() == lower_name:
            return candidate
    return None


def _cloud_or_fly_runtime() -> bool:
    if os.environ.get("FLY_APP_NAME") or os.environ.get("FLY_MACHINE_ID"):
        return True
    try:
        from config.settings import get_settings

        return str(getattr(get_settings(), "app_surface", "desktop")).strip().lower() == "cloud"
    except Exception:
        return False


def _loopback_direct_source(direct_ip: str | None) -> bool:
    if not direct_ip:
        return False
    if direct_ip == LOCALHOST_NAME:
        return True
    try:
        return ipaddress.ip_address(direct_ip).is_loopback
    except ValueError:
        return False


def _explicit_trusted_proxy_configured() -> bool:
    configured_env = os.environ.get("VIOLA_SECURITY_TRUSTED_PROXIES", "").strip()
    if configured_env:
        return True
    default_trusted = {LOCALHOST, "::1", LOCALHOST_NAME}
    try:
        from ui.security.config import get_security_config

        configured = set(get_security_config().trusted_proxies or [])
    except (AttributeError, ImportError, RuntimeError):
        return False
    return any(str(entry).strip() not in default_trusted for entry in configured)


def _forwarded_for_allowed_for_direct_source(direct_ip: str | None) -> bool:
    if not _loopback_direct_source(direct_ip):
        return True
    return _cloud_or_fly_runtime() or _explicit_trusted_proxy_configured()


def _trusted_proxy_entry_matches(direct_ip: str, entry: str) -> bool:
    entry = entry.strip()
    if not entry:
        return False
    if direct_ip == entry:
        return True

    try:
        direct_addr = ipaddress.ip_address(direct_ip)
    except ValueError:
        direct_addr = None

    if direct_addr is None:
        return False

    try:
        if direct_addr in ipaddress.ip_network(entry, strict=False):
            return True
    except ValueError:
        pass

    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(entry, None) if info and len(info) >= 5 and info[4]}
    except OSError:
        resolved = set()
    return str(direct_addr) in resolved


def _direct_ip_is_trusted_proxy(direct_ip: str | None) -> bool:
    """Return True iff the TCP peer that opened this connection is a trusted proxy.

    A proxy is trusted when it matches ``VIOLA_SECURITY_TRUSTED_PROXIES``
    (loaded via ``_get_trusted_proxy_ips``), including exact IPs, CIDRs, or
    resolvable hostnames such as the Docker Compose service name
    ``viola-edge``.

    Without this guard, any caller able to reach viola-api directly — for
    example any other container on the same Docker network, or any LAN
    machine if the API port leaks — can supply ``X-Viola-Client-IP`` and
    impersonate an arbitrary public IP for rate-limiting and abuse
    purposes.

    Note: ``ipaddress.IPv4Address.is_private`` is intentionally NOT used
    because Python's stdlib treats RFC 5737 documentation ranges as
    private; an attacker hosting at one of those ranges (or any future
    range the stdlib adds) would inherit unwarranted trust. The explicit
    trusted-proxy configuration is the canonical boundary.
    """
    if not direct_ip:
        return False
    trusted = _get_trusted_proxy_ips()
    return any(_trusted_proxy_entry_matches(direct_ip, entry) for entry in trusted)


def direct_ip_is_trusted_proxy(direct_ip: str | None) -> bool:
    """Public wrapper for shared HTTP/WebSocket proxy-source decisions."""
    return _direct_ip_is_trusted_proxy(direct_ip)


def validated_header_ip(value: str | None) -> str | None:
    """Return a stripped header IP only when it is a syntactically valid IP."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def extract_client_ip(request: Request) -> str | None:
    """Extract the real client IP from a request, respecting trusted proxies.

    Only trusts X-Forwarded-For / X-Viola-Client-IP / Fly-Client-IP when the
    direct connection comes from a trusted proxy (see
    ``_direct_ip_is_trusted_proxy``). Otherwise, returns the direct
    connection IP to prevent IP spoofing.

    Args:
        request: The incoming Starlette/FastAPI request.

    Returns:
        The best-effort client IP address, or None if unavailable.
    """
    # Get the direct connection IP (this is the TCP peer, cannot be spoofed)
    direct_ip = request.client.host if request.client else None
    direct_is_trusted = _direct_ip_is_trusted_proxy(direct_ip)

    viola_client_ip = _get_header(request, "x-viola-client-ip")
    if viola_client_ip and _cloud_or_fly_runtime():
        if direct_is_trusted:
            candidate = validated_header_ip(viola_client_ip)
            if candidate:
                return candidate
            logger.warning(
                "Invalid X-Viola-Client-IP header value: %s, falling back to proxy-aware IP extraction",
                viola_client_ip.strip(),
            )
        else:
            # An untrusted TCP peer set X-Viola-Client-IP. Ignoring rather
            # than honouring it; alarming so operators notice if a leaked
            # internal-network attacker tries to forge edge headers.
            logger.warning(
                "Ignoring X-Viola-Client-IP from untrusted direct source %s",
                direct_ip,
            )

    fly_client_ip = _get_header(request, "fly-client-ip")
    if fly_client_ip and _cloud_or_fly_runtime():
        if direct_is_trusted:
            candidate = validated_header_ip(fly_client_ip)
            if candidate:
                return candidate
            logger.warning(
                "Invalid Fly-Client-IP header value: %s, falling back to proxy-aware IP extraction",
                fly_client_ip.strip(),
            )
        else:
            logger.warning(
                "Ignoring Fly-Client-IP from untrusted direct source %s",
                direct_ip,
            )

    forwarded_for = _get_header(request, "x-forwarded-for")
    if forwarded_for and direct_ip:
        if direct_is_trusted and _forwarded_for_allowed_for_direct_source(direct_ip):
            # Request comes from a trusted proxy -- honour X-Forwarded-For
            candidate = forwarded_for.split(",")[0].strip()
            if _is_valid_ip(candidate):
                return candidate
            else:
                logger.warning(
                    "Invalid IP in X-Forwarded-For from trusted proxy %s: %s, using direct IP",
                    direct_ip,
                    candidate,
                )
        elif direct_is_trusted:
            logger.warning(
                "Ignoring X-Forwarded-For from loopback desktop source %s without explicit trusted-proxy config",
                direct_ip,
            )
        else:
            # Untrusted origin -- ignore X-Forwarded-For to prevent spoofing
            logger.warning(
                "Ignoring X-Forwarded-For from untrusted source %s",
                direct_ip,
            )

    return direct_ip


def extract_client_info(request: Request) -> tuple[str | None, str | None]:
    """Extract client IP (trusted-proxy-aware) and user-agent from a request.

    This is a drop-in replacement for the naive ``_extract_client_info`` that
    previously trusted raw X-Forwarded-For unconditionally.

    Returns:
        (client_ip, user_agent) tuple.
    """
    client_ip = extract_client_ip(request)
    user_agent = request.headers.get("user-agent")
    return client_ip, user_agent
