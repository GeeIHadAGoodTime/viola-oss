from __future__ import annotations

import asyncio
import json as _json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from plugins.permissions import PermissionDecision

from core.logging_config import get_logger
from fastapi import WebSocket
from ui.core.security import reject_websocket

logger = get_logger(__name__)


# F-024: Claude SDK control subtypes. Handlers may be registered via
# ``register_control_handler``. The set documents every subtype we
# *know about*; unregistered subtypes still return a typed
# unsupported response instead of raising, so the SDK contract stays
# stable as more wiring lands.
CLAUDE_CONTROL_SUBTYPES: frozenset[str] = frozenset(
    {
        "initialize",
        "interrupt",
        "can_use_tool",
        "set_permission_mode",
        "set_model",
        "set_max_thinking_tokens",
        "mcp_status",
        "mcp_message",
        "mcp_set_servers",
        "mcp_reconnect",
        "mcp_toggle",
        "hook_callback",
        "rewind_files",
        "cancel_async_message",
        "seed_read_state",
        "reload_plugins",
        "stop_task",
        "apply_flag_settings",
        "get_settings",
        "get_context_usage",
        "elicitation",
    }
)

# Maximum time (seconds) to wait for a single ws.send_json / ws.send_bytes
# before treating the client as unresponsive and dropping it.
_WS_SEND_TIMEOUT: float = 5.0


async def _send_json_safe(ws: WebSocket, data: Any) -> None:
    """Send JSON via WebSocket with ``ensure_ascii=True``.

    Starlette's ``WebSocket.send_json`` uses ``ensure_ascii=False`` which
    emits raw UTF-8 for non-ASCII characters.  On Windows this can cause
    mojibake if any intermediary (logging, proxy) misinterprets the bytes
    as cp1252.  Using ``ensure_ascii=True`` ensures the JSON text is pure
    ASCII, making it immune to codepage misinterpretation.
    """
    text = _json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    await ws.send({"type": "websocket.send", "text": text})


# Maximum number of messages to buffer per-client before dropping the oldest.
# Prevents unbounded memory growth from slow or unresponsive WebSocket clients.
_WS_QUEUE_MAX_SIZE: int = 100
_GLOBAL_BROADCAST_SCOPE = "__global__"

# S7-02 multi-tenant: Event types that are intrinsically global (server
# lifecycle, health, build/version info).  Everything else must be
# user-scoped — sending a task-/call-/state-shaped event to "all clients"
# leaks one tenant's activity to every connected device.  Add new event
# types here only if they truly carry no user state.
_GLOBAL_EVENT_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "server_status",
        "server_shutdown",
        "server_restart",
        "ping",
        "pong",
        "build_info",
    }
)


def _room_scope(user_id: str, room_id: str | None) -> str | None:
    """Scope a raw room id under a user.

    S7-03 multi-tenant: mirrors ``backend.cloud_runtime._room_scope`` so
    local EventHub and cloud runtime share the same tenant key convention
    (``user_id:room``). Two users picking the same room name no longer
    share a multiroom broadcast bucket.
    """
    if room_id is None:
        return None
    normalized = room_id.strip()
    if not normalized:
        return None
    return "%s:%s" % (user_id, normalized)


# Module-level singleton for cross-module access (set by ui/server.py at startup)
_global_hub: EventHub | None = None


def _client_ip_from_websocket(ws: WebSocket) -> str:
    client_ip = "unknown"
    if ws.client is not None:
        client_ip = ws.client.host or "unknown"

    try:
        from auth.ip_utils import direct_ip_is_trusted_proxy, validated_header_ip

        if direct_ip_is_trusted_proxy(client_ip):
            viola_client_ip = validated_header_ip(ws.headers.get("x-viola-client-ip", ""))
            if viola_client_ip:
                return viola_client_ip
            fly_client_ip = validated_header_ip(ws.headers.get("fly-client-ip", ""))
            if fly_client_ip:
                return fly_client_ip
            forwarded_for = ws.headers.get("x-forwarded-for", "")
            if forwarded_for:
                forwarded_ip = validated_header_ip(forwarded_for.split(",", 1)[0])
                if forwarded_ip:
                    return forwarded_ip
    except Exception:
        pass

    return client_ip


def set_event_hub(hub: EventHub) -> None:
    """Register the global EventHub singleton (called once at startup)."""
    global _global_hub
    _global_hub = hub


def get_event_hub() -> EventHub | None:
    """Return the global EventHub or None if not yet initialized."""
    return _global_hub


