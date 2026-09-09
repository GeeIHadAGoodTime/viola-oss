"""
Room Registry - Multi-room management for NOVVIOLA.

AI Instructions
===============
RoomRegistry manages room discovery and registration for multi-room sync.
All room operations go through StateHub - this is a convenience wrapper.

Usage:
    >>> from core.room_registry import RoomRegistry, get_room_registry
    >>>
    >>> registry = get_room_registry()
    >>> registry.register_local_room("Living Room")
    >>> registry.register_remote_room("room-abc", "Kitchen")
    >>> registry.set_active_room("room-abc")
    >>> rooms = registry.get_rooms()

Multi-Room Architecture:
    - Local room: The device running this NOVVIOLA instance
    - Remote rooms: Other NOVVIOLA instances discovered on the network
    - Each room has its own PlayerState
    - Commands can be sent to any room
    - Active room determines which state is displayed in UI

Related Modules:
    - core/state_hub.py: State mutations via commands
    - core/state_selectors.py: Reading room state
    - services/room_sync.py: Network synchronization
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from core.logging_config import get_logger
from core.state_hub import (
    AddRoom,
    RemoveRoom,
    RenameRoom,
    SetActiveRoom,
    SyncRoomState,
    UpdatePlayerState,
    get_state_hub,
)
from core.unified_state import AppState, RoomState

if TYPE_CHECKING:
    from models.player import PlayerState

logger = get_logger(__name__)


class RoomRegistry:
    """
    Manages room discovery and registration for multi-room support.

    Architecture:
    - Local room is auto-registered on startup
    - Remote rooms discovered via mDNS/network or manual registration
    - Each room maintains its own PlayerState
    - State updates flow through StateHub

    Thread Safety:
    - All operations are thread-safe (use StateHub)
    - Callbacks invoked from StateHub worker thread
    """

    def __init__(self, *, user_id: str | None = None) -> None:
        self._hub = get_state_hub(user_id=user_id)
        self.user_id = self._hub.user_id
        self._discovery_callbacks: list[Callable[[RoomState], None]] = []
        self._removal_callbacks: list[Callable[[str], None]] = []
        self._lock = threading.Lock()
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """Start monitoring room changes."""
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._hub.subscribe(self._on_state_changed)
        logger.info("RoomRegistry started")

    def stop(self) -> None:
        """Stop monitoring room changes."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
            logger.info("RoomRegistry stopped")

    # =========================================================================
    # Room Registration
    # =========================================================================

    def register_local_room(self, room_name: str = "This Device") -> None:
        """
        Register the local room (this device).

        Should be called once during application startup.

        Args:
            room_name: Display name for this device
        """
        self._hub.dispatch(
            AddRoom(
                room_id="local",
                room_name=room_name,
                is_primary=True,
            )
        )
        logger.info("Local room registered", room_name=room_name)

    def register_remote_room(
        self,
        room_id: str,
        room_name: str,
        initial_state: PlayerState | None = None,
    ) -> None:
        """
        Register a discovered remote room.

        Args:
            room_id: Unique identifier for the room
            room_name: Display name for the room
            initial_state: Optional initial player state
        """
        if room_id == "local":
            logger.warning("Cannot register remote room with ID 'local'")
            return

        self._hub.dispatch(
            AddRoom(
                room_id=room_id,
                room_name=room_name,
                is_primary=False,
            )
        )
        logger.info("Remote room registered", room_id=room_id, room_name=room_name)

        # If initial state provided, sync it
        if initial_state is not None:
            self.sync_room_state(room_id, initial_state)

    def unregister_room(self, room_id: str) -> bool:
        """
        Unregister a room.

        Cannot unregister the local/primary room.

        Args:
            room_id: Room to unregister

        Returns:
            True if room was unregistered, False if not found or is primary
        """
        if room_id == "local":
            logger.warning("Cannot unregister local room")
            return False

        room = self.get_room(room_id)
        if room is None:
            logger.warning("Room not found for unregistration", room_id=room_id)
            return False

        if room.is_primary:
            logger.warning("Cannot unregister primary room", room_id=room_id)
            return False

        self._hub.dispatch(RemoveRoom(room_id=room_id))
        logger.info("Room unregistered", room_id=room_id)
        return True

    def rename_room(self, room_id: str, new_name: str) -> bool:
        """
        Rename an existing room.

        Args:
            room_id: Room to rename
            new_name: New display name

        Returns:
            True if room was renamed, False if not found
        """
        room = self.get_room(room_id)
        if room is None:
            logger.warning("Room not found for rename", room_id=room_id)
            return False

        self._hub.dispatch(RenameRoom(room_id=room_id, new_name=new_name))
        logger.info("Room renamed", room_id=room_id, new_name=new_name)
        return True

    # =========================================================================
    # Room Selection
    # =========================================================================

    def set_active_room(self, room_id: str) -> bool:
        """
        Set the active room.

        The active room's state is displayed in the UI.

        Args:
            room_id: Room to make active

        Returns:
            True if room was activated, False if not found
        """
        room = self.get_room(room_id)
        if room is None:
            logger.warning("Room not found for activation", room_id=room_id)
            return False

        self._hub.dispatch(SetActiveRoom(room_id=room_id))
        logger.info("Active room changed", room_id=room_id)
        return True

    def get_active_room_id(self) -> str:
        """Get the active room ID."""
        return self._hub.get_state().active_room_id

    def get_active_room(self) -> RoomState | None:
        """Get the active room state."""
        return self._hub.get_state().active_room

    # =========================================================================
    # Room State Management
    # =========================================================================

    def sync_room_state(
        self,
        room_id: str,
        player_state: PlayerState,
        sync_time: float | None = None,
    ) -> None:
        """
        Sync state from a remote room.

        Used when receiving state updates over the network.

        Args:
            room_id: Room to update
            player_state: New player state
            sync_time: Optional sync timestamp
        """
        if sync_time is None:
            sync_time = time.time()

        self._hub.dispatch(
            SyncRoomState(
                room_id=room_id,
                player_state=player_state,
                sync_time=sync_time,
            )
        )

    def update_local_state(self, player_state: PlayerState) -> None:
        """
        Update the local room's player state.

        Convenience method for updating local room.

        Args:
            player_state: New player state
        """
        self._hub.dispatch(
            UpdatePlayerState(
                room_id="local",
                player_state=player_state,
            )
        )

    # =========================================================================
    # Room Queries
    # =========================================================================

    def get_rooms(self) -> list[RoomState]:
        """Get all registered rooms."""
        return list(self._hub.get_state().rooms.values())

    def get_room(self, room_id: str) -> RoomState | None:
        """Get a specific room by ID."""
        return self._hub.get_state().rooms.get(room_id)

    def get_local_room(self) -> RoomState | None:
        """Get the local (primary) room."""
        return self._hub.get_state().local_room

    def get_remote_rooms(self) -> list[RoomState]:
        """Get all non-primary (remote) rooms."""
        return [r for r in self._hub.get_state().rooms.values() if not r.is_primary]

    def get_room_count(self) -> int:
        """Get number of registered rooms."""
        return len(self._hub.get_state().rooms)

    def has_room(self, room_id: str) -> bool:
        """Check if a room exists."""
        return self.get_room(room_id) is not None

    def get_room_names(self) -> dict[str, str]:
        """Get mapping of room_id -> room_name."""
        return {r.room_id: r.room_name for r in self.get_rooms()}

    # =========================================================================
    # Room Discovery Callbacks
    # =========================================================================

    def on_room_discovered(self, callback: Callable[[RoomState], None]) -> Callable[[], None]:
        """
        Register callback for when a new room is discovered.

        Args:
            callback: Function called with the new RoomState

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._discovery_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._discovery_callbacks:
                    self._discovery_callbacks.remove(callback)

        return unsubscribe

    def on_room_removed(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """
        Register callback for when a room is removed.

        Args:
            callback: Function called with the removed room_id

        Returns:
            Unsubscribe function
        """
        with self._lock:
            self._removal_callbacks.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._removal_callbacks:
                    self._removal_callbacks.remove(callback)

        return unsubscribe

    def _on_state_changed(self, old: AppState, new: AppState) -> None:
        """Handle state changes to detect room additions/removals."""
        old_room_ids = set(old.rooms.keys())
        new_room_ids = set(new.rooms.keys())

        # Detect new rooms
        added = new_room_ids - old_room_ids
        for room_id in added:
            room = new.rooms[room_id]
            with self._lock:
                discovery_cbs = list(self._discovery_callbacks)
            for cb in discovery_cbs:
                try:
                    cb(room)
                except Exception as e:
                    logger.error("Room discovery callback error: %s", e)

        # Detect removed rooms
        removed = old_room_ids - new_room_ids
        for room_id in removed:
            with self._lock:
                removal_cbs = list(self._removal_callbacks)
            for removal_cb in removal_cbs:
                try:
                    removal_cb(room_id)
                except Exception as e:
                    logger.error("Room removal callback error: %s", e)


# =============================================================================
# User-Partitioned Registry
# =============================================================================

_REGISTRIES_BY_USER: dict[str, RoomRegistry] = {}
_registry_lock = threading.Lock()


def get_room_registry(*, user_id: str | None = None) -> RoomRegistry:
    """
    Get or create the current user's room registry.

    Returns:
        The current user's RoomRegistry instance
    """
    hub = get_state_hub(user_id=user_id)
    with _registry_lock:
        registry = _REGISTRIES_BY_USER.get(hub.user_id)
        if registry is None:
            registry = RoomRegistry(user_id=hub.user_id)
            registry.start()
            _REGISTRIES_BY_USER[hub.user_id] = registry
        return registry


def reset_room_registry(*, user_id: str | None = None) -> None:
    """
    Reset one user's room registry, or all registries when no user_id is supplied.
    """
    registries: list[RoomRegistry]
    with _registry_lock:
        if user_id is None:
            registries = list(_REGISTRIES_BY_USER.values())
            _REGISTRIES_BY_USER.clear()
        else:
            hub = get_state_hub(user_id=user_id)
            registry = _REGISTRIES_BY_USER.pop(hub.user_id, None)
            registries = [registry] if registry is not None else []
    for registry in registries:
        registry.stop()


def init_room_registry(
    local_room_name: str = "This Device",
    *,
    user_id: str | None = None,
) -> RoomRegistry:
    """
    Initialize the room registry with local room.

    Call this during application startup.

    Args:
        local_room_name: Display name for this device

    Returns:
        The initialized RoomRegistry
    """
    registry = get_room_registry(user_id=user_id)
    registry.register_local_room(local_room_name)
    return registry


__all__ = [
    "RoomRegistry",
    "get_room_registry",
    "init_room_registry",
    "reset_room_registry",
]
