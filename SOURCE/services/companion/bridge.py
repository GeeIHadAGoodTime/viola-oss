from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from core.constants import TIMEOUT_EXTENDED, TIMEOUT_SHORT
from core.logging_config import get_logger
from fastapi import WebSocket, WebSocketDisconnect

from .protocol import (
    CompanionBinaryFrame,
    CompanionMessage,
    message_scope,
    normalize_message_type,
    unsupported_cloud_companion_message_reason,
)
from .registry import (
    _COMMAND_STATUS_FAILED,
    _COMMAND_STATUS_PENDING,
    _COMMAND_STATUS_STREAMING,
    _COMMAND_STATUS_SUCCEEDED,
    _COMMAND_STATUS_TIMED_OUT,
    CompanionCommand,
    CompanionDevice,
    CompanionDeviceRegistry,
    get_companion_device_registry,
)
from .security import (
    CompanionOfflineError,
    CompanionSecurityError,
    CompanionSecurityManager,
    build_audit_details,
    capability_allows,
    normalize_capabilities,
)

logger = get_logger(__name__)

_BRIDGE_SINGLETON: CompanionBridge | None = None


@dataclass(slots=True)
class CompanionConnection:
    user_id: str
    device_id: str
    websocket: WebSocket
    capabilities: dict[str, Any]
    platform: str
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    latency_ms: float | None = None
    version_info: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "user_id": self.user_id,
            "capabilities": self.capabilities,
            "platform": self.platform,
            "connected_at": self.connected_at,
            "last_seen_at": self.last_seen_at,
            "latency_ms": self.latency_ms,
            "version_info": self.version_info,
        }


class CompanionUnsupportedProtocolError(RuntimeError):
    """Raised when a known Claude bridge protocol is intentionally unsupported."""


