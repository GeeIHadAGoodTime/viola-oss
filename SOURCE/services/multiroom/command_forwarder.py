"""
Multi-Room Command Forwarder.

Forwards transport commands (play, pause, resume, stop, skip, next, previous,
volume) to all remote rooms that share a group with the originating room.

Uses RoomGroupManager to discover group membership and RoomConnectionManager
to obtain HTTP clients for each remote room. Both dependencies are resolved
lazily so that the forwarder degrades gracefully when multiroom infrastructure
is not fully available.

Usage:
    >>> from services.multiroom.command_forwarder import get_command_forwarder
    >>> forwarder = get_command_forwarder()
    >>> await forwarder.forward_to_group("pause", "local")
    >>> await forwarder.forward_to_group("play", "local", query="Bohemian Rhapsody", source="spotify")
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from services.multiroom.room_connections import get_room_connection_manager

if TYPE_CHECKING:
    from services.multiroom.remote_client import RemoteRoomClient
    from services.multiroom.room_groups.manager import RoomGroupManager

log = get_logger(__name__)

# Hard timeout for forwarding a single command to a remote room.
# The RemoteRoomClient already has httpx connect=3s / read=5s timeouts,
# but this asyncio-level cap guarantees we never block the event loop
# even if DNS resolution or socket-level operations stall.
_FORWARD_TIMEOUT_SECONDS = 10.0

_COMMAND_MAP: dict[str, str] = {
    "play": "play",
    "pause": "pause",
    "resume": "resume",
    "stop": "stop",
    "skip": "skip",
    "next": "next_track",
    "previous": "previous",
    "volume": "set_volume",
    "seek": "seek",
}


class MultiRoomCommandForwarder:
    """
    Forwards playback commands to all remote rooms in the same group.

    Uses singletons internally:
    - RoomConnectionManager for HTTP clients to remote rooms
    - RoomGroupManager (lazily loaded) for group membership lookup

    If RoomGroupManager is unavailable (import or instantiation fails),
    forwarding silently does nothing.
    """

    def __init__(self) -> None:
        self._group_manager: RoomGroupManager | None = None
        self._group_manager_resolved = False
        self._event_hub: Any = None
        self._event_hub_resolved = False

    def _resolve_group_manager(self) -> RoomGroupManager | None:
        """
        Lazily resolve the RoomGroupManager singleton.

        Uses the same singleton as the room_groups API route so that
        groups created via the REST API are immediately visible to the
        forwarder (shared in-memory cache).

        Returns None if the import chain is unavailable (multiroom groups
        not configured). Caches the result so we only attempt once.
        """
        if self._group_manager_resolved:
            return self._group_manager

        try:
            from ui.api.routes.room_groups import get_room_group_manager

            self._group_manager = get_room_group_manager()
            log.debug("RoomGroupManager resolved (shared singleton)")
        except ImportError as exc:
            log.debug("RoomGroupManager not available (import): %s", exc)
            self._group_manager = None
        except Exception as exc:
            log.debug("RoomGroupManager not available (init): %s", exc)
            self._group_manager = None

        self._group_manager_resolved = True
        return self._group_manager

    def _resolve_event_hub(self) -> Any:
        """
        Lazily resolve the EventHub singleton.

        The EventHub is stored on the FastAPI app state (app.state.event_hub)
        during server initialization. The preferred way to provide the hub
        is via ``set_event_hub()`` during app startup. If that hasn't
        happened yet, this method returns None.

        Returns None if the hub is not yet available.
        """
        if self._event_hub_resolved:
            return self._event_hub

        # The EventHub lives on app.state.event_hub, but there is no global
        # get_app() accessor. The hub must be injected via set_event_hub().
        log.debug("EventHub not yet injected; call set_event_hub() during startup")
        self._event_hub = None
        self._event_hub_resolved = True
        return self._event_hub

    def set_event_hub(self, hub: Any) -> None:
        """
        Inject the EventHub reference from outside.

        Called during app startup to give the forwarder access to the
        WebSocket broadcast infrastructure without requiring a global
        app accessor.
        """
        self._event_hub = hub
        self._event_hub_resolved = True
        log.debug("EventHub injected into command forwarder")

    def _find_group_peers(self, room_id: str, *, user_id: str | None) -> list[str]:
        """
        Find all other room IDs that share a group with the given room.

        Args:
            room_id: The originating room ID.

        Returns:
            List of peer room IDs (excluding the originating room).
            Empty list if room is not in any group or manager is unavailable.
        """
        mgr = self._resolve_group_manager()
        if mgr is None:
            return []
        if not user_id:
            log.warning("Refusing userless room-group lookup for room=%s", room_id)
            return []

        peers: list[str] = []
        try:
            for group in mgr.list_groups(user_id=user_id):
                if room_id in group.room_ids:
                    for rid in group.room_ids:
                        if rid != room_id and rid not in peers:
                            peers.append(rid)
        except Exception as exc:
            log.warning("Failed to query room groups: %s", exc)

        # Filter out dead spokes — skip peers not in the online registry
        try:
            from services.multiroom.room_registry import get_room_registry

            registry = get_room_registry(user_id=user_id)
            online_ids = {r.id for r in registry.get_online_rooms()}
            dead_peers = [p for p in peers if p not in online_ids]
            if dead_peers:
                log.warning(
                    "Skipping %d dead spoke(s) in group broadcast: %s",
                    len(dead_peers),
                    dead_peers,
                )
            peers = [p for p in peers if p in online_ids]
        except Exception as exc:
            log.debug("Dead spoke filter unavailable: %s", exc)

        return peers

    async def _dispatch_to_room(
        self,
        room_id: str,
        client: RemoteRoomClient,
        command: str,
        **kwargs: Any,
    ) -> None:
        """
        Dispatch a single command to a single remote room.

        Logs success or failure. Never raises.
        """
        method_name = _COMMAND_MAP.get(command)
        if method_name is None:
            log.warning("Unknown multiroom command: %s", command)
            return

        method = getattr(client, method_name, None)
        if method is None:
            log.warning(
                "RemoteRoomClient for room %s missing method %s",
                room_id,
                method_name,
            )
            return

        try:
            if command == "play":
                await method(kwargs.get("query", ""), kwargs.get("source"))
            elif command == "volume":
                await method(kwargs.get("level", 80))
            elif command == "seek":
                await method(kwargs.get("position", 0))
            else:
                await method()
            log.debug("Forwarded '%s' to room %s", command, room_id)
        except Exception as exc:
            log.warning(
                "Failed to forward '%s' to room %s: %s",
                command,
                room_id,
                exc,
            )

    async def forward_to_room(
        self,
        command: str,
        target_room_id: str,
        **kwargs: Any,
    ) -> bool:
        """
        Forward a transport command to a single specific remote room.

        Args:
            command: Command name (play, pause, resume, stop, skip, next,
                     previous, volume).
            target_room_id: The room ID to send the command to.
            **kwargs: Extra arguments for the command. ``play`` requires
                      ``query`` and optionally ``source``; ``volume`` requires
                      ``level``.

        Returns:
            True if the command was dispatched successfully, False otherwise.
        """
        conn_mgr = get_room_connection_manager()
        client = conn_mgr.get_client(target_room_id)
        if client is None:
            log.warning(
                "No connection for room %s -- cannot forward '%s'",
                target_room_id,
                command,
            )
            return False

        try:
            await asyncio.wait_for(
                self._dispatch_to_room(target_room_id, client, command, **kwargs),
                timeout=_FORWARD_TIMEOUT_SECONDS,
            )
            log.info("Forwarded '%s' to room %s", command, target_room_id)
            return True
        except Exception as exc:
            log.warning(
                "Failed to forward '%s' to room %s: %s",
                command,
                target_room_id,
                exc,
            )
            return False

    async def forward_to_browser_spokes(
        self,
        command: str,
        room_id: str,
        *,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Forward a playback command to a tenant's browser-based spokes.

        Multi-tenant: ``user_id`` is REQUIRED.  Without it we cannot tell
        which tenant's spokes to target, and falling back to "every
        subscribed room" lets one user's playback command push to
        another tenant's room.  When unset the helper exits quietly.

        Broadcasts a ``room_playback_command`` to browser clients
        subscribed to this user's rooms.  If group membership exists,
        only group peers receive commands; otherwise the broadcast
        targets every room the user is currently subscribed to
        (excluding the source room).

        For ``play`` commands, ``track_url``, ``position``, and ``play_at``
        are forwarded so spokes can load the same video at a
        coordinated timestamp.
        """
        if not user_id:
            log.warning(
                "MultiRoomCommandForwarder refused userless browser-spoke forward for '%s' room=%s",
                command,
                room_id,
            )
            return

        hub = self._resolve_event_hub()
        if hub is None:
            log.debug("EventHub not available for browser spoke forwarding")
            return

        # Determine target rooms: prefer group peers, fall back to the
        # owning user's subscribed rooms so spoke browsers work without
        # group setup but never bleed cross-tenant.
        peers = self._find_group_peers(room_id, user_id=user_id)
        if not peers:
            try:
                all_rooms = await hub.get_subscribed_room_ids(user_id=user_id)
                peers = [r for r in all_rooms if r != room_id]
            except Exception as exc:
                log.debug("Failed to enumerate subscribed rooms: %s", exc)
                return

        if not peers:
            return

        # Build spoke-specific args: spokes need track_url, not query
        spoke_args = dict(kwargs)
        if command == "play":
            # Remove query/source (hub-only), keep track_url/position/play_at
            spoke_args.pop("query", None)
            spoke_args.pop("source", None)

        payload: dict[str, Any] = {
            "command": command,
            "args": spoke_args,
        }

        try:
            sent_total = 0
            for peer_id in peers:
                count = await hub.broadcast_to_room(
                    peer_id,
                    "room_playback_command",
                    payload,
                    user_id=user_id,
                )
                sent_total += count

            if sent_total > 0:
                log.info(
                    "Forwarded '%s' to %d browser spoke(s) in %d room(s) for user=%s",
                    command,
                    sent_total,
                    len(peers),
                    user_id,
                )
        except Exception as exc:
            log.warning("Failed to forward '%s' to browser spokes: %s", command, exc)

    async def forward_to_group(
        self,
        command: str,
        room_id: str,
        *,
        user_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Forward a transport command to all other rooms in the same group.

        Also forwards to browser-based spoke clients via WebSocket,
        regardless of group membership — spoke browsers work out of the
        box without manual group configuration.

        Args:
            command: Command name (play, pause, resume, stop, skip, next,
                     previous, volume).
            room_id: The originating room ID. Other rooms in the same group
                     will receive the command.
            **kwargs: Extra arguments for the command. ``play`` requires
                      ``query`` and optionally ``source``; ``volume`` requires
                      ``level``.

        Failures on individual rooms do not affect others. If the originating
        room is not in any group, HTTP forwarding is skipped but browser
        spoke forwarding still runs.
        """
        # Evict dead spokes from groups (TTL-based lazy eviction)
        try:
            from services.multiroom.room_registry import get_room_registry

            mgr = self._resolve_group_manager()
            if mgr is not None:
                if not user_id:
                    raise ValueError("user_id is required for room-group eviction")
                evicted = mgr.evict_dead_members(
                    user_id=user_id,
                    registry=get_room_registry(user_id=user_id),
                )
                if evicted:
                    log.info("Evicted %d dead spoke(s) from groups before broadcast", evicted)
        except Exception as exc:
            log.debug("Dead spoke eviction unavailable: %s", exc)

        peers = self._find_group_peers(room_id, user_id=user_id)

        # Forward to remote Viola instances via HTTP (requires group membership)
        if peers:
            conn_mgr = get_room_connection_manager()
            tasks: list[asyncio.Task[None]] = []

            for peer_id in peers:
                client = conn_mgr.get_client(peer_id)
                if client is None:
                    log.debug(
                        "No connection for peer room %s -- skipping forward",
                        peer_id,
                    )
                    continue
                tasks.append(
                    asyncio.ensure_future(
                        asyncio.wait_for(
                            self._dispatch_to_room(peer_id, client, command, **kwargs),
                            timeout=_FORWARD_TIMEOUT_SECONDS,
                        )
                    )
                )

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        log.warning(
                            "Unexpected error forwarding to peer: %s",
                            result,
                        )
                log.info(
                    "Forwarded '%s' to %d peer room(s) in group",
                    command,
                    len(tasks),
                )

        # Always forward to browser spokes via WebSocket (no group required).
        # Pass user_id through so spoke broadcasts stay tenant-scoped.
        try:
            await self.forward_to_browser_spokes(
                command,
                room_id,
                user_id=user_id,
                **kwargs,
            )
        except Exception as ws_exc:
            log.debug("Browser spoke forwarding skipped: %s", ws_exc)


# =============================================================================
# Module-level singleton
# =============================================================================

_forwarder: MultiRoomCommandForwarder | None = None
_forwarder_lock = threading.Lock()


def get_command_forwarder() -> MultiRoomCommandForwarder:
    """
    Get or create the global MultiRoomCommandForwarder singleton.

    Returns:
        The shared MultiRoomCommandForwarder instance.
    """
    global _forwarder

    with _forwarder_lock:
        if _forwarder is None:
            _forwarder = MultiRoomCommandForwarder()
        return _forwarder


__all__ = [
    "MultiRoomCommandForwarder",
    "get_command_forwarder",
]
