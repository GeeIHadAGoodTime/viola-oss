"""
Device Discovery Service for Multi-Room Synchronization.

Enables Viola instances to discover each other on the local network using
mDNS/Zeroconf. This module provides:

- ViolaServiceAnnouncer: Announces this instance on the network
- ViolaServiceBrowser: Discovers other Viola instances
- DiscoveryService: Combined service managing both announcer and browser

Privacy-first: Only discovers devices on local network, no external servers.

Usage:
    >>> from services.multiroom.discovery import DiscoveryService
    >>>
    >>> discovery = DiscoveryService(
    ...     device_id="uuid-...",
    ...     room_name="Living Room",
    ...     api_port=8756,
    ... )
    >>> discovery.start()
    >>> # ... application runs ...
    >>> discovery.stop()

Integration with RoomRegistry (optional auto-registration):
    >>> from services.multiroom.discovery import DiscoveryService
    >>> from core.room_registry import get_room_registry
    >>>
    >>> # NOTE: In production, do NOT pass room_registry so that users
    >>> # explicitly connect devices via POST /api/v1/devices/{id}/connect.
    >>> # Only pass room_registry for automated/test scenarios.
    >>> discovery = DiscoveryService(
    ...     device_id="uuid-...",
    ...     room_name="Living Room",
    ...     api_port=8756,
    ... )
"""

from __future__ import annotations

import platform
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from config.settings import settings
from core.constants import DEFAULT_API_PORT, LOCALHOST, TIMEOUT_DEFAULT
from core.logging_config import get_logger
from services.multiroom.exceptions import DeviceDiscoveryError

logger = get_logger(__name__)

# Attempt to import zeroconf - graceful degradation if unavailable
try:
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False
    logger.debug("zeroconf not available - mDNS discovery disabled")

if TYPE_CHECKING:
    from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

    from core.room_registry import RoomRegistry


# =============================================================================
# Constants
# =============================================================================

SERVICE_TYPE = "_viola._tcp.local."
SERVICE_NAME_PREFIX = "Viola"
API_VERSION = "1.0"

# Device staleness - remove devices not seen in this time (seconds)
DEVICE_STALE_TIMEOUT = 30.0

# Heartbeat interval for periodic announcements
HEARTBEAT_INTERVAL = 10.0


# =============================================================================
# Data Types
# =============================================================================


@dataclass
class DiscoveredDevice:
    """Information about a discovered Viola device on the network."""

    device_id: str
    room_name: str
    host: str
    port: int
    last_seen: float
    api_version: str = API_VERSION
    capabilities: list[str] = field(default_factory=list)
    role: str = "hub"

    def __post_init__(self) -> None:
        if not self.capabilities:
            self.capabilities = ["multiroom_sync", "playback_control"]

    @property
    def is_stale(self) -> bool:
        """Check if device hasn't been seen recently."""
        return (time.time() - self.last_seen) > DEVICE_STALE_TIMEOUT

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "device_id": self.device_id,
            "room_name": self.room_name,
            "host": self.host,
            "port": self.port,
            "last_seen": self.last_seen,
            "api_version": self.api_version,
            "capabilities": self.capabilities,
            "role": self.role,
        }


class DeviceCallback(Protocol):
    """Protocol for device discovery callbacks."""

    def __call__(self, device: DiscoveredDevice) -> None: ...


class DeviceRemovedCallback(Protocol):
    """Protocol for device removal callbacks."""

    def __call__(self, device_id: str) -> None: ...


# =============================================================================
# mDNS Service Listener
# =============================================================================


class _FallbackServiceListener:
    """Fallback when zeroconf is not available."""

    pass


if ZEROCONF_AVAILABLE:
    ServiceListenerBase: type[Any] = cast(type[Any], ServiceListener)
else:
    ServiceListenerBase = _FallbackServiceListener


