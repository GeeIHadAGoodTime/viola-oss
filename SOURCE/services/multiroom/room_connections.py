"""
Room Connection Manager for Multi-Room Playback.

Maintains a singleton registry that maps room IDs to RemoteRoomClient instances.
This allows any part of the application to look up the HTTP client for a connected
remote room and issue playback commands.

Thread Safety:
    All public methods are protected by a threading.Lock and are safe to call
    from any thread or async task.

Usage:
    >>> from services.multiroom.room_connections import get_room_connection_manager
    >>>
    >>> mgr = get_room_connection_manager()
    >>> mgr.register_connection("living-room", "192.168.1.42", 8756)
    >>> client = mgr.get_client("living-room")
    >>> if client:
    ...     await client.play("Bohemian Rhapsody")
"""

from __future__ import annotations

import threading

from core.logging_config import get_logger
from services.multiroom.remote_client import RemoteRoomClient

logger = get_logger(__name__)


class RoomConnectionManager:
    """
    Registry mapping room IDs to RemoteRoomClient instances.

    Provides thread-safe registration, lookup, and removal of remote room
    connections. Intended to be used as a singleton via get_room_connection_manager().
    """

    def __init__(self) -> None:
        self._connections: dict[str, RemoteRoomClient] = {}
        self._lock = threading.Lock()

    def register_connection(self, room_id: str, host: str, port: int) -> None:
        """
        Create and store a RemoteRoomClient for the given room.

        If a connection already exists for room_id it will be replaced.

        Args:
            room_id: Unique identifier for the remote room.
            host: IP address or hostname of the remote Viola instance.
            port: API port of the remote Viola instance.
        """
        client = RemoteRoomClient(host=host, port=port)
        with self._lock:
            self._connections[room_id] = client
        logger.info(
            "Registered connection for room %s at %s:%d",
            room_id,
            host,
            port,
        )

    def get_client(self, room_id: str) -> RemoteRoomClient | None:
        """
        Look up the RemoteRoomClient for a room.

        Args:
            room_id: The room ID to look up.

        Returns:
            The RemoteRoomClient if registered, None otherwise.
        """
        with self._lock:
            return self._connections.get(room_id)

    def remove_connection(self, room_id: str) -> bool:
        """
        Remove a room connection from the registry.

        Args:
            room_id: The room ID to remove.

        Returns:
            True if the connection was found and removed, False otherwise.
        """
        with self._lock:
            removed = self._connections.pop(room_id, None)
        if removed is not None:
            logger.info("Removed connection for room %s", room_id)
            return True
        logger.debug("No connection found for room %s to remove", room_id)
        return False

    def get_all_remote_rooms(self) -> dict[str, RemoteRoomClient]:
        """
        Return a snapshot (shallow copy) of all registered connections.

        Returns:
            Dict mapping room_id to RemoteRoomClient.
        """
        with self._lock:
            return dict(self._connections)

    def has_connection(self, room_id: str) -> bool:
        """
        Check whether a connection exists for the given room.

        Args:
            room_id: The room ID to check.

        Returns:
            True if a connection is registered, False otherwise.
        """
        with self._lock:
            return room_id in self._connections


# =============================================================================
# Module-level singleton
# =============================================================================

_connection_manager: RoomConnectionManager | None = None
_manager_lock = threading.Lock()


def get_room_connection_manager() -> RoomConnectionManager:
    """
    Get or create the global RoomConnectionManager singleton.

    Returns:
        The shared RoomConnectionManager instance.
    """
    global _connection_manager

    with _manager_lock:
        if _connection_manager is None:
            _connection_manager = RoomConnectionManager()
        return _connection_manager


def reset_room_connection_manager() -> None:
    """
    Reset the global RoomConnectionManager singleton.

    Intended for use in tests to ensure a clean state between test cases.
    """
    global _connection_manager

    with _manager_lock:
        _connection_manager = None


__all__ = [
    "RoomConnectionManager",
    "get_room_connection_manager",
    "reset_room_connection_manager",
]