class CompanionBridge:
    """Tracks live companion sockets and routes persisted commands to them."""

    def __init__(
        self,
        *,
        registry: CompanionDeviceRegistry | None = None,
        security: CompanionSecurityManager | None = None,
    ) -> None:
        self._registry = registry or get_companion_device_registry()
        self._security = security or CompanionSecurityManager()
        self._lock = asyncio.Lock()
        self._connections: dict[str, CompanionConnection] = {}
        self._pending_waiters: dict[str, asyncio.Future[CompanionCommand | None]] = {}
        self._timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self._dispatch_started_at: dict[str, float] = {}

    def set_rate_limiter_redis(self, redis_backend: Any | None) -> None:
        self._security.set_rate_limiter_redis(redis_backend)

    async def assert_ws_invalid_token_limit(self, *, client_ip: str, device_id: str) -> None:
        await self._security.assert_ws_invalid_token_limit(client_ip=client_ip, device_id=device_id)

    async def list_devices(self, *, user_id: str) -> list[dict[str, Any]]:
        devices = await self._registry.list_devices(user_id=user_id)
        async with self._lock:
            snapshots = {device_id: conn.snapshot() for device_id, conn in self._connections.items()}
        return [
            device.to_public_dict(
                online=device.device_id in snapshots,
                latency_ms=snapshots.get(device.device_id, {}).get("latency_ms"),
            )
            for device in devices
        ]

    async def get_connection_snapshot(self, device_id: str, *, user_id: str) -> dict[str, Any] | None:
        async with self._lock:
            connection = self._connections.get(device_id)
            if connection is not None and connection.user_id != user_id:
                return None
            return connection.snapshot() if connection is not None else None

    async def select_device_for_command(
        self,
        *,
        user_id: str,
        message_type: str,
        preferred_device_id: str | None = None,
    ) -> CompanionDevice | None:
        devices = await self._registry.list_devices(user_id=user_id)
        candidates = [d for d in devices if capability_allows(d.capabilities, message_type)]
        if preferred_device_id:
            for device in candidates:
                if device.device_id == preferred_device_id and await self._is_device_online(
                    user_id=user_id,
                    device_id=device.device_id,
                ):
                    return device
            return None

        async with self._lock:
            online = {device_id: self._connections[device_id] for device_id in self._connections}
        candidates = [device for device in candidates if device.device_id in online]
        candidates.sort(
            key=lambda device: (
                (
                    online[device.device_id].latency_ms
                    if online[device.device_id].latency_ms is not None
                    else float("inf")
                ),
                -device.last_heartbeat,
            )
        )
        return candidates[0] if candidates else None

    async def _is_device_online(self, *, user_id: str, device_id: str) -> bool:
        async with self._lock:
            connection = self._connections.get(device_id)
            return connection is not None and connection.user_id == user_id

    async def _assert_device_online(self, *, user_id: str, device_id: str) -> None:
        if await self._is_device_online(user_id=user_id, device_id=device_id):
            return
        raise CompanionOfflineError("Companion device is offline")

    async def send_command(
        self,
        *,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = TIMEOUT_EXTENDED,
        max_attempts: int = 1,
    ) -> CompanionCommand:
        device = await self._registry.get_device(user_id=user_id, device_id=device_id)
        if device is None:
            raise ValueError("Companion device not found")

        normalized_type = normalize_message_type(message_type)
        unsupported_reason = unsupported_cloud_companion_message_reason(normalized_type)
        if unsupported_reason is not None:
            raise CompanionUnsupportedProtocolError(unsupported_reason)

        command_payload = dict(payload or {})
        await self._security.assert_rate_limit(device_id=device_id)
        await self._security.assert_capability(
            capabilities=device.capabilities,
            message_type=normalized_type,
        )
        await self._assert_device_online(user_id=user_id, device_id=device_id)
        await self._security.assert_consent(
            user_id=user_id,
            device_id=device_id,
            message_type=normalized_type,
            payload=command_payload,
        )

        command = await self._registry.create_command(
            user_id=user_id,
            device_id=device_id,
            message_type=normalized_type,
            payload=command_payload,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        await self._registry.append_audit_event(
            user_id=user_id,
            device_id=device_id,
            action="command_created",
            request_id=command.request_id,
            details={"message_type": normalized_type, "payload": command_payload},
        )
        await self._dispatch_if_online(command)
        return command

    async def get_command_result(
        self,
        *,
        user_id: str,
        device_id: str,
        request_id: str,
    ) -> CompanionCommand | None:
        return await self._registry.get_command(
            user_id=user_id,
            device_id=device_id,
            request_id=request_id,
        )

    async def send_command_and_wait(
        self,
        *,
        user_id: str,
        device_id: str,
        message_type: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = TIMEOUT_EXTENDED,
        max_attempts: int = 1,
    ) -> CompanionCommand:
        """Send a command and block until the device replies (or it times out).

        ``send_command`` returns as soon as the command is queued/dispatched;
        callers that need the device's *answer* in the same turn (the
        cloud->desktop relay) use this instead. It registers a waiter future
        in ``_pending_waiters`` BEFORE dispatch, so a result that lands while
        we await is delivered through :meth:`_finish_waiter`.

        The returned :class:`CompanionCommand` is terminal when dispatch
        starts: ``succeeded``, ``failed``, or ``timed_out``. A device that is
        already offline fails fast before a pending command is created; the
        wall-clock guard below covers commands that were accepted for an
        online device but never produce a terminal result.
        """
        command = await self.send_command(
            user_id=user_id,
            device_id=device_id,
            message_type=message_type,
            payload=payload,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
        )
        if command.is_terminal:
            return command

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[CompanionCommand | None] = loop.create_future()
        self._pending_waiters[command.request_id] = waiter
        # Wall-clock guard: _watch_timeout already bounds dispatched commands,
        # but a socket loss or send failure can leave the waiter unresolved.
        guard = max(
            TIMEOUT_SHORT,
            float(timeout_seconds) * float(max(1, max_attempts)) + TIMEOUT_SHORT,
        )
        try:
            resolved = await asyncio.wait_for(waiter, timeout=guard)
        except TimeoutError:
            resolved = None
        finally:
            self._pending_waiters.pop(command.request_id, None)

        if resolved is not None:
            return resolved
        # The waiter never fired (device offline / lost). Surface the latest
        # persisted state; if it is still non-terminal, mark it timed out so
        # the relay caller gets a clean terminal command to fall back from.
        latest = await self._registry.get_command_by_request_id(command.request_id)
        if latest is not None and latest.is_terminal:
            return latest
        timed_out = await self._registry.complete_command(
            request_id=command.request_id,
            status=_COMMAND_STATUS_TIMED_OUT,
            expected_device_id=command.device_id,
            error_text="Companion device did not answer in time",
        )
        return timed_out if timed_out is not None else command

    async def close_device(self, device_id: str, *, code: int = 4001, reason: str = "revoked") -> None:
        websocket: WebSocket | None = None
        async with self._lock:
            connection = self._connections.pop(device_id, None)
            if connection is not None:
                websocket = connection.websocket
        if websocket is not None:
            with contextlib.suppress(Exception):
                await websocket.close(code=code, reason=reason)
        await self._cancel_timeouts_for_device(device_id)

    async def handle_websocket(self, websocket: WebSocket, *, device: CompanionDevice) -> None:
        await websocket.accept()
        previous_websocket: WebSocket | None = None
        async with self._lock:
            previous = self._connections.get(device.device_id)
            if previous is not None and previous.websocket is not websocket:
                previous_websocket = previous.websocket
            self._connections[device.device_id] = CompanionConnection(
                user_id=device.user_id,
                device_id=device.device_id,
                websocket=websocket,
                capabilities=normalize_capabilities(device.capabilities),
                platform=device.platform,
            )
        if previous_websocket is not None:
            with contextlib.suppress(Exception):
                await previous_websocket.close(code=4000, reason="replaced")

        await self._registry.update_heartbeat(
            device_id=device.device_id,
            capabilities=device.capabilities,
        )
        await self._registry.append_audit_event(
            user_id=device.user_id,
            device_id=device.device_id,
            action="device_connected",
            details={"platform": device.platform},
        )
        await self._drain_pending_commands(device.device_id)

        try:
            while True:
                raw = await websocket.receive()
                message_type = raw.get("type")
                if message_type == "websocket.disconnect":
                    break
                if raw.get("text") is not None:
                    await self._handle_text_message(device=device, raw_text=str(raw["text"]))
                    continue
                if raw.get("bytes") is not None:
                    await self._handle_binary_message(device=device, raw_bytes=bytes(raw["bytes"]))
        except WebSocketDisconnect:
            logger.info("Companion websocket disconnected for %s", device.device_id)
        except Exception:
            logger.exception("Companion websocket failed for %s", device.device_id)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="internal_error")
        finally:
            async with self._lock:
                current = self._connections.get(device.device_id)
                if current is not None and current.websocket is websocket:
                    self._connections.pop(device.device_id, None)
            await self._registry.append_audit_event(
                user_id=device.user_id,
                device_id=device.device_id,
                action="device_disconnected",
            )
            await self._registry.requeue_inflight_commands(
                device_id=device.device_id,
                reason="Companion disconnected before finishing command",
            )
            await self._cancel_timeouts_for_device(device.device_id)

    async def _handle_text_message(self, *, device: CompanionDevice, raw_text: str) -> None:
        message = CompanionMessage.from_json(raw_text)
        await self._mark_seen(device.device_id)
        if message.type == "system.capabilities_report":
            await self._handle_capabilities_report(device=device, message=message)
            return
        if message.type == "system.version_info":
            await self._handle_version_info(device=device, message=message)
            return
        if message.type == "system.health_check":
            await self._handle_health_check(device=device, message=message)
            return
        await self._handle_command_message(device=device, message=message)

    async def _handle_binary_message(self, *, device: CompanionDevice, raw_bytes: bytes) -> None:
        frame = CompanionBinaryFrame.unpack(raw_bytes)
        await self._mark_seen(device.device_id)
        if not frame.request_id:
            logger.warning(
                "Discarding companion binary frame without request_id from %s",
                device.device_id,
            )
            return
        if (
            await self._resolve_inbound_command(
                device=device,
                request_id=frame.request_id,
                message_type=frame.type,
            )
            is None
        ):
            return
        payload = {
            "binary": {
                "content_type": frame.content_type,
                "encoding": "base64",
                "data": base64.b64encode(frame.payload).decode("ascii"),
                "metadata": frame.metadata,
            }
        }
        if frame.type == "stream_chunk":
            await self._registry.mark_command_progress(
                request_id=frame.request_id,
                progress_payload=payload,
                user_id=device.user_id,
                device_id=device.device_id,
            )
            return
        completed = await self._registry.complete_command(
            request_id=frame.request_id,
            status=_COMMAND_STATUS_SUCCEEDED,
            expected_device_id=device.device_id,
            result_payload=payload,
            user_id=device.user_id,
            device_id=device.device_id,
        )
        await self._finish_waiter(frame.request_id, completed)

    async def _handle_capabilities_report(
        self,
        *,
        device: CompanionDevice,
        message: CompanionMessage,
    ) -> None:
        capabilities = normalize_capabilities(message.payload.get("capabilities") or {})
        await self._security.consent_store.sync_from_payload(
            user_id=device.user_id,
            device_id=device.device_id,
            grants=message.payload.get("active_consents"),
        )
        updated = await self._registry.update_heartbeat(
            device_id=device.device_id,
            capabilities=capabilities,
        )
        async with self._lock:
            connection = self._connections.get(device.device_id)
            if connection is not None:
                connection.capabilities = capabilities
                latency = message.payload.get("latency_ms")
                connection.latency_ms = float(latency) if latency is not None else connection.latency_ms
        await self._registry.append_audit_event(
            user_id=device.user_id,
            device_id=device.device_id,
            action="capabilities_report",
            details=build_audit_details(message=message, extra={"updated": updated is not None}),
        )

    async def _handle_version_info(
        self,
        *,
        device: CompanionDevice,
        message: CompanionMessage,
    ) -> None:
        async with self._lock:
            connection = self._connections.get(device.device_id)
            if connection is not None:
                connection.version_info = dict(message.payload)
        await self._registry.append_audit_event(
            user_id=device.user_id,
            device_id=device.device_id,
            action="version_info",
            details=build_audit_details(message=message),
        )

    async def _handle_health_check(
        self,
        *,
        device: CompanionDevice,
        message: CompanionMessage,
    ) -> None:
        latency = message.payload.get("latency_ms")
        if latency is not None:
            async with self._lock:
                connection = self._connections.get(device.device_id)
                if connection is not None:
                    connection.latency_ms = float(latency)
        await self._registry.update_heartbeat(device_id=device.device_id)
        await self._registry.append_audit_event(
            user_id=device.user_id,
            device_id=device.device_id,
            action="health_check",
            details=build_audit_details(message=message),
        )

    async def _handle_command_message(
        self,
        *,
        device: CompanionDevice,
        message: CompanionMessage,
    ) -> None:
        if not message.request_id:
            logger.warning("Discarding companion message without request_id: %s", message.type)
            return
        if (
            await self._resolve_inbound_command(
                device=device,
                request_id=message.request_id,
                message_type=message.type,
            )
            is None
        ):
            return
        started = self._dispatch_started_at.pop(message.request_id, None)
        if started is not None:
            latency_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            async with self._lock:
                connection = self._connections.get(device.device_id)
                if connection is not None:
                    connection.latency_ms = latency_ms

        if message.type == "progress" or message.type == "stream_start":
            await self._registry.mark_command_progress(
                request_id=message.request_id,
                progress_payload=message.payload,
                user_id=device.user_id,
                device_id=device.device_id,
            )
            return
        if message.type == "stream_end":
            await self._registry.mark_command_progress(
                request_id=message.request_id,
                progress_payload=message.payload,
                status=_COMMAND_STATUS_STREAMING,
                user_id=device.user_id,
                device_id=device.device_id,
            )
            return
        if message.type == "error":
            completed = await self._registry.complete_command(
                request_id=message.request_id,
                status=_COMMAND_STATUS_FAILED,
                expected_device_id=device.device_id,
                result_payload={},
                error_text=str(message.payload.get("error") or "Companion returned an error"),
                user_id=device.user_id,
                device_id=device.device_id,
            )
            await self._finish_waiter(message.request_id, completed)
            return
        completed = await self._registry.complete_command(
            request_id=message.request_id,
            status=_COMMAND_STATUS_SUCCEEDED,
            expected_device_id=device.device_id,
            result_payload=message.payload,
            user_id=device.user_id,
            device_id=device.device_id,
        )
        await self._finish_waiter(message.request_id, completed)

    async def _resolve_inbound_command(
        self,
        *,
        device: CompanionDevice,
        request_id: str,
        message_type: str,
    ) -> CompanionCommand | None:
        command = await self._registry.get_command(
            user_id=device.user_id,
            device_id=device.device_id,
            request_id=request_id,
        )
        if command is None:
            logger.warning(
                "Discarding companion result for request_id not owned by connected device: device=%s request=%s",
                device.device_id,
                request_id,
            )
            await self._registry.append_audit_event(
                user_id=device.user_id,
                device_id=device.device_id,
                action="command_result_rejected",
                request_id=request_id,
                details={
                    "message_type": message_type,
                    "reason": "request_not_owned_by_device",
                },
            )
            return None
        if command.is_terminal:
            logger.warning(
                "Discarding stale companion result for terminal request_id: device=%s request=%s status=%s",
                device.device_id,
                request_id,
                command.status,
            )
            await self._registry.append_audit_event(
                user_id=device.user_id,
                device_id=device.device_id,
                action="command_result_rejected",
                request_id=request_id,
                details={
                    "message_type": message_type,
                    "reason": "request_already_terminal",
                    "status": command.status,
                },
            )
            return None
        return command

    async def _dispatch_if_online(self, command: CompanionCommand) -> None:
        async with self._lock:
            connection = self._connections.get(command.device_id)
        if connection is None:
            return
        await self._dispatch_command(connection, command)

    async def _dispatch_command(self, connection: CompanionConnection, command: CompanionCommand) -> None:
        message = CompanionMessage(
            type=command.message_type,
            payload=command.payload,
            request_id=command.request_id,
        )
        try:
            await connection.websocket.send_text(message.to_json())
        except Exception:
            logger.exception("Failed to dispatch companion command %s", command.request_id)
            await self._registry.mark_command_progress(
                request_id=command.request_id,
                progress_payload={"dispatch_error": "socket_send_failed"},
                status=_COMMAND_STATUS_PENDING,
                user_id=command.user_id,
                device_id=command.device_id,
            )
            return
        self._dispatch_started_at[command.request_id] = time.monotonic()
        dispatched = await self._registry.mark_command_dispatched(command.request_id)
        await self._registry.append_audit_event(
            user_id=connection.user_id,
            device_id=connection.device_id,
            action="command_dispatched",
            request_id=command.request_id,
            details=build_audit_details(message=message, status=dispatched.status if dispatched else "unknown"),
        )
        self._schedule_timeout(command.request_id, connection.device_id)

    def _schedule_timeout(self, request_id: str, device_id: str) -> None:
        existing = self._timeout_tasks.pop(request_id, None)
        if existing is not None:
            existing.cancel()
        self._timeout_tasks[request_id] = asyncio.create_task(
            self._watch_timeout(request_id=request_id, device_id=device_id)
        )

    async def _watch_timeout(self, *, request_id: str, device_id: str) -> None:
        command = await self._registry.get_command_by_request_id(request_id)
        if command is None:
            return
        try:
            await asyncio.sleep(max(TIMEOUT_SHORT, float(command.timeout_seconds)))
            fresh = await self._registry.get_command_by_request_id(request_id)
            if fresh is None or fresh.is_terminal:
                return
            if fresh.attempt_count < fresh.max_attempts:
                await self._registry.mark_command_progress(
                    request_id=request_id,
                    progress_payload={"retry_reason": "timeout"},
                    status=_COMMAND_STATUS_PENDING,
                    user_id=fresh.user_id,
                    device_id=fresh.device_id,
                )
                await self._dispatch_if_online(fresh)
                return
            timed_out = await self._registry.complete_command(
                request_id=request_id,
                status=_COMMAND_STATUS_TIMED_OUT,
                expected_device_id=device_id,
                error_text="Companion command timed out",
                user_id=fresh.user_id,
                device_id=fresh.device_id,
            )
            await self._finish_waiter(request_id, timed_out)
        except asyncio.CancelledError:
            return
        finally:
            self._timeout_tasks.pop(request_id, None)
            self._dispatch_started_at.pop(request_id, None)
            if command is not None and command.device_id == device_id:
                await self._mark_seen(device_id)

    async def _drain_pending_commands(self, device_id: str) -> None:
        async with self._lock:
            connection = self._connections.get(device_id)
        if connection is None:
            return
        pending = await self._registry.list_pending_commands(device_id=device_id)
        for command in pending:
            await self._dispatch_command(connection, command)

    async def _cancel_timeouts_for_device(self, device_id: str) -> None:
        to_cancel: list[str] = []
        for request_id, task in list(self._timeout_tasks.items()):
            command = await self._registry.get_command_by_request_id(request_id)
            if command is None or command.device_id != device_id:
                continue
            task.cancel()
            to_cancel.append(request_id)
        for request_id in to_cancel:
            self._timeout_tasks.pop(request_id, None)
            self._dispatch_started_at.pop(request_id, None)

    async def _finish_waiter(self, request_id: str, command: CompanionCommand | None) -> None:
        task = self._timeout_tasks.pop(request_id, None)
        if task is not None:
            task.cancel()
        waiter = self._pending_waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(command)

    async def _mark_seen(self, device_id: str) -> None:
        async with self._lock:
            connection = self._connections.get(device_id)
            if connection is not None:
                connection.last_seen_at = time.time()


def get_companion_bridge() -> CompanionBridge:
    global _BRIDGE_SINGLETON
    if _BRIDGE_SINGLETON is None:
        _BRIDGE_SINGLETON = CompanionBridge()
    return _BRIDGE_SINGLETON
