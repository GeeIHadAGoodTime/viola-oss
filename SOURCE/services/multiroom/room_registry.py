"""
Room Registry Service - Multi-room Device Tracking

Tracks available rooms/devices for multi-room audio synchronization.
Each room represents a physical device running the Viola application.

Usage:
    from services.multiroom.room_registry import get_room_registry, RoomInfo

    registry = get_room_registry()

    # Get the local room (auto-registered on startup)
    local_room = registry.get_local_room()

    # List all known rooms
    rooms = registry.list_rooms()

    # Register a discovered remote room
    remote_room = RoomInfo(
        id="abc123",
        name="Living Room",
        device_id="device_fingerprint_here",
        ip_address="192.168.1.100",
        is_local=False,
        status="online",
        last_seen=time.time(),
    )
    registry.register_room(remote_room)
"""

from __future__ import annotations

import json
import os
import platform
import socket
import threading
import time
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.constants import LOCALHOST
from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


def _clamp_volume(value: object, default: int = 80) -> int:
    """Normalize room volume to the API contract range."""
    if isinstance(value, bool):
        return default
    try:
        volume = int(value)
    except (TypeError, ValueError):
        volume = default
    return max(0, min(100, volume))


def _default_storage_path() -> Path:
    """Return the durable room registry path under the configured data dir."""
    try:
        from config.settings import settings

        return Path(settings.data_dir) / "multiroom" / "rooms.json"
    except Exception:
        return get_data_dir() / "multiroom" / "rooms.json"


def _multiroom_dir() -> Path:
    """Return the multiroom state directory under the configured data dir."""
    try:
        from config.settings import settings

        data_dir = Path(settings.data_dir)
    except (ImportError, AttributeError, TypeError, ValueError):
        data_dir = get_data_dir()
    return data_dir / "multiroom"


def _storage_path_for_user(user_id: str) -> Path:
    """Return a tenant-scoped room registry path without exposing raw IDs."""
    digest = sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return _multiroom_dir() / "users" / digest / "rooms.json"


