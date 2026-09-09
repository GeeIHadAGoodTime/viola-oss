"""
Network Discovery — detect smart home services on the local network.

Supports mDNS service browsing plus targeted known-port checks. Opt-in only
(``network_discovery_enabled`` or ``VIOLA_NETWORK_DISCOVERY_ENABLED`` must be
set).

Usage:
    from services.network_discovery import scan_network

    services = await scan_network()
    for svc in services:
        print(svc.service_type, svc.ip, svc.port)
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DiscoveredService:
    """A service discovered on the local network."""

    service_type: str  # "home_assistant", "philips_hue", "mqtt", "hubitat", "sonos", "homekit", "smartthings"
    display_name: str
    ip: str
    port: int
    metadata: dict[str, str] | None = None


# Known port → service mappings.  Port scans are targeted at common gateway
# IPs to keep the scan fast and light on LAN chatter.
_SCAN_CACHE_TTL_SECONDS = 30.0
_SCAN_LOCK = asyncio.Lock()
_LAST_SCAN_COMPLETED_AT = 0.0
_LAST_SCAN_RESULT: tuple[DiscoveredService, ...] = ()


_PORT_SERVICE_MAP: dict[int, tuple[str, str]] = {
    8123: ("home_assistant", "Home Assistant"),
    1883: ("mqtt", "MQTT Broker"),
    80: ("hubitat", "Hubitat Elevation"),
    1400: ("sonos", "Sonos"),
}


def _discovery_enabled() -> bool:
    """Read the user-toggleable discovery flag from SettingsManager."""

    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("network_discovery_enabled", False))
    except Exception:
        # Settings manager may not be initialised in some CLI contexts;
        # fall back to the AppConfig env flag for backward compatibility.
        return bool(getattr(settings, "network_discovery_enabled", False))


async def scan_network() -> list[DiscoveredService]:
    """Scan the local network for known smart-home services.

    Opt-in only — returns an empty list unless the user has enabled
    ``network_discovery_enabled`` in Settings > Smart Home (or the
    ``VIOLA_NETWORK_DISCOVERY_ENABLED`` env var is set for CLI use).

    Returns:
        List of discovered services (may be empty).
    """
    if not _discovery_enabled():
        logger.debug("Network discovery disabled — skipping scan")
        return []

    async with _SCAN_LOCK:
        global _LAST_SCAN_COMPLETED_AT, _LAST_SCAN_RESULT

        now = time.monotonic()
        if _LAST_SCAN_COMPLETED_AT > 0 and now - _LAST_SCAN_COMPLETED_AT < _SCAN_CACHE_TTL_SECONDS:
            logger.debug("Network discovery returning cached scan result")
            return list(_LAST_SCAN_RESULT)

        discovered: list[DiscoveredService] = []

        # Run discovery methods in parallel while a singleflight lock coalesces
        # concurrent callers into one LAN broadcast/port-probe pass.
        results = await asyncio.gather(
            _scan_mdns(),
            _scan_known_ports(),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning("Discovery method failed: %s", result)
                continue
            if isinstance(result, list):
                discovered.extend(result)

        # Deduplicate by (service_type, ip)
        seen: set[tuple[str, str]] = set()
        unique: list[DiscoveredService] = []
        for svc in discovered:
            key = (svc.service_type, svc.ip)
            if key not in seen:
                seen.add(key)
                unique.append(svc)

        _LAST_SCAN_COMPLETED_AT = time.monotonic()
        _LAST_SCAN_RESULT = tuple(unique)
        logger.info("Network scan found %d service(s)", len(unique))
        return unique


async def _scan_mdns() -> list[DiscoveredService]:
    """Scan for mDNS services (Home Assistant, Hubitat).

    Uses zeroconf if available, otherwise returns empty.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("zeroconf not installed — skipping mDNS scan")
        return []

    discovered: list[DiscoveredService] = []

    _MDNS_SERVICES = {
        "_home-assistant._tcp.local.": ("home_assistant", "Home Assistant"),
        "_hubitat._tcp.local.": ("hubitat", "Hubitat Elevation"),
        "_hue._tcp.local.": ("philips_hue", "Philips Hue"),
        "_sonos._tcp.local.": ("sonos", "Sonos"),
        "_hap._tcp.local.": ("homekit", "HomeKit Accessory"),
        "_smartthings._tcp.local.": ("smartthings", "SmartThings"),
        "_spotify-connect._tcp.local.": ("spotify_connect", "Spotify Connect Device"),
    }

    class _Listener:
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                svc_type, display = _MDNS_SERVICES.get(type_, ("unknown", name))
                discovered.append(
                    DiscoveredService(
                        service_type=svc_type,
                        display_name=display,
                        ip=ip,
                        port=info.port or 0,
                    )
                )

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    listener = _Listener()
    browsers = []
    try:
        for svc_type in _MDNS_SERVICES:
            browsers.append(ServiceBrowser(zc, svc_type, listener))

        # Give mDNS 3 seconds to discover services
        await asyncio.sleep(3)
    finally:
        zc.close()

    return discovered


async def _scan_known_ports() -> list[DiscoveredService]:
    """Scan known ports on the local network gateway for smart home services."""
    discovered: list[DiscoveredService] = []

    # Get local network gateway (common patterns)
    gateway_candidates = _get_gateway_candidates()

    for ip in gateway_candidates:
        for port, (svc_type, display_name) in _PORT_SERVICE_MAP.items():
            if await _check_port(ip, port):
                discovered.append(
                    DiscoveredService(
                        service_type=svc_type,
                        display_name=display_name,
                        ip=ip,
                        port=port,
                    )
                )

    return discovered


async def _check_port(ip: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a port is open on a given IP."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (TimeoutError, ConnectionRefusedError, OSError):
        return False


def _get_gateway_candidates() -> list[str]:
    """Get likely local network IPs to scan.

    Returns common gateway/server IPs on the local subnet,
    including localhost and the machine's own IP.
    """
    candidates = []

    # Try to detect the local IP to infer the subnet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        # Infer subnet and add common server IPs
        parts = local_ip.split(".")
        if len(parts) == 4:
            subnet = ".".join(parts[:3])
            # Common IPs for home servers
            for last_octet in [1, 2, 50, 100, 150, 200]:
                ip = "%s.%d" % (subnet, last_octet)
                if ip not in candidates:
                    candidates.append(ip)
        # Also check localhost and own IP - services may run on this machine
        if local_ip not in candidates:
            candidates.append(local_ip)
    except Exception:
        logger.debug("Could not detect local subnet", exc_info=True)

    # Always include localhost
    if "127.0.0.1" not in candidates:
        candidates.insert(0, "127.0.0.1")

    return candidates
