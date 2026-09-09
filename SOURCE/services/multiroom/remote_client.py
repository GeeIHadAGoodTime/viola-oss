"""
Remote Room Client for Multi-Room Playback Control.

Provides an async HTTP client for communicating with remote Viola instances
on the local network. Each RemoteRoomClient represents a connection to a
single remote device and exposes playback control, volume, and state methods.

All methods are safe to call -- they catch exceptions internally, log failures,
and return None (or False for health_check) rather than raising.

Usage:
    >>> client = RemoteRoomClient(host="192.168.1.42", port=8756)
    >>> state = await client.get_state()
    >>> await client.play("Bohemian Rhapsody", source="spotify")
    >>> await client.set_volume(75)
"""

from __future__ import annotations

import httpx

from core.logging_config import get_logger

logger = get_logger(__name__)

# Timeout configuration: 3s connect, 5s read
_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT = 5.0
_TIMEOUT = httpx.Timeout(
    connect=_CONNECT_TIMEOUT,
    read=_READ_TIMEOUT,
    write=_READ_TIMEOUT,
    pool=_READ_TIMEOUT,
)


class RemoteRoomClient:
    """
    Async HTTP client for controlling a remote Viola instance.

    Communicates with the remote device's REST API to issue playback
    commands, adjust volume, and query player state.

    All public methods catch exceptions and return None (or False for
    health_check) on failure, so callers never need to handle HTTP errors.

    Thread Safety:
        This class is safe to use from multiple async tasks. The underlying
        httpx.AsyncClient handles concurrency internally.

    Args:
        host: IP address or hostname of the remote Viola instance.
        port: Port number of the remote Viola API.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._base_url = f"http://{host}:{port}"

    def __repr__(self) -> str:
        return f"RemoteRoomClient(host={self.host!r}, port={self.port!r})"

    # -------------------------------------------------------------------------
    # Playback control
    # -------------------------------------------------------------------------

    async def play(self, query: str, source: str | None = None) -> dict | None:
        """
        Send a play command to the remote device.

        Args:
            query: The search query or track identifier to play.
            source: Optional source/provider name (e.g. "spotify", "local").

        Returns:
            Response dict from the remote API, or None on failure.
        """
        return await self._post("/v1/play", json={"query": query, "source": source})

    async def pause(self) -> dict | None:
        """Pause playback on the remote device."""
        return await self._post("/v1/pause")

    async def resume(self) -> dict | None:
        """Resume playback on the remote device."""
        return await self._post("/v1/resume")

    async def stop(self) -> dict | None:
        """Stop playback on the remote device."""
        return await self._post("/v1/stop")

    async def skip(self) -> dict | None:
        """Skip the current track on the remote device."""
        return await self._post("/v1/skip")

    async def next_track(self) -> dict | None:
        """Advance to the next track on the remote device."""
        return await self._post("/v1/next")

    async def previous(self) -> dict | None:
        """Go to the previous track on the remote device."""
        return await self._post("/v1/previous")

    async def set_volume(self, level: int) -> dict | None:
        """
        Set the volume level on the remote device.

        Args:
            level: Volume level (typically 0-100).

        Returns:
            Response dict from the remote API, or None on failure.
        """
        return await self._post("/v1/volume", json={"level": level})

    # -------------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------------

    async def get_state(self) -> dict | None:
        """
        Fetch the current player state from the remote device.

        Returns:
            Player state dict, or None on failure.
        """
        return await self._get("/v1/player/state")

    async def health_check(self) -> bool:
        """
        Check whether the remote device is reachable and healthy.

        Returns:
            True if the device responded with HTTP 200, False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(f"{self._base_url}/health")
                return response.status_code == 200
        except Exception as exc:
            logger.debug(
                "Health check failed for %s:%d -- %s",
                self.host,
                self.port,
                exc,
            )
            return False

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _post(self, path: str, json: dict | None = None) -> dict | None:
        """
        Send a POST request to the remote device.

        Args:
            path: API path (e.g. "/v1/play").
            json: Optional JSON body.

        Returns:
            Parsed JSON response dict, or None on failure.
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, json=json)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning(
                "POST %s to %s:%d failed -- %s",
                path,
                self.host,
                self.port,
                exc,
            )
            return None

    async def _get(self, path: str) -> dict | None:
        """
        Send a GET request to the remote device.

        Args:
            path: API path (e.g. "/v1/player/state").

        Returns:
            Parsed JSON response dict, or None on failure.
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            logger.warning(
                "GET %s from %s:%d failed -- %s",
                path,
                self.host,
                self.port,
                exc,
            )
            return None


__all__ = [
    "RemoteRoomClient",
]