def _require_user_id(user_id: str | None) -> str:
    """Normalize a tenant id before creating user-owned room registry state."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("Room registry operations require user_id")
    return user_id.strip()


# --------------------------------------------------------------------------- #
# Registry ownership                                                           #
# --------------------------------------------------------------------------- #

# Sentinel owner key for the single desktop install.  It is not a legal user id
# (no GoTrue UUID and no ``device-...`` principal can collide with it), so it can
# never be mistaken for, or produced by, a real tenant.
DESKTOP_INSTALL_OWNER = "__desktop_install__"


def _is_cloud_surface() -> bool:
    """Return True when this process serves the multi-tenant cloud surface.

    Fails CLOSED: if the surface cannot be determined the caller is treated as
    cloud, which keeps strict per-account separation rather than collapsing two
    principals onto one store.
    """
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud"
    except (ImportError, AttributeError, TypeError, ValueError):
        return True


def resolve_room_registry_owner(user_id: str) -> str:
    """Return the owner key whose registry holds *user_id*'s paired rooms.

    Rooms in this registry are physical LAN speakers paired to ONE desktop
    install: Tier 3 device-local data that never reaches the cloud (the cloud
    surface has its own multi-tenant room service, and this module is not
    reachable from ``backend/cloud_app.py`` at all).  Viola is one user per
    install, so the owner of that durable list is the *install*, not whichever
    principal string a particular auth path happened to resolve.

    Before this resolver existed the storage path was keyed directly on the
    caller-supplied principal, so a browser spoke registering over
    ``/ws/audio-stream`` (which carries the device principal on its spoke
    credential, because a WebSocket has no request user contextvar) wrote to a
    different file than every HTTP reader, which carries the signed-in account
    principal.  The user's own paired rooms were therefore invisible to Viola's
    agent, and signing in or rotating the device salt silently orphaned them
    again (#4432).

    On the cloud surface the caller's principal is returned unchanged, so
    per-account separation there is exactly what it was.
    """
    normalized = _require_user_id(user_id)
    if _is_cloud_surface():
        return normalized
    return DESKTOP_INSTALL_OWNER


def _storage_path_for_owner(owner: str) -> Path:
    """Return the registry file backing *owner* (see ``resolve_room_registry_owner``)."""
    if owner == DESKTOP_INSTALL_OWNER:
        return _multiroom_dir() / "install" / "rooms.json"
    return _storage_path_for_user(owner)


def _adopt_orphaned_desktop_rooms(install_path: Path) -> None:
    """Carry rooms paired under an earlier principal forward to the install.

    Purely additive and one-time: it runs only when the install registry does
    not exist yet, reads every per-principal registry already on this machine's
    disk, and writes their remote rooms into the install registry.  Nothing is
    overwritten and nothing is deleted, so the old files stay recoverable; and
    because the install file exists afterwards (even when empty), a room the
    user later deletes stays deleted instead of being resurrected on restart.
    """
    if install_path.exists():
        return

    multiroom_dir = install_path.parent.parent
    sources: list[Path] = []
    legacy_global = multiroom_dir / "rooms.json"
    if legacy_global.is_file():
        sources.append(legacy_global)
    users_dir = multiroom_dir / "users"
    if users_dir.is_dir():
        sources.extend(sorted(path for path in users_dir.glob("*/rooms.json") if path.is_file()))

    adopted: dict[str, dict[str, object]] = {}
    for source in sources:
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            logger.exception("Skipping unreadable multi-room registry at %s during install adoption", source)
            continue

        rows = raw.get("rooms") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            logger.warning("Skipping malformed multi-room registry at %s during install adoption", source)
            continue

        for row in rows:
            room = RoomRegistry._room_from_payload(row)
            if room is None or room.is_local:
                continue
            existing = adopted.get(room.id)
            if existing is not None and float(existing.get("last_seen") or 0.0) >= room.last_seen:
                continue
            adopted[room.id] = RoomRegistry._room_to_payload(room)

    payload = {"version": 1, "rooms": [adopted[key] for key in sorted(adopted)]}
    tmp_path = install_path.with_name(".%s.%d.tmp" % (install_path.name, os.getpid()))
    try:
        install_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(install_path)
    except (OSError, TypeError, ValueError):
        logger.exception("Failed to seed the desktop install room registry at %s", install_path)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove temporary install registry file", exc_info=True)
        return

    if adopted:
        logger.info(
            "Adopted %d previously paired room(s) into the desktop install registry from %d source file(s)",
            len(adopted),
            len(sources),
        )


# --------------------------------------------------------------------------- #
# Data Structures                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoomInfo:
    """
    Information about a room/device in the multi-room network.

    Attributes:
        id: Unique room identifier (typically derived from device fingerprint)
        name: User-friendly name (e.g., "Living Room", "Bedroom")
        device_id: Device fingerprint from audio hardware configuration
        ip_address: IP address of the device (None for local or unknown)
        is_local: Whether this is the local device
        status: Current online/offline status
        last_seen: Unix timestamp when the room was last seen/updated
        volume: Room output volume, 0-100
        muted: Whether the room output is muted
    """

    id: str
    name: str
    device_id: str
    ip_address: str | None = None
    is_local: bool = False
    status: Literal["online", "offline"] = "online"
    last_seen: float = field(default_factory=time.time)
    volume: int = 80
    muted: bool = False


# --------------------------------------------------------------------------- #
# Room Registry                                                                #
# --------------------------------------------------------------------------- #


class RoomRegistry:
    """
    Registry of available rooms/devices for multi-room audio.

    Thread-safe singleton that tracks all known rooms including the local device.
    The local device is automatically registered on first access.

    This registry provides the foundation for multi-room features like:
    - Room grouping for synchronized playback
    - Per-room volume control
    - Device discovery and status tracking
    """

    def __init__(self, storage_path: str | Path | None = None) -> None:
        """Initialize the room registry."""
        self._rooms: dict[str, RoomInfo] = {}
        self._lock = threading.RLock()
        self._local_room_id: str | None = None
        self._initialized = False
        self._storage_path = Path(storage_path) if storage_path is not None else _default_storage_path()

    def _ensure_initialized(self) -> None:
        """Ensure the local room is registered on first access."""
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            self._load_persisted_rooms_locked()
            self._register_local_room()
            self._initialized = True

    @staticmethod
    def _room_to_payload(room: RoomInfo) -> dict[str, object]:
        """Serialize a remote room to the persisted registry format."""
        return {
            "id": room.id,
            "name": room.name,
            "device_id": room.device_id,
            "ip_address": room.ip_address,
            "is_local": room.is_local,
            "status": room.status,
            "last_seen": room.last_seen,
            "volume": room.volume,
            "muted": room.muted,
        }

    @staticmethod
    def _room_from_payload(payload: object) -> RoomInfo | None:
        """Parse a persisted room record, returning None for malformed rows."""
        if not isinstance(payload, dict):
            return None

        room_id = payload.get("id")
        name = payload.get("name")
        device_id = payload.get("device_id")
        if not isinstance(room_id, str) or not room_id.strip():
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(device_id, str) or not device_id.strip():
            return None

        ip_address_obj = payload.get("ip_address")
        ip_address = ip_address_obj if isinstance(ip_address_obj, str) and ip_address_obj.strip() else None
        last_seen_obj = payload.get("last_seen")
        last_seen = float(last_seen_obj) if isinstance(last_seen_obj, (int, float)) else time.time()
        volume = _clamp_volume(payload.get("volume"), default=80)
        muted_obj = payload.get("muted", False)
        muted = muted_obj if isinstance(muted_obj, bool) else False

        # Persisted rooms are reloaded as offline until a spoke reconnects.
        return RoomInfo(
            id=room_id.strip(),
            name=name.strip(),
            device_id=device_id.strip(),
            ip_address=ip_address,
            is_local=False,
            status="offline",
            last_seen=last_seen,
            volume=volume,
            muted=muted,
        )

    def _load_persisted_rooms_locked(self) -> None:
        """Load previously paired remote rooms from disk."""
        path = self._storage_path
        if not path.exists():
            return

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to load persisted multi-room registry from %s", path)
            return

        room_rows = raw.get("rooms") if isinstance(raw, dict) else raw
        if not isinstance(room_rows, list):
            logger.warning("Ignoring malformed multi-room registry at %s", path)
            return

        loaded = 0
        for row in room_rows:
            room = self._room_from_payload(row)
            if room is None:
                continue
            self._rooms[room.id] = room
            loaded += 1

        if loaded:
            logger.info("Loaded %d persisted multi-room room(s)", loaded)

    def _persist_remote_rooms_locked(self) -> None:
        """Atomically persist non-local rooms to disk."""
        path = self._storage_path
        rows = [
            self._room_to_payload(room)
            for room in sorted(self._rooms.values(), key=lambda item: item.id)
            if not room.is_local
        ]
        payload = {"version": 1, "rooms": rows}
        tmp_path = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception:
            logger.exception("Failed to persist multi-room registry to %s", path)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to remove temporary multi-room registry file", exc_info=True)

    def _register_local_room(self) -> None:
        """Auto-register the local device as a room."""
        try:
            # Import here to avoid circular dependencies at module load time
            from voice.wake_detector.device_profile_manager import (
                get_device_display_name,
                get_device_fingerprint,
            )

            device_id = get_device_fingerprint()
            device_name = get_device_display_name()

            # Generate a friendly room name from hostname
            hostname = self._get_hostname()
            room_name = self._generate_room_name(hostname, device_name)

            # Get local IP address
            local_ip = self._get_local_ip()

            local_room = RoomInfo(
                id=device_id,
                name=room_name,
                device_id=device_id,
                ip_address=local_ip,
                is_local=True,
                status="online",
                last_seen=time.time(),
            )

            self._rooms[device_id] = local_room
            self._local_room_id = device_id

            logger.info(
                "Registered local room: %s (device_id=%s, ip=%s)",
                room_name,
                device_id[:8],
                local_ip,
            )

        except Exception as e:
            logger.exception("Failed to register local room: %s", e)
            # Create a fallback local room
            fallback_id = "local-fallback"
            fallback_room = RoomInfo(
                id=fallback_id,
                name="This Device",
                device_id=fallback_id,
                ip_address=LOCALHOST,
                is_local=True,
                status="online",
                last_seen=time.time(),
            )
            self._rooms[fallback_id] = fallback_room
            self._local_room_id = fallback_id
            logger.warning("Using fallback local room registration")

    def _get_hostname(self) -> str:
        """Get the system hostname."""
        try:
            return platform.node() or "Unknown"
        except Exception:
            return "Unknown"

    def _generate_room_name(self, hostname: str, device_name: str) -> str:
        """Generate a user-friendly room name."""
        # Clean up the hostname
        name = hostname.strip()

        # Remove common suffixes like .local, .lan, etc.
        for suffix in (".local", ".lan", ".home", ".internal"):
            if name.lower().endswith(suffix):
                name = name[: -len(suffix)]
                break

        # If hostname is too generic, use device name instead
        generic_names = {"localhost", "unknown", "pc", "computer", "desktop", "laptop"}
        if name.lower() in generic_names:
            # Try to extract something meaningful from device name
            if device_name and device_name != "unknown-input":
                # Use first part of device name
                name = device_name.split("/")[0].strip()[:20]
            else:
                name = "This Device"

        return name

    def _get_local_ip(self) -> str:
        """Get the local IP address of this device."""
        try:
            # Connect to an external address to determine local IP
            # We don't actually send any data, just determine the route
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                ip = str(sock.getsockname()[0])
            finally:
                sock.close()
            return ip
        except Exception:
            return LOCALHOST

    # ----------------------------------------------------------------------- #
    # Public API                                                               #
    # ----------------------------------------------------------------------- #

    def register_room(self, room: RoomInfo) -> None:
        """
        Add or update a room in the registry.

        Args:
            room: RoomInfo to register. If a room with the same ID exists,
                  it will be updated with the new information.
        """
        self._ensure_initialized()

        with self._lock:
            existing = self._rooms.get(room.id)
            if existing is not None:
                logger.debug(
                    "Updating room: %s (status=%s -> %s)",
                    room.name,
                    existing.status,
                    room.status,
                )
            else:
                logger.info(
                    "Registering new room: %s (device_id=%s, ip=%s)",
                    room.name,
                    room.device_id[:8] if room.device_id else "unknown",
                    room.ip_address,
                )

            updated_room = room
            if existing is not None:
                updated_room = replace(
                    room,
                    volume=existing.volume,
                    muted=existing.muted,
                )

            self._rooms[room.id] = updated_room
            if not updated_room.is_local:
                self._persist_remote_rooms_locked()

    def register_remote_room(self, room_id: str, room_name: str) -> RoomInfo:
        """Register a user-owned remote room with a stable placeholder device id."""
        room = RoomInfo(
            id=room_id,
            name=room_name,
            device_id=room_id,
            is_local=False,
            status="online",
            last_seen=time.time(),
        )
        self.register_room(room)
        return room

    def set_room_volume(self, room_id: str, volume: int) -> RoomInfo | None:
        """Persist a room's output volume, returning the updated room if found."""
        self._ensure_initialized()
        clamped = _clamp_volume(volume)

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot set room volume: %s not found", room_id)
                return None

            updated_room = replace(room, volume=clamped, last_seen=time.time())
            self._rooms[room_id] = updated_room
            if not updated_room.is_local:
                self._persist_remote_rooms_locked()
            logger.info("Set room %s volume to %d", room_id, clamped)
            return updated_room

    def set_room_mute(self, room_id: str, muted: bool) -> RoomInfo | None:
        """Persist a room's muted state, returning the updated room if found."""
        self._ensure_initialized()

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot set room mute: %s not found", room_id)
                return None

            updated_room = replace(room, muted=bool(muted), last_seen=time.time())
            self._rooms[room_id] = updated_room
            if not updated_room.is_local:
                self._persist_remote_rooms_locked()
            logger.info("Set room %s muted=%s", room_id, bool(muted))
            return updated_room

    def rename_room(self, room_id: str, new_name: str) -> bool:
        """Rename a registered non-local room."""
        self._ensure_initialized()

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot rename room: %s not found", room_id)
                return False
            if room.is_local:
                logger.warning("Cannot rename local room: %s", room_id)
                return False
            self._rooms[room_id] = replace(room, name=new_name, last_seen=time.time())
            self._persist_remote_rooms_locked()
            logger.info("Renamed room %s to %s", room_id, new_name)
            return True

    def unregister_room(self, room_id: str) -> bool:
        """
        Remove a room from the registry.

        Args:
            room_id: ID of the room to remove

        Returns:
            True if the room was removed, False if it wasn't found
        """
        self._ensure_initialized()

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot unregister room: %s not found", room_id)
                return False

            # Don't allow unregistering the local room
            if room.is_local:
                logger.warning("Cannot unregister local room: %s", room_id)
                return False

            del self._rooms[room_id]
            self._persist_remote_rooms_locked()
            logger.info("Unregistered room: %s", room.name)
            return True

    def get_room(self, room_id: str) -> RoomInfo | None:
        """
        Get a room by its ID.

        Args:
            room_id: The unique room identifier

        Returns:
            RoomInfo if found, None otherwise
        """
        self._ensure_initialized()

        with self._lock:
            return self._rooms.get(room_id)

    def list_rooms(self) -> list[RoomInfo]:
        """
        List all registered rooms.

        Returns:
            List of all RoomInfo objects, including the local device
        """
        self._ensure_initialized()

        with self._lock:
            return list(self._rooms.values())

    def get_local_room(self) -> RoomInfo:
        """
        Get the local device as a room.

        This always returns the local device's room info. The local room
        is automatically registered on startup.

        Returns:
            RoomInfo for the local device
        """
        self._ensure_initialized()

        with self._lock:
            if self._local_room_id is None:
                # This shouldn't happen after initialization
                raise RuntimeError("Local room not registered")

            room = self._rooms.get(self._local_room_id)
            if room is None:
                raise RuntimeError("Local room was unregistered unexpectedly")

            # Update last_seen timestamp for local room
            updated_room = replace(room, last_seen=time.time())
            self._rooms[self._local_room_id] = updated_room
            return updated_room

    def mark_offline(self, room_id: str) -> bool:
        """
        Mark a room as offline.

        Args:
            room_id: ID of the room to mark offline

        Returns:
            True if the room was found and marked offline, False otherwise
        """
        self._ensure_initialized()

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot mark offline: room %s not found", room_id)
                return False

            if room.status == "offline":
                return True  # Already offline

            updated_room = replace(
                room,
                status="offline",
                last_seen=time.time(),
            )
            self._rooms[room_id] = updated_room
            if not updated_room.is_local:
                self._persist_remote_rooms_locked()
            logger.info("Marked room offline: %s", room.name)
            return True

    def mark_online(self, room_id: str) -> bool:
        """
        Mark a room as online.

        Args:
            room_id: ID of the room to mark online

        Returns:
            True if the room was found and marked online, False otherwise
        """
        self._ensure_initialized()

        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                logger.debug("Cannot mark online: room %s not found", room_id)
                return False

            if room.status == "online":
                return True  # Already online

            updated_room = replace(
                room,
                status="online",
                last_seen=time.time(),
            )
            self._rooms[room_id] = updated_room
            if not updated_room.is_local:
                self._persist_remote_rooms_locked()
            logger.info("Marked room online: %s", room.name)
            return True

    def cleanup_stale(self, max_age_seconds: float = 300.0) -> int:
        """
        Remove rooms that haven't been seen recently.

        This helps clean up rooms that have gone offline without properly
        unregistering (e.g., network disconnection, crash).

        Args:
            max_age_seconds: Maximum age in seconds before a room is considered
                             stale. Default is 5 minutes.

        Returns:
            Number of rooms removed
        """
        self._ensure_initialized()

        now = time.time()
        stale_threshold = now - max_age_seconds
        removed_count = 0

        with self._lock:
            stale_room_ids = [
                room_id
                for room_id, room in self._rooms.items()
                if not room.is_local and room.last_seen < stale_threshold
            ]

            for room_id in stale_room_ids:
                room = self._rooms.pop(room_id)
                logger.info(
                    "Removed stale room: %s (last seen %.0fs ago)",
                    room.name,
                    now - room.last_seen,
                )
                removed_count += 1

            if removed_count > 0:
                self._persist_remote_rooms_locked()

        if removed_count > 0:
            logger.info("Cleaned up %d stale room(s)", removed_count)

        return removed_count

    def get_online_rooms(self) -> list[RoomInfo]:
        """
        Get all rooms that are currently online.

        Returns:
            List of online RoomInfo objects
        """
        self._ensure_initialized()

        with self._lock:
            return [room for room in self._rooms.values() if room.status == "online"]

    def get_remote_rooms(self) -> list[RoomInfo]:
        """
        Get all remote (non-local) rooms.

        Returns:
            List of remote RoomInfo objects
        """
        self._ensure_initialized()

        with self._lock:
            return [room for room in self._rooms.values() if not room.is_local]


# --------------------------------------------------------------------------- #
# Singleton Accessor                                                           #
# --------------------------------------------------------------------------- #

_global_registry: RoomRegistry | None = None
_registries_by_user: dict[str, RoomRegistry] = {}
_global_lock = threading.Lock()


def _registry_for_owner(owner: str) -> RoomRegistry:
    """Return (creating once) the registry backing *owner*."""
    with _global_lock:
        registry = _registries_by_user.get(owner)
        if registry is None:
            storage_path = _storage_path_for_owner(owner)
            if owner == DESKTOP_INSTALL_OWNER:
                _adopt_orphaned_desktop_rooms(storage_path)
            registry = RoomRegistry(storage_path=storage_path)
            _registries_by_user[owner] = registry
        return registry


def get_room_registry(user_id: str | None = None) -> RoomRegistry:
    """
    Get a room registry instance.

    User-owned API surfaces must pass ``user_id``. The principal is mapped to a
    registry owner by :func:`resolve_room_registry_owner` -- the caller's own
    account on the multi-tenant cloud surface, and the single desktop install on
    the desktop -- so every writer and every reader of a given install's paired
    rooms addresses one and the same store (#4432).

    A userless call on the desktop resolves to that same install registry:
    the install has exactly one owner, so there is nothing to guess, and the
    old process-local fallback was a third store nobody else read.  It is why
    Viola's agent answered "Kitchen isn't paired yet" while the rooms API on
    the same running instance listed Kitchen -- the agent's music route reached
    the registry without a principal and got the legacy 3-room global file
    instead of the install's 13 (#4432, observed live 2026-08-01T17:02:24).
    Cloud keeps the legacy process-local registry for that userless case,
    because there the caller genuinely has no owner to resolve and must not be
    handed some other tenant's rooms.

    Returns:
        The requested RoomRegistry instance
    """
    global _global_registry

    if user_id is not None:
        return _registry_for_owner(resolve_room_registry_owner(user_id))

    if not _is_cloud_surface():
        return _registry_for_owner(DESKTOP_INSTALL_OWNER)

    if _global_registry is not None:
        return _global_registry

    with _global_lock:
        if _global_registry is None:
            _global_registry = RoomRegistry()
        return _global_registry


# --------------------------------------------------------------------------- #
# Exports                                                                      #
# --------------------------------------------------------------------------- #

__all__ = [
    "DESKTOP_INSTALL_OWNER",
    "RoomInfo",
    "RoomRegistry",
    "get_room_registry",
    "resolve_room_registry_owner",
]
