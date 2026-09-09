"""Port resolution and allocation utilities for backend server.

This module provides utilities for resolving available ports for the backend
server, including preference ordering, ephemeral port fallback, and runtime
port configuration.
"""

from __future__ import annotations

import contextlib
import os
import socket
from typing import Any, cast

from core.constants import DEFAULT_API_PORT, LOCALHOST, TIMEOUT_SHORT
from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from config.settings import settings
except Exception as e:  # pragma: no cover

    class _Settings:
        host: str = LOCALHOST
        port: int = DEFAULT_API_PORT

    settings = cast(Any, _Settings())
    logger.warning("config.settings unavailable, using defaults: %s", e)


class PortSelectionError(RuntimeError):
    """Raised when no suitable listen port can be selected."""


def _port_is_free(host: str, port: int) -> bool:
    """Return True if a TCP bind to host:port succeeds."""
    if port <= 0 or port > 65535:
        return False
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(TIMEOUT_SHORT)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError as e:
            logger.exception("Port %s:%d is not free: %s", host, port, e)
            return False
    return True


def _read_env_port() -> tuple[int | None, str | None]:
    """Return explicit port override from environment if present."""
    for key in ("VIOLA_API_PORT", "VIOLA_PORT"):
        raw = os.environ.get(key)
        if not raw:
            continue
        try:
            return int(raw), key
        except ValueError:
            logger.warning("Ignoring invalid %s value %r. Expected an integer TCP port.", key, raw)
    return None, None


def _apply_port_runtime(host: str, port: int) -> None:
    """Propagate selected port/host to config settings."""
    try:
        settings.api_host = host
        settings.api_port = port
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Unable to update config settings with selected port: %s", exc)


def resolve_listen_port(
    host: str,
    requested_port: int,
    **_kwargs: object,
) -> int:
    """
    Choose a TCP port for the Viola backend.

    Tries exactly ONE port (env override or requested). If occupied, raises
    PortSelectionError. The SingleInstanceGuard catches duplicate instances
    before this function is called, so a busy port means a non-Viola process.
    """
    env_port, env_key = _read_env_port()
    port = env_port if env_port is not None else requested_port

    if _port_is_free(host, port):
        if env_port is not None:
            logger.info("Using port %d from %s.", port, env_key)
        else:
            logger.info("Port %d is available for the Viola backend.", port)
        _apply_port_runtime(host, port)
        return port

    raise PortSelectionError(
        f"Port {port} is already in use. Is another Viola instance running? "
        "Kill the blocking process or set VIOLA_API_PORT to a free port."
    )


__all__ = ["PortSelectionError", "resolve_listen_port"]