class EventHub:
    """Broadcast hub for server-originated WebSocket events."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._last_broadcast_time: dict[str, float] = {}
        self._last_broadcast_payload: dict[str, Any] = {}
        # Minimum interval between broadcasts to avoid UI flicker
        self._broadcast_throttle_ms: float = 250.0
        # Dedup window: suppress identical payloads within this window,
        # even when force=True.  Prevents the 5-7 duplicate broadcasts
        # that occur when route handlers, on_state_change callbacks, and
        # the periodic broadcaster all fire for the same state change.
        self._dedup_window_ms: float = 100.0
        self._last_sent_hash: dict[str, int] = {}
        self._last_sent_time: dict[str, float] = {}
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Command handlers registry
        self._command_handlers: dict[str, Callable[..., Awaitable[Any]]] = {}
        # Per-IP connection tracking for rate limiting
        self._connections_by_ip: dict[str, int] = {}
        self._client_ip: dict[WebSocket, str] = {}
        # Load max connections per IP from settings
        try:
            from config.settings import settings

            self._max_connections_per_ip: int = settings.max_ws_connections_per_ip
        except Exception:
            self._max_connections_per_ip = 10
        # Room/user/device subscription mappings for multi-room broadcast
        self._client_room: dict[WebSocket, str] = {}
        self._room_clients: dict[str, set[WebSocket]] = {}
        self._client_user: dict[WebSocket, str] = {}
        self._user_clients: dict[str, set[WebSocket]] = {}
        self._client_device: dict[WebSocket, str] = {}
        self._device_clients: dict[str, set[WebSocket]] = {}
        # Per-client bounded message queues and writer tasks (backpressure)
        self._client_queues: dict[WebSocket, asyncio.Queue[dict[str, Any] | None]] = {}
        self._client_writer_tasks: dict[WebSocket, asyncio.Task[None]] = {}
        # Last known per-user state snapshot — sent to reconnecting clients immediately
        self._last_state_by_user: dict[str, dict[str, Any]] = {}
        # Claude-compatible SDK/control bridge handlers. Legacy action
        # handlers remain available through _command_handlers.
        self._control_handlers: dict[str, Callable[..., Awaitable[dict[str, Any] | PermissionDecision | None]]] = {}
        # F-057: track in-flight control_request operations so
        # ``control_cancel_request`` can cancel a specific request and
        # so keep_alive can refresh per-WebSocket heartbeat freshness.
        self._inflight_controls: dict[str, asyncio.Task[Any]] = {}
        self._inflight_control_ws: dict[str, WebSocket] = {}
        self._client_last_seen: dict[WebSocket, float] = {}
        # SDK flag settings are process-local request overrides surfaced
        # through ``get_settings`` / ``apply_flag_settings``.
        self._sdk_flag_settings: dict[str, Any] = {}

    async def connect(
        self,
        ws: WebSocket,
        *,
        user_id: str | None = None,
        device_id: str | None = None,
        room_id: str | None = None,
    ) -> None:
        # Extract client IP for per-IP connection limiting
        client_ip = _client_ip_from_websocket(ws)

        # Check per-IP connection limit before accepting
        async with self._lock:
            current_count = self._connections_by_ip.get(client_ip, 0)
            if current_count >= self._max_connections_per_ip:
                logger.warning(
                    "Per-IP WebSocket limit reached, rejecting connection: ip=%s count=%d max=%d",
                    client_ip,
                    current_count,
                    self._max_connections_per_ip,
                )
                await reject_websocket(ws, code=1008, reason="Too many connections from this IP")
                return

        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._client_ip[ws] = client_ip
            self._connections_by_ip[client_ip] = self._connections_by_ip.get(client_ip, 0) + 1
            if user_id is not None:
                self._client_user[ws] = user_id
                self._user_clients.setdefault(user_id, set()).add(ws)
            if device_id is not None:
                self._client_device[ws] = device_id
                self._device_clients.setdefault(device_id, set()).add(ws)
            if room_id is not None:
                # Multi-tenant: room subscriptions are partitioned by
                # user_id so two tenants choosing the same room name do
                # not share a broadcast bucket.  Userless rooms are
                # refused — we cannot prove which tenant a frame belongs
                # to without an owner.
                if user_id is None:
                    logger.warning(
                        "EventHub.connect refused room=%s for unauthenticated socket",
                        room_id,
                    )
                else:
                    scoped = _room_scope(user_id, room_id)
                    if scoped is not None:
                        self._client_room[ws] = scoped
                        self._room_clients.setdefault(scoped, set()).add(ws)
            client_count = len(self._clients)
        logger.info("WebSocket client connected", client_count=client_count, client_ip=client_ip)
        # M2: Create per-client bounded queue and start writer task.
        # Use try/except so that a mid-connect failure cleans up the partially
        # populated dicts — preventing stale entries if connect() raises before
        # the route handler's finally block can call disconnect().
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_WS_QUEUE_MAX_SIZE)
        self._client_queues[ws] = queue
        try:
            writer_task = asyncio.create_task(self._run_client_writer(ws))
        except Exception:
            # Roll back queue entry so disconnect() doesn't see orphaned state.
            self._client_queues.pop(ws, None)
            raise
        self._client_writer_tasks[ws] = writer_task
        # Send cached state snapshot so reconnecting clients get current state immediately.
        cached_state = self._last_state_by_user.get(user_id) if user_id is not None else None
        if cached_state is not None:
            self._enqueue_to_client(ws, cached_state)
        # Deliver anything that broke before there was a UI to tell. A failed
        # audio device is found during bootstrap, long before the first client
        # connects, so broadcasting it at the moment of discovery would reach an
        # empty room and the user would never learn why Viola cannot hear or
        # speak. Draining here (not peeking) shows each notice once.
        try:
            from core.user_notice import drain_pending_notices

            for notice in drain_pending_notices():
                self._enqueue_to_client(ws, {"type": "error", "payload": notice})
        except (ImportError, RuntimeError, OSError, TypeError, ValueError, AttributeError, LookupError):
            logger.debug("Could not deliver pending user notices on connect", exc_info=True)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            # Decrement per-IP connection counter
            client_ip = self._client_ip.pop(ws, None)
            if client_ip is not None:
                count = self._connections_by_ip.get(client_ip, 1) - 1
                if count <= 0:
                    self._connections_by_ip.pop(client_ip, None)
                else:
                    self._connections_by_ip[client_ip] = count
            self._unmap_client(ws)
            client_count = len(self._clients)
        # Stop the per-client writer task via sentinel then cancellation
        queue = self._client_queues.pop(ws, None)
        if queue is not None:
            try:
                queue.put_nowait(None)  # Sentinel: signals writer to exit cleanly
            except asyncio.QueueFull:
                pass  # Writer will be cancelled below
        task = self._client_writer_tasks.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()
        logger.info("WebSocket client disconnected", client_count=client_count)

    def _unmap_client(self, ws: WebSocket) -> None:
        """Remove a client from all room/user/device mappings. Must hold _lock."""
        room_id = self._client_room.pop(ws, None)
        if room_id is not None:
            room_set = self._room_clients.get(room_id)
            if room_set is not None:
                room_set.discard(ws)
                if not room_set:
                    del self._room_clients[room_id]

        user_id = self._client_user.pop(ws, None)
        if user_id is not None:
            user_set = self._user_clients.get(user_id)
            if user_set is not None:
                user_set.discard(ws)
                if not user_set:
                    del self._user_clients[user_id]

        device_id = self._client_device.pop(ws, None)
        if device_id is not None:
            device_set = self._device_clients.get(device_id)
            if device_set is not None:
                device_set.discard(ws)
                if not device_set:
                    del self._device_clients[device_id]

    def _enqueue_to_client(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        """Enqueue a message for a specific client.

        If the queue is full (slow client), the oldest pending message is
        dropped to make room.  This bounds memory growth to
        ``_WS_QUEUE_MAX_SIZE`` messages per client regardless of send speed.
        """
        queue = self._client_queues.get(ws)
        if queue is None:
            return
        if queue.full():
            try:
                queue.get_nowait()  # Drop oldest entry
                queue.task_done()
                logger.warning(
                    "WebSocket queue full (max=%d), dropped oldest message for client %s",
                    _WS_QUEUE_MAX_SIZE,
                    id(ws),
                )
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Defensive — shouldn't happen after the drop above
            logger.warning(
                "WebSocket queue still full after drop, message lost for client %s",
                id(ws),
            )

    async def _run_client_writer(self, ws: WebSocket) -> None:
        """Per-client writer task: drain the message queue and send to WebSocket.

        Runs as a background asyncio task for each connected client.
        Returns (and is cancelled) when the client disconnects or a send fails.
        The routes.py ``finally`` block calls ``disconnect()`` to do cleanup.
        """
        queue = self._client_queues.get(ws)
        if queue is None:
            return
        try:
            while True:
                msg = await queue.get()
                queue.task_done()
                if msg is None:  # Sentinel — graceful stop from disconnect()
                    return
                try:
                    await asyncio.wait_for(_send_json_safe(ws, msg), timeout=_WS_SEND_TIMEOUT)
                except TimeoutError:
                    logger.warning(
                        "WebSocket send timed out after %ss, stopping writer for client %s",
                        _WS_SEND_TIMEOUT,
                        id(ws),
                    )
                    return
                except Exception as exc:
                    logger.debug(
                        "WebSocket writer send failed (client likely disconnected): %s",
                        type(exc).__name__,
                    )
                    return
        except asyncio.CancelledError:
            pass  # disconnect() cancelled this task — clean exit

    @staticmethod
    def _payload_hash(event_type: str, payload: Any) -> int:
        """Return a fast content hash for dedup comparison."""
        import json

        # Sort keys for deterministic hashing of equivalent dicts.
        try:
            raw = json.dumps({"t": event_type, "p": payload}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = repr((event_type, payload))
        return hash(raw)

    @staticmethod
    def _scope_key(user_id: str | None) -> str:
        """Return the throttle/dedup cache key for a broadcast scope."""
        return user_id if user_id is not None else _GLOBAL_BROADCAST_SCOPE

    async def broadcast_playback_state(
        self,
        event_type: str,
        payload: Any,
        *,
        user_id: str | None = None,
        force: bool = False,
    ) -> None:
        """Broadcast a playback state event, scoped to the specified user."""
        await self.broadcast(
            event_type,
            payload,
            user_id=user_id,
            force=force,
        )

    async def broadcast(
        self,
        event_type: str,
        payload: Any,
        *,
        user_id: str | None = None,
        force: bool = False,
        global_event: bool = False,
    ) -> None:
        """
        Broadcast an event to WebSocket clients with throttling.

        Multi-tenant: ``user_id`` is REQUIRED for every event that carries
        user-shaped state (task progress, call status, playback, agent
        result, ...).  An event sent with ``user_id=None`` is rejected
        unless either:

        * the event type is in ``_GLOBAL_EVENT_TYPE_ALLOWLIST`` (server
          lifecycle / health / build info), OR
        * the caller passes ``global_event=True`` to opt into a truly
          global broadcast.

        This fails CLOSED — a missed user propagation no longer becomes a
        broadcast to every connected tenant.

        Args:
            event_type: Type of event.
            payload: Event payload.
            user_id: Broadcast only to this user's clients when provided.
            force: Bypass the longer throttle window (but NOT dedup).
            global_event: Allow this broadcast to fan out to every
                connected client.  Reserved for ops/lifecycle events that
                are intentionally tenant-agnostic.
        """
        import time

        current_time = time.time() * 1000  # milliseconds
        scope_key = self._scope_key(user_id)

        if user_id is None:
            allowed_global = global_event or event_type in _GLOBAL_EVENT_TYPE_ALLOWLIST
            if not allowed_global:
                logger.warning(
                    "EventHub.broadcast refused userless broadcast for event %s "
                    "(set global_event=True for ops broadcasts or supply user_id)",
                    event_type,
                )
                return
            if event_type == "state":
                # Per-user state snapshots NEVER fan out globally — the
                # event is shaped around a single tenant.
                logger.error("EventHub.broadcast refused global 'state' broadcast")
                return

        # DIAGNOSTIC: Extract volume for downstream diagnostic logging
        volume_in_payload = payload.get("volume") if isinstance(payload, dict) else None

        # ── Content-hash dedup (applies to ALL broadcasts) ──────────
        content_hash = self._payload_hash(event_type, payload)
        last_sent_time = self._last_sent_time.get(scope_key, 0.0)
        last_sent_hash = self._last_sent_hash.get(scope_key)
        time_since_last_sent = current_time - last_sent_time
        if last_sent_hash == content_hash and time_since_last_sent < self._dedup_window_ms:
            logger.debug(
                "Broadcast deduped (identical payload within %dms) event=%s",
                int(self._dedup_window_ms),
                event_type,
            )
            return

        # ── Throttle for non-forced broadcasts ──────────────────────
        if not force:
            last_broadcast_time = self._last_broadcast_time.get(scope_key, 0.0)
            time_since_last = current_time - last_broadcast_time
            if time_since_last < self._broadcast_throttle_ms:
                if self._last_broadcast_payload.get(scope_key) == payload:
                    logger.debug(
                        "Broadcast throttled (duplicate payload within %dms)",
                        int(self._broadcast_throttle_ms),
                        event_type=event_type,
                    )
                    return

        self._last_broadcast_time[scope_key] = current_time
        self._last_broadcast_payload[scope_key] = payload
        self._last_sent_hash[scope_key] = content_hash
        self._last_sent_time[scope_key] = current_time

        # Copy clients inside lock, then release lock before awaiting sends
        # This prevents deadlock if send_json() blocks on slow network
        async with self._lock:
            clients_snapshot = (
                list(self._user_clients.get(user_id, set())) if user_id is not None else list(self._clients)
            )

        # DIAGNOSTIC: Log client count for volume broadcasts
        if volume_in_payload is not None:
            logger.debug(
                "HUB_BROADCAST_SENDING: volume=%s to %d clients",
                volume_in_payload,
                len(clients_snapshot),
            )

        _msg_payload = {"type": event_type, "payload": payload}
        # Track last state snapshot for reconnecting clients.  S7-02
        # multi-tenant: state is rejected when user_id is None earlier,
        # but defensively guard the dict write so we never key by None.
        if event_type == "state" and user_id is not None:
            self._last_state_by_user[user_id] = _msg_payload
        # Enqueue to each client's bounded queue; writer tasks handle actual sends
        enqueued_count = 0
        for ws in clients_snapshot:
            self._enqueue_to_client(ws, _msg_payload)
            enqueued_count += 1
            if event_type == "state":
                logger.debug(
                    "[QUEUE_TRACE] WS_ENQUEUE client=%s event=%s",
                    id(ws),
                    event_type,
                )

        # DIAGNOSTIC: Log enqueue count for volume broadcasts
        if volume_in_payload is not None:
            logger.debug(
                "HUB_BROADCAST_ENQUEUED: volume=%s enqueued_count=%d",
                volume_in_payload,
                enqueued_count,
            )

    async def broadcast_command(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        user_id: str | None = None,
        origin_message_id: str | None = None,
        global_event: bool = False,
    ) -> None:
        """
        Broadcast a command to a single tenant's WebSocket clients.

        Multi-tenant: a userless command fans out to every connected
        client.  We treat that as a fail-open broadcast and refuse it
        unless the caller opts in with ``global_event=True``.

        Args:
            command: Command name
            payload: Command payload
            user_id: Target user.  Required unless ``global_event=True``.
            origin_message_id: Optional message ID to prevent echo
            global_event: Allow a tenant-agnostic fan-out (ops/lifecycle).
        """
        if user_id is None and not global_event:
            logger.warning(
                "EventHub.broadcast_command refused userless command %s " "(supply user_id or set global_event=True)",
                command,
            )
            return
        message = {"type": "command", "command": command, "payload": payload}
        if origin_message_id:
            message["origin_message_id"] = origin_message_id

        async with self._lock:
            clients_snapshot = (
                list(self._user_clients.get(user_id, set())) if user_id is not None else list(self._clients)
            )

        # Enqueue to each client's bounded queue; writer tasks handle actual sends
        for ws in clients_snapshot:
            self._enqueue_to_client(ws, message)

    def register_command_handler(self, command: str, handler: Callable[..., Awaitable[Any]]) -> None:
        """
        Register a handler for a specific command.

        Args:
            command: Command name
            handler: Async handler function
        """
        self._command_handlers[command] = handler
        logger.debug("Registered command handler for: %s", command)

    def register_control_handler(
        self,
        subtype: str,
        handler: Callable[..., Awaitable[dict[str, Any] | PermissionDecision | None]],
    ) -> None:
        """Register a Claude SDK control_request subtype handler."""
        self._control_handlers[subtype] = handler
        logger.debug("Registered control handler for: %s", subtype)

    async def dispatch_command(self, message: str, ws: WebSocket) -> bool:
        """
        Dispatch an incoming WebSocket message to the appropriate handler.

        Args:
            message: JSON message string with 'action' and optional 'payload'
            ws: WebSocket that sent the message

        Returns:
            True if message was handled, False otherwise
        """
        import json

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in WebSocket message (chars=%d)", len(message))
            return False

        message_type = data.get("type")
        if message_type == "keep_alive":
            # F-057: track heartbeat freshness instead of treating it
            # as a no-op. Operators querying ``client_last_seen`` can
            # detect stalled connections without polling the OS layer.
            self._client_last_seen[ws] = time.time()
            return True
        if message_type == "control_cancel_request":
            return await self._handle_control_cancel(data, ws)
        if message_type == "control_request":
            return await self._dispatch_control_request(data, ws)
        if message_type == "update_environment_variables":
            return await self._handle_update_environment_variables_message(data, ws)

        action = data.get("action")
        request_id = data.get("request_id") if isinstance(data.get("request_id"), str) else None
        if not action:
            logger.debug("WebSocket message missing 'action' field (chars=%d)", len(message))
            if request_id:
                await self._send_control_error(ws, request_id, "WebSocket message missing 'action' field")
            return False

        handler = self._command_handlers.get(action)
        if not handler:
            logger.debug("No handler registered for action: %s", action)
            # F-058: a missing handler is a fail-closed condition for
            # SDK clients. Surface a typed error rather than silently
            # returning False.
            if request_id:
                await self._send_control_error(ws, request_id, "No handler registered for action: %s" % action)
            else:
                await _send_json_safe(
                    ws,
                    {
                        "type": "error",
                        "error": {
                            "code": "no_handler",
                            "action": action,
                            "message": "No handler registered for action: %s" % action,
                        },
                    },
                )
            return False

        payload = data.get("payload", {})
        # Validate payload is a dict - handlers expect .get() method
        if not isinstance(payload, dict):
            logger.warning(
                "WebSocket message has non-dict payload for action %s: %s",
                action,
                type(payload).__name__,
            )
            payload = {}  # Use empty dict as fallback
        try:
            await handler(action, payload, ws)
            logger.debug("Dispatched WebSocket command: %s", action)
            return True
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError, KeyError) as exc:
            logger.exception("Error handling WebSocket command %s: %s", action, exc)
            # F-058: emit an error envelope to the client.
            error_text = "%s: %s" % (type(exc).__name__, exc)
            if request_id:
                await self._send_control_error(ws, request_id, error_text)
            else:
                await _send_json_safe(
                    ws,
                    {
                        "type": "error",
                        "error": {
                            "code": "handler_exception",
                            "action": action,
                            "message": error_text,
                        },
                    },
                )
            return False

    async def _handle_control_cancel(self, data: dict[str, Any], ws: WebSocket) -> bool:
        """F-057: cancel a specific in-flight control_request by id.

        Returns True even if the request_id is not in flight -- the
        cancel is idempotent. Emits a cancellation control_response so
        the SDK client knows the request will never complete.
        """
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            logger.debug("control_cancel_request missing request_id; ignoring")
            return True
        request_id = request_id.strip()
        task = self._inflight_controls.pop(request_id, None)
        target_ws = self._inflight_control_ws.pop(request_id, ws)
        if task is not None and not task.done():
            task.cancel()
            logger.debug("Cancelled in-flight control_request %s", request_id)
        # Emit cancellation acknowledgement on the originating WS.
        await _send_json_safe(
            target_ws,
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": "cancelled",
                },
            },
        )
        return True

    def get_client_last_seen(self, ws: WebSocket) -> float | None:
        """Return the most recent keep_alive/control timestamp for this WS."""
        return self._client_last_seen.get(ws)

    def stale_clients(self, max_age_seconds: float) -> list[WebSocket]:
        """F-057: list clients whose last keep_alive/control is older than threshold.

        Background tasks can call this to proactively disconnect stale
        spokes instead of waiting for the next send timeout. Clients
        that have never sent keep_alive (e.g. read-only listeners) are
        excluded.
        """
        cutoff = time.time() - max_age_seconds
        return [ws for ws, ts in self._client_last_seen.items() if ts < cutoff]

    def inflight_control_request_ids(self) -> list[str]:
        """F-057: snapshot of currently in-flight control_request ids."""
        return list(self._inflight_controls.keys())

    # ------------------------------------------------------------------ #
    # F-059 — SDK-compatible envelopes for the main WebSocket             #
    # ------------------------------------------------------------------ #
    #
    # The legacy broadcast shape is ``{"type": event_type, "payload": ...}``
    # with no request correlation. SDK / Claude-compatible clients need a
    # top-level envelope that carries ``request_id`` for responses and
    # progress events, and a headered binary frame for binary request
    # streams. These methods sit beside the legacy broadcasters; callers
    # that want SDK shape opt in by name. The companion bridge keeps its
    # own framing -- it is product-specific.

    async def broadcast_sdk_response(
        self,
        request_id: str,
        payload: dict[str, Any],
        *,
        user_id: str,
        progress: bool = False,
    ) -> int:
        """Send a request-correlated SDK envelope to one tenant's clients.

        ``progress=True`` emits an ``sdk_progress`` envelope; ``False``
        emits ``sdk_response``. The shape is top-level with a
        ``request_id`` field so the SDK host can correlate streamed
        chunks with the originating request.
        """
        if not request_id or not isinstance(request_id, str):
            raise ValueError("request_id is required for SDK envelopes")
        envelope_type = "sdk_progress" if progress else "sdk_response"
        envelope: dict[str, Any] = {
            "type": envelope_type,
            "request_id": request_id,
            "response": payload,
        }
        async with self._lock:
            clients = list(self._user_clients.get(user_id, set())) if user_id is not None else []
        sent = 0
        for ws in clients:
            self._enqueue_to_client(ws, envelope)
            sent += 1
        return sent

    async def broadcast_sdk_binary(
        self,
        request_id: str,
        data: bytes,
        *,
        user_id: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Send a headered SDK binary frame for a request stream.

        The frame mirrors the companion bridge's ``CompanionBinaryFrame``
        layout (4-byte big-endian header length + JSON header + raw
        bytes). Companion bridge framing stays product-specific; this
        helper makes the main hub use the same wire shape so SDK
        clients connected to /ws can decode both bridges identically.
        """
        from services.companion.protocol import CompanionBinaryFrame

        frame = CompanionBinaryFrame(
            type="sdk_response",
            payload=data,
            request_id=request_id,
            content_type=content_type,
            metadata=metadata or {},
        ).pack()
        async with self._lock:
            clients = list(self._user_clients.get(user_id, set())) if user_id is not None else []
        sent = 0
        for ws in clients:
            try:
                await asyncio.wait_for(ws.send_bytes(frame), timeout=_WS_SEND_TIMEOUT)
                sent += 1
            except TimeoutError:
                logger.warning("SDK binary send timed out for client %s", id(ws))
            except (RuntimeError, OSError, AttributeError):
                logger.debug("SDK binary send failed for client %s", id(ws))
        return sent

    async def _dispatch_control_request(self, data: dict[str, Any], ws: WebSocket) -> bool:
        request_id = data.get("request_id")
        request = data.get("request")
        if not isinstance(request_id, str) or not request_id.strip():
            logger.warning("control_request missing request_id")
            return False
        request_id = request_id.strip()
        if not isinstance(request, dict):
            await self._send_control_error(ws, request_id, "control_request.request must be an object")
            return True

        subtype = request.get("subtype")
        if not isinstance(subtype, str) or not subtype.strip():
            await self._send_control_error(ws, request_id, "control_request.request.subtype is required")
            return True
        subtype = subtype.strip()
        if subtype == "can_use_tool" and not str(request.get("tool_use_id") or request.get("toolUseID") or "").strip():
            await _send_json_safe(
                ws,
                PermissionDecision.deny(
                    "can_use_tool.tool_use_id is required",
                    decision_reason="bad_request",
                ).to_control_response(request_id),
            )
            return True
        self._client_last_seen[ws] = time.time()

        # F-057: register the in-flight task so control_cancel_request
        # can cancel a specific request_id.
        task = asyncio.create_task(self._handle_control_subtype(subtype, request, ws))
        self._inflight_controls[request_id] = task
        self._inflight_control_ws[request_id] = ws
        try:
            response = await task
        except asyncio.CancelledError:
            # The cancel handler already emitted the error envelope.
            return True
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError, KeyError) as exc:
            logger.exception("Error handling control_request %s: %s", subtype, exc)
            await self._send_control_error(ws, request_id, str(exc))
            return True
        finally:
            self._inflight_controls.pop(request_id, None)
            self._inflight_control_ws.pop(request_id, None)

        if isinstance(response, PermissionDecision):
            if subtype == "can_use_tool":
                response = response.with_tool_use_id(
                    str(request.get("tool_use_id") or request.get("toolUseID") or "").strip()
                )
            await _send_json_safe(ws, response.to_control_response(request_id))
            return True
        if response is None:
            await self._send_control_success(ws, request_id)
            return True
        await self._send_control_success(ws, request_id, response)
        return True

    async def _handle_control_subtype(
        self,
        subtype: str,
        request: dict[str, Any],
        ws: WebSocket,
    ) -> dict[str, Any] | PermissionDecision | None:
        handler = self._control_handlers.get(subtype)
        if handler is not None:
            return await handler(subtype, request, ws)

        if subtype == "initialize":
            return self._initialize_response(ws)
        if subtype == "interrupt":
            return await self._control_interrupt(ws)
        if subtype == "can_use_tool":
            # F-025: evaluate scoped permission rules instead of a
            # blanket deny. Falls back to the legacy deny when the
            # plugin permission store can't be loaded (test isolation).
            return self._can_use_tool_decision(request)
        if subtype == "mcp_status":
            return self._unsupported_response(
                subtype,
                "MCP status is not wired; refusing to report an empty inventory as live status",
            )
        if subtype == "mcp_message":
            return self._unsupported_response(subtype, "MCP relay is not yet implemented")
        if subtype == "mcp_set_servers":
            return self._unsupported_response(subtype, "MCP server registration is not yet implemented")
        if subtype == "mcp_reconnect":
            return self._unsupported_response(subtype, "MCP reconnect is not yet implemented")
        if subtype == "mcp_toggle":
            return self._unsupported_response(subtype, "MCP toggle is not yet implemented")
        if subtype == "get_context_usage":
            return self._empty_context_usage()
        if subtype == "reload_plugins":
            return self._reload_plugins_control_response(request)
        if subtype == "set_permission_mode":
            return self._set_permission_mode(request)
        if subtype == "set_model":
            return self._unsupported_runtime_mutator(subtype)
        if subtype == "set_max_thinking_tokens":
            return self._unsupported_runtime_mutator(subtype)
        if subtype == "get_settings":
            return self._get_settings_response()
        if subtype == "apply_flag_settings":
            return self._apply_flag_settings(request)
        if subtype == "update_environment_variables":
            return self._unsupported_runtime_mutator(subtype)
        if subtype == "stop_task":
            task_id = str(request.get("task_id") or request.get("taskId") or "").strip()
            if not task_id:
                raise RuntimeError("stop_task.task_id is required")
            legacy_handler = self._command_handlers.get("agent_cancel")
            if legacy_handler is not None:
                cancel_result = await legacy_handler("agent_cancel", {"agent_id": task_id}, ws)
                stopped = cancel_result is True
                if isinstance(cancel_result, dict):
                    stopped = bool(cancel_result.get("stopped") or cancel_result.get("cancelled"))
                return {"stopped": stopped, "task_id": task_id}
            return self._unsupported_response(subtype, "stop_task requires an agent_cancel handler")
        if subtype == "cancel_async_message":
            request_id = request.get("request_id")
            if isinstance(request_id, str):
                inflight = self._inflight_controls.pop(request_id, None)
                if inflight is not None and not inflight.done():
                    inflight.cancel()
                    self._inflight_control_ws.pop(request_id, None)
                    return {"cancelled": True}
            return {"cancelled": False}
        if subtype in {"rewind_files", "seed_read_state", "hook_callback", "elicitation"}:
            return self._unsupported_response(subtype, "%s is not yet implemented" % subtype)
        if subtype in CLAUDE_CONTROL_SUBTYPES:
            return self._unsupported_response(subtype, "%s has no registered handler" % subtype)

        raise RuntimeError(f"Unsupported control_request subtype: {subtype}")

    def _initialize_response(self, _ws: WebSocket) -> dict[str, Any]:
        return {
            "commands": [],
            "output_style": "normal",
            "available_output_styles": ["normal"],
            "models": [],
            "account": {},
            "pid": os.getpid(),
            "session_state": {},
            "supported_subtypes": sorted(CLAUDE_CONTROL_SUBTYPES),
        }

    async def _control_interrupt(self, ws: WebSocket) -> None:
        legacy_handler = self._command_handlers.get("agent_cancel")
        if legacy_handler is None:
            raise RuntimeError("interrupt is not supported without an agent_cancel handler")
        await legacy_handler("agent_cancel", {}, ws)
        return None

    @staticmethod
    def _unsupported_response(subtype: str, message: str) -> dict[str, Any]:
        return {
            "subtype": subtype,
            "supported": False,
            "message": message,
        }

    def _can_use_tool_decision(self, request: dict[str, Any]) -> PermissionDecision:
        tool_use_id = str(request.get("tool_use_id") or request.get("toolUseID") or "").strip()
        if not tool_use_id:
            return PermissionDecision.deny(
                "can_use_tool.tool_use_id is required",
                decision_reason="bad_request",
            )
        try:
            from plugins.singleton import get_plugin_manager

            manager = get_plugin_manager()
            pm = manager.permission_manager
        except (ImportError, RuntimeError, AttributeError) as exc:
            logger.warning("Permission manager unavailable for can_use_tool: %s", exc)
            return PermissionDecision.deny(
                "No SDK permission handler is registered for this WebSocket.",
                decision_reason="unsupported",
                tool_use_id=tool_use_id,
            )
        tool_name = str(request.get("tool_name") or request.get("toolName") or "").strip()
        if "input" in request:
            tool_input = request["input"]
        elif "tool_input" in request:
            tool_input = request["tool_input"]
        elif "toolInput" in request:
            tool_input = request["toolInput"]
        else:
            tool_input = {}
        plugin_name = request.get("plugin_name") or request.get("pluginName")
        rule_content = request.get("rule_content") or request.get("ruleContent")
        if not tool_name:
            return PermissionDecision.deny(
                "can_use_tool.tool_name is required",
                decision_reason="bad_request",
                tool_use_id=tool_use_id,
            )
        if not isinstance(tool_input, dict):
            return PermissionDecision.deny(
                "can_use_tool.input must be an object",
                decision_reason="bad_request",
                tool_use_id=tool_use_id,
            )
        return pm.decide_tool_use(
            tool_name=tool_name,
            tool_input=tool_input,
            plugin_name=plugin_name if isinstance(plugin_name, str) else None,
            rule_content=rule_content if isinstance(rule_content, str) else None,
            tool_use_id=tool_use_id,
        )

    def _set_permission_mode(self, request: dict[str, Any]) -> dict[str, Any]:
        mode = str(request.get("mode") or request.get("permission_mode") or "").strip()
        try:
            from plugins.singleton import get_plugin_manager

            get_plugin_manager().permission_manager.set_permission_mode(mode)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        except (ImportError, RuntimeError, AttributeError) as exc:
            raise RuntimeError(
                "Could not set permission mode: %s" % exc,
            ) from exc
        return {"mode": mode}

    def _unsupported_runtime_mutator(self, subtype: str) -> dict[str, Any]:
        return self._unsupported_response(
            subtype,
            "%s is not wired into subsequent runtime turns; refusing to acknowledge an inert mutation" % subtype,
        )

    async def _handle_update_environment_variables_message(self, data: dict[str, Any], ws: WebSocket) -> bool:
        await _send_json_safe(
            ws,
            {
                "type": "update_environment_variables_response",
                "response": self._unsupported_runtime_mutator("update_environment_variables"),
            },
        )
        if not isinstance(data.get("variables"), dict):
            logger.debug("update_environment_variables missing variables object")
        return True

    @staticmethod
    def _redact_sdk_setting_values(settings: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(settings)
        for key, value in redacted.items():
            if (
                isinstance(value, str)
                and value
                and (key in {"llm_api_key", "openai_api_key"} or key.endswith(("_token", "_api_key", "_access_token")))
            ):
                redacted[key] = "******"
        return redacted

    @staticmethod
    def _applied_settings_from_effective(effective: dict[str, Any]) -> dict[str, Any] | None:
        model = str(effective.get("agent_model") or effective.get("llm_model") or "").strip()
        if not model:
            return None
        raw_effort = str(
            effective.get("agent_reasoning_effort") or effective.get("reasoning_effort") or "",
        ).strip()
        effort = raw_effort if raw_effort in {"low", "medium", "high", "max"} else None
        if raw_effort == "xhigh":
            effort = "max"
        return {"model": model, "effort": effort}

    def _get_settings_response(self) -> dict[str, Any]:
        user_settings: dict[str, Any] = {}
        try:
            from ui.settings_manager import get_settings_manager

            settings_obj = getattr(get_settings_manager(), "settings", {})
            if isinstance(settings_obj, dict):
                user_settings = self._redact_sdk_setting_values(settings_obj)
        except (ImportError, RuntimeError, AttributeError, TypeError, OSError) as exc:
            logger.debug("SDK get_settings could not read SettingsManager: %s", exc)

        flag_settings = dict(self._sdk_flag_settings)
        effective = dict(user_settings)
        effective.update(flag_settings)
        response: dict[str, Any] = {
            "effective": effective,
            "sources": [
                {"source": "userSettings", "settings": user_settings},
                {"source": "flagSettings", "settings": flag_settings},
            ],
        }
        applied = self._applied_settings_from_effective(effective)
        if applied is not None:
            response["applied"] = applied
        return response

    def _apply_flag_settings(self, request: dict[str, Any]) -> dict[str, Any]:
        if "settings" in request:
            flags = request["settings"]
        elif "flag_settings" in request:
            flags = request["flag_settings"]
        elif "flags" in request:
            flags = request["flags"]
        else:
            raise RuntimeError("apply_flag_settings.settings must be an object")
        if not isinstance(flags, dict):
            raise RuntimeError("apply_flag_settings.settings must be an object")
        self._sdk_flag_settings.update(flags)
        return self._get_settings_response()

    @staticmethod
    def _reload_plugins_control_response(request: dict[str, Any]) -> dict[str, Any]:
        from plugins.singleton import get_plugin_manager

        raw_name = request.get("name") or request.get("plugin")
        name = str(raw_name).strip() if raw_name is not None else None
        manager = get_plugin_manager()
        reload_tagged = getattr(manager, "reload_tagged", None)
        if callable(reload_tagged):
            results = reload_tagged(name or None)
        else:
            results = manager.reload(name or None)

        error_count = 0
        plugins: list[dict[str, str]] = []
        for plugin_name, status in sorted(results.items()):
            if status != "reloaded":
                error_count += 1
                continue
            plugin_entry = EventHub._sdk_plugin_response_entry(manager, plugin_name)
            if plugin_entry is None:
                error_count += 1
                continue
            plugins.append(plugin_entry)
        return {
            "commands": [],
            "agents": [],
            "plugins": plugins,
            "mcpServers": [],
            "error_count": error_count,
        }

    @staticmethod
    def _sdk_plugin_response_entry(manager: Any, plugin_name: str) -> dict[str, str] | None:
        records = getattr(manager, "loaded_records", {})
        record = records.get(plugin_name) if isinstance(records, dict) else None
        plugin_path = getattr(record, "plugin_path", None)
        manifest = getattr(record, "manifest", None)
        source = getattr(manifest, "repository", None)

        if plugin_path is None:
            for attr, fallback_source in (("builtin_dir", "builtin"), ("user_dir", "user")):
                base_dir = getattr(manager, attr, None)
                if base_dir is None:
                    continue
                candidate = base_dir / plugin_name
                if candidate.is_dir():
                    plugin_path = candidate
                    source = source or fallback_source
                    break

        if plugin_path is None:
            return None

        entry = {"name": plugin_name, "path": str(plugin_path)}
        if source:
            entry["source"] = str(source)
        return entry

    async def _send_control_success(
        self,
        ws: WebSocket,
        request_id: str,
        response: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "control_response",
            "response": {"subtype": "success", "request_id": request_id},
        }
        if response is not None:
            payload["response"]["response"] = response
        await _send_json_safe(ws, payload)

    async def _send_control_error(self, ws: WebSocket, request_id: str, error: str) -> None:
        await _send_json_safe(
            ws,
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": error,
                },
            },
        )

    @staticmethod
    def _empty_context_usage() -> dict[str, Any]:
        return {
            "categories": [],
            "totalTokens": 0,
            "maxTokens": 0,
            "rawMaxTokens": 0,
            "percentage": 0,
            "gridRows": [],
            "model": "",
            "memoryFiles": [],
            "mcpTools": [],
            "agents": [],
            "isAutoCompactEnabled": False,
            "apiUsage": None,
        }

    # ------------------------------------------------------------------ #
    # Room / User / Device subscription and scoped broadcast              #
    # ------------------------------------------------------------------ #

    async def subscribe_to_room(self, ws: WebSocket, room_id: str) -> None:
        """Subscribe a WebSocket client to a user-scoped room.

        Multi-tenant: the raw ``room_id`` from the client is partitioned
        under the connected user so two tenants choosing the same room
        name do not share a broadcast bucket.  An unauthenticated socket
        cannot subscribe to any room.
        """
        async with self._lock:
            user_id = self._client_user.get(ws)
            if not user_id:
                logger.warning("EventHub.subscribe_to_room refused for unauthenticated socket")
                return
            scoped = _room_scope(user_id, room_id)
            if scoped is None:
                return
            # Remove from previous room if any
            old_room = self._client_room.get(ws)
            if old_room is not None and old_room != scoped:
                old_set = self._room_clients.get(old_room)
                if old_set is not None:
                    old_set.discard(ws)
                    if not old_set:
                        del self._room_clients[old_room]

            self._client_room[ws] = scoped
            self._room_clients.setdefault(scoped, set()).add(ws)
        logger.debug("WebSocket subscribed to room %s", scoped)

    async def unsubscribe_from_room(self, ws: WebSocket) -> None:
        """Unsubscribe a WebSocket client from its current room."""
        async with self._lock:
            room_id = self._client_room.pop(ws, None)
            if room_id is not None:
                room_set = self._room_clients.get(room_id)
                if room_set is not None:
                    room_set.discard(ws)
                    if not room_set:
                        del self._room_clients[room_id]

    async def set_client_user(self, ws: WebSocket, user_id: str) -> None:
        """Associate a WebSocket client with a user ID."""
        async with self._lock:
            old_user = self._client_user.get(ws)
            if old_user is not None and old_user != user_id:
                old_set = self._user_clients.get(old_user)
                if old_set is not None:
                    old_set.discard(ws)
                    if not old_set:
                        del self._user_clients[old_user]

            self._client_user[ws] = user_id
            self._user_clients.setdefault(user_id, set()).add(ws)

    async def set_client_device(self, ws: WebSocket, device_id: str) -> None:
        """Associate a WebSocket client with a device ID."""
        async with self._lock:
            old_device = self._client_device.get(ws)
            if old_device is not None and old_device != device_id:
                old_set = self._device_clients.get(old_device)
                if old_set is not None:
                    old_set.discard(ws)
                    if not old_set:
                        del self._device_clients[old_device]

            self._client_device[ws] = device_id
            self._device_clients.setdefault(device_id, set()).add(ws)

    async def get_subscribed_room_ids(self, user_id: str | None = None) -> list[str]:
        """Return subscribed room ids.

        Multi-tenant: the raw room ids stored internally are scoped
        ``user_id:room`` keys.  When called with ``user_id`` the helper
        returns the *raw* room names the user has subscribed to (no
        leading ``user_id:`` prefix).  Without ``user_id`` we return the
        full scoped keys — admin/diagnostic callers must filter by user
        themselves.
        """
        async with self._lock:
            scoped_ids = [rid for rid, clients in self._room_clients.items() if clients]
        if user_id is None:
            return scoped_ids
        prefix = "%s:" % user_id
        return [rid[len(prefix) :] for rid in scoped_ids if rid.startswith(prefix)]

    async def broadcast_to_room(
        self,
        room_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        user_id: str | None = None,
    ) -> int:
        """
        Broadcast an event to all WebSocket clients subscribed to a room.

        Multi-tenant: ``user_id`` is REQUIRED to disambiguate the room.
        ``room_id`` is the raw room name the user knows about (e.g.
        ``"kitchen"``); the hub combines it with ``user_id`` to find the
        scoped bucket.  A userless call is refused.

        Args:
            room_id: Raw room name (no user prefix).
            event_type: Type of event.
            payload: Event payload.
            user_id: Owner of the room.

        Returns:
            Number of clients the message was successfully sent to.
        """
        if user_id is None:
            logger.warning(
                "EventHub.broadcast_to_room refused userless broadcast for room %s",
                room_id,
            )
            return 0
        scoped = _room_scope(user_id, room_id)
        if scoped is None:
            return 0
        async with self._lock:
            clients = list(self._room_clients.get(scoped, set()))

        if not clients:
            return 0

        sent_count = 0
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await asyncio.wait_for(
                    _send_json_safe(ws, {"type": event_type, "payload": payload}),
                    timeout=_WS_SEND_TIMEOUT,
                )
                sent_count += 1
            except TimeoutError:
                logger.warning(
                    "WebSocket room send timed out after %ss, dropping client",
                    _WS_SEND_TIMEOUT,
                )
                dead.append(ws)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._unmap_client(ws)

        return sent_count

    async def broadcast_binary_to_rooms(self, data: bytes, *, user_id: str | None = None) -> int:
        """Send binary data to a tenant's room-subscribed WebSocket clients.

        Multi-tenant: ``user_id`` is REQUIRED.  Userless binary fan-out
        sent every connected spoke another tenant's JPEG viewport frames
        in the previous code path; this now fails closed.

        Args:
            data: Raw binary payload (e.g. JPEG bytes).
            user_id: Owner of the target spokes.

        Returns:
            Number of clients the data was successfully sent to.
        """
        if user_id is None:
            logger.warning("EventHub.broadcast_binary_to_rooms refused userless binary fan-out")
            return 0
        # Collect all room-subscribed clients for this tenant only.
        async with self._lock:
            user_clients = self._user_clients.get(user_id, set())
            all_room_clients = [ws for ws in user_clients if ws in self._client_room]

        if not all_room_clients:
            return 0

        sent_count = 0
        dead: list[WebSocket] = []
        for ws in all_room_clients:
            try:
                await asyncio.wait_for(ws.send_bytes(data), timeout=_WS_SEND_TIMEOUT)
                sent_count += 1
            except TimeoutError:
                logger.warning(
                    "WebSocket binary send timed out after %ss, dropping client",
                    _WS_SEND_TIMEOUT,
                )
                dead.append(ws)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._unmap_client(ws)

        return sent_count

    def get_room_spoke_count(self) -> int:
        """Return the total number of room-subscribed clients (spokes).

        Non-async for use from synchronous contexts (e.g. frame streamer
        FPS adjustment).  Reads ``_room_clients`` without locking — this is
        acceptable because it is advisory (for FPS backpressure) and the
        dict values are sets that are replaced atomically by the lock-holding
        methods.
        """
        count = 0
        for clients in self._room_clients.values():
            count += len(clients)
        return count

    async def broadcast_to_user(self, user_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """
        Broadcast an event to all WebSocket clients for a user.

        Args:
            user_id: Target user identifier.
            event_type: Type of event.
            payload: Event payload.

        Returns:
            Number of clients the message was successfully sent to.
        """
        async with self._lock:
            clients = list(self._user_clients.get(user_id, set()))

        if not clients:
            return 0

        sent_count = 0
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await asyncio.wait_for(
                    _send_json_safe(ws, {"type": event_type, "payload": payload}),
                    timeout=_WS_SEND_TIMEOUT,
                )
                sent_count += 1
            except TimeoutError:
                logger.warning(
                    "WebSocket user send timed out after %ss, dropping client",
                    _WS_SEND_TIMEOUT,
                )
                dead.append(ws)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._unmap_client(ws)

        return sent_count

    async def broadcast_to_device(
        self,
        device_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        user_id: str,
    ) -> int:
        """
        Broadcast an event to all WebSocket clients for a device, scoped to a user.

        ``user_id`` is REQUIRED keyword-only. The method filters
        ``self._device_clients[device_id]`` against ``self._client_user`` so
        only clients authenticated as ``user_id`` receive the event. Same
        per-user filter pattern as the sibling
        ``backend.cloud_runtime.CloudRuntime._send_device_local`` and the
        sibling ``broadcast_to_user`` / ``broadcast_to_room`` /
        ``broadcast_binary_to_rooms`` methods on this class.

        Hardened 2026-05-30 per the cross-user-isolation adversarial audit:
        ``_device_clients[device_id]`` is a globally-keyed dict and the prior
        ``broadcast_to_device`` had no ``user_id`` filter — a future caller
        passing a ``device_id`` whose registered clients span multiple users
        would have leaked the event cross-user. Zero production callers
        existed at the time of the fix (verified via repo-wide grep); making
        the parameter required prevents the regression-by-future-caller.

        Args:
            device_id: Target device identifier.
            event_type: Type of event.
            payload: Event payload.
            user_id: Authenticated user_id of the caller; only clients
                whose ``self._client_user[ws] == user_id`` receive the event.

        Returns:
            Number of clients the message was successfully sent to.
        """
        async with self._lock:
            device_clients = list(self._device_clients.get(device_id, set()))
            clients = [ws for ws in device_clients if self._client_user.get(ws) == user_id]

        if not clients:
            return 0

        sent_count = 0
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await asyncio.wait_for(
                    _send_json_safe(ws, {"type": event_type, "payload": payload}),
                    timeout=_WS_SEND_TIMEOUT,
                )
                sent_count += 1
            except TimeoutError:
                logger.warning(
                    "WebSocket device send timed out after %ss, dropping client",
                    _WS_SEND_TIMEOUT,
                )
                dead.append(ws)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._unmap_client(ws)

        return sent_count
