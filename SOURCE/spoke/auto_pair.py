"""
Auto-Pairing — discovers hub on the local network via mDNS.

Reuses the existing ``ViolaServiceBrowser`` to find Viola hub instances.
On discovery, returns the hub's host and port so the spoke can connect.
Falls back to explicit ``--hub-host`` if mDNS discovery times out.

Security model (SEC-047, 2026-06-09 sweep — FL-SPOKE-AUDIO):
    mDNS is an unauthenticated LAN protocol. Any device on the network can
    advertise a ``_viola._tcp`` service and impersonate the hub. The original
    code accepted the *first responder* and immediately streamed live
    microphone audio to it (``spoke/mic_streamer.py``), so a rogue hub that
    won the discovery race captured the user's mic.

    A ``role=hub`` TXT check is NOT authentication: an attacker simply
    advertises ``role=hub``. The load-bearing controls here are:

      1. **Pairing credential required.** Auto-discovery only trusts a
         discovered hub when this spoke holds a pairing credential
         (``VIOLA_SPOKE_TOKEN``). With no credential the spoke cannot have
         been paired with any hub, so auto-trusting a LAN responder is never
         safe — :func:`discover_hub` fails closed (returns ``None``) and the
         operator must name the hub explicitly with ``--hub-host``.

      2. **Hub identity pinning.** When the spoke knows which hub it paired
         with (``VIOLA_EXPECTED_HUB_DEVICE_ID`` and/or
         ``VIOLA_EXPECTED_HUB_HOST``), a discovered hub is trusted only when
         its mDNS identity matches the pin. A rogue responder that does not
         match the paired hub is rejected even if it advertises ``role=hub``.

    The hub independently authenticates the spoke's token on the WebSocket
    handshake (``ui/security/spoke_credentials.py`` — a different lane). The
    two halves together are mutual authentication: the spoke pins the hub
    identity it paired with; the hub verifies the spoke's HMAC credential.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class HubInfo:
    """Discovered hub connection details."""

    host: str
    port: int
    room_name: str
    device_id: str


def _expected_hub_device_id() -> str:
    """The device_id of the hub this spoke was paired with, if pinned."""
    return os.environ.get("VIOLA_EXPECTED_HUB_DEVICE_ID", "").strip()


def _expected_hub_host() -> str:
    """The host/IP of the hub this spoke was paired with, if pinned."""
    return os.environ.get("VIOLA_EXPECTED_HUB_HOST", "").strip()


def _spoke_has_pairing_credential() -> bool:
    """True when this spoke holds a pairing credential (it was paired).

    Without a credential the spoke was never paired with any hub, so trusting
    an unauthenticated mDNS responder would stream the mic to a stranger.
    """
    return bool(os.environ.get("VIOLA_SPOKE_TOKEN", "").strip())


def hub_is_authenticated(
    device: object,
    *,
    expected_device_id: str = "",
    expected_host: str = "",
) -> bool:
    """Decide whether a discovered hub may be trusted (fail closed).

    A discovered mDNS device is trusted only when:

    * it advertises the ``hub`` role (an explicit non-``hub`` role is always
      rejected — this filters Viola spokes/satellites, though it is not by
      itself an authentication signal since mDNS TXT is forgeable), AND
    * it matches the spoke's pinned hub identity. If a ``device_id`` pin is
      configured it MUST match; otherwise if a host pin is configured it MUST
      match. With neither pin configured the caller must have already required
      a pairing credential (see :func:`discover_hub`) — pinning then narrows
      *which* paired hub, host/device pins being optional belt-and-suspenders.

    Returns ``False`` on any mismatch or missing identity (fail closed).
    """
    role = getattr(device, "role", "") or ""
    if role and role != "hub":
        logger.debug(
            "Rejecting discovered device %s: role=%r is not 'hub'",
            getattr(device, "room_name", "?"),
            role,
        )
        return False

    dev_id = (getattr(device, "device_id", "") or "").strip()
    host = (getattr(device, "host", "") or "").strip()

    if expected_device_id:
        if dev_id != expected_device_id:
            logger.warning(
                "Rejecting discovered hub %s: device_id %r does not match pinned hub %r",
                getattr(device, "room_name", "?"),
                dev_id,
                expected_device_id,
            )
            return False
        return True

    if expected_host:
        if host != expected_host:
            logger.warning(
                "Rejecting discovered hub %s: host %r does not match pinned hub host %r",
                getattr(device, "room_name", "?"),
                host,
                expected_host,
            )
            return False
        return True

    # No identity pin configured. The caller (discover_hub) has already
    # required a pairing credential, so this is a paired spoke on its own LAN;
    # accept the role-validated hub. Operators wanting strict anti-spoofing
    # should pin VIOLA_EXPECTED_HUB_DEVICE_ID.
    return True


async def discover_hub(
    timeout: float = 15.0,
    local_device_id: str = "",
    *,
    require_authenticated: bool = True,
) -> HubInfo | None:
    """Discover a Viola hub on the local network via mDNS.

    Blocks for up to ``timeout`` seconds.  Returns the first *authenticated*
    hub found, or ``None`` if no trusted hub is discovered in time.

    Fails closed (returns ``None`` without browsing) when
    ``require_authenticated`` is set and this spoke holds no pairing credential
    — auto-trusting an unauthenticated LAN responder would let a rogue hub
    capture the live microphone (SEC-047).

    Args:
        timeout: Maximum seconds to wait for discovery.
        local_device_id: This spoke's device ID (to filter self-discovery).
        require_authenticated: When True (default), enforce the pairing
            credential + hub-identity pin before trusting any responder. Set
            False only for trusted/loopback test scenarios.

    Returns:
        HubInfo with connection details for an authenticated hub, or None.
    """
    expected_device_id = _expected_hub_device_id()
    expected_host = _expected_hub_host()

    if require_authenticated and not _spoke_has_pairing_credential():
        logger.error(
            "Refusing mDNS auto-discovery: no pairing credential (VIOLA_SPOKE_TOKEN) "
            "is set, so any LAN device could impersonate the hub and capture the "
            "microphone. Pair this spoke (or pass --hub-host for a trusted hub)."
        )
        return None

    try:
        from services.multiroom.discovery import (
            ZEROCONF_AVAILABLE,
            DiscoveredDevice,
            ViolaServiceBrowser,
        )
    except ImportError:
        logger.warning("zeroconf/discovery not available — cannot auto-discover hub")
        return None

    if not ZEROCONF_AVAILABLE:
        logger.warning("zeroconf not installed — cannot auto-discover hub")
        return None

    found_hub: HubInfo | None = None
    event = asyncio.Event()

    def _on_found(device: DiscoveredDevice) -> None:
        nonlocal found_hub
        if found_hub is not None:
            return
        # Authenticate the discovered hub before trusting it. mDNS is
        # unauthenticated, so a first-responder is NOT safe to stream mic
        # audio to (SEC-047). Reject anything that fails the credential/pin
        # gate; keep listening for a hub that does pass.
        if require_authenticated and not hub_is_authenticated(
            device,
            expected_device_id=expected_device_id,
            expected_host=expected_host,
        ):
            return
        found_hub = HubInfo(
            host=device.host,
            port=device.port,
            room_name=device.room_name,
            device_id=device.device_id,
        )
        logger.info(
            "Discovered authenticated hub: %s at %s:%d",
            device.room_name,
            device.host,
            device.port,
        )
        # Signal the async waiter
        try:
            asyncio.get_event_loop().call_soon_threadsafe(event.set)
        except RuntimeError:
            event.set()

    browser = ViolaServiceBrowser(
        local_device_id=local_device_id or "spoke-temp",
        on_device_found=_on_found,
    )

    browser.start()
    try:
        logger.info("Searching for Viola hub via mDNS (timeout=%.0fs)...", timeout)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning("mDNS discovery timed out after %.0fs — no trusted hub found", timeout)
    finally:
        browser.stop()

    return found_hub


def discover_hub_sync(
    timeout: float = 15.0,
    local_device_id: str = "",
    *,
    require_authenticated: bool = True,
) -> HubInfo | None:
    """Synchronous wrapper around :func:`discover_hub`."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't use loop.run_until_complete in a running loop
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    discover_hub(
                        timeout,
                        local_device_id,
                        require_authenticated=require_authenticated,
                    ),
                )
                return future.result(timeout=timeout + 5)
        return loop.run_until_complete(
            discover_hub(timeout, local_device_id, require_authenticated=require_authenticated)
        )
    except Exception:
        logger.exception("Hub discovery failed")
        return None