class _MDNSServiceListener(ServiceListenerBase):
    """
    Listens for mDNS service events and forwards them to the browser.

    This adapter bridges zeroconf's callback interface to ViolaServiceBrowser.
    """

    def __init__(self, browser: ViolaServiceBrowser) -> None:
        self._browser = browser

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._browser._handle_service_added(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._browser._handle_service_updated(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self._browser._handle_service_removed(zeroconf, service_type, name)


# =============================================================================
# ViolaServiceAnnouncer
# =============================================================================


class ViolaServiceAnnouncer:
    """
    Announces this Viola instance on the network via mDNS.

    The announcer registers a service with the following information:
    - Service type: _viola._tcp.local.
    - Service name: Viola-{room_name}
    - TXT records: device_id, room_name, api_port, capabilities

    Thread Safety:
        All public methods are thread-safe.

    Example:
        >>> announcer = ViolaServiceAnnouncer(
        ...     device_id="abc123",
        ...     room_name="Living Room",
        ...     api_port=8756,
        ... )
        >>> announcer.start()
        >>> # ... later ...
        >>> announcer.stop()
    """

    def __init__(
        self,
        device_id: str,
        room_name: str,
        api_port: int = DEFAULT_API_PORT,
        capabilities: list[str] | None = None,
    ) -> None:
        """
        Initialize the service announcer.

        Args:
            device_id: Unique identifier for this device (usually a UUID)
            room_name: Human-readable room name for this device
            api_port: Port where the API is running
            capabilities: Optional list of capabilities (defaults to standard set)
        """
        self.device_id = device_id
        self.room_name = room_name
        self.api_port = api_port
        self.capabilities = capabilities or ["multiroom_sync", "playback_control"]

        self._zeroconf: Zeroconf | None = None
        self._service_info: ServiceInfo | None = None
        self._running = False
        self._lock = threading.Lock()

        logger.debug(
            "ViolaServiceAnnouncer initialized",
            device_id=device_id[:8],
            room_name=room_name,
            api_port=api_port,
        )

    def start(self) -> None:
        """
        Start announcing this device on the network.

        Raises:
            DeviceDiscoveryError: If mDNS registration fails
        """
        if not ZEROCONF_AVAILABLE:
            logger.warning("zeroconf not available - cannot announce service")
            return

        with self._lock:
            if self._running:
                logger.debug("Announcer already running")
                return

            try:
                self._register_service()
                self._running = True
                logger.info(
                    "Service announced on network",
                    room_name=self.room_name,
                    api_port=self.api_port,
                )
            except Exception as e:
                logger.exception("Failed to announce service")
                raise DeviceDiscoveryError(f"Failed to announce service: {e}") from e

    def stop(self) -> None:
        """Stop announcing this device on the network."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._unregister_service()
            logger.info("Service announcement stopped", room_name=self.room_name)

    def _register_service(self) -> None:
        """Register the mDNS service."""
        if not ZEROCONF_AVAILABLE:
            return

        local_ip = self._get_local_ip()

        # Build TXT record properties
        properties = {
            "device_id": self.device_id,
            "room_name": self.room_name,
            "api_version": API_VERSION,
            "capabilities": ",".join(self.capabilities),
        }

        # Create service name (sanitize room name for DNS)
        # Include port to guarantee uniqueness when multiple instances share a hostname
        safe_name = self._sanitize_service_name(self.room_name)
        service_name = f"{SERVICE_NAME_PREFIX}-{safe_name}-{self.api_port}.{SERVICE_TYPE}"

        self._service_info = ServiceInfo(
            SERVICE_TYPE,
            service_name,
            addresses=[socket.inet_aton(local_ip)],
            port=self.api_port,
            properties=properties,
        )

        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._service_info)

        logger.debug(
            "mDNS service registered",
            service_name=service_name,
            ip=local_ip,
            port=self.api_port,
        )

    def _unregister_service(self) -> None:
        """Unregister the mDNS service."""
        if self._zeroconf is not None:
            try:
                if self._service_info is not None:
                    self._zeroconf.unregister_service(self._service_info)
                self._zeroconf.close()
            except Exception as e:
                logger.debug("Error unregistering service: %s", e)
            finally:
                self._zeroconf = None
                self._service_info = None

    def _get_local_ip(self) -> str:
        """Get the local IP address for this device."""
        try:
            # Create a UDP socket to determine the local IP
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Connect to a public address (doesn't actually send data)
                sock.connect(("8.8.8.8", 80))
                ip = str(sock.getsockname()[0])
            finally:
                sock.close()
            return ip
        except Exception as e:
            logger.debug("Could not determine local IP: %s, using localhost", e)
            return LOCALHOST

    @staticmethod
    def _sanitize_service_name(name: str) -> str:
        """Sanitize a name for use in DNS service name."""
        # Replace spaces and special characters
        sanitized = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
        # Remove consecutive hyphens
        while "--" in sanitized:
            sanitized = sanitized.replace("--", "-")
        # Trim leading/trailing hyphens
        return sanitized.strip("-") or "unknown"


# =============================================================================
# ViolaServiceBrowser
# =============================================================================


class ViolaServiceBrowser:
    """
    Discovers other Viola instances on the network via mDNS.

    The browser monitors for _viola._tcp.local. services and maintains
    a list of discovered devices. It can optionally integrate with
    RoomRegistry to auto-register discovered devices.

    Thread Safety:
        All public methods are thread-safe.

    Example:
        >>> def on_device_found(device: DiscoveredDevice) -> None:
        ...     print(f"Found: {device.room_name}")
        ...
        >>> browser = ViolaServiceBrowser(
        ...     local_device_id="my-device-id",
        ...     on_device_found=on_device_found,
        ... )
        >>> browser.start()
    """

    def __init__(
        self,
        local_device_id: str,
        on_device_found: DeviceCallback | None = None,
        on_device_removed: DeviceRemovedCallback | None = None,
        room_registry: RoomRegistry | None = None,
    ) -> None:
        """
        Initialize the service browser.

        Args:
            local_device_id: This device's ID (to filter out self-discovery)
            on_device_found: Callback when a new device is discovered
            on_device_removed: Callback when a device is removed
            room_registry: Optional RoomRegistry to auto-register devices
        """
        self.local_device_id = local_device_id
        self._on_device_found = on_device_found
        self._on_device_removed = on_device_removed
        self._room_registry = room_registry

        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._listener: _MDNSServiceListener | None = None

        self._devices: dict[str, DiscoveredDevice] = {}
        self._service_to_device: dict[str, str] = {}  # service_name -> device_id
        self._lock = threading.Lock()
        self._running = False

        # Cleanup thread for stale devices
        self._cleanup_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        logger.debug(
            "ViolaServiceBrowser initialized",
            local_device_id=local_device_id[:8],
        )

    def start(self) -> None:
        """
        Start browsing for Viola devices on the network.

        Raises:
            DeviceDiscoveryError: If browser cannot be started
        """
        if not ZEROCONF_AVAILABLE:
            logger.warning("zeroconf not available - cannot browse for services")
            return

        with self._lock:
            if self._running:
                logger.debug("Browser already running")
                return

            try:
                self._start_browser()
                self._start_cleanup_thread()
                self._running = True
                logger.info("Service browser started")
            except Exception as e:
                logger.exception("Failed to start service browser")
                raise DeviceDiscoveryError(f"Failed to start browser: {e}") from e

    def stop(self) -> None:
        """Stop browsing for devices."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._stop_event.set()

            # Stop cleanup thread
            if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
                self._cleanup_thread.join(timeout=TIMEOUT_DEFAULT)
            self._cleanup_thread = None

            # Stop browser
            self._stop_browser()

            # Clear device list
            self._devices.clear()
            self._service_to_device.clear()

            logger.info("Service browser stopped")

    def get_devices(self) -> list[DiscoveredDevice]:
        """
        Get all currently discovered devices.

        Returns:
            List of discovered devices (excludes stale devices)
        """
        with self._lock:
            # Filter out stale devices
            return [d for d in self._devices.values() if not d.is_stale]

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        """
        Get a specific device by ID.

        Args:
            device_id: The device ID to look up

        Returns:
            The device if found and not stale, None otherwise
        """
        with self._lock:
            device = self._devices.get(device_id)
            if device is not None and not device.is_stale:
                return device
            return None

    def _start_browser(self) -> None:
        """Start the mDNS service browser."""
        if not ZEROCONF_AVAILABLE:
            return

        self._zeroconf = Zeroconf()
        self._listener = _MDNSServiceListener(self)
        self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, listener=self._listener)

        logger.debug("mDNS browser started for %s", SERVICE_TYPE)

    def _stop_browser(self) -> None:
        """Stop the mDNS service browser."""
        if self._browser is not None:
            try:
                self._browser.cancel()
            except Exception as e:
                logger.debug("Error cancelling browser: %s", e)
            self._browser = None
            self._listener = None

        if self._zeroconf is not None:
            try:
                self._zeroconf.close()
            except Exception as e:
                logger.debug("Error closing zeroconf: %s", e)
            self._zeroconf = None

    def _start_cleanup_thread(self) -> None:
        """Start the background thread for cleaning up stale devices."""
        self._stop_event.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="ViolaBrowser-Cleanup",
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Background loop to remove stale devices."""
        while not self._stop_event.is_set():
            # Wait for interval or stop event
            if self._stop_event.wait(timeout=DEVICE_STALE_TIMEOUT / 2):
                break

            self._remove_stale_devices()

    def _remove_stale_devices(self) -> None:
        """Remove devices that haven't been seen recently."""
        with self._lock:
            stale_ids = [device_id for device_id, device in self._devices.items() if device.is_stale]

            for device_id in stale_ids:
                device = self._devices.pop(device_id, None)
                if device is not None:
                    logger.info(
                        "Removed stale device",
                        device_id=device_id[:8],
                        room_name=device.room_name,
                    )

                    # Notify callback
                    if self._on_device_removed is not None:
                        try:
                            self._on_device_removed(device_id)
                        except Exception as e:
                            logger.error("Device removal callback error: %s", e)

                    # Remove from room registry
                    if self._room_registry is not None:
                        try:
                            self._room_registry.unregister_room(device_id)
                        except Exception as e:
                            logger.debug("Error unregistering room: %s", e)

                    # Clean up service mapping
                    services_to_remove = [svc for svc, did in self._service_to_device.items() if did == device_id]
                    for svc in services_to_remove:
                        self._service_to_device.pop(svc, None)

    def _handle_service_added(self, zeroconf: Any, service_type: str, name: str) -> None:
        """Handle mDNS service addition event."""
        if not self._running:
            return

        try:
            self._process_service(zeroconf, service_type, name)
        except Exception as e:
            logger.debug("Error processing service addition: %s", e)

    def _handle_service_updated(self, zeroconf: Any, service_type: str, name: str) -> None:
        """Handle mDNS service update event."""
        if not self._running:
            return

        try:
            self._process_service(zeroconf, service_type, name)
        except Exception as e:
            logger.debug("Error processing service update: %s", e)

    def _handle_service_removed(self, zeroconf: Any, service_type: str, name: str) -> None:
        """Handle mDNS service removal event."""
        if not self._running:
            return

        with self._lock:
            device_id = self._service_to_device.pop(name, None)
            if device_id is not None and device_id in self._devices:
                device = self._devices.pop(device_id)
                logger.info(
                    "Device removed from network",
                    device_id=device_id[:8],
                    room_name=device.room_name,
                )

                # Notify callback
                if self._on_device_removed is not None:
                    try:
                        self._on_device_removed(device_id)
                    except Exception as e:
                        logger.error("Device removal callback error: %s", e)

                # Remove from room registry
                if self._room_registry is not None:
                    try:
                        self._room_registry.unregister_room(device_id)
                    except Exception as e:
                        logger.debug("Error unregistering room: %s", e)

    def _process_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        """Process a discovered or updated service."""
        if not ZEROCONF_AVAILABLE:
            return

        info = zeroconf.get_service_info(service_type, name)
        if info is None:
            logger.debug("Could not get service info for %s", name)
            return

        # Extract properties from TXT records
        device_id = self._get_property(info, "device_id")
        if not device_id:
            logger.debug("Service %s has no device_id", name)
            return

        # Don't register ourselves
        if device_id == self.local_device_id:
            logger.debug("Ignoring self-discovery for %s", name)
            return

        room_name = self._get_property(info, "room_name") or name.split(".")[0]
        api_version = self._get_property(info, "api_version") or API_VERSION
        capabilities_str = self._get_property(info, "capabilities") or ""
        capabilities = [c.strip() for c in capabilities_str.split(",") if c.strip()]

        # Get host address
        host = LOCALHOST
        if info.addresses:
            try:
                host = socket.inet_ntoa(info.addresses[0])
            except Exception:
                logger.debug("Failed to parse device host address, using localhost fallback")

        device = DiscoveredDevice(
            device_id=device_id,
            room_name=room_name,
            host=host,
            port=info.port,
            last_seen=time.time(),
            api_version=api_version,
            capabilities=capabilities or ["multiroom_sync", "playback_control"],
        )

        with self._lock:
            is_new = device_id not in self._devices
            self._devices[device_id] = device
            self._service_to_device[name] = device_id

        if is_new:
            logger.info(
                "Discovered new device",
                device_id=device_id[:8],
                room_name=room_name,
                host=host,
                port=info.port,
            )

            # Notify callback
            if self._on_device_found is not None:
                try:
                    self._on_device_found(device)
                except Exception as e:
                    logger.error("Device found callback error: %s", e)

            # Register with room registry
            if self._room_registry is not None:
                try:
                    self._room_registry.register_remote_room(
                        room_id=device_id,
                        room_name=room_name,
                    )
                except Exception as e:
                    logger.error("Error registering room: %s", e)
        else:
            logger.debug(
                "Updated device",
                device_id=device_id[:8],
                room_name=room_name,
            )

    @staticmethod
    def _get_property(info: Any, key: str) -> str:
        """Extract a string property from ServiceInfo."""
        if info.properties is None:
            return ""

        value = info.properties.get(key.encode("utf-8"), b"")
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)


