"""Stream Session Manager — token-based lifecycle for browser streaming.

Manages the mapping from secure URL tokens to BrowserStreamBridge instances.
Enforces TTL (15 min), single concurrent connection per token, single-use
(token invalidated on disconnect or expiry), and periodic cleanup.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import time
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from playwright.async_api import Page

    from services.browser.stream_bridge import BrowserStreamBridge

logger = get_logger(__name__)

_DEFAULT_TTL = 900  # 15 minutes
_CLEANUP_INTERVAL = 60  # Sweep every minute


# -----------------------------------------------------------------------
# Internal session record
# -----------------------------------------------------------------------


class _StreamSession:
    """Internal state for one streaming session."""

    __slots__ = ("bridge", "connected", "created_at", "metadata", "token", "ttl", "user_id")

    def __init__(
        self,
        token: str,
        bridge: BrowserStreamBridge,
        metadata: dict[str, Any],
        ttl: float,
        user_id: str,
    ) -> None:
        self.token = token
        self.bridge = bridge
        self.metadata = metadata
        self.created_at = time.monotonic()
        self.ttl = ttl
        self.connected = False
        self.user_id = user_id


# -----------------------------------------------------------------------
# StreamSessionManager
# -----------------------------------------------------------------------


class StreamSessionManager:
    """Manages browser streaming sessions."""

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._sessions: dict[str, _StreamSession] = {}
        self._ttl = ttl
        self._cleanup_task: asyncio.Task[None] | None = None

    # -- public API ------------------------------------------------------

    async def create_session(
        self,
        page: Page,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> str:
        """Create a new streaming session and return the access token.

        Starts the CDP screencast immediately.
        """
        from services.browser.stream_bridge import BrowserStreamBridge

        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id is required for browser stream sessions")
        token = secrets.token_urlsafe(32)
        bridge = BrowserStreamBridge(page)
        session = _StreamSession(
            token=token,
            bridge=bridge,
            metadata=metadata or {},
            ttl=self._ttl,
            user_id=normalized_user_id,
        )
        self._sessions[token] = session

        # Ensure the cleanup loop is running
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.ensure_future(self._cleanup_loop())

        # Start the screencast
        await bridge.start(token)
        logger.info("Stream session created: %s for user %s", token[:8], session.user_id)
        return token

    def get_session(self, token: str) -> BrowserStreamBridge | None:
        """Return the bridge for a valid, unexpired token.  ``None`` otherwise."""
        session = self._sessions.get(token)
        if session is None:
            return None
        if self._is_expired(session):
            asyncio.ensure_future(self._destroy(token))
            return None
        return session.bridge

    def get_session_user_id(self, token: str) -> str | None:
        """Return the owning user_id for a valid streaming session."""
        session = self._sessions.get(token)
        if session is None or self._is_expired(session):
            return None
        return session.user_id

    def mark_connected(self, token: str) -> bool:
        """Mark session as having an active WebSocket connection.

        Returns ``False`` if the session does not exist or already has a
        connection (one concurrent viewer enforced).
        """
        session = self._sessions.get(token)
        if session is None:
            return False
        if session.connected:
            return False
        session.connected = True
        return True

    def mark_disconnected(self, token: str) -> None:
        """Mark session as disconnected.  Single-use: session is destroyed."""
        session = self._sessions.get(token)
        if session is not None:
            session.connected = False
            asyncio.ensure_future(self._destroy(token))

    async def destroy_session(self, token: str) -> None:
        """Explicitly destroy a session (stop bridge, remove mapping)."""
        await self._destroy(token)

    async def shutdown(self) -> None:
        """Destroy all sessions (app shutdown)."""
        tokens = list(self._sessions.keys())
        for token in tokens:
            await self._destroy(token)
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()

    # -- internal --------------------------------------------------------

    async def _destroy(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session is None:
            return
        try:
            await session.bridge.stop()
        except Exception:
            logger.debug("Bridge stop failed for session %s", token[:8])
        logger.info("Stream session destroyed: %s", token[:8])

    def _is_expired(self, session: _StreamSession) -> bool:
        return (time.monotonic() - session.created_at) > session.ttl

    async def _cleanup_loop(self) -> None:
        """Periodically sweep expired sessions."""
        while self._sessions:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            expired = [tok for tok, sess in self._sessions.items() if self._is_expired(sess)]
            for tok in expired:
                await self._destroy(tok)


# -----------------------------------------------------------------------
# Module-level singleton
# -----------------------------------------------------------------------

_instance: StreamSessionManager | None = None


def get_stream_session_manager() -> StreamSessionManager:
    """Return (or create) the module-level session manager singleton."""
    global _instance
    if _instance is None:
        _instance = StreamSessionManager()
    return _instance


# -----------------------------------------------------------------------
# URL builder
# -----------------------------------------------------------------------


def build_stream_url(token: str) -> str:
    """Build the user-facing stream URL using the LAN IP and API port.

    Falls back to ``localhost`` if the LAN IP cannot be determined.
    """
    from config.settings import settings

    port = settings.api_port
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
        sock.close()
    except Exception:
        host = "localhost"
    return "http://%s:%d/stream/%s" % (host, port, token)
