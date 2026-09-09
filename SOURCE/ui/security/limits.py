"""
Resource Limits

Prevents resource exhaustion attacks (DoS).
"""

from __future__ import annotations

import asyncio

from core.logging_config import get_logger
from fastapi import HTTPException, Request, UploadFile, WebSocket

from .config import SecurityConfig, get_security_config

log = get_logger(__name__)


class ResourceLimits:
    """Resource limit enforcement."""

    def __init__(self, config: SecurityConfig | None = None):
        self.config = config or get_security_config()

        # Convert to bytes
        self.max_file_size = self.config.max_file_size_mb * 1024 * 1024
        self.max_request_size = self.config.max_request_size_mb * 1024 * 1024
        self.max_websocket_message_size = self.config.max_websocket_message_size_kb * 1024

        # WebSocket connection tracking (thread-safe)
        self._ws_connections: dict = {}
        self._ws_count = 0
        self._ws_lock = asyncio.Lock()  # Async lock for WebSocket operations

    def validate_file_size(self, file: UploadFile, max_size: int | None = None) -> None:
        """
        Validate file size.

        Args:
            file: UploadFile to validate
            max_size: Optional custom max size (in bytes)

        Raises:
            HTTPException: If file too large
        """
        max_size = max_size or self.max_file_size

        # Check Content-Length header if available
        if hasattr(file, "size") and file.size:
            if file.size > max_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size: {max_size // (1024 * 1024)}MB",
                )

        # Note: FastAPI will read file, but we check before processing
        # For complete protection, use middleware

    async def validate_upload_size(self, request: Request) -> None:
        """Validate request body size (for file uploads)."""
        content_length = request.headers.get("content-length")
        if content_length:
            size = int(content_length)
            if size > self.max_file_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request too large. Maximum size: {self.max_file_size // (1024 * 1024)}MB",
                )

    def validate_request_size(self, request: Request) -> None:
        """Validate request size."""
        content_length = request.headers.get("content-length")
        if content_length:
            size = int(content_length)
            if size > self.max_request_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"Request too large. Maximum size: {self.max_request_size // (1024 * 1024)}MB",
                )

    async def check_websocket_limit(self, websocket: WebSocket) -> bool:
        """
        Check if WebSocket connection limit exceeded (thread-safe).

        Returns:
            True if connection allowed, False if limit exceeded
        """
        async with self._ws_lock:
            if self._ws_count >= self.config.max_websocket_connections:
                log.warning(
                    "WebSocket connection limit exceeded (%s/%s)",
                    self._ws_count,
                    self.config.max_websocket_connections,
                )
                return False
            return True

    async def register_websocket(self, websocket: WebSocket) -> None:
        """Register WebSocket connection (thread-safe)."""
        async with self._ws_lock:
            self._ws_connections[id(websocket)] = websocket
            self._ws_count = len(self._ws_connections)

    async def unregister_websocket(self, websocket: WebSocket) -> None:
        """Unregister WebSocket connection (thread-safe)."""
        async with self._ws_lock:
            if id(websocket) in self._ws_connections:
                del self._ws_connections[id(websocket)]
                self._ws_count = len(self._ws_connections)

    def validate_websocket_message_size(self, message: str) -> None:
        """Validate WebSocket message size."""
        message_size = len(message.encode("utf-8"))
        if message_size > self.max_websocket_message_size:
            raise ValueError(f"Message too large: {message_size} bytes (max: {self.max_websocket_message_size} bytes)")

    def get_websocket_count(self) -> int:
        """Get current WebSocket connection count."""
        return self._ws_count

    def get_websocket_limit(self) -> int:
        """Get WebSocket connection limit."""
        return self.config.max_websocket_connections


# Global limits instance
_resource_limits: ResourceLimits | None = None


def get_resource_limits() -> ResourceLimits:
    """Get global resource limits."""
    global _resource_limits
    if _resource_limits is None:
        _resource_limits = ResourceLimits()
    return _resource_limits