# =============================================================================
# DiscoveryService - Combined Manager
# =============================================================================


class DiscoveryService:
    """
    Combined service that manages both announcing and browsing.

    This is the main entry point for device discovery functionality.
    It coordinates the announcer and browser, and handles edge cases
    like network changes and test mode detection.

    Thread Safety:
        All public methods are thread-safe.

    Example:
        >>> from services.multiroom.discovery import DiscoveryService
        >>> from core.room_registry import get_room_registry
        >>>
        >>> discovery = DiscoveryService(
        ...     device_id="my-uuid",
        ...     room_name="Living Room",
        ...     api_port=8756,
        ...     room_registry=get_room_registry(),
        ... )
        >>> discovery.start()
        >>> # ... application runs ...
        >>> devices = discovery.get_discovered_devices()
        >>> discovery.stop()
    """

    def __init__(
        self,
        device_id: str | None = None,
        room_name: str | None = None,
        api_port: int | None = None,
        capabilities: list[str] | None = None,
        room_registry: RoomRegistry | None = None,
        on_device_found: DeviceCallback | None = None,
        on_device_removed: DeviceRemovedCallback | None = None,
    ) -> None:
        """
        Initialize the discovery service.

        Args:
            device_id: Unique identifier for this device (auto-generated if None)
            room_name: Human-readable room name (auto-generated from hostname if None)
            api_port: Port where the API is running (defaults to settings.api_port)
            capabilities: Optional list of capabilities
            room_registry: Optional RoomRegistry for auto-registration
            on_device_found: Callback when a new device is discovered
            on_device_removed: Callback when a device is removed
        """
        self.device_id = device_id or str(uuid.uuid4())
        self.room_name = room_name or self._get_default_room_name()
        self.api_port = api_port or settings.api_port

        self._announcer = ViolaServiceAnnouncer(
            device_id=self.device_id,
            room_name=self.room_name,
            api_port=self.api_port,
            capabilities=capabilities,
        )

        self._browser = ViolaServiceBrowser(
            local_device_id=self.device_id,
            on_device_found=on_device_found,
            on_device_removed=on_device_removed,
            room_registry=room_registry,
        )

        self._running = False
        self._lock = threading.Lock()

        logger.debug(
            "DiscoveryService initialized",
            device_id=self.device_id[:8],
            room_name=self.room_name,
        )

    def start(self) -> bool:
        """
        Start the discovery service (both announcing and browsing).

        Returns:
            True if started successfully, False if disabled or failed
        """
        # Check if discovery is disabled
        if self._is_discovery_disabled():
            logger.info("Device discovery is disabled")
            return False

        with self._lock:
            if self._running:
                logger.debug("Discovery service already running")
                return True

            try:
                self._announcer.start()
                self._browser.start()
                self._running = True
                logger.info(
                    "Discovery service started",
                    device_id=self.device_id[:8],
                    room_name=self.room_name,
                )
                return True
            except Exception:
                logger.exception("Failed to start discovery service")
                # Attempt cleanup
                try:
                    self._announcer.stop()
                except Exception:
                    logger.debug("Announcer stop failed during discovery cleanup")
                try:
                    self._browser.stop()
                except Exception:
                    logger.debug("Browser stop failed during discovery cleanup")
                return False

    def stop(self) -> None:
        """Stop the discovery service."""
        with self._lock:
            if not self._running:
                return

            self._running = False
            self._announcer.stop()
            self._browser.stop()
            logger.info("Discovery service stopped")

    def get_discovered_devices(self) -> list[DiscoveredDevice]:
        """
        Get all currently discovered devices.

        Returns:
            List of discovered devices (excludes this device and stale devices)
        """
        return self._browser.get_devices()

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        """
        Get a specific discovered device by ID.

        Args:
            device_id: The device ID to look up

        Returns:
            The device if found, None otherwise
        """
        return self._browser.get_device(device_id)

    def is_running(self) -> bool:
        """Check if the discovery service is currently running."""
        with self._lock:
            return self._running

    def _is_discovery_disabled(self) -> bool:
        """Check if device discovery should be disabled."""
        # Check settings
        if settings.disable_device_discovery:
            return True

        # Check test mode
        if settings.test_mode:
            return True

        # Check pytest marker
        if getattr(settings, "pytest_in_progress", False):
            return True

        return False

    @staticmethod
    def _get_default_room_name() -> str:
        """Generate a default room name from the hostname."""
        try:
            hostname = platform.node()
            if hostname:
                return f"Viola-{hostname}"
        except Exception as e:
            logger.debug("Could not get hostname: %s", e)
        return "Viola"


# =============================================================================
# Module-level singleton (optional convenience)
# =============================================================================

_discovery_service: DiscoveryService | None = None
_discovery_lock = threading.Lock()


def get_discovery_service(
    device_id: str | None = None,
    room_name: str | None = None,
    room_registry: RoomRegistry | None = None,
) -> DiscoveryService:
    """
    Get or create the global discovery service singleton.

    This is a convenience function for applications that want a single
    shared discovery service instance.

    Args:
        device_id: Device ID (only used on first call)
        room_name: Room name (only used on first call)
        room_registry: Optional RoomRegistry for auto-registration

    Returns:
        The global DiscoveryService instance
    """
    global _discovery_service

    with _discovery_lock:
        if _discovery_service is None:
            _discovery_service = DiscoveryService(
                device_id=device_id,
                room_name=room_name,
                room_registry=room_registry,
            )
        return _discovery_service


def reset_discovery_service() -> None:
    """
    Reset the global discovery service (for testing).
    """
    global _discovery_service

    with _discovery_lock:
        if _discovery_service is not None:
            _discovery_service.stop()
            _discovery_service = None


__all__ = [
    "ZEROCONF_AVAILABLE",
    "DiscoveredDevice",
    "DiscoveryService",
    "ViolaServiceAnnouncer",
    "ViolaServiceBrowser",
    "get_discovery_service",
    "reset_discovery_service",
]
